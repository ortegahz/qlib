"""🔴 V2 **ETF 层归因**工具：回答「Alpha 是在 ``E = W × S`` 这步丢的，还是被
``MA120 / MA80 / cadence=90`` 绞杀的？」。

🔴 **为什么要有这个模块**（2026-11-18 目标文档）：
   V2 单盲结果 = 个股层 RankIC **0.1271**（t=9.04）但 ETF 组合 **−12.93% / 回撤 −54.93%**
   ⇒ 病根必在**下游 ETF 轮动层**。要拆解，就必须能在**不重跑选股**的前提下
   单独观察「ETF 层的 E 面板」：

     * :func:`etf_forward_return_panel` —— ETF 的**前瞻收益**面板（h 个交易日）；
     * :func:`etf_rank_ic` —— **ETF 层面**的逐日截面 Spearman IC（= 目标文档要求的
       「ETF 层面的 20 日 RankIC」）；
     * :func:`rebalance_decomposition` —— 每个调仓日「**选中组合 vs 候选池**」的
       超额拆解（回答「排序有没有比瞎买池子强」）。

🔴 **口径纪律**：
   * 本模块**只做计算**，**不碰**任何超参搜索、不读测试段；
   * 所有横截面统计与 ``v2/metrics.py::rank_ic`` **同口径**（逐日截面 Spearman ⇒
     均值 / ICIR / t 值）；样本不足时返回 ``None``（**不许编**）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import metrics as ME  # noqa: E402

__all__ = [
    "etf_forward_return_panel",
    "etf_rank_ic",
    "rebalance_decomposition",
    "pool_score_stats",
]


# ----------------------------------------------------------------- 前瞻收益
def etf_forward_return_panel(close: pd.DataFrame, horizon: int = 20) -> pd.DataFrame:
    """ETF 的**前瞻收益**面板（index=交易日，columns=ETF 代码，值 = ``h`` 个交易日后收益）。

    🔴 ``h`` 按**交易日行数**计（不是日历天）⇒ 与标签窗口口径一致。
    🔴 **尾部天然缺失**：最后 ``h`` 行没有前瞻 ⇒ NaN（**不许**用未来数据回填）。
    """
    h = int(horizon)
    if h <= 0:
        raise ValueError(f"🔴 horizon 必须 > 0（实得 {h}）")
    px = close.astype("float64").sort_index()
    fwd = px.shift(-h) / px - 1.0
    return fwd.replace([np.inf, -np.inf], np.nan)


# ----------------------------------------------------------------- ETF 层 RankIC
def etf_rank_ic(
    score_panel: pd.DataFrame,
    fwd_panel: pd.DataFrame,
    *,
    min_names: int = 3,
) -> dict[str, Any]:
    """🔴 **ETF 层面**的逐日截面 RankIC（``score_panel`` vs ``fwd_panel``）。

    * 每个截面日：``Spearman(score, fwd)``（**逐日**截面 ⇒ 与个股层同口径）；
    * 汇总：``ic`` = 各截面 IC 均值，``icir`` = 均值 / 标准差，``t_stat`` = 均值 / (标准差/√n)；
    * 有效截面 < 2 ⇒ ``ic/icir/t_stat`` 全 ``None``（**样本不足不许编**）；
    * ``min_names``：当日有效 ETF 数不足 ⇒ 该截面**不计**（ETF 池只有 ~20 只 ⇒ 默认 3 只）。

    返回 ``{"ic","icir","t_stat","n_cross_sections"}``（与 ``metrics.rank_ic`` 同结构，
    额外带 ``n_cross_sections`` / ``mean_names`` 便于解读样本量）。
    """
    if score_panel is None or len(score_panel) == 0:
        return {"ic": None, "icir": None, "t_stat": None, "n_cross_sections": 0,
                "mean_names": 0.0, "note": "🔴 分数面板为空"}
    sp = score_panel.copy()
    fp = fwd_panel.reindex(index=sp.index, columns=sp.columns)
    dates: list[Any] = []
    preds: list[float] = []
    fwds: list[float] = []
    for day, grp in sp.iterrows():
        sub = pd.DataFrame({"pred": grp, "fwd": fp.loc[day]}).dropna()
        if len(sub) < int(min_names):
            continue
        dates.extend([day] * len(sub))
        preds.extend(sub["pred"].astype(float).tolist())
        fwds.extend(sub["fwd"].astype(float).tolist())
    out = ME.rank_ic(dates, preds, fwds)
    out["mean_names"] = float(len(preds) / out["n_cross_sections"]) if out["n_cross_sections"] else 0.0
    return out


# ----------------------------------------------------------------- 候选池统计
def pool_score_stats(diag: pd.DataFrame) -> pd.DataFrame:
    """逐调仓日的**候选池 E 统计**（中位数 / 均值 / 负分占比 / 候选数）。

    🔴 **候选池口径** = ``coverage_ok & liquidity_ok & tradable`` 且**未被退场 MA 强平**；
       与 :func:`etf_layer.build_targets` 的「入场 MA 过滤**之前**」的池子一致
       （⇒ 负分闸门的统计量口径与这里一致）。
    """
    if diag is None or len(diag) == 0:
        return pd.DataFrame(columns=["date", "n_candidates", "median_e", "mean_e",
                                     "frac_neg_e", "n_selected"])
    df = diag.copy()
    df["date"] = df["date"].astype(str)
    cand = df[df["coverage_ok"].astype(bool) & df["liquidity_ok"].astype(bool)
              & df["tradable"].astype(bool)].copy()
    rows = []
    for day, grp in cand.groupby("date", sort=True):
        vals = pd.to_numeric(grp["alpha_score"], errors="coerce").dropna()
        sel = df[(df["date"] == day) & (df["selected"].astype(bool))]
        rows.append({
            "date": day,
            "n_candidates": int(len(grp)),
            "median_e": float(vals.median()) if len(vals) else np.nan,
            "mean_e": float(vals.mean()) if len(vals) else np.nan,
            "frac_neg_e": float((vals < 0).mean()) if len(vals) else np.nan,
            "n_selected": int(len(sel)),
            "selected": ",".join(str(c) for c in sel["code"].tolist()),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- 选股 vs 池子
def rebalance_decomposition(
    diag: pd.DataFrame,
    fwd_panel: pd.DataFrame,
    horizon: int = 20,
) -> pd.DataFrame:
    """逐调仓日拆解「**选中组合** vs **候选池**」的 ``horizon`` 日前瞻收益。

    * ``pool_fwd`` = 候选池（等权）前瞻收益（**瞎买池子的基准**）；
    * ``selected_fwd`` = 实际选中组合（等权）前瞻收益；
    * ``excess`` = ``selected_fwd - pool_fwd``（**排序是否真的比池子强**）。

    🔴 这是「Alpha 有没有在 ETF 层被吃掉」的**直接证据**：
       ``excess`` 系统性 ≤ 0 ⇒ 排序层无效（问题在排序，不在信号）。
    """
    if diag is None or len(diag) == 0:
        return pd.DataFrame()
    df = diag.copy()
    df["date"] = df["date"].astype(str)
    fwd = fwd_panel.copy()
    fwd.index = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in fwd.index]
    rows = []
    for day, grp in df.groupby("date", sort=True):
        if day not in fwd.index:
            continue
        frow = fwd.loc[day]
        cand = grp[grp["coverage_ok"].astype(bool) & grp["liquidity_ok"].astype(bool)
                   & grp["tradable"].astype(bool)]
        pool_vals = pd.to_numeric(frow.reindex(cand["code"].tolist()), errors="coerce").dropna()
        sel = grp[grp["selected"].astype(bool)]
        sel_vals = pd.to_numeric(frow.reindex(sel["code"].tolist()), errors="coerce").dropna()
        if not len(pool_vals) or not len(sel_vals):
            continue
        rows.append({
            "date": day,
            "n_candidates": int(len(pool_vals)),
            "n_selected": int(len(sel_vals)),
            "pool_fwd": float(pool_vals.mean()),
            "selected_fwd": float(sel_vals.mean()),
            "excess": float(sel_vals.mean() - pool_vals.mean()),
            "selected": ",".join(str(c) for c in sel["code"].tolist()),
        })
    return pd.DataFrame(rows)


def summarize_decomposition(dec: pd.DataFrame, horizon: int = 20) -> dict[str, Any]:
    """把 :func:`rebalance_decomposition` 的结果压成一句话结论（供报告直接引用）。"""
    if dec is None or len(dec) == 0:
        return {"n_periods": 0, "note": "🔴 无可分解的调仓日"}
    exc = pd.to_numeric(dec["excess"], errors="coerce").dropna()
    return {
        "n_periods": int(len(dec)),
        "horizon_days": int(horizon),
        "mean_pool_fwd": float(pd.to_numeric(dec["pool_fwd"], errors="coerce").mean()),
        "mean_selected_fwd": float(pd.to_numeric(dec["selected_fwd"], errors="coerce").mean()),
        "mean_excess": float(exc.mean()) if len(exc) else None,
        "hit_rate_excess_gt0": float((exc > 0).mean()) if len(exc) else None,
        "min_excess": float(exc.min()) if len(exc) else None,
        "max_excess": float(exc.max()) if len(exc) else None,
    }
