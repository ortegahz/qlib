"""🔴 2026-11-09 新增：LLM 最终裁决的「部分卖出 ≠ 清仓」回归。

事故来源（服务器通宵实跑，服务器 `probe_llm_parser` A20⑥ 红灯）：
  LLM 决策 md 同时写了
    「- 512800减持约**2,600份**，保留约**5,100份** …」
    「- 卖出512800**预计回笼**约**2,272.40元**；」
  ⇒ 旧口径：规则②不认「减持+数量」⇒ 方向被裸「卖出」规则抢走 ⇒ 目标权重写成 0%
  ⇒ 与真实裁决「减至约 15%」矛盾。

本文件钉死三件事：
  ① 「减持/减仓 + 数量」⇒ 方向 = 「减」，target **不得**是 0；
  ② 「卖出 X **预计回笼** N 元」是**收益预估句** ⇒ **不得**产生 target=0 的卖出指令；
  ③ 真清仓（「全部卖出 / 清仓」）**仍必须**判「卖 + 0%」（不许被上两条误伤）。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "manu" / "etf_rotation"))

import llm_final as LF  # noqa: E402

_HEAD = "## 六、最终交易决定\n\n**Executive Summary**: "


def _targets(text: str) -> dict:
    return (LF.extract_final_decision(text) or {}).get("targets") or {}


def test_reduce_with_quantity_is_not_liquidation() -> None:
    """① 「减持约 N 份，保留约 M 份」⇒ 方向「减」，目标不得为 0。"""
    text = _HEAD + "对 900001 减持约 2,600 份，保留约 5,100 份，对应市值约 4,457.40 元。\n"
    tgt = _targets(text).get("900001")
    assert tgt is not None, "该子句必须抽出目标条目"
    assert tgt.get("direction") == "减", f"方向必须是「减」，实得 {tgt.get('direction')!r}"
    assert tgt.get("target_pct") is None, "「减」不给目标权重（不许猜），实得 %r" % (
        tgt.get("target_pct"),
    )


def test_reduce_with_quantity_is_noted_as_partial_sell() -> None:
    """🔴 规则②的**扩词**必须生效：带数量的「减持」要记成「部分卖出」这条 note。

    （旧词表只认「卖出/卖掉/减掉」⇒ 「减持 N 份」会落到规则③的笼统 note「原文写明减仓」，
      **方向虽对、证据口径却丢了** ⇒ 报告里的证据句会变模糊。）
    """
    text = _HEAD + "对 900007 减持约 2,600 份，保留约 5,100 份。\n"
    tgt = _targets(text).get("900007") or {}
    note = str(tgt.get("note") or "")
    assert "部分卖出" in note, f"带数量的减仓必须记成「部分卖出」，实得 note={note!r}"


def test_proceeds_estimate_clause_is_not_a_sell_instruction() -> None:
    """② 「卖出 X 预计回笼 N 元」⇒ 只是收益预估，不得判成「卖 + 0%」。"""
    text = _HEAD + "卖出900001预计回笼约2,272.40元。\n"
    tgt = _targets(text).get("900001")
    assert tgt is None or tgt.get("target_pct") is None, (
        "收益预估句不得产生 target=0 的卖出指令，实得 %r" % (tgt,)
    )
    assert tgt is None or tgt.get("direction") != "卖", (
        "收益预估句不得判成「卖」，实得 %r" % (tgt.get("direction"),)
    )


def test_real_liquidation_still_zero() -> None:
    """③ 真清仓仍判「卖 + 0%」（不许被 ①② 误伤）。"""
    text = _HEAD + "全部卖出 900002 回收资金；对 900003 卖出约 1,234 份，保留约 5,678 份。\n"
    targets = _targets(text)
    full = targets.get("900002") or {}
    assert full.get("direction") == "卖", f"实得 {full.get('direction')!r}"
    assert full.get("target_pct") is not None and float(full["target_pct"]) == 0.0
    part = targets.get("900003") or {}
    assert part.get("direction") == "减", f"实得 {part.get('direction')!r}"
    assert part.get("target_pct") is None
