"""🔴 2026-11-16：《调仓后仓位分布》**幽灵零仓位行**的钉死测试。

【事故（真机交付件 `report_20261113.md`）】
被 3800 元新开仓门槛拦下的 `515450`（预算只剩 584.4 元）**仍留在 `actions` 里**
（为保会计恒等式 $\\sum w_i + w_{cash} = 1$，金额归零）⇒ 阶段 6 渲染《调仓后仓位分布》
时**没有过滤零股数** ⇒ 印出一行「0 股 / 0 元 / 0.00%」的**假持仓**。

【为什么不能靠删 `actions` 来修】
`actions` 里保留那行是为了让被拦金额**精准回流现金**、恒等式仍然自洽
⇒ 只在**渲染层**过滤（`post_trade.positions` 里 `shares <= 0` 的行不展示），
**账目层一个字都不动**。

【钉死的纪律】
* 零股数行**不得**出现在《调仓后仓位分布》表里；
* 真实持仓行**必须**原样保留（股数 / 市值 / 权重一字不改）；
* 被拦标的的**披露**不许丢（它在「无动作（维持）」表里已经写明「低于 3800 元门槛」）。
"""

from __future__ import annotations

import sys
from pathlib import Path

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import report  # noqa: E402

_RUN_DIR = "/mnt/data/siping/runs/etf_rotation_results/daily/20261113"


def _card_with_zero_position() -> dict:
    """一份**含幽灵零仓位行**的执行卡（口径取自 2026-11-13 真机交付件）。"""
    return {
        "asof": "2026-11-13",
        "execution_date": "2026-12-01",
        "execution_lag_days": 18,
        "execution_basis": "信号日 T 收盘出信号 → 执行日 T+1 开盘执行",
        "price_basis_note": "节点日收盘价 ≠ 执行日成交价",
        "account": {
            "total_assets": 30035.57,
            "cash": 23367.37,
            "cash_weight": 0.778,
            "market_value": 6668.20,
        },
        "orders": [
            {
                "code": "512800", "name": "银行ETF华宝", "action": "持有",
                "current_shares": 7700, "price": 0.8970, "price_date": "2026-11-13",
                "target_shares": 7700, "delta_shares": 0, "target_amount": 6906.90,
                "est_amount": 0.0, "est_fee": 0.0, "est_slippage": 0.0,
                "capped": False, "cap_note": "", "reason": "维持",
            },
            {
                "code": "515450", "name": "红利低波50ETF南方", "action": "持有",
                "current_shares": 0, "price": 1.4610, "price_date": "2026-11-13",
                "target_shares": 0, "delta_shares": 0, "target_amount": 0.0,
                "est_amount": 0.0, "est_fee": 0.0, "est_slippage": 0.0,
                "capped": True,
                "cap_note": "🔴 金额 584 元 < 新开仓门槛 3800 元 ⇒ 本轮不调仓",
                "reason": "入选本期组合；金额低于新开仓门槛 ⇒ 本轮不调仓",
            },
        ],
        "post_trade": {
            "cash": 23367.37,
            "equity_amount": 6906.90,
            "equity_weight": 0.23,
            "positions": [
                # 🔴 幽灵零仓位行：被 3800 元门槛拦下 ⇒ 金额归零，但仍留在 actions 里
                {"code": "515450", "name": "红利低波50ETF南方", "shares": 0,
                 "market_value": 0.0, "weight": 0.0, "note": ""},
                {"code": "512800", "name": "银行ETF华宝", "shares": 7700,
                 "market_value": 6906.90, "weight": 0.23, "note": ""},
            ],
            "cash_buffer": 23367.37,
            "cash_buffer_ok": True,
        },
        "cash_flow": {
            "cash_begin": 23367.37,
            "sell_proceeds": 0.0,
            "sell_fees": 0.0,
            "buy_amount": 0.0,
            "buy_fees": 0.0,
            "slippage": 0.0,
            "cash_end": 23367.37,
            "deficit": 0.0,
            "surplus": 0.0,
            "available_before_buy": 23367.37,
            "headroom": 0.0,
            "feasible": True,
            "unbuildable_slots": [],
            "scaled_down": [],
        },
        "warnings": [],
        "disclaimer": "本报告由本地流水线生成，不构成投资建议。",
        "constraints": {
            "target_positions": 5, "max_position_amount": 6000.0, "lot_size": 100,
            "cash_buffer_min": 200.0, "cash_buffer_max": 300.0,
        },
    }


def _result() -> dict:
    return {
        "live_trade": True,
        "asof": "2026-11-13",
        "execution_date": "2026-12-01",
        "run_dir": "/mnt/data/siping/runs/etf_rotation_results/daily/20261113",
        "snapshot": {
            "root": "/mnt/data/siping/runs/etf_rotation_results/snapshots",
            "path": "/mnt/data/siping/runs/etf_rotation_results/snapshots/20261113_202504",
            "run_id": "20261113_202504",
        },
        "report_json": _RUN_DIR + "/etf_analysis_report.json",
        "plan_json": _RUN_DIR + "/final_rebalance_plan.json",
        "node_json": _RUN_DIR + "/node_reprice_20261113.json",
        "card_json": _RUN_DIR + "/execution_card_20261113.json",
        "card": _card_with_zero_position(),
        "email_kind": "rebalance_card",
        "notes": [],
        "analysis_asof": "2026-11-13",
        "scores_max_date": "2026-11-13",
        "score_grid_last": "2026-11-13",
        "qlib_dataset_max_date": "2026-11-13",
        "llm_decision_status": "ok",
        "execution_lag_days": 18,
        "generated_at": "2026-11-13T21:45:47",
    }


def _position_section(text: str) -> str:
    """截出《调仓后仓位分布》那一节（到下一个 `## ` 为止）。"""
    start = text.index("## 调仓后仓位分布")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


def test_零仓位行不得出现在仓位分布表() -> None:
    """被门槛拦下（0 股 / 0 元 / 0.00%）的标的**不得**印成假持仓。"""
    text = report.render_report(_result(), live_trade=True)
    section = _position_section(text)
    assert "515450" not in section, section


def test_真实持仓行必须原样保留() -> None:
    """过滤只动零股数行 ⇒ 真实持仓（股数 / 市值 / 权重）一字不改。"""
    text = report.render_report(_result(), live_trade=True)
    section = _position_section(text)
    assert "512800" in section, section
    assert "7700" in section, section
    assert "6,906.90" in section, section
    assert "+23.00%" in section, section


def test_仓位分布表每一行的股数都必须大于零() -> None:
    """逐行解析表体 ⇒ **股数列必须 > 0**（零股数行一律不许存在）。"""
    text = report.render_report(_result(), live_trade=True)
    section = _position_section(text)
    body: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue          # 分隔行
        if cells[0] == "代码":
            continue          # 表头
        body.append(cells)
    assert body, section
    for cells in body:
        shares = float(cells[2].replace(",", ""))
        assert shares > 0, cells


def test_被拦标的的披露不许丢() -> None:
    """幽灵行删了，但「低于 3800 元门槛」的**披露**必须还在「无动作（维持）」表。"""
    text = report.render_report(_result(), live_trade=True)
    start = text.index("## 无动作（维持）")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    section = rest if end < 0 else rest[:end]
    assert "515450" in section, section
    assert "3800" in section, section
