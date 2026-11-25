"""🔴 2026-11-16：《调仓后仓位分布》**幽灵零仓位行**的钉死测试。

【事故（真机交付件 `report_20261113.md`）】
被 3800 元新开仓门槛拦下的 `515450`（预算只剩 584.4 元）**仍留在 `actions` 里**
（为保会计恒等式 $\\sum w_i + w_{cash} = 1$，金额归零）⇒ 阶段 6 渲染《调仓后仓位分布》
时**没有过滤零股数** ⇒ 印出一行「0 股 / 0 元 / 0.00%」的**假持仓**。

【为什么不能靠删 `actions` 来修】
`actions` 里保留那行是为了让被拦金额**精准回流现金**、恒等式仍然自洽
⇒ 只在**渲染层**过滤（`post_trade.positions` 里 `shares <= 0` 的行不展示），
**账目层一个字都不动**。

【🔴 2026-11-25 口径归位（本次改动）】
执行卡曾**从不读**研究链写在动作行上的 `action`（`min_trade_gate.py` 已正确标
「不建仓」）⇒ 门槛作废的新开仓被按 `delta_shares == 0` 重写成「持有」
⇒ 印进《无动作（维持）》表 ⇒ 读起来像「维持原仓」，实际是「本轮根本买不进」。
⇒ 现在这类席位归入 `cash_flow.unbuildable_slots`（**碎屑归现金**），
   在报告里落进《不可建仓（碎屑归现金）》段，**不再**出现在《无动作（维持）》段。

【钉死的纪律】
* 零股数行**不得**出现在《调仓后仓位分布》表里；
* 真实持仓行**必须**原样保留（股数 / 市值 / 权重一字不改）；
* 被门槛作废的新开仓**必须**出现在「不可建仓」段、**不得**出现在「维持」段；
* 被拦标的的**披露**不许丢（阈值 / 被作废金额必须仍出现在报告里）。
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
    """一份**含幽灵零仓位行**的执行卡（口径取自 2026-11-13 真机交付件）。

    🔴 2026-11-25：`515450` 已被门槛作废 ⇒ 归入 `unbuildable_slots`
    （**不在 `orders` 里** ⇒ 不进下单清单、归现金躺平）。
    """
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
        ],
        "post_trade": {
            "cash": 23367.37,
            "equity_amount": 6906.90,
            "equity_weight": 0.23,
            "positions": [
                # 🔴 幽灵零仓位行（最坏情况：账户快照兜底也可能把 0 股行带进来）
                #    ⇒ 渲染层必须照样把它过滤掉
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
            # 🔴 2026-11-25：被最低佣金门槛作废的**新开仓**席位 ⇒ 「不可建仓」（碎屑归现金）
            "unbuildable_slots": [
                {
                    "code": "515450", "name": "红利低波50ETF南方", "action": "不可建仓",
                    "target_shares": 0, "current_shares": 0,
                    "price": 1.4610, "price_date": "2026-11-13",
                    "one_lot_amount": 146.10, "max_amount": 6000.0,
                    "min_amount_threshold": 3800.0,
                    "min_amount_skipped_amount": 584.40,
                    "min_amount_kind": "新开仓",
                    "lot_note": "",
                    "reason": (
                        "🔴 最低佣金门槛作废（档位：新开仓）；"
                        "金额 584.40 元 < 门槛 3800.00 元；"
                        "⇒ 本轮买不进（0 股建仓）⇒ 该席位归现金躺平（**不下单**）"
                    ),
                },
            ],
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


def _section(text: str, title: str) -> str:
    """截出某一节（从 `## {title}` 起，到下一个 `## ` 为止）。"""
    start = text.index(f"## {title}")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


def _table_body(section: str) -> list[list[str]]:
    """把一节里的 Markdown 表格体解析成单元格列表（表头 / 分隔行不算）。"""
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
    return body


def test_零仓位行不得出现在仓位分布表() -> None:
    """被门槛拦下（0 股 / 0 元 / 0.00%）的标的**不得**印成假持仓。"""
    section = _section(report.render_report(_result(), live_trade=True), "调仓后仓位分布")
    assert "515450" not in section, section


def test_真实持仓行必须原样保留() -> None:
    """过滤只动零股数行 ⇒ 真实持仓（股数 / 市值 / 权重）一字不改。"""
    section = _section(report.render_report(_result(), live_trade=True), "调仓后仓位分布")
    assert "512800" in section, section
    assert "7700" in section, section
    assert "6,906.90" in section, section
    assert "+23.00%" in section, section


def test_仓位分布表每一行的股数都必须大于零() -> None:
    """逐行解析表体 ⇒ **股数列必须 > 0**（零股数行一律不许存在）。"""
    section = _section(report.render_report(_result(), live_trade=True), "调仓后仓位分布")
    body = _table_body(section)
    assert body, section
    for cells in body:
        shares = float(cells[2].replace(",", ""))
        assert shares > 0, cells


def test_被拦标的的披露不许丢() -> None:
    """幽灵行删了，但「低于 3800 元门槛」的**披露**必须还在「不可建仓」表。"""
    text = report.render_report(_result(), live_trade=True)
    section = _section(text, "不可建仓（碎屑归现金）")
    assert "515450" in section, section
    assert "3800" in section, section
    assert "584.40" in section, section


def test_被拦标的不得出现在维持段() -> None:
    """🔴 2026-11-25：门槛作废的新开仓是「**买不进**」，不是「维持原仓」。

    ⇒ 报告里它必须落在《不可建仓（碎屑归现金）》段，
    **不得**再出现在《无动作（维持）》段（否则读起来像「维持」，口径说谎）。
    """
    text = report.render_report(_result(), live_trade=True)
    hold = _section(text, "无动作（维持）")
    assert "515450" not in hold, hold
    codes = [cells[0] for cells in _table_body(hold)]
    assert codes == ["512800"], codes


def test_动作口径计数须把被拦席位算进不可建仓() -> None:
    """执行摘要里的口径计数：维持 1 只（512800）、不可建仓 1 只（515450）。"""
    text = report.render_report(_result(), live_trade=True)
    summary = _section(text, "执行摘要")
    assert "维持 1 只" in summary, summary
    assert "不可建仓 1 只" in summary, summary
