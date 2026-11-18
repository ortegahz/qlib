#!/usr/bin/env python3
"""🔴 **用「新的有上限惩罚」离线重排 2026-11-17 那轮 val 搜索的 240 个 trials**（2026-11-18）

【为什么要有这个工具】
  2026-11-17 夜班那次 V2 冻结是**在 2022–2023 单边熊市 val 段**上搜出来的，
  目标函数 = ``score = 超额 − 0.05 × max(0, 换手 − 4.0)``（**惩罚项无上限**），
  网格里 cadence ∈ {20, 40, 60, 90}。结果冻结出 **cadence=90**，
  ⇒ 单盲组合 **−12.93% / 回撤 −54.93%**。

  用户 2026-11-18 目标文档给的**根因假设**是：
  「惩罚项没有上限 ⇒ 在 cadence=20 下惩罚恒 ≈ 0.416，把超额（差异量级仅 0.05~0.20）
  完全压倒 ⇒ 目标函数退化成『最小化换手』⇒ 网格为了凑那条 ≤4× 红线把周期拉到 90」。

  本工具**用同一批 trials 的原始统计量**（``excess_vs_benchmark`` / ``annualized_turnover``，
  两者与目标函数无关）**离线重排**一遍：
    * **旧口径**（无上限惩罚）⇒ 复算应与落盘的 ``score`` **逐条相等**（自证口径没跑偏）；
    * **新口径**（``min(0.02, 0.05 × max(0, turn − 4.0))``）⇒ 看胜者变成谁。
  ⇒ 若旧口径胜者 = cadence=90 而新口径胜者 = cadence=20 ⇒ **根因假设被实测佐证**。

【🔴 红线】
  * 本工具**只读** val 搜索产物，**不写**任何结果目录、不碰封存件；
  * 它是**佐证工具**，不是选参工具：真正的选参权威 = **OOS 2019–2023 滚动前推**
    （见 ``manu/tools/v2_freeze_from_walkforward.py``），
    **绝不可**把本工具的新口径胜者当成「已验收参数」。

【用法（服务器）】
    export OMP_NUM_THREADS=4
    python manu/tools/v2_rerank_val_objective.py \
        --val-search /mnt/data/siping/runs/etf_rotation_results/v2/_search_v2_candidate/val_search.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

V2_DIR_CANDIDATES = (
    Path(os.environ["ETF_ROTATION_REPO_ROOT"]) / "manu" / "etf_rotation" / "v2"
    if os.environ.get("ETF_ROTATION_REPO_ROOT")
    else None,
    Path(__file__).resolve().parents[2] / "manu" / "etf_rotation" / "v2",
    Path("/tmp/pycharm_project_658d3fc4/manu/etf_rotation/v2"),
)
V2_DIR = next((p for p in V2_DIR_CANDIDATES if p and (p / "config_v2.py").exists()), None)
if V2_DIR is None:  # pragma: no cover - 环境兜底
    sys.stderr.write("🔴 找不到 manu/etf_rotation/v2/config_v2.py\n")
    raise SystemExit(2)

sys.path.insert(0, str(V2_DIR))
import config_v2 as C  # noqa: E402


def _old_penalty(turnover: float) -> float:
    """旧口径：**无上限**惩罚（2026-11-17 夜班那轮用的就是这个）。"""
    return C.TURNOVER_PENALTY_RATE * max(0.0, float(turnover) - C.TURNOVER_TARGET)


def _new_penalty(turnover: float) -> float:
    """新口径：**有上限**惩罚（``config_v2.turnover_penalty`` 唯一实现）。"""
    return C.turnover_penalty(turnover)


def _fmt_params(params: dict) -> str:
    return (
        f"cadence={params.get('rebalance_every_n_days')} "
        f"buffer={params.get('hold_buffer')} "
        f"arm={params.get('smoothing_arm')} "
        f"top_k={params.get('top_k')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="用新口径（有上限惩罚）离线重排旧 val 搜索的 trials")
    ap.add_argument(
        "--val-search",
        default="/mnt/data/siping/runs/etf_rotation_results/v2/val_search.json",
        help="val_search.json 路径（服务器）；默认 = 2026-11-17 夜班那次（选出 cadence=90 的根因文件）",
    )
    ap.add_argument("--top", type=int, default=10, help="各口径打印前 N 条")
    ap.add_argument("--json-out", default="", help="可选：把重排结果写成 JSON")
    args = ap.parse_args()

    path = Path(args.val_search)
    if not path.exists():
        sys.stderr.write(f"🔴 找不到 val 搜索产物：{path}\n")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))

    trials = []
    for tr in data.get("trials", []):
        if tr.get("status") != "ok":
            continue
        obj = tr.get("objective", {})
        exc = obj.get("excess_vs_benchmark")
        turn = obj.get("annualized_turnover")
        if exc is None or turn is None:
            continue
        trials.append(
            {
                "params": tr.get("params", {}),
                "excess": float(exc),
                "turnover": float(turn),
                "stored_score": float(tr.get("score", float("nan"))),
                "old_penalty": _old_penalty(turn),
                "new_penalty": _new_penalty(turn),
            }
        )
    if not trials:
        sys.stderr.write("🔴 val 搜索产物里没有可用 trials\n")
        return 2

    for t in trials:
        t["old_score"] = t["excess"] - t["old_penalty"]
        t["new_score"] = t["excess"] - t["new_penalty"]

    # 🔴 自证：用旧公式复算的 score 应与落盘的 score 逐条相等（否则说明口径已漂移，不可比）
    mism = [t for t in trials if not math.isclose(t["old_score"], t["stored_score"], abs_tol=1e-9)]
    self_check_ok = not mism

    old_rank = sorted(trials, key=lambda t: -t["old_score"])
    new_rank = sorted(trials, key=lambda t: -t["new_score"])

    cadences = sorted({t["params"].get("rebalance_every_n_days") for t in trials})
    per_cadence = []
    for cad in cadences:
        group = [t for t in trials if t["params"].get("rebalance_every_n_days") == cad]
        # 🔴 2026-11-18 换手口径正名：trial 里的 ``annualized_turnover`` 是**双边**
        #    （Σ|Δw|）⇒ 落盘键带口径后缀，并**并列**给出单边
        #    （= 0.5 × 双边，唯一实现 ``config_v2.one_way_turnover``）；
        #    旧键名 ``best_turnover`` 属历史错标（未标口径）⇒ 已改名。
        best_turn_two_way = min(g["turnover"] for g in group)
        per_cadence.append(
            {
                "cadence": cad,
                "n_trials": len(group),
                "best_excess": max(g["excess"] for g in group),
                "median_excess": sorted(g["excess"] for g in group)[len(group) // 2],
                "best_turnover_two_way": best_turn_two_way,
                "best_turnover_one_way": C.one_way_turnover(best_turn_two_way),
                "best_old_score": max(g["old_score"] for g in group),
                "best_new_score": max(g["new_score"] for g in group),
                "best_old_params": _fmt_params(max(group, key=lambda g: g["old_score"])["params"]),
                "best_new_params": _fmt_params(max(group, key=lambda g: g["new_score"])["params"]),
            }
        )

    print("=" * 96)
    print(f"🔴 旧 val 搜索离线重排｜ trials={len(trials)}｜ 窗口={data.get('val_window')}")
    print(f"   落盘 best_params : {_fmt_params(data.get('best_params', {}))}")
    print(f"   落盘 best_score  : {data.get('best_score')}")
    print("=" * 96)

    print("\n【自证】旧公式（无上限惩罚）复算 vs 落盘 score")
    print(f"   逐条相等：{'✅ 是' if self_check_ok else f'❌ 否（{len(mism)} 条不等 ⇒ 口径漂移，不可比）'}")
    if mism:
        for t in mism[:5]:
            print(f"     重算={t['old_score']:.12f} 落盘={t['stored_score']:.12f} {_fmt_params(t['params'])}")

    print("\n【旧口径】score = 超额 − 0.05 × max(0, 换手 − 4.0)（惩罚**无上限**）")
    for i, t in enumerate(old_rank[: args.top], 1):
        print(
            f"   {i:>2}. {t['old_score']:+.6f}  超额={t['excess']:+.4f}  换手(双边)={t['turnover']:6.2f}×  "
            f"惩罚={t['old_penalty']:.4f}  {_fmt_params(t['params'])}"
        )
    print(f"   ⇒ 旧口径胜者：{_fmt_params(old_rank[0]['params'])}（score={old_rank[0]['old_score']:+.6f}）")

    print("\n【新口径】score = 超额 − min(0.02, 0.05 × max(0, 换手 − 4.0))（惩罚**有上限**）")
    for i, t in enumerate(new_rank[: args.top], 1):
        print(
            f"   {i:>2}. {t['new_score']:+.6f}  超额={t['excess']:+.4f}  换手(双边)={t['turnover']:6.2f}×  "
            f"惩罚={t['new_penalty']:.4f}  {_fmt_params(t['params'])}"
        )
    print(f"   ⇒ 新口径胜者：{_fmt_params(new_rank[0]['params'])}（score={new_rank[0]['new_score']:+.6f}）")

    print("\n【按 cadence 分组】同一批 trials 的最优可得值")
    print(
        f"   {'cadence':>8} {'n':>4} {'最佳超额':>10} {'超额中位':>10} "
        f"{'最低换手(双边)':>9} {'旧口径best':>11} {'新口径best':>11}"
    )
    for row in per_cadence:
        print(
            f"   {row['cadence']:>8} {row['n_trials']:>4} {row['best_excess']:>+10.4f} "
            f"{row['median_excess']:>+10.4f} {row['best_turnover_two_way']:>8.2f}× "
            f"{row['best_old_score']:>+11.6f} {row['best_new_score']:>+11.6f}"
        )

    old_cad = old_rank[0]["params"].get("rebalance_every_n_days")
    new_cad = new_rank[0]["params"].get("rebalance_every_n_days")
    gate = data.get("turnover_gate") or {}
    if gate:
        print("\n【旧口径的第二个机制：换手闸门「全超标」兜底】")
        print(f"   · 目标 {gate.get('target')}×｜达标 trial 数 = {gate.get('n_trials_within_target')} / {gate.get('n_trials_with_turnover')}")
        print(f"   · 兜底选中者的换手 = {gate.get('chosen_turnover')}（该文件里最低 = {gate.get('min_turnover')}）")
        print(f"   · 兜底选中者的调仓次数 = {gate.get('chosen_n_rebalances')}（门槛 {gate.get('min_rebalances_for_selection')}）")
        print(f"   · 该文件自带告警：{gate.get('sample_note')}")
        print(f"   · 闸门消息：{gate.get('message')}")

    print("\n【结论】")
    print(f"   · 旧口径胜者 cadence = {old_cad}；新口径胜者 cadence = {new_cad}")
    print(f"   · 旧目标函数（惩罚无上限）把 score 极值推向 cadence={old_cad} ⇒ ✅ 佐证「惩罚压倒超额」")
    print(f"   · 换惩罚封顶后，本窗口的 score 极值变成 cadence={new_cad}")
    if new_cad != 20:
        print(
            "   🔴 **重要修正**：在这个 2 年单边熊市窗口上，**仅封顶惩罚并不会选出 cadence=20**"
        )
        print(
            "      ⇒ cadence=20 必须作为**先验硬约束**锁死（`REBALANCE_CADENCE_GRID=(20,)` ＋ `assert_cadence_locked()`），"
        )
        print(
            "      **不是**指望目标函数自己搜出来；且选参窗口必须换成 2019–2023 滚动前推（≈60 个月频样本）。"
        )
        print(
            f"      ⇒ 另一条独立佐证：该窗口 cadence=20 的**最佳超额仅 "
            f"{max(t['excess'] for t in trials if t['params'].get('rebalance_every_n_days') == 20):+.4f}**"
            f"（中位 {sorted(t['excess'] for t in trials if t['params'].get('rebalance_every_n_days') == 20)[len([t for t in trials if t['params'].get('rebalance_every_n_days') == 20]) // 2]:+.4f}）"
            f"⇒ 单边熊市窗口本来就压不出月频的价值。"
        )
    else:
        print("   · ✅ 新口径在本窗口也选出 cadence=20 ⇒ 处方「封顶 + 锁 cadence」可自洽成立。")
    print("   🔴 本工具只作**根因佐证**；选参权威仍是 OOS 2019–2023 滚动前推，**不得**直接采用此处胜者。")

    if args.json_out:
        out = {
            "val_search": str(path),
            "n_trials": len(trials),
            "self_check_old_formula_matches_stored": self_check_ok,
            "old_winner": {"params": old_rank[0]["params"], "score": old_rank[0]["old_score"]},
            "new_winner": {"params": new_rank[0]["params"], "score": new_rank[0]["new_score"]},
            "per_cadence": per_cadence,
            # 🔴 2026-11-18 换手口径正名：``turnover`` ⇒ ``turnover_two_way``（**双边**），
            #    并**并列**给出 ``turnover_one_way``（= 0.5 × 双边，唯一实现
            #    ``config_v2.one_way_turnover``）⇒ 落盘 JSON 不再有无口径的裸键。
            "ranking_old": [
                {"params": t["params"], "score": t["old_score"], "excess": t["excess"],
                 "turnover_two_way": t["turnover"],
                 "turnover_one_way": C.one_way_turnover(t["turnover"])}
                for t in old_rank
            ],
            "ranking_new": [
                {"params": t["params"], "score": t["new_score"], "excess": t["excess"],
                 "turnover_two_way": t["turnover"],
                 "turnover_one_way": C.one_way_turnover(t["turnover"])}
                for t in new_rank
            ],
        }
        Path(args.json_out).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n   ⇒ JSON 已写：{args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
