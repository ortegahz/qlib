"""valid 段的**分年稳定性**检验：哪些降换手参数在 valid 内部就稳？（不碰 test）

【为什么需要这个】
`turnover_sweep.py` 的 14 组 arm 里，**valid 排名第一的 A3 在 test 只有 +6.72%**，
而 **valid 排第 5 的 B3_rebal20 在 test 达 +11.83%** ⇒ valid→test 的参数秩相关很弱，
说明「在 valid 上取 argmax」本身带**选择性偏差**（14 组里挑 1 组，valid 的第一名有运气成分）。

本脚本只做一件事：**在 valid 段内部按自然年（2022 / 2023）拆开**，
看每组 arm 在两个子区间上是否**都还行**（而不是只在某一年好）。
⇒ 用于挑「跨子区间稳健」的参数，**完全不引用 test**，避免二次污染。

【方法】
- 复用 `turnover_sweep.run_arm_backtest`（qlib 真实回测）+ `turnover_analysis` 的抽数函数；
- 收益口径与 manifest 同源：`risk_analysis(report["return"] - report["bench"][- report["cost"]], freq="1day")`；
- 换手：单边 0.5*Σ|Δw|，按年切片统计。

【产物】`<run_dir>/valid_stability.json`（含每 arm × 每子区间的毛/净超额、IR、回撤、换手）

用法（服务器）：
    python manu/experiments/ashare_topk_selection/valid_stability.py \
        --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from turnover_analysis import (  # noqa: E402
    BACKTEST_FREQ_KEY,
    QLIB_DAILY_ANNUALIZATION_DAYS,
    compute_turnover_series,
    extract_trade_flow,
    extract_weight_frame,
    init_qlib,
    load_manifest,
    load_signal,
    md5_of,
)
from turnover_sweep import (  # noqa: E402
    ARM_GRID,
    SELECTION_SEGMENT,
    SEGMENT_SPECS,
    build_valid_signal,
    run_arm_backtest,
)

#: 默认参与稳定性检验的 arm（全部 14 组）
DEFAULT_ARMS: tuple[str, ...] = tuple(spec.arm_id for spec in ARM_GRID)


def _risk(report: pd.DataFrame) -> dict[str, float]:
    """毛/净超额（对基准）+ IR + 回撤，口径与 manifest 一致。"""
    from qlib.contrib.evaluate import risk_analysis

    gross = risk_analysis(report["return"] - report["bench"], freq=BACKTEST_FREQ_KEY)
    net = risk_analysis(report["return"] - report["bench"] - report["cost"], freq=BACKTEST_FREQ_KEY)
    return {
        "gross_annualized": float(gross.loc["annualized_return", "risk"]),
        "net_annualized": float(net.loc["annualized_return", "risk"]),
        "net_ir": float(net.loc["information_ratio", "risk"]),
        "net_max_drawdown": float(net.loc["max_drawdown", "risk"]),
        "n_days": int(len(report)),
    }


def analyze_valid_subperiods(arm_id: str, signal: pd.DataFrame) -> dict[str, Any]:
    """跑 valid 回测并按自然年拆分。"""
    spec = next(s for s in ARM_GRID if s.arm_id == arm_id)
    report, positions, indicator_obj = run_arm_backtest(spec, SELECTION_SEGMENT, signal)

    weight_df = extract_weight_frame(positions)
    turnover = compute_turnover_series(weight_df)
    holdings = (weight_df > 0).sum(axis=1)
    build_mask = holdings > 0
    build_date = pd.Timestamp(build_mask[build_mask].index[0])
    steady_mask = pd.Series(turnover.index > build_date, index=turnover.index) & turnover.notna()

    flow = extract_trade_flow(indicator_obj)
    flow = flow.reindex(report.index).fillna(0.0)

    subperiods: dict[str, Any] = {}
    for year in sorted({ts.year for ts in report.index}):
        mask = report.index.year == year
        sub_report = report[mask]
        if len(sub_report) < 20:
            continue
        sub_turnover = turnover[turnover.index.year == year]
        sub_steady = sub_turnover[steady_mask[steady_mask.index.year == year]]
        sub_flow = flow[flow.index.year == year]
        block = _risk(sub_report)
        block.update(
            {
                "turnover_daily_mean_steady": float(sub_steady.mean()) if len(sub_steady) else None,
                "turnover_annualized_x250": (
                    float(sub_steady.mean() * 250) if len(sub_steady) else None
                ),
                "total_trade_cost": float(sub_flow["trade_cost"].sum()),
                "daily_n_filled_orders_mean": float(sub_flow["n_filled_orders"].mean()),
                "holdings_median": float(holdings[holdings.index.year == year].median()),
            }
        )
        subperiods[str(year)] = block

    nets = [v["net_annualized"] for v in subperiods.values()]
    return {
        "arm_id": arm_id,
        "arm": spec._asdict(),
        "build_date": str(build_date.date()),
        "whole_valid": _risk(report),
        "by_year": subperiods,
        "min_year_net": min(nets) if nets else None,
        "max_year_net": max(nets) if nets else None,
        "spread_year_net": (max(nets) - min(nets)) if nets else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="valid 段分年稳定性检验（不引用 test）")
    parser.add_argument("--run-dir", default=None, help="产物目录 runs/<recorder_id>")
    parser.add_argument("--arms", nargs="*", default=list(DEFAULT_ARMS), help="参与检验的 arm_id")
    parser.add_argument("--out", default=None, help="输出 JSON（默认 <run_dir>/valid_stability.json）")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser() if args.run_dir else Path(C.latest_run_dir())
    if not run_dir.is_dir():
        print(f"❌ run 目录不存在: {run_dir}", file=sys.stderr)
        return 2
    model_path = run_dir / "model.pkl"
    pred_path = run_dir / "pred.pkl"
    if not model_path.is_file():
        print(f"❌ 缺 model.pkl: {model_path}", file=sys.stderr)
        return 2

    init_qlib()
    valid_signal, crosscheck = build_valid_signal(model_path, pred_path)
    print(f"[valid] 信号 {len(valid_signal)} 行；同模型交叉校验 max_abs_diff="
          f"{crosscheck.get('max_abs_diff')}")

    results: dict[str, Any] = {}
    for arm_id in args.arms:
        try:
            results[arm_id] = analyze_valid_subperiods(arm_id, valid_signal)
            yearly = results[arm_id]["by_year"]
            detail = " / ".join(f"{y}:{v['net_annualized']:+.4f}" for y, v in yearly.items())
            print(f"[OK] {arm_id}: {detail}")
        except Exception as exc:  # noqa: BLE001 —— 单组失败不中断
            results[arm_id] = {"arm_id": arm_id, "error": f"{type(exc).__name__}: {exc}"}
            print(f"[FAIL] {arm_id}: {type(exc).__name__}: {exc}")

    payload = {
        "schema_version": "1.0",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "purpose": "只用 valid 段的分年稳定性检验；**不引用 test** ⇒ 用于识别跨子区间稳健的参数",
        "selection_segment": SELECTION_SEGMENT,
        "valid_window": SEGMENT_SPECS[SELECTION_SEGMENT],
        "qlib_annualization_days": QLIB_DAILY_ANNUALIZATION_DAYS,
        "signal_crosscheck": crosscheck,
        "inputs": {
            "model_pkl_md5": md5_of(model_path),
            "pred_pkl_md5": md5_of(pred_path) if pred_path.is_file() else None,
            "manifest_md5": md5_of(run_dir / "manifest.json") if (run_dir / "manifest.json").is_file() else None,
            "script_md5": md5_of(Path(__file__).resolve()),
        },
        "results": results,
    }
    out_path = Path(args.out).expanduser() if args.out else run_dir / "valid_stability.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[out] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
