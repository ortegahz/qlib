"""🔴 V3 验收指标（S 层三项硬指标）—— 用户 2026-11-26 目标文档第 ⑤ 条。

**为什么单独一个文件**：V3 的验收必须**可复算、可对表、只读**；本模块**不训练、不回测**、
**不碰 qlib**，只做「分数 + 价格面板 ⇒ 指标」的纯计算 ⇒ 单元测试可在**本机**用合成数据跑。

**三项指标（逐字照抄目标文档）**

1. **ETF 成分股并集内的 20 日滚动 RankIC 均值与 ICIR**
   ⇒ 检验「预测周期与标的范围是否真正对齐」；
2. **打分 Top 10% 成分股组合的费后超额收益**
   ⇒ 检验「多头头部区分度」（防止模型只擅长排雷不擅长选牛股）；
3. **接入 V2 规则层后的端到端夏普比率与换手率**
   ⇒ 确认更高的上游 RankIC 确实转化成了下游更稳的净值
   （第 ③ 项在 `manu/tools/v3_comparison_probe.py` 里跑，本模块只提供**指标口径的纯函数**）。

🔴 **口径披露（不许夸大）**

* 「20 日滚动 RankIC」：本实现 = **逐打分日**的 Spearman RankIC（标签 = **t+15~t+25 均价退出前瞻超额收益（≈20 日周期）**），
  再对**IC 序列**做统计（均值 / 标准差 / ICIR / t / 胜率），并另给**20 个打分日滚动均值**序列的统计
  ⇒ 「20 日」既指**标签周期**，也指**滚动窗口长度**，两者在报告里分别写明。
* 🔴 **排序目标下的逐日中心化是不变量**：减当日截面均值**不改变**同日截面排名
  ⇒ RankIC 与 Top-10% 组合这两项指标**天然对「是否中心化」不敏感**；
  中心化的真实作用在 ① MSE 对照臂 ② 评估层超额口径 ③ 训练语义（跑赢 ETF 成分池均值）。
* **Top 10% 组合**：在**信号日到下一个信号日**之间等权持有（不重叠），
  费后 = 毛超额 − 2×(佣金+滑点)（每期**整体换仓** ⇒ 买 1 ＋ 卖 1）。
* **ICIR 口径**：`ICIR = mean(IC) / std(IC)`（逐日 IC 序列，非年化）；
  `t = mean / (std / sqrt(n))`（🔴 样本量必须随行披露）。
"""

from __future__ import annotations


import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

import config_v3 as C

__all__ = [
    "rank_ic_series",
    "icir_stats",
    "top_decile_excess",
    "scores_to_panel",
]


# ----------------------------------------------------------------- 工具
def scores_to_panel(scores: pd.DataFrame) -> pd.DataFrame:
    """``DataFrame[date, instrument, score]`` ⇒ 宽表（index=日期, columns=标的）。"""
    if scores is None or scores.empty:
        return pd.DataFrame()
    df = scores.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["instrument"] = df["instrument"].astype(str)
    panel = df.pivot_table(index="date", columns="instrument", values="score", aggfunc="last")
    return panel.sort_index()


def _filter_universe(panel: pd.DataFrame, universe: Iterable[str] | None) -> pd.DataFrame:
    """把面板限制到给定标的集合（`None` ⇒ 不限制）。"""
    if universe is None:
        return panel
    keep = {str(x) for x in universe}
    cols = [c for c in panel.columns if str(c) in keep]
    return panel.loc[:, cols]


def _fmt_day(value: Any) -> str:
    """把索引值格式化成日期字符串（**非**日期索引时原样转字符串）。"""
    try:
        return str(pd.Timestamp(value).date())
    except (TypeError, ValueError):
        return str(value)


def _spearman(x: pd.Series, y: pd.Series) -> float:
    """两列的 Spearman 秩相关（并列用平均秩；样本不足或常量 ⇒ NaN）。"""
    m = pd.concat([x, y], axis=1).dropna()
    if len(m) < 3:
        return float("nan")
    if m.iloc[:, 0].nunique() < 2 or m.iloc[:, 1].nunique() < 2:
        return float("nan")
    return float(m.iloc[:, 0].corr(m.iloc[:, 1], method="spearman"))


# ----------------------------------------------------------------- 指标 ①
def rank_ic_series(
    scores: pd.DataFrame | pd.Series,
    fwd_ret: pd.DataFrame,
    universe: Iterable[str] | None = None,
    min_names: int = 30,
) -> pd.Series:
    """逐打分日的 **Spearman RankIC**（分数 vs t+15~t+25 均价退出前瞻超额收益（≈20 日周期））。

    参数
    ----
    scores : ``DataFrame[date, instrument, score]`` 或宽表（index=日期, columns=标的）。
    fwd_ret : 宽表（index=日期, columns=标的）＝ **t+15~t+25 均价退出前瞻超额收益（≈20 日周期）**（原始收益即可：
        🔴 减同一天的基准常数**不改变**同日截面排名 ⇒ RankIC 数值不变）。
    universe : 限定标的集合（ETF 成分股并集）；`None` ⇒ 不限定。
    min_names : 当日最少有效样本数（不足 ⇒ 该日记 NaN，不计入统计）。

    返回：Series（index=打分日，值=RankIC）。
    """
    panel = scores_to_panel(scores) if isinstance(scores, pd.DataFrame) else scores.copy()
    panel = _filter_universe(panel, universe)
    fwd = _filter_universe(fwd_ret, universe)
    if panel.empty or fwd.empty:
        return pd.Series(dtype="float64")
    out: dict[pd.Timestamp, float] = {}
    for day in panel.index:
        srow = panel.loc[day].dropna()
        if srow.empty:
            continue
        frow = fwd.reindex(index=[day]).iloc[0] if day in fwd.index else pd.Series(dtype="float64")
        aligned = pd.concat([srow.rename("s"), frow.rename("f")], axis=1).dropna()
        if len(aligned) < int(min_names):
            continue
        out[day] = _spearman(aligned["s"], aligned["f"])
    return pd.Series(out, dtype="float64").sort_index()


def icir_stats(ic: pd.Series, rolling_window: int | None = None) -> dict:
    """IC 序列统计：均值 / 标准差 / **ICIR** / t / 胜率 / n / 滚动均值序列统计。

    🔴 `t = mean / (std / sqrt(n))`（独立同分布近似；序列自相关时**高估** ⇒ 报告须标注）。
    🔴 `ICIR = mean / std`（**非年化**；年化 ICIR = ICIR × sqrt(年化次数)）。
    """
    s = pd.Series(ic, dtype="float64").dropna()
    n = int(len(s))
    if n == 0:
        return {"n": 0, "mean_ic": float("nan"), "std_ic": float("nan"), "icir": float("nan"),
                "t_stat": float("nan"), "win_rate": float("nan")}
    mean = float(s.mean())
    std = float(s.std(ddof=1)) if n > 1 else float("nan")
    # 🔴 **退化保护**：常数序列的 std 会是 ~1e-17 的浮点残值 ⇒ 直接相除会得到
    #    「天文数字 ICIR」这种**看起来显著、实际毫无信息**的结果 ⇒ 低于数值地板一律判「不可定义」。
    std_floor = 1e-12
    usable = bool(np.isfinite(std) and std > std_floor)
    icir = mean / std if usable else float("nan")
    t_stat = mean / (std / math.sqrt(n)) if usable else float("nan")
    stats: dict[str, Any] = {
        "n": n,
        "mean_ic": mean,
        "std_ic": std,
        "icir": icir,
        "t_stat": t_stat,
        "win_rate": float((s > 0).mean()),
        "first_date": _fmt_day(s.index.min()) if n else None,
        "last_date": _fmt_day(s.index.max()) if n else None,
    }
    if rolling_window:
        roll = s.rolling(int(rolling_window), min_periods=max(3, int(rolling_window) // 2)).mean().dropna()
        stats["rolling_window"] = int(rolling_window)
        stats["rolling_n"] = int(len(roll))
        stats["rolling_mean"] = float(roll.mean()) if len(roll) else float("nan")
        stats["rolling_std"] = float(roll.std(ddof=1)) if len(roll) > 1 else float("nan")
        stats["rolling_positive_ratio"] = float((roll > 0).mean()) if len(roll) else float("nan")
    return stats


# ----------------------------------------------------------------- 指标 ②
def top_decile_excess(
    scores: pd.DataFrame,
    price_panel: pd.DataFrame,
    benchmark_price: pd.Series,
    universe: Iterable[str] | None = None,
    top_fraction: float | None = None,
    commission: float | None = None,
    slippage: float | None = None,
    min_names: int = 20,
    periods_per_year: float = 24.4,
) -> dict:
    """**打分 Top 10% 成分股组合的费后超额收益**（信号日 → 下一信号日，等权、不重叠）。

    参数
    ----
    scores : ``DataFrame[date, instrument, score]``（或宽表）。
    price_panel : 宽表（index=日期, columns=标的）＝ 成交价（🔴 用 `$vwap` 与标签同价口径）。
    benchmark_price : Series（index=日期）＝ 基准同价序列（沪深300）。
    universe : 限定标的集合（ETF 成分股并集）。
    top_fraction : 取前多少比例（默认 ``config_v3.ACCEPT_TOP_FRACTION`` = 0.10）。
    commission / slippage : 单边费率（默认 ``config_v3``）。
    min_names : 当日最少可选样本（不足 ⇒ 该期跳过）。
    periods_per_year : 年化用的期数（默认 24.4 ≈ 244/10，与 10 交易日信号栅格对齐）。

    口径
    ----
    * 每期：**整体换仓**（买满 + 卖空）⇒ 成本 = ``2 × (commission + slippage)``；
    * 超额 = 组合期收益 − 基准期收益（**先毛后费**）；
    * 组合收益 = 等权持有 Top-k 的**价格比**收益均值。
    """
    frac = float(C.ACCEPT_TOP_FRACTION if top_fraction is None else top_fraction)
    comm = float(C.COMMISSION if commission is None else commission)
    slip = float(C.SLIPPAGE if slippage is None else slippage)
    panel = scores_to_panel(scores) if isinstance(scores, pd.DataFrame) else scores.copy()
    panel = _filter_universe(panel, universe)
    if panel.empty or price_panel.empty:
        return {"n_periods": 0, "mean_excess_net": float("nan")}
    px = price_panel.copy()
    px.index = pd.to_datetime(px.index).normalize()
    px = px.sort_index()
    bench = pd.Series(benchmark_price, dtype="float64").copy()
    bench.index = pd.to_datetime(bench.index).normalize()
    bench = bench.sort_index()

    score_days = [d for d in panel.index if d in px.index]
    rows: list[dict[str, Any]] = []
    for i in range(len(score_days) - 1):
        d0, d1 = score_days[i], score_days[i + 1]
        srow = panel.loc[d0].dropna()
        srow = srow[[c for c in srow.index if c in px.columns]]
        srow = srow.dropna()
        if len(srow) < int(min_names):
            continue
        k = max(1, int(math.ceil(frac * len(srow))))
        top = srow.sort_values(ascending=False).index[:k]
        p0 = px.loc[d0, top]
        p1 = px.reindex(index=[d1], columns=list(top)).iloc[0]
        pair = pd.concat([p0.rename("p0"), p1.rename("p1")], axis=1).dropna()
        if pair.empty:
            continue
        port = float((pair["p1"] / pair["p0"] - 1.0).mean())
        if d0 in bench.index and d1 in bench.index:
            b0, b1 = float(bench.loc[d0]), float(bench.loc[d1])
            bench_ret = (b1 / b0 - 1.0) if b0 and np.isfinite(b0) and b0 > 0 else float("nan")
        else:
            bench_ret = float("nan")
        gross = port - bench_ret if np.isfinite(bench_ret) else float("nan")
        cost = 2.0 * (comm + slip)
        rows.append({
            "date": d0, "n_names": int(len(pair)), "port_ret": port, "bench_ret": bench_ret,
            "gross_excess": gross, "cost": cost,
            "net_excess": gross - cost if np.isfinite(gross) else float("nan"),
            "holding_days": int((px.index.get_loc(d1) - px.index.get_loc(d0))),
        })
    if not rows:
        return {"n_periods": 0, "mean_excess_net": float("nan"), "top_fraction": frac}
    df = pd.DataFrame(rows)
    net = df["net_excess"].dropna()
    n = int(len(net))
    mean = float(net.mean()) if n else float("nan")
    std = float(net.std(ddof=1)) if n > 1 else float("nan")
    t_stat = mean / (std / math.sqrt(n)) if n > 1 and std > 0 else float("nan")
    gross = df["gross_excess"].dropna()
    return {
        "n_periods": n,
        "top_fraction": frac,
        "mean_n_names": float(df["n_names"].mean()),
        "mean_holding_days": float(df["holding_days"].mean()),
        "mean_port_ret": float(df["port_ret"].mean()),
        "mean_bench_ret": float(df["bench_ret"].dropna().mean()),
        "mean_excess_gross": float(gross.mean()) if len(gross) else float("nan"),
        "mean_excess_net": mean,
        "std_excess_net": std,
        "t_stat_net": t_stat,
        "win_rate_net": float((net > 0).mean()) if n else float("nan"),
        "annualized_excess_net": mean * periods_per_year if np.isfinite(mean) else float("nan"),
        "annualized_excess_gross": (float(gross.mean()) * periods_per_year) if len(gross) else float("nan"),
        "total_cost_drag": float(df["cost"].sum()),
        "per_period": rows,
    }


# ----------------------------------------------------------------- 指标 ③（纯函数部分）
def end_to_end_metrics(nav: pd.Series, benchmark_nav: pd.Series | None,
                       turnover: Any = None, periods_per_year: int = 250) -> dict:
    """端到端指标（夏普 / 回撤 / 换手）—— **不 import V2**，本机可测。

    🔴 口径与 `manu/etf_rotation/v2/metrics.py::summarize` **对齐**：
    * 年化收益 = (nav[-1] / nav[0]) ** (periods_per_year / (n-1)) − 1；
    * Sharpe = 年化收益 / 年化波动（**标准差**口径，无风险利率取 0）；
    * 最大回撤 = min(nav / cummax(nav) − 1)；
    * 换手 = **双边** Σ|Δw|；年化换手 = 双边 / 年数。
    """
    s = pd.Series(nav, dtype="float64").dropna()
    if len(s) < 2:
        return {"n": int(len(s)), "sharpe": float("nan"), "annualized_return": float("nan"),
                "max_drawdown": float("nan"), "total_turnover": float("nan"),
                "annualized_turnover": float("nan"),
                # 🔴 双边与单边**必须成对落盘**（NaN 也成对 ⇒ 不许「双边有、单边缺」）
                "annualized_turnover_one_way": float("nan")}
    years = (len(s) - 1) / float(periods_per_year)
    total_ret = float(s.iloc[-1] / s.iloc[0] - 1.0)
    ann = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    rets = s.pct_change().dropna()
    vol = float(rets.std(ddof=1)) * math.sqrt(periods_per_year) if len(rets) > 1 else float("nan")
    sharpe = ann / vol if vol and np.isfinite(vol) and vol > 0 else float("nan")
    dd = float((s / s.cummax() - 1.0).min())
    if turnover is None:
        total_to = float("nan")
        ann_to = float("nan")
    elif isinstance(turnover, (int, float)):
        total_to = float(turnover)
        ann_to = total_to / years if years > 0 else float("nan")
    else:
        ts = pd.Series(turnover, dtype="float64").dropna()
        total_to = float(ts.sum()) if len(ts) else float("nan")
        ann_to = total_to / years if years > 0 else float("nan")
    out = {"n": int(len(s)), "years": years, "total_return": total_ret, "annualized_return": ann,
           "annualized_vol": vol, "sharpe": sharpe, "max_drawdown": dd,
           "total_turnover": total_to, "annualized_turnover": ann_to,
           # 🔴 单边换算走 **V3 自己的唯一实现**（不许手写 `* 0.5` ⇒ 换手口径会出现第二份实现）
           "annualized_turnover_one_way": C.one_way_turnover(ann_to)}
    if benchmark_nav is not None:
        b = pd.Series(benchmark_nav, dtype="float64").dropna()
        common = s.index.intersection(b.index)
        if len(common) > 2:
            sb, bb = s.loc[common], b.loc[common]
            years_b = (len(common) - 1) / float(periods_per_year)
            tr_b = float(bb.iloc[-1] / bb.iloc[0] - 1.0)
            ann_b = (1.0 + tr_b) ** (1.0 / years_b) - 1.0 if years_b > 0 else float("nan")
            out["benchmark_annualized_return"] = ann_b
            out["excess_annualized_return"] = ann - ann_b if np.isfinite(ann) and np.isfinite(ann_b) else float("nan")
    return out
