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

🔴 **2026-11-17（夜班 · 真机闸门 P12 红灯）新增口径：风险硬约束优先于最低佣金门槛**
    真机证据（快照 `20261117_190638`）：512800 目标 16% / 实际超配 22.5%（执行价口径 23.5%），
    减仓 **2,290 元** < 存量微调门槛 **2,800 元** ⇒ 旧口径把它**拦下**（省 5 元最低佣金）
    ⇒ 计划权益权重 **87.48%** 破生效上限 **80%**、计划现金权重 **12.52%** 破 **20%** 现金预留
    ⇒ 闸门 P12 转红（**真 bug**：门槛把「5 元手续费」放在了「权益上限 / 现金预留」之上）。
    ⇒ 新口径：**拦下这一笔会让组合权益权重破上限时 ⇒ 门槛不得作废这笔减仓**（照常执行并披露）。
    ⚠️ 只对**减仓**生效：拦下买入只会让现金更多 ⇒ 永远不会破现金预留 ⇒ 行为不变。
    ⚠️ 「权益上限」= 本轮**生效**上限（`min(1 − 现金预留比例, TA 建议)`）；没给上限 ⇒ 旧行为不变。

【口径纪律】
* **分类只看三个事实**（当前股数 / 目标股数 / 变动方向），**不看金额**；
* **被拦下的行不删**（动作从计划里消失 ⇒ 市值凭空蒸发 ⇒ 交付件自相矛盾）
  ⇒ 保留在 `actions` 里，但 delta/金额/费用归零、动作改「持有 / 不建仓」、
    目标权重按**当前市值**重算、差额归现金；
* **披露**：阈值、被拦明细、折合 bp、省下的最低佣金、死锁风险 ⇒ 一律如实产出。
"""

from __future__ import annotations

import math

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


def _weight_snapshot(plans: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """取「门槛作废前」的目标市值与当前市值（**执行价口径**）。

    ⇒ 返回 ``(intended, current)``：``intended[code] = target_shares × price``、
    ``current[code] = current_shares × price``（单位：元）。
    ⚠️ 必须在**任何一行被作废之前**取快照 ⇒ 作废会改写 `target_shares`。
    """
    intended: dict[str, float] = {}
    current: dict[str, float] = {}
    for row in plans or []:
        code = str(row.get("code") or "")
        price = row.get("price")
        if not code or price is None:
            continue
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        intended[code] = float(row.get("target_shares") or 0) * p
        current[code] = float(row.get("current_shares") or 0) * p
    return intended, current


def risk_veto_for_cap(
    row: dict,
    *,
    kind: str,
    amount: float,
    threshold: float,
    intended: dict[str, float],
    current: dict[str, float],
    total_assets: float | None,
    equity_cap: float | None,
    min_commission: float = 5.0,
    commission_rate: float = 0.0,
    eps: float = 1e-6,
) -> dict | None:
    """🔴 **风险硬约束优先**：这笔减仓若被门槛作废 ⇒ 会不会破「权益上限」？

    ⇒ 破 ⇒ 返回**否决记录**（门槛不得作废这一笔）；不破 ⇒ 返回 ``None``（照旧走门槛）。
    ⚠️ 判定口径（与 `_EQUITY_CAP` / `C.CASH_RESERVE_RATIO` 同源）：
      * ``w_exec``    = 全部行**按计划成交后**的权益权重（Σ 目标市值 ÷ 总资产）；
      * ``w_blocked`` = 把这一行拦下（保留当前持仓）后的权益权重；
      * 只有 ``w_blocked > 上限`` **且** ``w_exec ≤ 上限`` ⇒ **是门槛把组合推破上限**
        ⇒ 否决作废（这正是「5 元手续费让位给风控」的场景）。
    ⚠️ 只管**减仓**（``delta_shares < 0``）：拦下买入只会让现金更多 ⇒ 永远不破现金预留。
    """
    if equity_cap is None or not total_assets:
        return None
    if kind != "存量微调":
        return None
    delta = int(row.get("delta_shares") or 0)
    if delta >= 0:
        return None
    code = str(row.get("code") or "")
    total_assets = float(total_assets)
    sum_intended = float(sum(intended.values()))
    w_exec = sum_intended / total_assets
    w_blocked = (
        sum_intended - float(intended.get(code, 0.0)) + float(current.get(code, 0.0))
    ) / total_assets
    cap = float(equity_cap)
    if not (w_blocked > cap + eps):
        return None
    if w_exec > cap + eps:
        return None          # 破上限**不是**门槛造成的 ⇒ 不归门槛管
    fee_now = max(float(min_commission), float(amount) * float(commission_rate or 0.0))
    return {
        "code": code,
        "name": row.get("name"),
        "kind": kind,
        "amount": round(float(amount), 2),
        "threshold": float(threshold),
        "equity_cap": round(cap, 6),
        "equity_weight_if_executed": round(w_exec, 6),
        "equity_weight_if_blocked": round(w_blocked, 6),
        "min_commission_paid": round(fee_now, 2),
        "reason": (
            f"🔴 **风险硬约束优先于最低佣金门槛**：这笔减仓 {float(amount):.0f} 元虽低于"
            f"「{kind}」门槛 {float(threshold):.0f} 元，但**拦下它**会让计划权益权重升到 "
            f"**{w_blocked * 100:.2f}%**（破生效上限 {cap * 100:.2f}%）"
            f" ⇒ 门槛**不得**作废这一笔 ⇒ 本轮照常执行"
            f"（多付最低佣金 {fee_now:.2f} 元，换回组合回到 "
            f"{w_exec * 100:.2f}% ≤ {cap * 100:.2f}%）"
        ),
    }


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
    reason_override: str = "",
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

    🔴 ``reason_override``（2026-11-19 小资金兜底）：给了就**替换**标准那句
    「金额 X 元 < 档位门槛 Y 元」（用于「额度集中给 #1 ⇒ 其余候选本轮不建仓」这类
    **不是门槛不足**的作废 ⇒ 标准文案会写错事实）。
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
    if reason_override:
        suffix = f"；🔴 **本轮不调仓**：{reason_override}"
    else:
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
        #: 🔴 2026-11-19 小资金兜底：True = 这笔作废**不是**门槛不足（额度集中口径）
        "reason_override": bool(reason_override),
        #: 🔴 2026-11-16：True = 这次**没有**重复追加披露句（同一行此前已被作废过）
        "already_disclosed": already,
    }


def small_account_fallback_enabled(args=None) -> bool:
    """小资金兜底总开关：`--no-small-account-fallback` ⇒ 关；默认 **开**。"""
    if args is None:
        return bool(C.SMALL_ACCOUNT_FALLBACK_ENABLED)
    if getattr(args, "no_small_account_fallback", False):
        return False
    return bool(C.SMALL_ACCOUNT_FALLBACK_ENABLED)


def _round_lot(shares: float, lot: int = 100) -> int:
    """向下取整到整手（ETF 一手 = 100 股）—— 与阶段 5 / 阶段 6 同口径。"""
    if shares is None or shares <= 0:
        return 0
    lot = int(lot or 1)
    return int(math.floor(shares / lot) * lot)


def _fallback_score(row: dict) -> float:
    """候选排名键 = ``final_score``（缺失 ⇒ 排最后，不编分数）。"""
    try:
        value = row.get("final_score")
        return float(value) if value is not None else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


def apply_small_account_fallback(
    plans: list[dict],
    *,
    total_assets: float | None,
    equity_cap: float | None,
    thresholds: dict | None = None,
    commission_rate: float = 0.0,
    enabled: bool = True,
    min_amount: float | None = None,
    max_single_amount: float | None = None,
    lot: int = 100,
    price_basis: str = "",
    preferred_code: str = "",
) -> dict:
    """🔴 小资金兜底（2026-11-19 · 用户拍板）：**额度不足 ⇒ 集中买 #1**。

    【用户原话】
    「**保留 3 万元账户规模**，让 Agent 在阶段 5 增加一条小资金兜底规则：
      当**剩余权益额度不足以均分跨过 3,800 元**时，允许将**单笔建仓下限放宽至 1,500 元**，
      并**集中额度优先买入排名第 1 的顺延标的**。」

    【本函数干什么】
    ① **剩余额度（金额空间、执行价口径）** = 生效权益上限 × 总资产 − Σ(非新开仓候选的目标金额)
       ⇒ 「维持行」（TA 一票否决但仍持仓）**占额**先扣掉 ⇒ 不会因为它而多买；
    ② **触发条件** = 均分额（额度 ÷ 候选数）< 常规「新开仓」门槛
       **且** 集中落地金额 ≥ 放宽后的下限（1,500 元，按整手落地后仍要达标）；
    ③ **集中**：候选里 ``final_score`` 最高者（**同分保持原序**）**独占**全部额度；
    ④ **其余候选**：金额 < 放宽下限 ⇒ 交给**门槛层**正常拦下（口径唯一）；
       金额 ≥ 放宽下限 ⇒ 由本函数**就地作废**（额度已集中，不许第二笔再吃额度）
       ⇒ **无论哪种情况，落地权益都 ≤ 生效上限**。

    【为什么不只改阶段 5】
    阶段 6（节点日价）会把被拦下的碎单**重新算回来** ⇒ 门槛形同虚设
    ⇒ 本函数与门槛**共用本模块**（单一事实源），**阶段 5 / 阶段 6 都要先跑它**。

    ⇒ 返回值 = **披露结构**（挂到 ``plan["small_account_fallback"]`` ⇒ JSON 与 MD 如实呈现）。
    """
    thr = resolve_thresholds(thresholds)
    normal_limit = float(thr["新开仓"])
    relaxed = float(
        C.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT if min_amount is None else min_amount)
    relaxed = min(normal_limit, relaxed) if normal_limit > 0 else relaxed

    base: dict = {
        "enabled": bool(enabled),
        "triggered": False,
        "reason": "",
        "normal_new_limit": round(normal_limit, 2),
        "relaxed_new_limit": round(relaxed, 2),
        "equity_cap": round(float(equity_cap), 6) if equity_cap is not None else None,
        "total_assets": round(float(total_assets), 2) if total_assets is not None else None,
        "allowance_amount": 0.0,
        "per_candidate_amount": 0.0,
        "concentrated_amount": 0.0,
        "candidate_count": 0,
        "preferred_code": str(preferred_code or ""),
        #: 🔴 True = 额度**集中**给候选 #1；False = 只放宽门槛、**保持等权结构**
        "concentrated": False,
        "selected": None,
        "others": [],
        "notes": [],
    }
    if price_basis:
        base["price_basis"] = price_basis

    if not enabled:
        base["reason"] = ("小资金兜底未启用（`--no-min-amount-gate` 或 "
                           "`--no-small-account-fallback`）⇒ 行为与旧版一致")
        return base
    if total_assets is None or float(total_assets) <= 0:
        base["reason"] = "缺总资产 ⇒ 不触发（不编数字）"
        return base
    if equity_cap is None:
        base["reason"] = "缺生效权益上限 ⇒ 不触发（不编数字）"
        return base

    #: 候选 = 「新开仓」且**不是** TA 一票否决的维持行（维持行不建仓也不卖出 ⇒ 不参与）
    cands: list[dict] = [
        row for row in plans
        if not row.get("ta_rejected") and classify_min_amount_kind(row) == "新开仓"
    ]
    base["candidate_count"] = len(cands)
    if not cands:
        base["reason"] = "本轮没有「新开仓」候选 ⇒ 不触发"
        return base

    #: 🔴 剩余额度 = 上限空间 − **非候选行**（维持行 / 清仓 / 存量微调）占用
    _cand_ids = {id(row) for row in cands}
    allowance = float(equity_cap) * float(total_assets)
    for row in plans:
        if id(row) in _cand_ids:
            continue
        value = row.get("target_amount")
        if value is None:
            continue
        allowance -= float(value)
    allowance = max(0.0, allowance)
    per_candidate = allowance / len(cands)
    base["allowance_amount"] = round(allowance, 2)
    base["per_candidate_amount"] = round(per_candidate, 2)

    if per_candidate >= normal_limit:
        base["reason"] = (
            f"额度足以均分跨过门槛（均分 {per_candidate:.2f} 元 ≥ 常规门槛 "
            f"{normal_limit:.0f} 元）⇒ 不触发")
        return base
    if allowance < relaxed:
        base["reason"] = (
            f"剩余额度 {allowance:.2f} 元 < 放宽下限 {relaxed:.0f} 元 ⇒ 不触发"
            "（宁可空仓也不建达不到门槛的碎单）")
        return base

    #: 🔴 排名 #1 = ``final_score`` 最高者（同分保持原序 ⇒ 稳定排序）
    ordered = sorted(cands, key=lambda row: -_fallback_score(row))
    selected = ordered[0]
    #: 🔴 2026-11-19：**阶段 6 必须与阶段 5 选中同一只**（否则两阶段执行卡打架）
    #:    ⇒ 计划件里已带「集中标的」时，优先沿用它（它若仍在候选里）。
    _pref = str(preferred_code or "").strip()
    if _pref:
        _hit = next((r for r in ordered if str(r.get("code") or "").zfill(6) == _pref.zfill(6)), None)
        if _hit is not None:
            selected = _hit
    price = float(selected.get("price") or 0.0)
    if price <= 0:
        base["reason"] = "候选 #1 无有效价格 ⇒ 不触发（不编数字）"
        return base

    #: 🔴🔴 2026-11-19 真机复盘修：**集中只在「#1 自己够不到放宽后门槛」时才做**
    #:    · 若 #1 的**自身金额**已 ≥ 放宽下限 ⇒ 放宽门槛**已经**能让它建仓
    #:      ⇒ **不集中**（保持计划的等权结构 ⇒ 计划权益合计仍 = 生效上限）；
    #:    · 若 #1 也够不到 ⇒ 才把额度**集中**给它（否则额度全躺在现金里）。
    #:    ⚠️ 真机反例（合成夹具）：2 席 × 3,000 元、常规门槛 3,800 元
    #:       ⇒ 旧实现把 6,000 元全压给 #1（19.95%）⇒ 计划权益合计 = 29.95% ≠ 上限 20%（**结构被改坏**）。
    selected_amount = abs(float(selected.get("est_amount") or 0.0))
    if selected_amount >= relaxed:
        base["triggered"] = True
        base["concentrated"] = False
        base["reason"] = (
            f"额度不足均分跨过门槛（均分 {per_candidate:.2f} 元 < 常规门槛 {normal_limit:.0f} 元）"
            f" ⇒ 本轮「新开仓」单笔下限**放宽至 {relaxed:.0f} 元**；"
            f"候选 #1 自身金额 {selected_amount:.2f} 元**已达标** ⇒ **无需集中**（保持等权结构）")
        base["notes"] = [
            f"触发条件：均分额 **{per_candidate:.2f} 元** < 常规「新开仓」门槛 "
            f"**{normal_limit:.0f} 元** ⇒ 本轮「新开仓」单笔下限**放宽至 {relaxed:.0f} 元**（只降不升）",
            f"候选 #1 = {selected.get('code')} {selected.get('name')} 自身金额 "
            f"**{selected_amount:.2f} 元** ≥ 放宽下限 ⇒ 放宽即可建仓 ⇒ **不集中额度**"
            "（计划结构与权益合计不变）",
        ]
        return base

    #: 🔴 集中额度（不得超过**单席上限** ⇒ 免得把集中变成新的超配）
    amount_cap = allowance
    if max_single_amount is not None:
        amount_cap = min(amount_cap, float(max_single_amount))
    target_shares = _round_lot(amount_cap / price, lot)
    landed = target_shares * price
    if landed < relaxed:
        base["reason"] = (
            f"集中额度 {amount_cap:.2f} 元按整手落地仅 {landed:.2f} 元 < 放宽下限 "
            f"{relaxed:.0f} 元 ⇒ 不触发（宁可空仓也不建达不到门槛的碎单）")
        return base

    #: ---- 就地改写候选 #1：独占额度 ----
    current = int(selected.get("current_shares") or 0)
    delta = target_shares - current
    est_amount = delta * price
    selected["target_amount"] = round(landed, 6)
    selected["target_shares"] = int(target_shares)
    selected["delta_shares"] = int(delta)
    selected["est_amount"] = round(est_amount, 6)
    selected["est_fee"] = round(abs(est_amount) * float(commission_rate or 0.0), 6)
    if total_assets:
        selected["target_weight"] = landed / float(total_assets)
    selected["small_account_fallback"] = True
    selected["small_account_fallback_role"] = "集中承接额度"
    #: 🔴🔴 2026-11-19 真机复盘修：**可达权重必须同步**
    #:    `target_weight_effective` 是「计划权益权重」的取数口（阶段 5 `invested_weight`
    #:    = Σ`target_weight_effective`）⇒ 只改 `target_weight` 会让**计划权益 24.76%
    #:    与实际落地 34.97% 打架**（交付件自相矛盾）⇒ 这里一并改。
    if "target_weight_effective" in selected:
        selected["target_weight_effective"] = float(selected.get("target_weight") or 0.0)
    if "weight_to_cash" in selected:
        selected["weight_to_cash"] = 0.0

    _reason = str(selected.get("reason") or "")
    _note = (
        f"🔴 **小资金兜底**：剩余权益额度 {allowance:.2f} 元不足以均分跨过"
        f"「新开仓」常规门槛 {normal_limit:.0f} 元（均分 {per_candidate:.2f} 元）"
        f" ⇒ 本行**集中承接**额度 {landed:.2f} 元"
        f"（本轮新开仓单笔下限放宽至 {relaxed:.0f} 元，只降不升）"
    )
    if _note not in _reason:
        selected["reason"] = (_reason + "；" + _note).strip("；")

    #: ---- 其余候选：金额 < 放宽下限 ⇒ 交给门槛层；≥ 放宽下限 ⇒ 本函数就地作废 ----
    others: list[dict] = []
    for row in ordered[1:]:
        amount = abs(float(row.get("est_amount") or 0.0))
        by_gate = amount < relaxed
        #: 🔴 其余候选的**席位预算已被集中给 #1** ⇒ 它们的计划目标权重归零
        #:    （否则 Σ目标权重会**重复计**这笔预算 ⇒ 计划权益合计虚高、破上限口径失真）
        #:    「可达权重」同步归零（它们本轮不是持仓 ⇒ 计划权益不得把它们算进去）
        row["target_weight"] = 0.0
        if "target_weight_effective" in row:
            row["target_weight_effective"] = 0.0
        if not by_gate:
            block_row(
                row,
                kind="新开仓",
                amount=amount,
                threshold=relaxed,
                commission_rate=commission_rate,
                min_commission=float(C.ETF_MIN_COMMISSION_STRICT),
                total_assets=total_assets,
                reason_override=(
                    f"小资金兜底：额度已集中给候选 #1（{selected.get('code')}）"
                    f" ⇒ 本轮不建仓（避免第二笔再吃额度 ⇒ 落地权益不破生效上限）"),
            )
        others.append({
            "code": row.get("code"),
            "name": row.get("name"),
            "amount": round(amount, 2),
            "threshold": round(relaxed, 2),
            "final_score": row.get("final_score"),
            "blocked": True,
            "blocked_by": (
                "门槛（新开仓档放宽后）" if by_gate
                else "集中额度（其余候选本轮不建仓）"),
        })

    base["triggered"] = True
    base["concentrated"] = True
    base["reason"] = (
        f"额度不足均分跨过门槛（均分 {per_candidate:.2f} 元 < 常规门槛 {normal_limit:.0f} 元）"
        f" ⇒ 集中额度买入候选 #1")
    base["concentrated_amount"] = round(landed, 2)
    base["selected"] = {
        "code": selected.get("code"),
        "name": selected.get("name"),
        "final_score": selected.get("final_score"),
        "rank_among_candidates": 1,
        "price": round(price, 6),
        "target_amount": round(landed, 2),
        "target_shares": int(target_shares),
        "delta_shares": int(delta),
        "target_weight": round(float(selected.get("target_weight") or 0.0), 6),
        "role": "集中承接额度",
        #: 🔴 True = 沿用**阶段 5**已定的集中标的（保证两阶段同一只）
        "carried_from_plan": bool(_pref),
        #: 🔴 「顺延补位」标记：行内没有结构化字段 ⇒ 从 reason 文案识别（如实披露）
        "is_substitution": "顺延补位" in str(selected.get("reason") or ""),
    }
    base["others"] = others
    base["notes"] = [
        f"触发条件：均分额 **{per_candidate:.2f} 元** < 常规「新开仓」门槛 "
        f"**{normal_limit:.0f} 元**（剩余额度 {allowance:.2f} 元 ÷ 候选 {len(cands)} 只）"
        f" ⇒ 本轮「新开仓」单笔下限**放宽至 {relaxed:.0f} 元**（只降不升）",
        f"集中买入：候选 **#1 = {selected.get('code')} {selected.get('name')}**"
        f"（`final_score` = {selected.get('final_score')}）⇒ 承接额度 **{landed:.2f} 元**"
        f"（目标权重 {float(selected.get('target_weight') or 0.0) * 100:.2f}%）",
        f"其余候选 **{len(others)}** 只本轮**不建仓**"
        f"（额度已集中 ⇒ 落地权益不破生效上限 "
        f"{float(equity_cap) * 100:.2f}%）",
    ]
    return base


def apply_min_trade_threshold(
    plans: list[dict],
    *,
    total_assets: float | None,
    commission_rate: float = 0.0,
    thresholds: dict | None = None,
    enabled: bool = True,
    price_basis: str = "",
    equity_cap: float | None = None,
) -> dict:
    """最小调仓门槛（方案一）：把**低于门槛**的调仓动作就地作废（**行不删**）。

    ⇒ 返回值 = **披露结构**（挂到 ``plan["min_amount_gate"]`` ⇒ JSON 与 MD 如实呈现）。
    🔴 ``equity_cap``（本轮**生效**权益上限）给了以后，多一道**风险否决**：
    拦下某笔减仓会让组合权益权重**破上限** ⇒ **不得作废这一笔**（风控优先于手续费）。
    """
    thr = resolve_thresholds(thresholds)
    min_commission = float(C.ETF_MIN_COMMISSION_STRICT)

    skipped: list[dict] = []
    considered = 0
    new_open_total = 0
    new_open_skipped = 0
    saved_fee = 0.0
    #: 🔴 风险否决记录（门槛**没有**拦下、但**差点**拦下的减仓 ⇒ 如实披露）
    risk_overrides: list[dict] = []

    #: 🔴 作废前的权重快照（作废会改写 `target_shares` ⇒ 必须先取）
    _intended, _current = _weight_snapshot(plans)

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

            #: 🔴 风险硬约束优先（2026-11-17 真机 P12 红灯修）：
            #:    拦下这笔**减仓**会不会把组合权益权重推破生效上限？
            #:    会 ⇒ 门槛**不得**作废它（省 5 元手续费 ≠ 破风控约束）。
            _veto = risk_veto_for_cap(
                row,
                kind=kind,
                amount=amount,
                threshold=limit,
                intended=_intended,
                current=_current,
                total_assets=total_assets,
                equity_cap=equity_cap,
                min_commission=min_commission,
                commission_rate=commission_rate,
            )
            if _veto is not None:
                risk_overrides.append(_veto)
                continue                      # **不拦** ⇒ 这一行照常执行

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
    if risk_overrides:
        notes.append(
            "🔴 **风险硬约束优先于最低佣金门槛**：本轮有 "
            f"**{len(risk_overrides)}** 笔减仓虽低于门槛，但**拦下它**会让计划权益权重"
            "破生效上限 ⇒ 门槛**未**作废 ⇒ 照常执行（多付的最低佣金已计入现金流）"
        )
        for _ov in risk_overrides:
            notes.append(f"　· {_ov.get('code')} {_ov.get('name')}｜{_ov.get('reason')}")
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
        "risk_overrides": risk_overrides,
        "risk_overrides_count": len(risk_overrides),
        "equity_cap_used": (round(float(equity_cap), 6) if equity_cap is not None else None),
        "notes": notes,
    }
