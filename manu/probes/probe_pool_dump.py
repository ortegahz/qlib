#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：把 ``build_portfolio`` 的**完整候选池**打出来（引擎逻辑不复制一份）。

【为什么需要】
``build_portfolio`` 只返回 ``top_candidates``（前 15 只），
但池里有 31 只 ⇒ 排在后面的「边缘标的」（如消费50 / 医药ETF易方达）
在报告里看不到 ⇒ 无法判断「为了凑满 5 席会不会买进模型看空的品种」。

【做法】
用 spy 包一层 ``apply_theme_cap``（它是选池的最后一环，入参就是完整池子）
⇒ **不复制引擎逻辑**，只被动观测。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_pool_dump.py --asof 2026-11-03
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as C  # noqa: E402
import engine.portfolio as P  # noqa: E402
from engine.features import load_prices  # noqa: E402
from engine.weights import load_holdings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="打印 build_portfolio 的完整候选池")
    ap.add_argument("--asof", default="2026-11-03")
    ap.add_argument("--prices", default=None)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--holdings-csv", default=None)
    ap.add_argument("--scores", default=None)
    ap.add_argument("--holdings", default="", help="当前持仓权重，逗号分隔 code:weight")
    args = ap.parse_args()

    prices = load_prices(args.prices)
    universe = P.load_universe(args.universe)
    holdings = load_holdings(args.holdings_csv)
    scores = pd.read_csv(args.scores or C.STOCK_SCORES_CSV, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])

    cur: dict[str, float] = {}
    for item in args.holdings.split(","):
        if not item.strip():
            continue
        code, _, w = item.partition(":")
        cur[code.strip().zfill(6)] = float(w)

    captured: dict[str, pd.DataFrame] = {}
    orig = P.apply_theme_cap

    def _spy(scored, *a, **kw):
        captured["pool"] = scored.copy()
        return orig(scored, *a, **kw)

    P.apply_theme_cap = _spy
    try:
        res = P.build_portfolio(
            asof=pd.Timestamp(args.asof), prices=prices, holdings=holdings, scores=scores,
            universe=universe, current_holdings=cur or None,
        )
    finally:
        P.apply_theme_cap = orig

    pool = captured["pool"]
    pool = pool.sort_values("final_score", ascending=False)
    cols = ["code", "name", "final_score", "z_alpha", "z_mom", "alpha_score", "coverage",
            "mom120", "momentum_score_trend_adj", "is_defensive", "theme_id", "selected"]
    cols = [c for c in cols if c in pool.columns]
    print(f"asof={args.asof} 池子 {len(pool)} 只；入选 {res['n_selected']} 只；"
          f"cash_weight={res['cash_weight']}；defensive_mode={res['defensive_mode']}")
    print(pool[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
