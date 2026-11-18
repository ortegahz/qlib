#!/usr/bin/env python3
"""把 5 折滚动前推的 ``walkforward_summary.json`` 压成「**候选参数 + 验收口径**」清单。

🔴 纪律：
* 候选参数**只**来自 2019–2023 的 **5 折 OOS**（滚动前推、expanding window）；
* 🔴 **测试段（2024–2026）单盲已消耗**（``allow_rerun=False``）⇒ 本文件**不含**任何
  测试段数字，且必须显式写出「**需新的盲测窗口才能验收**」；
* 候选参数**不含**任何人工调参项（``cadence`` / ``top_k`` 已焊死，见 ``locked``）。

用法::

    python -u manu/tools/v2_make_candidate_params.py \
        [--root /mnt/data/siping/runs/etf_rotation_results/v2] [--subdir _walkforward_ext2018]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

#: 🔴 单边换手换算的**唯一实现**（``config_v2.one_way_turnover``）——不许手写 0.5。
_V2_DIR = Path(__file__).resolve().parents[1] / "etf_rotation" / "v2"
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))
import config_v2 as C  # noqa: E402


def _with_one_way_turnover(metrics: dict | None) -> dict | None:
    """给 ``oos_metrics`` 补上**并列的单边换手**（同 dict，双边 + 单边）。

    🔴 口径：``annualized_turnover`` = Σ|Δw| / 年 = **双边** ⇒ 单边 = 0.5 × 双边
       （唯一实现 ``config_v2.one_way_turnover``）。
    ⚠️ 2026-11-18（① NaN 语义正名后）：双边**缺失** ⇒ 单边 **None**；
       双边**非有限**（NaN / ±inf）⇒ 单边 **NaN** ⇒ **都不是 0.0**
       （缺失 ≠ 换手为零；``config_v2.one_way_turnover`` 的新语义）。
    """
    if not isinstance(metrics, dict):
        return metrics
    out = dict(metrics)
    if out.get("annualized_turnover_one_way") is None:
        two = out.get("annualized_turnover")
        out["annualized_turnover_one_way"] = (
            C.one_way_turnover(two) if two is not None else None
        )
    out.setdefault("turnover_convention",
                   "two_way_sum_abs_dw（annualized_turnover = 双边；单边 = 0.5 × 双边）")
    return out


def _disclosure(summary: dict) -> list:
    """产物 ``disclosure`` = 源产物披露 + **本次追加的口径披露**。

    🔴 2026-11-18（I2）：``selection_protocol.objective`` 是**原样透传**的历史 objective 串
       （它描述的是**当时真实跑过的协议**）⇒ 🔴 **不许篡改原文**（篡改 = 造假账）
       ⇒ 改成**追加披露**：若它是**改版前**的旧公式（换手惩罚**未封顶**、4.0 还是硬红线），
       则它与现行 ``run_v2`` **不是同一口径** ⇒ 该 ``score`` **被旧惩罚污染**
       ⇒ 指标数字**不受影响**，但**排序口径已变**。
    """
    items = list(summary.get("disclosure") or [])
    obj = str(summary.get("objective") or "")
    if obj and ("0.05" in obj or "4.0" in obj) and "封顶" not in obj:
        items.append(
            "🔴 **口径披露**：`selection_protocol.objective` 串是**改版前**的旧公式"
            "（换手惩罚**未封顶**、4.0× 曾是**硬红线**）⇒ 它描述的是**当时真实跑过的旧协议**"
            "（原文**原样保留**，不许篡改历史）⇒ 与现行 `run_v2`"
            "（`mode=config_v2.TURNOVER_TARGET_MODE` + 惩罚**封顶** "
            f"{C.TURNOVER_PENALTY_CAP} + {C.TURNOVER_TARGET}× **只作披露基准**）"
            "**不是同一口径** ⇒ 该 `score` **被旧惩罚污染** ⇒ 指标数字"
            "（年化 / 回撤 / 换手）**不受影响**，但**排序口径已变** ⇒ 重跑后才有可比的新 `score`。"
        )
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 V2 候选参数清单")
    ap.add_argument("--root", default="/mnt/data/siping/runs/etf_rotation_results/v2")
    ap.add_argument("--subdir", default="_walkforward_ext2018", help="探针产物子目录")
    ap.add_argument("--out", default="", help="输出 JSON（默认 <root>/V2_CANDIDATE_PARAMS_*.json）")
    args = ap.parse_args()

    root = Path(args.root)
    summary_path = root / args.subdir / "walkforward_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    best = summary.get("best") or {}
    params = best.get("params") or {}

    payload = {
        "version": "v2-walkforward-candidate",
        "generated_at": summary.get("generated_at") or datetime.now().isoformat(),
        "selection_protocol": {
            "mode": "5-fold expanding-window walk-forward",
            "oos_window": (summary.get("split_summary") or {}).get("oos_window"),
            "n_folds": (summary.get("split_summary") or {}).get("n_folds"),
            "objective": summary.get("objective"),
            "compressed_dof": (summary.get("grid") or {}).get("locked"),
            "grid_size": (summary.get("grid") or {}).get("n_configs"),
        },
        "candidate_params": {
            "scoring_arm": best.get("arm"),
            "desc": best.get("arm_desc"),
            "alpha_weight": params.get("alpha_weight"),
            "momentum_weight": params.get("momentum_weight"),
            "entry_ma_window": params.get("entry_ma_window"),
            "exit_ma_window": params.get("exit_ma_window"),
            "smoothing_arm": params.get("smoothing_arm"),
            "allow_theme_cap": params.get("allow_theme_cap"),
            "top_k": params.get("top_k"),
            "hold_buffer": params.get("hold_buffer"),
            "rebalance_every_n_days": params.get("rebalance_every_n_days"),
            "neg_score_gate": params.get("neg_score_gate"),
            "neg_score_gate_scale": params.get("neg_score_gate_scale"),
            "neg_score_gate_min_exposure": params.get("neg_score_gate_min_exposure"),
        },
        # 🔴 2026-11-18 口径正名：``oos_metrics.annualized_turnover`` = Σ|Δw| / 年
        #    = **双边** ⇒ **同一个 dict** 必须并列落单边（守卫规则③）。
        #    ⚠️ 源产物若是改版前生成的（没有单边键）⇒ 按唯一实现**口径换算**补齐
        #    （纯换算，不编数字），并把口径写进 ``turnover_convention``。
        "oos_metrics": _with_one_way_turnover(best.get("oos_metrics")),
        "etf_layer_rank_ic_20d": best.get("etf_rank_ic"),
        "objective_score": best.get("score"),
        "regression_check": summary.get("regression_check"),
        "void_folds": summary.get("void_folds"),
        "price_panel_coverage": summary.get("price_panel_coverage"),
        "acceptance_status": {
            "verified_on": "2019–2023（5 折 OOS 拼接）",
            "blind_test_window": "🔴 **未验收** —— 2024–2026 单盲已消耗（allow_rerun=False），"
                                 "本候选**不得**回头用它验收",
            "next_step": "需要一段**未使用过的**前向窗口（建议 2026-11 之后的新数据）做新的盲测，"
                         "或由用户显式授权重新解封测试段",
        },
        "disclosure": _disclosure(summary),
    }
    out = Path(args.out) if args.out else root / "V2_CANDIDATE_PARAMS_20261118.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[OK] 候选参数已写出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
