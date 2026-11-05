"""权重矩阵 W：ETF → 成分股 → 权重；以及映射分 `E = W × S`（doc2 的核心）。

【W 的来源】
akshare `fund_etf_holdings_tx`（腾讯）给出每只 ETF 的**实际持仓 + 市值占比%**，
备源 `fund_etf_pcf`（每日申购赎回清单）。
⚠️ **W 是「当前快照」**：akshare 的这两个接口都取不到历史 PCF（实测 `fund_etf_pcf`
只接受 `symbol`，无日期参数）⇒ 回测里 ETF 成分股的**历史变更**不可知 ⇒
**回测含成分股变更带来的前视偏差**（指数成分调整约半年一次，影响有限但**必须披露**）。

【E = W × S 的正确做法】
1. 把当日期的个股分数 S 在**全市场截面**上做 z-score（跨 ETF 可比）；
2. 每只 ETF 只用**在 S 里有分数的成分股**加权（缺失的成分股剔除并**重新归一**）；
3. 记录**覆盖率** = 有分数的成分股权重 / 总权重；覆盖率过低 ⇒ 该 ETF 的 E 不可信。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402

#: akshare 的「市场代码」→ qlib 的交易所前缀
_MARKET_TO_PREFIX = {"101": "SH", "102": "SZ", "103": "BJ"}


def to_qlib_instrument(code: str, market: str | None = None) -> str | None:
    """把 akshare 的股票代码转成 qlib 的 instrument 形式（``SH600000``）。

    支持三种输入：
      - ``"600000"`` + ``market="101"`` → ``SH600000``
      - ``"600000.SH"``（后缀式）→ ``SH600000``
      - ``"SH600000"``（已是 qlib 形式）→ 原样返回
    """
    if code is None:
        return None
    code = str(code).strip().upper()
    if "." in code:
        head, _, tail = code.partition(".")
        if head.isalpha():          # 已是 qlib 形式
            return code
        if tail.isalpha():          # 600000.SH
            if tail == "BJ":
                return f"BJ{head}"
            return f"{tail}{head}"
        code = head
    code = code.zfill(6)
    if not code.isdigit():
        return None
    if market is not None:
        prefix = _MARKET_TO_PREFIX.get(str(market).strip())
        if prefix:
            return f"{prefix}{code}"
    # 兜底按代码首位推断（与 Agent A 的落库约定一致）
    if code[0] in ("6", "5", "9") and code.startswith(("60", "68", "51", "52", "56", "58")):
        return f"SH{code}"
    if code[0] in ("0", "3"):
        return f"SZ{code}"
    if code[0] in ("4", "8"):
        return f"BJ{code}"
    return None


def load_holdings(path: Path | str = None) -> pd.DataFrame:
    """读 ETF 持仓（= W 的原料），列：etf_code, etf_name, stock_code, stock_name, weight_pct。"""
    path = Path(path or C.ETF_HOLDINGS_CSV)
    df = pd.read_csv(path, dtype={"etf_code": str, "stock_code": str})
    df["etf_code"] = df["etf_code"].astype(str).str.zfill(6)
    df["stock_code"] = df["stock_code"].astype(str).str.strip()
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce")
    # 统一成 qlib instrument
    df["instrument"] = df["stock_code"].map(
        lambda c: to_qlib_instrument(c) if "." in str(c) else None
    )
    missing = df["instrument"].isna()
    if missing.any():
        df.loc[missing, "instrument"] = df.loc[missing, "stock_code"].map(to_qlib_instrument)
    return df


def normalize_scores(scores: pd.DataFrame, method: str = C.STOCK_SCORE_NORMALIZE) -> pd.Series:
    """个股分数 → 截面 z-score（每个调仓日分别做）。"""
    s = scores.copy()
    s["z"] = np.nan
    for _, grp in s.groupby("date", sort=True):
        mu = grp["score"].mean()
        sd = grp["score"].std(ddof=0)
        s.loc[grp.index, "z"] = 0.0 if (not np.isfinite(sd) or sd == 0) else (grp["score"] - mu) / sd
    return s["z"]


def map_scores_to_etfs(
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    date: pd.Timestamp,
    min_coverage: float = 0.5,
) -> pd.DataFrame:
    """算出某一天的 ETF 映射分 ``E``。

    参数
    ----
    holdings : W 的原料（load_holdings 的输出）
    scores   : 个股分数，列 date, instrument, score（**已按 date 过滤为单日**）
    date     : 调仓日
    min_coverage : 覆盖率下限；低于此值的 ETF 标 ``coverage_ok=False``（不参与选股）

    返回：etf_code, etf_name, alpha_score(=E), coverage, n_stocks_scored, weight_scored
    """
    date = pd.Timestamp(date)
    sc = scores[pd.to_datetime(scores["date"]) == date].copy()
    if sc.empty:
        raise ValueError(f"{date.date()} 没有个股分数")
    sc["z"] = normalize_scores(sc)

    h = holdings.copy()
    merged = h.merge(
        sc[["instrument", "z"]], left_on="instrument", right_on="instrument", how="inner"
    )
    grouped = merged.groupby(["etf_code", "etf_name"], sort=True)
    out = grouped.apply(
        lambda g: pd.Series(
            {
                "alpha_score": float(np.average(g["z"], weights=g["weight_pct"])),
                "weight_scored": float(g["weight_pct"].sum()),
                "n_stocks_scored": int(len(g)),
            }
        ),
        include_groups=False,
    ).reset_index()
    total = h.groupby(["etf_code", "etf_name"], sort=True)["weight_pct"].sum().reset_index()
    total.columns = ["etf_code", "etf_name", "weight_total"]
    out = out.merge(total, on=["etf_code", "etf_name"], how="left")
    out["coverage"] = out["weight_scored"] / out["weight_total"].replace(0, np.nan)
    out["coverage_ok"] = out["coverage"] >= min_coverage
    return out
