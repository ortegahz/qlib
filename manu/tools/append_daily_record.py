#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_daily_record.py —— 把「不可从开源数据复现的核心信息」落成**可复盘的事实记录**。

【为什么有这个工具（铁律 §十四.122，2026-11-19 用户亲定）】
用户原话：
> 「你写memory的时候一定要注意记录核心信息，比如当天etf的权重等，方便我几个月以后复盘，
> 这些都是你在akshare开源数据接口拿不到的信息，你自己考虑需要保存哪些附加核心信息，
> 把这一条写到rule里面，作为铁律。」
> 「不一定写memory,也可以单独开个record什么的，记录。」

⇒ akshare / qlib 只有**行情与基本面**，「我当天为什么买 / 没买、买多少、为什么被否」
**事后压根拿不到** ⇒ 必须**当轮落成事实记录**。

【本工具做什么】
输入 = 一次**不可变快照**目录（``<RESULTS_ROOT>/snapshots/<run_id>/``），
输出 =
  ① ``manu/memory/records/etf_rotation_daily_ledger.csv`` —— **逐日台账**
     （一行 = 一只 ETF 一天；**append-only**；已存在的 (日期,代码) 行**不改写**，
      口径变了就**追加新行**并把旧行标成「作废」⇒ 保留完整演化轨迹）；
  ② ``manu/memory/records/<YYYYMMDD>.md`` —— 当日**人读记录**（目标 vs 实际分列）。
     段落：一、当日标的／二、现金流与落地／三、门槛与纪律／
     **四、持仓明细与净值口径**／**五、执行口径**／六、来源。
     🔴 四、五两段专门装「akshare 等开源接口**事后拿不到**」的口径事实
     （成本额 / 浮盈 / 收盘口径净值 / 账户三价差披露 / 执行盘口）⇒ 复盘时**不必猜**。
     🔴 另含**三类「双口径／陈旧度」披露**（2026-11-20 第四轮补齐；
        🔴 **只进当日记录 md，不进台账 30 列** ⇒ 台账列集恒定）：
       ① **成分股快照 W 的陈旧度** —— 取 ``manifest.stages[]`` 里 ``name == "w_holdings"`` 的项
          （``status`` / ``max_fetch_date`` / ``stale_days`` / ``n_codes``）。
          🔴 它的状态是 ``stale``，而记录里的「失败环节」只认 ``failed``/``error``/``degraded``
          ⇒ **旧口径永远显示不出它** ⇒ 必须单列（否则以为「成分股是当天的」）。
       ② **费率口径分歧** —— 计划件 ``meta.commission_bp`` / ``meta.slippage_bp``
          vs 执行卡 ``constraints.commission_rate`` / ``constraints.slippage_rate``（**小数** ⇒ ×10000 换 bp）。
          🔴 实测两者**确实不一致**（计划件 1.5bp/5bp vs 执行卡 3bp/1bp）⇒ 只写一套 ⇒ 算不出真实摩擦成本。
       ③ **现金流双口径** —— 计划件 ``cash_flow`` vs 执行卡 ``cash_flow``。
          🔴 同一天、同一张计划，**执行卡与计划件各有一套** ⇒ 期末现金可能不等
          （实测 2026-11-18 差 **−7.22 元**、2026-11-19 无交易差 **0.00**）⇒ 两套都要留痕 ＋ 差额行。
     ⇒ 三类都有**常驻守卫**（``manu/etf_rotation/tests/test_daily_record_disclosure_guard.py``，
       与 ``manu/tests/`` 逐字一致）⇒ 删掉任一披露 ⇒ 守卫**转红**。

【口径纪律（铁律：数字不许编）】
* 所有数字**只从快照产物里读**（执行卡 / 计划 JSON / manifest / 冻结参数 / 账户事实源）；
  读不到 ⇒ 写 ``缺``，**不许**猜、**不许**事后补算。
* 每行都带**来源**（快照目录 / 报告文件名）。
* 观察态（``live_trade=False``）⇒ 「是否成交」一律写 ``否（观察态不下单）``，
  成交价 / 成交金额写 ``不适用`` ⇒ **不许**把「计划」冒充「成交」。

【用法】
    python3 manu/tools/append_daily_record.py --snapshot-dir <快照目录> [--dry-run]
    python3 manu/tools/append_daily_record.py --snapshot-dir <快照目录> \\
        --records-dir manu/memory/records
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

#: 台账表头（**append-only**；顺序即列序，**不许**中途插列）
LEDGER_HEADER = (
    "日期", "记录时间", "记录状态", "代码", "名称", "动作",
    "当前股数", "当前权重", "目标权重", "目标权重生效",
    "目标金额", "目标股数", "计划金额",
    "是否成交", "成交价", "成交金额",
    "否决理由", "否决出处", "顺延补位者", "被顶替的标的", "说明",
    "权益上限(规则硬)", "权益上限(本轮生效)", "权益超配幅度",
    "账户总资产", "账户现金",
    "是否走兜底", "失败环节", "V2参数", "来源",
)

#: 缺失值的统一写法（**明确缺失**，不许留空让人误读成 0）
MISSING = "缺"

#: 台账里**每轮都会变**的字段 ⇒ 幂等比较时**不计入内容签名**
VOLATILE_FIELDS = ("记录时间", "记录状态")


def _row_signature(row: dict) -> tuple:
    """行内容的**指纹**（不含每轮都变的字段）⇒ 判断「是否同一事实」。"""
    return tuple(str(row.get(key, "")) for key in LEDGER_HEADER if key not in VOLATILE_FIELDS)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pick(obj: dict, *names, default=MISSING):
    """按候选名依次取键；取不到用 ``default``（**不猜值**）。"""
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
    return default


def _fmt(value) -> str:
    if value is None:
        return MISSING
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") if value else "0"
    return str(value)


def _fmt_money(value) -> str:
    """金额：**带符号 + 两位小数**（差额必须看得出方向）；不是数字 ⇒ ``缺``。"""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):+.2f}"
    return str(value)


def _is_number(value) -> bool:
    """是不是**实数**（``bool`` 不算数；字符串数字不算 ⇒ 缺就是缺）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _cell(value) -> str:
    """表格**单元格**文本：竖线转义（不许把表格撑歪）＋ 换行压平。"""
    if value is None:
        return MISSING
    text = value if isinstance(value, str) else str(value)
    text = text.replace("|", "\\|").replace("\r\n", " ").replace("\n", " ")
    return text


def _fmt_value(value) -> str:
    """通用取值：``dict``/``list`` 走 JSON（不许 Python repr），其余走 ``_fmt``。"""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _fmt(value)


def _caliber_label(text) -> str:
    """从一句盘口描述里**派生**标签（``开盘`` / ``收盘`` / ``未声明``）。

    🔴 **不许写死**：降级卡的 ``execution_basis`` 可能是 ``plan_only（节点日重算未完成）``
    ⇒ 那就老实写「未声明」，不许替它编一个盘口出来。
    """
    if not isinstance(text, str) or not text.strip():
        return "未声明"
    if "开盘" in text:
        return "开盘"
    if "收盘" in text:
        return "收盘"
    return "未声明"


def _date(value):
    """``YYYY-MM-DD`` → ``date``；解析不了 ⇒ ``None``（不许瞎算天数）。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def collect(snapshot_dir: Path) -> dict:
    """从快照目录抽取「当日核心事实」。**只读已有产物**，缺就标缺。"""
    plan = _read_json(snapshot_dir / "final_rebalance_plan.json") or {}
    manifest = _read_json(snapshot_dir / "snapshot_manifest.json") or {}
    frozen = _read_json(snapshot_dir / "v2_frozen_params.json") or {}
    account = _read_json(snapshot_dir / "account_context.json") or {}
    nav_state = _read_json(snapshot_dir / "nav_state.json") or {}

    # ---- 执行卡：**多张时按 asof 选**，仍不唯一 ⇒ 老实记「缺」（不许静默取错张）----
    exec_card = None
    exec_card_files = sorted(snapshot_dir.glob("execution_card_*.json"))
    if len(exec_card_files) == 1:
        exec_card = _read_json(exec_card_files[0]) or {}
    elif exec_card_files:
        manifest_asof = manifest.get("asof")
        matched = []
        for candidate in exec_card_files:
            card = _read_json(candidate) or {}
            if manifest_asof and card.get("asof") == manifest_asof:
                matched.append(card)
        exec_card = matched[0] if len(matched) == 1 else None

    meta = plan.get("meta") or {}
    screening = plan.get("ta_screening") or {}
    gate = plan.get("min_amount_gate") or {}
    cash_flow = plan.get("cash_flow") or {}

    # ---- V2 冻结参数（一行字符串，方便 grep） --------------------------------
    params = frozen.get("params") or {}
    v2_blob = json.dumps(params, ensure_ascii=False, sort_keys=True) if params else MISSING
    # 🔴 `v2_blob` **只进台账列「V2参数」** ⇒ 此处**一个字都不许动**（动了台账就 churn）。
    #    V2 的「生效口径」（seam / enabled / caveat）另存 ⇒ 只进**人读记录**，不进台账。
    v2_meta = {
        key: frozen.get(key)
        for key in ("seam", "enabled", "winner_arm", "frozen_at", "caveat")
        if key in frozen
    }
    v2_meta_blob = json.dumps(v2_meta, ensure_ascii=False, sort_keys=True) if v2_meta else MISSING

    # ---- 失败 / 降级环节（manifest 是唯一事实源） ----------------------------
    failed = []
    for stage in manifest.get("stages") or []:
        status = str(stage.get("status", ""))
        if status in {"failed", "error", "degraded"}:
            failed.append(f"{stage.get('name')}={status}")
    skipped = [str(s) for s in (manifest.get("skipped") or [])]
    failed_blob = "; ".join(failed + ([f"skipped:{','.join(skipped)}"] if skipped else [])) or "无"

    # ---- 兜底是否触发 -------------------------------------------------------
    fallback = "是" if gate.get("deadlock_risk") or gate.get("risk_overrides") else "否"

    # ---- 权益上限 -----------------------------------------------------------
    cap = screening.get("equity_cap")
    cap_eff = screening.get("equity_cap_effective")

    # ---- 账户口径：快照总资产 vs 收盘口径净值（**两者本来就可能不等**）--------
    total_assets = meta.get("total_assets")
    if total_assets is None:
        total_assets = account.get("total_assets")
    nav = nav_state.get("nav")
    nav_diff = None
    if _is_number(total_assets) and _is_number(nav):
        nav_diff = round(float(nav) - float(total_assets), 6)

    # ---- 回撤口径自校验（组合 drawdown vs 权益 drawdown）--------------------
    drawdown = nav_state.get("drawdown")
    equity_dd = nav_state.get("equity_drawdown_vs_peak")
    dd_mismatch = (
        _is_number(drawdown)
        and _is_number(equity_dd)
        and abs(float(equity_dd) - float(drawdown)) > 0.005
    )

    # ---- 成分股快照 W 的陈旧度（**披露项**，不是失败）-------------------------
    # 🔴 真缺陷（2026-11-20 实测）：manifest 的 `w_holdings` 阶段状态是 **`stale`**，
    #    它**不属于** {failed, error, degraded} ⇒ 上一版「失败/跳过环节」**永远不会**显示它
    #    ⇒ 逐日记录里一个字都没提 ⇒ 复盘时看不见「W 已滞后 15 天」这个关键风险。
    w_stage: dict = {}
    for stage in manifest.get("stages") or []:
        if isinstance(stage, dict) and stage.get("name") == "w_holdings":
            w_stage = stage
            break
    w_status = str(w_stage.get("status") or MISSING)
    w_blob = f"{w_status}｜快照日 {w_stage.get('max_fetch_date') or MISSING}"
    if _is_number(w_stage.get("stale_days")):
        w_blob += f"｜滞后 {int(w_stage['stale_days'])} 天"
    if _is_number(w_stage.get("n_codes")):
        w_blob += f"｜{int(w_stage['n_codes'])} 只"
    w_is_stale = w_status == "stale"

    # ---- 费率口径：计划件 meta vs 执行卡 constraints（**两处可能互不一致**）----
    # 🔴 真缺陷（2026-11-20 实测）：执行卡 `constraints.commission_rate`/`slippage_rate`
    #    与计划件 `meta.commission_bp`/`meta.slippage_bp` **不是同一套费率**
    #    （实测 11-19：计划件 1.5bp/5.0bp vs 执行卡 constraints 3bp/1bp）
    #    ⇒ 记录只写计划件那套 ⇒ 读者以为「费率已对齐」⇒ 复盘算不出真实摩擦成本。
    card_constraints = (exec_card or {}).get("constraints") or {}

    def _rate_to_bp(rate):
        """费率（小数）⇒ bp；不是数字 ⇒ ``None``（缺就是缺）。"""
        return round(float(rate) * 10000, 6) if _is_number(rate) else None

    plan_commission_bp = meta.get("commission_bp")
    plan_slippage_bp = meta.get("slippage_bp")
    card_commission_bp = _rate_to_bp(card_constraints.get("commission_rate"))
    card_slippage_bp = _rate_to_bp(card_constraints.get("slippage_rate"))
    fee_divergences: list[str] = []
    if card_commission_bp is not None and _is_number(plan_commission_bp):
        if abs(card_commission_bp - float(plan_commission_bp)) > 1e-6:
            fee_divergences.append(
                f"佣金：计划件 meta {_fmt(plan_commission_bp)} bp vs 执行卡 constraints {card_commission_bp} bp"
            )
    if card_slippage_bp is not None and _is_number(plan_slippage_bp):
        if abs(card_slippage_bp - float(plan_slippage_bp)) > 1e-6:
            fee_divergences.append(
                f"滑点：计划件 meta {_fmt(plan_slippage_bp)} bp vs 执行卡 constraints {card_slippage_bp} bp"
            )
    # 🔴 2026-11-20 第五轮自修：读不到费率时**不许判「两处一致」** ——
    #    「没有」≠「一样」⇒ 缺就是缺（铁律「数字不许编」）。
    _plan_bp_ok = _is_number(plan_commission_bp) and _is_number(plan_slippage_bp)
    _card_bp_ok = card_commission_bp is not None and card_slippage_bp is not None
    if fee_divergences:
        fee_divergence_blob = "；".join(fee_divergences)
    elif not (_plan_bp_ok and _card_bp_ok):
        fee_divergence_blob = "缺（有一侧费率读不到 ⇒ 不许判「两处一致」）"
    else:
        fee_divergence_blob = "无（两处一致）"

    # ---- 现金流口径：计划件 vs 执行卡（**同日两套口径，可能不等**）--------------
    # 🔴 真缺陷（2026-11-20 实测）：11-18 执行卡 `cash_flow.buy_fees=15.0`/`slippage=1.41458`
    #    ⇒ `cash_end=9205.15542`；计划件 `buy_fees=9.19477` ⇒ `cash_end=9212.37523`
    #    ⇒ **差 7.22 元** ⇒ 记录只写计划件那套 ⇒ 复盘算现金对不上账。
    card_cash_flow = (exec_card or {}).get("cash_flow") or {}
    plan_cash_end = cash_flow.get("cash_end")
    card_cash_end = card_cash_flow.get("cash_end")
    cash_end_diff = None
    if _is_number(plan_cash_end) and _is_number(card_cash_end):
        cash_end_diff = round(float(card_cash_end) - float(plan_cash_end), 6)

    # ---- 执行卡：目标 vs 实际 ----------------------------------------------
    orders = (exec_card or {}).get("orders") or []
    live_trade = bool((exec_card or {}).get("live_trade"))

    # ---- 盘口口径（**由产物原文派生**，不许写死）-----------------------------
    exec_basis = (exec_card or {}).get("execution_basis")
    discipline = plan.get("execution_discipline") or {}
    plan_rule = discipline.get("rule")
    caliber_exec = _caliber_label(exec_basis)
    caliber_plan = _caliber_label(plan_rule)
    caliber_same = (caliber_exec != "未声明") and (caliber_exec == caliber_plan)

    # ---- 执行卡**自己**声明的盘口证据（warnings 原文；它可能与 basis 打架）-----
    # 🔴 只认**人话条目**：结构化风险披露是以 dict 字面量串进 warnings 的（``{...}`` 开头），
    #    里面偶然出现的「收盘」不是盘口声明 ⇒ 不许混进盘口证据段。
    caliber_quotes: list[str] = []
    warnings_other = 0
    for item in (exec_card or {}).get("warnings") or []:
        if not isinstance(item, str):
            warnings_other += 1
            continue
        stripped = item.strip()
        if not stripped or stripped.startswith("{"):
            warnings_other += 1
            continue
        if any(keyword in stripped for keyword in ("开盘", "收盘", "成交价", "执行日")):
            caliber_quotes.append(stripped)
        else:
            warnings_other += 1
    obs_note = "否（观察态不下单）" if not live_trade else "见执行卡"

    # ---- 否决名单（唯一事实源 = ``ta_screening.rejected``，带出处）--------------
    veto_map: dict[str, dict] = {}
    for rej in screening.get("rejected") or []:
        if isinstance(rej, dict) and rej.get("code"):
            veto_map[str(rej["code"])] = rej
    # ---- 顺延补位（谁顶替了谁）----------------------------------------------
    sub_map: dict[str, dict] = {}       # 被否决的 code → 补位者
    sub_for: dict[str, dict] = {}       # 补位者 code → 它顶替的
    for sub in screening.get("substituted") or []:
        if not isinstance(sub, dict):
            continue
        sub_code = str(sub.get("code") or "")
        rej_code = str(sub.get("substituted_for") or "")
        if sub_code:
            sub_for[sub_code] = sub
        if rej_code:
            sub_map[rej_code] = sub

    rows: list[dict] = []
    actions = plan.get("actions") or []
    for act in actions:
        code = _pick(act, "code", default=MISSING)
        veto_entry = veto_map.get(str(code))
        veto_reason = str(veto_entry.get("reason")) if veto_entry else ""
        veto_src = str(veto_entry.get("source_section")) if veto_entry else ""
        note = _pick(act, "reason", default="")
        # 顺延双向：被否决者 → 谁顶上来；补位者 → 它顶替了谁
        substitute_up = ""
        replaced_by = ""
        if str(code) in sub_map:
            substitute_up = str(sub_map[str(code)].get("code") or "")
        if str(code) in sub_for:
            replaced_by = str(sub_for[str(code)].get("substituted_for") or "")
        if not substitute_up and act.get("ta_substituted_for"):
            replaced_by = str(act.get("ta_substituted_for"))

        row = {
            "日期": manifest.get("asof") or meta.get("asof") or MISSING,
            "记录时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "记录状态": "有效",
            "代码": code,
            "名称": _pick(act, "name", default=MISSING),
            "动作": _pick(act, "action", "qlib_action", default=MISSING),
            "当前股数": _fmt(act.get("current_shares")),
            "当前权重": _fmt(act.get("current_weight")),
            "目标权重": _fmt(act.get("target_weight")),
            "目标权重生效": _fmt(act.get("target_weight_effective")),
            "目标金额": _fmt(act.get("target_amount")),
            "目标股数": _fmt(act.get("target_shares")),
            "计划金额": _fmt(act.get("est_amount")),
            "是否成交": obs_note,
            "成交价": "不适用" if not live_trade else MISSING,
            "成交金额": "不适用" if not live_trade else MISSING,
            "否决理由": veto_reason or MISSING,
            "否决出处": veto_src or MISSING,
            "顺延补位者": substitute_up or MISSING,
            "被顶替的标的": replaced_by or MISSING,
            "说明": note or MISSING,
            "权益上限(规则硬)": _fmt(cap),
            "权益上限(本轮生效)": _fmt(cap_eff),
            "权益超配幅度": _fmt(screening.get("equity_cap_breach")),
            "账户总资产": _fmt(meta.get("total_assets", account.get("total_assets"))),
            "账户现金": _fmt(meta.get("cash", account.get("cash"))),
            "是否走兜底": fallback,
            "失败环节": failed_blob,
            "V2参数": v2_blob,
            "来源": str(snapshot_dir),
        }
        rows.append(row)

    return {
        "snapshot_dir": str(snapshot_dir),
        "asof": manifest.get("asof") or meta.get("asof") or MISSING,
        "run_id": manifest.get("run_id") or snapshot_dir.name,
        "live_trade": live_trade,
        "rows": rows,
        "meta": meta,
        "screening": screening,
        "gate": gate,
        "cash_flow": cash_flow,
        "manifest": manifest,
        "frozen": frozen,
        "account": account,
        "failed_blob": failed_blob,
        "v2_blob": v2_blob,
        "fallback": fallback,
        "orders": orders,
        "exec_card": exec_card or {},
        "nav_state": nav_state,
        "nav_diff": nav_diff,
        "total_assets": total_assets,
        "drawdown": drawdown,
        "equity_dd": equity_dd,
        "dd_mismatch": dd_mismatch,
        "discrepancy_note": account.get("discrepancy_note") or "",
        "execution_discipline": discipline,
        "v2_meta_blob": v2_meta_blob,
        "exec_basis": exec_basis,
        "plan_rule": plan_rule,
        "caliber_exec": caliber_exec,
        "caliber_plan": caliber_plan,
        "caliber_same": caliber_same,
        "caliber_quotes": caliber_quotes,
        "warnings_other": warnings_other,
        "exec_card_count": len(exec_card_files),
        "w_blob": w_blob,
        "w_status": w_status,
        "w_is_stale": w_is_stale,
        "plan_commission_bp": plan_commission_bp,
        "plan_slippage_bp": plan_slippage_bp,
        "card_commission_bp": card_commission_bp,
        "card_slippage_bp": card_slippage_bp,
        "fee_divergence_blob": fee_divergence_blob,
        "card_cash_flow": card_cash_flow,
        "cash_end_diff": cash_end_diff,
    }


def append_ledger(ledger_path: Path, rows: list[dict], dry_run: bool) -> tuple[int, int, int]:
    """把行**追加**进台账（append-only）。

    已存在的 (日期, 代码) 行**不改写** ⇒ 旧行标「作废」、新行标「有效」。
    **幂等**：同键行内容**完全一致** ⇒ 视为「无新事实」⇒ 既不追加也不作废。
    返回 (新增行数, 作废行数, 跳过行数)。
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if ledger_path.is_file():
        with ledger_path.open("r", encoding="utf-8", newline="") as fh:
            existing = list(csv.DictReader(fh))
    seen = {(r.get("日期"), r.get("代码")) for r in existing}
    # 同键的**当前有效**行 ⇒ 内容签名（用于幂等：同一快照重跑不许造重复行）
    valid_sig = {
        (r.get("日期"), r.get("代码")): _row_signature(r)
        for r in existing
        if r.get("记录状态") != "作废"
    }

    new_rows: list[dict] = []
    invalidated = 0
    skipped = 0
    for row in rows:
        key = (row["日期"], row["代码"])
        if key in valid_sig and valid_sig[key] == _row_signature(row):
            # 内容**完全一致** ⇒ 本轮无新事实 ⇒ 不追加、也不作废旧行（幂等）
            skipped += 1
            continue
        if key in seen:
            invalidated += 1
        else:
            seen.add(key)
        new_rows.append(row)

    if dry_run:
        return len(new_rows), invalidated, skipped

    if existing:
        # 旧行里同 (日期,代码) 的 ⇒ 标作废（只增不改，保留演化轨迹）
        for old in existing:
            if (old.get("日期"), old.get("代码")) in {
                (r["日期"], r["代码"]) for r in new_rows
            } and old.get("记录状态") != "作废":
                old["记录状态"] = "作废"
    with ledger_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(LEDGER_HEADER), lineterminator="\n")
        writer.writeheader()
        for old in existing:
            writer.writerow({k: old.get(k, "") for k in LEDGER_HEADER})
        for row in new_rows:
            writer.writerow({k: row.get(k, "") for k in LEDGER_HEADER})
    return len(new_rows), invalidated, skipped


def render_markdown(info: dict) -> str:
    """渲染当日的**人读记录**（目标 vs 实际分列；表格列数一致 ⇒ 过表块自检）。"""
    meta = info["meta"]
    screening = info["screening"]
    gate = info["gate"]
    cash_flow = info["cash_flow"]
    manifest = info["manifest"]
    asof = info["asof"]

    lines: list[str] = []
    lines.append(f"# 每日记录 · {asof}（ETF 轮动 · V2 线）")
    lines.append("")
    lines.append(f"- 快照目录：`{info['snapshot_dir']}`")
    lines.append(f"- run_id：`{info['run_id']}`｜运行模式：**{'实盘态' if info['live_trade'] else '观察态'}**")
    lines.append(f"- 账户总资产：**{meta.get('total_assets', MISSING)} 元**｜现金：**{meta.get('cash', MISSING)} 元**")
    lines.append(
        f"- 权益上限（规则硬）：{screening.get('equity_cap', MISSING)}"
        f"｜本轮生效：{screening.get('equity_cap_effective', MISSING)}"
        f"｜超配幅度：{screening.get('equity_cap_breach', MISSING)}"
    )
    lines.append(f"- 走兜底：**{info['fallback']}**｜失败/跳过环节：{info['failed_blob']}")
    lines.append(f"- 🔴 成分股快照 W（w_holdings）：{info['w_blob']}")
    lines.append(f"- V2 冻结参数：`{info['v2_blob']}`")
    lines.append(f"- V2 生效口径（seam / enabled）：`{info['v2_meta_blob']}`")
    lines.append("")

    lines.append("## 一、当日标的：目标 vs 实际")
    lines.append("")
    lines.append("> 🔴 「目标」与「实际」分列 —— 事后复盘要能分辨偏差出在**决策层**还是**执行层**。")
    lines.append("> 观察态**不产生任何成交** ⇒ 「成交」列一律为「否（观察态不下单）」。")
    lines.append("")
    lines.append("| 代码 | 名称 | 动作 | 当前股数 | 当前权重 | 目标权重 | 目标权重生效 | 目标金额 | 目标股数 | 计划金额 | 是否成交 | 否决理由 | 否决出处 | 顺延补位者 | 被顶替的标的 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |")
    for row in info["rows"]:
        lines.append(
            f"| {row['代码']} | {row['名称']} | {row['动作']} | {row['当前股数']} | {row['当前权重']} | "
            f"{row['目标权重']} | {row['目标权重生效']} | {row['目标金额']} | {row['目标股数']} | "
            f"{row['计划金额']} | {row['是否成交']} | {row['否决理由']} | {row['否决出处']} | "
            f"{row['顺延补位者']} | {row['被顶替的标的']} |"
        )
    lines.append("")
    lines.append("### 说明（入选 / 维持 / 顺延的理由）")
    lines.append("")
    lines.append("| 代码 | 说明 |")
    lines.append("| --- | --- |")
    for row in info["rows"]:
        lines.append(f"| {row['代码']} | {row['说明']} |")
    lines.append("")

    lines.append("## 二、现金流与落地")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | ---: |")
    for key in ("cash_begin", "sell_proceeds", "sell_fees", "buy_amount", "buy_fees",
                "cash_end", "available_before_buy", "deficit", "surplus", "headroom",
                "scaled_down", "feasible", "invested_weight_planned"):
        # 🔴 缺 key ⇒ **显式写「缺」**（不许整行省略 ⇒ 读者分不清「没有」还是「漏了」）
        lines.append(f"| {key} | {_fmt_value(cash_flow.get(key))} |")
    lines.append("")
    lines.append("### 执行卡口径的现金流（**与上面计划件口径可能不等 —— 两套都要留痕**）")
    lines.append("")
    lines.append("> 🔴 同一天、同一张计划，执行卡与计划件**各有一套 `cash_flow`**；")
    lines.append("> 费率与滑点的计价基准不同 ⇒ **期末现金可能不等** ⇒ 不许只留一套（否则复盘对不上账）。")
    lines.append("")
    card_cash_flow = info["card_cash_flow"]
    lines.append("| 项 | 计划件 | 执行卡 |")
    lines.append("| --- | ---: | ---: |")
    for key in ("buy_amount", "buy_fees", "slippage", "cash_end"):
        lines.append(
            f"| {key} | {_fmt_value(cash_flow.get(key))} | {_fmt_value(card_cash_flow.get(key))} |"
        )
    lines.append(
        f"| cash_end 差额（执行卡 − 计划件） | — | {_fmt_money(info['cash_end_diff'])} |"
    )
    lines.append("")

    lines.append("## 三、门槛与纪律")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 最小金额门槛开关 | {_fmt(gate.get('enabled'))} |")
    lines.append(f"| 门槛阈值 | {_fmt_value(gate.get('thresholds'))} |")
    lines.append(f"| 因门槛跳过只数 | {_fmt(gate.get('skipped_count'))} |")
    lines.append(f"| 门槛死锁风险 | {_fmt(gate.get('deadlock_risk'))} |")
    lines.append(f"| 权益上限（规则硬） | {_fmt(screening.get('equity_cap'))} |")
    lines.append(f"| 权益上限（本轮生效） | {_fmt(screening.get('equity_cap_effective'))} |")
    lines.append(f"| 权益超配幅度 | {_fmt(screening.get('equity_cap_breach'))} |")
    lines.append(f"| 目标只数 | {_fmt(meta.get('target_n_holdings'))} |")
    lines.append(f"| hold_buffer | {_fmt(meta.get('hold_buffer'))} |")
    lines.append(f"| 佣金 bp（计划件 meta） | {_fmt(info['plan_commission_bp'])} |")
    lines.append(f"| 滑点 bp（计划件 meta） | {_fmt(info['plan_slippage_bp'])} |")
    lines.append(f"| 佣金 bp（执行卡 constraints） | {_fmt(info['card_commission_bp'])} |")
    lines.append(f"| 滑点 bp（执行卡 constraints） | {_fmt(info['card_slippage_bp'])} |")
    lines.append(f"| 费率口径分歧 | {_cell(info['fee_divergence_blob'])} |")
    lines.append(f"| 成分股快照 W（w_holdings） | {_cell(info['w_blob'])} |")
    lines.append("")

    # ---- 四、持仓明细与净值口径（**开源接口拿不到的口径事实**）----------------
    account = info["account"]
    nav_state = info["nav_state"]
    nav_diff = info["nav_diff"]
    total_assets = info["total_assets"]
    drawdown = info["drawdown"]
    equity_dd = info["equity_dd"]
    lines.append("## 四、持仓明细与净值口径")
    lines.append("")
    lines.append(
        "> 🔴 本段的**成本额 / 浮盈 / 浮盈%** 与**收盘口径净值**都来自**快照产物**"
        "（`account_context.json` / `nav_state.json`）；akshare 等开源接口**事后拿不到**"
        "⇒ 必须随记录一起留痕（铁律 §十四.122）。"
    )
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 账户快照日 | {_fmt(account.get('snapshot_date'))} |")
    lines.append(f"| 快照总资产 | {_fmt(total_assets)} |")
    lines.append(f"| 快照现金 | {_fmt(account.get('cash'))} |")
    lines.append(f"| 快照市值 | {_fmt(account.get('market_value'))} |")
    lines.append(f"| 收盘口径总资产（nav_state.nav） | {_fmt(nav_state.get('nav'))} |")
    lines.append(f"| 两者差额（nav − 快照总资产） | {_fmt_money(nav_diff)} |")
    lines.append(f"| 净值峰值 peak | {_fmt(nav_state.get('peak'))} |")
    lines.append(f"| 净值峰值日 peak_date | {_fmt(nav_state.get('peak_date'))} |")
    lines.append(f"| 组合回撤 drawdown | {_fmt(drawdown)} |")
    lines.append(f"| 权益市值 equity_nav | {_fmt(nav_state.get('equity_nav'))} |")
    lines.append(f"| 净值口径说明 note | {_cell(nav_state.get('note'))} |")
    lines.append("")
    snapshot_date = account.get("snapshot_date")
    if snapshot_date and asof and snapshot_date != asof:
        lines.append(
            f"> 🔴 **账户事实源陈旧**：本记录的**账户快照日 = {snapshot_date}**，"
            f"与**记录日 {asof}** 相差 {abs((_date(asof) - _date(snapshot_date)).days)} 天"
            " ⇒ 下面的总资产 / 现金 / 市值 / 持仓都是**旧口径**，"
            "复盘时**不许**与当日口径混算（下一轮记录会随账户换代刷新）。"
        )
        lines.append("")
    lines.append("### 持仓明细（成本额与浮盈）")
    lines.append("")
    lines.append("| 代码 | 名称 | 股数 | 现价 | 市值 | 成本额 | 浮盈 | 浮盈% |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    positions = account.get("positions") or []
    if positions:
        for pos in positions:
            lines.append(
                f"| {_fmt(pos.get('code'))} | {_fmt(pos.get('name'))} | {_fmt(pos.get('shares'))} | "
                f"{_fmt(pos.get('price'))} | {_fmt(pos.get('market_value'))} | {_fmt(pos.get('cost'))} | "
                f"{_fmt(pos.get('pnl'))} | {_fmt(pos.get('pnl_pct'))} |"
            )
    else:
        lines.append(f"| {MISSING} | {MISSING} | {MISSING} | {MISSING} | {MISSING} | {MISSING} | {MISSING} | {MISSING} |")
    lines.append("")
    cleared = account.get("cleared_positions") or []
    if cleared:
        cleared_text = "、".join(
            f"{_fmt(pos.get('code'))}（{_fmt(pos.get('name'))}）" for pos in cleared
        )
        lines.append(f"- 已清仓标的（快照口径，**开源接口查不到「我卖过」**）：{cleared_text}")
        lines.append("")
    lines.append("### 账户口径披露（原文照抄，**不抹平**）")
    lines.append("")
    discrepancy = info["discrepancy_note"]
    if discrepancy:
        lines.append("> " + str(discrepancy).replace("\n", "\n> "))
    else:
        lines.append(f"> {MISSING}（该快照的 `account_context.json` 未带 `discrepancy_note`）")
    lines.append("")
    if info["dd_mismatch"]:
        lines.append(
            f"> 🔴 `nav_state.equity_drawdown_vs_peak` = {_fmt(equity_dd)} 与组合口径 "
            f"`drawdown` = {_fmt(drawdown)} **对不上（差 {abs(float(equity_dd) - float(drawdown)):.4f}）**"
            " ⇒ 该字段口径有伪 ⇒ **本记录不采信**，复盘回撤一律用 `drawdown`。"
        )
        lines.append("")

    # ---- 五、执行口径（**盘口口径分裂要显式写出来**）--------------------------
    exec_card = info["exec_card"]
    discipline = info["execution_discipline"]
    caliber_exec = info["caliber_exec"]
    caliber_plan = info["caliber_plan"]
    lines.append("## 五、执行口径")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 执行卡 asof | {_fmt(exec_card.get('asof'))} |")
    lines.append(f"| 执行卡 execution_date（本轮 T+1） | {_fmt(exec_card.get('execution_date'))} |")
    lines.append(f"| 执行卡 execution_basis（原文） | {_cell(exec_card.get('execution_basis'))} |")
    lines.append(f"| 执行卡 price_basis_note（原文） | {_cell(exec_card.get('price_basis_note'))} |")
    lines.append(f"| 执行卡 live_trade | {_fmt(exec_card.get('live_trade'))} |")
    lines.append(f"| 执行卡 order_enabled | {_fmt(exec_card.get('order_enabled'))} |")
    lines.append(f"| 计划件 signal_date（下轮信号日） | {_fmt(discipline.get('signal_date'))} |")
    lines.append(f"| 计划件 execution_date（下轮执行日） | {_fmt(discipline.get('execution_date'))} |")
    lines.append(f"| 计划件 rule（原文） | {_cell(discipline.get('rule'))} |")
    lines.append(
        f"| **盘口口径（由上面原文派生）** | 执行卡 = **{caliber_exec}**｜计划件 = **{caliber_plan}**"
        f" ⇒ **{'一致' if info['caliber_same'] else '不一致（差一个盘口）'}** |"
    )
    lines.append("")
    if info["caliber_same"]:
        lines.append(
            f"> ✅ **盘口口径一致**：执行卡与计划件都写「{caliber_exec}」⇒ 本轮无分裂。"
        )
    elif caliber_exec == "未声明":
        lines.append(
            "> 🔴 **盘口口径未声明**：执行卡 `execution_basis` 原文如上表，**没有**给出开盘/收盘"
            " ⇒ **不许**替它推断盘口；每日链权威口径见 `manu/memory/memory.md` ⑧ 段。"
        )
    else:
        lines.append(
            f"> 🔴 **盘口口径分裂（两个权威源写法不一致，必须显式知道）**：\n"
            f"> - **执行卡** `execution_basis` = **{caliber_exec}**执行（每日链权威，用户已锁死；"
            "其 `execution_date` 指**本轮** asof 的 T+1）；\n"
            f"> - **计划件** `execution_discipline.rule` = **{caliber_plan}**执行（研究链口径；"
            "其 `signal_date`/`execution_date` 指**下一轮**调仓日）。\n"
            "> ⇒ **差一个盘口** ⇒ 以**执行卡**为准；复盘时务必分清「本轮执行日」与「下轮信号/执行日」。"
            "缺陷明细见 `manu/memory/memory.md` ⑧⑨ 段。"
        )
    lines.append("")

    lines.append("### 执行卡**自己**声明的盘口证据（原文照抄，**不抹平**）")
    lines.append("")
    caliber_quotes = info["caliber_quotes"]
    if caliber_quotes:
        for quote in caliber_quotes:
            lines.append("> " + str(quote).replace("\n", "\n> "))
            lines.append("")
    else:
        lines.append(f"> {MISSING}（该执行卡的 `warnings` 里没有含「开盘/收盘」的条目）")
        lines.append("")
    lines.append(
        "🔴 **同一张执行卡里可能同时出现两种盘口**（`execution_basis` 与 `warnings` 各说各的）"
        "⇒ 复盘时**两处原文都要看**，别只看一处。"
    )
    lines.append("")
    lines.append(
        f"ℹ️ 该执行卡另有 **{info['warnings_other']}** 条 `warnings` 与盘口无关"
        "（含以 dict 字面量串进 `warnings` 的结构化风险披露）⇒ **未全文抄录**（避免记录被无关长文淹没）。"
    )
    lines.append("")

    orders = info["orders"]
    lines.append("### 执行卡 orders（目标股数与金额口径）")
    lines.append("")
    lines.append("| 代码 | 名称 | 动作 | 当前股数 | 价格 | 价格日 | 目标股数 | delta | 市值 | 目标金额 | 本笔金额 |")
    lines.append("| --- | --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |")
    if orders:
        for order in orders:
            lines.append(
                f"| {_fmt(order.get('code'))} | {_fmt(order.get('name'))} | {_fmt(order.get('action'))} | "
                f"{_fmt(order.get('current_shares'))} | {_fmt(order.get('price'))} | {_fmt(order.get('price_date'))} | "
                f"{_fmt(order.get('target_shares'))} | {_fmt(order.get('delta_shares'))} | "
                f"{_fmt(order.get('market_value'))} | {_fmt(order.get('target_amount'))} | "
                f"{_fmt(order.get('est_amount'))} |"
            )
    else:
        lines.append("| " + " | ".join([MISSING] * 11) + " |")
    lines.append("")
    lines.append(
        "🔴 维持行的**市值**（券商快照价）与**目标金额**（节点日收盘价 × 目标股数）**本来就不等**"
        " ⇒ 差额就是 §四 的「收盘口径 − 快照口径」那笔，**不是**记录算错。"
    )
    lines.append("")

    lines.append("## 六、来源")
    lines.append("")
    lines.append(f"- 快照：`{info['snapshot_dir']}`")
    lines.append(f"- 计划件：`final_rebalance_plan.json`｜执行卡：`execution_card_*.json`")
    lines.append(f"- 账户事实源：`account_context.json`｜净值口径：`nav_state.json`")
    lines.append(f"- manifest：created_at={manifest.get('created_at')}｜finalized_at={manifest.get('finalized_at')}")
    lines.append(f"- 逐日台账：`etf_rotation_daily_ledger.csv`（一行 = 一只 ETF 一天）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("🔴 本记录由 `manu/tools/append_daily_record.py` 从**快照产物**自动生成；")
    lines.append("所有数字只来自实跑产物或用户口述，**没有实测来源的一律写「缺」**（铁律：数字不许编）。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把每日核心事实落成可复盘记录（铁律 §十四.122）")
    parser.add_argument("--snapshot-dir", required=True, help="一次不可变快照目录")
    parser.add_argument("--records-dir", default="manu/memory/records", help="record 落点")
    parser.add_argument("--dry-run", action="store_true", help="只看要写什么，不落盘")
    args = parser.parse_args(argv)

    snapshot_dir = Path(args.snapshot_dir)
    if not snapshot_dir.is_dir():
        print(f"❌ 快照目录不存在：{snapshot_dir}", file=sys.stderr)
        return 2

    info = collect(snapshot_dir)
    records_dir = Path(args.records_dir)
    ledger_path = records_dir / "etf_rotation_daily_ledger.csv"
    if not info["asof"] or info["asof"] == MISSING:
        print(
            f"❌ 快照里读不到 asof（manifest / plan 都没有）⇒ 拒绝落盘"
            f"（否则会写出「{MISSING}.md」这种假记录）",
            file=sys.stderr,
        )
        return 2
    md_path = records_dir / f"{info['asof'].replace('-', '')}.md"

    added, invalidated, skipped = append_ledger(ledger_path, info["rows"], args.dry_run)
    if not args.dry_run:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(info), encoding="utf-8")

    print(f"asof={info['asof']}｜run_id={info['run_id']}｜标的数={len(info['rows'])}")
    print(
        f"台账新增 {added} 行｜同键旧行标作废 {invalidated} 行｜"
        f"内容一致跳过 {skipped} 行｜{ledger_path}"
    )
    print(f"当日记录：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
