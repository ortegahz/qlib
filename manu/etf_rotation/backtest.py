"""ETF 轮动策略回测（双周调仓，人工可执行口径）。

【口径（rules.md §12.1 六项）】
| 维度 | 取值 |
|:--|:--|
| ① 数据区间 | 2024-01 ~ 2026-11（调仓日来自个股分数文件） |
| ② 标的池 | 场内 ETF 候选池（流动性过滤后，见 `etf_universe.csv`） |
| ③ 成本假设 | 场内 ETF：佣金 1.5bp/边 + 滑点 5bp/边，**免印花税、免过户费**（另跑压力情景：5bp + 20bp） |
| ④ 组合约束 | 3~5 只、等权、单只上限 40%、同主题最多 1 只、无杠杆 |
| ⑤ 基准 | 沪深300ETF（510300）买入持有 |
| ⑥ 资金 | 200,000 元 |
| region | cn |

【执行时序（无未来函数）】
信号日 T（收盘后可得）→ **执行日 T+1 收盘** ⇒ 持仓权重在 (T+1 收盘, 下次执行日收盘] 区间内有效。

【已知偏差（必须披露）】
1. **W（ETF 成分权重）是当前快照** ⇒ 含成分股历史变更的前视偏差（指数调整约半年一次）；
2. **ETF 候选池是当前存续的 ETF** ⇒ 含幸存者偏差（新发的 ETF 缺失）；
3. **成本是线性近似**：滑点按固定 bp，未建模盘口深度与涨跌停排队。

【产物】`<out-dir>/etf_rotation_backtest.json`
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from engine.features import load_prices  # noqa: E402
from engine.portfolio import build_portfolio, load_universe  # noqa: E402
from engine.weights import load_holdings  # noqa: E402

TRADING_DAYS_PER_YEAR = 250


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ETF 轮动策略回测（双周调仓）")
    p.add_argument("--prices", default=None)
    p.add_argument("--universe", default=None)
    p.add_argument("--holdings-csv", default=None)
    p.add_argument("--scores", default=None)
    p.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--commission", type=float, default=C.ETF_COMMISSION)
    p.add_argument("--slippage", type=float, default=C.ETF_SLIPPAGE)
    p.add_argument("--tag", default="base", help="情景标签（base / stress）")
    p.add_argument("--rebalance-every", type=int, default=1,
                   help="每隔 N 个**信号日**取一个作为调仓日（1=每期都调；2=双周→月；4=双周→季）")
    p.add_argument("--alpha-weight", type=float, default=None,
                   help="E=W×S 映射分权重（默认取 config；0=纯动量，用于消融）")
    p.add_argument("--momentum-weight", type=float, default=None,
                   help="动量分权重（默认取 config）")
    p.add_argument("--hold-buffer", type=int, default=None,
                   help="迟滞缓冲区（默认取 config；0=每期全换）")
    p.add_argument("--pool-mode", default=None,
                   help="标的池属性口径：e_covered（只留能穿透 E=W×S 的）/ all（v1 行为）")
    p.add_argument("--brake-threshold", type=float, default=None,
                   help="组合级回撤刹车触发阈值（默认取 config；>1 表示关闭刹车）")
    p.add_argument("--prefer-held-in-theme", type=int, default=None, choices=[0, 1],
                   help="同族内「已持有品种优先」：1=开（默认 config.THEME_PREFER_HELD），0=关（A/B 用）")
    p.add_argument("--entry-ma-window", type=int, default=None,
                   help="入场破位过滤均线窗口（0 = 关闭；默认取 config）")
    p.add_argument("--exit-ma-window", type=int, default=None,
                   help="持仓强制退出的均线窗口（0 = 关闭；默认取 config）")
    p.add_argument("--segment-cut", default="2025-06-30",
                   help="分段稳定性切点（默认 2025-06-30 = H1 选参段 / H2 报告段）")
    return p.parse_args()


def _price_matrix(prices: pd.DataFrame) -> pd.DataFrame:
    """构造 close 的宽表（index=date, columns=code）。"""
    return prices.pivot_table(index="date", columns="code", values="close", aggfunc="last")


def _returns_from_weights(rets: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """按固定权重持有资产，逐日收益 = Σ w_i * 个股日收益（缺价记 0）。"""
    cols = [c for c in weights if c in rets.columns]
    w = pd.Series({c: weights[c] for c in cols})
    if w.empty:
        return pd.Series(0.0, index=rets.index)
    port = (rets[cols] * w).sum(axis=1)
    return port.fillna(0.0)


def run_backtest(args: argparse.Namespace) -> dict:
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    # 阈值覆盖（进程内生效，便于网格消融；不改配置文件）
    if args.brake_threshold is not None:
        C.DRAWDOWN_BRAKE_THRESHOLD = float(args.brake_threshold)
    if args.entry_ma_window is not None:
        C.ENTRY_MA_FILTER_WINDOW = int(args.entry_ma_window)
    if args.exit_ma_window is not None:
        C.EXIT_MA_FILTER_WINDOW = int(args.exit_ma_window)
    prices = load_prices(args.prices)
    universe = load_universe(args.universe)
    holdings = load_holdings(args.holdings_csv)
    scores = pd.read_csv(args.scores or C.STOCK_SCORES_CSV, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])

    sig_dates = sorted(scores["date"].unique())
    sig_dates = [pd.Timestamp(d) for d in sig_dates]
    if args.start:
        sig_dates = [d for d in sig_dates if d >= pd.Timestamp(args.start)]
    if args.end:
        sig_dates = [d for d in sig_dates if d <= pd.Timestamp(args.end)]
    if args.rebalance_every > 1:
        # 信号日是每 10 个交易日的双周栅格；再按 N 取一 ⇒ 实际调仓周期 = 10N 个交易日
        sig_dates = sig_dates[:: args.rebalance_every]
    print(f"[INFO] 调仓（信号）日 {len(sig_dates)} 个：{sig_dates[0].date()} ~ {sig_dates[-1].date()}"
          f"（每 {args.rebalance_every} 个信号日取一 ⇒ 实际约 {10 * args.rebalance_every} 个交易日）")

    px = _price_matrix(prices)
    px_rets = px.pct_change()
    all_dates = px.index

    # 信号日 → 执行日（往后第一个有价格的交易日）
    exec_dates: list[pd.Timestamp] = []
    for d in sig_dates:
        later = all_dates[all_dates > d]
        if len(later) == 0:
            continue
        exec_dates.append(later[0])
    pairs = list(zip(sig_dates, exec_dates))
    assert len(pairs) == len(sig_dates) == len(exec_dates), "信号日与执行日配对数量不一致"
    if not pairs:
        raise ValueError("没有可执行的调仓日")

    rebalance_log: list[dict] = []
    prev_weights: dict[str, float] = {}
    port_rets = pd.Series(np.nan, index=all_dates)   # NaN = 该日**没有**组合暴露（未覆盖）
    # 🔴 刻意**不用**「组合实际持有过的日子」反推评估窗口（rules.md §十四.6 明令禁止）

    n_failed = 0
    # ---- v2 · 组合级回撤刹车（用**组合自身净值**，不看个股/ETF 信号）----
    nav = 1.0
    nav_peak = 1.0
    brake_on = False
    brake_cooldown = 0
    brake_events: list[dict] = []
    for i, (sig, exe) in enumerate(pairs):
        carry_forward = False
        res = None
        nav_state = {
            "nav": nav, "peak": nav_peak,
            "in_brake": brake_on, "cooldown_left": brake_cooldown,
        }
        try:
            res = build_portfolio(
                asof=sig, prices=prices, holdings=holdings, scores=scores,
                universe=universe, current_holdings=prev_weights or None,
                alpha_weight=args.alpha_weight, momentum_weight=args.momentum_weight,
                hold_buffer=args.hold_buffer, pool_mode=args.pool_mode,
                prefer_held_in_theme=(None if args.prefer_held_in_theme is None
                                      else bool(args.prefer_held_in_theme)),
                nav_state=nav_state, entry_ma_window=args.entry_ma_window,
                exit_ma_window=args.exit_ma_window,
            )
        except Exception as exc:  # noqa: BLE001
            # 🔴 失败**不能静默跳过**：跳过 = 把该段当成「空仓现金」，
            #    既歪曲组合收益，又让不同场景的评估窗口长度不一致（基准不可比）。
            #    真实交易里「这期没调成仓」= **继续持有上一期持仓** ⇒ 按此处理。
            print(f"[WARN] {sig.date()} 构建组合失败 ⇒ 沿用上一期持仓：{exc!r}")
            traceback.print_exc()
            n_failed += 1
            carry_forward = True          # 🔴 必须置 True（否则审计字段恒 0 = 假披露）
            if not prev_weights:
                print(f"[WARN] {sig.date()} 构建组合失败且无历史持仓 ⇒ 该段空仓")
                continue
            avail = dict(prev_weights)
            detail_rows: list[dict] = []
        else:
            weights = {h["code"]: float(h["target_weight"]) for h in res["holdings"]}
            # 缺价的标的剔除并重新归一（实际拿不到就没法持有）
            avail = {c: w for c, w in weights.items() if c in px.columns}
            total = sum(avail.values())
            if total > 0:
                avail = {c: w / total for c, w in avail.items()}
            if not avail:
                # 🔴 同样不许静默跳过：目标标的全部无价 ⇒ 本期没法调仓
                n_failed += 1
                carry_forward = True
                print(f"[WARN] {sig.date()} 目标标的全部无价 ⇒ 沿用上一期持仓")
                if not prev_weights:
                    print(f"[WARN] {sig.date()} 且无历史持仓 ⇒ 该段空仓")
                    continue
                avail = dict(prev_weights)
                detail_rows = []
            else:
                detail_rows = [
                    {k: h[k] for k in ("code", "name", "target_weight", "final_score",
                                       "alpha_score", "mom120", "above_ma60", "coverage")}
                    for h in res["holdings"]
                ]

        turnover = sum(abs(avail.get(c, 0.0) - prev_weights.get(c, 0.0)) for c in set(avail) | set(prev_weights))
        cost_rate = (args.commission + args.slippage) * turnover

        # 从执行日收盘开始持有，直到下一个执行日收盘
        nxt = pairs[i + 1][1] if i + 1 < len(pairs) else None
        # 🔴 执行在 exe **收盘** ⇒ 只能赚 exe **之后**的收益；
        #    早先用 `>= exe` 把「买入当天」的涨跌也算进来了 ⇒ 每期多算 1 天（≈10% 超额暴露）。
        # 🔴 区间必须是 **(exe_i, exe_{i+1}]**：
        #    在 exe_i **收盘**调仓 ⇒ exe_i 当天的涨跌属于**上一期持仓**，
        #    新持仓的收益区间是 exe_i+1 .. exe_{i+1}（含右端）。
        #    早先写成两端都开区间 (`> exe` 且 `< nxt`) ⇒ **每期漏掉 1 个交易日的收益**，
        #    且评估窗口比实际短 N 天（34 期 ⇒ 漏 34 天）。
        seg = all_dates[(all_dates > exe)]
        if nxt is not None:
            seg = seg[seg <= nxt]
        seg_rets = _returns_from_weights(px_rets, avail)
        # 段内收益：从 exe 收盘到 seg 最后一个交易日收盘
        seg_vals = seg_rets.loc[seg].copy()
        if len(seg_vals):
            # 调仓成本：按**买卖差额的线性成本**一次性扣掉（记在段内第一天 ≈ 执行日次日，
            # 与真实「执行日收盘成交」相差 1 天利息，量级 ~0.65bp×日收益，可忽略）
            seg_vals.iloc[0] -= cost_rate
        seg_ret = float((1.0 + seg_vals).prod() - 1.0) if len(seg) else 0.0
        # 🔴 末期那一期的段是空的（窗口止于末个执行日）⇒ 它的换手/成本**从没进过净值**，
        #    不许计入报告（否则「报告的成本」≠「净值承担的成本」）。
        seg_in_window = bool(len(seg))
        turnover_in_window = turnover if seg_in_window else 0.0
        port_rets.loc[seg] = seg_vals

        rebalance_log.append(
            {
                "signal_date": sig.date().isoformat(),
                "exec_date": exe.date().isoformat(),
                "n_holdings": len(avail),
                "weights": {c: round(w, 6) for c, w in avail.items()},
                "holdings_detail": detail_rows,
                "carried_forward": carry_forward,
                "defensive_mode": None if res is None else res["defensive_mode"],
                "relaxed_absolute_momentum_filter": None if res is None else res["relaxed_absolute_momentum_filter"],
                "cash_weight": None if res is None else res["cash_weight"],
                "turnover_one_way": round(turnover, 6),
                "turnover_in_window": round(turnover_in_window, 6),
                "seg_in_window": bool(seg_in_window),
                "cost_rate": round(cost_rate, 8),
                "seg_return": round(seg_ret, 8),
            }
        )
        prev_weights = avail
        # ---- 净值跟踪 + 刹车状态机 ----
        nav *= (1.0 + seg_ret)
        nav_peak = max(nav_peak, nav)
        drawdown = nav / nav_peak - 1.0
        was_brake = brake_on
        if (not brake_on) and drawdown <= -C.DRAWDOWN_BRAKE_THRESHOLD:
            brake_on = True
            brake_cooldown = C.DRAWDOWN_BRAKE_COOLDOWN
            brake_events.append({
                "signal_date": sig.date().isoformat(), "exec_date": exe.date().isoformat(),
                "event": "TRIGGER", "nav": round(nav, 6), "peak": round(nav_peak, 6),
                "drawdown": round(drawdown, 6),
            })
        elif brake_on:
            brake_cooldown -= 1
            released = (brake_cooldown <= 0) and (drawdown >= -C.DRAWDOWN_BRAKE_RELEASE)
            if released:
                brake_on = False
                brake_cooldown = 0
                brake_events.append({
                    "signal_date": sig.date().isoformat(), "exec_date": exe.date().isoformat(),
                    "event": "RELEASE", "nav": round(nav, 6), "peak": round(nav_peak, 6),
                    "drawdown": round(drawdown, 6),
                })
        print(f"[{i + 1}/{len(pairs)}] 信号 {sig.date()} → 执行 {exe.date()}  "
              f"持仓 {len(avail)} 只  换手 {turnover:.3f}  段收益 {seg_ret:+.3%}  "
              f"净值 {nav:.4f} 回撤 {drawdown:+.2%}{'  [刹车ON]' if brake_on else ''}")

    # ---- 汇总 ----
    # 🔴 评估窗口 = 组合**真正持有过**的交易日（执行日之后，因为执行在收盘）。
    #    基准必须用**同一段**日期，否则基准会多算执行日当天的涨跌（口径不对齐）。
    # 🔴 评估窗口必须**确定性**：所有场景用同一段 (首个执行日, 末个执行日]，
    #    否则不同场景的基准不一致 ⇒ 超额不可比（这是之前 b2 场景数字失真的第二个原因）。
    eval_dates = all_dates[(all_dates > pairs[0][1]) & (all_dates <= pairs[-1][1])]
    port_raw = port_rets.reindex(eval_dates)
    n_uncovered = int(port_raw.isna().sum())
    port = port_raw.fillna(0.0)
    bench_code = C.ETF_BENCHMARK
    if bench_code in px.columns:
        bench_seg = px_rets[bench_code].reindex(port.index).fillna(0.0)
        # 基准的起点也必须是「执行日之后」⇒ 用同一段日期的**区间收益**对齐
        bench_seg = bench_seg.loc[port.index]
    else:
        bench_seg = pd.Series(0.0, index=port.index)

    def ann_metrics(r: pd.Series) -> dict:
        if len(r) < 2:
            return {"annualized_return": None, "volatility": None, "sharpe": None,
                    "max_drawdown": None, "total_return": None, "n_days": int(len(r))}
        cum = (1.0 + r).cumprod()
        years = len(r) / TRADING_DAYS_PER_YEAR
        ann = cum.iloc[-1] ** (1.0 / years) - 1.0 if years > 0 else None
        vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
        sharpe = float(ann / vol) if vol > 0 else None
        dd = float((cum / cum.cummax() - 1.0).min())
        return {
            "annualized_return": None if ann is None else float(ann),
            "volatility": vol,
            "sharpe": sharpe,
            "max_drawdown": dd,
            "total_return": float(cum.iloc[-1] - 1.0),
            "n_days": int(len(r)),
        }

    # 🔴 超额主口径 = **相对口径（几何）**：逐日 (1+r_p)/(1+r_b)-1 后再累计年化。
    #    （算术差序列不是任何真实组合的收益，年化/夏普会系统性偏；算术差仅留作诊断）
    excess_rel = (1.0 + port) / (1.0 + bench_seg) - 1.0
    excess_arith = port - bench_seg

    # ---- 分段稳定性（2024~2025H1 vs 2025H2~2026）----
    cut = pd.Timestamp(args.segment_cut)

    def _sub(r: pd.Series) -> dict:
        m = ann_metrics(r)
        m["start"] = str(r.index[0].date()) if len(r) else None
        m["end"] = str(r.index[-1].date()) if len(r) else None
        return m

    seg_report = {}
    for name, mask in (
        (f"H1_to_{cut.date()}", port.index <= cut),
        (f"H2_from_{cut.date()}", port.index > cut),
    ):
        sub_p = port[mask]
        sub_b = bench_seg[mask]
        seg_report[name] = {
            "portfolio": _sub(sub_p),
            "benchmark": _sub(sub_b),
            "excess": _sub((1.0 + sub_p) / (1.0 + sub_b) - 1.0),      # 相对口径（主）
            "excess_arith": _sub(sub_p - sub_b),                        # 算术差（诊断）
        }

    nav = pd.DataFrame(
        {
            "portfolio_ret": port,
            "benchmark_ret": bench_seg,
            "excess_ret": excess_rel,                                   # 相对口径日超额（主）
            "excess_ret_arith": excess_arith,                           # 算术差日超额（诊断）
            "nav_portfolio": (1.0 + port).cumprod(),                    # 累计净值（看形态用）
            "nav_benchmark": (1.0 + bench_seg).cumprod(),
            "nav_excess_rel": (1.0 + excess_rel).cumprod(),
        },
        index=port.index,
    )
    nav.index.name = "date"
    nav_path = Path(args.out_dir) / f"etf_rotation_nav_{args.tag}.csv"
    nav.to_csv(nav_path)

    port_net = port
    m_port = ann_metrics(port_net)
    m_bench = ann_metrics(bench_seg)
    m_excess = ann_metrics(excess_rel)
    m_excess_arith = ann_metrics(excess_arith)
    total_turnover = sum(r.get("turnover_in_window", 0.0) for r in rebalance_log)
    n_reb = len(rebalance_log)
    n_reb_in_window = sum(1 for r in rebalance_log if r.get("seg_in_window"))
    years = max(1e-9, len(port_net) / TRADING_DAYS_PER_YEAR)

    return {
        "scenario": args.tag,
        "cost_assumption": {
            "commission_one_side": args.commission,
            "slippage_one_side": args.slippage,
            "one_way_turnover_sum": total_turnover,
            "annualized_one_way_turnover": total_turnover / years,
            "total_cost_drag": total_turnover * (args.commission + args.slippage),
            "note": "ETF 免印花税、免过户费；佣金按万 1.5、滑点 5bp（压力情景见 stress）",
        },
        "rebalance": {
            "every_n_days": C.REBALANCE_EVERY_N_DAYS * args.rebalance_every,
            "signal_stride": args.rebalance_every,
            "hold_buffer": args.hold_buffer if args.hold_buffer is not None else C.HOLD_BUFFER,
            "weights_used": {
                "alpha": args.alpha_weight if args.alpha_weight is not None else C.ALPHA_SCORE_WEIGHT,
                "momentum": args.momentum_weight if args.momentum_weight is not None else C.MOMENTUM_SCORE_WEIGHT,
            },
            "execution_lag_days": C.EXECUTION_LAG_DAYS,
            "n_rebalances": n_reb,
        "n_rebalances_in_window": int(n_reb_in_window),
        "note_rebalances": "n_rebalances = 信号日总数；n_rebalances_in_window = 评估窗口内真正有持仓段的期数",
            "n_selected_median": float(np.median([r["n_holdings"] for r in rebalance_log])) if rebalance_log else 0,
            "n_defensive_rebalances": int(sum(1 for r in rebalance_log if r["defensive_mode"])),
        },
        "portfolio_net": m_port,
        "benchmark_510300": m_bench,
        "excess_vs_510300": m_excess,
        "excess_arith_vs_510300": m_excess_arith,
        "excess_definition": "相对口径（几何）：逐日 (1+r_p)/(1+r_b)-1 累计后再年化",
        "n_relaxed_rebalances": int(sum(1 for r in rebalance_log if r.get("relaxed_absolute_momentum_filter"))),
        "n_failed_rebalances": int(n_failed),
        "n_carried_forward_rebalances": int(sum(1 for r in rebalance_log if r.get("carried_forward"))),
        "n_uncovered_days": int(n_uncovered),
        "brake": {
            "threshold": C.DRAWDOWN_BRAKE_THRESHOLD,
            "release": C.DRAWDOWN_BRAKE_RELEASE,
            "cooldown": C.DRAWDOWN_BRAKE_COOLDOWN,
            "n_events": len(brake_events),
            "events": brake_events,
            "final_nav": round(nav, 6),
            "final_peak": round(nav_peak, 6),
            "final_drawdown": round(nav / nav_peak - 1.0, 6),
        },
        "pool_mode": args.pool_mode or C.POOL_MODE,
        "segment_cut": str(cut.date()),
        "eval_window": {
            "start": str(eval_dates[0].date()) if len(eval_dates) else None,
            "end": str(eval_dates[-1].date()) if len(eval_dates) else None,
            "n_days": int(len(eval_dates)),
        },
        "segment_report": seg_report,
        "nav_csv": str(nav_path),
        "bias_disclosures": [
            "W（ETF 成分权重）是当前快照 ⇒ 含成分股历史变更的前视偏差",
            "ETF 候选池是当前存续 ETF ⇒ 含幸存者偏差（新发 ETF 缺失）",
            "成本为固定 bp 线性近似，未建模盘口深度与涨跌停排队",
        ],
        "rebalance_log": rebalance_log,
    }


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = run_backtest(args)
    path = out_dir / f"etf_rotation_backtest_{args.tag}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("=" * 78)
    print(f"场景 {res['scenario']}：年化 {res['portfolio_net']['annualized_return']} "
          f"基准 {res['benchmark_510300']['annualized_return']} 超额 {res['excess_vs_510300']['annualized_return']}")
    print(f"换手（年化单边） {res['cost_assumption']['annualized_one_way_turnover']:.2f}×  "
          f"成本拖累 {res['cost_assumption']['total_cost_drag']:.4f}")
    print(f"已写出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
