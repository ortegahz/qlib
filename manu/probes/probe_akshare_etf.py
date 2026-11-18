"""探针：实测 akshare 1.20.x 的 ETF / 指数成分接口可用性与字段。

【为什么需要】
akshare 的 ETF 接口在版本间字段会变 ⇒ **不猜**，逐个跑一遍并把真实列名记下来。
本探针只读、只打印，不落盘任何数据。

【执行机】服务器 `huangzhe@192.168.99.40:32222`，解释器 `/home/huangzhe/anaconda3/envs/ak/bin/python`
（独立 env，避免污染 qlib 的 `ql` env —— 见 manu/memory/rules.md §二 缓存与依赖克制）

    python manu/etf_rotation/probes/probe_akshare_etf.py
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore")

import akshare as ak  # noqa: E402


def probe(name: str, fn) -> None:
    print("=" * 78)
    print(f"[PROBE] {name}")
    try:
        df = fn()
    except Exception as exc:  # noqa: BLE001 —— 探针就是要暴露异常
        print(f"  [FAIL] {type(exc).__name__}: {str(exc)[:300]}")
        return
    print(f"  [OK] shape={getattr(df, 'shape', None)}")
    try:
        print(f"  cols: {list(df.columns)[:25]}")
    except Exception:  # noqa: BLE001
        pass
    try:
        print(df.head(3).to_string()[:900])
    except Exception as exc:  # noqa: BLE001
        print(f"  head fail: {exc}")


def main() -> int:
    print(f"akshare={ak.__version__}")
    probe("fund_etf_spot_em（场内 ETF 全列表实时行情）", lambda: ak.fund_etf_spot_em())
    probe(
        "fund_etf_hist_em（ETF 日线历史，510300）",
        lambda: ak.fund_etf_hist_em(
            symbol="510300", period="daily", start_date="20240101", end_date="20240301", adjust="qfq"
        ),
    )
    probe(
        "fund_etf_fund_info_em（ETF 档案，510300）",
        lambda: ak.fund_etf_fund_info_em(fund="510300", start_date="20240101", end_date="20240301"),
    )
    probe(
        "index_stock_cons_weight_csindex（中证 300 成分权重）",
        lambda: ak.index_stock_cons_weight_csindex(symbol="000300"),
    )
    probe("fund_etf_category_sina（ETF 分类 + 跟踪标的）", lambda: ak.fund_etf_category_sina(symbol="ETF基金"))
    probe(
        "fund_portfolio_hold_em（场内基金持股明细，510300@2024Q4）",
        lambda: ak.fund_portfolio_hold_em(symbol="510300", date="2024"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
