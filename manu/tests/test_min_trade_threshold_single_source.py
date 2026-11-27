"""🔴 2026-11-26 真机 P11 红灯修：**最小调仓门槛「生效阈值」的单一事实源**闸门钉子。

【真机病根（证据：`<RESULTS_ROOT>/_gates_full_20261126.log` · 探针 `probe_node_scenarios`）】
产物披露的「新开仓」门槛 = **1,500 元**（小资金兜底已触发），闸门**独立重算**用的却是
**名义档 3,800 元** ⇒ **口径分叉** ⇒ 三条同时转红：
  * `P11` 误判「515450 新开仓 1,590.60 元本该被门槛作废」（实际 1,590.60 ≥ 1,500 ⇒ **不该**作废）；
  * `D5` / `D6` 两条真负例因「夹具本身就不自洽」⇒ **失去证明力**。

【根因】门槛有两档**语义不同**的量（`rules.md §十四.123` ④）：
  * **名义档** = `config.MIN_TRADE_AMOUNT_NEW` = 3,800 元；
  * **本轮生效档** = 兜底触发时
    `min(名义档, config.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT)` = 1,500 元。
「名义档 → 生效档」的换算原先散在生产侧各自内联 ⇒ 闸门按名义档独立重算 ⇒ 必然分叉。

【本文件钉死的行为】
  ① `min_trade_gate.resolve_effective_thresholds` 是**唯一**的生效门槛解析口：
     兜底未触发 ⇒ 名义档；兜底触发 ⇒ 新开仓 1,500（**只降不升**）；清仓/存量微调两档不动；
  ② 生效门槛的**期望值锚到事实源常量**（`config.py`）⇒ 产物自述只用来「跟事实源比」；
  ③ 探针的独立复算**不读被检对象自述** ⇒ 「自述 ＋ 产物字段」两处同改 ⇒ 仍必须转红；
  ④ 变异反证（守卫必须证明有牙）：
     M1 把生效阈值改回 3,800（回到分叉态）⇒ 对账必须转红；
     M2 把「被门槛作废」的行改成 `delta ≠ 0` ⇒ 对账必须转红（D5）；
     M3 删掉一条 `skipped` 披露 ⇒ 对账必须转红（D6）；
     M4「产物自述 ＋ 产物字段」同改（自洽篡改）⇒ 对账必须**仍**转红。

【夹具】全部**自造**（纯 dict，数字取自 2026-11-26 真机 canonical 根口径：
计划件 asof 2026-11-03 / 节点日 2026-11-04）⇒ **不依赖服务器路径、不读 canonical 产物**。

【镜像纪律】本文件有**两个镜像位置**（`manu/etf_rotation/tests/` 与 `manu/tests/`）
⇒ 两者必须**逐字节一致**。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

#: 🔴 **工程根**（向上找「唯一权威位置」的探针）⇒ 本文件在**两个镜像位置**
#:    （`manu/etf_rotation/tests/` 与 `manu/tests/`）都能跑，且**不依赖 cwd**。
_REPO_ROOT = Path(__file__).resolve().parents[-1]
for _parent in Path(__file__).resolve().parents:
    if (_parent / "manu" / "etf_rotation" / "probes" / "probe_node_scenarios.py").is_file():
        _REPO_ROOT = _parent
        break
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from manu.etf_rotation.probes import probe_node_scenarios as P  # type: ignore  # noqa: E402

#: 🔴 门槛模块 / 配置模块**一律走探针那一份**（`P.gate_mod` / `P.C`）
#:    ⇒ 不会出现「两个 `config` 模块实例」（镜像位置的 `sys.path` 顺序会变）
#:    ⇒ 期望值与生产**同源**。
gate_mod = P.gate_mod
C = P.C

# =============================================================================
# 夹具：数字取自 2026-11-26 真机 canonical 根（计划件 asof 2026-11-03 / 节点日 2026-11-04）
# =============================================================================
TOTAL_ASSETS = 29854.67
CASH = 15518.87
EQUITY_CAP = 0.16            # `ta_screening.equity_cap_effective`
PRICE_512800 = 0.874         # 节点日价
PRICE_515450 = 1.446         # 节点日价
TARGET_WEIGHT = 0.05333333333333332
#: 兜底触发的那个场景（`512800=A,515450=A`）：均分 3,184.50 元 < 3,800 ⇒ 生效档 1,500
ALLOWANCE_AA = 3184.5
#: 兜底**不**触发的那个场景（`512800=B,515450=A`）：额度被 512800 吃光 ⇒ 名义档 3,800
ALLOWANCE_BA = 0.0


def _plan() -> dict:
    """canonical 根计划件（**只留影响门槛的两行**；其余行的 `target_amount` 皆为 0）。"""
    return {
        "meta": {"total_assets": TOTAL_ASSETS, "cash": CASH, "asof": "2026-11-03"},
        "actions": [
            {"code": "512800", "name": "银行ETF华宝", "current_shares": 7700,
             "target_shares": 1800, "target_weight": TARGET_WEIGHT,
             "target_amount": 1592.2490666666663, "final_score": 1.1641683876804434,
             "price": 0.88, "ta_rejected": False, "held_not_touched": False},
            {"code": "515450", "name": "红利低波50ETF南方", "current_shares": 0,
             "target_shares": 0, "target_weight": TARGET_WEIGHT,
             "target_amount": 0.0, "final_score": 0.8671706873055339,
             "price": 1.466, "ta_rejected": False, "held_not_touched": False},
        ],
        "llm_final_decision": {"targets": {"512800": {"target_pct": 16.0, "lots": None}}},
        "ta_screening": {"equity_cap": 0.8, "equity_cap_effective": EQUITY_CAP},
    }


def _payload_aa() -> dict:
    """情景 `512800=A,515450=A`：兜底**触发** ⇒ 生效门槛 1,500 ⇒ **无**作废行。"""
    return {
        "meta": {"total_assets": TOTAL_ASSETS},
        "min_amount_gate": {
            "enabled": True,
            "thresholds": {"清仓止损": 0.0, "新开仓": 1500.0, "存量微调": 2800.0},
            "considered_actions": 2, "skipped_count": 0, "skipped": [],
            "new_open_total": 1, "new_open_skipped": 0, "deadlock_risk": False,
        },
        "small_account_fallback": {
            "enabled": True, "triggered": True,
            "reason": "额度不足均分跨过门槛（均分 3184.50 元 < 常规门槛 3800 元）",
            "normal_new_limit": 3800.0, "relaxed_new_limit": 1500.0,
            "equity_cap": EQUITY_CAP, "total_assets": TOTAL_ASSETS,
            "allowance_amount": ALLOWANCE_AA, "per_candidate_amount": ALLOWANCE_AA,
            "concentrated_amount": 0.0, "candidate_count": 1, "preferred_code": "",
            "concentrated": False, "selected": None, "others": [],
        },
        "actions": [
            {"code": "512800", "name": "银行ETF华宝", "action": "卖出",
             "current_shares": 7700, "target_shares": 1800, "delta_shares": -5900,
             "est_amount": -5156.6, "est_fee": 3.35, "price": PRICE_512800,
             "target_weight": TARGET_WEIGHT, "target_weight_effective": TARGET_WEIGHT,
             "weight_to_cash": 0.0},
            {"code": "515450", "name": "红利低波50ETF南方", "action": "买入",
             "current_shares": 0, "target_shares": 1100, "delta_shares": 1100,
             "est_amount": 1590.6, "est_fee": 1.03, "price": PRICE_515450,
             "target_weight": TARGET_WEIGHT, "target_weight_effective": TARGET_WEIGHT,
             "weight_to_cash": 0.0},
        ],
    }


def _payload_ba() -> dict:
    """情景 `512800=B,515450=A`：兜底**不**触发 ⇒ 生效门槛 3,800 ⇒ **两条**作废行。"""
    return {
        "meta": {"total_assets": TOTAL_ASSETS},
        "min_amount_gate": {
            "enabled": True,
            "thresholds": {"清仓止损": 0.0, "新开仓": 3800.0, "存量微调": 2800.0},
            "considered_actions": 2, "skipped_count": 2,
            "skipped": [
                {"code": "512800", "name": "银行ETF华宝", "kind": "存量微调",
                 "amount": 2010.2, "threshold": 2800.0,
                 "min_commission_saved": 5.0, "effective_bp": 24.9},
                {"code": "515450", "name": "红利低波50ETF南方", "kind": "新开仓",
                 "amount": 1590.6, "threshold": 3800.0,
                 "min_commission_saved": 5.0, "effective_bp": 31.4},
            ],
            "new_open_total": 1, "new_open_skipped": 1, "deadlock_risk": True,
        },
        "small_account_fallback": {
            "enabled": True, "triggered": False,
            "reason": "剩余额度 0.00 元 < 放宽下限 1500 元 ⇒ 不触发",
            "normal_new_limit": 3800.0, "relaxed_new_limit": 1500.0,
            "equity_cap": EQUITY_CAP, "total_assets": TOTAL_ASSETS,
            "allowance_amount": ALLOWANCE_BA, "per_candidate_amount": ALLOWANCE_BA,
            "concentrated_amount": 0.0, "candidate_count": 1, "preferred_code": "",
            "concentrated": False, "selected": None, "others": [],
        },
        "actions": [
            {"code": "512800", "name": "银行ETF华宝", "action": "持有",
             "current_shares": 7700, "target_shares": 7700, "delta_shares": 0,
             "est_amount": 0.0, "est_fee": 0.0, "price": PRICE_512800,
             "target_weight": TARGET_WEIGHT,
             "target_weight_effective": 7700 * PRICE_512800 / TOTAL_ASSETS,
             "weight_to_cash": 0.0,
             "min_amount_skipped": True, "min_amount_kind": "存量微调",
             "min_amount_threshold": 2800.0, "min_amount_skipped_amount": 2010.2},
            {"code": "515450", "name": "红利低波50ETF南方", "action": "不建仓",
             "current_shares": 0, "target_shares": 0, "delta_shares": 0,
             "est_amount": 0.0, "est_fee": 0.0, "price": PRICE_515450,
             "target_weight": TARGET_WEIGHT, "target_weight_effective": 0.0,
             "weight_to_cash": TARGET_WEIGHT,
             "min_amount_skipped": True, "min_amount_kind": "新开仓",
             "min_amount_threshold": 3800.0, "min_amount_skipped_amount": 1590.6},
        ],
    }


def _resolved(plan, choices, payload):
    """探针的**独立复算**结果 ⇒ `(意图, 生效门槛, 兜底披露, 门槛判定视图)`。"""
    intent = P._scenario_intent(plan, choices, payload, TOTAL_ASSETS)
    thr, fb = P._scenario_effective_thresholds(plan, intent, TOTAL_ASSETS)
    return intent, thr, fb, P._gate_view_from_intent(intent, thr)


# =============================================================================
# ① 生效门槛解析口（单一事实源）
# =============================================================================
def test_effective_thresholds_fallback_not_triggered_keeps_nominal() -> None:
    """兜底**未**触发 ⇒ 生效门槛 = 名义档（三档都不动）。"""
    thr = gate_mod.resolve_effective_thresholds(None, fallback={"triggered": False})
    assert thr == {"清仓止损": 0.0, "新开仓": 3800.0, "存量微调": 2800.0}
    assert thr == gate_mod.resolve_thresholds()


def test_effective_thresholds_fallback_triggered_relaxes_new_open_only() -> None:
    """兜底**触发** ⇒ 只放宽「新开仓」一档（3,800 → 1,500），另两档不动。"""
    thr = gate_mod.resolve_effective_thresholds(None, fallback={"triggered": True})
    assert thr["新开仓"] == 1500.0
    assert thr["清仓止损"] == 0.0
    assert thr["存量微调"] == 2800.0
    assert thr["新开仓"] == min(C.MIN_TRADE_AMOUNT_NEW, C.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT)


def test_effective_thresholds_only_never_raises() -> None:
    """🔴 **只降不升**：名义档本就低于 1,500 时 ⇒ 生效门槛**不得**被抬到 1,500。"""
    thr = gate_mod.resolve_effective_thresholds(
        {"新开仓": 1200.0}, fallback={"triggered": True})
    assert thr["新开仓"] == 1200.0


def test_effective_thresholds_anchored_to_fact_source_not_self_report() -> None:
    """🔴 期望值**锚到事实源常量**（§十四.123 ①）⇒ 产物自述被改坏也不影响期望值。"""
    #: 产物自述把「放宽档」伪造成 1,200 元 ⇒ 期望值**不许**跟着走
    thr = gate_mod.resolve_effective_thresholds(
        None, fallback={"triggered": True, "relaxed_new_limit": 1200.0})
    assert thr["新开仓"] == float(C.MIN_TRADE_AMOUNT_NEW_SMALL_ACCOUNT) == 1500.0


# =============================================================================
# ② 探针的独立复算（🔴 不读被检对象自述）
# =============================================================================
def test_independent_fallback_triggers_and_relaxes_to_1500() -> None:
    """场景 `A,A`：探针独立复算 ⇒ 兜底触发 ⇒ 生效门槛 1,500（**不读产物自述**）。"""
    plan, payload = _plan(), _payload_aa()
    _intent, thr, fb, view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    assert fb["triggered"] is True
    assert fb["candidate_count"] == 1
    assert abs(float(fb["allowance_amount"]) - ALLOWANCE_AA) <= 0.05
    assert thr["新开仓"] == 1500.0
    #: 1,590.60 ≥ 1,500 ⇒ **不该**被作废（真机 P11 误判的那一行）
    assert view["515450"]["blocked"] is False
    assert view["512800"]["blocked"] is False


def test_independent_fallback_not_triggered_keeps_3800() -> None:
    """场景 `B,A`：额度被 512800 吃光 ⇒ 兜底不触发 ⇒ 生效门槛回到 3,800。"""
    plan, payload = _plan(), _payload_ba()
    _intent, thr, fb, view = _resolved(plan, {"512800": "B", "515450": "A"}, payload)
    assert fb["triggered"] is False
    assert thr["新开仓"] == 3800.0
    assert view["512800"]["blocked"] is True
    assert view["515450"]["blocked"] is True


def test_independent_recompute_ignores_product_self_report() -> None:
    """🔴 §十四.123 ②：产物自述被改成「未触发」⇒ 探针的独立复算**不受影响**。"""
    plan, payload = _plan(), _payload_aa()
    payload["small_account_fallback"]["triggered"] = False
    _intent, thr, fb, _view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    assert fb["triggered"] is True, "独立复算被产物自述带偏 ⇒ 期望值取自被检对象自述"
    assert thr["新开仓"] == 1500.0


# =============================================================================
# ③ 门槛披露对账（探针独立重算 ⇔ 产物披露）
# =============================================================================
def test_gate_disclosure_is_clean_on_effective_threshold() -> None:
    """生效门槛统一后 ⇒ 真实产物**零违例**（真机 P11 三条假断言全部消失）。"""
    plan, payload = _plan(), _payload_aa()
    _intent, thr, fb, view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    problems = P._gate_disclosure_problems(payload, {"trades": []}, view, thr, fb)
    assert not problems, "生效门槛下仍报红 ⇒ 口径没统一：" + "；".join(problems)


def test_gate_disclosure_is_clean_for_blocked_scenario() -> None:
    """有**作废行**的场景（`B,A`）⇒ 对账同样零违例（作废行 delta/金额/股数都要对）。"""
    plan, payload = _plan(), _payload_ba()
    _intent, thr, fb, view = _resolved(plan, {"512800": "B", "515450": "A"}, payload)
    problems = P._gate_disclosure_problems(payload, {"trades": []}, view, thr, fb)
    assert not problems, "有作废行的场景反而报红 ⇒ 对账逻辑坏了：" + "；".join(problems)


# =============================================================================
# ④ 变异反证（守卫必须证明有牙）
# =============================================================================
def test_m1_revert_to_3800_threshold_must_turn_red() -> None:
    """M1：把生效阈值改回 3,800（回到真机的分叉态）⇒ 对账必须转红。"""
    plan, payload = _plan(), _payload_aa()
    intent, _thr, fb, _view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    forked = gate_mod.resolve_thresholds()          # ← 回到分叉态（名义档 3,800）
    #: 🔴 真实的分叉态 = 闸门**整条独立重算**都用 3,800（不只是对账时换口径）
    forked_view = P._gate_view_from_intent(intent, forked)
    problems = P._gate_disclosure_problems(payload, {"trades": []}, forked_view, forked, fb)
    assert problems, "回到 3,800 的分叉态却没人报错 ⇒ 守卫没牙"
    joined = "；".join(problems)
    assert "门槛阈值 新开仓" in joined, joined
    assert "515450" in joined, joined


def test_m2_blocked_row_with_nonzero_delta_must_turn_red() -> None:
    """M2（= D5）：把「被门槛作废」的行改成 `delta ≠ 0` ⇒ 对账必须转红。"""
    plan, payload = _plan(), _payload_ba()
    _intent, thr, fb, view = _resolved(plan, {"512800": "B", "515450": "A"}, payload)
    mutated = {"label": "合成夹具（门槛作废）", "choices": {}, "positions": [], "trades": [
        {"code": "512800", "name": "伪造建仓", "action": "买入", "current_shares": 7700,
         "target_shares": 7800, "delta_shares": 100, "price": PRICE_512800,
         "est_amount": 87.4, "est_fee": 0.0, "target_weight_pct": 0.0,
         "slot_breach": False},
    ]}
    problems = P._gate_disclosure_problems(payload, mutated, view, thr, fb)
    assert problems, "伪造建仓却没人报错 ⇒ 守卫没牙"
    assert any("512800" in p for p in problems), "；".join(problems)


def test_m3_drop_one_skipped_disclosure_must_turn_red() -> None:
    """M3（= D6）：删掉一条 `skipped` 披露 ⇒ 对账必须转红。"""
    plan, payload = _plan(), _payload_ba()
    _intent, thr, fb, view = _resolved(plan, {"512800": "B", "515450": "A"}, payload)
    mutated = copy.deepcopy(payload)
    gate = mutated["min_amount_gate"]
    gate["skipped"] = [rec for rec in gate["skipped"] if rec["code"] != "512800"]
    problems = P._gate_disclosure_problems(mutated, {"trades": []}, view, thr, fb)
    assert problems, "漏记披露却没人报错 ⇒ 守卫没牙"
    assert any("512800" in p for p in problems), "；".join(problems)


def test_m4_self_consistent_tamper_must_still_turn_red() -> None:
    """M4（§十四.123 ②）：「产物自述 ＋ 产物字段」**两处同改**（自洽）⇒ 仍必须转红。"""
    plan, payload = _plan(), _payload_aa()
    _intent, thr, fb, view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    mutated = copy.deepcopy(payload)
    #: 两处同改：自述说「未触发」＋ 门槛披露改成 3,800 ⇒ 彼此自洽
    mutated["small_account_fallback"]["triggered"] = False
    mutated["min_amount_gate"]["thresholds"]["新开仓"] = 3800.0
    problems = P._gate_disclosure_problems(mutated, {"trades": []}, view, thr, fb)
    assert problems, "自洽篡改却没人报错 ⇒ 期望值取自被检对象自述 ⇒ 守卫失灵"
    joined = "；".join(problems)
    assert "门槛阈值 新开仓" in joined, joined


def test_m5_relaxed_limit_self_report_tamper_must_turn_red() -> None:
    """M5（§十四.123 ③）：只改「当前实现里没被断言用到」的字段（放宽档）⇒ 必须转红。"""
    plan, payload = _plan(), _payload_aa()
    _intent, thr, fb, view = _resolved(plan, {"512800": "A", "515450": "A"}, payload)
    mutated = copy.deepcopy(payload)
    mutated["small_account_fallback"]["relaxed_new_limit"] = 1200.0
    problems = P._gate_disclosure_problems(mutated, {"trades": []}, view, thr, fb)
    assert problems, "篡改兜底自述的放宽档却没人报错 ⇒ 断言覆盖不全 ⇒ 静默放过"
    assert any("放宽档" in p for p in problems), "；".join(problems)


# =============================================================================
# ⑤ 真负例 D5 / D6 的「本轮没有作废行 ⇒ 自造夹具」路径
# =============================================================================
def test_d5_self_synthesized_fixture_must_detect() -> None:
    """D5：本轮产物**没有**作废行 ⇒ 负例**自造**夹具 ⇒ 仍必须真跑出 FAIL。"""
    ok, detail = P._d5_fake_open_negative([], {}, {}, TOTAL_ASSETS, {})
    assert ok, "自造夹具路径失守 ⇒ 负例空过：" + detail
    assert "合成夹具" in detail, detail


def test_d6_self_synthesized_fixture_must_detect() -> None:
    """D6：本轮产物**没有**作废行 ⇒ 负例**自造**夹具 ⇒ 仍必须真跑出 FAIL。"""
    ok, detail = P._d6_missing_disclosure_negative([], {}, {}, TOTAL_ASSETS, {})
    assert ok, "自造夹具路径失守 ⇒ 负例空过：" + detail
    assert "合成夹具" in detail, detail


def test_d5_d6_on_real_blocked_scenario_must_detect() -> None:
    """D5 / D6 在**真实**有作废行的场景上 ⇒ 负例必须真跑出 FAIL（证明力来源）。"""
    plan, payload = _plan(), _payload_ba()
    _intent, thr, fb, view = _resolved(plan, {"512800": "B", "515450": "A"}, payload)
    scen = [{"label": "512800=B,515450=A", "choices": {"512800": "B", "515450": "A"},
             "positions": [], "trades": [], "slot_breaches": []}]
    sig = P._signature(scen[0]["choices"])
    ok5, detail5 = P._d5_fake_open_negative(scen, {sig: payload}, {sig: view},
                                            TOTAL_ASSETS, {sig: thr})
    ok6, detail6 = P._d6_missing_disclosure_negative(scen, {sig: payload}, {sig: view},
                                                      TOTAL_ASSETS, {sig: thr})
    assert ok5, "真实作废行上 D5 失守：" + detail5
    assert ok6, "真实作废行上 D6 失守：" + detail6
    assert "真实产物情景" in detail5, detail5
