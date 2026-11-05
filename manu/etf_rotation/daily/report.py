"""report.py —— 流水线结果 → **Markdown 报告**（按需分析，直接贴进对话窗口）。

【为什么要有这个模块】
交付形态已从「服务器 cron 定时 + 邮件推送」改成「**按需分析**」：
用户在窗口喊「分析」⇒ 流水线跑一遍 ⇒ **报告直接回窗口**。
⇒ 阶段 6 的产出从「一封 HTML 邮件」变成「一份可读的 Markdown」，
邮件降级为**可选**（opt-in）出口。

【职责边界（渲染 ≠ 执行）】
* 本模块**纯渲染**：不联网、不读行情、不重算任何指标；
* **不 import 业务模块**（qlib / akshare / TradingAgents 一律不碰）⇒ 可离线单测；
* 输入只有上游已经算好的结果 dict ⇒ **不反推、不编数字**。

【🔴 数字纪律（铁律，违反即错）】
1. 缺失 / 非法 / NaN 一律渲染成「—」，**绝不**渲染 ``None`` / ``nan`` / ``N/A``；
2. **不许反推**：账户缺 ``total_assets`` 就不许用 ``cash + market_value`` 顶替；
   安全垫比例只从 ``distance_to_ma60_pct`` 取，不从 ``close / ma60 - 1`` 重算；
3. **不许编造**：所有数字都来自上游产物；上游没给的，报告里就是「—」；
4. ``notes`` / ``warnings`` / ``disclaimer`` / ``reason`` **原文照抄**（不改写数字）。

【格式约定】
* 金额：千分位 + 2 位小数（``30,035.57``）；
* 价格：4 位小数（``0.8740``）；
* 百分比：输入是**小数** ⇒ ``+3.93%``（带号 ⇒ 方向一眼可见）；
* 股数：整数（**不加千分位**，保持可 grep）。
"""
from __future__ import annotations

import math
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

#: 缺失值的统一占位符（**只此一个**，全报告不出现 None / nan 字样）
PLACEHOLDER = "—"


# =============================================================================
# 1. 数字格式化（缺失一律「—」）
# =============================================================================
def _to_float(value: object) -> float | None:
    """把任意输入转成 float；**转不了就返回 None**（⇒ 渲染成「—」）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _fmt(value: object, *, pct: bool = False, money: bool = False,
         price: bool = False, shares: bool = False,
         digits: int = 2, signed: bool = False) -> str:
    """统一数字格式化。

    * ``pct=True``：输入是**小数** ⇒ ``0.0393`` 渲染成 ``+3.93%``；
    * ``money=True``：千分位 ⇒ ``30035.57`` 渲染成 ``30,035.57``；
    * ``price=True``：4 位小数 ⇒ ``0.874`` 渲染成 ``0.8740``；
    * ``shares=True``：整数股数（不加千分位）；
    * ``signed=True``：正数前面补 ``+``（方向一眼可见）。
    """
    number = _to_float(value)
    if number is None:
        return PLACEHOLDER
    if pct:
        text = f"{number * 100:.{digits}f}%"
    elif money:
        text = f"{number:,.{digits}f}"
    elif price:
        text = f"{number:.{digits}f}"
    elif shares:
        text = str(int(number)) if float(number).is_integer() else f"{number:.2f}"
    else:
        text = f"{number:.{digits}f}"
    if signed and number > 0:
        text = "+" + text
    return text


def _text(value: object) -> str:
    """文本格式化：``None`` / 空串 / NaN ⇒ 「—」，其余原样（**不改写**）。"""
    if value is None:
        return PLACEHOLDER
    if isinstance(value, float) and math.isnan(value):
        return PLACEHOLDER
    text = str(value).strip()
    return text if text else PLACEHOLDER


def _yn(value: object) -> str:
    """布尔渲染：是 / 否 / —（未知就是「—」，**不猜**）。"""
    if value is None:
        return PLACEHOLDER
    if isinstance(value, bool):
        return "是" if value else "否"
    number = _to_float(value)
    if number is None:
        return PLACEHOLDER
    return "是" if number > 0 else "否"


def _get(mapping: object, key: str, default: object = None) -> object:
    """从 dict 取键（容忍 None / 非 dict）。"""
    if isinstance(mapping, Mapping):
        return mapping.get(key, default)
    return default


def _pick(key: str, *sources: object) -> object:
    """依次从多个来源取第一个**非 None** 的值（**不计算、不反推**）。"""
    for source in sources:
        value = _get(source, key)
        if value is not None:
            return value
    return None


def _cell(text: object) -> str:
    """Markdown 表格单元格转义（竖线 / 换行会破表）。"""
    value = _text(text)
    return value.replace("|", "／").replace("\r", " ").replace("\n", " ")


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """渲染一张 Markdown 表格（**空行也渲染表头**，不静默吞掉整节）。"""
    lines = [
        "| " + " | ".join(_cell(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    return lines


def _bullets(items: object) -> list[str]:
    """渲染要点列表（**原文照抄**，只做转义）。"""
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    return [f"- {_cell(item)}" for item in items]


# =============================================================================
# 2. 公共片段
# =============================================================================
def _account_rows(sources: Sequence[object]) -> list[list[str]]:
    """账户卡片行（总资产 / 现金 / 占比 / 市值 / 持仓数 / 浮盈）。

    🔴 **只取给定字段**：缺就是「—」，**不许**用 ``cash + market_value`` 反推总资产。
    """
    def pick(key: str) -> object:
        return _pick(key, *sources)

    return [
        ["账户快照日", _text(pick("snapshot_date"))],
        ["总资产（元）", _fmt(pick("total_assets"), money=True)],
        ["可用现金（元）", _fmt(pick("cash"), money=True)],
        ["现金占比", _fmt(pick("cash_weight"), pct=True)],
        ["ETF 持仓市值（元）", _fmt(pick("market_value"), money=True)],
        ["持仓占比", _fmt(pick("market_value_weight"), pct=True)],
        ["持仓只数", _fmt(pick("n_positions"), shares=True)],
        ["持仓浮盈（元）", _fmt(pick("total_pnl"), money=True, signed=True)],
        ["持仓浮盈率", _fmt(pick("total_pnl_pct"), pct=True, signed=True)],
    ]


def _route_quote(route: object) -> list[str]:
    """调度说明（来自 `mode_router.route()`；没给就整节不渲染）。"""
    mode = _get(route, "mode")
    if mode is None:
        return []
    reason = _get(route, "reason")
    execution_date = _get(route, "execution_date")
    lines = [f"> 调度：**模式 {mode}**"]
    if reason:
        lines.append(">")
        lines.append(f"> {_cell(reason)}")
    # 🔴 **模式被显式覆盖时必须披露**（否则读者会误以为这是真实信号日全流程）
    if _get(route, "mode_override"):
        eff = _get(route, "effective_mode") or mode
        lines.append(">")
        lines.append(
            f"> ⚠️ **本轮模式 {eff} 是显式 `--mode` 覆盖路由判定的结果**"
            f"（路由原判 {_get(route, 'mode')}）"
            f" ⇒ **不是**真实的调仓信号日全流程，结论按「演练/复核」口径读。"
        )
    if execution_date:
        lines.append(">")
        lines.append(f"> 执行日：{_cell(execution_date)}")
    return lines


# =============================================================================
# 3. 模式 A —— 日常巡检报告
# =============================================================================
def _render_mode_a(result: Mapping, account: object, route: object,
                   asof: str) -> list[str]:
    """渲染模式 A 报告。"""
    lines: list[str] = []
    status = _get(result, "status")
    ma = _get(result, "ma")
    action = _get(result, "action")
    acc_sources = [_get(result, "account"), account]

    lines.append(f"# ETF 轮动巡检报告 · {asof}（模式 A · 日常巡检）")
    lines.append("")
    lines.extend(_route_quote(route))
    if route:
        lines.append("")

    # ---- 一句话结论 ----
    lines.append("## 一句话结论")
    lines.append("")
    if status == "ok":
        lines.append("**趋势完好，守纪律躺平（无动作）。**")
    elif status == "broken":
        lines.append("**🔴 已跌破 MA60，需紧急平仓。**")
    elif status == "insufficient_data":
        lines.append("**样本不足，不做破位判定。**")
    else:
        lines.append(f"**状态：{_cell(status)}**（未识别的状态取值，不编结论）")
    lines.append("")

    # ---- 账户卡片 ----
    lines.append("## 账户卡片")
    lines.append("")
    lines.extend(_table(["项目", "数值"], _account_rows(acc_sources)))
    lines.append("")
    discrepancy = _pick("discrepancy_note", *acc_sources)
    if discrepancy:
        lines.append(f"> {_cell(discrepancy)}")
        lines.append("")

    # ---- 均线安全带 ----
    lines.append("## 均线安全带")
    lines.append("")
    lines.extend(_table(
        ["指标", "数值"],
        [
            ["收盘价（元）", _fmt(_get(ma, "close"), price=True, digits=4)],
            ["收盘日", _text(_get(ma, "close_date"))],
            ["MA20（进场准入线，元）", _fmt(_get(ma, "ma20"), price=True, digits=4)],
            ["MA60（持仓止损线，元）", _fmt(_get(ma, "ma60"), price=True, digits=4)],
            ["站上 MA20？", _yn(_get(ma, "above_ma20"))],
            ["站上 MA60？", _yn(_get(ma, "above_ma60"))],
            ["距 MA60", _fmt(_get(ma, "distance_to_ma60_pct"), pct=True, signed=True)],
            ["金额安全垫（元）", _fmt(_get(ma, "safety_buffer_amount"), money=True, signed=True)],
            ["安全垫比例", _fmt(_get(ma, "safety_buffer_pct"), pct=True, signed=True)],
            ["样本根数", _fmt(_get(ma, "n_obs"), shares=True)],
        ],
    ))
    lines.append("")

    # ---- 行动 ----
    lines.append("## 行动")
    lines.append("")
    if action:
        lines.append("### 🔴 紧急平仓指令")
        lines.append("")
        lines.extend(_table(
            ["项目", "数值"],
            [
                ["触发价（收盘，元）", _fmt(_get(action, "trigger_price"), price=True, digits=4)],
                ["MA60 止损线（元）", _fmt(_get(action, "ma60"), price=True, digits=4)],
                ["跌破幅度", _fmt(_get(action, "break_pct"), pct=True, signed=True)],
                ["清仓股数", _fmt(_get(action, "shares"), shares=True)],
                ["标的", f"{_cell(_get(action, 'code'))} {_cell(_get(action, 'name'))}"],
                ["动作", _text(_get(action, "action"))],
            ],
        ))
        lines.append("")
        instruction = _get(action, "instruction")
        if instruction:
            lines.append(f"**指令原文**：{_cell(instruction)}")
            lines.append("")
        reason = _get(action, "reason")
        if reason:
            lines.append(f"> 触发原因：{_cell(reason)}")
            lines.append("")
    else:
        lines.append(
            "本轮**不产生任何买卖指令**"
            "（模式 A 是构造性保证：路径内没有打分 / 选股 / LLM 调用）。"
        )
        lines.append("")

    # ---- 数据口径与备注 ----
    lines.append("## 数据口径与备注")
    lines.append("")
    notes = _get(result, "notes")
    if notes:
        lines.extend(_bullets(notes))
    else:
        lines.append("- （无备注）")
    generated_at = _get(result, "generated_at")
    lines.append(f"- 报告生成时间：{_cell(generated_at)}")
    lines.append("")
    return lines


# =============================================================================
# 4. 模式 B —— 月度调仓决策卡
# =============================================================================
def _order_rows(orders: Sequence[Mapping]) -> list[list[str]]:
    """订单表行（代码 / 名称 / 方向 / 股数 / 金额 / 费用 / 滑点 / 说明）。"""
    rows: list[list[str]] = []
    for order in orders or []:
        reason = _get(order, "reason")
        cap_note = _get(order, "cap_note")
        if _get(order, "capped") and cap_note:
            reason = f"{reason} ｜ {cap_note}" if reason else str(cap_note)
        rows.append([
            _text(_get(order, "code")),
            _text(_get(order, "name")),
            _text(_get(order, "action")),
            _fmt(_get(order, "current_shares"), shares=True),
            _fmt(_get(order, "target_shares"), shares=True),
            _fmt(_get(order, "delta_shares"), shares=True, signed=True),
            _fmt(_get(order, "price"), price=True, digits=4),
            _text(_get(order, "price_date")),
            _fmt(_get(order, "est_amount"), money=True),
            _fmt(_get(order, "est_fee"), money=True),
            _fmt(_get(order, "est_slippage"), money=True),
            _text(reason),
        ])
    return rows


def _split_orders(orders: Sequence[Mapping]) -> dict[str, list[Mapping]]:
    """按方向拆分订单：买入 / 卖出 / 维持 / 其他。"""
    buckets: dict[str, list[Mapping]] = {"buy": [], "sell": [], "hold": [], "other": []}
    for order in orders or []:
        action = str(_get(order, "action") or "")
        if action == "买入":
            buckets["buy"].append(order)
        elif action == "卖出":
            buckets["sell"].append(order)
        elif action == "持有":
            buckets["hold"].append(order)
        else:
            buckets["other"].append(order)
    return buckets


def _render_mode_b(result: Mapping, account: object, route: object,
                   asof: str) -> list[str]:
    """渲染模式 B 报告。"""
    lines: list[str] = []
    card = _get(result, "card")
    cash_flow = _get(card, "cash_flow")
    post_trade = _get(card, "post_trade")
    orders = _get(card, "orders") or []
    buckets = _split_orders(orders)
    execution_date = _pick("execution_date", card, result)
    acc_sources = [_get(card, "account"), _get(result, "account"), account]

    lines.append(f"# ETF 月度调仓决策卡 · {asof} 信号日（模式 B · 六阶段全流程）")
    lines.append("")
    lines.extend(_route_quote(route))
    if route:
        lines.append("")

    # ---- 执行摘要 ----
    lines.append("## 执行摘要")
    lines.append("")
    lines.extend(_table(["项目", "数值"], _account_rows(acc_sources)))
    lines.append("")
    feasible = _get(cash_flow, "feasible")
    if feasible is True:
        feasible_text = "✅ 可行"
    elif feasible is False:
        feasible_text = "❌ 不可行"
    else:
        feasible_text = PLACEHOLDER
    cash_end = _pick("cash_end", cash_flow)
    if cash_end is None:
        cash_end = _get(post_trade, "cash")
    buy_codes = "、".join(str(_get(o, "code")) for o in buckets["buy"]) or "无"
    sell_codes = "、".join(str(_get(o, "code")) for o in buckets["sell"]) or "无"
    unbuildable = _get(cash_flow, "unbuildable_slots") or []
    lines.extend(_table(
        ["项目", "数值"],
        [
            ["信号日", _text(asof)],
            ["执行日", _text(execution_date)],
            ["执行基准", _text(_get(card, "execution_basis"))],
            ["价格口径", _text(_get(card, "price_basis_note"))],
            ["调仓后现金（元）", _fmt(cash_end, money=True)],
            ["可行性", feasible_text],
            ["现金缓冲达标？", _yn(_get(post_trade, "cash_buffer_ok"))],
        ],
    ))
    lines.append("")
    lines.append(
        f"**动作口径**：买入 {len(buckets['buy'])} 只（{buy_codes}）、"
        f"卖出 {len(buckets['sell'])} 只（{sell_codes}）、"
        f"维持 {len(buckets['hold'])} 只、"
        f"不可建仓 {len(unbuildable)} 只。"
    )
    lines.append("")

    # ---- 六阶段流水线状态 ----
    lines.append("## 六阶段流水线状态")
    lines.append("")
    analysis_asof = _get(result, "analysis_asof")
    scores_max_date = _get(result, "scores_max_date")
    dataset_max = _get(result, "qlib_dataset_max_date")
    stage2_detail = f"打分日 = {_text(analysis_asof)}"
    if scores_max_date is not None and scores_max_date != analysis_asof:
        stage2_detail += f"（stock_scores.csv 最大日 = {_text(scores_max_date)}，与打分日不同）"
    grid_last = _get(result, "score_grid_last")
    if grid_last is not None:
        stage2_detail += f"；打分栅格末日 = {_text(grid_last)}"
    lines.extend(_table(
        ["阶段", "状态", "关键数字"],
        [
            [
                "阶段 1 · 盘后数据定型",
                "已确认" if dataset_max is not None else "未知",
                f"qlib 数据集覆盖末日 = {_text(dataset_max)}",
            ],
            [
                "阶段 2 · S 层全市场 Alpha 推断",
                "已打分" if analysis_asof is not None else "未产出",
                stage2_detail,
            ],
            [
                "阶段 3 · E=W×S 穿透 + MA20/MA60 过滤",
                "已完成" if _get(result, "report_json") else "未产出",
                f"分析报告 = {_text(_get(result, 'report_json'))}",
            ],
            [
                "阶段 4 · TradingAgents 多智能体辩论",
                _text(_get(result, "llm_decision_status")),
                f"调仓计划 = {_text(_get(result, 'plan_json'))}",
            ],
            [
                "阶段 5 · 规则层约束对账",
                "已对账" if _get(result, "node_json") else "未产出",
                f"节点日重算 = {_text(_get(result, 'node_json'))}",
            ],
            [
                "阶段 6 · 执行卡生成",
                "已生成" if card else "未生成",
                f"执行卡 = {_text(_get(result, 'card_json'))}",
            ],
        ],
    ))
    lines.append("")
    if card is None:
        lines.append("> 🔴 本轮**没有执行卡**（`card` 缺失）⇒ 下面各节无数据可渲染。")
        lines.append("")

    # ---- 买入 / 卖出 / 维持 ----
    order_headers = [
        "代码", "名称", "方向", "当前股数", "目标股数", "增减股数",
        "参考价（元）", "价格日", "预估金额（元）", "预估费用（元）", "预估滑点（元）", "说明",
    ]
    lines.append("## 买入清单")
    lines.append("")
    if buckets["buy"]:
        lines.extend(_table(order_headers, _order_rows(buckets["buy"])))
    else:
        lines.append("（本轮无买入动作）")
    lines.append("")

    lines.append("## 卖出清单")
    lines.append("")
    if buckets["sell"]:
        lines.extend(_table(order_headers, _order_rows(buckets["sell"])))
    else:
        lines.append("（本轮无卖出动作）")
    lines.append("")

    lines.append("## 无动作（维持）")
    lines.append("")
    if buckets["hold"]:
        lines.extend(_table(order_headers, _order_rows(buckets["hold"])))
    else:
        lines.append("（本轮无维持动作）")
    lines.append("")

    if buckets["other"]:
        lines.append("## 其他无法执行动作")
        lines.append("")
        lines.extend(_table(
            ["代码", "名称", "方向", "原因"],
            [
                [
                    _text(_get(o, "code")),
                    _text(_get(o, "name")),
                    _text(_get(o, "action")),
                    _text(_get(o, "reason")),
                ]
                for o in buckets["other"]
            ],
        ))
        lines.append("")

    # ---- 不可建仓 ----
    lines.append("## 不可建仓（碎屑归现金）")
    lines.append("")
    if unbuildable:
        lines.extend(_table(
            ["代码", "名称", "一手金额（元）", "单只上限（元）", "原因"],
            [
                [
                    _text(_get(item, "code")),
                    _text(_get(item, "name")),
                    _fmt(_get(item, "one_lot_amount"), money=True),
                    _fmt(_get(item, "max_amount"), money=True),
                    _text(_get(item, "reason")),
                ]
                for item in unbuildable
            ],
        ))
    else:
        lines.append("（本轮无不可建仓席位）")
    lines.append("")

    # ---- 调仓后仓位分布 ----
    lines.append("## 调仓后仓位分布")
    lines.append("")
    positions = _get(post_trade, "positions") or []
    if positions:
        lines.extend(_table(
            ["代码", "名称", "股数", "市值（元）", "权重", "备注"],
            [
                [
                    _text(_get(pos, "code")),
                    _text(_get(pos, "name")),
                    _fmt(_get(pos, "shares"), shares=True),
                    _fmt(_get(pos, "market_value"), money=True),
                    _fmt(_get(pos, "weight"), pct=True, signed=True),
                    _text(_get(pos, "note")),
                ]
                for pos in positions
            ],
        ))
    else:
        lines.append("（无仓位数据）")
    lines.append("")

    # ---- 现金流对账 ----
    lines.append("## 现金流对账")
    lines.append("")
    lines.extend(_table(
        ["项目", "数值"],
        [
            ["期初现金（元）", _fmt(_get(cash_flow, "cash_begin"), money=True)],
            ["卖出回款（元）", _fmt(_get(cash_flow, "sell_proceeds"), money=True)],
            ["卖出费用（元）", _fmt(_get(cash_flow, "sell_fees"), money=True)],
            ["买入金额（元）", _fmt(_get(cash_flow, "buy_amount"), money=True)],
            ["买入费用（元）", _fmt(_get(cash_flow, "buy_fees"), money=True)],
            ["滑点估算（元）", _fmt(_get(cash_flow, "slippage"), money=True)],
            ["期末现金（元）", _fmt(_get(cash_flow, "cash_end"), money=True)],
            ["缺口（元）", _fmt(_get(cash_flow, "deficit"), money=True)],
            ["盈余（元）", _fmt(_get(cash_flow, "surplus"), money=True)],
            ["买入前可用现金（元）", _fmt(_get(cash_flow, "available_before_buy"), money=True)],
            ["缓冲余量（元）", _fmt(_get(cash_flow, "headroom"), money=True)],
            ["现金缓冲（元）", _fmt(_get(post_trade, "cash_buffer"), money=True)],
            ["可行性", feasible_text],
        ],
    ))
    lines.append("")
    scaled = _get(cash_flow, "scaled_down") or []
    if scaled:
        lines.append("### 因现金缓冲而缩减的买单")
        lines.append("")
        for item in scaled:
            lines.append(
                f"- {_cell(_get(item, 'code'))} {_cell(_get(item, 'name'))}："
                f"减持 {_fmt(_get(item, 'reduced_shares'), shares=True)} 股"
            )
        lines.append("")

    # ---- 风险披露 ----
    lines.append("## 风险披露")
    lines.append("")
    warnings = _get(card, "warnings") or []
    if warnings:
        lines.extend(_bullets(warnings))
    else:
        lines.append("- （执行卡未给出额外告警）")
    lines.append("")
    disclaimer = _get(card, "disclaimer")
    if disclaimer:
        lines.append(f"> {_cell(disclaimer)}")
        lines.append("")
    notes = _get(result, "notes") or []
    if notes:
        lines.append("### 流水线备注")
        lines.append("")
        lines.extend(_bullets(notes))
        lines.append("")

    # ---- 挂单清单 ----
    lines.append("## 挂单清单（次日开盘人工执行）")
    lines.append("")
    lines.append(
        "按本表在券商 App 手工挂单，参考价 = 信号日收盘价，实际成交价可能不同。"
    )
    lines.append("")
    tradable = buckets["buy"] + buckets["sell"]
    if tradable:
        lines.extend(_table(
            ["代码", "名称", "方向", "股数", "参考价（元）"],
            [
                [
                    _text(_get(o, "code")),
                    _text(_get(o, "name")),
                    _text(_get(o, "action")),
                    _fmt(_get(o, "delta_shares"), shares=True, signed=True),
                    _fmt(_get(o, "price"), price=True, digits=4),
                ]
                for o in tradable
            ],
        ))
    else:
        lines.append("（本轮无挂单动作）")
    lines.append("")
    generated_at = _get(result, "generated_at")
    lines.append(f"- 报告生成时间：{_cell(generated_at)}")
    lines.append("")
    return lines


# =============================================================================
# 5. 公开 API
# =============================================================================
def render_report(result: dict, *, account: dict | None = None, route: dict | None = None,
                  mode: str | None = None, asof: str | None = None) -> str:
    """把流水线结果 dict 渲染成 **Markdown 报告**（可直接贴进对话窗口）。

    参数
    ----
    result : 模式 A / 模式 B 的结果 dict（结构见 ``agents_rebalance.run_mode_a``
        / ``run_mode_b`` 的返回值）。
    account: 账户快照（可选）—— 只在 ``result`` 里缺对应键时**兜底取用**，
        **不参与任何计算**（不反推总资产、不重算占比）。
    route  : ``mode_router.route()`` 的返回值（可选）⇒ 渲染调度说明。
    mode   : 模式（``"A"`` / ``"B"``）—— 缺省取 ``result["mode"]``。
    asof   : 报告日期 —— 缺省取 ``result["asof"]``。

    返回
    ----
    Markdown 字符串。

    🔴 **数字纪律**：缺失值一律渲染成「—」；**不反推、不编造**。
    """
    if not isinstance(result, Mapping):
        raise TypeError(f"result 必须是 dict，实际是 {type(result).__name__}")

    resolved_mode = mode or _get(result, "mode")
    if isinstance(resolved_mode, str):
        resolved_mode = resolved_mode.strip().upper()
    if resolved_mode not in ("A", "B"):
        raise ValueError(f"无法判定模式：mode={resolved_mode!r}（result['mode'] 也没有）")

    day = asof or _get(result, "asof")
    day_text = _text(day)

    if resolved_mode == "A":
        lines = _render_mode_a(result, account, route, day_text)
    else:
        lines = _render_mode_b(result, account, route, day_text)

    text = "\n".join(lines).rstrip() + "\n"
    return text


def write_report(markdown: str, path: str | Path) -> Path:
    """把 Markdown 写盘（**父目录自动创建**），返回写入的 ``Path``。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    return out


def render_failure_report(*, asof: str, stage: str, error: str,
                          traceback_text: str = "") -> str:
    """渲染**流水线失败报告**（Markdown）。

    🔴 为什么必须有它：邮件已改成「按需 opt-in」⇒ 失败时**默认没有邮件**。
    但失败**必须**留证据 ⇒ 落一份 Markdown 到 ``manu/report/`` 并打印到 stderr。

    参数
    ----
    asof          : 业务日期
    stage         : 失败的阶段名（如 ``"fetch_data"`` / ``"rebalance[B]"``）
    error         : 一行错误摘要（``type: message``）
    traceback_text: 原始 traceback（**原样贴出**，不许裁剪成"已处理"）
    """
    lines: list[str] = [
        f"# ETF 轮动分析失败报告 · {_text(asof)}",
        "",
        "## 失败摘要",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 业务日期 | {_text(asof)} |",
        f"| 失败阶段 | {_text(stage)} |",
        f"| 错误类型 | {_text(error) or '—'} |",
        f"| 报告生成时间 | {datetime.now().astimezone().isoformat(timespec='seconds')} |",
        "",
        "## 原始 traceback",
        "",
        "```text",
        (traceback_text or "（无 traceback）").rstrip(),
        "```",
        "",
        "> 本报告由本地流水线在**按需分析**模式下生成（邮件默认不发送）。",
        "> 错误已原样保留，未做吞异常或静默降级处理。",
        "",
    ]
    return "\n".join(lines)
