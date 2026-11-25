#!/usr/bin/env python
"""🔴 V2 ETF 层 **MA 窗口**的 val 段二次扫（用户待办 #10：ETF 层超参全部迁回 val 搜）。

背景：V1 的 ETF 层网格（含 MA 窗口 / 再平衡频率 / Top-K / 缓冲带 / EWMA 开关）跑在
**test 区间**上（`analyze.py:2002/2035` 自认"样本内最优"）⇒ V2 必须把这些超参**全部**
搬回 val(2022-01-01 ~ 2023-12-31) 段搜。

本脚本做**第二轮**：在第一轮（臂 × Top-K × 缓冲带，产物 `val_search.json`）选出的
最优臂/Top-K/缓冲带**固定**的前提下，再在 val 段扫 MA 窗口（进/出），
产出 `val_ma_sweep.json` ⇒ 由 `run_v2.py --stage freeze` 一并冻结。

红线：
  * 搜索窗口**必须**是 val 段（`hyperopt.assert_val_only` 物理拦截）；
  * 🔴 口径变更（2026-11-17）：**调仓节奏（cadence）已进入 val 段搜索空间**
    （用户红线「换手 16.57× → ≤4×」在 cadence=20 的折叠常量下不可达）⇒ 本脚本
    **固定第一轮选出的 cadence**（`fixed_from_round1.rebalance_every_n_days`），
    只扫 MA 窗口；cadence=20 与 V1 同口径，cadence≠20 与 V1 **不可直接对比**；
  * 产物落 `<V2 产物根>/val_ma_sweep.json`。

用法：
  /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/v2_val_ma_sweep.py \\
      [--v2-root /mnt/data/siping/runs/etf_rotation_results/v2] \\
      [--ma-grid 40,60,80] [--out <落点>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJ = Path("/tmp/pycharm_project_3b68a403")
V2 = PROJ / "manu" / "etf_rotation" / "v2"
sys.path.insert(0, str(V2))


def resolve_cadence(base: dict) -> int:
    """🔴 从**第一轮 val 段搜索产物**解析调仓节奏（cadence）。

    🔴 **不许静默回落常量**（旧口径是
    ``base.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)``）
    ⇒ 搜索产物是「cadence 进入搜索空间之前」的旧产物时会**静默写 20**
    ⇒ 与 ``run_v2.py:stage_freeze`` 的同源校验（cadence 不同源 ⇒ 硬报错）**对不上**
    （``fixed_from_round1`` 拿到的是回落值 20 ⇒ 校验形同虚设）
    ⇒ 会把「cadence=20 上下文里扫出来的 MA 窗口」与「别的 cadence 下选出的其余参数」
    一起冻进冻结产物 ⇒ 冻结参数**内部不自洽** ⇒ 白烧掉一次性单盲 token。
    ⇒ 这里改成**硬报错**（fail-closed）。
    """
    if "rebalance_every_n_days" not in base:
        raise RuntimeError(
            "🔴 第一轮 val 段搜索产物 `val_search.json` 的 `best_params` 里**没有** "
            "`rebalance_every_n_days`（cadence）⇒ **不许静默回落到常量**"
            "（口径漂移 ⇒ MA 窗口与 cadence 不同源 ⇒ 冻结参数不自洽 ⇒ 白烧单盲 token）"
            " ⇒ 请重跑 `run_v2.py --stage search`（val 段搜索空间已含 cadence 维度）"
        )
    return int(base["rebalance_every_n_days"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v2-root", default="/mnt/data/siping/runs/etf_rotation_results/v2")
    ap.add_argument("--ma-grid", default="40,60,80")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    out_dir = Path(args.v2_root)
    import config_v2 as C
    import backtest_v2 as BV
    import etf_layer as EL
    import hyperopt as HO
    import metrics as ME
    import run_v2 as RV

    search = json.loads((out_dir / "val_search.json").read_text(encoding="utf-8"))
    base = dict(search["best_params"])
    model_name = base.get("_model_name") or search.get("model_name")
    base["_model_name"] = model_name
    # 🔴 fail-closed：cadence 缺失 ⇒ 硬报错（不许静默回落常量 20，见 resolve_cadence 注释）
    cadence = resolve_cadence(base)
    base["rebalance_every_n_days"] = cadence

    ma_grid = [int(x) for x in args.ma_grid.split(",") if x.strip()]
    close, amount, names = RV.load_market_data()
    bench = RV._benchmark_nav(close)
    score_dates = sorted(
        __import__("pandas").to_datetime(
            __import__("pandas").read_csv(
                out_dir / f"stock_scores_v2_{model_name}.csv",
                dtype={"instrument": str}, usecols=["date"],
            )["date"].unique()
        ).tolist()
    )
    val_score_dates = [d for d in score_dates if C.VALID[0] <= str(d.date()) <= C.VALID[1]]
    # 🔴🔴 裁掉「标签窗口伸进测试段」的 val 截面（2026-11-17 21:20 审计发现）
    val_score_dates = RV.val_score_dates_without_label_leak(val_score_dates)
    window = RV.val_search_window()          # 🔴 内含 assert_val_only（越界即 raise）

    def objective(params: dict, win: tuple[str, str]) -> dict:
        p = dict(base)
        p.update(params)
        targets, _ = RV._build_targets_for(None, out_dir, p, close, amount, names, val_score_dates)
        if not targets:
            return {"score": float("-inf")}
        # 🔴 接入 ETF 层真实流动性约束（可买 / 可卖两套映射）
        buy_map, sell_map = RV.build_etf_tradable_maps()
        bt = BV.run_backtest_v2(close, targets,
                                commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE),
                                tradable=buy_map, tradable_sell=sell_map,
                                periods_per_year=250)
        # 🔴🔴 指标**只**在 val 窗口内算（旧实现用整段历史净值 ⇒ 混进测试段收益）
        m = RV.val_window_metrics(bt["nav"], bt.get("turnover_by_date"), bench,
                                  window=win, commission=float(C.COMMISSION),
                                  slippage=float(C.SLIPPAGE), periods_per_year=250)
        ann = m.get("annualized_return")
        exc = m.get("excess_vs_benchmark")
        excess = float(exc) if exc is not None else (float(ann) if ann is not None else 0.0)
        turn = float(m.get("annualized_turnover") or 0.0)
        # 🔴 2026-11-18：换手惩罚**必须有上限** —— 唯一实现 `config_v2.turnover_penalty`。
        #    本地旧口径（系数 0.05、目标 4.0、**无上限**）在 cadence=20 下恒 ≈ 0.416
        #    ⇒ 把超额（~0.1）完全盖住 ⇒ trial 的 `score` 被换手惩罚主导
        #    ⇒ 闸门改成「只披露」也救不了（winner 仍按被污染的 score 挑）。
        penalty = C.turnover_penalty(turn, float(C.TURNOVER_TARGET))
        return {
            "score": float(excess - penalty),
            "annualized_return": ann,
            "excess_vs_benchmark": exc,
            "max_drawdown": m.get("max_drawdown"),
            "annualized_turnover": turn,
            # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
            "annualized_turnover_one_way": C.one_way_turnover(turn),
            "total_turnover": m.get("total_turnover"),
            "entry_ma_window": params.get("entry_ma_window"),
            "exit_ma_window": params.get("exit_ma_window"),
            # 🔴 每轮 trial 都记录 cadence 与调仓次数（披露要求）
            "rebalance_every_n_days": int(base.get("rebalance_every_n_days",
                                                   cadence)),
            # 🔴 2026-11-17 夜班审计补：第二轮 trial 的元数据字段要与**第一轮齐**
            #    （第一轮有 `rebalance_signal_stride`，第二轮原本缺 ⇒ 字段不齐）。
            "rebalance_signal_stride": int(C.rebalance_signal_stride_for(
                int(cadence)
            )),
            "n_rebalances": len(targets),
        }

    grid = {"entry_ma_window": ma_grid, "exit_ma_window": ma_grid}
    RV.assert_no_rebalance_keys(grid)
    print(f"[INFO] MA 窗口二次扫（val {window[0]} ~ {window[1]}）：{grid}", flush=True)
    result = HO.search(objective, grid, val_window=window)
    # 🔴 换手闸门 / 披露基准（与第一轮同口径；mode 由 config_v2 统一决定 ⇒ 显式传）
    result = RV.apply_turnover_gate(
        result, float(C.TURNOVER_TARGET), mode=C.TURNOVER_TARGET_MODE
    )
    print(f"[INFO] 换手闸门：{result['gate']['message']}", flush=True)
    best = dict(result["best_params"])
    payload = {
        "searched_on": "val",
        "val_window": list(window),
        "grid": grid,
        "fixed_from_round1": {k: base.get(k) for k in
                              ("smoothing_arm", "top_k", "hold_buffer", "alpha_weight",
                               "momentum_weight", "rebalance_every_n_days", "_model_name")},
        "rebalance_every_n_days": int(base.get("rebalance_every_n_days",
                                                cadence)),
        "rebalance_signal_stride": int(C.rebalance_signal_stride_for(
            int(cadence)
        )),
        # 🔴 逐档统计来自**第一轮** val 段搜索（本轮只扫 MA 窗口，cadence 已固定）
        "cadence_disclosure": {
            **dict(search.get("cadence_disclosure") or {}),
            "round2_fixed_cadence": int(base.get("rebalance_every_n_days",
                                                 cadence)),
            "round2_note": ("🔴 本轮（MA 二次扫）的 cadence **固定**为第一轮 val 段胜出值；"
                            "逐档统计（per_cadence）来自第一轮搜索产物。"),
        },
        "objective": C.OBJECTIVE_DESCRIPTION,
        "best_params": best,
        "best_score": result["best_score"],
        "n_trials": result["n_trials_run"],
        "trials": result["trials"],
        "note": ("🔴 第二轮只扫 MA 窗口（进/出），其余参数固定为第一轮 val 段胜出值；"
                 "搜索窗口由 `hyperopt.assert_val_only` 物理隔离；"
                 "🔴 调仓节奏（cadence）由第一轮 val 段搜索选出并**固定**；"
                 "cadence≠20 与 V1 的 16.57× **不是同一口径**（不可直接对比）；"
                 "cadence=20 也只是**节奏**与 V1 相同，**换手权重基准仍不同源**"
                 "（V1 用上期**目标**权重未漂移／V2 用调仓前**漂移**权重）"
                 "⇒ 只趋势性对比；测试段从未参与。"),
    }
    out = Path(args.out) if args.out else (out_dir / "val_ma_sweep.json")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    print(f"[OK] MA 窗口二次扫已写出：{out}", flush=True)
    print(f"[OK] 最优：entry_ma={best.get('entry_ma_window')} exit_ma={best.get('exit_ma_window')} "
          f"score={result['best_score']:.6f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
