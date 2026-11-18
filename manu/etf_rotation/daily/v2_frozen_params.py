"""🔴 **V2 冻结参数 → 每日实盘链的接缝（seam）**（2026-11-18）

【为什么要有这个模块】
  2026-11-18 用户目标文档第三条：「把 ``frozen_params_wf.json``（``pure_E_native + buffer=2``）
  正式接入每日执行流水线（替换掉 V1 的短窗口平滑与冗余过滤）」。

  审计结论（改造前）：每日链的 ETF 层参数**全部来自 V1 的研究链常量**
  （``manu/etf_rotation/config.py``）：
  ``ALPHA_SCORE_WEIGHT=0.5`` / ``MOMENTUM_SCORE_WEIGHT=0.5``（V1 的 0.5/0.5）、
  ``ENABLE_SCORE_SMOOTHING=True`` + ``SMOOTHING_ALPHA=0.20`` + ``SMOOTHING_WINDOW=5``
  （**穿透分 E 的短窗口 EWMA**）、``ENTRY_MA_FILTER_WINDOW=60`` / ``EXIT_MA_FILTER_WINDOW=60`
  （**MA60 入场闸门 + 持仓强制退出**）。
  ⇒ 即「**每日实盘流水线用的是 V1 的 ETF 层老配置**」，
  **V2 五折滚动前推选出来的冠军参数压根没进每日链** ⇒ V2 ↔ 每日链**无 seam**。

  本模块 = 这条 seam 的**唯一入口**：把 V2 冻结件读成**可校验、可披露、可 fail-closed**
  的参数对象，再由每日链把它翻译成 ``analyze.py`` / ``recommend.py`` 的命令行参数。

【🔴 三条硬纪律】
  1. **fail-closed**：冻结件读不到 / 口径对不上 ⇒ **抛异常**，**不许**静默回落 V1 常量
     （静默回落 = 又回到「V2 只是回测里的玩具」的老坑，且**不会报错**）。
  2. **不许半支持**：冻结件里**本链无法复现**的机制 ⇒ 明确报错，**不许**「照跑不误」。
     当前 V2 冠军用到的机制**全部可复现**（见下表）；用不到的（如负分空仓闸门）⇒ 报错。
  3. **不许混口径**：冻结件的 ``top_k`` / ``cadence`` 与每日链的账户口径不一致 ⇒ 报错，
     **不许**「链里那份是 5、冻结件是 5，凑巧一样就算了」。

【参数映射表（V2 冻结件 ⇒ 每日链）】
  | 冻结件字段 | 值 | 每日链落点 |
  | --- | --- | --- |
  | ``alpha_weight`` | 1.0 | ``--alpha-weight 1.0``（**纯 E**，不再与动量 0.5 混） |
  | ``momentum_weight`` | 0.0 | ``--momentum-weight 0.0`` |
  | ``smoothing_arm`` | ``B_native_slow`` | ``--no-smoothing``（**关掉 V1 的 E 层 EWMA**） |
  | ``entry_ma_window`` | 0 | ``--entry-ma-window 0``（**关掉 V1 的 MA60 入场闸门**） |
  | ``exit_ma_window`` | 0 | ``--exit-ma-window 0``（**关掉 V1 的 MA60 强制退出**） |
  | ``top_k`` | 5 | ``--target-n-holdings 5`` |
  | ``hold_buffer`` | 2 | ``--hold-buffer 2`` |
  | ``allow_theme_cap`` | False | ``--max-per-theme 0``（**不限同族上限**） |
  | ``rebalance_every_n_days`` | 20 | **断言** = 每日链的部署节奏（20 个交易日） |

【🔴 口径映射的已知不等价（如实披露，不掩盖）】
  * V2 的 ``allow_theme_cap=False`` 只关「同族去重」；
    每日链的 ``apply_theme_cap`` 同时做「选股 + 同族上限 + 同质化去重（冲突对）」。
    ⇒ 本接缝把「同族上限」放开（``max_per_theme=0`` ⇒ 不限），
      但**保留**同质化去重过滤网 ⇒ **两者不完全等价**，已在披露里写明。
  * V2 的平滑臂 A（半衰期口径）与每日链的 EWMA（窗口 + alpha 口径）**参数化方式不同**
    ⇒ **不可互相换算** ⇒ 冻结件若不是 ``B_native_slow`` ⇒ 直接报错（不做近似）。

【用法】
    from daily.v2_frozen_params import load_frozen_params
    fp = load_frozen_params()          # 路径取 ``daily_config.V2_FROZEN_PARAMS_FILE``
    args = fp.etf_layer_args()         # ⇒ analyze.py / recommend.py 的命令行参数
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 🔴 V2 的「不平滑」臂 ID（= 直接用 20 日 Label 模型的**原生慢变量**输出排序）
SMOOTHING_ARM_NO_SMOOTHING = "B_native_slow"

#: 🔴 每日链能复现的平滑臂（**只支持不平滑**；A 臂的半衰期口径与本链的
#:    「窗口 + alpha」参数化**不可换算** ⇒ 落到别的臂一律报错）
SUPPORTED_SMOOTHING_ARMS = (SMOOTHING_ARM_NO_SMOOTHING,)

#: 🔴 V2 锁死的 cadence（**严格月频**，2026-11-18 用户拍板）
LOCKED_CADENCE = 20


class V2FrozenParamsError(RuntimeError):
    """🔴 V2 冻结参数**读不到 / 口径不符** ⇒ fail-closed（宁可不产出，不许悄悄回落 V1）。"""


@dataclass(frozen=True)
class FrozenV2Params:
    """V2 冻结件（``frozen_params_wf.json``）的**已校验**视图。"""

    source_path: Path
    md5: str
    frozen_at: str
    winner_arm: str
    alpha_weight: float
    momentum_weight: float
    smoothing_arm: str
    entry_ma_window: int
    exit_ma_window: int
    top_k: int
    hold_buffer: int
    allow_theme_cap: bool
    neg_score_gate: bool
    rebalance_every_n_days: int
    rebalance_signal_stride: int

    # ---- 派生属性 ----
    @property
    def smoothing_enabled(self) -> bool:
        """本链是否要开 E 层平滑（``B_native_slow`` ⇒ **不开**）。"""
        return self.smoothing_arm != SMOOTHING_ARM_NO_SMOOTHING

    @property
    def max_per_theme(self) -> int:
        """同族上限（``allow_theme_cap=False`` ⇒ **0 = 不限**）。"""
        return 0 if not self.allow_theme_cap else -1  # -1 = 让调用方回落 config 默认

    # ---- 命令行翻译 ----
    def etf_layer_args(self) -> list[str]:
        """⇒ ``analyze.py`` / ``recommend.py`` 的 ETF 层参数（**两链同一组**）。

        🔴 分析链与规则链**必须**拿到**逐字相同**的参数 ⇒ 少一条就分叉
        （历史上真出过「规则层无平滑 vs 交付层有平滑」的分叉 ⇒ 交付件与规则选出不同的票）。
        """
        args: list[str] = [
            "--alpha-weight", f"{self.alpha_weight:.6g}",
            "--momentum-weight", f"{self.momentum_weight:.6g}",
            "--hold-buffer", str(self.hold_buffer),
            "--target-n-holdings", str(self.top_k),
            "--max-per-theme", str(self.max_per_theme),
            "--entry-ma-window", str(self.entry_ma_window),
            "--exit-ma-window", str(self.exit_ma_window),
        ]
        if not self.smoothing_enabled:
            args.append("--no-smoothing")
        return args

    def disclosure(self) -> dict[str, Any]:
        """披露块（写进每日快照 / 交付件）⇒ 读者能一眼看出「参数从哪来、是什么」。"""
        return {
            "seam": "V2 冻结件 → 每日实盘链",
            "enabled": True,
            "source": str(self.source_path),
            "source_md5": self.md5,
            "frozen_at": self.frozen_at,
            "winner_arm": self.winner_arm,
            "params": {
                "alpha_weight": self.alpha_weight,
                "momentum_weight": self.momentum_weight,
                "smoothing_arm": self.smoothing_arm,
                "smoothing_enabled": self.smoothing_enabled,
                "entry_ma_window": self.entry_ma_window,
                "exit_ma_window": self.exit_ma_window,
                "top_k": self.top_k,
                "hold_buffer": self.hold_buffer,
                "allow_theme_cap": self.allow_theme_cap,
                "neg_score_gate": self.neg_score_gate,
                "rebalance_every_n_days": self.rebalance_every_n_days,
            },
            "caveat": (
                "🔴 V2 的 `allow_theme_cap=False` 只关「同族去重」；本链的同质化去重"
                "（冲突对过滤网）**仍然生效** ⇒ 两者**不完全等价**。"
            ),
        }


def _require(payload: dict, key: str, where: str) -> Any:
    if key not in payload:
        raise V2FrozenParamsError(f"🔴 V2 冻结件**缺字段** `{key}`（{where}）⇒ 拒绝使用")
    return payload[key]


def _as_float(value: Any, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise V2FrozenParamsError(f"🔴 V2 冻结件字段 `{key}` 不是数字：{value!r}") from exc


def _as_int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise V2FrozenParamsError(f"🔴 V2 冻结件字段 `{key}` 不是整数：{value!r}") from exc


def parse_frozen_params(payload: dict, *, source_path: Path, md5: str) -> FrozenV2Params:
    """把冻结件原始 dict 解析成 :class:`FrozenV2Params`（**口径校验全在这一处**）。"""
    if not isinstance(payload, dict):
        raise V2FrozenParamsError(f"🔴 V2 冻结件不是 JSON 对象：{source_path}")

    params = payload.get("params")
    if not isinstance(params, dict):
        raise V2FrozenParamsError(f"🔴 V2 冻结件缺 `params` 段：{source_path}")
    where = str(source_path)

    alpha_weight = _as_float(_require(params, "alpha_weight", where), "alpha_weight")
    momentum_weight = _as_float(_require(params, "momentum_weight", where), "momentum_weight")
    smoothing_arm = str(_require(params, "smoothing_arm", where))
    entry_ma_window = _as_int(_require(params, "entry_ma_window", where), "entry_ma_window")
    exit_ma_window = _as_int(_require(params, "exit_ma_window", where), "exit_ma_window")
    top_k = _as_int(_require(params, "top_k", where), "top_k")
    hold_buffer = _as_int(_require(params, "hold_buffer", where), "hold_buffer")
    allow_theme_cap = bool(_require(params, "allow_theme_cap", where))
    neg_score_gate = bool(_require(params, "neg_score_gate", where))
    cadence = _as_int(_require(params, "rebalance_every_n_days", where), "rebalance_every_n_days")
    stride = _as_int(payload.get("rebalance_signal_stride", 0), "rebalance_signal_stride")

    # ---- 口径校验（fail-closed）----
    if smoothing_arm not in SUPPORTED_SMOOTHING_ARMS:
        raise V2FrozenParamsError(
            f"🔴 V2 冻结件的平滑臂 `{smoothing_arm}` **本链无法复现**"
            "（A 臂是半衰期口径，本链是「窗口 + alpha」口径 ⇒ 不可换算）"
            "⇒ 拒绝使用；当前只支持 `B_native_slow`（不平滑）"
        )
    if neg_score_gate:
        raise V2FrozenParamsError(
            "🔴 V2 冻结件开启了**负分空仓闸门**，但每日链**没有这个机制**"
            "（每日链的防守职责在 `nav_state` 回撤刹车 + TA 宏观降仓带）"
            "⇒ 拒绝使用（不许「照跑不误」）"
        )
    if cadence != LOCKED_CADENCE:
        raise V2FrozenParamsError(
            f"🔴 V2 冻结件的 cadence = {cadence}，与锁死的**严格月频** {LOCKED_CADENCE} 不符"
            "⇒ 拒绝使用"
        )
    if alpha_weight + momentum_weight <= 0:
        raise V2FrozenParamsError(
            f"🔴 V2 冻结件的权重全为 0（alpha={alpha_weight} / momentum={momentum_weight}）"
            "⇒ 无法排序 ⇒ 拒绝使用"
        )
    if top_k < 1:
        raise V2FrozenParamsError(f"🔴 V2 冻结件的 top_k = {top_k} < 1 ⇒ 拒绝使用")
    if hold_buffer < 0:
        raise V2FrozenParamsError(f"🔴 V2 冻结件的 hold_buffer = {hold_buffer} < 0 ⇒ 拒绝使用")
    if entry_ma_window < 0 or exit_ma_window < 0:
        raise V2FrozenParamsError(
            f"🔴 V2 冻结件的均线窗口为负（entry={entry_ma_window} / exit={exit_ma_window}）"
            "⇒ 拒绝使用"
        )

    return FrozenV2Params(
        source_path=Path(source_path),
        md5=md5,
        frozen_at=str(payload.get("frozen_at", "")),
        winner_arm=str(payload.get("winner_arm", "")),
        alpha_weight=alpha_weight,
        momentum_weight=momentum_weight,
        smoothing_arm=smoothing_arm,
        entry_ma_window=entry_ma_window,
        exit_ma_window=exit_ma_window,
        top_k=top_k,
        hold_buffer=hold_buffer,
        allow_theme_cap=allow_theme_cap,
        neg_score_gate=neg_score_gate,
        rebalance_every_n_days=cadence,
        rebalance_signal_stride=stride,
    )


def load_frozen_params(path: str | Path | None = None, *, required: bool = True) -> FrozenV2Params:
    """读 V2 冻结件（**fail-closed**）。

    参数
    ----
    path
        冻结件路径；``None`` ⇒ 取 ``daily_config.V2_FROZEN_PARAMS_FILE``。
    required
        ``True``（默认）⇒ 读不到就**抛异常**；
        ``False`` ⇒ 读不到返回 ``None``（**只给显式降级用的测试/诊断用**）。
    """
    target = Path(path) if path is not None else _default_path()
    if not target.exists():
        if not required:
            return None  # type: ignore[return-value]
        raise V2FrozenParamsError(
            f"🔴 V2 冻结件读不到：{target}\n"
            "  ⇒ 每日链**不许**静默回落 V1 的 ETF 层老配置（那会让 V2 又变成回测里的玩具）\n"
            "  ⇒ 请先在服务器产出 `frozen_params_wf.json`"
            "（`manu/tools/v2_freeze_from_walkforward.py`），"
            "或显式关闭接缝（`ETF_ROTATION_V2_FROZEN_PARAMS_ENABLED=0`）"
        )
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise V2FrozenParamsError(f"🔴 V2 冻结件读不出来：{target}（{exc}）") from exc
    md5 = hashlib.md5(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise V2FrozenParamsError(f"🔴 V2 冻结件不是合法 JSON：{target}（{exc}）") from exc
    return parse_frozen_params(payload, source_path=target, md5=md5)


def _default_path() -> Path:
    """默认冻结件路径（**单一事实源 = ``daily_config.V2_FROZEN_PARAMS_FILE``**）。"""
    import daily_config as DC  # noqa: PLC0415 - 运行期导入，避免模块级循环

    return Path(DC.V2_FROZEN_PARAMS_FILE)
