"""聚类口径回归探针：新硬分区 vs 老并查集（并查集传递闭包）。

【目的】
服务器现役 `engine/portfolio.py` 走 **族硬分区 + 直接冲突对**（`build_theme_and_conflicts`），
而 2026-11-04 17:19 那批 v2 基线数字（年化 21.4929% / 超额 +8.3732% / MDD -28.1931%）
是用**老并查集聚类**（`cluster_by_overlap`，传递闭包）跑出来的。
本探针把 `build_theme_and_conflicts` 猴补丁成老口径并重跑同一场景，
用来判定「基线漂移」到底是**代码改了**还是**数据变了**。

【结论判据】
- 若猴补丁后能**逐位复现** 21.4929% / 12.1061% ⇒ 漂移来自聚类重构（代码变更），数据没变。
- 若复现不出来 ⇒ 另有原因，必须继续查。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/probes/probe_cluster_regression.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402
import engine.portfolio as P  # noqa: E402
from engine.filters import cluster_by_overlap  # noqa: E402

import backtest as BT  # noqa: E402


def _legacy_theme_and_conflicts(holdings, codes, overlap_thresh=None, names=None,
                                prices=None, asof=None):
    """老口径：并查集聚类（传递闭包），**没有**「直接冲突对」这一层。"""
    theme_map = cluster_by_overlap(
        holdings, codes,
        overlap_thresh=C.DEDUP_OVERLAP_THRESHOLD if overlap_thresh is None else overlap_thresh,
        names=names, prices=prices, asof=asof,
    )
    return theme_map, set()


def main() -> int:
    ap = argparse.ArgumentParser(description="聚类口径回归探针")
    ap.add_argument("--mode", choices=["legacy", "current"], default="legacy")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    args_cli = ap.parse_args()

    if args_cli.mode == "legacy":
        P.build_theme_and_conflicts = _legacy_theme_and_conflicts

    argv = [
        "--tag", args_cli.tag or f"probe_cluster_{args_cli.mode}",
        "--rebalance-every", "2",
        "--alpha-weight", "0.5", "--momentum-weight", "0.5",
        "--hold-buffer", "2", "--pool-mode", "e_covered",
        "--entry-ma-window", "20", "--exit-ma-window", "60",
        "--brake-threshold", "2.0", "--prefer-held-in-theme", "0",
        "--out-dir", args_cli.out_dir,
    ]
    sys.argv = ["backtest.py"] + argv
    bt_args = BT.parse_args()
    res = BT.run_backtest(bt_args)

    out = {
        "mode": args_cli.mode,
        "portfolio": res["portfolio_net"],
        "benchmark": res["benchmark_510300"],
        "excess": res["excess_vs_510300"],
        "cost": res["cost_assumption"],
        "rebalance": res["rebalance"],
        "eval_window": res["eval_window"],
        "n_failed_rebalances": res.get("n_failed_rebalances"),
        "n_carried_forward_rebalances": res.get("n_carried_forward_rebalances"),
        "holdings_by_date": {
            r["signal_date"]: [h["code"] for h in r["holdings_detail"]]
            for r in res["rebalance_log"]
        },
    }
    path = Path(args_cli.out_dir) / f"probe_cluster_{args_cli.mode}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "mode": args_cli.mode,
        "annualized": res["portfolio_net"]["annualized_return"],
        "benchmark": res["benchmark_510300"]["annualized_return"],
        "excess": res["excess_vs_510300"]["annualized_return"],
        "mdd": res["portfolio_net"]["max_drawdown"],
        "turnover": res["cost_assumption"]["annualized_one_way_turnover"],
        "out": str(path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
