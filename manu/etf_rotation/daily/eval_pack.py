"""eval_pack.py —— 🔴 **评估就绪包（eval pack）**：把「事后系统有效性评估」需要的
现场证据，从快照里**已有的**交付件**派生**成机器可读小表，落进**同一个快照目录**。

【为什么要有它 —— 2026-11-27 审计】
用户要做「系统有效性评估」，四个维度分别是：
  ① **傻瓜对照组**：候选池等权基准 ＋ 蒙特卡洛随机盲选分位；
  ② **信号单调性**：候选池按 ``E = W × S`` 分高/中/低三档看后验收益；
  ③ **模块级消融**：TA（多智能体陪审团）否决掉的标的 vs 留下的，后验收益谁高；
  ④ **部署物理损耗**：回测假设成本 vs 实盘真实佣金＋滑点。

审计结论：**资料不是"全有"，也不是"全无"，而是"原始件齐、机读件缺"**：
  * ① ② 的**原始输入齐**（``etf_analysis_report.json:candidates`` 有 19 只候选池
    ＋ ``e_score`` ＋ ``rank``），但**没有**一张能直接喂归因函数的表；
  * ③ 的**原始输入有硬伤**（``final_rebalance_plan.json:ta_screening.rejected``
    把 512800 / 159928 误记成"否决"，而 TA 原文分别是"减仓"和"买"）
    ⇒ **照它建账本会把 TA 的真实结论记反**；
  * ④ **实盘成交价 / 实扣佣金根本没有**（本轮是观察态，从未下单）
    ⇒ 只能落一张**待回填的台账模板**。

⇒ 本模块把上面 ①②③④ 需要的**可机读输入**一次性落进快照（**纯派生、零副作用**）：
  ``eval_candidate_pool.csv`` / ``eval_ta_shadow_ledger.csv`` / ``live_fills.csv``
  / ``cost_assumption.json`` / ``nav_history_snapshot.csv`` / ``eval_pack_manifest.json``。

【不变式（硬）】
  * **P1 零副作用**：不改任何决策、不改任何既有交付件、**不写根目录全局文件**；
  * **P2 fail-soft**：任何一件生成失败 ⇒ 只登记进 manifest 的 ``stages``，
    **绝不阻断流水线**（评估是"研究侧"的事，不是"交易侧"的事）；
  * **P3 纯标准库**：不 import pandas/numpy ⇒ 在 ql 环境与本机 env 都能跑；
  * **P4 口径写在件里**：每个字段的口径都写进 ``eval_pack_manifest.json``
    ⇒ 事后读表的人不必回头翻代码。
"""
from __future__ import annotations

import csv
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "EVAL_PACK_FILES",
    "LIVE_FILLS_COLUMNS",
    "build_candidate_pool_rows",
    "build_ta_ledger_rows",
    "build_fill_rows",
    "build_cost_assumption",
    "build_nav_rows",
    "normalize_verdict",
    "write_eval_pack",
]

#: 本包产出的 6 件（**契约 16 件之外的过程件** ⇒ 不进契约校验、不影响交付闸门）
EVAL_PACK_FILES: tuple[str, ...] = (
    "eval_candidate_pool.csv",
    "eval_ta_shadow_ledger.csv",
    "live_fills.csv",
    "cost_assumption.json",
    "nav_history_snapshot.csv",
    "eval_pack_manifest.json",
)

#: 三档切分的**分位边界**（用户方法论文档的口径：19 只 → 前 6 / 7~13 / 14~19）
#:    通用化：high = 前 32%、mid = 中间 36%、low = 剩下 ⇒ 19 只正好 6 / 7 / 6。
_TERCILE_HIGH_PCT = 0.32
_TERCILE_MID_PCT = 0.68

#: TA 方向 → 归一化裁决（**键长降序**匹配 ⇒ "不买" 不会被 "买" 抢走）
_VERDICT_KEYS: tuple[tuple[str, str], ...] = (
    ("不建仓", "否决"),
    ("否决", "否决"),
    ("不买", "否决"),
    ("不建", "否决"),
    ("加仓", "放行买入"),
    ("买入", "放行买入"),
    ("减仓", "放行减仓"),
    ("减持", "放行减仓"),
    ("卖出", "放行减仓"),
    ("持有", "放行持有"),
    ("减", "放行减仓"),
    ("卖", "放行减仓"),
    ("买", "放行买入"),
)

#: ``live_fills.csv`` 的表头（**人工回填**这几列 ⇒ 才能算实测滑点）
LIVE_FILLS_COLUMNS: tuple[str, ...] = (
    "signal_date",
    "execution_date",
    "code",
    "name",
    "side",
    "planned_shares",
    "planned_price",
    "planned_amount",
    "planned_fee",
    "planned_slippage",
    "fill_status",
    "actual_shares",
    "actual_price",
    "actual_amount",
    "actual_commission",
    "actual_transfer_fee",
    "note",
)

#: ``eval_candidate_pool.csv`` 的列（**刻意对齐** v2/attribution.py 的 diag 接口）
POOL_COLUMNS: tuple[str, ...] = (
    "date",
    "code",
    "name",
    "e_score",
    "rank",
    "final_score",
    "selected",
    "not_selected_reason",
    "coverage",
    "coverage_ok",
    "liquidity_ok",
    "tradable",
    "above_ma60",
    "entry_ma_ok",
    "exit_ma_ok",
    "broken",
    "mom20",
    "mom60",
    "mom120",
    "momentum_score",
    "n_constituents",
    "n_constituents_scored",
    "theme_id",
    "is_defensive",
    "tercile",
    "is_held_after",
)

#: ``eval_ta_shadow_ledger.csv`` 的列
LEDGER_COLUMNS: tuple[str, ...] = (
    "code",
    "name",
    "rule_rank",
    "rule_score",
    "rule_e_score",
    "rule_selected",
    "in_candidate_pool",
    "ta_verdict",
    "ta_verdict_source",
    "ta_confidence",
    "ta_target_pct",
    "ta_reason",
    "ta_final_parsed",
    "in_ta_screening_rejected",
    "screening_agrees",
    "disagreement_note",
    "divergent_views",
    "final_action",
    "final_weight",
    "final_shares",
    "promoted_as",
    "held_after",
)


# ----------------------------------------------------------------- 小工具
def _read_json(path: Path) -> dict:
    """读 JSON；文件不存在 / 坏了 ⇒ 返回 ``{}``（**不抛**）。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_csv(path: Path, rows: Sequence[dict], columns: Sequence[str]) -> Path:
    """写 CSV（**utf-8-sig** ⇒ Excel 双击不乱码；空表也写表头）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    return path


def _fnum(value: Any, default: float | None = None) -> float | None:
    """安全的 float 转换。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _text(value: Any) -> str:
    """安全转字符串（``None`` ⇒ 空串）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def normalize_verdict(direction: str) -> str:
    """TA 方向词 → 归一化裁决（``否决`` / ``放行买入`` / ``放行减仓`` / ``放行持有``）。

    🔴 空字符串 = **未解析** ⇒ 调用方必须把它当成"未知"，**不许**当成"放行"。
    """
    text = _text(direction)
    if not text:
        return ""
    for key, verdict in _VERDICT_KEYS:
        if key in text:
            return verdict
    return ""


def _tercile(rank: int, n_total: int) -> str:
    """按 rank 切三档（1 = 最高分）。"""
    if n_total <= 0 or rank <= 0:
        return ""
    high_cut = max(1, round(n_total * _TERCILE_HIGH_PCT))
    mid_cut = max(high_cut, round(n_total * _TERCILE_MID_PCT))
    if rank <= high_cut:
        return "高"
    if rank <= mid_cut:
        return "中"
    return "低"


# ------------------------------------------------------- ① 候选池归因表
def build_candidate_pool_rows(
    analysis_report: dict,
    *,
    asof: str,
    held_codes: Iterable[str] = (),
) -> list[dict]:
    """从 ``etf_analysis_report.json`` 派生**候选池归因表**。

    🔴 **列名刻意对齐** ``manu/etf_rotation/v2/attribution.py::rebalance_decomposition``
    需要的 ``date / code / coverage_ok / liquidity_ok / tradable / selected``
    ⇒ 这张表可以直接喂现成的等权池基准函数（**不许另写一套口径**）。
    """
    candidates = analysis_report.get("candidates") or []
    held = {str(c) for c in held_codes}
    n_total = len(candidates)
    rows: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("code"))
        rank = int(_fnum(item.get("rank"), 0) or 0)
        coverage = _fnum(item.get("coverage"))
        rows.append({
            "date": asof,
            "code": code,
            "name": _text(item.get("name")),
            "e_score": _fnum(item.get("e_score"), ""),
            "rank": rank,
            "final_score": _fnum(item.get("final_score"), ""),
            "selected": bool(item.get("selected")),
            "not_selected_reason": _text(item.get("not_selected_reason")),
            "coverage": coverage if coverage is not None else "",
            # 🔴 候选池成员**按定义**已过流动性 / 可交易 / 池属性三道闸
            #    （漏斗见 `filter_stats.stages`）⇒ 这三个布尔恒为 True。
            "coverage_ok": True,
            "liquidity_ok": True,
            "tradable": True,
            "above_ma60": item.get("above_ma60"),
            "entry_ma_ok": item.get("entry_ma_ok"),
            "exit_ma_ok": item.get("exit_ma_ok"),
            "broken": item.get("broken"),
            "mom20": _fnum(item.get("mom20"), ""),
            "mom60": _fnum(item.get("mom60"), ""),
            "mom120": _fnum(item.get("mom120"), ""),
            "momentum_score": _fnum(item.get("momentum_score"), ""),
            "n_constituents": item.get("n_constituents"),
            "n_constituents_scored": item.get("n_constituents_scored"),
            "theme_id": _text(item.get("theme_id")),
            "is_defensive": item.get("is_defensive"),
            "tercile": _tercile(rank, n_total),
            "is_held_after": code in held,
        })
    return rows


# ------------------------------------------------------- ③ TA 影子账本
def build_ta_ledger_rows(
    analysis_report: dict,
    plan: dict,
    *,
    held_codes: Iterable[str] = (),
) -> list[dict]:
    """派生 **TA 逐票影子账本**（模块级消融的输入）。

    🔴 **口径纪律（本模块最重要的一条）**：
    ``final_rebalance_plan.json:ta_screening.rejected`` **不可信** ——
    实测 2026-11-26 那轮 5 条里 **2 条误判**（512800 / 159928），
    根因是"从散文里刮否决词"会把**邻居标的**的否决词张冠李戴。
    ⇒ 本表的 ``ta_verdict`` **以 TA 最终裁决为准**，优先级：
       ① ``llm_final_decision.targets``（Trader 最终裁决，最高置信）
       ② ``llm_targets``（逐票方向表，中等置信）
       ③ ``ta_screening.rejected``（管道口径，**最低**置信，只作对照）
    并用 ``screening_agrees`` / ``disagreement_note`` 两列**显式暴露分歧**。
    """
    held = {str(c) for c in held_codes}
    candidates = {
        _text(c.get("code")): c
        for c in (analysis_report.get("candidates") or [])
        if isinstance(c, dict)
    }
    screening = plan.get("ta_screening") or {}
    rejected_by_pipe = {
        _text(r.get("code")): r
        for r in (screening.get("rejected") or [])
        if isinstance(r, dict)
    }
    llm_targets = plan.get("llm_targets") or {}
    final_targets = ((plan.get("llm_final_decision") or {}) or {}).get("targets") or {}
    # 顺延补位：谁顶替了谁
    promoted_for: dict[str, str] = {}
    for sub in (screening.get("substituted") or []):
        if isinstance(sub, dict):
            promoted_for[_text(sub.get("substituted_for"))] = _text(sub.get("code"))
    # 最终计划里的动作与落地权重
    action_by_code: dict[str, dict] = {}
    for act in (plan.get("actions") or []):
        if isinstance(act, dict):
            action_by_code[_text(act.get("code"))] = act
    # 逐票方向表里的分歧视角（bull / bear 各说各话）
    conflicts_by_code = {
        _text(c.get("code")): c
        for c in (plan.get("llm_conflicts") or [])
        if isinstance(c, dict)
    }

    codes: list[str] = []
    for source in (final_targets, llm_targets, rejected_by_pipe, action_by_code):
        for code in source:
            code = _text(code)
            if code and code not in codes:
                codes.append(code)
    # 顺延补位的**两端**都要进账本（顶替者 + 被顶替者）
    for code in promoted_for:
        if code and code not in codes:
            codes.append(code)
    for sub in (screening.get("substituted") or []):
        if isinstance(sub, dict):
            code = _text(sub.get("code"))
            if code and code not in codes:
                codes.append(code)

    rows: list[dict] = []
    for code in codes:
        cand = candidates.get(code) or {}
        final = final_targets.get(code) or {}
        agent = llm_targets.get(code) or {}
        pipe = rejected_by_pipe.get(code) or {}
        act = action_by_code.get(code) or {}
        conflict = conflicts_by_code.get(code) or {}

        verdict, source, confidence = "", "", "none"
        if isinstance(final, dict) and final:
            verdict = normalize_verdict(_text(final.get("direction")))
            source = "llm_final_decision.targets"
            confidence = "high"
        if not verdict and isinstance(agent, dict) and agent:
            verdict = normalize_verdict(_text(agent.get("direction")))
            source = "llm_targets"
            confidence = "medium"
        if not verdict and pipe:
            verdict = "否决"
            source = "ta_screening.rejected"
            confidence = "low"
        if not verdict:
            source, confidence = "none", "none"

        reason = ""
        if isinstance(final, dict) and final:
            reason = _text(final.get("evidence")) or _text(final.get("note"))
        if not reason and isinstance(agent, dict) and agent:
            reason = _text(agent.get("reason"))
        if not reason and pipe:
            reason = _text(pipe.get("reason"))

        pipe_says_rejected = code in rejected_by_pipe
        real_says_rejected = verdict == "否决"
        if not verdict:
            # 🔴 **没有可解析的裁决** ⇒ 既不能说"一致"也不能说"不一致" ⇒ 留空（未知）
            agrees = None
            note = "⚠️ **无可解析的 TA 裁决**（不在最终裁决表、不在逐票方向表）"
        elif pipe_says_rejected and not real_says_rejected:
            agrees = False
            note = (
                f"🔴 分歧：`ta_screening.rejected` 记为**否决**（管道口径），"
                f"但 TA 最终裁决是**{verdict or '未解析'}** ⇒ **以 TA 原文为准**"
            )
        elif real_says_rejected and not pipe_says_rejected:
            agrees = False
            note = "🟡 分歧：TA 原文是**否决**，但 `ta_screening.rejected` 里没有它"
        else:
            agrees = True
            note = ""

        target_pct = None
        if isinstance(final, dict):
            target_pct = _fnum(final.get("target_pct"))
        if target_pct is None and isinstance(agent, dict):
            target_pct = _fnum(agent.get("target_pct"))

        rows.append({
            "code": code,
            "name": (
                _text(cand.get("name"))
                or _text(act.get("name"))
                or _text(pipe.get("name"))
            ),
            "rule_rank": cand.get("rank", ""),
            "rule_score": _fnum(cand.get("final_score"), ""),
            "rule_e_score": _fnum(cand.get("e_score"), ""),
            "rule_selected": bool(cand.get("selected")),
            "in_candidate_pool": code in candidates,
            "ta_verdict": verdict,
            "ta_verdict_source": source,
            "ta_confidence": confidence,
            "ta_target_pct": target_pct if target_pct is not None else "",
            "ta_reason": reason,
            "ta_final_parsed": bool(final),
            "in_ta_screening_rejected": pipe_says_rejected,
            "screening_agrees": agrees,
            "disagreement_note": note,
            "divergent_views": _text(conflict.get("llm_reason")),
            "final_action": _text(act.get("action")),
            "final_weight": _fnum(act.get("target_weight"), "") if act else "",
            "final_shares": act.get("target_shares", "") if act else "",
            "promoted_as": promoted_for.get(code, ""),
            "held_after": code in held,
        })
    rows.sort(key=lambda r: (r.get("rule_rank") in ("", None), r.get("rule_rank") or 0))
    return rows


# ------------------------------------------------------- ④ 实盘成交台账
def build_fill_rows(card: dict, *, asof: str, live_trade: bool = False) -> list[dict]:
    """从执行卡派生**实盘成交台账**（**待人工回填**）。

    🔴 只有**计划侧**是机器写的（股数 / 参考价 / 预估费用）；
    ``actual_*`` 五列**必须人工回填**（券商成交回单）⇒ 才有实测滑点。
    """
    execution_date = _text(card.get("execution_date"))
    rows: list[dict] = []
    for order in (card.get("orders") or []):
        if not isinstance(order, dict):
            continue
        shares = order.get("delta_shares")
        rows.append({
            "signal_date": asof,
            "execution_date": execution_date,
            "code": _text(order.get("code")),
            "name": _text(order.get("name")),
            "side": _text(order.get("action")),
            "planned_shares": shares if shares not in (None, "") else "",
            "planned_price": _fnum(order.get("price"), ""),
            "planned_amount": _fnum(order.get("est_amount"), ""),
            "planned_fee": _fnum(order.get("est_fee"), ""),
            "planned_slippage": _fnum(order.get("est_slippage"), ""),
            "fill_status": "待回填" if live_trade else "未成交（观察态不下单）",
            "actual_shares": "",
            "actual_price": "",
            "actual_amount": "",
            "actual_commission": "",
            "actual_transfer_fee": "",
            "note": _text(order.get("reason")),
        })
    return rows


# ------------------------------------------------------- ④ 成本口径单一出处
def _research_chain_costs() -> dict:
    """读**研究链**的成本口径（``manu/etf_rotation/config.py``）⇒ 读不到就留空。

    🔴 读不到**不许编** ⇒ 返回 ``{"available": False, "note": ...}``。
    """
    here = Path(__file__).resolve()
    # 🔴 候选路径要覆盖**两份镜像**（``manu/etf_rotation/daily/`` 与 ``manu/daily/``）
    candidates = (
        here.parents[1] / "config.py",
        here.parents[1] / "etf_rotation" / "config.py",
        here.parents[2] / "manu" / "etf_rotation" / "config.py",
        here.parents[2] / "etf_rotation" / "config.py",
    )
    # 🔴 **逐个候选试**：不同路径下的 ``config.py`` 会按**自身位置**解析相对 import
    #    （仓库里 ``manu/config.py`` 与 ``manu/etf_rotation/config.py`` 是两份镜像）
    #    ⇒ 某个候选可能**加载失败** ⇒ 不能只看"文件存在"，必须**加载成功且带对常量**。
    path = None
    module = None
    errors: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "etf_research_cfg_for_eval", candidate
            )
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
            continue
        if _fnum(getattr(loaded, "ETF_COMMISSION", None)) is None:
            errors.append(f"{candidate}: 缺 ETF_COMMISSION")
            continue
        path, module = candidate, loaded
        break
    if module is None:
        return {
            "available": False,
            "note": (
                "研究链 config 不可读（候选："
                f"{[str(c) for c in candidates]}；失败详情：{errors}）"
            ),
        }
    commission = _fnum(getattr(module, "ETF_COMMISSION", None))
    slippage = _fnum(getattr(module, "ETF_SLIPPAGE", None))
    min_commission = _fnum(getattr(module, "ETF_MIN_COMMISSION", None))
    strict = _fnum(getattr(module, "ETF_MIN_COMMISSION_STRICT", None))
    if commission is None or slippage is None:
        return {
            "available": False,
            "note": "研究链 config 缺少 ETF_COMMISSION / ETF_SLIPPAGE",
        }
    return {
        "available": True,
        "source": str(path),
        "commission_one_side": commission,
        "slippage_one_side": slippage,
        "cost_rate_one_side": round(commission + slippage, 10),
        "min_commission_cashflow": min_commission,
        "min_commission_strict_disclosure_only": strict,
        "note": (
            "🔴 研究链**现金流**用 `ETF_MIN_COMMISSION=0.0`（**免 5 元下限**）；"
            "`ETF_MIN_COMMISSION_STRICT=5.0` 只用于**门槛披露**、**不参与**现金流"
            " ⇒ 回测口径对本账户**系统性低估**真实摩擦"
        ),
    }


def build_cost_assumption(
    card: dict,
    node_reprice: dict,
    *,
    asof: str,
    plan: dict | None = None,
) -> dict:
    """把**三档成本口径**并列到一处（**单一权威出处**）。

    🔴 本件**不改任何既有件的费用计算**（那是生产口径的事）⇒ 只做**并列披露 + 缺口量化**。
    """
    constraints = card.get("constraints") or {}
    card_cash = card.get("cash_flow") or {}
    node_cash = node_reprice.get("cash_flow") or {}
    research = _research_chain_costs()

    card_rate = _fnum(constraints.get("commission_rate"))
    card_min = _fnum(constraints.get("min_commission"))
    card_slip = _fnum(constraints.get("slippage_rate"))
    stamp = _fnum(constraints.get("stamp_duty_rate"))

    layers: dict[str, Any] = {
        "research_backtest": research,
        "execution_card": {
            "commission_rate": card_rate,
            "min_commission": card_min,
            "slippage_rate": card_slip,
            "stamp_duty_rate": stamp,
            "note": (
                "🔴 每日链执行卡口径：**含 5 元最低佣金下限**；"
                "ETF 买卖双向免印花税 ⇒ 卖出侧**不再叠加印花税**（与研究链口径同源）"
            ),
        },
        "node_reprice": {
            "buy_fees": _fnum(node_cash.get("buy_fees")),
            "sell_fees": _fnum(node_cash.get("sell_fees")),
            "cash_end": _fnum(node_cash.get("cash_end")),
            "note": "🔴 节点重算件口径：按 ``cost_rate`` 直算，**无 5 元下限** ⇒ 与执行卡不一致",
        },
        "final_plan_actions": {
            "note": "🔴 计划件 ``actions[].est_fee`` 同样按直算口径（与 node_reprice 一致）",
        },
    }

    plan_actions = plan.get("actions") if isinstance(plan, dict) else None
    plan_fee_by_code: dict[str, float | None] = {}
    for act in plan_actions or []:
        if isinstance(act, dict):
            plan_fee_by_code[_text(act.get("code"))] = _fnum(act.get("est_fee"))

    # 逐单量化：同一笔交易在几档口径下的费用差
    per_order: list[dict] = []
    research_rate = research.get("cost_rate_one_side") if research.get("available") else None
    for order in (card.get("orders") or []):
        if not isinstance(order, dict):
            continue
        amount = _fnum(order.get("est_amount"))
        if not amount:
            continue
        exec_fee = _fnum(order.get("est_fee"))
        exec_fee = 0.0 if exec_fee is None else exec_fee
        plan_fee = plan_fee_by_code.get(_text(order.get("code")))
        backtest_fee = round(amount * research_rate, 6) if research_rate is not None else None
        gap_bp = None
        if backtest_fee is not None and amount:
            gap_bp = round((exec_fee - backtest_fee) / amount * 10000.0, 4)
        per_order.append({
            "code": _text(order.get("code")),
            "name": _text(order.get("name")),
            "amount": amount,
            "execution_card_fee": exec_fee,
            "execution_card_fee_bp": round(exec_fee / amount * 10000.0, 4) if amount else None,
            "plan_or_node_fee": plan_fee,
            "backtest_fee_at_research_rate": backtest_fee,
            "gap_bp_vs_backtest": gap_bp,
        })

    disagreements: list[str] = []
    card_fees = card_cash.get("buy_fees")
    node_fees = node_cash.get("buy_fees")
    if _fnum(card_fees) is not None and _fnum(node_fees) is not None:
        if abs(float(card_fees) - float(node_fees)) > 1e-6:
            delta_cash = float(card_cash.get("cash_end") or 0) - float(node_cash.get("cash_end") or 0)
            disagreements.append(
                f"🔴 **同一笔买入的两件费用不一致**：执行卡 `buy_fees`={card_fees} vs "
                f"节点件 `buy_fees`={node_fees}（差 {float(card_fees) - float(node_fees):.6f} 元）"
                f" ⇒ 现金终值差 {delta_cash:.6f} 元"
            )
    if research.get("available") and card_min and research_rate:
        threshold = card_min / research_rate
        disagreements.append(
            f"🔴 **5 元下限的统治区间**：单笔成交额 < {threshold:,.2f} 元时，"
            f"实盘佣金恒为 5.00 元 ⇒ **回测的 {research_rate} 成本率对本账户系统性低估**"
        )

    return {
        "asof": asof,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "purpose": (
            "四个评估维度里『部署物理损耗』的成本口径**单一权威出处**"
            "（并列披露，不改既有件）"
        ),
        "layers": layers,
        "per_order": per_order,
        "disagreements": disagreements,
        "note": (
            "🔴 本件是**披露件**，不是**改口径件** —— 任何一处费用的修改都必须走"
            "「回测口径冻结件 → 显式重生成」流程，不许在快照层偷改"
        ),
    }


# ------------------------------------------------------- NAV 序列快照
def build_nav_rows(nav_state: dict, nav_history: dict, *, asof: str) -> list[dict]:
    """把**净值序列**落成快照内的一份 CSV，并**标记脏点**。

    🔴 实测脏点：**序列里出现未来日期**（如 2026-12-01，比 asof 还晚）
    ⇒ 回撤 / Sharpe 会被污染 ⇒ **只标记、不静默删除**（留着给人工判断）。
    """
    points = nav_history.get("points") if isinstance(nav_history, dict) else None
    if not isinstance(points, dict):
        points = {}
    rows: list[dict] = []
    for date_key, value in sorted(points.items()):
        date_text = _text(date_key)
        nav = _fnum(value)
        is_future = bool(date_text and date_text > asof)
        note = ""
        if is_future:
            note = "🔴 未来日期 ⇒ 脏点（会污染回撤 / Sharpe）"
        rows.append({
            "date": date_text,
            "nav": nav if nav is not None else "",
            "is_future": is_future,
            "is_asof": date_text == asof,
            "note": note,
        })
    if rows:
        return rows
    current_nav = _fnum(nav_state.get("nav"))
    current_peak = _fnum(nav_state.get("peak"))
    current_dd = _fnum(nav_state.get("drawdown"))
    return [{
        "date": asof,
        "nav": current_nav if current_nav is not None else "",
        "is_future": False,
        "is_asof": True,
        "note": (
            f"⚠️ 净值序列文件不可读 ⇒ 只落当前点（peak={current_peak} drawdown={current_dd}）"
        ),
    }]


# ------------------------------------------------------- 落盘入口
def write_eval_pack(
    snapshot_dir: str | Path,
    *,
    asof: str,
    nav_history: dict | None = None,
    live_trade: bool = False,
) -> dict:
    """把评估就绪包写进快照目录 ⇒ 返回摘要（**不抛**）。

    🔴 **P2 fail-soft**：本函数对**单件**失败是容忍的（跳过那件、记进 ``warnings``）；
    调用方（``main.py``）再把整体结果登记进 manifest 的 ``stages``。
    """
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    analysis = _read_json(root / "etf_analysis_report.json")
    plan = _read_json(root / "final_rebalance_plan.json")
    card = _read_json(root / ("execution_card_" + asof.replace("-", "") + ".json"))
    node = _read_json(root / ("node_reprice_" + asof.replace("-", "") + ".json"))
    nav_state = _read_json(root / "nav_state.json")

    # 落地持仓（用于「最终是否持有」列）
    held_codes: list[str] = []
    for pos in ((card.get("post_trade") or {}).get("positions") or []):
        if isinstance(pos, dict) and _fnum(pos.get("shares"), 0):
            held_codes.append(_text(pos.get("code")))

    written: list[str] = []

    # ① 候选池归因表
    try:
        rows = build_candidate_pool_rows(analysis, asof=asof, held_codes=held_codes)
        _write_csv(root / "eval_candidate_pool.csv", rows, POOL_COLUMNS)
        written.append("eval_candidate_pool.csv")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"eval_candidate_pool.csv 生成失败：{type(exc).__name__}: {exc}")

    # ③ TA 影子账本
    try:
        rows = build_ta_ledger_rows(analysis, plan, held_codes=held_codes)
        _write_csv(root / "eval_ta_shadow_ledger.csv", rows, LEDGER_COLUMNS)
        written.append("eval_ta_shadow_ledger.csv")
        n_disagree = sum(1 for r in rows if r.get("screening_agrees") is False)
        n_unknown = sum(1 for r in rows if r.get("screening_agrees") is None)
        if n_disagree:
            warnings.append(
                f"🔴 TA 影子账本与 `ta_screening.rejected` 有 **{n_disagree}** 条分歧"
                "（账本以 TA 最终裁决为准；详见 `disagreement_note` 列）"
            )
        if n_unknown:
            warnings.append(
                f"🟡 TA 影子账本里有 **{n_unknown}** 行**无可解析裁决**"
                "（TA 原文提到、但机器没解析出方向）"
            )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"eval_ta_shadow_ledger.csv 生成失败：{type(exc).__name__}: {exc}")

    # ④ 实盘成交台账（模板）
    try:
        rows = build_fill_rows(card, asof=asof, live_trade=live_trade)
        _write_csv(root / "live_fills.csv", rows, LIVE_FILLS_COLUMNS)
        written.append("live_fills.csv")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"live_fills.csv 生成失败：{type(exc).__name__}: {exc}")

    # ④ 成本口径单一出处
    try:
        payload = build_cost_assumption(card, node, asof=asof, plan=plan)
        (root / "cost_assumption.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        written.append("cost_assumption.json")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cost_assumption.json 生成失败：{type(exc).__name__}: {exc}")

    # NAV 序列快照
    try:
        if nav_history is None:
            nav_history = _load_nav_history()
        rows = build_nav_rows(nav_state, nav_history, asof=asof)
        _write_csv(
            root / "nav_history_snapshot.csv",
            rows,
            ("date", "nav", "is_future", "is_asof", "note"),
        )
        written.append("nav_history_snapshot.csv")
        n_dirty = sum(1 for r in rows if r.get("is_future"))
        if n_dirty:
            warnings.append(
                f"🔴 净值序列里有 **{n_dirty}** 个未来日期脏点"
                "（已标记 `is_future=True`，**未删除**）"
            )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"nav_history_snapshot.csv 生成失败：{type(exc).__name__}: {exc}")

    # 包自身的说明（口径都在这里 ⇒ 事后读表不必翻代码）
    manifest = {
        "schema_version": 1,
        "asof": asof,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "live_trade": bool(live_trade),
        "purpose": (
            "事后系统有效性评估的**现场证据入库**"
            "（四个维度：傻瓜基线 / 单调性 / TA 消融 / 真实摩擦）"
        ),
        "invariants": {
            "P1_zero_side_effect": "不改任何决策、不改任何既有交付件、不写根目录全局文件",
            "P2_fail_soft": "单件失败只登记 warnings，不阻断流水线",
            "P3_stdlib_only": "纯标准库（不 import pandas / numpy）",
            "P4_caliber_in_manifest": "每件的口径写在本文件里",
        },
        "files": {
            "eval_candidate_pool.csv": {
                "purpose": "维度①（等权池基准）＋ 维度②（三档单调性）的输入",
                "grain": "一行 = 一只候选池 ETF（asof 当日）",
                "columns": list(POOL_COLUMNS),
                "directly_consumable_by": (
                    "manu/etf_rotation/v2/attribution.py::rebalance_decomposition"
                    "（需 date/code/coverage_ok/liquidity_ok/tradable/selected 六列，本表已按此命名）"
                ),
                "caliber": {
                    "e_score": "E = W × S 穿透分（**绝对值**，可跨期比）",
                    "final_score": "池内 z-score（**单调等价变换** ⇒ 分档用 e_score 亦可）",
                    "tercile": "按 rank 切三档（19 只 ⇒ 6 / 7 / 6）",
                    "coverage_ok/liquidity_ok/tradable": "候选池成员**按定义**恒为 True",
                    "is_held_after": "执行卡 `post_trade.positions` 里股数 > 0",
                },
            },
            "eval_ta_shadow_ledger.csv": {
                "purpose": "维度③（TA 模块级消融）的**逐票影子账本**",
                "grain": "一行 = 一个被规则层 / TA / 计划任一方提到的 ETF",
                "columns": list(LEDGER_COLUMNS),
                "verdict_priority": [
                    "llm_final_decision.targets（高置信）",
                    "llm_targets（中置信）",
                    "ta_screening.rejected（低置信，**只作对照**）",
                ],
                "known_trap": (
                    "🔴 `ta_screening.rejected` 会把**邻居标的**的否决词张冠李戴"
                    "（实测 2026-11-26：512800 / 159928 误判）⇒ **不要直接读它建账本**"
                ),
            },
            "live_fills.csv": {
                "purpose": "维度④（真实摩擦）的**人工回填台账**",
                "grain": "一行 = 一条执行卡订单",
                "manual_columns": [
                    "fill_status",
                    "actual_shares",
                    "actual_price",
                    "actual_amount",
                    "actual_commission",
                    "actual_transfer_fee",
                ],
                "caliber": "actual_* 五列**只能来自券商成交回单** ⇒ 不许用参考价回填",
            },
            "cost_assumption.json": {
                "purpose": "维度④的成本口径**单一权威出处**（三档并列 + 缺口量化）",
                "layers": [
                    "research_backtest",
                    "execution_card",
                    "node_reprice",
                    "final_plan_actions",
                ],
            },
            "nav_history_snapshot.csv": {
                "purpose": "净值序列的快照内副本（算回撤 / Sharpe 用）",
                "caliber": "is_future=True ⇒ **脏点**（会污染回撤 / Sharpe）；**只标记不删除**",
            },
        },
        "gaps_still_open": [
            "🔴 **实盘成交价与实扣佣金没有** ⇒ `live_fills.csv` 的 actual_* 五列待人工回填",
            "🔴 **观察态快照不产生成交** ⇒ 执行卡订单**从未下单**",
            "🔴 **asof 之后的价格不在快照里** ⇒ 事后评估须另拉行情",
            "🔴 净值序列**点数不足**（约 11 点 + 未来日期脏点）⇒ Sharpe / 回撤暂不可靠",
        ],
        "warnings": warnings,
    }
    (root / "eval_pack_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    written.append("eval_pack_manifest.json")

    return {
        "files": written,
        "n_warnings": len(warnings),
        "warnings": warnings,
        "n_pool_rows": len(analysis.get("candidates") or []),
    }


def _load_nav_history() -> dict:
    """读净值序列文件（**读不到 ⇒ 空 dict**，不抛）。"""
    try:
        import daily_config as DC  # noqa: PLC0415

        path = Path(DC.NAV_STATE_FILE)
    except Exception:  # noqa: BLE001
        return {}
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
