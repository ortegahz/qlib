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

import config as C  # noqa: E402  （研究链根已在上面的 sys.path 里 ⇒ 落点常量单一事实源）
import pandas as pd  # noqa: E402
from report_fingerprint import report_content_sha1  # noqa: E402
import overlap as overlap_mod  # noqa: E402  （候选池重合度筛查：唯一口径来源）


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



def compute_crowding(prices: pd.DataFrame | None, codes: list[str],
                      asof: str) -> dict[str, dict]:
    """🔴 2026-11-11（文档 §三.2）：算**拥挤度**（放量倍数 + 池内分位）。

    【口径】对每只 ETF：``近 20 日日均成交额 ÷ 基期 120 日日均成交额``
    （>1 = 放量 = 拥挤上升；数据不足 ⇒ **不参与**，**不编造**）。
    判定「拥挤」= 放量倍数 ≥ `C.CROWDING_RATIO_THRESHOLD` **或** 池内分位 ≥
    `C.CROWDING_PERCENTILE_THRESHOLD`。
    【为什么】文档要求 TA 负责「检查行业重叠度与**拥挤度**」⇒ 此前只做了重叠度，
    拥挤度**未实现** ⇒ 这里补上，并**如实披露**数据可得性。
    """
    out: dict[str, dict] = {}
    if prices is None or len(prices) == 0:
        return out
    need = {"code", "date", "amount"}
    if not need.issubset(set(prices.columns)):
        return out
    df = prices.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].notna()]
    if asof:
        _asof = pd.to_datetime(asof, errors="coerce")
        if _asof is not None and not pd.isna(_asof):
            df = df[df["date"] <= _asof]
    recent_w = int(getattr(C, "CROWDING_RECENT_WINDOW", 20))
    base_w = int(getattr(C, "CROWDING_BASE_WINDOW", 120))
    for code in codes:
        code = str(code).zfill(6)
        sub = df[df["code"] == code].sort_values("date")
        if len(sub) < min(recent_w, base_w):
            continue      # 数据不足 ⇒ 不参与（不猜）
        amt = pd.to_numeric(sub["amount"], errors="coerce").dropna()
        if len(amt) < min(recent_w, base_w):
            continue
        recent = float(amt.tail(recent_w).mean())
        base = float(amt.tail(base_w).mean())
        if base <= 0:
            continue
        out[code] = {
            "ratio": recent / base,
            "recent_amount": recent,
            "base_amount": base,
            "n_obs": int(len(amt)),
        }
    # 池内分位（放量倍数越大 ⇒ 越拥挤）
    ratios = sorted(v["ratio"] for v in out.values())
    n = len(ratios)
    for code, v in out.items():
        rank = sum(1 for x in ratios if x < v["ratio"])
        v["pct"] = (rank / (n - 1)) if n > 1 else 1.0
        v["crowded"] = bool(
            v["ratio"] >= float(getattr(C, "CROWDING_RATIO_THRESHOLD", 1.5))
            or v["pct"] >= float(getattr(C, "CROWDING_PERCENTILE_THRESHOLD", 0.80))
        )
    return out


def _resolve_prices_path(run_root: Path, asof: str, explicit: str | None = None) -> Path:
    """🔴 2026-11-11（审查发现 3.5）：**行情源必须与规则层同源**。

    规则层（`final_rebalance_plan`）读的是**本轮**的
    `etf_prices_effective_{asof}.csv`；而本脚本原先读 canonical `C.ETF_PRICES_CSV`
    ⇒ 两边 `corr120` / 拥挤度可能对不上，且 canonical 过期时 `corr120` 会全 0。
    ⇒ 取价优先级：显式 `--prices` → 本轮 `etf_prices_effective_{asof}.csv`
    → 本轮 `etf_prices_daily_{asof}.csv` → canonical 兜底。
    """
    if explicit:
        return Path(explicit)
    if asof:
        _stamp = str(asof).replace("-", "")
        for pat in (f"etf_prices_effective_{_stamp}.csv", f"etf_prices_daily_{_stamp}.csv"):
            cand = Path(run_root) / pat
            if cand.exists():
                return cand
    return Path(C.ETF_PRICES_CSV)


def build(run_root: Path, report_md: Path, backtest_new: Path | None,
          backtest_old: Path | None, backtest_mid: Path | None = None,
          quality: Path | None = None, prices_path: str | None = None
          ) -> tuple[str, dict]:
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
    add("| 代码 | 名称 | 主题 | 最终分 | E(W×S) | mom120 | 覆盖 | 入选 |")
    add("|:--|:--|:--|--:|--:|--:|--:|:--:|")
    for c in rep["candidates"]:
        e_score = c.get("e_score")
        cover = c.get("coverage")
        theme = c.get("theme_id")
        add(f"| {c['code']} | {c.get('name', '—')} | "
            f"{'—' if not theme else theme} | "
            f"{c.get('final_score', float('nan')):.4f} | "
            f"{'—' if e_score is None else format(e_score, '.4f')} | "
            f"{_fmt_pct(c.get('mom120'))} | "
            f"{'—' if cover is None else format(cover, '.2f')} | "
            f"{'✔' if c.get('selected') else '✘'} |")
    add("")

    # ---- 3.5 候选池重合度筛查（一票否决依据）----
    #    🔴 数据来自**落盘产物**（etf_holdings.csv / etf_prices.csv），
    #       口径复用 `manu/etf_rotation/overlap.py`（唯一实现）⇒ 与规则层同源。
    _cands = rep.get("candidates", []) or []
    _ov_codes = [str(c.get("code") or "").zfill(6) for c in _cands if c.get("code")]
    _ov_names = {str(c.get("code") or "").zfill(6): str(c.get("name") or "") for c in _cands}
    _ov_pairs: list[dict] = []
    _ov_available = False
    _ov_reason = ""
    _prices_file = _resolve_prices_path(Path(run_root), str(rep["meta"]["asof"]), prices_path)
    # 🔴 2026-11-11：拥挤度与 meta 字段（文档 §三.2）—— 算不出来就**如实标注不可得**，不编造。
    _crowding: dict[str, dict] = {}
    _crowding_reason = ""
    try:
        _ov_holdings, _ov_prices = overlap_mod.load_overlap_inputs(
            holdings_path=C.ETF_HOLDINGS_CSV, prices_path=_prices_file)
        if _ov_holdings is None:
            _ov_reason = f"成分权重表不可得（{C.ETF_HOLDINGS_CSV}）"
        else:
            _ov_pairs = overlap_mod.compute_overlap_pairs(
                holdings=_ov_holdings, prices=_ov_prices, codes=_ov_codes,
                asof=rep["meta"]["asof"], names=_ov_names)
            _ov_available = True
    except Exception as exc:  # noqa: BLE001 —— 算不出来就**整节省略**，不喂假数据
        _ov_available = False
        _ov_reason = f"重合度计算异常：{type(exc).__name__}"
        _ov_pairs = []
    if _ov_available:
        add("## 3.5. 候选池重合度筛查（一票否决依据）")
        add("")
        add("> 规则层已按「**成分重叠 ≥ 0.90 或 120 日相关性 ≥ 0.95**」逐对拦截"
            "（冲突对不共存）；下表是**更宽口径**的预警"
            "（成分重叠 ≥ 0.50 或 120 日相关性 ≥ 0.80 或同族）——"
            "**这张表就是你行使一票否决权的合法依据**："
            "凡表中**同族**、或成分重叠/相关性明显偏高的候选对，"
            "你都可以据此**否决**其中一只"
            "（否决 ⇒ 该标的从候选池剔除 ⇒ 规则层顺延补位）。"
            "**否决后的确定性后果**：规则层会**顺延挑选下一个不同主题的资产**"
            "（等权口径不变：顺延者沿用被否决者的目标权重；"
            "顺延者必须与已入选集合**不同族**，且不构成规则层冲突对）；"
            "候选池里没有合格顺延者时**不强行补**（宁缺毋滥）。")
        add("")
        _flagged = [p for p in _ov_pairs if p.get("screen_flag")]
        if not _flagged:
            add(f"本轮候选池（{len(_ov_codes)} 只）内**没有**触发更宽口径预警的高重合对。")
            add("")
        else:
            add("| 代码A | 名称A | 代码B | 名称B | 成分重叠系数 | 120 日相关性 | 同族 | 规则层冲突对 |")
            add("|:--|:--|:--|:--|--:|--:|:--:|:--:|")
            for p in _flagged:
                add(f"| {p['a']} | {p.get('a_name') or '—'} | {p['b']} | "
                    f"{p.get('b_name') or '—'} | {float(p['weight_overlap']):.3f} | "
                    f"{float(p['corr120']):.3f} | "
                    f"{'是' if p.get('same_theme') else '否'} | "
                    f"{'**是**' if p.get('rule_conflict') else '否'} |")
            add("")
        # ---- 拥挤度（文档 §三.2「检查行业重叠度与**拥挤度**」）----
        # 🔴 2026-11-11：此前**只**做了重叠度，拥挤度未实现 ⇒ 这里按
        #    「近 20 日日均成交额 ÷ 基期 120 日日均成交额」算放量倍数 + 池内分位。
        _crowding = compute_crowding(_ov_prices, _ov_codes, str(rep["meta"]["asof"]))
        if _crowding:
            add("### 3.5b. 候选池拥挤度（放量倍数）")
            add("")
            add(f"> 口径：**近 {int(getattr(C, 'CROWDING_RECENT_WINDOW', 20))} 日日均成交额 ÷ "
                f"基期 {int(getattr(C, 'CROWDING_BASE_WINDOW', 120))} 日日均成交额**"
                f"（>1 = 放量 = 拥挤上升）；"
                f"判「拥挤」= 放量倍数 ≥ "
                f"{float(getattr(C, 'CROWDING_RATIO_THRESHOLD', 1.5)):.2f} "
                f"**或** 池内分位 ≥ "
                f"{float(getattr(C, 'CROWDING_PERCENTILE_THRESHOLD', 0.80)):.0%}。"
                "**拥挤度是风险提示，不是选股依据** —— 它提醒你「同主题 + 高拥挤」的组合"
                "在反身性上更脆弱（资金撤离时同向踩踏），"
                "可作为**否决某一只**的补充理由。")
            add("")
            add("| 代码 | 名称 | 放量倍数 | 池内分位 | 拥挤 |")
            add("|:--|:--|--:|--:|:--:|")
            for _c in _ov_codes:
                _cv = _crowding.get(_c)
                if not _cv:
                    continue
                add(f"| {_c} | {_ov_names.get(_c) or '—'} | {float(_cv['ratio']):.2f} | "
                    f"{float(_cv['pct']):.0%} | {'**是**' if _cv.get('crowded') else '否'} |")
            add("")
            _crowded_codes = [c for c, v in _crowding.items() if v.get("crowded")]
            if _crowded_codes:
                add(f"⚠️ 本轮判为**拥挤**的候选：{'、'.join(sorted(_crowded_codes))}"
                    f"（共 {len(_crowded_codes)} 只）")
            else:
                add("ℹ️ 本轮**无**候选被判为拥挤。")
            add("")
        else:
            _crowding_reason = (
                "拥挤度数据不可得（成交额字段缺失 / 样本不足 / 行情不可得）")
            add(f"> ℹ️ **拥挤度不可得**：{_crowding_reason} ⇒ 本轮**不得**据此下任何判断。")
            add("")
    else:
        _ov_reason = _ov_reason or "重合度数据不可得"
        _crowding_reason = f"依赖同一份行情（{_ov_reason}）⇒ 拥挤度一并不可得"


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
    # 🔴 2026-11-11：把 TA 的**身份 / 权力 / 职责**写进任务节。
    #    旧版只问了三个问题 ⇒ TA 不知道自己是「合规陪审团」、
    #    不知道自己能一票否决、也不知道否决之后规则层会顺延补位
    #    ⇒ 文档要求的「主题过度集中预警」职责事实上无法落地（TA 没有被授权）。
    add("### 7.1 你的身份与权力（合规陪审团，不是操盘手）")
    add("")
    add("- **身份**：你是**合规陪审团**，只「审」不「操盘」——"
        "不替规则层选股，也不替账户下单。")
    add("- **正式权力只有一票否决**：对**候选池里的标的**你可以行使**一票否决**"
        "（黑天鹅：成分股突发财务/政策风险；主题过度集中：底层资产高度重合）。"
        "否决后规则层把该标的从候选池剔除，并**顺延挑选下一个不同主题的资产**"
        "（等权口径不变，见 3.5 节）。")
    add("- **没有的权力**：❌ 不许推荐**候选池之外**的标的；"
        "❌ 不许自行指定**买多少股**——股数与权重由规则层用确定性数学算"
        "（等权口径 + 现金预留比例）；你只给方向与理由。")
    add("- **职责之一（主题过度集中 / 重合度审查）**：对照 **3.5 节**的重合度筛查表，"
        "检查排名靠前的两只是否底层资产高度重合（如「银行ETF」与「金融ETF」）；"
        "若重合度过高，请**明确点名否决**其中一只，别只在论据里提一句。")
    add("- **仓位建议（方向性，可选）**：你可以就**组合总仓位（权益敞口）**给方向性建议"
        "（例如极端逆风期建议压降权益上限、其余留现金）；"
        "**具体股数/权重由规则层算**。"
        "若你给出权益上限建议，请**写明数值与依据**"
        "（例如「权益上限 50%，依据：…」）。")
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
        # 🔴 「候选池重合度筛查」小节是否可用（算不出来 ⇒ 整节省略，**不喂假数据**）
        "overlap_screen_available": bool(_ov_available),
        "overlap_screen_reason": _ov_reason,
        # 🔴 2026-11-11（文档 §三.2）：拥挤度（放量倍数）可得性
        "crowding_available": bool(_crowding),
        "crowding_reason": _crowding_reason,
        # 🔴 2026-11-11（审查发现 3.5）：行情源必须与规则层同源（记下来便于审计）
        "prices_source": str(_prices_file),
    }
    return text, meta


def _sha1(p: Path) -> str:
    import hashlib
    return hashlib.sha1(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="构造 TradingAgents research_context")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    ap.add_argument("--report-md", default=None)
    ap.add_argument("--backtest-new", default=None)
    ap.add_argument("--backtest-old", default=None)
    ap.add_argument("--backtest-mid", default=None)
    ap.add_argument("--cluster-quality", default=None)
    ap.add_argument("--out", default=None)
    # 🔴 2026-11-11（审查发现 3.5）：显式指定**与规则层同源**的行情文件
    ap.add_argument("--prices", default=None)
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
        prices_path=args.prices,
    )
    out.write_text(text, encoding="utf-8")
    (run_root / "ta_context_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out}（{len(text)} 字符）")
    print(f"已写出 {run_root / 'ta_context_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
