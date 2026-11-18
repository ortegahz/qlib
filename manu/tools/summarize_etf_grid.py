"""把 ETF 轮动回测网格汇总成 Markdown 表（memory 用）。

用法（服务器）：
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/summarize_etf_grid.py \
        --dir <落点根>/runs --pattern "etf_rotation_backtest_*.json"

输出：每个场景一行（年化 / 基准 / 超额 / Sharpe / MDD / 换手 / 成本 / 刹车事件 / H1·H2 分段）。
口径提醒：超额为**相对口径（几何）**；(1+r_p)/(1+r_b)-1 逐日累计后年化。
"""
from __future__ import annotations

import math
import argparse
import glob
import json
import os
import sys
from pathlib import Path

#: 🔴 单边换手换算的**唯一实现**（``config_v2.one_way_turnover``）——不许手写 0.5。
_V2_DIR = Path(__file__).resolve().parents[1] / "etf_rotation" / "v2"
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))
import config_v2 as C  # noqa: E402


#: 🔴 **结果落点根**：单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT` ⇒ 本工具按**同一个环境变量**取值（两边不会分叉）。
_RESULTS_ROOT = os.environ.get(
    "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
)
#: 产物根缺省值（= 结果根；与 `daily_config.RESEARCH_RUN_ROOT` 同口径）
_DEFAULT_RUN_ROOT = _RESULTS_ROOT


def _f(x):
    return float(x) if isinstance(x, (int, float)) else float("nan")


def _num(x, spec: str) -> str:
    """🔴 2026-11-18 续四（reviewer NIT-4）：**缺失 / NaN 渲染成「—」**，
    不许把字面量 ``nan%`` / ``nan`` 写进 markdown 表格。

    🔴 换手改成「双边算不出来 ⇒ 单边也是 NaN」之后，缺失值会以 NaN 形态流到渲染层
    ⇒ 必须在这里收口（与 `v2/report.py::_fmt` 同约定）。
    """
    try:
        value = float(x)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value):
        return "—"
    return format(value, spec)


def _two_way(cost: dict) -> float | None:
    """年化**双边**换手（Σ|Δw| / 年）——**双边键优先 → 旧键兜底 → 都没有 ⇒ None**。

    🔴 旧键 ``annualized_one_way_turnover`` 的**值是双边**（历史错标：键名说单边）。
    """
    two = cost.get("annualized_turnover_two_way")
    if two is None:
        two = cost.get("annualized_one_way_turnover")   # 旧键兜底（原始产物兼容保留）
    return two


def _one_way(cost: dict) -> float | None:
    """年化**单边**换手（= 0.5 × 双边）——**唯一实现** ``config_v2.one_way_turnover``。

    🔴 **不许手写 ``* 0.5``**；**缺失 ⇒ None**（🔴 不许当 0.0：缺失 ≠ 换手为零，
    与 ``config_v2.one_way_turnover`` 的 NaN 语义一致）。
    """
    one = cost.get("annualized_turnover_one_way")
    if one is None:
        two = _two_way(cost)
        one = C.one_way_turnover(two) if two is not None else None
    return one


def collect(directory: str, pattern: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(directory, pattern))):
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        seg = d.get("segment_report", {})
        h1 = seg.get("H1_to_2025-06-30", {})
        h2 = seg.get("H2_from_2025-06-30", {})
        brake = d.get("brake", {})
        cost = d.get("cost_assumption") or {}
        rows.append({
            "tag": d.get("scenario", ""),
            "pool": d.get("pool_mode", ""),
            "ann": _f(d["portfolio_net"].get("annualized_return")),
            "bench": _f(d["benchmark_510300"].get("annualized_return")),
            "exc": _f(d["excess_vs_510300"].get("annualized_return")),
            "exc_arith": _f((d.get("excess_arith_vs_510300") or {}).get("annualized_return")),
            "sharpe": _f(d["excess_vs_510300"].get("sharpe")),
            "mdd": _f(d["portfolio_net"].get("max_drawdown")),
            # 🔴 2026-11-18 口径正名：``annualized_one_way_turnover`` 的**值是双边**
            #    （Σ|Δw| / 年）⇒ 新读法一律用带口径后缀的键；单边**只走唯一实现**
            #    ``config_v2.one_way_turnover``（🔴 不许手写 ``* 0.5``）。
            "turnover_two_way": _f(_two_way(cost)),
            "turnover_one_way": _f(_one_way(cost)),
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
            "换手×(双边)", "换手×(单边)", "成本", "期数", "刹车", "期末回撤",
            "H1超额", "H2超额", "失败")
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows:
        out.append("| " + " | ".join([
            r["tag"], r["pool"] or "-",
            _num(r["ann"], ".2%"), _num(r["bench"], ".2%"), _num(r["exc"], ".2%"),
            _num(r["exc_arith"], ".2%"),
            _num(r["sharpe"], ".3f"), _num(r["mdd"], ".1%"),
            _num(r["turnover_two_way"], ".1f"), _num(r["turnover_one_way"], ".1f"),
            _num(r["drag"], ".2%"),
            f"{r['nreb_win']}/{r['nreb']}", str(r["brake_events"]), _num(r["final_dd"], ".1%"),
            f"{_num(r['h1'], '.2%')}({r['h1n']})", f"{_num(r['h2'], '.2%')}({r['h2n']})",
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
