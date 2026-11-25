#!/usr/bin/env python3
"""🔴 V2 **val 段归因探针**：三步手术（目标文档 2026-11-18）

【问的问题】
  V2 单盲：个股层 RankIC **0.1271**（t=9.04）但 ETF 组合 **−12.93% / 回撤 −54.93%**
  ⇒ Alpha 是在 **``E = W × S`` 聚合**这一步丢的，还是被 **``MA120 / MA80 / cadence=90``**
  绞杀的？

【三步（严格照目标文档）】
  Step 1「纯净信号层验证」：锁 cadence=20，去掉 entry_ma / exit_ma / EWMA 平滑 / TA 否决，
        只用 V2 的 S 分算 E=W×S，每月无脑买 Top-5 等权 ⇒ 同时打印 **ETF 层面 20 日 RankIC**
        + 裸奔年化 / MDD / 换手。
  Step 2「cadence=20 的独立降换手」：**只**在 val 段搜 ``hold_buffer ∈ {2,3,4,5}``
        × ``top_k ∈ {4,5}``（🔴 不搜 90/120/150）。
  Step 3「负分空仓保护」：候选 ETF 的 E 普遍转负 ⇒ 按比例削减权益到现金。

【五条铁律（脚本自证，不靠嘴说）】
  ① **只在 val 段**：``hyperopt.assert_val_only()`` + ``val_window_metrics()`` 双重硬拦
     （窗口不等于 2022-01-01~2023-12-31 ⇒ 直接 raise）⇒ 测试段**不可能**被算。
  ② **不重跑单盲**：不 import ``evaluate``、不写 token / 登记表；
     跑前跑后**比对已封存单盲产物的 md5** ⇒ 被改动就直接 raise。
  ③ **不覆盖正式产物**：产物只写 ``v2/_probe_attribution/``；正式产物一个字都不动。
  ④ **不改仓库代码**：所有变体都在**内存里**（参数 dict）⇒ 仓库与已封存产物保持同代。
  ⑤ **重构自证**：用**冻结参数**在 val 段重建目标权重，把「每个 val 调仓日的选中集合」
     与**已封存** ``blind_diagnostics.csv`` 逐日比对 ⇒ 不一致直接 raise
     （证明本次 ``etf_layer`` 重构是**行为等价**的）。

【用法】
    python manu/tools/v2_val_attribution_probe.py --steps 1,2,3 --out-subdir _probe_attribution
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

V2_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    Path("/tmp/pycharm_project_3b68a403/manu/etf_rotation/v2"),
)
V2_DIR = next(
    (p for p in V2_DIR_CANDIDATES if p is not None and (p / "config_v2.py").is_file()),
    V2_DIR_CANDIDATES[-1],
)
if str(V2_DIR) not in sys.path:
    sys.path.insert(0, str(V2_DIR))

#: 🔴 已封存的产物（探针**不得**触碰 ⇒ 跑前跑后比 md5）
SEALED = (
    "single_blind_test_2024_2026.json",
    "_single_blind_registry.json",
    "frozen_params.json",
    "arm_search_val.json",
    "val_search.json",
    "blind_diagnostics.csv",
)


def _md5(path: Path) -> str:
    if not path.is_file():
        return "<不存在>"
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------- 变体定义
#: 🔴 Step 1 的变体矩阵（**全部锁 cadence=20**，除非名字里写明）
#:    字段含义：
#:      alpha_weight / momentum_weight —— 排序分合成（V2 默认 0.5/0.5；纯净 = 1.0/0.0）
#:      entry_ma_window / exit_ma_window —— 0 ⇒ **不启用**该闸门
#:      smoothing_arm —— None ⇒ **不**做下游 EWMA
#:      theme_cap —— 同族上限
#:      top_k / hold_buffer —— 选股与迟滞带
STEP1_VARIANTS: tuple[dict, ...] = (
    {
        "name": "S1_pure_E_top5_equalweight",
        "desc": "纯净信号：只看 E（W×S），无 MA / 无平滑 / 无缓冲，同族上限关，Top-5 等权",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": None,
        "theme_cap": False, "top_k": 5, "hold_buffer": 0, "cadence": 20,
    },
    {
        "name": "S1_pure_E_top5_themecap",
        "desc": "同上，但**保留同族上限**（现实口径）",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": None,
        "theme_cap": True, "top_k": 5, "hold_buffer": 0, "cadence": 20,
    },
    {
        "name": "S1_E_plus_mom_top5",
        "desc": "去掉 MA / 平滑，但**保留 V2 的 0.5E+0.5动量**合成 ⇒ 隔离「动量项」的影响",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": None,
        "theme_cap": True, "top_k": 5, "hold_buffer": 0, "cadence": 20,
    },
    {
        "name": "S1_v2_scoring_ma_off_c20",
        "desc": "V2 评分（E+动量，平滑臂 A15）但**去掉 MA120/MA80**，cadence=20",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": "A_half_life_15",
        "theme_cap": True, "top_k": 4, "hold_buffer": 0, "cadence": 20,
    },
    {
        "name": "S1_v2_scoring_ma120_80_c20",
        "desc": "V2 评分 + MA120/MA80（冻结口径），cadence=20 ⇒ 只隔离「cadence」的影响",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 120, "exit_ma_window": 80, "smoothing_arm": "A_half_life_15",
        "theme_cap": True, "top_k": 4, "hold_buffer": 0, "cadence": 20,
    },
    {
        "name": "S1_frozen_c90",
        "desc": "**冻结配置原样**（MA120/MA80 + A15 + top4 + buffer0 + cadence=90）⇒ val 段参照系",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 120, "exit_ma_window": 80, "smoothing_arm": "A_half_life_15",
        "theme_cap": True, "top_k": 4, "hold_buffer": 0, "cadence": 90,
    },
)

#: 🔴 Step 2 的**评分配置**（cadence 一律 20）
STEP2_CONFIGS: tuple[dict, ...] = (
    {
        "name": "naked_pure_E",
        "desc": "纯净 E（无 MA / 无平滑）",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": None,
        "theme_cap": True,
    },
    {
        "name": "naked_pure_E_nocap",
        "desc": "纯净 E（无 MA / 无平滑）且**关掉同族上限**（Step 1 证据：同族上限吃掉 ~9pp 超额）",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": None,
        "theme_cap": False,
    },
    {
        "name": "naked_pure_E_nocap_ma60",
        "desc": "纯净 E + 关同族上限，但**保留 MA60/MA60**（V1 默认闸门）⇒ 隔离「MA 闸门」的影响",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 60, "exit_ma_window": 60, "smoothing_arm": None,
        "theme_cap": False,
    },
    {
        "name": "naked_pure_E_nocap_ma120_80",
        "desc": "纯净 E + 关同族上限，但**保留 MA120/MA80**（V2 冻结口径的闸门）",
        "alpha_weight": 1.0, "momentum_weight": 0.0,
        "entry_ma_window": 120, "exit_ma_window": 80, "smoothing_arm": None,
        "theme_cap": False,
    },
    {
        "name": "v2_noMA",
        "desc": "V2 评分（E+动量，A15 平滑），**去掉 MA**",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 0, "exit_ma_window": 0, "smoothing_arm": "A_half_life_15",
        "theme_cap": True,
    },
    {
        "name": "v2_frozen_ma120_80",
        "desc": "V2 冻结评分（E+动量 + A15 + MA120/MA80）",
        "alpha_weight": 0.5, "momentum_weight": 0.5,
        "entry_ma_window": 120, "exit_ma_window": 80, "smoothing_arm": "A_half_life_15",
        "theme_cap": True,
    },
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,2,3", help="要跑的步骤（1/2/3，逗号分隔）")
    ap.add_argument("--out-subdir", default="_probe_attribution")
    ap.add_argument("--out-dir", default="",
                    help="产物目录**覆盖**（默认 = 正式产物根 / --out-subdir；"
                         "host 只读挂载时可用它落到 /tmp 冒烟）")
    ap.add_argument("--model", default="ablate_roc5", help="S 层模型名（消融胜者）")
    ap.add_argument("--topk-grid", default="4,5")
    ap.add_argument("--buffer-grid", default="2,3,4,5")
    ap.add_argument("--gate-scale-grid", default="0.02,0.05,0.10,0.20")
    ap.add_argument("--gate-min-exposure-grid", default="0.0,0.3")
    ap.add_argument("--gate-base-config", default="v2_frozen_ma120_80",
                    help="Step 3 用哪个评分配置做闸门扫描")
    ap.add_argument("--gate-top-k", type=int, default=0,
                    help="Step 3 基准配置的 top_k（0 = 从 Step 2 结果里挑）")
    ap.add_argument("--gate-hold-buffer", type=int, default=0,
                    help="Step 3 基准配置的 hold_buffer（0 = 从 Step 2 结果里挑）")
    ap.add_argument("--step2-configs", default="",
                    help="Step 2 只跑这些评分配置（逗号分隔；留空 = 全跑）")
    ap.add_argument("--variant-filter", default="",
                    help="只跑名字里含该子串的变体（冒烟用；留空 = 全跑）")
    ap.add_argument("--skip-regression", action="store_true",
                    help="跳过「重构自证」（快速冒烟用；正式跑**不许**跳）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config_v2 as C
    import hyperopt as HO

    steps = {s.strip() for s in args.steps.split(",") if s.strip()}
    topks = [int(x) for x in args.topk_grid.split(",") if x.strip()]
    buffers = [int(x) for x in args.buffer_grid.split(",") if x.strip()]
    scales = [float(x) for x in args.gate_scale_grid.split(",") if x.strip()]
    min_exps = [float(x) for x in args.gate_min_exposure_grid.split(",") if x.strip()]

    # 🔴 铁律①：硬拦 —— 搜索窗口必须**完全落在** val 段
    HO.assert_val_only(str(C.VALID[0]), str(C.VALID[1]))

    v2_root = Path(C.DEFAULT_V2_RUN_ROOT)
    probe_dir = Path(args.out_dir) if args.out_dir else (v2_root / args.out_subdir)
    _log(f"正式产物根：{v2_root}")
    _log(f"探针产物根：{probe_dir}")
    _log(f"val 窗口 = {list(C.VALID)}（assert_val_only 硬拦 ⇒ 测试段不可能被算）")
    if args.dry_run:
        _log("--dry-run ⇒ 未开跑")
        return 0

    before = {name: _md5(v2_root / name) for name in SEALED}

    import numpy as np
    import pandas as pd

    import attribution as AT
    import etf_layer as EL
    import run_v2 as R

    probe_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- 取数
    _log("=== 取数：ETF 行情 / 成分股权重 / S 层打分 ===")
    close, amount, names = R.load_market_data()
    bench = R._benchmark_nav(close)
    scores_csv = v2_root / f"stock_scores_v2_{args.model}.csv"
    if not scores_csv.is_file():
        raise SystemExit(f"🔴 找不到 S 层打分文件：{scores_csv}")
    scores = pd.read_csv(scores_csv, dtype={"instrument": str})
    scores["date"] = pd.to_datetime(scores["date"])
    score_dates = sorted(pd.to_datetime(scores["date"].unique()).tolist())
    val_score_dates = [d for d in score_dates
                       if pd.Timestamp(C.VALID[0]) <= d <= pd.Timestamp(C.VALID[1])]
    _n0 = len(val_score_dates)
    val_score_dates = R.val_score_dates_without_label_leak(val_score_dates)
    _log(f"val 信号日 {_n0} → {len(val_score_dates)}（标签窗口泄露裁剪后）")

    import constituents as CON
    holdings = CON.snapshot_frame(CON.load_holdings())
    weights = EL.build_weight_map(holdings)
    _log(f"成分股 ETF 数 = {len(weights)}｜ETF 行情面板 = {close.shape}")

    # ---- E 面板（**所有信号日**，用于 ETF 层 20 日 RankIC）----
    e_panel, _meta = EL.compute_alpha_panel(
        scores, weights, val_score_dates, names=names, codes=close.columns)
    fwd20 = AT.etf_forward_return_panel(close, 20)
    ic_e_raw = AT.etf_rank_ic(e_panel, fwd20)
    _log(f"ETF 层 20 日 RankIC（原始 E，全部 val 信号日）= {ic_e_raw}")

    # ---- 平滑臂 E 面板（按调仓日 asof 平滑）----
    import smoothing as SM

    def smoothed_panel_for(arm_id: str, days: list[pd.Timestamp]) -> pd.DataFrame:
        arm = SM.resolve_arm(arm_id)
        if arm is None or not arm.enabled:
            return e_panel.reindex(days)
        axis = [pd.Timestamp(d).normalize() for d in close.index]
        rows = {}
        for day in days:
            res = SM.apply_spec(e_panel, axis, arm, asof=day)
            if len(res.panel) == 0:
                continue
            rows[day] = res.panel.iloc[-1]
        if not rows:
            return pd.DataFrame(index=pd.DatetimeIndex(days), columns=e_panel.columns, dtype=float)
        return pd.DataFrame(rows).T.reindex(columns=e_panel.columns)

    # ----------------------------------------------------------------- 通用：跑一个变体
    def run_variant(cfg: dict, params: dict | None = None) -> dict:
        """按配置在 val 段跑一次（建目标 → 回测 → 指标 + ETF 层 RankIC + 拆解）。"""
        p = {
            "_model_name": args.model,
            "alpha_weight": cfg["alpha_weight"],
            "momentum_weight": cfg["momentum_weight"],
            "entry_ma_window": cfg["entry_ma_window"],
            "exit_ma_window": cfg["exit_ma_window"],
            "smoothing_arm": cfg.get("smoothing_arm"),
            "top_k": int(cfg["top_k"]),
            "hold_buffer": int(cfg["hold_buffer"]),
            "rebalance_every_n_days": int(cfg["cadence"]),
            "allow_theme_cap": bool(cfg.get("theme_cap", True)),
        }
        p.update(params or {})
        ns = argparse.Namespace()
        targets, diag = R._build_targets_for(ns, v2_root, p, close, amount, names, val_score_dates)
        bt = R._backtest(close, targets, p)
        m = R.val_window_metrics(
            bt["nav"], bt.get("turnover_by_date"), bench, window=(str(C.VALID[0]), str(C.VALID[1])),
            commission=float(C.COMMISSION), slippage=float(C.SLIPPAGE), periods_per_year=250,
        )
        rebal_days = [pd.Timestamp(d) for d in sorted(targets.keys())]
        # 排序分（final_score）在**调仓日**上的面板 ⇒ ETF 层 RankIC（决策口径）
        rank_panel = pd.DataFrame(
            {pd.Timestamp(d): g.set_index("code")["final_score"]
             for d, g in diag.groupby("date")},
        ).T
        rank_panel.index = pd.DatetimeIndex(rank_panel.index)
        ic_rank = AT.etf_rank_ic(rank_panel, fwd20)
        dec = AT.rebalance_decomposition(diag, fwd20, 20)
        pool_stats = AT.pool_score_stats(diag)
        return {
            "name": cfg.get("name", "custom"),
            "desc": cfg.get("desc", ""),
            "params": {k: v for k, v in p.items() if k != "_model_name"},
            "metrics": {
                "annualized_return": m.get("annualized_return"),
                "max_drawdown": m.get("max_drawdown"),
                "annualized_turnover": m.get("annualized_turnover"),
                # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                "annualized_turnover_one_way": C.one_way_turnover(m.get("annualized_turnover")),
                "total_turnover": m.get("total_turnover"),
                "excess_vs_benchmark": m.get("excess_vs_benchmark"),
                "sharpe": m.get("sharpe"),
                "calmar": m.get("calmar"),
            },
            "n_rebalances": int(len(targets)),
            "rank_ic_etf_rank_score": ic_rank,
            "pool_stats": pool_stats.to_dict(orient="records"),
            "decomposition": dec.to_dict(orient="records"),
            "_targets": targets,
            "_diag": diag,
        }

    # ----------------------------------------------------------------- 重构自证（铁律⑤）
    frozen_path = v2_root / "frozen_params.json"
    if args.skip_regression:
        _log("⚠️ --skip-regression ⇒ 跳过重构自证（仅冒烟；正式跑不许跳）")
    elif frozen_path.is_file():
        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        fp = dict(frozen.get("params") or {})
        fp["_model_name"] = args.model
        fp.setdefault("rebalance_every_n_days", frozen.get("rebalance_every_n_days", 90))
        _log(f"=== 重构自证：用冻结参数在 val 段重建目标权重（cadence={fp['rebalance_every_n_days']}）===")
        ns = argparse.Namespace()
        tg, dg = R._build_targets_for(ns, v2_root, fp, close, amount, names, val_score_dates)
        sealed_csv = v2_root / "blind_diagnostics.csv"
        if sealed_csv.is_file():
            sd = pd.read_csv(sealed_csv, dtype={"code": str})
            sd["date"] = sd["date"].astype(str)
            sd_val = sd[(sd["date"] >= str(C.VALID[0])) & (sd["date"] <= str(C.VALID[1]))]
            ok = True
            for day, grp in sd_val.groupby("date"):
                want = sorted(grp[grp["selected"].astype(bool)]["code"].astype(str).tolist())
                got = sorted(dg[(dg["date"].astype(str) == day) &
                                (dg["selected"].astype(bool))]["code"].astype(str).tolist())
                flag = "✅" if want == got else "🔴"
                if want != got:
                    ok = False
                _log(f"  {flag} {day}｜封存={want}｜重建={got}")
            if not ok:
                raise SystemExit("🔴🔴 重构自证失败：重建结果与已封存 blind_diagnostics 不一致")
            _log("  ✅ 重构自证通过（val 段每个调仓日的选中集合与已封存产物逐日一致）")
        else:
            _log("  ⚠️ 未找到 blind_diagnostics.csv ⇒ 跳过重构自证")

    results: dict[str, dict] = {}

    # ----------------------------------------------------------------- Step 1
    if "1" in steps:
        _log("=== Step 1：纯净信号层验证（cadence=20 锁死）===")
        rows = []
        variants = [c for c in STEP1_VARIANTS
                    if (not args.variant_filter) or (args.variant_filter in c["name"])]
        if not variants:
            raise SystemExit(f"🔴 没有变体匹配 {args.variant_filter!r}")
        for cfg in variants:
            _log(f"--- 变体 {cfg['name']}：{cfg['desc']}")
            r = run_variant(cfg)
            results[cfg["name"]] = r
            m = r["metrics"]
            _log(f"    年化 {m['annualized_return']}｜回撤 {m['max_drawdown']}｜"
                 f"换手 {m['annualized_turnover']}｜超额 {m['excess_vs_benchmark']}｜"
                 f"调仓 {r['n_rebalances']} 次｜ETF 层 RankIC(排序分) "
                 f"{r['rank_ic_etf_rank_score'].get('ic')}")
            rows.append({
                "variant": cfg["name"], "desc": cfg["desc"],
                "cadence": cfg["cadence"], "top_k": cfg["top_k"],
                "hold_buffer": cfg["hold_buffer"],
                "alpha_w": cfg["alpha_weight"], "mom_w": cfg["momentum_weight"],
                "entry_ma": cfg["entry_ma_window"], "exit_ma": cfg["exit_ma_window"],
                "smoothing_arm": cfg.get("smoothing_arm") or "none",
                "theme_cap": cfg.get("theme_cap", True),
                "annualized_return": m["annualized_return"],
                "max_drawdown": m["max_drawdown"],
                "annualized_turnover": m["annualized_turnover"],
                # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                "annualized_turnover_one_way": C.one_way_turnover(m["annualized_turnover"]),
                "excess_vs_benchmark": m["excess_vs_benchmark"],
                "sharpe": m["sharpe"],
                "n_rebalances": r["n_rebalances"],
                "etf_rank_ic_rank_score": r["rank_ic_etf_rank_score"].get("ic"),
                "etf_rank_ic_n": r["rank_ic_etf_rank_score"].get("n_cross_sections"),
                "mean_excess_vs_pool": AT.summarize_decomposition(
                    pd.DataFrame(r["decomposition"])).get("mean_excess"),
                "hit_rate_excess_gt0": AT.summarize_decomposition(
                    pd.DataFrame(r["decomposition"])).get("hit_rate_excess_gt0"),
            })
        _write_csv(probe_dir / "step1_variants.csv", rows)
        (probe_dir / "step1_variants.json").write_text(
            json.dumps({
                "step": 1,
                "window": list(C.VALID),
                "etf_rank_ic_raw_E_all_signal_days": ic_e_raw,
                "variants": [{k: v for k, v in r.items()
                              if not k.startswith("_")} for r in results.values()],
            }, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    # ----------------------------------------------------------------- Step 2
    if "2" in steps:
        _log("=== Step 2：cadence=20 的独立降换手测试（只搜 hold_buffer × top_k）===")
        rows = []
        step2_cfgs = [c for c in STEP2_CONFIGS
                      if (not args.step2_configs) or (c["name"] in args.step2_configs.split(","))]
        if not step2_cfgs:
            raise SystemExit(f"🔴 没有 Step 2 配置匹配 {args.step2_configs!r}")
        for cfg in step2_cfgs:
            for tk in topks:
                for bf in buffers:
                    c = dict(cfg)
                    c.update({"name": f"{cfg['name']}_k{tk}_b{bf}", "top_k": tk,
                              "hold_buffer": bf, "cadence": 20})
                    r = run_variant(c)
                    m = r["metrics"]
                    rows.append({
                        "config": cfg["name"], "top_k": tk, "hold_buffer": bf,
                        "cadence": 20, "annualized_return": m["annualized_return"],
                        "max_drawdown": m["max_drawdown"],
                        "annualized_turnover": m["annualized_turnover"],
                        # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                        "annualized_turnover_one_way": C.one_way_turnover(m["annualized_turnover"]),
                        "excess_vs_benchmark": m["excess_vs_benchmark"],
                        "sharpe": m["sharpe"], "n_rebalances": r["n_rebalances"],
                        "etf_rank_ic_rank_score": r["rank_ic_etf_rank_score"].get("ic"),
                        "mean_excess_vs_pool": AT.summarize_decomposition(
                            pd.DataFrame(r["decomposition"])).get("mean_excess"),
                        # 🔴 `turnover_ok` **只是披露标记**（是否达到换手披露基准），
                        #    **不是过滤器**：它不筛掉任何候选、也不构成拦截
                        #    （2026-11-18 起换手目标只作披露基准）；
                        #    Step 3 挑基准配置时**不参与选参**（见下方 Step 3 注释）。
                        "turnover_ok": bool((m["annualized_turnover"] or 1e9)
                                            <= float(C.TURNOVER_TARGET)),
                    })
                    _log(f"  {cfg['name']} k={tk} b={bf}｜年化 {m['annualized_return']}｜"
                         f"换手 {m['annualized_turnover']}｜超额 {m['excess_vs_benchmark']}｜"
                         f"回撤 {m['max_drawdown']}")
        _write_csv(probe_dir / "step2_cadence20_grid.csv", rows)
        (probe_dir / "step2_cadence20_grid.json").write_text(
            json.dumps({"step": 2, "window": list(C.VALID),
                        "turnover_target": float(C.TURNOVER_TARGET),
                        "grid": {"top_k": topks, "hold_buffer": buffers},
                        "rows": rows}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    # ----------------------------------------------------------------- Step 3
    if "3" in steps:
        _log("=== Step 3：负分空仓保护（E 普遍转负 ⇒ 按比例削减权益到现金）===")
        base = next((c for c in STEP2_CONFIGS if c["name"] == args.gate_base_config),
                    STEP2_CONFIGS[-1])
        # 基准配置的目标只数 / 缓冲带：用 Step 2 里**该配置**下超额最好的那一档
        # （🔴 2026-11-18 铁律：换手**只作披露，不参与选参** ⇒ 不按 `turnover_ok` 过滤）
        base_k, base_b = 4, 2
        if args.gate_top_k > 0:
            base_k = int(args.gate_top_k)
        if args.gate_hold_buffer > 0:
            base_b = int(args.gate_hold_buffer)
        if "2" in steps and not (args.gate_top_k > 0 or args.gate_hold_buffer > 0):
            # 🔴 2026-11-18 铁律：**换手只作披露，不参与选参** ⇒ 这里**不再**用
            #    ``turnover_ok`` 做前置过滤（它是披露标记，不是红线）；
            #    改为在**全部** ``config == base["name"]`` 的行里按超额取最大。
            #    （``turnover_ok`` 仍原样落 CSV 作**披露标记**；数值计算一字未改。）
            cand = [r for r in rows if r["config"] == base["name"]]
            if cand:
                best = max(cand, key=lambda r: (r["excess_vs_benchmark"] or -9e9))
                base_k, base_b = int(best["top_k"]), int(best["hold_buffer"])
        _log(f"  基准评分配置 = {base['name']}｜top_k={base_k}｜hold_buffer={base_b}")
        rows = []
        cfg = dict(base)
        cfg.update({"name": f"{base['name']}_nogate", "top_k": base_k,
                    "hold_buffer": base_b, "cadence": 20})
        base_res = run_variant(cfg)
        rows.append({
            "config": base["name"], "gate": "OFF", "scale": None, "min_exposure": None,
            "annualized_return": base_res["metrics"]["annualized_return"],
            "max_drawdown": base_res["metrics"]["max_drawdown"],
            "annualized_turnover": base_res["metrics"]["annualized_turnover"],
            # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
            "annualized_turnover_one_way": C.one_way_turnover(
                base_res["metrics"]["annualized_turnover"]),
            "excess_vs_benchmark": base_res["metrics"]["excess_vs_benchmark"],
            "sharpe": base_res["metrics"]["sharpe"], "n_rebalances": base_res["n_rebalances"],
        })
        for sc in scales:
            for me in min_exps:
                c = dict(cfg)
                c["name"] = f"{base['name']}_gate_s{sc}_m{me}"
                r = run_variant(c, params={"neg_score_gate": True,
                                           "neg_score_gate_scale": sc,
                                           "neg_score_gate_min_exposure": me})
                m = r["metrics"]
                rows.append({
                    "config": base["name"], "gate": "ON", "scale": sc, "min_exposure": me,
                    "annualized_return": m["annualized_return"],
                    "max_drawdown": m["max_drawdown"],
                    "annualized_turnover": m["annualized_turnover"],
                    # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
                    "annualized_turnover_one_way": C.one_way_turnover(m["annualized_turnover"]),
                    "excess_vs_benchmark": m["excess_vs_benchmark"],
                    "sharpe": m["sharpe"], "n_rebalances": r["n_rebalances"],
                })
                _log(f"  gate scale={sc} min_exp={me}｜年化 {m['annualized_return']}｜"
                     f"回撤 {m['max_drawdown']}｜超额 {m['excess_vs_benchmark']}")
        _write_csv(probe_dir / "step3_neg_score_gate.csv", rows)
        (probe_dir / "step3_neg_score_gate.json").write_text(
            json.dumps({"step": 3, "window": list(C.VALID), "base_config": base["name"],
                        "base_top_k": base_k, "base_hold_buffer": base_b,
                        "rows": rows}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    # ----------------------------------------------------------------- 收尾：封存产物自证
    after = {name: _md5(v2_root / name) for name in SEALED}
    changed = [n for n in SEALED if before[n] != after[n]]
    if changed:
        print(f"🔴🔴 已封存产物被改动：{changed} ⇒ 本探针**不合格**")
        return 1
    print("[OK] 🔴 已封存产物 md5 全部未变（单盲 / 冻结 / 正式搜索 / 单盲诊断）")
    print(f"[OK] 探针产物目录：{probe_dir}")
    return 0


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


if __name__ == "__main__":
    raise SystemExit(main())
