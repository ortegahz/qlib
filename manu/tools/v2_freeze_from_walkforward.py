#!/usr/bin/env python3
"""🔴 **从 5 折滚动前推（2019–2023）冻结 V2 参数**（2026-11-18）

【为什么要有这个工具】
  2026-11-17 夜班那次冻结是**在 2022–2023 单边熊市的 val 段上**搜出来的
  ⇒ 网格搜索为了凑「换手 ≤ 4×」红线，把调仓周期拉到了 **cadence=90**（每 4 个半月）
  ⇒ 单盲组合 **−12.93% / 回撤 −54.93%**。
  用户 2026-11-18 目标文档的处方是：
    * **锁死 cadence = 20**（严格月频）——降换手靠「月频 + Rank Buffer」，**不是**拉长周期；
    * **用 2019–2023 五年滚动前推（≈60 个独立月度样本）选参**，不再用 2 年单边熊市；
    * 只留**一维**自由度（``hold_buffer`` = Rank Buffer 排名缓冲带）。
  ⇒ 本工具 = 把「滚动前推的 OOS 冠军」落成**可审计的冻结产物**。

【🔴 与封存件的关系（红线）】
  * 封存的 ``frozen_params.json`` 是**一次性单盲登记/归因探针的指纹**
    ⇒ 本工具**绝不覆盖它**（输出名默认 ``frozen_params_wf.json``，且拒绝写封存路径）。
  * 新冻结件**不构成**「已验收」：2024–2026 单盲**已经消耗过**，**不可回头用它验收**
    ⇒ 只能作为「下一轮（2027 起）生产口径的候选冻结」。

【口径】
  * 选参权威 = **OOS（2019–2023 滚动前推）**，不是 val 段；
  * 目标函数与 ``config_v2.turnover_penalty`` 同源（有上限 0.02）；
  * cadence / top_k **不参与搜索**（锁定档），只从 OOS 冠军取 ``hold_buffer`` 与方案参数。

【用法（服务器）】
    export OMP_NUM_THREADS=4
    python manu/tools/v2_freeze_from_walkforward.py \
        --walkforward-dir /mnt/data/siping/runs/etf_rotation_results/v2/_walkforward \
        --out /mnt/data/siping/runs/etf_rotation_results/v2/frozen_params_wf.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

V2_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    Path("/tmp/pycharm_project_658d3fc4/manu/etf_rotation/v2"),
)
V2_DIR = next(
    (p for p in V2_DIR_CANDIDATES if p is not None and (p / "config_v2.py").is_file()),
    V2_DIR_CANDIDATES[-1],
)
if str(V2_DIR) not in sys.path:
    sys.path.insert(0, str(V2_DIR))

#: 🔴 **封存件名**（本工具**绝不**覆盖）
SEALED_FROZEN_NAME = "frozen_params.json"


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="从 5 折滚动前推冻结 V2 参数")
    ap.add_argument("--walkforward-dir", required=True, help="滚动前推产物目录（含 walkforward_summary.json）")
    ap.add_argument("--out", default="", help="输出冻结件路径（默认 <wf-dir>/frozen_params_wf.json）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import config_v2 as C
    import run_v2 as RV

    wf_dir = Path(args.walkforward_dir)
    summary_path = wf_dir / "walkforward_summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"🔴 找不到滚动前推汇总：{summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    out_path = Path(args.out) if args.out else (wf_dir / "frozen_params_wf.json")
    if out_path.name == SEALED_FROZEN_NAME:
        raise RuntimeError(
            f"🔴 拒绝写封存冻结件 `{SEALED_FROZEN_NAME}`"
            "（它是一次性单盲登记/归因探针的指纹）⇒ 换个文件名"
        )

    ranking = list(summary.get("ranking") or [])
    if not ranking:
        raise RuntimeError("🔴 滚动前推汇总里没有 ranking ⇒ 无冠军可冻")
    best = summary.get("best") or ranking[0]
    if best.get("arm") != ranking[0].get("arm") or \
            int(best.get("hold_buffer", -1)) != int(ranking[0].get("hold_buffer", -2)):
        _log("⚠️ summary.best 与 ranking[0] 不一致 ⇒ 以 ranking[0] 为准（ranking 已按 score 排序）")
        best = ranking[0]
    best_params = dict(best.get("params") or {})

    # 🔴 cadence / top_k **锁死**（用户红线）：不接受 OOS 冠军改这两个
    cadence = int(C.WALK_FORWARD_CADENCE)
    RV.assert_cadence_locked(cadence)
    top_k = int(C.WALK_FORWARD_TOPK_LOCKED)

    params = {
        "smoothing_arm": str(best_params.get("smoothing_arm")),
        "top_k": top_k,
        "hold_buffer": int(best_params.get("hold_buffer")),
        "rebalance_every_n_days": cadence,
    }
    for key in ("alpha_weight", "momentum_weight", "entry_ma_window", "exit_ma_window",
                "allow_theme_cap", "neg_score_gate", "neg_score_gate_stat",
                "neg_score_gate_scale", "neg_score_gate_min_exposure"):
        if key in best_params:
            params[key] = best_params[key]

    oos_actual = summary.get("oos_window_actual")
    oos_nominal = summary.get("oos_window_nominal")
    n_scored = int(summary.get("n_folds_scored") or 0)
    n_req = int(summary.get("n_folds_requested") or 0)
    if oos_actual is None:
        raise RuntimeError("🔴 滚动前推**没有实际 OOS 窗口** ⇒ 无从冻结")
    if n_scored < n_req:
        _log(f"⚠️ 只有 {n_scored}/{n_req} 折实际评分（作废折见 summary.void_folds）")

    metrics = dict(best.get("oos_metrics") or {})
    turn = metrics.get("annualized_turnover")
    payload = {
        "params": params,
        "model_name": str(C.ABLATION_WINNER),
        "winner_arm": str(best.get("arm")),
        "winner_arm_desc": str(best.get("arm_desc") or ""),
        "rebalance_every_n_days": cadence,
        "rebalance_signal_stride": int(C.rebalance_signal_stride_for(cadence)),
        "best_score": best.get("score"),
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": (
            f"🔴 **5 折 expanding window 滚动前推 OOS**（实际窗口 "
            f"{oos_actual[0]} ~ {oos_actual[1]}；{n_scored}/{n_req} 折评分）"
        ),
        "selection": {
            "objective": summary.get("objective"),
            "turnover_penalty_cap": float(C.TURNOVER_PENALTY_CAP),
            "turnover_target": float(C.TURNOVER_TARGET),
            "oos_window_nominal": oos_nominal,
            "oos_window_actual": oos_actual,
            "n_folds_scored": n_scored,
            "n_folds_requested": n_req,
            "void_folds": summary.get("void_folds") or [],
            "grid_locked": {"rebalance_every_n_days": cadence, "top_k": top_k},
            "oos_metrics": metrics,
            "etf_rank_ic": best.get("etf_rank_ic"),
            "n_oos_days": best.get("n_oos_days"),
            "price_panel_coverage": summary.get("price_panel_coverage"),
            "ranking_top5": ranking[:5],
        },
        "provenance": {
            "walkforward_summary": str(summary_path),
            "walkforward_summary_md5": _md5(summary_path),
            "walkforward_dir": str(wf_dir),
            "frozen_params_wf_md5": None,
        },
        "disclosure": [
            "🔴 **cadence 锁死 20 个交易日（严格月频）**：降换手的正确姿势是"
            "「月频 + Rank Buffer（宽出严进）」，**不是**拉长调仓周期"
            "（2026-11-17 事故：cadence=90 ⇒ 单盲 −12.93% / 回撤 −54.93%）。",
            f"🔴 **换手 ≤ {float(C.TURNOVER_TARGET):.1f}× 红线在 cadence=20 下结构性不可达**"
            f"（本件实际换手 = {turn}）⇒ 换手只作**并列披露**，不作选参硬门槛。",
            "🔴 **2024–2026 单盲已消耗**（2026-11-17 跑过一次）⇒ 本件**不可**回头用它验收；"
            "它只是**下一轮（2027 起）生产口径的候选冻结**。",
            "🔴 选参权威 = **OOS 2019–2023 滚动前推**（≈60 个月频样本），"
            "**不是** 2022–2023 单边熊市 val 段。",
            "🔴 折 4 训练段与原 2016~2021 逐日相同 ⇒ S 层分数逐位复现（探针铁律④自证）。",
            "🔴 每折从**空仓**起算（不跨折结转持仓）⇒ 拼接 OOS 在折边界有轻微跳空。",
        ],
    }
    if oos_nominal and list(oos_actual) != list(oos_nominal):
        payload["disclosure"].append(
            f"🔴 名义 OOS 窗口 = {oos_nominal[0]} ~ {oos_nominal[1]}，"
            f"**实际** = {oos_actual[0]} ~ {oos_actual[1]}（作废折见 selection.void_folds）"
        )

    if args.dry_run:
        _log("--dry-run ⇒ 未写盘")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out_path)
    os.chmod(out_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)   # 0444 = 只读
    payload["provenance"]["frozen_params_wf_md5"] = _md5(out_path)
    _log(f"✅ 已冻结：{out_path}（md5 {payload['provenance']['frozen_params_wf_md5']}，0444 只读）")
    _log(f"   冠军：{payload['winner_arm']}｜hold_buffer={params['hold_buffer']}"
         f"｜cadence={cadence}（stride {payload['rebalance_signal_stride']}）"
         f"｜top_k={top_k}")
    _log(f"   OOS（实际）= {oos_actual[0]} ~ {oos_actual[1]}（{n_scored}/{n_req} 折）"
         f"｜超额 {metrics.get('excess_vs_benchmark')}｜换手 {turn}")
    _log(f"   🔴 封存的 {SEALED_FROZEN_NAME} **未动**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
