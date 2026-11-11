#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🛡️ 一票否决**作用域**与**节点日重算**的回归钉子（2026-11-11 审计轮）。

【钉的是什么】
① **节点日重算不得推翻「TA 一票否决 ⇒ 保持现仓」**
   （`reprice_node_day.py`：旧实现按节点日价重算目标股数 ⇒ delta ≠ 0
   ⇒ 生成**卖出挂单** ⇒ `--live-trade` 下会被当真）。
② **表格行的目标权重列**必须在「代码格之后、理由列之前」被看到
   （否则买行被误否：`| 563020 | 买 | 8% | 不买 512170 |`）。
③ **子句级代码继承**不得跨过「持有语义」子句
   （`512800 保留本仓，其余不考虑。` 不得判 512800）。
④ **词表**：「拒绝 / 不予考虑」是真否决；「不排除 / 不买也不卖」不是。
⑤ **`.md` 兜底路径**与 **JSON 路径**口径必须一致（都恰好 13 只、零误伤）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_final  # noqa: E402
import reprice_node_day  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ta_decision_real_20261110.json"

#: TA 最终裁决里「0 仓观察」那批 = **真实否决集**（13 只）。
EXPECTED_13: frozenset[str] = frozenset({
    "563020", "159207", "512170", "515020", "516310", "512820",
    "512890", "159547", "515100", "159307", "515180", "515080", "511990",
})
#: TA 最终裁决明确「买 / 减」⇒ **绝不能**被当成否决。
PROTECTED: frozenset[str] = frozenset({"515450", "515650", "512800"})


def _codes(text: str) -> list[str]:
    return [str(r.get("code")) for r in llm_final.extract_rejections(text)]


# ───────────────────────── ① 节点日重算：TA 维持行 ─────────────────────────
def _plan(ta_hold: bool) -> dict:
    act = {
        "code": "512800",
        "name": "银行ETF华宝",
        "action": "维持" if ta_hold else "卖出",
        "target_weight": 0.2254,
        "current_shares": 7700,
        "target_shares": 7700,
        "price": 0.874,
        "price_date": "2026-11-09",
    }
    if ta_hold:
        act["ta_rejected"] = True
        act["held_not_touched"] = True
    return {"actions": [act]}


def _recompute(ta_hold: bool) -> dict:
    return reprice_node_day._recompute(
        plan=_plan(ta_hold),
        prices={"512800": (0.900, "2026-12-01")},   # 节点日价 ≠ 快照价
        node_date="2026-12-01",
        total_assets=29854.67,
        cash=15518.87,
        current_shares={"512800": 7700},
        fee_rate=0.00065,
    )


def test_节点日重算_TA维持行_不得变成卖出():
    """TA 一票否决且仍在持仓 ⇒ 节点日重算后**仍然**不卖（delta=0、动作=维持）。"""
    row = _recompute(True)["actions"][0]
    assert row["target_shares"] == 7700, row
    assert row["delta_shares"] == 0, row
    assert row["action"] == "维持", row
    assert float(row["est_amount"]) == 0.0, row


def test_节点日重算_TA维持行_标记必须透传():
    """`ta_rejected` / `held_not_touched` 必须透传给执行卡分支（否则分支不生效）。"""
    row = _recompute(True)["actions"][0]
    assert row.get("ta_rejected") is True, row
    assert row.get("held_not_touched") is True, row


def test_节点日重算_非TA行_仍按节点日价重算():
    """🔴 反向护栏：**普通行**必须照常按节点日价重算（否则本修复会误伤正常行）。"""
    row = _recompute(False)["actions"][0]
    assert row["delta_shares"] != 0, "普通行不应被强制成维持"
    assert row["target_shares"] != 7700, row
    assert "ta_rejected" not in row, "普通行不应被写上 TA 标记"


# ───────────────────────── ② 表格行：目标权重列 ─────────────────────────
def test_表格行_买行的理由格提到别只_本行不得被否():
    """`| 563020 | 买 | 8% | 不买 512170 |` ⇒ 563020 豁免、512170 照否。"""
    got = _codes("| 563020 | 买 | 8% | 不买 512170，改配本只 |")
    assert "563020" not in got, got
    assert "512170" in got, got


def test_表格行_零仓观察行_理由列里的正百分比不得豁免():
    """理由列里出现正百分比**不得**把「0 仓观察」行豁免掉。"""
    got = _codes("| 563020 | **持（0仓观察）** | 0% | 不值得 20% 核心仓 |")
    assert "563020" in got, got


# ───────────────────────── ③ 子句级代码继承 ─────────────────────────
def test_散文_保留句后接其余不考虑_不得误伤():
    """`512800 保留本仓，其余不考虑。` ⇒ 不得判 512800。"""
    assert "512800" not in _codes("512800 保留本仓，其余不考虑。")


def test_散文_继续持有句后接其余不考虑_不得误伤():
    assert "512800" not in _codes("512800 继续持有，其余标的均不考虑。")


def test_散文_代码在上一子句_动词在下一子句_仍要命中():
    """🔴 反向护栏：跨子句继承**不能被整体关掉**（否则会漏判真否决）。"""
    assert "512800" in _codes("512800 银行ETF华宝，本轮建议剔除。")


# ───────────────────────── ④ 词表 ─────────────────────────
def test_词表_拒绝与不予考虑_是真否决():
    assert "512170" in _codes("拒绝 512170。")
    assert "512170" in _codes("不予考虑 512170。")


def test_词表_不排除与不买也不卖_不是否决():
    assert "512800" not in _codes("不排除 512800。")
    assert "512800" not in _codes("不买也不卖 512800。")


# ───────────────────────── ⑤ md 兜底路径与 JSON 路径口径一致 ─────────────────────────
def test_权威提取_JSON与md兜底_口径一致():
    """两条路径都必须是**恰好 13 只**、零误伤（审计发现 3）。"""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    md_text = (
        "# TradingAgents 决策纪要\n\n"
        "## 二、研究经理结论（Research Manager）\n" + payload["investment_plan"]
        + "\n\n## 三、多空辩论要点\n\n### 多头（Bull）\n多头：保留 515450。\n"
        + "\n### 空头（Bear）\n空头：暂不买入 `515450`、`515650`。\n"
        + "\n### 辩论裁决\n" + payload["investment_debate"]["judge_decision"]
        + "\n\n## 四、风险辩论\n\n### 激进派\n激进派：清仓512800，等于放弃一个相对强势资产。\n"
        + "\n### 风险裁决\n" + payload["risk_debate"]["judge_decision"]
        + "\n\n## 六、最终交易决定（原始输出）\n" + payload["final_trade_decision"]
        + "\n"
    )
    tmp = Path("/tmp/etf_test_authority_md.md")
    tmp.write_text(md_text, encoding="utf-8")
    try:
        md_codes = {str(r.get("code")) for r in llm_final.extract_authority_rejections(tmp)}
        json_codes = {str(r.get("code")) for r in llm_final.extract_authority_rejections(FIXTURE)}
    finally:
        tmp.unlink(missing_ok=True)
    assert md_codes == json_codes == set(EXPECTED_13), (md_codes, json_codes)
    assert not (md_codes & PROTECTED), sorted(md_codes & PROTECTED)
