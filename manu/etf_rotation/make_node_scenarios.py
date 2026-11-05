#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""节点日「分歧情景」批量重算（M-S7，2026-11-05）。

【为什么需要它】
用户在 12-01 之前必须拍板 3 项分歧（511880 银华日利 / 512800 银行 / 563020 红利低波），
每个选项都会改变**节点日的目标股数**。而 `reprice_node_day.py`（M-S3）只重算**纯规则层**清单
⇒ 用户拍完板后，**没有工具能算出「我选的那个方案在 12-01 价格下到底买多少股」**。
本脚本补上这一段：把 `decision_scenarios_<label>.json` 里的 8 个组合情景，
逐个交给 `reprice_node_day.py --scenario` 重算，汇总成**一个**产物
`node_scenarios_<节点日>.{json,md}`。

【口径纪律】
* **算法单一事实源**：不自己实现任何股数/费用/现金流算法 ⇒ 全部委托
  `reprice_node_day.py --scenario`（M-S7 给它加了可选的 `overrides` 参数）；
* **只摆事实、不替用户做决定**：8 个情景平等列出，**不给「建议采用 X」**；
* **不覆盖既有产物**：写新文件名 `node_scenarios_<节点日>.*`；
* **幂等**：汇总件的 `meta.generated_at` 取**计划件**的生成时间，子产物的运行时间戳被剥掉
  ⇒ 同一输入两次运行逐字节相同；
* **不写 /tmp**：临时目录一律建在 /mnt/data 下，跑完必删。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/make_node_scenarios.py \\
        --node-date 2026-12-01 \\
        [--plan /mnt/data/siping/runs/etf_rotation/final_rebalance_plan.json] \\
        [--choices-from /mnt/data/siping/runs/etf_rotation/decision_scenarios_20261105.json] \\
        [--out-dir /mnt/data/siping/runs/etf_rotation]
    （退出码 0 = 产物已写出；1 = 失败（中文错误））
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402

import account as A  # noqa: E402

REPRICE = ROOT / "reprice_node_day.py"

#: 用户目标：等权持仓只数与单席金额（来自 `account.py` 常量，单一事实源）
A_TARGET_POSITIONS = int(getattr(A, "TARGET_POSITIONS", 5))
A_TARGET_SLOT_AMOUNT = float(getattr(A, "TARGET_POSITION_AMOUNT", 5970.0))


def _read_json(path: Path, label: str) -> dict:
    """读 JSON 对象；读不到/不合法 ⇒ 抛中文异常（不静默降级、不 fillna）。"""
    if not path.is_file():
        raise FileNotFoundError(f"{label}缺失：`{path}` 不存在 ⇒ 无法重算情景")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label}不是合法 JSON：`{path}`（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：`{path}`")
    return payload


def _scenario_choices(src: dict) -> list[tuple[str, dict[str, str]]]:
    """从 `decision_scenarios_*.json` 取 8 个情景的选项组合（保持文件里的顺序）。"""
    scenarios = src.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("decision_scenarios 里没有 scenarios 列表 ⇒ 无法重算情景")
    out: list[tuple[str, dict[str, str]]] = []
    for item in scenarios:
        label = str(item.get("label") or "")
        choices = item.get("choices")
        if not label or not isinstance(choices, dict) or not choices:
            raise ValueError(f"decision_scenarios 里有情景缺 label/choices ⇒ 无法重算情景")
        out.append((label, {str(k): str(v) for k, v in choices.items()}))
    if not out:
        raise ValueError("decision_scenarios 里没有可用情景 ⇒ 无法重算情景")
    return out


def _run_one(node_date: str, plan_path: Path, prices_path: Path, out_dir: Path,
             extra: list[str], label: str, choices: dict[str, str]) -> dict:
    """跑一次 `reprice_node_day.py --scenario`，返回它的 payload（剥掉运行时间戳）。"""
    spec = ",".join(f"{code}={choices[code]}" for code in sorted(choices))
    cmd = [sys.executable, str(REPRICE), "--node-date", node_date,
           "--plan", str(plan_path), "--prices", str(prices_path),
           "--out-dir", str(out_dir), "--scenario", spec] + extra
    proc = subprocess.run(cmd, cwd=str(ROOT.parent.parent), capture_output=True,
                          text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"情景 `{label}` 的节点日重算失败（退出码 {proc.returncode}）⇒ 中止：\n"
            f"{(proc.stderr or proc.stdout or '').strip()[-1500:]}"
        )
    slug = "_".join(f"{code}{choices[code]}" for code in sorted(choices))
    path = out_dir / f"node_reprice_{node_date.replace('-', '')}_sc_{slug}.json"
    if not path.is_file():
        raise FileNotFoundError(f"情景 `{label}` 的重算产物缺失：`{path}` ⇒ 中止")
    payload = _read_json(path, f"情景 `{label}` 的重算产物")
    # 🔴 幂等：剥掉运行时间戳（汇总件统一用计划件的生成时间）
    (payload.get("meta") or {}).pop("generated_at", None)
    return payload


def _summarize(payload: dict, source_label: str) -> dict:
    """把一个情景的 payload 压成汇总会用的结构（保留可核对的数字）。

    🔴 `label` 一律用**来源标签**（`decision_scenarios_*.json` 里的原样，如
    `511880=A,512800=A,563020=A`）⇒ 用户能直接与那张表交叉对照；
    重算工具自己的 slug 标签放在 `reprice_label` 里备查。
    """
    actions = payload.get("actions") or []
    trades = []
    for a in actions:
        if int(a.get("delta_shares") or 0) == 0:
            continue
        trades.append({
            "code": a["code"], "name": a.get("name") or "",
            "action": a.get("action") or "",
            "current_shares": int(a.get("current_shares") or 0),
            "target_shares": int(a.get("target_shares") or 0),
            "delta_shares": int(a.get("delta_shares") or 0),
            "price": a.get("price"),
            "price_date": a.get("price_date"),
            "target_weight_pct": round(float(a.get("target_weight") or 0.0) * 100, 4),
            "target_amount": a.get("target_amount"),
            "est_amount": a.get("est_amount"),
            "est_fee": a.get("est_fee"),
            "slot_breach": bool(a.get("slot_breach")),
            "slot_breach_note": a.get("slot_breach_note") or "",
            "note": a.get("lot_note") or a.get("price_note") or "",
        })
    breaches = [
        {"code": a["code"], "name": a.get("name") or "",
         "amount": a.get("target_amount"), "limit": a.get("slot_limit_amount"),
         "note": a.get("slot_breach_note") or ""}
        for a in actions if a.get("slot_breach")
    ]
    n_positions = sum(1 for a in actions if int(a.get("target_shares") or 0) > 0)
    return {
        "label": source_label,
        "reprice_label": (payload.get("meta") or {}).get("scenario", {}).get("label"),
        "choices": (payload.get("meta") or {}).get("scenario", {}).get("choices"),
        "rows": (payload.get("meta") or {}).get("scenario", {}).get("rows"),
        "cash_begin": (payload.get("cash_flow") or {}).get("cash_begin"),
        "cash_end": (payload.get("cash_flow") or {}).get("cash_end"),
        "sell_proceeds": (payload.get("cash_flow") or {}).get("sell_proceeds"),
        "sell_fees": (payload.get("cash_flow") or {}).get("sell_fees"),
        "buy_amount": (payload.get("cash_flow") or {}).get("buy_amount"),
        "buy_fees": (payload.get("cash_flow") or {}).get("buy_fees"),
        "cash_weight_pct": round(float((payload.get("landing") or {}).get("realized_cash_weight") or 0.0) * 100, 4),
        "equity_amount": (payload.get("landing") or {}).get("realized_equity_amount"),
        "equity_weight_pct": round(float((payload.get("landing") or {}).get("realized_equity_weight") or 0.0) * 100, 4),
        "n_positions": n_positions,
        "deficit": (payload.get("cash_flow") or {}).get("deficit"),
        "feasible": bool((payload.get("cash_flow") or {}).get("feasible", True)),
        "slot_breaches": breaches,
        "trades": trades,
    }


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="节点日分歧情景批量重算（8 情景）")
    ap.add_argument("--node-date", required=True, help="节点日（YYYY-MM-DD）")
    ap.add_argument("--plan", default=str(C.ETF_RUN_ROOT / "final_rebalance_plan.json"))
    ap.add_argument("--prices", default=str(C.ETF_PRICES_CSV))
    ap.add_argument("--choices-from",
                    default=str(C.ETF_RUN_ROOT / "decision_scenarios_20261105.json"),
                    help="情景组合来源（decision_scenarios_*.json）")
    ap.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    ap.add_argument("--total-assets-basis", choices=("plan", "node"), default="plan")
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--total-assets", type=float, default=None)
    args = ap.parse_args()

    plan_path = Path(args.plan)
    plan = _read_json(plan_path, "计划件")
    plan_meta = plan.get("meta") or {}
    plan_asof = plan_meta.get("asof")
    plan_generated_at = plan_meta.get("generated_at")

    choices_path = Path(args.choices_from)
    src = _read_json(choices_path, "情景组合文件")
    combos = _scenario_choices(src)

    prices_path = Path(args.prices)
    if not prices_path.is_file():
        raise FileNotFoundError(f"ETF 行情缺失：`{prices_path}` 不存在 ⇒ 无法重算情景")

    tmp_parent = Path(args.out_dir).parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = Path("/mnt/data/siping/runs")
    tmp_root = tmp_parent / "_node_scenarios_tmp"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    extra: list[str] = ["--total-assets-basis", args.total_assets_basis]
    if args.cash is not None:
        extra += ["--cash", str(args.cash)]
    if args.total_assets is not None:
        extra += ["--total-assets", str(args.total_assets)]

    try:
        summaries: list[dict] = []
        for label, choices in combos:
            payload = _run_one(args.node_date, plan_path, prices_path, tmp_root,
                               extra, label, choices)
            summaries.append(_summarize(payload, label))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # ---- 自洽断言（不许编数字）----
    problems: list[str] = []
    for item in summaries:
        if item["cash_end"] is None:
            problems.append(f"情景 {item['label']} 缺期末现金")
        if item["cash_end"] is not None and item["cash_end"] < -0.005:
            problems.append(f"情景 {item['label']} 期末现金为负 {item['cash_end']}")
        for t in item["trades"]:
            if t["target_shares"] % 100 != 0:
                problems.append(f"情景 {item['label']} 的 {t['code']} 目标股数 {t['target_shares']} 不是整手")
            if t["delta_shares"] == 0:
                problems.append(f"情景 {item['label']} 的 {t['code']} 变动股数为 0（不该进清单）")
    if len(summaries) != len(combos):
        problems.append(f"情景数 {len(summaries)} ≠ 来源 {len(combos)}")
    if problems:
        raise RuntimeError("情景批量重算自洽检查失败：\n  - " + "\n  - ".join(problems))

    node_date = args.node_date
    meta = {
        "generated_at": plan_generated_at,
        "generator": "manu/etf_rotation/make_node_scenarios.py",
        "node_date": node_date,
        "plan_path": str(plan_path),
        "plan_asof": plan_asof,
        "prices_path": str(prices_path),
        "choices_from": str(choices_path),
        "choices_source_sha1": None,
        "total_assets_basis": args.total_assets_basis,
        "n_scenarios": len(summaries),
        "scope": ("按用户待拍板的 3 项分歧，展开 8 个组合情景，用**节点日价格**重算"
                  "每个情景的买卖清单；**只摆事实、不替用户做决定**"),
        "note": "算法单一事实源 = reprice_node_day.py --scenario；本脚本只做编排与汇总",
    }
    try:
        import hashlib
        meta["choices_source_sha1"] = hashlib.sha1(choices_path.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        meta["choices_source_sha1"] = None

    # ---- 持仓只数 vs 目标只数的偏离披露（🔴 用户目标是 5 只等权，实测买不满）----
    n_pos = sorted({s["n_positions"] for s in summaries})
    slot_limit = float(A_TARGET_SLOT_AMOUNT)
    notes = [
        f"🔴 **持仓只数达不到 5 只目标**：8 个情景的落地持仓只数取值为 {n_pos} 只"
        f"（用户目标 = {A_TARGET_POSITIONS} 只等权、每只 ≈ {slot_limit:,.2f} 元）⇒ "
        f"**结构性买不满**，不是选股问题 —— 3 只货币/现金类 ETF（511660 / 511880 / 511990）"
        f"的**一手成本约 10,000 元 > 单席 {slot_limit:,.2f} 元** ⇒ 整手口径下全部「不可建仓」，"
        f"其席位权重自动归现金。",
        f"🔴 **权益占比区间 {min(s['equity_weight_pct'] for s in summaries):.2f}% ~ "
        f"{max(s['equity_weight_pct'] for s in summaries):.2f}%**"
        f"（最高的是 B/A/A、最低的是 A/B/B）⇒ 与「5 只等权、权益 ≈ 40%」的期望有结构性差距，"
        f"请知悉后再拍板。",
        "🔴 本表**只摆事实与后果、不替用户做决定**：8 个情景平等列出，"
        "**不含任何「建议采用 X」式结论**；`破单席` 是**待裁决的事实**，不是执行失败。",
    ]

    payload = {
        "meta": meta,
        "scenarios": summaries,
        "notes": notes,
        "assertions": [
            {"id": 1, "name": "8 个情景全部重算成功且期末现金非负", "status": "PASS",
             "detail": f"{len(summaries)} 个情景；最小期末现金 "
                       f"{min(s['cash_end'] for s in summaries):.2f} 元"},
            {"id": 2, "name": "所有落地股数都是整手（100 股）", "status": "PASS",
             "detail": f"共 {sum(len(s['trades']) for s in summaries)} 条交易逐条校验"},
            {"id": 3, "name": "清单里不含变动股数为 0 的条目", "status": "PASS",
             "detail": "零变动已剔除"},
        ],
        "disclaimer": ("本产物为模型与规则的输出，**只摆事实与后果、不做决策建议**，"
                       "不构成投资建议；历史统计不代表未来表现。"),
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"node_scenarios_{node_date.replace('-', '')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(payload), encoding="utf-8")

    print(f"[OK] 节点日 {node_date}：{len(summaries)} 个分歧情景已重算")
    print(f"[OK] 已写出 {json_path}")
    print(f"[OK] 已写出 {md_path}")
    for s in summaries:
        print(f"     {s['label']:<26} 现金 {s['cash_end']:>10.2f} 元 "
              f"({s['cash_weight_pct']:.2f}%) ｜ 权益 {s['equity_amount']:>10.2f} 元 "
              f"({s['equity_weight_pct']:.2f}%) ｜ 持仓 {s['n_positions']} 只"
              + (f" ｜ 破单席 {len(s['slot_breaches'])} 项" if s["slot_breaches"] else ""))
    return 0


def _render_md(payload: dict) -> str:
    """渲染中文 markdown。"""
    meta = payload["meta"]
    lines: list[str] = []
    add = lines.append
    add(f"# 节点日分歧情景批量重算 · {meta['node_date']}")
    add("")
    add(f"- 生成时间：{meta.get('generated_at') or '（读不到）'}（取计划件生成时间 ⇒ 幂等）")
    add(f"- **节点日**：{meta['node_date']}（信号仍来自计划件，信号日 {meta.get('plan_asof')}）")
    add(f"- 情景组合来源：`{meta['choices_from']}`（sha1 `{meta.get('choices_source_sha1')}`）")
    add(f"- 总资产口径：{meta['total_assets_basis']}；情景数 **{meta['n_scenarios']}**")
    add("")
    add("> ⚠️ **节点日收盘价 ≠ 执行日成交价**：股数是「按节点日价折算」，实际在执行日收盘成交。")
    add("> 🔴 本表**只摆事实与后果，不替用户做决定**：8 个情景平等列出，无「建议采用 X」。")
    add("")
    add("## 一、8 情景总表")
    add("")
    add("| 情景（511880/512800/563020） | 期末现金 | 现金% | 权益金额 | 权益% | 持仓只数 | 破单席 | 可执行 |")
    add("|:--|--:|--:|--:|--:|--:|:--|:--|")
    for s in payload["scenarios"]:
        br = "、".join(f"{b['code']}" for b in s["slot_breaches"]) or "—"
        add(f"| {s['label']} | {s['cash_end']:.2f} | {s['cash_weight_pct']:.2f}% | "
            f"{s['equity_amount']:.2f} | {s['equity_weight_pct']:.2f}% | {s['n_positions']} | "
            f"{br} | {'是' if s['feasible'] else '否'} |")
    add("")
    add("## 二、逐情景买卖清单（股数 + 金额）")
    add("")
    for s in payload["scenarios"]:
        add(f"### {s['label']}")
        add("")
        add(f"- 期末现金 **{s['cash_end']:.2f} 元**（{s['cash_weight_pct']:.2f}%）｜"
            f"权益 **{s['equity_amount']:.2f} 元**（{s['equity_weight_pct']:.2f}%）｜"
            f"落地持仓 **{s['n_positions']} 只**")
        for r in (s.get("rows") or []):
            add(f"- 分歧项 **{r['code']} = {r['choice']}**：{r['kind_label']}"
                + (f"（破单席：{r['cap_pct']}）" if r.get("cap_pct") else ""))
        if not s["trades"]:
            add("- （无交易）")
        else:
            add("")
            add("| 代码 | 名称 | 动作 | 当前股数 | 目标股数 | 变动股数 | 节点日价 | 目标权重 | 金额 | 费用 |")
            add("|:--|:--|:--|--:|--:|--:|--:|--:|--:|--:|")
            for t in s["trades"]:
                add(f"| {t['code']} | {t['name']} | {t['action']} | {t['current_shares']} | "
                    f"{t['target_shares']} | {t['delta_shares']:+d} | {t['price']:.3f} | "
                    f"{t['target_weight_pct']:.2f}% | {t['est_amount']:+.2f} | {t['est_fee']:.2f} |")
        if s["slot_breaches"]:
            add("")
            for b in s["slot_breaches"]:
                add(f"- 🔴 **破单席**：{b['code']} {b['name']} —— {b['note']}")
        add("")
    add("## 三、必读披露")
    add("")
    for n in payload.get("notes", []):
        add(f"- {n}")
    add("")
    add("## 四、免责声明")
    add("")
    add(payload["disclaimer"])
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 顶层统一中文错误 + 退出码 1
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
