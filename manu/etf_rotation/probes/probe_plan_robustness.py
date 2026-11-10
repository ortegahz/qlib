#!/usr/bin/env python
"""最终计划构建器的**鲁棒性**回归探针（只读正式产物；临时文件写在持久盘上）。

【为什么需要】
2026-11-04 22:3x 实测出两个真 bug（专题档 §25）：

  **M-B① F5 判定重复读盘**：`final_rebalance_plan._decision_failure_reason` 在一次
  `build_plan` 里被调 **8 次**（meta 里 3 次 + `_llm_staleness` 1 次 + `_decision_text` 2 次
  + `_decision_view` 1 次），每次重读 md(48KB) + json(107KB) ⇒ **累计重复读盘 1,214.6 KB**。
  更危险的是 meta 的三个字段（`source_decision` / `llm_decision_status` / `llm_decision_error`）
  **各读一次** ⇒ 决策文件在运行中被改写时，**同一份产物里三个字段会自相矛盾**。
  ⇒ 修法：改成「**运行内快照**」语义——key = 决策件绝对路径；一次构建内**只读一次盘、
  判定恒定**；下一次构建开头调 `reset_decision_failure_cache()` 清缓存 ⇒ 新构建必读新盘。

  🔴 2026-11-04 22:5x 实测**推翻了「按 (mtime_ns, 大小) 失效」的设计**：本机持久盘的
  mtime 粒度约 **4ms**，且**同大小改写时 mtime 与 size 都不变**（连写 3 次只得 2 个不同
  mtime_ns）⇒ 该键会返回**旧判定**，比不缓存更危险 ⇒ 改用快照语义。

  **M-B② sha1 链校验对象错位**：`_llm_staleness` 用 `--out-dir` 去找
  `ta_context_meta.json` / `ta_research_context.md` / `tradingagents_decision.json`，
  而这三件套其实**跟着决策文件走** ⇒ `--decision` 指向**另一个目录**时，
  要么**根本没校验**，要么拿 out_dir 里**上一轮遗留**的文件去校验**另一个目录**的决策
  ⇒ **校验对象错位，还报「通过」**。
  ⇒ 修法：三件套一律取「决策文件所在目录」。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_plan_robustness.py

  **M-F 削减后权重口径（2026-11-04 夜）**：「现金不足被削减」这条路径没把削减结果回写到权重
  ⇒ 交付件的「计划权益/计划现金权重」还是**削减前**的目标权重，而「期末现金」按**削减后**算
  ⇒ 两个数字互相打架（合成场景：计划现金 0.0% vs 实际 9.9%，差 198.83 元）。
  ⇒ R7/R7b/R7c 断言「计划现金必须与期末现金对得上」+「md 显示原值 → 可达值」。

【副作用】
只在临时工作区 `<数据集根>/_work/_probe_robustness/` 下写临时文件，结束即删；**不碰正式产物**。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

#: 工程根目录：probes/ → etf_rotation/ → manu/ → 工程根
PROJECT_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_DIR / "manu" / "etf_rotation"))
import config as C  # noqa: E402  （落点常量单一事实源）
import final_rebalance_plan as frp  # noqa: E402
from report_fingerprint import report_content_sha1  # noqa: E402

RUN_ROOT = C.ETF_RUN_ROOT
#: 🔴 2026-11-04 夜实测修（M-F；reviewer 独立复核查出）：
#:    探针自称「只读」，实际把 scratch 目录建在**官方产物目录里面** ⇒
#:    崩了就留残留 + 密钥扫描会扫到 + 并发跑会撞。
#:    ⇒ 改到**临时工作区** `<数据集根>/_work/`（仍在持久盘上，不写 /tmp、不写官方产物树）。
SCRATCH = C.WORK_ROOT / "_probe_robustness"

#: 一份**有效**的决策件正文（首行不能含「未完成」，也不能有「- 异常：」行）
VALID_DECISION_MD = "# TradingAgents 决策纪要 · 测试\n\n- 标的：**512800**；最终评级：**Hold**\n"


def _sha1_text(text: str) -> str:
    """文本的 sha1（与 `_sha1_file` 同口径）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    """写文件并**强制推进 mtime**（贴近真实文件系统行为；缓存已改为路径键，不再依赖 mtime）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    import os
    os.utime(path, (stat.st_atime + 1, stat.st_mtime + 1))


def _make_decision_dir(directory: Path, status: str = "ok", context_sha1: str = "",
                        context_text: str = "上下文正文\n", report_sha1: str = "",
                        decision_md: str = VALID_DECISION_MD) -> Path:
    """造一个「决策文件 + 上下文三件套」齐备的目录。"""
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / "tradingagents_decision.md", decision_md)
    payload = {"status": status, "meta": {"context_sha1": context_sha1}}
    _write(directory / "tradingagents_decision.json", json.dumps(payload, ensure_ascii=False))
    _write(directory / "ta_research_context.md", context_text)
    _write(directory / "ta_context_meta.json", json.dumps({"report_md_sha1": report_sha1},
                                                           ensure_ascii=False))
    return directory


class _Args:
    """`_llm_staleness` 需要的最小参数集。"""

    def __init__(self, report: Path, decision: Path, out_dir: Path):
        self.report = str(report)
        self.decision = str(decision)
        self.out_dir = str(out_dir)


def _check(name: str, ok: bool, detail: str, lines: list[str]) -> None:
    lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
    lines.append(f"        {detail}")


def main() -> int:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    lines: list[str] = []
    failures = 0

    # ============ R1 缓存：8 次调用只真读 1 次盘 ============
    decision_dir = _make_decision_dir(SCRATCH / "r1")
    decision_file = decision_dir / "tradingagents_decision.md"
    real_uncached = frp._decision_failure_reason_uncached
    counter = {"n": 0}

    def counting_uncached(path):
        counter["n"] += 1
        return real_uncached(path)

    frp._decision_failure_reason_uncached = counting_uncached
    frp._DECISION_FAILURE_CACHE.clear()
    try:
        verdicts = [frp._decision_failure_reason(decision_file) for _ in range(8)]
    finally:
        frp._decision_failure_reason_uncached = real_uncached
    same = len(set(verdicts)) == 1
    ok = counter["n"] == 1 and same
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R1 同一次运行内 8 次判定 ⇒ 只真读盘 1 次")
    lines.append(f"        实读盘 {counter['n']} 次（期望 1）｜8 次结果一致={same}｜判定={verdicts[0]!r}")

    # ============ R2 快照语义：运行中途改写 ⇒ 同一次运行内判定必须**恒定** ============
    # 🔴 2026-11-04 22:5x：快照语义下「文件被改写后判定自动刷新」**不再成立**（那是 mtime 键的旧设计）。
    #    快照要保证的是：**同一次运行内 8 次判定恒定**（meta 的三个字段不会自相矛盾）。
    frp.reset_decision_failure_cache()
    before = frp._decision_failure_reason(decision_file)
    # 把决策件改成「失败占位」⇒ 快照期内判定**不得**跟着变（否则同一份产物自相矛盾）
    _write(decision_file, "# TradingAgents 决策纪要\n\n**未完成**\n\n- 异常：RuntimeError: boom\n")
    verdicts = [frp._decision_failure_reason(decision_file) for _ in range(8)]
    consistent = len(set(verdicts)) == 1
    ok = before is None and consistent and verdicts[0] == before
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R2 快照语义：决策件中途被改写 ⇒ 同一次运行内 8 次判定必须恒定")
    lines.append(f"        改写前={before!r} → 改写后 8 次={verdicts[0]!r}｜恒定={consistent}（期望恒定且不自相矛盾）")

    # ============ R3 跨目录：sha1 链必须按**决策文件所在目录**找 ============
    # A 目录（= --out-dir）放**上一轮遗留**的过期上下文三件套；B 目录放真决策件（无上下文三件套）
    report_dir = SCRATCH / "r3"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_md = report_dir / "etf_analysis_report.md"
    _write(report_md, "# 报告\n")
    wrong_report_sha1 = "0" * 40
    stale_ctx = "**旧版上下文**\n"
    dir_a = _make_decision_dir(report_dir / "out_dir", context_sha1="1" * 40,
                               context_text=stale_ctx, report_sha1=wrong_report_sha1)
    dir_b = _make_decision_dir(report_dir / "decision_dir", context_sha1="2" * 40,
                               context_text="新上下文\n", report_sha1=wrong_report_sha1)
    # B 目录里把上下文三件套删掉，只留决策件（模拟「决策在别处、上下文不可得」）
    for name in ("ta_research_context.md", "ta_context_meta.json"):
        (dir_b / name).unlink()
    stale_info = frp._llm_staleness(_Args(report_md, dir_b / "tradingagents_decision.md", dir_a))
    # 旧实现会拿 A 的过期 meta（report_md_sha1 全 0）⇒ 报「旧版分析报告」；
    # 新实现找 B 的目录 ⇒ B **缺**上下文三件套 ⇒ 🔴 M-F4：链**无法校验**
    # ⇒ 必须报「未通过校验」（stale=True + unverifiable_reason）；
    # 但**绝不能**报「旧版分析报告」—— 那是拿 A 的过期件做**错位**校验才有的结论。
    reasons = " ".join(stale_info.get("reasons") or [])
    ok = (bool(stale_info.get("stale"))
          and ("旧版分析报告" not in reasons)
          and ("无法校验" in reasons)
          and bool(stale_info.get("unverifiable_reason")))
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R3 跨目录决策件 ⇒ sha1 链按决策目录找，不拿 out_dir 的旧件校验"
                 "（缺件 ⇒ 报『无法校验』而非『新鲜』，M-F4）")
    lines.append(f"        stale={stale_info.get('stale')}｜llm_present={stale_info.get('llm_present')}"
                 f"｜reasons={reasons or '无'}")
    # 反向断言：把 A 的过期件当作「决策目录里的件」时，**必须**报 stale（证明链路真的会查）
    stale_self = frp._llm_staleness(_Args(report_md, dir_a / "tradingagents_decision.md", dir_a))
    ok2 = bool(stale_self.get("stale")) and "旧版分析报告" in " ".join(stale_self.get("reasons") or [])
    failures += 0 if ok2 else 1
    lines.append(f"[{'PASS' if ok2 else 'FAIL'}] R3b 同目录且链确实过期 ⇒ 必须报 stale（证明链路没被削弱）")
    lines.append(f"        stale={stale_self.get('stale')}｜reasons={' '.join(stale_self.get('reasons') or []) or '无'}")

    # ============ R4 同目录且链新鲜 ⇒ 不该报 stale（正常流水线行为不变）============
    dir_c = SCRATCH / "r4"
    dir_c.mkdir(parents=True, exist_ok=True)
    report_c = dir_c / "etf_analysis_report.md"
    _write(report_c, "# 报告\n")
    ctx_text = "上下文正文\n"
    ctx_sha1 = _sha1_text(ctx_text)
    dir_c = _make_decision_dir(dir_c / "run", context_sha1=ctx_sha1, context_text=ctx_text,
                               report_sha1=_sha1_text("# 报告\n"))
    fresh = frp._llm_staleness(_Args(report_c, dir_c / "tradingagents_decision.md", dir_c))
    ok = (not fresh.get("stale")) and fresh.get("llm_present") is True
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R4 同目录 + 链新鲜 ⇒ 不报 stale、llm_present=True（行为未变）")
    lines.append(f"        stale={fresh.get('stale')}｜llm_present={fresh.get('llm_present')}"
                 f"｜reasons={' '.join(fresh.get('reasons') or []) or '无'}")

    # ============ R5 快照边界：清缓存（= 下一次构建）后判定必须刷新 ============
    # 🔴 快照语义 = 「本次运行看到的状态」；跨构建的新鲜度**只**靠
    #    `reset_decision_failure_cache()` 保证 ⇒ 必须同时验证「它真的被清了」。
    frp.reset_decision_failure_cache()
    first = frp._decision_failure_reason(dir_c / "tradingagents_decision.md")
    _write(dir_c / "tradingagents_decision.md", VALID_DECISION_MD + "\n- 异常：RuntimeError: x\n")
    still = frp._decision_failure_reason(dir_c / "tradingagents_decision.md")
    frp.reset_decision_failure_cache()          # = 下一次构建（build_plan 开头会做）
    refreshed = frp._decision_failure_reason(dir_c / "tradingagents_decision.md")
    ok = first is None and still is None and refreshed is not None
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R5 快照边界：清缓存后判定刷新（快照期内恒定、清后刷新）")
    lines.append(f"        首判={first!r} → 快照内={still!r} → 清缓存后={refreshed!r}（期望 None → None → 非 None）")

    # ============ R5b 构建入口必须清快照（否则跨构建沿用旧判定）============
    calls = {"n": 0}
    real_reset = frp.reset_decision_failure_cache

    def counting_reset() -> None:
        calls["n"] += 1
        real_reset()

    frp.reset_decision_failure_cache = counting_reset
    try:
        # build_plan 的**第一句**就是清快照 ⇒ 用不存在的输入调用也能证明它先被清过
        frp.build_plan(_Args("/nonexistent/report.json", "/nonexistent/dec.md", "/nonexistent"))
    except Exception:
        pass
    finally:
        frp.reset_decision_failure_cache = real_reset
    ok2 = calls["n"] == 1
    failures += 0 if ok2 else 1
    lines.append(f"[{'PASS' if ok2 else 'FAIL'}] R5b build_plan 入口必须调 reset_decision_failure_cache（快照边界）")
    lines.append(f"        build_plan 调用 reset 次数={calls['n']}（期望 1）")

    # ============ R6 决策件是**符号链接** ⇒ 上下文三件套必须按「软链所在目录」找 ============
    # 🔴 2026-11-04 22:4x 实测踩坑：修 M-B② 时用了 `dec.resolve().parent` ⇒ **resolve 会跟随软链**
    #    ⇒ 复跑目录里指向基线的决策软链被解析回**基线目录** ⇒ 链校验又对回基线的上下文
    #    ⇒ 可复现性探针直接报「9 处实质差异」（复跑件 stale、基线不 stale）。
    #    ⇒ 必须用 `absolute()`（不跟随软链）⇒ 取「调用方传入路径所在的目录」。
    r6_root = SCRATCH / "r6"
    target_dir = r6_root / "target"
    link_dir = r6_root / "linkdir"
    report_r6 = r6_root / "etf_analysis_report.md"
    _write(report_r6, "# 报告\n")
    good_sha1 = _sha1_text("# 报告\n")
    target_dir.mkdir(parents=True, exist_ok=True)
    # 🔴 M-F4：链「可校验」的前提是**三件套齐全** ⇒ 目标/软链两个目录都必须放 context
    #    （软链目录放**匹配**的那份；目标目录放**另一份** ⇒ 若代码跟随软链，第二环也会炸）。
    ctx_r6 = "R6 上下文正文\n"
    _write(target_dir / "tradingagents_decision.md", VALID_DECISION_MD)
    _write(target_dir / "tradingagents_decision.json",
           json.dumps({"status": "ok", "meta": {"context_sha1": _sha1_text(ctx_r6)}}))
    _write(target_dir / "ta_research_context.md", "目标目录的旧上下文\n")
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / "tradingagents_decision.md").symlink_to(target_dir / "tradingagents_decision.md")
    (link_dir / "tradingagents_decision.json").symlink_to(target_dir / "tradingagents_decision.json")
    _write(link_dir / "ta_research_context.md", ctx_r6)
    # 目标目录放**过期**的 meta，软链目录放**新鲜**的 meta ⇒ 只有「按软链目录找」才不报 stale
    _write(target_dir / "ta_context_meta.json", json.dumps({"report_md_sha1": "0" * 40}))
    _write(link_dir / "ta_context_meta.json", json.dumps({"report_md_sha1": good_sha1}))
    link_info = frp._llm_staleness(_Args(report_r6, link_dir / "tradingagents_decision.md", r6_root))
    ok = not link_info.get("stale")
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R6 决策件是软链 ⇒ 上下文三件套按**软链所在目录**找（不跟随软链）")
    lines.append(f"        stale={link_info.get('stale')}｜reasons={' '.join(link_info.get('reasons') or []) or '无'}"
                 "（期望 stale=False：软链目录的 meta 是新鲜的，目标目录的 meta 是过期的）")

    # ============ R7 现金不足被削减 ⇒ 计划权益/计划现金必须按**削减后**算 ============
    # 🔴 2026-11-04 夜实测（M-F；reviewer 独立复核查出、本机用合成场景复证）：
    #    「现金不足被削减」这条路径**没有把削减结果回写到权重** ⇒
    #    交付件的「计划权益仓位权重 / 计划现金权重」用的还是**削减前**的目标权重，
    #    而「期末现金」是按**削减后**的实际买入算的 ⇒ 同一份交付件里两个数字互相打架。
    #    ⚠️ 触发条件：买入总额 + 买入费 > 期初现金 + 卖出回款 − 卖出费。
    #    ⚠️ 真实严重度（实测，勿夸大）：**一致账户 + 5 席等权时缺口 ≈ 手续费**（很小）；
    #    只有「持有超配席 + 低现金」或权重和 > 100% 时才会出现大缺口。
    #    ⇒ 断言口径：**计划现金权重/金额必须与「期末现金」对得上**（误差 ≤ 总资产 1%，即手续费量级）。
    #    ⇒ 旧实现两者差 10pp（计划现金 0.0% vs 实际现金 9.9%）⇒ 会被这条断言**真的拦下**。
    r7_root = SCRATCH / "r7"
    r7_root.mkdir(parents=True, exist_ok=True)
    _write(r7_root / "prices.csv",
           "code,date,close\n"
           "512800,2026-11-03,1.00\n"
           "563020,2026-11-03,2.00\n"
           "159929,2026-11-03,10.00\n")
    _write(r7_root / "universe.csv", "code,name,last_price,last_price_date\n")
    _write(r7_root / "account.csv",
           "code,name,shares,price,market_value,pnl_pct\n"
           "512800,银行ETF,1000,1.00,1000,0\n"
           "159929,医药ETF,100,10.00,1000,0\n")
    r7_report = {
        "meta": {"asof": "2026-11-03", "params": {"commission_bp": 1.5, "slippage_bp": 5.0}},
        "current_holdings_diagnosis": {
            "total_assets": 2000.0,
            "cash": 0.0,
            "holdings": [
                {"code": "512800", "name": "银行ETF", "shares": 1000, "market_value": 1000.0},
                {"code": "159929", "name": "医药ETF", "shares": 100, "market_value": 1000.0},
            ],
        },
        "next_rebalance": {"target_date": "2026-12-01"},
        "recommendation": {
            "actions": [
                {"code": "512800", "name": "银行ETF", "action": "持有",
                 "target_weight": 0.5, "current_weight": 0.5, "reason": "合成场景"},
                {"code": "563020", "name": "红利低波", "action": "买入",
                 "target_weight": 0.5, "current_weight": 0.0, "reason": "合成场景"},
                {"code": "159929", "name": "医药ETF", "action": "卖出",
                 "target_weight": 0.0, "current_weight": 0.5, "reason": "合成场景"},
            ],
        },
    }
    _write(r7_root / "etf_analysis_report.json", json.dumps(r7_report, ensure_ascii=False))

    class _PlanArgs:
        """`build_plan` 需要的参数集（全部指向合成输入）。"""

        def __init__(self, root: Path):
            self.report = str(root / "etf_analysis_report.json")
            self.decision = str(root / "不存在的决策件.md")
            self.prices = str(root / "prices.csv")
            self.universe = str(root / "universe.csv")
            self.account = str(root / "account.csv")
            self.out_dir = str(root)

    plan_r7 = frp.build_plan(_PlanArgs(r7_root))
    flow_r7 = plan_r7["cash_flow"]
    total_r7 = 2000.0
    #: 独立重算：削减后的**实际**现金占比
    cash_weight_actual = flow_r7["cash_end"] / total_r7
    cash_amount_gap = abs(float(flow_r7.get("target_cash_amount") or 0.0) - flow_r7["cash_end"])
    ok = (cash_amount_gap <= total_r7 * 0.01)
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R7 现金不足被削减 ⇒ 计划现金必须与「期末现金」对得上")
    lines.append(
        f"        缺口={flow_r7['deficit']:.2f}｜计划现金权重={float(flow_r7.get('target_cash_weight') or 0) * 100:.1f}%"
        f"｜计划现金金额={float(flow_r7.get('target_cash_amount') or 0):.2f}｜期末现金={flow_r7['cash_end']:.2f}"
        f"｜实际现金占比={cash_weight_actual * 100:.1f}%｜差额={cash_amount_gap:.2f} 元"
        f"（阈值 {total_r7 * 0.01:.0f} 元；旧实现计划现金 0.0% vs 实际 9.9% ⇒ 差 198.83 元 ⇒ 会被拦下）")

    md_r7 = frp.render_markdown(plan_r7)
    row_r7 = next((ln for ln in md_r7.splitlines() if ln.startswith("| 563020")), "")
    ok2 = ("50.0% → **40.0%**" in row_r7)
    failures += 0 if ok2 else 1
    lines.append(f"[{'PASS' if ok2 else 'FAIL'}] R7b 被削减席位的 md 权重列显示「原值 → 可达值」")
    lines.append(f"        563020 行权重列={row_r7.split('|')[7].strip() if row_r7 else '(该行不存在)'}"
                 "（期望 50.0% → **40.0%**：原目标 50%，实际只买到 40%）")

    ok3 = abs(float(flow_r7.get("invested_weight_planned") or 0.0) - 0.90) < 1e-9
    failures += 0 if ok3 else 1
    lines.append(f"[{'PASS' if ok3 else 'FAIL'}] R7c 计划权益权重按**可达**口径求和（不是削减前的目标权重）")
    lines.append(f"        计划权益权重={float(flow_r7.get('invested_weight_planned') or 0):.4f}"
                 "（期望 0.9000 = 512800 的 0.50 + 563020 可达的 0.40；旧实现是 1.0000）")

    # ============ R10 第一环按「报告**内容指纹**」判（M-G / F4）============
    # 🔴 2026-11-04 夜实测修（M-G / F4，专题档 §21.4）：
    #    第一环原按「报告 md 的**原始 sha1**」判新鲜，而报告 md 含「生成时间」行
    #    ⇒ **任何一次重生成都改 sha1** ⇒ 第一环必断 ⇒ 交付件被误判「LLM 第二意见已过期」
    #    ⇒ 修它只能靠**重跑 TA**（929 秒 + 配额）。
    #    但实测（§21.1）：喂给 LLM 的 research_context 由报告 **JSON** 生成
    #    ⇒ 元数据变化**进不了上下文** ⇒ 旧决策依然成立 ⇒ 按 raw md sha1 判是**假阳性**。
    #    ⇒ 改按「报告 JSON 去掉易变 meta 后的规范化 sha1」（内容指纹）。
    #    🔴 **成对对照**：仅元数据变化 ⇒ 仍新鲜；**实质变化 ⇒ 必须 stale**（守卫不许被改松）。
    r10_root = SCRATCH / "r10"
    r10_root.mkdir(parents=True, exist_ok=True)
    r10_report = r10_root / "etf_analysis_report.json"
    r10_md = r10_root / "etf_analysis_report.md"
    _ctx_text = "上下文正文\n"
    _md_text = "# 报告\n> 生成时间 2026-11-04T00:00:00+08:00\n"
    base_report = {
        "meta": {
            "asof": "2026-11-03",
            "generated_at": "2026-11-04T00:00:00+08:00",
            "host": "test-Super-Server",
            "python_version": "3.12.16",
            "script": "/tmp/pycharm_project_658d3fc4/manu/etf_rotation/analyze.py",
            "inputs": {"stock_scores": {"size_bytes": 22758233}},
            "params": {"alpha_weight": 0.5},
        },
        "candidates": [{"code": "512800", "e_score": 1.23}],
    }
    _write(r10_report, json.dumps(base_report, ensure_ascii=False))
    _write(r10_md, _md_text)
    dir10 = _make_decision_dir(r10_root / "run", context_sha1=_sha1_text(_ctx_text),
                                context_text=_ctx_text, report_sha1=_sha1_text(_md_text))
    _meta10 = json.loads((dir10 / "ta_context_meta.json").read_text(encoding="utf-8"))
    _meta10["report_content_sha1"] = report_content_sha1(base_report)
    _write(dir10 / "ta_context_meta.json", json.dumps(_meta10, ensure_ascii=False))

    # ① 仅元数据变化（模拟报告重生成）⇒ 必须**仍新鲜**
    meta_only = json.loads(json.dumps(base_report))
    meta_only["meta"]["generated_at"] = "2099-01-01T00:00:00+08:00"
    meta_only["meta"]["host"] = "another-host"
    meta_only["meta"]["script"] = "/somewhere/else/analyze.py"
    meta_only["meta"]["python_version"] = "3.99.0"
    meta_only["meta"]["inputs"] = {"stock_scores": {"size_bytes": 999}}
    _write(r10_report, json.dumps(meta_only, ensure_ascii=False))
    _write(r10_md, _md_text.replace("2026-11-04T00:00:00+08:00", "2099-01-01T00:00:00+08:00"))
    info10a = frp._llm_staleness(_Args(r10_report, dir10 / "tradingagents_decision.md", r10_root))
    ok = (not info10a.get("stale")) and info10a.get("llm_present") is True
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R10a 仅元数据变化（md 生成时间行 + json 易变 meta）"
                 "⇒ 第一环**仍新鲜**（F4 的假阳性已消除）")
    lines.append(f"        stale={info10a.get('stale')}｜llm_present={info10a.get('llm_present')}"
                 f"｜reasons={' '.join(info10a.get('reasons') or []) or '无'}"
                 "（期望 stale=False；旧实现按 raw md sha1 判 ⇒ 这里会误报 stale）")

    # ② 实质变化（信号日）⇒ 必须 stale
    substantive = json.loads(json.dumps(base_report))
    substantive["meta"]["asof"] = "2026-11-04"
    _write(r10_report, json.dumps(substantive, ensure_ascii=False))
    info10b = frp._llm_staleness(_Args(r10_report, dir10 / "tradingagents_decision.md", r10_root))
    _reasons10b = " ".join(info10b.get("reasons") or [])
    ok2 = bool(info10b.get("stale")) and ("内容指纹" in _reasons10b)
    failures += 0 if ok2 else 1
    lines.append(f"[{'PASS' if ok2 else 'FAIL'}] R10b 实质变化（信号日 asof 改了）⇒ 第一环**必须 stale**"
                 "（F4 不许把守卫改松）")
    lines.append(f"        stale={info10b.get('stale')}｜reasons={_reasons10b or '无'}"
                 "（期望 stale=True 且原因含「内容指纹」）")

    # ============ R11 旧格式 meta（无内容指纹）⇒ **回落** raw md sha1（向后兼容）============
    _meta11 = json.loads((dir10 / "ta_context_meta.json").read_text(encoding="utf-8"))
    _meta11.pop("report_content_sha1", None)
    _write(dir10 / "ta_context_meta.json", json.dumps(_meta11, ensure_ascii=False))
    _write(r10_report, json.dumps(base_report, ensure_ascii=False))
    _write(r10_md, _md_text)
    info11a = frp._llm_staleness(_Args(r10_report, dir10 / "tradingagents_decision.md", r10_root))
    ok3 = not info11a.get("stale")
    failures += 0 if ok3 else 1
    lines.append(f"[{'PASS' if ok3 else 'FAIL'}] R11 旧格式 meta（无 report_content_sha1）+ md 未变"
                 "⇒ 回落 raw md sha1 ⇒ 仍新鲜（向后兼容，老产物不许被打成「不可校验」）")
    lines.append(f"        stale={info11a.get('stale')}｜reasons={' '.join(info11a.get('reasons') or []) or '无'}")
    # 旧格式 + md 变了 ⇒ 必须照旧报 stale（回落不是「永远新鲜」）
    _write(r10_md, _md_text.replace("2026-11-04T00:00:00+08:00", "2099-01-01T00:00:00+08:00"))
    info11b = frp._llm_staleness(_Args(r10_report, dir10 / "tradingagents_decision.md", r10_root))
    ok4 = bool(info11b.get("stale")) and ("旧版分析报告" in " ".join(info11b.get("reasons") or []))
    failures += 0 if ok4 else 1
    lines.append(f"[{'PASS' if ok4 else 'FAIL'}] R11b 旧格式 meta + md 变了 ⇒ 必须照旧报 stale"
                 "（回落路径不许变成「永远新鲜」）")
    lines.append(f"        stale={info11b.get('stale')}｜reasons={' '.join(info11b.get('reasons') or []) or '无'}")

    # ============ R8 说话人三表必须同源（优先级 / 中文名 / 章节登记）============
    # 🔴 2026-11-04 夜实测修（M-F3；reviewer 指出 `order` 字典与 `_LLM_SECTIONS` 不同源）：
    #    旧实现里风险三方（aggressive/conservative/neutral）全部落进「未知说话人」档 4
    #    ⇒ 三方之间相对次序随机；新增说话人时只改一张表 ⇒ 静默降级。
    #    ⇒ 抽成单一事实源 `_LLM_WHO_ORDER` + 运行时硬校验 `_assert_llm_who_tables()`。
    try:
        frp._assert_llm_who_tables()
        tables_ok, table_detail = True, (
            f"order={list(frp._LLM_WHO_ORDER)}｜label 键={sorted(frp._LLM_WHO_LABEL)}"
            f"｜section 说话人={sorted({w for w, _ in frp._LLM_SECTIONS})}")
    except Exception as exc:
        tables_ok, table_detail = False, f"{type(exc).__name__}: {exc}"
    _probe_whos = ("final", "research", "bull", "bear", "aggressive", "conservative", "neutral", "unknown")
    ranks = {w: frp._llm_who_rank(w) for w in _probe_whos}
    order_ok = (list(ranks.values()) == sorted(ranks.values())
                and len(set(ranks.values())) == len(ranks))
    ok = tables_ok and order_ok
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R8 说话人三表同源 + 优先级次序唯一"
                 "（final>research>bull>bear>风险三方>未知说话人）")
    lines.append(f"        {table_detail}｜rank={ranks}")

    # ============ R9 主视图（`_llm_targets`）按**优先级**挑，不是按出现顺序 ============
    def _r9_table(target: str, action: str) -> str:
        return ("| 标的 | 当前权重 | 动作 | 目标权重 |\n"
                "|:--|--:|:--|--:|\n"
                f"| 512800 | 20% | {action} | {target} |\n")

    r9_body = ("# 合成决策\n\n## 研究经理\n" + _r9_table("15%", "减")
               + "\n## 多头\n" + _r9_table("20%", "减")
               + "\n## 空头\n" + _r9_table("10%", "减")
               + "\n## 激进派\n" + _r9_table("5%", "减")
               + "\n## 最终交易决定\n" + _r9_table("25%", "买"))
    r9_md = SCRATCH / "r9_decision.md"
    _write(r9_md, r9_body)
    frp.reset_decision_failure_cache()
    t9 = (frp._llm_targets(str(r9_md)) or {}).get("512800") or {}
    ok = (t9.get("who") == "final" and float(t9.get("target_pct") or -1) == 25.0)
    failures += 0 if ok else 1
    lines.append(f"[{'PASS' if ok else 'FAIL'}] R9 五方同时有视图 ⇒ 主视图必须是 final（最终裁决优先）")
    lines.append(f"        选中 who={t9.get('who')}｜target_pct={t9.get('target_pct')}"
                 "（期望 final / 25.0；旧实现里 final 与风险三方同档风险 ⇒ 次序不可靠）")

    r9b_md = SCRATCH / "r9b_decision.md"
    _write(r9b_md, r9_body.split("\n## 最终交易决定\n")[0])
    frp.reset_decision_failure_cache()
    t9b = (frp._llm_targets(str(r9b_md)) or {}).get("512800") or {}
    ok2 = (t9b.get("who") == "research" and float(t9b.get("target_pct") or -1) == 15.0)
    failures += 0 if ok2 else 1
    lines.append(f"[{'PASS' if ok2 else 'FAIL'}] R9b 去掉「最终交易决定」⇒ 主视图退到 research（次高优先级）")
    lines.append(f"        选中 who={t9b.get('who')}｜target_pct={t9b.get('target_pct')}（期望 research / 15.0）")

    shutil.rmtree(SCRATCH, ignore_errors=True)

    total = len(lines) // 2
    print("=" * 78)
    print("最终计划构建器 · 鲁棒性探针（F5 判定缓存 + sha1 链目录口径 + 削减后权重口径）")
    print(f"临时目录：{SCRATCH}（已清理）")
    print("-" * 78)
    print("\n".join(lines))
    print("-" * 78)
    print(f"合计 {total} 项：PASS {total - failures} / FAIL {failures}")
    print("=" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
