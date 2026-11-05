#!/usr/bin/env python
"""ETF 轮动调仓 ·「规则层 vs LLM 层」分歧对照选项表生成器。

【用途】
把 `final_rebalance_plan.json` 里「qlib 规则层」与「TradingAgents LLM 层」的逐标的分歧，
对齐到**可执行的股数 / 金额 / 权重 / 现金影响**，产出 JSON + Markdown 双份对照表。
🔴 **只摆数字与后果，不替用户做决定**：全文并列「选项 A / 选项 B」，不出现任何
「建议采用 X」式结论；不可行约束（整手买不起、破单席上限、口径互斥）显式标注。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/make_decision_options.py
    ... --run-root /mnt/data/siping/runs/etf_rotation --out-dir <目录> --asof-label 20261105

【数据来源（全部只读）】
    * <run-root>/final_rebalance_plan.json    —— 规则层 + LLM 层对齐后的主产物
    * <run-root>/tradingagents_decision.json  —— TradingAgents LLM 原始决策
    * manu/etf_rotation/account.py            —— 真实账户快照（load_account_snapshot()）
    * manu/etf_rotation/final_rebalance_plan.py —— 整手常量 LOT（= 100 股）

【口径】
    * 整手：1 手 = `final_rebalance_plan.LOT` = 100 股，并与产物 `meta.lot_size` 交叉校验；
    * 价格基准：统一取 `actions[code].price`（asof 2026-11-03 收盘价），与规则层
      `target_shares` 同口径；LLM 原文用的是 2026-11-30 收盘价（本机无该日价格）⇒
      LLM 侧股数一律按规则层价格基准**重新折算**，原文股数并列展示、不混称；
    * 权重换股：目标金额 = 总资产 × 目标权重；股数 = floor(目标金额 / 价格 / LOT) × LOT；
    * 费用：单边费率 = `meta.fee_rate_one_way`（佣金 1.5bp + 滑点 5.0bp），费用 = 成交金额 × 费率；
    * 分歧阈值：2.0pp，沿用 `final_rebalance_plan.py` 冲突判定的同一口径；
    * 幂等：所有字段均派生自已交付产物，**不写运行时间戳** ⇒ 同一输入两次运行输出逐字节相同；
    * 无未来函数：只用已交付产物，不调用 LLM、不联网。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

#: 把脚本所在目录放进 sys.path ⇒ 可 import 同目录的 `account` / `final_rebalance_plan`
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import account as A  # noqa: E402
from final_rebalance_plan import LOT  # noqa: E402

#: 分歧判定阈值（pp）——沿用 `final_rebalance_plan.py` 的冲突判定口径（2.0pp）
DIVERGENCE_THRESHOLD_PP = 2.0
#: 产物根默认值（只读输入目录）
DEFAULT_RUN_ROOT = Path("/mnt/data/siping/runs/etf_rotation")
#: 文件名标签默认值
DEFAULT_ASOF_LABEL = "20261105"


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


def require_field(container: dict, key: str, label: str) -> Any:
    """取必需字段；缺失或为 None 一律抛中文异常（不许用默认值蒙混）。"""
    if not isinstance(container, dict) or key not in container or container[key] is None:
        raise RuntimeError(f"{label}缺少必需字段 `{key}`（不许用默认值蒙混、不许静默跳过）")
    return container[key]


def require_number(container: dict, key: str, label: str) -> float:
    """取必需数值字段（缺失 / 非有限值一律抛中文异常）。"""
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


# ------------------------------------------------------------------ 输入装载
def load_plan(run_root: Path) -> dict:
    """读取并校验 `final_rebalance_plan.json`。"""
    path = run_root / "final_rebalance_plan.json"
    plan = load_json_file(path, "规则层/LLM 层主产物 final_rebalance_plan.json")
    for key in ("meta", "next_rebalance", "cash_flow", "actions", "llm_conflicts",
                "llm_final_decision"):
        require_field(plan, key, "final_rebalance_plan.json")
    if not plan["actions"]:
        raise RuntimeError("final_rebalance_plan.json 的 `actions` 为空：没有规则层动作可对照")
    if not plan["llm_conflicts"]:
        raise RuntimeError("final_rebalance_plan.json 的 `llm_conflicts` 为空：没有分歧可比对")
    final = plan["llm_final_decision"]
    if not isinstance(final, dict) or not final.get("available"):
        raise RuntimeError(
            "final_rebalance_plan.json 的 `llm_final_decision.available` 为假："
            "LLM 最终裁决缺失，无法生成对照表（不许跳过）"
        )
    lot_size = require_number(plan["meta"], "lot_size", "final_rebalance_plan.json 的 meta")
    if int(lot_size) != int(LOT):
        raise RuntimeError(
            f"整手口径冲突：产物 meta.lot_size={lot_size} 与 final_rebalance_plan.LOT={LOT} 不一致"
        )
    return plan


def load_decision(run_root: Path) -> dict:
    """读取并校验 `tradingagents_decision.json`（LLM 原始决策）。"""
    path = run_root / "tradingagents_decision.json"
    decision = load_json_file(path, "TradingAgents 原始决策 tradingagents_decision.json")
    for key in ("status", "signal", "meta", "risk_debate", "final_trade_decision"):
        require_field(decision, key, "tradingagents_decision.json")
    if decision["status"] != "ok":
        raise RuntimeError(
            f"tradingagents_decision.json 的 status={decision['status']!r}（非 ok）："
            "原始决策不可用，不许用默认值蒙混"
        )
    return decision


# ------------------------------------------------------------------ 账户小节
def build_account_section(snapshot: pd.DataFrame) -> dict:
    """账户现状：持仓行（来自快照）+ 现金 / 总资产 / 权重。"""
    summary = A.account_summary(snapshot)
    total_assets = float(summary["total_assets"])
    positions = []
    for _, row in snapshot.iterrows():
        market_value = float(row["market_value"])
        positions.append({
            "code": str(row["code"]),
            "name": str(row["name"]),
            "shares": int(row["shares"]),
            "price": float(row["price"]),
            "market_value": market_value,
            "pnl_pct": float(row["pnl_pct"]),
            "cost": float(row["cost"]),
            "pnl": float(row["pnl"]),
            "weight_pct": market_value / total_assets * 100.0,
        })
    return {
        "snapshot_date": A.ACCOUNT_SNAPSHOT_DATE,
        "total_assets": total_assets,
        "cash": float(summary["cash"]),
        "market_value": float(summary["market_value"]),
        "cash_weight_pct": float(summary["cash_weight"]) * 100.0,
        "market_value_weight_pct": float(summary["market_value_weight"]) * 100.0,
        "total_cost": float(summary["total_cost"]),
        "total_pnl": float(summary["total_pnl"]),
        "total_pnl_pct": float(summary["total_pnl_pct"]) * 100.0,
        "n_positions": int(summary["n_positions"]),
        "target_positions": int(A.TARGET_POSITIONS),
        "target_position_weight_pct": float(A.TARGET_POSITION_WEIGHT) * 100.0,
        # 单席金额上限：总资产 × 单席权重（= 产物 actions 里的 target_amount）
        "slot_amount_limit": total_assets * float(A.TARGET_POSITION_WEIGHT),
        "slot_amount_limit_rounded": float(A.TARGET_POSITION_AMOUNT),
        "positions": positions,
    }


# ------------------------------------------------------------------ 规则层小节
def build_rule_actions(plan: dict) -> list[dict]:
    """规则层落地清单（逐条来自 `actions`）。"""
    total_assets = require_number(plan["meta"], "total_assets", "final_rebalance_plan.json 的 meta")
    rows = []
    for act in plan["actions"]:
        code = str(require_field(act, "code", "final_rebalance_plan.json 的 action"))
        price = require_number(act, "price", f"action {code} 的 price")
        target_shares = int(require_number(act, "target_shares", f"action {code} 的 target_shares"))
        current_shares = int(require_number(act, "current_shares", f"action {code} 的 current_shares"))
        rows.append({
            "code": code,
            "name": str(act.get("name", "")),
            "action": str(require_field(act, "action", f"action {code} 的 action")),
            "qlib_action": act.get("qlib_action"),
            "current_shares": current_shares,
            "target_shares": target_shares,
            "delta_shares": int(require_number(act, "delta_shares", f"action {code} 的 delta_shares")),
            "price": price,
            "price_date": act.get("price_date"),
            "target_weight_pct": float(require_number(act, "target_weight", f"action {code} 的 target_weight")) * 100.0,
            "target_weight_effective_pct": float(require_number(act, "target_weight_effective",
                                                                 f"action {code} 的 target_weight_effective")) * 100.0,
            "weight_to_cash_pct": float(require_number(act, "weight_to_cash", f"action {code} 的 weight_to_cash")) * 100.0,
            "target_amount": float(require_number(act, "target_amount", f"action {code} 的 target_amount")),
            "landing_amount": rnd(target_shares * price, 4),
            "landing_weight_pct": rnd(target_shares * price / total_assets * 100.0, 4),
            "one_lot_cost": rnd(LOT * price, 4),
            "est_amount": float(require_number(act, "est_amount", f"action {code} 的 est_amount")),
            "est_fee": float(require_number(act, "est_fee", f"action {code} 的 est_fee")),
            "reason": str(act.get("reason", "")),
            "lot_note": str(act.get("lot_note", "") or ""),
            "execution_note": str(act.get("execution_note", "") or ""),
        })
    return rows


# ------------------------------------------------------------------ LLM 口径归集
def collect_llm_views(code: str, conflict: dict, llm_targets: dict) -> list[dict]:
    """归集该标的的全部 LLM 视图（冲突表的 divergent views + 多头表主视图，去重）。"""
    views: list[dict] = []
    for view in conflict.get("all_divergent_views") or []:
        views.append({
            "who": view.get("who"),
            "target_pct": view.get("target_pct"),
            "direction": view.get("direction"),
            "current_pct": view.get("current_pct"),
            "reason": None,
        })
    main = llm_targets.get(code)
    if isinstance(main, dict) and main.get("target_pct") is not None:
        duplicate = any(
            view.get("who") == main.get("who")
            and view.get("target_pct") is not None
            and abs(float(view["target_pct"]) - float(main["target_pct"])) < 1e-9
            for view in views
        )
        if not duplicate:
            views.append({
                "who": main.get("who"),
                "target_pct": main.get("target_pct"),
                "direction": main.get("direction"),
                "current_pct": main.get("current_pct"),
                "reason": main.get("reason"),
            })
    return views


def shares_from_weight(target_pct: float, price: float, total_assets: float) -> dict:
    """把「目标权重」按整手折算成股数 / 金额（floor(目标金额/价格/LOT)*LOT）。"""
    target_amount = total_assets * float(target_pct) / 100.0
    shares = int(math.floor(target_amount / price / LOT) * LOT)
    return {
        "target_amount": rnd(target_amount, 4),
        "shares": shares,
        "amount": rnd(shares * price, 4),
        "weight_pct": rnd(shares * price / total_assets * 100.0, 4),
    }


# ------------------------------------------------------------------ 台账（现金）
def build_ledger(holdings: dict[str, int], prices: dict[str, float],
                 current_shares: dict[str, int], cash_begin: float,
                 fee_rate: float, total_assets: float) -> dict:
    """按「期末持仓股数」推演一笔调仓的现金台账（买入/卖出/费用/期末现金/权益）。"""
    sell_proceeds = 0.0
    sell_fees = 0.0
    buy_amount = 0.0
    buy_fees = 0.0
    trades = []
    for code, shares in sorted(holdings.items()):
        price = prices[code]
        delta = int(shares) - int(current_shares.get(code, 0))
        if delta == 0:
            continue
        amount = abs(delta) * price
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
            "side": side,
            "delta_shares": delta,
            "price": rnd(price, 6),
            "amount": rnd(amount, 4),
            "fee": rnd(fee, 6),
        })
    cash_end = cash_begin + sell_proceeds - sell_fees - buy_amount - buy_fees
    equity_amount = sum(int(shares) * prices[code] for code, shares in holdings.items())
    return {
        # 金额一律保留 10 位小数 ⇒ 展示值与产物 `cash_flow` 逐分一致（不再被四舍五入放大）
        "cash_begin": rnd(cash_begin, 10),
        "sell_proceeds": rnd(sell_proceeds, 10),
        "sell_fees": rnd(sell_fees, 10),
        "buy_amount": rnd(buy_amount, 10),
        "buy_fees": rnd(buy_fees, 10),
        "cash_end": rnd(cash_end, 10),
        "equity_amount": rnd(equity_amount, 10),
        "equity_weight_pct": rnd(equity_amount / total_assets * 100.0, 4),
        "cash_weight_pct": rnd(cash_end / total_assets * 100.0, 4),
        "trades": trades,
    }


# ------------------------------------------------------------------ 分歧识别
#: 真实证券代码 = 6 位数字（「现金」「合计」这类伪代码**不算**）
_REAL_CODE_RE = re.compile(r"^\d{6}$")


def identify_divergences(plan: dict) -> tuple[list[dict], list[dict]]:
    """归集 `llm_conflicts` 里的每个分歧标的（不硬编码代码）。

    🔴 2026-11-05 09:5x（M-Q 修）**真缺陷**：冲突表里可能出现**「现金」这类伪代码**
       （多头/空头表格把「现金」单独列一行，给出它在多空两方的目标占比）。
       旧实现把它当成普通标的 ⇒ 在 `actions` 里查不到 ⇒ 直接
       `RuntimeError: 分歧标的 现金 在 actions 里没有对应的规则层动作`
       ⇒ **整张分歧对照表生成不出来**（2026-11-05 09:4x 实测崩在这里）。
    ⇒ 修法：**只把 6 位真实代码当分歧标的**；伪代码**单独收集**并**显式披露**
       （不静默丢 ⇒ 用户能看见「LLM 也想动现金」这件事）。
    """
    total_assets = require_number(plan["meta"], "total_assets", "final_rebalance_plan.json 的 meta")
    actions = {str(a["code"]): a for a in build_rule_actions(plan)}
    llm_targets = plan.get("llm_targets") or {}
    final_targets = (plan.get("llm_final_decision") or {}).get("targets") or {}

    divergences = []
    pseudo_rows: list[dict] = []
    for conflict in plan["llm_conflicts"]:
        code = str(require_field(conflict, "code", "llm_conflicts 的一行"))
        if not _REAL_CODE_RE.match(code):
            # 🔴 伪代码（「现金」「合计」…）⇒ **不是可交易标的** ⇒ 不进 A/B 选项表，
            #    但必须**留痕**：LLM 对它的口径与规则层（现金 = 残额）不同。
            pseudo_rows.append({
                "code": code,
                "llm_target_pct": conflict.get("llm_target_pct"),
                "llm_direction": conflict.get("llm_direction"),
                "llm_who": conflict.get("llm_who"),
                "llm_reason": conflict.get("llm_reason"),
                "why_not_an_option": "非 6 位证券代码（伪代码/汇总行）⇒ 不是可交易标的，"
                                     "无法给 A/B 股数选项；其影响由现金台账体现",
            })
            continue
        action = actions.get(code)
        if action is None:
            raise RuntimeError(f"分歧标的 {code} 在 `actions` 里没有对应的规则层动作")
        final = final_targets.get(code)
        final_pct = None
        if isinstance(final, dict):
            final_pct = final.get("target_pct")
        if not isinstance(final, dict) or final_pct is None:
            # 🔴 2026-11-05 10:0x（M-Q 修）：**最终裁决没提这个标的**（或提了但抽不出数字）
            #    ⇒ 旧实现直接 `RuntimeError` ⇒ **整张对照表生成不出来**。
            #    ⇒ 改成：**不编造、不崩**，单独收集并**显式披露**
            #       （用户仍能在冲突表里看到多空两档的口径差）。
            pseudo_rows.append({
                "code": code,
                "llm_target_pct": conflict.get("llm_target_pct"),
                "llm_direction": conflict.get("llm_direction"),
                "llm_who": conflict.get("llm_who"),
                "llm_reason": conflict.get("llm_reason"),
                "why_not_an_option": "Trader 最终裁决**未提及该标的**（或提了但抽不出目标权重）"
                                     "⇒ 无法构造 A/B 对照；其多空口径差见冲突表",
            })
            continue
        conflict_pct = require_number(conflict, "llm_target_pct", f"llm_conflicts[{code}].llm_target_pct")
        rule_planned_pct = action["target_weight_pct"]
        rule_effective_pct = action["target_weight_effective_pct"]
        views = collect_llm_views(code, conflict, llm_targets)
        view_pcts = [float(v["target_pct"]) for v in views if v.get("target_pct") is not None]
        view_spread_pp = (max(view_pcts) - min(view_pcts)) if view_pcts else None
        price = action["price"]
        divergences.append({
            "code": code,
            "name": action["name"],
            "rule": {
                "action": action["action"],
                "qlib_action": action["qlib_action"],
                "target_weight_pct": rnd(rule_planned_pct, 4),
                "target_weight_effective_pct": rnd(rule_effective_pct, 4),
                "weight_to_cash_pct": rnd(action["weight_to_cash_pct"], 4),
                "target_amount": rnd(action["target_amount"], 4),
                "target_shares": action["target_shares"],
                "current_shares": action["current_shares"],
                "delta_shares": action["delta_shares"],
                "landing_amount": action["landing_amount"],
                "landing_weight_pct": action["landing_weight_pct"],
                "price": price,
                "price_date": action["price_date"],
                "one_lot_cost": action["one_lot_cost"],
                "reason": action["reason"],
                "lot_note": action["lot_note"],
                "execution_note": action["execution_note"],
            },
            "llm": {
                "conflict_table": {
                    "caliber": "llm_conflicts（多头/空头表格口径）",
                    "target_pct": rnd(conflict_pct, 4),
                    "direction": conflict.get("llm_direction"),
                    "who": conflict.get("llm_who"),
                    "current_pct": conflict.get("llm_current_pct"),
                    "reason": conflict.get("llm_reason"),
                    "converted": shares_from_weight(conflict_pct, price, total_assets),
                },
                "final_decision": {
                    "caliber": "llm_final_decision（Trader 最终裁决原文）",
                    "target_pct": rnd(final_pct, 4),
                    "target_pct_source": final.get("target_pct_source"),
                    "direction": final.get("direction"),
                    "cap_pct": final.get("cap_pct"),
                    "floor_pct": final.get("floor_pct"),
                    "stop_loss": final.get("stop_loss"),
                    "lots": final.get("lots"),
                    "shares_hint": final.get("shares_hint"),
                    "shares_keep": final.get("shares_keep"),
                    "shares_sell": final.get("shares_sell"),
                    "post_trade_pct": final.get("post_trade_pct"),
                    "post_trade_amount": final.get("post_trade_amount"),
                    "evidence": final.get("evidence"),
                    "note": final.get("note"),
                    "converted": shares_from_weight(final_pct, price, total_assets),
                },
                "views": [
                    {
                        "who": v.get("who"),
                        "target_pct": v.get("target_pct"),
                        "direction": v.get("direction"),
                        "current_pct": v.get("current_pct"),
                        "reason": v.get("reason"),
                        "converted": shares_from_weight(float(v["target_pct"]), price, total_assets),
                    }
                    for v in views
                ],
            },
            "diff": {
                "rule_planned_pct": rnd(rule_planned_pct, 4),
                "rule_effective_pct": rnd(rule_effective_pct, 4),
                "llm_conflict_pct": rnd(conflict_pct, 4),
                "llm_final_pct": rnd(final_pct, 4),
                "gap_vs_effective_pp": rnd(final_pct - rule_effective_pct, 4),
                "gap_vs_planned_pp": rnd(final_pct - rule_planned_pct, 4),
                "llm_source_gap_pp": rnd(final_pct - conflict_pct, 4),
                "llm_view_spread_pp": rnd(view_spread_pp, 4),
            },
        })
    return divergences, pseudo_rows


def filter_pending(divergences: list[dict]) -> list[dict]:
    """筛出「待拍板分歧」：最终裁决与规则层落地差 > 2.0pp，或多头/空头两档互斥。"""
    pending = []
    for item in divergences:
        gap = item["diff"]["gap_vs_effective_pp"]
        spread = item["diff"]["llm_view_spread_pp"]
        if (gap is not None and abs(gap) > DIVERGENCE_THRESHOLD_PP) or (
            spread is not None and spread > DIVERGENCE_THRESHOLD_PP
        ):
            pending.append(item)
    return pending


# ------------------------------------------------------------------ 选项 A / B
def build_options(plan: dict, account: dict, divergences: list[dict]) -> dict:
    """构造「选项 A（规则层）/ 选项 B（LLM 层）」的落地数字、约束与现金影响。"""
    total_assets = account["total_assets"]
    cash_begin = account["cash"]
    fee_rate = require_number(plan["meta"], "fee_rate_one_way", "final_rebalance_plan.json 的 meta")
    actions = {str(a["code"]): a for a in build_rule_actions(plan)}
    prices = {code: act["price"] for code, act in actions.items()}
    current_shares = {code: act["current_shares"] for code, act in actions.items()}
    rule_holdings = {code: act["target_shares"] for code, act in actions.items()}
    slot_limit = total_assets * float(A.TARGET_POSITION_WEIGHT)
    slot_weight_pct = float(A.TARGET_POSITION_WEIGHT) * 100.0

    # --- 方案 A：全部采纳规则层实际落地 -------------------------------------
    ledger_a = build_ledger(rule_holdings, prices, current_shares, cash_begin, fee_rate, total_assets)
    plan_cash_end = require_number(plan["cash_flow"], "cash_end",
                                   "final_rebalance_plan.json 的 cash_flow.cash_end")
    if abs(ledger_a["cash_end"] - plan_cash_end) >= 0.01:
        raise RuntimeError(
            f"方案 A 现金台账与产物对不上：本表算得 {ledger_a['cash_end']:.2f} 元，"
            f"产物 cash_flow.cash_end={plan_cash_end:.2f} 元（差 ≥ 0.01 元）"
        )

    # --- 方案 B：待拍板分歧标的采纳 LLM 最终裁决，其余照规则层 ----------------
    llm_holdings = dict(rule_holdings)
    for item in divergences:
        code = item["code"]
        final = item["llm"]["final_decision"]
        price = actions[code]["price"]
        lots = final.get("lots")
        if lots is not None:
            shares = int(lots) * int(LOT)
            basis = f"原文「买 {lots} 手」× LOT={LOT}"
        else:
            shares = int(final["converted"]["shares"])
            basis = (f"floor({fmt_amount(final['converted']['target_amount'])} ÷ {price} ÷ {LOT}) × {LOT}")
        final["executable_shares"] = shares
        final["executable_amount"] = rnd(shares * price, 4)
        final["executable_weight_pct"] = rnd(shares * price / total_assets * 100.0, 4)
        final["executable_basis"] = basis
        llm_holdings[code] = shares
    ledger_b = build_ledger(llm_holdings, prices, current_shares, cash_begin, fee_rate, total_assets)

    # --- 逐标的选项：A = 规则层；B = 该标的换 LLM（其余保持规则层） -----------
    options = []
    for item in divergences:
        code = item["code"]
        action = actions[code]
        price = action["price"]
        final = item["llm"]["final_decision"]
        conflict = item["llm"]["conflict_table"]
        rule_shares = action["target_shares"]
        rule_amount = float(action["landing_amount"])
        llm_shares = int(final["executable_shares"])
        llm_amount = float(final["executable_amount"])
        variant_holdings = dict(rule_holdings)
        variant_holdings[code] = llm_shares
        ledger_variant = build_ledger(variant_holdings, prices, current_shares,
                                      cash_begin, fee_rate, total_assets)
        constraints = {
            "slot_amount_limit": rnd(slot_limit, 4),
            "slot_weight_limit_pct": slot_weight_pct,
            "one_lot_cost": action["one_lot_cost"],
            "one_lot_weight_pct": rnd(action["one_lot_cost"] / total_assets * 100.0, 4),
            "rule_one_lot_buildable": bool(action["one_lot_cost"] <= slot_limit),
            "llm_weight_converted_shares": int(final["converted"]["shares"]),
            "llm_weight_converted_amount": final["converted"]["amount"],
            "llm_weight_converted_weight_pct": final["converted"]["weight_pct"],
            "llm_weight_lot_buildable": bool(
                final["converted"]["amount"] >= action["one_lot_cost"] or final["target_pct"] <= 0.0
            ),
            "llm_executable_breaks_slot_cap": bool(
                float(final["executable_weight_pct"]) > slot_weight_pct
            ),
            "llm_conflict_breaks_slot_cap": bool(
                float(conflict["converted"]["weight_pct"]) > slot_weight_pct
            ),
            "llm_cash_feasible": bool(ledger_variant["cash_end"] >= 0.0),
            "stop_loss": final["stop_loss"],
            "cap_pct": final["cap_pct"],
            "floor_pct": final["floor_pct"],
        }
        item["constraints"] = constraints
        options.append({
            "code": code,
            "name": action["name"],
            "option_a": {
                "label": "选项 A（采纳规则层）",
                "source": "final_rebalance_plan.json → actions",
                "direction": action["action"],
                "target_weight_pct": item["diff"]["rule_planned_pct"],
                "effective_weight_pct": item["diff"]["rule_effective_pct"],
                "target_shares": rule_shares,
                "target_amount": action["target_amount"],
                "landing_amount": rule_amount,
                "landing_weight_pct": action["landing_weight_pct"],
                "delta_shares": action["delta_shares"],
                "price": price,
                "cash_end": ledger_a["cash_end"],
                "cash_weight_pct": ledger_a["cash_weight_pct"],
                "equity_weight_pct": ledger_a["equity_weight_pct"],
                "reason": action["reason"],
            },
            "option_b": {
                "label": "选项 B（采纳 LLM 层）",
                "source": "final_rebalance_plan.json → llm_final_decision.targets",
                "direction": final["direction"],
                "target_weight_pct": item["diff"]["llm_final_pct"],
                "effective_weight_pct": final["executable_weight_pct"],
                "target_shares": llm_shares,
                "target_amount": final["converted"]["target_amount"],
                "landing_amount": llm_amount,
                "landing_weight_pct": final["executable_weight_pct"],
                "delta_shares": llm_shares - action["current_shares"],
                "price": price,
                "cash_end": ledger_variant["cash_end"],
                "cash_weight_pct": ledger_variant["cash_weight_pct"],
                "equity_weight_pct": ledger_variant["equity_weight_pct"],
                "reason": final["evidence"],
            },
            "delta": {
                "shares": llm_shares - rule_shares,
                "amount": rnd(llm_amount - rule_amount, 4),
                "weight_pp": rnd(float(final["executable_weight_pct"]) - float(action["landing_weight_pct"]), 4),
                "cash_end": rnd(ledger_variant["cash_end"] - ledger_a["cash_end"], 10),
            },
        })

    return {
        "scenarios": {
            "A_rule": {
                "label": "方案 A：全部采纳规则层实际落地",
                "description": "9 只标的的期末持仓股数全部取 `actions[].target_shares`"
                               "（整手买不起的槽位落地 0 股、权重归现金）",
                "ledger": ledger_a,
                "cash_end": ledger_a["cash_end"],
                "cash_weight_pct": ledger_a["cash_weight_pct"],
                "equity_amount": ledger_a["equity_amount"],
                "equity_weight_pct": ledger_a["equity_weight_pct"],
            },
            "B_llm": {
                "label": "方案 B：待拍板分歧标的采纳 LLM 最终裁决，其余照规则层",
                "description": "仅替换待拍板分歧标的的期末持仓股数，其余标的与方案 A 完全一致",
                "ledger": ledger_b,
                "cash_end": ledger_b["cash_end"],
                "cash_weight_pct": ledger_b["cash_weight_pct"],
                "equity_amount": ledger_b["equity_amount"],
                "equity_weight_pct": ledger_b["equity_weight_pct"],
            },
        },
        "per_code_options": options,
    }


# ------------------------------------------------------------------ 主载荷
def build_payload(plan: dict, decision: dict, snapshot: pd.DataFrame, asof_label: str,
                  run_root: Path) -> dict:
    """组装 JSON 载荷（Markdown 是它的渲染）。"""
    meta = plan["meta"]
    account = build_account_section(snapshot)
    rule_actions = build_rule_actions(plan)
    all_divergences, pseudo_rows = identify_divergences(plan)
    pending = filter_pending(all_divergences)
    options = build_options(plan, account, pending)

    rating = plan["llm_final_decision"].get("rating")
    notes = [
        "🔴 本表**不替用户做决定**：只并列「选项 A / 选项 B」及其数字后果，"
        "不输出任何「建议采用 X」式结论。",
        f"LLM 最终裁决评级：{rating}；TradingAgents 原始决策 signal：{decision.get('signal')}。",
        "🔴 口径提示：LLM 最终裁决原文按 2026-11-30 收盘价折算股数，本机无该日价格 ⇒ "
        "本表 LLM 侧股数一律按规则层价格基准（2026-11-03 收盘价）重算，"
        "原文股数并列展示，两者不混称。",
        f"价格基准 asof = {meta.get('asof')}；权重与占比一律以账户总资产 "
        f"{account['total_assets']:.2f} 元为分母；"
        f"费用按 meta.fee_rate_one_way = {meta.get('fee_rate_one_way')} 计算。",
        "方案 A 的现金台账已与产物 cash_flow.cash_end 逐分核对一致（差 < 0.01 元）。",
    ]
    if pseudo_rows:
        notes.append(
            "🔴 冲突表里还有 **"
            + "、".join(f"`{r['code']}`（多头/空头口径 {r.get('llm_target_pct')}%）"
                        for r in pseudo_rows)
            + "** **未进 A/B 对照表**：或因为是**伪代码行**（非 6 位证券代码 ⇒ 不是可交易标的，"
              "对账户的影响体现在现金台账上），或因为 **Trader 最终裁决未提及/抽不出目标权重**"
              "（⇒ 无法构造对照，不编造）。详见 `meta.pseudo_code_rows`。"
        )
    if rating != decision.get("signal"):
        notes.append(
            f"⚠️ 评级不一致：产物 llm_final_decision.rating={rating!r}，"
            f"原始决策 signal={decision.get('signal')!r}。"
        )

    return {
        "meta": {
            "asof_label": asof_label,
            "asof": meta.get("asof"),
            "plan_generated_at": meta.get("generated_at"),
            "run_root": str(run_root),
            "source_plan": str(run_root / "final_rebalance_plan.json"),
            "source_decision_json": str(run_root / "tradingagents_decision.json"),
            "source_decision_md": plan["llm_final_decision"].get("source_file"),
            "source_account_csv": str(A.ACCOUNT_SNAPSHOT_CSV),
            "lot_size": int(LOT),
            "total_assets": account["total_assets"],
            "cash_begin": account["cash"],
            "fee_rate_one_way": require_number(meta, "fee_rate_one_way",
                                               "final_rebalance_plan.json 的 meta"),
            "divergence_threshold_pp": DIVERGENCE_THRESHOLD_PP,
            "n_conflicts": len(all_divergences),
            "n_pending": len(pending),
            "pending_codes": [d["code"] for d in pending],
            "n_pseudo_code_rows": len(pseudo_rows),
            "pseudo_code_rows": pseudo_rows,
            "n_codes_without_final_target": len(pseudo_rows),
            "llm_rating": rating,
            "llm_signal": decision.get("signal"),
            "generator": "manu/etf_rotation/make_decision_options.py",
        },
        "account": account,
        "next_rebalance": {
            "target_date": plan["next_rebalance"].get("target_date"),
            "signal_date": plan["next_rebalance"].get("signal_date"),
            "execution_date": plan["next_rebalance"].get("execution_date"),
            "execution_lag_days": plan["next_rebalance"].get("execution_lag_days"),
            "rule": plan["next_rebalance"].get("rule"),
            "cycle_rule": plan["next_rebalance"].get("cycle_rule"),
            "trading_days_until": plan["next_rebalance"].get("trading_days_until"),
            "in_calendar_verified": plan["next_rebalance"].get("in_calendar_verified"),
            "execution_note": plan["next_rebalance"].get("execution_note"),
        },
        "rule_actions": rule_actions,
        "divergences": pending,
        "all_conflicts": all_divergences,
        "options": options,
        "notes": notes,
        "disclaimer": (
            "本表由已交付产物（final_rebalance_plan.json / tradingagents_decision.json / "
            "真实账户快照）机械折算生成，**只摆数字、不做决策建议**，**不构成投资建议**；"
            "历史统计不代表未来表现，使用者须自行判断并自行承担全部风险。"
        ),
    }


# ------------------------------------------------------------------ Markdown 渲染
def render_markdown(payload: dict) -> str:
    """把 JSON 载荷渲染成 Markdown（同一份数据的两种呈现）。"""
    meta = payload["meta"]
    account = payload["account"]
    nxt = payload["next_rebalance"]
    lines: list[str] = []
    add = lines.append

    add("# ETF 轮动调仓 · 规则层 vs LLM 层 · 分歧对照选项表")
    add("")
    add(f"- 数据口径：asof `{meta['asof']}`（计划件 generated_at `{meta['plan_generated_at']}`）"
        f"｜文件名标签 `{meta['asof_label']}`")
    add(f"- 整手：1 手 = **{meta['lot_size']} 股**｜价格基准：**2026-11-03 收盘价**"
        f"（与规则层 `target_shares` 同口径）｜费用单边费率 {meta['fee_rate_one_way']}")
    add("- 🔴 **本表只摆数字与后果，不替用户做决定**：并列「选项 A / 选项 B」，"
        "无任何「建议采用 X」结论。")
    add("")

    # 一、账户现状
    add(f"## 一、账户现状（真实账户快照 {account['snapshot_date']}，"
        "来源 `account.load_account_snapshot()`）")
    add("")
    add("| 代码 | 名称 | 股数 | 现价 | 市值(元) | 盈亏 | 权重 |")
    add("|---|---|---:|---:|---:|---:|---:|")
    for pos in account["positions"]:
        add(f"| {pos['code']} | {md_cell(pos['name'])} | {fmt_shares(pos['shares'])} | "
            f"{pos['price']:.3f} | {fmt_amount(pos['market_value'])} | "
            f"{pos['pnl_pct'] * 100:+.2f}% | {pos['weight_pct']:.2f}% |")
    add("")
    add(f"- 现金 **{fmt_amount(account['cash'])} 元**（{account['cash_weight_pct']:.2f}%）"
        f"｜持仓市值 **{fmt_amount(account['market_value'])} 元**"
        f"（{account['market_value_weight_pct']:.2f}%）"
        f"｜总资产 **{fmt_amount(account['total_assets'])} 元**")
    add(f"- 持仓 {account['n_positions']} 只｜总成本 {fmt_amount(account['total_cost'])} 元"
        f"｜总盈亏 {account['total_pnl']:+,.2f} 元（{account['total_pnl_pct']:+.2f}%）")
    add(f"- 目标结构：{account['target_positions']} 只等权，每只 "
        f"{account['target_position_weight_pct']:.0f}% ≈ "
        f"**{fmt_amount(account['slot_amount_limit'])} 元**（单席金额上限；"
        f"account.TARGET_POSITION_AMOUNT = {fmt_amount(account['slot_amount_limit_rounded'])} 元）")
    add("")

    # 二、下一调仓节点
    add("## 二、下一调仓节点与执行日（来源 `final_rebalance_plan.next_rebalance`）")
    add("")
    add(f"- 节点日（信号日）：**{nxt['target_date']}**｜执行日（下单日）：**{nxt['execution_date']}**"
        f"（lag = {nxt['execution_lag_days']} 个交易日）")
    add(f"- 节点规则：{md_cell(nxt['rule'])}｜周期规则：{md_cell(nxt['cycle_rule'])}")
    add(f"- 距节点 {nxt['trading_days_until']} 个交易日｜日历校验 "
        f"{'通过' if nxt['in_calendar_verified'] else '未通过'}")
    add(f"- 🔴 {md_cell(nxt['execution_note'])}")
    add("")

    # 三、分歧总览
    add(f"## 三、待拍板分歧总览（自动识别 {meta['n_pending']} 项，"
        f"阈值 {meta['divergence_threshold_pp']}pp）")
    add("")
    add("| # | 代码 | 名称 | 规则层计划% | 规则层落地% | LLM 冲突表% | LLM 最终裁决% | "
        "最终 vs 落地(pp) | 分歧性质 |")
    add("|---:|---|---|---:|---:|---:|---:|---:|---|")
    for idx, item in enumerate(payload["divergences"], start=1):
        diff = item["diff"]
        nature = []
        if diff["llm_source_gap_pp"] is not None and abs(diff["llm_source_gap_pp"]) > 0.01:
            nature.append(f"两个 LLM 口径互斥（{diff['llm_conflict_pct']:.1f}% vs "
                          f"{diff['llm_final_pct']:.1f}%）")
        if diff["llm_view_spread_pp"] is not None and diff["llm_view_spread_pp"] > 0.01:
            nature.append(f"多头/空头两档互斥（极差 {diff['llm_view_spread_pp']:.1f}pp）")
        if abs(diff["gap_vs_effective_pp"]) >= 19.99:
            nature.append("完全相反")
        add(f"| {idx} | {item['code']} | {md_cell(item['name'])} | "
            f"{diff['rule_planned_pct']:.2f}% | {diff['rule_effective_pct']:.2f}% | "
            f"{diff['llm_conflict_pct']:.2f}% | {diff['llm_final_pct']:.2f}% | "
            f"{diff['gap_vs_effective_pp']:+.1f} | {md_cell('；'.join(nature))} |")
    add("")

    # 四、逐标的对照
    add("## 四、逐标的对照（规则层 vs LLM 层）")
    add("")
    opts = {o["code"]: o for o in payload["options"]["per_code_options"]}
    for idx, item in enumerate(payload["divergences"], start=1):
        opt = opts[item["code"]]
        rule = item["rule"]
        llm = item["llm"]
        cons = item["constraints"]
        diff = item["diff"]
        add(f"### 4.{idx} {item['code']} {item['name']}")
        add("")
        add(f"**规则层**（动作：{rule['action']}）")
        add("")
        add(f"- 目标权重 {rule['target_weight_pct']:.2f}%（计划）→ "
            f"**落地 {rule['target_weight_effective_pct']:.2f}%**"
            f"（整手买不起则权重归现金 {rule['weight_to_cash_pct']:.2f}%）")
        add(f"- 目标股数 **{fmt_shares(rule['target_shares'])} 股**"
            f"（当前 {fmt_shares(rule['current_shares'])} 股，Δ {rule['delta_shares']:+d}）"
            f"｜目标金额 {fmt_amount(rule['target_amount'])} 元"
            f"｜落地金额 **{fmt_amount(rule['landing_amount'])} 元**"
            f"（落地权重 {rule['landing_weight_pct']:.2f}%）")
        add(f"- 价格 {rule['price']}（{rule['price_date']}）｜"
            f"一手 = {meta['lot_size']} × {rule['price']} = **{fmt_amount(rule['one_lot_cost'])} 元**")
        add(f"- 理由：{md_cell(rule['reason'])}")
        if rule["lot_note"] and rule["lot_note"] not in rule["reason"]:
            add(f"- 整手备注：{md_cell(rule['lot_note'])}")
        add("")
        add("**LLM 层**")
        add("")
        add("| 口径 | 目标权重 | 方向 | 谁 | 换算股数 | 换算金额(元) | 原文/说明 |")
        add("|---|---:|---|---|---:|---:|---|")
        ct = llm["conflict_table"]
        add(f"| {md_cell(ct['caliber'])} | {ct['target_pct']:.2f}% | {md_cell(ct['direction'])} | "
            f"{md_cell(ct['who'])} | {fmt_shares(ct['converted']['shares'])} | "
            f"{fmt_amount(ct['converted']['amount'])} | {md_cell(ct['reason'])} |")
        fd = llm["final_decision"]
        add(f"| {md_cell(fd['caliber'])} | {fd['target_pct']:.2f}% | {md_cell(fd['direction'])} | "
            f"Trader | {fmt_shares(fd.get('executable_shares'))} | "
            f"{fmt_amount(fd.get('executable_amount'))} | {md_cell(fd['evidence'])} |")
        for view in llm["views"]:
            add(f"| {md_cell(str(view['who']) + ' 视图')} | "
                f"{float(view['target_pct']):.2f}% | {md_cell(view['direction'])} | "
                f"{md_cell(view['who'])} | {fmt_shares(view['converted']['shares'])} | "
                f"{fmt_amount(view['converted']['amount'])} | {md_cell(view['reason'])} |")
        add("")
        add(f"- 目标权重来源：{md_cell(fd['target_pct_source'])}"
            f"｜换算股数 = floor(目标金额 ÷ 价格 ÷ {meta['lot_size']}) × {meta['lot_size']}"
            f"（2026-11-03 收盘价基准）")
        if fd["shares_keep"] is not None:
            add(f"- 原文股数：保留约 {fmt_shares(fd['shares_keep'])} 股 / "
                f"卖出约 {fmt_shares(fd['shares_sell'])} 股（**原文价格基准为 2026-11-30 收盘价**，"
                f"与本表折算口径不同）")
        if fd["note"]:
            add(f"- 原文解析备注：{md_cell(fd['note'])}")
        add("")
        add("**差异**")
        add("")
        add(f"- 权重差：规则层落地 {diff['rule_effective_pct']:.2f}% vs LLM 最终裁决 "
            f"{diff['llm_final_pct']:.2f}% ⇒ **{diff['gap_vs_effective_pp']:+.1f}pp**"
            f"（相对规则层**计划** {diff['rule_planned_pct']:.2f}% 则为 "
            f"{diff['gap_vs_planned_pp']:+.1f}pp）")
        add(f"- LLM 内部口径差：冲突表 {diff['llm_conflict_pct']:.2f}% vs 最终裁决原文 "
            f"{diff['llm_final_pct']:.2f}% ⇒ **{diff['llm_source_gap_pp']:+.1f}pp**"
            f"；多头/空头两档极差 {diff['llm_view_spread_pp']:.1f}pp")
        add(f"- 股数差：**{opt['delta']['shares']:+d} 股**｜金额差："
            f"**{opt['delta']['amount']:+,.2f} 元**｜落地权重差 {opt['delta']['weight_pp']:+.2f}pp")
        add("")
        add("**可执行性约束**")
        add("")
        add(f"- 单席金额上限 **{fmt_amount(cons['slot_amount_limit'])} 元**"
            f"（单席权重上限 {cons['slot_weight_limit_pct']:.0f}%）"
            f"｜一手金额 **{fmt_amount(cons['one_lot_cost'])} 元**"
            f"（占总资产 {cons['one_lot_weight_pct']:.2f}%）")
        add(f"- 规则层整手可建仓：{'是' if cons['rule_one_lot_buildable'] else '**否**'}"
            f"｜按 LLM 目标权重折算的股数 {fmt_shares(cons['llm_weight_converted_shares'])} 股"
            f"（{fmt_amount(cons['llm_weight_converted_amount'])} 元）"
            f"{'，**不足一手**' if not cons['llm_weight_lot_buildable'] else '，够一手'}")
        add(f"- LLM 落地口径破单席上限："
            f"{'**是**' if cons['llm_executable_breaks_slot_cap'] else '否'}"
            f"｜冲突表口径破单席上限："
            f"{'**是**' if cons['llm_conflict_breaks_slot_cap'] else '否'}"
            f"｜现金可承受：{'是' if cons['llm_cash_feasible'] else '**否**'}")
        if cons["cap_pct"] is not None:
            add(f"- LLM 原文约束：硬上限 {cons['cap_pct']}%｜下限 {cons['floor_pct']}%｜"
                f"价格硬止损 **{cons['stop_loss']}**")
        add("")
        add("**选项与现金影响**")
        add("")
        add("| 选项 | 动作 | 最终股数 | 最终金额(元) | 落地权重 | 期末现金(元) | 现金占比 | 落地权益占比 |")
        add("|---|---|---:|---:|---:|---:|---:|---:|")
        for key in ("option_a", "option_b"):
            op = opt[key]
            add(f"| {md_cell(op['label'])} | {md_cell(op['direction'])} | "
                f"{fmt_shares(op['target_shares'])} | {fmt_amount(op['landing_amount'])} | "
                f"{op['landing_weight_pct']:.2f}% | {fmt_amount(op['cash_end'])} | "
                f"{op['cash_weight_pct']:.2f}% | {op['equity_weight_pct']:.2f}% |")
        add("")
        add(f"- 选项 A vs 选项 B：现金差 **{opt['delta']['cash_end']:+,.2f} 元**"
            f"（选项 B 相对选项 A）")
        add(f"- 选项 A 理由：{md_cell(opt['option_a']['reason'])}"
            f"｜选项 B 理由：{md_cell(opt['option_b']['reason'])}")
        add("")

    # 五、规则层落地清单
    add("## 五、规则层落地清单（来源 `final_rebalance_plan.json → actions`）")
    add("")
    add("| 代码 | 名称 | 动作 | 当前股数 | 目标股数 | Δ股数 | 价格 | 目标金额 | 落地金额 | "
        "落地权重 | 费用 | 理由 |")
    add("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for act in payload["rule_actions"]:
        add(f"| {act['code']} | {md_cell(act['name'])} | {md_cell(act['action'])} | "
            f"{fmt_shares(act['current_shares'])} | {fmt_shares(act['target_shares'])} | "
            f"{act['delta_shares']:+d} | {act['price']} | {fmt_amount(act['target_amount'])} | "
            f"{fmt_amount(act['landing_amount'])} | {act['landing_weight_pct']:.2f}% | "
            f"{fmt_amount(act['est_fee'])} | {md_cell(act['reason'])} |")
    add("")

    # 六、两套方案汇总
    scen = payload["options"]["scenarios"]
    add("## 六、两套方案现金影响汇总")
    add("")
    add("| 方案 | 说明 | 卖出所得 | 卖出费用 | 买入金额 | 买入费用 | 期末现金 | 现金占比 | "
        "权益金额 | 权益占比 |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for key in ("A_rule", "B_llm"):
        sc = scen[key]
        led = sc["ledger"]
        add(f"| {md_cell(sc['label'])} | {md_cell(sc['description'])} | "
            f"{fmt_amount(led['sell_proceeds'])} | {fmt_amount(led['sell_fees'])} | "
            f"{fmt_amount(led['buy_amount'])} | {fmt_amount(led['buy_fees'])} | "
            f"{fmt_amount(led['cash_end'])} | {led['cash_weight_pct']:.2f}% | "
            f"{fmt_amount(led['equity_amount'])} | {led['equity_weight_pct']:.2f}% |")
    add("")
    add(f"- 方案 A 期末现金 {fmt_amount(scen['A_rule']['cash_end'])} 元"
        f"（{scen['A_rule']['cash_weight_pct']:.2f}%）与产物 `cash_flow.cash_end` 逐分一致；"
        f"权益 + 现金 ≠ 100% 是因为费用从现金中支出。")
    add(f"- 方案 B 期末现金 {fmt_amount(scen['B_llm']['cash_end'])} 元"
        f"（{scen['B_llm']['cash_weight_pct']:.2f}%）；与 LLM 原文「现金 ≈16,271 元（54.5%）」"
        f"的差额来自**价格基准不同**（原文用 2026-11-30 收盘价，本表用 2026-11-03 收盘价）。")
    add("")

    # 七、口径与免责
    add("## 七、口径说明与免责声明")
    add("")
    for note in payload["notes"]:
        add(f"- {md_cell(note)}")
    add(f"- {md_cell(payload['disclaimer'])}")
    add("")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="生成「规则层 vs LLM 层」ETF 调仓分歧对照选项表（JSON + Markdown）")
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
    decision = load_decision(run_root)
    snapshot = A.load_account_snapshot()
    payload = build_payload(plan, decision, snapshot, args.asof_label, run_root)
    markdown = render_markdown(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"decision_options_{args.asof_label}.json"
    md_path = out_dir / f"decision_options_{args.asof_label}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    print(f"[decision-options] 待拍板分歧 {payload['meta']['n_pending']} 项："
          f"{', '.join(payload['meta']['pending_codes'])}")
    for item in payload["divergences"]:
        diff = item["diff"]
        print(f"  {item['code']} {item['name']}: 规则层落地 "
              f"{diff['rule_effective_pct']:.2f}% vs LLM 最终 {diff['llm_final_pct']:.2f}% "
              f"（{diff['gap_vs_effective_pp']:+.1f}pp）")
    print(f"[decision-options] 方案 A 期末现金 "
          f"{payload['options']['scenarios']['A_rule']['cash_end']:.2f} 元"
          f"（{payload['options']['scenarios']['A_rule']['cash_weight_pct']:.2f}%）")
    print(f"[decision-options] 方案 B 期末现金 "
          f"{payload['options']['scenarios']['B_llm']['cash_end']:.2f} 元"
          f"（{payload['options']['scenarios']['B_llm']['cash_weight_pct']:.2f}%）")
    print(f"[decision-options] wrote {json_path} ({json_path.stat().st_size} B)")
    print(f"[decision-options] wrote {md_path} ({md_path.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
