#!/usr/bin/env python
"""S6：合成最终调仓建议（qlib 规则层 + TradingAgents 结论 + 真实持仓/资金）。

输入：
  --report   qlib ETF 分析报告 JSON（etf_analysis_report.json）
  --decision TradingAgents 辩论结论 MD（可选；不存在则跳过该节）
  --prices   ETF 日线 CSV（date, code, close, ...）用于给"新买入"标的取价
  --universe ETF 基础信息 CSV（code, name, last_price, ...）用于兜底取价
  --account  账户快照 CSV（code, name, shares, price, market_value, ...）

输出：
  final_rebalance_plan.json / final_rebalance_plan.md —— 每只标的给出
  **动作 / 目标股数 / 金额 / 理由**，并写明调仓节点日期与倒计时。

硬性口径（manu/memory/rules.md §十四）：
  * 调仓节点 = 下一个自然月的第一个交易日（qlib 历史日历 ∪ akshare 完整日历）；
  * ETF 免印花税，佣金按 commission_bp、单边滑点 slippage_bp；
  * 最小交易单位 100 股（整手），不足整手不买；
  * 现金不够时按 final_score 降序削减买入，并在产物里显式记录被削减的部分；
  * TradingAgents 结论只是**第二意见**，与规则层冲突时以规则层 + 真实持仓为准。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

#: 把脚本所在目录放进 sys.path ⇒ 可 import 同目录的 `report_fingerprint`
#: （与 `verify_artifacts.py` 同一套写法）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from report_fingerprint import report_content_sha1_from_file  # noqa: E402
import config as C  # noqa: E402  （落点常量单一事实源）
import direction_vocab  # noqa: E402  （LLM 方向词表单一事实源：与语义守卫同表）
import llm_final as llm_final_mod  # noqa: E402
import overlap as overlap_mod  # noqa: E402  （候选池重合度筛查：唯一口径来源）

LOT = 100  # ETF 最小交易单位：100 股（一手）


def _ACCOUNT_SNAPSHOT_CSV_DEFAULT():
    """账户基准 CSV 的缺省路径（**账户模块自己的口径**，不写死服务器绝对路径）。

    🔴 2026-11-10：原先 CLI 缺省写死 `/tmp/pycharm_project_658d3fc4/manu/etf_rotation/
    data/account_snapshot.csv` ⇒ 换机器 / 换目录就找不到账户基准（静默退化成空仓）。
    ⇒ 改读 `account.ACCOUNT_SNAPSHOT_CSV`（与 `analyze.py` 同源、同口径）。
    模块缺失时退回「脚本同目录 `data/account_snapshot.csv`」。
    """
    try:
        import account  # noqa: PLC0415

        return Path(account.ACCOUNT_SNAPSHOT_CSV)
    except Exception:  # noqa: BLE001 —— 账户模块不可用时退回同目录约定路径
        return _SCRIPT_DIR / "data" / "account_snapshot.csv"


def _f(value, default=None):
    """安全转 float；None / NaN / 非法值一律回落到 default（不编数字）。"""
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def load_prices(path: str | None, asof: str) -> dict[str, tuple[float, str]]:
    """code -> (收盘价, 价日期)；取 asof 及之前的最后一根。"""
    out: dict[str, tuple[float, str]] = {}
    if not path or not Path(path).exists():
        return out
    df = pd.read_csv(path, dtype={"code": str})
    if "date" not in df.columns or "code" not in df.columns:
        return out
    close_col = "close" if "close" in df.columns else "price"
    if close_col not in df.columns:
        return out
    df["code"] = df["code"].str.zfill(6)
    df["date"] = df["date"].astype(str)
    df = df[df["date"] <= asof].sort_values("date")
    for code, grp in df.groupby("code"):
        row = grp.iloc[-1]
        price = _f(row[close_col])
        if price:
            out[code] = (price, str(row["date"]))
    return out


def load_universe(path: str | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path or not Path(path).exists():
        return out
    df = pd.read_csv(path, dtype={"code": str})
    if "code" not in df.columns:
        return out
    df["code"] = df["code"].str.zfill(6)
    for _, row in df.iterrows():
        out[str(row["code"])] = {
            "name": str(row.get("name", "")),
            "last_price": _f(row.get("last_price")),
            "last_price_date": str(row.get("last_price_date", "")),
        }
    return out


def _official_name(code: str, official: dict[str, str] | None, fallback: str = "") -> str:
    """🔴 **官方名优先**：名称一律以 `--universe`（ETF 基础信息 CSV）的 `name` 列为准。

    🔴 2026-11-11（真机缺陷 ② · 名称抽取垃圾）：
        旧口径 `r.get("name") or universe.get(code, {}).get("name", "")`
        ⇒ 只要 LLM / 否决原文里解析出了**非空**名字就**不回填**官方名
        ⇒ 实测 `159547` 的 name 被抽成垃圾词「重叠」
        （原文「…与 512890/159547 重叠 0.974/0.978…」⇒ 「重叠」不是它的名字）
        ⇒ 交付件里出现一只**没有名字的基金**。
    ⇒ 改成：**官方名存在 ⇒ 用官方名**（忽略来源里的名字，哪怕它非空）；
      官方名没有 ⇒ 才保留来源里的名字；都没有 ⇒ 空串。
    ⚠️ 官方名**只改名字**，不改任何理由 / 权重 / 动作。
    """
    if official:
        nm = str(official.get(str(code)) or "").strip()
        if nm:
            return nm
    return str(fallback or "").strip()


def _pool_whitelist(report: dict, universe: dict[str, dict]) -> tuple[set[str] | None, str]:
    """返回 **(池内代码集合 | None, 来源说明)**。

    🔴 事实源优先级（2026-11-11 真机缺陷 ①）：
      ① **`--universe` 传入的 ETF 基础信息 CSV**（`code` 列）——**这是「池子」本身的定义**
         ⇒ 首选它。实测服务器上 `etf_universe.csv` = 1664 个代码，
         含 516620 / 159547 / 512890 / 563020 / 159207，**不含 566620**。
         ⚠️ 该 CSV 里另有 `in_universe` 列（实测只有 250 行为 True）⇒ **不许**按它过滤
            （那会把 516620 等真池内标的误杀）⇒ 白名单 = **全部 code 行**。
      ② **回退**：`--universe` 为空 / 不可读 ⇒ 用**报告侧**出现过的代码集合
         （`plans` / `names` / `name_map` / `candidates` / `recommendation.actions` /
         `current_holdings_diagnosis.holdings` / `filter_stats.stages[].eliminated` /
         `filter_stats.selected` 的**并集**）⇒ 取舍理由：报告侧是「本轮**看见过**的池」，
         可能比真池**小**（Top-N 截断）⇒ 故**只在 `--universe` 不可用时**才用它，
         且宁可**多收**（并集）也不漏收 ⇒ 避免误杀真否决。
      ③ 都拿不到 ⇒ 返回 **None** ⇒ **fail-closed**：**不剔除任何否决**
         （绝不能因为白名单缺失而放过真否决），并在产物里如实披露「未能校验」。
    """
    if universe:
        codes = {str(c) for c in universe if str(c).strip()}
        if codes:
            return codes, f"--universe（ETF 基础信息 CSV，{len(codes)} 个代码）"
    codes: set[str] = set()

    def _add(raw) -> None:
        text = str(raw or "").strip()
        if text:
            codes.add(text.zfill(6))

    def _walk(node) -> None:
        if isinstance(node, dict):
            if node.get("code"):
                _add(node["code"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)
        elif isinstance(node, str):
            _add(node)

    for key in ("plans", "names", "name_map"):
        _walk(report.get(key))
    _walk(report.get("candidates"))
    _walk((report.get("recommendation") or {}).get("actions"))
    _walk((report.get("current_holdings_diagnosis") or {}).get("holdings"))
    _walk((report.get("filter_stats") or {}).get("selected"))
    for stage in ((report.get("filter_stats") or {}).get("stages") or []):
        _walk((stage or {}).get("eliminated"))
    if codes:
        return codes, f"报告侧代码集合（--universe 不可用，{len(codes)} 个代码）"
    return None, "池内白名单不可用（--universe 缺失且报告侧取不到代码）"


def _split_rejections_by_pool(
    rejections: list[dict] | None,
    pool_codes: set[str] | None,
) -> tuple[list[dict], list[dict]]:
    """按**池内白名单**切分 TA 否决名单 ⇒ **(池内否决, 池外披露)**。

    🔴 2026-11-11（真机缺陷 ①）：LLM 把 `516620` 笔误写成 `566620`
    （`566620` **不在** ETF 池内）⇒ 旧口径**照单全收** ⇒ 交付件的否决名单里
    出现一只**根本不存在的标的** ⇒ 污染顺延逻辑与产物级闸门。
    ⇒ **池外代码不得进入 `ta_screening.rejected`**（它不是候选池的标的，
      否决它没有意义）⇒ 但**不许静默丢弃**：进 `rejected_out_of_universe` 如实披露。

    🔴 **fail-closed**：`pool_codes` 为空 / 不可用 ⇒ **一个都不剔除**
      （绝不能因为白名单缺失而放过真否决）。
    """
    kept: list[dict] = []
    out_of_pool: list[dict] = []
    for raw in (rejections or []):
        row = dict(raw)
        code_raw = str(row.get("code") or "").strip()
        code = code_raw.zfill(6) if code_raw else ""
        if pool_codes and code and code not in pool_codes:
            out_of_pool.append({
                "code": code,
                "name": str(row.get("name") or ""),
                "reason": str(row.get("reason") or ""),
                "source_section": str(row.get("source_section") or ""),
                "note": "池外代码，已剔除（不在 ETF 池内 ⇒ 不参与否决 / 顺延）",
            })
            continue
        kept.append(row)
    return kept, out_of_pool


def load_account(path: str | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path or not Path(path).exists():
        return out
    df = pd.read_csv(path, dtype={"code": str})
    if "code" not in df.columns:
        return out
    df["code"] = df["code"].str.zfill(6)
    for _, row in df.iterrows():
        out[str(row["code"])] = {
            "name": str(row.get("name", "")),
            "shares": int(_f(row.get("shares"), 0) or 0),
            "price": _f(row.get("price")),
            "market_value": _f(row.get("market_value")),
            "pnl_pct": _f(row.get("pnl_pct")),
            "pnl": _f(row.get("pnl")),
        }
    return out


def _round_lot(shares: float) -> int:
    """向下取整到整手。"""
    if shares is None or shares <= 0:
        return 0
    return int(math.floor(shares / LOT) * LOT)


def build_account_weights(diag: dict, account: dict[str, dict],
                          total_assets: float | None, cash: float | None
                          ) -> tuple[dict[str, float] | None, list[str]]:
    """账户**真值**当前权重（百分比）。**取不到真值 ⇒ 返回 ``None``（未知），绝不返回空 dict。**

    🔴 2026-11-05 01:4x（M-K 收尾；reviewer 独立复现的 **BLOCKING**）：
       「**没有账户真值**」与「**未持有**」必须**分开表达**。
       旧实现用**空 dict**同时表达两者 ⇒ 账户快照读不到时（``load_account`` 静默返回 ``{}``），
       所有标的的当前权重被写成 **0%（伪造）** ⇒ 语义守卫「减 ⇒ 目标 < 当前」必然不成立
       ⇒ **又一次假阳性拦下交付**（本轮要消灭的正是这类）。
       ⇒ 现在：真值**不可得** ⇒ ``None``（未知，守卫跳过该行）；真值**可得** ⇒
       **未持有**的代码才落 0%。

    真值来源：报告 ``current_holdings_diagnosis.holdings[].market_value / current_weight``
      （与计划件 ``actions[].current_shares / current_market_value`` **同源** ⇒ 只有一套口径）；
      并与账户快照 CSV **交叉核对**：不一致 ⇒ 记 warning 且**不猜**（返回 ``None``）。
    """
    warnings: list[str] = []
    if not total_assets or float(total_assets) <= 0:
        return None, ["🔴 报告缺总资产 ⇒ 算不出账户真值当前权重 ⇒ 本计划**不伪造**当前权重"
                      "（语义守卫对「未知」跳过，不判 FAIL）"]
    holdings = (diag.get("holdings") or []) if isinstance(diag, dict) else []
    available = bool(diag.get("available")) if isinstance(diag, dict) else False
    if not holdings and not available:
        return None, ["🔴 报告的持仓诊断不可用 ⇒ 算不出账户真值当前权重 ⇒ 本计划**不伪造**当前权重"]
    out: dict[str, float] = {}
    for h in holdings:
        code = str(h.get("code", "")).zfill(6)
        pct = None
        mv = _f(h.get("market_value"))
        if mv is not None:
            pct = float(mv) / float(total_assets) * 100.0
        else:
            w = _f(h.get("current_weight"))
            if w is not None:
                pct = float(w) * 100.0
        if pct is None:
            return None, [f"🔴 持仓 {code} 算不出当前权重 ⇒ 本计划**不伪造**当前权重"]
        out[code] = pct
    if cash is not None:
        out["现金"] = float(cash) / float(total_assets) * 100.0
    # ---- 与账户快照 CSV 交叉核对：两套口径分叉 ⇒ 记 warning 且**不猜** ----
    for code, row in (account or {}).items():
        mv = _f(row.get("market_value"))
        if mv is None:
            continue
        csv_pct = float(mv) / float(total_assets) * 100.0
        rep_pct = out.get(str(code))
        if rep_pct is not None and abs(rep_pct - csv_pct) > 0.01:
            warnings.append(
                f"🔴 账户口径分叉：{code} 报告持仓权重 {rep_pct:.4f}% vs 账户快照 CSV "
                f"{csv_pct:.4f}%（差 {abs(rep_pct - csv_pct):.4f} pp）⇒ 已弃用当前权重真值（**不猜**）")
            return None, warnings
    return out, warnings


def _priority_map(report: dict) -> dict[str, float]:
    """code -> final_score（来自 candidates），用于现金不足时排序。"""
    out: dict[str, float] = {}
    for row in report.get("candidates", []) or []:
        code = str(row.get("code", "")).zfill(6)
        score = _f(row.get("final_score"))
        if score is not None:
            out[code] = score
    for row in report.get("etf_scores", {}).get("rows", []) or []:
        code = str(row.get("code", "")).zfill(6)
        score = _f(row.get("e_score"))
        if score is not None and code not in out:
            out[code] = score
    return out


def _decision_excerpt(path: str | None, limit: int = 1800) -> str:
    """截取 TradingAgents 决策正文。

    🔴 **失败时不倒 traceback**（2026-11-04 夜实测）：
    TA 因 429「Token Plan 用量上限」失败时，决策文件里是整段 Python 异常栈（5.8 KB）
    ⇒ 旧代码直接把它当「第二意见」塞进交付件，等于把内部异常栈展示给用户。
    现在识别「未完成」标记，只保留**状态 + 一句话原因 + 时间**。
    """
    if not path or not Path(path).exists():
        return ""
    text = Path(path).read_text(encoding="utf-8")
    if "**未完成**" in text:
        reason = ""
        when = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- 异常：") and not reason:
                reason = stripped.split("：", 1)[1].strip().strip("`")
            elif stripped.startswith("- 失败时间：") and not when:
                when = stripped.split("：", 1)[1].strip()
        return (
            f"_TradingAgents 本轮 **未完成**（{when or '时间未记录'}）"
            "⇒ 本计划**不含任何 LLM 第二意见**，全部结论来自 qlib 规则层。_\n\n"
            f"- 失败原因：{reason[:240] if reason else '（未记录）'}"
        )
    marker = "## 1. 最终交易决策"
    if marker in text:
        tail = text.split(marker, 1)[1]
        if "## 2." in tail:
            tail = tail.split("## 2.", 1)[0]
        text = marker + tail
    return text.strip()[:limit]


def _decision_view(decision_path: str | None, head_limit: int = 1200,
                   table_limit: int = 4000) -> str:
    """交付件里的「第二意见」展示件：**与冲突表同源**（读 `.json`）。

    🔴 2026-11-04 21:5x reviewer 复现：旧实现用 `_decision_excerpt`（读**展示件 md**，
    而 md 被 `_tail(2500)` 截断）⇒ 交付件里**根本看不到产生冲突的那几张表**
    （实测 `"|" in view == False`，长度 1800）⇒ 用户**无法复核冲突表的任何数字**；
    而冲突表本身读的是 `.json`（结构完整，28,490 字）⇒ **展示件与冲突表不同源**。
    ⇒ 修法：展示件改读 `.json`（与冲突表同源），并显式附上「冲突表的数据来源表」，
      截断时打上「（已截断）」标记，不让读者误以为看到了原文全貌。
    """
    if not decision_path or not Path(decision_path).exists():
        return ""
    fail_reason = _decision_failure_reason(decision_path)
    if fail_reason:
        return _decision_excerpt(decision_path)   # 失败占位：沿用「未完成」渲染

    # ① 头部：决策纪要抬头 + 元信息（标的/日期/模型/耗时）+ **最终评级**
    #    （从展示件 md 取，因为它有这层结构；冲突表用不到评级，读者却最关心它）
    try:
        md_text = Path(decision_path).read_text(encoding="utf-8")
    except Exception:
        md_text = ""
    head_block = ""
    if md_text.strip():
        head_block = md_text
        for _marker in ("## 二、", "## 1.", "## 2."):
            _idx = head_block.find(_marker)
            if _idx > 0:
                head_block = head_block[:_idx]
                break
        head_block = head_block.strip()

    # ② 正文：研究经理结论（读 `.json`，与冲突表**同源**）
    text = _decision_text(decision_path)
    body = text.strip()
    if not body:
        body = _decision_excerpt(decision_path)
    elif len(body) > head_limit:
        cut = body[:head_limit]
        # 截到最近的段落 / 句子边界，避免出现「半句话」
        for _sep in ("\n\n", "。", "\n"):
            _pos = cut.rfind(_sep)
            if _pos > head_limit // 2:
                cut = cut[:_pos + len(_sep)]
                break
        body = cut.rstrip() + "\n\n_（正文已截断；完整内容见 tradingagents_decision.md / .json）_"

    # ③ 表格：冲突表的数据来源（只存在于 `.json` 里，展示件 md 的表被截掉了）
    table_lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    table_text = "\n".join(table_lines)
    truncated = len(table_text) > table_limit
    if truncated:
        table_text = table_text[:table_limit]

    parts = [x for x in (head_block, body) if x.strip()]
    if table_text.strip():
        parts.append(
            "**冲突表的数据来源（LLM 原始表格，与上方冲突表逐行对应）**\n\n" + table_text
            + ("\n\n_（表格已按上限截断，未列出的行不在冲突表里）_" if truncated else "")
        )
    return "\n\n".join(parts)


#: LLM 表格单元格识别（**列序无关**）：代码 / 百分比 / 方向
#: 🔴 2026-11-05 01:0x（M-K）修：**允许反引号**。
#:    实测新决策里研究经理的「Strategic Actions」表代码格写成 `` `512800` ``
#:    ⇒ 旧正则不匹配 ⇒ 该行**既不认成数据行、又被 `_header_roles` 认成表头**（因理由栏
#:    含「目标/理由」二字）⇒ **整张研究经理表被静默丢弃**（9 行只剩「现金」1 行）。
_LLM_CODE_CELL = re.compile(r"^\*{0,2}`?(\d{6}|现金)`?\*{0,2}$")
_LLM_PCT_CELL = re.compile(r"^\*{0,2}(\d+(?:\.\d+)?)\s*%\*{0,2}$")
#: 宽松百分比：**带负号**。
#: 🔴 2026-11-04 21:5x reviewer 复现：旧式 `(\d+...)` 丢掉负号 ⇒ 理由栏里的
#:    「浮亏-26.96%」被读成**正的** 26.96% ⇒ 「LLM 说清仓」被报成「LLM 要 26.96%」。
_LLM_PCT_LOOSE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
#: **纯百分比格**：整格几乎就是一个百分比（可带「约 / 左右」等前后缀）。
#: 🔴 「方向列锚定」**必须**用它：否则锚定会落到理由栏的长句上，
#:    取出长句里的第一个百分比当目标权重（= F6 要消灭的假冲突，逐字复现）。
_LLM_PCT_PURE = re.compile(
    r"^\*{0,2}(?:约|大约|大概|接近)?\s*(-?\d+(?:\.\d+)?)\s*%\*{0,2}(?:\s*(?:左右|上下))?$"
)
#: 「目标 X%」兜底：抓**目标权重**用的百分数（两个捕获组：窗口 / 数字）。
#: 🔴 2026-11-05 03:0x（M-L 修）**真 bug**：旧式 `目标[^0-9]{0,6}(-?\d+...)`
#:    里的填充字符类 `[^0-9]` **把负号一起吃掉** ⇒ 理由栏的
#:    「未进入目标组合，当前-18.91%」被读成**正的 18.91%**
#:    ⇒ 「LLM 说清仓」被报成「LLM 要 18.91%」⇒ 方向-权重语义自洽守卫必然 FAIL
#:    （2026-11-05 02:39 实测：流水线第 8 步 FATAL，而**产物本身没问题**）。
#:    ⇒ 修法 ①：填充字符类改成 `[^0-9-]` ⇒ **负号不许被吃掉**
#:      ⇒ 负值原样带出 ⇒ `_pct_ok`（只接受 [0, 100]）把它拒掉。
#: 🔴 2026-11-05 04:0x（M-N 修，reviewer A2/A3/B1/B2）：**统一取数**用的语境守卫。
#:    旧实现（`_LLM_PNL_WINDOW_WORDS`）是「『目标』与数字之间的窗口里有盈亏词 ⇒ 整条拒掉」，
#:    实测有**两个**致命副作用（reviewer A2/A3 复现）：
#:      ① **误伤合法目标** ⇒ 真实分歧被**静默漏报**（比误报更难发现）：
#:         「目标从当前 10% 升至 15%」⇒ 旧=10.0 / 新=None（应 15.0）；
#:         「目标收益 20%」⇒ 旧=20.0 / 新=None（应 20.0）；
#:         「目标较当前提升至 15%」⇒ 旧=None / 新=None（应 15.0）。
#:      ② **取错值**：旧实现的「取最后一个数字」只对「目标」后**第一个**百分数生效
#:         ⇒「目标从 20% 降到 10%」取到 20（应 10）、「目标由 20% 下调至 12%」取到 20（应 12）。
#:    ⇒ 改成**统一取数函数** `_extract_target_pct`（见下）：扫全格百分数 → 逐个过**语境守卫**
#:      → 「从 A% 到 B%」取**落点 B** → 否则取**第一个**存活值。
#:
#:    「非目标语境词」分**后向**与**前向**两张表，唯一差别是「收益 / 回报」：
#:      · **后向表不含**「收益 / 回报」—— 「目标收益 20%」是**合法目标**（reviewer A2 实测）；
#:        若后向也拦 ⇒ 该行判 None ⇒ 真实分歧静默漏报；
#:      · **前向表含**「收益 / 回报」—— 「20%收益」明确是收益口径。
#:    数字**贴着**这些词 ⇒ 它是损益/价格/动量，不是目标权重。
_LLM_NON_TARGET_BACKWARD = (
    "浮盈", "浮亏", "亏损", "盈利", "盈亏", "涨幅", "跌幅", "涨跌", "净值",
    "成本", "价格", "收益率", "当前", "现价", "现值", "动量", "胜率", "概率",
)
_LLM_NON_TARGET_FORWARD = (
    "浮盈", "浮亏", "亏损", "盈利", "盈亏", "涨幅", "跌幅", "涨跌", "净值",
    "成本", "价格", "收益率", "回报", "收益", "动量", "胜率", "概率",
)
#: 动词/连接词：**隔开**了非目标词与数字 ⇒ 该词不「贴着」数字 ⇒ 不算语境污染。
#:    实测依据：「目标较当前提升至 15%」里的「当前」被「提升至」隔开 ⇒ 不是当前权重 ⇒ 15 合法。
#: A16 不变量用的**绝对**盈亏/价格/当前类词。
#: 🔴 与 `_LLM_NON_TARGET_BACKWARD` 的差别：**不含**「动量 / 胜率 / 概率」——
#:    那三个是「别的比值」，与目标权重数值**偶然相等**并不罕见
#:    （例：「目标 20%，120日动量+20%」）⇒ 拿它们当不变量会造成**假阳性拦下交付**。
#:    本表是 2026-11-05 M-L 真 bug（原因由「当前-18.91%」被读成目标）的同族词。
_LLM_PNL_ABS_WORDS = (
    "浮盈", "浮亏", "亏损", "盈利", "盈亏", "涨幅", "跌幅", "涨跌", "净值",
    "成本", "价格", "收益率", "回报", "收益", "当前", "现价", "现值",
)
_LLM_VERB_CHARS = "到至升降调改变增减提为"
#: 「目标类锚点」：出现它 ⇒ 后面的百分数**才可能是目标权重**（用于兜底路径限定搜索范围）。
_LLM_TARGET_ANCHORS = ("目标", "建议", "权重", "仓位", "比例", "配置", "调仓", "方向")
#: 兜底路径用的**严格**锚点（必须明确表示「这是目标」）。
#:    「配置 / 调仓 / 方向」**不算**——理由栏里「不值得配置20%」这种句子太常见，
#:    拿它当锚点会把「**被否掉的** 20%」当成目标（2026-11-05 04:4x 实测）。
_LLM_TARGET_ANCHOR_STRICT = ("目标", "建议", "权重", "仓位", "比例")
_LLM_TARGET_ANCHOR_RE = re.compile("|".join(_LLM_TARGET_ANCHOR_STRICT))
#: 「从/由 A% … 到/至/升/降/调至 B%」⇒ 取**落点 B**（reviewer A3 实测口径）。
_LLM_FROM_TO = re.compile(
    r"(?:从|由|自)\s*[^%]{0,16}?(-?\d+(?:\.\d+)?)\s*%"
    r"[^%]{0,24}?(?:到|至|升|降|调|改|变|增|减|提)[^%]{0,8}?(-?\d+(?:\.\d+)?)\s*%"
)
#: 「当前权重→建议权重」型单元格：**箭头后面**才是目标权重。
#: 🔴 2026-11-05 04:1x（M-N 实测事故，流水线第 8 步 FATAL）：03:5x 那一版 TA 决策的
#:    **多头表**表头是「代码 | 方向 | **当前权重→建议权重** | 核心理由」
#:    ⇒ 单元格是「22.54%→20%」⇒ 旧取数「取第一个百分数」⇒ 取到 **22.54%（当前权重）**
#:    ⇒ 方向「卖」配目标 11.17% ⇒ 语义守卫 **18 条异常** ⇒ 流水线 FATAL。
#: ⇒ 修法：**箭头后面**的百分数才是目标；箭头前面的是「当前」。
_LLM_ARROW_SPLIT = re.compile(r"(?:[\u2192\u2794\u21d2]|->)")
#: 「盈亏/价格/收益」类**列名**（表头）⇒ 该列**不许**当目标列，也**不许**当锚定目标。
_LLM_PNL_COLUMN_WORDS = (
    "涨跌幅", "涨跌幅%", "涨跌", "盈亏", "盈亏率", "损益", "获利", "亏损率",
    "收益率", "浮盈", "浮亏", "净值", "成本", "价格", "现价", "回报", "动量",
    "得分", "最终分", "胜率", "概率",
)
_LLM_DIR_CELL = re.compile(
    r"(不买|不卖|清仓|买入|卖出|增持|减持|加仓|减仓|持有|保留|维持|"
    r"买|卖|增|减|加|持|清|留|"
    r"Buy|Sell|Hold|Overweight|Underweight|Neutral)",
    re.IGNORECASE,
)
#: 🔴 2026-11-09（独立审计实证）：**否定式买入**的长前缀。
#:    实测事故：LLM 最终表写「**不作为防守仓买入**」⇒ 旧方向抽取拿到「买入」
#:    ⇒ 目标权重 0% 与方向「买入」矛盾 ⇒ 交付件自带的「方向-权重语义自洽」守卫
#:    必然 FAIL（买 ⇒ 目标必须 >0%）。
#:    ⇒ 修法：方向词**前面**出现这些否定前缀 ⇒ 该方向按「**不买**」处理。
#:    ⚠️ 只认**长前缀**，不改成「往前 N 字里有『不』就算否定」——
#:    否则「虽然基本面不好但建议买入」会被误判成「不买」（实测反例）。
_NEG_BUY_PREFIX = ("不作为", "不作", "不予以", "不当", "不打算", "不计划")
#: 方向 ∈ 下列集合 ⇒ 目标权重**必然是 0%**（与 `llm_final.py` 的 `_ACTION_RULES` 同口径：
#:    清仓类 ⇒ 卖 + 0%｜裸「卖出」⇒ 卖 + 0%｜不买/不建仓 ⇒ 0%）。
#: 🔴 2026-11-11（critic 审计 · 高危）：本词表与守卫 `verify_artifacts.LLM_DIRECTION_ZERO`
#:    **原本是两份分叉的副本** ⇒ 解析器吐得出的「清」「Sell」不在解析器 ZERO 档里
#:    （⇒ 同类「清掉+8%」只被守卫拦死、不被强制归零），而「卖掉/清零/不建仓」是死条目。
#:    ⇒ 现在两边都从 `direction_vocab` 取（**单一事实源**），分叉从根上消失。
_ZERO_DIR_WORDS = direction_vocab.ZERO
#: markdown 噪声（强调/代码标记）—— 与 llm_final.py `_MD_NOISE` **同口径**
_MD_NOISE = r"[\s*`_~]*"
#: 「卖出约 2,600 股」= **部分卖出**（减），**不是**清仓 ⇒ 与 llm_final.py `_ACTION_RULES` ② 同口径
#: 🔴 2026-11-11（critic 审计 · 中危）：旧正则 `卖出\s*(?:约)?\s*\d+` **吃不掉 markdown 噪声**
#:    ⇒ 实测「卖出**约2,600份**」漏判成**清仓** ⇒ ZERO 守卫把它**强制归零**且语义自洽 ⇒
#:    **静默降级**（部分卖出被写成整仓清零，守卫还放行）。
#:    ⇒ 修法：动词与数量之间允许夹 **强调/代码标记**（`_MD_NOISE`，与 llm_final 同口径）。
_PARTIAL_SELL_RE = re.compile(
    r"(?:卖出|卖掉)" + _MD_NOISE + r"(?:约)?" + _MD_NOISE
    + r"[\d,]+(?:\.\d+)?" + _MD_NOISE + r"(?:股|份)"
)
#: 方向词里属「买入」语义的那些（用于否定式判定）
_POS_DIR_WORDS = ("买", "买入", "加仓", "增持", "加", "增")
#: 🔴 2026-11-10（服务器实跑 20261110_120420 实证）：**后置否定** ——
#:    否定词紧跟在买入类词**后面**（「买：否」「买(否)」「买，不」…），而不是前面。
#:    实测事故：LLM 空头表的方向格写「**买：否**」（同行目标权重 **0%**）
#:    ⇒ 旧实现只认**前缀**否定 ⇒ 方向被判成「买」（POS）
#:    ⇒ 与目标 0% 自相矛盾 ⇒ 交付件自带守卫「LLM 方向-权重语义自洽」报 4 条异常
#:       （159207 / 512170 / 515450 / 515650，四条全是空头视图的 direction='买'）。
#:    ⇒ 修法：买入类词**后面**紧跟（只隔标点/空白）否定标记 ⇒ 按「**不买**」处理
#:       （「不买」属 ZERO 类 ⇒ 目标必须 0%，与原文一致）。
#:    ⚠️ 判定必须**精确**：否定标记之后**不许**再出现实义内容
#:    （否则「买入，不做波段」这种**真买入**会被误判成「不买」）。
#:    ⚠️ 括号**可以**当分隔符（「买(否)」是本轮实测写法之一），
#:       真正把「买入（不追高）」挡在门外的是**否定标记之后的实义残留检查**。
_NEG_BUY_SUFFIX_RE = re.compile(
    r"(?:买入|增持|加仓|买|加|增)"
    r"[\s:：、,，;；/／|\-–—~～*()（）=≠]*"
    r"(?:否|不|没|无)"
)
#: 否定标记之后允许出现的**收尾**字符（空白 / 标点 / markdown 强调）
_CLOSING_ONLY_RE = re.compile(r"^[\s*~，。、；;：:（）()\[\]【】<>《》/\\|\-–—]*$")
#: 否定标记之后的**括号备注**（「买：否（估值偏高）」）—— 备注里不许再出现方向词
_BRACKET_TAIL_RE = re.compile(
    r"^[（(【\[][^（()）【】\[\]]*[）)】\]]?[\s*~]*$"
)


def _suffix_is_closing(rest: str) -> bool:
    """否定标记**之后**的残余文字是否只是「收尾」（标点 / 强调 / 括号备注）。

    🔴 精确性闸门：「买入，不做波段」这种**真买入**句 ⇒ 残余里还有实义内容
       ⇒ **不算**否定 ⇒ 方向仍是「买」（不许被后置否定规则误伤）。
    """
    text = (rest or "").strip()
    if not text:
        return True
    if _CLOSING_ONLY_RE.match(text):
        return True
    _m = _BRACKET_TAIL_RE.match(text)
    if _m and not _LLM_DIR_CELL.search(text):
        return True
    return False


def _negated_buy_suffix(cell: str) -> bool:
    """方向格里的**后置否定**（「买：否」「买:否」「买(否)」「买/否」「买，不」…）。

    🔴 2026-11-10（服务器实跑实证）：LLM 把空头表的方向格写成「**买：否**」
       ⇒ 旧口径只认前缀否定 ⇒ 方向 = 「买」⇒ 与目标 0% 自相矛盾 ⇒ 守卫 FAIL。
    """
    for _m in _NEG_BUY_SUFFIX_RE.finditer(cell or ""):
        if _suffix_is_closing(cell[_m.end():]):
            return True
    return False


def _classify_dir_cell(cell: str) -> str:
    """从表格单元格抽方向词（**否定式优先 → 长词优先**）。

    🔴 2026-11-09（独立审计实证）修的两个真缺陷：
      ① **否定式买入**：旧实现用 `search` 取**最左**匹配 ⇒ 「条件式小买，否则不买」
         取到「买」（POS）⇒ 目标 0% 与方向矛盾 ⇒ 守卫 FAIL；
         「不作为防守仓买入」取到「买入」⇒ 同样矛盾。
         ⇒ 现在：**长否定前缀**在前 ⇒ 按「不买」；**长词优先**（不买 > 买、卖出 > 卖）。
      ② **部分卖出**：「卖出约 2,600 股」是**减仓**不是清仓 ⇒ 不能按 ZERO 处理。

    🔴 2026-11-10（服务器实跑 20261110_120420 实证）补的第三个真缺陷：
      ③ **后置否定**：「买：否」这类写法（否定词在买入词**后面**）旧口径**不认**
         ⇒ 方向判成「买」⇒ 与同行目标权重 0% 自相矛盾 ⇒ 守卫「方向-权重语义自洽」FAIL。
         ⇒ 现在：**前缀否定**与**后置否定**都归「不买」；**部分卖出**仍是最高优先级。
    """
    hits = list(_LLM_DIR_CELL.finditer(cell))
    if not hits:
        return ""
    if _PARTIAL_SELL_RE.search(cell):
        return "减"
    for _m in hits:
        if _m.group(1) in _POS_DIR_WORDS and any(
                _p in cell[:_m.start()] for _p in _NEG_BUY_PREFIX):
            return "不买"
    # 🔴 后置否定：「买：否」⇒ 方向按「不买」处理（ZERO 类 ⇒ 目标必须 0%）
    if _negated_buy_suffix(cell):
        return "不买"
    # 长词优先；同长取最靠左
    _best = max(hits, key=lambda _m: (len(_m.group(1)), -_m.start()))
    return _best.group(1)


#: 标题里的序号前缀（「一、」「1.」「第二章」）—— 剥掉它才能判断标题**属于哪个说话人**
_LLM_HEADING_NUM = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|\d+(?:\.\d+)*[.、)]?\s*|第[一二三四五六七八九十百]+[章节部分][、.．]?\s*)"
)
#: 说话人 → 交付件里显示的中文名。🔴 2026-11-04 夜实测修（M-F）：
#:    旧实现只映射 research/bull/bear 三档 ⇒ **M-C 新登记的 `final`（Trader 最终裁决）**
#:    与风险三方（aggressive/conservative/neutral）在 md 里一律显示成「LLM」
#:    ⇒ 用户读到的是「谁说的 = LLM」，等于没归属。
#:    ⇒ 映射表与 `_LLM_SECTIONS` **同源**（新增说话人时不会再漏一次）。
_LLM_WHO_LABEL = {
    "research": "研究经理",
    "bull": "多头",
    "bear": "空头",
    "final": "Trader（最终裁决）",
    "aggressive": "激进派",
    "conservative": "保守派",
    "neutral": "中立派",
}

#: 章节 → 说话人（研究经理 > 多头 > 空头 > 风险三方）
#: 说话人关键词之后允许出现的**分隔符**。
#: 🔴 2026-11-05 01:0x（M-K）修：关键词后紧跟分隔符/结束 ⇒ 判定为**说话人标题**；
#:    关键词后直接跟汉字 ⇒ 是**正文里的小标题**（如「## 多头所谓的"三层验证"」、
#:    「## 二、多头所承认的…」）⇒ **不算**说话人标题。
#:    否则空头段里一个小标题就会把后续**所有**表格（含 Trader 最终裁决）的
#:    「谁说的」污染成多头 —— 实测旧实现正是如此。
_SPEAKER_HEADING_DELIMS = "（(【[「:：、,，.。;；-—–/· \t　"

#: 🔴 2026-11-05 01:0x（M-K）：加别名「研究经理结论」——
#:    展示件 md 的标题是「## 二、研究经理结论（Research Manager）」，
#:    严格规则下「研究经理」后面直接跟汉字「结论」会被拒 ⇒ 用别名整体匹配。
_LLM_SECTIONS = (
    ("research", ("strategic actions", "研究经理结论", "研究经理", "research manager")),
    ("bull", ("多头", "bull")),
    ("bear", ("空头", "bear")),
    ("aggressive", ("激进派", "aggressive")),
    ("conservative", ("保守派", "conservative")),
    ("neutral", ("中立派", "neutral")),
    # 🔴 2026-11-04 22:5x（M-C）补：「**最终交易决定**」是 Trader 的**最终裁决**，
    #    原实现没登记它 ⇒ 该章节的行会**沿用上一个说话人**（实测 `who='bear'`）
    #    ⇒ 交付件把「最终裁决」写成「空头说的」⇒ 归属错误。
    #    ⇒ 显式登记；该章节**没有表格**时对结果零影响（⇒ 正式产物逐字节不变）。
    ("final", ("最终交易决定", "最终交易决策", "final trade decision", "final_trade_decision")),
)

#: 说话人 → 主视图优先级（**数值小 = 权威高**）。
#: 🔴 2026-11-04 夜实测修（M-F3；reviewer 指出原 `order` 字典与 `_LLM_SECTIONS` **不同源**）：
#:    旧实现 `order = {"final":0,"research":1,"bull":2,"bear":3}` + `order.get(who, 4)`
#:    ⇒ ① 风险三方（aggressive/conservative/neutral）全部落进 **4** 档，
#:       与「**未知说话人**」同档 ⇒ 三方之间**相对次序随机**（sorted 稳定 ⇒ 取决于字典序）；
#:    ② 新增说话人时只改 `_LLM_SECTIONS` 却忘了改 `order` ⇒ 该说话人**静默降级**到最低档。
#:    ⇒ 抽成**单一事实源** `_LLM_WHO_ORDER`，并用 `_assert_llm_who_tables()` 运行时硬校验。
#:    ⚠️ 官方产物只含 bull/bear（实测：视图 who 分布 bull×10 + bear×10）⇒ 优先级不变、产物零变化。
_LLM_WHO_ORDER = (
    "final",        # Trader 最终裁决（辩论后的**最终裁决**，权威最高）
    "research",     # 研究经理
    "bull",         # 多头
    "bear",         # 空头
    "aggressive",   # 风险三方：激进派
    "conservative",  # 风险三方：保守派
    "neutral",      # 风险三方：中立派
)
#: 说话人 → 优先级序号（未知说话人不在表内 ⇒ 排最后）
_LLM_WHO_RANK = {who: idx for idx, who in enumerate(_LLM_WHO_ORDER)}


def _assert_llm_who_tables() -> None:
    """说话人三张表（优先级 / 中文名 / 章节登记）必须**同源**⇒ 不一致就抛异常。

    🔴 2026-11-04 夜实测修（M-F3）：三张表各写一份 ⇒ 新增说话人时极易只改一处。
    ⇒ 这里做**硬校验**（宁可不产出，不产出「说话人归属错乱」的交付件）。
    """
    order_set = set(_LLM_WHO_ORDER)
    label_set = set(_LLM_WHO_LABEL)
    section_set = {who for who, _ in _LLM_SECTIONS}
    if order_set != label_set or order_set != section_set:
        raise RuntimeError(
            "LLM 说话人三张表不同源："
            f"_LLM_WHO_ORDER={sorted(order_set)}｜_LLM_WHO_LABEL={sorted(label_set)}｜"
            f"_LLM_SECTIONS={sorted(section_set)}"
            "⇒ 请三处同步维护（新增说话人必须同时登记优先级 / 中文名 / 章节锚点）"
        )


def _llm_who_rank(who: str) -> int:
    """说话人 → 主视图优先级（数值小 = 权威高）；未知说话人统一排最后。"""
    _assert_llm_who_tables()
    return _LLM_WHO_RANK.get(who, len(_LLM_WHO_ORDER))


#: 导入即校验（三张表不同源 ⇒ 直接炸，不许带着错表跑完整轮）
_assert_llm_who_tables()


def _decision_text(decision_path: str | None) -> str:
    """把决策文件读成**完整**的 markdown 文本（优先 `.json`）。

    🔴 2026-11-04 20:30 实测事故：`tradingagents_decision.md` 是**展示件**，
    多空正文被 `_tail(..., 2500)` 截断 ⇒ LLM 的「标的 | 当前权重 | 动作 | 目标权重」
    表格**被截掉一半**（只剩 512800 / 159929 两行）⇒ 结构化目标权重解析不到
    ⇒ `llm_targets` / `llm_conflicts` **全空** ⇒ 交付件看起来「规则层与 LLM 没分歧」，
    实际有 5~6 处（512800 20% vs 10~15%、563020 20% vs 0~10%、511880 20% vs 33.5%、
    现金 60% vs 36.5~90%）。
    ⇒ 修法：结构化解析**改读 `.json`**（字段完整、无截断）；`.md` 只作展示兜底。
    """
    if not decision_path:
        return ""
    path = Path(decision_path)
    # 🔴 F5：失败占位里没有可解析的 LLM 意见（正文是异常摘要）⇒ 直接视为「无意见」。
    if _decision_failure_reason(path):
        return ""
    json_path = path.with_suffix(".json")
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            debate = payload.get("investment_debate") or {}
            risk = payload.get("risk_debate") or {}
            sections = (
                ("研究经理", payload.get("investment_plan", "")),
                ("多头", debate.get("bull_history", "")),
                ("空头", debate.get("bear_history", "")),
                ("激进派", risk.get("aggressive_history", "")),
                ("保守派", risk.get("conservative_history", "")),
                ("中立派", risk.get("neutral_history", "")),
                ("最终交易决定", payload.get("final_trade_decision", "")),
            )
            chunks = [f"## {title}\n{body}" for title, body in sections if body]
            if chunks:
                return "\n\n".join(chunks)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def _heading_is_speaker(heading_text: str, keys: tuple[str, ...]) -> bool:
    """章节标题是否属于说话人 ``keys`` 里的某一个。

    🔴 2026-11-05 01:0x（M-K）修：旧实现只 ``startswith`` ⇒ 正文里以说话人关键词
       开头的**小标题**也会被当成说话人标题 ⇒ 归属被污染。
       修法：关键词之后必须是**分隔符或结束**；紧跟汉字 ⇒ 判为「正文小标题」。
    """
    for k in keys:
        if not heading_text.startswith(k):
            continue
        rest = heading_text[len(k):].lstrip()
        if not rest or rest[0] in _SPEAKER_HEADING_DELIMS:
            return True
    return False


def _header_roles(cells: list[str]) -> dict | None:
    """识别表头行 → 列角色（代码 / 当前 / 目标 / 动作）。

    🔴 只靠「前两个百分比」猜列序是不稳的：本轮 LLM 的表头是
    「标的 | 当前权重 | 调整动作 | 执行后目标」与「标的 | 当前权重 | 方向 | 目标权重 | 核心理由」
    两种；且单元格值是 `约20%` / `一手，约33.5%` 这种带前缀的 ⇒ 必须用表头定角色。
    """
    roles: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        text = cell.strip()
        if any(k in text for k in ("标的", "代码", "品种")):
            roles.setdefault("code", idx)
        if any(k in text for k in ("当前", "现有", "现在", "目前")):
            roles.setdefault("current", idx)
        # 🔴 2026-11-05 02:4x（M-L 修）：**「机械目标 / 规则目标」列不是说话人的目标**。
        #    空头表常把「机械目标（规则层给的数）」与「熊方动作」并排放
        #    （2026-11-05 02:1x 实测表头：「代码 | 当前权重 | 机械目标 | 熊方动作」）
        #    ⇒ 旧实现把「机械目标」判成 target ⇒ 把**规则层**的数字当成 LLM 的目标
        #    ⇒ 产出「方向=不买、目标=20%」这种**自相矛盾**的视图
        #    ⇒ 语义守卫（方向-权重自洽）必然 FAIL ⇒ 交付件不可出。
        #    ⇒ 修法：列名里带「机械 / 规则 / 量化层 / mechanical / rule」的目标列
        #    **不登记为 target**（⇒ 改走「方向格自带百分比」与「语义兜底」）。
        # 🔴 2026-11-05 04:2x（M-N 修）：「建议权重 / 建议比例 / 建议仓位 / 建议目标」
        #    也算目标列；另加「建议 / 计划」类列（排除动作/方向/理由类词）。
        #    实测依据（2026-11-05 03:2x 官方决策的多头表头
        #    「标的 | 当前权重 | 方向 | 12月1日建议 | 核心理由」）：
        #    旧实现认不出「12月1日建议」是目标列 ⇒ 多头那 3 行**整行被丢弃**
        #    ⇒ 多头主张的 512800 20% / 563020 20% / 511880 33.5% **进不了冲突表**
        #    ⇒ 交付件的冲突表**漏掉了多头的声音**。
        # 🔴 盈亏/价格类列名（涨跌幅 / 盈亏率 / 收益率 …）**不登记为 target**
        #    （reviewer B2：否则「| ETF | 方向 | 涨跌幅 | 理由 |」会被锚定成假目标）。
        _is_pnl_col = any(w in text for w in _LLM_PNL_COLUMN_WORDS)
        if any(k in text for k in ("目标", "执行后", "调整到", "调后",
                                   "建议权重", "建议比例", "建议仓位", "建议目标")) and not any(
                k in text for k in ("机械", "规则", "量化层", "mechanical", "rule")) and not _is_pnl_col:
            roles.setdefault("target", idx)
        elif any(k in text for k in ("建议", "计划")) and not _is_pnl_col and not any(
                k in text for k in ("动作", "方向", "操作", "机械", "规则", "量化层",
                                   "理由", "原因", "依据", "说明", "备注", "逻辑",
                                   "要点", "注释", "判断", "结论", "具体执行",
                                   "mechanical", "rule")):
            roles.setdefault("target", idx)
        if any(k in text for k in ("动作", "方向", "建议", "操作")):
            roles.setdefault("action", idx)
        # 🔴 2026-11-04 21:5x reviewer 复现：理由列叫「备注 / 逻辑 / 要点 / 注释」时
        #    识别不出来 ⇒ 旧守卫 `_nxt != _ri` 恒真 ⇒ 锚定直读理由栏的百分比 ⇒ 假冲突。
        # 🔴 2026-11-05 01:3x（M-K 收尾）补「具体执行 / 执行明细 / 落地方式」：
        #    本轮研究经理与 Trader 的表头是「代码|动作|调仓后目标|**具体执行**」，
        #    旧词表认不出 ⇒ 理由列下标为空 ⇒ ① 交付件里 LLM 理由**整列丢失**
        #    （实测 10 只标的 reason 全是空串）；② 兜底找「当前权重」时**直读理由栏**
        #    （563020 行「不建20%仓」⇒ 当前权重被读成 20%）。
        if any(k in text for k in ("理由", "原因", "依据", "说明", "核心理由",
                                   "备注", "逻辑", "要点", "注释", "判断", "结论",
                                   "具体执行", "执行明细", "落地方式")):
            roles.setdefault("reason", idx)
    if not roles:
        return None
    # 🔴 2026-11-05 01:4x（M-K 收尾；reviewer 指出）：词表是**子串匹配** ⇒ 同一格可能被
    #    同时判成两个角色（实测「目标权重说明」既含「目标」又含「说明」⇒ target=reason=1）
    #    ⇒ 下游 `_ti != _ri` 恒否 ⇒ **目标列被静默作废** ⇒ 改走锚定、甚至整行被丢弃。
    #    ⇒ 护栏：理由列若与其它角色**同列**，保留其它角色、丢弃理由（目标最关键）。
    for _role in ("reason",):
        _idx = roles.get(_role)
        if _idx is not None and _idx in {roles.get("target"), roles.get("action"),
                                         roles.get("code"), roles.get("current")}:
            roles.pop(_role)
    return roles


def _pnl_column_indices(cells: list[str]) -> set[int]:
    """表头里**盈亏/价格/收益类**列的下标集合。

    🔴 2026-11-05 04:1x（M-N 修，reviewer B2）：旧实现的锚定路径（方向列的下一格）
       **不排除**这类列 ⇒ 表头「| ETF | 方向 | 涨跌幅 | 理由 |」时，
       锚定会把「涨跌幅」格（18.91%）当成目标权重 ⇒ 造出假冲突。
       ⇒ 这类列**既不登记为 target**，也**不许**被锚定取数。
    """
    out: set[int] = set()
    for idx, cell in enumerate(cells):
        text = str(cell).strip()
        if any(w in text for w in _LLM_PNL_COLUMN_WORDS):
            out.add(idx)
    return out


def _backward_segment(cell: str, start: int) -> str:
    """百分数**前面**的语境片段（≤8 字 → 标点截断 → 锚点截断）。

    🔴 截断规则各有实测依据：
       · **标点截断**：「卖，浮亏18.91%」⇒ 片段是「浮亏」（不是「卖，浮亏」）；
       · **锚点截断**：「当前目标 20%」⇒ 片段是「」（「当前」是「当前**目标**」的一部分
         ⇒ 不该判它污染语境）；「目标收益 20%」⇒ 片段是「收益」。
    """
    seg = cell[max(0, start - 8):start]
    seg = re.split(r"[，。；、,;：:（）()\[\]【】「」《》]", seg)[-1]
    best, best_len = -1, 0
    for a in _LLM_TARGET_ANCHORS:
        idx = seg.rfind(a)
        if idx > best:
            best, best_len = idx, len(a)
    if best >= 0:
        seg = seg[best + best_len:]
    return seg


def _forward_segment(cell: str, end: int) -> str:
    """百分数**后面**的语境片段（≤8 字 → 首个标点截断）。

    🔴 必须**前后都看**：真实案例「+9.11%**的浮盈**不能覆盖退出信号」里
       「浮盈」在数字**后面**（只看前面 ⇒ 漏判 ⇒ 9.11% 被当成目标权重）。
    """
    seg = cell[end:end + 8]
    return re.split(r"[，。；、,;：:（）()\[\]【】「」《》]", seg)[0]


def _attached_non_target(segment: str,
                         words: tuple[str, ...] = _LLM_NON_TARGET_BACKWARD) -> bool:
    """片段里是否有**贴着**数字的非目标语境词（被动词隔开的 ⇒ 不算）。

    🔴 「目标较当前提升至 15%」⇒ 「当前」与数字之间隔着「提升至」
       ⇒ 不算污染 ⇒ 15% 是合法目标（reviewer A2 实测）。
    """
    for w in words:
        idx = segment.rfind(w)
        if idx < 0:
            continue
        between = segment[idx + len(w):]
        if any(ch in between for ch in _LLM_VERB_CHARS):
            continue
        return True
    return False


def _is_non_target_pct(cell: str, start: int, end: int) -> bool:
    """这个百分数是不是**盈亏/价格/动量**语境里的数（⇒ 不是目标权重）。"""
    if _attached_non_target(_backward_segment(cell, start)):
        return True
    fwd = _forward_segment(cell, end)
    return any(w in fwd for w in _LLM_NON_TARGET_FORWARD)


def _is_abs_pnl_pct(cell: str, start: int, end: int) -> bool:
    """这个百分数是不是**绝对盈亏/价格/当前**语境里的数（A16 不变量专用）。

    🔴 与 `_is_non_target_pct` 的差别：这里**只用** `_LLM_PNL_ABS_WORDS`
       （**不含**「动量/胜率/概率」）⇒ 不会因为「目标 20%」与「动量+20%」数值相同
       而**假阳性拦下交付**（2026-11-05 04:5x 实测：官方决策里 512800 的多头行
       目标 20%、同行「120日动量+12.10%」⇒ 动量口径下两者不相等，但口径本身有风险）。
    """
    if _attached_non_target(_backward_segment(cell, start), _LLM_PNL_ABS_WORDS):
        return True
    fwd = _forward_segment(cell, end)
    return any(w in fwd for w in _LLM_PNL_ABS_WORDS)


def _is_anchored_pct(cell: str, start: int) -> bool:
    """百分数前面（≤10 字、且中间没有别的百分数）是否出现「目标类锚点」。"""
    seg = cell[max(0, start - 10):start]
    if "%" in seg:
        seg = seg.rsplit("%", 1)[1]
    return bool(_LLM_TARGET_ANCHOR_RE.search(seg))


#: 🔴 2026-11-11（真机实测）：**区间**写法（两个百分数之间是区间分隔符）
#:    ——「0%—10%」「10%~12%」「8% 至 10%」等（容忍 markdown 噪声）。
_LLM_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%[\s*`_~]*(?:—|–|-|~|～|至|到)[\s*`_~]*(\d+(?:\.\d+)?)\s*%"
)
#: 🔴 2026-11-11：区间**之后**若出现这些标记 ⇒ 后面的百分数才是**说话人的落点**
#:    （「基准约10%」「中枢8%」「目标 6%」「以 12% 为准」）。
_LLM_BASELINE_MARK_RE = re.compile(
    r"(?:基准(?:值)?|中枢|目标(?:值)?|以[^，,。;；\s]{0,8}为准)"
)


def _extract_target_pct(cell: str, anchor_required: bool = False) -> float | None:
    """从格内文本抽**目标权重**（M-N 统一口径，①②③ 三条路径共用）。

    口径（2026-11-05 04:1x 定稿，reviewer A2/A3/A4/B1/B2 逐条实测）：
      ① 扫格内**全部**百分数；
      ② 越界（<0 或 >100）⇒ 丢弃；
      ③ **盈亏/价格/动量语境**（数字前后紧邻窗口命中非目标词）⇒ 丢弃；
      ④ 「从/由 A% … 到/至/升/降/调至 B%」⇒ 取**落点 B**（reviewer A3）；
      ④' 「**区间在前、基准在后**」⇒ 取**基准值**（2026-11-11 真机实测，见下）；
      ⑤ 否则取**第一个**存活值（**区间口径：取下界**，reviewer A4：
         「目标(区间) 8%—10%」⇒ 8；Trader 那行「约9%，执行区间8%—10%」⇒ 9）；
      ⑥ 全都取不到 ⇒ ``None``（**不猜、不补 0**）⇒ 该行按「判不出目标」处理。

    🔴 2026-11-11（服务器真机实测 · 真缺陷）：④' 这条是补 reviewg A4 未覆盖的写法。
       LLM 的 Trader 最终裁决把目标写成「**区间在前 + 基准在后**」：
         `| 512800 | 22.54% | 减仓 | **0%—10%，基准约10%** | … |`
       ⇒ 旧口径「取第一个存活值」取到区间**下沿 0%** ⇒ 说话人明说「保留约 10%」
         被写成**目标 0%（清仓）** ⇒ 交付件的 LLM 目标全被抽空 ⇒ 分歧表空 ⇒
         情景生成器与三个闸门探针连环失败（且该错误**不会**被任何守卫拦下）。
       ⇒ 修法：**区间之后**若出现「基准 / 中枢 / 目标 / 以…为准」标记的百分数
         ⇒ 取**那个**（说话人的落点）；没有基准标记时**保持旧口径**（取下界）。

    ``anchor_required=True``：只在**带「目标类锚点」**的百分数里挑
    （兜底路径用 ⇒ 不会把理由栏里「不接受20%目标」的 20% 当成目标）。
    """
    if not cell:
        return None
    # 🔴 「当前权重→建议权重」型单元格 ⇒ **箭头后面**才是目标（箭头前面是「当前」）。
    scope = cell
    _arrow_parts = _LLM_ARROW_SPLIT.split(cell)
    if len(_arrow_parts) > 1:
        scope = _arrow_parts[-1]
    hits = list(_LLM_PCT_LOOSE.finditer(scope))
    if not hits:
        return None
    _ft = _LLM_FROM_TO.search(scope)
    if _ft is not None:
        _b = float(_ft.group(2))
        if _pct_ok(_b) and not _is_non_target_pct(scope, _ft.start(2), _ft.end(2)):
            return _b
    # 🔴 2026-11-11（真机实测）：**区间在前、基准在后** ⇒ 取**基准值**，不取区间下沿。
    #    「**0%—10%，基准约10%**」⇒ 说话人明说「保留约 10%」⇒ 必须取 10.0，
    #    旧口径取第一个存活值 = 0.0 ⇒ 把「减仓到 10%」静默写成「清仓 0%」。
    _rg = _LLM_RANGE_RE.search(scope)
    if _rg is not None:
        _tail = scope[_rg.end():]
        _bm = _LLM_BASELINE_MARK_RE.search(_tail)
        if _bm is not None:
            _pm = _LLM_PCT_LOOSE.search(_tail, _bm.end())
            if _pm is not None:
                _pv = float(_pm.group(1))
                if _pct_ok(_pv) and not _is_non_target_pct(scope, _pm.start(), _pm.end()):
                    return _pv
    for m in hits:
        value = float(m.group(1))
        if not _pct_ok(value):
            continue
        if _is_non_target_pct(scope, m.start(), m.end()):
            continue
        if anchor_required and not _is_anchored_pct(scope, m.start()):
            continue
        return value
    return None


def _yuan_cell_to_pct(cell: str, total_assets: float | None) -> float | None:
    """把「用金额表达的目标」折成百分比（例：「约2,985元」/ 账户 29,854.67 ⇒ 10.0%）。

    🔴 2026-11-05 01:0x（M-K）：LLM 的多头表常把目标写成**金额**而不是百分比
       （「约5,970元」「约2,985元」）⇒ 旧实现判不出百分比 ⇒ **整行被丢弃**
       ⇒ 多头「仅10%试仓」这类真实分歧永远进不了冲突表。
    🔴 只在单元格**只有「元」没有「%」**时才折算，避免与百分比口径混用；
       折算不出（没有金额 / 没有总资产）⇒ 返回 ``None``（不补 0）。
    """
    if not cell or total_assets is None or float(total_assets) <= 0:
        return None
    if "元" not in cell or "%" in cell:
        return None
    # 🔴 2026-11-05 04:3x（M-N）：「万」单位也要认（LLM 常写「买1手，约1万元」
    #    ⇒ 10,000 元 / 账户 29,854.67 元 = 33.4956%）。
    _amt = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(万)?\s*元", cell)
    if not _amt:
        return None
    _base = float(_amt.group(1).replace(",", ""))
    _yuan = _base * (10000.0 if _amt.group(2) else 1.0)
    value = _yuan / float(total_assets) * 100.0
    return value if _pct_ok(value) else None


def _pct_ok(value: float | None) -> bool:
    """权重必须落在 [0, 100]。

    🔴 2026-11-04 21:5x reviewer 复现：负数（`-26.96%` 的浮亏）或越界值**不是权重**，
    是损益率 / 笔误 ⇒ 解析时必须拒掉，否则「LLM 说清仓」会被读成「LLM 要 26.96%」。
    """
    return value is not None and 0.0 <= float(value) <= 100.0


def _resolve_current_pct(code: str, cells: list[str], row_roles: dict | None,
                         reason_idx: int | None, target_idx: int | None,
                         account_weights: dict[str, float] | None) -> float | None:
    """当前权重（百分比）：**账户真值** → LLM 显式「当前」列 → ``None``（未知）。

    🔴 2026-11-05 01:3x（M-K 收尾）：旧实现在这一格上有**三条**能造出假数字的路子
       （方向列前一格 ⇒ 读到**目标列**；行内第一个百分比 ⇒ 读到**理由栏**；
        兜底 ``current_pct = target_pct`` ⇒ **直接伪造**）。
       前两条会写进交付件；第三条会让交付件自带的「方向-权重语义自洽」守卫
       按「减 ⇒ 目标 < 当前」判 ⇒ 目标 = 当前 ⇒ **假阳性拦下交付**。
       ⇒ 现在只认两个可信来源：**账户快照真值**（账户里没有 ⇒ 未持有 ⇒ 0%）
       与**表头明确指认的「当前」列**（整格必须是纯百分比）；
       都没有 ⇒ **None（未知）** ⇒ 守卫跳过该行——**未知 ≠ 矛盾，但不许编**。

    🔴 2026-11-05 01:4x（reviewer 复现的 BLOCKING）：``account_weights`` 必须用
       **None（无真值）** 与 **非空 dict（有真值）** 区分；**空 dict 不再表示「有真值但没持有」**
       —— 否则账户快照读不到时，所有标的当前权重会被伪造成 0% ⇒ 守卫再次假阳性。
       ⇒ 有真值时：字典里没有的代码 = **未持有** ⇒ 0%（合法）；
         无真值时（``None``）：一律 **None（未知）**。
    """
    current_pct = None
    if account_weights is not None:
        if code in account_weights:
            current_pct = account_weights[code]
        elif code != "现金":
            # 账户里没有该代码 ⇒ **未持有** ⇒ 当前权重真值 = 0%（不是「未知」）
            current_pct = 0.0
    cur_idx = row_roles.get("current") if row_roles else None
    if (current_pct is None and cur_idx is not None and cur_idx < len(cells)
            and cur_idx != reason_idx and cur_idx != target_idx):
        m = _LLM_PCT_PURE.match(cells[cur_idx])
        if m and _pct_ok(float(m.group(1))):
            current_pct = float(m.group(1))
    return current_pct


def _llm_target_views(decision_md: str | None, total_assets: float | None = None,
                      account_weights: dict[str, float] | None = None) -> dict:
    """抽 LLM 表格里的逐标的目标权重：**列序无关**，并保留多方视图。

    🔴 2026-11-05 01:3x（M-K 收尾）：``account_weights`` 是**账户真值当前权重**
       （代码 → 百分比；``现金`` 单列）。当前权重**只**从它或**表头明确指认的
       「当前」列**取；两者都没有 ⇒ ``None``（**未知**）⇒ 绝不伪造。

    🔴 2026-11-05 01:0x（M-K）：``total_assets`` 用于把**用金额表达的目标**
       （如「约2,985元」= 账户 29,854.67 元的 10%）折成百分比；
       不给 ⇒ 该行判不出目标 ⇒ **整行丢弃**（多头「仅10%试仓」这类真实分歧会丢）。

    🔴 2026-11-04 夜实测事故：旧正则要求列序固定为「代码|方向|当前%|目标%」
    ⇒ 多头表（列序是「代码|当前%|方向|目标%|…」）与空头表**全部匹配失败被静默跳过**
    ⇒ 多头主张的「512800 加到 25%」（比规则层 20% 高 5pp，全场最大分歧）
    **根本没进冲突表** ⇒ 交付件只剩 3 条冲突。
    ⇒ 修法：按单元格语义识别（代码 / 方向 / 百分比），不看列序；并按章节标注是谁说的。
    """
    if not decision_md:
        return {}
    text = _decision_text(decision_md)
    if not text:
        return {}
    views: dict[str, list[dict]] = {}
    # 「谁说的」：按标题里的说话人标记滚动更新（研究经理 / 多头 / 空头 / 风险三方）；
    # 非标记标题**不重置**（多头/空头正文里自带 ## 级小标题，不能把归属冲掉）；
    # 判定用「剥掉序号后**前缀匹配**」⇒ 正文里出现「我会如何正面回应空头」这类小标题
    # 不会把归属从多头误切成空头。
    who = "unknown"
    roles: dict | None = None
    roles_ncols: int = -1
    pnl_cols: set[int] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading_text = _LLM_HEADING_NUM.sub("", line.lstrip("#").strip()).strip().lower()
            for _key, _keys in _LLM_SECTIONS:
                if _heading_is_speaker(heading_text, _keys):
                    who = _key
                    break
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        joined = "".join(cells)
        if not joined or set(joined) <= set("|-: "):
            continue
        _row_codes = [c for c in cells if _LLM_CODE_CELL.match(c)]
        if not _row_codes:
            # 没有代码列 ⇒ 可能是**表头**（含「标的 / 当前权重 / 目标权重 / 动作」等角色词）
            _roles = _header_roles(cells)
            if _roles:
                roles = _roles
                roles_ncols = len(cells)
                pnl_cols = _pnl_column_indices(cells)
            continue
        # 🔴 F6b：**表头列数与本行列数不一致** ⇒ 表头的列角色对不上（LLM 偶尔多写/少写一列，
        #    或理由里含竖线）⇒ 本行**不采信表头角色**，改用「方向列锚定」的路子。
        # 🔴 F6c（2026-11-04 21:5x reviewer 复现）：旧实现把表头角色**整体作废** ⇒ 整行被
        #    丢弃 ⇒ 「多头主张 512800 加到 25%」这类**真实重大分歧**永不进冲突表（漏报）。
        #    ⇒ 改成**降级**而非作废：① 角色下标必须 < 本行列数，否则该角色作废；
        #      ② 「目标列」在列数不符时额外要求**整格是纯百分比**才采信；
        #      ③ 「方向列」必须真的含方向词（角色自验证），否则该角色作废。
        row_roles = None
        target_strict = True
        if roles is not None:
            _valid_roles = {k: v for k, v in roles.items() if v < len(cells)}
            if _valid_roles:
                row_roles = _valid_roles
                target_strict = (len(cells) != roles_ncols)
        code = None
        for c in cells:
            m = _LLM_CODE_CELL.match(c)
            if m:
                code = m.group(1)
                break
        if code is None:
            continue
        pcts: list[tuple[int, float]] = []
        for idx, c in enumerate(cells):
            m = _LLM_PCT_LOOSE.search(c)
            if m:
                pcts.append((idx, float(m.group(1))))
        if len(pcts) < 1:
            continue
        _ri = row_roles.get("reason") if row_roles else None
        _ai = row_roles.get("action") if row_roles else None
        # 🔴 角色自验证：表头指认的「方向列」必须真的含方向词，否则作废、回落顺序扫描
        if _ai is not None and not _LLM_DIR_CELL.search(cells[_ai]):
            _ai = None
        # ---- 方向列：表头没指认就按顺序找第一个命中的单元格 ----
        if _ai is None:
            for _idx, cell_text in enumerate(cells):
                if cell_text == code:
                    continue
                # 🔴 2026-11-11（critic 审计 · 高危）：**理由列不许当方向列**
                #    理由栏是散文（「逢高卖出部分，保留底仓」）⇒ 里面的方向词**不是裁决**
                #    ⇒ 旧实现会把理由列认成方向列 ⇒ 随后的 ZERO 强制归零把合法目标改写成 0%。
                if _ri is not None and _idx == _ri:
                    continue
                if _LLM_DIR_CELL.search(cell_text):
                    _ai = _idx
                    break
        direction = ""
        _dir_idx: int | None = None
        if _ai is not None and _ai < len(cells):
            direction = _classify_dir_cell(cells[_ai])
            if direction:
                _dir_idx = _ai
        if not direction:
            for _idx, c in enumerate(cells):
                if c == code:
                    continue
                _d = _classify_dir_cell(c)
                if _d:
                    direction = _d
                    _dir_idx = _idx
                    break
        if not direction:
            continue
        # 🔴 2026-11-11（critic 审计 · 高危）：方向**只**能从**理由列**兜底扫出来时
        #    （表头没指认方向列 / 方向列写「—」⇒ `_ai` 作废后落到兜底扫描），
        #    这个方向词是**散文里的词**，不是 LLM 的方向裁决
        #    ⇒ **不许**拿它去强制归零：那会把一个合法目标**静默改成 0%**，
        #      而语义守卫因为「方向=卖 + 目标=0%」自洽还会**放行** ⇒ 静默降级。
        #    实测复现：`| 512800 | — | 10% | 逢高卖出部分，保留底仓 |`
        #      修复前：10.0 会被守卫**响亮报错**；修复后（盲目归零）：0.0 静默通过。
        #    ⇒ 修法：方向来源是理由列 ⇒ **不强制归零**（保留原值），
        #      让「LLM 方向-权重语义自洽」守卫**响亮拦截**（把歧义暴露出来，而不是替 LLM 猜）。
        _dir_from_reason = (_ri is not None and _dir_idx == _ri)
        # 🔴 2026-11-09：ZERO 方向（卖/清仓/不买…）⇒ 目标权重**必然是 0%**，
        #    且**先于任何数字路径生效**（见下面 ④ 之前的说明）。
        _force_zero = direction in _ZERO_DIR_WORDS and not _dir_from_reason

        # ---- 目标权重列：三条可靠路径，按优先级；**都判不出来 ⇒ 丢弃该行** ----
        # 🔴 F6（2026-11-04 21:2x E2E 实测事故）：
        #   旧兜底是「取行内**第 2 个**百分比当目标权重」⇒ 当 LLM 的表头**没有**
        #   「目标/执行后/调整到」这类列名（例：表头写「ETF | 权重 | 方向 | 理由」）
        #   时，`pcts[1]` 命中的往往是**理由栏里的话**——实测把
        #   「+9.11% 的浮盈不能覆盖退出信号」当成 159929 的**目标权重 9.11%**、
        #   把「一手约占 33.5%」当成 511880 的**目标权重 33.5%**、
        #   把「-26.96% 的亏损」当成 513330 的**目标权重 26.96%**
        #   ⇒ 交付件的冲突表写出「规则 0% vs LLM 26.96%」这种**完全相反**的结论
        #   （LLM 明明说的是清仓）。
        #   ⇒ 修法：① 表头指认「目标列」就用它；② 否则以**方向列为锚**，
        #     取方向列**紧邻的下一格**的百分比（本轮两种表头都符合这个结构）；
        #     ③ 仍判不出来就**丢弃该行**（宁可少报一条，不可乱报）。
        # 🔴 2026-11-11（真机跑批 · 审计）：ZERO 方向（卖/清仓/不买…）⇒ 目标权重
        #    **必然是 0%**，且**先于任何数字路径生效**（上面那句注释写的就是这个
        #    意图，但**旧实现只挡住了「金额→百分比」折算**，其它数字路径照样生效）。
        #    实测事故（空头表真实行）：
        #      `| 515450 | 未持有 | 不买/观察 | 若必须配置权益，条件性上限8%，绝非16% | … |`
        #    ⇒ 目标列里的「**条件性上限** 8%」被当成**目标权重** ⇒ 方向「不买」配目标 8%
        #    ⇒ 交付件自带的「LLM 方向-权重语义自洽」守卫 FAIL
        #    （`verify_artifacts._check_llm_direction_semantics` + `probe_llm_parser` I1）。
        #    ⇒ 修法：`_force_zero` 时 `target_pct` **直接置 0.0**，
        #      且**所有**取数路径（表头目标列 / 方向列下一格 / 目标锚定 / 方向格自带百分比）
        #      一律跳过（它们都以 `target_pct is None` 为前提 ⇒ 天然被挡住）。
        target_pct = 0.0 if _force_zero else None
        _ti = row_roles.get("target") if row_roles else None
        if _ti is not None and _ti != _ri and not _force_zero:
            _cell = cells[_ti]
            # 表头指认「目标列」：列数对齐时用**统一取数**（允许「一手，约33.5%」
            # 「减至约 20%」这种带前缀的，同时**挡住**盈亏语境，reviewer B2）；
            # 列数对不齐时**必须**是纯百分比格（否则可能读到被挤位的理由格）。
            if target_strict:
                match_obj = _LLM_PCT_PURE.match(_cell)
                pct_value = float(match_obj.group(1)) if match_obj else None
            else:
                pct_value = _extract_target_pct(_cell)
            if pct_value is not None and _pct_ok(pct_value):
                target_pct = pct_value
        # 🔴 2026-11-05 01:0x（M-K）修：目标用**金额**表达时（多头表写「约2,985元」
        #    = 账户 29,854.67 元的 10.0%）旧实现判不出百分比 ⇒ **整行被丢弃**
        #    ⇒ 多头「仅10%试仓」这类**真实分歧**永远进不了冲突表（信息丢失）。
        #    修法：表头指认的目标格里若**只有「元」没有「%」**，按 total_assets 折成百分比。
        if target_pct is None and _ti is not None and _ti != _ri and not _force_zero:
            # 🔴 2026-11-09（独立审计实证）：**ZERO 方向下不许做「金额→百分比」折算**。
            #    实测事故：多头表「| **159929** | **卖** | 清仓，约3,335元 | …」⇒
            #    3,335 元是**卖出金额**（＝清掉 11.17% 的现有仓位），却被折成
            #    **11.17% 的「目标权重」** ⇒ 方向「卖」配目标 11.17% ⇒ 语义守卫必然 FAIL。
            #    ⇒ 修法：方向 ∈ ZERO（卖/清仓/不买…）⇒ 目标**必然是 0%**（走 ④ 兜底），
            #      **任何金额都不许参与折算**（部分卖出「减」不受影响 ⇒ 折算照旧）。
            _yuan_pct = _yuan_cell_to_pct(cells[_ti], total_assets)
            if _yuan_pct is not None:
                target_pct = _yuan_pct
        if target_pct is None and _ai is not None and _ai + 1 < len(cells):
            _nxt = _ai + 1
            # 🔴 F6d（2026-11-04 21:5x reviewer 复现的 **BLOCKING**）：
            #    锚定取到的格子必须**整格就是一个百分比**。
            #    旧守卫只有 `_nxt != _ri`：LLM 表头写「备注」或整张表没有表头行时，
            #    理由列识别不出来 ⇒ `_ri is None` ⇒ 守卫恒真 ⇒ 锚定**直读理由栏**，
            #    实测把「+9.11% 的浮盈不能覆盖退出信号」当成 159929 的目标权重 9.11%
            #    （与 F6 要消灭的症状**逐字复现**；且因冲突表非空 ⇒ verify 判 PASS ⇒ 静默交付）。
            #    ⇒ 修法：锚定格必须是**纯百分比格**；不是 ⇒ 放弃锚定，走 ③/④。
            # 🔴 2026-11-05 04:2x（M-N 修；reviewer B2）：锚定**不许**落到
            #    「涨跌幅 / 盈亏率」这类列上（表头「| ETF | 方向 | 涨跌幅 | 理由 |」
            #    ⇒ 旧实现把 18.91% 当目标权重 ⇒ 造出假冲突）。
            if _nxt != _ri and _nxt not in pnl_cols:
                match_obj = _LLM_PCT_PURE.match(cells[_nxt])
                if match_obj:
                    pct_value = float(match_obj.group(1))
                    if _pct_ok(pct_value):
                        target_pct = pct_value
        if target_pct is None and _ti is None and _ai is not None and _ai < len(cells):
            # 🔴 2026-11-05 02:4x（M-L 修）：**方向格自带百分比** ⇒ 那是**说话人自己的目标**。
            #    触发条件：表头**没有**真正的目标列（要么没表头，要么那一列被判定为
            #    「机械目标 / 规则目标」⇒ 见 _header_roles 的修 1）⇒ 此时方向格是唯一的
            #    目标证据。
            #    实测（2026-11-05 02:1x 的空头表「代码|当前权重|机械目标|熊方动作」）：
            #      「不买，0%」⇒ 0.0（不是机械目标列的 20%）；
            #      「减至10%左右，上限15%」⇒ 10.0（取**第一个**百分比 = 说话人的落点）。
            #    🔴 顺序很关键：放在「表头目标列」与「方向列锚定」**之后**
            #    ⇒ 不动既有的 ①②③④ 判定口径（A2/A3/A4/A6/A7/A8 全部不变）。
            # 🔴 2026-11-05 04:2x（M-N 修；reviewer B1）：旧实现直接取方向格里
            #    **第一个**百分数 ⇒ 「卖，浮亏18.91%」⇒ 目标 18.91%（**同型 bug 复活**）
            #    ⇒ 方向-权重语义守卫必然 FAIL。⇒ 改走**统一取数**（带语境守卫）。
            _dv = _extract_target_pct(cells[_ai])
            if _dv is not None:
                target_pct = _dv
        if target_pct is None:
            # ③ 兜底：单元格文本里显式写「目标 X%」（取**最后一个**数字：
            #    「目标从 20% 降到 10%」的答案是 10，不是 20）
            # 🔴 2026-11-05 03:0x（M-L 修）**真 bug**：旧正则 `目标[^0-9]{0,6}(-?\d+...)`
            #    的填充字符类把**负号一起吃掉** ⇒ 理由栏的「当前-18.91%」被读成
            #    **正的 18.91%** ⇒ 「LLM 说清仓」被报成「LLM 要 18.91%」
            #    ⇒ 方向-权重语义自洽守卫必然 FAIL（2026-11-05 02:39 实测）。
            #    ⇒ 现在用 `_LLM_TARGET_PCT`（**负号保留** ⇒ 负值原样带出 ⇒
            #      `_pct_ok` 拒掉）+ **盈亏窗口守卫**（窗口里出现 当前/浮亏/亏损/…
            #      ⇒ 那是损益率 / 价格，不是目标权重）。
            # 🔴 2026-11-05 04:2x（M-N 修；reviewer A2/A3）：
            #    · 旧实现只认「目标」后**第一个**百分数 ⇒「目标从 20% 降到 10%」取 20（应 10）；
            #    · 旧实现的「窗口守卫」会把「目标从当前 10% 升至 15%」「目标收益 20%」
            #      **整条拒掉** ⇒ 该行被丢弃 ⇒ **真实分歧静默漏报**。
            #    ⇒ 改走**统一取数**（`_extract_target_pct`）：
            #      「从/由 A% … 到/至/降/调至 B%」取落点 B；否则取第一个通过语境守卫的值；
            #      且**必须**带「目标类锚点」（否则理由栏里「不接受20%目标」的 20% 会被误取）。
            for _cell in cells:
                if not _LLM_TARGET_ANCHOR_RE.search(_cell):
                    continue
                pct_value = _extract_target_pct(_cell, anchor_required=True)
                if pct_value is not None:
                    target_pct = pct_value
                    break
        if target_pct is None:
            # ④ 语义兜底：**卖出/清仓/不买 ⇒ 目标必然是 0%**（不需要百分比来证明）
            #    🔴 2026-11-09：ZERO 词表用 `_ZERO_DIR_WORDS`（与语义守卫同口径），
            #    旧正则漏了「不建仓 / 清零 / 卖掉」等词 ⇒ 那些行会被**丢弃**（信息丢失）。
            #    🔴 2026-11-09：旧口径用**子串** `卖` ⇒「减持」「减仓」里都命中
            #       ⇒ 减仓方向被兜底强制成 0%（清仓）⇒ 与「部分卖出≠清仓」冲突。
            #       ⇒ 现在先排除「减」字后面的「卖」（(?:^|[^减])卖），减仓保持 None（不猜）。
            if direction in _ZERO_DIR_WORDS or re.search(r"(?:^|[^减])卖|清|不买|退出|清掉", direction):
                target_pct = 0.0
            else:
                continue   # 判不出目标权重 ⇒ 丢弃该行，不猜
        # ---- 当前权重：**只认两个可信来源**，判不出就是「未知」（None），**绝不伪造** ----
        # 🔴 2026-11-05 01:3x（M-K 收尾）修：旧实现有**三条**能造出假当前权重的路子，
        #    任意一条都会把**不存在**的数字写进交付件：
        #      ① 「方向列**前一格**」⇒ 多头表「代码|当前权重|目标权重|机械动作」的
        #         方向列前一格正是**目标列** ⇒ 20%（目标）被当成当前权重；
        #      ② 「行内第一个非理由非目标百分比」⇒ 读到**理由栏**里的句子
        #         （实测 563020 行「不建20%仓，也不做10%试仓」⇒ 当前权重被读成 20%，
        #          而用户**根本没持有**该标的）；
        #      ③ 兜底 `current_pct = target_pct`（**直接伪造**）⇒ 研究经理/空头的
        #         「Strategic Actions」表**没有「当前权重」列** ⇒ 当前权重 = 目标权重
        #         ⇒ 交付件自带的「方向-权重语义自洽」守卫按「减 ⇒ 目标 < 当前」判
        #         ⇒ 10 < 10 不成立 ⇒ **假阳性拦下交付**（实测 512800）。
        #    ⇒ 修法：当前权重只从 ①**账户真值**（账户快照里该代码的真实权重；
        #      账户里没有 ⇒ 未持有 ⇒ 真值 0%）与 ②**表头明确指认的「当前」列**
        #      （整格必须是纯百分比）取；两者都没有 ⇒ **None（未知）**，
        #      守卫对**未知**跳过该行（不判 FAIL）——**未知 ≠ 矛盾**，但也**不许编**。
        current_pct = _resolve_current_pct(code, cells, row_roles, _ri, _ti,
                                           account_weights)
        # 理由列：有表头就用表头指认的那一列；没有就留空
        #（多头表最后一列是「执行后目标」，拿它当理由会写出「约20%」这种废话）
        reason = ""
        if _ri is not None and _ri < len(cells):
            reason = cells[_ri].strip().strip("*")
        # 🔴 2026-11-05 04:5x（M-N 修；reviewer C3）：把**本行所有格子里**
        #    「处于盈亏/价格/动量语境」的百分数一并记下来（**正负都算**）。
        #    用途：A16 不变量探针据此断言「目标权重不得等于该行的任何一个盈亏率」
        #    —— 旧版只扫 reason 格的**负号**百分数 ⇒ 抓不到真实决策里空头理由的
        #    **正数**「浮亏26.96%」，且对产出金标那行（reason 全空）**完全真空**。
        pnl_pcts: list[float] = []
        for _c in cells:
            for _m in _LLM_PCT_LOOSE.finditer(_c):
                if _is_abs_pnl_pct(_c, _m.start(), _m.end()):
                    pnl_pcts.append(float(_m.group(1)))
        views.setdefault(code, []).append({
            "direction": direction,
            "current_pct": current_pct,
            "target_pct": target_pct,
            "reason": reason,
            "who": who,
            "pnl_pcts": pnl_pcts,
        })
    return views

def _build_llm_final_decision(args, plans: list[dict], llm_targets: dict,
                              llm_conflicts: list[dict],
                              ta_rejected: list[dict] | None = None,
                              ta_moved_out: list[dict] | None = None) -> dict:
    """把 Trader 的**最终裁决**抽出来，并与规则层 / 多头表 / 空头表**对账**。

    🔴 2026-11-05 04:4x（M-P）**为什么必须加这一层**：
       `llm_targets` / `llm_conflicts` **只从表格取数** ⇒ Trader 的最终裁决写在
       「最终交易决定」章节的**散文**里（该章节没有表格）⇒ **完全抽不到**
       ⇒ 交付件里「LLM 目标」永远是**多头表/空头表**的数。
       实测：最终裁决与两张表**三处都不一样**（512800 12% vs 20%/10%；
       511880 买 1 手 vs 33.5%/不买；563020 不建仓 vs 10%/不买）
       ⇒ 冲突表按「与规则层**差得最远**」取行 ⇒ 报出来的是**空头**的数字
       ⇒ **用户读到的「LLM 结论」与最终裁决不一致**（511880 上甚至相反）。

    🔴 **口径**：本函数**不改动** `llm_targets` / `llm_conflicts`（金标 A1 必须不变）
       ⇒ 只**新增**一个独立字段 `llm_final_decision`。
       抽不出的数字一律 `None`（**不许编造**），每条都带**原文证据句**。

    🔴 2026-11-11（审计缺陷 D）：被 TA **一票否决**的代码原先会从本表里**静默消失**
       （表是按 `plans` 派生的，而被否决者已被剔除）⇒ 正文写着「**以本节为准**」，
       交付件却对该代码只字不提 ⇒ 自相矛盾。
       ⇒ 被否决的代码**单列一行**（规则层目标 = 0%，注明「TA 一票否决」）；
         若它**因否决而移出**了冲突表 ⇒ 在 `ta_moved_out` 里**显式标注**
         「因 TA 否决而移出」（不动 `llm_conflicts` 本体 ⇒ 金标 A1 不变）。
    """
    raw = llm_final_mod.load_from_decision(getattr(args, "decision", None))
    out: dict = {
        "available": bool(raw.get("available")),
        "source_file": raw.get("source_file"),
        "source_section": raw.get("source_section"),
        "rating": raw.get("rating"),
        "summary": raw.get("summary", ""),
        "post_trade": raw.get("post_trade", {}) or {},
        "targets": raw.get("targets", {}) or {},
        "unparsed_codes": raw.get("unparsed_codes", []) or [],
        "reconciliation": [],
        "note": raw.get("note", ""),
    }
    if not out["available"]:
        return out

    # 🔴 2026-11-11（真实决策 md 实跑实证）：Trader 把目标权重写在**不带代码**的句子里
    #    （实测 2026-11-03 那一轮：「将当前22.54%的仓位降至**基准约10%、硬上限10%**
    #      的战术观察仓」—— 这句**没有代码** ⇒ 散文层的**句级归属**拿不到标的
    #      ⇒ `target_pct=None`）
    #    ⇒ 旧口径 ⇒ 计划里 512800 的最终裁决「没有目标权重」
    #    ⇒ `make_decision_options` 判它「抽不出数字」⇒ **真实分歧（规则层 20% vs
    #      LLM 10%）被静默丢掉** ⇒ 「待拍板分歧 0 项」
    #    ⇒ `make_decision_scenarios` 抛错 ⇒ 重建链转红。
    #    ⇒ 修法：用**同一份决策 md 的「最终裁决表」**（`llm_targets` 里 `who == "final"`
    #      的那一档 —— 它本来就是**按代码归档**的 ⇒ **不存在猜归属**）回填目标权重，
    #      并把**口径来源**写进 `target_pct_source`（**不冒充**「散文原文抽到」）。
    #    ⇒ 只在「散文抽不到」时回填 ⇒ 散文抽得到的**一律以散文为准**（口径不被污染）。
    for _code, _t in (out.get("targets") or {}).items():
        if not isinstance(_t, dict) or _t.get("target_pct") is not None:
            continue
        _v = (llm_targets or {}).get(_code)
        if not isinstance(_v, dict) or _v.get("who") != "final":
            continue
        if _v.get("target_pct") is None:
            continue
        _t["target_pct"] = float(_v["target_pct"])
        _t["target_pct_source"] = (
            "最终裁决表（调仓后目标列）｜散文原文未抽到目标权重"
            "（数字写在不带代码的句子里）")

    rule_map = {str(p.get("code")): float(p.get("target_weight") or 0.0) * 100.0
                for p in plans}
    # 🔴 2026-11-05 04:5x（M-P 收尾）：比对必须用**实际落地权重**
    #    （`target_weight_effective`），不是**计划权重**（`target_weight`）。
    #    实测：511660/511990 计划权重 20%，但**整手买不起 ⇒ 权重归现金**
    #    ⇒ 实际落地 0%，而最终裁决也是 0%（不买）⇒ **两者其实一致**；
    #    若拿计划权重比 ⇒ 3 处真分歧被夸成 5 处 ⇒ 假警报。
    rule_eff_map = {str(p.get("code")): float(p.get("target_weight_effective")
                                              or 0.0) * 100.0 for p in plans}
    rule_cash_map = {str(p.get("code")): float(p.get("weight_to_cash") or 0.0) * 100.0
                     for p in plans}
    name_map = {str(p.get("code")): p.get("name", "") for p in plans}
    bull_map: dict[str, float] = {}
    for code, v in (llm_targets or {}).items():
        if isinstance(v, dict) and v.get("who") == "bull":
            bull_map[str(code)] = v.get("target_pct")
    bear_map: dict[str, float] = {}
    for c in llm_conflicts or []:
        for v in (c.get("all_divergent_views") or []):
            if v.get("who") == "bear":
                bear_map[str(c.get("code"))] = v.get("target_pct")

    rows: list[dict] = []
    for code, t in sorted((out["targets"] or {}).items()):
        rule_pct = rule_map.get(code)
        rule_eff = rule_eff_map.get(code)
        final_pct = t.get("target_pct")
        bull_pct = bull_map.get(code)
        bear_pct = bear_map.get(code)
        gap = None
        status = "无法比对"
        _cmp = rule_eff if rule_eff is not None else rule_pct
        if _cmp is not None and final_pct is not None:
            gap = round(float(final_pct) - float(_cmp), 4)
            status = "与规则层不一致" if abs(gap) > 2.0 else "与规则层一致"
        notes: list[str] = []
        if rule_pct is not None and rule_eff is not None and abs(rule_pct - rule_eff) > 0.01:
            notes.append(f"规则层计划 {rule_pct:.1f}% 但**整手买不起 ⇒ 实际落地 {rule_eff:.1f}%**"
                         f"（权重归现金 {rule_cash_map.get(code, 0.0):.1f}%）")
        if t.get("target_pct_source"):
            notes.append(f"目标权重来源：{t['target_pct_source']}")
        if t.get("shares_keep") is not None:
            notes.append(f"原文保留约 {t['shares_keep']} 股 / 卖出约 {t['shares_sell']} 股")
        if t.get("cap_pct") is not None:
            notes.append(f"硬上限 {t['cap_pct']}%")
        if t.get("floor_pct") is not None:
            notes.append(f"下限 {t['floor_pct']}%")
        if t.get("stop_loss") is not None:
            notes.append(f"价格硬止损 {t['stop_loss']}")
        if t.get("lots"):
            notes.append(f"原文写明买 {t['lots']} 手（{t['lots'] * 100} 份）")
        rows.append({
            "code": code,
            "name": name_map.get(code, ""),
            "final_target_pct": final_pct,
            "final_direction": t.get("direction"),
            "rule_target_pct": round(rule_pct, 4) if rule_pct is not None else None,
            "rule_effective_pct": round(rule_eff, 4) if rule_eff is not None else None,
            "bull_pct": bull_pct,
            "bear_pct": bear_pct,
            "gap_vs_rule_pp": gap,
            "status": status,
            "notes": notes,
            "evidence": t.get("evidence", ""),
        })
    # 🔴 2026-11-11（缺陷 D）：被 TA 一票否决的代码**单列一行**（不许静默消失）
    for r in (ta_rejected or []):
        code = str(r.get("code") or "").zfill(6)
        if not code or any(x["code"] == code for x in rows):
            continue
        _eff = rule_eff_map.get(code)
        _notes = [
            "🛡️ **TA 一票否决** ⇒ 本轮不建仓 / 不卖出（规则层目标作废 = 0%）",
        ]
        if _eff is not None and float(_eff) > 0:
            _notes.append(
                f"**仍在持仓** ⇒ 保留为『维持』席位（实际落地 = 当前权重 "
                f"{float(_eff):.1f}%，**不买不卖**）"
            )
        else:
            _notes.append("本轮**不在目标组合**里（实际落地 0%）")
        rows.append({
            "code": code,
            "name": r.get("name") or name_map.get(code, ""),
            "final_target_pct": None,
            "final_direction": None,
            "rule_target_pct": 0.0,
            "rule_effective_pct": round(float(_eff), 4) if _eff is not None else 0.0,
            "bull_pct": bull_map.get(code),
            "bear_pct": bear_map.get(code),
            "gap_vs_rule_pp": None,
            "status": "TA 一票否决（移出本轮候选）",
            "notes": _notes,
            "evidence": str(r.get("reason") or ""),
        })
    out["reconciliation"] = rows
    # 🔴 2026-11-11（缺陷 D）：因 TA 否决而**移出**冲突表的代码 ⇒ 显式标注
    out["ta_moved_out"] = list(ta_moved_out or [])
    diff = [r for r in rows if r["status"] == "与规则层不一致"]
    out["n_divergent"] = len(diff)
    out["summary_text"] = (
        f"最终裁决（{out.get('rating') or '未给评级'}）与规则层**{len(diff)} 处不一致**"
        + ("：" + "；".join(
            f"{r['code']} 最终 {r['final_target_pct']}% vs 规则层**实际落地** "
            f"{r.get('rule_effective_pct')}%（差 {r['gap_vs_rule_pp']:+.1f}pp）" for r in diff)
           if diff else "")
    )
    return out


def _llm_targets(decision_md: str | None, total_assets: float | None = None,
                  account_weights: dict[str, float] | None = None) -> dict:
    """每个代码的**主视图**（**最终裁决优先**，其次研究经理 / 多头 / 空头首次出现）。

    🔴 2026-11-04 22:5x（M-C）：「最终交易决定」是 Trader 辩论后的**最终裁决**，
    权威性高于研究经理 ⇒ 排到最前；其余说话人保持 research > bull > bear 的相对次序
    （未知说话人排最后）。
    """
    views = _llm_target_views(decision_md, total_assets, account_weights)
    # 🔴 M-F3：优先级改用**单一事实源** `_LLM_WHO_ORDER`（见其注释）⇒
    #    风险三方不再与「未知说话人」同档，新增说话人也不会静默降级。
    out: dict[str, dict] = {}
    for code, lst in views.items():
        out[code] = sorted(lst, key=lambda v: _llm_who_rank(v["who"]))[0]
    return out


#: `_decision_failure_reason` 的读盘缓存：**运行内快照**语义。
#: 🔴 2026-11-04 22:3x 实测（M-B①）：一次 build_plan 里它被调 **8 次**
#:    （meta 里 3 次 + `_llm_staleness` 1 次 + `_decision_text` 2 次 + `_decision_view` 1 次），
#:    每次重读 md(48KB) + json(107KB) ⇒ **累计重复读盘 1,214.6 KB**。
#:    更危险的是：meta 的三个字段（`source_decision` / `llm_decision_status` / `llm_decision_error`）
#:    **各读一次** ⇒ 决策文件在运行中被改写时，**同一份产物里三个字段会自相矛盾**
#:    （status=ok 却 error=失败 / source_decision 有值却 status=failed）。
#: 🔴 2026-11-04 22:5x 实测**推翻了「按 (mtime_ns, size) 失效」的设计**：
#:    本机持久盘（落点根所在盘）的 mtime 粒度约 **4ms**，且**同大小改写时 mtime 与 size 都不变**
#:    （连写 3 次只得 2 个不同 mtime_ns）⇒ 该键会返回**旧判定**，比不缓存更危险。
#: ⇒ 改成「**运行内快照**」：key = 决策件绝对路径（不跟随软链，用 `absolute()`）；
#:    **F5 判定**（`_decision_failure_reason`）一次构建内**只读一次盘、判定恒定**；
#:    下一次构建开头调 `reset_decision_failure_cache()` 清缓存 ⇒ 新构建必读新盘。
#:    语义 = 「**本次运行看到的状态**」，不是「文件此刻的状态」。
#: 🔴 2026-11-04 夜实测修措辞（M-F3；reviewer 指出「只读一次盘」**言过其实**）：
#:    本缓存**只覆盖 F5 判定**，**不覆盖正文内容**。实测一次 `build_plan`：
#:    决策 md 读 **2** 次 + 决策 json 读 **5** 次 = **7** 次（整轮读盘 11 次）⇒
#:    「一次构建只读一次盘」只对**判定**成立，对**正文**不成立。
#:    ⇒ 这里只改措辞、不动实现：正文读盘被 `_decision_text` / `_decision_view` /
#:      `_llm_target_views` 共用，改成缓存会动到**冲突表解析**这条判定链
#:      （风险高于收益 ⇒ 留待白天空窗期专项处理，且必须重跑 TA 复核）。
_DECISION_FAILURE_CACHE: dict[str, "str | None"] = {}


def reset_decision_failure_cache() -> None:
    """清空 F5 判定缓存（= **运行内快照的边界**）。

    🔴 每次 `build_plan` 开头都要调：否则上一轮构建的判定会被这一轮沿用
    ⇒ 决策文件在两次构建之间被改写时，本轮**看不到**新内容（判定陈旧）。
    """
    _DECISION_FAILURE_CACHE.clear()


def _decision_failure_reason(decision_path: str | Path | None) -> str | None:
    """判断决策文件是不是「**失败占位**」⇒ 也就是**没有**有效的 LLM 第二意见。

    🔴 F5（2026-11-04 21:0x 实测，E2E 首跑暴露）：
    `run_tradingagents.py` 失败时**照样会写一份** `tradingagents_decision.md`，
    内容是「**未完成** + 异常摘要」⇒ 于是「决策文件存在」被当成了「有 LLM 意见」。
    后果（实测）：`meta.llm_stale.llm_present=true`、`source_decision=<失败占位路径>`、
    notes 里写着「TradingAgents 结论是第二意见」⇒ **交付件谎称有 LLM 第二意见**，
    而冲突表却是 0 条 ⇒ 规则层与 LLM「无分歧」的假象。
    ⇒ 修法：失败占位一律判为「**无 LLM 意见**」，并把失败原因显式写进产物。

    返回：失败原因（字符串）；**不是**失败占位或文件不存在时返回 None。

    🔴 2026-11-04 22:5x（M-B①）：本函数走「**运行内快照**」缓存
    （key = 决策件绝对路径；一次构建内只读一次盘、判定恒定）。
    ⇒ 同一次运行内多次调用**结果必然一致**（原来 8 次各读各的 ⇒ 文件被改写会自相矛盾）。
    ⇒ 跨构建的新鲜度由 `reset_decision_failure_cache()` 保证（`build_plan` 开头调用）。
    """
    if not decision_path:
        return None
    path = Path(decision_path)
    if not path.exists():
        return None
    # 🔴 用 `absolute()` 而非 `resolve()`：`resolve()` 会跟随软链 ⇒ 同一份决策件
    #    经由软链与真实路径会得到两个 key ⇒ 快照被绕过（又变成多次读盘）。
    cache_key = str(path.absolute())
    if cache_key in _DECISION_FAILURE_CACHE:
        return _DECISION_FAILURE_CACHE[cache_key]
    reason = _decision_failure_reason_uncached(path)
    _DECISION_FAILURE_CACHE[cache_key] = reason
    return reason


def _decision_failure_reason_uncached(path: Path) -> str | None:
    """真正判定的版本（**不走缓存**）；缓存逻辑见 `_decision_failure_reason`。"""
    # ① 优先看结构化件（status 字段是权威口径）
    json_path = path.with_suffix(".json")
    if json_path.exists():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").strip()
            if status and status != "ok":
                err = str(payload.get("error") or "").strip()
                return f"status={status}" + (f"（{err[:160]}）" if err else "")
    # ② 回退看展示件里的「未完成」标记
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    # 🔴 2026-11-04 21:5x reviewer 复现：① 零字节文件走不到任何分支 ⇒ 返回 None
    #    ⇒ `_llm_staleness` 置 llm_present=True ⇒ **谎称有第二意见 + 冲突表 0 条**
    #    （= F5 要消灭的假象，且 md 与 json 自相矛盾）⇒ 空文件必须判失败。
    stripped_text = text.strip()
    if not stripped_text:
        return "决策文件为空（0 字节）"
    # 🔴 ②「未完成」必须**锚定在首行标题**上，不做全文子串匹配：
    #    否则正常辩论正文里偶然出现加粗的「未完成」会把整份意见静默丢弃（反方向的漏冲突）。
    #    ③ 兼容「- 异常：」这种占位件的结构特征（run_tradingagents.py 的失败写法）。
    first_line = stripped_text.splitlines()[0].strip()
    head_marked = "未完成" in first_line
    has_exc_line = any(
        line.strip().startswith("- 异常：") for line in stripped_text.splitlines()
    )
    if head_marked or has_exc_line:
        for line in stripped_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- 异常："):
                return stripped.split("：", 1)[1].strip().strip("`")[:160]
        return "决策文件标记为未完成"
    return None


def _sha1_file(path: Path) -> str:
    import hashlib
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _context_equivalent(src: Path, cur: Path) -> bool:
    """两个 `ta_research_context.md` **内容是否等价**。

    🔴 2026-11-11 修（假 stale 的根因）：context 正文里嵌了**本快照的绝对路径**
       （末行「完整报告：…/snapshots/<快照号>/etf_analysis_report.md」）⇒
       **同一个数据源**在两个快照里生成的 context **只差这一行** ⇒ sha1 必然不同。
    ⇒ 比对时**只把各自的快照目录归一化成占位符**，其余**逐字**比。
    ⚠️ 归一化**只限「自己的快照目录」这一段** ⇒ 任何其它内容差异（分数变了、
       结论变了……）都判不等价 ⇒ 该报 stale 还是报 stale。
    """

    def _norm(path: Path) -> str:
        txt = Path(path).read_text(encoding="utf-8")
        return txt.replace(str(Path(path).absolute().parent), "<SNAPSHOT>")

    try:
        return _norm(src) == _norm(cur)
    except OSError:
        return False


def _llm_staleness(args) -> dict:
    """检查 LLM 意见是不是**基于当前这版报告**跑出来的。

    🔴 2026-11-04 夜实测事故：`run_pipeline.sh` 用 `--skip-ta` 重跑时，
    第 7 步照样拿**上一轮遗留**的 `tradingagents_decision.md` 生成最终计划
    ⇒ 交付件 = 新规则层报告 + 旧上下文推出的 LLM 意见。
    旧 context 里还写着「回测产物不可得，当前回撤位置也未知」，而当前报告明明写了
    MDD −32.29% / 期末回撤 −23.47% ⇒ **那句话是假的，却嵌在交付件里**。
    ⇒ 修法：用 sha1 链路（报告 md → context md → 决策）逐环校验，不一致就标 stale。
    """
    # 🔴 F2（2026-11-04 夜，专题档 §20.4）：「决策文件**缺失**」与「决策**过期**」是两回事，
    #    原实现对缺失直接早退 `{"stale": false}` ⇒ 交付件里 `llm_stale=false` 会被误读成
    #    「LLM 意见是新鲜的」，而实际是**根本没有 LLM 意见**（且无任何说明）。
    #    ⇒ 缺失时置 `llm_present=False` + `absent_reason`，由 build_plan 显式写进 notes/warnings。
    out = {"stale": False, "reasons": [], "llm_present": False, "absent_reason": None}
    dec = Path(args.decision) if args.decision else None
    if dec is None or not dec.exists():
        out["absent_reason"] = (
            f"未找到 LLM 决策文件（{args.decision or '未指定'}）⇒ 本计划**不含任何 LLM 第二意见**"
            "（冲突表为空是正常的，不是解析失败）"
        )
        return out
    # 🔴 F5：文件**存在**不等于「有 LLM 意见」——失败占位（status!=ok / 「未完成」）⇒
    #    一律按「**没有 LLM 意见**」处理（llm_present=False + absent_reason），
    #    并且**不**进 sha1 链校验（没有意见 ⇒ 无所谓新旧）。
    fail_reason = _decision_failure_reason(dec)
    if fail_reason:
        out["llm_present"] = False
        out["absent_reason"] = (
            f"LLM 决策文件存在但**标记为未完成**（{fail_reason}）"
            "⇒ 本计划**不含任何 LLM 第二意见**（冲突表为空是正常的，不是解析失败）"
        )
        return out
    out["llm_present"] = True
    # 🔴 2026-11-04 22:3x 实测修（M-B②）：上下文三件套（`ta_context_meta.json` /
    #    `ta_research_context.md` / `tradingagents_decision.json`）必须**跟着决策文件走**，
    #    不能跟着 `--out-dir` 走。旧实现一律用 out_dir ⇒ 当 `--decision` 指向**另一个目录**时：
    #    ① 报告→context 那一环**根本没校验**（meta 在别的目录）；
    #    ② 或者更糟：拿 out_dir 里**上一轮遗留**的 decision.json / context 去校验
    #       **另一个目录**的决策 ⇒ **校验对象错位，还报「通过」**。
    #    ⇒ 修法：三者都取「决策文件所在目录」（resolve 后的绝对路径）。
    #    ⚠️ 正常流水线里 `--decision` 与 `--out-dir` 同目录 ⇒ 行为**逐字节不变**。
    #    ⚠️ **不能用 `resolve()`**：它会跟随符号链接 ⇒ 复跑目录里指向基线的决策软链
    #    会被解析回**基线目录** ⇒ 链校验又对回基线的上下文去了（2026-11-04 22:4x 实测：
    #    可复现性探针直接报「9 处实质差异」—— 复跑件被判 stale 而基线不 stale）。
    #    ⇒ 用 `absolute()`（**不跟随软链**）⇒ 取「调用方传入路径所在的目录」。
    ctx_dir = dec.absolute().parent
    meta_path = ctx_dir / "ta_context_meta.json"
    # 🔴 2026-11-09 15:55 实测修（带 LLM 首跑暴露）：
    #    决策 JSON 的名字**必须跟着决策文件走**（上面那段注释自己就要求
    #    「三件套跟着决策文件走」）⇒ 历史上存在过一份**不同名**的决策件
    #    （为的是不覆盖根目录金标 `tradingagents_decision.md`）
    #    ⇒ 旧代码硬编码 `tradingagents_decision.json`
    #    ⇒ 三件套**永远缺件** ⇒ sha1 链**永远验不了**
    #    ⇒ 明明是**本轮刚跑出来**的 LLM 第二意见，被判成「已过期 ⇒ 不可作数」
    #    （实测：2026-11-09 15:55 的影子期计划里 `llm_stale.unverifiable_reason`
    #      = "上下文三件套**缺件** ['tradingagents_decision.json']"，
    #      冲突表 5 条**全部**被标 stale ⇒ LLM 第二意见**整个作废**）。
    #    ⇒ 修法：由决策 md 推出决策 json（`<stem>.json`，与
    #      `_decision_failure_reason_uncached` 的 `path.with_suffix(".json")` 同口径）
    #      ⇒ **生产路径逐字节不变**（`tradingagents_decision.md` → 同名 json）。
    dec_json = dec.with_suffix(".json")
    ctx_path = ctx_dir / "ta_research_context.md"
    report_md = Path(args.report).with_suffix(".md")
    # 🔴 2026-11-04 夜实测修（M-F4；reviewer 的 I-6，本机复现）：
    #    旧实现两段校验都写成 `if <件存在> and <件存在>:` ⇒ **件缺失时整段被跳过**，
    #    既不报错、也不进 reasons ⇒ `stale` 保持 False ⇒ 交付件里写的是
    #    「LLM 意见未过期」—— 但真实情况是「**根本没校验过**」
    #    ⇒ **不确定被当成确定**（与 `verify_artifacts.py` 在 M-F2 里已堵掉的
    #      「无法校验却判 PASS」是**同一个洞**，只是发生在计划侧）。
    #    ⇒ 修法：缺件 ⇒ 走「**未通过校验**」分支：`stale=True` + `unverifiable_reason`，
    #      与「已过期」区分开：
    #        absent_reason      = 没有 LLM 意见（文件不存在/失败占位）⇒ 不是过期问题；
    #        unverifiable_reason = 有 LLM 意见，但 sha1 链**缺件 ⇒ 验不了**；
    #        reasons（已过期）  = 有 LLM 意见，链**验过了 ⇒ 确实过期**。
    #    ⚠️ 键**按需出现**（不在基础字典里预置）⇒ 正常路径下 `llm_stale` 结构
    #      与改造前**逐字节一致** ⇒ 正式产物 sha1 不变。
    _missing = []
    if not meta_path.exists():
        _missing.append("ta_context_meta.json")
    if not ctx_path.exists():
        _missing.append("ta_research_context.md")
    if not dec_json.exists():
        _missing.append(dec_json.name)
    if not report_md.exists():
        _missing.append("etf_analysis_report.md")
    if _missing:
        out["stale"] = True
        out["verifiable"] = False
        out["unverifiable_reason"] = (
            f"上下文三件套**缺件** {sorted(_missing)}（目录 {ctx_dir}）"
            "⇒ **无法校验** sha1 链（报告 md → context md → 决策）"
            "⇒ 不许断言「LLM 意见未过期」（不确定 ≠ 确定新鲜）"
        )
        out["reasons"].append(out["unverifiable_reason"])
    if meta_path.exists() and report_md.exists():
        try:
            meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
            # 🔴 2026-11-04 夜实测修（M-G / F4，专题档 §21.4）：
            #    第一环**优先**按「**报告内容指纹**」判（= 报告 JSON 去掉易变 meta 后的规范化 sha1）。
            #    理由：喂给 LLM 的 research_context 是由报告 **JSON** 的压缩版生成的
            #    ⇒ 「生成时间/指纹表/主机/解释器版本」这类元数据变化**进不了上下文**
            #    ⇒ context sha1 不变 ⇒ 旧决策依然成立 ⇒ 按 raw md sha1 判是**假阳性 stale**。
            #    ⚠️ **旧格式回落**：meta 没有 `report_content_sha1` ⇒ 用原来的 raw md sha1
            #    （向后兼容，不把老产物打成「不可校验」）。
            _content_old = meta_payload.get("report_content_sha1")
            if _content_old:
                _content_now = report_content_sha1_from_file(Path(args.report))
                if _content_now is None:
                    out["stale"] = True
                    out["reasons"].append(
                        "🔴 **无法计算报告内容指纹**（报告 json 缺失/损坏）"
                        "⇒ **未通过**新鲜度校验（算不出来 ≠ 一致）"
                    )
                elif _content_now != _content_old:
                    out["stale"] = True
                    out["reasons"].append(
                        f"context 是基于**旧版分析报告**生成的"
                        f"（报告内容指纹 {str(_content_old)[:8]} → 现 {_content_now[:8]}）"
                    )
            else:
                _old = meta_payload.get("report_md_sha1")
                if _old and _old != _sha1_file(report_md):
                    out["stale"] = True
                    out["reasons"].append(
                        f"context 是基于**旧版分析报告**生成的"
                        f"（报告 md sha1 {str(_old)[:8]} → 现 {_sha1_file(report_md)[:8]}）"
                    )
        except Exception as exc:
            # 🔴 2026-11-04 21:5x reviewer：旧代码 `except Exception: pass`
            #    ⇒ context meta 损坏/截断时**从未校验过**，却仍断言「LLM 意见未过期」。
            #    ⇒ 修法：校验失败就**如实说没校验成功**（标 stale），不确定就说不确定。
            out["stale"] = True
            out["reasons"].append(
                f"无法校验 context 新鲜度（{type(exc).__name__}: {exc}）⇒ **未通过**新鲜度校验"
            )
    if dec_json.exists() and ctx_path.exists():
        try:
            _d = json.loads(dec_json.read_text(encoding="utf-8"))
            _cs = ((_d.get("meta") or {}).get("context_sha1")) or ""
            if _cs and _cs != _sha1_file(ctx_path):
                # 🔴 2026-11-11 修（**假 stale**：跨快照复用决策件 ⇒ 路径不同 ≠ 内容过期）：
                #    `--skip-llm` 复用**另一个快照**的决策件时，新快照的 context 与
                #    决策件当初吃到的 context **只差「快照路径行」**（实测两文件 diff 仅 1 行）
                #    ⇒ sha1 必然不同 ⇒ 旧口径把「路径不同」当成「内容过期」
                #    ⇒ 交付件**谎报**「LLM 第二意见已过期 ⇒ 不可作数」
                #    （实测：把 512800 那条**真冲突**整条标成作废 ⇒ 交付件自相矛盾）。
                #    ⇒ 修法：**逐字比对**决策件 meta 里记录的**上游 context**
                #      （`meta.context_path`，且它的 sha1 必须等于决策件记录的 `context_sha1`
                #        ⇒ 证明「这就是当初喂给 LLM 的那份」）与本快照 context
                #      （**只把各自的快照目录**归一化成占位符）⇒ 内容一致 ⇒ **不算过期**。
                #    ⚠️ **不伪造 sha1、不放宽「内容变了必须报过期」**：任何非路径差异仍判 stale。
                _src_ctx = str(((_d.get("meta") or {}).get("context_path")) or "")
                _equivalent = False
                if _src_ctx:
                    try:
                        _src_p = Path(_src_ctx)
                        if _src_p.exists() and _src_p.is_file() and _sha1_file(_src_p) == _cs:
                            if _context_equivalent(_src_p, ctx_path):
                                _equivalent = True
                                out["context_source_path"] = str(_src_p)
                                out["context_source_snapshot"] = _src_p.parent.name
                                out["context_equivalence_note"] = (
                                    f"决策件来自快照 **{_src_p.parent.name}**；"
                                    f"本快照 context 与其**逐字一致**（仅快照路径行不同）"
                                    f"⇒ **未过期**（内容未变）"
                                )
                    except OSError:
                        _equivalent = False
                if not _equivalent:
                    out["stale"] = True
                    out["reasons"].append(
                        f"决策文件是基于**旧版 research_context** 跑出来的"
                        f"（context sha1 {str(_cs)[:8]} → 现 {_sha1_file(ctx_path)[:8]}）"
                    )
            elif not _cs and dec.stat().st_mtime < ctx_path.stat().st_mtime:
                # 旧版决策文件没有 context_sha1 ⇒ 退化成时间戳比对
                out["stale"] = True
                out["reasons"].append(
                    f"决策文件（{pd.Timestamp.fromtimestamp(dec.stat().st_mtime):%m-%d %H:%M}）"
                    f"早于 research_context（{pd.Timestamp.fromtimestamp(ctx_path.stat().st_mtime):%m-%d %H:%M}）"
                    "⇒ 它不是基于当前上下文跑出来的"
                )
        except Exception as exc:
            out["stale"] = True
            out["reasons"].append(
                f"无法校验决策与 research_context 的 sha1 链路（{type(exc).__name__}: {exc}）"
                "⇒ **未通过**新鲜度校验"
            )
    return out


#: 🛡️ TA 合规筛选：**顺延时跳过同族**的开关（生产恒为 True）。
#:    ⇒ 探针用它做**变异负例**（关掉它 ⇒ 顺延会选到同族标的 ⇒ 断言必须转红）。
_SKIP_SAME_THEME_ON_SUBSTITUTE = True

#: 🛡️ TA 合规筛选：**顺延时跳过规则层冲突对**的开关（生产恒为 True）。
#:    ⇒ 同上，探针用它做变异负例。
_SKIP_CONFLICT_ON_SUBSTITUTE = True

#: 🔴 2026-11-11（审计缺陷 A · 席位交互）：TA 合规筛选后的**权益上限**
#:    = 1 − 现金预留比例（单一事实源 = `config.CASH_RESERVE_RATIO`；**不写死 0.2**）。
#:    「被否决且在持仓」的那一行**仍占席位**（保留为『维持』）⇒ 若再顺延补位，
#:    就是**多出一席** ⇒ 权益可能超配 ⇒ 补位前必须过这道闸。
_EQUITY_CAP = 1.0 - float(C.CASH_RESERVE_RATIO)


#: 🔴 2026-11-11（Beta 避险落地）：TA 的**权益上限压降建议**在规则层的落地口径。
#:    ① **只认目标动词**（`llm_final.post_trade.equity_target_pct`）⇒ 陈述句
#:       （「组合层权益约 56%」）**不算建议**；
#:    ② **只许压降、不许抬高** ⇒ 建议值 ≥ 规则层硬上限 ⇒ **忽略**；
#:    ③ 解析不出 ⇒ **完全保持原行为**（不压降，产物逐字节不变）。
_TA_CAP_REASONS = (
    "no_ta_advice",        # 没有决策文件 / 决策不可用
    "not_a_target",        # 有决策，但没读到「目标动词」口径的权益上限
    "out_of_range",        # 数字越界（不在 0~100）
    "at_or_above_base_cap",  # 建议值 ≥ 规则层硬上限 ⇒ 忽略（只许压降）
    "accepted",            # **采纳**：压降权益上限
)


def _resolve_ta_equity_cap(llm_raw: dict | None) -> dict:
    """🔴 2026-11-11（Beta 避险落地）：解析 TA 的**组合权益上限压降建议**。

    文档口径（「三.3 极端行情下的仓位建议」）：TA 可以建议**压降组合权益上限**，
    其余资金留现金 ⇒ 规则层**采纳**这个建议（否则 TA 的建议只是「披露」，不是「建议」）。

    返回 ``{"pct": float|None, "evidence": str, "reason": str}``：
      * ``pct``   = TA 建议的**权益上限**（0~1 小数）；解析不出 ⇒ ``None``
                    （⇒ 调用方**完全保持原行为**）。
      * 🔴 **输入单位 = 百分点**（`post_trade.equity_target_pct = 36.0` 表示 **36%**），
        与 `llm_final` 写出的口径**一致**；小数形式（0.36）⇒ 判 ``out_of_range``（**不静默压降**）。
      * ``evidence`` = 原文证据句（可回放；没有就是空串，**不编造**）。
      * ``reason`` ∈ ``_TA_CAP_REASONS``。
    """
    out: dict = {"pct": None, "evidence": "", "reason": "no_ta_advice"}
    if not isinstance(llm_raw, dict) or not llm_raw.get("available"):
        return out
    pt = llm_raw.get("post_trade") or {}
    if not isinstance(pt, dict):
        return out
    raw_pct = pt.get("equity_target_pct")
    if raw_pct is None:
        out["reason"] = "not_a_target"
        return out
    try:
        pct = float(raw_pct)
    except (TypeError, ValueError):
        out["reason"] = "out_of_range"
        return out
    # 🔴 2026-11-11（审计缺陷 C）：**输入单位 = 百分点**（36.0 表示 36%）。
    #    小数形式（0.36）**无法与「0.36%」区分** ⇒ 一律判越界（**不许**静默压降 100 倍）。
    if not (0.0 <= pct <= 100.0) or 0.0 < pct < 1.0:
        out["reason"] = "out_of_range"
        return out
    out["evidence"] = str(pt.get("equity_target_pct_evidence") or "")
    cap = pct / 100.0
    out["pct"] = cap
    out["reason"] = "accepted" if cap < _EQUITY_CAP - 1e-12 else "at_or_above_base_cap"
    return out


def apply_ta_screening(
    *,
    actions: list[dict] | None = None,
    candidates: list[dict] | None = None,
    rejections: list[dict] | None = None,
    held_codes: list[str] | None = None,
    current_weights: dict[str, float] | None = None,
    holdings=None,
    prices=None,
    asof=None,
    names: dict[str, str] | None = None,
    equity_cap: float | None = None,
    universe_codes: set[str] | None = None,
    official_names: dict[str, str] | None = None,
) -> dict:
    """🛡️ TA 合规筛选：**剔除**被 TA 点名否决的候选 + **顺延**补位。

    🔴 **只改副本**：返回的是**新的** action 列表，调用方的原对象一字不动。

    口径
    ----
    ① **剔除**：被 TA 点名否决的代码，其 action 从候选 action 列表里**移除**
       （**不是**「卖出」⇒ 目标权重**不写 0**）。
       🔴 2026-11-11（审计缺陷 A）：若它本来就在**持仓**里 ⇒ 那一行**不许消失**
       （旧行为只记 `held_not_touched=true`，行本身从计划里彻底消失 ⇒ 市值凭空蒸发
       ⇒ 权益被低估 ⇒ 「权益 ≤ 1−现金预留比例」的闸门拿偏小的数去判 ⇒ 闸门可被绕过）。
       ⇒ 改记进 `ta_screening["orphaned_holdings"]`（**被否决且在持仓的代码列表**）
       + `ta_screening["orphaned_holding_rows"]`（逐只明细：当前权重 / 席位预算），
       由 `build_plan` 落成一行「**维持**」（目标权重 = **当前权重**（不是 0）、
       `ta_rejected=True`、`held_not_touched=True`）⇒
         - 它**计入**权益/现金口径 ⇒ 计划权益/现金/总资产恒等式**自洽**；
         - 它**不进**买入/卖出挂单（目标股数 = 当前股数 ⇒ 既不买也不卖）。
       ⚠️ 返回的 `actions` 里**不含**它（剔除 ≠ 权重 0：被否决者不得留在候选 action 列表里）。
    ② **顺延**：从候选池里按 `final_score` 降序找**第一个**合格者补位，
       合格条件**全部**满足：
         - 不在否决名单里；
         - `selected` 为 False（原本没入选）；
         - `broken` / `entry_ma_ok` / `above_ma60` / **覆盖闸门** 全部通过；
           - 🔴 2026-11-11（审计缺陷 B）：覆盖闸门原先判 `c.get("coverage_ok")`，
             而报告候选字典（`analyze.py::_build_candidates`）**只有 `coverage`、
             **没有** `coverage_ok` ⇒ 该合格条件**永不触发**（死代码）⇒
             改用 `coverage` 与 **`C.MIN_E_COVERAGE`**（既有常量，语义 = E 的最低覆盖率）
             比较：`coverage` **存在但为空/算不出** ⇒ **按不达标处理（fail-closed）**；
             `coverage` **低于阈值** ⇒ 不达标；`coverage` 字段**不存在** ⇒ 该闸门
             **不适用**（与其它闸门同口径：字段不存在 ⇒ 视为通过，不编数据；
             报告口径下 `coverage` **恒存在** ⇒ 生产链路上覆盖闸门**始终生效**）。
         - 与「剔除后仍入选的标的集合」**不构成规则层冲突对**
           （重叠 ≥ 0.90 或 120 日相关性 ≥ 0.95 ⇒ 跳过）；
         - 与「剔除后仍入选的集合」**不同族**（`theme_id` 相同 ⇒ 跳过），
           **且**与**被否决者**也不同族（否则「否决一只银行 ETF、换来另一只银行 ETF」
           ⇒ 否决形同虚设）；
         - 该族未达 `C.MAX_PER_THEME`；
         - 🔴 2026-11-11（缺陷 A · 席位交互）：补位后**权益合计不得突破**
           `1 − CASH_RESERVE_RATIO`（被否决且在持仓者**仍占席位** ⇒ 再补一席 = 多一席）。
    ③ 顺延者的**目标权重 = 被否决者的目标权重**（保持等权口径）。
    ④ 找不到合格顺延者 ⇒ **不强行补**（宁缺毋滥）⇒ 记 `no_substitute_reason`。

    参数 ``equity_cap``
        本轮**生效的权益上限**（默认 = 规则层硬上限 ``1 − 现金预留比例``）。
        🔴 2026-11-11（Beta 避险）：当 TA 明确建议压降组合权益时，调用方把
        ``min(规则层上限, TA 建议)`` 传进来 ⇒ 顺延补位也必须过这道闸
        ⇒ **不许**出现「上限被压到 36%，却顺延补位到 80%」的自相矛盾。

    参数 ``universe_codes`` / ``official_names``
        🔴 2026-11-11（真机缺陷 ①②）：
        - ``universe_codes`` = **池内白名单**（来自 ``--universe`` 的代码集）
          ⇒ **池外代码**（LLM 笔误 / 幻觉出来的代码）**不得**进入 ``ta_screening.rejected``
          （它不是候选池的标的 ⇒ 否决它没有意义）⇒ 改进 ``rejected_out_of_universe`` 如实披露。
          ⚠️ 白名单**不可用**（None / 空集）⇒ **一个都不剔除**（fail-closed ⇒ 不放过真否决）。
        - ``official_names`` = **官方名**（``--universe`` 的 ``name`` 列）⇒ 名称**官方优先**
          （LLM 文本里抽出来的垃圾名不许盖住官方名）。
        ⚠️ 这两个参数**不传** ⇒ 行为与旧版**完全一致**（向后兼容）。

    返回 ``{"actions": [...], "ta_screening": {...}}``。
    """
    #: 本轮**生效**的权益上限（Beta 避险 ⇒ 可低于规则层硬上限；**永不低于**调用方给的硬上限口径）
    _cap = float(equity_cap) if equity_cap is not None else _EQUITY_CAP
    acts = [dict(a) for a in (actions or [])]
    cands = [dict(c) for c in (candidates or [])]
    # 🔴 2026-11-11（真机缺陷 ①）：**池外代码剔除**（fail-closed + 如实披露）
    #    ⇒ LLM 笔误出来的池外代码（如 566620）**不得**进入 `rejected` / `rejected_codes`
    #      （否则污染顺延逻辑与产物级闸门）⇒ 改进 `rejected_out_of_universe` 披露。
    rejected, rejected_out_of_universe = _split_rejections_by_pool(
        rejections or [], universe_codes)
    rejected_codes = {str(r.get("code") or "").zfill(6) for r in rejected if r.get("code")}
    #: 🔴 2026-11-11（真机缺陷 ②）：**官方名**（`--universe` 的 name 列）⇒ 名称官方优先。
    #:    ⚠️ **不污染** `name_map`：它要喂 `overlap_mod.compute_overlap_pairs(names=...)`
    #:    做**名称族**判定 ⇒ 塞官方名进去会改变 theme 判定 ⇒ 只另开一张表。
    official: dict[str, str] = {
        str(k): str(v) for k, v in (official_names or {}).items() if str(v or "").strip()
    }
    held = {str(c).zfill(6) for c in (held_codes or [])}
    cw = {str(k).zfill(6): (_f(v, 0.0) or 0.0) for k, v in (current_weights or {}).items()}
    #: 规则层原始动作行（按代码索引）⇒ 用来取「被否决者的席位预算 / 原目标权重」
    rule_action: dict[str, dict] = {}
    for a in acts:
        _c = str(a.get("code") or "").zfill(6)
        if _c:
            rule_action[_c] = a

    def _cur_weight(code: str) -> float:
        """该代码的**当前权重**（小数口径）⇒ 维持现仓时目标权重就取它。

        取数顺序：`current_weights` 参数（账户真值，来自持仓诊断）→ 规则层动作行的
        `current_weight` → 0.0（**拿不到就不编数字**，退化成 0 ⇒ 维持行权重为 0）。
        """
        w = _f(cw.get(code))
        if w is None:
            w = _f(rule_action.get(code, {}).get("current_weight"))
        return float(w or 0.0)

    cand_by_code: dict[str, dict] = {}
    for c in cands:
        code = str(c.get("code") or "").zfill(6)
        if code:
            cand_by_code[code] = c

    name_map: dict[str, str] = {str(k): str(v) for k, v in (names or {}).items()}
    for code, c in cand_by_code.items():
        if c.get("name"):
            name_map.setdefault(code, str(c.get("name")))
    for a in acts:
        code = str(a.get("code") or "").zfill(6)
        if a.get("name"):
            name_map.setdefault(code, str(a.get("name")))

    def _theme_of(code: str) -> str:
        c = cand_by_code.get(code)
        if c and c.get("theme_id"):
            return str(c.get("theme_id"))
        return code

    def _gate_ok(c: dict) -> tuple[bool, str]:
        """破位 / 入场 MA / MA60 / **覆盖**：逐项判定 ⇒ 返回 (是否合格, 不合格原因)。

        🔴 2026-11-11（审计缺陷 B）：覆盖闸门原先判 `c.get("coverage_ok")`，
        而报告候选字典（`analyze.py::_build_candidates`）**只有 `coverage`、没有
        `coverage_ok`** ⇒ 该合格条件**永不触发**（死代码）⇒ 顺延池里 coverage
        不达标的标的照样能被选中。
        ⇒ 改用 `coverage` 与既有常量 **`C.MIN_E_COVERAGE`**（语义 = E 的最低覆盖率）
        直接比较（**不新造语义不同的常量**）：
          - `coverage` **存在但为空 / 算不出** ⇒ **不达标（fail-closed）**
            （E 根本算不出来 ⇒ 分数不可信）；
          - `coverage` **低于阈值** ⇒ 不达标；
          - `coverage` 字段**不存在** ⇒ 该闸门**不适用**（与 `broken` /
            `entry_ma_ok` / `above_ma60` 同口径：字段不存在 ⇒ 视为通过，不编数据）；
            报告口径下 `coverage` **恒存在** ⇒ 生产链路上覆盖闸门**始终生效**。
        """
        if bool(c.get("broken")):
            return False, "已破位（跌破入场均线）"
        if c.get("entry_ma_ok") is False:
            return False, "入场均线过滤不通过"
        if c.get("above_ma60") is False:
            return False, "仍在 MA60 之下"
        if "coverage" in c:
            cov = _f(c.get("coverage"))
            if cov is None:
                return False, "coverage 缺失（E 算不出来）⇒ 按不达标处理"
            if float(cov) < float(C.MIN_E_COVERAGE):
                return False, (
                    f"coverage {float(cov):.0%} < 阈值 {float(C.MIN_E_COVERAGE):.0%}"
                    " ⇒ 覆盖不达标"
                )
        return True, ""

    def _sort_key(c: dict) -> tuple[int, float]:
        v = _f(c.get("final_score"))
        if v is None:
            return (1, 0.0)          # 分数算不出来 ⇒ 排最后
        return (0, -float(v))

    # ---- ① 剔除：被否决的 action 从候选列表里移除（**不是卖出**）----
    kept: list[dict] = []
    dropped: list[dict] = []
    for a in acts:
        code = str(a.get("code") or "").zfill(6)
        if code in rejected_codes:
            dropped.append(a)
            continue
        kept.append(a)

    # ---- 🔴 2026-11-11（缺陷 A）：被否决 ∩ 在持仓 ⇒「孤儿持仓」----
    #    旧行为：那几行**从计划里彻底消失** ⇒ 它们的市值在口径里凭空蒸发
    #    ⇒ 计划权益被低估 ⇒「权益 ≤ 1−现金预留比例」的闸门拿偏小的数去判 ⇒ 可被绕过。
    #    ⇒ 现在：**保留成一行「维持」**（目标权重 = **当前权重**，不是 0），
    #      由 `build_plan` 落进计划 ⇒ 计入权益/现金口径，但**不进**买卖挂单。
    orphan_codes = sorted(rejected_codes & held)
    orphan_rows: list[dict] = []
    for _code in orphan_codes:
        _a = rule_action.get(_code, {})
        _w = _cur_weight(_code)
        orphan_rows.append({
            "code": _code,
            "name": _official_name(_code, official,
                                   _a.get("name") or name_map.get(_code) or ""),
            # 维持现仓 ⇒ 目标权重 = **当前权重**（账户真值），不是 0
            "current_weight": _w,
            "target_weight": _w,
            # 规则层原本给它分配的席位预算（披露用：当前权重超出它 ⇒ 超配席位）
            "rule_target_weight": float(_f(_a.get("target_weight"), 0.0) or 0.0),
            "reason": (
                "🛡️ TA 一票否决 ⇒ 保持现仓不卖出（保留为『维持』席位："
                "目标权重 = 当前权重，**不买不卖**，但**计入**权益/现金口径）"
            ),
            "ta_rejected": True,
            "held_not_touched": True,
        })

    #: 席位账（用于权益上限闸门）：
    #:   权益合计 = 规则层全部席位 − 被否决者（原行） + 孤儿维持行 + 顺延补位行
    _s_all = sum(float(_f(a.get("target_weight"), 0.0) or 0.0) for a in acts)
    _s_drop = sum(float(_f(a.get("target_weight"), 0.0) or 0.0) for a in dropped)
    _s_orph = sum(float(r["current_weight"]) for r in orphan_rows)
    _s_sub = 0.0

    # ---- 顺延池：候选池里「原本没入选」的，按 final_score 降序 ----
    pool = [c for c in cands
            if str(c.get("code") or "").zfill(6) not in rejected_codes
            and not bool(c.get("selected"))]
    pool.sort(key=_sort_key)

    # ---- 已入选集合（剔除后仍留在目标组合里的标的）----
    def _selected_codes(acts_list: list[dict]) -> list[str]:
        out: list[str] = []
        for a in acts_list:
            tw = _f(a.get("target_weight"), 0.0) or 0.0
            if tw > 0:
                out.append(str(a.get("code") or "").zfill(6))
        return out

    # ---- 规则层冲突对：一次性算好（重叠 ≥ 0.90 或 corr120 ≥ 0.95）----
    sel0 = _selected_codes(kept)
    all_codes = sorted(set(sel0) | {str(c.get("code") or "").zfill(6) for c in pool}
                       | rejected_codes)
    pair_conflict: dict[tuple[str, str], bool] = {}
    overlap_ready = holdings is not None
    if all_codes:
        try:
            for p in overlap_mod.compute_overlap_pairs(
                holdings=holdings, prices=prices, codes=all_codes,
                asof=asof, names=name_map,
            ):
                pair_conflict[(p["a"], p["b"])] = bool(p["rule_conflict"])
                pair_conflict[(p["b"], p["a"])] = bool(p["rule_conflict"])
        except Exception:  # noqa: BLE001 —— 重合度算不出来 ⇒ 视为不可用（不编结论）
            pair_conflict = {}
            overlap_ready = False

    substituted: list[dict] = []
    used: set[str] = set()
    no_reason: str | None = None
    #: 因「权益合计超上限」而**放弃**的顺延补位（披露用）
    cap_skipped: list[dict] = []
    for a in dropped:
        code_rej = str(a.get("code") or "").zfill(6)
        name_rej = _official_name(code_rej, official,
                                  a.get("name") or name_map.get(code_rej) or "")
        tw = _f(a.get("target_weight"), 0.0) or 0.0
        if tw <= 0:
            # 被否决的标的**原本目标权重就是 0**（规则层本就打算卖出它）
            # ⇒ 否决 = **不动持仓**，没有「替补席位」可言 ⇒ 不顺延。
            if no_reason is None:
                no_reason = (
                    f"{code_rej} {name_rej} 被 TA 否决，但它原本的目标权重就是 0"
                    "（规则层本就打算卖出）⇒ 否决 = **不动持仓**，**无席位可替补**"
                )
            continue
        sel_codes = _selected_codes(kept)
        # 🔴 顺延必须是「**不同主题**的资产」：
        #    既不能与「剔除后仍入选的集合」同族，**也不能**与被否决者同族
        #    （否则否决一只银行 ETF、换来另一只银行 ETF ⇒ 否决形同虚设）。
        sel_themes = {_theme_of(c) for c in sel_codes} | {_theme_of(code_rej)}
        picked: dict | None = None
        gate_fail: list[str] = []
        for c in pool:
            code = str(c.get("code") or "").zfill(6)
            if not code or code in rejected_codes or code in used:
                continue
            if code in sel_codes:
                continue
            _ok, _why = _gate_ok(c)
            if not _ok:
                if _why and len(gate_fail) < 6:
                    gate_fail.append(f"{code}（{_why}）")
                continue
            theme = _theme_of(code)
            if _SKIP_SAME_THEME_ON_SUBSTITUTE and theme in sel_themes:
                continue      # 同族 ⇒ 跳过（文档口径：「顺延挑选下一个**不同主题**的资产」）
            if sum(1 for x in sel_codes if _theme_of(x) == theme) >= C.MAX_PER_THEME:
                continue
            if _SKIP_CONFLICT_ON_SUBSTITUTE:
                if not overlap_ready:
                    continue      # 重合度数据不可得 ⇒ **不强行补**（宁缺毋滥）
                if any(pair_conflict.get((code, x), False) for x in sel_codes):
                    continue
            picked = c
            break
        if picked is None:
            if no_reason is None:
                no_reason = (
                    f"候选池里**没有合格的顺延者**（{code_rej} {name_rej} 被 TA 否决）："
                    "合格条件 = 原本未入选 / 未破位 / 未被 MA 过滤 / 覆盖达标（coverage "
                    f"≥ {float(C.MIN_E_COVERAGE):.0%}，缺失按不达标）/ "
                    "与已入选集合及被否决者均不同族 / 与已入选集合不构成规则层冲突对 / "
                    "该族未达 MAX_PER_THEME"
                    + ("（且成分权重数据不可得 ⇒ 无法核验冲突对）" if not overlap_ready else "")
                    + ("；被闸门挡下的候选：" + "、".join(gate_fail) if gate_fail else "")
                )
            continue
        # 🔴 2026-11-11（缺陷 A · 席位交互）：被否决者若**仍在持仓** ⇒ 它**占着席位**
        #    （保留为『维持』行）⇒ 再顺延一个补位者 = **多出一席** ⇒ 权益可能超配。
        #    ⇒ 补位前必须过「权益合计 ≤ 1 − 现金预留比例」这道闸；过不了 ⇒ **不补位**
        #      （宁缺毋滥），并在 `no_substitute_reason` 写明原因。
        _s_after = _s_all - _s_drop + _s_orph + _s_sub + tw
        if _s_after > _cap + 1e-9:
            cap_skipped.append({
                "code": str(picked.get("code") or "").zfill(6),
                "name": _official_name(
                    str(picked.get("code") or "").zfill(6), official,
                    picked.get("name") or name_map.get(str(picked.get("code") or "").zfill(6)) or ""),
                "substituted_for": code_rej,
                "equity_weight_after": round(_s_after, 6),
                "equity_cap": round(_cap, 6),
                "reason": (
                    f"被否决者 {code_rej} {name_rej} **仍在持仓**（保留为『维持』席位、仍占席位）"
                    f" ⇒ 再顺延补位会让权益合计达 {_s_after:.1%} > 上限 {_cap:.1%}"
                    "（本轮生效的权益上限）⇒ **不补位**（避免多出一席、权益超配）"
                ),
            })
            if no_reason is None:
                no_reason = (
                    f"{code_rej} {name_rej} 被 TA 否决且**仍在持仓**（保留为『维持』席位、仍占席位）"
                    f" ⇒ 顺延补位会让权益合计达 {_s_after:.1%} > 上限 {_cap:.1%}"
                    "（本轮生效的权益上限）⇒ **不补位**（宁缺毋滥，避免多出一席、权益超配）"
                )
            continue
        code = str(picked.get("code") or "").zfill(6)
        name = _official_name(code, official,
                              picked.get("name") or name_map.get(code) or "")
        kept.append({
            "code": code,
            "name": name,
            "action": "买入",
            "target_weight": tw,
            "current_weight": cw.get(code, 0.0),
            "reason": (f"🛡️ TA 合规筛选**顺延补位**（替补被否决的 {code_rej} {name_rej}；"
                       "目标权重沿用被否决者，保持等权口径）"),
            "ta_substitute": True,
            "ta_substituted_for": code_rej,
        })
        used.add(code)
        _s_sub += tw
        substituted.append({
            "code": code,
            "name": name,
            "final_score": _f(picked.get("final_score")),
            "theme": _theme_of(code),
            "substituted_for": code_rej,
            "substituted_for_name": name_rej,
            "reason": (f"原候选 {code_rej} {name_rej} 被 TA 否决 ⇒ 顺延挑选下一个"
                       f"**不同主题**的资产（最终分 {_f(picked.get('final_score'))}）"),
        })

    # ---- 披露用：候选池里被**更宽口径**标记为高重合的对 ----
    screen_pairs: list[dict] = []
    screen_note = ""
    pool_codes = sorted({str(c.get("code") or "").zfill(6) for c in cands} | rejected_codes)
    if pool_codes and holdings is not None:
        try:
            for p in overlap_mod.compute_overlap_pairs(
                holdings=holdings, prices=prices, codes=pool_codes,
                asof=asof, names=name_map,
            ):
                if p.get("screen_flag"):
                    screen_pairs.append(p)
        except Exception:  # noqa: BLE001
            screen_pairs = []
            screen_note = "重合度计算失败（异常）⇒ 本次无高重合预警对"
    elif pool_codes:
        screen_note = "成分权重数据不可得 ⇒ 本次无高重合预警对"

    rejected_rows = []
    for r in rejected:
        code = str(r.get("code") or "").zfill(6)
        rejected_rows.append({
            "code": code,
            "name": _official_name(code, official,
                                   r.get("name") or name_map.get(code) or ""),
            "reason": str(r.get("reason") or ""),
            "source_section": str(r.get("source_section") or ""),
        })

    ta_screening = {
        "enabled": True,
        "rejected": rejected_rows,
        # 🔴 2026-11-11（真机缺陷 ①）：**池外代码如实披露**（不许静默丢弃）
        #    ⇒ LLM 原文里出现、但**不在 ETF 池内**的代码（笔误 / 幻觉）
        #      ⇒ 已从 `rejected` 剔除（它不是候选池的标的 ⇒ 否决它没有意义）
        #      ⇒ 但**如实留痕**，供审计对账。
        "rejected_out_of_universe": rejected_out_of_universe,
        "substituted": substituted,
        "no_substitute_reason": no_reason,
        "screen_pairs": screen_pairs,
        # 🔴 被否决的标的**若本来就在持仓里** ⇒ 只剔除候选、**不动持仓**
        "held_not_touched": bool(rejected_codes & held),
        "screen_note": screen_note,
        # 🔴 2026-11-11（缺陷 A）：**被否决且在持仓**的代码列表（便于审计）
        #    ⇒ 它们在计划里各占一行「维持」（目标权重 = 当前权重），不进买卖挂单。
        "orphaned_holdings": orphan_codes,
        "orphaned_holding_rows": orphan_rows,
        # 🔴 席位交互：因权益上限而放弃的顺延补位
        "cap_skipped": cap_skipped,
        # 🔴 2026-11-11（Beta 避险）：`equity_cap` = **规则层硬上限**（1 − 现金预留比例）
        #    ⇒ **恒为 0.8**（验收脚本按它断言，不许被 TA 改写）；
        #    `equity_cap_effective` = **本轮生效**的上限（= min(硬上限, TA 建议)）。
        "equity_cap": round(_EQUITY_CAP, 6),
        "equity_cap_effective": round(_cap, 6),
        # 🔴 **不可消除**的超配：账户在「被否决且仍在持仓」这只上的权重**超过**它在
        #    规则层的席位预算 ⇒ 即使**一点也不顺延**，权益合计也已经突破上限
        #    （TA 一票否决 ⇒ 不许卖 ⇒ 这个超配是**事实**，不许粉饰 ⇒ 如实披露）。
        "equity_cap_breach": round(max(0.0, _s_all - _s_drop + _s_orph - _cap), 6),
    }
    return {"actions": kept, "ta_screening": ta_screening}


def build_plan(args) -> dict:
    # 🔴 M-B①：F5 判定走「运行内快照」⇒ 每次构建**先清缓存**（新构建必读新盘）
    reset_decision_failure_cache()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    meta = report.get("meta", {})
    asof = str(meta.get("asof", ""))
    params = meta.get("params", {})
    # 🔴 报告缺必需段落 ⇒ 直接抛（宁可不产出，不产出一份「0 个动作 + 全部买入可执行」的空计划）
    _missing = [k for k in ("current_holdings_diagnosis", "next_rebalance", "recommendation")
                 if not report.get(k)]
    if _missing:
        raise ValueError(f"分析报告缺少必需段落：{_missing}（文件：{args.report}）")
    if not (report.get("recommendation") or {}).get("actions"):
        raise ValueError("分析报告的 recommendation.actions 为空 ⇒ 无从生成调仓计划，拒绝产出空计划")
    # 🔴 2026-11-04 21:5x reviewer：动作缺 target_weight 时旧代码静默取 0.0
    #    ⇒ 该标的被当成「目标 0%」⇒ 计划里生成**卖出全部持仓**。
    #    ⇒ 修法：缺字段直接抛（宁可不产出，不产出「全部清仓」的错误计划）。
    _missing_tw = [
        str(a.get("code", ""))
        for a in ((report.get("recommendation") or {}).get("actions") or [])
        if a.get("target_weight") is None
    ]
    if _missing_tw:
        raise ValueError(
            f"分析报告的 recommendation.actions 缺少 target_weight：{_missing_tw}"
            "（宁可不产出，不产出「目标 0% ⇒ 全部清仓」的错误计划）"
        )
    diag = report.get("current_holdings_diagnosis", {}) or {}
    total_assets = _f(diag.get("total_assets"))
    cash = _f(diag.get("cash"))
    next_rb = report.get("next_rebalance", {}) or {}
    rec = report.get("recommendation", {}) or {}
    # 🔴 2026-11-04 21:5x reviewer：旧写法 `_f(x, 1.5) or 1.5` 会把**显式的 0**（零佣金）
    #    改写成 1.5（滑点 0 → 5.0）⇒ 费用口径被静默篡改。
    #    ⇒ 修法：`_f` 已经能把 None/非法值归一到默认值，**不再**用 `or` 兜底。
    commission_bp = _f(params.get("commission_bp"), 1.5)
    slippage_bp = _f(params.get("slippage_bp"), 5.0)
    fee_rate = (commission_bp + slippage_bp) / 1e4

    prices = load_prices(args.prices, asof)
    universe = load_universe(args.universe)
    # 🔴 2026-11-11（真机缺陷 ①②）：**池内白名单** + **官方名表**。
    #    - 白名单事实源优先级：`--universe`（ETF 基础信息 CSV）→ 报告侧代码集合 → 不可用。
    #      取舍理由见 `_pool_whitelist` 的 docstring（**首选 `--universe`**，因为它就是
    #      「池子」本身的定义；报告侧只是「本轮看见过的池」，可能是真池的子集）。
    #    - 官方名 = `--universe` 的 `name` 列 ⇒ 名称**官方优先**（LLM 抽出来的垃圾名
    #      不许盖住它；实测 159547 被抽成「重叠」）。
    _pool_codes, _pool_source = _pool_whitelist(report, universe)
    _official_names: dict[str, str] = {
        str(c): str((row or {}).get("name") or "")
        for c, row in universe.items()
        if str((row or {}).get("name") or "").strip()
    }
    #: 🔴 白名单不可用 ⇒ **fail-closed**（不剔除任何否决）⇒ 必须**如实披露**。
    _pool_whitelist_warning = (
        "⚠️ **池内白名单不可用**（`--universe` 缺失/不可读，且报告侧取不到代码）"
        "⇒ 本轮**未能校验** TA 否决名单里的池外代码 ⇒ 池外代码**未剔除**"
        "（fail-closed：绝不能因为白名单缺失而放过真否决）"
    ) if not _pool_codes else ""
    account = load_account(args.account)
    # 🔴 2026-11-11（审计缺陷 C）：**顺延池 = 报告的 `candidates`**（= analyze.py 的 Top-N）。
    #    报告 JSON **没有**「完整候选池」字段（只有 `candidates` 与
    #    `filter_stats.n_candidate_pool` = 池子**总数**）⇒ 若 `--top-n` 小于真实池大小，
    #    池内第 N+1 名以后**不可见** ⇒ 会误报「候选池里没有合格的顺延者」。
    #    ⇒ 这里**显式核对**两者 ⇒ 截断了就**报警**（不静默）。
    _pool_n = _f((report.get("filter_stats") or {}).get("n_candidate_pool"))
    _vis_n = len(report.get("candidates") or [])
    _pool_truncated = ""
    if _pool_n is not None and _vis_n and float(_pool_n) > float(_vis_n):
        _pool_truncated = (
            f"🔴 **顺延池被截断**：报告的候选池真实 {int(_pool_n)} 只，但报告只带了 "
            f"{int(_vis_n)} 只（analyze.py 的 --top-n 截断）⇒ 第 {int(_vis_n) + 1} 名以后"
            "**不可见** ⇒ 顺延可能漏人（每日链已把 --top-n 调到 100；"
            "研究链 run_pipeline.sh 若用默认值 15 ⇒ 必须自己调大）"
        )

    # 🔴 2026-11-05 01:3x（M-K 收尾）：当前权重的**账户真值**（百分比）。
    #    解析 LLM 表格时用它当「当前权重」，不再从相邻格子里乱读。
    account_weights, account_weight_warnings = build_account_weights(
        diag, account, total_assets, cash)
    priority = _priority_map(report)

    holdings_by_code = {
        str(h.get("code", "")).zfill(6): h for h in (diag.get("holdings") or [])
    }

    # 🛡️ TA 合规筛选（否决 + 顺延）：**在构建 plans 之前**执行。
    #    🔴 只改**副本**（`rec` 原对象一字不动）⇒ 被否决的标的是「从本轮候选里剔除 + 顺延」，
    #    **不是**「卖出」⇒ 目标权重不写 0；如果它本来就在持仓里 ⇒ 只剔除候选、不动持仓。
    _ov_holdings, _ov_prices = overlap_mod.load_overlap_inputs(
        holdings_path=C.ETF_HOLDINGS_CSV, prices_path=args.prices)
    # 🔴 2026-11-11（缺陷 A）：「维持」行的目标权重 = **当前权重** ⇒ 当前权重的取数
    #    必须**完整**：持仓诊断（报告口径）为准，**账户快照 CSV 兜底**
    #    （诊断里没有、但账户确实持有 ⇒ 否则那一行的权重会被算成 0）。
    _acct_w: dict[str, float] = {}
    if total_assets:
        for _c, _row in (account or {}).items():
            _mv = _f(_row.get("market_value"))
            if _mv is not None:
                _acct_w[str(_c).zfill(6)] = float(_mv) / float(total_assets)
    _screen_cw = {
        **_acct_w,
        **{str(k).zfill(6): (_f(v.get("current_weight"), 0.0) or 0.0)
           for k, v in holdings_by_code.items()},
    }
    # 🔴 2026-11-11（Beta 避险落地）：**采纳** TA 的组合权益上限压降建议。
    #    口径：只认 `llm_final.post_trade.equity_target_pct`（**目标动词**口径，
    #    陈述句不算建议）、**只许压降**（建议 ≥ 规则层硬上限 ⇒ 忽略）、
    #    解析不出 ⇒ **完全保持原行为**。
    _ta_cap = _resolve_ta_equity_cap(
        llm_final_mod.load_from_decision(getattr(args, "decision", None))
    )
    _effective_cap = (
        _EQUITY_CAP if _ta_cap["pct"] is None else min(_EQUITY_CAP, float(_ta_cap["pct"]))
    )
    # 🔴 一票否决**只认权威裁决段落**（观点段落没有否决权）——
    #    真实决策件里空头说「暂不买入 515450/515650」、激进派说「清仓512800，
    #    等于放弃…」⇒ 旧实现（扫全文）误伤 3 只 ⇒ 见 llm_final.AUTHORITY_SECTIONS。
    _rejections_all = llm_final_mod.extract_authority_rejections(args.decision)
    # 🔴 2026-11-11（真机缺陷 ①）：**池外代码剔除**（fail-closed）。
    #    LLM 笔误出来的池外代码（如 566620；池内是 516620）**不得**进入否决名单
    #    ⇒ 剔除，但**如实披露**（`ta_screening.rejected_out_of_universe` + md 里的 ⚠️ 行）。
    #    ⚠️ 白名单不可用 ⇒ `_split_rejections_by_pool` **一个都不剔除**（不放过真否决）。
    _rejections, _rejections_out_of_universe = _split_rejections_by_pool(
        _rejections_all, _pool_codes)
    # 🔴 「维持」行（被 TA 否决但**仍在持仓**）**占席位**（目标权重 = 当前权重）
    #    ⇒ 席位预算必须先扣掉它 ⇒ 否则「维持行 + 席位行」会突破本轮生效的上限。
    _orphan_weight = sum(
        float(_screen_cw.get(str(r.get("code") or "").zfill(6), 0.0) or 0.0)
        for r in _rejections
        if str(r.get("code") or "").zfill(6) in _screen_cw
    )
    #: 🔴 席位等比缩放：把规则层席位预算压到「本轮生效上限 − 维持行占额」。
    #:    ⚠️ 无 TA 建议 ⇒ `_scale == 1.0` ⇒ **产物逐字节不变**（既有金标不动）。
    _rule_actions = list(rec.get("actions") or [])
    _seat_base = sum(float(_f(a.get("target_weight"), 0.0) or 0.0) for a in _rule_actions)
    _seat_cap = max(0.0, _effective_cap - _orphan_weight)
    _scale = 1.0
    if _seat_base > 0 and _effective_cap < _EQUITY_CAP - 1e-12:
        _scale = min(1.0, _seat_cap / _seat_base)
    _scaled_actions = [
        {**a, "target_weight": float(_f(a.get("target_weight"), 0.0) or 0.0) * _scale}
        for a in _rule_actions
    ]
    _ta_screen = apply_ta_screening(
        actions=_scaled_actions,
        candidates=report.get("candidates") or [],
        # ⚠️ 传**原始**否决名单：池外切分由 `apply_ta_screening` **自己**做
        #    （那里才会同时产出 `ta_screening.rejected` 与 `rejected_out_of_universe`）
        #    ⇒ 切两遍会让披露字段为空。
        rejections=_rejections_all,
        held_codes=sorted(set(holdings_by_code) | set(account)),
        current_weights=_screen_cw,
        holdings=_ov_holdings,
        prices=_ov_prices,
        asof=asof,
        equity_cap=_effective_cap,
        universe_codes=_pool_codes,
        official_names=_official_names,
    )
    _screen_actions = list(_ta_screen["actions"])
    _ta_screening = _ta_screen["ta_screening"]

    # 🔴 2026-11-11（真机缺陷 ①）：把「池内白名单的来源 / 规模」**写进披露**
    #    （审计要看得见：本轮的「池」到底是哪份文件 / 多少个代码）。
    _ta_screening["pool_whitelist"] = {
        "source": _pool_source,
        "available": bool(_pool_codes),
        "n_codes": (len(_pool_codes) if _pool_codes else 0),
        "n_rejected_out_of_universe": len(_rejections_out_of_universe),
        "note": (
            "池内白名单 = ETF 池的代码集合（首选 `--universe` 的 code 列；"
            "不可用时回退报告侧代码集合）⇒ **池外代码**不得进入否决名单；"
            "白名单不可用 ⇒ **一个都不剔除**（fail-closed ⇒ 不放过真否决）"
        ),
    }

    # 🔴 2026-11-11（Beta 避险落地）：把「TA 建议 / 生效上限 / 席位缩放」**写进披露**
    #    （用户必须看得见：TA 说压降到多少、规则层实际按多少落地、席位缩了多少）。
    _ta_screening["equity_cap_ta_recommendation"] = {
        "pct": None if _ta_cap["pct"] is None else round(float(_ta_cap["pct"]), 6),
        "pct_display": (None if _ta_cap["pct"] is None
                        else round(float(_ta_cap["pct"]) * 100, 2)),
        "evidence": _ta_cap["evidence"],
        "reason": _ta_cap["reason"],
        "base_cap": round(_EQUITY_CAP, 6),
        "effective_cap": round(_effective_cap, 6),
        "note": (
            "TA 的**权益上限压降建议**（只认『降至/定为/控制在』这类目标动词；"
            "陈述句不算建议）；**只许压降、不许抬高**；解析不出 ⇒ 不压降"
        ),
    }
    _ta_screening["equity_cap_scale"] = {
        "factor": round(_scale, 6),
        "seat_base_weight": round(_seat_base, 6),
        "seat_cap_weight": round(_seat_cap, 6),
        "orphan_hold_weight": round(_orphan_weight, 6),
        "note": (
            "席位目标权重**等比缩放**到「本轮生效上限 − 维持行占额」"
            "（『维持』行 = 被 TA 否决但仍在持仓者 ⇒ 目标权重 = 当前权重，权重不动）"
        ),
    }

    # 🔴 2026-11-11（审计缺陷 A）：**被 TA 否决且仍在持仓**的代码 ⇒ 保留成一行
    #    「维持」（语义 = 持有不动仓）：
    #      - 目标权重 = **当前权重**（不是 0）⇒ 它**计入**权益/现金口径
    #        ⇒ 计划权益 / 计划现金 / 总资产恒等式**自洽**（不再凭空蒸发市值）；
    #      - 目标股数 = **当前股数** ⇒ delta = 0 ⇒ **既不买也不卖**（不进挂单）。
    #    ⚠️ 它**不在** `apply_ta_screening` 返回的候选 action 列表里（剔除 ≠ 权重 0），
    #      是**持仓行**⇒ 由这里单独接进来。
    for _r in (_ta_screening.get("orphaned_holding_rows") or []):
        _screen_actions.append({
            "code": _r.get("code"),
            "name": _r.get("name") or "",
            "action": "维持",
            "target_weight": _f(_r.get("target_weight"), 0.0) or 0.0,
            "current_weight": _f(_r.get("current_weight"), 0.0) or 0.0,
            "reason": _r.get("reason") or "",
            "ta_rejected": True,
            "held_not_touched": True,
        })

    plans = []
    for act in _screen_actions:
        code = str(act.get("code", "")).zfill(6)
        # 🔴 2026-11-11（真机缺陷 ②）：名称**官方优先**（`--universe` 的 name 列）
        #    ⇒ 官方名存在 ⇒ 用官方名（忽略来源里的名字）；官方名没有 ⇒ 才用来源名。
        name = _official_name(code, _official_names, act.get("name", ""))
        held = account.get(code, {})
        holdings_row = holdings_by_code.get(code, {})
        current_shares = int(held.get("shares", 0) or holdings_row.get("shares", 0) or 0)
        account_price = _f(held.get("price")) or _f(holdings_row.get("price"))
        # 🔴 价格基准必须**单一且有日期**：账户快照里的现价是**更晚一天**的收盘价
        #    （实测 512800：账户 0.874 = 11-04 收盘，而 11-03 收盘是 0.880），
        #    旧代码拿账户价算持仓、拿 asof 价算新标的，却把 price_date 一律写成 asof
        #    ⇒ 一份计划里混了两个价格日，且 512800 目标股数因此多留 100 份。
        #    ⇒ 统一用 **asof 收盘价**（≤ asof 的最后一根）算目标股数与金额；
        #      没有行情的标的才退回账户快照价，并如实标注价日期。
        price = None
        price_date = ""
        if code in prices:
            price, price_date = prices[code]
        if price is None:
            price = account_price
            price_date = "账户快照价（该 ETF 无行情数据）"
        if price is None:
            # 🔴 潜在未来函数（2026-11-04 21:5x reviewer 指出）：
            #    universe 的 last_price_date 可能**晚于 asof**（实测 etf_universe.csv 是
            #    2026-11-04，而 asof 是 2026-11-03）⇒ 用它折算目标股数 = 用未来价算今天的仓位。
            #    ⇒ 修法：只在 last_price_date <= asof 时才用它；否则不取价（保持 None，交付件显式标注）。
            _u = universe.get(code, {}) or {}
            _upd = str(_u.get("last_price_date") or "")
            if _upd and _upd <= asof:
                price = _f(_u.get("last_price"))
                price_date = _upd
        target_weight = _f(act.get("target_weight"), 0.0) or 0.0
        #: 🔴 2026-11-11（缺陷 A）：「维持」行（被 TA 否决但仍在持仓）⇒
        #    目标股数 = **当前股数**（**不卖不买**）；目标金额按**执行价**口径
        #    （= 当前股数 × 执行价）⇒ 与 `target_shares × price` 自洽（不触发
        #    「实际买入额 > 目标金额」的超配守卫），与其它行的差异就是**价格基差**。
        _is_ta_hold = bool(act.get("ta_rejected"))
        if _is_ta_hold:
            target_amount = (current_shares * price) if price is not None else None
            target_shares = current_shares
        else:
            target_amount = target_weight * total_assets if total_assets else None
            target_shares = (
                _round_lot(target_amount / price)
                if (price is not None and target_amount is not None)
                else None
            )
        delta_shares = (target_shares - current_shares) if target_shares is not None else None
        est_amount = delta_shares * price if (delta_shares is not None and price) else None
        if _is_ta_hold:
            action = "维持"
        elif delta_shares is None:
            action = str(act.get("action", ""))
        elif delta_shares > 0:
            action = "买入"
        elif delta_shares < 0:
            action = "卖出"
        else:
            action = "持有"
        fee = abs(est_amount) * fee_rate if est_amount else 0.0
        plans.append({
            "code": code,
            "name": name,
            "qlib_action": str(act.get("action", "")),
            "action": action,
            "current_shares": current_shares,
            "price": price,
            "price_date": price_date,
            "account_price": account_price,
            "current_market_value": _f(held.get("market_value")) or (current_shares * price if price else None),
            "current_weight": _f(act.get("current_weight")),
            "pnl_pct": _f(held.get("pnl_pct")),
            "target_weight": target_weight,
            "target_amount": target_amount,
            "target_shares": target_shares,
            "delta_shares": delta_shares,
            "est_amount": est_amount,
            "est_fee": fee,
            "final_score": priority.get(code),
            "reason": str(act.get("reason", "")),
            # 🔴 2026-11-11（缺陷 A）：否决/维持标记随行走 ⇒ 交付层与每日链
            #    （执行卡挂单）都能认出「这一行不许动」。
            "ta_rejected": bool(act.get("ta_rejected")),
            "held_not_touched": bool(act.get("held_not_touched")),
            "broken": holdings_row.get("broken"),
            "ma20_ok": holdings_row.get("ma20_ok"),
            "ma60_ok": holdings_row.get("ma60_ok"),
            "e_score": holdings_row.get("e_score"),
            "mom120": holdings_row.get("mom120"),
            "rejected_by_stage": holdings_row.get("rejected_by_stage"),
            "reject_reason": holdings_row.get("reject_reason"),
            "lot_note": "",
        })

    # 追加口径事实（2026-11-04 夜修）：① 跌破 MA60 ⇒ 持仓强制退出（比「E 算不出」更贴切）；
    # ② 入选标的补 E(W×S) 与动量数字；③ 目标股数是**按 asof 价**折算的，执行日必须重算。
    for p in plans:
        # 🔴 2026-11-11（审计缺陷 B）：**执行说明必须每行都有**（含「维持」行）。
        #    `probe_execution_date` 的 P9 要求**每一条** action 的 `execution_note`
        #    同时写「信号日」与「执行日」；旧实现里「维持」行（被 TA 一票否决但仍在持仓）
        #    在 `execution_note` **赋值之前**就 `continue` 了
        #    ⇒ 一旦 TA 否决了**在持仓**的标的，计划就**缺执行说明** ⇒ P9 必红。
        #    ⚠️ 之所以长期没暴露：本轮「维持」行数 = 0（TA 只否决了未持仓候选）
        #    ⇒ 缺陷**潜伏**，直到某轮 TA 否决了持仓才会炸（跑批时不出声 ⇒ 极难排查）。
        #    ⇒ 修法：日期口径**提到分支之前**算好（与循环无关），
        #      「维持」行走**专用文案**（写明「不建仓也不卖出」+ 信号日/执行日），
        #      **不污染**「跌破 MA60 ⇒ 强制退出」这类卖出文案（缺陷 A 的修法不受影响）。
        _node_date = str((next_rb or {}).get("signal_date")
                         or (next_rb or {}).get("target_date") or "")
        _exec_date = str((next_rb or {}).get("execution_date") or "")
        _exec_tail = (
            f"于**执行日 {_exec_date}** 收盘下单"
            if _exec_date else "执行日算不出来（报告未给）⇒ 不许编日期"
        )
        # 🔴 2026-11-11（缺陷 A）：「维持」行（被 TA 一票否决但仍在持仓）**不卖**
        #    ⇒ 不许被「跌破 MA60 ⇒ 持仓强制退出」这类**规则层卖出**文案污染
        #    （否则同一行既写『维持』又写『强制退出』⇒ 交付件自相矛盾）。
        if p.get("ta_rejected"):
            # 「维持」行：**不卖** ⇒ 不追加「跌破 MA60 ⇒ 强制退出」这类**卖出**文案
            #    （否则同一行既写『维持』又写『强制退出』⇒ 交付件自相矛盾）；
            #    但 **E / 动量**等**中性事实**照常披露（不藏数据）。
            if p["e_score"] is not None:
                p["reason"] += (f"；E(W×S)={float(p['e_score']):.4f}"
                                + (f"，mom120={float(p['mom120']):+.2%}"
                                   if p["mom120"] is not None else ""))
            # 🔴 2026-11-11（审计缺陷 B）：「维持」行**也必须有**执行说明
            #    （P9 要求每行都写「信号日」+「执行日」）。
            p["execution_note"] = (
                f"**维持当前持仓**（本轮被 TA 一票否决 ⇒ 既不建仓也不卖出；"
                f"目标股数＝当前股数 {p.get('current_shares')} 股，不做整手重算）；"
                f"自**信号日（{_node_date or '报告未给日期'}）**收盘起按当日价格盯市，"
                f"至**执行日（{_exec_date or '报告未给日期'}）**收盘前复核"
                f"（本轮不产生任何买卖动作）"
            )
            continue
        if p["current_shares"] and p["ma60_ok"] is False:
            p["reason"] = (p["reason"] + "；**已跌破 MA60 ⇒ 持仓强制退出（不看排名）**").strip("；")
        if p["e_score"] is not None:
            p["reason"] += (f"；E(W×S)={float(p['e_score']):.4f}"
                            + (f"，mom120={float(p['mom120']):+.2%}" if p["mom120"] is not None else ""))
        # 🔴 2026-11-04 21:5x reviewer：旧代码把调仓节点**硬编码**成 2026-12-01，
        #    写进**每一条** action 的执行说明 ⇒ 下个月口径一变，每条说明都写错日期，
        #    且与 JSON 里的 next_rebalance 自相矛盾 ⇒ 改用报告给的权威日期。
        # 🔴 M-J（2026-11-05 夜实测）：**信号日 ≠ 执行日**（config.EXECUTION_LAG_DAYS=1）。
        #   旧文案只写「实际在调仓节点（2026-12-01）……再下单」⇒ 读者会以为 12-01 当天下单
        #   ⇒ 那等于拿 **11-03 的旧信号**、用 12-01 的价格成交 ⇒ 比回测口径早一天，
        #   **不在回测假设内**（§十四.7 的持仓区间也是按 exec 日算的）。
        #   ⇒ 执行说明必须同时给出**信号日**（重算股数）与**执行日**（下单）。
        #    （日期口径已在循环开头算好 —— 见「审计缺陷 B」的注释。）
        p["execution_note"] = (
            f"目标股数按 {p['price_date']} 收盘价 {p['price']} 折算；"
            f"实际在**信号日（{_node_date or '报告未给日期'}）**收盘后按当日价格重新折算整手股数，"
            f"{_exec_tail}")

    # 目标金额不足一手（100 份）时，标出来 —— 不静默变成 0
    for p in plans:
        if p.get("ta_rejected"):
            continue      # 「维持」行不建仓 ⇒ 不适用「买不进」口径
        if (
            p["target_amount"] is not None
            and p["price"] is not None
            and p["target_amount"] > 0
            and p["price"] * LOT > p["target_amount"]
        ):
            p["lot_note"] = (
                f"目标金额 {p['target_amount']:.0f} 元 < 一手({LOT} 份 × {p['price']:.3f} = "
                f"{p['price'] * LOT:.0f} 元) ⇒ 整手口径下本轮无法按目标权重建仓"
            )
            p["reason"] = (p["reason"] + "；" + p["lot_note"]).strip("；")

    # ---- 「买不进的席位」不占仓位，权重归现金 --------------------------------
    # 🔴 2026-11-04 夜实测（真实账户 29,854.67 元、20% 席 ≈ 5,970 元）：
    #    引擎入选的 5 席里有 3 只是货币 ETF（511660/511880/511990），
    #    一手 100 份 ≈ 10,000 元 > 5,970 元 ⇒ **一手都买不起**。
    #    旧代码只加一句 lot_note，仓位表里照样显示「20% 目标 / 持有 0 股」，
    #    现金小结还说「全部买入均可执行」⇒ 交付件自相矛盾（看着像满仓，实际是 40%）。
    #    现在：这类席位显式标成「不可建仓」，**权重归现金**，并在现金小结如实体现。
    unbuildable: list[dict] = []
    for p in plans:
        p["target_weight_effective"] = p["target_weight"]
        p["weight_to_cash"] = 0.0
        if (
            p["lot_note"]
            and (p["delta_shares"] or 0) == 0
            and p["current_shares"] == 0
            and (p["target_amount"] or 0) > 0
        ):
            p["action"] = "不可建仓"
            p["target_weight_effective"] = 0.0
            p["weight_to_cash"] = p["target_weight"]
            p["target_shares"] = 0
            unbuildable.append({
                "code": p["code"],
                "name": p["name"],
                "target_weight": p["target_weight"],
                "target_amount": p["target_amount"],
                "one_lot_cost": round(p["price"] * LOT, 2),
                "price": p["price"],
                "reason": p["lot_note"],
            })

    #: 现金替代方案：买不进的那几席，若按「1 手」执行会占多大权重（供用户单独决策）
    cash_alternatives: list[dict] = []
    for u in unbuildable:
        cash_alternatives.append({
            "code": u["code"],
            "name": u["name"],
            "one_lot_shares": LOT,
            "one_lot_cost": u["one_lot_cost"],
            "weight_if_one_lot": (u["one_lot_cost"] / total_assets) if total_assets else None,
            "note": "作为现金替代需至少 1 手；该权重会**超出 20% 单席** ⇒ "
                    "属 5 只等权结构之外的额外决策，不是本计划的持仓",
        })

    # ---- 现金可行性：卖出回款 + 期初现金 能不能覆盖买入 ----
    sells = [p for p in plans if (p["delta_shares"] or 0) < 0]
    buys = [p for p in plans if (p["delta_shares"] or 0) > 0]
    proceeds = sum(abs(p["est_amount"]) for p in sells if p["est_amount"])
    sell_fees = sum(p["est_fee"] for p in sells)
    buy_amount = sum(p["est_amount"] for p in buys if p["est_amount"])
    buy_fees = sum(p["est_fee"] for p in buys)
    available = (cash or 0.0) + proceeds - sell_fees
    deficit = buy_amount + buy_fees - available
    # 🔴 语义：`deficit` 一律 **>= 0**（缺口）；富余单独放 `surplus`。
    #    旧代码在「不缩量」分支保留负值 ⇒ JSON 里 deficit=-17938.84（其实是富余）
    #    而 MD 表头又写「负=富余」⇒ 同一份产物里三个数字互相打架。
    surplus = max(0.0, -deficit)
    deficit = max(0.0, deficit)
    scaled = []
    if deficit > 0 and buys:
        buys.sort(key=lambda p: (p["final_score"] is not None, p["final_score"] or 0), reverse=True)
        budget = available
        for p in buys:
            if budget <= 0:
                p["delta_shares"] = 0
                p["target_shares"] = p["current_shares"]
                p["est_amount"] = 0.0
                p["est_fee"] = 0.0
                p["action"] = "持有"
                p["reason"] = (p["reason"] + "；现金不足，本轮不建仓").strip("；")
                scaled.append({"code": p["code"], "name": p["name"], "cut": True})
                continue
            if p["est_amount"] + p["est_fee"] > budget:
                lots = int(budget / (p["price"] * (1 + fee_rate)) / LOT) * LOT
                new_amount = lots * p["price"]
                p["target_shares"] = p["current_shares"] + lots
                p["delta_shares"] = lots
                p["est_amount"] = new_amount
                p["est_fee"] = new_amount * fee_rate
                budget -= new_amount + p["est_fee"]
                if lots <= 0:
                    p["action"] = "持有"
                    p["reason"] = (p["reason"] + "；现金不足，本轮不建仓").strip("；")
                scaled.append({"code": p["code"], "name": p["name"], "cut": True,
                               "planned_delta": None})
        buy_amount = sum(p["est_amount"] for p in buys if p["est_amount"])
        buy_fees = sum(p["est_fee"] for p in buys)
        deficit = max(0.0, buy_amount + buy_fees - available)
        surplus = max(0.0, available - (buy_amount + buy_fees))

    # 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出、本机已自建场景复证）：
    #    「现金不足被削减」这条路径**没有把削减结果回写到权重** ⇒
    #    交付件的「计划权益仓位权重 / 计划现金权重」用的还是**削减前**的目标权重，
    #    而「期末现金」是**按削减后的实际买入**算的 ⇒ 同一份交付件里两个数字互相打架。
    #    ⚠️ 触发条件：账户现金不足以覆盖「卖出回款 + 期初现金」时买满全部席位。
    #    ⚠️ 一致账户 + 5 席等权时缺口≈手续费（很小）；但「持有超配席 + 低现金」会缺口很大。
    #    ⇒ 修法：被削减的席位按「调仓后持仓市值 / 总资产」重算**可达权重**，
    #      差额记进 weight_to_cash（权重归现金）；**计划权益/计划现金在削减之后才算**。
    #    ⚠️ 未被削减的席位保持 target_weight_effective = target_weight（计划口径）
    #      ⇒ 正式产物（deficit=0、无削减）**逐字节不变**。
    if scaled and total_assets:
        _cut_codes = {s["code"] for s in scaled}
        for p in plans:
            if p["code"] not in _cut_codes:
                continue
            _plan_w = float(p.get("target_weight") or 0.0)
            _after_value = (p["current_shares"] + (p["delta_shares"] or 0)) * (p["price"] or 0.0)
            _eff = _after_value / total_assets
            p["target_weight_effective"] = _eff
            p["weight_to_cash"] = max(0.0, _plan_w - _eff)
            _note = f"；现金不足 ⇒ 可达权重 {_eff * 100:.1f}%"
            if _eff < _plan_w - 1e-12:
                _note += f"（原目标 {_plan_w * 100:.1f}%）"
            p["reason"] = (p["reason"] + _note).strip("；")

    #: 计划内的**权益仓位权重**（不可建仓 / 现金不足被削减的席位已按**可达**口径处理）
    #: ⇒ 其余权重 = 现金。🔴 必须在**削减之后**算，否则与「期末现金」对不上。
    invested_weight = float(sum(p["target_weight_effective"] for p in plans))
    target_cash_weight = float(max(0.0, 1.0 - invested_weight))
    target_cash_amount = (target_cash_weight * total_assets) if total_assets else None

    cash_after = (cash or 0.0) + proceeds - sell_fees - buy_amount - buy_fees

    # 🔴 2026-11-05 00:1x 实测修（M-H）：**计划权重 ≠ 实际落地权重**。
    #    目标股数按 100 股整手**向下取整**（`_round_lot`）⇒ 每个可建席位的**实际买入额**
    #    **小于**目标金额（最多少 1 手）⇒ Σ(target_shares × price) < invested_weight × total_assets。
    #    实测（2026-11-03 asof）：512800 目标 5,970.93 元 ⇒ 6,785 股 ⇒ 向下取整 **6,700 股**
    #    = **5,896.00 元 = 19.75%**（不是 20.0%）；563020 = 5,963.30 元 = 19.97%
    #    ⇒ 实际权益 **11,859.30 元 = 39.72%**，与「计划 40.0%」差 **82.57 元 / 0.28pp**。
    #    旧产物只给「计划权益权重 40.0%」⇒ 交付件把**计划值**当**实际值**报 ⇒ 口径失真。
    #    ⇒ 现在**同时**给「计划」与「实际落地」，差额（整手取整缺口）**自动留在现金**。
    #: 实际落地权益市值 = Σ(目标股数 × 执行价)，只算**建得了仓**的席位（不可建仓 target_shares=0）。
    _realized_equity_amount = 0.0
    #: 价格基差 = 账户快照价口径的持仓市值 − 执行价（asof 收盘价）口径的持仓市值。
    #: 🔴 两个口径的「总资产」**不相等**（差这一项）⇒ 权重不许跨口径相减。
    _snapshot_mv = 0.0
    _exec_mv = 0.0
    for _p in plans:
        _shares = int(_p.get("target_shares") or 0)
        _price = _f(_p.get("price"), 0.0) or 0.0
        if _shares and _price:
            _realized_equity_amount += _shares * _price
        _cur = int(_p.get("current_shares") or 0)
        if _cur:
            _snapshot_mv += _f(_p.get("current_market_value"), 0.0) or 0.0
            _exec_mv += _cur * _price
    _price_basis_diff = _snapshot_mv - _exec_mv
    _planned_equity_amount = (invested_weight * total_assets) if total_assets else None
    _lot_rounding_gap = (
        (_planned_equity_amount - _realized_equity_amount) if total_assets else None
    )
    _equity_weight_realized = (
        (_realized_equity_amount / total_assets) if total_assets else None
    )
    _cash_weight_realized = ((cash_after / total_assets) if total_assets else None)
    #: 执行价口径的总资产（= 账户总资产 − 价格基差）⇒ 用于解释「权益% + 现金% ≠ 100%」。
    _exec_basis_total_assets = (
        (total_assets - _price_basis_diff) if total_assets else None
    )
    # ---- LLM 第二意见 vs 规则层：逐标的比对目标权重，差异 > 2pp ⇒ 记一条冲突 ----
    llm_targets = _llm_targets(args.decision, total_assets, account_weights)
    llm_views = _llm_target_views(args.decision, total_assets, account_weights)
    llm_conflicts: list[dict] = []
    for _p in plans:
        _views = llm_views.get(str(_p["code"])) or []
        if not _views:
            continue
        _rule_pct = float(_p.get("target_weight") or 0.0) * 100.0
        _div = [v for v in _views if abs(_rule_pct - v["target_pct"]) > 2.0]
        if not _div:
            continue
        _div.sort(key=lambda v: abs(_rule_pct - v["target_pct"]), reverse=True)
        _t = _div[0]
        _reason = _t["reason"]
        if not _reason:
            # 多头表没有「理由」列 ⇒ 借用同一分歧里另一方的理由，别留空
            for _alt in _div[1:]:
                if _alt["reason"]:
                    _reason = _alt["reason"]
                    break
        llm_conflicts.append({
            "code": _p["code"],
            "name": _p["name"],
            "rule_target_pct": round(_rule_pct, 2),
            "llm_target_pct": _t["target_pct"],
            "llm_direction": _t["direction"],
            "llm_who": _t["who"],
            # 🔴 2026-11-05 01:3x（M-K 收尾）：把「当前权重」也带进冲突行，
            #    否则自检的「方向-权重语义自洽」守卫在**冲突行**上
            #    拿不到当前权重 ⇒ 「减 ⇒ 目标 < 当前」这一档**被静默跳过**
            #    （守卫覆盖出现空洞：主视图查了、冲突行没查）。
            "llm_current_pct": _t.get("current_pct"),
            "llm_reason": _reason,
            "all_divergent_views": [
                {"who": v["who"], "target_pct": v["target_pct"],
                 "direction": v["direction"], "current_pct": v.get("current_pct")}
                for v in _div
            ],
        })
    if "现金" in llm_targets and total_assets:
        _cash_rule_pct = (cash_after / total_assets) * 100.0
        # 🔴 2026-11-04 21:5x reviewer：旧实现用**主视图**（research > bull > bear 首个）
        #    做门限与取值，而 ETF 行用的是「**取与规则层差得最远**的那个视图」
        #    ⇒ 两套口径 ⇒ **更大的分歧被埋在 all_divergent_views 里**。
        #    实测（真实决策）：规则层现金 60%，多头 36.5%（差 23.5pp）、空头 90%（差 30pp）
        #    ⇒ 旧实现报 36.5%，真正差得最远的空头 90% 上不了台面。
        #    ⇒ 修法：现金行与 ETF 行**同口径**（取差得最远的视图）；
        #      主视图仍保留在 all_divergent_views 里，不丢信息。
        _cash_views = llm_views.get("现金") or []
        _cash_div = [v for v in _cash_views if abs(_cash_rule_pct - v["target_pct"]) > 2.0]
        if _cash_div:
            _cash_div.sort(key=lambda v: abs(_cash_rule_pct - v["target_pct"]), reverse=True)
            _cash_top = _cash_div[0]
            llm_conflicts.append({
                "code": "现金",
                "name": "现金",
                "rule_target_pct": round(_cash_rule_pct, 2),
                "llm_target_pct": _cash_top["target_pct"],
                "llm_direction": _cash_top["direction"],
                "llm_who": _cash_top["who"],
                "llm_reason": _cash_top["reason"] or llm_targets["现金"]["reason"],
                # 🔴 2026-11-05 01:4x（M-K 收尾；reviewer 指出）：现金冲突行**原先没带**
                #    当前权重 ⇒ 守卫在冲突行上的「减 ⇒ 目标 < 当前 / 增 ⇒ 目标 > 当前」
                #    两档**永远跳过**（LLM 一旦写「减现金」就查不到）⇒ 与 ETF 行**同口径**补齐。
                "llm_current_pct": _cash_top.get("current_pct"),
                "all_divergent_views": [
                    {"who": v["who"], "target_pct": v["target_pct"],
                     "direction": v["direction"], "current_pct": v.get("current_pct")}
                    for v in _cash_div
                ],
            })
    stale_info = _llm_staleness(args)
    if stale_info.get("stale"):
        for conflict in llm_conflicts:
            conflict["stale"] = True
    # 🔴 2026-11-05 04:4x（M-P）：Trader 的**最终裁决**单独成块（**不改** llm_targets /
    #    llm_conflicts ⇒ 金标 A1 不动）⇒ 交付件必须让用户看见「最终结论」而不是
    #    「与规则层差得最远的子智能体视图」。
    # 🔴 2026-11-11（审计缺陷 D）：被 TA 一票否决的代码 ⇒ 最终裁决表里**单列一行**
    #    （原先它们从表里静默消失 ⇒ 与正文「以本节为准」自相矛盾）；
    #    其中**因否决而移出**本轮组合的 ⇒ 显式标注「因 TA 否决而移出」。
    _ta_rejected_rows = [
        {
            "code": str(r.get("code") or "").zfill(6),
            "name": str(r.get("name") or ""),
            "reason": str(r.get("reason") or ""),
        }
        for r in (_ta_screening.get("rejected") or [])
        if r.get("code")
    ]
    _plan_codes = {str(pl.get("code")) for pl in plans}
    _ta_moved_out = [
        {
            "code": r["code"],
            "name": r["name"],
            "reason": "因 TA 一票否决而移出本轮候选（冲突表里的对应条目随之移除）",
            "had_llm_view": bool(llm_views.get(r["code"])),
            "llm_views": llm_views.get(r["code"]) or [],
        }
        for r in _ta_rejected_rows
        if r["code"] not in _plan_codes
    ]
    llm_final_decision = _build_llm_final_decision(
        args, plans, llm_targets, llm_conflicts,
        ta_rejected=_ta_rejected_rows, ta_moved_out=_ta_moved_out,
    )
    plan = {
        "meta": {
            "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asof": asof,
            "total_assets": total_assets,
            "cash": cash,
            # 🔴 2026-11-05 01:3x（M-K 收尾）：把「当前权重」的账户真值写进产物，
            #    让 verify_artifacts 的**重放**与生产**同参**（否则重放算不出
            #    「金额表达的目标」，与生产解析结果不一致 ⇒ 可追溯守卫会被误伤）。
            "account_current_weights_pct": account_weights,
            "target_n_holdings": params.get("target_n_holdings"),
            "hold_buffer": rec.get("hold_buffer"),
            "weights_used": rec.get("weights_used"),
            "commission_bp": commission_bp,
            "slippage_bp": slippage_bp,
            "fee_rate_one_way": fee_rate,
            "lot_size": LOT,
            "source_report": str(args.report),
            # 🔴 F5：失败占位**不算**「引用了决策」⇒ source_decision 置 None，
            #    并把失败原因单独记在 llm_decision_status / llm_decision_error 里。
            "source_decision": (
                str(args.decision)
                if args.decision and Path(args.decision).exists() and not _decision_failure_reason(args.decision)
                else None
            ),
            "llm_decision_status": (
                "failed" if _decision_failure_reason(args.decision)
                else ("ok" if stale_info.get("llm_present") else "absent")
            ),
            "llm_decision_error": _decision_failure_reason(args.decision),
            "llm_stale": stale_info,
        },
        "next_rebalance": dict(next_rb),
        "execution_discipline": {
            "signal_date": str(next_rb.get("signal_date") or next_rb.get("target_date") or ""),
            "execution_date": str(next_rb.get("execution_date") or ""),
            "execution_lag_days": next_rb.get("execution_lag_days"),
            "rule": str(next_rb.get("execution_rule") or ""),
            "note": (
                f"**{next_rb.get('signal_date') or next_rb.get('target_date')} 是信号日**"
                f"（T 日收盘出信号），**实际下单日 = {next_rb.get('execution_date')}**（T+1 收盘执行）。"
                f"本文件的股数是按 **{asof} 收盘价**折算的样例值 ⇒ "
                f"须在信号日收盘后按当日收盘价重算整手股数，再于执行日收盘下单；"
                f"在信号日当天下单 = 早于回测口径一天，**不在回测假设内**。"
                if next_rb.get("execution_date") else
                "⚠️ 执行日算不出来（交易日历未覆盖）⇒ 不许编日期。"
            ),
        },
        "cash_flow": {
            "cash_begin": cash,
            "sell_proceeds": proceeds,
            "sell_fees": sell_fees,
            "buy_amount": buy_amount,
            "buy_fees": buy_fees,
            "cash_end": cash_after,
            "available_before_buy": available,
            "deficit": deficit,
            "surplus": surplus,
            "headroom": available - (buy_amount + buy_fees),
            "scaled_down": scaled,
            "feasible": deficit <= 0,
            # 🔴 2026-11-04 夜新增：买不进的席位（整手买不起）**权重归现金**，
            #    必须显式写进产物，否则仓位表与现金小结会互相打架。
            "unbuildable_slots": unbuildable,
            "invested_weight_planned": invested_weight,
            "target_cash_weight": target_cash_weight,
            "target_cash_amount": target_cash_amount,
            # 🔴 M-H：以下 6 个字段 = **实际落地**口径（整手取整后 / 执行价口径）。
            #    🔴 与上面的「计划」口径**不许混用**：计划权益 40.0% ≠ 实际落地 39.72%。
            "equity_amount_realized": _realized_equity_amount,
            "equity_weight_realized": _equity_weight_realized,
            "cash_weight_realized": _cash_weight_realized,
            "planned_equity_amount": _planned_equity_amount,
            "lot_rounding_gap_amount": _lot_rounding_gap,
            "price_basis_diff_amount": _price_basis_diff,
            "exec_basis_total_assets": _exec_basis_total_assets,
        },
        "actions": plans,
        "cash_alternatives": cash_alternatives,
        "tradingagents_view": _decision_view(args.decision),
        "llm_targets": llm_targets,
        "llm_conflicts": llm_conflicts,
        "llm_final_decision": llm_final_decision,
        # 🛡️ TA 合规筛选（否决 / 顺延）——**独立字段**：
        #    不改 `llm_targets` / `llm_conflicts`（金标 A1 不动）⇒ 只新增披露与执行结果。
        "ta_screening": _ta_screening,
        "risk_disclosure": report.get("risk_disclosure", []),
        "warnings": list(report.get("warnings", [])) + list(account_weight_warnings)
        + ([_pool_truncated] if _pool_truncated else [])
        + ([_pool_whitelist_warning] if _pool_whitelist_warning else [])
        + ([(
            f"🔴 **权益超配（不可消除）**：账户在「被 TA 一票否决但仍在持仓」的标的上的"
            f"权重，合计已超出**本轮生效上限** "
            f"{float(_ta_screening.get('equity_cap_effective') or _ta_screening.get('equity_cap') or 0.8):.1%}"
            f"（规则层硬上限 {float(_ta_screening.get('equity_cap') or 0.8):.1%}）"
            f"达 {float(_ta_screening.get('equity_cap_breach') or 0.0):.2%}"
            "（TA 一票否决 ⇒ **不许卖** ⇒ 该超配是事实）⇒ 需人工决定是否豁免"
        )] if float(_ta_screening.get("equity_cap_breach") or 0.0) > 1e-9 else []) + ([
            ("ℹ️ 未找到 LLM 决策文件 ⇒ 本计划**只有 qlib 规则层结论**（无 LLM 第二意见、无冲突表）"
             if not (args.decision and Path(args.decision).exists())
             else "ℹ️ LLM 决策文件存在但**本轮未完成**（失败占位）⇒ 本计划**只有 qlib 规则层结论**"
                  "（无 LLM 第二意见、无冲突表）"),
        ] if not stale_info.get("llm_present", False) else []),
        "disclaimer": report.get("disclaimer", ""),
        "notes": [
            "TradingAgents 结论是第二意见；与 qlib 规则层冲突时以规则层 + 真实持仓为准。",
            "目标股数按 100 股整手向下取整；现金不足时按 final_score 降序削减买入并记录。",
            "费用按 ETF 口径（免印花税、免过户费）：佣金 commission_bp + 单边滑点 slippage_bp。",
            "**价格基准**：目标股数一律按 asof（信号日）收盘价折算；账户快照里的「现价」是"
            "更晚一日的收盘价，只用于持仓对账，不参与股数计算（该 ETF 无行情时才退回账户价并标注）。",
            "🔴 **信号日 ≠ 执行日**：本文件里的调仓节点是**信号日**（T 日收盘出信号）；"
            "按 config.EXECUTION_LAG_DAYS=1，**实际下单日 = 信号日 + 1 个交易日的收盘**。"
            "在信号日当天下单 = 拿上一次的旧信号成交 ⇒ **早于回测口径一天，不在回测假设内**。",
            "**执行日必须重算**：信号日收盘后要按当日收盘价重新折算整手股数，"
            "并重新跑一遍信号（LLM 第二意见明确要求）；本文件的股数与金额是按 "
            + str(asof) + " 收盘价算的样例值，执行前必须重算。",
        ] + ([
            "ℹ️ **本计划不含 LLM 第二意见**：无可用 LLM 决策（"
            + str(stale_info.get("absent_reason") or "未指定")
            + "）⇒ 目标权重全部来自 qlib 规则层 + 真实持仓/资金。",
        ] if not stale_info.get("llm_present", False) else []) + ([
            # 🔴 M-F4：区分「**验不了**」（三件套缺件）与「**验过了、确实过期**」。
            #    两者对用户的处置动作一样（以规则层为准 + 重跑第 5~6 步），但**性质不同** ⇒
            #    措辞必须分开，否则用户会以为「重跑一遍就好」，实际是「上下文/决策件压根不在那目录」。
            ("🔴 **LLM 第二意见无法校验（unverifiable）**：本文件里的 LLM 意见**没验过**"
             "（" + "；".join(stale_info.get("reasons", [])) + "）⇒ **冲突表里的 LLM 目标权重不可作数**，"
             "以规则层为准；请确认上下文三件套与决策件同目录，必要时重跑第 5~6 步。")
            if stale_info.get("unverifiable_reason")
            else (
                "🔴 **LLM 第二意见已过期（stale）**：本文件里的 LLM 结论不是基于当前这版分析报告跑出来的"
                "（" + "；".join(stale_info.get("reasons", [])) + "）⇒ **冲突表里的 LLM 目标权重不可作数**，"
                "以规则层为准；需要 LLM 意见请重跑第 5~6 步。"
            )
        ] if stale_info.get("stale") else []),
    }
    return plan


def render_markdown(plan: dict) -> str:
    meta = plan["meta"]
    nxt = plan.get("next_rebalance", {}) or {}
    flow = plan["cash_flow"]
    lines = [
        "# 最终调仓建议 · A股 ETF 轮动",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 数据基准日（asof）：**{meta['asof']}**",
        f"- **调仓信号日：{nxt.get('signal_date') or nxt.get('target_date', '未确定')}**"
        f"（{nxt.get('rule', '')}），距**信号日（asof {meta.get('asof')}）** "
        f"**{nxt.get('trading_days_until', '?')} 个交易日**"
        f"（该天数**自信号日起算**，不是自本文件生成日起算），"
        f"日历校验={nxt.get('in_calendar_verified')}",
        f"- 🔴 **执行日 = {nxt.get('execution_date') or '算不出来'}**"
        f"（{nxt.get('execution_rule') or '引擎口径 execution_lag_days'}）"
        + (
            f"⇒ **{nxt.get('signal_date') or nxt.get('target_date')} 只是信号日，"
            f"真正下单在 {nxt.get('execution_date')} 收盘**；"
            f"本文件的股数与金额是按 **{meta.get('asof')} 收盘价**折算的样例值，"
            f"**须在 {nxt.get('signal_date') or nxt.get('target_date')} 收盘后按当日收盘价重算整手股数**，"
            f"再于 {nxt.get('execution_date')} 收盘下单。"
            if nxt.get("execution_date") else
            "⇒ ⚠️ 交易日历未覆盖，**执行日算不出来**（不许编日期）⇒ 下单前必须人工确认。"
        ),
        f"- 账户：总资产 **{meta['total_assets']:.2f} 元**，期初现金 **{meta['cash']:.2f} 元**，"
        f"目标持仓数 {meta.get('target_n_holdings')}（缓冲 {meta.get('hold_buffer')}）",
        f"- 打分权重：{meta.get('weights_used')}",
        "",
        "## 一、逐只操作清单",
        "",
        f"> ℹ️ **本清单的股数与金额是按 {meta.get('asof')}（信号日）收盘价折算的样例值**；"
        + (
            f"**信号日 {nxt.get('signal_date') or nxt.get('target_date')} 收盘后必须按当日收盘价重算整手股数**，"
            f"并于**执行日 {nxt.get('execution_date')} 收盘**下单"
            + (f"（引擎口径 {nxt.get('execution_rule')}）。" if nxt.get("execution_rule") else "。")
            if nxt.get("execution_date") else
            "⚠️ 执行日算不出来 ⇒ 下单前必须人工确认执行日。"
        ),
        "",
        "| 代码 | 名称 | 当前股数 | 现价 | 当前市值 | 盈亏率 | 目标权重 | 目标金额 | 目标股数 | 动作 | 金额 | 理由 |",
        "|:--|:--|--:|--:|--:|--:|--:|--:|--:|:--|--:|:--|",
    ]
    for p in plan["actions"]:
        def _num(value, fmt, scale=1.0, sign=False):
            """None ⇒ 破折号（不编数字）。"""
            if value is None:
                return "—"
            text = format(value * scale, "+.2f" if sign else ".2f")
            return text if sign else format(value * scale, ".2f")

        def _weight_cell(p):
            """目标权重：不可建仓 / 现金不足被削减的席位标成「原值 → 可达值」（权重归现金）。

            🔴 2026-11-04 夜实测修（M-F）：旧实现只在 `weight_to_cash` 非零时写死「→ 0%」，
            现金不足**部分**削减的席位会把原值原样显示 ⇒ 看不到实际能买到多少。
            ⇒ 改为按 `target_weight_effective`（可达权重）渲染。
            ⚠️ 可达权重为 0 时仍输出「→ **0%**」⇒ 正式产物逐字节不变。
            """
            tw = float(p.get("target_weight") or 0.0)
            twe = p.get("target_weight_effective")
            twe = tw if twe is None else float(twe)
            if abs(twe - tw) < 1e-12:
                return f"{tw * 100:.1f}%"
            if abs(twe) < 1e-12:
                return f"{tw * 100:.1f}% → **0%**"
            return f"{tw * 100:.1f}% → **{twe * 100:.1f}%**"

        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | **{}** | {} | {} |".format(
                p["code"],
                p["name"],
                p["current_shares"],
                "—" if p["price"] is None else f"{p['price']:.4f}".rstrip("0").rstrip("."),
                _num(p["current_market_value"], ".2f"),
                "—" if p["pnl_pct"] is None else f"{p['pnl_pct'] * 100:+.2f}%",
                _weight_cell(p),
                "—" if p["target_amount"] is None else f"{p['target_amount']:.0f}",
                "—" if p["target_shares"] is None else p["target_shares"],
                p["action"],
                "—" if p["est_amount"] is None else f"{p['est_amount']:+.2f}",
                p["reason"],
            )
        )
    # ---- 一之二 · 本账户可执行口径（整手可行性）----
    _unb = flow.get("unbuildable_slots") or []
    _alts = plan.get("cash_alternatives") or []
    if _unb:
        lines += [
            "",
            "## 一之二、⚠️ 本账户可执行口径（整手可行性）",
            "",
            f"账户总资产 **{meta['total_assets']:.2f} 元**，单席目标 **"
            f"{meta['total_assets'] * 0.2:.0f} 元**（20%）；ETF 最小交易单位 **100 份**。",
            "",
            "| 代码 | 名称 | 引擎目标权重 | 一手成本（元） | 处理 |",
            "|:--|:--|--:|--:|:--|",
        ]
        for u in _unb:
            lines.append(
                f"| {u['code']} | {u['name']} | {u['target_weight'] * 100:.1f}% | "
                f"{u['one_lot_cost']:.0f} | **不可建仓 ⇒ 权重归现金** |"
            )
        lines += [
            "",
            f"⇒ **计划**权益仓位 = **{flow.get('invested_weight_planned', 0) * 100:.1f}%**"
            f"（{(flow.get('planned_equity_amount') or 0):.2f} 元），"
            f"其余 **{flow.get('target_cash_weight', 0) * 100:.1f}%** 留在现金。",
            f"⇒ 🔴 **实际落地**（100 股整手**向下取整**后）："
            f"权益 **{(flow.get('equity_amount_realized') or 0):.2f} 元 = "
            f"{(flow.get('equity_weight_realized') or 0) * 100:.2f}%**"
            f"（= 计划 {(flow.get('invested_weight_planned') or 0) * 100:.1f}% − 整手取整缺口 "
            f"**{(flow.get('lot_rounding_gap_amount') or 0):.2f} 元 / "
            f"{((flow.get('lot_rounding_gap_amount') or 0) / (meta['total_assets'] or 1) * 100):.2f}pp**，"
            f"缺口**自动留在现金**）；现金 **{flow['cash_end']:.2f} 元 = "
            f"{(flow.get('cash_weight_realized') or 0) * 100:.2f}%**。",
            f"⇒ ℹ️ 为何「实际权益% + 实际现金% ≠ 100%」：账户快照价与调仓节点执行价**不是同一口径** ⇒ "
            f"价格基差 **{(flow.get('price_basis_diff_amount') or 0):.2f} 元** ＋ 已付费用 "
            f"**{(flow.get('sell_fees') or 0) + (flow.get('buy_fees') or 0):.2f} 元** "
            f"= {(((flow.get('price_basis_diff_amount') or 0)) + (flow.get('sell_fees') or 0) + (flow.get('buy_fees') or 0)):.2f} 元"
            f" ⇒ 执行价口径总资产 {(flow.get('exec_basis_total_assets') or 0):.2f} 元。",
            "",
        ]
        if _alts:
            lines += [
                "若要把部分现金换成货币 ETF（用户原话「余款留现金/货币 ETF」），"
                "**至少 1 手**，且权重会超出 20% 单席 ⇒ 属 5 只等权结构之外的额外决策：",
                "",
                "| 代码 | 名称 | 1 手成本（元） | 买入后占账户 |",
                "|:--|:--|--:|--:|",
            ]
            for a in _alts:
                _w = a.get("weight_if_one_lot")
                lines.append(
                    f"| {a['code']} | {a['name']} | {a['one_lot_cost']:.0f} | "
                    f"{('%.1f%%' % (_w * 100)) if _w else '—'} |"
                )
            lines += [""]

    lines += [
        "",
        "## 二、现金流小结",
        "",
        "| 项 | 金额（元） |",
        "|:--|--:|",
        f"| 期初现金 | {flow['cash_begin']:.2f} |",
        f"| 卖出回款 | +{flow['sell_proceeds']:.2f} |",
        f"| 卖出费用 | -{flow['sell_fees']:.2f} |",
        f"| 买入支出 | -{flow['buy_amount']:.2f} |",
        f"| 买入费用 | -{flow['buy_fees']:.2f} |",
        f"| **期末现金** | **{flow['cash_end']:.2f}** |",
        f"| 买入前可用 | {flow['available_before_buy']:.2f} |",
        f"| 买入后现金余量（**正=富余**） | {flow.get('surplus', flow['headroom']):.2f} |",
        f"| 现金缺口（正=缺钱） | {flow['deficit']:.2f} |",
        f"| 计划权益仓位权重 | {flow.get('invested_weight_planned', 0) * 100:.1f}% |",
        f"| 计划现金权重 | {flow.get('target_cash_weight', 0) * 100:.1f}% |",
        f"| **实际落地权益**（整手取整后，元） | {(flow.get('equity_amount_realized') or 0):.2f} |",
        f"| **实际落地权益权重** | {(flow.get('equity_weight_realized') or 0) * 100:.2f}% |",
        f"| **实际落地现金权重** | {(flow.get('cash_weight_realized') or 0) * 100:.2f}% |",
        f"| 整手取整缺口（计划权益 − 实际权益，元） | {(flow.get('lot_rounding_gap_amount') or 0):.2f} |",
        f"| 价格基差（账户快照价 vs 执行价，元） | {(flow.get('price_basis_diff_amount') or 0):.2f} |",
        f"| 执行价口径总资产（元） | {(flow.get('exec_basis_total_assets') or 0):.2f} |",
        "",
    ]
    if flow["scaled_down"]:
        lines += ["因现金不足被削减/放弃的买入：", ""]
        for s in flow["scaled_down"]:
            lines.append(f"- {s['code']} {s['name']}")
        lines.append("")
    elif _unb:
        lines += [
            f"现金约束：**可执行的买入全部可执行**（无需削减）；另有 {len(_unb)} 个席位"
            "因整手买不起而**权重归现金**（见「一之二」节）。",
            "",
        ]
    else:
        lines += ["现金约束：**全部买入均可执行**（无需削减）。", ""]

    # ---- 🛡️ TA 合规筛选（否决 / 顺延）——**独立章节**，与 LLM 冲突表并列 ----
    _scr = plan.get("ta_screening") or {}
    if _scr.get("enabled"):
        _rej = _scr.get("rejected") or []
        _sub = _scr.get("substituted") or []
        _pairs = _scr.get("screen_pairs") or []
        lines += ["## 二之四、🛡️ TA 合规筛选（否决 / 顺延）", ""]
        lines += [
            "> 口径：TA（TradingAgents）拥有**一票否决权** ⇒ 被点名否决的标的"
            "**从本轮候选里剔除**（**不是卖出**，目标权重不写 0），并**顺延**挑选"
            "下一个**不同主题**的资产补位；规则层的重合度判定口径**不变**。",
            "",
        ]
        # 🔴 2026-11-11（真机缺陷 ①）：**池外代码如实披露**（不许静默丢弃）。
        #    LLM 原文里出现、但**不在 ETF 池内**的代码（笔误 / 幻觉）
        #    ⇒ 已从否决名单剔除（它不是候选池的标的 ⇒ 否决它没有意义）
        #    ⇒ 但必须在这里**写一行**，让用户看得见。
        _out_pool = _scr.get("rejected_out_of_universe") or []
        for _o in _out_pool:
            lines.append(
                f"⚠️ LLM 原文出现池外代码 {_o.get('code')}"
                "（不在 ETF 池内，已剔除、不参与否决/顺延）"
            )
        if _out_pool:
            lines.append("")
            for _o in _out_pool:
                _why = str(_o.get("reason") or "—").replace("|", "／")[:160]
                lines.append(
                    f"- {_o.get('code')}{_o.get('name') or ''}：{_why}"
                    f"（出处章节：{_o.get('source_section') or '—'}）"
                    "⇒ **不在 ETF 池内** ⇒ 不是候选池的标的 ⇒ 否决它没有意义 ⇒ 已剔除"
                )
            lines.append("")
        # 🔴 2026-11-11（Beta 避险落地）：披露「TA 建议的权益上限 ⇒ 本轮生效上限」。
        #    ⚠️ 只有**读到** TA 的目标口径权益建议时才加这段 ⇒ 没有建议 ⇒ 产物逐字节不变。
        _cap_rec = _scr.get("equity_cap_ta_recommendation") or {}
        if _cap_rec.get("pct") is not None:
            _cap_eff = _scr.get("equity_cap_effective")
            _cap_base = _scr.get("equity_cap")
            _cap_scale = _scr.get("equity_cap_scale") or {}
            if _cap_rec.get("reason") == "accepted":
                lines += [
                    "**Beta 避险（TA 建议压降组合权益上限）**：TA 建议本轮权益上限 **"
                    f"{float(_cap_rec.get('pct') or 0.0) * 100:.1f}%** ⇒ 本轮**生效上限 = "
                    f"{float(_cap_eff or 0.0) * 100:.1f}%**（规则层硬上限 "
                    f"{float(_cap_base or 0.0) * 100:.1f}% = 1 − 现金预留比例；"
                    "**只许压降、不许抬高**）⇒ 席位目标权重**等比缩放**（系数 "
                    f"{float(_cap_scale.get('factor') or 1.0):.4f}），差额**留现金**。",
                    "",
                    f"> TA 原文证据：{str(_cap_rec.get('evidence') or '（未记录证据句）')[:200]}",
                    "",
                ]
            else:
                lines += [
                    "**Beta 避险（权益上限）**：TA 提到的组合权益 "
                    f"**{float(_cap_rec.get('pct') or 0.0) * 100:.1f}%** "
                    "**不低于**规则层硬上限 "
                    f"{float(_cap_base or 0.0) * 100:.1f}% ⇒ **不采纳**（只许压降）"
                    "⇒ 本轮生效上限不变。",
                    "",
                ]
        if not _rej:
            lines += ["本轮 TA **未点名否决**任何标的 ⇒ 候选池与规则层计划**逐条一致**。", ""]
        else:
            lines += [f"TA **一票否决**了 **{len(_rej)}** 只标的（已从本轮候选中剔除）：", ""]
            lines += ["| 代码 | 名称 | 理由（原文） | 出处章节 |", "|:--|:--|:--|:--|"]
            for r in _rej:
                _why = (r.get("reason") or "—").replace("|", "／")[:160]
                lines.append(f"| {r.get('code')} | {r.get('name') or '—'} | {_why} | "
                             f"{r.get('source_section') or '—'} |")
            lines += [""]
            if _scr.get("held_not_touched"):
                lines += ["> 🔴 被否决的标的里有**当前持仓** ⇒ 本轮**只剔除候选、"
                          "不动持仓**（不生成卖出动作）。", ""]
            _orph = _scr.get("orphaned_holdings") or []
            if _orph:
                _orph_rows = {str(r.get("code")): r
                              for r in (_scr.get("orphaned_holding_rows") or [])}
                lines += [
                    f"🔴 **孤儿持仓（{len(_orph)} 只）**：被否决且**仍在持仓** ⇒ "
                    "在「一、逐只操作清单」里**各占一行『维持』**（目标权重 = **当前权重**，"
                    "**不买不卖**，但**计入**权益/现金口径 ⇒ 计划权益/现金/总资产恒等式自洽）。",
                    "",
                    "| 代码 | 名称 | 当前权重（= 目标权重） | 规则层原席位预算 |",
                    "|:--|:--|--:|--:|",
                ]
                for _c in _orph:
                    _r = _orph_rows.get(str(_c), {})
                    _cw = _r.get("current_weight")
                    _rt = _r.get("rule_target_weight")
                    lines.append(
                        f"| {_c} | {_r.get('name') or '—'} | "
                        f"{'—' if _cw is None else format(float(_cw) * 100, '.2f') + '%'} | "
                        f"{'—' if _rt is None else format(float(_rt) * 100, '.2f') + '%'} |"
                    )
                lines += [""]
            _cap_skip = _scr.get("cap_skipped") or []
            if _cap_skip:
                lines += [
                    f"🔴 **席位交互（权益上限）**：权益上限 = 1 − 现金预留比例 = "
                    f"{float(_scr.get('equity_cap') or 0.8) * 100:.1f}%。"
                    "被否决且仍在持仓者**仍占席位** ⇒ 顺延补位若会让权益合计突破上限 ⇒ "
                    "**不补位**（宁缺毋滥，避免多出一席、权益超配）：",
                    "",
                ]
                for _c in _cap_skip:
                    lines.append(f"- {_c.get('code')} {_c.get('name') or ''}："
                                 f"{_c.get('reason') or ''}")
                lines += [""]
        if _sub:
            lines += ["**顺延记录**（补位者 ← 被否决者）：", ""]
            lines += ["| 补位代码 | 补位名称 | 最终分 | 主题 | 替补对象 | 替补对象名称 |",
                      "|:--|:--|--:|:--|:--|:--|"]
            for s in _sub:
                _fs = s.get("final_score")
                lines.append(
                    f"| {s.get('code')} | {s.get('name') or '—'} | "
                    f"{'—' if _fs is None else format(float(_fs), '.4f')} | "
                    f"{s.get('theme') or '—'} | {s.get('substituted_for') or '—'} | "
                    f"{s.get('substituted_for_name') or '—'} |")
            lines += [""]
        elif _rej:
            lines += [f"**顺延**：本轮**没有**顺延补位 ⇒ "
                      f"{_scr.get('no_substitute_reason') or '（原因未记录）'}", ""]
        if _pairs:
            lines += ["**高重合预警对**（**更宽口径**：成分重叠 ≥ 0.50 或 120 日相关性 ≥ 0.80 "
                      "或同族 ⇒ **只提示**，不改变规则层判定；规则层仍按"
                      "「重叠 ≥ 0.90 或相关性 ≥ 0.95」逐对拦截）：", "",
                      "| 代码A | 名称A | 代码B | 名称B | 成分重叠 | 120日相关性 | 同族 | 规则层冲突对 |",
                      "|:--|:--|:--|:--|--:|--:|:--:|:--:|"]
            for p in _pairs:
                lines.append(
                    f"| {p.get('a')} | {p.get('a_name') or '—'} | {p.get('b')} | "
                    f"{p.get('b_name') or '—'} | {float(p.get('weight_overlap') or 0.0):.3f} | "
                    f"{float(p.get('corr120') or 0.0):.3f} | "
                    f"{'是' if p.get('same_theme') else '否'} | "
                    f"{'**是**' if p.get('rule_conflict') else '否'} |")
            lines += [""]
        elif _scr.get("screen_note"):
            lines += [f"（高重合预警：{_scr['screen_note']}）", ""]

    # ---- 🔴 2026-11-05 04:4x（M-P）：Trader 的**最终裁决**（独立于冲突表） ----
    _final = plan.get("llm_final_decision") or {}
    if _final.get("available"):
        _rows = _final.get("reconciliation") or []
        _diff = [r for r in _rows if r.get("status") == "与规则层不一致"]
        lines += ["## 二之二、🔴 LLM **最终裁决**（Trader「最终交易决定」）——**下单前必看**", ""]
        _rating = _final.get("rating") or "未给评级"
        _src = _final.get("source_section") or "最终交易决定"
        lines += [f"**最终评级**：**{_rating}** ｜ 原文章节：{_src} ｜ 摘要来源：决策文件"
                  "「最终交易决定」章节（**散文**，非表格）", ""]
        lines += [
            "> 🔴 **口径（必读）**：本节是 Trader 的**最终裁决**；下一节的「冲突表」报的是"
            "**与规则层差得最远的子智能体视图**（多头/空头表）——**两者不是一回事**。",
            f"> 本轮实测二者**{len(_diff)} 处不一致**"
            + ("：" + "、".join(f"**{r['code']}**" for r in _diff) if _diff else "")
            + " ⇒ **以本节为准**。",
            "> ⚠️ 但**成交动作仍以规则层 + 真实持仓为准**：最终裁决给的是**目标权重**，"
            "**不是股数**；原文明说股数必须在**信号日收盘后按当日价重算**。",
            "",
        ]
        if _rows:
            lines += ["| 代码 | 名称 | 最终裁决目标 | 方向 | 规则层计划 | 规则层实际落地 | "
                      "多头表 | 空头表 | 与规则层 | 原文证据 |",
                      "|:--|:--|--:|:--|--:|--:|--:|--:|:--|:--|"]
            for r in _rows:
                def _pct(v):
                    return "—" if v is None else f"{v:.1f}%"
                _ev = (r.get("evidence") or "").replace("|", "／")[:110]
                _st = r.get("status", "")
                _gap = r.get("gap_vs_rule_pp")
                _stxt = _st if _gap is None else f"{_st}（{_gap:+.1f}pp）"
                _dir = r.get("final_direction") or "—"
                lines.append(
                    f"| {r['code']} | {r.get('name', '')} | **{_pct(r.get('final_target_pct'))}** | "
                    f"{_dir} | {_pct(r.get('rule_target_pct'))} | "
                    f"{_pct(r.get('rule_effective_pct'))} | {_pct(r.get('bull_pct'))} | "
                    f"{_pct(r.get('bear_pct'))} | {_stxt} | {_ev} |")
            lines += [""]
        # 🔴 2026-11-11（缺陷 D）：因 TA 一票否决而**移出**本轮组合的代码
        #    ⇒ 冲突表里的对应条目随之移除 ⇒ 这里**显式标注**，不让它悄悄消失。
        _ta_moved = _final.get("ta_moved_out") or []
        if _ta_moved:
            lines += [
                "> 🔴 **因 TA 一票否决而移出**：下列代码**不在本轮目标组合**里 ⇒ "
                "「二之三 冲突表」里的对应条目已随之移除（本节表格仍**单列一行**以示否决）：",
                "",
            ]
            for _m in _ta_moved:
                lines.append(
                    f"- **{_m.get('code')} {_m.get('name') or ''}**："
                    f"{_m.get('reason') or '因 TA 一票否决而移出本轮候选'}"
                    + ("（LLM 表里原本有它的条目 ⇒ 该分歧已不在冲突表里）"
                       if _m.get("had_llm_view") else "（LLM 表里没有它的条目）")
                )
            lines += [""]
        if _diff:
            lines += ["**🔴 最终裁决与规则层不一致之处（须用户拍板）**："]
            for r in _diff:
                _note = "；".join(r.get("notes") or []) or "无附加说明"
                lines.append(
                    f"- **{r['code']} {r.get('name', '')}**：最终裁决 "
                    f"**{r.get('final_target_pct')}%**（{r.get('final_direction') or '—'}）"
                    f" vs 规则层**实际落地 {r.get('rule_effective_pct')}%**"
                    f"（计划 {r.get('rule_target_pct')}%，差 {r.get('gap_vs_rule_pp'):+.1f}pp）"
                    f"｜多头表 {r.get('bull_pct')}%、"
                    f"空头表 {r.get('bear_pct')}%｜{_note}")
            lines += [""]
        _pt = _final.get("post_trade") or {}
        if _pt:
            _ptxt = []
            if _pt.get("equity_pct") is not None:
                _ptxt.append(f"组合层权益敞口 ≈{_pt['equity_pct']}%"
                             "（**组合级数字，不归属任何单只标的**）")
            if _pt.get("cash_amount") is not None:
                _ptxt.append(f"现金 ≈{_pt['cash_amount']:,.0f} 元"
                             + (f"（{_pt['cash_pct']}%）" if _pt.get("cash_pct") is not None else ""))
            if _ptxt:
                lines += ["**最终裁决给的调仓后状态**：" + " ｜ ".join(_ptxt), ""]
        if _final.get("unparsed_codes"):
            lines += [f"⚠️ 提到但**未能结构化提取**的代码：{', '.join(_final['unparsed_codes'])}"
                      "（原文写法不含可判定的数字 ⇒ 留空，**不猜**）", ""]
        lines += ["> **原文摘要**（未改写）：", ">"]
        _sum = (_final.get("summary") or "").splitlines()
        lines += ["> " + ln for ln in (_sum[:6] if _sum else ["（无）"])]
        lines += [""]

    conflicts = plan.get("llm_conflicts") or []
    # 🔴 2026-11-13（真缺陷 · 交付件 `snapshots/20261113_092926` 实证）：
    #    原实现 `if conflicts:` ⇒ **冲突表为空时整个「二之三」节不渲染**
    #    ⇒ 但正文里「二之二」节与「二之四」节仍在**引用**「二之三 冲突表」
    #      （"「二之三 冲突表」里的对应条目已随之移除"）⇒ **悬空交叉引用**；
    #    ⇒ 且章节编号变成 二 / 二之二 / 二之四（**二之三缺号**）⇒ 用户对不上号。
    #    ⇒ 现在**始终渲染**本节：空表时如实写「本轮无冲突」，编号与引用**自洽**。
    _stale = bool((plan.get("meta", {}).get("llm_stale") or {}).get("stale"))
    _ttl = "## 二之三、⚠️ LLM 第二意见 vs 规则层的冲突（**默认以规则层为准**，但用户应知情）"
    if _stale:
        _ttl = ("## 二之三、🔴 LLM 第二意见**已过期**（基于旧版分析报告）⇒ 下表**不可作数**，"
                "以规则层为准")
    lines += [_ttl, ""]
    if not conflicts:
        lines += [
            "**本轮无冲突条目**：规则层目标与 LLM 第二意见的偏离**均在 ±2pp 以内**"
            "（口径：`|规则% − LLM%| > 2`）⇒ 没有需要用户拍板的分歧。",
            "",
            "> ℹ️ 「空冲突表」是**合法结果**（不是解析器坏了）："
            "只有当原文重放能找出 >2pp 分歧、却没进本表时，才判解析器失效。",
            "",
        ]
    else:
        lines += ["| 代码 | 名称 | 规则层目标 | LLM 目标 | LLM 方向 | 谁说的 | LLM 理由 |", "|:--|:--|--:|--:|:--|:--|:--|"]
        for c in conflicts:
            rule_txt = "—" if c.get("rule_target_pct") is None else f"{c['rule_target_pct']:.0f}%"
            who = _LLM_WHO_LABEL.get(c.get("llm_who", ""), "LLM")
            _alt = c.get("all_divergent_views") or []
            _alt_txt = ""
            if len(_alt) > 1:
                _alt_txt = "（另有：" + "、".join(
                    f"{_LLM_WHO_LABEL.get(v.get('who'), 'LLM')} "
                    f"{v.get('target_pct', 0):.0f}%" for v in _alt[1:]) + "）"
            lines.append(
                f"| {c['code']} | {c['name']} | {rule_txt} | {c['llm_target_pct']:.0f}% | "
                f"{c['llm_direction']} | {who}{_alt_txt} | {c.get('llm_reason', '')[:160]} |")
        lines += [""]

    view = plan.get("tradingagents_view")
    lines += ["## 三、TradingAgents 多智能体意见（第二意见）", ""]
    if view:
        lines += [view, "", "> ⚠️ 与规则层冲突时**以规则层 + 真实持仓为准**。", ""]
    else:
        lines += ["_（本次没有 TradingAgents 决策文件；决策完全来自 qlib 规则层。）_", ""]

    lines += ["## 四、风险提示（来自分析报告）", ""]
    for r in plan.get("risk_disclosure", []) or []:
        lines.append(f"- **[{r.get('severity', '?')}]** {r.get('title', '')}：{r.get('detail', '')}")
    lines += ["", "## 五、免责与纪律", ""]
    for n in plan.get("notes", []):
        lines.append(f"- {n}")
    lines += [f"- {plan.get('disclaimer', '本文件不构成投资建议。')}", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(C.ETF_RUN_ROOT / "etf_analysis_report.json"))
    ap.add_argument("--decision", default=str(C.ETF_RUN_ROOT / "tradingagents_decision.md"))
    ap.add_argument("--prices", default=str(C.ETF_DATA_ROOT / "etf_prices.csv"))
    ap.add_argument("--universe", default=str(C.ETF_DATA_ROOT / "etf_universe.csv"))
    # 🔴 2026-11-10：原先写死服务器绝对路径（换机器/换目录就找不到账户基准）
    #    ⇒ 改用**账户模块自己的口径**（account.ACCOUNT_SNAPSHOT_CSV，与 analyze.py 同源）。
    ap.add_argument(
        "--account",
        default=str(_ACCOUNT_SNAPSHOT_CSV_DEFAULT()),
        help="账户基准 CSV（缺省 = account.ACCOUNT_SNAPSHOT_CSV）",
    )
    ap.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    plan = build_plan(args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "final_rebalance_plan.json"
    md_path = out_dir / "final_rebalance_plan.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(plan), encoding="utf-8")
    print(f"[s6] wrote {json_path} ({json_path.stat().st_size} B)")
    print(f"[s6] wrote {md_path} ({md_path.stat().st_size} B)")
    for p in plan["actions"]:
        print(f"  {p['code']} {p['name']}: {p['action']} {p['delta_shares']} 股 "
              f"@ {p['price']} → {p['est_amount']} 元 | {p['reason']}")
    _deficit = float(plan["cash_flow"]["deficit"])
    _surplus = float(plan["cash_flow"].get("surplus", 0.0) or 0.0)
    _cash_tail = (
        f"⚠️ 现金缺口 {_deficit:.2f} 元（买入需按最终分降序削减）"
        if _deficit > 0
        else f"现金余量 {_surplus:.2f} 元，无缺口（全部买入可执行）"
    )
    print(f"[s6] cash {plan['cash_flow']['cash_begin']:.2f} → {plan['cash_flow']['cash_end']:.2f} "
          f"（{_cash_tail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
