#!/usr/bin/env python
"""最小调仓门槛（方案一 · 2026-11-13）——**单一事实源**模块。

【用户 2026-11-13 的原话（方案一）】
「在阶段 5 的规则层里，**不要用单一金额一刀切**，而是按交易性质区分：
① **清仓止损 / 跌破 MA60 退出** ⇒ 门槛 **0 元**（风控优先于手续费，无条件执行）；
② **全新建仓** ⇒ 门槛 **3,500~4,000 元**（取 3,800）；
③ **存量仓位微调（加仓/减仓）** ⇒ 门槛 **2,500~3,000 元**（取 2,800）。」

🔴 **死锁推导（这就是为什么单一 5,000 元门槛是错的）**
3 万本金、5 只等权 ⇒ 单只标准席 ≈ 30,035 × 16% ≈ **4,805 元**
⇒ 门槛若硬设 5,000 元 ⇒ 4,805 < 5,000 ⇒ **所有「新开仓」被全部拦死**
⇒ 以后**再也买不进任何新 ETF**（数学死锁，不是风控）。
⇒ 本模块内置**死锁检测**：本轮新开仓**全部**被拦 ⇒ 产物里显式报警。

🔴 **5 元最低佣金在单边成交额上的真实磨损**（费率 = 5 / 成交额）
    1,000 元 → **50.0 bp**（极亏，必须拦）｜2,000 元 → **25.0 bp**（较亏）
    4,800 元 → **10.4 bp**（可接受）｜6,000~8,000 元 → **6.2~8.3 bp**（很划算）

【为什么必须同时落在「阶段 5」和「阶段 6」】
`final_rebalance_plan.py`（阶段 5）按**信号日价**判门槛；
`reprice_node_day.py`（阶段 6，执行卡）按**节点日价**重算股数与金额
⇒ 若只在阶段 5 设门槛，阶段 6 会把被拦下的碎单**重新算回来** ⇒ 门槛形同虚设。
⇒ 两个脚本**共用本模块**（口径、阈值、分类、披露结构完全一致）⇒ 单一事实源。

【口径纪律】
* **分类只看三个事实**（当前股数 / 目标股数 / 变动方向），**不看金额**；
* **被拦下的行不删**（动作从计划里消失 ⇒ 市值凭空蒸发 ⇒ 交付件自相矛盾）
  ⇒ 保留在 `actions` 里，但 delta/金额/费用归零、动作改「持有 / 不建仓」、
    目标权重按**当前市值**重算、差额归现金；
* **披露**：阈值、被拦明细、折合 bp、省下的最低佣金、死锁风险 ⇒ 一律如实产出。
"""

from __future__ import annotations

import config as C

#: 三档口径的**分类键**（披露、测试、日志都用这三个字面量 ⇒ 勿随意改）
MIN_AMOUNT_KINDS = ("清仓止损", "新开仓", "存量微调")


def classify_min_amount_kind(row: dict) -> str:
    """判定一条动作的**交易性质**（⇒ 适用哪一档门槛）。空串 = 不参与门槛。

    ① **清仓止损**：`delta_shares < 0` 且 `target_shares == 0`（卖到 0 股）
       ⇒ 覆盖「跌破 MA60 ⇒ 持仓强制退出」与规则层清仓
       ⇒ 门槛 **0 元**（风控永远优先于省手续费）。
    ② **新开仓**：`current_shares == 0` 且 `delta_shares > 0`（从 0 买）
       ⇒ 门槛 `MIN_TRADE_AMOUNT_NEW`。
    ③ **存量微调**：其余（`current_shares > 0` 且 `delta_shares != 0`）
       ⇒ 门槛 `MIN_TRADE_AMOUNT_ADJUST`。
    """
    delta = int(row.get("delta_shares") or 0)
    if delta == 0:
        return ""
    if delta < 0 and int(row.get("target_shares") or 0) == 0:
        return "清仓止损"
    if int(row.get("current_shares") or 0) == 0 and delta > 0:
        return "新开仓"
    return "存量微调"


def resolve_thresholds(overrides: dict | None = None) -> dict:
    """三档门槛（config 为**默认**，CLI 可覆盖）。"""
    thr = {
        "清仓止损": float(C.MIN_TRADE_AMOUNT_LIQUIDATE),
        "新开仓": float(C.MIN_TRADE_AMOUNT_NEW),
        "存量微调": float(C.MIN_TRADE_AMOUNT_ADJUST),
    }
    if overrides:
        for key, value in overrides.items():
            if key in thr and value is not None:
                thr[key] = float(value)
    return thr


def gate_enabled(args=None) -> bool:
    """门槛总开关：`--no-min-amount-gate` ⇒ 关（回退/对比用）；默认 **开**。"""
    if args is None:
        return bool(C.MIN_AMOUNT_GATE_ENABLED)
    if getattr(args, "no_min_amount_gate", False):
        return False
    return bool(C.MIN_AMOUNT_GATE_ENABLED)


def thresholds_from_args(args=None) -> dict | None:
    """CLI 覆盖 → 阈值覆盖表（没给就 None ⇒ 全用 config）。"""
    if args is None:
        return None
    _map = {
        "清仓止损": getattr(args, "min_amount_liquidate", None),
        "新开仓": getattr(args, "min_amount_new", None),
        "存量微调": getattr(args, "min_amount_adjust", None),
    }
    if all(value is None for value in _map.values()):
        return None
    return _map


def _strict_fee(amount: float, commission_rate: float, min_commission: float) -> float:
    """**5 元最低佣金下限口径**下的单边费用：max(按费率, 最低佣金)。

    ⚠️ 研究链自身的费用模型是「免 5 元」（`ETF_MIN_COMMISSION = 0.0`）
    ⇒ 本函数**只用于门槛披露**（折合费率 / 省下的最低佣金），
    **不参与**计划件里的现金流（现金流沿用研究链口径，勿混算）。
    """
    return max(float(min_commission), float(amount) * float(commission_rate or 0.0))


def block_row(
    row: dict,
    *,
    kind: str,
    amount: float,
    threshold: float,
    commission_rate: float,
    min_commission: float,
    total_assets: float | None,
    reason_suffix: str = "",
) -> dict:
    """把一行**就地作废**（**行保留**）⇒ 返回披露记录。

    🔴 **不删行**：动作从计划里消失 ⇒ 该持仓市值凭空蒸发 ⇒ 交付件与现金小结自相矛盾。
    ⇒ ① `delta_shares` 归零、`target_shares` 回退到 `current_shares`（**不调仓**）；
      ② `est_amount` / `est_fee` 归零（⇒ **不进现金流水** ⇒ 期末现金自然自洽）；
      ③ `action` 改成「持有」（有持仓）或「不建仓」（原本 0 股）；
      ④ `target_amount` 按**当前持仓 × 执行价**重算（与「维持」行同口径，避免超配守卫误报）；
      ⑤ `target_weight_effective` 按**当前市值**重算、差额记进 `weight_to_cash`
         （⇒ 「计划权益权重」与「期末现金」仍然自洽）；
      ⑥ `reason` 追加**为什么被拦**（金额 + 档位 + 门槛 + 折合 bp + 省下的最低佣金）。
    """
    amount = float(amount)
    threshold = float(threshold)
    cur = int(row.get("current_shares") or 0)
    price = row.get("price")
    price = float(price) if price is not None else None

    row["min_amount_skipped"] = True
    row["min_amount_kind"] = kind
    row["min_amount_threshold"] = threshold
    row["min_amount_skipped_amount"] = round(amount, 2)
    row["delta_shares"] = 0
    row["target_shares"] = cur
    row["est_amount"] = 0.0
    row["est_fee"] = 0.0
    row["action"] = "持有" if cur > 0 else "不建仓"

    _eff = 0.0
    if total_assets and price is not None:
        _eff = (cur * price) / float(total_assets)
    row["target_weight_effective"] = _eff
    #: 🔴 2026-11-13 夜实测修（闸门 P13 自洽崩 ⇒ 流水线 FATAL exit 1）：
    #:    `weight_to_cash` 是**有符号**的「目标权重 − 实际落地权重」差额，
    #:    **不许截断成非负** —— 旧实现 `max(0.0, ...)` 把「超配没减仓」的**负**差额抹成 0
    #:    ⇒ Σ`weight_to_cash` ≠ 席位缺口 ⇒ 「Σ有效权重 + Σ权重归现金 = 1」恒等式崩
    #:    ⇒ 计划件自检 FATAL ⇒ **流水线退出码 1 ⇒ 产物被判「不可交付」**。
    #:    实测（规则层 · 11-03 基线）：512800 目标 20.0% / 超配实际 22.70%
    #:    ⇒ 有符号差额 = **−2.70%**（现金相应比目标少 2.70pp）。
    row["weight_to_cash"] = float(row.get("target_weight") or 0.0) - _eff
    if price is not None:
        row["target_amount"] = cur * price

    fee_now = _strict_fee(amount, commission_rate, min_commission)
    saved = fee_now
    bp = (float(min_commission) / amount * 1e4) if amount else 0.0
    suffix = (
        f"；🔴 **金额 {amount:.0f} 元 < {kind}门槛 {threshold:.0f} 元**"
        f"（按 {float(min_commission):.0f} 元最低佣金折合 {bp:.1f} bp）"
        f" ⇒ **本轮不调仓**（省最低佣金 {saved:.2f} 元）"
    )
    if reason_suffix:
        suffix += f"；{reason_suffix}"
    #: 🔴 2026-11-16 修（周一交付件复核发现）：**同一行可能被作废两次**
    #:    （阶段 5 按信号日价作废 → 阶段 6 按节点日价重新作废；两次金额口径一致时
    #:    **披露句完全相同** ⇒ `reason` 里同一句出现两遍 ⇒ 交付件表格读起来像重复报错）。
    #:    ⇒ 修法：**幂等追加** —— 句子里已经有这句就不再追加（披露一次即可）。
    #:    ⚠️ 只影响**文案**；金额/费用归零/现金自洽/门槛计数本来就正确（`skipped_count` 不翻倍）。
    _reason = str(row.get("reason") or "")
    already = bool(suffix) and suffix in _reason
    if not already:
        row["reason"] = (_reason + suffix).strip("；")

    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "kind": kind,
        "amount": round(amount, 2),
        "threshold": threshold,
        "min_commission_saved": round(saved, 2),
        "effective_bp": round(bp, 1),
        #: 🔴 2026-11-16：True = 这次**没有**重复追加披露句（同一行此前已被作废过）
        "already_disclosed": already,
    }


def apply_min_trade_threshold(
    plans: list[dict],
    *,
    total_assets: float | None,
    commission_rate: float = 0.0,
    thresholds: dict | None = None,
    enabled: bool = True,
    price_basis: str = "",
) -> dict:
    """最小调仓门槛（方案一）：把**低于门槛**的调仓动作就地作废（**行不删**）。

    ⇒ 返回值 = **披露结构**（挂到 `plan["min_amount_gate"]` ⇒ JSON 与 MD 如实呈现）。
    """
    thr = resolve_thresholds(thresholds)
    min_commission = float(C.ETF_MIN_COMMISSION_STRICT)

    skipped: list[dict] = []
    considered = 0
    new_open_total = 0
    new_open_skipped = 0
    saved_fee = 0.0

    if enabled:
        for row in plans:
            if row.get("ta_rejected"):
                continue                      # 「维持」行不建仓也不卖出 ⇒ 不参与
            delta = int(row.get("delta_shares") or 0)
            if delta == 0:
                continue                      # 无动作 / 不可建仓 ⇒ 不参与
            amount = abs(float(row.get("est_amount") or 0.0))
            if amount <= 0:
                continue
            kind = classify_min_amount_kind(row)
            if not kind:
                continue
            considered += 1
            limit = thr[kind]
            if kind == "新开仓":
                new_open_total += 1
            if amount >= limit:
                continue                      # 达标 ⇒ 放行

            record = block_row(
                row,
                kind=kind,
                amount=amount,
                threshold=limit,
                commission_rate=commission_rate,
                min_commission=min_commission,
                total_assets=total_assets,
            )
            saved_fee += float(record["min_commission_saved"])
            if kind == "新开仓":
                new_open_skipped += 1
            skipped.append(record)

    deadlock = bool(new_open_total > 0 and new_open_skipped >= new_open_total)
    notes = [
        f"门槛口径（按交易性质拆三档，**不许单一金额一刀切**）："
        f"清仓/止损 **{thr['清仓止损']:.0f} 元**（无条件执行）"
        f"｜新开仓 **{thr['新开仓']:.0f} 元**｜存量微调 **{thr['存量微调']:.0f} 元**",
        f"折合费率口径 = **单边 {min_commission:.0f} 元最低佣金**（研究链自身费用模型是「免 5 元」"
        f"⇒ 本节 bp 与「省下的最低佣金」是**下限口径**，不参与计划件现金流）",
    ]
    if price_basis:
        notes.append(f"判定金额口径 = **{price_basis}**")
    if deadlock:
        notes.append(
            "🔴 **门槛与账户规模不匹配（数学死锁）**：本轮**所有**新开仓都被门槛拦下"
            " ⇒ 以后再也买不进任何新 ETF ⇒ 必须下调「新开仓」门槛或提高账户规模"
        )
    return {
        "enabled": bool(enabled),
        "thresholds": thr,
        "min_commission_strict": min_commission,
        "considered_actions": considered,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "new_open_total": new_open_total,
        "new_open_skipped": new_open_skipped,
        "deadlock_risk": deadlock,
        "min_commission_saved": round(saved_fee, 2),
        "notes": notes,
    }
