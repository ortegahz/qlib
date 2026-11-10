"""把 ETF 轮动回测网格汇总成 Markdown 表（memory 用）。

用法（服务器）：
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/summarize_etf_grid.py \
        --dir <落点根>/runs --pattern "etf_rotation_backtest_*.json"

输出：每个场景一行（年化 / 基准 / 超额 / Sharpe / MDD / 换手 / 成本 / 刹车事件 / H1·H2 分段）。
口径提醒：超额为**相对口径（几何）**；(1+r_p)/(1+r_b)-1 逐日累计后年化。
"""
from __future__ import annotations

import argparse
import glob
import json
import os


#: 🔴 **结果落点根**：单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT` ⇒ 本工具按**同一个环境变量**取值（两边不会分叉）。
_RESULTS_ROOT = os.environ.get(
    "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
)
#: 产物根缺省值（= 结果根；与 `daily_config.RESEARCH_RUN_ROOT` 同口径）
_DEFAULT_RUN_ROOT = _RESULTS_ROOT


def _f(x):
    return float(x) if isinstance(x, (int, float)) else float("nan")


def collect(directory: str, pattern: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(directory, pattern))):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        seg = d.get("segment_report", {})
        h1 = seg.get("H1_to_2025-06-30", {})
        h2 = seg.get("H2_from_2025-06-30", {})
        brake = d.get("brake", {})
        rows.append({
            "tag": d.get("scenario", ""),
            "pool": d.get("pool_mode", ""),
            "ann": _f(d["portfolio_net"].get("annualized_return")),
            "bench": _f(d["benchmark_510300"].get("annualized_return")),
            "exc": _f(d["excess_vs_510300"].get("annualized_return")),
            "exc_arith": _f((d.get("excess_arith_vs_510300") or {}).get("annualized_return")),
            "sharpe": _f(d["excess_vs_510300"].get("sharpe")),
            "mdd": _f(d["portfolio_net"].get("max_drawdown")),
            "turnover": _f(d["cost_assumption"].get("annualized_one_way_turnover")),
            "drag": _f(d["cost_assumption"].get("total_cost_drag")),
            "nreb": d.get("n_rebalances", 0),
            "nreb_win": d.get("n_rebalances_in_window", 0),
            "brake_events": brake.get("n_events", 0),
            "final_dd": _f(brake.get("final_drawdown")),
            "h1": _f((h1.get("excess") or {}).get("annualized_return")),
            "h1n": (h1.get("excess") or {}).get("n_days", 0),
            "h2": _f((h2.get("excess") or {}).get("annualized_return")),
            "h2n": (h2.get("excess") or {}).get("n_days", 0),
            "failed": d.get("n_failed_rebalances", 0),
            "carried": d.get("n_carried_forward_rebalances", 0),
            "uncov": d.get("n_uncovered_days", 0),
            "n_days": (d.get("eval_window") or {}).get("n_days", 0),
        })
    return rows


def render(rows: list[dict]) -> str:
    head = ("场景", "池", "年化", "基准", "超额(相对)", "超额(算术)", "Sharpe", "MDD",
            "换手×", "成本", "期数", "刹车", "期末回撤", "H1超额", "H2超额", "失败")
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows:
        out.append("| " + " | ".join([
            r["tag"], r["pool"] or "-",
            f"{r['ann']:.2%}", f"{r['bench']:.2%}", f"{r['exc']:.2%}", f"{r['exc_arith']:.2%}",
            f"{r['sharpe']:.3f}", f"{r['mdd']:.1%}", f"{r['turnover']:.1f}", f"{r['drag']:.2%}",
            f"{r['nreb_win']}/{r['nreb']}", str(r["brake_events"]), f"{r['final_dd']:.1%}",
            f"{r['h1']:.2%}({r['h1n']})", f"{r['h2']:.2%}({r['h2n']})",
            f"{r['failed']}/{r['carried']}/{r['uncov']}",
        ]) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(_DEFAULT_RUN_ROOT))
    ap.add_argument("--pattern", default="etf_rotation_backtest_*.json")
    args = ap.parse_args()
    rows = collect(args.dir, args.pattern)
    if not rows:
        print(f"[WARN] {args.dir}/{args.pattern} 没有匹配到产物")
        return 1
    print(f"窗口 n_days={rows[0]['n_days']}  场景数={len(rows)}")
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
