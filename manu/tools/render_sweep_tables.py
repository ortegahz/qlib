"""把 turnover_sweep.json / valid_stability.json 渲染成 memory 用的 Markdown 表格。

【为什么需要这个】
降换手 sweep 有 14 组 arm × (valid/test) × 11 列 + 14 组分年 × 4 列 ≈ 200 个数字。
手工抄进 memory 极易抄错 ⇒ 本脚本**直接从产物 JSON 生成表格文本**，逐格可追溯。

【只读】只读 JSON，不写任何文件（表格打到 stdout，由人/主 agent 粘贴进 memory）。

【用法】
    python manu/tools/render_sweep_tables.py \
        --sweep  turnover_sweep.json \
        --stability valid_stability.json
    # 或从服务器取：
    scp -P 32222 huangzhe@192.168.99.40:<run_dir>/turnover_sweep.json .

【输出】
  1) §4.7.4 完整结果表（14 行 × 12 列）
  2) §4.7.5 valid 分年表（按 min_year_net 降序）
  3) §4.7.5 Spearman 表（3 行）
  4) 一致性自检：sweep 的 valid 净超额 vs stability 的 whole_valid.net_annualized（逐位差）
  5) 断言汇总 + 耗时
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def pct(x: float, digits: int = 2) -> str:
    return f"{x * 100:+.{digits}f}%"


def num(x: float, digits: int = 3) -> str:
    return f"{x:.{digits}f}"


def get(d: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman 秩相关（纯 python 实现：平均秩 + Pearson），不依赖 scipy。

    秩相同的元素取平均秩（与 scipy.stats.rankdata 的默认 'average' 一致）。
    """

    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num_ = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num_ / den if den else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", required=True, help="turnover_sweep.json 路径")
    ap.add_argument("--stability", required=True, help="valid_stability.json 路径")
    args = ap.parse_args()

    sweep = json.loads(Path(args.sweep).read_text(encoding="utf-8"))
    stab = json.loads(Path(args.stability).read_text(encoding="utf-8"))
    results = sweep["results"]
    arms = {r["arm_id"]: r for r in sweep["arm_grid"]}
    sres = stab["results"]

    # ---------- 1) 完整结果表 ----------
    lines = ["#### 4.7.4 完整结果（14 组 arm × 2 段）", ""]
    lines.append(
        "| arm | 参数 | valid毛 | valid净 | valid IR | valid换手 | test毛 | test净 | test IR | test MDD | test换手 | test成本 |"
    )
    lines.append("|:--|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for arm_id, arm in arms.items():
        v = results[arm_id]["valid"]
        t = results[arm_id]["test"]
        params = f"{arm['topk']},{arm['n_drop']},{arm['hold_thresh']},{arm['rebalance_every']}"
        gap = get(t, "cost_reconciliation", "recomputed", "gap_pp", default=float("nan"))
        lines.append(
            f"| {arm_id} | {params} "
            f"| {pct(get(v, 'returns', 'without_cost', 'annualized_return'))} "
            f"| {pct(get(v, 'returns', 'with_cost', 'annualized_return'))} "
            f"| {num(get(v, 'returns', 'with_cost', 'information_ratio'))} "
            f"| {get(v, 'turnover', 'annualized_single_side_x250_steady'):.1f} "
            f"| {pct(get(t, 'returns', 'without_cost', 'annualized_return'))} "
            f"| {pct(get(t, 'returns', 'with_cost', 'annualized_return'))} "
            f"| {num(get(t, 'returns', 'with_cost', 'information_ratio'))} "
            f"| {pct(get(t, 'returns', 'with_cost', 'max_drawdown'))} "
            f"| {get(t, 'turnover', 'annualized_single_side_x250_steady'):.1f} "
            f"| {gap:.2f}pp |"
        )

    # ---------- 2) valid 分年表 ----------
    lines += ["", "#### 4.7.5 valid 分年稳定性", ""]
    lines.append("| arm | 2022 | 2023 | 最差年 min | 极差 spread |")
    lines.append("|:--|--:|--:|--:|--:|")
    ordered = sorted(sres.values(), key=lambda x: x["min_year_net"], reverse=True)
    for row in ordered:
        y22 = row["by_year"]["2022"]["net_annualized"]
        y23 = row["by_year"]["2023"]["net_annualized"]
        lines.append(
            f"| {row['arm_id']} | {pct(y22)} | {pct(y23)} "
            f"| {pct(row['min_year_net'])} | {row['spread_year_net'] * 100:.2f}pp |"
        )

    # ---------- 3) Spearman ----------
    arm_ids = [a for a in arms if a in sres]
    valid_whole = [sres[a]["whole_valid"]["net_annualized"] for a in arm_ids]
    valid_min = [sres[a]["min_year_net"] for a in arm_ids]
    test_net = [results[a]["test"]["returns"]["with_cost"]["annualized_return"] for a in arm_ids]
    turnover = [results[a]["test"]["turnover"]["annualized_single_side_x250_steady"] for a in arm_ids]
    r_whole = spearman(valid_whole, test_net)
    r_min = spearman(valid_min, test_net)
    r_turn = spearman(turnover, test_net)
    lines += [
        "",
        "**秩相关统计（Spearman，14 组 arm）**：",
        "",
        "| 判据 vs test 段含成本年化超额 | ρ | 读法 |",
        "|:--|--:|:--|",
        f"| valid **整段**净超额 | **{r_whole:.3f}** | 弱预测 ⇒ 在 valid 上取 argmax 不可靠 |",
        f"| valid **分年最差值** `min_year_net` | **{r_min:.3f}** | 更强 ⇒ **跨子区间稳健是更好的选参判据** |",
        f"| 年化单边换手 | **{r_turn:.3f}** | 弱负相关 ⇒ 换手越高越差 |",
        "",
        f"> （独立复算：valid 整段 ρ={r_whole:.6f} / min_year ρ={r_min:.6f} / 换手 ρ={r_turn:.6f}）",
    ]

    # ---------- 4) 一致性自检 ----------
    lines += ["", "**一致性自检：sweep 的 valid 净超额 vs stability 的 whole_valid.net_annualized**", ""]
    lines.append("| arm | sweep valid净 | stability whole_valid净 | 绝对差 |")
    lines.append("|:--|--:|--:|--:|")
    worst = 0.0
    for arm_id in arm_ids:
        a = results[arm_id]["valid"]["returns"]["with_cost"]["annualized_return"]
        b = sres[arm_id]["whole_valid"]["net_annualized"]
        diff = abs(a - b)
        worst = max(worst, diff)
        lines.append(f"| {arm_id} | {a!r} | {b!r} | {diff:.3e} |")
    lines += ["", f"⇒ **最大绝对差 {worst:.3e}**（补丁后应 ≈ 0，即两脚本 valid 口径逐位一致）"]

    # ---------- 5) 断言 / 耗时 ----------
    a = sweep.get("assertions", {})
    failed = a.get("failed_checks", [])
    fatal = a.get("fatal_failures", [])
    lines += [
        "",
        "**断言与耗时**：",
        "",
        f"- 断言总数 **{len(a.get('results', []))}**，"
        f"失败 **{len(failed)}**，致命失败 **{len(fatal)}**"
        f"（`all_passed={a.get('all_passed')}`）",
        f"- 失败断言清单：{failed if failed else '（无）'}",
        f"- 致命失败清单：{fatal if fatal else '（无）'}",
        f"- sweep 耗时 **{sweep.get('elapsed_sec')} s**",
        f"- 产物 `generated_at` = {sweep.get('generated_at')}（stability: {stab.get('generated_at')}）",
    ]

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
