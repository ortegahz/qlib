"""组合决策层：把「个股映射分 E」+「ETF 自身动量」合成最终排序，并给出目标权重。

【决策链（doc1 + doc2 合流）】
    个股分数 S（全市场, qlib）
      → E = W × S（ETF 的个股 alpha 暴露分）
      → + ETF 自身动量分（20/60/120 日 + 趋势调整 + 相对强弱）
      → 三道过滤网（流动性 / 同质化去重 / 主题多样性）
      → 双重动量防守（绝对动量为负 ⇒ 切黄金/国债）
      → Top 3~5 + 目标权重 → **人工执行清单**

【双重动量（doc1）】
- **绝对动量**：权益 ETF 的 120 日收益必须 > 0（`ABSOLUTE_MOMENTUM_THRESHOLD`）才可入选；
- **相对动量**：在通过的池子里按最终分排序；
- **防守开关**：若**没有任何**权益 ETF 通过绝对动量 ⇒ 组合切到黄金 ETF / 国债 ETF。

【权重】
默认**等权**（3~5 只 ⇒ 20%~33%/只），单只上限 40%（防集中）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402
from engine.filters import (  # noqa: E402
    apply_liquidity_filter,
    apply_theme_cap,
    build_theme_and_conflicts,
)
from engine.features import (  # noqa: E402
    add_relative_strength,
    compute_etf_features,
    momentum_score,
)
from engine.weights import map_scores_to_etfs  # noqa: E402


def _z(series: pd.Series) -> pd.Series:
    """截面 z-score；非有限值填 0（中性）。"""
    s = series.astype(float)
    mu, sd = s.mean(), s.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s - mu) / sd


def load_universe(path: Path | str = None) -> pd.DataFrame:
    """读候选池（etf_universe.csv）。"""
    path = Path(path or C.ETF_UNIVERSE_CSV)
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    for col in ("avg_amount_20d", "float_mcap", "last_price"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "name" not in df.columns:
        df["name"] = df["code"]
    return df


def _is_defensive(name: str, code: str, bucket: str | None = None) -> bool:
    """判定是否「防守资产」（可绕过绝对动量过滤）。

    🔴 **按 fund_type_bucket 判，不按名字**：
    - ``money_market``（货币/现金替代）⇒ 防守；
    - ``commodity`` 且名字是**现货金**（黄金ETF / 金ETF / 上海金）⇒ 防守；
      但商品**期货** ETF（有色/豆粕/能源化工…）是风险资产 ⇒ **不防守**；
    - ``equity_cn``（含「黄金**股**ETF」）⇒ **永远不是**防守资产（那是权益）；
    - ``cross_border``（纳指/标普…）⇒ 风险资产。
    """
    code = str(code)
    name = str(name)
    if code in C.DEFENSIVE_CODES:
        return True
    bucket = (bucket or "").strip()
    if bucket in C.DEFENSIVE_BUCKETS:
        return True
    if bucket == "commodity":
        if any(k in name for k in C.COMMODITY_FUTURES_KEYWORDS):
            return False
        return any(k in name for k in C.SPOT_GOLD_KEYWORDS)
    return False


def _ma_ok_map(prices: pd.DataFrame, asof: pd.Timestamp, window: int) -> dict[str, bool]:
    """{code: 收盘价是否站上 N 日均线}（只用 asof 及之前的数据）。

    🔴 `engine/features.py` 里只有 MA60（趋势过滤用），破位过滤要 MA20 ⇒ 这里单独算。
    历史不足 window+1 根 bar ⇒ 记 False（**不放行**，宁可少选不可选错）。
    """
    if not window or window <= 0:
        return {}
    df = prices[prices["date"] <= pd.Timestamp(asof)].sort_values(["code", "date"])
    out: dict[str, bool] = {}
    for code, g in df.groupby("code", sort=False):
        close = g["close"].to_numpy(dtype=float)
        if len(close) < window + 1:
            out[str(code)] = False
            continue
        ma = float(np.mean(close[-window:]))
        out[str(code)] = bool(np.isfinite(ma) and close[-1] > ma)
    return out


def build_portfolio(
    asof: pd.Timestamp,
    prices: pd.DataFrame,
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    universe: pd.DataFrame,
    current_holdings: dict[str, float] | None = None,
    alpha_weight: float | None = None,
    momentum_weight: float | None = None,
    hold_buffer: int | None = None,
    pool_mode: str | None = None,
    nav_state: dict | None = None,
    entry_ma_window: int | None = None,
    exit_ma_window: int | None = None,
    prefer_held_in_theme: bool | None = None,
) -> dict:
    """算出 asof 截面的持仓建议。返回 dict（含明细、打分、过滤统计、交易指令）。

    v2 新增参数
    ----------
    pool_mode : {"e_covered", "all"}
        标的池属性口径。``"e_covered"``（默认）= **只允许有 A 股成分且覆盖率达标**的 ETF
        入池 ⇒ **每只持仓都真的过了 E=W×S 穿透**（v1 里商品/跨境 ETF coverage=0，
        等于绕过了整个个股建模机制）。
    nav_state : dict | None
        组合级风控刹车状态：``{"nav": float, "peak": float, "in_brake": bool,
        "cooldown_left": int}``。``in_brake=True`` ⇒ **整个组合切防守资产**。
    """
    asof = pd.Timestamp(asof)
    current_holdings = current_holdings or {}

    feats_all = compute_etf_features(prices, asof)
    merged = apply_liquidity_filter(universe, feats_all)
    stats = {
        "n_universe": int(len(merged)),
        "n_pass_liquidity": int(merged["pass_liquidity"].sum()),
        "filter_reject": merged.loc[~merged["pass_liquidity"], "filter_reason"].value_counts().to_dict(),
    }
    cand = merged[merged["pass_liquidity"]].copy()
    if cand.empty:
        raise ValueError(f"{asof.date()} 无 ETF 通过流动性过滤")

    cand = add_relative_strength(cand)
    cand = momentum_score(cand)

    # ---- 个股映射分 E = W × S ----
    alpha = map_scores_to_etfs(holdings, scores, asof)
    alpha_cols = ["etf_code", "alpha_score", "coverage", "coverage_ok", "n_stocks_scored"]
    cand = cand.merge(alpha[alpha_cols].rename(columns={"etf_code": "code"}), on="code", how="left")
    cand["alpha_score"] = cand["alpha_score"].astype(float)
    # 🔴 没有 A 股成分的 ETF（美股 / 港股通 / 黄金）⇒ E = W×S **算不出来**，
    #    coverage=0 ⇒ alpha_score 留空 ⇒ z_alpha 取截面均值（=0，中性），
    #    它们只能靠**自身动量**入选。coverage_ok 明确标出来，便于披露。
    cand["coverage_ok"] = cand["coverage_ok"].eq(True)   # NaN → False，不触发 pandas 降级告警
    bucket_col = "fund_type_bucket" if "fund_type_bucket" in cand.columns else None
    buckets = cand[bucket_col].astype(str) if bucket_col else pd.Series([""] * len(cand), index=cand.index)
    cand["is_defensive"] = [
        _is_defensive(n, c, b)
        for n, c, b in zip(cand["name"].astype(str), cand["code"].astype(str), buckets)
    ]

    # ---- v2 · 标的池属性规范化：只留「能穿透」的品种（防守资产永远保留）----
    mode = pool_mode or C.POOL_MODE
    n_before_pool = int(len(cand))
    if mode == "e_covered":
        keep_mask = cand["coverage_ok"].eq(True) | cand["is_defensive"].eq(True)
        cand = cand[keep_mask].copy()
    cand = cand.reset_index(drop=True)
    stats["pool_mode"] = mode
    stats["n_pool_after_attr_filter"] = int(len(cand))
    stats["n_dropped_by_attr_filter"] = int(n_before_pool - len(cand))

    # ---- v2 · 破位过滤（入场硬过滤 + 持仓强制退出）----
    _entry_win = C.ENTRY_MA_FILTER_WINDOW if entry_ma_window is None else int(entry_ma_window)
    _exit_win = C.EXIT_MA_FILTER_WINDOW if exit_ma_window is None else int(exit_ma_window)
    entry_ma = _ma_ok_map(prices, asof, _entry_win)
    cand["entry_ma_ok"] = [
        (bool(entry_ma.get(str(c), False)) if entry_ma else True) or bool(d)
        for c, d in zip(cand["code"], cand["is_defensive"])
    ]
    stats["n_entry_ma_reject"] = int((~cand["entry_ma_ok"]).sum())
    # 持仓强制退出：已持仓且跌破 EXIT_MA_FILTER_WINDOW 均线 ⇒ 本期无条件卖出（不看排名）
    exit_codes: set[str] = set()
    if _exit_win and current_holdings:
        exit_ma = _ma_ok_map(prices, asof, _exit_win)
        exit_codes = {str(c) for c in current_holdings if not exit_ma.get(str(c), True)}
    stats["forced_exit_codes"] = sorted(exit_codes)

    # ---- v2 · 组合级回撤刹车：净值自峰值回撤超阈值 ⇒ 整个组合切防守资产 ----
    brake_mode = bool(nav_state and nav_state.get("in_brake"))
    if brake_mode:
        cand = cand[cand["is_defensive"]].copy().reset_index(drop=True)
        stats["brake_mode"] = True
        stats["brake_nav"] = float(nav_state.get("nav", float("nan")))
        stats["brake_peak"] = float(nav_state.get("peak", float("nan")))
        stats["brake_cooldown_left"] = int(nav_state.get("cooldown_left", 0))

    # ---- 绝对动量过滤（只对权益类；防守资产不受此限）----
    equity = cand[~cand["is_defensive"]]
    abs_ok = equity[equity["mom120"] > C.ABSOLUTE_MOMENTUM_THRESHOLD]
    relaxed = False
    if abs_ok.empty:
        pool = cand.copy()          # 没有权益 ETF 通过绝对动量 ⇒ 走防守
        relaxed = True
    elif len(abs_ok) < C.MIN_N_HOLDINGS:
        pool = cand.copy()          # 通过的太少 ⇒ 放宽（记录 relaxed）
        relaxed = True
    else:
        pool = pd.concat([abs_ok, cand[cand["is_defensive"]]], ignore_index=True)

    # ---- v2 · 入场破位过滤（新开仓不许买破位的；已持有的允许留到 EXIT 规则处理）----
    held = set(str(c) for c in current_holdings)
    if not brake_mode:
        block = (~pool["entry_ma_ok"]) & (~pool["code"].astype(str).isin(held))
        pool = pool[~block].copy().reset_index(drop=True)
    # ---- v2 · 持仓强制退出（跌破 EXIT 均线 ⇒ 本期无条件卖出，不看排名）----
    if exit_codes:
        pool = pool[~pool["code"].astype(str).isin(exit_codes)].copy().reset_index(drop=True)

    # ---- 分数合成 ----
    pool["z_alpha"] = _z(pool["alpha_score"].fillna(pool["alpha_score"].mean()))
    pool["z_mom"] = _z(pool["momentum_score_trend_adj"])
    w_alpha = C.ALPHA_SCORE_WEIGHT if alpha_weight is None else float(alpha_weight)
    w_mom = C.MOMENTUM_SCORE_WEIGHT if momentum_weight is None else float(momentum_weight)
    if brake_mode:
        w_alpha, w_mom = 0.0, 1.0      # 防守模式下只按自身动量排（E 对防守资产无意义）
    pool["final_score"] = w_alpha * pool["z_alpha"] + w_mom * pool["z_mom"]

    # ---- 主题聚类 + 多样性上限（含迟滞：已持有的优先保留）----
    _names = {str(r["code"]): str(r["name"]) for _, r in pool.iterrows()}
    theme_map, conflict_pairs = build_theme_and_conflicts(
        holdings, pool["code"].astype(str).tolist(),
        names=_names, prices=prices, asof=asof,
    )
    keep_codes = set(current_holdings) if current_holdings else set()
    buf = C.HOLD_BUFFER if hold_buffer is None else int(hold_buffer)
    pool = apply_theme_cap(pool, theme_map, keep_codes=keep_codes, hold_buffer=buf,
                           conflicts=conflict_pairs,
                           prefer_held_in_theme=prefer_held_in_theme)

    selected = pool[pool["selected"]].copy().sort_values("final_score", ascending=False)
    defensive_mode = bool(len(equity[equity["mom120"] > C.ABSOLUTE_MOMENTUM_THRESHOLD]) == 0)

    # ---- 目标权重：等权 + 单只上限 ----
    n = len(selected)
    cap = 0.40
    if n == 0:
        target: dict[str, float] = {}
    else:
        w = min(1.0 / n, cap)            # 等权，但单只不超过 40%
        # 🔴 `Series.items()` 迭代的是 (索引, 值)，索引不是行 ⇒ 不能用 r["code"] 取代码
        target = {
            str(row["code"]): float(w)
            for _, row in selected.iterrows()
        }
    #: 权重上限导致未配满的部分 = 现金（不外借、不加杠杆）
    cash_weight = float(max(0.0, 1.0 - sum(target.values())))

    # ---- 手工操作清单 ----
    codes = sorted(set(target) | set(current_holdings))
    trades = []
    for code in codes:
        tgt = target.get(code, 0.0)
        cur = float(current_holdings.get(code, 0.0))
        delta = tgt - cur
        name = ""
        hit = selected[selected["code"].astype(str) == code]
        if len(hit):
            name = str(hit.iloc[0]["name"])
        else:
            hu = universe[universe["code"].astype(str) == code]
            if len(hu):
                name = str(hu.iloc[0]["name"])
        action = "买入" if delta > 1e-6 else ("卖出" if delta < -1e-6 else "不变")
        trades.append(
            {
                "code": code,
                "name": name,
                "target_weight": round(tgt, 6),
                "current_weight": round(cur, 6),
                "delta_weight": round(delta, 6),
                "action": action,
                "amount": round(delta * C.ETF_ACCOUNT, 2),
            }
        )
    trades = sorted(trades, key=lambda t: (-t["target_weight"], -t["delta_weight"]))

    detail = []
    for _, r in selected.iterrows():
        detail.append(
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "target_weight": round(float(target.get(str(r["code"]), 0.0)), 6),
                "final_score": round(float(r["final_score"]), 4),
                "alpha_score": None if not np.isfinite(r["alpha_score"]) else round(float(r["alpha_score"]), 4),
                "coverage": None if not np.isfinite(r.get("coverage", np.nan)) else round(float(r["coverage"]), 4),
                "coverage_ok": bool(r.get("coverage_ok", False)),
                "mom20": round(float(r["mom20"]), 4) if np.isfinite(r["mom20"]) else None,
                "mom60": round(float(r["mom60"]), 4) if np.isfinite(r["mom60"]) else None,
                "mom120": round(float(r["mom120"]), 4) if np.isfinite(r["mom120"]) else None,
                "rs60": round(float(r["rs60"]), 4) if np.isfinite(r.get("rs60", np.nan)) else None,
                "above_ma60": bool(r["above_ma60"]),
                "vol20": round(float(r["vol20"]), 4) if np.isfinite(r["vol20"]) else None,
                "theme_id": str(r["theme_id"]),
                "is_defensive": bool(r["is_defensive"]),
            }
        )

    return {
        "asof": asof.date().isoformat(),
        "brake_mode": brake_mode,
        "pool_mode": mode,
        "defensive_mode": defensive_mode,
        "relaxed_absolute_momentum_filter": relaxed,
        "n_selected": n,
        "cash_weight": round(cash_weight, 6),
        "weights_used": {"alpha": w_alpha, "momentum": w_mom},
        "hold_buffer": buf,
        "stats": stats,
        "holdings": detail,
        "trades": trades,
        "top_candidates": [
            {
                "code": str(r["code"]),
                "name": str(r["name"]),
                "final_score": round(float(r["final_score"]), 4),
                "alpha_score": None if not np.isfinite(r["alpha_score"]) else round(float(r["alpha_score"]), 4),
                "momentum_score": round(float(r["momentum_score_trend_adj"]), 4),
                "mom120": round(float(r["mom120"]), 4) if np.isfinite(r["mom120"]) else None,
                "coverage": None if not np.isfinite(r.get("coverage", np.nan)) else round(float(r["coverage"]), 4),
                "coverage_ok": bool(r.get("coverage_ok", False)),
                "theme_id": str(r["theme_id"]),
                "selected": bool(r["selected"]),
            }
            for _, r in pool.sort_values("final_score", ascending=False).head(15).iterrows()
        ],
    }
