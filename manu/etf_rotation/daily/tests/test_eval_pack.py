"""eval_pack（评估就绪包）守卫测试。

【为什么需要】
2026-11-27 审计发现：快照里「事后系统有效性评估」所需资料**不是全有，也不是全无**，
而是**原始件齐、机读件缺**；且 ``ta_screening.rejected`` 这条最现成的影子账本源
**本身有误判**（实测 2026-11-26：5 条里 2 条把"邻居标的的否决词"张冠李戴）。

⇒ `eval_pack.py` 的职责就是把**可机读、可审计**的评估输入落进快照。
本测试把三条最关键的不变式钉死：

  ① **裁决优先级**：影子账本的 ``ta_verdict`` 必须以 **TA 最终裁决**为准，
     **不许**用 ``ta_screening.rejected``（它会误判）；
  ② **分歧必须显式暴露**：管道口径与 TA 原文不一致时，``screening_agrees=False``
     且 ``disagreement_note`` 非空（**不许**悄悄吞掉）；
  ③ **零副作用 + fail-soft**：只写快照目录内的 6 件，不碰任何既有交付件。

【镜像副本】
本文件在 ``manu/etf_rotation/daily/tests/`` 与 ``manu/daily/tests/`` **逐字一致**（镜像纪律）。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import eval_pack as EP  # noqa: E402


# ----------------------------------------------------------------- 夹具
def _candidate(rank: int, code: str, e_score: float, *, selected: bool = False,
               name: str = "") -> dict:
    return {
        "rank": rank,
        "code": code,
        "name": name or f"ETF{code}",
        "e_score": e_score,
        "final_score": (e_score - 0.14) / 0.138,
        "selected": selected,
        "not_selected_reason": "" if selected else "分数排名未进入目标名额",
        "coverage": 1.0,
        "above_ma60": True,
        "mom120": 0.05,
        "theme_id": "测试",
        "is_defensive": False,
    }


@pytest.fixture()
def analysis_report() -> dict:
    """19 只候选池（与真实快照同量级 ⇒ 三档切成 6 / 7 / 6）。"""
    return {
        "candidates": [
            _candidate(i + 1, f"code{i:03d}", 0.4 - i * 0.02, selected=(i < 3))
            for i in range(19)
        ],
        "filter_stats": {"n_candidate_pool": 19, "n_selected": 3},
    }


@pytest.fixture()
def plan() -> dict:
    """一份**复刻真实快照缺陷**的计划件。

    🔴 这里刻意保留两处**真实缺陷**：
       * ``ta_screening.rejected`` 把 512800（TA 原文="减仓"）和 159928（TA 原文="买"）
         误记成"否决"；
       * ``llm_final_decision.targets`` **没有** 159928（解析器漏了）⇒ 只能退回
         ``llm_targets``（中等置信）。
    """
    return {
        "ta_screening": {
            "rejected": [
                {"code": "512800", "name": "银行ETF", "reason": "辩论议题句"},
                {"code": "159928", "name": "消费ETF", "reason": "否决 515170：与 159928 重叠"},
                {"code": "515170", "name": "食品饮料ETF", "reason": "正式否决"},
            ],
            "substituted": [
                {"code": "515650", "substituted_for": "512800", "name": "消费50ETF"},
            ],
        },
        "llm_targets": {
            "512800": {"direction": "卖", "target_pct": 0.0, "reason": "降风险"},
            "159928": {"direction": "买", "target_pct": 20.0, "reason": "最终分最高"},
            "515170": {"direction": "不买", "target_pct": 0.0, "reason": "重叠高"},
            "515450": {"direction": "买", "target_pct": 20.0, "reason": "E 与动量为正"},
        },
        "llm_final_decision": {
            "targets": {
                "512800": {
                    "direction": "减",
                    "target_pct": 10.0,
                    "evidence": "卖出约 4,400 份",
                    "shares_sell": 4400,
                },
                "515170": {"direction": "不建仓", "target_pct": 0.0, "evidence": "正式否决"},
            },
        },
        "actions": [
            {"code": "515450", "action": "买入", "target_weight": 0.1172, "target_shares": 2300,
             "est_fee": 2.26},
            {"code": "512800", "action": "维持", "target_weight": 0.2327, "target_shares": 7700},
        ],
    }


@pytest.fixture()
def card() -> dict:
    return {
        "asof": "2026-11-26",
        "execution_date": "2026-11-27",
        "orders": [
            {"code": "515450", "name": "红利低波50", "action": "买入", "delta_shares": 2300,
             "price": 1.512, "est_amount": 3477.6, "est_fee": 5.0, "est_slippage": 0.34776,
             "reason": "入选本期组合"},
            {"code": "512800", "name": "银行ETF", "action": "维持", "delta_shares": 0,
             "price": 0.927, "est_amount": 0.0, "est_fee": 0.0, "est_slippage": 0.0,
             "reason": "保持现仓"},
        ],
        "cash_flow": {"buy_fees": 5.0, "cash_end": 19889.64},
        "constraints": {
            "commission_rate": 0.0003, "min_commission": 5.0,
            "slippage_rate": 0.0001,
            # 🔴 P2（2026-11-27：ETF 卖出侧印花税归零）：执行卡现在披露 **0.0**
            #    （场内 ETF 买卖双向免印花税 ⇒ 与研究链口径同源）
            "stamp_duty_rate": 0.0,
        },
        "post_trade": {
            "positions": [
                {"code": "515450", "shares": 2300},
                {"code": "512800", "shares": 7700},
                {"code": "515650", "shares": 0},
            ],
        },
    }


# ----------------------------------------------------------------- ① 裁决优先级
@pytest.mark.parametrize(
    "raw,expect",
    [
        ("买", "放行买入"),
        ("买入", "放行买入"),
        ("加仓", "放行买入"),
        ("卖", "放行减仓"),
        ("减", "放行减仓"),
        ("减持", "放行减仓"),
        ("卖出", "放行减仓"),
        ("不买", "否决"),
        ("不建仓", "否决"),
        ("否决", "否决"),
        ("", ""),
    ],
)
def test_normalize_verdict(raw: str, expect: str) -> None:
    """方向词归一化（**"不买" 不许被 "买" 抢走**；空 ⇒ 未解析）。"""
    assert EP.normalize_verdict(raw) == expect


def test_normalize_verdict_empty_is_unknown_not_pass() -> None:
    """🔴 空方向 = **未知**，不许当"放行"。"""
    assert EP.normalize_verdict("") == ""
    assert EP.normalize_verdict("   ") == ""
    assert EP.normalize_verdict("说不清") == ""


# ----------------------------------------------------------------- ② 候选池表
def test_candidate_pool_rows_shape(analysis_report: dict) -> None:
    """19 行；列名**对齐** v2/attribution.py 的 diag 接口。"""
    rows = EP.build_candidate_pool_rows(analysis_report, asof="2026-11-26",
                                        held_codes=["code000"])
    assert len(rows) == 19
    for key in ("date", "code", "coverage_ok", "liquidity_ok", "tradable", "selected"):
        assert key in rows[0], f"归因接口缺列 {key}"
    assert rows[0]["date"] == "2026-11-26"
    assert rows[0]["is_held_after"] is True
    assert rows[1]["is_held_after"] is False


def test_candidate_pool_tercile_split(analysis_report: dict) -> None:
    """三档切分 19 ⇒ **6 / 7 / 6**（用户方法论文档口径）。"""
    rows = EP.build_candidate_pool_rows(analysis_report, asof="2026-11-26")
    buckets = [r["tercile"] for r in rows]
    assert buckets.count("高") == 6
    assert buckets.count("中") == 7
    assert buckets.count("低") == 6
    assert buckets[0] == "高" and buckets[-1] == "低"


# ----------------------------------------------------------------- ③ 影子账本
def test_ta_ledger_prefers_final_decision_over_screening(analysis_report: dict,
                                                         plan: dict) -> None:
    """🔴 512800：管道记"否决"，TA 原文是"减仓" ⇒ **以 TA 原文为准**。"""
    rows = {r["code"]: r for r in
            EP.build_ta_ledger_rows(analysis_report, plan, held_codes=["512800"])}
    row = rows["512800"]
    assert row["ta_verdict"] == "放行减仓"
    assert row["ta_verdict_source"] == "llm_final_decision.targets"
    assert row["ta_confidence"] == "high"
    assert row["in_ta_screening_rejected"] is True
    assert row["screening_agrees"] is False
    assert "分歧" in row["disagreement_note"]


def test_ta_ledger_falls_back_to_llm_targets(analysis_report: dict, plan: dict) -> None:
    """159928：最终裁决表没解析出 ⇒ 退回 ``llm_targets``（中等置信）+ 标记分歧。"""
    rows = {r["code"]: r for r in
            EP.build_ta_ledger_rows(analysis_report, plan)}
    row = rows["159928"]
    assert row["ta_verdict"] == "放行买入"
    assert row["ta_verdict_source"] == "llm_targets"
    assert row["ta_confidence"] == "medium"
    assert row["ta_final_parsed"] is False
    assert row["screening_agrees"] is False


def test_ta_ledger_marks_unknown_verdict_as_none(analysis_report: dict, plan: dict) -> None:
    """无可解析裁决的行 ⇒ ``screening_agrees`` **留空**（不是 True）。"""
    rows = {r["code"]: r for r in
            EP.build_ta_ledger_rows(analysis_report, plan)}
    row = rows["515650"]
    assert row["ta_verdict"] == ""
    assert row["screening_agrees"] is None
    assert "无可解析" in row["disagreement_note"]


def test_ta_ledger_true_rejection_agrees(analysis_report: dict, plan: dict) -> None:
    """真否决（515170）⇒ 与管道口径**一致** ⇒ agrees=True。"""
    rows = {r["code"]: r for r in
            EP.build_ta_ledger_rows(analysis_report, plan)}
    row = rows["515170"]
    assert row["ta_verdict"] == "否决"
    assert row["screening_agrees"] is True
    assert row["disagreement_note"] == ""


def test_ta_ledger_promoted_as(analysis_report: dict, plan: dict) -> None:
    """顺延补位：512800 → 515650。"""
    rows = {r["code"]: r for r in
            EP.build_ta_ledger_rows(analysis_report, plan)}
    assert rows["512800"]["promoted_as"] == "515650"


# ----------------------------------------------------------------- ④ 台账 / 成本 / NAV
def test_fill_rows_leave_actual_blank(card: dict) -> None:
    """actual_* 五列**必须留空**（只能人工回填）。"""
    rows = EP.build_fill_rows(card, asof="2026-11-26", live_trade=True)
    assert len(rows) == 2
    for row in rows:
        for key in ("actual_shares", "actual_price", "actual_amount",
                    "actual_commission", "actual_transfer_fee"):
            assert row[key] == ""
        assert row["fill_status"] == "待回填"
        assert row["signal_date"] == "2026-11-26"
        assert row["execution_date"] == "2026-11-27"


def test_fill_rows_observation_mode(card: dict) -> None:
    """观察态 ⇒ fill_status 写明"未成交"，不许假装成交。"""
    rows = EP.build_fill_rows(card, asof="2026-11-26", live_trade=False)
    assert all("观察态" in r["fill_status"] for r in rows)


def test_cost_assumption_flags_two_calibers(card: dict) -> None:
    """🔴 执行卡 5.00 vs 节点件 2.26044 ⇒ 必须被**并列披露**。"""
    node = {"cash_flow": {"buy_fees": 2.26044, "cash_end": 19892.72956}}
    payload = EP.build_cost_assumption(card, node, asof="2026-11-26", plan={"actions": []})
    blob = json.dumps(payload, ensure_ascii=False)
    assert "5.0" in blob and "2.26044" in blob
    assert any("不一致" in d for d in payload["disagreements"])
    assert payload["per_order"], "逐单费用对照缺失"
    assert payload["per_order"][0]["gap_bp_vs_backtest"] is not None


def test_cost_assumption_note_states_etf_stamp_duty_exempt(card: dict) -> None:
    """🔴 P2：执行卡层的 note 必须**如实**写「ETF 买卖双向免印花税 ⇒ 卖出侧不再叠加印花税」。

    🔴 旧文案（「卖出另加千一印花税」＋「卖出侧费用口径偏高」）与新口径**直接矛盾**
       ⇒ 留着就是**说谎的披露件**（P4「口径写在件里」）。
    期望值是**字面量**：note 里必须**不含**旧文案的两句关键措辞，且必须**含**新措辞。
    """
    node = {"cash_flow": {"buy_fees": 2.26044, "cash_end": 19892.72956}}
    payload = EP.build_cost_assumption(card, node, asof="2026-11-26", plan={"actions": []})
    note = str(payload["layers"]["execution_card"]["note"])
    assert "免印花税" in note
    assert "卖出另加千一印花税" not in note, f"旧口径文案仍在披露件里：{note!r}"
    assert "口径偏高" not in note, f"旧口径文案仍在披露件里：{note!r}"
    # 🔴 这条只验「披露层**如实转述**卡里的口径」（期望值 = 字面量 0.0）；
    #    卡里那个 0.0 **本身**由每日链生产 ⇒ 它的牙齿在
    #    ``test_agents_rebalance.py::test_card_constraints_disclose_zero_stamp_duty``。
    assert payload["layers"]["execution_card"]["stamp_duty_rate"] == 0.0


def test_cost_assumption_note_mutation_proof(tmp_path: Path, card: dict) -> None:
    """🔴 变异反证：把 ``eval_pack.py`` 的 note **改回旧文案**（临时副本）⇒
    上面那条断言**必须转红**（否则它就是摆设）。

    🔴 只动**临时目录里的副本** ⇒ **绝不碰真源码**（``eval_pack.py`` 是纯标准库模块 ⇒ 加载无副作用）。
    """
    src = Path(EP.__file__).read_text(encoding="utf-8")
    old_note = (
        '"🔴 每日链执行卡口径：**含 5 元最低佣金下限**；卖出另加千一印花税"\n'
        '                "（🔴 但 ETF 实为**免印花税** ⇒ 卖出侧费用口径偏高）"'
    )
    new_note = (
        '"🔴 每日链执行卡口径：**含 5 元最低佣金下限**；"\n'
        '                "ETF 买卖双向免印花税 ⇒ 卖出侧**不再叠加印花税**（与研究链口径同源）"'
    )
    assert old_note not in src, "🔴 活链里找不到旧文案 ⇒ 源码结构变了，本文件的变异反证失效"
    assert new_note in src, "🔴 活链里找不到新文案 ⇒ 源码结构变了，本文件的变异反证失效"
    mutated = src.replace(new_note, old_note, 1)
    assert mutated != src, "变异未生效"
    tmp_src = tmp_path / "eval_pack_mutated_stamp.py"
    tmp_src.write_text(mutated, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("eval_pack_mutated_stamp", tmp_src)
    assert spec is not None and spec.loader is not None
    mut = importlib.util.module_from_spec(spec)
    sys.modules["eval_pack_mutated_stamp"] = mut
    try:
        spec.loader.exec_module(mut)
        node = {"cash_flow": {"buy_fees": 2.26044, "cash_end": 19892.72956}}
        payload = mut.build_cost_assumption(card, node, asof="2026-11-26", plan={"actions": []})
        mutated_note = str(payload["layers"]["execution_card"]["note"])
        assert "卖出另加千一印花税" in mutated_note, (
            "🔴 变异反证失效：改回旧文案后旧措辞竟然没出现 ⇒ 上面那条断言没有牙齿"
        )
        # ⇒ 上面那条守卫（「卖出另加千一印花税」不在 note 里）在**变异体上必然转红**
    finally:
        sys.modules.pop("eval_pack_mutated_stamp", None)


def test_nav_rows_flag_future_dirty_point() -> None:
    """🔴 未来日期 ⇒ 标脏点（**只标记、不删除**）。"""
    nav_state = {"nav": 30510.49, "peak": 30510.49, "drawdown": 0.0}
    history = {"points": {"2026-11-26": 30510.49, "2026-12-01": 30212.67}}
    rows = EP.build_nav_rows(nav_state, history, asof="2026-11-26")
    assert len(rows) == 2
    future = [r for r in rows if r["date"] == "2026-12-01"][0]
    assert future["is_future"] is True
    assert "脏点" in future["note"]
    assert [r for r in rows if r["date"] == "2026-11-26"][0]["is_asof"] is True


def test_nav_rows_fallback_when_history_missing() -> None:
    """序列不可读 ⇒ 只落当前点（**不许编**）。"""
    rows = EP.build_nav_rows({"nav": 1.0}, {}, asof="2026-11-26")
    assert len(rows) == 1 and rows[0]["nav"] == 1.0


# ----------------------------------------------------------------- 落盘
def test_write_eval_pack_writes_six_files(tmp_path: Path, analysis_report: dict,
                                           plan: dict, card: dict) -> None:
    """快照目录里落 6 件；manifest 自带缺口声明。"""
    root = tmp_path / "snap"
    root.mkdir()
    (root / "etf_analysis_report.json").write_text(
        json.dumps(analysis_report, ensure_ascii=False), encoding="utf-8")
    (root / "final_rebalance_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (root / "execution_card_20261126.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8")
    (root / "node_reprice_20261126.json").write_text(
        json.dumps({"cash_flow": {"buy_fees": 2.26}}, ensure_ascii=False), encoding="utf-8")

    info = EP.write_eval_pack(root, asof="2026-11-26")
    for name in EP.EVAL_PACK_FILES:
        assert (root / name).is_file(), f"缺件 {name}"
    assert len(info["files"]) == len(EP.EVAL_PACK_FILES)
    assert info["n_pool_rows"] == 19

    manifest = json.loads((root / "eval_pack_manifest.json").read_text(encoding="utf-8"))
    assert manifest["gaps_still_open"], "必须写明仍未闭合的缺口"
    assert manifest["invariants"]["P1_zero_side_effect"]

    with (root / "eval_candidate_pool.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 19
    assert rows[0]["date"] == "2026-11-26"


def test_write_eval_pack_is_fail_soft(tmp_path: Path) -> None:
    """🔴 快照里啥都没有 ⇒ **不抛**，照样落 manifest（评估不许阻断流水线）。"""
    root = tmp_path / "empty"
    info = EP.write_eval_pack(root, asof="2026-11-26")
    assert (root / "eval_pack_manifest.json").is_file()
    assert info["files"], "manifest 本身必须落"


def test_write_eval_pack_does_not_touch_existing_artifacts(tmp_path: Path,
                                                            analysis_report: dict) -> None:
    """🔴 **零副作用**：既有交付件的字节不许变。"""
    root = tmp_path / "snap"
    root.mkdir()
    target = root / "etf_analysis_report.json"
    target.write_text(json.dumps(analysis_report, ensure_ascii=False), encoding="utf-8")
    before = target.read_bytes()
    EP.write_eval_pack(root, asof="2026-11-26")
    assert target.read_bytes() == before
