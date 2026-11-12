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

【🔴 两种运行态（唯一的状态维度）】
* **观察态**（缺省）：执行卡**挂单禁用**、**不发信** ⇒ 报告顶部有警示块，
  执行卡段落渲染成「观察清单（不可下单）」⇒ 全文**不出现**任何下单指令；
* **实盘态**（``--live-trade``）：渲染真实挂单清单。

「模式」分诊**已彻底下线** ⇒ 只剩**一条**渲染路径（``_render_pipeline``）；
运行态**只**由 ``live_trade`` 表达（观察态 / 实盘态）。

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


# =============================================================================
# 3. 统一流水线 —— 月度调仓决策卡（观察态 / 实盘态共用同一条渲染路径）
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


def _observation_banner(result: Mapping) -> list[str]:
    """观察态顶部警示块（**结构性挂单禁用**）。"""
    snapshot_path = _text(_get(_get(result, "snapshot"), "path"))
    return [
        "> **[观察态 · 挂单禁用 · 非实盘信号]**",
        "> 本轮未开 `--live-trade` ⇒ 执行卡不可下单、未发信。",
        f"> 快照目录：{snapshot_path}",
        "",
    ]


def _ranking_basis(result: Mapping) -> str:
    """排序依据口径（**照抄 result 里的真实值**；没有 ⇒「—」，**不写死常量**）。"""
    raw = None
    for key in ("score_smoothing", "ranking_basis"):
        value = _get(result, key)
        if value is not None:
            raw = value
            break
    if raw is None:
        return PLACEHOLDER
    if isinstance(raw, str):
        return raw.strip() or PLACEHOLDER
    if not isinstance(raw, Mapping):
        return _text(raw)
    basis = _get(raw, "ranking_basis")
    if isinstance(basis, str) and basis.strip():
        return basis.strip()
    method = window = alpha = None
    for key in ("method", "kind", "type", "name"):
        if _get(raw, key) is not None:
            method = _get(raw, key)
            break
    for key in ("window", "window_days", "days", "span"):
        if _get(raw, key) is not None:
            window = _get(raw, key)
            break
    alpha = _get(raw, "alpha")
    if method is None and window is None and alpha is None:
        return PLACEHOLDER
    if method is None:
        method_text = ""
    else:
        method_text = "EWMA" if "ewma" in str(method).lower() else str(method).strip()
    window_text = "" if window is None else f"{_text(window)} 日"
    core = f"{window_text} {method_text} 平滑分".strip() or "平滑分"
    return f"{core}（alpha={_text(alpha) if alpha is not None else PLACEHOLDER}）"


def _provenance_rows(result: Mapping, live_trade: bool) -> list[list[str]]:
    """快照 / 运行态 / ``live_trade`` 的**显式披露**行（缺 ⇒「—」，**不编造**）。"""
    has_raw_live = isinstance(result, Mapping) and "live_trade" in result
    raw_live = _get(result, "live_trade")
    state_text = "实盘态（可下单 · 可发信）" if live_trade else "观察态（挂单禁用 · 不发信）"
    # 🔴 2026-11-09（P1-5）：`--full` 覆盖掉的 skip 开关**必须**在报告里披露
    #    （原先只进 manifest ⇒ 交付面看不到 ⇒ 等于静默降级）。
    _skipped = _get(result, "skipped_switches") or []
    if isinstance(_skipped, str):
        _skipped = []
    _skipped_text = (
        "；".join(
            f"{_get(s, 'name')}（{_get(s, 'reason')}）"
            for s in _skipped
            if isinstance(s, Mapping)
        )
        or "无"
    )
    return [
        ["快照目录", _text(_get(_get(result, "snapshot"), "path"))],
        ["本轮状态", state_text],
        ["live_trade", _yn(raw_live) if has_raw_live else PLACEHOLDER],
        ["本轮被覆盖的开关", _skipped_text],
    ]


def _render_pipeline(result: Mapping, account: object,
                     asof: str, live_trade: bool = False) -> list[str]:
    """渲染统一流水线报告。

    🔴 **观察态 / 实盘态共用同一条渲染路径**（「模式」分诊已下线）：
    * 观察态（``live_trade=False``）：顶部警示块 ＋ 执行卡段落走「观察清单」分支；
    * 实盘态（``live_trade=True``）：渲染真实挂单清单。
    """
    lines: list[str] = []
    card = _get(result, "card")
    cash_flow = _get(card, "cash_flow")
    post_trade = _get(card, "post_trade")
    orders = _get(card, "orders") or []
    buckets = _split_orders(orders)
    execution_date = _pick("execution_date", card, result)
    acc_sources = [_get(card, "account"), _get(result, "account"), account]

    if not live_trade:
        lines.extend(_observation_banner(result))
    if live_trade:
        lines.append(f"# ETF 月度调仓决策卡 · {asof} 信号日（统一流水线 · 六阶段全流程）")
    else:
        lines.append(
            f"# [观察态 · 挂单禁用] ETF 月度调仓决策卡 · {asof} 信号日"
            "（统一流水线 · 挂单禁用 · 非实盘信号）"
        )
    lines.append("")
    # ---- 快照与运行口径（🔴 不许静默降级 ⇒ 关键口径必须显式披露）----
    lines.append("## 快照与运行口径")
    lines.append("")
    lines.extend(_table(["项目", "数值"], _provenance_rows(result, live_trade)))
    lines.append("")
    lines.append(f"> 排序依据：{_ranking_basis(result)}")
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
    #: 🔴 2026-11-10：现金**预留**口径（比例 / 比例下限金额 / 生效下限）—— 全部来自执行卡，**不反推**
    constraints = _get(card, "constraints")
    reserve_ratio = _pick("cash_reserve_ratio", post_trade, constraints)
    reserve_required = _get(post_trade, "cash_reserve_required")
    buf_min_eff = _pick("cash_buffer_min_eff", constraints)
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
            ["现金预留比例", _fmt(reserve_ratio, pct=True)],
            ["现金预留下限（元）", _fmt(reserve_required, money=True)],
            [
                "现金缓冲达标？",
                f"{_yn(_get(post_trade, 'cash_buffer_ok'))}"
                f"（生效下限 {_fmt(buf_min_eff, money=True)} 元）",
            ],
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

    # ---- 🛡 TA 合规筛选（否决 / 顺延）----
    # 🔴 2026-11-10：TA 只做**一票否决 + 顺延**，数字全部来自计划件
    #    （`plan["ta_screening"]` ⇒ `result["ta_screening"]`）⇒ 本模块**不反推、不编造**。
    ta_screen = _get(result, "ta_screening") or {}
    if isinstance(ta_screen, Mapping) and ta_screen:
        rejected = ta_screen.get("rejected") or []
        substituted = ta_screen.get("substituted") or []
        screen_pairs = ta_screen.get("screen_pairs") or []
        no_reason = ta_screen.get("no_substitute_reason")
        held_not_touched = bool(ta_screen.get("held_not_touched"))
        lines.append("## 🛡 TA 合规筛选（否决 / 顺延）")
        lines.append("")
        lines.append(
            "**定位**：TradingAgents 只做「合规陪审团」——有**一票否决权**，"
            "但**不能凭空选股**；被否决 ⇒ **规则层顺延**挑下一个**不同主题**的资产。"
        )
        lines.append("")
        if rejected:
            lines.append("### 被 TA 否决的候选")
            lines.append("")
            lines.extend(_table(
                ["代码", "名称", "否决理由", "原文出处"],
                [
                    [
                        _text(_get(r, "code")),
                        _text(_get(r, "name")),
                        _text(_get(r, "reason")),
                        _text(_get(r, "source_section")),
                    ]
                    for r in rejected
                ],
            ))
            lines.append("")
        else:
            lines.append("**本轮无 TA 否决**（候选池全部通过合规审查）。")
            lines.append("")
        # ---- ⚠️ 池外代码（LLM 笔误 / 幻觉代码）**剔除但如实披露** ----
        # 🔴 2026-11-12：TA 的权威节里可能出现**候选池外**的代码
        #    （实测 2026-11-11：LLM 把 516620 笔误写成 566620）。
        #    规则层只认「池内白名单」⇒ 池外代码**不进否决名单**（否则会伪造一条
        #    「被否决」的假事实）⇒ 但**必须披露**（不许悄悄吞掉）。
        _out_of_universe = ta_screen.get("rejected_out_of_universe") or []
        if _out_of_universe:
            lines.append("### ⚠️ 池外代码（**已剔除，不计入否决名单**）")
            lines.append("")
            lines.extend(_table(
                ["代码", "名称", "LLM 给出的理由", "原文出处", "处置"],
                [
                    [
                        _text(_get(r, "code")),
                        _text(_get(r, "name")),
                        _text(_get(r, "reason")),
                        _text(_get(r, "source_section")),
                        _text(_get(r, "note")) or "池外代码 ⇒ 已剔除",
                    ]
                    for r in _out_of_universe
                ],
            ))
            lines.append("")
            lines.append(
                "ℹ️ **池外代码不进否决名单**：上述代码不在本次 ETF 候选池"
                "（`--universe` 白名单）内 ⇒ 规则层**不认**这条否决"
                "（多半是 LLM 笔误/幻觉）⇒ 已剔除；**池内真否决行为不受影响**。"
            )
            lines.append("")
        if substituted:
            lines.append("### 顺延补位记录")
            lines.append("")
            lines.extend(_table(
                ["代码", "名称", "替补对象", "最终分", "主题", "顺延理由"],
                [
                    [
                        _text(_get(s, "code")),
                        _text(_get(s, "name")),
                        _text(_get(s, "substituted_for")),
                        _fmt(_get(s, "final_score")),
                        _text(_get(s, "theme")),
                        _text(_get(s, "reason")),
                    ]
                    for s in substituted
                ],
            ))
            lines.append("")
        if no_reason:
            lines.append(f"**无顺延补位**：{_text(no_reason)}")
            lines.append("")
        orphans = ta_screen.get("orphaned_holdings") or []
        if held_not_touched:
            lines.append(
                "ℹ️ **被否决的标的本来就在持仓里** ⇒ 只从候选池剔除，"
                "**持仓不动**（不强制卖出）；该席位在计划里保留为「维持」行"
                "（目标权重 = **当前权重**）⇒ 市值仍计入权益口径。"
            )
            lines.append("")
        if orphans:
            lines.append(
                f"**孤儿持仓（被否决但仍在仓）**：{'、'.join(str(c) for c in orphans)}"
                " ⇒ 保留为「维持」行，不买不卖。"
            )
            lines.append("")
        equity_cap = _get(ta_screen, "equity_cap")
        equity_cap_eff = _get(ta_screen, "equity_cap_effective")
        cap_recommend = _get(ta_screen, "equity_cap_ta_recommendation") or {}
        cap_skipped = _get(ta_screen, "cap_skipped")
        breach = _get(ta_screen, "equity_cap_breach")
        if equity_cap is not None:
            lines.append(
                f"**权益上限**：规则层硬上限 {_fmt(equity_cap, pct=True)}"
                f"（= 1 − 现金预留比例）"
                + (f"｜**本轮生效上限 {_fmt(equity_cap_eff, pct=True)}**"
                   if equity_cap_eff is not None else "")
                + "｜顺延补位因超限被跳过："
                f"{_yn(bool(cap_skipped))}｜权益超配额：{_fmt(breach, pct=True)}"
            )
            lines.append("")
            # 🔴 2026-11-11（Beta 避险落地）：TA 建议压降权益上限被**规则层采纳** ⇒
            #    日报必须让用户看见「TA 建议多少 / 规则层按多少落地 / 原文依据」。
            if isinstance(cap_recommend, Mapping):
                _rec_pct = _get(cap_recommend, "pct")
                _rec_ev = str(_get(cap_recommend, "evidence") or "").strip()
                _rec_reason = str(_get(cap_recommend, "reason") or "")
                if _rec_pct is not None:
                    lines.append(
                        f"**Beta 避险（TA 权益上限建议）**：TA 建议 "
                        f"{_fmt(_rec_pct, pct=True)}（{_rec_reason}）"
                        + (f" ⇒ **本轮生效上限 {_fmt(equity_cap_eff, pct=True)}**"
                           if _rec_reason == "accepted" and equity_cap_eff is not None
                           else " ⇒ **未采纳**（只许压降、不许抬高）")
                        + (f"｜原文依据：{_rec_ev[:160]}" if _rec_ev else "")
                    )
                    lines.append("")
                elif _rec_reason == "not_a_target":
                    # 🔴 2026-11-11（审计缺陷 D）：TA 提到了权益数字但**不是目标口径**
                    #    （陈述/预测句，如「回升至 56%」）⇒ 规则层**未压降**
                    #    ⇒ **必须让用户看见**，否则「TA 明明说了 ⇒ 却按 80% 执行」无从追查。
                    lines.append(
                        "**Beta 避险（TA 权益上限建议）**：TA 提到的权益数字"
                        "**不是目标口径**（陈述/预测句）⇒ 规则层**未压降**"
                        f"（仍按硬上限 {_fmt(_get(cap_recommend, 'base_cap'), pct=True)} 执行）"
                        + (f"｜原文依据：{_rec_ev[:160]}" if _rec_ev else "")
                    )
                    lines.append("")
        if screen_pairs:
            # 🔴 2026-11-11：TA 已被明确授权可**据此点名否决**（合规陪审团）
            #    ⇒ 标题不再写「仅供审查参考」；规则层**硬拦口径本身不变**（仍只拦重叠 ≥ 0.90 / corr120 ≥ 0.95）。
            lines.append(
                "### 候选池高重合预警对（**TA 可据此点名否决**；规则层硬拦口径不变）"
            )
            lines.append("")
            lines.extend(_table(
                ["标的 A", "标的 B", "成分重叠度", "120 日相关性", "同族"],
                [
                    [
                        f"{_get(p, 'a')} {_get(p, 'a_name')}",
                        f"{_get(p, 'b')} {_get(p, 'b_name')}",
                        _fmt(_get(p, "weight_overlap")),
                        _fmt(_get(p, "corr120")),
                        _yn(_get(p, "same_theme")),
                    ]
                    for p in screen_pairs
                ],
            ))
            lines.append("")
        else:
            lines.append(
                f"**高重合预警对**：{_text(ta_screen.get('screen_note')) or '无'}"
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
    if live_trade:
        lines.append("## 买入清单")
    else:
        lines.append("## 买入方向（观察 · **不可下单**）")
    lines.append("")
    if buckets["buy"]:
        lines.extend(_table(order_headers, _order_rows(buckets["buy"])))
    else:
        lines.append("（本轮无买入动作）")
    lines.append("")

    if live_trade:
        lines.append("## 卖出清单")
    else:
        lines.append("## 卖出方向（观察 · **不可下单**）")
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
    #: 🔴 2026-11-10：现金预留口径进对账表（预留比例 / 预留金额 / 实得现金占比）
    total_assets = _get(_get(card, "account"), "total_assets")
    cash_ratio = (cash_end / total_assets) if (cash_end is not None and total_assets) else None
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
            ["实得现金占比", _fmt(cash_ratio, pct=True)],
            ["现金预留比例", _fmt(reserve_ratio, pct=True)],
            ["现金预留金额（元）", _fmt(reserve_required, money=True)],
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

    # ---- 挂单清单 / 观察清单 ----
    # 🔴 **观察态 ⇒ 这一段不许长成「挂单清单」**：
    #    实测事故（2026-11-06）：观察态报告里出现了
    #    「## 挂单清单（次日开盘人工执行）」＋「按本表在券商 App 手工挂单」
    #    ⇒ 顶部有警示块也没用 ⇒ **正文仍在教人下单** ⇒ 违反「结构性不可下单」。
    #    ⇒ 观察态改成「**观察清单（不可下单）**」：标题、正文提示、每行状态列
    #    三处都写明**禁用**，让任何人单独截取这张表也不会误当挂单指令。
    if live_trade:
        lines.append("## 挂单清单（次日开盘人工执行）")
        lines.append("")
        lines.append(
            "按本表在券商 App 手工挂单，参考价 = 信号日收盘价，实际成交价可能不同。"
        )
        lines.append("")
    else:
        lines.append("## 观察清单（**不可下单** · 挂单禁用）")
        lines.append("")
        lines.append(
            "🔴 **本表不可下单**：本轮是**观察态**（未开 `--live-trade`）⇒ "
            "**不产生实盘信号**、**未发信**。下表仅用于**观察**排序与方向的当日演化；"
            "参考价为**信号日收盘价**（≠ 节点日成交价），**不是**下单指令。"
        )
        lines.append("")
    tradable = buckets["buy"] + buckets["sell"]
    if tradable:
        if live_trade:
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
            lines.extend(_table(
                ["状态", "代码", "名称", "方向", "股数（观察值）", "参考价（元）"],
                [
                    [
                        "🔴 禁用",
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
        lines.append("（本轮无挂单动作）" if live_trade else "（本轮无动作）")
    lines.append("")
    generated_at = _get(result, "generated_at")
    lines.append(f"- 报告生成时间：{_cell(generated_at)}")
    lines.append("")
    return lines


# =============================================================================
# 4. 公开 API
# =============================================================================
def render_report(result: dict, *, account: dict | None = None,
                  asof: str | None = None,
                  live_trade: bool | None = None) -> str:
    """把流水线结果 dict 渲染成 **Markdown 报告**（可直接贴进对话窗口）。

    参数
    ----
    result : 统一流水线的结果 dict（结构见 ``agents_rebalance.run_pipeline`` 的返回值）。
    account: 账户快照（可选）—— 只在 ``result`` 里缺对应键时**兜底取用**，
        **不参与任何计算**（不反推总资产、不重算占比）。
    asof   : 报告日期 —— 缺省取 ``result["asof"]``。
    live_trade: 实盘态开关 —— 缺省取 ``result["live_trade"]``（缺省 False = **观察态**）。

    返回
    ----
    Markdown 字符串。

    🔴 **数字纪律**：缺失值一律渲染成「—」；**不反推、不编造**。
    """
    if not isinstance(result, Mapping):
        raise TypeError(f"result 必须是 dict，实际是 {type(result).__name__}")

    day = asof or _get(result, "asof")
    day_text = _text(day)

    if live_trade is None:
        resolved_live_trade = bool(_get(result, "live_trade", False))
    else:
        resolved_live_trade = bool(live_trade)

    lines = _render_pipeline(result, account, day_text, resolved_live_trade)
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
    但失败**必须**留证据 ⇒ 落一份 Markdown 到 ``<RESULTS_ROOT>/reports/`` 并打印到 stderr。

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
