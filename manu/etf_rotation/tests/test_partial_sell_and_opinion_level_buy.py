#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-17 新增：**「部分卖出」不得当成清仓** ＋ **观点层的「买」不构成误伤**。

【事故来源】2026-11-17 真机跑批 `snapshots/20261117_085229`（服务器 `/mnt/data/siping/
runs/etf_rotation_results/`）⇒ `run_gates.sh` 两阶段红灯：

  ① `probe_llm_parser` **A20⑥ 红灯**
     真机最终交易决定原句（逐字）：
         最终交易决定：对 `512800` 执行**部分卖出、降低超配**，不是清仓，也不是建立反向空头。
         - 因此卖出约**2,200份 `512800`**，参考毛回收约1,922.80元。
     ⇒ 旧词表 `_PARTIAL_SELL_MARKERS` 只有「减仓 / 减持 / 减掉」⇒ **没有**「部分卖出」
     ⇒ 裸「卖出」规则（第 ④ 档）命中 ⇒ 方向='卖'、目标权重=**0%**（＝清仓）
     ⇒ 交付件把「减至规则层 16% 标准槽」写成「**清仓 0%**」⇒ 与动作表自相矛盾
     ⇒ 「与规则层不一致」的缺口被夸成 -23.5pp（真缺口只有 -7.5pp）。

  ② `probe_verify_guards` **G-TA1 红灯（假红）**
     真机决策 md 里 `515650` 在**研究经理层**是**真否决**（`卖/不买，正式否决`），
     而**多头（观点层）**的表里写 `| `515650` | **买入** |` ⇒ 两处方向相反。
     ⇒ 旧判据把「层级 0（观点层/认不出）」的买侧证据与「层级 0（出处认不出）的否决」
       **打平** ⇒ 判成「否决名单误伤」⇒ **假红**（真机否决成立，且多头那句只是观点）。

【口径铁律】
  * 「部分卖出」＝**减**（不是清仓）⇒ 方向必须「减」，目标权重**不许**被清零；
  * **观点层**（多头/空头/激进派/保守派/中立派）的「买」**不构成**「TA 明确要买」
    ⇒ 不许据此判「否决名单误伤」（同 `tests/test_probe_ta_reject_authority_rank.py` 口径）；
  * 「纯代码格」可以是**多个**代码（`|`516310`、`515020`、`512820`|`）⇒ 方向格对**每一只**都算
    （否则 12 只取不到证据 ⇒ 闸门整段判「不适用」＝ 不是通过）；
  * 上面每条都配**变异反证**：把护栏改回旧口径 ⇒ 对应断言必须**转红**。
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_final as L  # noqa: E402
from manu.etf_rotation.probes import probe_verify_guards as P  # type: ignore  # noqa: E402

#: 🔴 真机最终交易决定原句（逐字取自 `snapshots/20261117_085229/tradingagents_decision.md`）
REAL_FINAL_SECTION = (
    "## 六、最终交易决定（原始输出）\n\n"
    "**Rating**: Underweight\n\n"
    "**Executive Summary**\n\n"
    "最终交易决定：对 `512800` 执行**部分卖出、降低超配**，不是清仓，也不是建立反向空头。\n\n"
    "- 因此卖出约**2,200份 `512800`**，参考毛回收约1,922.80元；"
    "卖出部分相对0.82元成本价实现约118.80元税前价差收益。\n\n"
    "**Position Sizing**: 减持至规则层 16% 标准单槽上限，"
    "即卖出约 2,200–2,300 股释放约 1,900–2,000 元。\n"
)

#: 真机研究经理表（多代码格共用一个方向格）
REAL_RESEARCH_TABLE = (
    "| 标的 | 方向 | 执行理由 |\n"
    "|---|---|---|\n"
    "| `516310`、`515020`、`512820` | **卖/不买，正式否决** | 与 `512800` 成分重叠。 |\n"
)


# --------------------------------------------------------------------- ① 生产解析
def test_真机原句_部分卖出必须判减不得判清仓() -> None:
    """🔴 主用例：「执行**部分卖出、降低超配**，不是清仓」⇒ 方向=减、目标**不许** 0%。"""
    out = L.extract_final_decision(REAL_FINAL_SECTION)
    row = (out.get("targets") or {}).get("512800")
    assert row, f"🔴 512800 的最终裁决被整条丢掉：{out.get('targets')}"
    assert row["direction"] == "减", f"🔴 「部分卖出」必须判「减」，实得 {row['direction']!r}"
    assert row["target_pct"] not in (0.0, 0), (
        f"🔴 「不是清仓」⇒ 目标权重不许被清零，实得 {row['target_pct']!r}"
    )


def test_变异_去掉部分卖出标记_必须重新误判成清仓(monkeypatch) -> None:
    """🔴 **变异反证**：把「部分卖出」从标记表里拿掉 ⇒ 必须重新被判成「卖 / 0%」
    ⇒ 证明这条护栏**真的承重**（不是断言本来就不成立）。"""
    monkeypatch.setattr(L, "_PARTIAL_SELL_MARKERS", ("减仓", "减持", "减掉"))
    out = L.extract_final_decision(REAL_FINAL_SECTION)
    row = (out.get("targets") or {}).get("512800") or {}
    assert row.get("direction") == "卖", f"变异后应退回裸「卖出」，实得 {row.get('direction')!r}"
    assert row.get("target_pct") == 0.0, f"变异后应退回清仓 0%，实得 {row.get('target_pct')!r}"


# --------------------------------------------------------------------- ② 探针：多代码格
def test_多代码格的方向对每一只代码都算证据() -> None:
    """🔴 真机研究经理表：「`516310`、`515020`、`512820`」共用一个方向格
    ⇒ 三只**都**要取到方向证据（否则闸门把它们判成「不适用」＝ 不是通过）。"""
    for code in ("516310", "515020", "512820"):
        found = P._md_directions(REAL_RESEARCH_TABLE, code)
        assert found, f"🔴 {code} 取不到方向证据（多代码格被漏判）"
        assert not P._is_buy_side(P._classify_direction(found[0][1])), (
            f"🔴 {code} 的方向被判成买侧：{found}"
        )


def test_顺带提到代码的散文格仍然不算代码格() -> None:
    """老口径不许丢：「与 515450 相关性 0.917」这种**顺带提到**的格不算代码格。"""
    md = "\n".join([
        "| 标的 | 方向 | 核心理由 |",
        "|---|---:|---|",
        "| **563020** | **不买** | 未被规则选入，且与515450相关性0.917。 |",
    ])
    assert P._md_directions(md, "563020") == [("md:L3", "不买")]
    assert P._md_directions(md, "515450") == [], "顺带提到的代码不许继承别人的方向"


# --------------------------------------------------------------------- ③ 探针：观点层买侧
def _veto_plan(code: str, source_section: str) -> dict:
    return {
        "ta_screening": {
            "enabled": True,
            "rejected": [{"code": code, "name": "", "reason": "", "source_section": source_section}],
        },
        "llm_targets": {},
        "llm_final_decision": {"available": True, "targets": {}},
    }


def test_观点层的买不得判成否决名单误伤() -> None:
    """🔴 真机形状：`515650` 在**研究经理层**被真否决，多头（观点层）说「买入」
    ⇒ 观点层没有否决权、也没有「要买」的约束力 ⇒ **不许**判误伤。"""
    md = "\n".join([
        "## 三、多空辩论要点",
        "",
        "### 多头（Bull）",
        "",
        "| 代码 | 方向 | 理由 |",
        "|---|---|---|",
        "| `515650` | **买入** | 120日动量+9.27%。 |",
        "",
        "## 二、研究经理结论（Research Manager）",
        "",
        "| 标的 | 方向 | 执行理由 |",
        "|---|---|---|",
        "| `515650` | **卖/不买，正式否决** | 拥挤度 94% 分位。 |",
    ])
    verdict = P.find_ta_reject_mis_hits(_veto_plan("515650", "二、研究经理结论（Research Manager）"), md)
    assert verdict["status"] == "pass", (
        f"🔴 观点层的「买」不构成误伤，实得 {verdict['status']}：{verdict['detail']}"
    )


def test_变异_把买侧证据升到最终交易决定层_必须重新判红() -> None:
    """🔴 **变异反证**：同一份 md，把买侧证据挪到「最终交易决定」（层级 40）
    ⇒ 权威层级的买侧指令 ⇒ **必须**判误伤 ⇒ 证明层级判据仍在承重。"""
    md = "\n".join([
        "## 六、最终交易决定（原始输出）",
        "",
        "| 代码 | 方向 | 理由 |",
        "|---|---|---|",
        "| `515650` | **买入** | 维持红利低波第三槽。 |",
    ])
    verdict = P.find_ta_reject_mis_hits(_veto_plan("515650", "二、研究经理结论（Research Manager）"), md)
    assert verdict["status"] == "fail", (
        f"🔴 权威层的买侧证据必须判误伤，实得 {verdict['status']}：{verdict['detail']}"
    )


# --------------------------------------------------------------------- ④ 探针：N9/N10 前置
def test_N9N10前置_自建夹具永远有可变异的非零目标() -> None:
    """🔴 真机形状：本轮 LLM 裁决表**只有方向、没有目标百分比** ⇒ 旧口径 N9/N10 整块假红。
    ⇒ 改成**自建夹具**后，前置**永远**成立（任何一轮都能真的构造变异）。"""
    fixture = P._fixture_plan(Path("/tmp/_probe_fixture_decision.md"))
    code = P._pick_target_code(fixture)
    assert code is not None, "🔴 自建夹具里必须有带非零 target_pct 的条目"
    row = fixture["llm_targets"][code]
    assert row.get("target_pct"), f"🔴 {code} 的 target_pct 必须非零，实得 {row.get('target_pct')!r}"
    # 变异本身必须**真的改坏**夹具（改了等于没改 ⇒ 负例无牙）
    mutated = copy.deepcopy(fixture)
    mutated["llm_targets"][code]["direction"] = "Sell"
    mutated["llm_targets"][code]["target_pct"] = 26.96
    assert mutated != fixture, "🔴 N9 的变异必须真的生效"
    semantic_ok, _trace = P._guard_verdicts(mutated)
    assert semantic_ok is False, "🔴 「Sell 配 26.96%」必须被语义守卫拦下"
    # N10：未分类方向词 ⇒ 无法校验 ≠ 可以放过
    mutated2 = copy.deepcopy(fixture)
    mutated2["llm_targets"][code]["direction"] = "减持到三成"
    semantic_ok2, _trace2 = P._guard_verdicts(mutated2)
    assert semantic_ok2 is False, "🔴 未分类方向词必须被语义守卫拦下"


# --------------------------------------------------------------------- ⑤ 探针/生产口径同源
def test_探针的部分卖出标记表必须与生产同源() -> None:
    """🔴 2026-11-17 实测事故：探针的标记表是**手抄副本** ⇒ 生产加了「部分卖出」
    而探针没加 ⇒ **口径漂移** ⇒ 探针把「部分卖出」判成清仓 ⇒ A20⑥ **假红**。
    ⇒ 现在探针**直接引用**生产常量 ⇒ 以后生产加词，探针自动跟上。"""
    from manu.etf_rotation.probes import probe_llm_parser as PLP  # noqa: PLC0415

    assert PLP._MD_PARTIAL_SELL_MARKERS == L._PARTIAL_SELL_MARKERS, (
        "🔴 探针与生产的部分卖出标记表漂移："
        f"{PLP._MD_PARTIAL_SELL_MARKERS} ≠ {L._PARTIAL_SELL_MARKERS}"
    )
    assert "部分卖出" in L._PARTIAL_SELL_MARKERS, "🔴 生产标记表必须含「部分卖出」"


def test_探针不得把部分卖出判成零方向() -> None:
    """🔴 真机原句 ⇒ 探针的「零方向集合」**不许**含 512800（它是减仓，不是清仓）。"""
    from manu.etf_rotation.probes import probe_llm_parser as PLP  # noqa: PLC0415

    zero, note = PLP._md_zero_direction_codes(REAL_FINAL_SECTION)
    assert "512800" not in zero, f"🔴 「部分卖出」被探针判成清仓：{sorted(zero)}｜{note}"
