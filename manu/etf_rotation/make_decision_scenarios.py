#!/usr/bin/env python
"""ETF 轮动调仓 ·「三项分歧一起选」组合矩阵生成器（2³ = 8 情景）。

【用途】
把上一轮 `decision_options_<label>.md` 的**逐项**对照（511880 / 512800 / 563020 三项
各选 A 或 B）展开成 **8 个组合情景**，逐情景给出**账户后果**：三项的最终股数与金额、
期末现金、期末权益金额、权益/现金占比、情景总资产、与账户口径 29,854.67 元的差额、
破单席清单、整手不可建仓清单、是否现金为负（可执行性）。
🔴 **只摆事实与后果，不替用户做决定**：全文不出现任何「建议采用 X」式结论。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/make_decision_scenarios.py
    ... --run-root /mnt/data/siping/runs/etf_rotation --out-dir <目录> --asof-label 20261105

【数据来源（全部只读）】
    * <run-root>/final_rebalance_plan.json      —— 规则层动作 / 价格 / 费用率 / 现金台账
    * <run-root>/decision_options_<label>.json  —— 上一轮逐项对照（**只作交叉校验**，
                                                  不是答案来源；本表数字全部从 plan 独立重算）
    * manu/etf_rotation/account.py              —— 真实账户（CASH / TOTAL_ASSETS /
                                                   load_account_snapshot()）
    * manu/etf_rotation/final_rebalance_plan.py —— 整手常量 LOT（= 100 股）

【口径】
    * 整手：1 手 = `final_rebalance_plan.LOT` = 100 股，与产物 `meta.lot_size` 交叉校验，
      不一致直接抛中文异常；
    * 价格基准：`actions[code].price`（asof 2026-11-03 收盘价），与规则层 `target_shares`
      同口径；账户快照价（2026-11-04）**不参与**股数与市值计算；
    * 权重分母：账户总资产 `A.TOTAL_ASSETS` = 29,854.67 元（= 20% 单席上限 5,970.934 的分母）；
    * 费用：单边费率 = `meta.fee_rate_one_way`（佣金 1.5bp + 滑点 5.0bp = 0.00065），
      **卖出与买入都计费**（卖出：所得 − 卖出费；买入：金额 + 买入费）；
    * 现金台账：`期末现金 = A.CASH + Σ卖出金额 − Σ卖出费 − Σ买入金额 − Σ买入费`；
    * 权益金额 = 期末持仓股数 × 规则层价格；情景总资产 = 期末现金 + 期末权益；
    * 与账户口径的差额 = **价格基差**（账户快照价 vs 规则层价格）+ **本情景全部费用**；
    * 幂等：**不写运行时间戳**（时间戳只取产物 `meta.generated_at`）⇒ 同一输入两次运行
      输出逐字节相同；
    * 无未来函数：只用已交付产物，不调 LLM、不联网。

【断言（自洽探针；结果写进产物并决定退出码）】
    1. A/A/A 情景（全采纳规则层）的期末现金 vs 产物 `cash_flow.cash_end`，偏差 < 0.01 元；
    2. 8 个情景现金均不为负；出现负现金 ⇒ 该情景标「不可执行」且退出码为 1；
    3. 8 个情景总资产（现金 + 权益）的彼此差异 ≤ 全部费用之和（费用是唯一损耗）；
    4. 三个分歧项的选项股数 / 金额与 `decision_options_<label>.json` 逐项一致
       （偏差 < 0.01 元 / 0 股）；
    5. 破单席（权重 > 20%）标记完整性：逐情景独立重算破单席清单并与打标结果比对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from itertools import product
from pathlib import Path
from typing import Any

import pandas as pd

#: 把脚本所在目录放进 sys.path ⇒ 可 import 同目录的 `account` / `final_rebalance_plan`
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import account as A  # noqa: E402
from final_rebalance_plan import LOT  # noqa: E402

#: 产物根默认值（只读输入目录）
DEFAULT_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation")
#: 文件名标签默认值
DEFAULT_ASOF_LABEL = "20261105"
#: 三个待拍板分歧项（顺序固定 ⇒ 情景标签与顺序稳定）
DIVERGENCE_CODES = ("511880", "512800", "563020")
#: 每个分歧项的选项字母
OPTION_LETTERS = ("A", "B")
#: 单席权重上限（%）—— 取自账户口径 `A.TARGET_POSITION_WEIGHT`
SLOT_WEIGHT_LIMIT_PCT = float(A.TARGET_POSITION_WEIGHT) * 100.0
#: 金额容差（元）
TOL_AMOUNT = 0.01


# ------------------------------------------------------------------ 基础工具
def load_json_file(path: Path, label: str) -> dict:
    """读取 JSON 对象；文件缺失 / 非法 / 结构不对一律抛中文异常。"""
    if not path.exists():
        raise FileNotFoundError(f"{label}不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：{path}")
    return payload


def require_field(container: Any, key: str, label: str) -> Any:
    """取必需字段；缺失或为 None 一律抛中文异常（不许用默认值蒙混、不许静默跳过）。"""
    if not isinstance(container, dict) or key not in container or container[key] is None:
        raise RuntimeError(f"{label}缺少必需字段 `{key}`（不许用默认值蒙混、不许静默跳过）")
    return container[key]


def require_number(container: Any, key: str, label: str) -> float:
    """取必需数值字段（缺失 / 非数值 / 非有限值一律抛中文异常）。"""
    value = require_field(container, key, label)
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}的 `{key}` 不是数值：{value!r}") from exc
    if math.isnan(out) or math.isinf(out):
        raise RuntimeError(f"{label}的 `{key}` 不是有限数值：{value!r}")
    return out


def rnd(value: float | None, digits: int = 6) -> float | None:
    """四舍五入到固定小数位（保证 JSON 逐字节稳定、跨次运行不漂移）。"""
    if value is None:
        return None
    return round(float(value), digits)


def fmt_amount(value: float | None) -> str:
    """金额格式化（元，千分位 + 2 位小数）。"""
    return "—" if value is None else f"{float(value):,.2f}"


def fmt_shares(value: float | None) -> str:
    """股数格式化（千分位整数）。"""
    return "—" if value is None else f"{int(value):,}"


def md_cell(text: Any) -> str:
    """把任意文本压成 Markdown 表格单元（转义竖线、压掉换行）。"""
    if text is None:
        return "—"
    out = str(text).replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").strip()
    return out if out else "—"


def sha1_of_file(path: Path) -> str:
    """文件内容 sha1（数据源指纹；只读、不改文件）。"""
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ 输入装载
def load_plan(run_root: Path) -> dict:
    """读取并校验 `final_rebalance_plan.json`。"""
    plan = load_json_file(run_root / "final_rebalance_plan.json", "final_rebalance_plan.json")
    for key in ("meta", "cash_flow", "actions", "llm_final_decision", "next_rebalance"):
        if key not in plan or plan[key] is None:
            raise RuntimeError(f"final_rebalance_plan.json 缺少必需节 `{key}`")
    return plan


def load_options(run_root: Path, asof_label: str) -> dict:
    """读取上一轮逐项对照产物（**只作交叉校验**，不作答案来源）。"""
    return load_json_file(run_root / f"decision_options_{asof_label}.json",
                          f"decision_options_{asof_label}.json")


def build_action_map(plan: dict) -> dict[str, dict]:
    """把 `actions[]` 归集成 `code -> 规则层动作`。"""
    actions: dict[str, dict] = {}
    for raw in require_field(plan, "actions", "final_rebalance_plan.json"):
        code = str(require_field(raw, "code", "actions 的一行"))
        if code in actions:
            raise RuntimeError(f"final_rebalance_plan.json 的 actions 里 `{code}` 重复")
        actions[code] = {
            "code": code,
            "name": str(require_field(raw, "name", f"actions[{code}]")),
            "action": str(require_field(raw, "action", f"actions[{code}]")),
            "current_shares": int(require_number(raw, "current_shares", f"actions[{code}]")),
            "price": require_number(raw, "price", f"actions[{code}]"),
            "target_shares": int(require_number(raw, "target_shares", f"actions[{code}]")),
            "target_amount": require_number(raw, "target_amount", f"actions[{code}]"),
            "target_weight": require_number(raw, "target_weight", f"actions[{code}]"),
            "target_weight_effective": require_number(raw, "target_weight_effective", f"actions[{code}]"),
        }
    if not actions:
        raise RuntimeError("final_rebalance_plan.json 的 actions 为空")
    return actions


def cross_check_account(plan: dict, actions: dict[str, dict], snapshot: pd.DataFrame) -> dict:
    """账户口径交叉校验：产物 meta ↔ `account.py` 常量 ↔ 账户快照 CSV。"""
    plan_meta = require_field(plan, "meta", "final_rebalance_plan.json")
    plan_total = require_number(plan_meta, "total_assets", "final_rebalance_plan.json 的 meta")
    plan_cash = require_number(plan_meta, "cash", "final_rebalance_plan.json 的 meta")
    if abs(plan_total - float(A.TOTAL_ASSETS)) >= TOL_AMOUNT:
        raise RuntimeError(
            f"账户总资产口径不一致：产物 meta.total_assets={plan_total:.2f} 元 vs "
            f"account.TOTAL_ASSETS={A.TOTAL_ASSETS:.2f} 元（差 ≥ 0.01 元）"
        )
    if abs(plan_cash - float(A.CASH)) >= TOL_AMOUNT:
        raise RuntimeError(
            f"账户现金口径不一致：产物 meta.cash={plan_cash:.2f} 元 vs "
            f"account.CASH={A.CASH:.2f} 元（差 ≥ 0.01 元）"
        )
    snapshot_shares = {str(row["code"]): int(row["shares"]) for _, row in snapshot.iterrows()}
    for code, shares in sorted(snapshot_shares.items()):
        if code not in actions:
            raise RuntimeError(f"账户快照里的 `{code}` 在 final_rebalance_plan.json 的 actions 里缺失")
        if actions[code]["current_shares"] != shares:
            raise RuntimeError(
                f"持仓口径不一致：`{code}` 账户快照 {shares} 股 vs 产物 actions.current_shares "
                f"{actions[code]['current_shares']} 股"
            )
    positions = []
    for _, row in snapshot.iterrows():
        code = str(row["code"])
        market_value = float(row["market_value"])
        positions.append({
            "code": code,
            "name": str(row["name"]),
            "shares": int(row["shares"]),
            "price": float(row["price"]),
            "market_value": rnd(market_value, 4),
            "weight_pct": rnd(market_value / float(A.TOTAL_ASSETS) * 100.0, 4),
        })
    return {
        "snapshot_date": A.ACCOUNT_SNAPSHOT_DATE,
        "total_assets": rnd(float(A.TOTAL_ASSETS), 4),
        "cash": rnd(float(A.CASH), 4),
        "market_value": rnd(sum(p["market_value"] for p in positions), 4),
        "n_positions": len(positions),
        "positions": positions,
    }


def cross_check_lot(plan: dict) -> int:
    """整手交叉校验：`final_rebalance_plan.LOT` ↔ 产物 `meta.lot_size`。"""
    plan_meta = require_field(plan, "meta", "final_rebalance_plan.json")
    lot_size = int(require_number(plan_meta, "lot_size", "final_rebalance_plan.json 的 meta"))
    if lot_size != int(LOT):
        raise RuntimeError(
            f"整手口径不一致：final_rebalance_plan.LOT={LOT} vs 产物 meta.lot_size={lot_size}"
        )
    return lot_size


# ------------------------------------------------------------------ 分歧项选项
def resolve_divergence_options(plan: dict, actions: dict[str, dict],
                               total_assets: float) -> dict[str, dict]:
    """给出三个分歧项的 A / B 两个选项的股数与金额（**独立从 plan 重算**）。

    * 选项 A = 规则层实际落地（`actions[code].target_shares`）；
    * 选项 B = LLM 最终裁决：原文给了手数就按手数换算，否则按目标权重折整手。
    """
    final_targets = require_field(
        require_field(plan, "llm_final_decision", "final_rebalance_plan.json"),
        "targets", "final_rebalance_plan.json 的 llm_final_decision")
    options: dict[str, dict] = {}
    for code in DIVERGENCE_CODES:
        action = actions.get(code)
        if action is None:
            raise RuntimeError(f"分歧标的 `{code}` 不在 final_rebalance_plan.json 的 actions 里")
        price = action["price"]
        final = final_targets.get(code)
        if not isinstance(final, dict):
            raise RuntimeError(f"分歧标的 `{code}` 在 llm_final_decision.targets 里缺条目")
        lots = final.get("lots")
        if lots is not None:
            llm_shares = int(lots) * int(LOT)
            llm_basis = f"LLM 最终裁决原文「买 {lots} 手」× LOT={LOT}"
        else:
            target_pct = require_number(final, "target_pct", f"llm_final_decision.targets[{code}]")
            target_amount = total_assets * target_pct / 100.0
            llm_shares = int(math.floor(target_amount / price / LOT) * LOT)
            llm_basis = (f"LLM 最终裁决目标权重 {target_pct:.4f}% ⇒ 目标金额 {target_amount:.4f} 元 "
                         f"⇒ floor(目标金额 ÷ 价格 ÷ {LOT}) × {LOT}")
        if action["target_shares"] % int(LOT) != 0 or llm_shares % int(LOT) != 0:
            raise RuntimeError(f"分歧标的 `{code}` 的股数不是 {LOT} 股整手")
        options[code] = {
            "code": code,
            "name": action["name"],
            "price": rnd(price, 6),
            "one_lot_cost": rnd(price * LOT, 4),
            "option_a": {
                "letter": "A",
                "label": "选项 A（采纳规则层）",
                "source": "final_rebalance_plan.json → actions[].target_shares",
                "shares": int(action["target_shares"]),
                "amount": rnd(action["target_shares"] * price, 4),
                "weight_pct": rnd(action["target_shares"] * price / total_assets * 100.0, 4),
                "basis": "规则层 `target_shares`（整手向下取整后的实际落地股数）",
                "note": "",
            },
            "option_b": {
                "letter": "B",
                "label": "选项 B（采纳 LLM 最终裁决）",
                "source": "final_rebalance_plan.json → llm_final_decision.targets",
                "shares": int(llm_shares),
                "amount": rnd(llm_shares * price, 4),
                "weight_pct": rnd(llm_shares * price / total_assets * 100.0, 4),
                "basis": llm_basis,
                "note": "",
            },
        }
    return options


def annotate_option_notes(options: dict[str, dict], slot_limit: float) -> None:
    """给每个选项补一句「事实性说明」（整手买不起 / 破单席上限），不做任何建议。"""
    for item in options.values():
        for letter in OPTION_LETTERS:
            opt = item[f"option_{letter.lower()}"]
            notes: list[str] = []
            if opt["shares"] == 0 and item["one_lot_cost"] > slot_limit:
                notes.append(f"整手买不起：一手 {LOT} × {item['price']} = "
                             f"{fmt_amount(item['one_lot_cost'])} 元 > 单席上限 "
                             f"{fmt_amount(slot_limit)} 元 ⇒ 落地 0 股")
            if opt["shares"] > 0 and float(opt["weight_pct"]) > SLOT_WEIGHT_LIMIT_PCT + 1e-9:
                notes.append(f"破单席 {SLOT_WEIGHT_LIMIT_PCT:.0f}% 上限（落地 "
                             f"{float(opt['weight_pct']):.2f}%）")
            if opt["shares"] == 0 and not notes:
                notes.append("落地 0 股（该槽位在本情景不建仓）")
            opt["note"] = "；".join(notes)


# ------------------------------------------------------------------ 情景推演
def build_scenario(label: str, choices: dict[str, str], actions: dict[str, dict],
                   options: dict[str, dict], unbuildable_slots: list[dict],
                   cash_begin: float, fee_rate: float, total_assets: float,
                   slot_limit: float) -> dict:
    """推演一个组合情景的账户后果（现金台账 + 权益 + 破单席 + 整手可建仓）。"""
    holdings = {code: act["target_shares"] for code, act in actions.items()}
    for code in DIVERGENCE_CODES:
        holdings[code] = options[code][f"option_{choices[code].lower()}"]["shares"]

    sell_proceeds = 0.0
    sell_fees = 0.0
    buy_amount = 0.0
    buy_fees = 0.0
    trades: list[dict] = []
    for code in sorted(actions):
        action = actions[code]
        delta = holdings[code] - action["current_shares"]
        if delta == 0:
            continue
        amount = abs(delta) * action["price"]
        fee = amount * fee_rate
        if delta > 0:
            buy_amount += amount
            buy_fees += fee
            side = "买入"
        else:
            sell_proceeds += amount
            sell_fees += fee
            side = "卖出"
        trades.append({
            "code": code,
            "name": action["name"],
            "side": side,
            "delta_shares": int(delta),
            "price": rnd(action["price"], 6),
            "amount": rnd(amount, 4),
            "fee": rnd(fee, 6),
        })

    cash_end = cash_begin + sell_proceeds - sell_fees - buy_amount - buy_fees
    total_fees = sell_fees + buy_fees
    equity_amount = sum(holdings[code] * actions[code]["price"] for code in actions)
    scenario_total = cash_end + equity_amount

    positions = []
    for code in sorted(actions):
        shares = holdings[code]
        if shares <= 0:
            continue
        amount = shares * actions[code]["price"]
        positions.append({
            "code": code,
            "name": actions[code]["name"],
            "shares": int(shares),
            "price": rnd(actions[code]["price"], 6),
            "amount": rnd(amount, 4),
            "weight_pct": rnd(amount / total_assets * 100.0, 4),
        })
    breaches = [p for p in positions if float(p["weight_pct"]) > SLOT_WEIGHT_LIMIT_PCT + 1e-9]

    unbuildable_codes = {str(slot["code"]) for slot in unbuildable_slots}
    unbuildable_items: list[dict] = []
    empty_slot_items: list[dict] = []
    for code in sorted(actions):
        action = actions[code]
        if action["target_weight"] <= 0.0 or holdings[code] > 0:
            continue
        if code in unbuildable_codes:
            slot = next(s for s in unbuildable_slots if str(s["code"]) == code)
            item = {
                "code": code,
                "name": action["name"],
                "type": "整手不可建仓",
                "target_amount": rnd(float(slot["target_amount"]), 4),
                "one_lot_cost": rnd(float(slot["one_lot_cost"]), 4),
                "price": rnd(float(slot["price"]), 6),
                "reason": str(slot["reason"]),
            }
            unbuildable_items.append(item)
            empty_slot_items.append(dict(item))
        else:
            empty_slot_items.append({
                "code": code,
                "name": action["name"],
                "type": "主动不建仓（分歧项选 B）",
                "target_amount": rnd(action["target_amount"], 4),
                "one_lot_cost": rnd(action["price"] * LOT, 4),
                "price": rnd(action["price"], 6),
                "reason": "分歧项选 B（LLM 最终裁决不建仓），该槽位权重归现金",
            })

    diff_vs_account = scenario_total - total_assets
    price_basis_diff = total_assets - (
        cash_begin + sum(act["current_shares"] * act["price"] for act in actions.values())
    )
    expected_total = total_assets - price_basis_diff - total_fees

    divergence_items = {}
    for code in DIVERGENCE_CODES:
        opt = options[code][f"option_{choices[code].lower()}"]
        divergence_items[code] = {
            "option": opt["letter"],
            "name": options[code]["name"],
            "shares": opt["shares"],
            "price": options[code]["price"],
            "amount": opt["amount"],
            "weight_pct": opt["weight_pct"],
            "note": opt["note"],
        }

    cash_negative = cash_end < 0.0
    return {
        "label": label,
        "choices": {code: choices[code] for code in DIVERGENCE_CODES},
        "divergence_items": divergence_items,
        "positions": positions,
        "trades": trades,
        "ledger": {
            "cash_begin": rnd(cash_begin, 10),
            "sell_proceeds": rnd(sell_proceeds, 10),
            "sell_fees": rnd(sell_fees, 10),
            "buy_amount": rnd(buy_amount, 10),
            "buy_fees": rnd(buy_fees, 10),
            "total_fees": rnd(total_fees, 10),
            "cash_end": rnd(cash_end, 10),
            "equity_amount": rnd(equity_amount, 10),
            "total_assets": rnd(scenario_total, 10),
        },
        "cash_end": rnd(cash_end, 10),
        "cash_weight_pct": rnd(cash_end / total_assets * 100.0, 4),
        "equity_amount": rnd(equity_amount, 10),
        "equity_weight_pct": rnd(equity_amount / total_assets * 100.0, 4),
        "cash_share_pct": rnd(cash_end / scenario_total * 100.0, 4),
        "equity_share_pct": rnd(equity_amount / scenario_total * 100.0, 4),
        "total_assets": rnd(scenario_total, 10),
        "diff_vs_account_total_assets": rnd(diff_vs_account, 10),
        "diff_breakdown": {
            "account_total_assets": rnd(total_assets, 4),
            "price_basis_diff": rnd(price_basis_diff, 4),
            "fees": rnd(total_fees, 10),
            "identity": "情景总资产 = 账户总资产 − 价格基差 − 本情景全部费用",
            "residual": rnd(scenario_total - expected_total, 10),
        },
        "slot_breaches": breaches,
        "breaks_slot_cap": bool(breaches),
        "unbuildable_lot_items": unbuildable_items,
        "empty_slot_items": empty_slot_items,
        "cash_negative": bool(cash_negative),
        "executable": not cash_negative,
        "executable_note": "现金为负 ⇒ 不可执行" if cash_negative else "现金非负 ⇒ 现金层面可执行",
    }


def build_scenarios(actions: dict[str, dict], options: dict[str, dict],
                    unbuildable_slots: list[dict], cash_begin: float, fee_rate: float,
                    total_assets: float, slot_limit: float) -> list[dict]:
    """按 2³ 展开 8 个组合情景（顺序固定 ⇒ 输出稳定）。"""
    scenarios = []
    for combo in product(OPTION_LETTERS, repeat=len(DIVERGENCE_CODES)):
        choices = {code: letter for code, letter in zip(DIVERGENCE_CODES, combo)}
        label = ",".join(f"{code}={choices[code]}" for code in DIVERGENCE_CODES)
        scenarios.append(build_scenario(label, choices, actions, options, unbuildable_slots,
                                         cash_begin, fee_rate, total_assets, slot_limit))
    return scenarios


# ------------------------------------------------------------------ 断言
def build_assertions(plan: dict, options: dict[str, dict], options_ref: dict,
                     scenarios: list[dict]) -> list[dict]:
    """5 条自洽断言（结果写进产物；任一 FAIL ⇒ 退出码 1）。"""
    assertions: list[dict] = []

    # --- 断言 1：A/A/A 期末现金 vs 产物 cash_flow.cash_end --------------------
    plan_cash_end = require_number(require_field(plan, "cash_flow", "final_rebalance_plan.json"),
                                   "cash_end", "final_rebalance_plan.json 的 cash_flow")
    base = next(sc for sc in scenarios if sc["label"] == "511880=A,512800=A,563020=A")
    diff1 = abs(float(base["cash_end"]) - plan_cash_end)
    assertions.append({
        "id": 1,
        "name": "A/A/A（全采纳规则层）期末现金与产物 cash_flow.cash_end 一致",
        "status": "PASS" if diff1 < TOL_AMOUNT else "FAIL",
        "tolerance": TOL_AMOUNT,
        "expected": rnd(plan_cash_end, 6),
        "actual": rnd(base["cash_end"], 6),
        "diff": rnd(diff1, 10),
        "detail": f"差 {diff1:.10f} 元（容差 0.01 元）",
    })

    # --- 断言 2：8 个情景现金均不为负 ---------------------------------------
    negatives = [sc["label"] for sc in scenarios if sc["cash_negative"]]
    min_cash = min(float(sc["cash_end"]) for sc in scenarios)
    assertions.append({
        "id": 2,
        "name": "8 个情景期末现金均不为负（负现金 ⇒ 不可执行）",
        "status": "PASS" if not negatives else "FAIL",
        "tolerance": 0.0,
        "expected": "无负现金情景",
        "actual": f"负现金情景 {negatives or '无'}；最小期末现金 {min_cash:.2f} 元",
        "diff": None,
        "detail": f"最小期末现金 {min_cash:.2f} 元（账户现金 {float(A.CASH):.2f} 元起算）",
    })

    # --- 断言 3：总资产彼此差异 ≤ 全部费用之和 -------------------------------
    totals = [float(sc["total_assets"]) for sc in scenarios]
    spread = max(totals) - min(totals)
    fee_sum = sum(float(sc["ledger"]["total_fees"]) for sc in scenarios)
    fee_max = max(float(sc["ledger"]["total_fees"]) for sc in scenarios)
    assertions.append({
        "id": 3,
        "name": "8 个情景总资产（现金 + 权益）的彼此差异 ≤ 全部费用之和",
        "status": "PASS" if spread <= fee_sum + 1e-9 else "FAIL",
        "tolerance": rnd(fee_sum, 10),
        "expected": rnd(fee_sum, 10),
        "actual": rnd(spread, 10),
        "diff": rnd(spread, 10),
        "detail": (f"最大总资产 {max(totals):.2f} 元、最小 {min(totals):.2f} 元，"
                   f"最大偏差 {spread:.6f} 元 ≤ 8 情景费用之和 {fee_sum:.6f} 元"
                   f"（单情景最大费用 {fee_max:.6f} 元亦为上界）"),
    })

    # --- 断言 4：三个分歧项的选项股数 / 金额与 decision_options 逐项一致 -------
    per_code = {str(item["code"]): item for item in
                require_field(require_field(options_ref, "options", "decision_options json"),
                              "per_code_options", "decision_options json 的 options")}
    mismatches = []
    max_d_shares = 0
    max_d_amount = 0.0
    for code in DIVERGENCE_CODES:
        ref = per_code.get(code)
        if ref is None:
            raise RuntimeError(f"decision_options json 里没有分歧标的 `{code}` 的选项")
        for letter in OPTION_LETTERS:
            mine = options[code][f"option_{letter.lower()}"]
            theirs = require_field(ref, f"option_{letter.lower()}", f"decision_options 的 {code}")
            d_shares = abs(int(mine["shares"]) - int(require_number(
                theirs, "target_shares", f"decision_options 的 {code} option_{letter}")))
            d_amount = abs(float(mine["amount"]) - float(require_number(
                theirs, "landing_amount", f"decision_options 的 {code} option_{letter}")))
            max_d_shares = max(max_d_shares, d_shares)
            max_d_amount = max(max_d_amount, d_amount)
            if d_shares >= 1 or d_amount >= TOL_AMOUNT:
                mismatches.append(f"{code} 选项 {letter}：股数差 {d_shares} 股 / 金额差 {d_amount:.4f} 元")
    assertions.append({
        "id": 4,
        "name": "三个分歧项的选项股数 / 金额与 decision_options 逐项一致（< 0.01 元 / 0 股）",
        "status": "PASS" if not mismatches else "FAIL",
        "tolerance": TOL_AMOUNT,
        "expected": "6 个选项全部一致",
        "actual": "全部一致" if not mismatches else "；".join(mismatches),
        "diff": None,
        "detail": (f"6 个选项逐项比对：最大股数差 {max_d_shares} 股、最大金额差 "
                   f"{max_d_amount:.6f} 元（容差 0.01 元 / 0 股）；本表数字独立由 "
                   f"final_rebalance_plan.json 重算，decision_options 只作交叉校验"),
    })

    # --- 断言 5：破单席标记完整性（独立重算 vs 打标结果） ---------------------
    recomputed_ok = True
    detail_lines = []
    for sc in scenarios:
        expected_codes = sorted(p["code"] for p in sc["positions"]
                                if float(p["weight_pct"]) > SLOT_WEIGHT_LIMIT_PCT + 1e-9)
        flagged_codes = sorted(p["code"] for p in sc["slot_breaches"])
        if expected_codes != flagged_codes or sc["breaks_slot_cap"] != bool(expected_codes):
            recomputed_ok = False
            detail_lines.append(f"{sc['label']} 重算 {expected_codes} vs 打标 {flagged_codes}")
    flagged_scenarios = [sc["label"] for sc in scenarios if sc["breaks_slot_cap"]]
    assertions.append({
        "id": 5,
        "name": "破单席（权重 > 20%）标记完整性：逐情景独立重算并与打标结果比对",
        "status": "PASS" if recomputed_ok else "FAIL",
        "tolerance": 0.0,
        "expected": "破单席清单与独立重算完全一致",
        "actual": ("一致；破单席情景：" + (", ".join(flagged_scenarios) if flagged_scenarios else "无"))
                  if recomputed_ok else "；".join(detail_lines),
        "diff": None,
        "detail": (f"破单席情景 {len(flagged_scenarios)}/{len(scenarios)} 个"
                   f"（{', '.join(flagged_scenarios) if flagged_scenarios else '无'}）；"
                   f"破单席是**待用户裁决的事实**，不是失败项"),
    })
    return assertions


# ------------------------------------------------------------------ 主载荷
def build_payload(plan: dict, options_ref: dict, snapshot: pd.DataFrame,
                  run_root: Path, asof_label: str) -> dict:
    """组装 JSON 载荷（Markdown 是它的渲染）。"""
    plan_meta = require_field(plan, "meta", "final_rebalance_plan.json")
    cash_flow = require_field(plan, "cash_flow", "final_rebalance_plan.json")
    lot_size = cross_check_lot(plan)
    actions = build_action_map(plan)
    account = cross_check_account(plan, actions, snapshot)

    total_assets = float(A.TOTAL_ASSETS)
    cash_begin = float(A.CASH)
    fee_rate = require_number(plan_meta, "fee_rate_one_way", "final_rebalance_plan.json 的 meta")
    slot_limit = total_assets * float(A.TARGET_POSITION_WEIGHT)
    unbuildable_slots = list(require_field(cash_flow, "unbuildable_slots",
                                           "final_rebalance_plan.json 的 cash_flow"))

    options = resolve_divergence_options(plan, actions, total_assets)
    annotate_option_notes(options, slot_limit)
    scenarios = build_scenarios(actions, options, unbuildable_slots, cash_begin, fee_rate,
                                total_assets, slot_limit)
    assertions = build_assertions(plan, options, options_ref, scenarios)

    fingerprints = {
        "final_rebalance_plan.json": sha1_of_file(run_root / "final_rebalance_plan.json"),
        f"decision_options_{asof_label}.json": sha1_of_file(run_root / f"decision_options_{asof_label}.json"),
        "account_snapshot.csv": sha1_of_file(A.ACCOUNT_SNAPSHOT_CSV),
    }

    meta = {
        "asof": str(require_field(plan_meta, "asof", "final_rebalance_plan.json 的 meta")),
        "asof_label": asof_label,
        "plan_generated_at": str(require_field(plan_meta, "generated_at",
                                               "final_rebalance_plan.json 的 meta")),
        "generator": "manu/etf_rotation/make_decision_scenarios.py",
        "description": "三个待拍板分歧项（511880 / 512800 / 563020）的 2³ = 8 组合情景账户后果矩阵",
        "run_root": str(run_root),
        "source_plan": str(run_root / "final_rebalance_plan.json"),
        "source_options": str(run_root / f"decision_options_{asof_label}.json"),
        "source_fingerprints_sha1": fingerprints,
        "fee_rate_one_way": rnd(fee_rate, 10),
        "commission_bp": rnd(require_number(plan_meta, "commission_bp",
                                            "final_rebalance_plan.json 的 meta"), 6),
        "slippage_bp": rnd(require_number(plan_meta, "slippage_bp",
                                          "final_rebalance_plan.json 的 meta"), 6),
        "fee_note": "单边费率；卖出与买入都计费",
        "lot_size": lot_size,
        "slot_weight_limit_pct": SLOT_WEIGHT_LIMIT_PCT,
        "slot_amount_limit": rnd(slot_limit, 4),
        "slot_amount_limit_rounded": rnd(float(A.TARGET_POSITION_AMOUNT), 4),
        "account_total_assets": rnd(total_assets, 4),
        "account_cash": rnd(cash_begin, 4),
        "account_snapshot_date": A.ACCOUNT_SNAPSHOT_DATE,
        "price_basis": "actions[].price（asof 2026-11-03 收盘价）；账户快照价不参与股数与市值计算",
        "total_assets_caliber": "情景总资产 = 期末现金 + 期末权益；权重分母统一为账户总资产 29,854.67 元",
        "n_scenarios": len(scenarios),
        "divergence_codes": list(DIVERGENCE_CODES),
        "next_rebalance": require_field(plan, "next_rebalance", "final_rebalance_plan.json"),
    }

    notes = [
        "🔴 本表**不替用户做决定**：只列每个组合情景的事实与后果 + 可执行性标记，"
        "全文不含任何「建议采用 X」式结论。",
        "价格基准 asof = 2026-11-03（`actions[].price`）；账户快照价（2026-11-04）只用于持仓对账，"
        "不参与股数与市值计算。",
        "权重与占比的分母统一是**账户总资产 29,854.67 元**（与产物 `cash_flow` 的 realized 口径一致）"
        "⇒ 「现金% + 权益% < 100%」，缺口 = (价格基差 + 本情景费用) ÷ 29,854.67。",
        "情景总资产 = 期末现金 + 期末权益；与账户口径 29,854.67 元的差额 = **价格基差**"
        "（账户快照价 vs 规则层价格，72.30 元）+ **本情景全部费用**（费用是唯一损耗）。",
        "破单席 = 该标的落地权重 > 20%（等价于金额 > 5,970.93 元）；它是**待用户裁决的事实**，"
        "不是执行失败。",
        "整手不可建仓 = 目标槽位金额 < 一手成本（100 × 价格）⇒ 落地 0 股、权重归现金；"
        "「主动不建仓」是分歧项选 B 的结果，与整手约束无关。",
        "期末现金 = 15,518.87 + Σ卖出金额 − Σ卖出费 − Σ买入金额 − Σ买入费；"
        "单边费率 0.00065（佣金 1.5bp + 滑点 5.0bp），卖出与买入都计费。",
        "幂等：不写运行时间戳（时间戳只取产物 `meta.generated_at`）⇒ 同一输入两次运行逐字节相同。",
    ]
    disclaimer = (
        "本表由已交付产物（final_rebalance_plan.json / decision_options json / 真实账户快照）机械折算生成，"
        "**只摆事实与后果、不做决策建议**，**不构成投资建议**；历史统计不代表未来表现，"
        "使用者须自行判断并自行承担全部风险。"
    )

    return {
        "meta": meta,
        "account": account,
        "divergence_items": options,
        "scenarios": scenarios,
        "assertions": assertions,
        "notes": notes,
        "disclaimer": disclaimer,
    }


# ------------------------------------------------------------------ Markdown 渲染
def summarize_slot_items(scenarios: list[dict], item_type: str) -> str:
    """汇总某一类「槽位落地 0 股」的标的与出现的情景数（动态生成，不硬编码）。"""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for sc in scenarios:
        seen = {str(item["code"]) for item in sc["empty_slot_items"]
                if str(item["type"]) == item_type}
        for code in seen:
            counts[code] = counts.get(code, 0) + 1
            for item in sc["empty_slot_items"]:
                if str(item["code"]) == code:
                    names[code] = str(item["name"])
    if not counts:
        return "无"
    parts = [f"`{code}` {names[code]}（{counts[code]}/{len(scenarios)} 情景）"
             for code in sorted(counts)]
    return "、".join(parts)


def render_markdown(payload: dict) -> str:
    """把 JSON 载荷渲染成 Markdown（8 行情景总表 + 分歧项明细 + 断言 + 口径 + 免责）。"""
    lines: list[str] = []
    add = lines.append
    meta = payload["meta"]
    add("# ETF 调仓 · 三项分歧组合矩阵（8 情景）")
    add("")
    add(f"- 数据 asof（价格基准）：{meta['asof']}　｜　文件名标签：{meta['asof_label']}　｜　"
        f"计划件生成时间：{meta['plan_generated_at']}")
    add(f"- 账户总资产 {meta['account_total_assets']:,.2f} 元（现金 {meta['account_cash']:,.2f} 元，"
        f"快照日 {meta['account_snapshot_date']}）　｜　单席上限 {meta['slot_amount_limit']:,.2f} 元 / "
        f"{meta['slot_weight_limit_pct']:.0f}%")
    add(f"- 单边费率 {meta['fee_rate_one_way']}（佣金 {meta['commission_bp']}bp + 滑点 "
        f"{meta['slippage_bp']}bp，**卖出与买入都计费**）　｜　整手 {meta['lot_size']} 股　｜　"
        f"情景数 {meta['n_scenarios']}")
    add("- 🔴 本表**只摆事实与后果，不替用户做决定**：全文不含任何「建议采用 X」式结论。")
    add("")

    # 一、情景总表
    add("## 一、情景总表（2³ = 8 个组合）")
    add("")
    add("| 标签 | 511880 股数 | 512800 股数 | 563020 股数 | 期末现金 | 现金% | 权益金额 | 权益% | "
        "总资产 | 破单席 | 可执行 |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for sc in payload["scenarios"]:
        div = sc["divergence_items"]
        breaches = "、".join(f"{p['code']}({float(p['weight_pct']):.2f}%)" for p in sc["slot_breaches"])
        add(f"| {md_cell(sc['label'])} | {fmt_shares(div['511880']['shares'])} | "
            f"{fmt_shares(div['512800']['shares'])} | {fmt_shares(div['563020']['shares'])} | "
            f"{fmt_amount(sc['cash_end'])} | {float(sc['cash_weight_pct']):.2f}% | "
            f"{fmt_amount(sc['equity_amount'])} | {float(sc['equity_weight_pct']):.2f}% | "
            f"{fmt_amount(sc['total_assets'])} | {md_cell(breaches) if breaches else '—'} | "
            f"{'是' if sc['executable'] else '**否（现金为负）**'} |")
    add("")
    add("- 「现金% / 权益%」的分母是**账户总资产 29,854.67 元**（与产物 `cash_flow` realized 口径一致）；"
        "「总资产」= 期末现金 + 期末权益。")
    add("- 「破单席」= 该标的落地权重 > 20%（金额 > 5,970.93 元）；这是**待用户裁决的事实**，不是失败。")
    add("- 「可执行」= 期末现金不为负（负现金 ⇒ 该情景不可执行）。")
    add(f"- 整手不可建仓（目标槽位 < 一手成本 ⇒ 落地 0 股、权重归现金）："
        f"{summarize_slot_items(payload['scenarios'], '整手不可建仓')}。")
    add(f"- 主动不建仓（分歧项选 B，槽位权重归现金）："
        f"{summarize_slot_items(payload['scenarios'], '主动不建仓（分歧项选 B）')}。")
    add("")

    # 二、分歧项明细
    add("## 二、三个分歧项的选项明细")
    add("")
    for index, code in enumerate(meta["divergence_codes"], start=1):
        item = payload["divergence_items"][code]
        add(f"### 2.{index} {code} {item['name']}（价格 {item['price']}，一手 "
            f"{fmt_amount(item['one_lot_cost'])} 元）")
        add("")
        for letter in OPTION_LETTERS:
            opt = item[f"option_{letter.lower()}"]
            add(f"- **选项 {letter}**：{fmt_shares(opt['shares'])} 股 / {fmt_amount(opt['amount'])} 元"
                f"（落地权重 {float(opt['weight_pct']):.2f}%）—— {md_cell(opt['basis'])}"
                + (f"；{md_cell(opt['note'])}" if opt["note"] else ""))
        add("")

    # 三、断言结果
    add("## 三、断言结果（自洽探针）")
    add("")
    add("| # | 断言 | 结果 | 关键数字 |")
    add("|---|---|---|---|")
    for item in payload["assertions"]:
        add(f"| {item['id']} | {md_cell(item['name'])} | **{item['status']}** | {md_cell(item['detail'])} |")
    add("")

    # 四、口径说明
    add("## 四、口径说明")
    add("")
    for note in payload["notes"]:
        add(f"- {md_cell(note)}")
    add("")

    # 五、免责声明
    add("## 五、免责声明")
    add("")
    add(payload["disclaimer"])
    add("")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="生成「三项分歧一起选」的 ETF 调仓组合矩阵（JSON + Markdown）")
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT),
                        help="已交付产物所在目录（只读输入，默认 %(default)s）")
    parser.add_argument("--out-dir", default=None,
                        help="输出目录（默认与 --run-root 相同）")
    parser.add_argument("--asof-label", default=DEFAULT_ASOF_LABEL,
                        help="文件名标签（默认 %(default)s）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = parse_args(argv)
    run_root = Path(args.run_root).expanduser()
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else run_root
    if not run_root.is_dir():
        raise FileNotFoundError(f"产物根目录不存在：{run_root}")
    plan = load_plan(run_root)
    options_ref = load_options(run_root, args.asof_label)
    snapshot = A.load_account_snapshot()
    payload = build_payload(plan, options_ref, snapshot, run_root, args.asof_label)
    markdown = render_markdown(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"decision_scenarios_{args.asof_label}.json"
    md_path = out_dir / f"decision_scenarios_{args.asof_label}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")

    print(f"[decision-scenarios] asof={payload['meta']['asof']} "
          f"（价格基准）｜账户总资产 {payload['meta']['account_total_assets']:,.2f} 元 "
          f"｜单边费率 {payload['meta']['fee_rate_one_way']}｜整手 {payload['meta']['lot_size']} 股")
    print(f"[decision-scenarios] 三个分歧项 {', '.join(payload['meta']['divergence_codes'])} "
          f"× 2 选项 = {payload['meta']['n_scenarios']} 个情景：")
    for sc in payload["scenarios"]:
        div = sc["divergence_items"]
        breaches = "、".join(f"{p['code']}({float(p['weight_pct']):.2f}%)" for p in sc["slot_breaches"])
        print(f"  {sc['label']}: 511880={div['511880']['shares']}股 512800={div['512800']['shares']}股 "
              f"563020={div['563020']['shares']}股 | 期末现金 {sc['cash_end']:>10,.2f} 元 "
              f"({float(sc['cash_weight_pct']):.2f}%) | 权益 {sc['equity_amount']:>10,.2f} 元 "
              f"({float(sc['equity_weight_pct']):.2f}%) | 总资产 {sc['total_assets']:>10,.2f} 元 "
              f"| 破单席 {breaches or '无'} | 可执行 {'是' if sc['executable'] else '否'}")
    for item in payload["assertions"]:
        print(f"[decision-scenarios] 断言 {item['id']}: {item['status']} —— {item['name']}"
              f"｜{item['detail']}")
    print(f"[decision-scenarios] wrote {json_path} ({json_path.stat().st_size} B)")
    print(f"[decision-scenarios] wrote {md_path} ({md_path.stat().st_size} B)")

    failed = [item["id"] for item in payload["assertions"] if item["status"] != "PASS"]
    if failed:
        print(f"[decision-scenarios] 断言未通过：{failed} ⇒ 退出码 1")
        return 1
    print("[decision-scenarios] 5 条断言全部 PASS ⇒ 退出码 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
