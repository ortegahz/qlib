"""🔴 2026-11-13 新增：「**探针真空假红**」四处的**成对负例**（修完仍然有牙）。

【事故来源】2026-11-13 全量跑批的闸门红了 8 项，其中 **6 项是「探针真空假红」**：
本轮 LLM 决策与规则层**在 ±2pp 内一致** ⇒ 「冲突表」（规则层 vs LLM 的 >2pp 分歧集）
**合法为空（0 行）**；但有几条断言**预设了「冲突表必非空」** ⇒ 空集 ⇒ 假红。

【本文件钉住的四件事（每件都有「变异 ⇒ 必须转红」的成对负例）】
  ① `probe_llm_parser.py` 的 **I4**：冲突表**为空**时必须**合法**
     （重放 LLM 原文有 >2pp 分歧却没进表 ⇒ FAIL；无分歧 ⇒ 空表合法）；
     变异：让解析器**漏读**一条真分歧 ⇒ I4 必须转红。
  ② `probe_verify_guards.py` 的 **P0b**：冲突表为空 ⇒ **N/A**（不适用 ≠ 通过），
     不是 FAIL；有冲突行却缺 `llm_current_pct` ⇒ 仍必须 FAIL（有牙）。
  ③ `probe_verify_guards.py` 的 **N1/N2/N4/N5**：负例**自建夹具**
     （非空冲突表 + 计划件 + LLM 原文）⇒ 不依赖本轮真实产物；
     变异：把变异打回**真实（空）冲突表**上 ⇒ 变异空转 ⇒ 负例必须转红。
  ④ `probe_node_scenarios.py` 的 **P3**：**按原始浮点值比较**（容差只放浮点表示误差）
     ⇒ 展示用舍入值（聚合件的 4 位小数百分数）**不是**判据；
     变异：注入 0.001 权重的真实差异 ⇒ P3 必须转红（不许把容差放大到掩盖它）。

【为什么用合成夹具】本机没有本轮真实产物（落点根在服务器上），
  且断言的**口径**与真实产物无关 ⇒ 自带夹具更稳、也更快。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

#: `manu/etf_rotation/probes/`（探针模块自己会把 `manu/etf_rotation/` 插进 sys.path）
_PROBES_DIR = pathlib.Path(__file__).resolve().parents[1] / "probes"
if str(_PROBES_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBES_DIR))

import probe_llm_parser as LP  # noqa: E402  （探针本体：①）
import probe_verify_guards as VG  # noqa: E402  （探针本体：②③）
import probe_node_scenarios as NS  # noqa: E402  （探针本体：④）

#: 合成 LLM 原文（**非真实产物**）：一行「卖出 0%」⇒ 与规则层 20% 差 20pp（>2pp）
_DECISION_MD = """# 合成决策（探针夹具 · 非真实产物）

## 研究经理

| 标的 | 当前权重 | 方向 | 目标权重 | 理由 |
|:--|--:|:--|--:|:--|
| 512800 | 20.00% | **卖出** | 0% | 夹具行：规则层 20%，LLM 清仓 |
"""


def _write_decision(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "tradingagents_decision.md"
    path.write_text(_DECISION_MD, encoding="utf-8")
    return path


def _plan(decision_md: pathlib.Path, rule_weight: float,
          conflicts: list | None = None, llm_view: str = "合成夹具") -> dict:
    """合成计划件：规则层权重 `rule_weight`（小数）+ 冲突表 + LLM 层标记。"""
    return {
        "meta": {
            "total_assets": 100000.0,
            "source_decision": str(decision_md),
            "account_current_weights_pct": {"512800": 20.0},
        },
        "actions": [{"code": "512800", "name": "夹具ETF", "target_weight": rule_weight}],
        "llm_targets": {
            "512800": {"direction": "卖出", "current_pct": 20.0, "target_pct": 0.0,
                       "who": "research", "reason": "夹具"},
        },
        "llm_conflicts": conflicts or [],
        "tradingagents_view": llm_view,
    }


# =============================================================================
# ① `probe_llm_parser.py` 的 I4：冲突表**为空**时必须**合法**
# =============================================================================
def test_i4_empty_conflict_table_is_legal_when_replay_finds_no_divergence(tmp_path) -> None:
    """① 正例：规则层与 LLM **一致**（差 0pp）⇒ 冲突表为空**合法** ⇒ I4 PASS。"""
    md = _write_decision(tmp_path)
    plan = _plan(md, rule_weight=0.0)  # 规则层 0% = LLM 的 0% ⇒ 无 >2pp 分歧
    status, detail = LP._conflict_table_emptiness(plan, md)
    assert status == "pass", f"合法的空冲突表被判红（假红）：{detail}"


def test_i4_mutation_missing_divergence_turns_red(tmp_path) -> None:
    """① **成对负例**：解析器**漏读**一条真分歧（规则 20% vs LLM 0%）⇒ I4 必须转红。

    ⇒ 这就是「解析器坏了」的真现场：冲突表空、但重放原文**有** >2pp 分歧。
    """
    md = _write_decision(tmp_path)
    plan = _plan(md, rule_weight=0.2)  # 规则层 20% vs LLM 0% ⇒ 差 20pp > 2pp
    status, detail = LP._conflict_table_emptiness(plan, md)
    assert status == "fail", f"解析器漏读分歧却没被判红（I4 失去牙齿）：{detail}"


def test_i4_mutation_revert_to_must_be_nonempty_turns_red(tmp_path) -> None:
    """① **变异反证（回退修复）**：旧口径「冲突表**不得为空**」在合法空表上**必红**。

    ⇒ 证明本用例盖住的正是 2026-11-13 的假红现场；
       若旧口径判 PASS，说明现场根本没被覆盖（本测试失去意义）。
    """
    md = _write_decision(tmp_path)
    plan = _plan(md, rule_weight=0.0)
    status, _ = LP._conflict_table_emptiness(plan, md)
    assert status == "pass", "新口径：合法空表必须 PASS"
    old_status = "pass" if (plan.get("llm_conflicts") or []) else "fail"
    assert old_status == "fail", "旧口径（冲突表不得为空）没把合法空表判红 ⇒ 假红现场没被覆盖"


def test_i4_non_empty_conflict_table_passes(tmp_path) -> None:
    """① 冲突表非空 ⇒ 直接 PASS（不需要重放判定）。"""
    md = _write_decision(tmp_path)
    row = {"code": "512800", "rule_target_pct": 20.0, "llm_target_pct": 0.0,
           "llm_direction": "卖出", "llm_who": "research", "llm_current_pct": 20.0}
    status, detail = LP._conflict_table_emptiness(_plan(md, 0.2, [row]), md)
    assert status == "pass", f"非空冲突表被判红：{detail}"


def test_i4_no_llm_layer_is_na_not_pass(tmp_path) -> None:
    """① 本轮**没有 LLM 意见**（--skip-ta / LLM 未完成）⇒ **N/A**（不适用 ≠ 通过）。"""
    md = _write_decision(tmp_path)
    plan = _plan(md, rule_weight=0.0, llm_view="")
    plan["meta"]["source_decision"] = ""
    status, detail = LP._conflict_table_emptiness(plan, md)
    assert status == "na", f"没有 LLM 意见时必须报 N/A（不适用 ≠ 通过）：{detail}"


# =============================================================================
# ② `probe_verify_guards.py` 的 P0b：冲突表为空 ⇒ **N/A**（不是 FAIL）
# =============================================================================
def test_p0b_empty_conflicts_is_na_not_fail() -> None:
    """② 正例：冲突表为空 ⇒ **本前置不适用**（N/A）⇒ 不得判 FAIL、也不得算 PASS。"""
    verdict = VG._p0b_verdict([])
    assert verdict["status"] == "na", f"空冲突表必须报 N/A，实际：{verdict}"
    assert verdict["status"] != "fail", "空冲突表被判 FAIL ⇒ 真空假红"
    assert verdict["status"] != "pass", "空冲突表被判 PASS ⇒ 真空伪装成通过"


def test_p0b_conflict_row_without_current_pct_still_fails() -> None:
    """② **成对负例**：冲突行缺 `llm_current_pct` ⇒ 仍必须 FAIL（这条必须有牙）。"""
    verdict = VG._p0b_verdict([{"code": "512800", "llm_target_pct": 0.0}])
    assert verdict["status"] == "fail", f"缺 llm_current_pct 却被放过：{verdict}"


def test_p0b_normal_conflicts_pass() -> None:
    """② 正常形态：每行都带 `llm_current_pct` ⇒ PASS。"""
    verdict = VG._p0b_verdict([{"code": "512800", "llm_current_pct": 20.0}])
    assert verdict["status"] == "pass", f"正常冲突行被判红：{verdict}"


def test_p0b_mutation_revert_to_require_nonempty_turns_red() -> None:
    """② **变异反证（回退修复）**：旧口径「冲突表必须非空」在空表上**必红**（假红现场）。"""
    verdict = VG._p0b_verdict([])
    old_ok = bool([]) and True  # 旧口径：bool(conflicts) and ... ⇒ 空表 ⇒ False ⇒ FAIL
    assert old_ok is False, "旧口径没把空冲突表判红 ⇒ 假红现场没被覆盖"
    assert verdict["status"] == "na", "新口径：空冲突表必须报 N/A"


def test_p0b_na_does_not_turn_the_whole_stage_into_not_applicable() -> None:
    """② **配套**：断言级 N/A **不许**把整个阶段变成「不适用」（退出码 2）。

    ⇒ 否则 run_gates.sh 会把这一阶段整段跳过 ⇒ 闸门覆盖面悄悄没了
       （而本轮其实有 20+ 条断言真的跑过、真的 PASS）。
    """
    assert VG._exit_code(0, False) == 0, "无失败且无阶段级 na ⇒ 退出码必须是 0（通过）"
    assert VG._exit_code(0, True) == 2, "阶段级 na ⇒ 退出码必须是 2（不适用 ≠ 通过）"
    assert VG._exit_code(1, False) == 1, "有失败 ⇒ 退出码必须是 1"
    assert VG._exit_code(1, True) == 1, "有失败 ⇒ 退出码必须是 1（失败优先）"


# =============================================================================
# ③ N1/N2/N4/N5：负例**自建夹具**（不依赖本轮真实产物）
# =============================================================================
@pytest.fixture()
def guard_fixture(tmp_path) -> tuple[dict, pathlib.Path]:
    """自建夹具：非空冲突表 + 计划件 + LLM 原文（探针自己造 ⇒ 不依赖真实产物）。"""
    md = tmp_path / "_probe_fixture_decision.md"
    md.write_text(VG._FIXTURE_DECISION_MD, encoding="utf-8")
    return VG._fixture_plan(md), md


def test_fixture_baseline_passes_both_guards(guard_fixture) -> None:
    """③ 夹具自检：未变异的夹具 ⇒ 两条守卫都 PASS（否则下面的负例就是**空转**）。"""
    plan, _md = guard_fixture
    semantic, traceable = VG._guard_verdicts(plan)
    assert semantic and traceable, "自建夹具本身就不合规 ⇒ 负例会空转"
    assert len(plan.get("llm_conflicts") or []) > 0, "夹具的冲突表必须非空"


def test_fixture_expectations_still_demand_fail() -> None:
    """③ **铁律守卫**：夹具负例的期望**不许**被改成「期望 PASS」（那是把断言改弱）。

    ⇒ 四条负例里「可追溯守卫要 FAIL」**恒为真**；N1/N2/N4 的「语义守卫要 FAIL」也恒为真。
    """
    assert set(VG._FIXTURE_EXPECT) == {"N1", "N2", "N4", "N5"}, "期望表缺项/多项"
    for tag, (want_semantic, want_traceable) in VG._FIXTURE_EXPECT.items():
        assert want_traceable is True, f"{tag} 的可追溯守卫期望被改成了 PASS（断言被改弱）"
    for tag in ("N1", "N2", "N4"):
        assert VG._FIXTURE_EXPECT[tag][0] is True, f"{tag} 的语义守卫期望被改成了 PASS"


def test_fixture_negative_cases_all_bite(guard_fixture) -> None:
    """③ **成对负例**：探针本体的夹具负例逻辑 ⇒ 四条**全部**符合期望（守卫真的转红）。

    ⇒ 直接跑 `probe_verify_guards.py` 里的**同一份函数** ⇒ 单测能盖住探针本体，
       而不只是复刻一份逻辑。
    """
    plan, _md = guard_fixture
    cases = VG._fixture_negative_cases(plan)
    assert len(cases) == 4, f"夹具负例数不对：{len(cases)}"
    for label, ok, detail in cases:
        assert ok, f"{label} 未达期望：{detail}"


@pytest.mark.parametrize(
    ("tag", "want_semantic_fail", "want_traceable_fail"),
    [
        ("N1", True, True),
        ("N2", True, True),
        ("N4", True, True),
        ("N5", False, True),
    ],
)
def test_fixture_mutations_turn_guards_red(guard_fixture, tag, want_semantic_fail,
                                           want_traceable_fail) -> None:
    """③ **成对负例**：N1/N2/N4/N5 的每条变异都必须让对应守卫**真的转红**。

    ⇒ 期望**不许**从「期望 FAIL」改成「期望 PASS」（那是把断言改弱）。
    """
    plan, _md = guard_fixture
    variants = {t.split(" ", 1)[0]: v for t, v in VG._fixture_variants(plan)}
    assert tag in variants, f"夹具里少了 {tag} 的变异"
    semantic, traceable = VG._guard_verdicts(variants[tag])
    assert semantic is (not want_semantic_fail), f"{tag} 变异后语义守卫判定不对（期望 FAIL={want_semantic_fail}）"
    assert traceable is (not want_traceable_fail), f"{tag} 变异后可追溯守卫判定不对（期望 FAIL={want_traceable_fail}）"


def test_fixture_mutations_are_noop_on_empty_real_conflicts(guard_fixture) -> None:
    """③ **变异反证（回退修复）**：负例**不许**依赖本轮真实交付件。

    ⇒ 旧实现的变异打在**本轮真实（空）冲突表**上 ⇒ 人造行注入不进去 ⇒ 变异**空转**
       ⇒ 期望的 FAIL 没发生 ⇒ 负例自己先红（假红）。
    ⇒ 现在负例走**自建夹具**（自带非空冲突表）⇒ 与本轮真实表无关。
    ⇒ 本用例**真的调探针本体**（`VG._fixture_variants` / `VG._fixture_negative_cases`），
       不是在一个本地字典上空转。
    """
    # 前提 1：本轮真实形态 = 冲突表为空
    real_plan = {"llm_conflicts": []}
    # 前提 2：反向机制 —— 变异打在这份**空**表上必然空转（旧负例假红的根因）
    _real_variants = {t.split(" ", 1)[0]: v for t, v in VG._fixture_variants(real_plan)}
    assert _real_variants, "探针没有产出任何变异体"
    assert all(v == real_plan for v in _real_variants.values()), (
        "🔴 空冲突表上的变异竟然生效了 ⇒ 旧负例的假红机制已变，结论需重新评估")
    # 主断言：夹具负例（探针本体）**与真实表无关**，仍然 4 条全红
    plan, _md = guard_fixture
    assert len(plan.get("llm_conflicts") or []) > 0, "夹具必须自带非空冲突表"
    cases = VG._fixture_negative_cases(plan)
    assert len(cases) == 4, f"夹具负例数不对：{len(cases)}"
    assert all(ok for _label, ok, _detail in cases), (
        "🔴 本轮真实冲突表为空时，夹具负例仍须全红 ⇒ 负例不得空转")


# =============================================================================
# ④ `probe_node_scenarios.py` 的 P3：按**原始浮点值**比较
# =============================================================================
#: 2026-11-13 实测的现场：默认权重 0.05333333333…（原始值）vs 聚合件 5.3333%（4 位小数展示值）
_DEFAULT_WEIGHT = 0.05333333333333333
_AGG_PCT = 5.3333
_TOTAL_ASSETS = 30000.0
_AMOUNT = round(_DEFAULT_WEIGHT * _TOTAL_ASSETS, 6)


def _default_payload(amount: float = _AMOUNT, weight: float = _DEFAULT_WEIGHT) -> dict:
    return {
        "actions": [{
            "code": "512800", "target_shares": 1800, "delta_shares": -5900,
            "action": "卖出", "target_weight": weight, "target_amount": amount,
        }],
    }


def _aaa_scenario(amount: float = _AMOUNT, pct: float = _AGG_PCT,
                  shares: int = 1800, action: str = "卖出") -> dict:
    return {
        "trades": [{
            "code": "512800", "target_shares": shares, "delta_shares": -5900,
            "action": action, "target_weight_pct": pct, "target_amount": amount,
        }],
    }


def test_p3_display_rounding_is_not_a_difference() -> None:
    """④ 正例：同一笔交易（默认 0.05333333333… vs 聚合件 5.3333%）⇒ **不算差异**。

    ⇒ 旧口径拿「展示用舍入值」当判据 ⇒ 这里必红（2026-11-13 的假红现场）。
    """
    problems, common = NS._p3_compare(
        NS._p3_default_signature(_default_payload()),
        NS._p3_aaa_signature(_aaa_scenario()),
        _TOTAL_ASSETS,
    )
    assert not problems, f"仅打印精度不同却被判红（假红）：{problems}"
    assert common == {"512800"}


def test_p3_mutation_revert_to_rounded_weight_turns_red() -> None:
    """④ **变异反证（回退修复）**：旧口径（各自 `round(..., 10)` 后比**权重**）**必红**。

    ⇒ 证明本用例盖住的正是 2026-11-13 的假红现场。
    """
    old_default = round(_DEFAULT_WEIGHT, 10)
    old_aaa = round(_AGG_PCT / 100.0, 10)
    assert old_default != old_aaa, "旧口径没把打印精度差异判红 ⇒ 假红现场没被覆盖"
    assert NS.P3_AMOUNT_TOL < 1e-6, "容差不许放大到能掩盖展示舍入的量级"


def test_p3_mutation_real_weight_difference_turns_red() -> None:
    """④ **成对负例**：真实差异（0.001 权重 ≈ 30 元）⇒ P3 **必须**转红（不许被容差放过）。"""
    real_diff_amount = round((_DEFAULT_WEIGHT + 0.001) * _TOTAL_ASSETS, 6)
    problems, _common = NS._p3_compare(
        NS._p3_default_signature(_default_payload()),
        NS._p3_aaa_signature(_aaa_scenario(amount=real_diff_amount)),
        _TOTAL_ASSETS,
    )
    assert problems, "0.001 权重的真实差异被放过 ⇒ 容差被放大了（断言被改弱）"


def test_p3_mutation_share_difference_turns_red() -> None:
    """④ **成对负例**：股数不同 ⇒ P3 必须转红。"""
    problems, _common = NS._p3_compare(
        NS._p3_default_signature(_default_payload()),
        NS._p3_aaa_signature(_aaa_scenario(shares=1700)),
        _TOTAL_ASSETS,
    )
    assert problems, "股数差异被放过 ⇒ P3 失去牙齿"


def test_p3_mutation_action_difference_turns_red() -> None:
    """④ **成对负例**：动作不同 ⇒ P3 必须转红。"""
    problems, _common = NS._p3_compare(
        NS._p3_default_signature(_default_payload()),
        NS._p3_aaa_signature(_aaa_scenario(action="买入")),
        _TOTAL_ASSETS,
    )
    assert problems, "动作差异被放过 ⇒ P3 失去牙齿"


def test_p3_extra_trade_is_reported() -> None:
    """④ 「全 A」多出交易项 ⇒ P3 必须报出来。"""
    scenario = _aaa_scenario()
    scenario["trades"].append({"code": "515650", "target_shares": 100, "delta_shares": 100,
                                "action": "买入", "target_weight_pct": 5.0, "target_amount": 1600.0})
    problems, _common = NS._p3_compare(
        NS._p3_default_signature(_default_payload()),
        NS._p3_aaa_signature(scenario),
        _TOTAL_ASSETS,
    )
    assert any("515650" in p for p in problems), f"多出的交易项没被报出来：{problems}"
