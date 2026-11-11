#!/usr/bin/env python
"""ETF 分析报告 · **规格符合性**回归探针（只读；退出码 0=通过 / 1=失败）。

【为什么要有这个探针】
用户给 `analyze.py` 写死了 10 段 JSON 规格（夜间任务书 §5）。此前**没有任何断言**守着它
⇒ 以后改 `analyze.py`（改字段名、删段落、把 `null` 填成 0）都能一路 PASS 到交付，
直到用户明早读报告才发现缺段。本探针把那份规格**逐条固化成可执行断言**。

【断言覆盖（用户任务书 §5 的 10 段 + 硬性要求）】
  meta / stock_scores / rank_ic / etf_scores / filter_stats /
  candidates / current_holdings_diagnosis / next_rebalance / risk_disclosure / disclaimer
  ＋ 硬性要求：无未来函数、`PYTHONHASHSEED=0`、α=0.5（铁律十四.3）、
    e_covered 池（铁律十四）、min_coverage=0.5、真实账户数不许改。

【守卫生效性自检（铁律十四.52：新增守卫必须自带负例）】
  正例 P0：正式报告原样 ⇒ 全部断言 PASS；
  负例 N1~N17：故意改坏 17 处 ⇒ 对应断言**必须真的 FAIL**（否则断言可能写死成永远 PASS）。
  ⚠️ 负例构造本身失败（StopIteration 等）⇒ 判 FAIL 并写清原因，**不许把探针带崩**。

【用法】
    PYTHONHASHSEED=0 /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_report_spec.py \
        --report <落点根>/runs/etf_analysis_report.json

【副作用】
不写任何文件（负例在内存里改副本）；只读正式报告与两个日历文件。
"""
from __future__ import annotations

import argparse
import ast
import copy
import csv
import json
import re
import sys

from pathlib import Path

#: 研究链根（探针目录不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ETF_ROOT = Path(__file__).resolve().parents[1]
if str(_ETF_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETF_ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）

#: qlib 历史日历（一行一个 YYYY-MM-DD）
QLIB_CALENDAR = Path(C.PROVIDER_URI) / "calendars" / "day.txt"
#: akshare 完整交易日历（**宽表**：第一行是表头 + 全部日期列）
AK_CALENDAR = C.ETF_DATA_ROOT / "trade_calendar.csv"
#: 被静态审计的源码（`analyze.py`；规格硬性要求：中文 docstring / 4 空格 / snake_case）
ANALYZE_SOURCE = Path(__file__).resolve().parents[1] / "analyze.py"
#: 账户事实源（**报告同目录**）：``account_context.json`` —— 期望值的唯一来源
#: 🔴 期望值**不再写死**（旧值 29854.67 / 15518.87 是 2026-11-04 的 5 只旧账户口径
#:    ⇒ 本轮真实账户是 2026-11-05 口径（1 只 512800 / 30035.57 / 23367.37）⇒ 写死必红）；
#:    事实源缺失 ⇒ H1 **必定 FAIL**（不许真空 PASS）。
ACCOUNT_CONTEXT_NAME = "account_context.json"
#: 金额**精确对账**容差（元）：事实源 ↔ 报告的同一笔数字，只许浮点表示噪声
ACCOUNT_EXACT_TOLERANCE = 1e-6
#: 风险披露必须覆盖的主题（用户任务书 §5 第 9 条点名的 7 项）
REQUIRED_RISK_IDS = (
    "in_sample_selection_bias",
    "current_drawdown_position",
    "w_snapshot_bias",
    "pool_attribute_limits",
    "turnover_and_cost",
    "non_lookthrough_etf",
    "strategy_capacity",
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_calendar() -> list[str]:
    """加载交易日历（qlib ∪ akshare），返回**升序**列表。

    🔴 两个来源的口径不同：qlib 的 `day.txt` 只到 2026-11-03（数据截止），
       akshare 的完整日历到 2026-12-31 ⇒ **必须取并集**，否则「下一个交易日」算不出来。
    """
    days: set[str] = set()
    for line in QLIB_CALENDAR.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if _DATE_RE.fullmatch(text):
            days.add(text)
    with AK_CALENDAR.open(encoding="utf-8") as handle:
        for row in csv.reader(handle):
            for cell in row:
                text = cell.strip()
                if _DATE_RE.fullmatch(text):
                    days.add(text)
    return sorted(days)


def load_account_expectation(report_dir: Path) -> dict:
    """从**报告同目录**的 ``account_context.json`` 派生账户期望值。

    返回 ``{"codes": [...], "total_assets": float|None, "cash": float|None,
    "source": str, "error": str}``。

    🔴 事实源缺失 / 不完整 ⇒ ``error`` 非空 ⇒ H1 **必须 FAIL**（不许真空 PASS）。
    """
    path = report_dir / ACCOUNT_CONTEXT_NAME
    info: dict = {"codes": [], "total_assets": None, "cash": None,
                  "source": str(path), "error": ""}
    if not path.is_file():
        info["error"] = f"账户事实源缺失 ⇒ 不许真空 PASS：{path}"
        return info
    try:
        ctx = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 事实源损坏必须报出来，不许静默
        info["error"] = f"账户事实源不可读 {path}：{type(exc).__name__}: {exc}"
        return info
    if not isinstance(ctx, dict):
        info["error"] = f"账户事实源格式非法 {path}（不是 JSON 对象）"
        return info
    positions = ctx.get("positions")
    if not isinstance(positions, list) or not positions:
        info["error"] = f"账户事实源 {path} 里没有非空 positions 列表"
        return info
    codes: list[str] = []
    for item in positions:
        if not isinstance(item, dict):
            info["error"] = f"账户事实源 {path} 的 positions 里有非对象元素"
            return info
        code = str(item.get("code") or "").strip()
        if not code:
            info["error"] = f"账户事实源 {path} 的 positions 里有缺 code 的行"
            return info
        codes.append(code)
    total_assets = ctx.get("total_assets")
    cash = ctx.get("cash")
    if not isinstance(total_assets, (int, float)) or isinstance(total_assets, bool):
        info["error"] = f"账户事实源 {path} 缺 total_assets"
        return info
    if not isinstance(cash, (int, float)) or isinstance(cash, bool):
        info["error"] = f"账户事实源 {path} 缺 cash"
        return info
    info["codes"] = sorted(codes)
    info["total_assets"] = float(total_assets)
    info["cash"] = float(cash)
    return info


class Checker:
    """收集断言结果（每条：id / 名称 / 是否通过 / 细节）。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str) -> None:
        """登记一条断言结果。"""
        self.items.append((check_id, name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, str, bool, str]]:
        """返回全部失败的断言。"""
        return [it for it in self.items if not it[2]]


def _get(report: dict, path: str, default=None):
    """按点号路径取嵌套字段（取不到返回 default）。"""
    node = report
    for part in path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _c1_candidates_verdict(report: dict) -> tuple[bool, str]:
    """C1：candidates 条数 == **min(params.top_n, 过滤后候选池存活数)**。

    🔴 2026-11-11 实测修（服务器快照 20261111_094103）：
       `config.ANALYZE_TOP_N` 由 15 提到 **100**，而**过滤后候选池实测只有 21 只**
       ⇒ 老断言「len(candidates) == params.top_n」在 ``top_n > 池存活数`` 时**恒错**
       （实测 top_n=100｜实得=21｜`filter_stats.n_candidate_pool`=21）。
    ⇒ 正确口径 = **min(top_n, 池存活数)**，且池存活数**必须从产物读**
      （`filter_stats.n_candidate_pool`）⇒ 不许写死 21、不许靠猜、不许「非空即 PASS」。
    ⇒ 池被抽干（``len < top_n``）**不是缺陷** ⇒ 允许；但池存活数**缺失** ⇒ 判 FAIL
      （算不出来 ≠ 一致）。
    """
    cands = _get(report, "candidates", []) or []
    top_n = _get(report, "meta.params.top_n")
    pool_n = _get(report, "filter_stats.n_candidate_pool")
    n_got = len(cands)
    if not (isinstance(top_n, int) and not isinstance(top_n, bool) and top_n > 0):
        return False, f"params.top_n 缺失/非法（{top_n!r}）⇒ 无法判定候选条数"
    if not (isinstance(pool_n, int) and not isinstance(pool_n, bool) and pool_n >= 0):
        return False, (
            f"filter_stats.n_candidate_pool 缺失/非法（{pool_n!r}）"
            "⇒ 池存活数算不出来 ⇒ 不许拿 top_n 猜（写死/猜都不许）"
        )
    expect = min(top_n, pool_n)
    ok = n_got == expect
    # 🔴 2026-11-11 措辞修（审计：老措辞把两种形态混成一句「池被抽干」）：
    #    ① **池 > top_n**（截断形态）⇒ 必须给满 top_n 条 ⇒ 少给就是「**静默截断**」；
    #    ② **池 ≤ top_n**（抽干形态）⇒ 池里就只有那么多 ⇒ 少给是**合法形态**。
    #    ⚠️ 老措辞还把解释指向「F2 的 filter_stats.stages 逐道计数」——
    #       但 **F2 只遍历 `per_net` 三张网**，根本不读 `filter_stats.stages`
    #       ⇒ 运维被指向一条**查不到该问题**的检查 ⇒ 现在改成指向真正的独立计数
    #       （`filter_stats.stages[extra_exit_ma].n_out`，见 C1b）。
    if pool_n > top_n:
        if n_got < top_n:
            why = (f"池 {pool_n} > top_n {top_n} ⇒ 必须给满 {top_n} 条，实得 {n_got}"
                   f" ⇒ **静默截断**（少给 {top_n - n_got} 条）")
        else:
            why = f"池 {pool_n} > top_n {top_n} ⇒ 候选清单按 top_n 截断（实得 {n_got} 条，**没少给**）"
    elif n_got < top_n:
        why = (f"池 {pool_n} ≤ top_n {top_n} ⇒ 池被抽干（实得 {n_got} < top_n {top_n}）"
               f" ⇒ **合法形态**（池里就只有 {pool_n} 只）；池子条数请对 "
               "filter_stats.stages[extra_exit_ma].n_out（见 C1b）")
    else:
        why = f"池 {pool_n} ≤ top_n {top_n} ⇒ 候选清单未截断（池全量给出）"
    return ok, (
        f"top_n={top_n}｜池存活 n_candidate_pool={pool_n}｜期望 min(top_n, 池)={expect}"
        f"｜实得={n_got}｜{why}"
    )


def _c1b_pool_count_verdict(report: dict) -> tuple[bool, str]:
    """C1b：`filter_stats.n_candidate_pool` 必须与**产物里的另一个独立计数**对账。

    🔴 为什么必须有（C1 的盲区，审计实测）：
       C1 只比 `len(candidates)` 与 `min(top_n, n_candidate_pool)` ⇒ 这**两个数自洽地
       一起改**就能骗过它（实测形态：top_n=100 / pool=10 / cands=10 ⇒ C1 PASS，
       而真实池子是 21 只 ⇒ 假绿）。
    ⇒ 同一份产物里另有两个**独立计数**可以对账：
       ① `filter_stats.stages[stage=extra_exit_ma].n_out`
          —— 「附加 · 持仓强制退出（MA60）」这道**过滤道**走完后的**池子条数**
          （`apply_theme_cap` 只打 `selected` 标记、**不删行** ⇒ 该段 n_out == len(pool)
          == `n_candidate_pool`；见 `analyze.py::_build_filter_chain`）；
       ② `etf_scores.n_candidate_pool_total`（池内 ETF 行数）⇒ **存在时**互证。
    🔴 口径：算不出来 ≠ 一致 ⇒ 必需的段缺失/非法一律判 FAIL（fail-closed）。
    """
    pool_n = _get(report, "filter_stats.n_candidate_pool")
    stages = _get(report, "filter_stats.stages") or []
    stage_out = None
    stage_found = False
    for st in stages:
        if isinstance(st, dict) and str(st.get("stage") or "") == "extra_exit_ma":
            stage_out = st.get("n_out")
            stage_found = True
            break
    total = _get(report, "etf_scores.n_candidate_pool_total")
    problems: list[str] = []
    if not (isinstance(pool_n, int) and not isinstance(pool_n, bool) and pool_n >= 0):
        problems.append(f"filter_stats.n_candidate_pool 缺失/非法（{pool_n!r}）")
    if not stage_found:
        problems.append("filter_stats.stages 里没有 stage=extra_exit_ma 这一段")
    elif not (isinstance(stage_out, int) and not isinstance(stage_out, bool) and stage_out >= 0):
        problems.append(f"filter_stats.stages[extra_exit_ma].n_out 非法（{stage_out!r}）")
    if total is not None and not (isinstance(total, int) and not isinstance(total, bool)
                                  and total >= 0):
        problems.append(f"etf_scores.n_candidate_pool_total 非法（{total!r}）")
    if not problems:
        if pool_n != stage_out:
            problems.append(
                f"n_candidate_pool={pool_n} ≠ stages[extra_exit_ma].n_out={stage_out}"
                "（两个独立计数打架 ⇒ 池子条数被自洽篡改过）")
        if total is not None and pool_n != total:
            problems.append(
                f"n_candidate_pool={pool_n} ≠ etf_scores.n_candidate_pool_total={total}")
    ok = not problems
    detail = (
        f"n_candidate_pool={pool_n!r}｜stages[extra_exit_ma].n_out={stage_out!r}"
        f"｜etf_scores.n_candidate_pool_total={total!r}"
        + ("｜问题：" + "；".join(problems) if problems else "｜三者一致 ✓")
        + ("" if total is not None else "｜注：etf_scores.n_candidate_pool_total 缺失 ⇒ 互证项跳过")
    )
    return ok, detail

def run_checks(report: dict, calendar: list[str],
               account: dict | None = None) -> list[tuple[str, str, bool, str]]:
    """对报告跑全部规格断言，返回 [(id, 名称, 是否通过, 细节)]。

    ``account`` = 账户事实源派生值（见 :func:`load_account_expectation`）；
    缺省 ``None`` ⇒ H1 按 fail-closed 判 FAIL（不许真空 PASS）。
    """
    checker = Checker()
    asof = str(_get(report, "meta.asof", "") or "")

    # ---------- §1 meta ----------
    missing_meta = [k for k in ("asof", "generated_at", "host", "hash_seed", "inputs", "params")
                    if _get(report, f"meta.{k}") in (None, "", {}, [])]
    checker.add("M1", "meta 必需字段齐全（asof/生成时间/host/hash_seed/inputs/params）",
                not missing_meta, f"缺字段={missing_meta or '无'}｜asof={asof}")
    bad_fp = []
    for name, item in (_get(report, "meta.inputs", {}) or {}).items():
        if not isinstance(item, dict) or any(k not in item for k in ("path", "exists", "size_bytes")):
            bad_fp.append(name)
    checker.add("M2", "meta.inputs 每项都带输入指纹（path/exists/size_bytes）",
                not bad_fp, f"指纹缺失={bad_fp or '无'}｜输入数={len(_get(report, 'meta.inputs', {}) or {})}")
    params = _get(report, "meta.params", {}) or {}
    missing_params = [k for k in ("pool_mode", "alpha_weight", "momentum_weight", "target_n_holdings",
                                  "hold_buffer", "entry_ma_window", "exit_ma_window")
                      if params.get(k) is None]
    checker.add("M3", "meta.params 生效参数齐全（含组合 topk / 破位窗口 / 缓冲）",
                not missing_params,
                f"缺参数={missing_params or '无'}｜pool_mode={params.get('pool_mode')}"
                f"｜α={params.get('alpha_weight')}｜动量={params.get('momentum_weight')}"
                f"｜topk={params.get('target_n_holdings')}｜buffer={params.get('hold_buffer')}"
                f"｜MA{params.get('entry_ma_window')}/{params.get('exit_ma_window')}")
    checker.add("M4", "α=0.5（铁律十四.3：E 是暴露分，不是独立 alpha）",
                float(params.get("alpha_weight", -1)) == 0.5,
                f"alpha_weight={params.get('alpha_weight')}（期望 0.5）")
    checker.add("M5", "PYTHONHASHSEED=0 可复现（meta.hash_seed == '0'）",
                str(_get(report, "meta.hash_seed", "")) == "0",
                f"hash_seed={_get(report, 'meta.hash_seed')!r}（期望 '0'）")

    # ---------- §2 stock_scores ----------
    ss = _get(report, "stock_scores", {}) or {}
    missing_ss = [k for k in ("n_stocks", "mean", "std", "min", "p25", "p50", "p75", "max",
                              "unique_values") if ss.get(k) is None]
    checker.add("S1", "stock_scores 横截面统计量齐全（只数/均值/标准差/分位/唯一值数）",
                not missing_ss,
                f"缺字段={missing_ss or '无'}｜n={ss.get('n_stocks')}｜唯一值={ss.get('unique_values')}")
    unique_values = ss.get("unique_values")
    std = ss.get("std")
    degenerate_ok = (isinstance(unique_values, int) and unique_values > 1
                     and isinstance(std, (int, float)) and std > 0
                     and bool((_get(report, "stock_scores.degenerate_check", {}) or {}).get("passed")))
    checker.add("S2", "退化守卫：唯一值数 > 1 且标准差 > 0 且 degenerate_check.passed",
                degenerate_ok,
                f"唯一值={unique_values}｜std={std}｜passed="
                f"{(_get(report, 'stock_scores.degenerate_check', {}) or {}).get('passed')}"
                "（铁律十四.5：宁可不产出，不产出假结果）")
    n_stocks = ss.get("n_stocks")
    n_rows = ss.get("n_rows_raw")
    n_non_finite = ss.get("n_non_finite")
    ss_consistent = (isinstance(n_stocks, int) and isinstance(n_rows, int)
                     and isinstance(n_non_finite, int) and n_stocks == n_rows - n_non_finite)
    checker.add("S3", "stock_scores 计数自洽（n_stocks == n_rows_raw − n_non_finite）",
                ss_consistent, f"n_stocks={n_stocks}｜n_rows_raw={n_rows}｜n_non_finite={n_non_finite}")

    # ---------- §3 rank_ic ----------
    ri = _get(report, "rank_ic", {}) or {}
    horizons = ri.get("horizons") or {}
    checker.add("R1", "rank_ic 同时给出 N=5 与 N=20（用户规格点名的两个窗口）",
                "5" in horizons and "20" in horizons, f"horizons={sorted(horizons.keys())}")
    bad_h = []
    for key, item in horizons.items():
        status = (item or {}).get("status")
        if status == "ok":
            for field in ("mean_rank_ic", "icir", "t_stat", "n_cross_sections"):
                if (item or {}).get(field) is None:
                    bad_h.append(f"{key}.{field}")
            n_cs = (item or {}).get("n_cross_sections")
            if not isinstance(n_cs, int) or n_cs < 5:
                bad_h.append(f"{key}.n_cross_sections<5")
        else:
            # 🔴 规格原文：「样本 <5 个截面 ⇒ 输出 null 并注明『样本不足』，不许编数字」
            if any((item or {}).get(f) is not None
                   for f in ("mean_rank_ic", "icir", "t_stat")):
                bad_h.append(f"{key}:status!=ok 却带数字")
            if not str((item or {}).get("reason") or ""):
                bad_h.append(f"{key}:status!=ok 却无原因说明")
    checker.add("R2", "rank_ic 每个窗口：ok ⇒ 指标齐全且截面 ≥5；非 ok ⇒ 数值为 null 且注明原因",
                not bad_h, f"问题={bad_h or '无'}")
    late_dates = []
    for key, item in horizons.items():
        for row in (item or {}).get("per_date") or []:
            date = str(row.get("date") or "")
            if asof and date > asof:
                late_dates.append(f"{key}:{date}")
    checker.add("R3", "rank_ic 的截面日期 ≤ asof（无未来函数）",
                not late_dates, f"越界日期={late_dates[:5] or '无'}｜asof={asof}")
    checker.add("R4", "rank_ic 标注为「仅评估指标」，不参与选股决策",
                bool(ri.get("evaluation_only")), f"evaluation_only={ri.get('evaluation_only')}")

    # ---------- §4 etf_scores ----------
    es = _get(report, "etf_scores", {}) or {}
    rows = es.get("rows") or []
    checker.add("E1", "etf_scores 注明 W 快照日与来源",
                bool(es.get("w_snapshot_date")) and bool(es.get("w_source")),
                f"w_snapshot_date={es.get('w_snapshot_date')}｜w_source={es.get('w_source')}")
    # 🔴 口径（铁律十四.12）：**穿透为 0 的 ETF 其 E 算不出来** ⇒ `e_score/coverage/e_rank`
    #    必须是 **null**，**不许**填 0 冒充「算出来是 0」。
    #    ⚠️ 「穿透为 0」有**两种**成因，`n_constituents` 都合法：
    #       ① `etf_holdings.csv` **无该 ETF 记录**（数据缺口）⇒ n_constituents = 0；
    #       ② **有成分记录，但成分股一只都不在 qlib 个股池里**（真实穿透为 0）⇒ n_constituents > 0。
    #    ⇒ 两种成因都必须把 `n_constituents_scored` 留 **null**（不许填 0 冒充「有成分被评分」）。
    bad_row = []
    no_e_rows = 0
    no_record = 0
    for row in rows:
        code = row.get("code")
        if row.get("e_score") is None:
            no_e_rows += 1
            if row.get("coverage") is not None:
                bad_row.append(f"{code}:无 E 却有 coverage={row.get('coverage')}（不许填 0 冒充）")
            if row.get("e_rank") is not None:
                bad_row.append(f"{code}:无 E 却有 e_rank")
            if row.get("n_constituents_scored") is not None:
                bad_row.append(f"{code}:穿透为 0 却有 n_constituents_scored="
                               f"{row.get('n_constituents_scored')}（不许冒充有成分被评分）")
            if not isinstance(row.get("n_constituents"), int):
                bad_row.append(f"{code}:n_constituents 非整数")
            elif row.get("n_constituents") == 0:
                no_record += 1
            if row.get("coverage_ok") is not False:
                bad_row.append(f"{code}:穿透为 0 却 coverage_ok=True")
        else:
            for field in ("code", "name", "coverage", "n_constituents", "e_rank"):
                if row.get(field) is None:
                    bad_row.append(f"{code}.{field}")
    checker.add("E2", "etf_scores：有 E 的带全字段；穿透为 0 的必须 null + 点名（不许填 0 冒充）",
                not bad_row and bool(rows),
                f"行数={len(rows)}｜有 E={len(rows) - no_e_rows}｜穿透为 0={no_e_rows}"
                f"（其中无成分记录 {no_record} / 有记录但穿透为 0 {no_e_rows - no_record}）"
                f"｜问题={bad_row[:5] or '无'}")
    ranks = sorted(row.get("e_rank") for row in rows if row.get("e_rank") is not None)
    # 🔴 2026-11-04 夜实测修（M-F；reviewer 遗留风险）：
    #    旧判定 `len(ranks) == len(set(ranks)) and ranks == list(range(1, len(ranks)+1))`
    #    在 **ranks 为空** 时是 `[] == []` ⇒ **恒 True（真空 PASS）**
    #    ⇒ 把所有 e_rank 抹掉（或报告里一只有 E 的都没有）这条断言照样 PASS。
    #    ⇒ 修法：显式要求「有排名行数 > 0」。
    rank_ok = (
        len(ranks) > 0
        and len(ranks) == len(set(ranks))
        and ranks == list(range(1, len(ranks) + 1))
    )
    checker.add("E3", "etf_scores 的 e_rank（有 E 的子集）唯一且是 1..N 的连续排名",
                rank_ok, f"有排名行数={len(ranks)}｜最大={ranks[-1] if ranks else '（无）'}｜连续={rank_ok}"
                         "（空集判 FAIL：避免真空 PASS）")
    checker.add("E4", "e_covered 池口径：min_coverage = 0.5",
                float(es.get("min_coverage", -1)) == 0.5,
                f"min_coverage={es.get('min_coverage')}（期望 0.5）")

    # ---------- §5 filter_stats ----------
    fs = _get(report, "filter_stats", {}) or {}
    per_net = fs.get("per_net") or {}
    net_ok = len(per_net) == 3
    checker.add("F1", "filter_stats 三道过滤网齐备（逐道进/出计数）",
                net_ok, f"per_net={sorted(per_net.keys())}")
    bad_count = []
    for name, net in per_net.items():
        n_in = (net or {}).get("n_in")
        n_out = (net or {}).get("n_out")
        n_elim = (net or {}).get("n_eliminated")
        if not all(isinstance(v, int) for v in (n_in, n_out, n_elim)):
            bad_count.append(f"{name}:非整数")
        elif n_in - n_elim != n_out:
            bad_count.append(f"{name}:{n_in}-{n_elim}≠{n_out}")
    checker.add("F2", "过滤网逐道计数自洽（n_in − n_eliminated == n_out）",
                not bad_count, f"不自洽={bad_count or '无'}")
    no_reason = []
    for name, net in per_net.items():
        for item in (net or {}).get("eliminated") or []:
            if not str(item.get("reason") or "").strip():
                no_reason.append(f"{name}:{item.get('code')}")
    checker.add("F3", "被淘汰名单每条都带原因（能看出「为什么没进」）",
                not no_reason,
                f"缺原因={no_reason[:5] or '无'}｜淘汰条目数="
                f"{sum(len((n or {}).get('eliminated') or []) for n in per_net.values())}")
    selected = fs.get("selected") or []
    checker.add("F4", "最终入选名单非空且池口径为 e_covered",
                bool(selected) and fs.get("pool_mode") == "e_covered",
                f"入选={len(selected)} 只 {[s.get('code') for s in selected]}｜pool_mode={fs.get('pool_mode')}")

    # ---------- §6 candidates ----------
    cands = _get(report, "candidates", []) or []
    # 🔴 2026-11-11：口径改成 min(top_n, 池存活数) ⇒ 池存活数从产物读（不许写死 21）。
    _c1_ok, _c1_detail = _c1_candidates_verdict(report)
    checker.add("C1", "candidates 数量 == min(params.top_n, filter_stats.n_candidate_pool)",
                _c1_ok, _c1_detail)
    # 🔴 2026-11-11 新增 C1b：堵「n_candidate_pool 与 len(candidates) **自洽篡改**」的假绿
    _c1b_ok, _c1b_detail = _c1b_pool_count_verdict(report)
    checker.add("C1b", "filter_stats.n_candidate_pool 与 stages[extra_exit_ma].n_out 对账"
                       "（etf_scores.n_candidate_pool_total 互证）",
                _c1b_ok, _c1b_detail)
    bad_c = []
    for row in cands:
        code = row.get("code")
        for field in ("code", "name", "final_score", "broken", "theme_id", "mom20", "mom60", "mom120"):
            if row.get(field) is None:
                bad_c.append(f"{code}.{field}")
        if row.get("e_score") is None and row.get("n_constituents") != 0:
            bad_c.append(f"{code}:无 E 却有成分数 {row.get('n_constituents')}")
        if row.get("e_score") is not None and row.get("coverage") is None:
            bad_c.append(f"{code}:有 E 却缺 coverage")
    checker.add("C2", "candidates 每行含 code/name/最终分/破位/主题簇/动量；E 缺失须点名",
                not bad_c and bool(cands),
                f"行数={len(cands)}｜无 E 的候选={[r.get('code') for r in cands if r.get('e_score') is None]}"
                f"｜问题={bad_c[:5] or '无'}")
    cand_ranks = [row.get("rank") for row in cands]
    # 🔴 同 E3：cands 为空时 `[] == []` ⇒ 恒 True（真空 PASS）⇒ 显式要求非空。
    cand_rank_ok = (
        len(cand_ranks) > 0 and sorted(cand_ranks) == list(range(1, len(cand_ranks) + 1))
    )
    checker.add("C3", "candidates 的 rank 是 1..N 连续排名",
                cand_rank_ok,
                f"rank={cand_ranks[:5]}…｜空集判 FAIL：避免真空 PASS")

    # ---------- §7 current_holdings_diagnosis ----------
    hd = _get(report, "current_holdings_diagnosis", {}) or {}
    holdings = hd.get("holdings") or []
    # 🔴 期望值来自**报告同目录的账户事实源**（``account_context.json``）⇒ 不再写死旧账户口径。
    if account is None:
        acct: dict = {}
        acct_err = "未加载账户事实源 ⇒ 不许真空 PASS"
    else:
        acct = account
        acct_err = str(acct.get("error") or "")
    exp_codes = list(acct.get("codes") or [])
    exp_total = acct.get("total_assets")
    exp_cash = acct.get("cash")
    got_codes = sorted(str(row.get("code") or "").strip() for row in holdings)
    if acct_err or not exp_codes:
        checker.add("H1", "真实持仓逐只诊断（与账户事实源一致）+ 总资产/现金精确对账",
                    False,
                    f"{acct_err}｜诊断持仓={got_codes}")
    else:
        got_total = hd.get("total_assets")
        got_cash = hd.get("cash")
        ok_codes = got_codes == exp_codes
        ok_total = (
            isinstance(got_total, (int, float)) and not isinstance(got_total, bool)
            and abs(float(got_total) - float(exp_total)) <= ACCOUNT_EXACT_TOLERANCE
        )
        ok_cash = (
            isinstance(got_cash, (int, float)) and not isinstance(got_cash, bool)
            and abs(float(got_cash) - float(exp_cash)) <= ACCOUNT_EXACT_TOLERANCE
        )
        checker.add("H1", "真实持仓逐只诊断（与账户事实源一致）+ 总资产/现金精确对账",
                    ok_codes and ok_total and ok_cash,
                    f"口径={acct.get('source')}｜诊断持仓={got_codes}｜期望={exp_codes}（精确相等）"
                    f"｜总资产 {got_total} vs {exp_total}｜现金 {got_cash} vs {exp_cash}")
    bad_h = []
    for row in holdings:
        for field in ("code", "name", "shares", "market_value", "pnl_pct", "in_candidate_pool",
                      "ma20_ok", "ma60_ok", "broken", "mom120"):
            if row.get(field) is None:
                bad_h.append(f"{row.get('code')}.{field}")
    checker.add("H2", "每只持仓带诊断字段（在池/MA20/MA60/破位/E/动量/市值/盈亏）",
                not bad_h, f"缺字段={bad_h[:5] or '无'}")
    unselected = [row for row in holdings if not row.get("selected")]
    no_reason_h = [row.get("code") for row in unselected
                   if not str(row.get("reject_reason") or "").strip()]
    # 🔴 本轮账户只有 1 只持仓且 **selected=True** ⇒ 「未入选」集合为空 ⇒
    #    H3 恒 PASS（**真空 PASS**）⇒ 必须在 detail 里说清楚，避免被误读成「守卫有效」。
    vacuous_note = (
        "｜本轮**无未入选持仓** ⇒ 真空 PASS（本断言本轮不构成有效拦人，负例 N6 用夹具补测）"
        if not unselected else ""
    )
    checker.add("H3", "每只未入选的持仓都写明被哪道网淘汰 + 原因",
                not no_reason_h,
                f"缺原因={no_reason_h or '无'}｜淘汰阶段="
                f"{[(row.get('code'), row.get('rejected_by_stage')) for row in unselected]}"
                f"{vacuous_note}")

    # ---------- §8 next_rebalance ----------
    nr = _get(report, "next_rebalance", {}) or {}
    target = str(nr.get("target_date") or "")
    in_cal = target in set(calendar)
    checker.add("N1", "下一调仓节点在交易日历内（独立重算，不信任产物自述）",
                bool(target) and in_cal,
                f"target_date={target}｜日历内={in_cal}｜日历源={QLIB_CALENDAR} ∪ {AK_CALENDAR}")
    month_days = [d for d in calendar if d.startswith(str(nr.get("target_month") or ""))]
    is_first = bool(month_days) and month_days[0] == target
    checker.add("N2", "下一节点是目标自然月的**第一个交易日**",
                is_first, f"当月首个交易日={month_days[0] if month_days else '（无）'}｜target={target}")
    recomputed = sum(1 for d in calendar if asof < d <= target)
    checker.add("N3", "距该日的交易日数与独立重算一致（口径 = (asof, target] 左开右闭）",
                nr.get("trading_days_until") == recomputed,
                f"产物={nr.get('trading_days_until')}｜重算={recomputed}｜asof={asof}")
    checker.add("N4", "日历校验标记为真（in_calendar_verified）",
                bool(nr.get("in_calendar_verified")), f"in_calendar_verified={nr.get('in_calendar_verified')}")

    # ---------- §9 risk_disclosure ----------
    risks = _get(report, "risk_disclosure", []) or []
    got_ids = {str((r or {}).get("id") or "") for r in risks}
    missing_risk = [rid for rid in REQUIRED_RISK_IDS if rid not in got_ids]
    checker.add("G1", "risk_disclosure 非空数组且覆盖规格点名的 7 个主题",
                bool(risks) and not missing_risk,
                f"条数={len(risks)}｜缺主题={missing_risk or '无'}")
    detail_by_id = {str(r.get("id")): str(r.get("detail") or "") for r in risks}
    no_num_required = [rid for rid in REQUIRED_RISK_IDS
                       if not re.search(r"\d", detail_by_id.get(rid, ""))]
    checker.add("G2", "规格点名的 7 个风险主题，每条都带数字（不许写「效果不错」这种无数字结论）",
                not no_num_required, f"无数字的必需主题={no_num_required or '无'}")
    empty_detail = [str((r or {}).get("id")) for r in risks if not str((r or {}).get("detail") or "").strip()]
    checker.add("G3", "全部风险条目的 detail 非空",
                not empty_detail, f"空 detail={empty_detail or '无'}｜条数={len(risks)}")

    # ---------- §10 disclaimer ----------
    disclaimer = str(_get(report, "disclaimer", "") or "")
    checker.add("D1", "disclaimer 明确「不构成投资建议」",
                "不构成投资建议" in disclaimer, f"disclaimer 前 60 字={disclaimer[:60]!r}")

    # ---------- §11 回测口径真值（meta.backtest_summary；2026-11-05 00:0x 新增） ----------
    bs = _get(report, "meta.backtest_summary", None)
    checker.add("B1", "meta.backtest_summary 类型合法（dict 或 None＝产物不可得）",
                bs is None or isinstance(bs, dict), f"类型={type(bs).__name__}")
    if isinstance(bs, dict):
        pf = bs.get("portfolio") or {}
        ex = bs.get("excess_relative") or {}
        bm = bs.get("benchmark_510300") or {}
        co = bs.get("cost") or {}
        segs = bs.get("segments") or []
        need = {
            "组合年化": pf.get("annualized_return"),
            "相对超额": ex.get("annualized_return"),
            "组合 MDD": pf.get("max_drawdown"),
            "基准年化": bm.get("annualized_return"),
            "年化换手": co.get("annualized_one_way_turnover"),
            "窗口天数": (bs.get("eval_window") or {}).get("n_days"),
        }
        miss = [k for k, v in need.items() if v is None]
        checker.add("B2", "回测真值关键项齐全（年化/超额/MDD/基准/换手/窗口天数）",
                    not miss, f"缺项={miss or '无'}")
        bench = bm.get("annualized_return")
        bench_ok = bench is not None and abs(float(bench) - 0.1211) <= 0.0005
        checker.add("B3", "基准 510300 年化 == 12.11% ± 0.05pp（回归探针，窗口被改坏就 FAIL）",
                    bench_ok, f"基准年化={bench!r}｜期望 0.1211±0.0005")
        checker.add("B4", "分段至少 2 段，且 segment_sign_opposite 与实际符号自洽",
                    len(segs) >= 2
                    and bool(bs.get("segment_sign_opposite"))
                    == (any((sg.get("excess_annualized") or 0) > 0 for sg in segs)
                        and any((sg.get("excess_annualized") or 0) < 0 for sg in segs)),
                    f"段数={len(segs)}｜sign_opposite={bs.get('segment_sign_opposite')}")
        # 🔴 B5：与**回测产物独立重算**比对（不许自说自话 ⇒ 数字漂移必被抓住）
        bt_path = str((_get(report, "meta.inputs.backtest_report.path", "") or ""))
        bt_ok, bt_detail = True, "产物不可得 ⇒ 跳过独立重算"
        if bt_path and Path(bt_path).exists():
            try:
                raw = json.loads(Path(bt_path).read_text(encoding="utf-8"))
                pairs = [
                    ("组合年化", pf.get("annualized_return"),
                     (raw.get("portfolio_net") or {}).get("annualized_return")),
                    ("相对超额", ex.get("annualized_return"),
                     (raw.get("excess_vs_510300") or {}).get("annualized_return")),
                    ("基准年化", bm.get("annualized_return"),
                     (raw.get("benchmark_510300") or {}).get("annualized_return")),
                    ("组合 MDD", pf.get("max_drawdown"),
                     (raw.get("portfolio_net") or {}).get("max_drawdown")),
                    ("年化换手", co.get("annualized_one_way_turnover"),
                     (raw.get("cost_assumption") or {}).get("annualized_one_way_turnover")),
                ]
                for sg in segs:
                    block_ = (raw.get("segment_report") or {}).get(sg.get("key")) or {}
                    pairs.append((f"分段 {sg.get('key')} 超额", sg.get("excess_annualized"),
                                  (block_.get("excess") or {}).get("annualized_return")))
                bad = []
                for name, got, want in pairs:
                    if got is None or want is None:
                        bad.append(f"{name}(缺)")
                    elif abs(float(got) - float(want)) > 1e-9:
                        bad.append(f"{name}(产物={got} vs 重算={want})")
                bt_ok = not bad
                bt_detail = f"不一致={bad or '无'}｜产物={bt_path}"
            except Exception as exc:  # noqa: BLE001
                bt_ok, bt_detail = False, f"读产物失败：{type(exc).__name__}: {exc}"
        checker.add("B5", "回测真值与**回测产物独立重算**逐位一致（不许自说自话）",
                    bt_ok, bt_detail)
        checker.add("B6", "summary.source_path 与 meta.inputs 指向同一产物",
                    str(bs.get("source_path") or "") == bt_path,
                    f"source_path={bs.get('source_path')!r}｜inputs={bt_path!r}")

    # ---------- 硬性要求：asof 与报告日期一致 ----------
    checker.add("X1", "stock_scores 截面日 == meta.asof（口径一致）",
                str(ss.get("date") or "") == asof, f"stock_scores.date={ss.get('date')}｜asof={asof}")

    return checker.items


def run_static_checks(source: str) -> list[tuple[str, str, bool, str]]:
    """对 `analyze.py` **源码**跑静态断言（规格 §5 硬性要求那一组）。

    检查项：4 空格（无 tab）、函数名 snake_case、所有函数有**中文** docstring、
    数值输入**不许** `fillna(0)`、每一处 `fillna` 都必须**显式声明**（中文注释）、
    模块级 docstring 含中文。

    ⚠️ `fillna` 的判定用 **AST**（只认真实的函数调用），不做文本匹配
    ⇒ docstring 里写的「绝不 `fillna(0)` 蒙混过关」**不会**被误判成调用。
    """
    checker = Checker()
    lines = source.splitlines()
    tab_lines = [i + 1 for i, line in enumerate(lines) if "\t" in line]
    checker.add("A1", "源码不用 tab 缩进（4 空格口径）",
                not tab_lines, f"含 tab 的行={tab_lines[:5] or '无'}｜总行数={len(lines)}")
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    bad_name = [n.name for n in funcs if not re.fullmatch(r"_?[a-z][a-z0-9_]*", n.name)]
    checker.add("A2", "所有函数名是 snake_case（允许前导 _）",
                not bad_name, f"不合规={bad_name or '无'}｜函数数={len(funcs)}")
    no_doc = [n.name for n in funcs if not ast.get_docstring(n)]
    no_cjk = [n.name for n in funcs
              if ast.get_docstring(n) and not re.search(r"[\u4e00-\u9fff]", ast.get_docstring(n))]
    checker.add("A3", "所有函数都有**中文** docstring",
                not no_doc and not no_cjk,
                f"缺 docstring={no_doc or '无'}｜docstring 无中文={no_cjk or '无'}")
    fillna_calls = [node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fillna"]
    zero_calls = []
    for node in fillna_calls:
        if (node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, (int, float))
                and not isinstance(node.args[0].value, bool)
                and node.args[0].value == 0):
            zero_calls.append(node.lineno)
    checker.add("A4", "数值输入**不许** fillna(0)（规格硬性要求：宁可不产出，不产出假结果）",
                not zero_calls,
                f"fillna(0) 的行={zero_calls or '无'}｜fillna 调用总数={len(fillna_calls)}")
    undeclared = []
    for node in fillna_calls:
        index = node.lineno - 1
        window = lines[index] if index < len(lines) else ""
        if index > 0:
            window = window + "\n" + lines[index - 1]
        if not re.search(r"[\u4e00-\u9fff]", window):
            undeclared.append(node.lineno)
    checker.add("A5", "每一处 fillna 都必须**显式声明**（同行或上一行有中文说明，不许蒙混）",
                not undeclared, f"未声明的行={undeclared or '无'}")
    module_doc = ast.get_docstring(tree)
    module_ok = bool(module_doc) and bool(re.search(r"[\u4e00-\u9fff]", module_doc))
    checker.add("A6", "模块级 docstring 存在且含中文（口径与铁律写在文件头）",
                module_ok, f"模块 docstring 长度={len(module_doc or '')}")
    return checker.items


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 分析报告 · 规格符合性探针（只读）")
    parser.add_argument("--report", required=True, help="etf_analysis_report.json 路径")
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"[FAIL] 报告不存在：{report_path}")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    calendar = load_calendar()
    # 🔴 账户事实源 = **报告同目录**的 ``account_context.json``（缺失 ⇒ H1 fail-closed）
    account = load_account_expectation(report_path.resolve().parent)

    lines: list[str] = []
    failures: list[str] = []

    def emit(tag: str, name: str, ok: bool, detail: str) -> None:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}")
        lines.append(f"        {detail}")
        if not ok:
            failures.append(f"{tag} {name}")

    # ---- 正例：正式报告原样 ----
    results = run_checks(report, calendar, account)
    for check_id, name, ok, detail in results:
        emit(check_id, name, ok, detail)

    # ---- 正例：analyze.py 源码 ----
    if not ANALYZE_SOURCE.exists():
        emit("A0", f"源码可读：{ANALYZE_SOURCE}", False, "文件不存在")
        source_text = ""
    else:
        source_text = ANALYZE_SOURCE.read_text(encoding="utf-8")
        for check_id, name, ok, detail in run_static_checks(source_text):
            emit(check_id, name, ok, detail)

    # ---- 负例：故意改坏 ⇒ 对应断言必须真的 FAIL ----
    def mutate(fn):
        bad = copy.deepcopy(report)
        fn(bad)
        return {cid: ok for cid, _n, ok, _d in run_checks(bad, calendar, account)}

    def _break_reject_reason(rep: dict) -> None:
        """把某只**未入选**持仓的淘汰原因抹掉 ⇒ H3 必须 FAIL。

        🔴 本轮唯一持仓是 ``selected=True``、``reject_reason=None``
           ⇒ 旧负例直接抹 ``holdings[0].reject_reason`` **进不了 H3 的判定集**
           ⇒ H3 必然仍 PASS ⇒ 负例**无牙**。
           ⇒ 修法：先找「未入选且有 reject_reason」的行；找不到就**自造夹具**
             （塞一条 ``selected=False`` 且带 reject_reason 的行）再抹它的原因。
        """
        hd = rep.get("current_holdings_diagnosis")
        if not isinstance(hd, dict):
            hd = {}
            rep["current_holdings_diagnosis"] = hd
        holdings = hd.get("holdings")
        if not isinstance(holdings, list):
            holdings = []
            hd["holdings"] = holdings
        target = next(
            (row for row in holdings
             if isinstance(row, dict) and not row.get("selected")
             and str(row.get("reject_reason") or "").strip()),
            None,
        )
        if target is None:
            target = {
                "code": "999999", "name": "夹具-未入选持仓", "shares": 0,
                "market_value": 0.0, "pnl_pct": 0.0, "in_candidate_pool": True,
                "ma20_ok": True, "ma60_ok": True, "broken": False, "mom120": 0.0,
                "selected": False, "rejected_by_stage": "filter_net_1_liquidity",
                "rejected_by_stage_title": "流动性过滤",
                "reject_reason": "夹具：近 20 日平均成交额不足（负例用）",
                "reject_detail": "夹具",
            }
            holdings.append(target)
        target["reject_reason"] = ""

    def _break_pool_vs_candidates(rep: dict) -> None:
        """把「池存活数 ↔ 候选清单条数」的关系改坏 ⇒ C1 必须 FAIL。

        🔴 反例必须**在两种形态下都咬人**（探针可能跑在截断形态或抽干形态的报告上）：
          ① 池存活 > top_n（截断形态）⇒ 候选清单必须**给满 top_n** ⇒ 少给一条 = 静默截断；
          ② 池存活 ≤ top_n（抽干形态）⇒ 候选清单条数 == 池存活数 ⇒ 谎报池数即不自洽。
        🔴 2026-11-11 修（审计：else 分支在「池 == top_n」形态下**咬不动**）：
           旧 else 分支把池数改成 `len(cands)+1` ⇒ 若 `len(cands)+1 > top_n`，
           则 `min(top_n, 池)` 仍是 `top_n` == 实得条数 ⇒ **C1 照样 PASS** ⇒ 负例失效（假红）。
           ⇒ 现在：`len(cands)+1 > top_n` 时**改成截断 candidates 一条**（池数不动）
             ⇒ 期望值不变、实得少一条 ⇒ 两种形态都咬。
        """
        cands = rep.get("candidates") or []
        pool = (rep.get("filter_stats") or {}).get("n_candidate_pool")
        top_n = ((rep.get("meta") or {}).get("params") or {}).get("top_n")
        if isinstance(pool, int) and isinstance(top_n, int) and pool > top_n and cands:
            rep["candidates"] = cands[:-1]
        else:
            fs = rep.setdefault("filter_stats", {})
            if isinstance(top_n, int) and len(cands) + 1 > top_n:
                # 池数改成 len(cands)+1 会 **> top_n** ⇒ min() 仍取 top_n ⇒ 咬不动
                # ⇒ 改成「截断候选清单一条」（池数不动 ⇒ 期望值不变 ⇒ 少给 = 自相矛盾）
                rep["candidates"] = cands[:-1] if cands else []
            else:
                fs["n_candidate_pool"] = len(cands) + 1

    def _break_pool_count_self_consistent(rep: dict) -> None:
        """**自洽篡改**池子条数 ⇒ C1 必须仍然 PASS（骗过去了），但 **C1b 必须 FAIL**。

        🔴 这是 C1 的真实攻击面：C1 只比 `len(candidates)` 与 `min(top_n, n_candidate_pool)`
           ⇒ 两个数**一起**改成自洽值就能骗过它（审计实测形态：top_n=100 / pool=10 / cands=10
           ⇒ C1 PASS，而真实池子是 21 只 ⇒ 假绿）。
        ⇒ 负例做法：**少给一条候选** + 把 `n_candidate_pool` 改成与它**自洽**
           ⇒ C1：`min(top_n, len(cands)) == len(cands)` ⇒ 仍 PASS（确实骗过去了）
           ⇒ C1b：`n_candidate_pool` 与**独立计数** `stages[extra_exit_ma].n_out` 对不上 ⇒ 必 FAIL。
        """
        cands = rep.get("candidates") or []
        if not cands:
            raise ValueError("报告里没有 candidates ⇒ 夹具不适用")
        cands = cands[:-1]
        rep["candidates"] = cands
        fs = rep.setdefault("filter_stats", {})
        fs["n_candidate_pool"] = len(cands)

    negative_cases = [
        ("N1", "删掉 meta.hash_seed", "M5",
         lambda r: r["meta"].pop("hash_seed", None)),
        ("N2", "stock_scores.unique_values 改成 1（分数退化成常数）", "S2",
         lambda r: r["stock_scores"].__setitem__("unique_values", 1)),
        ("N3", "rank_ic N=5 标成「样本不足」却仍留着数字", "R2",
         lambda r: r["rank_ic"]["horizons"]["5"].__setitem__("status", "insufficient")),
        ("N4", "rank_ic 的截面日期挪到 asof 之后（未来函数）", "R3",
         lambda r: r["rank_ic"]["horizons"]["5"]["per_date"][0].__setitem__("date", "2099-01-01")),
        ("N5", "过滤网 1 的淘汰计数改错", "F2",
         lambda r: r["filter_stats"]["per_net"]["filter_net_1_liquidity"].__setitem__("n_eliminated", 1)),
        # 🔴 2026-11-10 修：旧负例抹的是 ``holdings[0]``（本轮它 selected=True）
        #    ⇒ 抹了也进不了 H3 的判定集 ⇒ H3 仍 PASS ⇒ **负例无牙**。
        #    ⇒ 改成「未入选且有原因的行优先；没有就自造夹具」⇒ H3 必须转 FAIL。
        ("N6", "抹掉某只未入选持仓的淘汰原因（无则自造夹具）", "H3",
         _break_reject_reason),
        ("N7", "下一节点的交易日距离改成 999", "N3",
         lambda r: r["next_rebalance"].__setitem__("trading_days_until", 999)),
        ("N8", "删掉「策略容量」那条风险披露", "G1",
         lambda r: r.__setitem__("risk_disclosure",
                                 [x for x in r["risk_disclosure"] if x.get("id") != "strategy_capacity"])),
        ("N9", "把有 E 的行改成「无 E」但仍留着 e_rank", "E2",
         lambda r: next(x for x in r["etf_scores"]["rows"] if x.get("e_score") is not None)
         .__setitem__("e_score", None)),
        ("N10", "把无 E 的行 coverage 从 null 改成 0（把「算不出」冒充「算出来是 0」）", "E2",
         lambda r: next(x for x in r["etf_scores"]["rows"] if x.get("e_score") is None)
         .__setitem__("coverage", 0.0)),
        ("N11", "把某个必需风险主题的数字抹掉", "G2",
         lambda r: next(x for x in r["risk_disclosure"] if x.get("id") == "strategy_capacity")
         .__setitem__("detail", "策略容量有限，需人工判断。")),
        ("N12", "把穿透为 0 的行 n_constituents_scored 改成 0（冒充有成分被评分）", "E2",
         lambda r: next(x for x in r["etf_scores"]["rows"] if x.get("e_score") is None)
         .__setitem__("n_constituents_scored", 0)),
        # 🔴 M-F 新增：空集负例（E3/C3 的「真空 PASS」）
        ("N16", "把所有 e_rank 抹掉（排名集合变空）", "E3",
         lambda r: [x.__setitem__("e_rank", None) for x in r["etf_scores"]["rows"]]),
        ("N17", "把 candidates 整个清空（排名集合变空）", "C3",
         lambda r: r.__setitem__("candidates", [])),
        # 🔴 2026-11-05 00:0x 新增：回测真值必须与**产物独立重算**一致
        #    （否则报告里的年化/分段超额可以自己编，且没人拦）。
        ("N18", "把回测真值的组合年化改成 99.99%（数字漂移/编造）", "B5",
         lambda r: r["meta"]["backtest_summary"]["portfolio"].__setitem__(
             "annualized_return", 0.9999)),
        ("N19", "把分段 H1 的超额改成 0（抹掉符号相反的事实）", "B5",
         lambda r: r["meta"]["backtest_summary"]["segments"][0].__setitem__(
             "excess_annualized", 0.0)),
        ("N20", "把基准年化改成 15.00%（回归探针被绕过）", "B3",
         lambda r: r["meta"]["backtest_summary"]["benchmark_510300"].__setitem__(
             "annualized_return", 0.15)),
        # 🔴 2026-11-11（C1 口径改成 min(top_n, 池存活数) 后的配套反证）
        #    ⚠️ 反例必须**在两种形态下都咬人** ⇒ 用**条件变异**：
        #      ① 池 > top_n（截断形态）⇒ 少给一条就是「静默截断」⇒ 必须 FAIL；
        #      ② 池 ≤ top_n（抽干形态）⇒ 声称的池存活数与实际条数不符 ⇒ 必须 FAIL。
        ("N21", "把「池存活数 ↔ 候选清单」的关系改坏（截断形态少给一条 / 抽干形态谎报池数）", "C1",
         _break_pool_vs_candidates),
        ("N22", "删掉 filter_stats.n_candidate_pool（池存活数算不出来）", "C1",
         lambda r: r["filter_stats"].pop("n_candidate_pool", None)),
        ("N23", "静默截断候选清单（池没抽干却少给一只）", "C1",
         lambda r: r.__setitem__("candidates", (r.get("candidates") or [])[:-1])),
        # 🔴 2026-11-11 新增：审计实测的**自洽篡改**（C1 的真实盲区）
        #    ⇒ C1 必须**仍 PASS**（证明确实骗过了它）＋ C1b 必须 FAIL（独立计数把它抓住）
        ("N24", "**自洽篡改**池子条数（n_candidate_pool 与 len(candidates) 一起改）"
                "⇒ C1 骗得过去、C1b 必须 FAIL", "C1b",
         _break_pool_count_self_consistent, {"C1": True}),
    ]
    for case in negative_cases:
        case_id, desc, target_id, fn = case[:4]
        #: 可选的**伴随断言**（第 5 个元素）：`{断言 id: 期望判定}`
        #:   ⇒ 用来证明「这条负例真的是那种攻击面」（例：自洽篡改必须**骗过 C1**，
        #:     否则就证明不了 C1b 是必需的）。
        extra = case[4] if len(case) > 4 else {}
        # 🔴 2026-11-04 夜实测修（M-F；reviewer 遗留风险）：
        #    负例用 `next(x for x in rows if …)` 选行 ⇒ 若报告里**没有**匹配行，
        #    `next` 抛 StopIteration（PEP 479 ⇒ RuntimeError）⇒ **探针直接崩掉**，
        #    崩掉 ≠ 报 FAIL（CI 里会看成「探针坏了」而不是「这条断言有问题」）。
        #    ⇒ 修法：捕获异常 ⇒ 判 FAIL 并写清原因（不许崩、不许静默）。
        try:
            got = mutate(fn)
        except Exception as exc:  # noqa: BLE001 —— 负例构造失败必须报出来
            emit(case_id, f"负例「{desc}」⇒ 断言 {target_id} 必须 FAIL",
                 False, f"负例构造本身失败：{type(exc).__name__}: {exc}"
                         "（探针必须报出来，不许崩、不许静默）")
            continue
        ok = got.get(target_id) is False
        detail = f"{target_id} 的判定={got.get(target_id)}（期望 False；True ⇒ 断言写死了）"
        if extra:
            extra_bad = [f"{eid} 的判定={got.get(eid)}（期望 {exp}）"
                         for eid, exp in extra.items() if got.get(eid) is not exp]
            if extra_bad:
                ok = False
                detail += "｜伴随断言不成立：" + "；".join(extra_bad)
            else:
                detail += ("｜伴随断言成立："
                           + "；".join(f"{eid}={exp}" for eid, exp in extra.items()))
        emit(case_id, f"负例「{desc}」⇒ 断言 {target_id} 必须 FAIL", ok, detail)

    # ---- 正例（口径自检）：「池被抽干」是**合法**形态 ⇒ 不得误判红 ----
    # 🔴 2026-11-11：C1 改成 min(top_n, 池存活数) 后，必须证明它**不会**把
    #    「池被抽干（len(candidates) < top_n）」这种合法形态判红
    #    ⇒ 用**自造夹具**（成对对照：正例必须 PASS / 负例必须 FAIL）。
    def _c1_on(mutated: dict) -> tuple[bool, str]:
        return _c1_candidates_verdict(mutated)

    # 🔴 2026-11-11 修（审计：正例缺异常保护 ⇒ 报告缺字段时探针**崩**而不是报 FAIL）：
    #    ① 夹具要用到 `meta.params.top_n` / `filter_stats.n_candidate_pool` / `candidates`
    #       ⇒ 报告缺这些字段时直接下标会 KeyError ⇒ 探针崩掉（与本文件自订规矩不一致）；
    #    ② 夹具假定报告里有 ≥5 条候选 ⇒ 不足 5 条时「池被抽干」这个形态**构造不出来**。
    #    ⇒ 现在：try/except 包住 + 先断言 `len(candidates) >= 5`，否则 emit(..., False, "夹具不适用")。
    _cands_p1 = report.get("candidates") or []
    if len(_cands_p1) < 5:
        emit("P1", "正例「池被抽干（池存活 5 < top_n 100，候选给 5 条）」⇒ 断言 C1 必须 PASS",
             False, f"夹具不适用：报告里只有 {len(_cands_p1)} 条候选（<5）"
                     "⇒ 构造不出「池被抽干」形态")
    else:
        try:
            _drained = copy.deepcopy(report)
            _drained["meta"]["params"]["top_n"] = 100
            _drained["filter_stats"]["n_candidate_pool"] = 5
            _drained["candidates"] = _cands_p1[:5]
            _ok, _detail = _c1_on(_drained)
        except Exception as exc:  # noqa: BLE001 —— 夹具构造失败必须报出来，不许崩
            emit("P1", "正例「池被抽干（池存活 5 < top_n 100，候选给 5 条）」⇒ 断言 C1 必须 PASS",
                 False, f"夹具构造/判定失败：{type(exc).__name__}: {exc}"
                         "（探针必须报出来，不许崩、不许静默）")
        else:
            emit("P1", "正例「池被抽干（池存活 5 < top_n 100，候选给 5 条）」⇒ 断言 C1 必须 PASS",
                 _ok, f"C1 的判定={_ok}（期望 True；False ⇒ 口径把合法形态误判红了）｜{_detail}")

    # ---- 负例（源码类）：把 analyze.py 改坏 ⇒ 静态断言必须 FAIL ----
    source_negatives = [
        ("N13", "把名称列的 fillna(\"\") 改成 fillna(0)", "A4",
         lambda s: s.replace('fillna("")', "fillna(0)")),
        ("N14", "把源码里所有中文抹掉（docstring 失去中文）", "A3",
         lambda s: re.sub(r"[\u4e00-\u9fff]", "x", s)),
        ("N15", "把一处缩进换成 tab", "A1",
         lambda s: s.replace("    pool = apply_theme_cap(", "\tpool = apply_theme_cap(", 1)),
    ]
    for case_id, desc, target_id, fn in source_negatives:
        mutated = fn(source_text)
        try:
            got = {cid: ok for cid, _n, ok, _d in run_static_checks(mutated)}
        except SyntaxError as exc:
            got = {target_id: False}
            print(f"[WARN] {case_id} 变异后源码语法错误（也算拦下）：{exc}")
        ok = got.get(target_id) is False
        emit(case_id, f"负例「{desc}」⇒ 静态断言 {target_id} 必须 FAIL",
             ok, f"{target_id} 的判定={got.get(target_id)}（期望 False；True ⇒ 断言写死了）")

    total = len(lines) // 2
    print("=" * 78)
    print("ETF 分析报告 · 规格符合性探针（用户任务书 §5 的 10 段 + 硬性要求 + analyze.py 静态规范）")
    print(f"报告：{report_path}｜日历：qlib ∪ akshare（{len(calendar)} 个交易日）")
    print(f"账户事实源：{account.get('source')}｜"
          + (f"错误={account.get('error')}" if account.get("error")
             else f"持仓={account.get('codes')}｜总资产={account.get('total_assets')}"
                  f"｜现金={account.get('cash')}"))
    print("-" * 78)
    print("\n".join(lines))
    print("-" * 78)
    print(f"合计 {total} 项：PASS {total - len(failures)} / FAIL {len(failures)}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
