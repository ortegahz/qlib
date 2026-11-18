"""🔴 V2 下游平滑 —— 决策③「EWMA / Rank Buffer 三臂」的可执行实现。

【背景（用户 2026-11-17 硬约束，原文见
``/mnt/data/siping/runs/etf_rotation_results/codex_overnight/USER_DECISION_20261117.txt``）】
实盘持有周期 = **20 个交易日**，而 V1 的下游 EWMA（``alpha = 0.20``）记忆只有
**3.11 个交易日**（``ln0.5/ln0.8``）⇒ **口径不匹配** ⇒ V2 必须在
**val（2022-01-01 ~ 2023-12-31）** 上比较三臂并**定死一个**，
2024–2026 单盲**只跑最终冻结的那一臂一次**。

三臂（臂 ID 与参数**只**由 ``config_v2`` 给，本模块不写死字面量）：

  ==================  =====================================================
  臂 ID                语义
  ==================  =====================================================
  ``A_half_life_15``   窗口拉长：EWMA 半衰期 = **15** 个交易日
  ``A_half_life_20``   窗口拉长：EWMA 半衰期 = **20** 个交易日
  ``B_native_slow``    **去掉**下游 EWMA ⇒ 直接用 V2 的 20 日 Label 模型
                       **原生慢变量输出**（打分管线原始分数）排序 +
                       Rank Buffer（Rank Buffer 由统一评估引擎
                       ``etf_layer.build_targets`` 分配 ⇒ 与其它臂同一条代码路径）
  ``C_v1_current``     V1 现状对照：``alpha=0.20`` / ``window=5`` /
                       ``history_span=30``（🔴 **不得默认胜出**）
  ==================  =====================================================

🔴 **臂 A 的 alpha 必须由半衰期自算**（``alpha = 1 - 0.5 ** (1 / half_life)``），
   **不许写死字面量**（0.04516 / 0.03406 只是**自证值**，由测试反算校验）。

【EWMA 约定（与 V1 ``score_smoothing.py`` 逐位一致，V1 只读参考）】
  * 递推式 ``state = alpha * x_t + (1 - alpha) * state_{t-1}``
    ⇒ **新观测权重 = alpha**、历史权重 = ``1 - alpha``
    ⇒ **记忆半衰期 = ln(0.5) / ln(1 - alpha) 个交易日**；
  * **冷启动 min_periods = 1**：首个有效观测**即自身**（不预热、不填 0）；
  * 观测先 **LOCF**（末值前向填充）摊到**交易日轴**上 ⇒ 衰减单位是**交易日**
    （不是「打分点」：打分栅格每 10 个交易日一个点，窗口若按点算会差 10 倍）；
  * **历史不足 ⇒ 权重随可得历史动态变化**（第 k 个旧观测的权重 = ``alpha*(1-alpha)**k``，
    累积权重 ``1-(1-alpha)**n`` 随历史增长）⇒ **不许抛 NaN / KeyError**；
  * **全历史不可用 ⇒ 退化为瞬态分**并输出
    ``WARNING: Fallback to transient score``（与 V1 字面量一致）。

【V2 硬隔离】
本模块**不 import V1 任何业务模块**（V2 与 V1 baseline 物理隔离）；
与 V1 的**数值一致性**由 ``v2/tests/test_smoothing_arms.py`` 里的
**只读交叉校验**钉死（测试里才允许只读 import V1 的 ``score_smoothing.py``）。
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402

__all__ = [
    "FALLBACK_WARNING",
    "MIN_HALF_LIVES_PER_SPAN",
    "SMOOTHING_METHODS",
    "SmoothingArm",
    "SmoothResult",
    "alpha_from_half_life",
    "effective_half_life_days",
    "arm_spec",
    "arm_specs",
    "resolve_arm",
    "smooth_panel",
    "apply_spec",
    "apply_arm",
]

#: 全局回退时的日志字面量（**与 V1 ``score_smoothing.FALLBACK_WARNING`` 逐字一致**）
FALLBACK_WARNING = "WARNING: Fallback to transient score"

#: 支持的平滑方法（``none`` = 不做下游滤波 ⇒ 臂 B 用）
SMOOTHING_METHODS: tuple[str, ...] = ("ewma", "rolling", "none")

#: ``history_span_days`` 至少要覆盖这么多个**半衰期** ⇒ EWMA 预热后收敛
#: （臂 C 的 30 个交易日 ÷ 半衰期 3.11 天 ≈ 9.7 个半衰期 ⇒ 刚好够；
#:   臂 A 的半衰期 15/20 天 ⇒ 跨度必须同步拉长，否则预热不足）
MIN_HALF_LIVES_PER_SPAN = 10


# ----------------------------------------------------------------- alpha 换算
def alpha_from_half_life(half_life_days: float) -> float:
    """🔴 由**半衰期（交易日）**自算 EWMA 系数。

    ``alpha = 1 - 0.5 ** (1 / half_life_days)``
    （⇒ 记忆衰减因子 ``1 - alpha = 0.5 ** (1/half_life)`` ⇒ 每 ``half_life`` 个交易日
    权重减半 ⇒ 半衰期**就是** ``half_life_days``）。

    非法输入（``<= 0`` 或非有限）⇒ **直接 raise**（宁可不产出，不产出假结果）。
    """
    half_life = float(half_life_days)
    if not math.isfinite(half_life) or half_life <= 0.0:
        raise ValueError(
            f"🔴 半衰期必须是正的交易日数（> 0），实得 {half_life_days!r}"
        )
    return 1.0 - 0.5 ** (1.0 / half_life)


def effective_half_life_days(alpha: float) -> float:
    """🔴 由 alpha **反算**记忆半衰期（交易日）：``ln(0.5) / ln(1 - alpha)``。

    与 :func:`alpha_from_half_life` 互为反函数（测试里做往返一致性校验）。
    alpha 越界（不在 ``(0, 1)``）⇒ **直接 raise**。
    """
    a = float(alpha)
    if not math.isfinite(a) or not (0.0 < a < 1.0):
        raise ValueError(f"🔴 alpha 必须落在 (0, 1) 开区间内，实得 {alpha!r}")
    return math.log(0.5) / math.log(1.0 - a)


# ----------------------------------------------------------------- 臂注册表
@dataclass(frozen=True)
class SmoothingArm:
    """一条**可执行**的平滑臂（方法 + 参数 + 口径说明）。"""

    arm_id: str
    method: str                       # "ewma" | "rolling" | "none"
    alpha: float | None
    window: int
    history_span_days: int
    half_life_days: float | None
    native_slow: bool = False         # 臂 B：用 V2 原生慢变量，不做下游 EWMA
    note: str = ""

    @property
    def enabled(self) -> bool:
        """是否要做**下游**滤波（臂 B = False ⇒ 原生慢变量直通）。"""
        return self.method != "none"

    def as_dict(self) -> dict:
        """落盘用的参数字典（写进 ``arm_search_val.json`` 供对账）。"""
        return {
            "arm_id": self.arm_id,
            "method": self.method,
            "alpha": self.alpha,
            "window": int(self.window),
            "history_span_days": int(self.history_span_days),
            "half_life_days": self.half_life_days,
            "native_slow": bool(self.native_slow),
            "note": self.note,
        }


def arm_specs() -> dict[str, SmoothingArm]:
    """🔴 臂注册表：把 ``config_v2.SMOOTHING_ARM_IDS`` 变成**可执行参数**。

    * 臂 A：``alpha`` **由半衰期自算**（绝不写死字面量）；
      ``history_span_days`` 随半衰期同步拉长
      （``max(30, ceil(10 × 半衰期))`` ⇒ 预热窗口 ≥ 10 个半衰期 ⇒ EWMA 收敛）；
    * 臂 B：**不做**下游 EWMA（``method="none"``）⇒ 原生慢变量直通；
    * 臂 C：V1 现状（``alpha=0.20`` / ``window=5`` / ``history_span=30``）。
    """
    specs: dict[str, SmoothingArm] = {}

    for raw_half_life in C.ARM_A_HALF_LIFE_DAYS:
        half_life = float(raw_half_life)
        arm_id = f"A_half_life_{int(round(half_life))}"
        alpha = alpha_from_half_life(half_life)
        specs[arm_id] = SmoothingArm(
            arm_id=arm_id,
            method="ewma",
            alpha=alpha,
            window=int(C.ARM_C_WINDOW),
            history_span_days=int(
                max(int(C.ARM_C_HISTORY_SPAN_DAYS),
                    math.ceil(MIN_HALF_LIVES_PER_SPAN * half_life))
            ),
            half_life_days=half_life,
            native_slow=False,
            note=(
                f"臂 A（窗口拉长）：EWMA 半衰期 {half_life:g} 个交易日 ⇒ "
                f"alpha **自算** = 1 - 0.5**(1/{half_life:g}) = {alpha:.6f}；"
                f"预热跨度随半衰期拉长（≥ {MIN_HALF_LIVES_PER_SPAN} 个半衰期 ⇒ 收敛）"
            ),
        )

    specs[C.ARM_B_ID] = SmoothingArm(
        arm_id=C.ARM_B_ID,
        method="none",
        alpha=None,
        window=int(C.ARM_C_WINDOW),
        history_span_days=int(C.ARM_C_HISTORY_SPAN_DAYS),
        half_life_days=None,
        native_slow=True,
        note=(
            "臂 B（原生平滑 + Rank Buffer）：**去掉**下游 EWMA 滤波器 ⇒ 直接用 V2 的 "
            "20 日 Label 模型**原生慢变量输出**（打分管线原始分数）排序；"
            "Rank Buffer 由统一评估引擎（etf_layer.build_targets）分配 "
            "⇒ 与其它臂走**同一条**评估/回测代码路径"
        ),
    )

    arm_c_id = "C_v1_current"
    specs[arm_c_id] = SmoothingArm(
        arm_id=arm_c_id,
        method="ewma",
        alpha=float(C.ARM_C_ALPHA),
        window=int(C.ARM_C_WINDOW),
        history_span_days=int(C.ARM_C_HISTORY_SPAN_DAYS),
        half_life_days=effective_half_life_days(float(C.ARM_C_ALPHA)),
        native_slow=False,
        note=(
            f"臂 C（V1 现状对照）：alpha={float(C.ARM_C_ALPHA)} / "
            f"window={int(C.ARM_C_WINDOW)} / history_span={int(C.ARM_C_HISTORY_SPAN_DAYS)} "
            f"⇒ 记忆半衰期 {effective_half_life_days(float(C.ARM_C_ALPHA)):.2f} 个交易日"
            "（🔴 不得默认胜出）"
        ),
    )

    expected = tuple(C.SMOOTHING_ARM_IDS)
    missing = [a for a in expected if a not in specs]
    extra = [a for a in specs if a not in expected]
    if missing or extra:
        raise ValueError(
            f"🔴 臂注册表与 config_v2.SMOOTHING_ARM_IDS 不一致：缺 {missing} / 多 {extra}"
        )
    return specs


def arm_spec(arm_id: str) -> SmoothingArm:
    """取一条臂的规格（未知臂 ⇒ raise，不静默回落成默认臂）。"""
    specs = arm_specs()
    key = str(arm_id).strip()
    if key not in specs:
        raise ValueError(f"🔴 未知平滑臂 {arm_id!r} ⇒ 可选臂 {sorted(specs)}")
    return specs[key]


def resolve_arm(
    arm_id: str | None = None,
    *,
    method: str | None = None,
    alpha: float | None = None,
    window: int | None = None,
    history_span_days: int | None = None,
) -> SmoothingArm | None:
    """把「臂 ID」或「显式参数」解析成一条**可执行**的臂规格。

    * 什么都没给（``arm_id=None`` 且无任何显式参数）⇒ 返回 ``None``
      ⇒ **不平滑**（保持既有调用方行为不变）；
    * 给了 ``arm_id`` ⇒ 取注册表规格，**显式参数优先**（逐项覆盖）；
    * 只给显式参数 ⇒ 拼一条 ``custom`` 臂。

    非法参数（方法未知 / 窗口 < 1 / ewma 缺 alpha / 跨度 < 1）⇒ 直接 raise。
    """
    base = arm_spec(arm_id) if arm_id is not None else None
    eff_method = str(method).strip().lower() if method is not None else (
        base.method if base is not None else None
    )
    eff_alpha = alpha if alpha is not None else (base.alpha if base is not None else None)
    eff_window = int(window) if window is not None else (
        int(base.window) if base is not None else int(C.ARM_C_WINDOW)
    )
    eff_span = int(history_span_days) if history_span_days is not None else (
        int(base.history_span_days) if base is not None else int(C.ARM_C_HISTORY_SPAN_DAYS)
    )

    if eff_method is None and eff_alpha is None and window is None and history_span_days is None:
        return None                      # 🔴 默认：不平滑
    if eff_method is None:
        eff_method = "ewma"
    if eff_method not in SMOOTHING_METHODS:
        raise ValueError(f"🔴 平滑方法非法：{method!r} ⇒ 只支持 {list(SMOOTHING_METHODS)}")
    if eff_window < 1:
        raise ValueError(f"🔴 平滑窗口必须 ≥ 1 个交易日，实得 {eff_window}")
    if eff_span < 1:
        raise ValueError(f"🔴 历史预热跨度必须 ≥ 1 个交易日，实得 {eff_span}")
    if eff_method == "ewma":
        if eff_alpha is None:
            raise ValueError("🔴 EWMA 必须给 alpha（或给半衰期由 alpha_from_half_life 自算）")
        eff_alpha = float(eff_alpha)
        if not (0.0 < eff_alpha < 1.0):
            raise ValueError(f"🔴 EWMA 系数必须落在 (0, 1)，实得 {eff_alpha!r}")

    return SmoothingArm(
        arm_id=(base.arm_id if base is not None else "custom"),
        method=eff_method,
        alpha=eff_alpha,
        window=eff_window,
        history_span_days=eff_span,
        half_life_days=(
            effective_half_life_days(float(eff_alpha))
            if eff_method == "ewma" and eff_alpha else None
        ),
        native_slow=bool(base.native_slow) if base is not None else (eff_method == "none"),
        note=(base.note if base is not None else "自定义平滑参数（未走臂注册表）"),
    )


# ----------------------------------------------------------------- 平滑核心
@dataclass
class SmoothResult:
    """平滑结果：``panel``（轴 × 代码）+ ``diagnostics``（审计用）。"""

    panel: pd.DataFrame
    diagnostics: dict[str, Any]


def _normalize_axis(axis: Sequence[pd.Timestamp]) -> list[pd.Timestamp]:
    """交易日轴 ⇒ 升序去重列表（与 V1 ``_normalize_axis`` 同口径）。"""
    out: list[pd.Timestamp] = []
    for day in axis:
        stamp = pd.Timestamp(day).normalize()
        if stamp not in out:
            out.append(stamp)
    return sorted(out)


def _locf_matrix(panel: pd.DataFrame, axis: list[pd.Timestamp]) -> tuple[np.ndarray, np.ndarray]:
    """把稀疏观测摊到交易日轴上（LOCF 阶梯函数）。

    返回 ``(locf[n_days, n_codes], n_obs[n_codes])``：
    ``locf[i, j]`` = 轴上第 i 个交易日的观测值（首个观测之前为 NaN；
    两个观测点之间**沿用上一个** ⇒ 阶梯函数）。
    """
    n_days = len(axis)
    codes = list(panel.columns)
    locf = np.full((n_days, len(codes)), np.nan, dtype=float)
    n_obs = np.zeros(len(codes), dtype=int)
    if n_days == 0 or panel is None or panel.empty:
        return locf, n_obs
    for j, code in enumerate(codes):
        series = panel[code]
        obs: list[tuple[pd.Timestamp, float]] = []
        for stamp, value in series.items():
            if value is None or not np.isfinite(float(value)):
                continue
            day = pd.Timestamp(stamp).normalize()
            if day > axis[-1]:
                continue                       # 观测在轴末端之后 ⇒ 不参与本轮平滑
            obs.append((day, float(value)))
        if not obs:
            continue
        obs.sort(key=lambda item: item[0])
        current = np.nan
        cursor = 0
        for i in range(n_days):
            while cursor < len(obs) and obs[cursor][0] <= axis[i]:
                current = obs[cursor][1]
                cursor += 1
                n_obs[j] += 1
            locf[i, j] = current
    return locf, n_obs


def _ewma_matrix(locf: np.ndarray, alpha: float) -> np.ndarray:
    """EWMA 递推（**V1 逐位同式**）：``state = alpha*x + (1-alpha)*state``，冷启动即自身。"""
    out = np.full(locf.shape, np.nan, dtype=float)
    for j in range(locf.shape[1]):
        state = np.nan
        for i in range(locf.shape[0]):
            value = locf[i, j]
            if not np.isfinite(value):
                continue
            state = value if not np.isfinite(state) else alpha * value + (1.0 - alpha) * state
            out[i, j] = state
    return out


def _rolling_matrix(locf: np.ndarray, window: int) -> np.ndarray:
    """滚动均值（V1 同口径：窗口内有效值的均值）。"""
    out = np.full(locf.shape, np.nan, dtype=float)
    for j in range(locf.shape[1]):
        for i in range(locf.shape[0]):
            chunk = locf[max(0, i - window + 1): i + 1, j]
            usable = chunk[np.isfinite(chunk)]
            if usable.size:
                out[i, j] = float(usable.mean())
    return out


def smooth_panel(
    panel: pd.DataFrame,
    axis: Sequence[pd.Timestamp],
    *,
    method: str = "ewma",
    alpha: float = 0.20,
    window: int = 5,
    history_span_days: int | None = None,
    asof: Any = None,
) -> SmoothResult:
    """把「日期 × ETF 代码」的穿透分面板沿**交易日轴**做时间平滑。

    参数
    ----
    panel : 宽表，``index`` = 观测日期（打分日），``columns`` = ETF 代码，值 = 穿透分 E。
    axis : **交易日轴**（升序；衰减单位是交易日，不是打分点）。
    method : ``"ewma"`` / ``"rolling"`` / ``"none"``。
    alpha : EWMA 系数（**必须**由半衰期自算，见 :func:`alpha_from_half_life`）。
    window : 滚动窗口 / 读数窗口（交易日）。
    history_span_days : 预热跨度（交易日）⇒ 只用**最后这么多个交易日**做递推
        （与 V1 ``compute_smoothed_alpha`` 同口径）。
    asof : 只用 ``<= asof`` 的轴与观测（``None`` = 用整条轴）。

    返回
    ----
    :class:`SmoothResult`：``panel`` = 沿轴平滑后的 E（``index`` = 实际使用的轴），
    ``diagnostics`` = 审计字段（含回退标记与半衰期）。

    🔴 **不许抛 NaN / KeyError**：历史不足时权重随可得历史动态变化；
    **全历史不可用** ⇒ 退化为瞬态分并输出 ``WARNING: Fallback to transient score``。
    """
    method = str(method).strip().lower()
    if method not in SMOOTHING_METHODS:
        raise ValueError(f"🔴 平滑方法非法：{method!r} ⇒ 只支持 {list(SMOOTHING_METHODS)}")
    if method == "ewma":
        alpha = float(alpha)
        if not (0.0 < alpha < 1.0):
            raise ValueError(f"🔴 EWMA 系数必须落在 (0, 1)，实得 {alpha!r}")
    if int(window) < 1:
        raise ValueError(f"🔴 平滑窗口必须 ≥ 1 个交易日，实得 {window}")

    axis_list = _normalize_axis(axis)
    if asof is not None:
        stamp = pd.Timestamp(asof).normalize()
        axis_list = [d for d in axis_list if d <= stamp]
    if history_span_days is not None:
        span = int(history_span_days)
        if span < 1:
            raise ValueError(f"🔴 历史预热跨度必须 ≥ 1 个交易日，实得 {span}")
        axis_list = axis_list[-span:]

    frame = pd.DataFrame() if panel is None else panel.copy()
    if len(frame.columns):
        frame.index = pd.DatetimeIndex([pd.Timestamp(d).normalize() for d in frame.index])
    codes = list(frame.columns)

    diagnostics: dict[str, Any] = {
        "method": method,
        "alpha": float(alpha) if method == "ewma" else None,
        "window": int(window),
        "history_span_days": None if history_span_days is None else int(history_span_days),
        "axis_len": int(len(axis_list)),
        "axis_start": axis_list[0].date().isoformat() if axis_list else None,
        "axis_end": axis_list[-1].date().isoformat() if axis_list else None,
        "n_codes": int(len(codes)),
        "n_codes_with_history": 0,
        "n_codes_fallback_transient": 0,
        "fallback_to_transient": False,
        "warning": None,
        "effective_half_life_trading_days": (
            round(effective_half_life_days(float(alpha)), 4) if method == "ewma" else None
        ),
    }

    if not axis_list or frame.empty:
        # 🔴 全历史不可用 ⇒ 退化为瞬态分（这里瞬态分本身也取不到 ⇒ 空面板）
        diagnostics["fallback_to_transient"] = True
        diagnostics["warning"] = FALLBACK_WARNING
        diagnostics["n_codes_fallback_transient"] = int(len(codes))
        print(FALLBACK_WARNING, flush=True)
        empty = pd.DataFrame(index=pd.DatetimeIndex(axis_list), columns=codes, dtype=float)
        return SmoothResult(panel=empty, diagnostics=diagnostics)

    locf, n_obs = _locf_matrix(frame, axis_list)
    transient = locf[-1, :] if len(axis_list) else np.full(len(codes), np.nan)

    if method == "none":
        smoothed = locf.copy()
    elif method == "ewma":
        smoothed = _ewma_matrix(locf, float(alpha))
    else:
        smoothed = _rolling_matrix(locf, int(window))

    # 🔴 全历史不可用（没有任何一只标的在这个窗口里有观测）⇒ 退化为瞬态分 + 警告
    if int((n_obs > 0).sum()) == 0:
        diagnostics["fallback_to_transient"] = True
        diagnostics["warning"] = FALLBACK_WARNING
        diagnostics["n_codes_fallback_transient"] = int(len(codes))
        print(FALLBACK_WARNING, flush=True)
        smoothed = locf.copy()

    # 逐只：观测点 ≤ 1 ⇒ 退化为瞬态分（V1 同口径：fallback_to_transient = n_obs <= 1）
    fallback_mask = n_obs <= 1
    for j in range(len(codes)):
        if not fallback_mask[j]:
            continue
        if np.isfinite(smoothed[-1, j]):
            smoothed[-1, j] = transient[j]      # 瞬态分 = 末值前向填充
    diagnostics["n_codes_with_history"] = int((n_obs > 0).sum())
    diagnostics["n_codes_fallback_transient"] = int(fallback_mask.sum())

    out = pd.DataFrame(smoothed, index=pd.DatetimeIndex(axis_list), columns=codes)
    return SmoothResult(panel=out, diagnostics=diagnostics)


def apply_spec(
    panel: pd.DataFrame,
    axis: Sequence[pd.Timestamp],
    arm: SmoothingArm,
    *,
    asof: Any = None,
) -> SmoothResult:
    """按**一条臂的规格**做平滑（臂 B = ``method="none"`` ⇒ 原生慢变量直通）。"""
    if arm.method == "none":
        res = smooth_panel(panel, axis, method="none", window=arm.window,
                           history_span_days=arm.history_span_days, asof=asof)
        res.diagnostics.update({
            "arm_id": arm.arm_id,
            "native_slow": True,
            "note": "原生慢变量直通（不做下游 EWMA）",
        })
        return res
    res = smooth_panel(panel, axis, method=arm.method, alpha=float(arm.alpha),
                       window=arm.window, history_span_days=arm.history_span_days, asof=asof)
    res.diagnostics.update({"arm_id": arm.arm_id, "native_slow": bool(arm.native_slow)})
    return res


def apply_arm(
    panel: pd.DataFrame,
    axis: Sequence[pd.Timestamp],
    arm_id: str,
    *,
    asof: Any = None,
) -> SmoothResult:
    """按**臂 ID**做平滑（未知臂 ⇒ raise）。"""
    return apply_spec(panel, axis, arm_spec(arm_id), asof=asof)
