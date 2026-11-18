"""「维持」行（被 TA 一票否决但**仍在持仓**）必须**也有**执行说明。

🔴 2026-11-11 审计缺陷 B（真实潜伏缺陷，本轮才暴露出来）
------------------------------------------------------------
`probe_execution_date` 的 **P9** 要求**每一条** action 的 `execution_note` 同时写
「信号日」与「执行日」；而旧实现里「维持」行在 `execution_note` **赋值之前**就
`continue` 了 ⇒ 一旦 TA 一票否决了**在持仓**的标的，计划就**缺执行说明** ⇒ P9 必红。

⚠️ 之所以长期没暴露：本轮「维持」行数 = **0**（TA 只否决了未持仓候选）
⇒ 缺陷**潜伏**，直到某轮 TA 否决了持仓才会炸 —— 且炸在**交付闸门**上
（跑批时不出声 ⇒ 极难排查）。

⇒ 本测试用**真实的 `build_plan` 路径**（合成夹具）复现「TA 否决在持仓标的」，
   断言那一行：
   ① `action == "维持"`、`ta_rejected is True`、**delta_shares == 0**（既不买也不卖；
      **严格等于 0，不接受 None**）；
   ② `execution_note` **存在**，且同时含「信号日」与「执行日」**两个关键词**
      **和两个具体日期**（信号日 `2026-11-09` / 执行日 `2026-12-01`），
      且两个日期**不许错位**（信号日位置写执行日 ⇒ 一样要红）；
   ③ 送进 `probe_execution_date.evaluate` ⇒ **P9 必须 PASS**（闸门与生产同口径）。
另附**反证**：把 `execution_note` 抹掉 ⇒ P9 必须 FAIL（证明闸门有牙，不是空过）。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import final_rebalance_plan as frp  # noqa: E402
from manu.etf_rotation.probes import probe_execution_date as ped  # noqa: E402

_HELD = "512800"
#: 🔴 断言必须**精确到日期**（只查「信号日」「执行日」两个关键词 ⇒ 写错日期也照样 PASS）。
#:    这两个日期是**夹具给的权威日期**（`next_rebalance.signal_date` / `.execution_date`），
#:    生产实现从 `next_rebalance` 取数、**不许硬编码** ⇒ 断言直接钉住这两个值。
_SIGNAL_DATE = "2026-11-09"   # 信号日（重算股数的口径日）
_EXEC_DATE = "2026-12-01"     # 执行日（实际下单的口径日）


def _fixture(tmp_path: Path) -> types.SimpleNamespace:
    """合成一份「账户持有 512800、TA 一票否决它」的报告 + 决策件。"""
    report = {
        "meta": {
            "asof": _SIGNAL_DATE,
            "params": {"commission_bp": 1.5, "slippage_bp": 5.0,
                       "pool_mode": "e_covered"},
        },
        "current_holdings_diagnosis": {
            "total_assets": 30035.57,
            "cash": 23367.37,
            "holdings": [{
                "code": _HELD, "name": "银行ETF华宝", "shares": 7700,
                "market_value": 6668.20, "cost": 1.0, "e_score": 0.4177,
                "ma20_ok": True, "ma60_ok": True, "mom120": 0.1288,
            }],
        },
        "next_rebalance": {"signal_date": _SIGNAL_DATE, "target_date": _EXEC_DATE,
                           "execution_date": _EXEC_DATE},
        "recommendation": {"actions": [{
            "code": _HELD, "name": "银行ETF华宝", "action": "持有",
            "target_weight": 0.16, "reason": "规则层：银行主题唯一入选",
        }]},
        "candidates": [{
            "code": _HELD, "name": "银行ETF华宝", "final_score": 0.9,
            "e_score": 0.4177, "coverage": 1.0, "theme_id": "银行",
            "mom20": 0.05, "mom60": 0.06, "mom120": 0.1288, "broken": False,
        }],
        "filter_stats": {"n_candidate_pool": 1, "universe": 1664, "liquidity": 292},
    }
    (tmp_path / "etf_analysis_report.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "etf_prices.csv").write_text(
        f"code,date,close\n512800,{_SIGNAL_DATE},0.866\n512800,2026-11-06,0.860\n",
        encoding="utf-8")
    # 🔴 否决动词必须用 `llm_final._REJECT_VERBS` 词表内的词（清仓/卖出**不在**里面
    #    ⇒ 那是「方向词」，不是「否决词」⇒ 断言式测试要跟生产口径一致）。
    (tmp_path / "tradingagents_decision.json").write_text(json.dumps({
        "status": "ok", "signal": "Underweight",
        "final_trade_decision": (
            "## 最终交易决定\n\n"
            "| 标的 | 动作 | 目标权重 | 理由 |\n|---|---|---|---|\n"
            "| 512800 | 不配置 | 0% | 组合银行暴露已足，不予考虑 |"
        ),
        "meta": {"context_sha1": "0" * 40},
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "tradingagents_decision.md").write_text(
        "# 决策\n\n## 最终交易决定\n\n不配置 512800\n", encoding="utf-8")
    return types.SimpleNamespace(
        report=str(tmp_path / "etf_analysis_report.json"),
        decision=str(tmp_path / "tradingagents_decision.md"),
        out_dir=str(tmp_path),
        prices=str(tmp_path / "etf_prices.csv"),
        universe=str(tmp_path / "etf_universe.csv"),
        account=None,
    )


def _build(args) -> dict:
    return frp.build_plan(args)


def _p9(plan: dict) -> tuple[bool, str]:
    """跑真实闸门里的 P9（`probe_execution_date.evaluate`）⇒ 只取 P9 那一项。"""
    rows = ped.evaluate(plan, "# 计划\n", {}, [])
    for name, ok, detail in rows:
        if name.startswith("P9"):
            return ok, detail
    raise AssertionError("P9 断言不见了（探针结构变了 ⇒ 本测试要跟着改）")


def test_维持行_必须有执行说明_且含信号日与执行日(tmp_path: Path):
    plan = _build(_fixture(tmp_path))
    rows = {str(a.get("code")): a for a in (plan.get("actions") or [])}
    assert _HELD in rows, f"计划里没有 {_HELD}：{list(rows)}"
    row = rows[_HELD]
    assert row.get("action") == "维持"
    assert row.get("ta_rejected") is True
    # 🔴 严格等于 0：**不接受 None**（旧写法 `int(delta_shares or 0) == 0` 把 None 当 0 放过
    #    ⇒ 目标股数算不出来时「维持行 delta=0」这条契约就形同虚设）。
    assert row.get("delta_shares") == 0, (
        f"维持行 delta 必须严格等于 0（既不买也不卖）：实为 {row.get('delta_shares')!r}"
    )
    note = str(row.get("execution_note") or "")
    assert note, "🔴 维持行缺 execution_note ⇒ probe_execution_date 的 P9 必红"
    assert "信号日" in note and "执行日" in note, note
    # 🔴 精确到**日期**：只查关键词 ⇒ 「信号日写成执行日那个日期」也照样 PASS。
    assert _SIGNAL_DATE in note, f"执行说明里没有信号日 {_SIGNAL_DATE}：{note}"
    assert _EXEC_DATE in note, f"执行说明里没有执行日 {_EXEC_DATE}：{note}"
    # 🔴 两个日期不许**错位**：信号日位置必须是信号日、执行日位置必须是执行日
    #    （生产实现曾把调仓节点硬编码成执行日 ⇒ 这正是要防的那类错）。
    _sig_part = note.split("信号日", 1)[1].split("执行日", 1)[0]
    _exe_part = note.split("执行日", 1)[1]
    assert _SIGNAL_DATE in _sig_part, f"「信号日」后面写的不是 {_SIGNAL_DATE}：{note}"
    assert _EXEC_DATE in _exe_part, f"「执行日」后面写的不是 {_EXEC_DATE}：{note}"


def test_维持行_送进真实闸门_P9必须PASS(tmp_path: Path):
    plan = _build(_fixture(tmp_path))
    ok, detail = _p9(plan)
    assert ok, detail


def test_反证_抹掉维持行的执行说明_P9必须FAIL(tmp_path: Path):
    """闸门有牙：同一份计划，只抹掉执行说明 ⇒ P9 必须转红。"""
    plan = _build(_fixture(tmp_path))
    for row in plan.get("actions") or []:
        if str(row.get("code")) == _HELD:
            row["execution_note"] = ""
    ok, detail = _p9(plan)
    assert not ok, "P9 没拦住缺执行说明的行 ⇒ 闸门失效"
    assert _HELD in detail


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
