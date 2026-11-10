"""前瞻审计（look-ahead audit）：ETF 轮动链的「无未来函数」合规性台账。

【为什么要有这个模块】
任务书 §5 硬性要求「无未来函数」；规则 §十四.12 要求 **W 快照偏差必须显式披露**。
但披露到「偏差不可量化」为止不够（铁律：每条结论必须带数字 + 区间 + 口径），
⇒ 本模块把两件事做成**可复算**的：

1. **输入日期台账**（:func:`audit_input_dates`）：把链路上每个「带日期的输入」列出来，
   逐条判定它**实际被消费的那部分数据**是否越过 asof ⇒ 构成前视。
   🔴 关键口径：**文件里含有未来行 ≠ 前视**。
   判据是「**实际消费日期** > asof」：例如 ``etf_prices.csv`` 最晚 2026-11-04，
   但特征层只取 ``date <= asof`` ⇒ 实际消费日期 = 2026-11-03 ⇒ **不是前视**。
   台账里同时给 ``file_date``（文件最晚日）与 ``effective_date``（实际消费日），
   两者之差就是「未来行被截断了多少」。
2. **W 快照前视的量化**（:func:`w_snapshot_lookahead_bound` + :func:`rank_flip_robustness`）：
   本链唯一**无法修掉**的前视是成分股快照（W）的 ``fetch_date`` 晚于 asof
   （实测快照日 2026-11-04 > asof 2026-11-03）。
   成分股的历史变更**不可观测**（akshare 的两个接口都取不到历史 PCF，本地只有**一个**快照日）
   ⇒ **修不了，只能量化**：给出「单只成分股进出」下 **E 的误差上界**，
   再把它传导到**最终分**与**排名**，回答一个可操作的问题：
   *这个前视到底会不会改名单？*

【上界的两档口径】
- **现实档**（``small_weight_cap``，默认成分权重 ≤ 5%）：一天之内真正会进出成分表的
  通常是小权重成分股 ⇒ 这是**现实上界**。
- **极端档**（全部成分，含最大权重股）：假设最大权重成分股在一天内进出 ⇒ 这是**保守上界**。

两档都算、分别报告，**不许只报好看的那个**。

【口径声明】
- 本模块**只读、零写入**，不参与选股决策，只做审计与披露。
- 所有数字都带**样本数**与**口径**；算不出来一律 ``None`` + ``reason``，
  **绝不补 0**（规则 §十四.5 退化守卫）。
- 「不可修」与「未修」是两回事：这里的前视是**数据不可得**导致的**结构性**前视，
  只能披露 + 定量，不能靠改代码消除。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402

from engine.weights import normalize_scores  # noqa: E402


#: 「小权重成分」的权重上限（%）：用于算**现实上界**
DEFAULT_SMALL_WEIGHT_CAP = 5.0


# --------------------------------------------------------------------------- #
# 1) 输入日期台账
# --------------------------------------------------------------------------- #

def _delta_days(value, asof: pd.Timestamp) -> int | None:
    """两个日期相差几天；日期认不出 ⇒ None（不猜）。"""
    if value is None:
        return None
    try:
        return int((pd.Timestamp(str(value)[:10]) - pd.Timestamp(asof)).days)
    except Exception:
        return None


def _entry(name: str, path, date_field: str, file_date, effective_date, asof: pd.Timestamp,
           role: str, used_for: str, note: str = "", severity_override: str | None = None) -> dict:
    """构造台账里的一条记录。

    参数
    ----
    file_date : 文件里该字段的最大值（**只是文件现状**，不代表被用了）
    effective_date : **实际被消费**的那部分数据的日期；``None`` 表示这部分**根本没被消费**
        （例如被 ``<= asof`` 门控挡掉，或该字段无时间维度）
    role :
        * ``decision``   —— 直接参与打分/选股/下单；
        * ``disclosure`` —— 只用于风险披露与叙述；
        * ``calendar``   —— 日历类输入，**设计上**就在 asof 之后。

    🔴 前视判据 = ``effective_date > asof``（**不是** ``file_date > asof``）：
    文件含未来行但消费时被截断 ⇒ **不是前视**。
    """
    file_date = None if file_date is None else str(file_date)[:10]
    effective_date = None if effective_date is None else str(effective_date)[:10]
    d_file = _delta_days(file_date, asof)
    d_eff = _delta_days(effective_date, asof)
    is_future = bool(d_eff is not None and d_eff > 0)
    look_ahead = bool(role == "decision" and is_future)
    if severity_override is not None:
        severity = severity_override
        look_ahead = False
    elif role == "calendar":
        severity = "none"
    elif look_ahead:
        severity = "blocking"
    elif d_file is not None and d_file > 0:
        severity = "info"
    else:
        severity = "none"
    return {
        "name": name,
        "path": str(path) if path is not None else None,
        "date_field": date_field,
        "file_date": file_date,
        "file_delta_days_vs_asof": d_file,
        "effective_date": effective_date,
        "effective_delta_days_vs_asof": d_eff,
        "truncated_days": (
            None if (d_file is None or d_eff is None) else max(0, d_file - d_eff)
        ),
        "role": role,
        "used_for": used_for,
        "look_ahead": look_ahead,
        "severity": severity,
        "note": note,
    }


def _liquidity_fallback_inertness(prices: pd.DataFrame | None, universe: pd.DataFrame | None,
                                 asof: pd.Timestamp | None = None) -> dict:
    """实测：``etf_universe.csv`` 的 ``avg_amount_20d``（快照日晚 asof）**只作兜底**，
    且**兜底对选股无影响** —— 证明「未来成交额没有进过流动性过滤」。

    逻辑（与 ``engine.filters.apply_liquidity_filter`` 完全一致）：
    ``liq_amount = amount_20d_mean.fillna(avg_amount_20d)``；
    ``amount_20d_mean`` 来自 asof 截断的日线 ⇒ 只有**没有 ≤asof 历史**的 ETF 才会退到兜底，
    而这类 ETF 的 ``n_days`` 必然 < ``MIN_HISTORY_DAYS`` ⇒ 流动性过滤**一定不通过**。

    返回
    ----
    dict
        ``n_need_fallback``（会用到兜底的只数）/ ``n_need_fallback_pass_liq``
        （其中通过流动性过滤的只数，**必须为 0**）/ ``inert``（bool）/ ``note``
    """
    out = {
        "n_need_fallback": None,
        "n_need_fallback_pass_liq": None,
        "inert": None,
        "note": "缺输入 ⇒ 不可判定（不给数字）",
    }
    if prices is None or universe is None or not len(prices) or not len(universe):
        return out
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"])
    cut = pd.Timestamp(asof).normalize() if asof is not None else px["date"].max()
    px_asof = px[px["date"] <= cut]
    if px_asof.empty:
        return out
    amt = px_asof.groupby(px_asof["code"].astype(str).str.zfill(6))["amount"].apply(
        lambda s: s.tail(20).mean() if len(s) else np.nan
    )
    ndays = px_asof.groupby(px_asof["code"].astype(str).str.zfill(6)).size()
    need = amt[amt.isna()].index
    out["n_need_fallback"] = int(len(need))
    n_pass = 0
    for code in need:
        nd = int(ndays.get(code, 0))
        if nd >= int(C.MIN_HISTORY_DAYS):
            n_pass += 1
    out["n_need_fallback_pass_liq"] = n_pass
    out["inert"] = bool(n_pass == 0)
    out["note"] = (
        f"会用到 universe 兜底成交额的 {len(need)} 只 ETF 里，通过流动性过滤的有 {n_pass} 只"
        f"（MIN_HISTORY_DAYS={int(C.MIN_HISTORY_DAYS)}）⇒ 兜底**对选股无影响**"
        if out["inert"] else
        f"⚠️ 有 {n_pass} 只 ETF 用到未来成交额且通过了流动性过滤 ⇒ 兜底**有影响**，须修"
    )
    return out


def audit_input_dates(
    asof: pd.Timestamp,
    *,
    prices: pd.DataFrame | None = None,
    universe: pd.DataFrame | None = None,
    holdings: pd.DataFrame | None = None,
    scores: pd.DataFrame | None = None,
    stock_prices: pd.DataFrame | None = None,
    backtest_window: tuple[str, str] | None = None,
    calendar_max_date=None,
) -> dict:
    """列出链路里每个带日期的输入，判定**实际消费部分**是否构成前视。

    参数
    ----
    asof : 信号日（含）
    prices : ETF 日线（``engine.features.load_prices`` 的输出）
    universe : 候选池（``engine.portfolio.load_universe`` 的输出）
    holdings : ETF 成分持仓（``engine.weights.load_holdings`` 的输出）
    scores : 个股分数（date, instrument, score）
    stock_prices : 个股日线（用于 RankIC 前瞻收益）
    backtest_window : 回测产物的评估区间 ``(start, end)``
    calendar_max_date : 交易日历最后一天

    返回
    ----
    dict
        ``entries`` 台账、``n_inputs``、``n_look_ahead``、``blocking`` 列表、
        ``liquidity_fallback``（兜底惰性实测）、``summary`` 一句话结论。
    """
    asof = pd.Timestamp(asof).normalize()
    entries: list[dict] = []

    # --- ETF 日线：文件含 11-04，但特征层严格截断 ⇒ 实际消费日 = asof ----
    if prices is not None and len(prices):
        p = prices.copy()
        p["date"] = pd.to_datetime(p["date"])
        pmax = p["date"].max()
        eff = p.loc[p["date"] <= asof, "date"].max()
        entries.append(_entry(
            "etf_prices.csv", C.ETF_PRICES_CSV, "date(max)", pmax, eff, asof,
            role="decision", used_for="动量 mom20/60/120、MA60、波动、主题相关矩阵",
            note=("文件最晚 %s 晚于 asof，但 engine.features / compute_corr_matrix 都只取 "
                  "date<=asof ⇒ 实际消费到 %s ⇒ **不是前视**（未来行被截断）"
                  % (pmax.date().isoformat(),
                     eff.date().isoformat() if pd.notna(eff) else "无")),
        ))

    # --- 候选池：静态属性（无时间维度）+ 成交额兜底 + 兜底取价 ----
    if universe is not None and len(universe):
        fetch_date = None
        if "fetch_date" in universe.columns:
            vals = universe["fetch_date"].dropna().astype(str).unique().tolist()
            fetch_date = max(vals) if vals else None
        entries.append(_entry(
            "etf_universe.csv(静态属性)", C.ETF_UNIVERSE_CSV, "fetch_date", fetch_date, None, asof,
            role="decision", used_for="候选池静态属性（名称 / 规模 / 类别 / 家族关键词）",
            note=("静态属性**不随日变** ⇒ 无时间维度 ⇒ **不是前视**；"
                  "文件级 fetch_date 只说明快照日"),
        ))
        fb = _liquidity_fallback_inertness(prices, universe, asof)
        entries.append(_entry(
            "etf_universe.csv(avg_amount_20d 兜底)", C.ETF_UNIVERSE_CSV, "fetch_date",
            fetch_date, fetch_date, asof,
            role="decision", used_for="流动性过滤的**兜底**成交额（优先用 asof 自算值）",
            note=("快照日晚于 asof ⇒ 若被用到即前视；实测：" + str(fb.get("note"))
                  + f"（会用到兜底 {fb.get('n_need_fallback')} 只）"),
            # 🔴 兜底**实测惰性**（用到兜底的 0 只）⇒ 不构成决策前视，只作「未来行被截断/未用」记录
            severity_override=None if fb.get("inert") is not True else "info",
        ))
        last_price_date = None
        eff_lp = None
        if "last_price_date" in universe.columns:
            vals = universe["last_price_date"].dropna().astype(str)
            vals = [v for v in vals.unique().tolist() if v]
            if vals:
                last_price_date = max(vals)
                ok = [v for v in vals if _delta_days(v, asof) is not None and _delta_days(v, asof) <= 0]
                eff_lp = max(ok) if ok else None
        entries.append(_entry(
            "etf_universe.csv(last_price_date)", C.ETF_UNIVERSE_CSV, "last_price_date",
            last_price_date, eff_lp, asof,
            role="decision", used_for="最终计划件的兜底取价",
            note=("final_rebalance_plan 只在 last_price_date<=asof 时才用它 ⇒ 晚于 asof 的那些"
                  "**根本没被取用** ⇒ 不是前视（M-F5 已修）"),
        ))

    # --- 🔴 W 的原料：成分股持仓快照（**唯一真前视**） ---
    if holdings is not None and len(holdings):
        snap = None
        if "fetch_date" in holdings.columns:
            vals = holdings["fetch_date"].dropna().astype(str).unique().tolist()
            snap = max(vals) if vals else None
        entries.append(_entry(
            "etf_holdings.csv(W 成分快照)", C.ETF_HOLDINGS_CSV, "fetch_date", snap, snap, asof,
            role="decision", used_for="W 矩阵 ⇒ E = W × S（个股映射分）",
            note=("🔴 **结构性前视**：本地只有这一个快照日，akshare 的 fund_etf_holdings_tx / "
                  "fund_etf_pcf 都取不到历史 PCF ⇒ asof 当日的真实成分集**不可观测** ⇒ "
                  "**修不了，只能量化披露**（见 w_snapshot_lookahead_bound）"),
        ))

    # --- 个股分数 ---
    if scores is not None and len(scores):
        s = scores.copy()
        s["date"] = pd.to_datetime(s["date"])
        smax = s["date"].max()
        eff = s.loc[s["date"] <= asof, "date"].max()
        entries.append(_entry(
            "stock_scores.csv", C.STOCK_SCORES_CSV, "date(max)", smax, eff, asof,
            role="decision", used_for="S（个股分数）⇒ E = W × S",
            note="个股分数按 asof 截面取；asof 之后的分数不参与",
        ))

    # --- 个股日线（RankIC 前瞻收益的底） ---
    if stock_prices is not None and len(stock_prices):
        s = stock_prices.copy()
        s["date"] = pd.to_datetime(s["date"])
        smax = s["date"].max()
        eff = s.loc[s["date"] <= asof, "date"].max()
        entries.append(_entry(
            "stock_prices.csv(个股日线)", C.ETF_RUN_ROOT / "stock_prices.csv", "date(max)",
            smax, eff, asof,
            role="decision", used_for="RankIC 的前瞻收益（实现日必须 <= asof）",
            note="analyze.py 用 date<=asof 截断后再判定前瞻收益是否**已实现** ⇒ 不是前视",
        ))

    # --- 回测产物：只用于披露 ---
    if backtest_window is not None:
        start, end = backtest_window
        entries.append(_entry(
            "etf_rotation_backtest_*.json", C.BACKTEST_JSON, "eval_window_end", end, end, asof,
            role="disclosure", used_for="年化/超额/MDD/分段稳定性等**披露**数字",
            note=("评估区间 %s ~ %s 比 asof 多 %s 天 ⇒ 属**披露口径**（历史统计），"
                  "**不参与**本期选股与下单 ⇒ 不构成决策前视，但必须披露"
                  % (start, end, (pd.Timestamp(end) - asof).days)),
        ))

    # --- 交易日历：设计上就在未来 ---
    if calendar_max_date is not None:
        entries.append(_entry(
            "trade_calendar.csv", C.ETF_DATA_ROOT / "trade_calendar.csv", "date(max)",
            str(calendar_max_date)[:10], str(calendar_max_date)[:10], asof,
            role="calendar", used_for="推导「下一个自然月的第一个交易日」",
            note="日历必须覆盖未来 ⇒ **设计上**晚于 asof，不构成前视",
        ))

    blocking = [e for e in entries if e["look_ahead"]]
    return {
        "asof": asof.date().isoformat(),
        "n_inputs": len(entries),
        "entries": entries,
        "n_look_ahead": len(blocking),
        "blocking": blocking,
        "liquidity_fallback": _liquidity_fallback_inertness(prices, universe, asof),
        "summary": (
            "共 %d 个带日期输入；**决策前视 %d 项**（%s）"
            % (len(entries), len(blocking),
               "、".join(e["name"] for e in blocking) if blocking else "无")
        ),
    }


# --------------------------------------------------------------------------- #
# 2) W 快照前视的 E 误差上界
# --------------------------------------------------------------------------- #

def w_snapshot_lookahead_bound(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    asof: pd.Timestamp,
    codes: list[str] | None = None,
    small_weight_cap: float = DEFAULT_SMALL_WEIGHT_CAP,
) -> pd.DataFrame:
    """W 快照前视导致的 **E 误差上界**（逐 ETF，两档口径）。

    E 的定义（与 ``engine.weights.map_scores_to_etfs`` 完全一致）::

        E = Σ_{i∈S} w_i·z_i / Σ_{i∈S} w_i

    其中 S = **在个股分数里有分**的成分股集合，w_i = 成分权重（%），z_i = 个股分数 z-score。

    若 asof 当日的真实成分集与快照相差**一只**成分股 j：

    * **移除 j**（快照里有、asof 没有）::

          E' = (W_S·E − w_j·z_j) / (W_S − w_j)
          ΔE = w_j·(z_j − E) / (W_S − w_j)

    * **加入 j**（asof 有、快照里没有）::

          E' = (W_S·E + w_j·z_j) / (W_S + w_j)
          ΔE = w_j·(z_j − E) / (W_S + w_j)

    ⇒ 「移除」口径分母更小 ⇒ 数值更大 ⇒ **同时覆盖两个方向**，是保守上界。

    两档口径：
      * ``delta_e_extreme``：对**全部**成分取最大（含最大权重股）⇒ 极端上界；
      * ``delta_e_small_weight``：只在权重 ≤ ``small_weight_cap`` 的成分里取最大
        ⇒ **现实上界**（一天之内真正进出成分表的一般是小权重股）。

    参数
    ----
    holdings : ``engine.weights.load_holdings`` 的输出
    scores   : 个股分数（date, instrument, score）
    asof     : 信号日
    codes    : 只算这些 ETF（None = 全部有分的 ETF）
    small_weight_cap : 「小权重」阈值（%）

    返回
    ----
    pandas.DataFrame
        每行一只 ETF：code / name / e_score / n_scored / weight_scored_pct /
        max_constituent_weight_pct / delta_e_extreme / delta_e_small_weight /
        bound_delta_e / bound_relative_pct / reason
    """
    asof = pd.Timestamp(asof).normalize()
    sc = scores[pd.to_datetime(scores["date"]) == asof].copy()
    if sc.empty:
        raise ValueError(f"{asof.date()} 没有个股分数，无法算 W 前视上界")
    sc["z"] = normalize_scores(sc)

    h = holdings.copy()
    if codes is not None:
        h = h[h["etf_code"].astype(str).isin({str(c) for c in codes})]
    if h.empty:
        return pd.DataFrame()

    merged = h.merge(sc[["instrument", "z"]], left_on="instrument", right_on="instrument", how="inner")
    total_w = h.groupby(["etf_code", "etf_name"], sort=True)["weight_pct"].sum()

    rows: list[dict] = []
    for (code, name), g in merged.groupby(["etf_code", "etf_name"], sort=True):
        w = g["weight_pct"].to_numpy(dtype=float)
        z = g["z"].to_numpy(dtype=float)
        if len(w) == 0 or not np.isfinite(w).all() or w.sum() <= 0:
            continue
        e = float(np.average(z, weights=w))
        w_sum = float(w.sum())
        denom = w_sum - w
        with np.errstate(divide="ignore", invalid="ignore"):
            delta = np.abs(w * (z - e) / denom)
        delta = np.where(np.isfinite(delta), delta, np.nan)
        extreme = float(np.nanmax(delta)) if np.isfinite(delta).any() else None
        small_mask = w <= float(small_weight_cap)
        small = (float(np.nanmax(delta[small_mask]))
                 if small_mask.any() and np.isfinite(delta[small_mask]).any() else None)
        w_max = float(np.max(w))
        rows.append({
            "code": str(code),
            "name": str(name),
            "e_score": round(e, 6),
            "n_scored": int(len(w)),
            "weight_scored_pct": round(w_sum, 4),
            "weight_total_pct": round(float(total_w.get((code, name), w_sum)), 4),
            "max_constituent_weight_pct": round(w_max, 4),
            "delta_e_extreme": None if extreme is None else round(extreme, 6),
            "delta_e_small_weight": None if small is None else round(small, 6),
            "bound_delta_e": None if extreme is None else round(extreme, 6),
            "bound_relative_pct": (
                None if (extreme is None or abs(e) < 1e-12) else round(abs(extreme / e), 6)
            ),
            "reason": None if extreme is not None else "算不出上界（成分权重或 z 缺失）",
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("bound_delta_e", ascending=False, na_position="last").reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- #
# 3) 上界传导到最终分与排名（会不会改名单？）
# --------------------------------------------------------------------------- #

def rank_flip_robustness(
    pool: pd.DataFrame,
    bounds: pd.DataFrame,
    alpha_weight: float | None = None,
    momentum_weight: float | None = None,
    small_weight_cap: float = DEFAULT_SMALL_WEIGHT_CAP,
) -> dict:
    """把 E 的误差上界传导到**最终分**与**排名**，判断名单会不会变。

    最终分口径（与 ``engine.portfolio.build_portfolio`` 一致）::

        final = w_alpha · z_alpha + w_mom · z_mom
        z_alpha = 截面 z(E)（NaN 用池内均值补齐后再做 z）

    只 perturb 某一只 ETF 的 E（幅度取它的上界）⇒ ``z_alpha`` 变化 ≈ ``ΔE / σ(E)``
    （σ 用**池内** E 的总体标准差，ddof=0，与引擎口径一致）
    ⇒ ``Δfinal = w_alpha · ΔE / σ(E)``。

    **换手判定**（对每一对「已选 s / 落选 n」）：
      挑战者 n 之所以能顶掉 s，最坏情况是 n 涨到上界、s 掉到下界::

          需要 final(n) − final(s) > Δfinal(n) + Δfinal(s)

      ⇒ 定义 **余量 margin = (final(n) − final(s)) − (Δfinal(n) + Δfinal(s))**
      ⇒ ``margin > 0`` ⇒ 这一对**可能翻**；``margin ≤ 0`` ⇒ **翻不了**。
      ⇒ 整体结论取所有配对里 **margin 的最大值**（最危险的那一对），
      **最大 margin ≤ 0 ⇒ 名单稳健**。

    **主题感知**：同主题的配对 = 换**产品**（敞口不变）；不同主题 = 换**主题**（敞口变了）。

    参数
    ----
    pool : 候选池（须含 code / theme_id / alpha_score / final_score / selected）
    bounds : :func:`w_snapshot_lookahead_bound` 的输出
    alpha_weight / momentum_weight : 默认取 config
    small_weight_cap : 与 bounds 保持一致（决定用哪一档上界）

    返回
    ----
    dict
        ``sigma_e`` / ``max_delta_final_realistic`` / ``max_delta_final_extreme`` /
        ``same_theme_gaps`` / ``min_same_theme_gap_*`` / ``cross_theme_gaps`` /
        ``min_cross_theme_gap_*`` / ``theme_composition`` / ``conclusion``。

    🔴 **同主题配对的三种口径**（三者回答的问题不同，披露时**不许混称**）：

    ============================== ========================================= ==========================================
    键                              排序口径                                    回答的问题
    ============================== ========================================= ==========================================
    ``same_theme_closest_by_gap``   ``|final_gap|`` 升序（并列按现实档余量升序）    最终分差最近 ⇒ **最容易翻**
    ``same_theme_tightest_realistic````margin_realistic`` 降序（None 排最后）      现实档余量最大 ⇒ **最难翻（现实档）**
    ``same_theme_tightest_extreme`` ``margin_extreme`` 降序                      极端档余量最大 ⇒ **最难翻（极端档）**
    ``same_theme_challengers``      同 ``same_theme_tightest_extreme``（历史键）  向后兼容，**口径不变**
    ============================== ========================================= ==========================================
    """
    if pool is None or len(pool) == 0:
        return {"conclusion": "候选池为空 ⇒ 无法做排名翻转检验", "n_may_change": None}
    w_alpha = C.ALPHA_SCORE_WEIGHT if alpha_weight is None else float(alpha_weight)
    w_mom = C.MOMENTUM_SCORE_WEIGHT if momentum_weight is None else float(momentum_weight)

    # σ(E)：与引擎一致 —— alpha_score 的 NaN 先用池内均值补齐，再取总体标准差
    alpha = pd.to_numeric(pool.get("alpha_score"), errors="coerce")
    alpha_filled = alpha.fillna(alpha.mean())
    sigma_e = float(alpha_filled.std(ddof=0)) if alpha_filled.notna().sum() > 1 else None
    final = pd.to_numeric(pool.get("final_score"), errors="coerce")
    theme = pool.get("theme_id")

    bmap: dict[str, dict] = {}
    if bounds is not None and len(bounds):
        bmap = {str(r["code"]): r for r in bounds.to_dict("records")}

    def delta_final(code: str, field: str) -> float | None:
        b = bmap.get(str(code))
        if not b or b.get(field) is None or not sigma_e:
            return None
        return float(w_alpha) * float(b[field]) / sigma_e

    # ⚠️ 入选/落选的划分用**整个池**（含无 W 记录的防御性 ETF），不能用「有上界的那部分」
    rows: list[dict] = []
    for idx, r in pool.iterrows():
        code = str(r["code"])
        d_real = delta_final(code, "delta_e_small_weight")
        d_ext = delta_final(code, "delta_e_extreme")
        fv = final.loc[idx]
        rows.append({
            "code": code,
            "name": str(r.get("name", "")),
            "theme": None if theme is None else str(theme.loc[idx]),
            "final_score": None if not np.isfinite(fv) else round(float(fv), 6),
            "selected": bool(r.get("selected", False)),
            "has_w_record": code in bmap,
            "delta_final_realistic": None if d_real is None else round(d_real, 6),
            "delta_final_extreme": None if d_ext is None else round(d_ext, 6),
        })

    sel = [r for r in rows if r["selected"]]
    non = [r for r in rows if not r["selected"]]
    sel_themes = {r["theme"] for r in sel}

    same_theme_gaps: list[dict] = []
    cross_theme_gaps: list[dict] = []
    for s in sel:
        for n in non:
            if s["final_score"] is None or n["final_score"] is None:
                continue
            gap = n["final_score"] - s["final_score"]
            item = {
                "selected": s["code"], "selected_name": s["name"], "selected_theme": s["theme"],
                "challenger": n["code"], "challenger_name": n["name"], "challenger_theme": n["theme"],
                "final_gap": round(gap, 6),
            }
            for tag, dkey in (("realistic", "delta_final_realistic"), ("extreme", "delta_final_extreme")):
                ds = s.get(dkey)
                dn = n.get(dkey)
                thr = None if (ds is None or dn is None) else ds + dn
                item[f"swap_threshold_{tag}"] = None if thr is None else round(thr, 6)
                item[f"margin_{tag}"] = (
                    None if (thr is None or ds is None) else round(gap - thr, 6)
                )
            if n["theme"] == s["theme"]:
                same_theme_gaps.append(item)
            elif n["theme"] not in sel_themes:
                # 挑战者的主题**当前没有持仓** ⇒ 换进来是「换主题」（同时要让位）
                cross_theme_gaps.append(item)

    def _max_margin(rows_: list[dict], tag: str) -> float | None:
        vals = [r[f"margin_{tag}"] for r in rows_ if r.get(f"margin_{tag}") is not None]
        return round(max(vals), 6) if vals else None

    def _tightest(rows_: list[dict]) -> list[dict]:
        """最危险的配对 = margin 最大（>0 才算可能翻）。

        🔴 **口径固定为极端档**（排序键 ``margin_extreme`` 降序）⇒ 回答的问题是
        「**按极端档上界，谁最难翻**」⇒ **不等于**「最终分差最近的那一对」。
        对外暴露为 ``same_theme_challengers``（历史口径，保持不变，向后兼容）。
        🔴 2026-11-10（服务器实跑 20261110_120420 实证）：旧排序键
           ``-(r.get("margin_extreme") or -1e9)`` 用了**真值兜底** ⇒
           ① ``margin_extreme is None`` 的行被当成 −1e9（虽然排在最后，但**不是**真「不可得」）；
           ② **``margin_extreme == 0.0`` 也被当成 −1e9** ⇒ 一个真实的 0 余量配对被踢到最后
           ⇒ 与「``margin_extreme`` 降序」的文档口径**相反** ⇒ 闸门 L17「极端档余量未降序」红。
           ⇒ 修法：**显式**把 ``None``（不可得）排最后，其余按数值降序（0.0 仍是真值）。
        """
        def _key(r: dict) -> tuple:
            me = r.get("margin_extreme")
            if me is None:
                return (1, 0.0)
            return (0, -float(me))

        return sorted(rows_, key=_key)[:5]

    def _closest_by_gap(rows_: list[dict]) -> list[dict]:
        """**最终分差最近**的一批配对（``|final_gap|`` 升序，取前 5）。

        口径：``|final_gap|`` 升序；并列时按 ``margin_realistic`` 升序
        （余量更小 = 更接近翻转 ⇒ 更靠前）。
        回答的问题：「**只看最终分的差距，谁离翻转最近**」⇒ 这是**最容易翻**的一对，
        与「余量最大 = 最难翻」是**两个不同的口径**，不许混称。
        """
        def _key(r: dict) -> tuple:
            gap = r.get("final_gap")
            mag = abs(float(gap)) if gap is not None else float("inf")
            mr = r.get("margin_realistic")
            mr_v = float(mr) if mr is not None else float("inf")
            return (mag, mr_v)

        return sorted(rows_, key=_key)[:5]

    def _tightest_realistic(rows_: list[dict]) -> list[dict]:
        """**现实档余量最大**的一批配对（``margin_realistic`` 降序，取前 5）。

        口径：``margin_realistic`` 降序（余量越大 ⇒ 越难翻）；``margin_realistic``
        为 ``None``（该档上界不可得）的一律排在**最后**。
        回答的问题：「**在现实档上界下，谁最难翻**」⇒ 这是现实档的**最难翻**口径。
        🔴 2026-11-10（服务器实跑 20261110_120420 实证）：旧排序键把 ``None`` 打成
           ``(0, 0.0)``、把真值打成 ``(1, -mr)`` ⇒ **``None`` 反而排在最前**
           ⇒ 与本函数 docstring「None 排最后」**正好相反** ⇒ 本轮报告把
           「现实档余量最大」口径表首行点成**余量不可得（None）**的那一对，
           而真正余量最大的是 −0.079164（512800/515020）⇒ 报告内部自相矛盾
           （正文一处说「不可得」、另一处说「余量 −0.0792」）⇒ 闸门 L17 失败。
           ⇒ 修法：``None`` 显式排**最后**（``(1, 0.0)``），真值按降序（``(0, -mr)``）。
           ⚠️ ``None`` 的行**不许丢**（不许悄悄少报一行）⇒ 只是排到末尾。
        """
        def _key(r: dict) -> tuple:
            mr = r.get("margin_realistic")
            if mr is None:
                return (1, 0.0)
            return (0, -float(mr))

        return sorted(rows_, key=_key)[:5]

    max_same_real = _max_margin(same_theme_gaps, "realistic")
    max_same_ext = _max_margin(same_theme_gaps, "extreme")
    max_cross_real = _max_margin(cross_theme_gaps, "realistic")
    max_cross_ext = _max_margin(cross_theme_gaps, "extreme")

    max_real = max((abs(r["delta_final_realistic"]) for r in rows
                    if r.get("delta_final_realistic") is not None), default=None)
    max_ext = max((abs(r["delta_final_extreme"]) for r in rows
                   if r.get("delta_final_extreme") is not None), default=None)

    composition: dict[str, int] = {}
    for r in sel:
        composition[r["theme"]] = composition.get(r["theme"], 0) + 1

    def _judge(max_margin: float | None, label: str) -> str:
        if max_margin is None:
            return f"{label}：池内**没有可比较的配对** ⇒ 该维度**不可能变**"
        if max_margin > 0:
            return f"{label}：**可能被改**（最危险配对余量 {max_margin:+.4f} > 0）"
        return f"{label}：**稳健（名单不变）**（最危险配对余量 {max_margin:+.4f} ≤ 0）"

    conclusion = "；".join([
        _judge(max_cross_real, "现实档·换主题"),
        _judge(max_cross_ext, "极端档·换主题"),
        _judge(max_same_real, "现实档·同主题换产品"),
        _judge(max_same_ext, "极端档·同主题换产品"),
    ])

    return {
        "sigma_e": None if sigma_e is None else round(sigma_e, 6),
        "alpha_weight": w_alpha,
        "momentum_weight": w_mom,
        "small_weight_cap": float(small_weight_cap),
        "n_pool": int(len(pool)),
        "n_selected": len(sel),
        "n_pool_with_w_record": int(sum(1 for r in rows if r["has_w_record"])),
        "theme_composition": composition,
        "max_delta_final_realistic": None if max_real is None else round(max_real, 6),
        "max_delta_final_extreme": None if max_ext is None else round(max_ext, 6),
        "same_theme_gaps": same_theme_gaps[:10],
        # 🔴 三个口径**互不相同**，披露时必须各自点名、**不许混称**：
        #   same_theme_challengers    —— 极端档余量最大（最难翻·极端档）；历史键，排序口径不变。
        "same_theme_challengers": _tightest(same_theme_gaps),
        #   same_theme_tightest_extreme —— 与 same_theme_challengers **同一口径**（极端档），
        #                                 另起一个自解释的名字，避免披露文案把它叫成「最接近」。
        "same_theme_tightest_extreme": _tightest(same_theme_gaps),
        #   same_theme_closest_by_gap  —— |final_gap| 最小（最终分差最近 ⇒ 最容易翻）。
        "same_theme_closest_by_gap": _closest_by_gap(same_theme_gaps),
        #   same_theme_tightest_realistic —— 现实档余量最大（最难翻·现实档）。
        "same_theme_tightest_realistic": _tightest_realistic(same_theme_gaps),
        "cross_theme_gaps": cross_theme_gaps[:10],
        "max_same_theme_margin_realistic": max_same_real,
        "max_same_theme_margin_extreme": max_same_ext,
        "max_cross_theme_margin_realistic": max_cross_real,
        "max_cross_theme_margin_extreme": max_cross_ext,
        "conclusion": conclusion,
    }
