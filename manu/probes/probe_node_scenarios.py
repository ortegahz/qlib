#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""节点日「分歧情景」重算闸门探针（M-S7 新增）。

【用途】
把「用户拍板 N 项分歧 ⇒ 12-01 当天到底买多少股」这条链变成**可执行的闸门**：
两个脚本常驻、**默认路径零回归**（规则层产物不被污染）、情景 A/A/A ≡ 规则层、
2^N 情景聚合件结构完整、现金非负、整手合法、**独立重算**现金流与破单席标记、幂等、
以及三个**真负例**（非法情景字母 / 未知代码 / 篡改现金 ⇒ 必须 FAIL）。

🔴 **分歧项个数 / 情景数一律不写死**：N 与期望情景数（2^N）都从**当前产物 payload**
    动态取（「全 A」组合标签、负例用的分歧码也都按产物里的顺序动态取）
    ⇒ 分歧项换人时探针不用改，也不会与新产物错配。

【为什么需要】
M-S7 之前，`reprice_node_day.py` 只会重算**纯规则层**清单 ⇒ 用户拍完板后
**没有任何工具**能算出「我选的那个方案在节点日价格下买多少股」⇒ 12-01 当天只能手算。
本探针保证这条链**不会悄悄坏掉**，且**默认（规则层）路径一个字节都没变**。

【🔴 为什么情景独立重算必须套用同一套最小调仓门槛（2026-11-13 方案一）】
`manu/etf_rotation/min_trade_gate.py` 是**最小调仓门槛的单一事实源**（三档阈值 +
交易性质分类 + 作废语义），**阶段 5**（`final_rebalance_plan.py`）与**阶段 6**
（`reprice_node_day.py`，**含 `--scenario` 情景分支**）**共用**它
⇒ **情景产物里的调仓行同样会被门槛作废** ⇒ 探针的独立重算若不套门槛，
就会出现「重算说该建仓、产物说没建」的**假违例**。

🔴 **门槛作废一行的语义**（`min_trade_gate.block_row`）：
① **行保留**（不删行 ⇒ 市值不凭空蒸发）；② `delta_shares` **归零**；
③ `est_amount` / `est_fee` **归零**；④ `action` 改「持有」（有持仓）/「不建仓」（原本 0 股）；
⑤ `target_shares` **回退到当前股数**；⑥ `target_weight_effective` 按**当前市值**重算、
差额（`weight_to_cash`，**有符号**）记进「权重归现金」。

🔴 **2026-11-13 实测假红（修法依据）**：情景 `512800=B` 的意图调整 = 目标权重 16%
   ⇒ 5,400 股 ⇒ 卖 2,300 股 × 0.874 = **2,010.20 元 < 存量微调门槛 2,800 元**
   ⇒ **门槛作废**（行保留、delta=0、金额=0）⇒ 产物**不调仓是对的**；
   旧口径的探针（不套门槛）却判它「应建仓但没建」⇒ **假红**。
⇒ 本探针用 `min_trade_gate.resolve_thresholds` / `classify_min_amount_kind`
   **复刻同一套阈值与分类**，并把「被作废的行」与门槛披露**逐条对账**
   （code / kind / amount / threshold 一致 + 该行 delta=0、金额=0、目标股数回退到当前股数）。

🔴 **2026-11-26 真机 P11 红灯修（门槛「生效阈值」口径分叉）**
   证据：`<RESULTS_ROOT>/_gates_full_20261126.log`（探针 `probe_node_scenarios`）。
   病根：门槛有**两个语义不同的量**（`rules.md §十四.123` ④）——
     · **名义档** = `config.MIN_TRADE_AMOUNT_NEW` = **3,800 元**；
     · **本轮生效档** = 小资金兜底触发时
       `min(名义档, config.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT)` = **1,500 元**。
   真机现象：产物披露 **1,500**（兜底已生效），本探针独立重算却按 **3,800**
   ⇒ **口径分叉** ⇒ P11 误判「515450 新开仓 1,590.60 元本该被门槛作废」
   ⇒ 连带 D5 / D6 两条真负例因「夹具本身就不自洽」而**失去证明力**（三条同时转红）。
   修法（**口径统一到「本轮生效门槛」**）：
     ① 生效门槛的解析口 = `min_trade_gate.resolve_effective_thresholds`
        （**单一事实源**；期望值锚到 `config.py` 的事实源常量）；
     ② 「兜底是否触发」由本探针**独立复算**
        （调**生产侧同一个函数** `min_trade_gate.apply_small_account_fallback`
          ＋ 探针自己重算的**意图行**）⇒ 🔴 **不读被检对象自述**
        （否则「产物自述 ＋ 产物字段」两处同改仍自洽 ⇒ 守卫失灵，§十四.123 ②）；
     ③ P11 的三条「应被作废」断言随之改成「按**生效门槛**不该被作废 ⇒ 与产物一致」。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_node_scenarios.py \\
        --run-root <落点根>/runs
    （退出码 0 = 全过，1 = 有 FAIL，2 = 环境错）

【断言清单】
P1  两个脚本在仓库里常驻（存在 / 非空）
P2  默认路径零回归：默认产物**无** `meta.scenario`、动作行**无** `scenario_override`
P3  情景 A/A/A 与默认运行**逐条一致**（股数 / 目标金额 / 动作；🔴 2026-11-13 起
    **按原始浮点值比较**，容差只放浮点表示误差 —— 旧口径拿「默认的原始权重」比
    「聚合件的 4 位小数百分数（展示值）」⇒ 同一笔权重也因打印精度不同被判红）
P4  聚合件结构完整（情景数 = 2^N / notes 非空 / 3 条断言全 PASS / disclaimer 非空）
P5  全部情景期末现金**均不为负**且 `feasible`
P6  所有落地股数都是**整手**（100 股）
P7  **独立重算**：逐情景从 trades 手推现金恒等式 == 产物 `cash_end`（容差 0.01 元）
P8  **独立重算**：破单席标记完整（落地金额 > 单席上限 ⇔ `slot_breach`）
P9  幂等：聚合件两次独立运行 sha1 **逐字节**相同
P10 情景覆盖语义：选项 B 的落地股数与 `llm_final_decision.targets` 的 lots/target_pct 一致
P11 最小调仓门槛披露对账（**本轮生效门槛** ⇔ 产物 skipped：code/kind/amount/threshold
    + 作废行 delta=0 / 金额=0 / 目标股数回退 + 兜底披露锚事实源常量）
D1  真负例：情景字母非法（X）⇒ 退出码 ≠ 0 且中文报错
D2  真负例：情景代码不在计划件里 ⇒ 退出码 ≠ 0 且中文报错
D3  真负例：篡改聚合件的 `cash_end` ⇒ P7 的独立重算必须 FAIL（证明闸门有牙）
D4  真负例：空仓情景（positions 为空）里塞破单席 ⇒ P8 必须 FAIL（证明闸门有牙）
D5  真负例：把「被门槛作废」的行改成 delta≠0 ⇒ P11 必须 FAIL（证明闸门有牙）
D6  真负例：把门槛披露里某条 skipped 记录删掉 ⇒ P11 必须 FAIL（证明闸门有牙）
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
while not (REPO_ROOT / "manu" / "etf_rotation" / "reprice_node_day.py").is_file():
    if REPO_ROOT.parent == REPO_ROOT:
        break
    REPO_ROOT = REPO_ROOT.parent

REPRICE = REPO_ROOT / "manu" / "etf_rotation" / "reprice_node_day.py"
BATCH = REPO_ROOT / "manu" / "etf_rotation" / "make_node_scenarios.py"

#: 🔴 2026-11-09 修（critic 复核轮）：探针的**金额阈值 / 整手 / 起始现金**一律从
#:    **单一事实源**取（`account.py` 的账户口径 + `final_rebalance_plan.LOT` 的整手），
#:    **不许写死**（旧值：整手 100、单席上限 5,970.934 元、现金 15,518.87 元）⇒
#:    总资产 / 单席权重一变，探针与产品**静默分叉**（判红判绿都可能是错的）。
sys.path.insert(0, str(REPO_ROOT / "manu" / "etf_rotation"))
import config as C  # noqa: E402  （落点常量单一事实源）
import account as A  # noqa: E402
import min_trade_gate as gate_mod  # noqa: E402  （最小调仓门槛：阶段5/阶段6 单一事实源）


def _resolve_lot() -> int:
    """整手份数：真源 = `final_rebalance_plan.LOT`（兜底 100，与 `probe_node_reprice` 同款）。"""
    try:
        from final_rebalance_plan import LOT as _LOT  # noqa: PLC0415
        return int(_LOT)
    except Exception:  # noqa: BLE001
        return 100


#: 整手（ETF 最小交易单位）
LOT = _resolve_lot()


def _resolve_round_lot():
    """整手取整：真源 = `final_rebalance_plan._round_lot`（兜底 = 向下取整到整手）。"""
    try:
        from final_rebalance_plan import _round_lot as _rl  # noqa: PLC0415
        return _rl
    except Exception:  # noqa: BLE001
        def _rl(shares: float) -> int:
            if shares is None or shares <= 0:
                return 0
            return int(math.floor(shares / LOT) * LOT)
        return _rl


#: 整手取整（**向下**取整 —— 与产品 `reprice_node_day._round_lot` 同口径）
ROUND_LOT = _resolve_round_lot()

#: 节点日（情景重算的节点日；**唯一一处**写死 ⇒ 不许在别处再散落字面量）
NODE_DATE = "2026-11-04"

#: 单席上限（账户级 20%）= 总资产 × 单席权重 —— **兜底**口径（`account.py` 研究链旧账户）
#: 🔴 2026-11-10：这两个常量（5970.934 / 15518.87）是 **2026-11-04 的 5 只旧账户**口径。
#:    本轮该探针跑的是 canonical 根（口径自洽），但 canonical 根一旦换上
#:    **按新账户生成**的计划就会与产品（`reprice_node_day.py` 用的是
#:    **计划件口径**的 `total_assets × 单席权重`）错位 ⇒ 误判。
#: ⇒ 口径改成：**优先从计划件 `meta.total_assets` / `meta.cash` 派生**，
#:   取不到再回落这两个旧常量（见 `_account_basis`）。
SLOT_LIMIT = float(A.TOTAL_ASSETS) * float(A.TARGET_POSITION_WEIGHT)
#: 账户起始现金 —— **兜底**口径（`account.py` 研究链旧账户）
CASH0 = float(A.CASH)


def _account_basis(plan: dict | None) -> tuple[float, float, float, str]:
    """账户口径（总资产 / 现金 / 单席上限）—— **优先从计划件 `meta` 派生**，取不到回落旧常量。

    🔴 2026-11-10：`SLOT_LIMIT` / `CASH0` 写死旧账户常量 ⇒ canonical 根换上
       按新账户生成的计划后，探针的单席上限与产品（`reprice_node_day.py`
       用**计划件口径**的总资产 × 单席权重）就会错位 ⇒ P8 误判。
    ⇒ 现在：**计划件 meta 优先**，旧常量兜底；返回值第四项是**口径说明**
       （必须打进探针输出 ⇒ 不许悄悄换口径）。

    返回：(总资产, 现金, 单席上限, 口径说明)。
    """
    meta = (plan or {}).get("meta") or {}
    total = meta.get("total_assets")
    cash = meta.get("cash")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and float(total) > 0:
        total_assets = float(total)
        total_src = "计划件 meta.total_assets"
        slot_limit = total_assets * float(A.TARGET_POSITION_WEIGHT)
    else:
        total_assets = float(A.TOTAL_ASSETS)
        total_src = "account.py 常量（回落）"
        slot_limit = SLOT_LIMIT
    if isinstance(cash, (int, float)) and not isinstance(cash, bool):
        cash_value = float(cash)
        cash_src = "计划件 meta.cash"
    else:
        cash_value = CASH0
        cash_src = "account.py 常量（回落）"
    return (total_assets, cash_value, slot_limit,
            "总资产 %.2f（%s）｜现金 %.2f（%s）｜单席上限 %.2f（= 总资产 × %.0f%%）"
            % (total_assets, total_src, cash_value, cash_src, slot_limit,
               float(A.TARGET_POSITION_WEIGHT) * 100))


def _d4_empty_position_negative(agg: dict, views: dict, slot_limit: float) -> tuple[bool, str]:
    """D4 真负例：给一个**空 positions** 的情景硬塞一条破单席 ⇒ 独立重算必须检出。

    🔴 2026-11-10（实测红灯）：本轮**不存在**空仓情景（分歧项只有 3 个，
       且 512800 是固定持有 ⇒ 任何组合至少留 1 只持仓）⇒ 旧实现「找既有空仓情景」
       找不到 ⇒ 负例**没能触发**（探针无牙）。
    ⇒ 修法：**自造一只** —— 深拷贝一个已有情景、`positions` 置空（**键存在但为空**）、
       label 追加「（合成空仓）」⇒ 塞进 `mutated["scenarios"]`；
       随后照旧给它塞一条凭空 `slot_breaches` ⇒ 独立重算（宇宙 = ∅）必然与产物
       对不上 ⇒ 检出 ⇒ 闸门会 FAIL。

    返回：(负例是否触发, 细节)。
    """
    if not agg:
        return False, "负例未能触发（聚合件为空）"
    mutated = copy.deepcopy(agg)
    empty_scen = next(
        (s for s in mutated.get("scenarios", [])
         if "positions" in s and not (s.get("positions") or [])), None)
    synth = False
    if empty_scen is None:
        src_scen = next((s for s in mutated.get("scenarios", [])), None)
        if src_scen is None:
            return False, "负例未能触发（聚合件里一个情景都没有）"
        empty_scen = copy.deepcopy(src_scen)
        empty_scen["positions"] = []
        empty_scen["label"] = str(empty_scen.get("label") or "") + "（合成空仓）"
        mutated.setdefault("scenarios", []).append(empty_scen)
        synth = True
    empty_scen["slot_breaches"] = [
        {"code": "999999", "name": "凭空破单席",
         "amount": 999999.0, "limit": slot_limit}]
    #: 🔴 2026-11-13：负例改成调用**真正的 P8 检查函数**（`_slot_breach_problems`）
    #:    ⇒ 证明的是 P8 本身有牙，而不是另抄一份口径。
    found, _stale = _slot_breach_problems(mutated.get("scenarios") or [], views, slot_limit)
    detected = bool(found)
    if not detected:
        return False, "改坏后独立重算**未**检出 ⇒ 闸门无牙"
    detail = "空仓情景里凭空塞的破单席被独立重算检出 ⇒ 闸门会 FAIL"
    if synth:
        detail += "｜本轮无空仓情景 ⇒ 负例**自造**了一只「" + str(empty_scen.get("label")) + "」"
    return True, detail


# ==============================================================================
# 最小调仓门槛（方案一 · 2026-11-13）—— 探针侧的**独立重算**
# ==============================================================================

def _signature(choices: dict) -> str:
    """情景选项的**规范签名**（按代码排序）⇒ 情景的唯一键（聚合件 ↔ 节点日产物对表用）。"""
    return ",".join(f"{str(code).zfill(6)}={str(choices[code])}"
                    for code in sorted(choices or {}))


def _plan_actions_map(plan: dict) -> dict[str, dict]:
    """计划件 `actions` ⇒ `{代码: 动作行}`（代码统一 6 位）。"""
    out: dict[str, dict] = {}
    for row in plan.get("actions") or []:
        out[str(row.get("code") or "").zfill(6)] = row
    return out


def _llm_targets_map(plan: dict) -> dict[str, dict]:
    """计划件 `llm_final_decision.targets` ⇒ `{代码: 裁决行}`。"""
    targets = ((plan.get("llm_final_decision") or {}).get("targets") or {})
    return {str(code).zfill(6): (row or {}) for code, row in targets.items()}


def _intended_target_shares(code, letter, plan_action, llm_target, price, total_assets):
    """独立重算「门槛作废**之前**」的目标股数（口径同 `reprice_node_day._recompute`）。

    · 选项 A（规则层）⇒ `target_weight × 总资产` → **向下取整到整手**；
    · 选项 B（LLM 最终裁决）⇒ `lots` ⇒ `lots × 整手`；`target_pct == 0` ⇒ 0 股；
      其余 ⇒ `target_pct% × 总资产` → **向下取整到整手**；
    · **TA 一票否决且保持现仓**的行 ⇒ 目标 = 当前股数（delta=0 ⇒ 不参与门槛）。

    🔴 2026-11-26（P11 真红修）：一并返回「门槛作废**之前**的**目标金额**」
       （`target_amount`）—— 小资金兜底算「剩余额度」时**按目标金额扣非候选行**
       ⇒ 探针独立复算兜底时必须拿到**同一个口径**的目标金额
       （口径与 `reprice_node_day._recompute` 逐项一致，见 `_scenario_intent`）。

    返回 `(目标股数 | None, 口径说明, 目标权重百分数 | None, 目标金额 | None)`。
    """
    action = plan_action or {}
    cur = int(action.get("current_shares") or 0)
    if bool(action.get("ta_rejected")) and bool(action.get("held_not_touched")):
        return (cur, "TA 一票否决 ⇒ 保持现仓（不参与门槛）", None,
                cur * float(price or 0.0))
    if letter == "B":
        lots = (llm_target or {}).get("lots")
        pct = (llm_target or {}).get("target_pct")
        if lots not in (None, ""):
            _shares = int(lots) * LOT
            return (_shares,
                    f"LLM 裁决：买 {int(lots)} 手（固定 {int(lots) * LOT} 股）", None,
                    _shares * float(price or 0.0))
        if pct is not None and float(pct) == 0:
            return 0, "LLM 裁决：不建仓（目标权重 0%）", 0.0, 0.0
        if pct is None:
            return None, "LLM 裁决既无 lots 也无 target_pct ⇒ 不猜", None, None
        amount = float(pct) / 100.0 * float(total_assets or 0.0)
        return (ROUND_LOT(amount / float(price)),
                f"LLM 裁决目标权重 {float(pct):.4f}%", float(pct), amount)
    weight = float(action.get("target_weight") or 0.0)
    amount = weight * float(total_assets or 0.0)
    shares = ROUND_LOT(amount / float(price)) if amount > 0 else 0
    return shares, f"规则层 target_weight {weight * 100:.4f}%", weight * 100.0, amount


def _gate_judgement(current_shares, intended_shares, price, ta_rejected, thresholds):
    """独立重算**门槛判定**（分类 + 阈值**全部取自 `min_trade_gate` 单一事实源**）。

    判定顺序与 `min_trade_gate.apply_min_trade_threshold` 一致：
    `ta_rejected` 行不参与 ⇒ `delta == 0` 不参与 ⇒ 金额 ≤ 0 不参与 ⇒
    分类（清仓止损 / 新开仓 / 存量微调）⇒ **金额 < 该档门槛 ⇒ 作废**（否则放行）。
    """
    delta = int(intended_shares) - int(current_shares)
    amount = abs(delta) * float(price or 0.0)
    thr = gate_mod.resolve_thresholds(thresholds)
    if ta_rejected or delta == 0 or amount <= 0:
        return {"kind": "", "amount": round(amount, 2), "threshold": None,
                "blocked": False, "delta": delta}
    kind = gate_mod.classify_min_amount_kind({
        "current_shares": int(current_shares),
        "target_shares": int(intended_shares),
        "delta_shares": delta,
    })
    if not kind:
        return {"kind": "", "amount": round(amount, 2), "threshold": None,
                "blocked": False, "delta": delta}
    limit = float(thr.get(kind, 0.0))
    return {"kind": kind, "amount": round(amount, 2), "threshold": limit,
            "blocked": bool(amount < limit), "delta": delta}


def _scenario_gate_view(plan, choices, payload, total_assets, thresholds):
    """一个情景的**独立重算**：逐行 ⇒「意图目标股数 + 门槛判定」。

    ⇒ `{代码: {intended_shares, basis, target_pct, price, current_shares,
                kind, amount, threshold, blocked, delta}}`
    """
    intent = _scenario_intent(plan, choices, payload, total_assets)
    return _gate_view_from_intent(intent, thresholds)


def _scenario_intent(plan, choices, payload, total_assets):
    """一个情景的**意图行**（门槛作废**之前**）—— 独立重算。

    ⇒ `{代码: {intended_shares, target_amount, basis, target_pct, price,
                current_shares, delta, amount, final_score, ta_rejected}}`

    🔴 **口径与 `reprice_node_day._recompute` 逐项一致**（探针不许另抄一份）：
       · 规则层（选项 A）⇒ `target_amount = target_weight × 总资产`；
       · 选项 B「按手数」⇒ `target_amount = 股数 × 节点日价`；
       · 选项 B「按权重」⇒ `target_amount = 权重% × 总资产`；
       · 选项 B「不建仓」⇒ `target_amount = 0`；
       · TA 一票否决且保持现仓 ⇒ `target_amount = 当前股数 × 节点日价`。
    🔴 为什么要「意图行」：小资金兜底（`min_trade_gate.apply_small_account_fallback`）
       算「剩余额度」时要**扣掉非候选行的目标金额** ⇒ 兜底判定必须在
       **门槛作废之前**的行上算 ⇒ 探针必须自己重算出这批行（不能读产物自述）。
    """
    actions = _plan_actions_map(plan)
    llm_targets = _llm_targets_map(plan)
    intent: dict[str, dict] = {}
    for row in payload.get("actions") or []:
        code = str(row.get("code") or "").zfill(6)
        price = float(row.get("price") or 0.0)
        if price <= 0:
            continue
        plan_action = actions.get(code)
        if plan_action is None:
            continue
        cur = int(plan_action.get("current_shares") or 0)
        letter = str((choices or {}).get(code) or "A")
        shares, basis, pct, target_amount = _intended_target_shares(
            code, letter, plan_action, llm_targets.get(code), price, total_assets)
        if shares is None:
            continue
        delta = int(shares) - cur
        intent[code] = {
            "intended_shares": int(shares),
            "target_amount": target_amount,
            "basis": basis,
            "target_pct": pct,
            "price": price,
            "current_shares": cur,
            "delta": delta,
            "amount": abs(delta) * price,
            "final_score": plan_action.get("final_score"),
            "name": plan_action.get("name") or row.get("name") or "",
            "ta_rejected": bool(row.get("ta_rejected")),
            "held_not_touched": bool(row.get("held_not_touched")),
        }
    return intent


def _gate_view_from_intent(intent, thresholds):
    """意图行 ⇒ **门槛判定**视图（分类 + 阈值全部取自 `min_trade_gate` 单一事实源）。"""
    view: dict[str, dict] = {}
    for code, it in (intent or {}).items():
        judgement = _gate_judgement(
            it.get("current_shares") or 0,
            it.get("intended_shares") or 0,
            it.get("price") or 0.0,
            bool(it.get("ta_rejected")),
            thresholds,
        )
        judgement.update({
            "intended_shares": int(it.get("intended_shares") or 0),
            "basis": it.get("basis"),
            "target_pct": it.get("target_pct"),
            "price": it.get("price"),
            "current_shares": int(it.get("current_shares") or 0),
            "target_amount": it.get("target_amount"),
            "final_score": it.get("final_score"),
        })
        view[code] = judgement
    return view


def _plan_equity_cap(plan):
    """本轮**生效权益上限**（阶段 6 同口径：从计划件 `ta_screening` 读）。"""
    scr = (plan or {}).get("ta_screening") or {}
    for key in ("equity_cap_effective", "equity_cap"):
        value = scr.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _plan_preferred_code(plan):
    """阶段 5 已定的「集中标的」（阶段 6 沿用它 ⇒ 两阶段买同一只）。"""
    fb = (plan or {}).get("small_account_fallback") or {}
    return str((fb.get("selected") or {}).get("code") or "")


def _fallback_rows_from_intent(intent):
    """意图行 ⇒ **小资金兜底**用的动作行（字段口径同 `reprice_node_day._recompute`）。"""
    rows: list[dict] = []
    for code, it in (intent or {}).items():
        delta = int(it.get("delta") or 0)
        rows.append({
            "code": code,
            "name": it.get("name") or "",
            "price": float(it.get("price") or 0.0),
            "current_shares": int(it.get("current_shares") or 0),
            "target_shares": int(it.get("intended_shares") or 0),
            "delta_shares": delta,
            "est_amount": round(abs(delta) * float(it.get("price") or 0.0), 6),
            "target_amount": it.get("target_amount"),
            "final_score": it.get("final_score"),
            "ta_rejected": bool(it.get("ta_rejected")),
            "held_not_touched": bool(it.get("held_not_touched")),
        })
    return rows


def _independent_fallback(plan, intent, total_assets):
    """独立复算本轮**小资金兜底**是否触发（🔴 **不读被检对象的自述**）。

    🔴 **为什么必须独立复算**（`rules.md §十四.123`）：
       守卫的**期望值**（本轮生效门槛）**不许**取自被检对象（节点日产物）自述的
       `small_account_fallback.triggered` ⇒ 否则「产物自述 ＋ 产物字段」两处同改
       仍自洽 ⇒ **守卫失灵**（事实在错、守卫却绿 ⇒ 比没有守卫更危险）。
    🔴 **为什么是「同源共享函数」而不是「自己复刻判据」**：
       兜底判据（「剩余额度不足以均分跨过门槛」）只有**一份**——在
       `min_trade_gate.apply_small_account_fallback` 里 ⇒ 探针直接调用**生产侧
       同一个函数**（输入 = 探针自己独立重算的**意图行**）⇒ 判据不会分叉。
    ⇒ 返回 `apply_small_account_fallback` 的披露结构（`triggered` / `relaxed_new_limit` / …）。
    """
    rows = _fallback_rows_from_intent(intent)
    return gate_mod.apply_small_account_fallback(
        rows,
        total_assets=total_assets,
        equity_cap=_plan_equity_cap(plan),
        thresholds=None,
        commission_rate=0.0,
        enabled=True,
        #: 🔴 阶段 6 同口径：集中额度不得超过**账户级单席上限**
        max_single_amount=(total_assets * float(A.TARGET_POSITION_WEIGHT)
                           if total_assets else None),
        preferred_code=_plan_preferred_code(plan),
    )


def _scenario_effective_thresholds(plan, intent, total_assets):
    """一个情景的**本轮生效门槛**（独立复算）⇒ `(生效门槛, 兜底披露)`。

    🔴 口径与阶段 6（`reprice_node_day.py`）**逐项一致**：
       「兜底触发 ⇒ 新开仓档 = min(名义档, 放宽下限)」（**只降不升**）。
    ⇒ 解析口 = `min_trade_gate.resolve_effective_thresholds`（**单一事实源**）。
    """
    fb = _independent_fallback(plan, intent, total_assets)
    return gate_mod.resolve_effective_thresholds(None, fallback=fb), fb


def _gate_disclosure_problems(payload, agg_scenario, view, thresholds, fallback=None):
    """门槛披露对账（探针**独立重算** ⇔ 产物披露）+ 作废行语义核对。

    ① 独立重算说「该作废」的行 ⇒ 必须出现在 `min_amount_gate.skipped` 里，
       且 **code / kind / amount / threshold 逐项一致**；
    ② 披露里的行 ⇒ 独立重算也要说「该作废」（否则是口径分叉）；
    ③ 作废行在情景产物里 ⇒ **delta = 0、金额 = 0、目标股数回退到当前股数**（行保留）；
    ④ 作废行在**聚合件**里不许出现调仓（delta ≠ 0）；
    ⑤ 门槛阈值本身 ⇒ 必须与**本轮生效门槛**（探针**独立复算**）一致；
    ⑥ 小资金兜底披露 ⇒ 必须与**事实源常量**一致（`config.py` 的名义档 / 放宽档），
       且「是否触发」必须与探针**独立复算**的结论一致（🔴 不许拿产物自述当期望值）。

    🔴 2026-11-26 真机 P11 红灯修：`thresholds` 的语义是「**本轮生效门槛**」
       （三档齐全；`None` ⇒ 名义档）⇒ **不是**名义档。
       旧口径用名义档（3,800）去对账产物（兜底触发后的 1,500）⇒ 口径分叉 ⇒ 假红。
    """
    problems: list[str] = []
    gate = payload.get("min_amount_gate") or {}
    thr = gate_mod.resolve_thresholds(thresholds)
    for kind_name, limit in thr.items():
        got = (gate.get("thresholds") or {}).get(kind_name)
        if got is None or abs(float(got) - float(limit)) > 0.005:
            problems.append(f"门槛阈值 {kind_name} 产物 {got} vs 独立复算的生效门槛 {limit}")
    problems.extend(_fallback_disclosure_problems(payload, fallback))
    disclosed: dict[str, dict] = {}
    for rec in gate.get("skipped") or []:
        disclosed[str(rec.get("code") or "").zfill(6)] = rec
    expected = {code: j for code, j in (view or {}).items() if j.get("blocked")}
    for code, judge in sorted(expected.items()):
        rec = disclosed.get(code)
        if rec is None:
            problems.append(
                f"{code} 独立重算应被门槛作废（{judge['kind']} {judge['amount']:.2f} 元 "
                f"< {judge['threshold']:.0f} 元）但门槛披露里没有")
            continue
        if str(rec.get("kind") or "") != str(judge["kind"]):
            problems.append(f"{code} 门槛披露 kind {rec.get('kind')!r} vs 独立重算 {judge['kind']!r}")
        if abs(float(rec.get("threshold") or 0.0) - float(judge["threshold"])) > 0.01:
            problems.append(f"{code} 门槛披露 threshold {rec.get('threshold')} vs 独立重算 {judge['threshold']}")
        if abs(float(rec.get("amount") or 0.0) - float(judge["amount"])) > 0.01:
            problems.append(f"{code} 门槛披露 amount {rec.get('amount')} vs 独立重算 {judge['amount']}")
    for code in sorted(disclosed):
        if code not in expected:
            problems.append(f"{code} 门槛披露里被作废，但独立重算不认为该作废（口径分叉）")
    rows = {str(r.get("code") or "").zfill(6): r for r in payload.get("actions") or []}
    for code in sorted(expected):
        row = rows.get(code)
        if row is None:
            problems.append(f"{code} 门槛作废的行在情景产物里缺失")
            continue
        if int(row.get("delta_shares") or 0) != 0:
            problems.append(f"{code} 被门槛作废 ⇒ delta 应为 0，实际 {row.get('delta_shares')}")
        if abs(float(row.get("est_amount") or 0.0)) > 0.005:
            problems.append(f"{code} 被门槛作废 ⇒ 金额应为 0，实际 {row.get('est_amount')}")
        if int(row.get("target_shares") or 0) != int(row.get("current_shares") or 0):
            problems.append(f"{code} 被门槛作废 ⇒ 目标股数应回退到当前股数 "
                            f"{row.get('current_shares')}，实际 {row.get('target_shares')}")
        if agg_scenario:
            for t in agg_scenario.get("trades") or []:
                if str(t.get("code") or "").zfill(6) == code and int(t.get("delta_shares") or 0) != 0:
                    problems.append(f"{code} 被门槛作废 ⇒ 聚合件里却有 delta={t.get('delta_shares')} 的调仓")
    return problems


def _fallback_disclosure_problems(payload, fallback):
    """小资金兜底披露的对账（**事实源锚定** ＋ **独立复算**）⇒ 违例清单。

    🔴 `rules.md §十四.123`：
       ① 期望值锚到**事实源常量**（`config.py` 的名义档 / 放宽档）；
       ② 产物自述只用来「跟事实源比」，**绝不**把自述的值当期望值；
       ③ 「名义档 vs 本轮生效档」是**两个语义不同的量** ⇒ 两个分支都要覆盖。
    ⇒ 本函数覆盖：兜底披露里的**两个档位字面量**（锚事实源）＋
      「是否触发 / 额度 / 均分额 / 候选数」（与探针独立复算逐项比对）。
    """
    problems: list[str] = []
    fb = payload.get("small_account_fallback") or {}
    if not fb:
        return problems
    normal = fb.get("normal_new_limit")
    relaxed = fb.get("relaxed_new_limit")
    if normal is None or abs(float(normal) - float(C.MIN_TRADE_AMOUNT_NEW)) > 0.005:
        problems.append(f"兜底披露名义档 {normal} vs 事实源 "
                        f"MIN_TRADE_AMOUNT_NEW {C.MIN_TRADE_AMOUNT_NEW}")
    if relaxed is None or abs(float(relaxed)
                              - float(C.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT)) > 0.005:
        problems.append(f"兜底披露放宽档 {relaxed} vs 事实源 "
                        f"MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT "
                        f"{C.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT}")
    if fallback is None:
        return problems
    if bool(fb.get("triggered")) != bool(fallback.get("triggered")):
        problems.append(f"兜底是否触发 产物 {fb.get('triggered')} vs 独立复算 "
                        f"{fallback.get('triggered')}（口径分叉）")
    if int(fb.get("candidate_count") or 0) != int(fallback.get("candidate_count") or 0):
        problems.append(f"兜底候选数 产物 {fb.get('candidate_count')} vs 独立复算 "
                        f"{fallback.get('candidate_count')}")
    for key in ("allowance_amount", "per_candidate_amount"):
        got = fb.get(key)
        want = fallback.get(key)
        if got is None or want is None:
            continue
        if abs(float(got) - float(want)) > 0.05:
            problems.append(f"兜底 {key} 产物 {got} vs 独立复算 {want}")
    return problems


def _slot_breach_problems(scenarios, views, slot_limit):
    """破单席标记完整性（**独立重算**，门槛感知）⇒ `(违例描述, 陈旧标记清单)`。

    口径（与 `reprice_node_day.py` 的 `slot_breach` 判据同源）：
    **落地持仓金额**（落地股数 × 节点日价）> 单席上限 ⇔ 有 `slot_breach` 标记；
    宇宙 = 聚合件的**落地持仓全集** `positions`（含「本轮不动」的仓位）。

    🔴 **被门槛作废的行按「门槛后」的最终落地判定**（2026-11-13 生产修复后）：
       生产已把 `slot_breach` 的重算挪到 `apply_min_trade_threshold` **之后**
       ⇒ 判据 = 门槛作废后的**最终落地股数 × 节点日价**。
       🔴 为什么必须这样：门槛拦下一笔「超配减仓」时，账户**仍然超单席上限**
       （实测：落地 6,729.80 元 > 上限 4,776.75 元）⇒ 破单席**必须报出来**，
       否则产物自相矛盾（`landed_amount` 超限却 `slot_breach=false`）。
       ⇒ 若产物仍按「门槛前」目标股数判定 ⇒ 本探针**判红**（口径分叉 ⇒ 硬失败）。
    """
    problems: list[str] = []
    stale: list[str] = []
    for s in scenarios:
        view = views.get(_signature(s.get("choices") or {})) or {}
        if "positions" not in s:
            problems.append(
                f"{s.get('label')} 聚合件里**没有 positions 键**（落地持仓全集）"
                "⇒ 产物结构不完整 ⇒ 无法独立重算破单席")
            continue
        positions = s.get("positions") or []
        recomputed: set[str] = set()
        for p in positions:
            code = str(p.get("code") or "").zfill(6)
            price = float(p.get("price") or 0.0)
            landed = int(p.get("target_shares") or 0) * price
            judge = view.get(code) or {}
            #: 🔴 2026-11-13（生产修复后）：破单席判据 = **门槛作废之后的最终落地金额**
            #:    （生产已把 `slot_breach` 的重算挪到 `apply_min_trade_threshold` 之后 ⇒
            #:    「门槛拦下一笔减仓」时，账户**仍然超单席上限** ⇒ 必须报出来）。
            if landed > slot_limit + 0.005:
                recomputed.add(code)
            if judge.get("blocked"):
                #: 口径分叉检测：**产物自己的标记**必须与「门槛后的落地金额」一致
                #: （生产已于 2026-11-13 把破单席重算挪到门槛作废之后 ⇒ 不一致 ⇒ 硬失败）。
                _prod_flag = bool(p.get("slot_breach"))
                if _prod_flag != (landed > slot_limit + 0.005):
                    stale.append(
                        f"{s.get('label')}/{code}：门槛作废后落地 {landed:.2f} 元 vs "
                        f"单席上限 {slot_limit:.2f} 元 ⇒ 应标记破单席="
                        f"{landed > slot_limit + 0.005}，但产物标记={_prod_flag}"
                        f"（门槛前意图股数 {judge.get('intended_shares')}）")
        claimed = {str(b.get("code") or "").zfill(6) for b in s.get("slot_breaches") or []}
        flagged = {str(p.get("code") or "").zfill(6)
                   for p in positions if p.get("slot_breach")}
        if claimed != recomputed:
            problems.append(f"{s.get('label')} 重算 {recomputed} vs 产物 {claimed}")
        if flagged != claimed:
            problems.append(f"{s.get('label')} 逐行 slot_breach 标记 {flagged} "
                            f"与破单席清单 {claimed} 不一致")
    #: 🔴 2026-11-13：口径分叉（门槛作废前后判据不一致）**不再是「披露」而是硬失败**
    #:    ⇒ 宁可红，不要「静默放过」一个没报出来的破单席。
    for _msg in stale:
        problems.append(f"[口径分叉] {_msg}")
    return problems, stale


def _p10_problems(scenarios, views, llm_targets):
    """选项 B 的落地股数/权重必须与 `llm_final_decision` 一致（**门槛感知**）。

    · 门槛**未**作废 ⇒ 产物里必须有这笔交易，且股数 / 权重与裁决一致；
    · 门槛**已**作废（金额 < 该档门槛）⇒ 产物**不调仓**才是对的
      （行保留、delta=0、金额=0 ⇒ 不进 `trades`）；
    · 意图目标 = 当前持仓 ⇒ 本轮不该调仓（不许凭空多一笔）。
    """
    problems: list[str] = []
    for s in scenarios:
        choices = s.get("choices") or {}
        view = views.get(_signature(choices)) or {}
        for code, letter in choices.items():
            if letter != "B":
                continue
            code = str(code).zfill(6)
            tgt = llm_targets.get(code) or {}
            lots = tgt.get("lots")
            pct = tgt.get("target_pct")
            judge = view.get(code)
            hit = next((t for t in (s.get("trades") or []) if t["code"] == code), None)
            if judge is None:
                problems.append(f"{s.get('label')}/{code} 情景产物里找不到该行 ⇒ 无法核对落地")
                continue
            if judge.get("blocked"):
                if hit is not None and int(hit.get("delta_shares") or 0) != 0:
                    problems.append(
                        f"{s.get('label')}/{code} 被门槛作废（{judge['kind']} "
                        f"{judge['amount']:.2f} 元 < {judge['threshold']:.0f} 元）"
                        f"却仍有 delta={hit.get('delta_shares')} 的调仓")
                continue
            if lots not in (None, ""):
                want = int(lots) * LOT
                if hit is None or int(hit.get("target_shares") or 0) != want:
                    problems.append(
                        f"{s.get('label')}/{code} 期望 {want} 股（买 {lots} 手），实际 "
                        f"{hit.get('target_shares') if hit else '未建仓'}")
            elif pct is not None and float(pct) == 0:
                if hit is not None and int(hit.get("target_shares") or 0) > 0:
                    problems.append(f"{s.get('label')}/{code} 应不建仓，实际建了仓")
            elif int(judge.get("delta") or 0) == 0:
                if hit is not None and int(hit.get("delta_shares") or 0) != 0:
                    problems.append(
                        f"{s.get('label')}/{code} 意图目标 = 当前持仓 ⇒ 本轮不该调仓，"
                        f"却有 delta={hit.get('delta_shares')}")
            elif hit is None:
                problems.append(f"{s.get('label')}/{code} 应建仓但没建")
            else:
                want_pct = float(pct or 0.0)
                got = float(hit.get("target_weight_pct") or 0.0)
                if abs(got - want_pct) > 0.05:
                    problems.append(
                        f"{s.get('label')}/{code} 权重 {got:.4f}% vs 裁决 {want_pct:.4f}%")
    return problems


def _run_scenario_payloads(node_date, plan_path, prices_path, out_dir, combos):
    """逐情景跑一次 `reprice_node_day.py --scenario` ⇒ 取回**带门槛披露**的产物。

    🔴 **为什么探针要自己再跑一遍**：`make_node_scenarios.py` 的聚合件**只汇总**
       股数 / 金额 / 破单席，**不携带 `min_amount_gate` 门槛披露**
       ⇒ 「被门槛作废的行」在聚合件里看不出来 ⇒ 探针拿不到对账依据。
    ⇒ 参数与 `make_node_scenarios.py` **逐项一致**（同一计划件 / 同一行情 / 同一总资产口径）。
    """
    out: dict[str, dict] = {}
    for label, choices in combos:
        spec = ",".join(f"{code}={choices[code]}" for code in sorted(choices))
        rc, log = run([sys.executable, str(REPRICE), "--node-date", node_date,
                       "--plan", str(plan_path), "--prices", str(prices_path),
                       "--out-dir", str(out_dir), "--scenario", spec,
                       "--total-assets-basis", "plan"], REPO_ROOT)
        if rc != 0:
            raise RuntimeError(f"情景 `{label}` 的节点日重算失败（退出码 {rc}）⇒ 闸门无法对账门槛披露：\n"
                               f"{(log or '').strip()[-500:]}")
        slug = "_".join(f"{code}{choices[code]}" for code in sorted(choices))
        path = out_dir / f"node_reprice_{node_date.replace('-', '')}_sc_{slug}.json"
        out[_signature(choices)] = read_json(path, f"情景 `{label}` 的节点日重算产物")
    return out


def _find_blocked_case(scen, payloads, views):
    """找一个**真实的**「被门槛作废」案例 ⇒ `(情景行, 节点日产物, 独立重算, 代码)`。"""
    for s in scen:
        sig = _signature(s.get("choices") or {})
        view = views.get(sig) or {}
        payload = payloads.get(sig)
        if payload is None:
            continue
        for code, judge in sorted(view.items()):
            if judge.get("blocked"):
                return s, payload, view, code
    return None


def _synth_blocked_case(total_assets: float):
    """合成一个**自洽**的「被门槛作废」夹具（真实产物里没有作废行时，负例不许空过）。

    ⇒ 返回 `(节点日产物, 聚合件情景行, 独立重算, 代码)`；
    夹具本身**必须通过**门槛对账（否则负例证明不了任何事）。
    """
    code = "999998"
    thr = gate_mod.resolve_thresholds()
    weight = 500.0 / float(total_assets)      # ⇒ 意图目标股数 = 500 股（1.00 元/股）
    plan = {
        "meta": {"total_assets": float(total_assets), "cash": 0.0},
        "actions": [{"code": code, "name": "合成门槛夹具", "action": "卖出",
                     "current_shares": 1000, "target_shares": 500, "delta_shares": -500,
                     "target_weight": weight, "target_amount": 500.0, "price": 1.0,
                     "ta_rejected": False, "held_not_touched": False}],
        "llm_final_decision": {"targets": {}},
    }
    payload = {
        "meta": {"total_assets": float(total_assets)},
        "min_amount_gate": {
            "enabled": True, "thresholds": thr, "skipped_count": 1,
            "skipped": [{"code": code, "name": "合成门槛夹具", "kind": "存量微调",
                         "amount": 500.0, "threshold": float(thr["存量微调"]),
                         "min_commission_saved": 5.0, "effective_bp": 100.0}],
        },
        "actions": [{"code": code, "name": "合成门槛夹具", "action": "持有",
                     "current_shares": 1000, "target_shares": 1000, "delta_shares": 0,
                     "est_amount": 0.0, "est_fee": 0.0, "price": 1.0,
                     "target_weight": weight,
                     "target_weight_effective": 1000.0 / float(total_assets),
                     "weight_to_cash": weight - 1000.0 / float(total_assets),
                     "min_amount_skipped": True, "min_amount_kind": "存量微调",
                     "min_amount_threshold": float(thr["存量微调"]),
                     "min_amount_skipped_amount": 500.0,
                     "ta_rejected": False, "held_not_touched": False}],
    }
    agg_scenario = {"label": "合成夹具（门槛作废）", "choices": {}, "positions": [],
                    "trades": [], "slot_breaches": []}
    view = _scenario_gate_view(plan, {}, payload, total_assets, None)
    return payload, agg_scenario, view, code


def _d5_fake_open_negative(scen, payloads, views, total_assets, eff_thresholds=None):
    """D5 真负例：把「被门槛作废」的行改成 **delta ≠ 0（伪造建仓）** ⇒ 门槛断言必须 FAIL。"""
    case = _find_blocked_case(scen, payloads, views)
    if case is None:
        payload, agg_scenario, view, code = _synth_blocked_case(total_assets)
        source = "合成夹具（本轮产物里没有「被门槛作废」的行）"
        #: 合成夹具**没有兜底** ⇒ 生效门槛 = 名义档
        thr = gate_mod.resolve_thresholds()
    else:
        s, payload, view, code = case
        agg_scenario = s
        source = f"真实产物情景 {s.get('label')}"
        thr = ((eff_thresholds or {}).get(_signature(s.get("choices") or {}))
               or gate_mod.resolve_thresholds())
    base = _gate_disclosure_problems(payload, agg_scenario, view, thr)
    mutated = copy.deepcopy(agg_scenario)
    judge = view.get(code) or {}
    mutated.setdefault("trades", []).append({
        "code": code, "name": "伪造建仓", "action": "买入",
        "current_shares": int(judge.get("current_shares") or 0),
        "target_shares": int(judge.get("current_shares") or 0) + LOT,
        "delta_shares": LOT, "price": float(judge.get("price") or 0.0),
        "est_amount": LOT * float(judge.get("price") or 0.0), "est_fee": 0.0,
        "target_weight_pct": 0.0, "slot_breach": False,
    })
    probs = _gate_disclosure_problems(payload, mutated, view, thr)
    detected = any(code in p for p in probs)
    detail = (f"{source}：{code}（{judge.get('kind')} {judge.get('amount')} 元 "
              f"< {judge.get('threshold')} 元 ⇒ 门槛作废）被伪造成 delta={LOT} 的建仓后，"
              f"门槛对账检出 {len(probs)} 条违例 ⇒ 闸门会 FAIL")
    if base:
        detail = f"⚠️ 夹具本身就不自洽（{base[0]}）⇒ 负例证明力有限"
    elif not detected:
        detail = "改坏后门槛对账**未**检出 ⇒ 闸门无牙"
    return detected and not base, detail


def _d6_missing_disclosure_negative(scen, payloads, views, total_assets, eff_thresholds=None):
    """D6 真负例：把门槛披露里某条 **skipped 记录删掉** ⇒ 门槛对账必须 FAIL。"""
    case = _find_blocked_case(scen, payloads, views)
    if case is None:
        payload, agg_scenario, view, code = _synth_blocked_case(total_assets)
        source = "合成夹具（本轮产物里没有「被门槛作废」的行）"
        #: 合成夹具**没有兜底** ⇒ 生效门槛 = 名义档
        thr = gate_mod.resolve_thresholds()
    else:
        s, payload, view, code = case
        agg_scenario = s
        source = f"真实产物情景 {s.get('label')}"
        thr = ((eff_thresholds or {}).get(_signature(s.get("choices") or {}))
               or gate_mod.resolve_thresholds())
    base = _gate_disclosure_problems(payload, agg_scenario, view, thr)
    mutated = copy.deepcopy(payload)
    gate = mutated.get("min_amount_gate") or {}
    gate["skipped"] = [rec for rec in (gate.get("skipped") or [])
                       if str(rec.get("code") or "").zfill(6) != code]
    mutated["min_amount_gate"] = gate
    probs = _gate_disclosure_problems(mutated, agg_scenario, view, thr)
    detected = any(code in p for p in probs)
    detail = (f"{source}：把 {code} 的 skipped 记录删掉后，门槛对账检出 {len(probs)} 条违例"
              f"⇒ 闸门会 FAIL")
    if base:
        detail = f"⚠️ 夹具本身就不自洽（{base[0]}）⇒ 负例证明力有限"
    elif not detected:
        detail = "删掉披露记录后门槛对账**未**检出 ⇒ 闸门无牙"
    return detected and not base, detail


class Checker:
    """断言收集器。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((check_id, name, bool(ok), detail))


def sha1_of(path: Path) -> str:
    """文件 sha1；读不到就抛异常（不静默跳过）。"""
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    return hashlib.sha1(path.read_bytes()).hexdigest()


def read_json(path: Path, label: str) -> dict:
    """读 JSON 对象；不合法 ⇒ 中文异常。"""
    if not path.is_file():
        raise FileNotFoundError(f"{label}不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label}不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是 JSON 对象：{path}")
    return payload


def run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 合并输出)。"""
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def divergence_codes(scenarios: list[dict]) -> list[str]:
    """从情景列表的 `choices` 键取**分歧码集合**（按首次出现顺序 ⇒ 产物里的顺序）。

    🔴 **不写死任何具体代码、不写死分歧项个数** ⇒ 分歧项换人时探针不用改。
    """
    codes: list[str] = []
    for item in scenarios or []:
        for code in (item.get("choices") or {}):
            if str(code) not in codes:
                codes.append(str(code))
    return codes


def all_a_label(codes: list[str]) -> str:
    """「全 A」组合标签：按产物里的分歧码顺序拼接（**不写死任何具体代码**）。"""
    return ",".join(f"{code}=A" for code in codes)


def all_a_scenario(scenarios: list[dict], expect_label: str = "") -> dict | None:
    """找「全部选项都是 A」的那个情景（2^N 组合里唯一）。

    先按**动态标签**（`expect_label`，按产物里的分歧码顺序拼）匹配；
    标签形态对不上时退回**语义匹配**（choices 里全是 A）⇒ 既不依赖写死的标签串，
    也不依赖标签的书写格式。
    """
    if expect_label:
        for item in scenarios or []:
            if str(item.get("label") or "") == expect_label:
                return item
    for item in scenarios or []:
        choices = dict(item.get("choices") or {})
        if choices and all(letter == "A" for letter in choices.values()):
            return item
    return None


#: 🔴 2026-11-13（探针真空假红 · P3）：金额比较容差（元）。
#:    **只**放**浮点表示误差**（1e-9 量级）⇒ 真实差异照样被拦下：
#:    权重差 0.001（≈ 30 元）≫ 1e-9 ⇒ 必被报出来。
#:    🔴 **不许**把它放大到能掩盖真实差异（也不许拿「展示用舍入值」当判据）。
P3_AMOUNT_TOL = 1e-9


def _p3_default_signature(payload: dict) -> dict[str, dict]:
    """默认（规则层）产物的每条动作 ⇒ `{代码: {股数, 变动, 动作, 目标金额}}`。"""
    out: dict[str, dict] = {}
    for row in payload.get("actions") or []:
        out[str(row.get("code"))] = {
            "target_shares": row.get("target_shares"),
            "delta_shares": row.get("delta_shares"),
            "action": row.get("action"),
            "target_amount": row.get("target_amount"),
        }
    return out


def _p3_aaa_signature(scenario: dict) -> dict[str, dict]:
    """「全 A」情景的每笔交易 ⇒ `{代码: {股数, 变动, 动作, 目标金额}}`（字段与默认侧同构）。"""
    out: dict[str, dict] = {}
    for row in scenario.get("trades") or []:
        out[str(row.get("code"))] = {
            "target_shares": row.get("target_shares"),
            "delta_shares": row.get("delta_shares"),
            "action": row.get("action"),
            "target_amount": row.get("target_amount"),
        }
    return out


def _p3_compare(default_sig: dict[str, dict], aaa_sig: dict[str, dict],
                total_assets: float) -> tuple[list[str], set[str]]:
    """默认（规则层） vs 「全 A」情景逐条比对 ⇒ `(差异描述, 共同代码集)`。

    🔴 2026-11-13（探针真空假红）**口径换版**：旧口径把
       「默认产物的**原始权重** `target_weight`」与「聚合件的 **4 位小数百分数**
       `target_weight_pct`（= `make_node_scenarios.py` 的 `round(w*100, 4)`，
       **展示用**的舍入值）」放在一起比（还各自 `round(..., 10)`）
       ⇒ 同一笔权重也会因**打印精度**不同而被判红
       （实测 512800：默认 0.0533333333 vs A/A/A 0.053333 ⇒ 假红）。
    ⇒ 正确口径：**按原始浮点值比较**，容差只放**浮点表示误差**（`P3_AMOUNT_TOL`）：
       · 股数 / 变动股数 / 动作 ⇒ **逐位相等**；
       · 目标**金额**（两边都存 `round(target_amount, 6)` ⇒ **逐位相同**的原始浮点值）
         ⇒ 差 > `P3_AMOUNT_TOL` 才算不一致（0.001 权重 ≈ 30 元 ⇒ 必被拦下）。
       ⇒ **不许**把「展示用舍入值」当判据（那会把真实差异一起放过）。
    """
    problems: list[str] = []
    common = set(default_sig) & set(aaa_sig)
    for code in sorted(common):
        left = default_sig[code]
        right = aaa_sig[code]
        for field in ("target_shares", "delta_shares", "action"):
            if left.get(field) != right.get(field):
                problems.append(
                    f"{code}: {field} 默认 {left.get(field)!r} vs A/A/A {right.get(field)!r}")
        left_amount = left.get("target_amount")
        right_amount = right.get("target_amount")
        if left_amount is None or right_amount is None:
            if left_amount != right_amount:
                problems.append(
                    f"{code}: 目标金额缺失 默认 {left_amount!r} vs A/A/A {right_amount!r}")
            continue
        diff = abs(float(left_amount) - float(right_amount))
        if diff > P3_AMOUNT_TOL:
            weight_pp = (diff / total_assets * 100.0) if total_assets else float("nan")
            problems.append(
                f"{code}: 目标金额 默认 {float(left_amount):.6f} 元 vs "
                f"A/A/A {float(right_amount):.6f} 元（差 {diff:.6f} 元 ≈ {weight_pp:.6f}pp 权重）")
    for code in sorted(set(aaa_sig) - set(default_sig)):
        problems.append(f"{code}: A/A/A 多出交易项")
    return problems, common


def main() -> int:
    """命令行入口。"""
    ap = argparse.ArgumentParser(description="节点日分歧情景重算闸门")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()
    run_root = Path(args.run_root)

    checker = Checker()
    plan_path = run_root / "final_rebalance_plan.json"
    choices_path = run_root / "decision_scenarios_20261105.json"

    # --- P1 两个脚本常驻 ---
    p1_ok = (REPRICE.is_file() and REPRICE.stat().st_size > 0
             and BATCH.is_file() and BATCH.stat().st_size > 0)
    checker.add("P1", "两个脚本在仓库里常驻（reprice_node_day.py / make_node_scenarios.py）", p1_ok,
                f"{REPRICE}｜{BATCH}" if p1_ok else "缺失或为空")

    plan = read_json(plan_path, "计划件")
    src = read_json(choices_path, "情景组合文件")
    #: 🔴 2026-11-10：账户口径**优先从计划件派生**（旧常量只作兜底）⇒ P7/P8/D3/D4
    #:    与产品（`reprice_node_day.py` 用计划件口径的总资产）同源。
    _total_assets, cash0, slot_limit, _basis_note = _account_basis(plan)
    print("账户口径：" + _basis_note)
    combos = [(str(s["label"]), {str(k): str(v) for k, v in s["choices"].items()})
              for s in src.get("scenarios", [])]
    #: 上游情景组合文件里的分歧码集合（按文件里的顺序 ⇒ 也是「全 A」组合标签的顺序）
    src_codes = divergence_codes(src.get("scenarios") or [])

    tmp_parent = run_root.parent
    if not str(tmp_parent).startswith("/mnt/data/"):
        tmp_parent = C.WORK_ROOT
    tmp_root = tmp_parent / "_node_scen_probe_tmp"
    shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    #: 🔴 逐情景的节点日产物（含门槛披露）单独落**数据集工作区**（本项目允许的落点根之一）
    #:    ⇒ 跑完必删（`finally`）。
    payload_dir = C.WORK_ROOT / "_node_scen_probe_payloads"
    shutil.rmtree(payload_dir, ignore_errors=True)
    payload_dir.mkdir(parents=True, exist_ok=True)

    try:
        # --- 跑默认（规则层）与聚合件 ---
        rc_def, log_def = run([sys.executable, str(REPRICE), "--node-date", NODE_DATE,
                               "--plan", str(plan_path), "--prices", str(C.ETF_PRICES_CSV),
                               "--out-dir", str(tmp_root / "def")], REPO_ROOT)
        default_payload = {}
        if rc_def == 0:
            default_payload = read_json(
                tmp_root / "def" / f"node_reprice_{NODE_DATE.replace('-', '')}.json",
                "默认重算产物")
        rc_batch, log_batch = run([sys.executable, str(BATCH), "--node-date", NODE_DATE,
                                   "--plan", str(plan_path), "--prices", str(C.ETF_PRICES_CSV),
                                   "--choices-from", str(choices_path),
                                   "--out-dir", str(tmp_root / "agg")], REPO_ROOT)
        agg = {}
        if rc_batch == 0:
            agg = read_json(
                tmp_root / "agg" / f"node_scenarios_{NODE_DATE.replace('-', '')}.json",
                "情景聚合件")
        #: 🔴 逐情景的节点日产物（**带门槛披露** `min_amount_gate`）——
        #:    聚合件不携带门槛披露 ⇒ 探针必须自己再跑一遍 `reprice_node_day.py --scenario`
        #:    （参数与 `make_node_scenarios.py` 逐项一致 ⇒ 同一份产物）。
        scen_payloads = _run_scenario_payloads(NODE_DATE, plan_path, C.ETF_PRICES_CSV,
                                               payload_dir, combos)
        #: 每个情景的**独立重算**（意图目标股数 + 门槛判定）⇒ `{情景签名: {代码: 判定}}`
        #: 🔴 2026-11-26（P11 真红修）：门槛判定**必须用「本轮生效门槛」**
        #:    （独立复算 ⇒ 不读产物自述）⇒ 名义档 3,800 只在「兜底未触发」时才是生效档。
        views: dict[str, dict] = {}
        eff_thresholds: dict[str, dict] = {}
        fallbacks: dict[str, dict] = {}
        for _label, _choices in combos:
            _sig = _signature(_choices)
            _payload = scen_payloads.get(_sig)
            if _payload is None:
                continue
            _intent = _scenario_intent(plan, _choices, _payload, _total_assets)
            _thr, _fb = _scenario_effective_thresholds(plan, _intent, _total_assets)
            eff_thresholds[_sig] = _thr
            fallbacks[_sig] = _fb
            views[_sig] = _gate_view_from_intent(_intent, _thr)
        #: 分歧码集合 / 期望情景数：**从当前产物 payload 动态取**（N 个分歧项 ⇒ 2^N 情景）
        #: 🔴 不写死 8 / 16 / 3 / 4 ⇒ 分歧项换人时探针不用改，也不会与新产物错配。
        agg_codes = divergence_codes(agg.get("scenarios") or [])
        div_codes = agg_codes or src_codes
        expected_n = 2 ** len(div_codes)
        aaa_label = all_a_label(src_codes or div_codes)

        # --- P2 默认路径零回归 ---
        p2_problems = []
        if rc_def != 0:
            p2_problems.append(f"默认重算退出码 {rc_def}")
        else:
            if default_payload.get("meta", {}).get("scenario") is not None:
                p2_problems.append("默认产物里出现了 meta.scenario")
            for a in default_payload.get("actions", []):
                if "scenario_override" in a:
                    p2_problems.append(f"默认动作行 {a.get('code')} 出现了 scenario_override")
                if "slot_breach" in a:
                    p2_problems.append(f"默认动作行 {a.get('code')} 出现了 slot_breach")
        p2_ok = not p2_problems
        checker.add("P2", "默认路径零回归：默认产物无 meta.scenario / 无 scenario_override / 无 slot_breach",
                    p2_ok, "；".join(p2_problems) if p2_problems
                    else f"{len(default_payload.get('actions', []))} 条动作行全是纯规则层字段")

        # --- P3 情景 A/A/A ≡ 默认 ---
        p3_problems = []
        aaa = all_a_scenario(agg.get("scenarios", []), aaa_label)
        common: set[str] = set()
        if aaa is None:
            p3_problems.append(f"聚合件里找不到「全 A」情景（期望标签 {aaa_label}）")
        else:
            # 🔴 2026-11-13（探针真空假红）：**按原始浮点值比较**（见 `_p3_compare` 的口径）
            #   —— 旧口径拿「默认的原始权重」比「聚合件的 4 位小数百分数（展示值）」
            #   ⇒ 同一笔权重也因打印精度不同被判红（0.0533333333 vs 0.053333）。
            default_sig = _p3_default_signature(default_payload)
            aaa_sig = _p3_aaa_signature(aaa)
            # 默认产物里的「零变动」条目在聚合件里被剔除了 ⇒ 只比共同的（有变动的）条目
            p3_problems, common = _p3_compare(default_sig, aaa_sig, _total_assets)
        p3_ok = not p3_problems
        checker.add("P3", "情景 A/A/A 与默认（规则层）逐条一致（股数 / 目标金额 / 动作）", p3_ok,
                    "；".join(p3_problems) if p3_problems
                    else f"共同 {len(common)} 条交易项的股数/目标金额/动作全一致"
                         f"（目标金额 = 权重 × 总资产，容差 {P3_AMOUNT_TOL} 元）")

        # --- P4 聚合件结构完整 ---
        p4_problems = []
        scen = agg.get("scenarios", [])
        if not div_codes:
            p4_problems.append("推不出分歧码集合（无法确定期望情景数 2^N）")
        else:
            if len(scen) != expected_n:
                p4_problems.append(f"情景数 {len(scen)} ≠ 2^{len(div_codes)} = {expected_n}")
            if len(combos) != expected_n:
                p4_problems.append(f"来源情景数 {len(combos)} ≠ 2^{len(div_codes)} = {expected_n}")
            if agg_codes and src_codes and sorted(agg_codes) != sorted(src_codes):
                p4_problems.append(
                    f"聚合件的分歧码集合 {sorted(agg_codes)} ≠ 上游 {sorted(src_codes)}")
        if not agg.get("notes"):
            p4_problems.append("notes 为空")
        if not agg.get("disclaimer"):
            p4_problems.append("disclaimer 为空")
        for a in agg.get("assertions", []):
            if a.get("status") != "PASS":
                p4_problems.append(f"聚合件断言 {a.get('id')} 状态 {a.get('status')}")
        p4_ok = not p4_problems
        checker.add("P4", "聚合件结构完整（情景数 = 2^N / notes / disclaimer / 断言全 PASS）", p4_ok,
                    "；".join(p4_problems) if p4_problems
                    else f"情景 {len(scen)} 个（= 2^{len(div_codes)} = {expected_n}）、"
                         f"来源 {len(combos)} 个、notes {len(agg.get('notes', []))} 条、"
                         f"断言 {len(agg.get('assertions', []))} 条全 PASS")

        # 🔴 修（2026-11-09 独立审计实证）：**空情景集合会制造「空洞 PASS」** ——
        #    P5~P8/P10 都是「逐情景遍历 ⇒ 没找到违例 ⇒ PASS」的结构 ⇒ 情景为 0 条时
        #    它们会「零违例通过」，而 detail 还会谎报「全部情景手推全对」。
        #    （实测：上游生成器崩掉时聚合件为空 ⇒ P5 的 min() 直接抛 ValueError ⇒
        #    整份断言报告丢失、闸门只能报 rc=2，看不出真因。）
        #    ⇒ 修法：情景为空 ⇒ 这些断言**必须 FAIL**，并把「上游没产出」写进 detail。
        _scen_missing = ([] if scen else
                         ["聚合件里没有情景（上游生成器未产出 ⇒ 一条情景都没覆盖）"])

        # --- P5 现金非负 + feasible ---
        p5_problems = list(_scen_missing)
        for s in scen:
            if float(s.get("cash_end") or 0.0) < -0.005:
                p5_problems.append(f"{s.get('label')} 现金 {s.get('cash_end')}")
            if not s.get("feasible", True):
                p5_problems.append(f"{s.get('label')} 不可行")
        p5_ok = not p5_problems
        checker.add("P5", "全部情景期末现金均不为负且可行", p5_ok,
                    "；".join(p5_problems) if p5_problems
                    else f"最小期末现金 "
                         f"{min((float(s['cash_end']) for s in scen), default=0.0):.2f} 元"
                         f"（{len(scen)} 个情景）")

        # --- P6 整手 ---
        p6_problems = list(_scen_missing)
        for s in scen:
            for t in s.get("trades", []):
                if int(t.get("target_shares") or 0) % LOT != 0:
                    p6_problems.append(f"{s.get('label')}/{t.get('code')} {t.get('target_shares')}")
        p6_ok = not p6_problems
        checker.add("P6", f"所有落地股数都是整手（{LOT} 股）", p6_ok,
                    "；".join(p6_problems) if p6_problems
                    else f"共 {sum(len(s.get('trades', [])) for s in scen)} 条交易逐条校验通过")

        # --- P7 独立重算现金恒等式 ---
        p7_problems = list(_scen_missing)
        for s in scen:
            cash = float(s.get("cash_begin") or cash0)
            for t in s.get("trades", []):
                amt = float(t.get("est_amount") or 0.0)
                fee = float(t.get("est_fee") or 0.0)
                if t.get("delta_shares", 0) > 0:
                    cash -= abs(amt) + fee
                else:
                    cash += abs(amt) - fee
            if abs(cash - float(s.get("cash_end") or 0.0)) >= 0.01:
                p7_problems.append(f"{s.get('label')} 手推 {cash:.6f} vs 产物 {s.get('cash_end')}")
        p7_ok = not p7_problems
        checker.add("P7", "独立重算：逐情景现金恒等式与产物一致（容差 0.01 元）", p7_ok,
                    "；".join(p7_problems) if p7_problems
                    else f"{len(scen)} 个情景手推全对")

        # --- P8 破单席标记完整性（独立重算，**门槛感知**）---
        #: 🔴 口径（与 `reprice_node_day.py` 的 `slot_breach` 判据同源）：
        #:    **落地持仓金额**（落地股数 × 节点日价）> 单席上限 ⇔ 有 `slot_breach` 标记；
        #:    宇宙 = 聚合件的**落地持仓全集** `positions`（含「本轮不动」的仓位）
        #:    —— 用 `trades`（变动股数 ≠ 0）当宇宙 ⇒ 会漏掉「已经在仓、目标仍超单席上限」的席位
        #:    ⇒ 口径对不上（产品按全集判、探针按变动集判）⇒ 假红。
        #:    度量 = **自己算**（`target_shares × price`）—— **不信产品给的金额字段**
        #:    ⇒ 与 `make_decision_scenarios.py` 的破单席清单同口径。
        #: 🔴 2026-11-10 修（实测红灯）：**「positions 键缺失」与「positions 为空」是两回事**。
        #:    合法空仓 ⇒ `positions` 键**存在**但**是空数组** ⇒ 独立重算的宇宙是 ∅
        #:    ⇒ 破单席也必须为空（⇒ 照常校验）；**键缺失** = 产物结构不完整（⇒ FAIL）。
        #: 🔴 2026-11-13 修（最小调仓门槛上线后的**真红**）：**被门槛作废的行**单列 ——
        #:    产品把破单席判定算在**门槛作废之前**（`reprice_node_day.py` 第 408-438 行判、
        #:    第 488 行才作废）⇒ 该行的标记按「**门槛前**的目标股数」判定
        #:    ⇒ 本探针按**同一顺序**独立重算（闸门保持有牙），
        #:    并把「落地后已超单席上限、但产物未标记」的事实**如实披露**（生产缺口）。
        p8_problems = list(_scen_missing)
        _p8_stale: list[str] = []
        if scen:
            _p8_found, _p8_stale = _slot_breach_problems(scen, views, slot_limit)
            p8_problems.extend(_p8_found)
        p8_ok = not p8_problems
        _p8_detail = "；".join(p8_problems) if p8_problems else (
            f"{len(scen)} 个情景的破单席标记与独立重算全一致"
            f"（落地金额 > 单席上限 {slot_limit:.2f} 元 ⇔ 标记）")
        if _p8_stale:
            _p8_detail += ("｜🔴 口径分叉（破单席标记与门槛后落地不一致）："
                           + "；".join(_p8_stale))
        checker.add("P8", "独立重算：破单席标记完整（落地金额 > 单席上限 ⇔ 标记）", p8_ok,
                    _p8_detail)
        if _p8_stale:
            print("🔴 [口径分叉 · 破单席标记与门槛后落地不一致] " + "；".join(_p8_stale)
                  + "｜生产已于 2026-11-13 把破单席判定挪到 `apply_min_trade_threshold` "
                    "**之后**（按最终落地股数 × 节点日价）⇒ 这里不一致 ⇒ 闸门判红")

        # --- P9 幂等 ---
        p9_detail = ""
        p9_ok = False
        if rc_batch == 0:
            rc2, _ = run([sys.executable, str(BATCH), "--node-date", NODE_DATE,
                          "--out-dir", str(tmp_root / "agg2")], REPO_ROOT)
            if rc2 == 0:
                same = all(
                    sha1_of(tmp_root / "agg" / name) == sha1_of(tmp_root / "agg2" / name)
                    for name in (f"node_scenarios_{NODE_DATE.replace('-', '')}.json",
                                 f"node_scenarios_{NODE_DATE.replace('-', '')}.md")
                )
                p9_ok = same
                p9_detail = "两次运行 json / md sha1 逐字节相同" if same else "两次运行 sha1 不同"
            else:
                p9_detail = f"第二次运行退出码 {rc2}"
        else:
            p9_detail = f"第一次运行退出码 {rc_batch}"
        checker.add("P9", "幂等：聚合件两次独立运行逐字节相同", p9_ok, p9_detail)

        # --- P10 情景覆盖语义（选项 B 按 llm_final_decision 落地，**门槛感知**）---
        #: 🔴 2026-11-13 修（最小调仓门槛上线后的**真红**）：选项 B 的落地意图要**先过门槛** ——
        #:    意图调整金额 < 该档门槛 ⇒ 门槛作废（行保留、delta=0、金额=0）⇒ 产物**不调仓是对的**
        #:    ⇒ 旧口径（直接要求「必须有这笔交易」）把「门槛作废」判成「应建仓但没建」⇒ **假红**。
        #:    实测：情景 `512800=B` 意图 5,400 股（卖 2,300 股 = 2,010.20 元）
        #:    < 存量微调门槛 2,800 元 ⇒ 门槛作废 ⇒ 产物不建仓是对的。
        p10_problems = list(_scen_missing)
        llm_targets = _llm_targets_map(plan)
        if scen:
            p10_problems.extend(_p10_problems(scen, views, llm_targets))
        p10_ok = not p10_problems
        _p10_blocked = sorted({code for _sig, _view in views.items()
                                for code, _j in _view.items() if _j.get("blocked")})
        checker.add("P10", "情景覆盖语义：选项 B 的落地股数/权重与 llm_final_decision 一致"
                    "（门槛作废行按门槛语义：不调仓 + 披露对账）", p10_ok,
                    "；".join(p10_problems) if p10_problems
                    else "B 选项逐项按 lots / target_pct 落地"
                         f"（门槛作废行 {'/'.join(_p10_blocked) or '无'}：不调仓是符合门槛语义的）")

        # --- P11 最小调仓门槛披露对账（独立重算 ⇔ 产物披露）---
        #: 🔴 「被门槛作废的行」必须**如实披露**（`min_amount_gate.skipped`）且
        #:    **逐条对账**（code / kind / amount / threshold 一致）+ 该行 **delta=0、金额=0**
        #:    —— 否则「门槛悄悄把一笔调仓吃掉」谁也看不出来。
        p11_problems = list(_scen_missing)
        _p11_detail: list[str] = []
        for s in scen:
            _sig = _signature(s.get("choices") or {})
            _payload = scen_payloads.get(_sig)
            if _payload is None:
                p11_problems.append(f"{s.get('label')} 缺该情景的节点日产物 ⇒ 无法对账门槛披露")
                continue
            _view = views.get(_sig) or {}
            _thr = eff_thresholds.get(_sig) or gate_mod.resolve_thresholds()
            for _prob in _gate_disclosure_problems(_payload, s, _view, _thr,
                                                    fallbacks.get(_sig)):
                p11_problems.append(f"{s.get('label')}/{_prob}")
            _blocked = [f"{c}({j['kind']} {j['amount']:.2f}<{j['threshold']:.0f})"
                        for c, j in sorted(_view.items()) if j.get("blocked")]
            _fb = fallbacks.get(_sig) or {}
            _p11_detail.append(
                f"{s.get('label')}：生效门槛 新开仓 {_thr.get('新开仓')}（兜底触发="
                f"{_fb.get('triggered')}）｜作废 {'/'.join(_blocked) or '无'}")
        p11_ok = not p11_problems
        checker.add("P11", "最小调仓门槛披露对账（独立重算 ⇔ 产物 skipped：code/kind/amount/threshold"
                    " + 作废行 delta=0 / 金额=0 / 目标股数回退）", p11_ok,
                    "；".join(p11_problems) if p11_problems else "；".join(_p11_detail))

        # --- D1 非法情景字母 ---
        # 🔴 负例必须用「当前产物里的**真实分歧码** + 非法字母」：
        #    写死的旧代码若已不在分歧表里，报错原因会从「选项字母非法」变成「代码不在计划件」，
        #    负例就名不副实（测的东西换了）⇒ 取当前产物里的第一个分歧码。
        d1_code = div_codes[0] if div_codes else ""
        d1_ok = False
        d1_detail = "推不出分歧码集合 ⇒ 负例无法构造"
        if d1_code:
            rc_d1, out_d1 = run([sys.executable, str(REPRICE), "--node-date", NODE_DATE,
                                 "--out-dir", str(tmp_root / "d1"), "--scenario", f"{d1_code}=X"],
                                REPO_ROOT)
            d1_ok = rc_d1 != 0 and ("X" in out_d1 and "非法" in out_d1)
            d1_detail = (f"退出码 {rc_d1}（规格 {d1_code}=X）" if d1_ok
                         else f"退出码 {rc_d1}，输出：{out_d1.strip()[-200:]}")
        checker.add("D1", "真负例：情景字母非法（X）⇒ 退出码 ≠ 0 且中文报错", d1_ok, d1_detail)

        # --- D2 未知代码 ---
        rc_d2, out_d2 = run([sys.executable, str(REPRICE), "--node-date", NODE_DATE,
                             "--out-dir", str(tmp_root / "d2"), "--scenario", "999999=B"],
                            REPO_ROOT)
        d2_ok = rc_d2 != 0 and "999999" in out_d2 and "不在计划件" in out_d2
        checker.add("D2", "真负例：情景代码不在计划件里 ⇒ 退出码 ≠ 0 且中文报错", d2_ok,
                    f"退出码 {rc_d2}" if d2_ok else f"退出码 {rc_d2}，输出：{out_d2.strip()[-200:]}")

        # --- D3 篡改聚合件现金 ⇒ 独立重算必须 FAIL ---
        d3_ok = False
        d3_detail = "负例未能触发"
        if agg:
            mutated = copy.deepcopy(agg)
            for s in mutated.get("scenarios", []):
                s["cash_end"] = float(s.get("cash_end") or 0.0) + 100.0
                break
            detected = False
            for s in mutated.get("scenarios", []):
                cash = float(s.get("cash_begin") or cash0)
                for t in s.get("trades", []):
                    amt = float(t.get("est_amount") or 0.0)
                    fee = float(t.get("est_fee") or 0.0)
                    if t.get("delta_shares", 0) > 0:
                        cash -= abs(amt) + fee
                    else:
                        cash += abs(amt) - fee
                if abs(cash - float(s.get("cash_end") or 0.0)) >= 0.01:
                    detected = True
                    break
            d3_ok = detected
            d3_detail = ("改坏后独立重算检出偏差 ⇒ 闸门会 FAIL" if detected
                         else "改坏后独立重算**未**检出 ⇒ 闸门无牙")
        checker.add("D3", "真负例：篡改聚合件 cash_end ⇒ 独立重算必须 FAIL（证明闸门有牙）",
                    d3_ok, d3_detail)

        # --- D4 空仓情景（positions = []）里塞一条破单席 ⇒ P8 必须 FAIL（证明闸门有牙）---
        #: 🔴 2026-11-10：P8 改成「positions 键缺失 ⇒ FAIL；键存在但为空 ⇒ 照常校验」之后，
        #:    **必须**补一条负例钉死「空仓情景不许白过」——
        #:    给一个**空 positions** 的情景硬塞一条 `slot_breaches` ⇒ 独立重算（宇宙 = ∅）
        #:    必然与产物对不上 ⇒ 探针必须 FAIL。
        d4_ok, d4_detail = _d4_empty_position_negative(agg, views, slot_limit)
        checker.add("D4",
                    "真负例：空仓情景（positions 为空）里塞破单席 ⇒ 独立重算必须 FAIL（证明闸门有牙）",
                    d4_ok, d4_detail)

        # --- D5 伪造「被门槛作废」的行建仓 ⇒ 门槛对账必须 FAIL ---
        d5_ok, d5_detail = _d5_fake_open_negative(scen, scen_payloads, views, _total_assets,
                                                  eff_thresholds)
        checker.add("D5", "真负例：把「被门槛作废」的行改成 delta≠0（伪造建仓）"
                    " ⇒ 门槛对账必须 FAIL（证明闸门有牙）", d5_ok, d5_detail)

        # --- D6 门槛披露漏记 ⇒ 门槛对账必须 FAIL ---
        d6_ok, d6_detail = _d6_missing_disclosure_negative(scen, scen_payloads, views,
                                                            _total_assets, eff_thresholds)
        checker.add("D6", "真负例：把门槛披露里某条 skipped 记录删掉"
                    " ⇒ 门槛对账必须 FAIL（证明闸门有牙）", d6_ok, d6_detail)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        shutil.rmtree(payload_dir, ignore_errors=True)

    failed = [it for it in checker.items if not it[2]]
    for check_id, name, ok, detail in checker.items:
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", check_id, name)
              + (("｜" + detail) if detail else ""))
    print("合计 %d 项：PASS %d / FAIL %d" % (len(checker.items), len(checker.items) - len(failed),
                                             len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 闸门无法完成：{exc}", file=sys.stderr)
        raise SystemExit(2)
