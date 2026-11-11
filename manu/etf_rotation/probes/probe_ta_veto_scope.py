#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🛡️ **一票否决作用域**闸门探针（否决只认「权威裁决段落」）。

【为什么要有这个探针】
2026-11-11 真机跑批暴露：`_decision_text()` 把决策 JSON 的 **7 个段落**
（研究经理 / 多头 / 空头 / 激进派 / 保守派 / 中立派 / 最终交易决定）拼成一段文本，
`extract_rejections` 扫**全文** ⇒ 把**分析师观点**当**最终否决**：

  * 空头观点含「暂不买入 `515450`、`515650`」 ⇒ 误伤 2 只
    （TA 的**最终裁决**明确「买 515450 8% / 买 515650 6%」）；
  * 激进派观点含「清仓512800，等于放弃…」 ⇒ 误伤 1 只
    （TA 的最终裁决是「**减**至 10%、硬上限 12%」＝减仓指令，不是不建仓）。

⇒ 真实否决集应是 **13 只**（0 仓观察那批），实跑却得到 **16 只**。
⇒ 修法：否决解析**只喂权威段落**（`llm_final.AUTHORITY_SECTIONS`）。

🔴 **本探针用真机决策件的高保真裁剪夹具**（`tests/fixtures/ta_decision_real_20261110.json`）
⇒ **不依赖网络、不依赖服务器产物**。

【断言清单】
A1  权威提取 ⇒ **恰好 13 只**，且与期望集合**逐个相等**（不多不少）
A2  **零误伤**：515450 / 515650 / 512800 **不得**出现在否决集里
A3  **护栏是「load-bearing」的**：观点段落**单独**喂解析器**确实会**命中这 3 只
    （证明 A2 不是靠硬编码名单蒙出来的；将来若有人放宽作用域，这里立刻红）
A4  `.md` **兜底**路径 ⇒ 同样 13 只、零误伤（JSON 缺失时不能退化到"扫全文"）
A5  **变异反证**：把权威段落的「563020 0 仓观察」行改成「买 8%」
    ⇒ 563020 **不再**被否 ⇒ 断言没有靠硬编码名单
A6  **Fail-safe**：决策文件缺失 / JSON 损坏 ⇒ 返回空列表、**不抛异常**
A7  `extract_rejections` 本体语义**未变**（合成正例命中、反例不命中）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_final  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ta_decision_real_20261110.json"

#: TA 最终裁决里「0 仓观察」那批 = **真实否决集**（13 只）。
EXPECTED_13: frozenset[str] = frozenset({
    "563020", "159207", "512170", "515020", "516310", "512820",
    "512890", "159547", "515100", "159307", "515180", "515080", "511990",
})

#: TA 最终裁决明确「买 / 减」⇒ **绝不能**被当成否决。
PROTECTED: frozenset[str] = frozenset({"515450", "515650", "512800"})

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打一条断言结果。

    🔴 必须带 `[PASS]` / `[FAIL]` 字面量（`run_gates.sh` 靠 `grep -c '\\[PASS\\]'` 计数）。
    """
    (PASS if ok else FAIL).append(name)
    mark = "[PASS]" if ok else "[FAIL]"
    emoji = "✅" if ok else "❌"
    print(f"  {mark} {emoji} {name}" + (f" —— {detail}" if detail else ""))


def _codes(rows: list[dict]) -> list[str]:
    return [str(r.get("code")) for r in rows]


def _run_a_class() -> None:
    """A1/A2：权威提取 ⇒ 恰好 13 只、零误伤。"""
    print("--- A1/A2 权威提取（真机夹具）---")
    rows = llm_final.extract_authority_rejections(FIXTURE)
    codes = _codes(rows)
    got = set(codes)
    check("A1 权威提取 ⇒ 恰好 13 只", len(codes) == 13, f"实得 {len(codes)} 只：{codes}")
    check("A1b 权威提取 ⇒ 与期望集合逐个相等（不多不少）", got == set(EXPECTED_13),
          f"多={sorted(got - set(EXPECTED_13))} 缺={sorted(set(EXPECTED_13) - got)}")
    hit_protected = sorted(got & PROTECTED)
    check("A2 零误伤：515450/515650/512800 不得被否", not hit_protected,
          f"误伤 {hit_protected}" if hit_protected else "三只均未被误判")


def _run_b_class() -> None:
    """A3：护栏 load-bearing —— 观点段落单独喂解析器确实会命中保护组。"""
    print("--- A3 观点段落没有否决权（护栏 load-bearing）---")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    opinion_chunks = [
        ("多头", payload["investment_debate"]["bull_history"]),
        ("空头", payload["investment_debate"]["bear_history"]),
        ("激进派", payload["risk_debate"]["aggressive_history"]),
        ("保守派", payload["risk_debate"]["conservative_history"]),
        ("中立派", payload["risk_debate"]["neutral_history"]),
    ]
    opinion_codes: set[str] = set()
    for title, body in opinion_chunks:
        if not body:
            continue
        for row in llm_final.extract_rejections(f"## {title}\n{body}"):
            opinion_codes.add(str(row.get("code")))
    leaked = sorted(opinion_codes & PROTECTED)
    check("A3 观点段落单独解析确实会命中保护组（证明 A2 不是硬编码蒙出来的）",
          bool(leaked), f"观点段落命中保护组 {leaked}" if leaked else "⚠️ 观点段落已不再命中 ⇒ 保护组可能已失效")


def _run_c_class() -> None:
    """A4：`.md` 兜底路径同样正确。"""
    print("--- A4 .md 兜底路径 ---")
    md_text = ("# TradingAgents 决策纪要\n\n"
               "## 二、研究经理结论（Research Manager）\n"
               + json.loads(FIXTURE.read_text(encoding="utf-8"))["investment_plan"]
               + "\n\n## 三、多空辩论要点\n\n空头：暂不买入 `515450`、`515650`。\n"
               "\n## 四、风险辩论\n\n激进派：清仓512800，等于放弃一个相对强势资产。\n"
               "\n## 六、最终交易决定（原始输出）\n"
               + json.loads(FIXTURE.read_text(encoding="utf-8"))["final_trade_decision"]
               + "\n")
    tmp = Path("/tmp/etf_veto_scope_md.md")
    tmp.write_text(md_text, encoding="utf-8")
    try:
        codes = _codes(llm_final.extract_authority_rejections(tmp))
        check("A4 .md 兜底 ⇒ 恰好 13 只", len(codes) == 13, f"实得 {len(codes)} 只：{codes}")
        check("A4b .md 兜底 ⇒ 零误伤", not (set(codes) & PROTECTED),
              f"误伤 {sorted(set(codes) & PROTECTED)}")
    finally:
        tmp.unlink(missing_ok=True)


def _run_d_class() -> None:
    """A5：变异反证 —— 否决结果必须**跟着权威段落内容走**（不是硬编码名单）。"""
    print("--- A5 变异反证 ---")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _mutated(research_plan: str) -> dict:
        """只留「研究经理」一段有裁决权，其余权威段清空。"""
        out = dict(payload)
        out["investment_plan"] = research_plan
        out["investment_debate"] = {**payload["investment_debate"], "judge_decision": ""}
        out["risk_debate"] = {**payload["risk_debate"], "judge_decision": ""}
        out["trader_investment_plan"] = ""
        out["final_trade_decision"] = ""
        return out

    tmp = Path("/tmp/etf_veto_scope_mutated.json")
    try:
        # 变体①：权威段说「买 8%」⇒ 不得被否
        buy = _mutated("| `563020` | **买** | **8%** | 复权后动量强 |")
        tmp.write_text(json.dumps(buy, ensure_ascii=False), encoding="utf-8")
        codes_buy = set(_codes(llm_final.extract_authority_rejections(tmp)))
        check("A5 变异①：权威段把 563020 改成「买 8%」⇒ 不再被否", "563020" not in codes_buy,
              "563020 仍在否决集里（断言可能靠硬编码名单）" if "563020" in codes_buy
              else "563020 已随权威段落变化而退出否决集")

        # 变体②（正反对照）：权威段说「0 仓观察」⇒ 必须仍被否
        hold = _mutated("| `563020` | **持（0仓观察）** | 0% | 数据口径冲突 |")
        tmp.write_text(json.dumps(hold, ensure_ascii=False), encoding="utf-8")
        codes_hold = set(_codes(llm_final.extract_authority_rejections(tmp)))
        check("A5b 变异②（正反对照）：权威段说「0 仓观察」⇒ 仍被否", "563020" in codes_hold,
              "563020 未被否（⇒ 变体机制本身失效，A5 变成空断言）" if "563020" not in codes_hold
              else "563020 按权威段落被正确否决")
    finally:
        tmp.unlink(missing_ok=True)


def _run_e_class() -> None:
    """A6：Fail-safe；A7：`extract_rejections` 本体语义未变。"""
    print("--- A6 Fail-safe / A7 本体语义 ---")
    try:
        rows = llm_final.extract_authority_rejections(None)
        rows += llm_final.extract_authority_rejections("/tmp/不存在的决策件_20261111.md")
        rows += llm_final.extract_authority_rejections("/tmp/损坏的决策件_20261111.json")
        check("A6 决策件缺失/损坏 ⇒ 返回空且不抛异常", rows == [], f"实得 {rows}")
    except Exception as exc:  # noqa: BLE001
        check("A6 决策件缺失/损坏 ⇒ 返回空且不抛异常", False, f"抛异常：{exc}")

    hit = llm_final.extract_rejections("剔除 `512170` 医疗ETF华宝，不建仓。")
    miss = llm_final.extract_rejections("保留 `515450`，一票否决权不下放。")
    check("A7 extract_rejections 本体语义未变（正例命中/反例不命中）",
          [str(r.get("code")) for r in hit] == ["512170"]
          and not [r for r in miss if str(r.get("code")) == "515450"],
          f"正例={[r.get('code') for r in hit]} 反例={[r.get('code') for r in miss]}")


def main() -> int:
    """跑全部断言并汇总（退出码 0 = 全过，1 = 有 FAIL）。"""
    print("=== 🛡️ 一票否决作用域 · 闸门探针 ===")
    _run_a_class()
    _run_b_class()
    _run_c_class()
    _run_d_class()
    _run_e_class()
    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    if FAIL:
        print("FAIL：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
