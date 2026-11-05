"""ETF 微观结构实测：折溢价、流动性分层、跟踪误差代理。

【为什么要有它】
`analyze.py` 原先的风险披露里，「ETF 自身风险：跟踪误差、折溢价与流动性分层未建模」
只写了**结论**（"未建模"）**没写数字** ⇒ 只读报告的人无法判断"未建模"到底差多少。
本模块把三类量**实测出来**，让风险披露能写成「数字 + 区间 + 口径」。

【口径铁律】
- **无未来函数**：所有**时间序列**统计一律裁到 `asof`（含）为止；asof 之后的 bar 不用。
- **算不出来就写"不可得 + 原因"，绝不补 0**（`None` + `reason` 字段）。
- **点时刻快照**（`etf_spot.csv` 的折溢价）**只有一个日期**，不随 asof 变 ⇒
  输出里必须带 `spot_snapshot_date`，并由调用方披露它与 asof 的**天差**（这是已知口径差，
  不是本模块引入的）。
- **跟踪误差是"代理"不是真 TE**：真 TE 需要指数净值序列，本数据集没有 ⇒
  用「ETF 日收益 − 静态成分权重加权日收益」的年化标准差做代理，并**显式标注**
  「权重快照回溯、不含再平衡 ⇒ 不可与券商公布 TE 直接比较」。

【产物结构】
`measure(...)` 返回一个**纯 dict**（可直接 JSON 序列化），字段见各函数 docstring。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

#: 年化用的交易日数（A股惯例 252）
TRADING_DAYS_PER_YEAR = 252

#: 计算年化波动 / 跟踪误差的最少收益样本数（少于它 ⇒ 不可得，不报数）
MIN_RETURNS = 30

#: 计算 20 日均成交额的最少 bar 数
MIN_AMOUNT_BARS = 10

#: 成分股加权组合的**每日权重覆盖率**下限（低于它 ⇒ 该日剔除，避免用残缺组合当参照）
MIN_WEIGHT_COVERAGE = 0.95


def _stats(values: list[float] | np.ndarray) -> dict | None:
    """一组数字的分布统计（n / min / median / max / mean_abs）。

    空集合 ⇒ 返回 ``None``（**不返回 0**，避免"没算出来"被误读成"等于 0"）。
    """
    arr = np.asarray([v for v in np.asarray(values, dtype=float).ravel()
                      if np.isfinite(v)], dtype=float)
    if arr.size == 0:
        return None
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "median": float(np.median(arr)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
        "mean_abs": float(np.abs(arr).mean()),
    }


def _pct(value: float | None, digits: int = 4) -> str | None:
    """把小数比例格式化成百分数字符串（``None`` ⇒ ``None``，不补 0）。"""
    if value is None or not np.isfinite(value):
        return None
    return f"{value * 100:.{digits}f}"


def load_spot(path: Path | str) -> pd.DataFrame | None:
    """读 ``etf_spot.csv``（**中文表头**、UTF-8 BOM）。

    读不到 / 列缺失 ⇒ 返回 ``None``（调用方据此把该段标成"不可得"，不编数字）。
    """
    path = Path(path)
    if not path.exists():
        return None
    df = pd.read_csv(path, dtype={"代码": str}, encoding="utf-8-sig")
    if "代码" not in df.columns:
        return None
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    for col in ("基金折价率", "最新价", "IOPV实时估值", "成交额", "换手率"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _etf_returns(prices: pd.DataFrame, asof: pd.Timestamp, lookback: int) -> dict[str, pd.Series]:
    """{code: 该 ETF 在 asof(含) 之前的日收益序列}。

    🔴 **只用 ``date <= asof``**（无未来函数）；收益 = 收盘价一阶差分。
    """
    if prices is None or prices.empty:
        return {}
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    df = df[df["date"] <= pd.Timestamp(asof)].sort_values(["code", "date"])
    out: dict[str, pd.Series] = {}
    for code, grp in df.groupby("code", sort=False):
        close = grp["close"].to_numpy(dtype=float)
        if close.size < 2:
            continue
        tail = close[-(lookback + 1):]
        idx = grp["date"].to_numpy()[-tail.size:]
        ret = pd.Series(np.diff(tail) / tail[:-1], index=pd.to_datetime(idx[1:]), name=code)
        out[code] = ret[np.isfinite(ret.to_numpy())]
    return out


def _avg_amount_20d(prices: pd.DataFrame, asof: pd.Timestamp) -> dict[str, float]:
    """{code: 截至 asof 的最近 20 个交易日**均成交额**（元）。

    🔴 自己算而**不用** ``etf_universe.csv`` 的 ``avg_amount_20d``：后者的快照日
    （2026-11-04）比 asof（2026-11-03）晚 1 天 ⇒ 用它会把 T+1 的信息带进 T 日的报告。
    """
    if prices is None or prices.empty:
        return {}
    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    if "amount" not in df.columns:
        return {}
    df = df[df["date"] <= pd.Timestamp(asof)].sort_values(["code", "date"])
    out: dict[str, float] = {}
    for code, grp in df.groupby("code", sort=False):
        amt = grp["amount"].to_numpy(dtype=float)
        tail = amt[-20:]
        if tail.size < MIN_AMOUNT_BARS:
            continue
        if not np.isfinite(tail).any():
            continue
        out[code] = float(np.nanmean(tail))
    return out


def _premium_stats(
    spot: pd.DataFrame | None,
    codes: list[str],
    money_market: set[str],
) -> dict:
    """折溢价率统计（单位 %）。

    - ``基金折价率`` 是**折价率**：正=折价、负=溢价（已用 ``最新价/IOPV-1`` 交叉验证）。
    - 货币 ETF 的价格锚在 100 元面值 ⇒ 其折溢价**无经济含义** ⇒ 单独分开统计。
    """
    if spot is None:
        return {
            "available": False,
            "reason": "etf_spot.csv 不可得（或无「代码」列）⇒ 折溢价率无法实测",
        }
    sub = spot[spot["代码"].isin(codes)]
    n_missing = len(codes) - len(sub)
    out: dict = {
        "available": True,
        "source": "etf_spot.csv 的「基金折价率」（正=折价、负=溢价）",
        "n_candidates": len(codes),
        "n_in_spot": int(len(sub)),
        "n_not_in_spot": int(n_missing),
        "codes_not_in_spot": sorted(set(codes) - set(sub["代码"])),
    }
    if "数据日期" in sub.columns:
        dates = sorted({str(d) for d in sub["数据日期"].dropna().unique()})
        out["spot_snapshot_date"] = dates[0] if len(dates) == 1 else dates
    field = sub["基金折价率"].to_numpy(dtype=float)
    equity_mask = np.array([c not in money_market for c in sub["代码"]])
    out["all"] = _stats(field)
    out["equity_only"] = _stats(field[equity_mask]) if equity_mask.any() else None
    out["money_market_only"] = _stats(field[~equity_mask]) if (~equity_mask).any() else None
    out["money_market_codes"] = sorted(money_market & set(codes))
    out["money_market_note"] = (
        "货币 ETF 价格锚在 100 元面值 ⇒ 其折溢价率无经济含义，只列不解读"
    )
    # 交叉验证：字段「折价率」应 ≈ −(最新价/IOPV−1)×100
    dev: list[float] = []
    deriv: list[float] = []
    for _, row in sub.iterrows():
        price = row.get("最新价")
        iopv = row.get("IOPV实时估值")
        fld = row.get("基金折价率")
        if not all(v is not None and np.isfinite(v) for v in (price, iopv, fld)):
            continue
        if iopv <= 0:
            continue
        d = (float(price) / float(iopv) - 1.0) * 100.0
        deriv.append(d)
        dev.append(abs(float(fld) + d))
    out["derived_premium_pct"] = _stats(deriv)
    out["n_derivable"] = len(deriv)
    out["n_missing_iopv"] = int(len(sub) - len(deriv))
    out["cross_check_max_abs_dev_pp"] = float(max(dev)) if dev else None
    out["cross_check_note"] = (
        "字段「基金折价率」与自算 (最新价/IOPV−1)×100 的最大绝对偏差（pp）"
        "⇒ 偏差小 ⇒ 字段口径可信"
    )
    return out


def _tracking_error_proxy(
    codes: list[str],
    holdings: pd.DataFrame,
    etf_rets: dict[str, pd.Series],
    stock_prices: pd.DataFrame | None,
    asof: pd.Timestamp,
    lookback: int,
) -> dict:
    """跟踪误差**代理**：年化 std(ETF 日收益 − 静态成分加权日收益)。

    - 真 TE 需要指数净值序列 ⇒ 本数据集**没有** ⇒ 这里只是**代理**。
    - 成分权重取 `etf_holdings.csv` 的**静态快照**（含前视）⇒ 输出里必须标注。
    - 货币 ETF 在 holdings 里**无成分记录** ⇒ 不可得（写明原因，不补 0）。
    """
    if stock_prices is None or stock_prices.empty:
        return {
            "available": False,
            "reason": "个股日线（stock_prices.csv）不可得 ⇒ 跟踪误差代理无法计算",
        }
    if holdings is None or holdings.empty:
        return {"available": False, "reason": "etf_holdings.csv 为空 ⇒ 跟踪误差代理无法计算"}

    sp = stock_prices.copy()
    sp["date"] = pd.to_datetime(sp["date"])
    sp["instrument"] = sp["instrument"].astype(str)
    sp = sp[sp["date"] <= pd.Timestamp(asof)]
    if sp.empty:
        return {"available": False, "reason": f"个股日线在 {pd.Timestamp(asof).date()} 之前无数据"}

    # 只保留候选 ETF 的成分股 ⇒ 大幅裁剪 390 万行
    need = set()
    per_etf: dict[str, pd.DataFrame] = {}
    for code in codes:
        h = holdings[holdings["etf_code"].astype(str).str.zfill(6) == code]
        if h.empty:
            continue
        h = h[["instrument", "weight_pct"]].copy()
        h["weight_pct"] = pd.to_numeric(h["weight_pct"], errors="coerce")
        h = h[np.isfinite(h["weight_pct"].to_numpy(dtype=float)) & (h["weight_pct"] > 0)]
        if h.empty:
            continue
        per_etf[code] = h
        need.update(h["instrument"].tolist())
    if not need:
        return {
            "available": False,
            "reason": "候选 ETF 在 etf_holdings.csv 里没有任何正权重成分 ⇒ 跟踪误差代理不可算",
        }

    sp = sp[sp["instrument"].isin(need)]
    if sp.empty:
        return {"available": False, "reason": "候选 ETF 的成分股在个股日线里没有行情"}
    wide = sp.pivot_table(index="date", columns="instrument", values="close", aggfunc="last")
    wide = wide.sort_index()
    # 🔴 必须用 ``fill_method=None``：默认的 ``pad`` 会把**缺价日**前向填充成 0% 收益，
    #    缺价日因此伪装成"有行情" ⇒ 下面的每日权重覆盖率被抬高 ⇒ 用**假收益**当参照。
    #    不填充 ⇒ 缺价日收益为 NaN ⇒ ``notna()`` 正确反映真实覆盖率。
    rets = wide.pct_change(fill_method=None)
    # 只保留 asof 之前最近 lookback+1 个交易日
    rets = rets.iloc[-(lookback + 1):]

    te_vals: list[float] = []
    corr_vals: list[float] = []
    detail: dict[str, dict] = {}
    not_computable: dict[str, str] = {}
    for code, h in per_etf.items():
        if code not in etf_rets:
            not_computable[code] = "该 ETF 在 etf_prices.csv 里没有 asof 之前的行情"
            continue
        cols = [c for c in h["instrument"] if c in rets.columns]
        if not cols:
            not_computable[code] = "成分股在个股日线里没有行情"
            continue
        w = h.set_index("instrument")["weight_pct"].reindex(cols).to_numpy(dtype=float)
        total_w = float(w.sum())
        if total_w <= 0:
            not_computable[code] = "成分权重全为 0"
            continue
        sub = rets[cols]
        # 每日权重覆盖率（缺失成分股的权重占比）
        cov = sub.notna().mul(w, axis=1).sum(axis=1) / total_w
        ok = cov >= MIN_WEIGHT_COVERAGE
        if int(ok.sum()) < MIN_RETURNS:
            not_computable[code] = (
                f"成分权重覆盖率 ≥{MIN_WEIGHT_COVERAGE:.0%} 的交易日只有 {int(ok.sum())} 天"
                f"（< {MIN_RETURNS}）⇒ 不可得"
            )
            continue
        port = sub.mul(w, axis=1).sum(axis=1, min_count=1) / total_w
        joined = pd.concat(
            [etf_rets[code].rename("etf"), port.rename("port")], axis=1, join="inner"
        ).dropna()
        joined = joined.loc[joined.index <= pd.Timestamp(asof)]
        if len(joined) < MIN_RETURNS:
            not_computable[code] = (
                f"ETF 与成分组合的重叠交易日只有 {len(joined)} 天（< {MIN_RETURNS}）⇒ 不可得"
            )
            continue
        diff = (joined["etf"] - joined["port"]).to_numpy(dtype=float)
        te = float(np.std(diff, ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)
        corr = float(np.corrcoef(joined["etf"].to_numpy(), joined["port"].to_numpy())[0, 1])
        te_vals.append(te)
        corr_vals.append(corr)
        detail[code] = {
            "te_annualized_pct": te,
            "corr": corr,
            "n_days": int(len(joined)),
            "window_start": joined.index.min().date().isoformat(),
            "window_end": joined.index.max().date().isoformat(),
            "n_constituents": int(len(cols)),
            "min_daily_weight_coverage": float(cov.loc[joined.index].min()),
        }
    for code in codes:
        if code not in per_etf:
            not_computable[code] = "etf_holdings.csv 里没有该 ETF 的成分记录（如货币 ETF）⇒ 不可得"
    return {
        "available": bool(te_vals),
        "reason": None if te_vals else "所有候选 ETF 的跟踪误差代理都不可得",
        "definition": (
            "年化 std(ETF 日收益 − 静态成分权重加权日收益) × √252；"
            "成分权重 = etf_holdings.csv 快照（静态、不含再平衡）"
        ),
        "caveat": (
            "🔴 这是**代理**不是真跟踪误差：真 TE 需指数净值序列（本数据集没有）；"
            "权重快照回溯 ⇒ 含前视偏差 ⇒ 不可与券商公布 TE 直接比较"
        ),
        "min_weight_coverage_rule": MIN_WEIGHT_COVERAGE,
        "n_candidates": len(codes),
        "n_computable": len(te_vals),
        "n_not_computable": len(not_computable),
        "not_computable_reason": not_computable,
        "te_annualized_pct": _stats(te_vals),
        "daily_return_correlation": _stats(corr_vals),
        "per_etf": detail,
    }


def measure(
    codes: list[str],
    *,
    prices: pd.DataFrame,
    universe: pd.DataFrame,
    holdings: pd.DataFrame,
    spot: pd.DataFrame | None,
    stock_prices: pd.DataFrame | None,
    asof: pd.Timestamp,
    lookback: int = 60,
    liquidity_threshold: float | None = None,
) -> dict:
    """把候选 ETF 的**折溢价 / 流动性 / 波动 / 跟踪误差代理**一次性测出来。

    参数
    ----
    codes : 待测的候选 ETF 代码列表
    prices : ``etf_prices.csv``（date, code, close, volume, amount）
    universe : ``etf_universe.csv``（用于取 ``fund_type_bucket`` 识别货币 ETF）
    holdings : ``etf_holdings.csv``（成分权重）
    spot : ``etf_spot.csv``（折溢价 / 单日成交额 / 换手率；可为 ``None``）
    stock_prices : 个股日线（date, instrument, close）；可为 ``None``（则 TE 不可得）
    asof : 信号日（**所有时间序列统计裁到这里**）
    lookback : 波动 / TE 的回看交易日数（默认 60）
    liquidity_threshold : 流动性过滤阈值（``config.MIN_AVG_AMOUNT_20D``）⇒ 只用于对照

    返回
    ----
    dict：``{"sample": ..., "premium_discount": ..., "liquidity": ...,
    "etf_volatility": ..., "tracking_error_proxy": ..., "notes": [...]}``
    """
    codes = [str(c).zfill(6) for c in codes]
    asof = pd.Timestamp(asof)
    money_market: set[str] = set()
    if universe is not None and not universe.empty and "fund_type_bucket" in universe.columns:
        um = universe.copy()
        um["code"] = um["code"].astype(str).str.zfill(6)
        money_market = set(
            um.loc[um["fund_type_bucket"].astype(str).str.strip() == "money_market", "code"]
        )

    rets = _etf_returns(prices, asof, lookback)
    amounts = _avg_amount_20d(prices, asof)
    price_dates = []
    if prices is not None and not prices.empty:
        pd_dates = pd.to_datetime(prices["date"])
        pd_dates = pd_dates[pd_dates <= asof]
        if len(pd_dates):
            price_dates = [pd_dates.min().date().isoformat(), pd_dates.max().date().isoformat()]

    vol_vals: list[float] = []
    vol_detail: dict[str, dict] = {}
    for code in codes:
        r = rets.get(code)
        if r is None or len(r) < MIN_RETURNS:
            continue
        v = float(np.std(r.to_numpy(dtype=float), ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)
        vol_vals.append(v)
        vol_detail[code] = {
            "annualized_vol_pct": v,
            "n_returns": int(len(r)),
            "window_start": r.index.min().date().isoformat(),
            "window_end": r.index.max().date().isoformat(),
        }
    equity_codes = [c for c in codes if c not in money_market]
    equity_vol = [
        vol_detail[c]["annualized_vol_pct"] for c in equity_codes if c in vol_detail
    ]

    amt_vals = [amounts[c] for c in codes if c in amounts]
    turnover_vals: list[float] = []
    if spot is not None and not spot.empty and "换手率" in spot.columns:
        sub = spot[spot["代码"].isin(codes)]
        turnover_vals = [
            float(v) for v in sub["换手率"].to_numpy(dtype=float) if np.isfinite(v)
        ]

    te = _tracking_error_proxy(codes, holdings, rets, stock_prices, asof, lookback)
    prem = _premium_stats(spot, codes, money_market)

    amt_stats = _stats(amt_vals)
    liq: dict = {
        "avg_amount_20d_selfcalc": amt_stats,
        "avg_amount_20d_source": (
            "etf_prices.csv 的 amount 字段，按 asof(含) 往前 20 个交易日自算"
            "（不用 etf_universe.csv 的 avg_amount_20d ⇒ 后者快照日晚 asof 1 天）"
        ),
        "avg_amount_20d_max_over_min": (
            float(amt_stats["max"] / amt_stats["min"])
            if amt_stats and amt_stats.get("min", 0) > 0 else None
        ),
        "turnover_rate_pct": _stats(turnover_vals),
        "filter_threshold_avg_amount_20d": liquidity_threshold,
        "note": (
            "流动性过滤阈值只卡在**日均成交额下限** ⇒ 池内仍可能存在数量级的分层；"
            "本线**未按流动性做权重分层，也未建模冲击成本**"
        ),
    }

    return {
        "sample": {
            "n_candidates": len(codes),
            "codes": list(codes),
            "asof": asof.date().isoformat(),
            "etf_price_window": price_dates,
            "lookback_trading_days": lookback,
            "n_money_market": len(money_market & set(codes)),
            "money_market_codes": sorted(money_market & set(codes)),
        },
        "premium_discount": prem,
        "liquidity": liq,
        "etf_volatility": {
            "definition": f"日收益标准差 × √{TRADING_DAYS_PER_YEAR}（最近 {lookback} 个交易日）",
            "all": _stats(vol_vals),
            "equity_only": _stats(equity_vol),
            "equity_only_note": (
                "货币 ETF 面值几乎不变 ⇒ 其波动率不可与权益类混在同一分布"
            ),
            "per_etf": vol_detail,
        },
        "tracking_error_proxy": te,
        "notes": [
            "所有时间序列统计一律裁到 asof（含）为止 ⇒ 无未来函数",
            "折溢价率只有一个点时刻快照（etf_spot.csv）⇒ 快照日与 asof 的天差由调用方披露",
            "跟踪误差是代理（无指数净值序列）⇒ 权重快照回溯，含前视偏差",
            "算不出来的量一律 None + reason，绝不补 0",
        ],
    }
