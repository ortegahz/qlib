#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**ETF 穿透分时间平滑**闸门探针（锁 `manu/etf_rotation/score_smoothing.py` 的行为）。

【为什么要有这个探针】
``E = W × S`` 是**单日截面快照** ⇒ S 层每次刷新都重打分 ⇒ E 每次都变 ⇒ 截面密集区的 ETF 排名
**逐日洗牌** ⇒ 「单日暴涨 3 倍」的噪声能把排名直接劫持到 Top 1 ⇒ 高频换仓假象。
本模块把 E 放到**交易日轴**上做时间滤波（EWMA / 滚动均值 + LOCF 阶梯），
本探针把它的行为**钉死** ⇒ 任何回归（改口径 / 改列 / 去掉 LOCF / 去掉回退 / 去掉告警）当场变红。

【每类断言钉死了什么】
A 类 · 脉冲假数据（需求 §4.1 指定的验收点）—— 噪声不许劫持排名
  A1 脉冲标的的平滑分必须**显著低于** raw 分（脉冲被压下去）
  A2 脉冲标的的平滑分必须**精确等于** ``0.35×3.0 + 0.65×1.0 = 1.70``（EWMA 口径可手算对账）
  A3 按平滑分降序，脉冲标的**不得**排第 1（需求原文：「且未在首日冲上 Top 1」）
  A4 脉冲标的的平滑分必须**低于**稳态标的（噪声没劫持排名）
  A5 脉冲标的的 ``score_stability`` 必须**高于**稳态标的（脉冲更不稳定）

B 类 · 数值口径正确性 —— 四个口径逐个手算对账
  B1 EWMA 手算：α=0.5、序列 ``[1.0, 2.0, 4.0]`` ⇒ ``[1.0, 1.5, 2.75]``
  B2 Rolling 手算：window=3、序列 ``[1.0, 2.0, 4.0]`` ⇒ 末值 ``(1+2+4)/3 = 2.3333…``
  B3 LOCF 阶梯：只给 d1 的观测 ⇒ 轴上 d1~d5 全部等于 d1 的值（与「逐日都给」的对照**逐位相等**）；
     再给 d5 补一个新观测 ⇒ 结果必须**不等于**全 1.0 那条 ⇒ 证明取的是「最近观测」
  B4 半衰期：``effective_half_life_days(0.35) ≈ 1.609`` 个交易日（容差 1e-3）且 **< 3.0**
     （需求文档写「3~5 个交易日的半衰期」⇒ **实际算出来只有 1.61 日** ⇒ 断言按实际值，不照抄文档措辞）

C 类 · 边界与 fail-safe —— 宁可不产出，不产出假结果
  C1 只有 1 个观测点的新标的 ⇒ 平滑分 == raw 分、逐只标记回退、**不抛异常**
  C2 观测点为空的标的（整行缺失 ⇒ 被剔除；全 NaN ⇒ None）⇒ **不抛 KeyError**、不产出 NaN
  C3 有效点 < 2 ⇒ ``score_stability`` 必须是 **None**（**不是 0**）
  C4 非法参数（``method="ma"`` / ``window=0`` / ``alpha=0`` / ``alpha=1.5``）⇒ 必须抛**中文**异常
  C5 全局没有历史观测日 ⇒ ``diagnostics["warning"]`` 与 ``FALLBACK_WARNING`` **逐字相等**，
     且 ``diagnostics["fallback_reason"]`` 非空
  C6 ``smooth_panel`` 返回列**恰好**等于 ``RESULT_COLUMNS``（含顺序；含空输入早退路径）

D 类 · 变异反证（闸门有牙，铁律十四.52）—— 把被测行为换成错的实现 ⇒ 相关断言**必须失败**
  D1 「EWMA 平滑」换成「直接取 raw 不平滑」⇒ A1/A2/A3 必须失败
  D2 「LOCF 阶梯」换成「只有观测点本身、其余轴上空缺」⇒ B3 必须失败
  D3 变异构造过程本身抛异常 ⇒ 判 **FAIL**（不许崩掉整个探针）

E 类 · 源码静态检查（铁律十四.57 的同款口径：对源码跑，**AST 判定**，不许文本匹配）
  E1 无 tab 缩进（4 空格口径）
  E2 函数名 snake_case（允许前导 ``_``，那是 PEP8 私有命名）
  E3 每个函数都有**含中文**的 docstring
  E4 无 ``fillna(0)`` 调用（AST 判定 ⇒ docstring 里的字样不会被误判成调用）
  E5 模块级 docstring 含中文

【2026-11-09 全量闸门抓到的真缺陷 ⇒ 新增 F/G/H/I/K 类把它钉死】
真缺陷：``engine/portfolio.py::build_portfolio`` 之前**没有** ``smoothing_config`` 参数、
``recommend.py`` 也没传平滑表 ⇒ **规则层用原始分排序、交付层用平滑分排序**
⇒ 选出不同的票 ⇒ 闸门 ``probe_run_lock``「规则层与交付层入选同源」判红。
修法：交易日轴**由数据本身派生**（``canonical_trading_axis`` = 行情日 ∪ 打分日，**不用日历**）
＋ 唯一生产口 ``build_alpha_override`` ＋ 两层 CLI **同名同默认**。
⇒ 新增断言把「轴唯一 / 共享口唯一 / 引擎同源 / CLI 同名同默认」逐条钉死：

F 类 · 交易日轴唯一性（``canonical_trading_axis``）
  F1 轴 == 行情日 ∪ 打分日（都 ≤ asof）、升序去重、**不含** asof 之后的日期
  F2 ``prices``/``scores`` 为 None 或缺 ``date`` 列 ⇒ **不崩**
  F3 同一输入两次调用**逐位相同**（纯函数 ⇒ 轴唯一）

G 类 · 共享口一致性（``build_alpha_override`` 是**唯一**平滑表生产口）
  G1 override 表的列**恰好** == ``etf_code`` / ``smoothed_score`` / ``score_stability``
  G2 override 表**逐值等于** ``compute_smoothed_alpha(...).alpha`` 的对应三列
  G3 override 的 diagnostics 与 ``compute_smoothed_alpha`` 的 diagnostics **逐字一致**

H 类 · 引擎同源保证（**核心**：内存夹具上跑真的 ``build_portfolio``）
  H1 引擎内部自算的平滑表 == 直接调 ``build_alpha_override`` 的表（**逐值一致**，且只调 1 次）
  H2 开启平滑 ⇒ ``alpha_score_used`` == **平滑分**（且至少一只确实 ≠ 原始分）
  H3 不传 ``smoothing_config`` ⇒ ``alpha_score_used`` == **原始分**（= ``--no-smoothing`` 口径）
  H4 ``smoothing_config.enabled=False`` ⇒ ``alpha_score_used`` == **原始分**

I 类 · CLI 同名同默认（``analyze.py`` 与 ``recommend.py``，**AST 判定**，不许文本匹配）
  I1 两个文件的 5 个平滑 CLI 参数**都存在**（``--no-smoothing`` / ``--smoothing-method`` /
     ``--smoothing-window`` / ``--smoothing-alpha`` / ``--smoothing-history-days``）
  I2 两个文件的平滑默认值**都来自** ``config.py`` 的常量（``ENABLE_SCORE_SMOOTHING`` /
     ``SMOOTHING_METHOD`` / ``SMOOTHING_WINDOW`` / ``SMOOTHING_ALPHA`` /
     ``SMOOTHING_HISTORY_SPAN_DAYS``），且 argparse 层**不许写死字面量**默认值
  I3 ``config.py`` **真的定义**了这 5 个常量（单一事实源不许消失）
  I4 ``recommend.py`` 的 ``build_portfolio(...)`` 调用**必须**带 ``smoothing_config=``
     （规则层不许退回原始分排序 ⇒ 那正是 2026-11-09 判红的真缺陷）
  I5 ``analyze.py`` 的 ``build_portfolio(...)`` 调用**必须**带 ``alpha_override=`` 与
     ``smoothing_config=``（交付层与引擎同源）

K 类 · 新增断言的**变异反证**（每条结构性断言都必须有「换成错的实现 ⇒ 必须失败」的负例）
  K1 轴不过滤 asof ⇒ F1 必须失败
  K2 轴丢掉打分日 ⇒ F1 必须失败
  K3 轴不做 None 兜底 ⇒ F2 必须失败
  K4 共享口换一条轴 ⇒ G2 必须失败
  K5 共享口少一列 ⇒ G1 必须失败
  K6 引擎侧换一条轴（真缺陷复刻）⇒ H1/H2 必须失败
  K7 合并函数忽略覆盖表（真缺陷复刻：规则层用原始分排序）⇒ H2 必须失败
  K8 某个 CLI 参数被改名 ⇒ I1 必须失败
  K9 某个默认值被写死成字面量 ⇒ I2 必须失败
  K10 ``recommend.py`` 不把 ``smoothing_config`` 传给引擎 ⇒ I4 必须失败
  K11 ``analyze.py`` 不把 ``alpha_override`` 传给引擎 ⇒ I5 必须失败

【用法】
    ~/anaconda3/envs/uav/bin/python manu/etf_rotation/probes/probe_score_smoothing.py
    （退出码 0 = 全过，1 = 有 FAIL）

【副作用声明】
本探针是**纯函数 + 内存夹具**：不联网、不读生产 CSV、不写任何目录（含任何生产目录），
只在内存里用 pandas 造假数据；变异反证只**运行时替换**被测模块的函数对象并立即还原，
**不改被测模块源码**、不 import 任何生产数据模块。
"""
from __future__ import annotations

import ast
import math
import re
import sys
from bisect import bisect_left
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import config as C  # noqa: E402
import score_smoothing as SS  # noqa: E402
from engine.portfolio import build_portfolio  # noqa: E402

#: 被测模块的源码路径（E 类静态检查用）
MODULE_PATH = MODULE_DIR / "score_smoothing.py"

#: 规则层 / 交付层两个入口的源码路径（I 类 CLI 同名同默认用）
ANALYZE_PATH = MODULE_DIR / "analyze.py"
RECOMMEND_PATH = MODULE_DIR / "recommend.py"

#: 单一事实源（config.py）路径（I3 用）
CONFIG_PATH = MODULE_DIR / "config.py"

#: 夹具用的 5 个连续交易日轴（升序）
AXIS: list[pd.Timestamp] = list(pd.bdate_range("2026-11-02", periods=5))

#: 变异前先抓住原始函数对象（还原时用；避免变异体自我递归）
_ORIGINAL_SMOOTH_PANEL: Callable[..., pd.DataFrame] = SS.smooth_panel
_ORIGINAL_LOCF: Callable[..., tuple] | None = getattr(SS, "_locf_array", None)

#: 共享口与轴的原始函数对象（变异反证时用来还原；也用来算「期望值」）
_ORIGINAL_CANONICAL_AXIS: Callable[..., list] = SS.canonical_trading_axis
_ORIGINAL_BUILD_OVERRIDE: Callable[..., tuple] = SS.build_alpha_override
_ORIGINAL_APPLY_OVERRIDE: Callable[..., pd.DataFrame] = SS.apply_alpha_override

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打一条断言结果。

    🔴 必须带 `[PASS]` / `[FAIL]` 字面量（`run_gates.sh` 靠 `grep -c '\\[PASS\\]'` 计数）。
    """
    (PASS if ok else FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    emoji = "✅" if ok else "❌"
    print(f"  {mark} {emoji} {name}" + (f" —— {detail}" if detail else ""))


def _num(value: Any) -> float | None:
    """把可能是 ``None`` / NaN 的值转成 float（NaN ⇒ None），避免比较时炸掉。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _section(title: str) -> None:
    """打一个小节标题。"""
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 夹具


def _pulse_observations() -> pd.DataFrame:
    """A 类夹具：``PULSE`` 在 d5 单日暴涨 3 倍，``STEADY`` 恒定 2.0。"""
    rows: list[tuple[pd.Timestamp, str, float]] = [(day, "PULSE", 1.0) for day in AXIS[:-1]]
    rows.append((AXIS[-1], "PULSE", 3.0))
    rows.extend((day, "STEADY", 2.0) for day in AXIS)
    return pd.DataFrame(rows, columns=["date", "code", "score"])


def _locf_observations() -> pd.DataFrame:
    """B3 夹具：三条曲线，用来暴露 LOCF 阶梯是否生效。

    * ``EARLY_ONLY``：只在 d1 给一个观测（1.0）⇒ 有 LOCF 时轴上 d1~d5 全是 1.0；
    * ``FULL_ONE``：d1~d5 逐日都给 1.0 ⇒ 对照组；
    * ``EARLY_LATE``：d1 给 1.0、d5 补一个新观测 3.0 ⇒ 必须取「最近观测」。
    """
    rows: list[tuple[pd.Timestamp, str, float]] = [(AXIS[0], "EARLY_ONLY", 1.0)]
    rows.extend((day, "FULL_ONE", 1.0) for day in AXIS)
    rows.append((AXIS[0], "EARLY_LATE", 1.0))
    rows.append((AXIS[-1], "EARLY_LATE", 3.0))
    return pd.DataFrame(rows, columns=["date", "code", "score"])


def _holdings_fixture() -> pd.DataFrame:
    """W 夹具：两只 ETF × 两只成分股的权重快照（纯内存，不读生产 CSV）。"""
    return pd.DataFrame(
        [
            {"etf_code": "510300", "etf_name": "沪深300ETF",
             "instrument": "SH600000", "weight_pct": 60.0},
            {"etf_code": "510300", "etf_name": "沪深300ETF",
             "instrument": "SZ000001", "weight_pct": 40.0},
            {"etf_code": "510500", "etf_name": "中证500ETF",
             "instrument": "SH600519", "weight_pct": 50.0},
            {"etf_code": "510500", "etf_name": "中证500ETF",
             "instrument": "SZ000002", "weight_pct": 50.0},
        ]
    )


def _scores_fixture(dates: list[pd.Timestamp]) -> pd.DataFrame:
    """S 夹具：给定打分日造一份个股分数（列 date / instrument / score）。"""
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(dates):
        rows.append({"date": day, "instrument": "SH600000", "score": 1.0 + index})
        rows.append({"date": day, "instrument": "SZ000001", "score": -1.0 - index})
        rows.append({"date": day, "instrument": "SH600519", "score": 0.5})
        rows.append({"date": day, "instrument": "SZ000002", "score": -0.5})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- A 类


def _a_class_results(smooth_fn: Callable[..., pd.DataFrame]) -> dict[str, bool]:
    """在给定平滑实现上跑 A 类五条断言，返回 ``{编号: 是否通过}``。"""
    result = smooth_fn(_pulse_observations(), AXIS, method="ewma", window=5, alpha=0.35)
    rows = {str(row["code"]): row for _, row in result.iterrows()}
    pulse, steady = rows["PULSE"], rows["STEADY"]
    pulse_smoothed = _num(pulse["smoothed_score"])
    steady_smoothed = _num(steady["smoothed_score"])
    pulse_raw = _num(pulse["raw_score"])
    pulse_stability = _num(pulse["score_stability"])
    steady_stability = _num(steady["score_stability"])
    ordered = [
        str(code)
        for code in result.sort_values("smoothed_score", ascending=False)["code"].tolist()
    ]
    expect_a2 = 0.35 * 3.0 + 0.65 * 1.0
    return {
        "A1": (
            pulse_smoothed is not None
            and pulse_raw is not None
            and pulse_smoothed < pulse_raw
        ),
        "A2": pulse_smoothed is not None and abs(pulse_smoothed - expect_a2) <= 1e-9,
        "A3": bool(ordered) and ordered[0] != "PULSE",
        "A4": (
            pulse_smoothed is not None
            and steady_smoothed is not None
            and pulse_smoothed < steady_smoothed
        ),
        "A5": (
            pulse_stability is not None
            and steady_stability is not None
            and pulse_stability > steady_stability
        ),
    }


def _run_a_class() -> None:
    """A 类：单日暴涨 3 倍的噪声不许劫持排名（需求 §4.1 验收点）。"""
    _section("A 类 · 脉冲假数据（噪声不许劫持排名）")
    result = _ORIGINAL_SMOOTH_PANEL(
        _pulse_observations(), AXIS, method="ewma", window=5, alpha=0.35
    )
    results = _a_class_results(_ORIGINAL_SMOOTH_PANEL)
    rows = {str(row["code"]): row for _, row in result.iterrows()}
    pulse, steady = rows["PULSE"], rows["STEADY"]
    ordered = [
        str(code)
        for code in result.sort_values("smoothed_score", ascending=False)["code"].tolist()
    ]
    check(
        "A1 脉冲标的的平滑分显著低于 raw 分（3.0 ⇒ 被压下去）",
        results["A1"],
        f"raw={_num(pulse['raw_score'])}｜smoothed={_num(pulse['smoothed_score'])}",
    )
    check(
        "A2 平滑分精确等于 0.35×3.0 + 0.65×1.0 = 1.70（容差 1e-9）",
        results["A2"],
        f"期望={0.35 * 3.0 + 0.65 * 1.0!r}｜实得={_num(pulse['smoothed_score'])!r}",
    )
    check(
        "A3 按平滑分降序，脉冲标的不排第 1（未在首日冲上 Top 1）",
        results["A3"],
        f"降序={ordered}",
    )
    check(
        "A4 脉冲标的的平滑分低于稳态标的（噪声没劫持排名）",
        results["A4"],
        f"PULSE={_num(pulse['smoothed_score'])}｜STEADY={_num(steady['smoothed_score'])}",
    )
    check(
        "A5 脉冲标的的 score_stability 高于稳态标的（脉冲更不稳定）",
        results["A5"],
        f"PULSE={_num(pulse['score_stability'])}｜STEADY={_num(steady['score_stability'])}",
    )


# ---------------------------------------------------------------- B 类


def _run_b_class() -> None:
    """B 类：EWMA / 滚动均值 / LOCF / 半衰期四个口径手算对账。"""
    _section("B 类 · 数值口径正确性")
    axis3 = AXIS[:3]
    series = pd.DataFrame(
        [(axis3[0], "X", 1.0), (axis3[1], "X", 2.0), (axis3[2], "X", 4.0)],
        columns=["date", "code", "score"],
    )
    ewma = _ORIGINAL_SMOOTH_PANEL(series, axis3, method="ewma", window=3, alpha=0.5)
    ewma_value = _num(ewma.iloc[0]["smoothed_score"])
    check(
        "B1 EWMA 手算：α=0.5、序列 [1,2,4] ⇒ 递推 1.0 → 1.5 → 2.75",
        ewma_value is not None and abs(ewma_value - 2.75) <= 1e-9,
        f"期望=2.75｜实得={ewma_value!r}",
    )
    rolling = _ORIGINAL_SMOOTH_PANEL(series, axis3, method="rolling", window=3)
    rolling_value = _num(rolling.iloc[0]["smoothed_score"])
    expect_rolling = (1.0 + 2.0 + 4.0) / 3.0
    check(
        "B2 Rolling 手算：window=3、序列 [1,2,4] ⇒ 末值 2.3333…",
        rolling_value is not None and abs(rolling_value - expect_rolling) <= 1e-9,
        f"期望={expect_rolling!r}｜实得={rolling_value!r}",
    )
    locf = _ORIGINAL_SMOOTH_PANEL(
        _locf_observations(), AXIS, method="ewma", window=5, alpha=0.35
    )
    locf_rows = {str(row["code"]): row for _, row in locf.iterrows()}
    early = _num(locf_rows["EARLY_ONLY"]["smoothed_score"])
    full = _num(locf_rows["FULL_ONE"]["smoothed_score"])
    late = _num(locf_rows["EARLY_LATE"]["smoothed_score"])
    check(
        "B3a LOCF 阶梯：只给 d1 的观测 ⇒ 轴上 d1~d5 全等于 d1 的值（与逐日给的对照逐位相等）",
        early is not None and full is not None and early == full,
        f"EARLY_ONLY={early!r}｜FULL_ONE={full!r}",
    )
    check(
        "B3b LOCF 取「最近观测」：d5 补一个新观测 3.0 ⇒ 结果不等于全 1.0 那条",
        late is not None and full is not None and late != full,
        f"EARLY_LATE={late!r}｜FULL_ONE={full!r}",
    )
    half_life = SS.effective_half_life_days(0.35)
    check(
        "B4 半衰期：effective_half_life_days(0.35) ≈ 1.609 且 < 3.0（按实际值，不照抄文档）",
        abs(half_life - 1.609) <= 1e-3 and half_life < 3.0,
        f"实得={half_life!r}｜需求文档写「3~5 个交易日」⇒ 实际只有约 1.61 个交易日",
    )


# ---------------------------------------------------------------- C 类


def _run_c_class() -> None:
    """C 类：边界与 fail-safe（宁可不产出，不产出假结果）。"""
    _section("C 类 · 边界与 fail-safe")
    solo = pd.DataFrame([(AXIS[0], "SOLO", 2.0)], columns=["date", "code", "score"])
    try:
        solo_result = _ORIGINAL_SMOOTH_PANEL(solo, AXIS, method="ewma", window=5, alpha=0.35)
        solo_row = solo_result.iloc[0]
        solo_ok = (
            _num(solo_row["smoothed_score"]) == _num(solo_row["raw_score"])
            and bool(solo_row["fallback_to_transient"]) is True
        )
        solo_detail = (
            f"raw={_num(solo_row['raw_score'])!r}｜smoothed={_num(solo_row['smoothed_score'])!r}"
            f"｜fallback={solo_row['fallback_to_transient']}"
        )
    except Exception as exc:  # noqa: BLE001
        solo_ok, solo_detail = False, f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "C1 只有 1 个观测点的新标的：平滑分 == raw 分、标记回退、不抛异常",
        solo_ok,
        solo_detail,
    )

    nan_obs = pd.DataFrame(
        [(AXIS[0], "NANCODE", np.nan), (AXIS[1], "NANCODE", np.nan)],
        columns=["date", "code", "score"],
    )
    try:
        nan_result = _ORIGINAL_SMOOTH_PANEL(nan_obs, AXIS, method="ewma", window=5, alpha=0.35)
        nan_codes = [str(code) for code in nan_result["code"].tolist()]
        nan_row = nan_result.iloc[0] if len(nan_result) else None
        nan_value = nan_row["smoothed_score"] if nan_row is not None else "缺行"
        nan_ok = (
            nan_row is not None
            and nan_value is None
            and _num(nan_row["raw_score"]) is None
            and "NANCODE" in nan_codes
        )
        nan_detail = f"codes={nan_codes}｜smoothed_score={nan_value!r}（None，不是 NaN、不是 0）"
    except Exception as exc:  # noqa: BLE001
        nan_ok, nan_detail = False, f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "C2a 观测点全 NaN 的标的不抛 KeyError、不产出 NaN（写 None）",
        nan_ok,
        nan_detail,
    )

    ghost_obs = pd.DataFrame([(AXIS[0], "REAL", 1.0)], columns=["date", "code", "score"])
    try:
        ghost_result = _ORIGINAL_SMOOTH_PANEL(
            ghost_obs, AXIS, method="ewma", window=5, alpha=0.35
        )
        ghost_codes = [str(code) for code in ghost_result["code"].tolist()]
        ghost_ok = "REAL" in ghost_codes and "GHOST" not in ghost_codes
        ghost_detail = f"codes={ghost_codes}（没有观测的标的不许被凭空造出来）"
    except Exception as exc:  # noqa: BLE001
        ghost_ok, ghost_detail = False, f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "C2b 观测点整行缺失的标的被剔除（不产出空行、不产出 NaN）",
        ghost_ok,
        ghost_detail,
    )

    oneshot = pd.DataFrame([(AXIS[-1], "ONESHOT", 1.5)], columns=["date", "code", "score"])
    try:
        oneshot_result = _ORIGINAL_SMOOTH_PANEL(
            oneshot, AXIS, method="ewma", window=5, alpha=0.35
        )
        stability = oneshot_result.iloc[0]["score_stability"]
        oneshot_ok = stability is None
        oneshot_detail = f"score_stability={stability!r}（有效点 < 2 ⇒ None，不是 0）"
    except Exception as exc:  # noqa: BLE001
        oneshot_ok, oneshot_detail = False, f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "C3 有效点 < 2 时 score_stability 是 None（不是 0）",
        oneshot_ok,
        oneshot_detail,
    )

    bad_params = [
        ("method='ma'", {"method": "ma"}),
        ("window=0", {"window": 0}),
        ("alpha=0", {"alpha": 0}),
        ("alpha=1.5", {"alpha": 1.5}),
    ]
    config_bad: list[str] = []
    entry_bad: list[str] = []
    for label, kwargs in bad_params:
        try:
            SS.SmoothingConfig(enabled=True, **kwargs).validated()
            config_bad.append(f"{label}: 没抛")
        except Exception as exc:  # noqa: BLE001
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", str(exc)))
            if not (isinstance(exc, ValueError) and has_cjk):
                config_bad.append(f"{label}: {type(exc).__name__}={exc}")
        try:
            _ORIGINAL_SMOOTH_PANEL(_pulse_observations(), AXIS, **kwargs)
            entry_bad.append(f"{label}: 没抛")
        except Exception as exc:  # noqa: BLE001
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", str(exc)))
            if not (isinstance(exc, ValueError) and has_cjk):
                entry_bad.append(f"{label}: {type(exc).__name__}={exc}")
    check(
        "C4a 非法参数（method/window/alpha）⇒ SmoothingConfig.validated() 抛中文 ValueError",
        not config_bad,
        f"不合规={config_bad or '无'}（四组全过）",
    )
    check(
        "C4b 非法参数 ⇒ smooth_panel 入口同样抛中文 ValueError",
        not entry_bad,
        f"不合规={entry_bad or '无'}（四组全过）",
    )

    config = SS.SmoothingConfig(enabled=True, method="ewma", window=5, alpha=0.35)
    try:
        outcome = SS.compute_smoothed_alpha(
            _holdings_fixture(),
            _scores_fixture([AXIS[-1]]),
            AXIS[-1],
            list(AXIS),
            config,
        )
        diagnostics = outcome.diagnostics
        warning = diagnostics.get("warning")
        reason = diagnostics.get("fallback_reason")
        c5_ok = warning == SS.FALLBACK_WARNING and bool(reason)
        c5_detail = f"warning={warning!r}｜fallback_reason={reason!r}"
    except Exception as exc:  # noqa: BLE001
        c5_ok, c5_detail = False, f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "C5 全局没有历史观测日 ⇒ warning 与 FALLBACK_WARNING 逐字相等、fallback_reason 非空",
        c5_ok,
        c5_detail,
    )

    normal_columns = tuple(
        _ORIGINAL_SMOOTH_PANEL(
            _pulse_observations(), AXIS, method="ewma", window=5, alpha=0.35
        ).columns
    )
    empty_columns = tuple(
        _ORIGINAL_SMOOTH_PANEL(pd.DataFrame(columns=["date", "code", "score"]), AXIS).columns
    )
    expect_columns = tuple(SS.RESULT_COLUMNS)
    c6_ok = normal_columns == expect_columns and empty_columns == expect_columns
    check(
        "C6 smooth_panel 返回列恰好等于 RESULT_COLUMNS（含顺序、空输入早退路径）",
        c6_ok,
        f"正常={list(normal_columns)}｜空输入={list(empty_columns)}｜期望={list(expect_columns)}",
    )


# ---------------------------------------------------------------- D 类


def _mutant_no_smoothing(
    observations: pd.DataFrame, axis: Any, **kwargs: Any
) -> pd.DataFrame:
    """变异体：把「EWMA 平滑」换成「直接取 raw，不平滑」。"""
    base = _ORIGINAL_SMOOTH_PANEL(observations, axis, **kwargs)
    out = base.copy()
    out["smoothed_score"] = out["raw_score"]
    return out


def _mutant_no_locf(
    obs_dates: Any, obs_values: Any, axis: list[pd.Timestamp]
) -> tuple[np.ndarray, int, int | None, pd.Timestamp | None]:
    """变异体：把「LOCF 阶梯」换成「只有观测点本身、其余轴上空缺」。"""
    locf = np.full(len(axis), np.nan, dtype=float)
    stamped: list[tuple[int, float, pd.Timestamp]] = []
    for date, value in zip(obs_dates, obs_values):
        if value is None or not np.isfinite(float(value)):
            continue
        day = pd.Timestamp(date).normalize()
        pos = bisect_left(axis, day)
        if pos >= len(axis):
            continue
        stamped.append((pos, float(value), day))
    if not stamped:
        return locf, 0, None, None
    per_index: dict[int, tuple[float, pd.Timestamp]] = {}
    for pos, value, day in stamped:
        per_index[pos] = (value, day)
    ordered = sorted(per_index.items())
    for pos, (value, _day) in ordered:
        locf[pos] = value
    last_pos, last_pair = ordered[-1]
    return locf, len(stamped), last_pos, last_pair[1]


def _b3_results(smooth_fn: Callable[..., pd.DataFrame]) -> dict[str, bool]:
    """在给定平滑实现上跑 B3 两条 LOCF 断言。"""
    result = smooth_fn(_locf_observations(), AXIS, method="ewma", window=5, alpha=0.35)
    rows = {str(row["code"]): row for _, row in result.iterrows()}
    early = _num(rows["EARLY_ONLY"]["smoothed_score"])
    full = _num(rows["FULL_ONE"]["smoothed_score"])
    late = _num(rows["EARLY_LATE"]["smoothed_score"])
    return {
        "B3a": early is not None and full is not None and early == full,
        "B3b": late is not None and full is not None and late != full,
    }


def _run_mutation(
    label: str, runner: Callable[[], dict[str, bool]], must_fail: set[str]
) -> tuple[bool, str]:
    """跑一次变异反证：构造变异体 → 跑断言 → 指定断言**必须失败**。

    返回 ``(是否通过, 明细)``；**任何异常都被吞掉并判 FAIL**（不许崩掉整个探针）。
    """
    try:
        mutated = runner()
    except Exception as exc:  # noqa: BLE001
        return False, f"{label}: 变异构造/执行抛异常 ⇒ 判 FAIL（{type(exc).__name__}: {exc}）"
    still_ok = [key for key in sorted(must_fail) if mutated.get(key, True)]
    return (not still_ok), (
        f"{label}: 变异体下仍然通过的断言={still_ok or '无'}（这些必须失败）"
    )


def _runner_no_smoothing() -> dict[str, bool]:
    """变异：把「EWMA 平滑」换成「直接取 raw，不平滑」，跑 A 类断言。"""
    SS.smooth_panel = _mutant_no_smoothing  # type: ignore[assignment]
    try:
        return _a_class_results(SS.smooth_panel)
    finally:
        SS.smooth_panel = _ORIGINAL_SMOOTH_PANEL  # type: ignore[assignment]


def _runner_no_locf() -> dict[str, bool]:
    """变异：把「LOCF 阶梯」换成「只有观测点本身、其余轴上空缺」，跑 B3 断言。"""
    SS._locf_array = _mutant_no_locf  # type: ignore[attr-defined]
    try:
        return _b3_results(_ORIGINAL_SMOOTH_PANEL)
    finally:
        SS._locf_array = _ORIGINAL_LOCF  # type: ignore[attr-defined]


def _runner_boom() -> dict[str, bool]:
    """变异：故意抛异常（用来验 D3：变异炸了 ⇒ 判 FAIL，不许崩探针）。"""
    raise RuntimeError("变异体故意抛异常")


def _run_d_class() -> None:
    """D 类：变异反证（闸门有牙，铁律十四.52）。"""
    _section("D 类 · 变异反证（把行为换成错的实现 ⇒ 相关断言必须失败）")

    d1_ok, d1_detail = _run_mutation(
        "D1 不平滑变异", _runner_no_smoothing, {"A1", "A2", "A3"}
    )
    check(
        "D1 变异「直接取 raw 不平滑」⇒ A1/A2/A3 必须失败",
        d1_ok,
        d1_detail,
    )

    if _ORIGINAL_LOCF is None:
        d2_ok, d2_detail = False, "找不到被测模块的 LOCF 实现（_locf_array）⇒ 无法构造变异"
    else:
        d2_ok, d2_detail = _run_mutation("D2 去掉 LOCF 变异", _runner_no_locf, {"B3a"})
    check(
        "D2 变异「去掉 LOCF 阶梯」⇒ B3 必须失败",
        d2_ok,
        d2_detail,
    )

    try:
        d3_ok, d3_detail = _run_mutation("D3 自检", _runner_boom, {"A1"})
        d3_ok = d3_ok is False
        d3_detail = d3_detail + "｜期望：判 FAIL 且探针不崩"
    except Exception as exc:  # noqa: BLE001
        d3_ok, d3_detail = False, f"探针自身崩了：{type(exc).__name__}: {exc}"
    check(
        "D3 变异构造/执行抛异常 ⇒ 判 FAIL 且不许崩掉整个探针",
        d3_ok,
        d3_detail,
    )


# ---------------------------------------------------------------- E 类


def _run_e_class() -> None:
    """E 类：对被测模块源码跑静态检查（铁律十四.57 的同款口径）。"""
    _section("E 类 · 源码静态检查（AST 判定，不许文本匹配）")
    try:
        source = MODULE_PATH.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        check("E1~E5 静态检查", False, f"读不到源码：{exc}")
        return
    lines = source.splitlines()
    tree = ast.parse(source)
    funcs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    tab_lines = [index + 1 for index, line in enumerate(lines) if "\t" in line]
    check(
        "E1 源码不用 tab 缩进（4 空格口径）",
        not tab_lines,
        f"含 tab 的行={tab_lines[:5] or '无'}｜总行数={len(lines)}",
    )
    bad_names = [
        node.name for node in funcs if not re.fullmatch(r"_?[a-z][a-z0-9_]*", node.name)
    ]
    check(
        "E2 所有函数名是 snake_case（允许前导 _）",
        not bad_names,
        f"不合规={bad_names or '无'}｜函数数={len(funcs)}",
    )
    no_doc = [node.name for node in funcs if not ast.get_docstring(node)]
    no_cjk = [
        node.name
        for node in funcs
        if ast.get_docstring(node)
        and not re.search(r"[\u4e00-\u9fff]", ast.get_docstring(node) or "")
    ]
    check(
        "E3 每个函数都有含中文的 docstring",
        not no_doc and not no_cjk,
        f"缺 docstring={no_doc or '无'}｜docstring 无中文={no_cjk or '无'}",
    )
    fillna_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fillna"
    ]
    zero_calls = []
    for node in fillna_calls:
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, (int, float))
            and not isinstance(node.args[0].value, bool)
            and node.args[0].value == 0
        ):
            source_line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            zero_calls.append(
                f"line {node.lineno}: {ast.unparse(node.func.value)}.fillna(0)｜原文「{source_line}」"
            )
    check(
        "E4 源码无 fillna(0) 调用（AST 判定：宁可不产出，不产出假结果）",
        not zero_calls,
        f"fillna(0) 出现于={zero_calls or '无'}｜fillna 调用总数={len(fillna_calls)}",
    )
    score_fillna = []
    for node in fillna_calls:
        target = ast.unparse(node.func.value)
        if "score" in target.lower() or "stability" in target.lower():
            score_fillna.append(f"line {node.lineno}: {target}.fillna(...)")
    check(
        "E4b 分数/稳定性列不得用 fillna 蒙混（分数列本身必须干净）",
        not score_fillna,
        f"命中={score_fillna or '无'}",
    )
    module_doc = ast.get_docstring(tree)
    check(
        "E5 模块级 docstring 存在且含中文",
        bool(module_doc) and bool(re.search(r"[\u4e00-\u9fff]", module_doc or "")),
        f"模块 docstring 长度={len(module_doc or '')}",
    )


# ---------------------------------------------------------------- F/G/H/I 类夹具

#: H 类夹具的信号日（周五）
_FIXTURE_ASOF = pd.Timestamp("2026-11-06")

#: H 类夹具的行情日长度（**由 config 派生** ⇒ ``MIN_HISTORY_DAYS`` 变了夹具不会失效）
_FIXTURE_N_DAYS: int = max(int(C.MIN_HISTORY_DAYS) + 10, 130)

#: H 类夹具的行情日（≥ 121 根 ⇒ ``mom120`` 可算；≥ ``MIN_HISTORY_DAYS`` ⇒ 过流动性门槛）
_FIXTURE_DAYS: list[pd.Timestamp] = list(
    pd.bdate_range(end=_FIXTURE_ASOF, periods=_FIXTURE_N_DAYS)
)


def _fixture_score_days() -> list[pd.Timestamp]:
    """H 类夹具的 3 个打分日（**由 config 派生**）。

    🔴 必须落在**平滑轴截断窗口**（最后 ``SMOOTHING_HISTORY_SPAN_DAYS`` 个交易日）之内
    ⇒ 才有「历史观测点」⇒ 平滑才真的生效 ⇒ 断言不空洞。
    """
    span = max(int(C.SMOOTHING_HISTORY_SPAN_DAYS), 3)
    window = _FIXTURE_DAYS[-span:]
    step = max(len(window) // 3, 1)
    picks = {window[-1], window[-1 - step], window[-1 - 2 * step]}
    return sorted(picks)

#: H 类夹具的三只 ETF（代码 / 名称 / 基准价 / 日漂移 / 正弦周期 / 相位）
#: 🔴 名称**故意不含** ``config.THEME_FAMILY_KEYWORDS`` 的关键词 ⇒ 不会被主题聚类并成一簇
_FIXTURE_ETFS: tuple[tuple[str, str, float, float, int, float], ...] = (
    ("510300", "样本甲ETF", 4.0, 0.0006, 37, 0.0),
    ("510500", "样本乙ETF", 6.0, 0.0008, 53, 1.1),
    ("512100", "样本丙ETF", 2.0, 0.0010, 71, 2.3),
)

#: H 类夹具的成分股权重（每只 ETF 两个成分股、互不重叠 ⇒ 不会被持仓重叠判据并簇）
_FIXTURE_HOLDINGS: tuple[tuple[str, str, str, float], ...] = (
    ("510300", "样本甲ETF", "SH600000", 60.0),
    ("510300", "样本甲ETF", "SZ000001", 40.0),
    ("510500", "样本乙ETF", "SH600519", 50.0),
    ("510500", "样本乙ETF", "SZ000002", 50.0),
    ("512100", "样本丙ETF", "SH601318", 70.0),
    ("512100", "样本丙ETF", "SZ000651", 30.0),
)


def _fixture_prices() -> pd.DataFrame:
    """H 类夹具：130 个交易日的 ETF 日线（**纯内存生成**，不读生产 CSV、不联网）。"""
    rows: list[dict[str, Any]] = []
    for code, _name, base, drift, period, phase in _FIXTURE_ETFS:
        for index, day in enumerate(_FIXTURE_DAYS):
            close = (
                base
                * (1 + drift * index)
                * (1 + 0.002 * math.sin(2 * math.pi * index / period + phase))
            )
            rows.append(
                {
                    "date": day,
                    "code": code,
                    "open": close,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "close": close,
                    "volume": 1.0e7,
                    "amount": 1.2e8,
                }
            )
    return pd.DataFrame(rows)


def _fixture_universe() -> pd.DataFrame:
    """H 类夹具：候选池（成交额 / 规模都过流动性门槛；桶 = 权益 ⇒ 不走防守旁路）。"""
    return pd.DataFrame(
        [
            {
                "code": code,
                "name": name,
                "avg_amount_20d": 1.2e8,
                "float_mcap": 1.0e10,
                "last_price": 3.0,
                "fund_type_bucket": "equity_cn",
            }
            for code, name, *_rest in _FIXTURE_ETFS
        ]
    )


def _fixture_holdings() -> pd.DataFrame:
    """H 类夹具：成分股权重快照（W）。"""
    return pd.DataFrame(
        list(_FIXTURE_HOLDINGS),
        columns=["etf_code", "etf_name", "instrument", "weight_pct"],
    )


def _fixture_scores() -> pd.DataFrame:
    """H 类夹具：3 个打分日的个股分数（逐日变动 ⇒ 平滑分 ≠ 原始分 ⇒ 断言不空洞）。"""
    rows: list[dict[str, Any]] = []
    for step, day in enumerate(_fixture_score_days()):
        values = {
            "SH600000": 1.0 + 0.3 * step,
            "SZ000001": -0.8 - 0.1 * step,
            "SH600519": 0.2 + 0.5 * step,
            "SZ000002": -0.3 - 0.4 * step,
            "SH601318": 0.9 - 0.2 * step,
            "SZ000651": -0.6 + 0.25 * step,
        }
        for instrument, score in values.items():
            rows.append({"date": day, "instrument": instrument, "score": score})
    return pd.DataFrame(rows)


def _fixture_config() -> SS.SmoothingConfig:
    """H 类夹具用的平滑参数（**取自 config.py** ⇒ 配置漂移会当场变红）。"""
    return SS.SmoothingConfig(
        enabled=True,
        method=str(C.SMOOTHING_METHOD),
        window=int(C.SMOOTHING_WINDOW),
        alpha=float(C.SMOOTHING_ALPHA),
        history_span_days=int(C.SMOOTHING_HISTORY_SPAN_DAYS),
    )


def _fixture_axis_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp, list[pd.Timestamp]]:
    """F 类夹具：行情日 ∪ 打分日（**故意含 asof 之后的日期** ⇒ 用来验 asof 过滤）。

    返回 ``(prices, scores, asof, 期望轴)``；期望轴 = 行情日 ∪ 打分日 去掉 asof 之后的部分。
    """
    asof = pd.Timestamp("2026-11-06")
    price_days = [asof + pd.Timedelta(days=offset) for offset in (-3, -1, 0, 2)]
    score_days = [asof + pd.Timedelta(days=offset) for offset in (-2, 0, 1)]
    prices = pd.DataFrame(
        {"date": price_days, "code": ["AAA"] * 4, "close": [1.0, 2.0, 3.0, 4.0]}
    )
    scores = pd.DataFrame(
        {
            "date": score_days,
            "instrument": ["SH600000"] * 3,
            "score": [1.0, 2.0, 3.0],
        }
    )
    expect = [asof + pd.Timedelta(days=offset) for offset in (-3, -2, -1, 0)]
    return prices, scores, asof, expect


def _frames_equal(left: pd.DataFrame | None, right: pd.DataFrame | None) -> bool:
    """两张表**逐值**相等（列序一致、数值含 NaN 逐位相等、代码逐字相等）。"""
    if left is None or right is None:
        return False
    if list(left.columns) != list(right.columns):
        return False
    if len(left) != len(right):
        return False
    left = left.reset_index(drop=True)
    right = right.reset_index(drop=True)
    for column in left.columns:
        if column == "etf_code":
            if left[column].astype(str).tolist() != right[column].astype(str).tolist():
                return False
            continue
        left_values = pd.to_numeric(left[column], errors="coerce").to_numpy(dtype=float)
        right_values = pd.to_numeric(right[column], errors="coerce").to_numpy(dtype=float)
        if not np.array_equal(left_values, right_values, equal_nan=True):
            return False
    return True


# ---------------------------------------------------------------- F 类


def _f_results(axis_fn: Callable[..., Any]) -> dict[str, bool]:
    """F 类：交易日轴唯一性（在给定的轴实现上跑一遍）。"""
    prices, scores, asof, expect = _fixture_axis_inputs()
    results: dict[str, bool] = {}
    try:
        axis = list(axis_fn(prices, scores, asof))
        got = [pd.Timestamp(day).normalize() for day in axis]
        results["F1"] = got == expect
    except Exception:  # noqa: BLE001
        results["F1"] = False
    robust = True
    for args in (
        (None, scores, asof),
        (prices, None, asof),
        (None, None, asof),
        (prices.drop(columns=["date"]), scores, asof),
        (prices, scores.drop(columns=["date"]), asof),
    ):
        try:
            out = axis_fn(*args)
            if not isinstance(out, list):
                robust = False
        except Exception:  # noqa: BLE001
            robust = False
    results["F2"] = robust
    try:
        first = [pd.Timestamp(day).normalize() for day in axis_fn(prices, scores, asof)]
        second = [pd.Timestamp(day).normalize() for day in axis_fn(prices, scores, asof)]
        results["F3"] = first == second
    except Exception:  # noqa: BLE001
        results["F3"] = False
    return results


def _run_f_class() -> None:
    """F 类：交易日轴唯一性（canonical_trading_axis）。"""
    _section("F 类 · 交易日轴唯一性（行情日 ∪ 打分日，≤ asof）")
    results = _f_results(_ORIGINAL_CANONICAL_AXIS)
    prices, scores, asof, expect = _fixture_axis_inputs()
    try:
        axis = [pd.Timestamp(day).normalize() for day in _ORIGINAL_CANONICAL_AXIS(prices, scores, asof)]
        got_text = "|".join(day.date().isoformat() for day in axis)
    except Exception as exc:  # noqa: BLE001
        axis = []
        got_text = f"抛异常：{type(exc).__name__}: {exc}"
    check(
        "F1 轴 == 行情日 ∪ 打分日（≤ asof）、升序去重、不含 asof 之后的日期",
        results["F1"],
        f"实得={got_text}｜期望={'|'.join(day.date().isoformat() for day in expect)}",
    )
    check(
        "F2 prices/scores 为 None 或缺 date 列 ⇒ 不崩（返回列表）",
        results["F2"],
        "5 组退化输入（None×2 / 缺 date 列×2 / 全 None）全部未抛异常",
    )
    check(
        "F3 同一输入两次调用逐位相同（纯函数 ⇒ 轴唯一）",
        results["F3"],
        f"两次结果一致={results['F3']}",
    )


# ---------------------------------------------------------------- G 类


def _g_results(build_fn: Callable[..., Any]) -> dict[str, bool]:
    """G 类：共享口一致性（在给定的 ``build_alpha_override`` 实现上跑一遍）。"""
    holdings = _fixture_holdings()
    scores = _fixture_scores()
    prices = _fixture_prices()
    config = _fixture_config()
    results: dict[str, bool] = {}
    override = None
    diagnostics = None
    try:
        override, diagnostics = build_fn(
            holdings, scores, prices, _FIXTURE_ASOF, config, C.MIN_E_COVERAGE
        )
        results["G1"] = list(override.columns) == [
            "etf_code",
            "smoothed_score",
            "score_stability",
        ]
    except Exception:  # noqa: BLE001
        results["G1"] = False
    try:
        reference_axis = _ORIGINAL_CANONICAL_AXIS(prices, scores, _FIXTURE_ASOF)
        outcome = SS.compute_smoothed_alpha(
            holdings, scores, _FIXTURE_ASOF, reference_axis, config, C.MIN_E_COVERAGE
        )
        reference = outcome.alpha[["etf_code", "smoothed_score", "score_stability"]]
        results["G2"] = _frames_equal(override, reference)
        results["G3"] = diagnostics is not None and diagnostics == outcome.diagnostics
    except Exception:  # noqa: BLE001
        results["G2"] = False
        results["G3"] = False
    return results


def _run_g_class() -> None:
    """G 类：共享口一致性（build_alpha_override 是唯一的平滑表生产口）。"""
    _section("G 类 · 共享口一致性（唯一平滑表生产口）")
    results = _g_results(_ORIGINAL_BUILD_OVERRIDE)
    holdings = _fixture_holdings()
    scores = _fixture_scores()
    prices = _fixture_prices()
    config = _fixture_config()
    override, diagnostics = _ORIGINAL_BUILD_OVERRIDE(
        holdings, scores, prices, _FIXTURE_ASOF, config, C.MIN_E_COVERAGE
    )
    check(
        "G1 override 表的列恰好 == etf_code / smoothed_score / score_stability",
        results["G1"],
        f"实得={list(override.columns)}",
    )
    reference_axis = _ORIGINAL_CANONICAL_AXIS(prices, scores, _FIXTURE_ASOF)
    outcome = SS.compute_smoothed_alpha(
        holdings, scores, _FIXTURE_ASOF, reference_axis, config, C.MIN_E_COVERAGE
    )
    reference = outcome.alpha[["etf_code", "smoothed_score", "score_stability"]]
    check(
        "G2 override 表逐值 == compute_smoothed_alpha(...).alpha 的对应三列",
        results["G2"],
        f"override={override['smoothed_score'].round(6).tolist()}"
        f"｜reference={reference['smoothed_score'].round(6).tolist()}",
    )
    check(
        "G3 override 的 diagnostics 与 compute_smoothed_alpha 的 diagnostics 逐字一致",
        results["G3"],
        f"观测日={diagnostics.get('observation_dates')}｜轴长={diagnostics.get('axis_len')}",
    )


# ---------------------------------------------------------------- H 类


def _h_run_engine(smoothing_config: SS.SmoothingConfig | None) -> tuple[list, dict | None]:
    """跑一次真的 ``build_portfolio``，并记录**引擎内部用的**平滑表。

    记录手法 = **运行时替换** ``SS.build_alpha_override`` 成一个转发壳（``finally`` 里还原）
    ⇒ **不改任何生产模块源码**，且还原后模块状态与调用前逐位一致。
    """
    calls: list[pd.DataFrame] = []
    original_build = SS.build_alpha_override

    def _recorder(*args: Any, **kwargs: Any) -> Any:
        produced = original_build(*args, **kwargs)
        table = produced[0] if isinstance(produced, tuple) else produced
        calls.append(table.copy())
        return produced

    SS.build_alpha_override = _recorder  # type: ignore[assignment]
    try:
        result = build_portfolio(
            asof=_FIXTURE_ASOF,
            prices=_fixture_prices(),
            holdings=_fixture_holdings(),
            scores=_fixture_scores(),
            universe=_fixture_universe(),
            smoothing_config=smoothing_config,
        )
    finally:
        SS.build_alpha_override = original_build  # type: ignore[assignment]
    return calls, result


def _h_results() -> dict[str, bool]:
    """H 类：引擎同源保证（在**当前**的共享口 / 合并函数实现上跑一遍）。"""
    holdings = _fixture_holdings()
    scores = _fixture_scores()
    prices = _fixture_prices()
    config = _fixture_config()
    results: dict[str, bool] = {}
    expected, _expected_diag = _ORIGINAL_BUILD_OVERRIDE(
        holdings, scores, prices, _FIXTURE_ASOF, config, C.MIN_E_COVERAGE
    )
    smooth_map = {
        str(code): float(value)
        for code, value in zip(expected["etf_code"], expected["smoothed_score"])
    }
    calls: list = []
    result = None
    try:
        calls, result = _h_run_engine(config)
        results["H1"] = (
            len(calls) == 1
            and _frames_equal(calls[0], expected[["etf_code", "smoothed_score", "score_stability"]])
        )
    except Exception:  # noqa: BLE001
        results["H1"] = False
    try:
        used_map = {
            str(row["code"]): float(row["alpha_score_used"])
            for row in result["top_candidates"]
        }
        raw_map = {
            str(row["code"]): float(row["alpha_score"])
            for row in result["top_candidates"]
        }
        same = bool(used_map) and set(used_map).issubset(set(smooth_map))
        for code, used in used_map.items():
            if abs(used - smooth_map[code]) > 1e-4:
                same = False
        # 🔴 至少一只的平滑分必须真的 ≠ 原始分 ⇒ 否则这条断言是空洞的
        differs = any(abs(used_map[code] - raw_map[code]) > 1e-9 for code in used_map)
        results["H2"] = bool(same and differs)
    except Exception:  # noqa: BLE001
        results["H2"] = False
    for key, config_arg in (
        ("H3", None),
        ("H4", SS.SmoothingConfig(
            enabled=False,
            method=str(C.SMOOTHING_METHOD),
            window=int(C.SMOOTHING_WINDOW),
            alpha=float(C.SMOOTHING_ALPHA),
            history_span_days=int(C.SMOOTHING_HISTORY_SPAN_DAYS),
        )),
    ):
        try:
            extra_calls, extra_result = _h_run_engine(config_arg)
            used = {
                str(row["code"]): float(row["alpha_score_used"])
                for row in extra_result["top_candidates"]
            }
            raw = {
                str(row["code"]): float(row["alpha_score"])
                for row in extra_result["top_candidates"]
            }
            equal = bool(used) and all(abs(used[code] - raw[code]) <= 1e-9 for code in used)
            if key == "H3":
                # 🔴 不传 config ⇒ 引擎**不许**自己去调共享口（不许偷偷平滑）
                equal = equal and len(extra_calls) == 0
            results[key] = bool(equal)
        except Exception:  # noqa: BLE001
            results[key] = False
    return results


def _run_h_class() -> None:
    """H 类：引擎同源保证（内存夹具上跑真的 build_portfolio）。"""
    _section("H 类 · 引擎同源保证（build_portfolio 自算的表 == 共享口的表）")
    results = _h_results()
    holdings = _fixture_holdings()
    scores = _fixture_scores()
    prices = _fixture_prices()
    config = _fixture_config()
    expected, _diag = _ORIGINAL_BUILD_OVERRIDE(
        holdings, scores, prices, _FIXTURE_ASOF, config, C.MIN_E_COVERAGE
    )
    calls, result = _h_run_engine(config)
    check(
        "H1 引擎内部自算的平滑表 == 直接调 build_alpha_override 的表（逐值一致）",
        results["H1"],
        f"引擎调用次数={len(calls)}｜共享口平滑分="
        f"{[round(float(v), 6) for v in expected['smoothed_score']]}",
    )
    used_text = "|".join(
        f"{row['code']}:used={row['alpha_score_used']},raw={row['alpha_score']}"
        for row in result["top_candidates"]
    )
    check(
        "H2 开启平滑 ⇒ alpha_score_used == 平滑分（且至少一只 ≠ 原始分）",
        results["H2"],
        used_text,
    )
    _calls_none, result_none = _h_run_engine(None)
    none_text = "|".join(
        f"{row['code']}:used={row['alpha_score_used']},raw={row['alpha_score']}"
        for row in result_none["top_candidates"]
    )
    check(
        "H3 不传 smoothing_config ⇒ alpha_score_used == 原始分（= --no-smoothing 口径）",
        results["H3"],
        none_text,
    )
    disabled = SS.SmoothingConfig(
        enabled=False,
        method=str(C.SMOOTHING_METHOD),
        window=int(C.SMOOTHING_WINDOW),
        alpha=float(C.SMOOTHING_ALPHA),
        history_span_days=int(C.SMOOTHING_HISTORY_SPAN_DAYS),
    )
    _calls_off, result_off = _h_run_engine(disabled)
    off_text = "|".join(
        f"{row['code']}:used={row['alpha_score_used']},raw={row['alpha_score']}"
        for row in result_off["top_candidates"]
    )
    check(
        "H4 smoothing_config.enabled=False ⇒ alpha_score_used == 原始分",
        results["H4"],
        off_text,
    )


# ---------------------------------------------------------------- I 类

#: 5 个平滑 CLI 参数（两层入口**必须同名**，少一个就分叉）
_SMOOTH_CLI_OPTIONS: tuple[str, ...] = (
    "--no-smoothing",
    "--smoothing-method",
    "--smoothing-window",
    "--smoothing-alpha",
    "--smoothing-history-days",
)

#: CLI 参数 ↔ (config.py 常量, argparse 落点, SmoothingConfig 形参)
_SMOOTH_CLI_SPEC: dict[str, tuple[str, str, str]] = {
    "--no-smoothing": ("ENABLE_SCORE_SMOOTHING", "no_smoothing", "enabled"),
    "--smoothing-method": ("SMOOTHING_METHOD", "smoothing_method", "method"),
    "--smoothing-window": ("SMOOTHING_WINDOW", "smoothing_window", "window"),
    "--smoothing-alpha": ("SMOOTHING_ALPHA", "smoothing_alpha", "alpha"),
    "--smoothing-history-days": (
        "SMOOTHING_HISTORY_SPAN_DAYS",
        "smoothing_history_days",
        "history_span_days",
    ),
}

#: config.py 必须提供的 5 个单一事实源常量
_CONFIG_SMOOTH_CONSTANTS: tuple[str, ...] = (
    "ENABLE_SCORE_SMOOTHING",
    "SMOOTHING_METHOD",
    "SMOOTHING_WINDOW",
    "SMOOTHING_ALPHA",
    "SMOOTHING_HISTORY_SPAN_DAYS",
)


def _i_file_results(tree: ast.AST) -> dict[str, bool]:
    """I 类：单个入口文件的 AST 判定（CLI 参数齐全 + 默认值来自 config 常量）。"""
    results: dict[str, bool] = {"I1": False, "I2": False}
    add_argument_calls: list[ast.Call] = []
    options: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            add_argument_calls.append(node)
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                options.add(node.args[0].value)
    results["I1"] = all(option in options for option in _SMOOTH_CLI_OPTIONS)
    # ---- I2a：argparse 层不许写死字面量默认值（写死 = 绕过 config 单一事实源）----
    literal_defaults: list[str] = []
    for node in add_argument_calls:
        if not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in _SMOOTH_CLI_OPTIONS
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "default":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is not None:
                literal_defaults.append(ast.unparse(value))
    # ---- I2b：SmoothingConfig 的每个形参都必须「CLI 覆盖 or config 常量」----
    config_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "SmoothingConfig"
    ]
    refs_ok = bool(config_calls)
    missing: list[str] = []
    for call in config_calls:
        for option, (const_name, arg_name, field) in _SMOOTH_CLI_SPEC.items():
            value = None
            for keyword in call.keywords:
                if keyword.arg == field:
                    value = keyword.value
                    break
            if value is None:
                refs_ok = False
                missing.append(f"{option}:缺形参 {field}")
                continue
            names = {
                node.attr for node in ast.walk(value) if isinstance(node, ast.Attribute)
            }
            if const_name not in names:
                refs_ok = False
                missing.append(f"{option}:未引用 C.{const_name}")
            if arg_name not in names:
                refs_ok = False
                missing.append(f"{option}:未引用 args.{arg_name}")
    results["I2"] = bool(refs_ok and not literal_defaults)
    results["_I2_detail"] = f"写死默认值={literal_defaults or '无'}｜口径问题={missing or '无'}"
    return results


def _i_build_portfolio_kwargs(tree: ast.AST) -> list[list[str]]:
    """取出源码里每个 ``build_portfolio(...)`` 调用的关键字参数名（AST 判定）。

    🔴 **必须排序后再返回**：集合的打印顺序受 ``PYTHONHASHSEED`` 影响 ⇒
    不排序会让探针连跑两次的输出不一致（幂等性红线）。
    """
    out: list[list[str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_portfolio"
        ):
            out.append(
                sorted(keyword.arg for keyword in node.keywords if keyword.arg)
            )
    return out


def _i_portfolio_results(tree: ast.AST, required: tuple[str, ...]) -> bool:
    """``build_portfolio(...)`` 调用是否**真的**带上了要求的参数（少一个就分叉）。"""
    calls = _i_build_portfolio_kwargs(tree)
    return bool(calls) and all(set(required).issubset(set(k)) for k in calls)


def _i_config_results() -> dict[str, bool]:
    """I3：config.py 真的定义了这 5 个常量（单一事实源不许消失）。"""
    try:
        tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"I3": False}
    assigned: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    missing = [name for name in _CONFIG_SMOOTH_CONSTANTS if name not in assigned]
    return {"I3": not missing}


def _run_i_class() -> None:
    """I 类：CLI 同名同默认（AST 判定，不许文本匹配）。"""
    _section("I 类 · CLI 同名同默认（analyze.py ↔ recommend.py ↔ config.py）")
    for label, path in (("analyze.py", ANALYZE_PATH), ("recommend.py", RECOMMEND_PATH)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            results = _i_file_results(tree)
            detail = results.get("_I2_detail", "")
            options = sorted(
                {
                    node.args[0].value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value in _SMOOTH_CLI_OPTIONS
                }
            )
        except Exception as exc:  # noqa: BLE001
            results = {"I1": False, "I2": False}
            detail = f"解析失败：{type(exc).__name__}: {exc}"
            options = []
        check(
            f"I1 {label} 的 5 个平滑 CLI 参数齐全（同名）",
            bool(results["I1"]),
            f"实得={options}",
        )
        check(
            f"I2 {label} 的平滑默认值来自 config.py 常量（不许写死字面量）",
            bool(results["I2"]),
            str(detail),
        )
    config_results = _i_config_results()
    check(
        "I3 config.py 定义了 5 个平滑单一事实源常量",
        bool(config_results["I3"]),
        f"常量={list(_CONFIG_SMOOTH_CONSTANTS)}",
    )
    for label, path, required, key in (
        (
            "recommend.py",
            RECOMMEND_PATH,
            ("smoothing_config",),
            "I4",
        ),
        (
            "analyze.py",
            ANALYZE_PATH,
            ("alpha_override", "smoothing_config"),
            "I5",
        ),
    ):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            kwargs_list = _i_build_portfolio_kwargs(tree)
            ok = _i_portfolio_results(tree, required)
            detail = f"build_portfolio 调用={len(kwargs_list)} 次｜实得关键字={kwargs_list}"
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"解析失败：{type(exc).__name__}: {exc}"
        if key == "I4":
            check(
                "I4 recommend.py 的 build_portfolio 调用必须带 smoothing_config=（规则层不许退回原始分排序）",
                bool(ok),
                detail,
            )
        else:
            check(
                "I5 analyze.py 的 build_portfolio 调用必须带 alpha_override= 与 smoothing_config=（交付层与引擎同源）",
                bool(ok),
                detail,
            )


# ---------------------------------------------------------------- K 类（新增断言的变异反证）


def _mutant_axis_no_asof_filter(
    prices: pd.DataFrame | None, scores: pd.DataFrame | None, asof: pd.Timestamp
) -> list[pd.Timestamp]:
    """变异体：轴**不过滤 asof**（把 asof 之后的日期也放进去 ⇒ 轴长不同 ⇒ 递推步长不同）。"""
    stamps: set[pd.Timestamp] = set()
    for frame in (prices, scores):
        if frame is None or len(frame) == 0 or "date" not in frame.columns:
            continue
        for stamp in pd.to_datetime(frame["date"], errors="coerce").dropna().unique():
            stamps.add(pd.Timestamp(stamp).normalize())
    return sorted(stamps)


def _mutant_axis_prices_only(
    prices: pd.DataFrame | None, scores: pd.DataFrame | None, asof: pd.Timestamp
) -> list[pd.Timestamp]:
    """变异体：轴**只取行情日**（把打分日丢掉 ⇒ 并集不完整 ⇒ 轴与打分栅格对不上）。"""
    stamps: set[pd.Timestamp] = set()
    for frame in (prices,):
        if frame is None or len(frame) == 0 or "date" not in frame.columns:
            continue
        for stamp in pd.to_datetime(frame["date"], errors="coerce").dropna().unique():
            stamps.add(pd.Timestamp(stamp).normalize())
    return sorted(stamps)


def _mutant_axis_crash_on_none(
    prices: pd.DataFrame | None, scores: pd.DataFrame | None, asof: pd.Timestamp
) -> list[pd.Timestamp]:
    """变异体：轴**不做 None 兜底** ⇒ 输入为 None 时直接崩。"""
    stamps: set[pd.Timestamp] = set()
    for frame in (prices, scores):
        for stamp in pd.to_datetime(frame["date"], errors="coerce").dropna().unique():
            stamps.add(pd.Timestamp(stamp).normalize())
    return sorted(stamps)


def _mutant_build_override_calendar_axis(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    prices: pd.DataFrame | None,
    asof: pd.Timestamp,
    config: SS.SmoothingConfig,
    min_coverage: float = 0.5,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """变异体：共享口把交易日轴换成「只有打分日」⇒ **真缺陷复刻**（轴长不同 ⇒ 平滑分不同）。"""
    axis = SS._score_dates(scores)
    outcome = SS.compute_smoothed_alpha(
        holdings, scores, asof, axis, config, min_coverage
    )
    override = outcome.alpha[["etf_code", "smoothed_score", "score_stability"]].copy()
    return override, dict(outcome.diagnostics)


def _mutant_build_override_drop_column(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    prices: pd.DataFrame | None,
    asof: pd.Timestamp,
    config: SS.SmoothingConfig,
    min_coverage: float = 0.5,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """变异体：共享口少给一列（``score_stability``）⇒ 合并函数拿不到稳定性。"""
    override, diagnostics = _ORIGINAL_BUILD_OVERRIDE(
        holdings, scores, prices, asof, config, min_coverage
    )
    return override[["etf_code", "smoothed_score"]].copy(), diagnostics


def _mutant_apply_override_ignore(
    frame: pd.DataFrame, override: pd.DataFrame | None
) -> pd.DataFrame:
    """变异体：合并函数**忽略覆盖表** ⇒ 排序退回原始分（**真缺陷复刻**：规则层用原始分排序）。"""
    out = frame.copy()
    if "alpha_score" in out.columns:
        out["alpha_score_raw"] = pd.to_numeric(out["alpha_score"], errors="coerce")
    else:
        out["alpha_score_raw"] = np.nan
    out["smoothed_score"] = out["alpha_score_raw"]
    out["score_stability"] = None
    out["alpha_score_used"] = out["alpha_score_raw"]
    return out


def _mutant_ast_rename_cli_option(tree: ast.AST, option: str) -> ast.AST:
    """变异体：把某个平滑 CLI 参数**改名** ⇒ 两层入口不再同名。"""
    new_tree = ast.parse(ast.unparse(tree))
    for node in ast.walk(new_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == option
        ):
            node.args[0].value = option + "-mutated"
    ast.fix_missing_locations(new_tree)
    return new_tree


def _mutant_ast_drop_portfolio_keyword(
    tree: ast.AST, keyword: str, func_name: str = "build_portfolio"
) -> ast.AST:
    """变异体：把 ``build_portfolio(...)`` 调用里的某个关键字**删掉** ⇒ 平滑参数没传进去。"""
    new_tree = ast.parse(ast.unparse(tree))
    for node in ast.walk(new_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
        ):
            node.keywords = [kw for kw in node.keywords if kw.arg != keyword]
    ast.fix_missing_locations(new_tree)
    return new_tree


def _mutant_ast_hardcode_default(tree: ast.AST, field: str = "method") -> ast.AST:
    """变异体：把平滑默认值**写死成字面量** ⇒ config 单一事实源被架空。"""
    new_tree = ast.parse(ast.unparse(tree))
    for node in ast.walk(new_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SmoothingConfig"
        ):
            for keyword in node.keywords:
                if keyword.arg == field:
                    keyword.value = ast.Constant(value="ewma")
    ast.fix_missing_locations(new_tree)
    return new_tree


def _runner_h_calendar_axis() -> dict[str, bool]:
    """变异：引擎侧换成另一条轴（真缺陷复刻）⇒ H1/H2 必须失败。"""
    original = SS.build_alpha_override
    SS.build_alpha_override = _mutant_build_override_calendar_axis  # type: ignore[assignment]
    try:
        return _h_results()
    finally:
        SS.build_alpha_override = original  # type: ignore[assignment]


def _runner_h_ignore_override() -> dict[str, bool]:
    """变异：合并函数忽略覆盖表（规则层用原始分排序）⇒ H2 必须失败。"""
    original = SS.apply_alpha_override
    SS.apply_alpha_override = _mutant_apply_override_ignore  # type: ignore[assignment]
    try:
        return _h_results()
    finally:
        SS.apply_alpha_override = original  # type: ignore[assignment]


def _run_k_class() -> None:
    """K 类：新增结构性断言的变异反证（每条都必须有「换成错的实现 ⇒ 必须失败」的负例）。"""
    _section("K 类 · 新增断言的变异反证（换成错的实现 ⇒ 对应断言必须失败）")

    f1 = _f_results(_mutant_axis_no_asof_filter)
    check(
        "K1 变异「轴不过滤 asof」⇒ F1 必须失败",
        not f1["F1"],
        f"变异体下 F1={f1['F1']}（期望 False）",
    )

    f2 = _f_results(_mutant_axis_prices_only)
    check(
        "K2 变异「轴丢掉打分日」⇒ F1 必须失败",
        not f2["F1"],
        f"变异体下 F1={f2['F1']}（期望 False）",
    )

    f3 = _f_results(_mutant_axis_crash_on_none)
    check(
        "K3 变异「轴不做 None 兜底」⇒ F2 必须失败",
        not f3["F2"],
        f"变异体下 F2={f3['F2']}（期望 False）",
    )

    g1 = _g_results(_mutant_build_override_calendar_axis)
    check(
        "K4 变异「共享口换一条轴」⇒ G2 必须失败",
        not g1["G2"],
        f"变异体下 G2={g1['G2']}（期望 False）",
    )

    g2 = _g_results(_mutant_build_override_drop_column)
    check(
        "K5 变异「共享口少一列」⇒ G1 必须失败",
        not g2["G1"],
        f"变异体下 G1={g2['G1']}（期望 False）",
    )

    h1 = _runner_h_calendar_axis()
    check(
        "K6 变异「引擎侧换一条轴」（真缺陷复刻）⇒ H1/H2 必须失败",
        (not h1["H1"]) and (not h1["H2"]),
        f"变异体下 H1={h1['H1']}｜H2={h1['H2']}（期望都 False）",
    )

    h2 = _runner_h_ignore_override()
    check(
        "K7 变异「合并函数忽略覆盖表」（真缺陷复刻：规则层用原始分排序）⇒ H2 必须失败",
        not h2["H2"],
        f"变异体下 H2={h2['H2']}（期望 False）｜H1={h2['H1']}（期望仍 True：表本身没变）",
    )

    try:
        recommend_tree = ast.parse(RECOMMEND_PATH.read_text(encoding="utf-8"))
        mutated = _mutant_ast_rename_cli_option(recommend_tree, "--smoothing-method")
        i1 = _i_file_results(mutated)
    except Exception as exc:  # noqa: BLE001
        i1 = {"I1": False, "I2": False}
        check(
            "K8 变异「某个 CLI 参数被改名」⇒ I1 必须失败",
            False,
            f"变异构造失败：{type(exc).__name__}: {exc}",
        )
    else:
        check(
            "K8 变异「某个 CLI 参数被改名」⇒ I1 必须失败",
            not i1["I1"],
            f"变异体下 I1={i1['I1']}（期望 False）",
        )

    try:
        mutated = _mutant_ast_hardcode_default(recommend_tree, "method")
        i2 = _i_file_results(mutated)
    except Exception as exc:  # noqa: BLE001
        i2 = {"I1": False, "I2": False}
        check(
            "K9 变异「某个默认值被写死成字面量」⇒ I2 必须失败",
            False,
            f"变异构造失败：{type(exc).__name__}: {exc}",
        )
    else:
        check(
            "K9 变异「某个默认值被写死成字面量」⇒ I2 必须失败",
            not i2["I2"],
            f"变异体下 I2={i2['I2']}（期望 False）｜{i2.get('_I2_detail', '')}",
        )

    try:
        recommend_tree = ast.parse(RECOMMEND_PATH.read_text(encoding="utf-8"))
        mutated = _mutant_ast_drop_portfolio_keyword(recommend_tree, "smoothing_config")
        i4 = _i_portfolio_results(mutated, ("smoothing_config",))
    except Exception as exc:  # noqa: BLE001
        i4 = False
        check(
            "K10 变异「recommend.py 不把 smoothing_config 传给引擎」⇒ I4 必须失败",
            False,
            f"变异构造失败：{type(exc).__name__}: {exc}",
        )
    else:
        check(
            "K10 变异「recommend.py 不把 smoothing_config 传给引擎」⇒ I4 必须失败",
            not i4,
            f"变异体下 I4={i4}（期望 False）",
        )

    try:
        analyze_tree = ast.parse(ANALYZE_PATH.read_text(encoding="utf-8"))
        mutated = _mutant_ast_drop_portfolio_keyword(analyze_tree, "alpha_override")
        i5 = _i_portfolio_results(mutated, ("alpha_override", "smoothing_config"))
    except Exception as exc:  # noqa: BLE001
        i5 = False
        check(
            "K11 变异「analyze.py 不把 alpha_override 传给引擎」⇒ I5 必须失败",
            False,
            f"变异构造失败：{type(exc).__name__}: {exc}",
        )
    else:
        check(
            "K11 变异「analyze.py 不把 alpha_override 传给引擎」⇒ I5 必须失败",
            not i5,
            f"变异体下 I5={i5}（期望 False）",
        )


# ---------------------------------------------------------------- 入口


def main() -> int:
    """跑全部断言并汇总（退出码 0 = 全过，1 = 有 FAIL）。"""
    print("=== ETF 穿透分时间平滑 · 闸门探针 ===")
    _run_a_class()
    _run_b_class()
    _run_c_class()
    _run_d_class()
    _run_e_class()
    # ---- F/G/H/I/K 类：2026-11-09 全量闸门抓到的真缺陷（规则层与交付层分叉）----
    baseline_pass = len(PASS)
    baseline_fail = len(FAIL)
    _run_f_class()
    _run_g_class()
    _run_h_class()
    _run_i_class()
    _run_k_class()
    new_pass = len(PASS) - baseline_pass
    new_fail = len(FAIL) - baseline_fail
    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    print(
        f"=== 新增断言：PASS {new_pass} / FAIL {new_fail}"
        f"（基线 {baseline_pass + baseline_fail} 条 + 新增 {new_pass + new_fail} 条"
        f" = {len(PASS) + len(FAIL)} 条）==="
    )
    if FAIL:
        print("FAIL：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
