"""ETF 调仓建议（主入口）：每个调仓日输出一份**可直接手工执行**的清单。

【用法（服务器）】
    export PYTHONHASHSEED=0
    python manu/etf_rotation/recommend.py --asof 2026-11-03 \
        --holdings "510300:0.5,518880:0.5" \
        --out-dir <落点根>/runs

`--holdings` 传**当前持仓权重**（可省略；省略则视为空仓 ⇒ 全部是买入）。
不传当前持仓时脚本只给「目标组合」，不产生买卖差额。

【输出】
- `<out-dir>/rebalance_recommendation.json`：完整结果（含打分明细、过滤统计、交易指令）
- `<out-dir>/rebalance_recommendation.csv`：交易指令表
- stdout：中文可读报告

【重要】本脚本输出是**模型打分与规则筛选的结果**，不是投资建议。
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
import score_smoothing as SS  # noqa: E402
from engine.features import load_prices  # noqa: E402
from engine.portfolio import build_portfolio, load_universe  # noqa: E402
from engine.weights import load_holdings  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ETF 调仓建议（人工执行版）")
    p.add_argument("--asof", default=None, help="信号日（含），默认取个股分数里最新的一天")
    p.add_argument("--holdings", default="", help="当前持仓权重，形如 '510300:0.5,518880:0.5'")
    p.add_argument("--prices", default=None)
    p.add_argument("--universe", default=None)
    p.add_argument("--holdings-csv", default=None, help="ETF 持仓（W）CSV")
    p.add_argument("--scores", default=None, help="个股分数 CSV（stock_scores.csv）")
    p.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    p.add_argument("--n-holdings", type=int, default=C.TARGET_N_HOLDINGS)
    p.add_argument("--nav", type=float, default=None,
                   help="**你账户的实际净值**（例如 1.35 = 本金 1 元变成 1.35 元）；"
                        "用于 v2 组合级回撤刹车判断")
    p.add_argument("--nav-peak", type=float, default=None,
                   help="**你账户的历史最高净值**（刹车判据：nav/peak-1 ≤ -阈值 ⇒ 切防守）")
    p.add_argument("--pool-mode", default=None, help="标的池口径：e_covered（默认）/ all")
    p.add_argument("--prefer-held-in-theme", type=int, default=None, choices=[0, 1],
                   help="同族内「已持有品种优先」：1=开（默认 config.THEME_PREFER_HELD），0=关")
    p.add_argument("--entry-ma-window", type=int, default=None, help="入场破位过滤均线（0=关）")
    p.add_argument("--exit-ma-window", type=int, default=None, help="持仓强制退出均线（0=关）")
    # ---- 🔴 穿透分时间平滑（与 analyze.py **同名同默认**，少一个就分叉）----
    p.add_argument("--no-smoothing", action="store_true",
                   help="关闭穿透分时间平滑（排序退回单日瞬态分；默认开启）")
    p.add_argument("--smoothing-method", default=None, choices=list(SS.SMOOTHING_METHODS),
                   help="平滑方法：ewma（默认）/ rolling")
    p.add_argument("--smoothing-window", type=int, default=None,
                   help="滚动窗口（交易日；默认 5）")
    p.add_argument("--smoothing-alpha", type=float, default=None,
                   help="EWMA 系数（默认取 config.SMOOTHING_ALPHA）")
    p.add_argument("--smoothing-history-days", type=int, default=None,
                   help="EWMA 预热历史跨度（交易日；默认 30）")
    return p.parse_args()


def parse_holdings_arg(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        code, _, w = chunk.partition(":")
        try:
            out[code.strip().zfill(6)] = float(w)
        except ValueError:
            continue
    return out


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    prices = load_prices(args.prices)
    universe = load_universe(args.universe)
    holdings = load_holdings(args.holdings_csv)
    scores = pd.read_csv(args.scores or C.STOCK_SCORES_CSV, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])

    asof = pd.Timestamp(args.asof) if args.asof else scores["date"].max()
    print(f"[INFO] 信号日 asof = {asof.date()}（价格数据最新 {prices['date'].max().date()}）")
    print(f"[INFO] 候选池 {len(universe)} 只 / 有持仓的 ETF {holdings['etf_code'].nunique()} 只 / "
          f"个股分数 {len(scores)} 行")

    # ---- v2 · 组合级回撤刹车：需要用户给出**自己账户的实际净值/峰值** ----
    nav_state = None
    if args.nav is not None and args.nav_peak is not None:
        nav = float(args.nav)
        peak = float(args.nav_peak)
        dd = nav / peak - 1.0 if peak > 0 else 0.0
        in_brake = dd <= -C.DRAWDOWN_BRAKE_THRESHOLD
        nav_state = {"nav": nav, "peak": peak, "in_brake": in_brake, "cooldown_left": 0}
        print(f"[INFO] 风控刹车检查：净值 {nav:.4f} / 峰值 {peak:.4f} ⇒ 回撤 {dd:+.2%} "
              f"（阈值 -{C.DRAWDOWN_BRAKE_THRESHOLD:.0%}）⇒ {'🚨 刹车 ON：切防守资产' if in_brake else '正常'}")

    # ---- 🔴 穿透分时间平滑：参数与 analyze.py **同名同默认** ----
    #    🔴 这里不自己算平滑表：由引擎走
    #    `score_smoothing.build_alpha_override`（与 analyze.py 同一个函数、
    #    同一份输入文件）⇒ 规则层与交付层**同源**。
    smoothing_config = SS.SmoothingConfig(
        enabled=(not args.no_smoothing) and bool(C.ENABLE_SCORE_SMOOTHING),
        method=args.smoothing_method or C.SMOOTHING_METHOD,
        window=(
            C.SMOOTHING_WINDOW if args.smoothing_window is None else int(args.smoothing_window)
        ),
        alpha=(
            C.SMOOTHING_ALPHA if args.smoothing_alpha is None else float(args.smoothing_alpha)
        ),
        history_span_days=(
            C.SMOOTHING_HISTORY_SPAN_DAYS
            if args.smoothing_history_days is None
            else int(args.smoothing_history_days)
        ),
    ).validated()

    result = build_portfolio(
        asof=asof,
        prices=prices,
        holdings=holdings,
        scores=scores,
        universe=universe,
        current_holdings=parse_holdings_arg(args.holdings),
        pool_mode=args.pool_mode,
        nav_state=nav_state,
        entry_ma_window=args.entry_ma_window,
        exit_ma_window=args.exit_ma_window,
        prefer_held_in_theme=(None if args.prefer_held_in_theme is None
                              else bool(args.prefer_held_in_theme)),
        smoothing_config=smoothing_config,
    )
    smoothing_diag = result.get("score_smoothing") or {}
    if smoothing_diag.get("warning"):
        print(f"[WARN] {smoothing_diag['warning']}")
    print(
        f"[INFO] 穿透分平滑：{'关闭' if not smoothing_config.enabled else smoothing_config.method}"
        f"｜窗口 {smoothing_config.window} 交易日｜alpha {smoothing_config.alpha}"
        f"｜观测日 {smoothing_diag.get('n_observation_dates')} 个"
        f"（{', '.join(smoothing_diag.get('observation_dates') or []) or '无'}）"
        f"｜退化成瞬态分 {smoothing_diag.get('n_codes_fallback_transient')} 只"
    )

    # ---- 落盘 ----
    jpath = out_dir / "rebalance_recommendation.json"
    jpath.write_text(
        json.dumps(
            {
                "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                "host": platform.node(),
                "asof": result["asof"],
                "defensive_mode": result["defensive_mode"],
                "brake_mode": result.get("brake_mode", False),
                "pool_mode": result.get("pool_mode"),
                # 🔴 平滑口径审计（规则层自己算的那一份 ⇒ 可与交付层对账）
                "score_smoothing": smoothing_diag,
                "nav_state": nav_state,
                "relaxed_absolute_momentum_filter": result["relaxed_absolute_momentum_filter"],
                "rebalance_every_n_days": C.DEPLOYED_REBALANCE_EVERY_N_DAYS,
                "signal_stride_base_n_days": C.REBALANCE_EVERY_N_DAYS,
                "signal_stride_multiplier": C.REBALANCE_STRIDE_MULTIPLIER,
                "execution_lag_days": C.EXECUTION_LAG_DAYS,
                "n_holdings": result["n_selected"],
                # 🔴 2026-11-11（真缺陷 · 交付件自证口径）：现金预留 / 权益预算
                #    这三个字段**引擎自己已经算出来了**（`engine/portfolio.py` 的
                #    `cash_weight` / `cash_reserve_ratio` / `equity_budget`，
                #    分别来自 `config.CASH_RESERVE_RATIO` 与 `1 - CASH_RESERVE_RATIO`）
                #    ⇒ 但本文件落盘时**只漏了这三个** ⇒ 规则层自己的交付件
                #       `rebalance_recommendation.json` 里查不到「留 20% 现金」的口径
                #       （同一份报告的 `etf_analysis_report.json` 有 ⇒ 两件自相矛盾）。
                #    ⇒ 现在补齐：**规则层自己算的值**，不是外部常量、也不是反推。
                "cash_weight": result.get("cash_weight"),
                "cash_reserve_ratio": result.get("cash_reserve_ratio"),
                "equity_budget": result.get("equity_budget"),
                "holdings": result["holdings"],
                "trades": result["trades"],
                "top_candidates": result["top_candidates"],
                "stats": result["stats"],
                "cost_assumption": {
                    "commission_one_side": C.ETF_COMMISSION,
                    "slippage_one_side": C.ETF_SLIPPAGE,
                    "note": "场内 ETF 免印花税、免过户费；佣金按万 1.5",
                },
                "disclaimer": "模型打分与规则筛选结果，不构成投资建议；人工下单，注意流动性与冲击成本。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cpath = out_dir / "rebalance_recommendation.csv"
    pd.DataFrame(result["trades"]).to_csv(cpath, index=False, encoding="utf-8-sig")

    # ---- 屏幕报告 ----
    print("=" * 78)
    print(f"ETF 调仓建议 · 信号日 {result['asof']} · 执行日 = 信号日 + {C.EXECUTION_LAG_DAYS} 个交易日")
    if result["defensive_mode"]:
        print("⚠️ 防守模式：没有权益 ETF 通过绝对动量门槛 ⇒ 建议切黄金/国债")
    if result["relaxed_absolute_momentum_filter"]:
        print("⚠️ 已放宽绝对动量门槛（通过数不足 3 只）")
    print(f"过滤：候选池 {result['stats']['n_universe']} → 过流动性 {result['stats']['n_pass_liquidity']} 只")
    print("-" * 78)
    print(f"{'代码':<8}{'名称':<24}{'权重':>7}  {'总分':>7} {'映射分E':>8} {'mom120':>8} {'趋势':>5} {'波动':>7}  覆盖")
    for h in result["holdings"]:
        cov = h["coverage"]
        cov_s = f"{cov:.0%}" if cov is not None else "  -"
        mom = f"{h['mom120']:+.1%}" if h["mom120"] is not None else "  -"
        vol = f"{h['vol20']:.1%}" if h["vol20"] is not None else "  -"
        alpha = f"{h['alpha_score']:.3f}" if h["alpha_score"] is not None else "  -"
        print(
            f"{h['code']:<8}{h['name'][:22]:<24}{h['target_weight']:>6.1%}  "
            f"{h['final_score']:>7.3f} {alpha:>8} {mom:>8} {'Y' if h['above_ma60'] else 'N':>5} "
            f"{vol:>7}  {cov_s}"
        )
    print("-" * 78)
    if result["trades"]:
        print("【手工操作清单】（按目标权重与当前持仓的差额）")
        print(f"{'动作':<6}{'代码':<8}{'名称':<24}{'目标':>8}{'当前':>8}{'差额':>8}{'金额':>12}")
        for t in result["trades"]:
            print(
                f"{t['action']:<6}{t['code']:<8}{t['name'][:22]:<24}"
                f"{t['target_weight']:>7.1%}{t['current_weight']:>8.1%}"
                f"{t['delta_weight']:>+8.1%}{t['amount']:>12,.0f}"
            )
    print("=" * 78)
    print(f"已写出 {jpath}")
    print(f"已写出 {cpath}")
    print("⚠️ 本结果是模型打分与规则筛选的输出，**不构成投资建议**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
