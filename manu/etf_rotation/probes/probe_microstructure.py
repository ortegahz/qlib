#!/usr/bin/env python
"""ETF 微观结构披露闸门（**只读**，不写任何文件、不联网、不跑 LLM）。

【为什么需要（2026-11-05 00:5x 实测，专题档 §38）】
M-K 修的是 **报告正文**：三条风险披露原先**只有结论、没有数字**，且有一条指针指错小节：

① 🔴 ``risk_disclosure[8]``（segment_instability）
   旧文案只写「半年度超额**符号相反**」**不带数字**，且指针写「详见 … **§11.2**」——
   而 §11.2 是**聚类重构（#21）之前**的老口径，已被 §18.4 明确作废
   ⇒ 只读报告的人落点即坑（§11.2 正文还残留 MDD −39.31% / 换手 19.2× 等旧数字）。
   修法：正文带 H1/H2 **实测超额 + 天数区间**，指针改指 **§18.4**。

② 🔴 ``risk_disclosure[11]``（upstream_degradation）只写「已做退化守卫」，**没给数字**。

③ 🔴 ``risk_disclosure[12]``（etf_tracking_and_premium）只写「未建模折溢价/跟踪误差/
   流动性分层」，**没给数字** ⇒ 读者无法判断「未建模」到底差多少。

【本探针做什么】
**不信任产物自述**：折溢价率中位数与跟踪误差代理中位数都**独立重算**（本文件自带一份
**另写**的实现，从原始 CSV 起算）再与报告比对；并用**负例**证明断言**真的会拦人**
（把产物改坏 ⇒ 对应断言必须 FAIL）。

🔴 **负例纪律（rules.md §十四.68）**：负例必须**真的改到被断言的对象上**。
本探针的被断言对象是 ``dict`` ⇒ 负例用 ``copy.deepcopy`` 后**原地改字段**再喂回
``evaluate()``（dict 是引用语义 ⇒ 改动一定生效），**不做**「字符串 replace 不回写」那种假负例。

【用法】
    PYTHONHASHSEED=0 /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_microstructure.py

【副作用】**零写入**：只读官方产物与原始 CSV。
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys

from pathlib import Path
import numpy as np
import pandas as pd
#: 研究链根（探针目录不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ETF_ROOT = Path(__file__).resolve().parents[1]
if str(_ETF_ROOT) not in sys.path:
    sys.path.insert(0, str(_ETF_ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）


RUN_ROOT = C.ETF_RUN_ROOT
ETF_DATA = C.ETF_DATA_ROOT

#: 年化交易日数
TD = 252
#: 折溢价率中位数允许的复算偏差（pp）
TOL_PREMIUM_PP = 0.01
#: 跟踪误差代理中位数允许的复算偏差（pp）
TOL_TE_PP = 0.05


def _median(values: list[float]) -> float | None:
    """中位数（空集合 ⇒ None，不补 0）。"""
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    if arr.size == 0:
        return None
    return float(np.median(arr))


def _to_instrument(code: str, exchange: str | None) -> str | None:
    """把 ``etf_holdings.csv`` 的 ``stock_code`` 转成 qlib instrument（**独立实现**）。

    🔴 ``stock_prices.csv`` 的主键是 ``instrument``（如 ``SH600000``），
       而 ``etf_holdings.csv`` 给的是 ``300857.SZ`` ⇒ 不转换就**一只都匹配不上**
       （独立重算会静默返回 0 只 ⇒ 断言变成空转）。优先用 ``stock_exchange`` 列。
    """
    c = str(code).strip().upper()
    if not c:
        return None
    ex = str(exchange or "").strip().upper()
    if "." in c:
        head, _, tail = c.partition(".")
        if head.isalpha():
            return c
        if tail in ("SH", "SZ", "BJ"):
            ex = ex or tail
            c = head
    c = c.zfill(6)
    if not c.isdigit():
        return None
    if ex in ("SH", "SZ", "BJ"):
        return f"{ex}{c}"
    if c.startswith(("60", "68", "51", "52", "56", "58")):
        return f"SH{c}"
    if c.startswith(("00", "30")):
        return f"SZ{c}"
    if c.startswith(("43", "83", "87", "88", "92")):
        return f"BJ{c}"
    return None


def independent_recompute(codes: list[str], asof: str, lookback: int = 60,
                          stock_prices: Path | None = None,
                          etf_prices: Path | None = None) -> dict:
    """**独立**重算折溢价率中位数与跟踪误差代理中位数（不走 analyze 的实现路径）。

    刻意另写一份实现：从 ``etf_prices.csv`` / ``etf_holdings.csv`` / ``etf_spot.csv``
    / ``stock_prices.csv`` 直接起算，用于校验报告里的数字**确实是数据算出来的**。

    ``stock_prices`` = 个股日线 CSV 路径（缺省 = canonical 产物根那份）；
    架构迁移后**交付件根 = 快照目录** ⇒ 独立重算必须读**同一轮**的个股日线，
    否则「报告数字 vs 独立重算」会跨轮对比。

    🔴 ``etf_prices`` = ETF 行情 CSV 路径（缺省 = canonical ``etf_prices.csv``，
       止于 2026-11-04）——**报告读的是本轮** ``etf_prices_effective_<asof>.csv``
       （止于 2026-11-09）⇒ 探针不传对输入就**根本没在验报告那份数据**
       （实测报告 2.5451% vs 用 canonical 重算 2.4437%，差 0.1014pp > 0.05pp）。
       ⇒ 调用方必须把**报告实际用的那份**传进来（见 :func:`resolve_etf_prices`）。
    """
    asof_ts = pd.Timestamp(asof)
    out: dict = {"premium_median_pct": None, "te_median_pct": None, "n_te_computable": 0}

    # ---- 折溢价率（etf_spot.csv 的「基金折价率」）----
    spot_path = ETF_DATA / "etf_spot.csv"
    if spot_path.exists():
        spot = pd.read_csv(spot_path, dtype={"代码": str}, encoding="utf-8-sig")
        spot["代码"] = spot["代码"].astype(str).str.zfill(6)
        spot["基金折价率"] = pd.to_numeric(spot["基金折价率"], errors="coerce")
        sub = spot[spot["代码"].isin(codes)]
        vals = [float(v) for v in sub["基金折价率"].to_numpy(dtype=float) if np.isfinite(v)]
        out["premium_median_pct"] = _median(vals)
        out["premium_n"] = len(vals)

    # ---- 跟踪误差代理 ----
    prices_path = Path(etf_prices) if etf_prices is not None else (ETF_DATA / "etf_prices.csv")
    hold_path = ETF_DATA / "etf_holdings.csv"
    sp_path = Path(stock_prices) if stock_prices is not None else (RUN_ROOT / "stock_prices.csv")
    out["etf_prices_path"] = str(prices_path)
    out["etf_prices_last_date"] = None
    if not (prices_path.exists() and hold_path.exists() and sp_path.exists()):
        return out
    pr = pd.read_csv(prices_path, dtype={"code": str})
    pr["date"] = pd.to_datetime(pr["date"])
    if not pr.empty:
        out["etf_prices_last_date"] = str(pr["date"].max().date())
    pr["code"] = pr["code"].astype(str).str.zfill(6)
    pr = pr[pr["date"] <= asof_ts].sort_values(["code", "date"])
    etf_ret: dict[str, pd.Series] = {}
    for code, g in pr.groupby("code", sort=False):
        if code not in codes:
            continue
        close = g["close"].to_numpy(dtype=float)
        if close.size < lookback + 1:
            continue
        tail = close[-(lookback + 1):]
        idx = pd.to_datetime(g["date"].to_numpy()[-tail.size:])
        r = pd.Series(np.diff(tail) / tail[:-1], index=idx[1:])
        r = r[np.isfinite(r.to_numpy())]
        if len(r) >= 30:
            etf_ret[code] = r
    if not etf_ret:
        return out

    hd = pd.read_csv(hold_path, dtype={"etf_code": str, "stock_code": str})
    hd["etf_code"] = hd["etf_code"].astype(str).str.zfill(6)
    hd["weight_pct"] = pd.to_numeric(hd["weight_pct"], errors="coerce")
    if "stock_exchange" in hd.columns:
        hd["instrument_probe"] = [
            _to_instrument(sc, ex)
            for sc, ex in zip(hd["stock_code"], hd["stock_exchange"])
        ]
    else:
        hd["instrument_probe"] = [_to_instrument(sc, None) for sc in hd["stock_code"]]
    need: set[str] = set()
    per: dict[str, pd.DataFrame] = {}
    for code in codes:
        h = hd[hd["etf_code"] == code].copy()
        if h.empty:
            continue
        h = h[["instrument_probe", "weight_pct"]]
        h = h[h["instrument_probe"].notna()]
        h = h[np.isfinite(h["weight_pct"].to_numpy(dtype=float)) & (h["weight_pct"] > 0)]
        if h.empty:
            continue
        per[code] = h
        need.update(h["instrument_probe"].tolist())
    if not need:
        return out

    sp = pd.read_csv(sp_path, dtype={"instrument": str})
    sp["date"] = pd.to_datetime(sp["date"])
    sp["close"] = pd.to_numeric(sp["close"], errors="coerce")
    sp = sp[(sp["date"] <= asof_ts) & (sp["instrument"].isin(need))]
    if sp.empty:
        return out
    wide = sp.pivot_table(index="date", columns="instrument", values="close", aggfunc="last").sort_index()
    rets = wide.pct_change(fill_method=None).iloc[-(lookback + 1):]

    tes: list[float] = []
    for code, h in per.items():
        if code not in etf_ret:
            continue
        cols = [c for c in h["instrument_probe"] if c in rets.columns]
        if not cols:
            continue
        w = h.set_index("instrument_probe")["weight_pct"].reindex(cols).to_numpy(dtype=float)
        tw = float(w.sum())
        if tw <= 0:
            continue
        sub = rets[cols]
        cov = sub.notna().mul(w, axis=1).sum(axis=1) / tw
        ok = cov >= 0.95
        if int(ok.sum()) < 30:
            continue
        port = sub.mul(w, axis=1).sum(axis=1, min_count=1) / tw
        j = pd.concat([etf_ret[code].rename("e"), port.rename("p")], axis=1, join="inner").dropna()
        if len(j) < 30:
            continue
        tes.append(float(np.std((j["e"] - j["p"]).to_numpy(dtype=float), ddof=1) * math.sqrt(TD) * 100.0))
    out["te_median_pct"] = _median(tes)
    out["n_te_computable"] = len(tes)
    return out


def resolve_etf_prices(report_path: Path, asof: str, explicit: str = "") -> tuple[Path, str]:
    """决定探针用哪一份 ETF 行情 ⇒ (路径, 口径说明)。

    优先级：
    ① ``--etf-prices`` 显式指定；
    ② **报告同目录**的 ``etf_prices_effective_<asof>.csv``（交付件里的落盘口径是
       ``<YYYYMMDD>`` 紧凑写法，``daily/main.py`` 用 ``asof.replace('-', '')`` 命名；
       = 报告本轮实际用的那份；架构迁移后交付件根 = 快照目录，effective 行情随快照一起落盘）；
    ③ canonical ``etf_prices.csv``（止于上一轮 asof ⇒ **跨轮对比**，只在 ② 不存在时回落）。

    🔴 回落口径必须在输出里**显式打印**（路径 + 末日）⇒ 事后能判读探针到底验了哪份数据。
    """
    if explicit:
        return Path(explicit), "显式指定（--etf-prices）"
    # 🔴 交付件里的命名口径 = ``etf_prices_effective_<YYYYMMDD>.csv``（daily/main.py:
    #    ``asof.replace('-', '')``）⇒ 两种写法都试，命中即用。
    for stamp in (asof.replace("-", ""), asof):
        effective = report_path.resolve().parent / f"etf_prices_effective_{stamp}.csv"
        if effective.is_file():
            return effective, f"报告同目录 effective（asof={asof}）"
    return ETF_DATA / "etf_prices.csv", "canonical 回落（⚠️ 与报告那份可能不同轮）"


def evaluate(report: dict, md_text: str, indep: dict) -> list[tuple[str, bool, str]]:
    """对给定报告跑**全部**断言，返回 [(名称, 是否通过, 详情), ...]。

    🔴 本函数**只依赖入参** ⇒ 负例可以直接喂「改坏的报告」，不必重跑流水线。
    """
    out: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, bool(ok), detail))

    micro = report.get("etf_microstructure") or {}
    prem = micro.get("premium_discount") or {}
    te = micro.get("tracking_error_proxy") or {}
    liq = micro.get("liquidity") or {}
    eq = prem.get("equity_only") or {}
    tes = te.get("te_annualized_pct") or {}
    disc = {d.get("id"): d for d in (report.get("risk_disclosure") or [])}

    # M1 微观结构段存在
    add("M1 报告必须带 etf_microstructure 段（数字随报告走）",
        bool(micro) and bool(micro.get("sample")),
        f"sample.n_candidates={(micro.get('sample') or {}).get('n_candidates')}")

    # M2 折溢价字段口径可信（与自算 IOPV 溢价交叉验证）
    dev = prem.get("cross_check_max_abs_dev_pp")
    add("M2 折溢价率字段口径可信（与 最新价/IOPV−1 交叉验证偏差 < 0.01pp）",
        dev is not None and float(dev) < 0.01,
        f"max_abs_dev={dev} pp")

    # M3 折溢价中位数与**独立重算**一致
    pm = eq.get("median")
    ipm = indep.get("premium_median_pct")
    ok3 = (
        pm is not None and ipm is not None
        and abs(float(pm) - float(ipm)) <= TOL_PREMIUM_PP
    )
    add("M3 折溢价率中位数与独立重算一致（±0.01pp）",
        ok3, f"报告={pm}%｜独立重算={ipm}%")

    # M4 跟踪误差代理中位数与**独立重算**一致
    tm = tes.get("median")
    itm = indep.get("te_median_pct")
    ok4 = (
        tm is not None and itm is not None
        and abs(float(tm) - float(itm)) <= TOL_TE_PP
    )
    add("M4 跟踪误差代理中位数与独立重算一致（±0.05pp）",
        ok4,
        f"报告={tm}%｜独立重算={itm}%｜探针输入={indep.get('etf_prices_path')}"
        f"（末日 {indep.get('etf_prices_last_date')}）")

    # M5 跟踪误差可算只数与独立重算一致
    add("M5 跟踪误差代理可算只数与独立重算一致",
        te.get("n_computable") == indep.get("n_te_computable"),
        f"报告={te.get('n_computable')}｜独立重算={indep.get('n_te_computable')}")

    # M6 不可算的 ETF 必须**逐只给原因**（不许静默丢）
    reasons = te.get("not_computable_reason") or {}
    n_bad = te.get("n_not_computable") or 0
    ok6 = (n_bad == 0) or (len(reasons) >= n_bad and all(str(v).strip() for v in reasons.values()))
    add("M6 跟踪误差不可算的 ETF 必须逐只给原因（不静默丢）",
        ok6, f"n_not_computable={n_bad}｜给了原因={len(reasons)}")

    # M7 流动性阈值要被记下来（读者才知道分层幅度相对什么）
    add("M7 微观结构段必须记录流动性过滤阈值",
        liq.get("filter_threshold_avg_amount_20d") is not None,
        f"threshold={liq.get('filter_threshold_avg_amount_20d')}")

    # M8 点时刻快照日必须被记下来（asof 与快照日可能差 1 天 ⇒ 必须披露）
    add("M8 折溢价快照日必须被记录（披露与 asof 的口径差）",
        bool(prem.get("spot_snapshot_date")),
        f"spot_snapshot_date={prem.get('spot_snapshot_date')}")

    # M9 risk_disclosure[11] 必须带**实测数字**
    d11 = str((disc.get("upstream_degradation") or {}).get("detail") or "")
    ss = report.get("stock_scores") or {}
    ok9 = (
        bool(d11)
        and str(ss.get("n_stocks")) in d11
        and str(ss.get("unique_values")) in d11
    )
    add("M9 风险披露「上游退化」必须带实测数字（只数/唯一值）",
        ok9, f"n_stocks={ss.get('n_stocks')}｜unique={ss.get('unique_values')}")

    # M10 risk_disclosure[8] 必须带 H1/H2 超额数字
    d8 = str((disc.get("segment_instability") or {}).get("detail") or "")
    seg = ((report.get("meta") or {}).get("backtest_summary") or {}).get("segments") or []
    exs = [s.get("excess_annualized") for s in seg if s.get("excess_annualized") is not None]
    ok10 = bool(exs) and all(f"{v * 100:.2f}%" in d8 for v in exs)
    add("M10 风险披露「分段不稳定」必须带 H1/H2 实测超额数字",
        ok10, f"分段超额={[round(v * 100, 2) for v in exs]}")

    # M11 指针必须指向 §18.4，**不许**再指向 §11.2
    # 🔴 用**正则抓「见 … §X」的引用**而不是「句子里有没有 §11.2」——
    #    正文里「§11 / §11.2 是…老口径，已作废」是**合法的免责说明**（它前面没有「见」），
    #    不能因为出现这三个字就判违规；真正要禁的是「详见 … §11.2」这种**指针**。
    refs = re.findall(r"见[^§]{0,160}§\s*([\d]+(?:\.[\d]+)?)", d8)
    ok11 = bool(refs) and ("18.4" in refs) and ("11.2" not in refs)
    add("M11 风险披露「分段不稳定」指针必须指向 §18.4（不许指 §11.2 老口径）",
        ok11, f"「见…§X」引用到的章节={refs or '（未找到）'}")

    # M12 risk_disclosure[12] 必须带折溢价中位 + TE 中位 + bp 倍数
    d12 = str((disc.get("etf_tracking_and_premium") or {}).get("detail") or "")
    ok12 = (
        bool(d12)
        and pm is not None and f"{float(pm):.2f}%" in d12
        and tm is not None and f"{float(tm):.2f}%" in d12
        and "bp" in d12
    )
    add("M12 风险披露「ETF 自身风险」必须带折溢价中位 + TE 中位 + bp 换算",
        ok12, f"premium_med={pm}%｜te_med={tm}%｜含 bp={'bp' in d12}")

    # M13 报告 §八 表头必须说清倒计时起算点（自信号日 asof）
    ok13 = ("距**信号日**交易日数（自信号日 asof 起算）" in md_text)
    add("M13 报告 §八 表头必须写明倒计时自信号日起算",
        ok13, f"查找串存在={ok13}")

    return out


def _fmt(rows: list[tuple[str, bool, str]]) -> list[str]:
    lines: list[str] = []
    for name, ok, detail in rows:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
        lines.append(f"        {detail}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 微观结构披露闸门")
    ap.add_argument("--report", default=str(RUN_ROOT / "etf_analysis_report.json"))
    ap.add_argument("--md", default=str(RUN_ROOT / "etf_analysis_report.md"))
    ap.add_argument("--stock-prices", default=str(RUN_ROOT / "stock_prices.csv"),
                    help="个股日线 CSV（缺省 = 产物根那份；交付件根 = 快照时须显式指向快照内那份）")
    ap.add_argument("--etf-prices", default="",
                    help="ETF 行情 CSV（缺省 = 报告同目录 etf_prices_effective_<asof>.csv；"
                         "没有才回落 canonical etf_prices.csv）")
    args = ap.parse_args()

    rep_path, md_path = Path(args.report), Path(args.md)
    for p, label in ((rep_path, "分析报告 json"), (md_path, "分析报告 md")):
        if not p.exists():
            print(f"🔴 缺{label}：{p}")
            return 1
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8")

    codes = [str(c.get("code", "")).zfill(6) for c in (report.get("candidates") or [])]
    asof = str((report.get("meta") or {}).get("asof") or "")
    etf_prices, price_source = resolve_etf_prices(rep_path, asof, args.etf_prices)
    print(f"[INFO] 候选 {len(codes)} 只｜asof {asof}｜独立重算中…")
    print(f"[INFO] 探针 ETF 行情输入：{etf_prices}（口径：{price_source}）")
    indep = independent_recompute(codes, asof, stock_prices=Path(args.stock_prices),
                                   etf_prices=etf_prices)
    print(f"[INFO] 该行情末日：{indep.get('etf_prices_last_date')}")
    print(f"[INFO] 独立重算：折溢价中位={indep.get('premium_median_pct')}%｜"
          f"TE 中位={indep.get('te_median_pct')}%｜可算 {indep.get('n_te_computable')} 只")

    rows = evaluate(report, md_text, indep)
    print("=" * 78)
    for line in _fmt(rows):
        print(line)

    # ---- 负例：证明这些断言真的会拦人（§十四.68：负例必须真的改到被断言的对象）----
    print("-" * 78)
    neg_rows: list[tuple[str, bool, str]] = []

    def expect_fail(tag: str, target: str, mutate) -> None:
        """喂一个「被改坏的报告」，断言指定的那条**必须 FAIL**。"""
        bad = copy.deepcopy(report)
        mutate(bad)
        got = {name: ok for name, ok, _ in evaluate(bad, md_text, indep)}
        ok = got.get(target) is False
        neg_rows.append((f"N{tag} 把产物改坏 ⇒ 「{target}」必须 FAIL", ok,
                         f"改坏后该断言 ok={got.get(target)}（必须为 False）"))

    def _strip_numbers(d: dict) -> None:
        for item in d.get("risk_disclosure") or []:
            if item.get("id") == "segment_instability":
                item["detail"] = "同一配置的半年度超额**符号相反** ⇒ 结论脆弱"

    def _back_pointer(d: dict) -> None:
        for item in d.get("risk_disclosure") or []:
            if item.get("id") == "segment_instability":
                item["detail"] = str(item.get("detail") or "").replace("§18.4", "§11.2")

    def _strip_etf_numbers(d: dict) -> None:
        for item in d.get("risk_disclosure") or []:
            if item.get("id") == "etf_tracking_and_premium":
                item["detail"] = "折溢价率、跟踪误差、流动性分层**未建模**"

    def _tamper_te(d: dict) -> None:
        te = (d.get("etf_microstructure") or {}).get("tracking_error_proxy") or {}
        st = te.get("te_annualized_pct") or {}
        if st.get("median") is not None:
            st["median"] = float(st["median"]) + 5.0

    def _tamper_premium(d: dict) -> None:
        prem = (d.get("etf_microstructure") or {}).get("premium_discount") or {}
        eq = prem.get("equity_only") or {}
        if eq.get("median") is not None:
            eq["median"] = float(eq["median"]) + 0.5

    def _drop_micro(d: dict) -> None:
        d.pop("etf_microstructure", None)

    def _tamper_score(d: dict) -> None:
        ss = d.get("stock_scores") or {}
        if ss.get("n_stocks") is not None:
            ss["n_stocks"] = int(ss["n_stocks"]) + 1

    expect_fail("1", "M10 风险披露「分段不稳定」必须带 H1/H2 实测超额数字", _strip_numbers)
    expect_fail("2", "M11 风险披露「分段不稳定」指针必须指向 §18.4（不许指 §11.2 老口径）", _back_pointer)
    expect_fail("3", "M12 风险披露「ETF 自身风险」必须带折溢价中位 + TE 中位 + bp 换算", _strip_etf_numbers)
    expect_fail("4", "M4 跟踪误差代理中位数与独立重算一致（±0.05pp）", _tamper_te)
    expect_fail("5", "M3 折溢价率中位数与独立重算一致（±0.01pp）", _tamper_premium)
    expect_fail("6", "M1 报告必须带 etf_microstructure 段（数字随报告走）", _drop_micro)
    expect_fail("7", "M9 风险披露「上游退化」必须带实测数字（只数/唯一值）", _tamper_score)

    for line in _fmt(neg_rows):
        print(line)

    n_pass = sum(1 for _, ok, _ in rows if ok)
    n_neg = sum(1 for _, ok, _ in neg_rows if ok)
    n_fail = len(rows) - n_pass + (len(neg_rows) - n_neg)
    print("=" * 78)
    print(f"合计 {len(rows) + len(neg_rows)} 项：PASS {n_pass + n_neg} / FAIL {n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
