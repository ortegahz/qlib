#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""节点日「分歧情景」批量重算（M-S7，2026-11-05）。

【为什么需要它】
用户在下一调仓节点之前必须拍板 N 项分歧（**分歧标的集合与个数从
`decision_scenarios_<label>.json` 动态取，不写死**），
每个选项都会改变**节点日的目标股数**。而 `reprice_node_day.py`（M-S3）只重算**纯规则层**清单
⇒ 用户拍完板后，**没有工具能算出「我选的那个方案在节点日价格下到底买多少股」**。
本脚本补上这一段：把 `decision_scenarios_<label>.json` 里的全部组合情景（2^N 个），
逐个交给 `reprice_node_day.py --scenario` 重算，汇总成**一个**产物
`node_scenarios_<节点日>.{json,md}`。

【口径纪律】
* **算法单一事实源**：不自己实现任何股数/费用/现金流算法 ⇒ 全部委托
  `reprice_node_day.py --scenario`（M-S7 给它加了可选的 `overrides` 参数）；
* **只摆事实、不替用户做决定**：全部情景平等列出，**不给「建议采用 X」**；
* **不覆盖既有产物**：写新文件名 `node_scenarios_<节点日>.*`；
* **幂等**：汇总件的 `meta.generated_at` 取**计划件**的生成时间，子产物的运行时间戳被剥掉
  ⇒ 同一输入两次运行逐字节相同；
* **不写 /tmp**：临时目录一律建在持久盘（唯一落点根）下，跑完必删。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/make_node_scenarios.py \\
        --node-date 2026-12-01 \\
        [--plan <落点根>/runs/final_rebalance_plan.json] \\
        [--choices-from <落点根>/runs/decision_scenarios_20261105.json] \\
        [--out-dir <落点根>/runs]
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
from final_rebalance_plan import LOT  # noqa: E402  （整手份数的唯一真源）

REPRICE = ROOT / "reprice_node_day.py"

#: 用户目标：等权持仓只数与单席金额（来自 `account.py` 常量，单一事实源）
A_TARGET_POSITIONS = int(getattr(A, "TARGET_POSITIONS", 5))
#: 🔴 2026-11-09 修（critic 复核轮）：单席上限 = **总资产 × 账户级单席权重**，
#:    不再用**取整后**的 `TARGET_POSITION_AMOUNT`（5,970.0）⇒ 与产品链其它三处
#:    （`reprice_node_day.py` / `make_decision_options.py` / `make_decision_scenarios.py`
#:    / `probe_node_scenarios.py`）的 5,970.934 **逐位同口径**
#:    （旧值差 0.93 元 ⇒ 金额落在这个区间里的席位两处判定会相反）。
A_TARGET_SLOT_AMOUNT = float(A.TOTAL_ASSETS) * float(A.TARGET_POSITION_WEIGHT)


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
    """从 `decision_scenarios_*.json` 取全部情景的选项组合（保持文件里的顺序）。"""
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
    `<code>=A,<code>=A,…`）⇒ 用户能直接与那张表交叉对照；
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
    #: 🔴 破单席清单的**金额口径 = 落地金额**（落地股数 × 节点日价）⇒ 与
    #:    `reprice_node_day.py` 的 `slot_breach` 判据、`probe_node_scenarios.py` P8
    #:    的独立重算**逐位同口径**（旧值写的是「计划目标金额」⇒ 与探针分叉）。
    breaches = [
        {"code": a["code"], "name": a.get("name") or "",
         "amount": round(int(a.get("target_shares") or 0) * float(a.get("price") or 0.0), 4),
         "limit": a.get("slot_limit_amount"),
         "note": a.get("slot_breach_note") or ""}
        for a in actions if a.get("slot_breach")
    ]
    n_positions = sum(1 for a in actions if int(a.get("target_shares") or 0) > 0)
    #: 🔴 **落地持仓全集**（含「本轮不动」的仓位）—— 破单席判定必须落在**全集**上，
    #:    只看 `trades`（变动股数 ≠ 0）会漏掉「已经在仓、且目标仍超单席上限」的席位。
    positions = [
        {
            "code": a["code"],
            "name": a.get("name") or "",
            "current_shares": int(a.get("current_shares") or 0),
            "target_shares": int(a.get("target_shares") or 0),
            #: 🔴 落地持仓的**价格与落地金额**一并落进聚合件 ⇒ 探针 P8 能**独立重算**
            #:    破单席（不信产品给的金额字段）⇒ 探针与产品口径漂移时立刻能被抓到。
            "price": a.get("price"),
            "landed_amount": round(int(a.get("target_shares") or 0) * float(a.get("price") or 0.0), 4),
            "target_amount": a.get("target_amount"),
            "target_weight_pct": round(float(a.get("target_weight") or 0.0) * 100, 4),
            "slot_breach": bool(a.get("slot_breach")),
            "slot_limit_amount": a.get("slot_limit_amount"),
        }
        for a in actions if int(a.get("target_shares") or 0) > 0
    ]
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
        "positions": positions,
        "trades": trades,
    }


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="节点日分歧情景批量重算（2^N 情景）")
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
    #: 🔴 分歧码集合 = 各情景 `choices` 的键（**不写死任何具体代码 / 个数**）
    div_codes: list[str] = []
    for _label, _choices in combos:
        for _code in _choices:
            if _code not in div_codes:
                div_codes.append(_code)

    prices_path = Path(args.prices)
    if not prices_path.is_file():
        raise FileNotFoundError(f"ETF 行情缺失：`{prices_path}` 不存在 ⇒ 无法重算情景")

    tmp_parent = Path(args.out_dir).parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = C.WORK_ROOT
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
        "scope": (f"按用户待拍板的 {len(div_codes)} 项分歧（{' / '.join(div_codes)}），"
                  f"展开 {len(summaries)} 个组合情景，用**节点日价格**重算"
                  "每个情景的买卖清单；**只摆事实、不替用户做决定**"),
        "divergence_codes": list(div_codes),
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
    n_scen = len(summaries)
    #: 🔴 「为什么买不满」**从计划件当场算**，**不许写死某几个标的**
    #:    （上一轮把「3 只货币/现金类 ETF 一手买不起」写死在文案里 ⇒ 口径一变就成了假话）
    plan_actions = plan.get("actions") or []
    dropped = [a for a in plan_actions if int(a.get("target_shares") or 0) == 0]
    unbuyable = []
    for action in dropped:
        try:
            one_lot = float(action.get("price") or 0.0) * LOT
        except (TypeError, ValueError):
            one_lot = 0.0
        if one_lot > slot_limit:
            unbuyable.append(action)
    n_declined = len(dropped) - len(unbuyable)
    hi_scen = max(summaries, key=lambda s: float(s["equity_weight_pct"]))
    lo_scen = min(summaries, key=lambda s: float(s["equity_weight_pct"]))
    notes = [
        f"🔴 **持仓只数达不到 {A_TARGET_POSITIONS} 只目标**：{n_scen} 个情景的落地持仓只数取值为 {n_pos} 只"
        f"（用户目标 = {A_TARGET_POSITIONS} 只等权、每只 ≈ {slot_limit:,.2f} 元）⇒ "
        f"**结构性买不满**，不是选股问题 —— 计划件里有 {len(dropped)} 个席位落地 0 股："
        f"其中 {len(unbuyable)} 个是**一手成本 > 单席 {slot_limit:,.2f} 元**（整手口径买不起），"
        f"{n_declined} 个是**本轮裁决主动不建仓**；这些席位的权重自动归现金。",
        f"🔴 **权益占比区间 {min(s['equity_weight_pct'] for s in summaries):.2f}% ~ "
        f"{max(s['equity_weight_pct'] for s in summaries):.2f}%**"
        f"（最高的是 {hi_scen['label']}、最低的是 {lo_scen['label']}）"
        f"⇒ 与「{A_TARGET_POSITIONS} 只等权」的期望有结构性差距，"
        f"请知悉后再拍板。",
        f"🔴 本表**只摆事实与后果、不替用户做决定**：{n_scen} 个情景平等列出，"
        "**不含任何「建议采用 X」式结论**；`破单席` 是**待裁决的事实**，不是执行失败。",
    ]

    payload = {
        "meta": meta,
        "scenarios": summaries,
        "notes": notes,
        "assertions": [
            {"id": 1, "name": f"{n_scen} 个情景全部重算成功且期末现金非负", "status": "PASS",
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
    div_codes = meta.get("divergence_codes") or []
    if div_codes:
        add(f"- 分歧标的（{len(div_codes)} 项）：{' / '.join(div_codes)}")
    add("")
    add("> ⚠️ **节点日收盘价 ≠ 执行日成交价**：股数是「按节点日价折算」，实际在执行日收盘成交。")
    add(f"> 🔴 本表**只摆事实与后果，不替用户做决定**：{meta['n_scenarios']} 个情景平等列出，"
        "无「建议采用 X」。")
    add("")
    add(f"## 一、{meta['n_scenarios']} 情景总表")
    add("")
    add(f"| 情景（{'/'.join(div_codes) if div_codes else '全部分歧项'}） "
        "| 期末现金 | 现金% | 权益金额 | 权益% | 持仓只数 | 破单席 | 可执行 |")
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
