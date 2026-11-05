"""抓取 A 股**完整交易日历**（含未来交易日）→ `trade_calendar.csv`。

【为什么需要它】
qlib 自带的 `calendars/day.txt` **只到最后一个有行情的交易日**（当前是 2026-11-03），
**不含未来** ⇒ 无法据此判断「下一个自然月的第一个交易日」。
akshare 的 `tool_trade_date_hist_sina()`（新浪）给出交易所公布的**整年日历，含未来交易日**。

【执行机】服务器；解释器 `/home/huangzhe/anaconda3/envs/ak/bin/python`
（🔴 akshare **绝对不能**装进 `ql` env —— 两个 env 必须分开）

【用法】
    /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_trade_calendar.py \
        --out /mnt/data/siping/datasets/etf/trade_calendar.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

DEFAULT_OUT = Path("/mnt/data/siping/datasets/etf/trade_calendar.csv")


def fetch() -> pd.DataFrame:
    """取新浪 A 股交易日历，返回列 ``date``（YYYY-MM-DD，升序、去重）。"""
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    if df is None or len(df) == 0:
        raise RuntimeError("akshare tool_trade_date_hist_sina() 返回空 ⇒ 交易日历不可得")
    col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    out = pd.DataFrame({"date": pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d")})
    out = out.drop_duplicates().sort_values("date").reset_index(drop=True)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="抓 A 股完整交易日历（含未来）")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = fetch()
    df.to_csv(out, index=False, encoding="utf-8")
    print(f"[OK] 交易日历 {len(df)} 行 → {out}")
    print(f"     首日 {df['date'].iloc[0]}  末日 {df['date'].iloc[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
