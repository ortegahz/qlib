"""TradingAgents 决策层：把 ETF 量化分析报告喂进多智能体辩论，产出评级与辩论纪要。

【为什么用 Python API 而不是 CLI】
`cli/run.py:204` 仍走默认 `research_context=""` ⇒ 命令行注入没接线 ⇒ 必须用 API。

【口径】
- 研究上下文 = `ta_research_context.md`（由 `build_ta_context.py` 生成，量化层结论）；
- 模型 = MiniMax-M3.1-Flash（端点 api.minimaxi.com），`reasoning_effort=max`、
  `max_tokens=32768`（**effort=max 时思考 token 会吃光 max_tokens，4096 会正文 0 字**）；
- 只跑 **1 次**（一次全图 = 约 11~13 次 LLM 调用），产物落 `<落点根>/runs/`。

【产物】
- `tradingagents_decision.md`：最终评级 + 研究经理结论 + 多空/风险辩论要点 + Trader 计划；
- `tradingagents_decision.json`：结构化版（便于下游合成脚本消费）。

【失败口径】
任何异常 ⇒ 写一份**如实标注「未完成」**的 md（写明失败原因与已跑到的阶段），不许假装成功。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

#: 研究链根（`llm/` 不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as C  # noqa: E402

DEFAULT_TA_DIR = "/tmp/pycharm_project_658d3fc4/TradingAgents"
#: 产物根（落点 `<落点根>/runs`；与研究链 `config.ETF_RUN_ROOT` 同源）
DEFAULT_RUN_ROOT = C.ETF_RUN_ROOT


#: markdown 表格行（以竖线开头）—— **整块永不裁**（机器解析依赖表格行）。
_TABLE_ROW_RE = re.compile(r"^\s*\|")


def _split_table_blocks(text: str) -> list[tuple[bool, str]]:
    """把文本切成 ``(是否表格块, 文本)`` 序列：连续的表格行算**一个**表格块。

    🔴 为什么要按块切：表格行**必须整块保留或整块丢弃**——切一半会产出
       「`| 563020 | 买 | 0%`」这种**断行**，解析器会把它当成**残行**读出假目标。
    """
    blocks: list[tuple[bool, str]] = []
    buf: list[str] = []
    buf_is_table: bool | None = None
    for line in text.splitlines():
        is_table = bool(_TABLE_ROW_RE.match(line))
        if buf_is_table is None or is_table == buf_is_table:
            buf.append(line)
            buf_is_table = is_table
        else:
            blocks.append((bool(buf_is_table), "\n".join(buf)))
            buf = [line]
            buf_is_table = is_table
    if buf:
        blocks.append((bool(buf_is_table), "\n".join(buf)))
    return blocks


#: 散文块被裁剪时，至少保留多少字符（低于这个值就整块丢，不留半截）。
_MIN_PROSE_KEEP = 300

def _head_tail(text: str, limit: int) -> str:
    """头 1/3 + 省略标记 + 尾 2/3（旧的裁剪口径，只用于**散文**）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit // 3] + "\n\n...[中段省略]...\n\n" + text[-(2 * limit) // 3:]


def _tail(text: str, limit: int = 4000) -> str:
    """裁剪长文本，但**整块保留 markdown 表格**；只裁**散文**块。

    🔴 2026-11-05 05:0x（M-N 修）**实测事故（交付件级）**：
       旧实现按字符数裁「头 1/3 + 尾 2/3」、中段直接丢弃 ⇒ 2026-11-05 04:06 那一版
       `tradingagents_decision.md` 里：
         · **多头表**（表头 + 9 行）被腰斩 ⇒ 只剩表头 + 3 行 + 1 行**残行**；
         · **空头表**（表头 + 9 行）**整表消失**（它正好落在正文中间）；
       ⇒ **用户读的纪要里根本没有空头的意见**（结构化解析因为改读 `.json` 才幸免）。
    ⇒ 修法：表格块**整体保留**（哪怕超预算）；散文块按预算从**中段往两头**丢；
      实在丢不动时优先保表格 ⇒ 宁可 md 长一点，也不许把表格切碎。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    blocks = _split_table_blocks(text)
    if not any(is_table for is_table, _ in blocks):
        # 整段没有表格 ⇒ 保持旧口径（头 1/3 + 省略标记 + 尾 2/3）
        return _head_tail(text, limit)
    #: 🔴 `limit` 是**散文预算**；表格**额外**保留（不占预算）⇒ 输出可能超 limit。
    #:    这是刻意的取舍：**宁可 md 长一点，也不许把表格切碎**
    #:    （表格被切碎 = 用户读不到空头意见 + 机器解析读出假目标）。
    budget = limit
    prose_idx = [i for i, (is_table, _) in enumerate(blocks) if not is_table]
    prose_len = {i: len(blocks[i][1]) for i in prose_idx}
    used = 0
    kept_text: dict[int, str] = {}
    dropped: set[int] = set()
    #: 🔴 2026-11-10（M-O 修）**结构分隔块** = 前后邻居**都是表格块**的散文块。
    #:
    #:    实测事故（2026-11-09 17:49 那一轮交付 md，`probe_llm_stage_repro` P7/D4 红灯）：
    #:      源文本里两张表之间是 `"\n### 新目标及候选\n"`（12 字符），
    #:      旧口径把它当**普通散文** ⇒ 散文预算超支时**整块丢弃** ⇒ 后果有两条：
    #:        ① 分隔块里的**标题整段消失**（交付件丢内容 ⇒ M-N 同类事故）；
    #:        ② **两张表首尾相接**（中间没有任何空行）⇒ 在 markdown 渲染器里
    #:           两张表会被当成**一张表**（第二张表的表头变成第一张表的数据行）
    #:           ⇒ 用户读的纪要里第二张表**根本没有渲染出来**。
    #:
    #:    ⇒ 修法：分隔块**不许整块丢**（丢 ⇒ 两张表粘成一张 ⇒ 表不渲染）；
    #:      但它**仍受散文预算约束**（超预算 ⇒ 截短，而不是整段保留）。
    separator_idx = {
        i
        for i in prose_idx
        if 0 < i < len(blocks) - 1 and blocks[i - 1][0] and blocks[i + 1][0]
    }
    # 从**离中段最近**的散文块开始处理 ⇒ 头尾的可读性保留得最好
    for i in sorted(prose_idx, key=lambda i: (abs((len(blocks) - 1) / 2.0 - i), i)):
        length = prose_len[i]
        if used + length <= budget:
            used += length
            kept_text[i] = blocks[i][1]
            continue
        remaining = budget - used
        if remaining >= _MIN_PROSE_KEEP:
            # 🔴 散文块超预算 ⇒ **按头 1/3 + 尾 2/3 裁**（不是整块丢）：
            #    整块丢会把「四、正面回应空头的核心质疑」这类**核心论证**整段抹掉
            #    ⇒ 用户读到的纪要只剩表格、没有观点 ⇒ 与「辩论纪要」的定位不符。
            kept_text[i] = _head_tail(blocks[i][1], remaining)
            used += len(kept_text[i])
        elif i not in separator_idx:
            dropped.add(i)
        else:
            # 🔴 分隔块**不许整块丢**：散文预算已经见底，但仍要留下**头部**
            #    （表与表之间的标题通常就在开头）⇒ 两张表之间仍有分隔 ⇒ 不会粘连。
            kept_text[i] = _head_tail(blocks[i][1], _MIN_PROSE_KEEP)
    parts: list[tuple[bool, str]] = []
    marker_done = False
    for i, (is_table, block) in enumerate(blocks):
        if is_table:
            parts.append((True, block))
            continue
        if i in kept_text:
            parts.append((False, kept_text[i]))
            continue
        if not marker_done:
            parts.append((False, "...[中段省略]..."))
            marker_done = True
    # 🔴 2026-11-10（M-O 修）**表格前后都要留空行**：
    #    ① GFM 规定「表格不能打断段落」⇒ 表格紧跟在散文行后面会被当成
    #       **段落的延续** ⇒ 那张表**根本不渲染成表格**
    #       （实测：旧交付 md 里 8 张表有 3 张的前一行是散文/省略标记 ⇒ 3 张表不渲染）；
    #    ② 两张表之间没有空行 ⇒ 会被当成**一张表**（第二张表的表头变成第一张表的数据行）。
    out: list[str] = []
    for is_table, text in parts:
        if not is_table:
            out.append(text)
            continue
        if out and out[-1].strip():
            out.append("")
        out.append(text)
        out.append("")
    return "\n".join(out)


#: 「六、最终交易决定」的渲染预算（**散文不裁**；仅在异常超长时才兜底裁剪）
#: 🔴 2026-11-05 10:2x（M-Q）：旧口径是 1500 ⇒ 3,254 字的最终裁决被裁掉一多半
_FINAL_DECISION_LIMIT = 20000


def _sha1(path: Path) -> str:
    import hashlib
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def _load_account_json(path: Path) -> dict:
    """读**账户上下文 JSON**（每日流水线导出的实盘口径）。

    🔴 为什么要有这条路径（2026-11-05 正确性 bug）：
    默认口径读的是**研究链**的 `manu/etf_rotation/account.py`
    （2026-11-04 快照：现金 15,518.87 元 / 5 只持仓）
    ⇒ Portfolio Manager 拿一个**用户根本没持有**的组合做推理
    ⇒ 交付件混入无源结论（历史实测编出过「目标 15%」「止损 0.804」这类假论据）。
    ⇒ 每日流水线用 `--account-json` 把**实盘口径**喂进来。

    🔴 校验不过 ⇒ **直接报错**（**不许**静默回退到研究链 CSV ⇒ 那正是 bug 的根因）。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"账户上下文 JSON 结构不对（不是对象）：{path}")
    missing = [k for k in ("cash", "total_assets", "positions") if k not in payload]
    if missing:
        raise SystemExit(f"账户上下文 JSON 缺必需字段 {missing}：{path}")
    if not isinstance(payload.get("positions"), list):
        raise SystemExit(f"账户上下文 JSON 的 positions 不是数组：{path}")
    return payload


def _account_positions(payload: dict) -> list[dict]:
    """把账户上下文 JSON 的持仓映射成 TA ``Position`` 的入参（**纯函数** ⇒ 可离线自测）。

    ``cost`` 是**总成本**（元）⇒ 每股成本 = ``cost / shares``（TA 的 ``average_price``）。
    """
    out: list[dict] = []
    for pos in payload["positions"]:
        shares = float(pos["shares"])
        cost = float(pos.get("cost") or 0.0)
        out.append({
            "ticker": str(pos["code"]),
            "quantity": shares,
            "average_price": (cost / shares) if shares else None,
        })
    return out


def _render_md(payload: dict) -> str:
    """把决策 **json** 渲染成纪要 md（**纯函数** ⇒ 可从已有 json 重渲）。

    🔴 2026-11-05 05:0x（M-N）：抽成纯函数是为了支持 `--render-only` ——
       修完 `_tail`（表格不再被腰斩）后，**不需要重跑 LLM** 就能把新渲染口径
       套用到**已有那一轮**的决策上（json 是完整事实源，md 只是展示件）。
    """
    meta = payload.get("meta") or {}
    debate = payload.get("investment_debate") or {}
    risk = payload.get("risk_debate") or {}
    plan = payload.get("investment_plan", "") or ""
    trader = payload.get("trader_investment_plan", "") or ""
    decision = payload.get("final_trade_decision", "") or ""
    signal = payload.get("signal", "") or ""
    elapsed = float(payload.get("elapsed_seconds") or 0.0)
    ctx_path = meta.get("context_path", "")
    ctx_chars = meta.get("context_chars", 0)
    md = [
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        f"- 标的：**{meta.get('ticker')}**；决策日：**{meta.get('date')}**；"
        f"analysts=`{list(meta.get('analysts') or [])}`",
        f"- 模型：`{meta.get('deep_think_llm')}`（provider `{meta.get('provider')}`，"
        f"reasoning_effort=`{meta.get('reasoning_effort')}`，max_tokens=`{meta.get('max_tokens')}`）",
        f"- 上下文：`{ctx_path}`（{ctx_chars} 字符）",
        f"- 耗时：{elapsed:.0f} 秒",
        "",
        "## 一、最终评级",
        "",
        f"**{signal}**",
        "",
        "## 二、研究经理结论（Research Manager）",
        "",
        _tail(plan, 6000),
        "",
        "## 三、多空辩论要点",
        "",
        "### 多头（Bull）",
        "",
        _tail(debate.get("bull_history", ""), 2500),
        "",
        "### 空头（Bear）",
        "",
        _tail(debate.get("bear_history", ""), 2500),
        "",
        "### 辩论裁决",
        "",
        _tail(debate.get("judge_decision", ""), 3000),
        "",
        "## 四、风险辩论",
        "",
        "### 激进派",
        "",
        _tail(risk.get("aggressive_history", ""), 1500),
        "",
        "### 保守派",
        "",
        _tail(risk.get("conservative_history", ""), 1500),
        "",
        "### 中立派",
        "",
        _tail(risk.get("neutral_history", ""), 1500),
        "",
        "### 风险裁决",
        "",
        _tail(risk.get("judge_decision", ""), 2500),
        "",
        "## 五、Trader 交易计划",
        "",
        _tail(trader, 3000),
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        # 🔴 2026-11-05 10:2x（M-Q 修）：**最终裁决整段保留**，不许按散文预算裁剪。
        #    实测：json 里 `final_trade_decision` 有 3,254 字，而旧口径 `_tail(decision, 1500)`
        #    ⇒ md 里出现「...[中段省略]...」⇒ **Trader 的最终裁决在用户读的纪要里被裁掉了**
        #    （被裁掉的恰好可能是「目标权重 X%」那句 ⇒ 探针 A20③ 的回放会误红，
        #      而这**不是解析器造数**，是**渲染件缺内容**）。
        #    ⇒ 口径：**辩论/研究上下文可以裁，最终裁决不许裁**（它是交付件的一等公民）。
        _tail(decision, _FINAL_DECISION_LIMIT),
        "",
        "---",
        "",
        "⚠️ 本纪要由 LLM 多智能体辩论生成，**不构成投资建议**；"
        "量化层数字以 `etf_analysis_report.json` 为准，"
        "最终调仓清单以 `final_rebalance_plan.md` 为准。",
    ]
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser(description="TradingAgents ETF 轮动决策")
    ap.add_argument("--ta-dir", default=DEFAULT_TA_DIR)
    ap.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    ap.add_argument("--context", default=None, help="research_context md（默认 <run-root>/ta_research_context.md）")
    ap.add_argument("--ticker", default="512800", help="传给 agent 的标的（主持仓 + 最高分候选）")
    ap.add_argument("--date", default="2026-11-03")
    ap.add_argument("--analysts", default="news", help="逗号分隔；至少 1 个")
    ap.add_argument("--news-vendor", default="alpha_vantage",
                    help="新闻数据 vendor。默认用 alpha_vantage（无 key ⇒ 立即失败）而不是 "
                         "yfinance（实测 Yahoo 限流，每次调用退避 2+4+8s，会把整轮辩论拖死）")
    ap.add_argument("--max-recur", type=int, default=60,
                    help="图的最大递归步数（默认 60：analyst 工具轮 + 多空辩论 + 风险辩论共约 15~25 步；"
                         "设 6 会被 analyst 的多轮工具调用直接耗尽 ⇒ GraphRecursionError）")
    ap.add_argument("--offline-tools", type=int, default=1,
                    help="1=把数据工具打桩成「不可用」哨兵（默认）；"
                         "0=真联网（Yahoo 限流 + 无 FRED key ⇒ 实测要么空转要么抛异常掀翻整张图）")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--render-only", type=int, default=0,
                    help="1=**不跑 LLM**，只用已存在的 --out-json 重渲 --out-md（渲染器修复后"
                         "重出展示件用；json 是完整事实源 ⇒ 结果与重跑一遍一致，但零 API 成本）")
    ap.add_argument("--account-json", default=None,
                    help="🔴 账户上下文 JSON（**优先于** account.py 的默认研究链快照）："
                         "每日流水线把实盘口径导出后传进来 ⇒ Portfolio Manager 看到的是"
                         "用户真实持仓，而不是研究链基准")
    ap.add_argument("--reasoning-effort", default=None,
                    choices=["max", "high", "medium", "low"],
                    help="reasoning_effort 覆盖（优先级：**显式 env > 命令行 > .env**）；"
                         "🔴 effort=max 时 MiniMax 单次调用实测 185 秒 ⇒ 慢但更准，"
                         "medium 实测 91 秒")
    args = ap.parse_args()

    ta_dir = Path(args.ta_dir)
    run_root = Path(args.run_root)
    ctx_path = Path(args.context) if args.context else run_root / "ta_research_context.md"
    out_md = Path(args.out_md) if args.out_md else run_root / "tradingagents_decision.md"
    out_json = Path(args.out_json) if args.out_json else run_root / "tradingagents_decision.json"

    # 🔴 2026-11-05 05:0x（M-N）：`--render-only` ⇒ 只重渲 md，**不调 LLM、不改 json**。
    if args.render_only:
        if not out_json.exists():
            raise SystemExit(f"决策 json 不存在：{out_json}（--render-only 需要现成的 json）")
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit(f"决策 json 结构不对（不是对象）：{out_json}")
        out_md.write_text(_render_md(payload), encoding="utf-8")
        print(f"✅ 已重渲 {out_md}（源 json {out_json}，未调用 LLM）")
        return 0

    if not ctx_path.exists():
        raise SystemExit(f"研究上下文不存在：{ctx_path}（先跑 build_ta_context.py）")
    context = ctx_path.read_text(encoding="utf-8")

    # ⚠️ 必须在 import tradingagents **之前**加载 .env：
    #    default_config 在 import 期就把 TRADINGAGENTS_* 环境变量固化进 DEFAULT_CONFIG。
    from dotenv import load_dotenv
    # 🔴 记录 **.env 载入前** 的显式 env（优先级：显式 env > 命令行 > .env）
    _pre_env_effort = os.environ.get("TRADINGAGENTS_REASONING_EFFORT", "").strip()
    load_dotenv(ta_dir / ".env", override=False)
    # 🔴 reasoning_effort 优先级：**显式 env > 命令行 > .env**
    #    （load_dotenv(override=False) ⇒ .env 只填空位 ⇒ 已有 env 不会被 .env 覆盖）
    if args.reasoning_effort:
        if _pre_env_effort:
            print(f"[INFO] reasoning_effort：环境变量已有值 ⇒ "
                  f"命令行 --reasoning-effort {args.reasoning_effort} 不生效（显式 env 优先）")
        else:
            os.environ["TRADINGAGENTS_REASONING_EFFORT"] = args.reasoning_effort
            print(f"[INFO] reasoning_effort：命令行覆盖 = {args.reasoning_effort}")
    sys.path.insert(0, str(ta_dir))

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext, Position

    # 🔴 账户上下文必须喂进图（2026-11-04 夜实测）：不喂 ⇒ Portfolio Manager 只能拿到
    #    「Portfolio context: not provided」⇒ 它在**不知道持仓与现金**的前提下
    #    编出「目标 15%」「止损 0.804」「组合拖累 1.2%」等无源数字
    #    （上游风险披露明明写着「仍没有组合级硬止损」）⇒ 交付件里混入假论据。
    if args.account_json:
        # 🔴 每日流水线的**实盘口径**（优先于研究链基准）—— 2026-11-05 正确性修复。
        _acct = _load_account_json(Path(args.account_json))
        _positions = [
            Position(ticker=p["ticker"], quantity=p["quantity"], average_price=p["average_price"])
            for p in _account_positions(_acct)
        ]
        _portfolio = PortfolioContext(
            cash=float(_acct["cash"]),
            currency=str(_acct.get("currency") or "CNY"),
            positions=_positions,
        )
        print(f"[INFO] 账户上下文（--account-json {args.account_json}）："
              f"总资产 {float(_acct['total_assets']):.2f} 元，"
              f"现金 {_portfolio.cash} 元，持仓 {len(_positions)} 只 "
              f"（{', '.join(p.ticker for p in _positions)}）")
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import account as _account

        _snap = _account.load_account_snapshot()
        _positions = []
        for _, _r in _snap.iterrows():
            _shares = float(_r["shares"])
            _cost_ps = float(_r["cost"]) / _shares if _shares else None
            _positions.append(Position(
                ticker=str(_r["code"]),
                quantity=_shares,
                average_price=_cost_ps,
            ))
        _portfolio = PortfolioContext(
            cash=float(_account.CASH),
            currency="CNY",
            positions=_positions,
        )
        print(f"[INFO] 账户上下文：现金 {_portfolio.cash} 元，持仓 {len(_positions)} 只 "
              f"（{', '.join(p.ticker for p in _positions)}）")

    if args.offline_tools:
        # 🔴 实测（2026-11-04 18:19 / 18:22 两轮）：
        #   · yfinance 新闻：Yahoo 限流 ⇒ 每次调用退避 2+4+8s，LLM 反复重试 ⇒ 空转；
        #   · alpha_vantage：无 key ⇒ VendorNotConfiguredError 直接抛出 ⇒ **掀翻整张图**（第一次跑就是这么挂的）。
        #   ⇒ 统一打桩成「数据不可用」哨兵：analyst 如实写「数据缺失」，不编数，也不浪费时间。
        import tradingagents.agents.tools as _ta_tools

        def _offline_route(method: str, *args, **kwargs) -> str:
            return (
                f"DATA_UNAVAILABLE: 本次运行**未接入外部数据源**（方法 {method} 不可用）。"
                "请在报告中如实说明该类数据缺失，不要估计、不要编造数值；"
                "**你所在的 analyst 拿不到 research_context**，因此不要引用或复述"
                "「上游量化层结论」；只如实声明数据缺失即可。"
                "**不要再调用任何工具**，请直接基于已有信息写完报告。"
            )

        _ta_tools.route_to_vendor = _offline_route

        # 🔴 `get_company_profile` 这条链**不经过** route_to_vendor（直接 yf.Ticker().info）
        #    ⇒ 打桩不彻底 ⇒ ta_run.log 里仍有 Yahoo 限流重试，
        #    且 instrument_context 会随 Yahoo 可达性变化 ⇒ **同一输入 prompt 不再可复现**。
        #    ⇒ 连它一起打桩。
        import tradingagents.dataflows.vendors.yahoo.fundamentals as _ta_fund
        import tradingagents.agents.context as _ta_ctx

        def _offline_profile(ticker: str) -> dict:
            return {}

        _ta_fund.get_company_profile = _offline_profile
        if hasattr(_ta_ctx, "get_company_profile"):
            _ta_ctx.get_company_profile = _offline_profile

        # 🔴 settlement 节点（tradingagents/graph/settlement.py::fetch_returns）用 Yahoo 拉
        #    「历史决策结算」所需的收盘价 ⇒ 实测 2026-11-04 20:13 每次调用退避 2+4+8s 才放弃
        #    （≈14s/次），且与本轮决策无关（结的是历史账）⇒ 夜间离线运行直接打桩成「无行情」。
        #    打桩后 fetch_returns 内部 except 会把异常吞成 None（待下次重试），不掀翻整张图。
        import tradingagents.graph.settlement as _ta_settle

        def _offline_closes(*_args, **_kwargs):
            raise RuntimeError("offline stub: 结算用行情不可用（夜间离线运行，结算留待下次）")

        _ta_settle.get_closes = _offline_closes

    cfg = dict(DEFAULT_CONFIG)
    # 缓存/产物一律落唯一落点根下（铁律：不写 /tmp）
    ta_root = run_root / "ta"
    cfg["results_dir"] = str(ta_root / "results")
    cfg["data_cache_dir"] = str(ta_root / "cache")
    cfg["memory_log_path"] = str(ta_root / "memory" / "trading_memory.md")
    # 少抓点新闻 ⇒ 少花时间与 token
    cfg["news_article_limit"] = 5
    cfg["global_news_article_limit"] = 5
    cfg["global_news_lookback_days"] = 3
    cfg["max_recur_limit"] = args.max_recur
    # 🔴 实测（2026-11-04 18:19~18:21）：yfinance 新闻 vendor 被 Yahoo 限流，
    #    每次调用退避 2+4+8s；配合 reasoning_effort=max ⇒ 一轮新闻抓取要几分钟，
    #    且 LLM 会反复重试同一批工具 ⇒ 空转。改为 alpha_vantage（无 key ⇒ 秒失败）
    #    ⇒ analyst 拿不到新闻就如实说「不可用」，不编数据。
    cfg["data_vendors"] = dict(cfg.get("data_vendors") or {})
    cfg["data_vendors"]["news_data"] = args.news_vendor

    analysts = tuple(a.strip() for a in args.analysts.split(",") if a.strip())
    started = time.time()
    meta = {
        "ticker": args.ticker,
        "news_vendor": args.news_vendor,
        "offline_tools": bool(args.offline_tools),
        "max_recur_limit": args.max_recur,
        "date": args.date,
        "analysts": list(analysts),
        "account_json": args.account_json,
        "reasoning_effort_arg": args.reasoning_effort,
        "provider": cfg.get("llm_provider"),
        "deep_think_llm": cfg.get("deep_think_llm"),
        "quick_think_llm": cfg.get("quick_think_llm"),
        "reasoning_effort": cfg.get("reasoning_effort"),
        "max_tokens": cfg.get("max_tokens"),
        "temperature": cfg.get("temperature"),
        "backend_url": cfg.get("backend_url"),
        "context_path": str(ctx_path),
        "context_chars": len(context),
        "context_sha1": _sha1(ctx_path),
        "env_sha1": _sha1(ta_dir / ".env") if (ta_dir / ".env").exists() else None,
        "ta_dir": str(ta_dir),
    }

    try:
        ta = TradingAgentsGraph(selected_analysts=analysts, config=cfg)
        final_state, signal = ta.propagate(
            args.ticker, args.date, research_context=context, portfolio=_portfolio
        )
    except Exception as exc:  # noqa: BLE001 —— 失败必须如实落盘
        err = traceback.format_exc()
        out_md.write_text(
            "# TradingAgents 决策 · **未完成**\n\n"
            f"- 失败时间：{time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"- 标的：{args.ticker} @ {args.date}；analysts={list(analysts)}\n"
            f"- 异常：`{type(exc).__name__}: {exc}`\n\n```\n{err}\n```\n",
            encoding="utf-8",
        )
        out_json.write_text(json.dumps(
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
             "traceback": err, "meta": meta}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"❌ TradingAgents 失败：{type(exc).__name__}: {exc}")
        print(f"已写出 {out_md}（标注为未完成）")
        return 1

    elapsed = time.time() - started
    debate = final_state.get("investment_debate_state", {}) or {}
    risk = final_state.get("risk_debate_state", {}) or {}
    plan = final_state.get("investment_plan", "") or ""
    trader = final_state.get("trader_investment_plan", "") or ""
    decision = final_state.get("final_trade_decision", "") or ""

    payload = {
        "status": "ok",
        "signal": signal,
        "elapsed_seconds": round(elapsed, 1),
        "meta": meta,
        "news_report": final_state.get("news_report", ""),
        "investment_debate": {
            "judge_decision": debate.get("judge_decision", ""),
            "bull_history": debate.get("bull_history", ""),
            "bear_history": debate.get("bear_history", ""),
        },
        "investment_plan": plan,
        "risk_debate": {
            "judge_decision": risk.get("judge_decision", ""),
            "aggressive_history": risk.get("aggressive_history", ""),
            "conservative_history": risk.get("conservative_history", ""),
            "neutral_history": risk.get("neutral_history", ""),
        },
        "trader_investment_plan": trader,
        "final_trade_decision": decision,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    out_md.write_text(_render_md(payload), encoding="utf-8")
    print(f"✅ TradingAgents 完成：signal={signal}，耗时 {elapsed:.0f}s")
    print(f"已写出 {out_md}")
    print(f"已写出 {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
