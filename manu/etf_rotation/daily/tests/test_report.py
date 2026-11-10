"""test_report.py —— `daily/report.py` 的**离线**单测。

【为什么全离线】
报告渲染器是**纯函数**（不联网、不读行情、不 import qlib）⇒ 可以用**合成数据**
把「缺字段」「观察态」「实盘态」三条路径都钉死，不依赖服务器上的任何产物。

【钉死的纪律】
* 缺失字段渲染成「—」，整篇报告**不出现** ``None`` / ``nan`` 字样；
* 百分比按小数口径渲染（``+3.93%``），金额带千分位（``30,035.57``）；
* **观察态**（``live_trade=False``）的报告**不许**出现任何下单指令；
* **实盘态**（``live_trade=True``）的挂单清单**一行都不许退化**；
* Mode A/B/C 分诊**已下线** ⇒ ``"A"`` / ``"C"`` 必须抛 ``ValueError``（附迁移提示）。
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
def _pipeline_result(live_trade: bool = False) -> dict:
    """统一流水线 · 完整执行卡（数值取自 2026-11-04 dry 实测口径）。"""
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
        "mode": "unified",
        "live_trade": live_trade,
        "asof": "2026-11-04",
        "execution_date": "2026-11-05",
        "run_dir": "/mnt/data/siping/runs/etf_rotation_results/daily/20261104",
        "snapshot": {
            "root": "/mnt/data/siping/runs/etf_rotation_results/snapshots",
            "path": "/mnt/data/siping/runs/etf_rotation_results/snapshots/20261104_160244",
            "run_id": "20261104_160244",
        },
        "report_json": "/mnt/data/siping/runs/etf_rotation_results/daily/20261104/etf_analysis_report.json",
        "plan_json": "/mnt/data/siping/runs/etf_rotation_results/daily/20261104/final_rebalance_plan.json",
        "node_json": "/mnt/data/siping/runs/etf_rotation_results/daily/20261104/node_reprice_20261104.json",
        "card_json": (
            "/mnt/data/siping/runs/etf_rotation_results/daily/20261104/execution_card_20261104.json"
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
def test_unified_report_has_all_sections():
    text = report.render_report(_pipeline_result(live_trade=True), live_trade=True)
    assert "None" not in text
    assert "nan" not in text
    for header in (
        "# ETF 月度调仓决策卡",
        "## 快照与运行口径",
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


def test_unified_report_discloses_score_date():
    text = report.render_report(_pipeline_result(live_trade=True), live_trade=True)
    assert "打分日 = 2026-11-03" in text
    assert "qlib 数据集覆盖末日 = 2026-11-03" in text
    assert "18,215.70" in text
    assert "563020" in text
    assert "512800" in text
    assert "511660" in text
    assert "10,001.00" in text


def test_unified_report_shows_cap_note_and_ordering():
    text = report.render_report(_pipeline_result(live_trade=True), live_trade=True)
    # capped=True ⇒ cap_note 必须出现
    assert "单只上限 6000.00 元" in text
    # 买入单在买入清单、卖出单在卖出清单
    buy_section = text.split("## 买入清单")[1].split("## 卖出清单")[0]
    sell_section = text.split("## 卖出清单")[1].split("## 无动作")[0]
    assert "563020" in buy_section
    assert "512800" not in buy_section
    assert "512800" in sell_section
    assert "563020" not in sell_section


def test_unified_report_card_missing_does_not_crash():
    result = _pipeline_result(live_trade=True)
    result["card"] = None
    text = report.render_report(result, live_trade=True)
    assert "没有执行卡" in text
    assert "None" not in text
    assert "nan" not in text


def test_unknown_mode_raises():
    """🔴 2026-11-09：Mode A/B/C 分诊**已下线** ⇒ ``"A"`` / ``"C"`` 与乱填的
    ``"X"`` 一样是**非法值**；错误文案必须给**迁移提示**（不许写「未知模式」）。"""
    for bad in ("A", "C", "X"):
        with pytest.raises(ValueError) as excinfo:
            report.render_report({"mode": bad})
        message = str(excinfo.value)
        assert "未知模式" not in message
        assert "--live-trade" in message
        assert "--full" in message
    # 结果里根本没有 mode ⇒ 同样非法（统一流水线只有一种模式）
    with pytest.raises(ValueError):
        report.render_report({})
    with pytest.raises(TypeError):
        report.render_report(None)  # type: ignore[arg-type]


def test_legacy_mode_b_alias_is_treated_as_unified():
    """旧别名 ``"B"`` 兼容 ⇒ 视作 unified（不抛异常）。"""
    text = report.render_report(_pipeline_result(live_trade=True), mode="B")
    assert "# ETF 月度调仓决策卡" in text


def test_observation_mode_report_marks_order_disabled():
    """🔴 观察态（``live_trade=False``）的报告**不许**出现任何下单指令。

    实测事故（2026-11-06）：观察态报告里渲染出了
    「## 挂单清单（次日开盘人工执行）」＋「按本表在券商 App 手工挂单」
    ⇒ 顶部有警示块也没用（有人会单独截取那张表）⇒ 违反「**结构性不可下单**」。
    """
    text = report.render_report(_pipeline_result(live_trade=False), live_trade=False)
    assert "观察态" in text
    assert "挂单禁用" in text
    assert "挂单清单" not in text
    assert "手工挂单" not in text
    assert "券商 App" not in text
    assert "观察清单" in text
    assert "不可下单" in text


def test_live_trade_report_renders_order_instructions():
    """🔴 零回归：实盘态的**真**挂单清单必须原样保留（一行都不许退化）。"""
    text = report.render_report(_pipeline_result(live_trade=True), live_trade=True)
    assert "## 挂单清单（次日开盘人工执行）" in text
    assert "按本表在券商 App 手工挂单" in text
    assert "观察清单" not in text
    assert "观察态" not in text
    # 清单内容本身（含方向与股数）不许退化
    order_section = text.split("## 挂单清单（次日开盘人工执行）")[1]
    assert "563020" in order_section
    assert "512800" in order_section
    assert "+4900" in order_section
    assert "-900" in order_section


def test_observation_mode_report_has_no_legacy_mode_c_wording():
    """🔴 防回归：观察态报告**不许**再出现 Mode A/B/C 时代的旧词。"""
    text = report.render_report(_pipeline_result(live_trade=False), live_trade=False)
    for banned in ("MODE C", "影子", "shadow", "模式 C", "Mode C", "影子推演"):
        assert banned not in text, banned


def test_report_discloses_snapshot_and_live_trade():
    """🔴 快照目录 / live_trade 必须**显式披露**（缺 ⇒「—」，不许编造）。"""
    result = _pipeline_result(live_trade=False)
    text = report.render_report(result, live_trade=False)
    assert result["snapshot"]["path"] in text
    assert "快照目录" in text
    assert "live_trade" in text
    # 结果里没有 live_trade 字段 ⇒ 披露成「—」（不猜）
    bare = _pipeline_result(live_trade=False)
    bare.pop("live_trade")
    bare.pop("snapshot")
    bare_text = report.render_report(bare, live_trade=False)
    assert "| live_trade | — |" in bare_text
    assert "| 快照目录 | — |" in bare_text


def test_report_ranking_basis_is_copied_from_result():
    """🔴 排序依据口径**照抄 result 里的真实值**（不许写死常量）。"""
    result = _pipeline_result(live_trade=True)
    result["score_smoothing"] = {
        "ranking_basis": "EWMA 平滑分（alpha=0.35，窗口 5 交易日）",
    }
    text = report.render_report(result, live_trade=True)
    assert "排序依据：EWMA 平滑分（alpha=0.35，窗口 5 交易日）" in text

    # 只有原始参数（没有 ranking_basis）⇒ 照抄参数拼口径
    result2 = _pipeline_result(live_trade=True)
    result2["score_smoothing"] = {"method": "ewma", "window": 5, "alpha": 0.35}
    text2 = report.render_report(result2, live_trade=True)
    assert "排序依据：5 日 EWMA 平滑分（alpha=0.35）" in text2

    # 没有该字段 ⇒ 「—」（不编造）
    text3 = report.render_report(_pipeline_result(live_trade=True), live_trade=True)
    assert "排序依据：—" in text3


def test_observation_mode_report_missing_values_render_placeholder():
    result = _pipeline_result(live_trade=False)
    result.pop("snapshot")
    result["generated_at"] = None
    text = report.render_report(result, live_trade=False)
    assert "—" in text
    assert "None" not in text
    assert "nan" not in text


def test_write_report_creates_parent_dir(tmp_path: Path):
    out = tmp_path / "nested" / "deeper" / "report_20261105.md"
    written = report.write_report("# hello\n", out)
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# hello\n"


# =============================================================================
# 排序依据口径（2026-11-10：交付面报告表头的「排序依据」不许渲染成「—」）
# =============================================================================
def test_ranking_basis_reads_score_smoothing_ranking_basis() -> None:
    """🔴 回归：result 带了 `score_smoothing` ⇒ 表头必须显示**真实**排序口径。

    🔴 旧行为：pipeline 的 result **不带** `score_smoothing`
    ⇒ `daily/report.py::_ranking_basis()` 找不到 ⇒ 渲染成「—」
    ⇒ 用户在交付面报告表头看不到「排序依据」⇒ 与铁律「口径必须显式」相悖。
    """
    result = {
        "score_smoothing": {
            "enabled": True,
            "method": "ewma",
            "alpha": 0.35,
            "window": 5,
            "ranking_basis": "EWMA 平滑分（alpha=0.35，窗口 5 交易日）",
        },
    }

    assert report._ranking_basis(result) == "EWMA 平滑分（alpha=0.35，窗口 5 交易日）"


def test_ranking_basis_missing_is_placeholder_not_invented() -> None:
    """没有平滑段 ⇒ 渲染「—」（**不许编造**口径）。"""
    assert report._ranking_basis({}) == report.PLACEHOLDER
