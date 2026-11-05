"""构造喂给 TradingAgents 的 research_context（压缩版，不是整份报告）。

【为什么压缩】
`etf_analysis_report.md` 有 34KB（≈12K tokens）。它会进 **7 个 prompt**
（bull / bear / research_manager / 3 风险辩手 / trader）⇒ 12 次 LLM 调用
会把上下文重复 12 遍，既慢又稀释辩论焦点。

【本脚本产出】
`<out>/ta_research_context.md`：账户现状 + 规则层调仓建议 + 候选清单 + 过滤漏斗 +
下一调仓节点 + 回测关键数字（**含新旧聚类口径对比**）+ 风险披露要点。
外加 `<out>/ta_context_meta.json`（指纹 + 生成时间）。

【口径】
- 全部数字来自落盘产物（analyze 报告 JSON / recommend JSON / account.csv）；
- **不许**在这里现算交易动作 ⇒ 规则层与 LLM 层职责分离。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account  # noqa: E402
from report_fingerprint import report_content_sha1  # noqa: E402


def _fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{float(x) * 100:+.{digits}f}%"


def _fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{float(x):,.{digits}f}"


def _req(d: dict, key: str, label: str | None = None) -> float:
    """取必填数字：**缺失或 None 直接抛错**（宁可不产出，不产出假结果）。

    🔴 2026-11-04 夜实测事故：本脚本原先写 `ss.get('n_unique')` / `blk.get('n_dates')`，
    而报告里的真实键是 `unique_values` / `n_cross_sections` ⇒ context 里渲染出
    「唯一值 None」「截面数 None」⇒ LLM 把这个**序列化 bug** 当成「上游分数退化 WARN」
    写进了 Bear 的模型风险与最终评级论据（凭空造出一条负面证据）。
    ⇒ 修法：全部改用 `_req`，缺键即炸，绝不让 None 混进 prompt。
    """
    if key not in d or d[key] is None:
        raise KeyError(f"必填字段缺失：{label or key}（key={key!r}）⇒ 拒绝生成含 None 的 prompt")
    return d[key]


def build(run_root: Path, report_md: Path, backtest_new: Path | None,
          backtest_old: Path | None, backtest_mid: Path | None = None,
          quality: Path | None = None) -> tuple[str, dict]:
    rep = json.loads((run_root / "etf_analysis_report.json").read_text(encoding="utf-8"))
    rec = json.loads((run_root / "rebalance_recommendation.json").read_text(encoding="utf-8"))
    summary = account.account_summary()
    weights = account.current_weights()

    lines: list[str] = []
    add = lines.append

    add("# ETF 轮动 · 调仓决策上下文（量化层结论，供多智能体辩论）")
    add("")
    add(f"- 信号日 / 基准日：**{rep['meta']['asof']}**；"
        f"下一调仓节点：**{rep['next_rebalance']['target_date']}**"
        f"（{rep['next_rebalance']['trading_days_until']} 个交易日后）")
    add(f"- 标的池口径 `pool_mode={rec['pool_mode']}`；调仓频率 = 每 "
        f"{rec['rebalance_every_n_days']} 个交易日一个信号日；执行日 = 信号日 +1 交易日")
    add(f"- 防守模式：{rec['defensive_mode']}；净值刹车：{rec['brake_mode']}；"
        f"绝对动量过滤放宽：{rec['relaxed_absolute_momentum_filter']}")
    add("")

    # ---- 1. 账户 ----
    add("## 1. 账户与资金（真实口径，不许改数）")
    add("")
    add(f"- 总资产 **{_fmt_num(summary['total_assets'])} 元**；现金 "
        f"**{_fmt_num(summary['cash'])} 元**（占 {summary['cash_weight'] * 100:.2f}%）；"
        f"持仓市值 {_fmt_num(summary['market_value'])} 元"
        f"（{summary['market_value_weight'] * 100:.2f}%）")
    add(f"- 持仓浮动盈亏 **{_fmt_num(summary['total_pnl'])} 元"
        f"（{summary['total_pnl_pct'] * 100:+.2f}%）**")
    add(f"- 目标结构：**{summary['target_positions']} 只等权**，每只 "
        f"{summary['target_position_weight'] * 100:.0f}% ≈ "
        f"**{_fmt_num(summary['target_position_amount'])} 元**")
    add("")
    add("| 代码 | 名称 | 股数 | 现价 | 市值 | 盈亏率 | 当前权重 |")
    add("|:--|:--|--:|--:|--:|--:|--:|")
    snap = account.load_account_snapshot()
    for _, r in snap.iterrows():
        code = str(r["code"])
        add(f"| {code} | {r['name']} | {int(r['shares']):,} | {r['price']:.3f} | "
            f"{_fmt_num(r['market_value'])} | {float(r['pnl_pct']) * 100:+.2f}% | "
            f"{weights.get(code, 0.0) * 100:.2f}% |")
    add("")

    # ---- 2. 规则层结论 ----
    add("## 2. 规则层（qlib 个股穿透 → E=W×S → 过滤/打分）的机械输出")
    add("")
    add("**建议持仓（目标等权 20%）**")
    add("")
    add("| 代码 | 名称 | 目标权重 | 最终分 | 映射分 E | mom120 | 站上 MA60 | 覆盖 | 主题 |")
    add("|:--|:--|--:|--:|--:|--:|:--:|--:|:--|")
    for h in rec["holdings"]:
        alpha = h.get("alpha_score")
        cover = h.get("coverage")
        add(f"| {h['code']} | {h['name']} | {h['target_weight'] * 100:.0f}% | "
            f"{h['final_score']:.4f} | "
            f"{'—' if alpha is None else format(alpha, '.4f')} | "
            f"{_fmt_pct(h.get('mom120'))} | {'Y' if h.get('above_ma60') else 'N'} | "
            f"{'—' if cover is None else format(cover, '.2f')} | "
            f"{h.get('theme_id', '—')} |")
    add("")
    add("**相对当前持仓的操作清单**")
    add("")
    add("| 动作 | 代码 | 名称 | 目标权重 | 当前权重 | 金额(元) |")
    add("|:--|:--|:--|--:|--:|--:|")
    for t in rec["trades"]:
        add(f"| {t['action']} | {t['code']} | {t['name']} | {t['target_weight'] * 100:.0f}% | "
            f"{t['current_weight'] * 100:.2f}% | {_fmt_num(t['amount'])} |")
    add("")
    st = rec["stats"]
    stages = (rep.get("filter_stats", {}) or {}).get("stages", []) or []
    funnel = " → ".join(f"{s.get('stage')} {s.get('n_in')}→{s.get('n_out')}" for s in stages)
    add(f"过滤漏斗（逐道，取自分析报告 filter_stats.stages）：{funnel}")
    add(f"（⚠️ MA 破位过滤对最终 31 只候选池**实际剔除 0 只**；"
        f"`n_entry_ma_reject={st.get('n_entry_ma_reject')}` 是在 203 只候选集上的**诊断计数**，"
        "**不是漏斗上的一步** ⇒ 不许把两个数混着算）")
    add(f"持仓强制退出（跌破 MA60）：{', '.join(st.get('forced_exit_codes') or []) or '无'}")
    add("")
    add("注：货币/黄金类**防守资产**不受「入场 MA 破位过滤」与「绝对动量过滤」约束"
        "（设计如此：防守资产本来就是用来躲下跌的），所以 511990 即使 MA60 破位仍可入选。")
    add("")

    # ---- 3. 候选清单 ----
    add("## 3. 候选清单（Top-N，按最终分降序）")
    add("")
    add("| 代码 | 名称 | 最终分 | E(W×S) | mom120 | 覆盖 | 入选 |")
    add("|:--|:--|--:|--:|--:|--:|:--:|")
    for c in rep["candidates"]:
        e_score = c.get("e_score")
        cover = c.get("coverage")
        add(f"| {c['code']} | {c.get('name', '—')} | {c.get('final_score', float('nan')):.4f} | "
            f"{'—' if e_score is None else format(e_score, '.4f')} | "
            f"{_fmt_pct(c.get('mom120'))} | "
            f"{'—' if cover is None else format(cover, '.2f')} | "
            f"{'✔' if c.get('selected') else '✘'} |")
    add("")

    # ---- 4. 个股层统计 + RankIC ----
    add("## 4. 个股穿透层（E=W×S 的上游）")
    add("")
    ss = rep.get("stock_scores", {})
    _dg = ss.get("degenerate_check", {}) or {}
    add(f"- asof 当天个股分数横截面：只数 {_req(ss, 'n_stocks')}，"
        f"唯一值 {_req(ss, 'unique_values')}，"
        f"退化守卫（唯一值数>1 且 标准差>0）**通过={_dg.get('passed')}**"
        f"（本次无退化，**不得**当成模型风险），"
        f"均值 {_req(ss, 'mean')}，标准差 {_req(ss, 'std')}")
    ric = rep.get("rank_ic", {}).get("horizons", {})
    for hz in ("5", "20"):
        blk = ric.get(hz)
        if not blk:
            continue
        if blk.get("status") != "ok":
            add(f"- RankIC({hz}日)：**样本不足**（{blk.get('reason')}）")
        else:
            add(f"- RankIC({hz}日)：mean={blk['mean_rank_ic']:.4f}，"
                f"ICIR={blk['icir']:.3f}，t={blk['t_stat']:.2f}，"
                f"截面数 {_req(blk, 'n_cross_sections')}（**评估指标，不参与选股**）")
    add("")

    # ---- 5. 回测数字（含口径变更披露） ----
    add("## 5. 回测关键数字（务必连同口径一起引用）")
    add("")
    if backtest_new and backtest_new.exists():
        b = json.loads(backtest_new.read_text(encoding="utf-8"))
        p, bm, ex = b["portfolio_net"], b["benchmark_510300"], b["excess_vs_510300"]
        add(f"- **现役口径**（同族硬分区 + 直接冲突对聚类，prefer-held=on）："
            f"年化 **{p['annualized_return'] * 100:.2f}%**，超额（相对口径）"
            f"**{ex['annualized_return'] * 100:+.2f}%**，MDD **{p['max_drawdown'] * 100:.2f}%**，"
            f"年化单边换手 **{b['cost_assumption']['annualized_one_way_turnover']:.2f}×**，"
            f"成本拖累 {b['cost_assumption']['total_cost_drag'] * 100:.2f}%")
        add(f"- 基准 510300：年化 {bm['annualized_return'] * 100:.2f}%，"
            f"MDD {bm['max_drawdown'] * 100:.2f}%；评估窗口 "
            f"{b['eval_window']['start']} ~ {b['eval_window']['end']}（{b['eval_window']['n_days']} 天）")
        seg = b.get("segment_report", {})
        if seg:
            parts = []
            for k, v in seg.items():
                e = v.get("excess", {}).get("annualized_return")
                if e is not None:
                    parts.append(f"{k} 超额 {e * 100:+.2f}%")
            if parts:
                add(f"- 分段稳定性：{'；'.join(parts)}"
                    "（**H1/H2 符号相反 ⇒ 样本内选择偏差未消，不能当预期收益**）")
    if backtest_old and backtest_old.exists():
        o = json.loads(backtest_old.read_text(encoding="utf-8"))
        op = o.get("portfolio_net") or o.get("portfolio")
        add(f"- ⚠️ **旧聚类口径已被替换**（并查集传递闭包 → 族硬分区 + 直接冲突对）："
            f"老口径年化 {op['annualized_return'] * 100:.2f}%、"
            f"MDD {op['max_drawdown'] * 100:.2f}%")
        if quality is not None and quality.exists():
            q = json.loads(quality.read_text(encoding="utf-8"))
            add(f"（老口径把 279 只 ETF 里的 **{q.get('legacy_largest_cluster')} 只串成巨型簇**"
                f"（共 {q.get('legacy_n_clusters')} 簇）⇒ 引用旧数字即引用一个已知有缺陷的口径）")
    if backtest_mid is not None and backtest_mid.exists() and backtest_old and backtest_old.exists():
        o = json.loads(backtest_old.read_text(encoding="utf-8"))
        m = json.loads(backtest_mid.read_text(encoding="utf-8"))
        op = o.get("portfolio_net") or o.get("portfolio")
        mp = m.get("portfolio_net") or m.get("portfolio")
        np_ = json.loads(backtest_new.read_text(encoding="utf-8"))["portfolio_net"] if backtest_new and backtest_new.exists() else None
        if np_ is not None:
            _d1 = (mp["annualized_return"] - op["annualized_return"]) * 100
            _d2 = (np_["annualized_return"] - mp["annualized_return"]) * 100
            add(f"- **口径变更的归因（单变量分解，别把两件事混成一件）**："
                f"聚类重构单独 = {op['annualized_return'] * 100:.2f}% → {mp['annualized_return'] * 100:.2f}%"
                f"（{_d1:+.2f}pp）；prefer-held（同族已持有优先）单独 = "
                f"{mp['annualized_return'] * 100:.2f}% → {np_['annualized_return'] * 100:.2f}%"
                f"（{_d2:+.2f}pp）；净变化 {(_d1 + _d2):+.2f}pp。"
                f"⇒ **现役口径 = 新聚类 + prefer-held 开**")
    add("")

    # ---- 6. 风险披露 ----
    add("## 6. 风险披露（量化层已识别的，辩论必须正面回应）")
    add("")
    for i, r in enumerate(rep.get("risk_disclosure", []) or [], 1):
        add(f"{i}. {r}")
    add("")

    add("## 7. 任务")
    add("")
    # 🔴 任务里的前提必须**从真实账户现算**（2026-11-04 夜实测：旧文案写死
    #    「当前 5 个仓位里有 3 只货币 ETF（合计 60%）」，而真实持仓里**一只货币 ETF 都没有**
    #    ⇒ LLM 花大量篇幅"纠正"一个不存在的前提，Bear 的整个方案也建立在纠正之上）。
    _snap = account.load_account_snapshot()
    _cur_codes = [str(r["code"]) for _, r in _snap.iterrows()]
    _cur_names = [str(r["name"]) for _, r in _snap.iterrows()]
    _money_kw = ("货币", "日利", "添益", "现金添益")
    _money_held = [n for n in _cur_names if any(k in n for k in _money_kw)]
    _tgt_def = [h for h in rec.get("holdings", []) if h.get("is_defensive")]
    _tgt_def_pct = sum(float(h.get("target_weight") or 0.0) for h in _tgt_def) * 100
    _tgt_def_names = "、".join(f"{h['code']} {h['name']}" for h in _tgt_def) or "无"
    add("请就**下一个调仓节点（2026-12-01）的 ETF 调仓方案**发表意见："
        "每只标的给「买 / 卖 / 持 / 减」的方向性判断与理由，"
        "并特别回答：")
    add(f"1. **账户现状 vs 目标结构的差异（先看事实）**："
        f"当前真实持仓是 {'、'.join(_cur_codes)}（{'、'.join(_cur_names)}），"
        f"**其中货币/类现金 ETF 有 {len(_money_held)} 只"
        f"{'（' + '、'.join(_money_held) + '）' if _money_held else '（一只都没有）'}**，"
        f"现金 {_fmt_num(summary['cash'])} 元（{summary['cash_weight'] * 100:.2f}%）；"
        f"而规则层的**目标组合**里列了 {_tgt_def_names} 共 {len(_tgt_def)} 只防守资产"
        f"（合计目标 {_tgt_def_pct:.0f}%）—— 但它们**一手约 10,000 元 ≈ 总资产的 33.5%**，"
        f"在本账户上**整手买不进**。⇒ 请回答："
        f"① 防守槽位是**只保留 1 只**、还是**全部留现金**？"
        f"② `512800` 的目标权重定 20% 还是更低？")
    add("2. **红利低波 ETF（563020）的个股穿透分 E 为负**（个股微观景气度不支持），"
        "仅靠自身动量入选 —— 是否值得建仓 20%？")
    add("3. 量化层明确指出 H1/H2 超额符号相反、样本内选择偏差未消 —— "
        "在这种证据强度下，本轮应当**加仓权益**还是**降低权益敞口**？")
    add("")
    add("---")
    add(f"（完整报告：`{report_md}`；结构化版本：`{run_root / 'etf_analysis_report.json'}`）")

    text = "\n".join(lines)
    # 🔴 终检：prompt 里**不允许出现 None**（铁律「宁可不产出，不产出假结果」）。
    #    取错的 JSON key 会渲染成 None，而 LLM 会把它当成"上游数据缺失/退化"写进论据
    #    （2026-11-04 夜实测：唯一值 None ⇒ Bear 凭空造出一条「模型退化风险」）。
    if "None" in text:
        _bad = [ln for ln in text.splitlines() if "None" in ln]
        raise ValueError(
            "生成的 research_context 里出现了 None（多半是取错了 JSON 键）⇒ 拒绝投喂：\n  "
            + "\n  ".join(_bad[:10])
        )
    meta = {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "asof": rep["meta"]["asof"],
        "chars": len(text),
        "report_md": str(report_md),
        "report_md_sha1": _sha1(report_md) if report_md.exists() else None,
        # 🔴 2026-11-04 夜实测修（M-G / F4，专题档 §21.4）：链路第一环**不能**只按
        #    「报告 md 的原始 sha1」判新鲜 —— 报告 md 含「生成时间」行 ⇒ **任何重生成都改 sha1**
        #    ⇒ 第一环必断 ⇒ 交付件被误判「LLM 第二意见已过期」⇒ 只能靠重跑 TA（929 秒 + 配额）修。
        #    ⇒ 额外记「**报告内容指纹**」（= 报告 JSON 去掉易变 meta 后的规范化 sha1）：
        #      仅元数据变化 ⇒ 指纹**不变**（假阳性消失）；实质变化 ⇒ 指纹**变**（守卫不被削弱）。
        #    ⚠️ 旧格式 meta 没有这个键 ⇒ 调用方回落 raw md sha1（向后兼容）。
        "report_content_sha1": report_content_sha1(rep),
        "recommendation_sha1": _sha1(run_root / "rebalance_recommendation.json"),
    }
    return text, meta


def _sha1(p: Path) -> str:
    import hashlib
    return hashlib.sha1(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="构造 TradingAgents research_context")
    ap.add_argument("--run-root", default="/mnt/data/siping/runs/etf_rotation")
    ap.add_argument("--report-md", default=None)
    ap.add_argument("--backtest-new", default=None)
    ap.add_argument("--backtest-old", default=None)
    ap.add_argument("--backtest-mid", default=None)
    ap.add_argument("--cluster-quality", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    run_root = Path(args.run_root)
    report_md = Path(args.report_md) if args.report_md else run_root / "etf_analysis_report.md"
    out = Path(args.out) if args.out else run_root / "ta_research_context.md"
    text, meta = build(
        run_root, report_md,
        Path(args.backtest_new) if args.backtest_new else run_root / "etf_rotation_backtest_v4_prefer1.json",
        Path(args.backtest_old) if args.backtest_old else run_root / "probe_cluster_legacy.json",
        Path(args.backtest_mid) if args.backtest_mid else run_root / "etf_rotation_backtest_v4_prefer0.json",
        Path(args.cluster_quality) if args.cluster_quality else run_root / "probe_cluster_quality.json",
    )
    out.write_text(text, encoding="utf-8")
    (run_root / "ta_context_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out}（{len(text)} 字符）")
    print(f"已写出 {run_root / 'ta_context_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
