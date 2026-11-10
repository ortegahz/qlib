"""从 qlib 导出**个股日线**（date / instrument / close），供 `analyze.py --stock-prices` 用。

【为什么需要】
`analyze.py` 的多日 RankIC（N=5 / N=20 交易日前瞻收益）需要**全市场个股收盘价**。
`stock_scores.csv` 自带的 `fwd_ret` 只有 **1 日**前瞻收益 ⇒ 多日 RankIC 必须另取价格。
本脚本从 **qlib 本地数据**（`$close`）导出，避免临时用 akshare 拉全市场行情。

【口径】
- 区间：默认 `[首个打分日 - LOOKBACK_BUFFER 个自然日, asof]`，可 `--start/--end` 覆盖。
  留 buffer 是为了让「信号日往前推 N 个交易日」的窗口也有价（否则 N=20 的前瞻收益无处落脚）。
- 复权：qlib 的 `$close` 是**已复权**价格（qlib 落盘时统一处理），与打分用的 `$vwap` 同一口径。
- 未来函数：只导出 **≤ asof** 的行；N 日前瞻收益的**实现日必须 ≤ asof**，由 analyze.py 负责守。

【用法（服务器）】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/data/export_qlib_prices.py \
        --asof 2026-11-03 --out <落点根>/runs/stock_prices.csv

产物落在**唯一落点根**下（持久盘），**不写 /tmp**。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

#: 研究链根（`data/` 不是包 ⇒ 按**绝对路径**把研究链根加进 sys.path）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as C  # noqa: E402

# qlib 数据与打分产物（**与研究链 `config.py` 同源** ⇒ 单一事实源，不会分叉）
#: qlib provider（上游基座；与研究链 `config.PROVIDER_URI` 同源）
PROVIDER_URI = str(C.PROVIDER_URI)
#: 个股分数 S（上游产物；落点 `<落点根>/runs/stock_scores.csv`）
STOCK_SCORES_CSV = str(C.ETF_RUN_ROOT / "stock_scores.csv")
#: 多日 RankIC 的最大前瞻窗口（交易日）⇒ 往前多留一点，防止信号日附近无价
MAX_HORIZON = 20
#: 额外留的自然日缓冲（约 MAX_HORIZON 个交易日 ≈ 28 自然日，取 60 保险）
LOOKBACK_BUFFER_DAYS = 60


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从 qlib 导出个股日线（供 analyze.py --stock-prices）")
    p.add_argument("--asof", default=None, help="截止日（含）；默认取 stock_scores.csv 最新日")
    p.add_argument("--start", default=None, help="起始日（含）；默认 = 首个打分日 - LOOKBACK_BUFFER")
    p.add_argument("--end", default=None, help="结束日（含）；默认 = --asof")
    p.add_argument("--scores", default=STOCK_SCORES_CSV, help="个股分数 CSV（取打分日范围）")
    p.add_argument("--provider-uri", default=PROVIDER_URI, help="qlib provider_uri")
    p.add_argument("--out", default=str(C.ETF_RUN_ROOT / "stock_prices.csv"))
    p.add_argument("--instruments", default="all", help="qlib 股票池，默认 all（全市场）")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    scores = pd.read_csv(args.scores, dtype={"instrument": str}, usecols=["date", "instrument"])
    scores["date"] = pd.to_datetime(scores["date"])
    if scores.empty:
        raise ValueError(f"个股分数文件为空：{args.scores}")
    first_score = scores["date"].min()
    last_score = scores["date"].max()

    asof = pd.Timestamp(args.asof) if args.asof else last_score
    end = pd.Timestamp(args.end) if args.end else asof
    start = (
        pd.Timestamp(args.start)
        if args.start
        else first_score - pd.Timedelta(days=LOOKBACK_BUFFER_DAYS)
    )
    if start >= end:
        raise ValueError(f"起始日 {start.date()} 必须早于结束日 {end.date()}")

    print(f"[INFO] 打分区间 {first_score.date()} ~ {last_score.date()}")
    print(f"[INFO] 导出区间 {start.date()} ~ {end.date()}（≤ asof，无未来函数）")

    import qlib
    from qlib.data import D

    qlib.init(provider_uri=args.provider_uri, region="cn")

    # 🔴 两个上游坑（与 manu/etf_rotation/qlib_scores.py:286-306 同一份记录）：
    #   1) `D.features` 的第一个参数必须是 **list/Index**（`get_instruments_d` 只认
    #      dict / list / Index / ndarray，传字符串会抛 "Unsupported input type for param `instrument`"）；
    #   2) `D.list_instruments` 的第一个参数必须是 **dict**，且**必须带 `filter_pipe` 键**
    #      （内部直接取 `instruments["filter_pipe"]`）⇒ 漏了这个键就是 `KeyError: 'filter_pipe'`。
    insts = list(
        D.list_instruments({"market": args.instruments, "filter_pipe": []}, as_list=True)
    )
    if not insts:
        raise ValueError(f"qlib 股票池为空：market={args.instruments}")
    print(f"[INFO] 股票池 {args.instruments} ⇒ {len(insts)} 只")

    wide = D.features(
        insts,
        ["$close"],
        start_time=start.strftime("%Y-%m-%d"),
        end_time=end.strftime("%Y-%m-%d"),
        freq="day",
    )
    if wide is None or wide.empty:
        raise ValueError("qlib 返回空的 $close 面板 ⇒ 无法导出（检查 provider_uri 与区间）")

    # 🔴 `D.features` 返回的 MultiIndex 名字是 **(instrument, datetime)**（instrument 在前！）
    out = wide.reset_index()
    out = out.rename(columns={"$close": "close"})
    # MultiIndex 层级名是 (instrument, datetime) ⇒ reset_index 后列名是 instrument/datetime
    if "date" not in out.columns and "datetime" in out.columns:
        out = out.rename(columns={"datetime": "date"})
    if "date" not in out.columns or "instrument" not in out.columns:
        raise ValueError(
            f"qlib 返回的列名与预期不符：{list(out.columns)}"
            "（期望含 date/instrument/close）⇒ 拒绝落盘"
        )
    out["instrument"] = out["instrument"].astype(str)
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["close"]).copy()
    out = out[["date", "instrument", "close"]]
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out = out.sort_values(["date", "instrument"]).reset_index(drop=True)

    # 退化守卫：价格面板必须有足够多的行与标的，否则下游 RankIC 会「跑完但无信息」
    n_dates = out["date"].nunique()
    n_inst = out["instrument"].nunique()
    if len(out) < 100_000 or n_dates < 60 or n_inst < 500:
        raise ValueError(
            f"导出的价格面板过小（行 {len(out)} / 日期 {n_dates} / 标的 {n_inst}）"
            " ⇒ 拒绝落盘（宁可不产出，不产出假结果）"
        )
    price_null_ratio = float(out["close"].isna().mean())
    if price_null_ratio > 0.2:
        raise ValueError(f"close 缺失率 {price_null_ratio:.2%} 过高 ⇒ 拒绝落盘")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    print(f"[INFO] 已写出 {out_path}（{len(out)} 行 / {size_mb:.1f} MB）")
    print(f"[INFO] 交易日 {n_dates} 天 / 标的 {n_inst} 只 / 区间 {out['date'].min()} ~ {out['date'].max()}")
    print(f"[INFO] 末行样本：{out.tail(1).to_dict('records')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
