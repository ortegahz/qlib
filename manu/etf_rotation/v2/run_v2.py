#!/usr/bin/env python
"""🔴 V2 编排器：训练 → 消融决策 → 打分 → **val 段搜索** → 冻结 → **单盲验收**。

**红线（每一步都有闸门，不靠自觉）**：

  ① 消融决策（baseline vs 去 ROC5）**只在 val 段**用 RankIC 判定；
  ② ETF 层超参搜索**只许在 val 段**（``hyperopt.assert_val_only`` 硬拦截，越界直接抛错）；
  ③ 参数**冻结成 json** 后才允许进测试段；
  ④ 测试段 **2024–2026 只跑一次**（``evaluate.run_single_blind`` 写 token，
     第二次跑 ⇒ ``SingleBlindViolation`` 直接抛错，**fail-closed**）。

用法（服务器）：
    export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
    python manu/etf_rotation/v2/run_v2.py --stage all \\
        --out-dir /mnt/data/siping/runs/etf_rotation_v2_results
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import etf_layer as EL  # noqa: E402
import evaluate as EV  # noqa: E402
import hyperopt as HO  # noqa: E402
import metrics as ME  # noqa: E402
import smoothing as SM  # noqa: E402  —— 决策③：下游 EWMA / Rank Buffer 三臂
import splits as SP  # noqa: E402
import train_s as TS  # noqa: E402

try:
    from . import backtest_v2  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    import backtest_v2  # type: ignore[no-redef]

__all__ = ["run_pipeline"]

#: 基准 ETF（V1 同口径：沪深 300 ETF）
BENCHMARK_CODE = "510300"

#: 🔴 搜索空间里**唯一**被放行的调仓节奏键（**显式白名单**）：
#:    其余含 "rebalance" / "stride" 的键（``stride`` / ``rebalance_signal_stride`` …）
#:    **一律禁止** ⇒ 见 :func:`assert_no_rebalance_keys`。
ALLOWED_REBALANCE_KEYS: frozenset[str] = frozenset({"rebalance_every_n_days"})


# ----------------------------------------------------------------- 工具
def build_search_grid(topk_grid: Any, buffer_grid: Any) -> dict:
    """🔴 val 段搜索空间 = **臂（A15 / A20 / B / C） × TOPK_GRID × HOLD_BUFFER_GRID**。

    🔴🔴 **cadence（调仓节奏）已于 2026-11-18 锁死 = 20（严格月频）**，
    **不再是搜索维度**（历史留档：``config_v2.REBALANCE_CADENCE_GRID_LEGACY``
    = {20, 40, 60, 90} 是 2026-11-17 那轮的搜索空间）。

    事故链（用户 2026-11-18 目标文档「为什么 V2 在盲测里大亏」）：
      * 2026-11-17 夜班：cadence 进了搜索空间 + 目标函数
        ``excess − 0.05 × max(0, turn − 4)`` **惩罚项无上限**
        ⇒ 在 cadence=20（turn ≈ **12.32×**，val 段折叠常量下的实测最低值）时惩罚 ≈ 0.416，
        **完全压倒超额**（0.05~0.20）⇒ 目标函数退化成「最小化换手」
        ⇒ 选出 **cadence=90**（每 4 个半月才动一次）⇒ 单盲 **−12.93% / 回撤 −54.93%**；
      * 2026-11-18 处方：① cadence **锁死 20**；② 换手惩罚**封顶**
        （``config_v2.TURNOVER_PENALTY_CAP`` = 0.02）⇒ 惩罚不再压倒超额；
        ③ 选参权威从 2 年单边熊市 val 段换成 **2019–2023 五折滚动前推 OOS**
        （见 ``manu/tools/v2_walkforward_probe.py``）。

    🔴 **≤4× 红线在 cadence=20 下结构性不可达**（不是搜不到）：
      降换手的正确姿势是「**月频 + Rank Buffer（宽出严进）**」，
      **不是**拉长调仓周期（拉长周期已被 cadence=90 的负超额证伪）。
    """
    grid = {
        "smoothing_arm": list(C.SMOOTHING_ARM_IDS),
        "top_k": [int(x) for x in topk_grid],
        "hold_buffer": [int(x) for x in buffer_grid],
        "rebalance_every_n_days": [int(x) for x in C.REBALANCE_CADENCE_GRID],
        # 🔴 2026-11-18（目标文档 Step 1 + val 归因证据）：
        #    **排序分里 alpha 与动量的配比**进搜索空间。
        #    证据（val 段 2022-2023，纯净信号层验证）：
        #      纯 E（alpha_weight=1.0）    ⇒ ETF 层 20 日 RankIC **+0.1016**、超额 **+7.06%**；
        #      V2 现状（0.5E + 0.5 动量）  ⇒ RankIC **+0.0550**、超额 **−10.06%**；
        #    ⇒ **动量项把 Alpha 砍掉一半并翻负**；而这个配比此前**写死 0.5/0.5**
        #    （不在搜索空间里 ⇒ 网格搜索**不可能**修好它）。
        #    ⇒ 现在把 ``alpha_weight`` 放进网格，``momentum_weight = 1 - alpha_weight``
        #      （由 :func:`run_v2.objective` 推导 ⇒ 两者**永远**互补）。
        "alpha_weight": [float(x) for x in C.ALPHA_WEIGHT_GRID],
        # 🔴 同族上限（V1 遗留）进搜索空间：val 证据显示它在弱势市里把分散化打掉
        #    （纯 E 变体：关掉同族上限超额 +7.06% vs 打开 −1.90% ⇒ 差 ~9pp）。
        "allow_theme_cap": [bool(x) for x in C.THEME_CAP_GRID],
    }
    assert_no_rebalance_keys(grid)
    return grid


def assert_no_rebalance_keys(grid: Any) -> None:
    """🔴 搜索空间里**只放行显式白名单**的 cadence 键；其余调仓节奏相关键一律 raise。

    🔴 **为什么从「全部禁止」改成「白名单放行」**（2026-11-17，用户红线驱动）：
      * 用户红线 = 「换手 16.57×（V1 同口径）→ **≤4×**，回撤收敛」；
      * 实测：在 cadence = 20 的**折叠常量**下，臂 / Top-K / 缓冲带 / MA 窗口**全部**搜完后，
        年化换手最低仍 **12.32×** ⇒ **≤4× 不可达** ⇒ 唯一杠杆 = **调仓节奏**；
      * ⇒ cadence（``rebalance_every_n_days``）必须进 **val 段**搜索空间。

    🔴 **口径变更必须披露**（不许闷声改口径）：
      * cadence = **20** 与 V1 **同口径** ⇒ 该档**保留可见**，可与 V1 的 16.57× 直接对表；
      * cadence ≠ 20 ⇒ 换手与 V1 的 16.57× **不是同一口径**，**不可直接对比**。

    🔴 **仍然严格禁止**（只放行一个键，不许「随便放行」）：
      ``stride`` / ``rebalance_signal_stride`` / 任何**未列入白名单**的含
      "rebalance" / "stride" 的键 ⇒ 一律 raise。
    """
    keys = list(grid.keys()) if isinstance(grid, dict) else list(grid)
    forbidden = [
        k for k in keys
        if ("rebalance" in str(k).lower() or "stride" in str(k).lower())
        and str(k) not in ALLOWED_REBALANCE_KEYS
    ]
    if forbidden:
        raise ValueError(
            f"🔴 搜索空间里出现了**未放行**的调仓节奏键 {forbidden}"
            f"（只放行 {sorted(ALLOWED_REBALANCE_KEYS)}；stride 类键一律禁止）"
        )


def cadence_sample_note(n_rebalances: int | None) -> str:
    """🔴 调仓次数 → **样本充足性**说明（披露口径的单点来源）。

    val 段只有 ≈2 年 ⇒ cadence 越大 ⇒ 调仓次数越少 ⇒ 年化 / 回撤**噪声主导**。
    低于 ``config_v2.CADENCE_MIN_REBALANCES`` 的档位**必须**标注「样本不足，不可据此选参」。
    """
    n = int(n_rebalances or 0)
    if n < int(C.CADENCE_MIN_REBALANCES):
        return (f"🔴 样本不足：val 段仅 {n} 次调仓（< 门槛 {C.CADENCE_MIN_REBALANCES}）"
                f" ⇒ 年化 / 回撤统计量**噪声主导**，**不可据此选参**")
    return f"✅ 样本量可接受：val 段 {n} 次调仓（≥ 门槛 {C.CADENCE_MIN_REBALANCES}）"


def cadence_disclosure_notes() -> list[str]:
    """🔴 **cadence 口径披露的四条硬要求**（产物 / 报告 / 成绩单逐条落地，单点来源）。"""
    # 🔴 2026-11-18 换手口径正名：V1 的 16.57× 是**双边**（Σ|Δw|；旧键名属历史错标）
    #    ⇒ 披露文案里必须标明「双边」。
    v1_turn = float(C.V1_BASELINE["annualized_turnover_two_way"])
    return [
        (f"🔴 **cadence = {C.REBALANCE_EVERY_N_DAYS} ⇒ 调仓**节奏**与 V1 相同**"
         f"（V1 = 每 {C.REBALANCE_EVERY_N_DAYS} 个交易日一次调仓）"
         f" ⇒ 该档**保留可见**（每轮 trial 都记录 `rebalance_every_n_days` 与 `n_rebalances`）"
         f"；🔴 但**换手口径仍不同源**（V1 = 「上期**目标**权重未漂移」／V2 = 「调仓前**漂移**权重"
         f"（含 Rank Buffer 留存量）」）⇒ 与 V1 的 {v1_turn:.2f}×（**双边**）"
         f" **只能趋势性对比，不是同源**"),
        (f"🔴 **cadence ≠ {C.REBALANCE_EVERY_N_DAYS} 时，换手与 V1 的 {v1_turn:.2f}×"
         f"（双边）不是同一口径**"
         f"（调仓频率不同 ⇒ 同样的持仓变动被摊到不同年数上）"
         f" ⇒ **不可直接对比**，只能与本档内部的其它参数比"),
        ("🔴 **cadence 越大 ⇒ 调仓次数越少 ⇒ val 段的年化 / 回撤统计量越噪声**"
         " ⇒ 各档指标**必须**连同 `n_rebalances` 一起读"),
        (f"🔴 val 段只有 ≈2 年（{C.VALID[0]} ~ {C.VALID[1]}）⇒ cadence = 90 时只剩**个位数**调仓"
         f" ⇒ 该档指标**必须**标注「样本不足，不可据此选参」"
         f"（判据：调仓次数 < {C.CADENCE_MIN_REBALANCES}）"),
    ]


def build_cadence_disclosure(trials: list[dict] | None,
                             best_params: dict | None = None) -> dict:
    """🔴 汇总 **cadence 口径披露块**（写进 ``val_search.json`` / ``arm_search_val.json``）。

    内容：候选集、V1 同口径档、**逐档**统计（trial 数 / 最佳 trial 的指标 /
    最低换手 / 调仓次数 / 样本充足性标注 / 是否 ≤ 目标）+ 四条披露口径。
    """
    trials = list(trials or [])
    best_params = dict(best_params or {})
    selected = int(best_params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS))
    per_cadence: dict[str, Any] = {}
    for cadence in C.REBALANCE_CADENCE_GRID:
        cad = int(cadence)
        sub = [
            t for t in trials
            if int((t.get("params") or {}).get("rebalance_every_n_days",
                                              C.REBALANCE_EVERY_N_DAYS)) == cad
        ]
        ok_trials = [t for t in sub if t.get("status") == "ok"]
        entry: dict[str, Any] = {
            "cadence": cad,
            "rebalance_signal_stride": int(C.rebalance_signal_stride_for(cad)),
            "n_trials": len(sub),
            "n_trials_ok": len(ok_trials),
            "comparable_to_v1": cad == int(C.REBALANCE_EVERY_N_DAYS),
            "turnover_target": float(C.TURNOVER_TARGET),
        }
        turns = [
            float(((t.get("objective") or {}).get("annualized_turnover")))
            for t in ok_trials
            if ((t.get("objective") or {}).get("annualized_turnover")) is not None
        ]
        entry["min_annualized_turnover"] = min(turns) if turns else None
        entry["any_trial_within_turnover_target"] = (
            bool(turns) and min(turns) <= float(C.TURNOVER_TARGET)
        )
        if ok_trials:
            best_trial = max(ok_trials, key=lambda t: float(t.get("score")))
            obj = dict(best_trial.get("objective") or {})
            n_reb = obj.get("n_rebalances")
            entry["best_trial"] = {
                "params": best_trial.get("params"),
                "score": best_trial.get("score"),
                "metrics": obj,
            }
            entry["n_rebalances"] = n_reb
            entry["sample_note"] = cadence_sample_note(n_reb)
        else:
            entry["best_trial"] = None
            entry["n_rebalances"] = None
            entry["sample_note"] = "🔴 本档没有有效 trial（不得据此判负）"
        per_cadence[str(cad)] = entry
    return {
        "candidates": [int(x) for x in C.REBALANCE_CADENCE_GRID],
        "v1_equivalent_cadence": int(C.REBALANCE_EVERY_N_DAYS),
        # 🔴 2026-11-18 换手口径正名：V1 换手**两个口径并列落盘**
        #    （双边 = Σ|Δw|，旧键名 `v1_annualized_turnover` 属历史错标 ⇒ 已改名；
        #    单边 = 0.5 × 双边，唯一实现 `config_v2.one_way_turnover`）。
        "v1_annualized_turnover_two_way": float(C.V1_BASELINE["annualized_turnover_two_way"]),
        "v1_annualized_turnover_one_way": float(C.V1_BASELINE["annualized_turnover_one_way"]),
        "turnover_target": float(C.TURNOVER_TARGET),
        "min_rebalances_for_selection": int(C.CADENCE_MIN_REBALANCES),
        "selected_cadence": selected,
        "selected_rebalance_signal_stride": int(C.rebalance_signal_stride_for(selected)),
        "notes": cadence_disclosure_notes(),
        "per_cadence": per_cadence,
    }


def val_search_window() -> tuple[str, str]:
    """搜索窗口 = val 段（🔴 **物理红线**：``hyperopt.assert_val_only`` 越界即 raise）。"""
    start, end = str(C.VALID[0]), str(C.VALID[1])
    HO.assert_val_only(start, end)          # 🔴 越界（碰到 2024-01-01）直接抛错
    return start, end


def val_window_metrics(
    nav,
    turnover=None,
    benchmark=None,
    window: tuple[str, str] | None = None,
    commission: float | None = None,
    slippage: float | None = None,
    periods_per_year: int = 250,
) -> dict:
    """🔴🔴 **在 val 窗口内**算指标（红线：超参搜索只许看 val 段）。

    🔴 **事故背景（2026-11-17 21:20 独立审计）**：旧实现把**整段历史净值**
    （2020-01-02 ~ 2026-11-10）直接交给 ``metrics.summarize`` ⇒ 年化收益 / 年化换手
    **混进了测试段已实现收益** ⇒ 超参其实是在「看过测试段」的情况下选出来的
    ⇒ 违反「超参搜索只许在 val 段做」，并把下游的「单盲」掏空。

    ⇒ 本函数把 **净值 / 换手 / 基准三者同时切到 val 窗口**再算指标；
    且**窗口不等于 val 段 ⇒ 直接抛错**（防御纵深：不依赖调用方自觉）。

    参数
    ----
    nav : ``pd.Series``（index = 日期）。
    turnover : ``dict[str, float]`` | ``pd.Series`` | None（逐调仓日换手）。
    benchmark : ``pd.Series`` | None（基准净值）。
    window : ``(start, end)``；None ⇒ 用 ``config_v2.VALID``。
    commission / slippage / periods_per_year : 透传给 ``metrics.summarize``。

    返回
    ----
    ``metrics.summarize()`` 的返回值。
    """
    expect = (str(C.VALID[0]), str(C.VALID[1]))
    got = expect if window is None else (str(window[0]), str(window[1]))
    if got != expect:
        raise RuntimeError(
            f"🔴 指标窗口必须是 val 段 {list(expect)}，实得 {list(got)}"
            f"⇒ 测试段不得参与超参选择"
        )
    nav_v, to_v, bench_v = ME.slice_window(
        nav, turnover, benchmark,
        start=pd.Timestamp(got[0]), end=pd.Timestamp(got[1]),
    )
    return ME.summarize(
        nav_v, bench_v, to_v,
        commission=float(C.COMMISSION if commission is None else commission),
        slippage=float(C.SLIPPAGE if slippage is None else slippage),
        periods_per_year=int(periods_per_year),
    )


def val_score_dates_without_label_leak(
    score_dates,
    valid_end: str | None = None,
    horizon: int | None = None,
) -> list[pd.Timestamp]:
    """🔴 裁掉「**标签窗口伸进测试段**」的 val 截面。

    🔴 **事故背景（2026-11-17 21:20 独立审计）**：qlib 的负 ``Ref`` 会**自动延伸取数**
    ⇒ val 段最后一个截面（如 2023-12-19）的标签窗口末端 = t+25 ≈ **2024-01-23**
    **落在测试段内** ⇒ 消融判定 / val RankIC 实际**用了测试段的价格** ⇒ 破坏单盲。

    判据：样本日 ``d`` 的标签窗口末端（``d`` 之后第 ``horizon`` 个交易日）必须
    **不晚于** ``valid_end``；否则该截面被剔除。

    参数
    ----
    score_dates : 可迭代的日期（``pd.Timestamp`` / ``date`` / ISO 字符串）。
    valid_end : val 段末日（默认 ``config_v2.VALID[1]``）。
    horizon : 标签窗口长度（默认 ``config_v2.LABEL_HORIZON_DAYS`` = 25）。

    返回
    ----
    保留的日期列表（``pd.Timestamp``，升序）。
    """
    valid_end = str(valid_end or C.VALID[1])
    horizon = int(horizon if horizon is not None else C.LABEL_HORIZON_DAYS)
    cal = SP.trading_calendar()
    if not cal:
        raise RuntimeError("🔴 交易日历为空 ⇒ 无法判定标签窗口泄露")
    pos = {d: i for i, d in enumerate(cal)}
    cut = max((i for i, d in enumerate(cal) if d.isoformat() <= valid_end), default=-1)
    out: list[pd.Timestamp] = []
    for raw in score_dates:
        ts = pd.Timestamp(raw).normalize()
        i = pos.get(ts.date())
        if i is None:
            continue
        if i + horizon <= cut:
            out.append(ts)
    return out


def _min_turn_str(with_turn: list[dict]) -> str:
    """把「有效 trial 里的最低年化换手」渲染成披露用字符串（**缺字段 ⇒ 写「—」**）。"""
    vals = []
    for t in with_turn:
        raw = ((t.get("objective") or {}).get("annualized_turnover"))
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v):
            vals.append(v)
    return f"{min(vals):.3f}×" if vals else "—"


def apply_turnover_gate(
    result: dict,
    target: float | None = None,
    *,
    mode: str = "gate",
) -> dict:
    """🔴 **换手闸门 / 披露基准**：在 val 段搜索结果里挑 winner。

    🔴 **两种模式**（用户 2026-11-18 拍板：废除月频策略的「≤4× 换手」硬拦截）：

    * ``mode="gate"``（**默认**，**旧行为逐字不变**）：
      先看**所有有效 trial**里换手 ≤ 目标的 ⇒ 取其中 ``score`` 最高者；
      一个都没有 ⇒ 按 ``config_v2.TURNOVER_GATE_ON_MISS`` 兜底
      （默认 ``"best_score"``：按主目标取分最高者），
      并把 ``gate["ok"] = False``（**不许**假装达标）。
    * ``mode="disclosure"``（**新默认口径**，``config_v2.TURNOVER_TARGET_MODE``）：
      **换手目标只作披露基准** ⇒ **不许**用换手达标与否过滤候选 trial
      ⇒ **永远按 ``score`` 最高挑**；但 ``gate`` 块**如实**记录
      ``ok`` / ``n_trials_within_target`` / ``min_turnover`` / ``chosen_turnover``
      ⇒ 报告里**并列披露**单 / 双边换手率。

    🔴 **为什么废除硬拦截**（两条依据）：
      ① 用户 2026-11-18 拍板：改「cadence=20 先验锁定 + hold_buffer=2
         + 报告并列披露单/双边换手率」；
      ② 实测成本可承受：年化**双边**换手 14.10× ⇒ 年化成本拖累 ≈ **0.915%**
         ⇒ 再拿换手筛参数 = **用 alpha 买断**。

    返回
    ----
    ``{"best_params", "best_score", "trials", "gate"}``；``gate`` 含
    ``{"ok", "target", "n_trials_ok", "n_trials_total", "chosen_turnover",
      "min_turnover", "message"}``（+ ``mode`` / ``blocking``）。

    ⚠️ **默认 ``mode="gate"``** ⇒ 既有调用方与既有测试行为**逐字不变**；
    生产调用点必须**显式**传 ``mode=C.TURNOVER_TARGET_MODE``。
    """
    if mode not in ("gate", "disclosure"):
        raise ValueError(
            f"🔴 换手目标模式非法：{mode!r} ⇒ 只允许 'gate' / 'disclosure'"
        )
    target = float(target if target is not None else C.TURNOVER_TARGET)
    trials = list(result.get("trials") or [])
    ok_trials = [t for t in trials if t.get("status") == "ok"]
    if not ok_trials:
        raise RuntimeError("🔴 换手闸门：没有任何有效 trial ⇒ 不得挑 winner")

    def _turn(t: dict) -> float | None:
        """取该 trial 的年化换手（缺失 / 非有限 ⇒ ``None``，**不当成达标**）。"""
        val = ((t.get("objective") or {}).get("annualized_turnover"))
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        return f if np.isfinite(f) else None

    def _n_reb(t: dict) -> int | None:
        """取该 trial 的 val 段调仓次数（缺失 ⇒ ``None`` ⇒ 样本量未知）。"""
        val = ((t.get("objective") or {}).get("n_rebalances"))
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None

    with_turn = [t for t in ok_trials if _turn(t) is not None]
    within = [t for t in with_turn if float(_turn(t)) <= target]
    if mode == "disclosure":
        # 🔴 2026-11-18 用户拍板：废除「≤4× 换手」**硬拦截** ⇒ 换手**只作披露基准**
        #    ⇒ **不许**用换手达标与否过滤候选 trial ⇒ 永远按主目标（score）最高挑。
        #    但 ``gate`` 块仍**如实**记录达标情况（报告要并列披露单/双边换手率）。
        chosen = max(ok_trials, key=lambda t: float(t.get("score")))
        chosen_turn = _turn(chosen)
        ok = bool(chosen_turn is not None and float(chosen_turn) <= target)
        chosen_turn_str = "—" if chosen_turn is None else f"{float(chosen_turn):.3f}×"
        message = (
            f"🔴 换手目标**仅作披露基准**（不再作为选参硬门槛）："
            f"{len(with_turn)} 个有效 trial 里 {len(within)} 个 ≤ 目标 {target}×"
            f"（最低 {_min_turn_str(with_turn)}）；"
            f"已按**主目标（score）**在全部 {len(ok_trials)} 个有效 trial 中取最高者"
            f"（选中者年化换手 {chosen_turn_str}）"
            f"⇒ 换手目标**不再过滤**候选 trial"
        )
    elif within:
        chosen = max(within, key=lambda t: float(t.get("score")))
        ok = True
        message = (f"🔴 换手目标**已满足**：在 {len(within)}/{len(with_turn)} 个达标 trial 中"
                   f"取 score 最高者（年化换手 {_turn(chosen):.3f}× ≤ 目标 {target}×）")
    else:
        # 🔴 2026-11-18 用户目标文档修正：**兜底口径由 `config_v2.TURNOVER_GATE_ON_MISS` 决定**。
        #    * ``"best_score"``（**新默认**）：按**主目标**（扣费后净超额）挑最好的那个，
        #      并如实披露「≤4× 未达成」⇒ 不许假装达标，**也不许用换手率把 alpha 买断**。
        #    * ``"min_turnover"``（旧口径，仅供审计对表）：挑换手最低者 ⇒
        #      **正是它把 cadence=90 选成了胜者**（2026-11-17 夜班那次冻结）。
        on_miss = str(C.TURNOVER_GATE_ON_MISS)
        if on_miss == "min_turnover":
            chosen = min(with_turn, key=lambda t: float(_turn(t))) if with_turn else max(
                ok_trials, key=lambda t: float(t.get("score")))
            pick_desc = "退而取**换手最低**者（旧口径，仅供审计对表）"
        elif on_miss == "best_score":
            chosen = max(ok_trials, key=lambda t: float(t.get("score")))
            pick_desc = ("退而按**主目标（扣费后净超额）**取最好者"
                         "（不许用换手率把 alpha 买断）")
        else:
            raise ValueError(
                f"🔴 换手闸门兜底口径非法：{on_miss!r} ⇒ 只允许 "
                f"{C.TURNOVER_GATE_ON_MISS!r} / 'min_turnover'")
        ok = False
        message = (f"🔴 **换手目标未达成**：{len(with_turn)} 个有效 trial 的年化换手全部 > 目标 "
                   f"{target}×（最低 {_min_turn_str(with_turn)}）"
                   f"⇒ {pick_desc}，并如实披露")
    gate = {
        "ok": bool(ok),
        "target": target,
        "n_trials_ok": len(ok_trials),
        "n_trials_with_turnover": len(with_turn),
        "n_trials_within_target": len(within),
        "chosen_turnover": _turn(chosen),
        "min_turnover": (min(float(_turn(t)) for t in with_turn) if with_turn else None),
        # 🔴 2026-11-17 夜班审计补：**样本量门槛必须让选参逻辑「看得见」**
        #    （`config_v2.CADENCE_MIN_REBALANCES`）。🔴 **只披露、不当过滤器** ——
        #    把它改成过滤器会让用户的「换手 ≤4×」红线**不可达**（cadence ≤ 40 的档位
        #    在 val 段换手仍 ≫ 4×）⇒ 那是**改口径**，不许偷偷做。
        #    ⇒ 这里把「样本不足的 trial 数 / 选中 trial 的样本量 / 是否充足」写进闸门输出。
        "min_rebalances_for_selection": int(C.CADENCE_MIN_REBALANCES),
        "chosen_n_rebalances": _n_reb(chosen),
        "chosen_sample_sufficient": (
            (_n_reb(chosen) is not None)
            and (int(_n_reb(chosen)) >= int(C.CADENCE_MIN_REBALANCES))
        ),
        "n_trials_low_sample": len([
            t for t in with_turn
            if (_n_reb(t) is None) or (int(_n_reb(t)) < int(C.CADENCE_MIN_REBALANCES))
        ]),
        "sample_note": cadence_sample_note(_n_reb(chosen)),
        "message": message,
        # 🔴 口径自述：本次闸门是**硬闸门**还是**只披露**（用户 2026-11-18 拍板）
        "mode": mode,
        "blocking": bool(mode == "gate"),
    }
    out = dict(result)
    out["best_params"] = dict(chosen.get("params") or {})
    out["best_score"] = chosen.get("score")
    gate["on_miss"] = str(C.TURNOVER_GATE_ON_MISS) if mode == "gate" else None
    gate["locked_cadence_grid"] = list(C.REBALANCE_CADENCE_GRID)
    out["gate"] = gate
    # 🔴 2026-11-18：闸门本身**保持「只看换手、与 cadence 无关」**的语义
    #    （cadence 锁定不变量在 `stage_freeze` 里断言 ⇒ 那里才是「冻结」发生的地方）。
    #    这里只**披露**选中的 cadence，便于对账。
    _chosen_cadence = (chosen.get("params") or {}).get("rebalance_every_n_days")
    if _chosen_cadence is not None:
        gate["chosen_cadence"] = int(_chosen_cadence)
    return out


def build_arm_search_payload(result: dict, grid: dict, model_name: str) -> dict:
    """🔴 把 val 段搜索结果整成**臂对照产物**（``arm_search_val.json`` 的载荷）。

    载荷必须含：每个候选臂的全部指标（``per_arm``）、胜者（``winner_arm``）、
    判定窗口（``decision_window``）、口径声明（``primary_metric`` / ``objective`` / ``note``）。
    """
    best = dict(result["best_params"])
    winner_arm = str(best.get("smoothing_arm") or "")
    if winner_arm not in C.SMOOTHING_ARM_IDS:
        raise ValueError(f"🔴 胜出臂非法：{winner_arm!r} ⇒ 不在 {list(C.SMOOTHING_ARM_IDS)}")

    # 🔴 逐臂汇总（每个候选臂的最佳组合 + 全部指标 ⇒ 三臂可直接对表）
    per_arm: dict[str, Any] = {}
    for arm_id in C.SMOOTHING_ARM_IDS:
        arm_trials = [
            t for t in result["trials"]
            if (t.get("params") or {}).get("smoothing_arm") == arm_id and t.get("status") == "ok"
        ]
        entry: dict[str, Any] = {
            "arm_spec": SM.arm_spec(arm_id).as_dict(),
            "n_ok_trials": len(arm_trials),
        }
        if arm_trials:
            best_trial = max(arm_trials, key=lambda t: float(t["score"]))
            entry.update({
                "best_params": best_trial.get("params"),
                "best_score": best_trial.get("score"),
                "metrics": best_trial.get("objective") or {},
            })
            # 🔴 该臂最佳 trial 的 cadence（臂对比**跨 cadence** ⇒ 必须披露，见 note）
            entry["rebalance_every_n_days"] = int(
                (best_trial.get("params") or {}).get("rebalance_every_n_days",
                                                     C.REBALANCE_EVERY_N_DAYS)
            )
        else:
            entry["note"] = "🔴 本臂没有有效 trial（不得据此判负）"
        per_arm[arm_id] = entry

    return {
        "searched_on": "val",
        "val_window": list(C.VALID),
        "decision_window": {
            "start": str(C.VALID[0]),
            "end": str(C.VALID[1]),
            "source": "config_v2.VALID",
            "note": "🔴 三臂对比**只**在这段做；2024–2026 单盲只跑最终冻结的那一臂一次",
        },
        "grid": {k: list(v) for k, v in grid.items()},
        "arms": {aid: SM.arm_spec(aid).as_dict() for aid in C.SMOOTHING_ARM_IDS},
        "primary_metric": "扣费后年化超额收益（excess_vs_benchmark）",
        "secondary_metric": (
            "年化双边换手（annualized_turnover；单边 = 0.5×；"
            "4.0× 仅作披露基准，不是硬门槛）"
        ),
        "objective": C.OBJECTIVE_DESCRIPTION,
        "best_params": best,
        "best_score": result["best_score"],
        "winner_arm": winner_arm,
        "per_arm": per_arm,
        "n_trials": result["n_trials_run"],
        "trials": result["trials"],
        "model_name": model_name,
        # 🔴 胜出的 cadence（**冻结时进 frozen_params.json** ⇒ 盲测/报告用同一个值）
        "rebalance_every_n_days": int(best.get("rebalance_every_n_days",
                                                C.REBALANCE_EVERY_N_DAYS)),
        "rebalance_signal_stride": int(C.rebalance_signal_stride_for(
            int(best.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS))
        )),
        "rebalance_candidates": [int(x) for x in C.REBALANCE_CADENCE_GRID],
        # 🔴 cadence 口径披露块（逐档统计 + 四条披露口径）
        "cadence_disclosure": build_cadence_disclosure(result["trials"], best),
        "note": (
            "🔴 搜索窗口 = val 段（2022-01-01 ~ 2023-12-31），由 "
            "`hyperopt.assert_val_only` **在调用 objective 之前**强制校验；"
            "触碰 2024-01-01 即抛错。测试段从未参与选参。"
            "🔴 **调仓节奏（cadence）已进入搜索空间**（口径变更：用户红线「换手 ≤4×」"
            "在 cadence=20 的折叠常量下不可达）⇒ 候选 "
            f"{list(C.REBALANCE_CADENCE_GRID)}；其中 cadence=20 与 V1 同**节奏**（保留可见），"
            "cadence≠20 与 V1 的 16.57× **不可直接对比**；详见 `cadence_disclosure`。"
            "🔴 三臂对比是**跨 cadence** 取每个臂的最高分 trial ⇒ 臂间差异可能部分来自 "
            "cadence 而非臂本身（`per_arm.rebalance_every_n_days` 已逐臂标出）。"
            "🔴 口径不对称（已披露）：个股打分文件自 2022-01-01 起 ⇒ val 段开头的 EWMA "
            "预热历史不足（冷启动）；测试段有 val+test 全历史 ⇒ 预热完整。"
        ),
    }


def _log(msg: str) -> None:
    """带时间戳的进度日志（V2 编排器的统一输出格式）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _write_json(path: Path, payload: Any) -> Path:
    """把载荷写成 json（产物落盘，统一 UTF-8 + indent=2）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    _log(f"[OK] 写出 {path}")
    return path


def parse_args() -> argparse.Namespace:
    """解析命令行参数（V2 编排器的全部开关）。"""
    p = argparse.ArgumentParser(description="V2 编排器")
    p.add_argument("--out-dir", default=str(C.DEFAULT_V2_RUN_ROOT))
    p.add_argument("--provider-uri", default=C.DEFAULT_PROVIDER_URI)
    p.add_argument("--stage", default="all",
                   help="all | train | ablation | score | search | freeze | blind | report")
    p.add_argument("--ablation-candidates", default="baseline_full,ablate_roc5",
                   help="参与消融对照的组（默认两组）")
    p.add_argument("--val-universe", default=C.TRAIN_UNIVERSE,
                   help="消融判定用的打分池（csi300：轻量、且是训练池 ⇒ 可比）")
    # 🔴 **信号栅格步长**（S 层打分的疏密，= ``SIGNAL_STRIDE_N_DAYS`` = 10 个交易日）；
    #    **不是**调仓频率（调仓节奏 cadence = ``rebalance_every_n_days``，
    #    2026-11-17 夜班起**已进 val 段搜索空间**；cadence ⇒ 调仓栅格步长 = cadence // 本值）。
    p.add_argument("--signal-stride", type=int, default=C.SIGNAL_STRIDE_N_DAYS,
                   help="S 层打分栅格步长（交易日）；**调仓节奏 cadence 是独立维度**"
                        "（`rebalance_every_n_days`，已进 val 段搜索空间；"
                        "cadence ⇒ 调仓栅格步长 = cadence // 本值）")
    p.add_argument("--topk-grid", default=",".join(str(int(x)) for x in C.TOPK_GRID))
    p.add_argument("--buffer-grid", default=",".join(str(int(x)) for x in C.HOLD_BUFFER_GRID))
    p.add_argument("--allow-rerun-blind", action="store_true",
                   help="🔴 显式授权重跑单盲段（**必须**同时写 --blind-rerun-reason）")
    p.add_argument("--blind-rerun-reason", default="")
    p.add_argument("--skip-train", action="store_true", help="复用已训好的模型（不重训）")
    return p.parse_args()


# ----------------------------------------------------------------- 数据
def load_market_data() -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    """读 ETF 行情 ⇒ ``(close 宽表, amount 宽表, {代码: 名称})``。"""
    import constituents as CON

    prices = EL.load_etf_prices()
    close = prices.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    amount = prices.pivot_table(index="date", columns="code", values="amount", aggfunc="last")
    # 🔴 2026-11-17 夜跑修复：`CON.load_holdings()` 返回的是 **ConstituentsSnapshot**
    #    （不是 DataFrame ⇒ 旧代码 `.columns` 直接 AttributeError）。
    #    取「ETF 名称映射」要用**原始长表**（`etf_name` 列只在长表里）。
    holdings = CON.read_holdings_frame()
    names: dict[str, str] = {}
    if "etf_name" in holdings.columns:
        for code, grp in holdings.groupby("etf_code", sort=True):
            names[str(code)] = str(grp["etf_name"].iloc[0])
    return close.sort_index(), amount.sort_index(), names


# ----------------------------------------------------------------- 阶段
def stage_train(args: argparse.Namespace, out_dir: Path) -> None:
    """阶段：**训练** S 层（每个消融组一个模型）。"""
    groups = [g.strip() for g in args.ablation_candidates.split(",") if g.strip()]
    for g in groups:
        drop = tuple(C.ABLATION_GROUPS[g])
        _log(f"=== 训练 S 层：{g}（剔除 {drop or '无'}）===")
        TS.train_s_layer(C.TRAIN_UNIVERSE, drop, out_dir, g)


def _val_end_without_label_leak() -> str:
    """🔴 返回「标签窗口末端仍不晚于 val 段末日」的**最后一个交易日**（ISO 字符串）。

    用于把 val 打分窗口的末日往前裁，使**任何**落在该窗口内的截面都不会
    把标签窗口伸进测试段。
    """
    cal = SP.trading_calendar()
    valid_end = str(C.VALID[1])
    cut = max((i for i, d in enumerate(cal) if d.isoformat() <= valid_end), default=-1)
    idx = cut - int(C.LABEL_HORIZON_DAYS)
    if idx < 0:
        raise RuntimeError(
            f"🔴 交易日历里 {valid_end} 之前不足 {C.LABEL_HORIZON_DAYS} 个交易日 ⇒ 无法裁剪标签泄露"
        )
    return cal[idx].isoformat()


def stage_ablation(args: argparse.Namespace, out_dir: Path) -> dict:
    """在 **val 段**用 RankIC 判定消融组胜者（🔴 只许看 val）。"""
    groups = [g.strip() for g in args.ablation_candidates.split(",") if g.strip()]
    TS.init_qlib(args.provider_uri)
    # 🔴 配置一致性自检（审计发现：``DROPPED_FEATURES`` / ``ABLATION_METRIC`` 原本全库无人引用
    #    ⇒ 「默认 = 消融 ROC5」「判定指标 = RankIC」只是注释级约定）
    if tuple(C.ABLATION_GROUPS.get(C.ABLATION_WINNER, ())) != tuple(C.DROPPED_FEATURES):
        raise RuntimeError(
            f"🔴 配置不一致：默认消融组 {C.ABLATION_GROUPS.get(C.ABLATION_WINNER)!r} "
            f"≠ config_v2.DROPPED_FEATURES {tuple(C.DROPPED_FEATURES)!r}"
        )
    # 🔴🔴 裁掉「标签窗口伸进测试段」的 val 截面（审计发现：qlib 负 Ref 自动延伸取数
    #    ⇒ 2023-12-19 这个截面的标签末端 ≈ 2024-01-23 **落在测试段内**）
    val_end = _val_end_without_label_leak()
    _log(f"🔴 val 打分窗口末日裁到 {val_end}（标签窗口末端 ≤ {C.VALID[1]}）")
    results: dict[str, Any] = {}
    for g in groups:
        drop = tuple(C.ABLATION_GROUPS[g])
        model_path = out_dir / f"model_{g}.pkl"
        frozen_path = out_dir / f"frozen_norm_{g}.pkl"
        if not model_path.is_file():
            _log(f"[WARN] {g} 没有模型 ⇒ 跳过")
            continue
        _log(f"=== 消融对照打分（val 段，池={args.val_universe}）：{g} ===")
        score_dir = out_dir / "ablation_val"
        # 🔴 消融判定的打分栅格 = **信号栅格**（``SIGNAL_STRIDE_N_DAYS`` = 10 个交易日）。
        #    理由：已冻结的消融结论 ``ABLATION_WINNER = "ablate_roc5"`` 就是在这套栅格下判出来的
        #    （证据：``ablation_val/score_manifest_val_*.json`` 里
        #    ``rebalance_every_n_days = 10``、``n_score_dates = 48``、区间 2022-01-11~2023-12-19）
        #    ⇒ 这里必须用 10，重跑才**可复现**同一结论；
        #    （🔴 调仓节奏 cadence 是 ETF 层的独立维度（已进 val 段搜索空间），
        #      与这里的 S 层打分栅格是两件事，不许混用）
        TS.score_universe(model_path, frozen_path, drop, args.val_universe, score_dir,
                           C.VALID[0], val_end, C.SIGNAL_STRIDE_N_DAYS,
                           chunk_months=24, model_name=f"val_{g}", with_fwd=True)
        csv_path = score_dir / f"stock_scores_v2_val_{g}.csv"
        scores = pd.read_csv(csv_path, dtype={"instrument": str})
        scores["date"] = pd.to_datetime(scores["date"])
        ic = _rank_ic_of(scores)
        results[g] = ic
        _log(f"[RESULT] {g}: val RankIC={ic.get('ic')} ICIR={ic.get('icir')} "
             f"t={ic.get('t_stat')} n={ic.get('n_cross_sections')}")
    if not results:
        raise RuntimeError("🔴 消融对照没有任何有效结果")
    winner = max(results, key=lambda g: (results[g].get("ic") if results[g].get("ic") is not None else -9e9))
    payload = {
        "decided_on": "val",
        "val_window": list(C.VALID),
        "metric": f"{C.ABLATION_METRIC}（逐日截面 Spearman）",
        "candidates": groups,
        "results": results,
        "winner": winner,
        "winner_features": list(C.ABLATION_GROUPS[winner]),
        "score_window": [C.VALID[0], val_end],
        "note": (
            "🔴 消融决策**只在 val 段**用 RankIC 判定；测试段从未参与。"
            "判定池 = csi300（训练池，与 S 层模型同池 ⇒ 可比）。"
            "🔴 判定截面已按「标签窗口末端 ≤ val 段末日」裁剪"
            "（否则 qlib 负 Ref 会把标签窗口伸进测试段）。"
        ),
    }
    _write_json(out_dir / "ablation_choice.json", payload)
    return payload


def _rank_ic_of(scores: pd.DataFrame) -> dict:
    """从 ``date/instrument/score/fwd_ret`` 计算逐日截面 RankIC 汇总。"""
    df = scores.dropna(subset=["score", "fwd_ret"]).copy()
    if df.empty:
        return {"ic": None, "icir": None, "t_stat": None, "n_cross_sections": 0}
    dates, pred, fwd = [], [], []
    for day, grp in df.groupby("date", sort=True):
        if len(grp) < 30:
            continue
        dates.extend([day] * len(grp))
        pred.extend(grp["score"].tolist())
        fwd.extend(grp["fwd_ret"].tolist())
    return ME.rank_ic(dates, pred, fwd)


def stage_score(args: argparse.Namespace, out_dir: Path) -> dict:
    """用**消融胜者**在 ``all`` 池上打 val + test 的分。"""
    choice = json.loads((out_dir / "ablation_choice.json").read_text(encoding="utf-8"))
    winner = choice["winner"]
    drop = tuple(C.ABLATION_GROUPS[winner])
    TS.init_qlib(args.provider_uri)
    _log(f"=== 用消融胜者 '{winner}' 在 {C.SCORE_UNIVERSE} 池上打分（{C.VALID[0]} ~ {C.TEST[1]}）===")
    # 🔴 打分栅格 = **信号栅格**（``SIGNAL_STRIDE_N_DAYS`` = 10 个交易日，S 层打分的疏密）；
    #    调仓节奏（20 个交易日）由 ``REBALANCE_SIGNAL_STRIDE`` 从这个栅格里取，
    #    **不是**这里的步长 ⇒ 两件事不许混用。
    return TS.score_universe(
        out_dir / f"model_{winner}.pkl", out_dir / f"frozen_norm_{winner}.pkl", drop,
        C.SCORE_UNIVERSE, out_dir, C.VALID[0], C.TEST[1], int(args.signal_stride),
        chunk_months=12, model_name=winner, with_fwd=True,
    )


#: 🔴 ETF 可交易标记的**模块级缓存**（``{(buy_map, sell_map)}``）
#: —— ``mark_etf_tradable`` 要扫 30 万行行情；网格搜索会调上百次 ⇒ 必须缓存。
_ETF_TRADABLE_CACHE: dict[str, dict[str, dict[str, bool]]] = {}


def build_etf_tradable_maps() -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """🔴 构造 ETF 的 **(可买, 可卖)** 两套逐日标记（真实流动性约束）。

    🔴 **买/卖两侧语义不同**（涨跌停的本质）：

    ==========  ==========  ==========  ==============================
    情形        可买入      可卖出      物理含义
    ==========  ==========  ==========  ==============================
    停牌        ❌          ❌          没有成交，报单挂不上
    涨停(+10%)  ❌          ✅          买盘排队买不到；但卖单成交容易
    跌停(-10%)  ✅          ❌          卖盘排队卖不掉；但买盘能成交
    正常        ✅          ✅          ——
    ==========  ==========  ==========  ==============================

    ⇒ **可买** = ``not suspended and not limit_up``
    ⇒ **可卖** = ``not suspended and not limit_down``

    返回 ``(buy_map, sell_map)``，键为 ``"YYYY-MM-DD"``，值为 ``{代码: bool}``。

    🔴 **已知局限（必须披露）**：涨跌停用「日涨跌幅 ≥/≤ 9.5%」近似判定
    （``config_v2.LIMIT_THRESHOLD``）⇒ ① 对 ±20% 涨跌停品种（跨境/商品 ETF）
    判不出板（会**偏保守**地把正常票当成不可买）；② 真实判板应看
    「收盘价 == 涨跌停价」，本项目未取到涨跌停价字段。
    """
    if "maps" in _ETF_TRADABLE_CACHE:
        return _ETF_TRADABLE_CACHE["maps"]
    import liquidity as LQ

    prices = EL.load_etf_prices()
    flags = LQ.mark_etf_tradable(prices)
    buy_map, sell_map = etf_tradable_maps_from_flags(flags)
    _ETF_TRADABLE_CACHE["maps"] = (buy_map, sell_map)
    return buy_map, sell_map


def etf_tradable_maps_from_flags(
    flags: pd.DataFrame,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """把 ``liquidity.mark_etf_tradable`` 的四个标记转成 **(可买, 可卖)** 两套逐日映射。

    参数
    ----
    flags : 含 ``date`` / ``code`` / ``suspended`` / ``limit_up`` / ``limit_down``
        四列的长表（``mark_etf_tradable`` 的输出）。

    返回
    ----
    ``(buy_map, sell_map)``，键 = ``"YYYY-MM-DD"``，值 = ``{代码: bool}``。

    🔴 语义（**买/卖两侧不同**，这是涨跌停的本质）：
      * 可买 = 不停牌 且 不涨停（涨停买不进）；
      * 可卖 = 不停牌 且 不跌停（跌停卖不掉）。
    """
    buy_map: dict[str, dict[str, bool]] = {}
    sell_map: dict[str, dict[str, bool]] = {}
    if flags is None or len(flags) == 0:
        return buy_map, sell_map
    day_col = pd.to_datetime(flags["date"]).dt.strftime("%Y-%m-%d")
    for day, grp in flags.groupby(day_col, sort=True):
        suspended = grp["suspended"].to_numpy(dtype=bool)
        limit_up = grp["limit_up"].to_numpy(dtype=bool)
        limit_down = grp["limit_down"].to_numpy(dtype=bool)
        codes = [str(c) for c in grp["code"].to_numpy()]
        buy_map[str(day)] = {
            c: bool((not s) and (not u)) for c, s, u in zip(codes, suspended, limit_up)
        }
        sell_map[str(day)] = {
            c: bool((not s) and (not d)) for c, s, d in zip(codes, suspended, limit_down)
        }
    return buy_map, sell_map


def _build_targets_for(args: argparse.Namespace, out_dir: Path, params: dict,
                        close: pd.DataFrame, amount: pd.DataFrame, names: dict,
                        dates: list[pd.Timestamp]):
    """按一组参数算出逐调仓日的**目标权重**（含 Rank Buffer）。"""
    import constituents as CON

    scores_csv = out_dir / f"stock_scores_v2_{params['_model_name']}.csv"
    scores = pd.read_csv(scores_csv, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])
    # 🔴 2026-11-17 夜跑修复：同 `load_market_data` —— 权重表要**原始长表**
    #    （`build_weight_map` 自己负责正权重过滤 + instrument 归一），
    #    但**必须按快照日过滤**（成分股表是 append-only 累积的 ⇒ 不许把多天混进同一次计算）。
    snapshot = CON.load_holdings()
    holdings = CON.snapshot_frame(snapshot)
    weights = EL.build_weight_map(holdings)
    feats = EL.compute_features(close, amount)
    targets, diag = EL.build_targets(
        dates, scores, weights, close, feats, names=names,
        alpha_weight=float(params.get("alpha_weight", 0.5)),
        momentum_weight=float(params.get("momentum_weight", 0.5)),
        top_k=int(params.get("top_k", C.TARGET_N_HOLDINGS)),
        hold_buffer=int(params.get("hold_buffer", C.HOLD_BUFFER)),
        entry_ma_window=int(params.get("entry_ma_window", 60)),
        exit_ma_window=int(params.get("exit_ma_window", 60)),
        min_coverage=float(C.MIN_COVERAGE),
        min_amount_20d=float(C.MIN_ETF_AVG_AMOUNT_20D),
        min_history_days=int(C.MIN_ETF_HISTORY_DAYS),
        # 🔴 真实流动性约束（停牌/涨停 ⇒ 买不进）：候选池过滤
        tradable=build_etf_tradable_maps()[0],
        # 🔴 2026-11-18：同族上限可关（**纯净信号层**归因要「无脑买 Top-5」⇒ 不许被
        #    同族上限改写选股结果）；默认 ``True`` ⇒ 既有调用方行为逐字不变。
        allow_theme_cap=bool(params.get("allow_theme_cap", True)),
        # 🔴 负分空仓保护（默认 **关闭** ⇒ 既有调用方行为逐字不变）
        neg_score_gate=bool(params.get("neg_score_gate", False)),
        neg_score_gate_stat=str(params.get("neg_score_gate_stat", "median")),
        neg_score_gate_scale=float(params.get("neg_score_gate_scale", 0.05)),
        neg_score_gate_min_exposure=float(params.get("neg_score_gate_min_exposure", 0.0)),
        # 🔴 决策③：平滑臂（``B_native_slow`` = 不做下游 EWMA ⇒ 原生慢变量直通）
        smoothing_arm=params.get("smoothing_arm"),
        # 🔴 调仓栅格步长 = **被搜索的 cadence**（`rebalance_every_n_days`）
        #    ⇒ stride = cadence // SIGNAL_STRIDE_N_DAYS（20→2 / 40→4 / 60→6 / 90→9）；
        #    缺省（未搜 / 旧冻结产物）⇒ 回落到 V1 同口径的 20 个交易日。
        # 🔴 2026-11-17 夜班审计补：**必须走 `config_v2.rebalance_signal_stride_for()`**
        #    （它对「cadence 不是信号栅格步长的整数倍」**硬报错**）⇒ 裸 `//` 会静默取整
        #    ⇒ 等于把「不许静默取整」这道守卫在生产调用路径上绕过去了。
        rebalance_signal_stride=int(
            C.rebalance_signal_stride_for(
                int(params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)))
        ),
    )
    return targets, diag


def _backtest(close: pd.DataFrame, targets: dict, params: dict) -> dict:
    """跑一次 Cost-Aware 回测（佣金 + 滑点 + 换手全进净值）。"""
    buy_map, sell_map = build_etf_tradable_maps()
    return backtest_v2.run_backtest_v2(
        close, targets,
        commission=float(params.get("commission", C.COMMISSION)),
        slippage=float(params.get("slippage", C.SLIPPAGE)),
        # 🔴 买侧 = 可买入（不停牌、不涨停）；卖侧 = 可卖出（不停牌、不跌停）
        tradable=buy_map,
        tradable_sell=sell_map,
        periods_per_year=250,
    )


def _benchmark_nav(close: pd.DataFrame, code: str = BENCHMARK_CODE) -> pd.Series | None:
    """取基准 ETF（沪深300）的归一净值曲线（用于超额口径）。"""
    if code not in close.columns:
        return None
    s = close[code].astype("float64").dropna()
    if len(s) < 2:
        return None
    return s / s.iloc[0]


def stage_search(args: argparse.Namespace, out_dir: Path) -> dict:
    """🔴 **只在 val 段**做「三臂 × topk × buffer」网格搜索（``hyperopt`` 硬拦截越界）。"""
    choice = json.loads((out_dir / "ablation_choice.json").read_text(encoding="utf-8"))
    model_name = choice["winner"]
    close, amount, names = load_market_data()
    feats = EL.compute_features(close, amount)
    # 🔴 物理隔离：搜索窗口**必须**落在 val 段（越界 ⇒ 直接抛错，不进 objective）
    window = val_search_window()
    val_dates = [d for d in close.index if pd.Timestamp(C.VALID[0]) <= d <= pd.Timestamp(C.VALID[1])]
    score_dates = sorted(pd.read_csv(out_dir / f"stock_scores_v2_{model_name}.csv",
                                     dtype={"instrument": str}, usecols=["date"])["date"].unique())
    score_dates = sorted(pd.to_datetime(score_dates).tolist())
    val_score_dates = [d for d in score_dates if pd.Timestamp(C.VALID[0]) <= d <= pd.Timestamp(C.VALID[1])]
    # 🔴 裁掉「标签窗口伸进测试段」的 val 截面（审计发现：qlib 负 Ref 会自动延伸取数）
    _n_before = len(val_score_dates)
    val_score_dates = val_score_dates_without_label_leak(val_score_dates)
    _log(f"val 段信号日 {len(val_score_dates)} 个（调仓日 = 每 cadence//{C.SIGNAL_STRIDE_N_DAYS} "
         f"个信号日取一个 ⇒ cadence={C.REBALANCE_EVERY_N_DAYS} 时每 {C.REBALANCE_EVERY_N_DAYS} "
         f"个交易日一个；cadence 候选 {list(C.REBALANCE_CADENCE_GRID)}）；价格日 {len(val_dates)} 个"
         f"；🔴 标签窗口泄露裁剪：{_n_before} → {len(val_score_dates)}")
    bench = _benchmark_nav(close)

    def objective(params: dict, window: tuple[str, str]) -> dict:
        """网格搜索的目标函数：**扣费后**年化净超额 − 换手超目标惩罚。

        🔴🔴 **红线：指标窗口必须 == val 窗口**（2022-01-01 ~ 2023-12-31）。

        🔴 **事故背景（2026-11-17 21:20 独立审计）**：旧实现把**整段历史净值**
        （2020-01-02 ~ 2026-11-10）直接交给 ``ME.summarize`` ⇒ 年化收益 / 年化换手
        **混进了测试段已实现收益** ⇒ 超参其实是在「看过测试段」的情况下选出来的
        ⇒ 违反「超参搜索只许在 val 段做」，并把下游的「单盲」掏空。
        （实测：同一组参数，全窗年化 −1.63% vs val 段真值 −19.11% vs test 段 +8.92%。）

        ⇒ 现在 **净值 / 换手 / 基准三者同时切到 val 窗口**再算指标。
        """
        p = dict(params)
        p["_model_name"] = model_name
        # 🔴 2026-11-18：alpha 与动量的配比**互补**（``momentum_weight = 1 - alpha_weight``）
        #    ⇒ 网格里只放行 ``alpha_weight`` 一个键（避免两个键给出互相矛盾的配置）。
        p["momentum_weight"] = round(1.0 - float(p.get("alpha_weight", 0.5)), 6)
        # 🔴 cadence（调仓节奏）= **被搜索的超参** ⇒ stride 由它推导（20→2 / 40→4 / 60→6 / 90→9）
        cadence = int(params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS))
        stride = int(C.rebalance_signal_stride_for(cadence))
        targets, _ = _build_targets_for(args, out_dir, p, close, amount, names, val_score_dates)
        if not targets:
            return {"score": float("-inf")}
        bt = _backtest(close, targets, p)
        # 🔴🔴 指标**只**在 val 窗口内算（红线；见 ``val_window_metrics`` 的事故背景）
        m = val_window_metrics(
            bt["nav"], bt.get("turnover_by_date"), bench, window=window,
            commission=float(p.get("commission", C.COMMISSION)),
            slippage=float(p.get("slippage", C.SLIPPAGE)),
            periods_per_year=250,
        )
        ann = m["annualized_return"]
        exc = m.get("excess_vs_benchmark")
        excess = float(exc) if exc is not None else (float(ann) if ann is not None else 0.0)
        turn = float(m.get("annualized_turnover") or 0.0)
        # 🔴 目标函数：**扣费后**年化净超额 − 换手超目标惩罚
        #    （规格原文：「让训练/验证阶段优化的目标就是扣费后的真实净超额」；
        #      另加一条「换手 ≤ 4×」的硬目标 ⇒ 超目标按 5%/单位惩罚）
        # 🔴 2026-11-18 用户目标文档修正：**惩罚项必须有上限**。
        #    旧口径 `0.05 * max(0, turn - 4.0)` 在 cadence=20 下恒 ≈ 0.416，
        #    而超额差异只有 0.05~0.20 ⇒ 目标函数退化成「最小化换手」
        #    ⇒ 网格搜出 cadence=90 的「蜗牛参数」⇒ 单盲 −12.93% / 回撤 −54.93%。
        #    详见 `config_v2.TURNOVER_PENALTY_CAP` 的注释（唯一实现）。
        penalty = C.turnover_penalty(turn, float(C.TURNOVER_TARGET))
        score = excess - penalty
        return {
            "score": float(score),
            "annualized_return": ann,
            "excess_vs_benchmark": exc,
            "max_drawdown": m.get("max_drawdown"),
            "annualized_turnover": turn,
            # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
            "annualized_turnover_one_way": C.one_way_turnover(turn),
            "total_turnover": m.get("total_turnover"),
            "turnover_penalty": penalty,
            "n_rebalances": len(targets),
            # 🔴 每轮 trial 都必须记录 cadence 与调仓次数（披露要求：口径可比性 + 样本量）
            "rebalance_every_n_days": cadence,
            "rebalance_signal_stride": stride,
        }

    grid = build_search_grid(
        [int(x) for x in args.topk_grid.split(",") if x.strip()],
        [int(x) for x in args.buffer_grid.split(",") if x.strip()],
    )
    _log(f"=== val 段网格搜索：{grid} ===")
    result = HO.search(objective, grid, val_window=window)
    # 🔴 换手硬目标闸门（在冻结之前先把「≤4×」变成硬约束）
    gated = apply_turnover_gate(
        result, float(C.TURNOVER_TARGET), mode=C.TURNOVER_TARGET_MODE
    )
    _log(gated["gate"]["message"])
    best = dict(gated["best_params"])
    best["_model_name"] = model_name
    payload = build_arm_search_payload(gated, grid, model_name)
    payload["turnover_gate"] = gated["gate"]
    payload["best_params"] = best          # 带上模型名（单盲段要用）
    # 🔴 臂对照产物（本次任务的**正式产物**）
    _write_json(out_dir / "arm_search_val.json", payload)
    # 🔴 兼容镜像：``v2/report.py`` 与 ``v2/probes/probe_v2_artifacts.py``（**别人维护**）
    #    仍读 ``val_search.json`` ⇒ 这里落一份**逐字相同**的载荷，不改那两个文件。
    _write_json(out_dir / "val_search.json", payload)
    return payload


def assert_cadence_locked(cadence: int | None) -> int:
    """🔴 **cadence 锁定不变量**：冻结前必须断言「cadence ∈ 锁定档」。

    🔴🔴 事故背景（2026-11-18 用户目标文档「为什么 V2 在盲测里大亏」）：
    2026-11-17 夜班那次冻结把 **cadence=90** 选成了胜者（当时网格是 {20,40,60,90}）
    ⇒ 整个 ETF 层被拧成「每 4 个半月才动一次」
    ⇒ 一个 20 日预测极准的模型（RankIC 0.1271 / t=9.04）被锁死 4 个半月
    ⇒ 单盲组合 **−12.93% / 回撤 −54.93%**。

    ⇒ 现在 cadence **锁死 = 20 个交易日（严格月频）**
    （`config_v2.REBALANCE_CADENCE_GRID = (20,)`）
    ⇒ 这里再加一道**冻结前硬断言**：越界 ⇒ 直接 raise（**fail-closed**），
    不许「先选出来再悄悄改回去」。

    用户原话：「**锁死 cadence = 20（严格月频）**」「**不准搜索 90 / 120 / 150
    这种反直觉长周期**」——降换手的正确姿势是「月频 + Rank Buffer」，**不是**拉长调仓周期。
    """
    if cadence is None:
        raise RuntimeError(
            "🔴 冻结参数里**没有** `rebalance_every_n_days`（cadence）"
            "⇒ **不许静默回落到常量**（口径漂移 ⇒ 会用错节奏烧掉一次性单盲段）"
            "⇒ 请重跑 `--stage search`")
    locked = [int(x) for x in C.REBALANCE_CADENCE_GRID]
    if int(cadence) not in locked:
        raise RuntimeError(
            f"🔴 冻结的 cadence={int(cadence)} 不在**锁定档** {locked} 里 "
            "⇒ 拒绝冻结（fail-closed）"
            "⇒ 调仓节奏不得拉长（用户红线：不许搜 90/120/150 这种反直觉长周期）")
    return int(cadence)


def stage_freeze(args: argparse.Namespace, out_dir: Path) -> dict:
    """阶段：**冻结** val 段选出的**胜出臂 + topk + buffer**（冻结后才允许进测试段）。"""
    search_payload = json.loads((out_dir / "arm_search_val.json").read_text(encoding="utf-8"))
    best = dict(search_payload["best_params"])
    winner_arm = str(best.get("smoothing_arm") or search_payload.get("winner_arm") or "")
    if winner_arm not in C.SMOOTHING_ARM_IDS:
        raise ValueError(f"🔴 胜出臂非法：{winner_arm!r} ⇒ 不在 {list(C.SMOOTHING_ARM_IDS)}")
    # 🔴🔴 2026-11-17 夜班审计补：**cadence 必须来自本轮 val 搜索产物**。
    #    旧口径是 `best.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)`
    #    ⇒ **静默回落常量 20** ⇒ 而 `stage_blind` 的守卫只看「键在不在」
    #    ⇒ 旧产物会**用错 cadence 烧掉一次性的单盲 token** ⇒ 这里改成**硬报错**。
    if "rebalance_every_n_days" not in best:
        raise RuntimeError(
            "🔴 搜索产物 `arm_search_val.json` 的 `best_params` 里**没有** "
            "`rebalance_every_n_days`（cadence）⇒ **不许静默回落到常量 20**"
            "（口径漂移 ⇒ 会用错节奏烧掉一次性单盲段）"
            "⇒ 请重跑 `--stage search`（val 段搜索空间已含 cadence 维度）"
        )
    # 🔴 2026-11-18：cadence **锁定不变量**（事故：曾冻结出 cadence=90）
    #    （缺键的情况上面已经硬报错 ⇒ 这里只在键存在时做「锁定档」断言）
    _cadence = best.get("rebalance_every_n_days")
    if _cadence is not None:
        assert_cadence_locked(_cadence)
    # 🔴 冻结产物**只含**胜出臂与选定参数（含**被搜索的 cadence** ⇒ 盲测/报告用同一个值）
    frozen_params = {
        "smoothing_arm": winner_arm,
        "top_k": int(best["top_k"]),
        "hold_buffer": int(best["hold_buffer"]),
        "rebalance_every_n_days": int(best["rebalance_every_n_days"]),
    }
    # 🔴 样本量披露：把「选中 trial 在 val 段的调仓次数」写进冻结产物
    #    （低于 `CADENCE_MIN_REBALANCES` ⇒ 样本不足 ⇒ 盲测段才是最终判据）。
    _gate = dict(search_payload.get("turnover_gate") or {})
    frozen_params["n_rebalances_val"] = _gate.get("chosen_n_rebalances")
    frozen_params["sample_note"] = _gate.get("sample_note") or ""
    # 🔴 2026-11-17 夜班补：**第二轮 val 扫的 MA 窗口**（用户待办 #10：ETF 层超参
    #    —— MA 窗口 / Top-K / 缓冲带 / 缓冲带 / 平滑臂 —— **全部**迁回 val 段搜）。
    #    `manu/tools/v2_val_ma_sweep.py` 在第一轮胜出臂固定的前提下扫 MA 窗口，
    #    产物 `val_ma_sweep.json`；这里把它并进冻结参数（文件不存在 ⇒ 跳过，兼容旧流程）。
    ma_sweep_path = out_dir / "val_ma_sweep.json"
    ma_sweep = None
    if ma_sweep_path.is_file():
        ma_sweep = json.loads(ma_sweep_path.read_text(encoding="utf-8"))
        # 🔴🔴 **MA 窗口与 cadence 是强耦合的**（MA 强平 = 换手的结构性来源，
        #    cadence 决定 MA 多久被检查一次）⇒ **不许把「cadence=20 上下文里扫出来的
        #    MA 窗口」与「别的 cadence 下选出的其余参数」混冻**（冻结参数内部不自洽）。
        #    旧产物 `val_ma_sweep.json` 的 `fixed_from_round1` 里**没有** cadence 键
        #    ⇒ 视为**陈旧** ⇒ 硬报错，要求重跑 MA 二次扫（该脚本已支持固定第一轮 cadence）。
        _fixed = dict(ma_sweep.get("fixed_from_round1") or {})
        _fixed_cadence = _fixed.get("rebalance_every_n_days")
        _stale: list[str] = []
        if _fixed_cadence is None:
            _stale.append("`fixed_from_round1` 里没有 `rebalance_every_n_days`（旧口径产物）")
        elif int(_fixed_cadence) != int(frozen_params["rebalance_every_n_days"]):
            _stale.append(
                f"MA 二次扫固定的 cadence={int(_fixed_cadence)} ≠ 本轮胜出 cadence="
                f"{int(frozen_params['rebalance_every_n_days'])}")
        for _k in ("smoothing_arm", "top_k", "hold_buffer"):
            if _k in _fixed and _fixed.get(_k) != best.get(_k):
                _stale.append(f"MA 二次扫固定的 {_k}={_fixed.get(_k)!r} ≠ 本轮胜出 {best.get(_k)!r}")
        if _stale:
            raise RuntimeError(
                "🔴 `val_ma_sweep.json` 与本轮 val 搜索**不同源**（MA 窗口与 cadence / 臂 / "
                "Top-K / 缓冲带强耦合 ⇒ 混冻 = 冻结参数内部不自洽）：" + "；".join(_stale)
                + " ⇒ 请重跑 `manu/tools/v2_val_ma_sweep.py --ma-grid 40,60,80,120,150`"
                "（它会固定本轮胜出的臂 / Top-K / 缓冲带 / cadence）"
            )
        for key in ("entry_ma_window", "exit_ma_window"):
            val = (ma_sweep.get("best_params") or {}).get(key)
            if val is not None:
                frozen_params[key] = int(val)
    frozen = {
        "params": frozen_params,
        "best_score": search_payload["best_score"],
        "model_name": search_payload["model_name"],
        "winner_arm": winner_arm,
        "rebalance_every_n_days": int(frozen_params["rebalance_every_n_days"]),
        "rebalance_signal_stride": int(
            C.rebalance_signal_stride_for(int(frozen_params["rebalance_every_n_days"]))
        ),
        "frozen_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "source": "val 段三臂网格搜索（2022-01-01 ~ 2023-12-31）",
        "note": (
            "🔴 本文件冻结后才允许进测试段；测试段只跑一次。"
            "🔴 **测试段（2024-01-01 ~ 2026-11-03）未参与**本次选参（三臂对比只在 val 段做）。"
            f"🔴 调仓节奏（cadence = {frozen_params['rebalance_every_n_days']} 个交易日）"
            "**在 val 段搜索后冻结**：cadence≠20 与 V1 的 16.57× **不是同一口径**，不可直接对比；"
            "cadence=20 也只是**节奏**相同，**换手权重基准仍不同源** ⇒ 只趋势性对比。"
        ),
    }
    if ma_sweep is not None:
        frozen["ma_sweep"] = {
            "val_window": ma_sweep.get("val_window"),
            "grid": ma_sweep.get("grid"),
            "best_params": ma_sweep.get("best_params"),
            "fixed_from_round1": ma_sweep.get("fixed_from_round1"),
            "n_trials": ma_sweep.get("n_trials"),
        }
        frozen["source"] = "val 段网格搜索（2022-01-01 ~ 2023-12-31）＋ 第二轮 MA 窗口二次扫"
    path = HO.freeze_params(frozen_params, out_dir / "frozen_params.json")
    # 🔴 补写「测试段未参与」等审计字段（freeze_params 已封 0444 ⇒ 先放开再补，最后重新封只读）
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update({
        "winner_arm": winner_arm,
        "arm_spec": SM.arm_spec(winner_arm).as_dict(),
        "model_name": search_payload["model_name"],
        "selected_on": "val",
        "rebalance_every_n_days": int(frozen_params["rebalance_every_n_days"]),
        "rebalance_signal_stride": int(
            C.rebalance_signal_stride_for(int(frozen_params["rebalance_every_n_days"]))
        ),
        "cadence_disclosure": search_payload.get("cadence_disclosure") or {},
        "test_segment_note": (
            "🔴 **测试段未参与**：三臂对比与 topk/buffer 的选择**只在 val 段"
            f"（{C.VALID[0]} ~ {C.VALID[1]}）完成；测试段（{C.TEST[0]} ~ {C.TEST[1]}）"
            "单盲只跑最终冻结的这一臂一次。"
        ),
    })
    path.chmod(0o644)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    path.chmod(0o444)
    _log(f"[OK] 参数已冻结：{path}")
    return frozen


def stage_blind(args: argparse.Namespace, out_dir: Path) -> dict:
    """🔴 单盲验收：2024–2026 **只跑一次**（token 封存，第二次 ⇒ 抛错）。"""
    frozen = json.loads((out_dir / "frozen_params.json").read_text(encoding="utf-8"))
    params = dict(frozen.get("params") or frozen)
    model_name = params.pop("_model_name", None) or frozen.get("model_name")
    params["_model_name"] = model_name
    # 🔴🔴 调仓节奏（cadence）**必须来自冻结产物**（不许回落到常量 ⇒ 口径漂移）
    if "rebalance_every_n_days" not in params:
        raise RuntimeError(
            "🔴 冻结产物里没有 `rebalance_every_n_days`（cadence）⇒ **不许回落到常量**"
            "（口径漂移）⇒ 请重跑 `--stage freeze`（val 段搜索空间已含 cadence 维度）"
        )
    cadence = int(params["rebalance_every_n_days"])
    close, amount, names = load_market_data()
    scores_csv = out_dir / f"stock_scores_v2_{model_name}.csv"
    scores = pd.read_csv(scores_csv, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])
    score_dates = sorted(scores["date"].unique().tolist())
    all_dates = [d for d in score_dates if pd.Timestamp(C.VALID[0]) <= d <= pd.Timestamp(C.TEST[1])]
    bench = _benchmark_nav(close)

    def _run() -> dict:
        """单盲段的实际执行体（🔴 只在 ``run_single_blind`` 的闭包里被调用一次）。"""
        targets, diag = _build_targets_for(args, out_dir, params, close, amount, names, all_dates)
        bt = _backtest(close, targets, params)
        nav = bt["nav"]
        seg = nav.loc[(nav.index >= pd.Timestamp(C.TEST[0])) & (nav.index <= pd.Timestamp(C.TEST[1]))]
        to = bt.get("turnover_by_date")
        to_seg = None
        if to is not None:
            s = pd.Series(to)
            # 🔴🔴 2026-11-17 夜班审计补（**必崩点**）：`backtest_v2` 返回的
            #    `turnover_by_date` 是 **`dict[str, float]`**（键是 "YYYY-MM-DD" **字符串**）
            #    ⇒ `pd.Series(to)` 的 index 是**字符串** ⇒ 直接拿它跟
            #    `pd.Timestamp(...)` 比较 ⇒
            #    `TypeError: '>=' not supported between instances of 'str' and 'Timestamp'`
            #    ⇒ **盲测阶段必崩**（且崩在**一次性 token 已被消耗之后** ⇒ 代价极大）。
            #    ⇒ 修法：**切片前**把 index 转成 `DatetimeIndex`。
            s.index = pd.to_datetime(s.index)
            to_seg = s.loc[(s.index >= pd.Timestamp(C.TEST[0])) & (s.index <= pd.Timestamp(C.TEST[1]))]
        bench_seg = None
        if bench is not None:
            bench_seg = bench.loc[(bench.index >= pd.Timestamp(C.TEST[0])) &
                                  (bench.index <= pd.Timestamp(C.TEST[1]))]
        m_test = ME.summarize(seg, bench_seg, to_seg,
                              commission=float(params.get("commission", C.COMMISSION)),
                              slippage=float(params.get("slippage", C.SLIPPAGE)),
                              periods_per_year=250)
        m_val = ME.summarize(nav.loc[(nav.index >= pd.Timestamp(C.VALID[0])) &
                                      (nav.index <= pd.Timestamp(C.VALID[1]))],
                              None, None, periods_per_year=250)
        ic_test = _rank_ic_of(scores[scores["date"] >= pd.Timestamp(C.TEST[0])])
        # 🔴 val 段 RankIC 同样要裁掉「标签窗口伸进测试段」的截面
        _val_scores = scores[(scores["date"] >= pd.Timestamp(C.VALID[0])) &
                             (scores["date"] <= pd.Timestamp(C.VALID[1]))]
        _keep = set(val_score_dates_without_label_leak(sorted(_val_scores["date"].unique())))
        ic_val = _rank_ic_of(_val_scores[_val_scores["date"].isin(_keep)])
        diag_path = out_dir / "blind_diagnostics.csv"
        diag.to_csv(diag_path, index=False, encoding="utf-8-sig")
        return {
            "params": params,
            "model_name": model_name,
            "rebalance_every_n_days": cadence,
            "rebalance_signal_stride": int(C.rebalance_signal_stride_for(cadence)),
            "test_window": [C.TEST[0], C.TEST[1]],
            "val_window": [C.VALID[0], C.VALID[1]],
            "test_metrics": m_test,
            "val_metrics": m_val,
            "rank_ic_test": ic_test,
            "rank_ic_val": ic_val,
            "n_rebalances_total": len(targets),
            "n_rebalances_test": len([d for d in targets if pd.Timestamp(C.TEST[0]) <= pd.Timestamp(d) <= pd.Timestamp(C.TEST[1])]),
            "diagnostics_csv": str(diag_path),
            "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        }

    fingerprint = json.dumps(params, sort_keys=True, default=str)
    # 🔴🔴 **机器无关的单盲登记**（审计发现 #4：token 绑在 ``--out-dir`` 上 ⇒ 换个目录
    #    就能无声把测试段跑第二遍）⇒ 这里额外查/写一个**固定路径**的登记表。
    registry_path = Path(C.DEFAULT_V2_RUN_ROOT) / "_single_blind_registry.json"
    _registry = {}
    if registry_path.is_file():
        try:
            _registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _registry = {}
    _tag = "test_2024_2026"
    if _tag in (_registry.get("tags") or {}) and not bool(args.allow_rerun_blind):
        raise EV.SingleBlindViolation(
            f"🔴 单盲段 {_tag} **在固定登记处已被消耗**（{registry_path}，"
            f"首次登记 {(_registry.get('tags') or {}).get(_tag, {}).get('created_at', '未知')}）"
            f"⇒ 测试段只许跑一次；换 --out-dir **不能**绕过这道闸门"
        )
    # 🔴🔴 **预登记**（崩溃路径上也要 fail-closed）：旧口径是「评估**成功后**才写登记」
    #    ⇒ 测试段**中途崩溃 ⇒ 登记表里零痕迹** ⇒ 可以再跑一次（换 --out-dir 也拦不住
    #    ——因为登记表压根没写）⇒ 红线「只跑一次」在**崩溃路径上失效**。
    #    现在：**开跑前**先写一份 ``status="running"`` 的登记 ⇒ 崩了也留下「已消耗」痕迹。
    _registry.setdefault("tags", {})
    _registry["tags"][_tag] = {
        "created_at": (_registry["tags"].get(_tag) or {}).get("created_at")
        or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "running",
        "fingerprint": fingerprint,
        "out_dir": str(out_dir),
        "reason": str(args.blind_rerun_reason),
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if registry_path.exists():
        registry_path.chmod(0o644)
    registry_path.write_text(json.dumps(_registry, ensure_ascii=False, indent=2, sort_keys=True),
                             encoding="utf-8")
    registry_path.chmod(0o444)
    result = EV.run_single_blind(
        out_dir, _tag, _run,
        allow_rerun=bool(args.allow_rerun_blind),
        reason=str(args.blind_rerun_reason),
        fingerprint=fingerprint,
    )
    # 登记（追加式；只在评估成功后写）
    _registry.setdefault("tags", {})
    _registry["tags"][_tag] = {
        "created_at": (_registry["tags"].get(_tag) or {}).get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "done",
        "fingerprint": fingerprint,
        "out_dir": str(out_dir),
        "reason": str(args.blind_rerun_reason),
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    if registry_path.exists():
        registry_path.chmod(0o644)
    registry_path.write_text(json.dumps(_registry, ensure_ascii=False, indent=2, sort_keys=True),
                             encoding="utf-8")
    registry_path.chmod(0o444)
    _log(f"[OK] 单盲段完成（已封存）：年化={result['test_metrics'].get('annualized_return')} "
         f"MDD={result['test_metrics'].get('max_drawdown')} "
         f"换手={result['test_metrics'].get('annualized_turnover')}")
    return result


def _fmt_metric(value: Any) -> str:
    """指标格式化（百分比；缺失 ⇒ —）。"""
    try:
        if value is None:
            return "—"
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def render_cadence_disclosure_markdown(disc: dict) -> list[str]:
    """🔴 把 cadence 披露块渲染成 **成绩单章节**的 Markdown 行。"""
    lines: list[str] = ["## 8. 🔴 调仓节奏（cadence）口径披露", ""]
    if not disc:
        lines.append("- —（搜索产物里没有 cadence 披露块 ⇒ 请重跑 `--stage search`）")
        return lines
    for note in disc.get("notes") or []:
        lines.append(f"> {note}")
        lines.append("")
    lines.append(f"- cadence 候选：`{disc.get('candidates')}`"
                 f"｜V1 同**节奏**档：`{disc.get('v1_equivalent_cadence')}`"
                 f"｜V1 年化换手（**双边**）：`{disc.get('v1_annualized_turnover_two_way')}`×"
                 f"（单边 `{disc.get('v1_annualized_turnover_one_way')}`×）"
                 f"｜本项目标：≤ `{disc.get('turnover_target')}`×"
                 f"｜选中 cadence：`{disc.get('selected_cadence')}`"
                 f"（stride = `{disc.get('selected_rebalance_signal_stride')}`）")
    lines.append("")
    # 🔴 2026-11-17 夜班审计修：原来「年化换手」列取的是**最佳 trial** 的换手，
    #    而「≤4×」列取的是**该档所有 trial 的最小换手** ⇒ 两列可能来自**不同 trial**
    #    ⇒ 读者会看到「15.2× / ≤4× ✅」这种自相矛盾的行。
    #    ⇒ 现在把「最低年化换手」单独列出来，并把来源写进表头。
    # 🔴 2026-11-18 换手口径正名：这两列的值都是**双边**（Σ|Δw|）⇒ 表头标口径，
    #    免得读者把它当单边（历史上就踩过这个坑）。
    lines.append("| cadence | stride | trial 数 | 最佳 trial 年化 | 超额 | MDD | "
                 "最佳 trial 年化换手（双边） | 该档**最低**年化换手（双边） | "
                 "调仓次数 | 有 trial ≤4× | 与 V1 **节奏**相同 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for key, entry in (disc.get("per_cadence") or {}).items():
        m = ((entry.get("best_trial") or {}).get("metrics")) or {}
        turn = m.get("annualized_turnover")
        lines.append(
            f"| {key} | {entry.get('rebalance_signal_stride')} | {entry.get('n_trials')} | "
            f"{_fmt_metric(m.get('annualized_return'))} | "
            f"{_fmt_metric(m.get('excess_vs_benchmark'))} | "
            f"{_fmt_metric(m.get('max_drawdown'))} | "
            f"{'—' if turn is None else f'{float(turn):.3f}×'} | "
            # 🔴 2026-11-18：原写法是 **PEP 701**（3.12+ 才允许 f-string 内再嵌同类型引号的
            #    f-string）⇒ 在 host 的 Python 3.10 上直接 SyntaxError ⇒ 整包 import 失败。
            #    等价改写：把内层格式化提到表达式外（语义完全一致，3.10/3.12 都能跑）。
            f"{('—' if entry.get('min_annualized_turnover') is None else format(float(entry.get('min_annualized_turnover')), '.3f') + '×')} | "
            f"{entry.get('n_rebalances')} | "
            f"{'✅' if entry.get('any_trial_within_turnover_target') else '🔴 否'} | "
            f"{'✅ 是（但换手口径仍不同源 ⇒ 只趋势性对比）' if entry.get('comparable_to_v1') else '🔴 否（不可与 V1 直接比）'} |"
        )
    lines.append("")
    for key, entry in (disc.get("per_cadence") or {}).items():
        lines.append(f"- cadence = {key}：{entry.get('sample_note')}")
    lines.append("")
    return lines


def append_cadence_disclosure_to_scorecard(out_dir: Path) -> Path | None:
    """🔴 把 **cadence（调仓节奏）口径披露**追加进成绩单（``V2_SCOREBOARD.md``）末尾。

    🔴 **为什么在这里追加**：``v2/report.py`` 不在本任务的写域内 ⇒ 披露章节由本模块
    生成并追加，内容与 ``val_search.json`` / ``arm_search_val.json`` 里的
    ``cadence_disclosure`` **同源**（同一个 :func:`build_cadence_disclosure`）。
    """
    payload: dict = {}
    for name in ("arm_search_val.json", "val_search.json"):
        path = out_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload:
            break
    disc = payload.get("cadence_disclosure") or {}
    scorecard = out_dir / "V2_SCOREBOARD.md"
    if not scorecard.is_file():
        return None
    body = scorecard.read_text(encoding="utf-8").rstrip("\n")
    scorecard.write_text(body + "\n\n" + "\n".join(render_cadence_disclosure_markdown(disc)) + "\n",
                         encoding="utf-8")
    return scorecard


def stage_report(args: argparse.Namespace, out_dir: Path) -> Path:
    """阶段：渲染 V2 成绩单 Markdown（**含 cadence 口径披露章节**）。"""
    import report as RP

    path = RP.write_report(out_dir)
    appended = append_cadence_disclosure_to_scorecard(out_dir)
    if appended is not None:
        _log(f"[OK] cadence 口径披露已追加进成绩单：{appended}")
    _log(f"[OK] V2 报告已写出：{path}")
    return path


# ----------------------------------------------------------------- main
def run_pipeline(args: argparse.Namespace) -> dict:
    """V2 编排主流程（按 ``--stage`` 依次推进各阶段）。"""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = str(args.stage).lower()
    t0 = time.time()
    _log(f"=== V2 编排启动（stage={stage}，out={out_dir}）===")
    # 🔴 qlib 必须先 init（否则 handler 构造报 "Please run qlib.init() first"）
    TS.init_qlib(args.provider_uri)
    if stage in ("all", "train"):
        stage_train(args, out_dir)
    if stage in ("all", "ablation"):
        stage_ablation(args, out_dir)
    if stage in ("all", "score"):
        stage_score(args, out_dir)
    if stage in ("all", "search"):
        stage_search(args, out_dir)
    if stage in ("all", "freeze"):
        stage_freeze(args, out_dir)
    if stage in ("all", "blind"):
        stage_blind(args, out_dir)
    if stage in ("all", "report"):
        stage_report(args, out_dir)
    _log(f"=== V2 编排结束，用时 {time.time() - t0:.1f}s ===")
    return {"stage": stage, "out_dir": str(out_dir), "elapsed_sec": round(time.time() - t0, 1)}


def main() -> int:
    """V2 编排器入口。"""
    args = parse_args()
    run_pipeline(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
