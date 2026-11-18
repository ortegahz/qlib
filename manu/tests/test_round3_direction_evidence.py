"""🔴 2026-11-11 新增：审计 P1-1「**na 被当成 PASS**」的根因回归。

事故链（2026-11-11 审计实测，交付快照 `20261111_163156`）：

  ① TA 的否决表里方向格写的是 **复合写法**：
         | **515020** | **不买，否决同族替代** | 与512800高度重合… |
     旧口径要求方向格**整格精确等于**词表词 ⇒ 这类格子被当成「**没有方向证据**」
     ⇒ 该代码进 `unjudged` ⇒ 生产闸门 `find_ta_reject_mis_hits` 判 **status="na"**；
  ② 生产闸门自己写明「**不适用 ≠ 通过**（无法校验 ≠ 可以放过）」，
     且闸门运行器把 `na` 单列为 `[N/A]`、**不进 PASS 计数**；
  ③ 但**验收脚本**把 `na` 记成了 PASS（`else: reporter.ok(...)`）
     ⇒ 汇总 FAIL=0、退出码 0 ⇒ **假绿**（判不了 ⇒ 被当成过了）。

本文件钉死两件事：

  ① **根因**：复合方向格里的**非买侧**方向词必须被认出来
     （`不买，否决同族替代` ⇒ `不买`）⇒ 该代码**判得了**且判为**非买侧**
     ⇒ `unjudged` 清零 ⇒ 闸门判**真 pass**（而不是 na）；
  ② **护栏不许被顺手放宽**：**买侧**词在复合格里**仍然不认**
     （`买，但仅在数据复核通过后` 是**附条件**意见 ⇒ 不得凭空造出买侧证据
      ⇒ 否则真否决会被误判成误伤）。

自带夹具，**不依赖服务器产物**。
"""

from __future__ import annotations

import pathlib

from manu.etf_rotation.probes import probe_verify_guards as PG  # type: ignore  # noqa: E402


# ---- 最小夹具：一张 TA 否决表（复合方向格）+ 一张买侧表 ----
_DECISION_MD = """## 六、最终交易决定

| 标的 | 结论 | 理由 |
| --- | --- | --- |
| **515020** | **不买，否决同族替代** | 与 512800 高度重合 |
| **516310** | **不买，否决同族替代** | 同族替代 |
| **512820** | **买入** | 独立逻辑 |
"""


def _plan(rejected: list[str], buy_codes: list[str]) -> dict:
    return {
        "ta_screening": {"enabled": True, "rejected": rejected},
        "llm_targets": {code: {"direction": "买"} for code in buy_codes},
    }


def test_cell_direction_recognizes_non_buy_compound():
    """复合方向格里的**非买侧**词必须认出来（根因）。"""
    assert PG._classify_cell_direction("不买，否决同族替代") == "不买"
    assert PG._classify_cell_direction("卖出，清仓") == "卖出"
    assert PG._classify_cell_direction("减持") == "减持"


def test_cell_direction_does_not_invent_buy_side():
    """**买侧**词在复合格里**不许**被认出来（附条件买 ⇒ 不算买侧证据）。"""
    assert PG._classify_cell_direction("买，但仅在数据复核通过后") is None
    assert PG._classify_cell_direction("买入，条件性加仓") is None
    # 整格精确命中仍然照旧（不回归）
    assert PG._classify_cell_direction("买入") == "买入"
    assert PG._classify_cell_direction("Overweight") == "Overweight"


def test_reject_codes_with_compound_direction_are_judged_not_unjudged():
    """否决代码的方向格是复合写法 ⇒ **判得了**（unjudged 清零）⇒ 闸门判真 pass。"""
    verdict = PG.find_ta_reject_mis_hits(
        _plan(rejected=["515020", "516310"], buy_codes=[]), _DECISION_MD
    )
    assert verdict["status"] == "pass", verdict
    assert verdict["unjudged"] == [], verdict
    assert verdict["mis_hits"] == [], verdict


def test_buy_side_rejected_code_is_still_caught():
    """**护栏不许被放宽**：否决名单里混进买侧标的 ⇒ 必须判 fail。"""
    verdict = PG.find_ta_reject_mis_hits(
        _plan(rejected=["515020", "512820"], buy_codes=[]), _DECISION_MD
    )
    assert verdict["status"] == "fail", verdict
    assert [hit["code"] for hit in verdict["mis_hits"]] == ["512820"], verdict


def test_plan_side_direction_still_used():
    """计划件里的方向也是证据（买侧 ⇒ 误伤）。"""
    verdict = PG.find_ta_reject_mis_hits(
        _plan(rejected=["515020"], buy_codes=["515020"]), _DECISION_MD
    )
    assert verdict["status"] == "fail", verdict
