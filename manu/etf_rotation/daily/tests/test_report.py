"""test_report.py —— `daily/report.py` 的**离线**单测。

【为什么全离线】
报告渲染器是**纯函数**（不联网、不读行情、不 import qlib）⇒ 可以用**合成数据**
把「缺字段」「破位」「调仓卡」三条路径都钉死，不依赖服务器上的任何产物。

【钉死的纪律】
* 缺失字段渲染成「—」，整篇报告**不出现** ``None`` / ``nan`` 字样；
* 百分比按小数口径渲染（``+3.93%``），金额带千分位（``30,035.57``）；
* 模式 A 的「构造性保证」文案必须出现在报告里。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import report  # noqa: E402


# =============================================================================
# 合成数据
# =============================================================================
def _mode_a_ok() -> dict:
    """模式 A · 趋势完好（数值取自 2026-11-05 实测口径）。"""
    return {
        "mode": "A",
        "asof": "2026-11-05",
        "status": "ok",
        "holding": {
            "code": "512800",
            "name": "银行ETF华宝",
            "shares": 7700,
            "cost_price": 0.8239,
            "price": 0.866,
            "market_value": 6668.20,
            "pnl_pct": 0.0511,
        },
        "account": {
            "snapshot_date": "2026-11-05",
            "total_assets": 30035.57,
            "cash": 23367.37,
            "cash_weight": 0.778,
            "market_value": 6668.20,
            "market_value_weight": 0.222,
            "discrepancy_note": "⚠️ 账户口径披露：用户口径里现价与市值相差 7.70 元，未做抹平。",
        },
        "ma": {
            "code": "512800",
            "close": 0.8740,
            "close_date": "2026-11-05",
            "ma20": 0.8650,
            "ma60": 0.8410,
            "above_ma20": True,
            "above_ma60": True,
            "distance_to_ma60_pct": 0.0393,
            "safety_buffer_amount": 254.23,
            "safety_buffer_pct": 0.0393,
            "n_obs": 120,
        },
        "action": None,
        "email_kind": "daily_ok",
        "notes": [
            "✅ 趋势完好，守纪律躺平：收盘 0.8740 高于 MA60 0.8410（安全垫 +3.93%）",
            "🔴 模式 A 是构造性保证：路径内没有打分 / 选股 / LLM 调用",
        ],
        "generated_at": "2026-11-05T15:31:12",
    }


def _mode_a_broken() -> dict:
    """模式 A · 跌破 MA60（触发紧急平仓）。"""
    base = _mode_a_ok()
    base["status"] = "broken"
    base["ma"].update({
        "close": 0.8000,
        "ma60": 0.8410,
        "above_ma60": False,
        "distance_to_ma60_pct": -0.0487,
        "safety_buffer_amount": -315.70,
        "safety_buffer_pct": -0.0487,
    })
    base["action"] = {
        "action": "紧急平仓",
        "kind": "emergency_liquidation",
        "code": "512800",
        "name": "银行ETF华宝",
        "shares": 7700,
        "trigger_price": 0.8000,
        "ma60": 0.8410,
        "break_pct": -0.0487,
        "reason": "收盘价 0.8000 有效跌破 MA60（0.8410），跌破幅度 -4.87%",
        "instruction": "下一交易时段清掉 7700 股 银行ETF华宝（参考价 0.8000 元/股）",
    }
    base["email_kind"] = "emergency_break"
    base["notes"] = [
        "🔴 紧急避险：512800 收盘 0.8000 < MA60 0.8410（-4.87%）⇒ 已生成紧急平仓指令",
    ]
    return base


def _mode_a_missing() -> dict:
    """模式 A · 字段大面积缺失（样本不足 / 账户不完整）。"""
    return {
        "mode": "A",
        "asof": "2026-11-05",
        "status": "insufficient_data",
        "holding": {"code": "512800"},
        "account": {"snapshot_date": "2026-11-05"},
        "ma": {
            "code": "512800",
            "close": None,
            "close_date": None,
            "ma20": None,
            "ma60": None,
            "above_ma20": None,
            "above_ma60": None,
            "distance_to_ma60_pct": None,
            "safety_buffer_amount": None,
            "safety_buffer_pct": None,
            "n_obs": 0,
        },
        "action": None,
        "notes": ["⚠️ 样本不足：MA60 需要 60 根收盘价，实际只有 0 根 ⇒ 不做破位判定"],
        "generated_at": None,
    }


def _mode_b_result() -> dict:
    """模式 B · 完整执行卡（数值取自 2026-11-04 dry 实测口径）。"""
    orders = [
        {
            "code": "512800", "name": "银行ETF华宝", "action": "卖出",
            "current_shares": 7700, "price": 0.8740, "price_date": "2026-11-04",
            "target_shares": 6800, "delta_shares": -900, "target_amount": 5943.20,
            "est_amount": -786.60, "est_fee": 5.79, "est_slippage": 0.08,
            "capped": False, "cap_note": "", "reason": "个股穿透景气度转负 ⇒ 减仓",
        },
        {
            "code": "563020", "name": "红利低波ETF", "action": "买入",
            "current_shares": 0, "price": 1.2110, "price_date": "2026-11-04",
            "target_shares": 4900, "delta_shares": 4900, "target_amount": 5933.90,
            "est_amount": 5933.90, "est_fee": 5.00, "est_slippage": 0.59,
            "capped": True,
            "cap_note": "目标金额 6200.00 元 > 单只上限 6000.00 元 ⇒ 股数按上限缩减到 4900 股",
            "reason": "站上 MA20 且景气度排第一",
        },
        {
            "code": "510880", "name": "红利ETF", "action": "持有",
            "current_shares": 1000, "price": 3.4560, "price_date": "2026-11-04",
            "target_shares": 1000, "delta_shares": 0, "target_amount": 3456.00,
            "est_amount": 0.0, "est_fee": 0.0, "est_slippage": 0.0,
            "capped": False, "cap_note": "", "reason": "维持",
        },
    ]
    unbuildable = [
        {
            "code": "511660", "name": "货币ETF", "action": "不可建仓",
            "target_shares": 0, "price": 100.0100, "price_date": "2026-11-04",
            "one_lot_amount": 10001.00, "max_amount": 6000.00,
            "reason": "一手（100 份 × 100.0100 元 = 10001.00 元）> 单只上限 6000.00 元",
        },
    ]
    card = {
        "asof": "2026-11-04",
        "execution_date": "2026-11-05",
        "execution_lag_days": 1,
        "execution_basis": "信号日 T 收盘出信号 → 执行日 T+1 开盘执行",
        "price_basis_note": "节点日收盘价 ≠ 执行日成交价，实际股数/金额可能不同",
        "account": {
            "total_assets": 30035.57,
            "cash": 23367.37,
            "cash_weight": 0.778,
            "market_value": 6668.20,
        },
        "orders": orders,
        "post_trade": {
            "cash": 18215.70,
            "equity_amount": 11820.00,
            "equity_weight": 0.3935,
            "positions": [
                {"code": "512800", "name": "银行ETF华宝", "shares": 6800,
                 "market_value": 5943.20, "weight": 0.1978, "note": ""},
                {"code": "563020", "name": "红利低波ETF", "shares": 4900,
                 "market_value": 5933.90, "weight": 0.1975},
                {"code": "510880", "name": "红利ETF", "shares": 1000,
                 "market_value": 3456.00, "weight": 0.1150},
            ],
            "cash_buffer": 18215.70,
            "cash_buffer_ok": True,
        },
        "cash_flow": {
            "cash_begin": 23367.37,
            "sell_proceeds": 786.60,
            "sell_fees": 5.79,
            "buy_amount": 5933.90,
            "buy_fees": 5.00,
            "slippage": 0.67,
            "cash_end": 18215.70,
            "deficit": 0.0,
            "surplus": 18208.70,
            "available_before_buy": 24148.18,
            "headroom": 18208.70,
            "feasible": True,
            "unbuildable_slots": unbuildable,
            "scaled_down": [],
        },
        "warnings": [
            "⚠️ S 层打分日 2026-11-03 早于信号日 2026-11-04 ⇒ 穿透景气度可能滞后",
        ],
        "disclaimer": "本报告由本地流水线生成，不构成投资建议。",
        "constraints": {
            "target_positions": 5, "max_position_amount": 6000.0, "lot_size": 100,
            "cash_buffer_min": 200.0, "cash_buffer_max": 300.0,
        },
    }
    return {
        "mode": "B",
        "asof": "2026-11-04",
        "execution_date": "2026-11-05",
        "run_dir": "/mnt/data/siping/runs/etf_rotation/daily/20261104",
        "report_json": "/mnt/data/siping/runs/etf_rotation/daily/20261104/etf_analysis_report.json",
        "plan_json": "/mnt/data/siping/runs/etf_rotation/daily/20261104/final_rebalance_plan.json",
        "node_json": "/mnt/data/siping/runs/etf_rotation/daily/20261104/node_reprice_20261104.json",
        "card_json": (
            "/mnt/data/siping/runs/etf_rotation/daily/20261104/execution_card_20261104.json"
        ),
        "card": card,
        "email_kind": "rebalance_card",
        "notes": [
            "✅ 分析报告已产出（分析日 = 2026-11-03）",
            "✅ TradingAgents 决策已产出（status = ok）",
        ],
        "analysis_asof": "2026-11-03",
        "scores_max_date": "2026-11-03",
        "score_grid_last": "2026-11-03",
        "qlib_dataset_max_date": "2026-11-03",
        "llm_decision_status": "ok",
        "execution_lag_days": 1,
        "generated_at": "2026-11-04T16:02:44",
    }


# =============================================================================
# 测试
# =============================================================================
def test_mode_a_ok_report_says_lie_flat():
    text = report.render_report(_mode_a_ok())
    assert "None" not in text
    assert "nan" not in text
    assert "趋势完好，守纪律躺平" in text
    assert "不产生任何买卖指令" in text
    assert "## 均线安全带" in text
    assert "## 账户卡片" in text
    assert "## 数据口径与备注" in text


def test_mode_a_ok_formats_pct_and_money():
    text = report.render_report(_mode_a_ok())
    assert "+3.93%" in text
    assert "30,035.57" in text
    assert "23,367.37" in text
    assert "0.8740" in text
    assert "254.23" in text


def test_mode_a_broken_report_has_emergency_instruction():
    text = report.render_report(_mode_a_broken())
    assert "已跌破 MA60，需紧急平仓" in text
    assert "紧急平仓" in text
    assert "0.8000" in text
    assert "7700" in text
    assert "-4.87%" in text


def test_mode_a_missing_fields_render_placeholder():
    text = report.render_report(_mode_a_missing())
    assert "样本不足，不做破位判定" in text
    assert "—" in text
    assert "None" not in text
    assert "nan" not in text


def test_mode_a_account_uses_account_argument_as_fallback():
    result = _mode_a_ok()
    result["account"] = {"snapshot_date": "2026-11-05"}
    text = report.render_report(result, account=_mode_a_ok()["account"])
    assert "30,035.57" in text
    assert "23,367.37" in text


def test_mode_b_report_has_all_sections():
    text = report.render_report(_mode_b_result())
    assert "None" not in text
    assert "nan" not in text
    for header in (
        "# ETF 月度调仓决策卡",
        "## 执行摘要",
        "## 六阶段流水线状态",
        "## 买入清单",
        "## 卖出清单",
        "## 无动作（维持）",
        "## 不可建仓（碎屑归现金）",
        "## 调仓后仓位分布",
        "## 现金流对账",
        "## 风险披露",
        "## 挂单清单（次日开盘人工执行）",
    ):
        assert header in text, header
    for stage in ("阶段 1", "阶段 2", "阶段 3", "阶段 4", "阶段 5", "阶段 6"):
        assert stage in text, stage


def test_mode_b_discloses_score_date():
    text = report.render_report(_mode_b_result())
    assert "打分日 = 2026-11-03" in text
    assert "qlib 数据集覆盖末日 = 2026-11-03" in text
    assert "18,215.70" in text
    assert "563020" in text
    assert "512800" in text
    assert "511660" in text
    assert "10,001.00" in text


def test_mode_b_shows_cap_note_and_ordering():
    text = report.render_report(_mode_b_result())
    # capped=True ⇒ cap_note 必须出现
    assert "单只上限 6000.00 元" in text
    # 买入单在买入清单、卖出单在卖出清单
    buy_section = text.split("## 买入清单")[1].split("## 卖出清单")[0]
    sell_section = text.split("## 卖出清单")[1].split("## 无动作")[0]
    assert "563020" in buy_section
    assert "512800" not in buy_section
    assert "512800" in sell_section
    assert "563020" not in sell_section


def test_mode_b_card_missing_does_not_crash():
    result = _mode_b_result()
    result["card"] = None
    text = report.render_report(result)
    assert "没有执行卡" in text
    assert "None" not in text
    assert "nan" not in text


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        report.render_report({"mode": "C"})
    with pytest.raises(TypeError):
        report.render_report(None)  # type: ignore[arg-type]


def test_write_report_creates_parent_dir(tmp_path: Path):
    out = tmp_path / "nested" / "deeper" / "report_20261105.md"
    written = report.write_report("# hello\n", out)
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# hello\n"
