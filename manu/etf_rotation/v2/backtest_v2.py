"""🔴 V2 回测引擎（Cost-Aware + Rank Buffer 计入换手）。

口径（与 V1 ``manu/etf_rotation/backtest.py`` 对齐；V1 是**冻结 baseline**，本模块
**不 import V1**，只按其口径说明复刻）：

======================  ======================================================
维度                    取值 / 依据
======================  ======================================================
换手                    ``Σ_代码 |目标权重 − 调仓前漂移权重|``（**双边**；单边 = 0.5×）
                        ⇒ **Rank Buffer 造成的留存量/修剪量自动计入**（对齐③的落点）
成本                    ``cost_rate = (commission + slippage) × turnover``，
                        **在调仓当天**从净值里扣
权重演化                调仓之间权重随价格**漂移**（drift），净值按漂移后权重演化
执行时序                目标权重 = **调仓日收盘成交** ⇒ 该日收益归**上一段**持仓，
                        新权重自**次日**起承担收益（与 V1 ``backtest.py`` 的 (exe_i, exe_{i+1}] 同口径）
不可交易                目标里不可交易的代码 ⇒ **不换手、保持原仓**；持仓里卖不掉的
                        ⇒ 同样保持原仓；两者都记入 ``blocked`` 明细
======================  ======================================================

⚠️ **与 V1 换手定义的一处差异（必须披露）**：V1 ``backtest.py:204`` 用的是
「上期**目标**权重」（未漂移）；V2 用「**调仓前漂移权重**」⇒ V2 把「漂移后的回补交易」
也算进换手 ⇒ V2 的换手与 V1 baseline（16.57×）**不完全同口径**，对比表里要写明。

🔴 **Rank Buffer 语义**（``buffer_targets``，V1 ``engine/filters.py::apply_theme_cap``
的**等价逻辑**，不 import V1）：
  * 已持有的标的只要排名还在「top_k + buffer」之内 ⇒ **继续持有（不换手）**；
  * 它占住一个槽位 ⇒ 被挤掉的是「已选中、且**不是当前持仓**」里排名最靠后的那个；
  * 持仓数 = ``min(top_k, 可用标的数)``（V1 同口径：**持仓只数不增加**）；
    极端情形（持仓数已超过 top_k）时最多到 ``min(top_k + buffer, 可用标的数)``；
  * 权重**等权** = 1 / 持仓数。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

import config_v2 as C

try:  # 包内导入（`manu.etf_rotation.v2`）
    from . import metrics  # type: ignore[attr-defined]
except ImportError:  # 平铺导入（与 `splits.py` 的 `import config_v2` 同风格）
    import metrics  # type: ignore[no-redef]

__all__ = ["run_backtest_v2", "buffer_targets"]


# ----------------------------------------------------------------- 工具
def _to_timestamp(value: Any) -> pd.Timestamp:
    """把日期（字符串 / date / Timestamp）统一成 ``pd.Timestamp``。"""
    ts = pd.Timestamp(value)
    if ts is pd.NaT:
        raise ValueError(f"🔴 日期非法：{value!r}")
    return ts


def _norm_weights(raw: Mapping[str, Any] | None) -> dict[str, float]:
    """权重字典归一（去非有限、去 ≤0、代码转 str）。"""
    out: dict[str, float] = {}
    for code, weight in (raw or {}).items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(w) or w <= 0.0:
            continue
        out[str(code)] = w
    return out


def _drift(weights: Mapping[str, float], rets: Mapping[str, float], port_ret: float) -> dict[str, float]:
    """权重漂移：``w_c ← w_c × (1+r_c) / (1 + r_port)``（权重和恒为 1）。

    缺价当 0 收益（与 V1 ``_returns_from_weights`` 的 ``fillna(0.0)`` 同口径）⇒ 权重不变。
    """
    if not weights:
        return {}
    gross = 1.0 + float(port_ret)
    if gross <= 0.0:                       # 组合归零（极端行情）⇒ 权重原样保留
        return {c: float(w) for c, w in weights.items()}
    out: dict[str, float] = {}
    for code, w in weights.items():
        r = float(rets.get(code, 0.0))
        out[code] = float(w) * (1.0 + r) / gross
    return out


# ----------------------------------------------------------------- Rank Buffer
def buffer_targets(
    ranks: Mapping[str, int],
    held: list[str],
    top_k: int,
    buffer: int,
) -> dict[str, float]:
    """🔴 **Rank Buffer** 目标权重（等权）—— V1 ``apply_theme_cap`` 的等价逻辑。

    语义（逐条）：
      1. ``ranks`` = ``{代码: 排名(int，越小越好)}``；
      2. ``held`` = 当前持仓代码列表；
      3. 基础选择 = 排名最前的 ``top_k`` 只；
      4. **迟滞（buffer）**：``held`` 里排名 ≤ ``top_k + buffer`` 的标的**继续持有**
         （**不换手**），它顶掉「已选中、且**不是当前持仓**」里排名最靠后的那只；
      5. 返回 ``{代码: 1/持仓数}``（等权）。

    持仓数：``min(top_k, 可用标的数)``（V1 同口径：**持仓只数不增加**）；
    只有当「当前持仓数已超过 top_k」时才会涨到 ``min(top_k + buffer, 可用标的数)``。

    ⚠️ 口径说明：V1 的 ``apply_theme_cap`` 还带「同主题上限 / 冲突对」约束；
       本函数**只做 buffer 语义**（V2 的主题约束在标的池侧另行处理）⇒ 不 import V1。
    """
    top_k = int(top_k)
    buffer = int(buffer)
    if top_k <= 0:
        raise ValueError(f"🔴 top_k 必须 ≥ 1（got {top_k}）")
    if buffer < 0:
        raise ValueError(f"🔴 buffer 必须 ≥ 0（got {buffer}）")

    items = sorted(((int(r), str(c)) for c, r in (ranks or {}).items()),
                   key=lambda kv: (kv[0], kv[1]))
    if not items:
        return {}
    rank_of = {code: rank for rank, code in items}
    band = top_k + buffer
    held_set = {str(c) for c in (held or [])}

    selected: list[str] = [code for _, code in items[:top_k]]
    protected = [code for rank, code in items if rank <= band and code in held_set]
    for code in protected:
        if code in selected:
            continue
        victims = [c for c in selected if c not in held_set]
        if victims:
            victim = max(victims, key=lambda c: rank_of[c])   # 排名最靠后的非持有标的
            selected[selected.index(victim)] = code
        else:
            selected.append(code)                              # 持仓已超 top_k ⇒ 只增不减

    weight = 1.0 / len(selected)
    return {code: weight for code in selected}


# ----------------------------------------------------------------- 回测
def run_backtest_v2(
    prices: pd.DataFrame,
    targets: Mapping[str, Mapping[str, float]],
    *,
    commission: float | None = None,
    slippage: float | None = None,
    start: str | None = None,
    end: str | None = None,
    initial_weights: Mapping[str, float] | None = None,
    tradable: Mapping[str, Mapping[str, bool]] | None = None,
    tradable_sell: Mapping[str, Mapping[str, bool]] | None = None,
    periods_per_year: int = 252,
) -> dict:
    """跑一次 V2 回测（Cost-Aware：佣金 + 滑点 + Rank Buffer 换手全进净值）。

    参数
    ----
    prices : 宽表（index=日期，columns=代码，值=**收盘价**），**至少 2 个交易日**。
    targets : ``{日期字符串: {代码: 目标权重}}``（**已含 Rank Buffer 后的目标权重**，
        由调用方用 ``buffer_targets`` 算好）。
    commission / slippage : 单边佣金 / 滑点（默认 ``config_v2.COMMISSION`` / ``SLIPPAGE``）。
    start / end : 评估区间（含端点）。
    initial_weights : 首个调仓日之前的持仓（默认空仓 ⇒ 首日收益为 0）。
    tradable : ``{日期: {代码: bool}}``（可选）⇒ **不可买入**的代码不换手、保持原仓，
        并记入 ``blocked`` 明细（``reason="target_untradable"``）。
        🔴 语义 = **可买入**：不停牌 且 不涨停（涨停买不进，但**照样能卖**）。
    tradable_sell : ``{日期: {代码: bool}}``（可选）⇒ **不可卖出**的代码不换手、保持原仓，
        并记入 ``blocked`` 明细（``reason="sell_untradable"``）。
        🔴 语义 = **可卖出**：不停牌 且 不跌停（跌停卖不掉，但**照样能买**）。
        ⇒ 不给 ⇒ **退化**为与 ``tradable`` 相同（保持既有调用方行为逐字不变）。
    periods_per_year : 年化期数（默认 252；V1 用 250）。

    返回
    ----
    ``{"nav", "turnover_by_date", "total_turnover", "annualized_turnover",
       "total_cost_drag", "weights_by_date", "blocked", "metrics"}``（+ 审计字段）。

    🔴 时序：目标权重 = **调仓日收盘成交** ⇒ 调仓日的涨跌归上一段持仓，新权重自次日生效。
    """
    # ---- 参数与数据校验 ----
    if not isinstance(prices, pd.DataFrame):
        raise TypeError(f"🔴 prices 必须是宽表 DataFrame（got {type(prices).__name__}）")
    commission = float(C.COMMISSION if commission is None else commission)
    slippage = float(C.SLIPPAGE if slippage is None else slippage)

    px = prices.astype("float64").copy()
    px.index = pd.DatetimeIndex([_to_timestamp(x) for x in px.index])
    if px.index.has_duplicates:
        dup = sorted({str(d) for d in px.index[px.index.duplicated()]})
        raise ValueError(f"🔴 prices 的日期索引有重复：{dup[:5]} ⇒ 无法定位调仓日")
    px = px.sort_index()
    if start is not None:
        px = px.loc[px.index >= _to_timestamp(start)]
    if end is not None:
        px = px.loc[px.index <= _to_timestamp(end)]
    if len(px) < 2:
        raise ValueError(
            f"🔴 prices 至少需要 2 个交易日（当前 {len(px)} 个）⇒ 无法计算漂移与净值"
        )

    dates = list(px.index)
    codes = list(px.columns)

    # ---- 目标权重 / 可交易状态归一 ----
    target_map: dict[pd.Timestamp, dict[str, float]] = {}
    skipped_target_dates: list[str] = []
    for raw_date, raw_target in (targets or {}).items():
        ts = _to_timestamp(raw_date)
        if ts not in px.index:
            skipped_target_dates.append(ts.strftime("%Y-%m-%d"))
            continue                      # 非交易日的目标：忽略（不制造假调仓日）
        target_map[ts] = _norm_weights(raw_target)
    tradable_map: dict[pd.Timestamp, dict[str, bool]] = {}
    for raw_date, raw_map in (tradable or {}).items():
        ts = _to_timestamp(raw_date)
        tradable_map[ts] = {str(c): bool(v) for c, v in (raw_map or {}).items()}
    # 🔴 卖出侧映射：不给 ⇒ 复用买入侧（向后兼容：既有调用方行为逐字不变）
    tradable_sell_map: dict[pd.Timestamp, dict[str, bool]] = {}
    sell_src = tradable_sell if tradable_sell is not None else tradable
    for raw_date, raw_map in (sell_src or {}).items():
        ts = _to_timestamp(raw_date)
        tradable_sell_map[ts] = {str(c): bool(v) for c, v in (raw_map or {}).items()}

    # ---- 状态 ----
    nav = 1.0
    nav_values: list[float] = []
    w_hold = _norm_weights(initial_weights)
    turnover_by_date: dict[str, float] = {}
    weights_by_date: dict[str, dict[str, float]] = {}
    blocked: list[dict] = []
    prev_row: pd.Series | None = None

    for day in dates:
        row = px.loc[day]
        if prev_row is None:
            rets = {c: 0.0 for c in codes}
        else:
            raw = (row / prev_row.replace(0.0, np.nan)) - 1.0
            rets = {c: float(v) if np.isfinite(v) else 0.0
                    for c, v in raw.fillna(0.0).items()}

        # ① 当日组合收益：用**上一时点**的权重（调仓日 ⇒ 该日收益归上一段持仓）
        port_ret = float(sum(w_hold.get(c, 0.0) * rets.get(c, 0.0) for c in codes))
        nav *= (1.0 + port_ret)

        # ② 漂移到当日收盘
        w_close = _drift(w_hold, rets, port_ret)

        # ③ 调仓
        if day in target_map:
            eff, turnover, blocked_rows = _rebalance(
                target_map[day], w_close,
                tradable_map.get(day, {}), tradable_sell_map.get(day, {}),
            )
            cost_rate = (commission + slippage) * turnover
            nav *= (1.0 - cost_rate)
            key = day.strftime("%Y-%m-%d")
            turnover_by_date[key] = float(turnover)
            weights_by_date[key] = dict(eff)
            blocked.extend(blocked_rows)
            w_hold = eff
        else:
            w_hold = w_close

        nav_values.append(float(nav))
        prev_row = row

    nav_series = pd.Series(nav_values, index=pd.DatetimeIndex(dates), name="nav")
    total_turnover = float(sum(turnover_by_date.values()))
    years = (len(nav_series) - 1) / float(periods_per_year) if len(nav_series) >= 2 else 0.0
    turnover_series = pd.Series(turnover_by_date, dtype="float64")
    cost_drag = float(total_turnover * (commission + slippage))
    # 🔴 年化换手（**双边**）；年数算不出来 ⇒ NaN（不是 0.0）
    _ann_turn = float(total_turnover / years) if years > 0 else float("nan")

    return {
        "nav": nav_series,
        "turnover_by_date": turnover_by_date,
        "total_turnover": total_turnover,
        "annualized_turnover": _ann_turn,
        # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径，
        #    唯一实现 `config_v2.one_way_turnover`）。
        #    🔴 **口径一致**：双边算不出来（NaN）⇒ 单边**也是 NaN**（不是 0.0）
        #    ⇒ 同一条记录里不许出现「NaN 与 0.0 混搭」（与 `metrics.summarize` 同口径）。
        "annualized_turnover_one_way": (
            C.one_way_turnover(_ann_turn) if np.isfinite(_ann_turn) else float("nan")
        ),
        "total_cost_drag": cost_drag,
        "weights_by_date": weights_by_date,
        "blocked": blocked,
        "metrics": metrics.summarize(
            nav_series,
            turnover=turnover_series,
            commission=commission,
            slippage=slippage,
            periods_per_year=periods_per_year,
        ),
        # ---- 审计字段 ----
        "commission": commission,
        "slippage": slippage,
        "cost_rate_total": float(sum((commission + slippage) * t
                                     for t in turnover_by_date.values())),
        "n_rebalances": int(len(turnover_by_date)),
        "skipped_target_dates": skipped_target_dates,
        "n_blocked": int(len(blocked)),
        "start": dates[0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "n_days": int(len(dates)),
        "execution_lag": "调仓日收盘成交 ⇒ 当日涨跌归上一段持仓，新权重自**次日**起承担收益",
        "turnover_definition": "Σ_代码 |目标权重 − 调仓前漂移权重|（双边 = 总成交额/组合净值；单边 = 0.5×；含 Rank Buffer 留存量）",
    }


def _rebalance(
    target: Mapping[str, float],
    drifted: Mapping[str, float],
    tradable_map: Mapping[str, bool],
    tradable_sell_map: Mapping[str, bool] | None = None,
) -> tuple[dict[str, float], float, list[dict]]:
    """一次调仓：返回 ``(有效目标权重, 换手, blocked 明细)``。

    规则（逐条）：
      1. 目标里**不可买入**（``tradable_map``）的代码 ⇒ 不买（保持原仓，记 blocked）；
      2. 持仓里**不可卖出**（``tradable_sell_map``，不给则退回 ``tradable_map``）
         且本要卖掉的 ⇒ 不卖（保持原仓，记 blocked）；
      3. 其余可成交目标按比例缩放（预算 = 目标暴露 − 被拒部分，且不许超配/加杠杆）；
      4. 换手 = ``Σ_代码 |有效目标 − 漂移权重|``。

    🔴 **买/卖两侧语义不同**（涨跌停的本质）：
      * 涨停 ⇒ **买不进**（买侧不可交易）但**能卖出**（卖侧可交易）；
      * 跌停 ⇒ **卖不掉**（卖侧不可交易）但**能买入**（买侧可交易）；
      * 停牌 ⇒ 两侧都不可交易。
    """
    keep: dict[str, float] = {}
    blocked_rows: list[dict] = []
    # 🔴 卖出侧：不给 ⇒ 退回买入侧（向后兼容）
    sell_map = tradable_map if tradable_sell_map is None else tradable_sell_map

    for code, weight in target.items():
        if tradable_map.get(code, True):
            continue
        kept = float(drifted.get(code, 0.0))
        keep[code] = kept
        blocked_rows.append({
            "code": code,
            "reason": "target_untradable",
            "requested_weight": float(weight),
            "kept_weight": kept,
        })

    for code, weight in drifted.items():
        if code in target or weight <= 0.0:
            continue
        if sell_map.get(code, True):
            continue
        keep[code] = float(weight)
        blocked_rows.append({
            "code": code,
            "reason": "sell_untradable",
            "requested_weight": 0.0,
            "kept_weight": float(weight),
        })

    tradable_target = {c: w for c, w in target.items() if tradable_map.get(c, True)}
    target_sum = float(sum(target.values()))
    refused_sum = float(sum(w for c, w in target.items() if c not in tradable_target))
    keep_sum = float(sum(keep.values()))
    budget = min(max(target_sum - refused_sum, 0.0), max(1.0 - keep_sum, 0.0))
    tradable_sum = float(sum(tradable_target.values()))
    if tradable_sum > 0.0 and budget > 0.0:
        scale = budget / tradable_sum
        eff = {c: float(w) * scale for c, w in tradable_target.items()}
    else:
        eff = {}
    eff.update(keep)

    turnover = float(sum(abs(eff.get(c, 0.0) - float(drifted.get(c, 0.0)))
                         for c in set(eff) | set(drifted)))
    return eff, turnover, blocked_rows
