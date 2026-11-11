#!/usr/bin/env python
"""回归自检探针：一条命令验证 ETF 轮动线的产物是否仍然自洽、可复现、未漂移。

【为什么需要】
明早 host 用 ``handoff_from_server.sh --adopt`` 拉回服务器侧状态后，必须能**一条命令**回答三个问题：
① **基准 510300 年化还是不是 12.11%**（口径漂移回归探针，``rules.md §十四``：变了就说明窗口/数据被改坏）；
② **账户 / 现金 / 股数三方对账是否闭合**（交付件里不许出现算术矛盾）；
③ **报告 → context → 决策的 sha1 链是否新鲜**（不许「新规则层 + 旧 LLM 意见」混进交付件）。

【口径】
- **只读**：本脚本不修改任何产物；唯一的写入是**自检日志**。
- 🔴 2026-11-10（架构迁移收口）：**自检日志绝不写进快照目录**。
  架构迁移后闸门的断言对象 = **最新完整快照**（``<canonical>/snapshots/<stamp>/``），
  而快照是**不可变**的（跑完即冻结）⇒ 日志落点改成：
  ① ``--log-path`` 显式指定 > ② canonical 根（快照根的上一级）
  ⇒ ``verify_artifacts_<快照名>.log``；**非快照目录**保持旧口径（``<run-root>/verify_artifacts.log``）。
- 任一断言失败 ⇒ 打印 FAIL 表并以**非零退出码**结束（可直接进 CI / 交接检查）。
- 断言一律带**数字与容差**，不写「看起来正常」这种无数字结论。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/verify_artifacts.py \
        --run-root <落点根>/runs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

#: 基准 510300 年化的回归探针值与容差（rules.md §十四：必须恒为 12.11%）
BENCH_ANNUALIZED = 0.1211
BENCH_TOLERANCE = 0.0005
#: 账户对账容差（元）：算术恒等式（Σ市值 + 现金 = 总资产、股数 × 现价 = 市值）
ACCOUNT_TOLERANCE = 0.01
#: 金额**精确对账**容差（元）：事实源 ↔ 报告的同一笔数字，只许浮点表示噪声
ACCOUNT_EXACT_TOLERANCE = 1e-6
#: 账户事实源（**交付件同目录**）：``account_context.json`` —— 期望值的唯一来源
#: 🔴 真实账户的持仓集合 / 总资产 / 现金**不再写死**（旧值 5 只 / 29854.67 / 15518.87
#:    是 2026-11-04 的旧账户口径 ⇒ 本轮真实账户是 2026-11-05 口径 ⇒ 写死必红）；
#:    事实源缺失时相关断言**必定 FAIL**（不许真空 PASS）。
ACCOUNT_CONTEXT_NAME = "account_context.json"
#: 账户快照（**交付件同目录**）：``account_snapshot.csv`` —— 逐只持仓的原始口径
ACCOUNT_SNAPSHOT_NAME = "account_snapshot.csv"
#: 报告 JSON 必须齐的段落
REQUIRED_SECTIONS = (
    "meta", "stock_scores", "rank_ic", "etf_scores", "filter_stats",
    "candidates", "current_holdings_diagnosis", "next_rebalance",
    "risk_disclosure", "disclaimer",
)
#: 密钥泄漏探测用的特征串（只报命中数，不打印内容）
SECRET_MARKER = "sk-cp-"
#: 快照目录的标志文件（存在 ⇒ 该目录是**不可变快照**，不许往里写任何东西）
SNAPSHOT_MANIFEST = "snapshot_manifest.json"
#: 自检日志的默认文件名（非快照目录时用）
LOG_NAME = "verify_artifacts.log"

#: 把脚本所在目录放进 sys.path ⇒ 可 import 同目录的 final_rebalance_plan（重放 LLM 原文用）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from report_fingerprint import report_content_sha1_from_file  # noqa: E402
import config as C  # noqa: E402  （落点常量单一事实源）
import direction_vocab as _DV  # noqa: E402  （LLM 方向词表单一事实源）
#: LLM「方向 → 目标权重」语义类别（**精确匹配**，不做子串匹配）：
#: 「不买」含「买」、「卖出」含「卖」⇒ 子串匹配会把「不买」误判成「要买」。
#: zero = 明确「不持有」⇒ 目标权重必须 = 0%；
#: pos  = 明确「要持有」⇒ 目标权重必须 > 0%；
#: 其余（减 / 持 / 保留 / 不卖 / 中性…）**不设约束**（减仓可以是 0%，也可以是 10%）。
#: 🔴 这条守卫**独立于解析机制**（不依赖 final_rebalance_plan 的正则），
#:    正是能拦住「假冲突」类事故的那一条。
#: LLM 方向词 → 语义分类。🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出、本机实测复现）：
#:    旧实现只有两档（ZERO/POS），而**解析器** `final_rebalance_plan._LLM_DIR_CELL`
#:    能产出 **25 个**方向词（不买/不卖/清仓/买入/卖出/增持/减持/加仓/减仓/持有/保留/维持/
#:    买/卖/增/减/加/持/清/留 + Buy/Sell/Hold/Overweight/Underweight/Neutral）
#:    ⇒ 凡是不在这两档里的词（增持/减持/减仓/增/减/持/留 + **全部英文**）
#:      **完全不受约束** ⇒ 实测「direction='Sell' 配 target=26.96%」这种**假冲突**
#:      能一路 PASS 到交付（守卫判 PASS、异常 0 条）⇒ 守卫比它要拦的东西窄。
#:    ⇒ 修法：方向词**逐个**分类；**未分类的方向词直接判 FAIL**
#:      （不确定就说不确定，不许静默放过）；
#:      并由 `probes/probe_verify_guards.py` 断言「本表 ⊇ 解析器正则的全部备选词」
#:      ⇒ 以后给解析器加词而忘了守卫，会被探针**真的拦下**（不是靠人记得）。
#: 语义分类：
#:   ZERO     = 目标权重必须 **0%**（清仓 / 卖出 / 不买 / Sell）
#:   POS      = 目标权重必须 **>0%**（买入 / 加仓 / Buy / Overweight）
#:   NEUTRAL  = **不设约束**（持有 / 保留 / 维持 / 不卖 / Hold / Neutral）
#:   REDUCE   = 目标权重必须 **< 当前权重**（减 / 减持 / 减仓 / Underweight）
#:   INCREASE = 目标权重必须 **> 当前权重**（增 / 加 / Increase）
#: 🔴 2026-11-11（critic 审计 · 高危）：本组词表与解析器
#:    `final_rebalance_plan._ZERO_DIR_WORDS` **原本是两份分叉的副本**
#:    ⇒ 解析器吐得出的「清」「Sell」不在解析器 ZERO 档（⇒ 只被守卫拦死、不被强制归零），
#:      而「卖掉/清零/不建仓」在解析器侧是死条目。
#:    ⇒ 现在两边都引用 `direction_vocab`（**单一事实源**）；本模块的公开名保持不变。
LLM_DIRECTION_ZERO = _DV.ZERO
LLM_DIRECTION_POS = _DV.POS
LLM_DIRECTION_NEUTRAL = _DV.NEUTRAL
LLM_DIRECTION_REDUCE = _DV.REDUCE
LLM_DIRECTION_INCREASE = _DV.INCREASE
#: 全部已分类的方向词（供「未分类」判定用）
LLM_DIRECTION_ALL = _DV.ALL
#: LLM 分歧门槛（pp）——必须与 final_rebalance_plan.py 建计划时的 `> 2.0` 保持一致
LLM_DIVERGENCE_PP = 2.0


def _sha1(path: Path) -> str:
    """文件的 sha1（十六进制）。"""
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _read_json(path: Path) -> Any:
    """读 JSON；不存在或损坏时返回 None（由调用方判 FAIL）。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_account_context(run_root: Path) -> tuple[dict | None, str]:
    """读**交付件同目录**的账户事实源 ``account_context.json``。

    🔴 事实源缺失 / 不可读 ⇒ 返回 ``(None, 原因)`` ⇒ 调用方**必须 FAIL**
       （不许因为取不到期望值就跳过断言或判 PASS ⇒ 不许真空 PASS）。
    """
    path = run_root / ACCOUNT_CONTEXT_NAME
    if not path.is_file():
        return None, f"缺账户事实源 {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 事实源损坏必须报出来，不许静默
        return None, f"账户事实源不可读 {path}：{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, f"账户事实源格式非法 {path}（不是 JSON 对象）"
    return payload, str(path)


def _account_expectations(ctx: dict) -> tuple[list[str], float | None, float | None, str]:
    """从账户事实源派生期望值：(持仓代码集合, 总资产, 现金, 原因)。

    事实源里缺字段 ⇒ 返回 ``None`` + 原因 ⇒ 调用方 FAIL；
    **绝不**拿写死的旧账户常量兜底（那正是本闸门上一轮变红的根因）。
    """
    positions = ctx.get("positions")
    if not isinstance(positions, list) or not positions:
        return [], None, None, "账户事实源里没有非空 positions 列表"
    codes: list[str] = []
    for item in positions:
        if not isinstance(item, dict):
            return [], None, None, "账户事实源 positions 里有非对象元素"
        code = str(item.get("code") or "").strip()
        if not code:
            return [], None, None, "账户事实源 positions 里有缺 code 的行"
        codes.append(code)
    total_assets = ctx.get("total_assets")
    cash = ctx.get("cash")
    if not isinstance(total_assets, (int, float)) or isinstance(total_assets, bool):
        return sorted(codes), None, None, "账户事实源缺 total_assets"
    if not isinstance(cash, (int, float)) or isinstance(cash, bool):
        return sorted(codes), None, None, "账户事实源缺 cash"
    return sorted(codes), float(total_assets), float(cash), ""


def _calendar_days(run_root: Path) -> tuple[set[str], str]:
    """交易日历并集：qlib 历史日历 ∪ akshare 完整日历。

    qlib 的 ``day.txt`` 只到数据末日（当前 2026-11-03），
    「下一个自然月的第一个交易日」需要 akshare 的完整日历（到 2026-12-31）⇒ 取并集。
    """
    days: set[str] = set()
    sources: list[str] = []
    qlib_cal = Path(C.PROVIDER_URI) / "calendars" / "day.txt"
    if qlib_cal.exists():
        days |= {line.strip() for line in qlib_cal.read_text(encoding="utf-8").splitlines() if line.strip()}
        sources.append(str(qlib_cal))
    ak_cal = C.ETF_DATA_ROOT / "trade_calendar.csv"
    if ak_cal.exists():
        for line in ak_cal.read_text(encoding="utf-8").splitlines()[1:]:
            token = line.strip()
            if token:
                days.add(token)
        sources.append(str(ak_cal))
    return days, " ∪ ".join(sources) if sources else "（无日历源）"


def _check_products(run_root: Path, allow_missing_decision: bool = False) -> list[tuple[str, bool, str]]:
    """产物齐全性：7 个交付件都在且非空。

    `allow_missing_decision=True`（= `--skip-ta` 模式，本轮**按设计**没有 LLM 决策）
    ⇒ 决策的两个文件不算缺失。
    """
    names = (
        "etf_analysis_report.json", "etf_analysis_report.md",
        "rebalance_recommendation.json",
        "tradingagents_decision.md", "tradingagents_decision.json",
        "final_rebalance_plan.json", "final_rebalance_plan.md",
    )
    out: list[tuple[str, bool, str]] = []
    missing: list[str] = []
    tiny: list[str] = []
    for name in names:
        if allow_missing_decision and name.startswith("tradingagents_decision"):
            continue
        path = run_root / name
        if not path.exists():
            missing.append(name)
        elif path.stat().st_size < 50:
            tiny.append(f"{name}({path.stat().st_size}B)")
    ok = not missing and not tiny
    detail = "7/7 存在且非空"
    if missing:
        detail = f"缺 {len(missing)} 个：{', '.join(missing)}"
    elif tiny:
        detail = f"过小：{', '.join(tiny)}"
    out.append(("产物齐全性", ok, detail))
    return out


def _check_benchmark(run_root: Path, backtest: Path | None) -> list[tuple[str, bool, str]]:
    """基准年化回归探针 + 回测口径自洽。"""
    if backtest is None:
        # 回测是**重活**、不由一键流水线跑 ⇒ 新 run-root 里可能压根没有回测产物
        # ⇒ 这一项按「跳过」处理（不算 FAIL），否则自检会误报。
        return [("基准 510300 年化回归探针", True,
                 "跳过：本次 run-root 内无 etf_rotation_backtest_*.json（回测是重活，未随流水线跑）")]
    payload = _read_json(backtest)
    if not isinstance(payload, dict):
        return [("基准 510300 年化回归探针", False, f"{backtest.name} 不可读")]
    bench = (payload.get("benchmark_510300") or {}).get("annualized_return")
    port = (payload.get("portfolio_net") or {}).get("annualized_return")
    window = payload.get("eval_window") or {}
    ok = isinstance(bench, (int, float)) and abs(float(bench) - BENCH_ANNUALIZED) <= BENCH_TOLERANCE
    detail = (
        f"基准年化 {float(bench) * 100:.4f}%（期望 {BENCH_ANNUALIZED * 100:.2f}% ±{BENCH_TOLERANCE * 100:.2f}pp）"
        f"｜组合年化 {float(port) * 100:.4f}%"
        f"｜窗口 {window.get('start')}~{window.get('end')}（{window.get('n_days')} 天）"
        f"｜场景 {payload.get('scenario')}"
    ) if isinstance(bench, (int, float)) and isinstance(port, (int, float)) else "回测产物缺字段"
    out = [("基准 510300 年化回归探针", ok, detail)]
    # 超额口径必须是「相对（几何）」，否则与历史数字不可比
    excess = payload.get("excess_vs_510300") or {}
    exc_ann = excess.get("annualized_return")
    out.append((
        "超额口径为相对（几何）",
        isinstance(exc_ann, (int, float)),
        f"excess_vs_510300.annualized_return = {float(exc_ann) * 100:.4f}%"
        f"｜定义：{payload.get('excess_definition')}" if isinstance(exc_ann, (int, float)) else "缺字段",
    ))
    return out


def _read_account_snapshot_csv(path: Path) -> tuple[list[dict[str, str]], str]:
    """读账户快照 CSV ⇒ (行列表, 错误原因)。"""
    # 🔴 该 CSV 带 **UTF-8 BOM**（`account.py` 用 `encoding="utf-8-sig"` 读）⇒ 这里必须同样剥 BOM，
    #    否则表头变成「\ufeffcode」⇒ 误报「快照缺列」。
    raw = path.read_text(encoding="utf-8-sig")
    rows = [line.split(",") for line in raw.splitlines() if line.strip()]
    if not rows:
        return [], f"账户快照 {path} 是空的"
    header = [c.strip() for c in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    need = ("code", "name", "shares", "price", "market_value")
    missing_cols = [c for c in need if c not in idx]
    if missing_cols:
        return [], f"账户快照 {path} 缺列：{missing_cols}"
    out: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        out.append({name: row[idx[name]].strip() for name in need})
    return out, ""


def _check_account(run_root: Path) -> list[tuple[str, bool, str]]:
    """账户对账：Σ市值 + 现金 = 总资产；股数 × 现价 = 市值；持仓集合与账户事实源精确相等。

    🔴 期望值（现金 / 总资产 / 持仓集合）的**唯一来源** = **交付件同目录**的
       ``account_context.json``（账户事实源）。旧实现写死
       ``/tmp/pycharm_project_658d3fc4/manu/etf_rotation/data/account_snapshot.csv``
       + cash=15518.87 / total_assets=29854.67（2026-11-04 的 5 只旧账户口径）
       ⇒ 本轮真实账户（2026-11-05 口径：1 只 512800 / 总资产 30035.57 / 现金 23367.37）
       必然对不上 ⇒ 闸门必红。
       ⇒ 现在：事实源优先 ⇒ 同目录 ``account_snapshot.csv`` 回落 ⇒ 都没有 ⇒ FAIL（不许真空 PASS）。
    """
    ctx, ctx_detail = _load_account_context(run_root)
    snap_path = run_root / ACCOUNT_SNAPSHOT_NAME
    snap_rows, snap_err = ([], f"缺同目录 {ACCOUNT_SNAPSHOT_NAME}")
    if snap_path.is_file():
        snap_rows, snap_err = _read_account_snapshot_csv(snap_path)
    if ctx is None:
        return [("账户对账", False,
                 f"账户事实源缺失 ⇒ 不许真空 PASS：{ctx_detail}"
                 f"｜{snap_err}（期望的现金/总资产/持仓集合无从核对）")]
    exp_codes, exp_total, exp_cash, exp_err = _account_expectations(ctx)
    if exp_err:
        return [("账户对账", False,
                 f"账户事实源不完整 ⇒ 不许真空 PASS：{ctx_detail}：{exp_err}")]

    total_mv = 0.0
    worst = 0.0
    codes: list[str] = []
    source = ""
    if snap_rows:
        source = f"{ACCOUNT_SNAPSHOT_NAME}（逐只原始口径）"
        for row in snap_rows:
            codes.append(row["code"].zfill(6))
            shares = float(row["shares"])
            price = float(row["price"])
            mv = float(row["market_value"])
            total_mv += mv
            worst = max(worst, abs(shares * price - mv))
    else:
        source = f"{ACCOUNT_CONTEXT_NAME}（positions 自带市值）"
        for item in ctx.get("positions") or []:
            codes.append(str(item.get("code") or "").strip())
            shares = float(item.get("shares") or 0.0)
            price = float(item.get("price") or 0.0)
            mv = float(item.get("market_value") or 0.0)
            total_mv += mv
            worst = max(worst, abs(shares * price - mv))
    diff = abs(total_mv + exp_cash - exp_total)
    ok = (
        bool(exp_codes)
        and sorted(codes) == exp_codes
        and diff < ACCOUNT_TOLERANCE
        and worst < ACCOUNT_TOLERANCE
    )
    detail = (
        f"口径={ctx_detail}｜市值来源={source}｜"
        f"Σ市值 {total_mv:.2f} + 现金 {exp_cash:.2f} = {total_mv + exp_cash:.2f}"
        f"（总资产 {exp_total:.2f}，差 {diff:.4f}）"
        f"｜股数×现价 最大偏差 {worst:.4f}｜持仓 {len(codes)} 只 {sorted(codes)}"
        f"｜期望 {exp_codes}（精确相等）"
    )
    if snap_err:
        detail += f"｜注：{snap_err}"
    return [("账户对账（Σ市值+现金=总资产）", ok, detail)]


def _check_report(report: dict, run_root: Path) -> list[tuple[str, bool, str]]:
    """分析报告：段落齐全、风险披露非空、持仓诊断与账户事实源一致、RankIC 不为空。

    🔴 持仓诊断的期望值来自**交付件同目录**的 ``account_context.json``（账户事实源）
       ⇒ 不再写死「5 只 / 29854.67 / 15518.87」（2026-11-04 的旧账户口径）；
       事实源缺失 ⇒ 该条**必定 FAIL**（不许真空 PASS）。
    """
    out: list[tuple[str, bool, str]] = []
    missing = [s for s in REQUIRED_SECTIONS if s not in report]
    out.append(("报告段落齐全", not missing, "10 段齐全" if not missing else f"缺：{missing}"))
    risks = report.get("risk_disclosure") or []
    out.append(("风险披露非空", isinstance(risks, list) and len(risks) >= 7, f"{len(risks)} 条（要求 ≥7）"))
    guard = ((report.get("stock_scores") or {}).get("degenerate_check") or {})
    out.append((
        "个股分数退化守卫通过",
        bool(guard.get("passed")),
        f"唯一值 {guard.get('unique_values')}｜std {guard.get('std')}｜通过={guard.get('passed')}",
    ))
    ic = report.get("rank_ic") or {}
    horizons = ic.get("horizons") or {}
    ic_detail = []
    ic_ok = True
    for key in ("5", "20"):
        item = horizons.get(key) or {}
        if item.get("status") != "ok":
            ic_ok = False
            ic_detail.append(f"N={key}:{item.get('status')}({item.get('reason')})")
        else:
            ic_detail.append(
                f"N={key} mean_rank_ic={float(item.get('mean_rank_ic')):.4f}"
                f" icir={float(item.get('icir')):.3f} t={float(item.get('t_stat')):.2f}"
                f" 截面={item.get('n_cross_sections')}"
            )
    out.append(("RankIC N=5 / N=20 可算", ic_ok, "；".join(ic_detail)))
    diag = report.get("current_holdings_diagnosis") or {}
    holdings = diag.get("holdings") or []
    got = sorted(str(h.get("code") or "").strip() for h in holdings)
    ctx, ctx_detail = _load_account_context(run_root)
    if ctx is None:
        out.append((
            "持仓诊断逐只齐全（与账户事实源一致）",
            False,
            f"账户事实源缺失 ⇒ 不许真空 PASS：{ctx_detail}",
        ))
    else:
        exp_codes, exp_total, exp_cash, exp_err = _account_expectations(ctx)
        if exp_err:
            out.append((
                "持仓诊断逐只齐全（与账户事实源一致）",
                False,
                f"账户事实源不完整 ⇒ 不许真空 PASS：{ctx_detail}：{exp_err}",
            ))
        else:
            got_total = diag.get("total_assets")
            got_cash = diag.get("cash")
            ok_codes = bool(exp_codes) and got == exp_codes
            ok_total = (
                isinstance(got_total, (int, float)) and not isinstance(got_total, bool)
                and abs(float(got_total) - exp_total) <= ACCOUNT_EXACT_TOLERANCE
            )
            ok_cash = (
                isinstance(got_cash, (int, float)) and not isinstance(got_cash, bool)
                and abs(float(got_cash) - exp_cash) <= ACCOUNT_EXACT_TOLERANCE
            )
            out.append((
                "持仓诊断逐只齐全（与账户事实源一致）",
                ok_codes and ok_total and ok_cash,
                f"口径={ctx_detail}｜诊断持仓 {got}｜期望 {exp_codes}（精确相等）"
                f"｜总资产 {got_total} vs {exp_total}｜现金 {got_cash} vs {exp_cash}",
            ))
    funnel = report.get("filter_stats") or {}
    out.append((
        "过滤漏斗计数自洽",
        funnel.get("n_candidate_pool", 0) >= funnel.get("n_selected", 0) > 0,
        f"universe {funnel.get('n_universe_rows')} → 流动性 {funnel.get('n_pass_liquidity')}"
        f" → 池 {funnel.get('n_candidate_pool')} → 入选 {funnel.get('n_selected')}"
        f"｜行情覆盖 {funnel.get('price_coverage_pct')}%",
    ))
    return out


def _check_next_rebalance(report: dict) -> list[tuple[str, bool, str]]:
    """下一调仓节点：在日历并集内、是当月第一个交易日、距今 >0。"""
    nxt = report.get("next_rebalance") or {}
    target = str(nxt.get("target_date") or "")
    days, source = _calendar_days(C.ETF_RUN_ROOT)
    in_calendar = target in days
    is_first_of_month = bool(target) and target[8:10] == "01"
    # 当月第一个交易日：目标日之前、当月内没有更早的交易日
    month_prefix = target[:7]
    same_month = sorted(d for d in days if d.startswith(month_prefix))
    is_first_trading_day = bool(same_month) and same_month[0] == target
    dist = int(nxt.get("trading_days_until") or 0)
    ok = in_calendar and is_first_trading_day and dist > 0 and bool(nxt.get("in_calendar_verified"))
    detail = (
        f"目标 {target}｜日历校验 {nxt.get('in_calendar_verified')}｜距 {dist} 个交易日"
        f"｜在日历并集内={in_calendar}｜是当月首个交易日={is_first_trading_day}"
        f"（当月首个 {same_month[0] if same_month else 'n/a'}）｜日历源 {source}"
    )
    return [("下一调仓节点可执行", ok, detail)]


def _check_sha1_chain(run_root: Path) -> list[tuple[str, bool, str]]:
    """sha1 链：报告 md → context md → 决策 ⇒ 不许「新规则层 + 旧 LLM 意见」。

    🔴 2026-11-04 夜实测修（M-F3；reviewer 指出 `report` 形参**从未被使用** ⇒ 死参数）：
    删除该形参，避免读者误以为「链校验用到了报告 json 里的内容」——
    实际链校验**只读盘上的四个文件**（报告 md / ta_research_context.md /
    ta_context_meta.json / tradingagents_decision.json）。
    """
    out: list[tuple[str, bool, str]] = []
    report_md = run_root / "etf_analysis_report.md"
    ctx_md = run_root / "ta_research_context.md"
    ctx_meta = _read_json(run_root / "ta_context_meta.json") or {}
    decision_path = run_root / "tradingagents_decision.json"
    decision = _read_json(decision_path) or {}

    ok = True
    detail = []
    # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出、本机实测复现）：
    #    「**决策件存在却损坏**」必须与「**决策件不存在**」分开判。
    #    `_read_json` 对损坏的文件也返回 None ⇒ 旧实现把两者混为一谈：
    #    ① 损坏 ⇒ 报「本轮无决策文件（--skip-ta 模式，按设计）」PASS ⇒ 交付闸门这一关是**空的**；
    #    ② 而且这条分支在「无 meta 且无决策」之前 ⇒ 连链校验都被短路成 PASS。
    #    ⇒ 修法：先判损坏（⇒ FAIL），再判「无决策件」（⇒ 按设计 PASS）。
    if not decision and decision_path.exists():
        out.append((
            "sha1 链新鲜（报告→context→决策）",
            False,
            "🔴 决策 json **存在但不可解析**（损坏/截断）⇒ 无法校验链，"
            "且不许当成「本轮无 LLM 意见」放行",
        ))
        out.append((
            "TradingAgents 决策状态",
            False,
            "🔴 决策 json **存在但不可解析**（损坏/截断）⇒ 不许当成「--skip-ta 无决策」放行",
        ))
        return out
    if not ctx_meta and not decision:
        # 本轮压根没有 LLM 链路（如 --skip-ta）⇒ 无从校验 sha1 链（按设计，不是失败）
        out.append(("sha1 链新鲜（报告→context→决策）", True,
                    "本轮**无 LLM 链路可校验**（无 ta_context_meta.json 且无决策 json ⇒ --skip-ta 模式）"))
        return out
    if not decision:
        # ⚠️ 有上下文三件套但**没有决策件** ⇒ 本轮**没有 LLM 意见** ⇒ 链无从校验（按设计）。
        #    （那套上下文是上一轮遗留，本轮**未被引用** ⇒ 不构成「旧意见当新意见」。）
        out.append(("sha1 链新鲜（报告→context→决策）", True,
                    "本轮**无决策文件** ⇒ 无 LLM 意见 ⇒ 无链可校验（--skip-ta 模式，按设计）；"
                    "⚠️ run-root 内的上下文三件套是**上一轮遗留**，本轮未被引用"))
        out.append(("TradingAgents 决策状态", True, "本轮**无决策文件**（--skip-ta 模式，按设计）"))
        return out
    # 🔴 2026-11-04 夜实测修（M-G / F4，专题档 §21.4）：
    #    第一环**优先**按「**报告内容指纹**」判（报告 JSON 去掉易变 meta 后的规范化 sha1）。
    #    旧格式 meta（没有该键）⇒ **回落** raw md sha1（向后兼容）。
    _content_ref = ctx_meta.get("report_content_sha1")
    if _content_ref:
        _content_now = report_content_sha1_from_file(run_root / "etf_analysis_report.json")
        if _content_now is None:
            ok = False
            detail.append("🔴 **无法校验第一环**（报告 json 缺失/损坏 ⇒ 算不出内容指纹）"
                          "⇒ 不许断言「链新鲜」（算不出来 ≠ 一致）")
        else:
            same = (_content_now == _content_ref)
            ok = ok and same
            detail.append(f"报告**内容指纹** {'一致' if same else '不一致'}"
                          f"（{str(_content_ref)[:8]} vs {_content_now[:8]}）")
    elif report_md.exists() and ctx_meta.get("report_md_sha1"):
        same = _sha1(report_md) == ctx_meta["report_md_sha1"]
        ok = ok and same
        detail.append(f"报告 md sha1 {'一致' if same else '不一致'}（{_sha1(report_md)[:8]} vs {str(ctx_meta['report_md_sha1'])[:8]}）")
    else:
        # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出、本机实测复现）：
        #    旧实现只往 detail 里追加「缺 …」，**不动 ok** ⇒
        #    「链根本没法校验」时这条断言照样报 PASS，detail 里却写着「缺 …」。
        #    ⇒ 这条链存在的唯一理由就是拦「新规则层 + 旧 LLM 意见」；
        #      链不可校验 ⇒ **不确定** ⇒ 不许写成「确定新鲜」。
        ok = False
        detail.append("🔴 **无法校验第一环**（缺报告 md 或 ta_context_meta.report_md_sha1）"
                      "⇒ 不许断言「链新鲜」")
    if ctx_md.exists() and (decision.get("meta") or {}).get("context_sha1"):
        same = _sha1(ctx_md) == decision["meta"]["context_sha1"]
        ok = ok and same
        detail.append(f"context md sha1 {'一致' if same else '不一致'}（{_sha1(ctx_md)[:8]} vs {str(decision['meta']['context_sha1'])[:8]}）")
    else:
        ok = False
        detail.append("🔴 **无法校验第二环**（缺 context md 或 decision.meta.context_sha1）"
                      "⇒ 不许断言「链新鲜」")
    out.append(("sha1 链新鲜（报告→context→决策）", ok, "；".join(detail)))
    out.append((
        "TradingAgents 决策状态",
        decision.get("status") == "ok",
        f"status={decision.get('status')}｜signal={decision.get('signal')}｜耗时 {decision.get('elapsed_seconds')} 秒"
        f"｜模型 {((decision.get('meta') or {}).get('deep_think_llm'))}"
        f"｜effort={((decision.get('meta') or {}).get('reasoning_effort'))}"
        f"｜max_tokens={((decision.get('meta') or {}).get('max_tokens'))}",
    ))
    return out


def _check_final_plan(plan: dict, plan_md: str, rec: dict, run_root: Path) -> list[tuple[str, bool, str]]:
    """最终计划：现金对账、权重闭合、MD/JSON 数字一致、与规则层同源。"""
    out: list[tuple[str, bool, str]] = []
    cf = plan.get("cash_flow") or {}
    computed = (
        float(cf.get("cash_begin", 0.0)) + float(cf.get("sell_proceeds", 0.0))
        - float(cf.get("sell_fees", 0.0)) - float(cf.get("buy_amount", 0.0))
        - float(cf.get("buy_fees", 0.0))
    )
    diff = abs(computed - float(cf.get("cash_end", 0.0)))
    out.append((
        "最终计划现金对账",
        diff < ACCOUNT_TOLERANCE,
        f"期初 {float(cf.get('cash_begin', 0)):,.2f} + 卖出 {float(cf.get('sell_proceeds', 0)):,.2f}"
        f" - 卖出费 {float(cf.get('sell_fees', 0)):.2f} - 买入 {float(cf.get('buy_amount', 0)):,.2f}"
        f" - 买入费 {float(cf.get('buy_fees', 0)):.2f} = {computed:,.2f}"
        f"（产物 {float(cf.get('cash_end', 0)):,.2f}，差 {diff:.4f}）",
    ))
    actions = plan.get("actions") or []
    eff_sum = sum(float(a.get("target_weight_effective") or 0.0) for a in actions)
    planned = float(cf.get("invested_weight_planned") or 0.0)
    out.append((
        "计划权重闭合",
        abs(eff_sum - planned) < 1e-6,
        f"Σ有效权重 {eff_sum:.4f} vs 计划权益 {planned:.4f}｜计划现金 {float(cf.get('target_cash_weight') or 0):.4f}"
        f"｜权益+现金 = {eff_sum + float(cf.get('target_cash_weight') or 0):.4f}",
    ))
    unbuild = cf.get("unbuildable_slots") or []
    bad = [
        u.get("code") for u in unbuild
        if abs(float(u.get("target_weight") or 0.0)) > 1e-9
        and any(
            abs(float(a.get("target_weight_effective") or 0.0)) > 1e-9
            for a in actions if str(a.get("code")) == str(u.get("code"))
        )
    ]
    out.append((
        "买不进 1 手的席位权重归现金",
        not bad,
        f"{len(unbuild)} 个席位（{[u.get('code') for u in unbuild]}）｜异常 {bad or '无'}",
    ))
    flat = plan_md.replace(",", "").replace("**", "")
    md_ok = all(tok in flat for tok in (
        f"{float(cf.get('cash_end', 0)):.2f}",
        f"{planned * 100:.1f}%",
        f"{float(cf.get('target_cash_weight') or 0) * 100:.1f}%",
    ))
    out.append((
        "交付件 MD 与 JSON 数字一致",
        md_ok,
        f"MD 含 期末现金 {float(cf.get('cash_end', 0)):.2f} / 权益 {planned * 100:.1f}% / 现金 {float(cf.get('target_cash_weight') or 0) * 100:.1f}%",
    ))
    # 规则层「holdings」= 本期 5 只**目标**；交付层「actions」= 目标 + 持仓中被清掉的 4 只
    # ⇒ 只比「目标侧」：交付层里 target_weight > 0 的那些。
    rec_codes = sorted(str(h.get("code")) for h in (rec.get("holdings") or []))
    plan_codes = sorted(str(a.get("code")) for a in actions if float(a.get("target_weight") or 0.0) > 0.0)
    # 🔴 2026-11-11：TA 合规筛选（**一票否决 + 顺延补位**）会**合法地**改变交付层名单 ⇒
    #    「同源」的口径必须**扣掉被否决的、补上顺延的、加上保留为「维持」的孤儿持仓**；
    #    否则一次正确的 TA 否决会被误判成「规则层与交付层不同源」。
    ta = plan.get("ta_screening") or {}
    _rejected = {str(r.get("code")) for r in (ta.get("rejected") or []) if r.get("code")}
    _substituted = {str(s.get("code")) for s in (ta.get("substituted") or []) if s.get("code")}
    _orphans = {str(c) for c in (ta.get("orphaned_holdings") or []) if c}
    _expected = (set(rec_codes) - _rejected) | _substituted | _orphans
    _same = set(plan_codes) == _expected
    _ta_note = (
        f"｜TA 否决 {sorted(_rejected)}｜顺延 {sorted(_substituted)}｜维持席位 {sorted(_orphans)}"
        if (_rejected or _substituted or _orphans)
        else "｜本轮无 TA 否决/顺延"
    )
    out.append((
        "规则层与交付层入选同源（TA 否决/顺延已对账）",
        _same,
        f"规则层 {rec_codes}｜交付层 {plan_codes}｜期望 {sorted(_expected)}{_ta_note}",
    ))
    conflicts = plan.get("llm_conflicts") or []
    has_llm_view = bool((plan.get("tradingagents_view") or "").strip())
    src_decision = (plan.get("meta") or {}).get("source_decision")
    if conflicts:
        out.append((
            "LLM 冲突表已解析",
            True,
            f"{len(conflicts)} 条：" + "、".join(f"{c.get('code')}(规则 {c.get('rule_target_pct')}% vs LLM {c.get('llm_target_pct')}%)" for c in conflicts),
        ))
    elif (plan.get("meta") or {}).get("llm_decision_status") == "failed":
        # 🔴 F5：本轮 TradingAgents **未完成**（失败占位）⇒ 交付件里**没有任何 LLM 意见**
        #    ⇒ 冲突表为空是**正常的**，不是解析器读错了展示件。
        #    真正的失败由另一项「TradingAgents 决策状态」报告（那里才是 FAIL）。
        out.append((
            "LLM 冲突表已解析",
            True,
            f"本轮 TradingAgents **未完成**（{(plan.get('meta') or {}).get('llm_decision_error') or '原因未记录'}）"
            "⇒ 无 LLM 意见可对比，冲突表为空是**正常的**（真实失败见「TradingAgents 决策状态」项）",
        ))
    elif not has_llm_view and not src_decision:
        # 🔴 `--skip-ta` 模式：**按设计**就没有 LLM 第二意见 ⇒ 冲突表为空是正常的，不算 FAIL。
        #    但要提醒：run-root 里若躺着上一轮的 tradingagents_decision.md，
        #    它**没有**被本轮交付件引用 ⇒ 不许拿它当本轮 LLM 结论。
        stale_file = (run_root / "tradingagents_decision.md").exists()
        out.append((
            "LLM 冲突表已解析",
            True,
            "本轮**未嵌入 LLM 意见**（--skip-ta 模式，按设计）"
            + ("；⚠️ run-root 内存在上一轮 tradingagents_decision.md，但本交付件**未引用**它 ⇒ 不许当作本轮 LLM 结论" if stale_file else ""),
        ))
    else:
        # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出）：
        #    「有 LLM 视图但冲突表 0 条」有两种情形：① 规则层与 LLM **完全一致**（合法）
        #    ② 解析器整个坏掉（非法）。旧实现一律 FAIL ⇒ 合法情形被误拦（交付闸门假阳性）。
        #    ⇒ 改由**可追溯守卫**用「重放 LLM 原文」区分这两种情形（见 _check_conflict_traceability）。
        out.append((
            "LLM 冲突表已解析",
            True,
            f"冲突表 0 条（source_decision={src_decision}）⇒ 由「冲突表数字可追溯」那条"
            "**重放原文**判定：重放若无 >2pp 分歧 ⇒ 规则层与 LLM 一致（合法），"
            "有分歧却没进表 ⇒ 那一项判 FAIL",
        ))
    stale = (plan.get("meta") or {}).get("llm_stale") or {}
    out.append((
        "LLM 意见未过期（llm_stale=False）",
        not stale.get("stale"),
        f"stale={stale.get('stale')}｜原因 {stale.get('reasons') or '无'}",
    ))
    # 🔴 2026-11-04 夜实测修（M-F4）：「**没过期**」与「**验过了**」是两回事。
    #    三件套缺件时 `_llm_staleness` 走「未通过校验」分支（stale=True）⇒ 上一条已能拦住；
    #    这里再单列一条，把「**缺件 ⇒ 无法校验**」这个具体原因显式报出来（便于定位是哪个件缺）。
    out.append((
        "LLM 新鲜度可校验（三件套齐全）",
        not stale.get("unverifiable_reason"),
        f"unverifiable_reason={stale.get('unverifiable_reason') or '无（链已逐环校验）'}",
    ))
    _semantic_ok, _semantic_detail = _check_llm_direction_semantics(plan)
    out.append(("LLM 方向-权重语义自洽", _semantic_ok, _semantic_detail))
    _traceable_ok, _traceable_detail = _check_conflict_traceability(plan)
    out.append(("冲突表数字可追溯（回放 LLM 原文）", _traceable_ok, _traceable_detail))
    return out


def _num(value: Any, default: float = 0.0) -> float:
    """安全转 float；None / NaN / 非法值一律回落到 default（**不编数字**）。"""
    parsed = _num_or_none(value)
    return default if parsed is None else parsed


def _num_or_none(value: Any) -> float | None:
    """安全转 float；无法解析（None / NaN / 非法值）时返回 None。

    与 `_num` 分开，是为了区分「**缺失**」与「**真的是 0**」——
    两者在权重对账里含义完全不同，不能用一个默认值糊过去。
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _check_llm_direction_semantics(plan: dict) -> tuple[bool, str]:
    """LLM「方向 → 目标权重」语义自洽（**与解析机制无关**的纯语义守卫）。

    🔴 2026-11-04 reviewer 遗留风险：自检原来只查「冲突表是否为空」，
       **不查表里的方向与数字是否自相矛盾** ⇒ 一条「非空但方向相反」的**假冲突**
       （把理由栏的「+9.11% 浮盈」当成目标权重、却配着「卖 / 清仓」的方向）
       能一路 PASS 到交付。
    ⇒ 这里对**每一行** LLM 意见（主视图 + 冲突行 + 冲突行里的每条分歧视图）断言：
       · 方向 ∈ ZERO（不买/清仓/卖出/Sell…）⇒ 目标权重必须 = 0%；
       · 方向 ∈ POS（买入/加仓/Buy/Overweight…）⇒ 目标权重必须 > 0%；
       · 方向 ∈ REDUCE（减/减持/减仓/Underweight…）⇒ 目标权重必须 **< 当前权重**；
       · 方向 ∈ INCREASE（增/加…）⇒ 目标权重必须 **> 当前权重**；
       · 方向 ∈ NEUTRAL（持有/保留/维持/不卖/Hold…）⇒ **不设约束**；
       · **未分类的方向词 ⇒ 直接判 FAIL**（守卫比解析器窄 ⇒ 漏检 ⇒ 假冲突能 PASS 到交付）。
    🔴 2026-11-04 夜实测：旧实现只有 ZERO/POS 两档 ⇒ 解析器能产出的另外 **18 个**方向词
       （含全部英文）**完全不受约束** ⇒ 「Sell 配 26.96%」实测 PASS（异常 0 条）。
    """
    rows: list[tuple[str, str, float, float | None]] = []

    def _collect(tag: str, view: dict) -> None:
        """把一行 LLM 意见收成 (标签, 方向, 目标权重, 当前权重)。"""
        rows.append((
            tag,
            str(view.get("direction") or ""),
            _num(view.get("target_pct"), 0.0),
            _num_or_none(view.get("current_pct")),
        ))

    for code, view in (plan.get("llm_targets") or {}).items():
        if isinstance(view, dict):
            _collect(f"主视图[{code}]", view)
    for conflict in plan.get("llm_conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        code = conflict.get("code")
        _collect(f"冲突[{code}]", {
            "direction": conflict.get("llm_direction"),
            "target_pct": conflict.get("llm_target_pct"),
            # 🔴 2026-11-05 01:3x（M-K 收尾）：冲突行也要带当前权重，
            #    否则「减 ⇒ 目标 < 当前」这一档在**冲突行**上被静默跳过
            #    （守卫覆盖出现空洞：主视图查了、冲突行没查）。
            "current_pct": conflict.get("llm_current_pct"),
        })
        for view in conflict.get("all_divergent_views") or []:
            if isinstance(view, dict):
                _collect(f"冲突[{code}]·{view.get('who')}", view)

    bad: list[str] = []
    for tag, direction, target_pct, current_pct in rows:
        if direction not in LLM_DIRECTION_ALL:
            # 🔴 未分类方向词 ⇒ **无法校验** ⇒ 直接判 FAIL（不许静默放过）
            bad.append(f"{tag} 方向「{direction}」**未分类** ⇒ 无法校验语义")
            continue
        if direction in LLM_DIRECTION_ZERO:
            if abs(target_pct) > 1e-9:
                bad.append(f"{tag} 方向「{direction}」⇒ 目标权重必须 0%，实为 {target_pct}%")
        elif direction in LLM_DIRECTION_POS:
            if target_pct <= 0.0:
                bad.append(f"{tag} 方向「{direction}」⇒ 目标权重必须 >0%，实为 {target_pct}%")
        elif direction in LLM_DIRECTION_REDUCE:
            # 🔴 2026-11-11（服务器实跑 20261111_100742 实测红灯 · 假阳性）：
            #    `current_pct == 0.0` 表示「**账户里没这只**」（`_resolve_current_pct`：
            #    有账户真值时，字典里没有的代码 = 未持有 ⇒ 0%），**不是**「持有 0%」。
            #    ⇒ 账户相对约束（目标 < 当前）在「未持有」上**没有可比基准**
            #      （LLM 的「减 11.17%→6%」是相对**研究基线组合**说的）。
            #    实测事故：159929 不在本账户（账户只持 512800）⇒ 守卫误报
            #      「主视图[159929] 方向「减」⇒ 目标权重必须 < 当前权重 0.0%，实为 6.0%」。
            #    ⇒ 修法：`current_pct` 为 None（未知）或 <= 0.0（未持有 ⇒ 无可比基准）
            #      时**跳过**这两档约束；其余档（ZERO/POS）**不受影响**，继续严查。
            if (current_pct is not None and current_pct > 0.0
                    and target_pct >= current_pct - 1e-9):
                bad.append(f"{tag} 方向「{direction}」⇒ 目标权重必须 < 当前权重 {current_pct}%，实为 {target_pct}%")
        elif direction in LLM_DIRECTION_INCREASE:
            if (current_pct is not None and current_pct > 0.0
                    and target_pct <= current_pct + 1e-9):
                bad.append(f"{tag} 方向「{direction}」⇒ 目标权重必须 > 当前权重 {current_pct}%，实为 {target_pct}%")
        # NEUTRAL（持有 / 保留 / 维持 / 不卖）：**不设约束**
    detail = f"{len(rows)} 行 LLM 意见逐行校验｜异常 {len(bad)} 条"
    if bad:
        detail += "：" + "；".join(bad[:6])
    elif not rows:
        detail = "本轮无 LLM 意见可校验（--skip-ta 或 LLM 未完成，按设计）"
    return (not bad), detail


def _check_conflict_traceability(plan: dict) -> tuple[bool, str]:
    """冲突表数字可追溯：用 **LLM 原文** 重放一遍，最远分歧视图必须与交付件逐字段一致。

    🔴 2026-11-04 reviewer 遗留风险：自检只查「冲突表是否空」，
       **不查表里的数字是否与 LLM 原文一致** ⇒ 凭空出现或被改写的冲突能一路 PASS 到交付。
    ⇒ 这里用**生产解析器**（final_rebalance_plan._llm_target_views）重放原文，
       对每条冲突重算「与规则层差得最远的那条分歧视图」，比对
       llm_target_pct / llm_direction / llm_who 三个字段；
       并重算规则层权重（ETF 取 actions.target_weight×100，现金取 cash_end/总资产×100）
       与产物里的 rule_target_pct 对账。
    ⚠️ 重放用**同一个解析器** ⇒ 它只能证「交付件与原文解析一致」，
       证不了「解析器本身对」⇒ 解析器语义另由「方向-权重语义自洽」那条守卫兜底。
    """
    conflicts = plan.get("llm_conflicts") or []
    src = (plan.get("meta") or {}).get("source_decision")
    _src_ok = bool(src) and Path(src).exists()
    if not conflicts and not _src_ok:
        return True, "无冲突行可追溯（本轮无 LLM 意见）"
    if conflicts and not _src_ok:
        return False, f"有 {len(conflicts)} 条冲突，但 source_decision 不存在：{src}"
    try:
        import final_rebalance_plan as frp
    except Exception as exc:  # noqa: BLE001 —— 导入失败必须报出来，不许静默跳过
        return False, f"无法导入 final_rebalance_plan（{exc}）⇒ 无法重放 LLM 原文"
    try:
        # 🔴 2026-11-05 01:3x（M-K 收尾）：重放必须与**生产同参**。
        #    旧实现 `frp._llm_target_views(src)` 不带 total_assets ⇒ 重放**算不出**
        #    「用金额表达的目标」（多头「约2,985元」= 10%）⇒ 那行在重放里被丢弃
        #    ⇒ 重放与生产结论**不一致**（本轮侥幸没炸，因为最远分歧视图恰好不是它，
        #    但这是**靠运气**，不是靠正确性）。
        #    ⇒ 现在从产物 meta 里取 total_assets 与账户真值当前权重，一并传入。
        _meta = plan.get("meta") or {}
        _total_assets = _num_or_none(_meta.get("total_assets"))
        _acct_w = _meta.get("account_current_weights_pct")
        _acct_w = {str(k): float(v) for k, v in _acct_w.items()} if isinstance(_acct_w, dict) else None
        views_by_code = frp._llm_target_views(src, _total_assets, _acct_w)
    except Exception as exc:  # noqa: BLE001
        return False, f"重放 LLM 原文失败：{exc}"

    # ---- 规则层权重重建（与 final_rebalance_plan 建计划时同源）----
    meta = plan.get("meta") or {}
    flow = plan.get("cash_flow") or {}
    rule_pct: dict[str, float] = {}
    for action in plan.get("actions") or []:
        if not isinstance(action, dict):
            continue
        weight = action.get("target_weight")
        if weight is None:
            continue
        rule_pct[str(action.get("code"))] = float(weight) * 100.0
    total_assets = _num(meta.get("total_assets"))
    if total_assets > 0:
        rule_pct["现金"] = (_num(flow.get("cash_end"))) / total_assets * 100.0

    if not conflicts:
        # 🔴 2026-11-04 夜实测修（M-F）：冲突表 0 条 + 有 LLM 原文 ⇒ 必须**重放**，
        #    区分「规则层与 LLM 完全一致」（合法）与「解析器漏读」（非法）。
        #    旧实现直接 return True ⇒ 解析器整个坏掉（0 条）也照样 PASS。
        missed: list[str] = []
        for code, views in (views_by_code or {}).items():
            rule = rule_pct.get(code)
            if rule is None:
                continue
            _div = [v for v in views
                    if abs(rule - (_num(v.get("target_pct")))) > LLM_DIVERGENCE_PP]
            if not _div:
                continue
            _top = max(_div, key=lambda v: abs(rule - (_num(v.get("target_pct")))))
            missed.append(f"[{code}] 重放得出 {_num(_top.get('target_pct')):.2f}%（规则 {rule:.4f}%）")
        if missed:
            return False, (
                f"冲突表为 0 条，但重放 LLM 原文得出 {len(missed)} 处 >{LLM_DIVERGENCE_PP}pp 分歧"
                f" ⇒ 解析器漏读：" + "；".join(missed[:6])
            )
        return True, (
            f"冲突表为 0 条，且重放 LLM 原文无 >{LLM_DIVERGENCE_PP}pp 分歧"
            " ⇒ 规则层与 LLM 完全一致（合法）"
        )

    bad: list[str] = []
    checked = 0
    for conflict in conflicts:
        code = str(conflict.get("code"))
        rule = rule_pct.get(code)
        stored_rule = _num_or_none(conflict.get("rule_target_pct"))
        if rule is None:
            bad.append(f"[{code}] 规则层权重无法从 actions/cash_flow 重建")
            continue
        if stored_rule is None or abs(rule - stored_rule) > 0.011:
            bad.append(f"[{code}] 规则层权重对不上：重建 {rule:.4f}% vs 产物 {stored_rule}")
        views = views_by_code.get(code) or []
        divergent = [
            view for view in views
            if abs(rule - (_num(view.get("target_pct")))) > LLM_DIVERGENCE_PP
        ]
        if not divergent:
            bad.append(f"[{code}] 产物有冲突行，但重放 LLM 原文后无 >{LLM_DIVERGENCE_PP}pp 分歧 ⇒ 疑似凭空冲突")
            continue
        divergent.sort(
            key=lambda view: abs(rule - (_num(view.get("target_pct")))),
            reverse=True,
        )
        top = divergent[0]
        expect_pct = _num(top.get("target_pct"))
        expect_direction = str(top.get("direction") or "")
        expect_who = str(top.get("who") or "")
        got_pct = _num(conflict.get("llm_target_pct"))
        got_direction = str(conflict.get("llm_direction") or "")
        got_who = str(conflict.get("llm_who") or "")
        checked += 1
        if (abs(expect_pct - got_pct) > 1e-6
                or expect_direction != got_direction
                or expect_who != got_who):
            bad.append(
                f"[{code}] 与原文重放不一致：产物({got_pct}%/{got_direction}/{got_who})"
                f" vs 重放({expect_pct}%/{expect_direction}/{expect_who})"
            )
    detail = f"{checked}/{len(conflicts)} 条冲突与 LLM 原文重放一致｜异常 {len(bad)} 条"
    if bad:
        detail += "：" + "；".join(bad[:6])
    return (not bad), detail


def _check_secret_leak(run_root: Path) -> list[tuple[str, bool, str]]:
    """密钥泄漏探测：受控产物里不许出现 API key（只报命中数，不打印内容）。"""
    hits = 0
    scanned = 0
    for path in sorted(run_root.glob("*")):
        if not path.is_file() or path.suffix.lower() in {".csv"}:
            continue
        if path.stat().st_size > 5_000_000:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hits += text.count(SECRET_MARKER)
    return [("受控产物无 API key", hits == 0, f"扫描 {scanned} 个文件，命中 {hits} 处")]


def _is_snapshot_dir(root: Path) -> bool:
    """该目录是不是**不可变快照**（有 manifest ⇒ 是）。"""
    return (root / SNAPSHOT_MANIFEST).is_file()


def _resolve_log_path(run_root: Path, explicit: str = "") -> Path:
    """自检日志落点：**绝不写进快照目录**（快照跑完即冻结，写进去就破了不变式）。

    优先级：
    ① ``--log-path`` 显式指定（调用方自己负责不是快照）；
    ② run-root **不是**快照 ⇒ 旧口径 ``<run-root>/verify_artifacts.log``；
    ③ run-root **是**快照 ⇒ canonical 根（快照根的上一级）``verify_artifacts_<快照名>.log``。
    """
    if explicit:
        return Path(explicit)
    if not _is_snapshot_dir(run_root):
        return run_root / LOG_NAME
    canonical = run_root.parent.parent if run_root.parent.name == "snapshots" else run_root.parent
    return canonical / f"verify_artifacts_{run_root.name}.log"


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 轮动线产物回归自检")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    ap.add_argument("--backtest", default=None, help="回测产物路径（默认取 run-root 里最新的 etf_rotation_backtest_*.json）")
    ap.add_argument("--log-path", default="", help="自检日志落点（缺省：快照 ⇒ canonical 根；非快照 ⇒ run-root）")
    args = ap.parse_args()

    run_root = Path(args.run_root)
    log_path = _resolve_log_path(run_root, args.log_path)
    report = _read_json(run_root / "etf_analysis_report.json")
    plan = _read_json(run_root / "final_rebalance_plan.json")
    rec = _read_json(run_root / "rebalance_recommendation.json")
    plan_md_path = run_root / "final_rebalance_plan.md"
    plan_md = plan_md_path.read_text(encoding="utf-8") if plan_md_path.exists() else ""

    backtest: Path | None = Path(args.backtest) if args.backtest else None
    if backtest is None:
        candidates = sorted(run_root.glob("etf_rotation_backtest_*.json"), key=lambda p: p.stat().st_mtime)
        backtest = candidates[-1] if candidates else None

    # `--skip-ta` 模式：本轮**按设计**没有 LLM 决策文件 ⇒ 不算缺失、也不算不自洽
    skip_ta = (
        isinstance(plan, dict)
        and not (plan.get("meta") or {}).get("source_decision")
        and not (plan.get("tradingagents_view") or "").strip()
    )

    results: list[tuple[str, bool, str]] = []
    results += _check_products(run_root, allow_missing_decision=skip_ta)
    if not isinstance(report, dict):
        results.append(("分析报告可读", False, "etf_analysis_report.json 缺失或损坏"))
    else:
        results += _check_report(report, run_root)
        results += _check_next_rebalance(report)
    if not isinstance(plan, dict):
        results.append(("最终计划可读", False, "final_rebalance_plan.json 缺失或损坏"))
    else:
        results += _check_final_plan(plan, plan_md, rec if isinstance(rec, dict) else {}, run_root)
    results += _check_account(run_root)
    results += _check_benchmark(run_root, backtest)
    results += _check_sha1_chain(run_root)
    results += _check_secret_leak(run_root)

    lines = []
    lines.append("=" * 78)
    lines.append("ETF 轮动线 · 产物回归自检")
    lines.append(f"run-root : {run_root}")
    lines.append(f"backtest: {backtest if backtest else '（无）'}")
    lines.append("=" * 78)
    failed = 0
    for name, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        lines.append(f"[{flag}] {name}")
        lines.append(f"        {detail}")
    lines.append("-" * 78)
    lines.append(f"合计 {len(results)} 项：PASS {len(results) - failed} / FAIL {failed}")
    text = "\n".join(lines)
    print(text)
    print(f"[INFO] 自检日志落点：{log_path}")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 —— 写日志失败不该掩盖自检结论
        print(f"[WARN] 写自检日志失败：{exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
