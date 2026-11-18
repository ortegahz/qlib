"""🔴 2026-11-17 新增：G-TA1b（零误伤闸门的变异负例）**自足夹具**的回归测试。

【背景 · 为什么要改】
真机跑批 `snapshots/20261117_210415` 的闸门里 `probe_verify_guards` 红了一条：

    [FAIL] G-TA1b 变异负例：把一个 llm_targets 买侧代码塞进 ta_screening.rejected
           ⇒ 零误伤闸门必须 FAIL（否则闸门没牙）
            变异代码=515450｜变异后判定=pass（期望 fail）

【根因】
旧实现从**本轮真实产物**的 `llm_targets` 里挑一个买侧代码。那一轮挑到的是
`515450`，其 `who="bull"` ⇒ 权威层级 **0**（观点层）；而零误伤闸门
（`probes/probe_verify_guards.py::find_ta_reject_mis_hits`）**按设计**规定
「层级 0 的买侧证据不构成『TA 明确要买』」⇒ 变异后闸门仍然 PASS
⇒ **期望的 FAIL 根本没发生** ⇒ 负例空转、探针假红。

【修法（不许放宽断言）】
负例改用**自足夹具** `_fixture_authoritative_buy_mis_hit()`：
合成一条**权威层（Trader 最终裁决，层级 40）**的买侧证据 + 一条否决名单行
⇒ 不依赖任何真实产物 ⇒ 任何一轮都能真的转红。
同时新增 G-TA1c：把「观点层（层级 0）的买不构成误伤」这条口径**正面钉死**，
防止有人为了让 G-TA1b 转红而把层级 0 也算成证据（那会制造假红）。

【期望方向】
G-TA1b 期望 **FAIL**、G-TA1c 期望 **PASS** —— 本文件**不许**把期望改成反方向。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_PROBE_PATH = (
    Path(__file__).resolve().parents[1] / "probes" / "probe_verify_guards.py"
)


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_verify_guards_gta1b", _PROBE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


P = _load_probe()


def _minimal_plan() -> dict:
    """一份**最小**计划件（不含任何真实产物内容）。"""
    return {
        "ta_screening": {
            "enabled": True,
            "rejected": [
                {"code": "512170", "name": "医疗ETF华宝", "reason": "真否决",
                 "source_section": "辩论裁决"},
            ],
        },
        "llm_targets": {},
        "llm_final_decision": {"targets": {}},
    }


def test_夹具自造权威层买侧证据_闸门必须FAIL() -> None:
    """自足夹具 ⇒ 零误伤闸门必须 FAIL（期望方向不许改）。"""
    plan, md = P._fixture_authoritative_buy_mis_hit(_minimal_plan())
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", (
        f"权威层买侧代码进了否决名单 ⇒ 必须判误伤，实得 {verdict['status']}"
    )
    assert P._FIXTURE_MIS_HIT_CODE in [hit["code"] for hit in verdict["mis_hits"]], (
        f"必须点名 {P._FIXTURE_MIS_HIT_CODE}，实得 {[h['code'] for h in verdict['mis_hits']]}"
    )


def test_夹具不依赖本轮真实产物_空计划也能转红() -> None:
    """夹具必须**自足**：哪怕计划件里一个买侧代码都没有，也要能真的转红。"""
    plan = {"ta_screening": {"enabled": True, "rejected": []}}
    mutated, md = P._fixture_authoritative_buy_mis_hit(plan)
    verdict = P.find_ta_reject_mis_hits(mutated, md)
    assert verdict["status"] == "fail", "空计划也必须能构造出真实转红的负例"
    assert P._FIXTURE_MIS_HIT_CODE in [
        hit["code"] for hit in verdict["mis_hits"]
    ]


def test_变异反证_去掉权威层证据_负例就失去牙齿() -> None:
    """把夹具里的**权威层证据**去掉 ⇒ 闸门回到 PASS ⇒ 证明 FAIL 确实来自那条证据。

    ⇒ 证明负例**不是恒红**（探针有判别力），也不是恒 PASS（闸门有牙）。
    """
    mutated, md = P._fixture_authoritative_buy_mis_hit(_minimal_plan())
    # 去掉计划件里的最终裁决买侧证据
    mutated["llm_final_decision"]["targets"].pop(P._FIXTURE_MIS_HIT_CODE, None)
    # 去掉合成原文里那一行表格行（md 里不再有任何 599999 的买侧证据）
    md_wo = "\n".join(
        line for line in md.splitlines() if P._FIXTURE_MIS_HIT_CODE not in line
    )
    verdict = P.find_ta_reject_mis_hits(mutated, md_wo)
    assert verdict["status"] != "fail", (
        "没有买侧证据时闸门不得判误伤（说明负例的 FAIL 来自注入的权威层证据）"
    )


def test_观点层买侧不构成误伤_闸门必须PASS() -> None:
    """G-TA1c：`who="bull"`（层级 0 = 观点层）的「买」**不构成**误伤。"""
    plan = {
        "ta_screening": {"enabled": True, "rejected": []},
        "llm_targets": {},
        "llm_final_decision": {"targets": {}},
    }
    plan = P._inject_buy_side_target(plan, P._FIXTURE_OPINION_CODE, who="bull")
    plan = P._inject_rejected_code(plan, P._FIXTURE_OPINION_CODE)
    verdict = P.find_ta_reject_mis_hits(plan, "")
    assert verdict["status"] == "pass", (
        f"观点层的买不构成误伤，实得 {verdict['status']}"
    )


def test_变异反证_层级0一旦被当成证据_观点层断言必须能区分() -> None:
    """模拟「有人把层级 0 也算成买侧证据」（生产被改坏）⇒ 观点层夹具必须变成误伤。

    ⇒ 证明 G-TA1c 这条断言**有判别力**（不是恒 PASS 的空断言）。
    """
    plan = {
        "ta_screening": {"enabled": True, "rejected": []},
        "llm_targets": {},
        "llm_final_decision": {"targets": {}},
    }
    plan = P._inject_buy_side_target(plan, P._FIXTURE_OPINION_CODE, who="bull")
    plan = P._inject_rejected_code(plan, P._FIXTURE_OPINION_CODE)

    real = P._plan_direction_ranks

    def broken(plan_arg: dict, code: str) -> dict:
        out = real(plan_arg, code)
        return {k: (40 if k == "plan.llm_targets" else v) for k, v in out.items()}

    original = P._plan_direction_ranks
    P._plan_direction_ranks = broken  # type: ignore[assignment]
    try:
        verdict = P.find_ta_reject_mis_hits(plan, "")
    finally:
        P._plan_direction_ranks = original  # type: ignore[assignment]
    assert verdict["status"] == "fail", (
        "层级 0 被当成证据后必须出现误伤（否则 G-TA1c 就没有判别力）"
    )


def test_探针源码里G_TA1b期望仍是FAIL() -> None:
    """静态护栏：探针源码里 G-TA1b 的期望**不许**被改成 PASS。"""
    src = _PROBE_PATH.read_text(encoding="utf-8")
    start = src.find("G-TA1b 变异负例")
    end = src.find("G-TA1c 阳性对照")
    assert start != -1 and end != -1 and end > start, "探针源码里找不到 G-TA1b 断言块"
    text = src[start:end]
    assert '== "fail"' in text, "G-TA1b 的期望必须仍是 fail"
    assert '== "pass"' not in text, "G-TA1b 的期望不许是 pass"
