#!/usr/bin/env python3
"""🔴 V2 **纯 val 段**探针：问一个问题 ——「年化换手 ≤4× 在 val 段到底可达吗？」

【为什么需要这个探针】
  夜班正式搜索的结论：**240 个 trial 全部 > 4×**（最低 5.305×，cadence=90）⇒ 红灯线
  「换手 16.57× → ≤4×」**未达成**。但那轮搜索的 cadence 候选只有 {20,40,60,90}，
  且 Rank Buffer 只到 4 ⇒ **「再放缓节奏 / 再放宽缓冲带」这一格根本没被搜过**。
  ⇒ 本探针把这两条杠杆**再推开一格**，看看 4× 是不是**结构性不可达**。

【四条铁律（脚本自证，不靠嘴说）】
  ① **只在 val 段跑**：搜索器 `hyperopt.search()` → `assert_val_only()` 在**任何 objective
     调用之前**硬拦（窗口不落在 val 段 ⇒ 直接 raise）⇒ 测试段**不可能**被算。
  ② **不重跑单盲**：本脚本**不 import** `evaluate`、**不写**任何 token / 登记表路径；
     并在跑前跑后**比对已封存单盲产物的 md5** ⇒ 被改动就直接 raise。
  ③ **不覆盖正式产物**：产物写到**独立子目录**（默认 `v2/_probe_cadence/`），
     正式产物 `v2/arm_search_val.json` / `v2/val_search.json` / `frozen_params.json`
     **一个字都不会动**。
  ④ **不改仓库代码**：cadence 候选只在**内存里**扩（`config_v2.REBALANCE_CADENCE_GRID`）
     ⇒ 仓库代码与**已封存产物**保持同代（跑完可 `md5sum` 复核）。

【产物】
  `<out-dir>/probe_arm_search_val.json` / `probe_val_search.json`（正式搜索产物的**副本**，
  仅作证据）/ `probe_turnover_table.csv`（逐 cadence × buffer 的换手表）/ `PROBE_README.md`。

【用法】
    python manu/tools/v2_val_cadence_probe.py \
        --cadence-grid 90,120,150 --topk-grid 4,5 --buffer-grid 0,1,2,3,4,6,8 \
        --out-subdir _probe_cadence
"""
from __future__ import annotations

import math
import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

V2_DIR_CANDIDATES = (
    # ① 环境变量优先（沿用 `daily/daily_config.py::ETF_ROTATION_REPO_ROOT` 的既有约定）
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    # ② 从脚本自身位置推导（本文件在 `<repo>/manu/tools/` ⇒ 仓库根 = parents[2]）
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    # ③ 兜底：服务器工程根（脚本被单独拷走时）
    Path("/tmp/pycharm_project_658d3fc4/manu/etf_rotation/v2"),
)
V2_DIR = next(
    (p for p in V2_DIR_CANDIDATES if p is not None and (p / "config_v2.py").is_file()),
    V2_DIR_CANDIDATES[-1],
)
if str(V2_DIR) not in sys.path:
    sys.path.insert(0, str(V2_DIR))


def _md5(path: Path) -> str:
    if not path.is_file():
        return "<不存在>"
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: 🔴 已封存的单盲产物（探针**不得**触碰 ⇒ 跑前跑后比 md5）
SEALED = (
    "single_blind_test_2024_2026.json",
    "_single_blind_registry.json",
    "frozen_params.json",
    "arm_search_val.json",
    "val_search.json",
)


def _turn(value) -> str:
    """🔴 2026-11-18 续四（reviewer NIT-4）：换手值缺失 / 非有限 ⇒ 渲染成「—」。

    🔴 换手改成「双边算不出来 ⇒ 单边也是 NaN」之后，``None`` / ``NaN`` 会流到
    报告里变成字面量 ``None×`` / ``nan×`` ⇒ 读者会误读成数字 0。
    """
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.4f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cadence-grid", default="90,120,150",
                    help="扩展后的 cadence 候选（**只在内存里**生效，不改 config_v2.py）")
    ap.add_argument("--topk-grid", default="4,5")
    ap.add_argument("--buffer-grid", default="0,1,2,3,4,6,8")
    ap.add_argument("--out-subdir", default="_probe_cadence")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config_v2 as C

    cadences = [int(x) for x in args.cadence_grid.split(",") if x.strip()]
    topk = [int(x) for x in args.topk_grid.split(",") if x.strip()]
    buffers = [int(x) for x in args.buffer_grid.split(",") if x.strip()]
    for c in cadences:
        C.rebalance_signal_stride_for(c)  # 🔴 非法 cadence（不是 10 的倍数）⇒ 直接 raise

    v2_root = Path(C.DEFAULT_V2_RUN_ROOT)
    probe_dir = v2_root / args.out_subdir

    print(f"[INFO] 正式产物根：{v2_root}")
    print(f"[INFO] 探针产物根：{probe_dir}")
    print(f"[INFO] cadence 候选 = {cadences}｜top_k = {topk}｜hold_buffer = {buffers}"
          f"｜臂 = {list(C.SMOOTHING_ARM_IDS)}")
    print(f"[INFO] trial 总数 = {len(cadences) * len(topk) * len(buffers) * len(C.SMOOTHING_ARM_IDS)}")
    print(f"[INFO] 🔴 搜索窗口 = {C.VALID}（`assert_val_only` 硬拦 ⇒ 测试段不可能被算）")
    if args.dry_run:
        print("[INFO] --dry-run ⇒ 未开跑")
        return 0

    before = {name: _md5(v2_root / name) for name in SEALED}

    probe_dir.mkdir(parents=True, exist_ok=True)
    # 搜索的输入：消融决策 + 个股打分（复制进探针目录 ⇒ 正式目录只读）
    shutil.copy2(v2_root / "ablation_choice.json", probe_dir / "ablation_choice.json")
    for csv_name in ("stock_scores_v2_ablate_roc5.csv", "stock_scores_v2_baseline_full.csv"):
        src = v2_root / csv_name
        if src.is_file():
            shutil.copy2(src, probe_dir / csv_name)

    # 🔴 铁律④：cadence 候选**只在内存里**扩 ⇒ 仓库代码零改动
    C.REBALANCE_CADENCE_GRID = tuple(cadences)

    import run_v2 as R

    ns = argparse.Namespace(
        topk_grid=",".join(str(x) for x in topk),
        buffer_grid=",".join(str(x) for x in buffers),
    )
    payload = R.stage_search(ns, probe_dir)

    # 正式搜索产物的名字在探针目录里改个名 ⇒ 不会被误当成正式产物
    for src_name, dst_name in (("arm_search_val.json", "probe_arm_search_val.json"),
                               ("val_search.json", "probe_val_search.json")):
        p = probe_dir / src_name
        if p.is_file():
            p.rename(probe_dir / dst_name)

    # 逐 trial 的换手表（cadence × buffer → 最低年化换手）
    rows = []
    for t in payload.get("trials") or []:
        prm = t.get("params") or {}
        obj = t.get("objective") or {}
        rows.append({
            "cadence": prm.get("rebalance_every_n_days"),
            "top_k": prm.get("top_k"),
            "hold_buffer": prm.get("hold_buffer"),
            "smoothing_arm": prm.get("smoothing_arm"),
            "annualized_turnover": obj.get("annualized_turnover"),
            # 🔴 并列披露：上方是**双边**（Σ|Δw|），本键是**单边**（Top-K 线同源口径）
            "annualized_turnover_one_way": C.one_way_turnover(obj.get("annualized_turnover")),
            "excess_vs_benchmark": obj.get("excess_vs_benchmark"),
            "n_rebalances": obj.get("n_rebalances"),
            "score": t.get("score"),
            "status": t.get("status"),
        })
    rows.sort(key=lambda r: (r["annualized_turnover"] is None, r["annualized_turnover"] or 0))
    table_path = probe_dir / "probe_turnover_table.csv"
    with table_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                           ["cadence", "top_k", "hold_buffer", "smoothing_arm",
                            "annualized_turnover", "annualized_turnover_one_way",
                            "excess_vs_benchmark", "n_rebalances", "score", "status"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    within = [r for r in rows if (r["annualized_turnover"] or 1e9) <= float(C.TURNOVER_TARGET)]
    best = rows[0] if rows else {}
    gate = payload.get("turnover_gate") or {}
    (probe_dir / "PROBE_README.md").write_text(
        "# V2 · 纯 val 段探针产物（**不是**正式搜索产物）\n\n"
        f"- 探针问题：**年化换手 ≤{C.TURNOVER_TARGET}× 在 val 段可达吗？**\n"
        f"- cadence 候选 = {cadences}（**仅内存扩展**，仓库 `config_v2.py` 未改）\n"
        f"- 搜索窗口 = {list(C.VALID)}（`assert_val_only` 硬拦 ⇒ 测试段未被算）\n"
        f"- trial 数 = {len(rows)}；**≤{C.TURNOVER_TARGET}× 的 trial 数 = {len(within)}**\n"
        f"- 最低年化换手 = {_turn(best.get('annualized_turnover'))}×"
        f"（单边 {_turn(best.get('annualized_turnover_one_way'))}×）"
        f"（cadence={best.get('cadence')}, top_k={best.get('top_k')}, "
        f"hold_buffer={best.get('hold_buffer')}, arm={best.get('smoothing_arm')}, "
        f"n_rebalances={best.get('n_rebalances')}）\n"
        f"- 正式搜索产物的闸门结论：`{gate.get('message', '')}`\n\n"
        "🔴 **口径提醒**：本探针**只回答「val 段可不可达」**；\n"
        "   **不能**当作测试段结论 ⇒ 若要上生产，**必须另起一个全新的盲测窗口**再验一次。\n",
        encoding="utf-8")

    after = {name: _md5(v2_root / name) for name in SEALED}
    changed = [n for n in SEALED if before[n] != after[n]]
    if changed:
        print(f"🔴🔴 已封存产物被改动：{changed} ⇒ 本探针**不合格**")
        return 1
    print("[OK] 🔴 已封存产物 md5 全部未变（单盲 / 冻结 / 正式搜索产物）")
    print(f"[OK] 逐 trial 换手表：{table_path}")
    print(f"[OK] ≤{C.TURNOVER_TARGET}× 的 trial 数 = {len(within)} / {len(rows)}")
    print(f"[OK] 最低年化换手 = {_turn(best.get('annualized_turnover'))}×"
          f"（单边 {_turn(best.get('annualized_turnover_one_way'))}×）"
          f"（cadence={best.get('cadence')}, buffer={best.get('hold_buffer')},"
          f" n_rebalances={best.get('n_rebalances')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
