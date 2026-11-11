r"""🔴 2026-11-11（第九轮续·二）：**裸方向/状态词被当成 ETF 名称**的钉死测试。

① **裸方向词行产出垃圾名称**（`llm_final.py::_is_name_like` / `_row_hits`）
   事故：决策表的方向/状态列写的是裸词（`| 515020 | 否 | …不买 |`）
   ⇒ 该格被 `_row_hits` 当成**名称列**取走 ⇒ 交付件被否名单出现「515020 | 否」。
   同族裸词：`维持` / `目标` / `是` / `有` / `无` / `加` / `减` …

② **变异反证：真名称不许被一起毙掉**
   `_NAME_STOP_WORDS` 是**子串**判定（`w in txt`）⇒ 若把裸词直接塞进去，
   「**有**色金属ETF」「**减**…」这类**真名称**会被误杀。
   ⇒ 裸词必须走**整格相等**的独立判定（`_NAME_BARE_STATE_WORDS`）。

③ **名称非法时仍走 `name_map` 回填**（`final_rebalance_plan.py` 的 code→name 表）
   名称取空 ⇒ 渲染层显示「—」⇒ 由计划层用分析报告的 code→name 表回填。

本文件自带夹具，不依赖服务器产物、不联网。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"
_FRP_PATH = _ETF_DIR / "final_rebalance_plan.py"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_name_parsing_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_frp():
    spec = importlib.util.spec_from_file_location("_frp_name_parsing_under_test", _FRP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


#: 裸方向/状态词（整格等于它 ⇒ 不是名称）
_BARE_WORDS = ("否", "维持", "目标", "是", "有", "无", "加", "减")


# ────────────────────────── ① 裸方向词 ⇒ 不产出垃圾名称 ──────────────────────────


def test_bare_direction_word_cell_is_not_a_name():
    """裸方向/状态词**整格**出现 ⇒ 判定为「不是名称」。"""
    llf = _load_llm_final()
    for word in _BARE_WORDS:
        assert llf._is_name_like(word) is False, (
            f"裸方向/状态词 {word!r} 被当成了名称"
        )


def test_row_hits_does_not_return_bare_direction_word_as_name():
    """表格行的方向/状态格写成裸词 ⇒ 名称取空串（不猜）。"""
    llf = _load_llm_final()
    hits = llf._row_hits("| 515020 | 否 | …不买 |")
    assert hits, "该行仍应命中「否决」"
    for code, name in hits:
        assert name == "", f"代码 {code} 的名称字段出现了垃圾名 {name!r}"
    for word in _BARE_WORDS:
        row_hits = llf._row_hits(f"| 515020 | {word} | …不买 |")
        for _code, name in row_hits:
            assert name == "", f"裸词 {word!r} 被当成了名称"


def test_authority_rejection_name_is_empty_for_bare_direction_row(tmp_path):
    """端到端：权威段落里的裸词行 ⇒ 被否名单的名称字段为空串。"""
    llf = _load_llm_final()
    md = (
        "## 六、最终交易决定\n\n"
        "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
        "| 515020 | 否 | 不买 |\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    rows = llf.extract_authority_rejections(str(md_path))
    by_code = {str(r.get("code")): r for r in rows}
    assert "515020" in by_code, f"515020 应在否决名单里：{sorted(by_code)}"
    assert not (by_code["515020"].get("name") or "").strip(), (
        f"应取不到名称（空串），实际 {by_code['515020'].get('name')!r}"
    )


# ────────────────────────── ② 变异反证：真名称不许被误杀 ──────────────────────────


def test_real_etf_names_are_not_killed():
    """含 `ETF` / `LOF` / `联接` 后缀的真名称仍被正确识别。"""
    llf = _load_llm_final()
    real_names = (
        "有色金属ETF",     # 含裸词「有」⇒ 子串黑名单会误杀 ⇒ 必须整格判定
        "银行ETF华夏",
        "红利低波50ETF南方",
        "消费50ETF富国",
        "中证红利ETF招商",
        "某指数LOF基金",
        "易方达蓝筹ETF联接A",
    )
    for name in real_names:
        assert llf._is_name_like(name) is True, f"真名称 {name!r} 被误杀了"


def test_name_near_still_returns_real_name():
    """变异反证：散文里的真名称仍取得到。"""
    llf = _load_llm_final()
    sentence = "剔除 512800 银行ETF华宝"
    got = llf._name_near(sentence, "512800", sentence.find("512800"))
    assert got == "银行ETF华宝", got


# ────────────────────────── ③ 名称非法 ⇒ 走 name_map 回填 ──────────────────────────


def test_invalid_name_falls_back_to_name_map(tmp_path):
    """代码级断言：名称非法（空串）⇒ 计划层用分析报告的 code→name 表回填。

    断言打在**真正的 owner**上：`final_rebalance_plan._build_llm_final_decision`
    的被否行（`name = r.get("name") or name_map.get(code, "")`）。
    """
    llf = _load_llm_final()
    frp = _load_frp()

    code = "515020"
    plans = [{"code": code, "name": "银行ETF华夏", "target_weight": 0.16}]

    # 决策件：裸词行 ⇒ LLM 侧取不到名称（空串）
    md = (
        "## 六、最终交易决定\n\n"
        "**Rating**: Underweight\n\n"
        "| 标的 | 方向 | 理由 |\n|---|---|---|\n"
        "| 515020 | 否 | 不买 |\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    hits = llf._row_hits("| 515020 | 否 | …不买 |")
    llm_name = hits[0][1] if hits else ""
    assert llm_name == "", f"LLM 侧应取不到名称，实际 {llm_name!r}"

    # 计划层：被否行名称为空 ⇒ 回填 name_map
    args = types.SimpleNamespace(decision=str(md_path))
    out = frp._build_llm_final_decision(
        args,
        plans=plans,
        llm_targets={},
        llm_conflicts=[],
        ta_rejected=[{"code": code, "name": llm_name, "reason": "一票否决"}],
    )
    row = next(r for r in out["reconciliation"] if r["code"] == code)
    assert row["name"] == "银行ETF华夏", (
        f"名称非法时未走 name_map 回填，实际 {row['name']!r}"
    )
