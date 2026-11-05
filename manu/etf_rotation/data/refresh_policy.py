#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""刷新策略（**纯函数**）—— 把「刷到哪天 / 哪些 code 还要抓 / 快照是否过期」的口径从抓取脚本里抽出来。

【为什么要有这个模块（2026-11-05 05:3x 实测）】
三个抓取脚本各自内嵌了一套「什么时候算抓完了」的判定，口径都是**按文件存在性**判的：

* `fetch_etf_prices.py --resume`：**代码在文件里就跳过**
  ⇒ 到下一个调仓节点（12-01）再跑时**全部跳过** ⇒ **0 新行**
  ⇒ 刷新变成**静默空转** ⇒ 新鲜度预检照样 `die`（用户完全看不出为什么）；
* 同一脚本的默认区间 `DEFAULT_END="20261104"` **硬编码**
  ⇒ 不传 `--end` 时永远抓不到新日期（换个日子跑也是同一批旧数据）；
* `fetch_etf_universe.py::stage_spot`：**快照文件存在就跳过**
  ⇒ 流动性 / 规模口径永远停在旧快照；
* `fetch_etf_holdings.py --resume`：同「按存在性跳过」⇒ W 成分快照永不更新。

⇒ 口径统一为「**按数据实际到达的日期判定**」：
  * **结束日**：显式 `--end` 优先；留空 ⇒ **今天之前（含）的最近一个交易日**
    （读交易日历；读不到 ⇒ 退回今天）⇒ **绝不钉死某一天**；
  * **跳过（行情）**：只跳过「**最新日已达目标结束日**」的 code；
  * **跳过（成分）**：只跳过「**fetch_date 已是今天**」的 code；
  * **快照**：按快照里的**数据日期**判定新鲜度，**不按文件是否存在**判定。

【为什么抽成独立模块】
口径一旦写在四个脚本里就会**各自漂移**；抽成纯函数后，
探针能在 **ql env**（**没装 akshare**）上直接断言，还能构造**真负例**
证明这些判定**真的会拦人**（§十四.36：断言写错 ⇒ 假阳性；负例不生效 ⇒ 假阴性）。

【纪律】
* 本模块**只做纯计算**：不联网、不写文件、不读 akshare（输入一律由调用方传入）；
* 每个函数都要**中文 docstring**；4 空格；snake_case；
* 读不到必需输入时**抛异常或返回明确的 None**，**不许**编数字（§十四.5）。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

DEFAULT_START = "20200101"
#: 交易日历文件（合并了 qlib 历史日历与 akshare 完整日历）；找不到时退回「今天」。
DEFAULT_CALENDAR = "trade_calendar.csv"


# ---------------------------------------------------------------- 基础
def today_compact(today: date | None = None) -> str:
    """返回今天的 ``YYYYMMDD``；可注入 ``today`` 便于测试。"""
    return (today or date.today()).strftime("%Y%m%d")


def to_timestamp(value: object) -> pd.Timestamp | None:
    """把各种日期表示统一成 ``pd.Timestamp``（去掉时分秒）；解析不出来返回 ``None``。"""
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return None
    return ts.normalize()


def load_trading_dates(calendar_path: Path | str | None) -> list[str]:
    """读交易日历，返回升序的 ``YYYYMMDD`` 列表；文件不存在 / 读不出 ⇒ 返回空列表（不编数字）。"""
    if not calendar_path:
        return []
    path = Path(calendar_path)
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty:
        return []
    col = "date" if "date" in df.columns else df.columns[0]
    vals = pd.to_datetime(df[col], errors="coerce").dropna()
    if vals.empty:
        return []
    return sorted({v.strftime("%Y%m%d") for v in vals})


# ---------------------------------------------------------------- 结束日 / 起始日
def resolve_end_date(
    explicit: str | None,
    today: date | None = None,
    calendar_dates: list[str] | None = None,
) -> str:
    """解析抓取结束日（``YYYYMMDD``）。

    * 显式传了 ``--end`` ⇒ 原样返回（允许补历史区间）；
    * 没传 ⇒ 取「**今天之前（含）的最近一个交易日**」；交易日历读不到 ⇒ 退回今天。

    🔴 **绝不钉死某一天**：钉死会让刷新在调仓节点当天变成**静默空转**（0 新行），
    而流水线的新鲜度预检仍会 `die` ⇒ 用户看不到任何有效线索。
    """
    if explicit:
        return str(explicit).strip()
    now = today or date.today()
    if calendar_dates:
        candidates = [d for d in calendar_dates if d <= now.strftime("%Y%m%d")]
        if candidates:
            return candidates[-1]
    return now.strftime("%Y%m%d")


def resolve_start_date(explicit: str | None, default: str = DEFAULT_START) -> str:
    """解析抓取起始日（``YYYYMMDD``）；没传 ⇒ 用默认（20200101，保证 MA120 / mom120 有历史）。"""
    if explicit:
        return str(explicit).strip()
    return default


def end_is_behind_today(end_compact: str, today: date | None = None) -> bool:
    """结束日是否早于今天（= 本轮是**补历史**而不是**追最新**）。"""
    end = to_timestamp(end_compact)
    now = to_timestamp(today or date.today())
    if end is None or now is None:
        return False
    return end < now


# ---------------------------------------------------------------- 行情：按「最新日」判定
def latest_date_by_code(prices_df: pd.DataFrame | None) -> dict[str, str]:
    """返回 ``{code: 最新日 'YYYY-MM-DD'}``；输入为空 / 缺列 ⇒ 返回空字典（不编数字）。"""
    if prices_df is None or getattr(prices_df, "empty", True):
        return {}
    if "code" not in prices_df.columns or "date" not in prices_df.columns:
        return {}
    df = prices_df.copy()
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if df.empty:
        return {}
    latest = df.groupby("code")["date"].max()
    return {str(code): ts.strftime("%Y-%m-%d") for code, ts in latest.items()}


def codes_needing_price_refresh(
    prices_df: pd.DataFrame | None,
    codes: list[str],
    end_compact: str,
) -> tuple[list[str], list[str]]:
    """按「**该 code 的最新日是否已达目标结束日**」判定要抓哪些。

    返回 ``(要抓, 可跳过)``。

    🔴 **不能用「文件里有没有这个 code」判定**：那样 ``--resume`` 会把**全部**已有 code 跳过
    ⇒ 刷新变成**静默空转**（0 新行）⇒ 新鲜度预检仍然 `die`，且用户看不出原因。
    """
    latest = latest_date_by_code(prices_df)
    end = to_timestamp(end_compact)
    need: list[str] = []
    skip: list[str] = []
    for raw in codes:
        code = str(raw).strip().zfill(6)
        if not code:
            continue
        last = latest.get(code)
        if last is None:
            need.append(code)
            continue
        last_ts = to_timestamp(last)
        if end is None or last_ts is None or last_ts < end:
            need.append(code)
        else:
            skip.append(code)
    return need, skip


def history_span_by_code(prices_df: pd.DataFrame | None) -> dict[str, tuple[str, str, int]]:
    """返回 ``{code: (首日, 末日, 自然日跨度)}``；用于查「刷新把历史截断」这类静默损坏。"""
    if prices_df is None or getattr(prices_df, "empty", True):
        return {}
    if "code" not in prices_df.columns or "date" not in prices_df.columns:
        return {}
    df = prices_df.copy()
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    if df.empty:
        return {}
    out: dict[str, tuple[str, str, int]] = {}
    for code, g in df.groupby("code"):
        first = g["date"].min()
        last = g["date"].max()
        out[str(code)] = (first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"), int((last - first).days))
    return out


# ---------------------------------------------------------------- 成分：按「fetch_date」判定
def codes_needing_holdings_refresh(
    holdings_df: pd.DataFrame | None,
    codes: list[str],
    today_compact: str,
) -> tuple[list[str], list[str]]:
    """按「**该 code 的成分快照 fetch_date 是否已是今天**」判定要抓哪些。

    返回 ``(要抓, 可跳过)``。同样是**按数据到达日**判定，而不是按「文件里有没有这个 code」。
    """
    have: dict[str, str] = {}
    if holdings_df is not None and getattr(holdings_df, "empty", True) is False:
        if "etf_code" in holdings_df.columns and "fetch_date" in holdings_df.columns:
            df = holdings_df.copy()
            df["etf_code"] = df["etf_code"].astype(str).str.strip().str.zfill(6)
            df["fetch_date"] = pd.to_datetime(df["fetch_date"], errors="coerce")
            df = df.dropna(subset=["fetch_date"])
            if not df.empty:
                latest = df.groupby("etf_code")["fetch_date"].max()
                have = {str(code): ts.strftime("%Y-%m-%d") for code, ts in latest.items()}
    today = to_timestamp(today_compact)
    need: list[str] = []
    skip: list[str] = []
    for raw in codes:
        code = str(raw).strip().zfill(6)
        if not code:
            continue
        last = have.get(code)
        if last is None:
            need.append(code)
            continue
        last_ts = to_timestamp(last)
        if today is None or last_ts is None or last_ts < today:
            need.append(code)
        else:
            skip.append(code)
    return need, skip


def holdings_snapshot_dates(holdings_df: pd.DataFrame | None) -> dict[str, object]:
    """成分快照的日期口径：``{"min": ..., "max": ..., "n_codes": ..., "n_stale": ...}``。

    🔴 **必须同时给 min 和 max**：刷新后可能出现「**混合快照日期**」
    （部分 code 抓成功、部分失败保留旧行）⇒ 只看 max 会**低估**滞后天数
    ⇒ 披露偏乐观（这正是 M-R 守卫的遗留缺口）。
    """
    empty = {"min": None, "max": None, "n_codes": 0, "n_stale": 0, "mixed": False, "stale_codes": []}
    if holdings_df is None or getattr(holdings_df, "empty", True):
        return empty
    if "fetch_date" not in holdings_df.columns:
        return empty
    df = holdings_df.copy()
    df["fetch_date"] = pd.to_datetime(df["fetch_date"], errors="coerce")
    df = df.dropna(subset=["fetch_date"])
    if df.empty:
        return empty
    code_col = "etf_code" if "etf_code" in df.columns else None
    latest = df.groupby(code_col)["fetch_date"].max() if code_col else pd.Series(dtype="datetime64[ns]")
    if latest.empty:
        return empty
    mn = latest.min()
    mx = latest.max()
    stale = latest[latest < mx]
    return {
        "min": mn.strftime("%Y-%m-%d"),
        "max": mx.strftime("%Y-%m-%d"),
        "n_codes": int(len(latest)),
        "n_stale": int(len(stale)),
        "mixed": bool(len(stale) > 0),
        "stale_codes": [str(c) for c in stale.index.tolist()[:20]],
    }


# ---------------------------------------------------------------- 现货快照
def spot_snapshot_is_stale(spot_df: pd.DataFrame | None, today_compact: str) -> tuple[bool, str]:
    """判断 ETF 现货快照是否**过期**（= 需要重新拉）。

    口径：按快照里的**数据日期 / 更新时间**判定，**不按文件是否存在**判定。
    返回 ``(是否过期, 人话说明)``。
    """
    if spot_df is None or getattr(spot_df, "empty", True):
        return True, "快照缺失 ⇒ 需要重新拉"
    today = to_timestamp(today_compact)
    for col in ("数据日期", "更新时间"):
        if col not in spot_df.columns:
            continue
        vals = pd.to_datetime(spot_df[col], errors="coerce").dropna()
        if vals.empty:
            continue
        latest = vals.max().normalize()
        if today is None:
            return True, "无法比较日期 ⇒ 按过期处理"
        if latest < today:
            return True, f"快照数据日期 {latest.date()} 早于今天 {today.date()} ⇒ 需重拉"
        return False, f"快照数据日期 {latest.date()} ≥ 今天 {today.date()} ⇒ 已是最新"
    return True, "快照里没有数据日期 / 更新时间列 ⇒ 按过期处理"


# ---------------------------------------------------------------- 输入侧完整性体检
#: 池内标的的**自然日**历史跨度下限（≈ 120 个交易日 + 缓冲）⇒ 用于查「刷新把历史截断」
MIN_POOL_HISTORY_DAYS = 250


def check_dataset_integrity(
    prices_df: pd.DataFrame | None,
    universe_df: pd.DataFrame | None,
    holdings_df: pd.DataFrame | None,
    target_date: str | None = None,
    min_pool_history_days: int = MIN_POOL_HISTORY_DAYS,
) -> list[dict]:
    """输入侧完整性体检；返回**问题列表**（空列表 = 全部达标）。

    🔴 **口径分清：硬拦 vs 只披露**（同 §十四.77）

    | 编号 | 判据 | 类别 | 为什么这么定 |
    |:--|:--|:--|:--|
    | I1 | 池内标的（``in_universe=True``）**没有行情** | **硬拦** | 池里没有行情 ⇒ E / 动量算不出 ⇒ 会被当「不可评估」悄悄淘汰 ⇒ 数据缺口伪装成信号 |
    | I2 | 行情 ``(date, code)`` **有重复行** | **硬拦** | 重复行会让均线 / 动量算错，且不会报错 |
    | I3 | 池内标的**历史跨度中位数** < ``min_pool_history_days`` | **硬拦** | 刷新时若用了短窗口，历史会被截断 ⇒ MA120 / mom120 直接失效（静默降级） |
    | I3b | 单只池内标的跨度不足（逐只） | 只披露 | 新上市 ETF 天然短 ⇒ 不当错误，但要列出来 |
    | I4 | 池内标的**没有成分记录** | 只披露 | 货币 / 黄金 / 海外 ETF 本就无 A 股成分 ⇒ 正常；但 A 股权益型无成分是**数据缺口** ⇒ 要列出来 |
    | I5 | W 快照**混合日期**（部分 code 陈旧） | 只披露 | 刷新部分失败 ⇒ 滞后只看不看 max 会低估 |

    参数
    ----
    prices_df / universe_df / holdings_df
        三张输入表（**由调用方读取**，本函数不碰文件）。
    target_date
        目标交易日（``YYYY-MM-DD`` 或 ``YYYYMMDD``）；给了就一并检查「最新日是否到达」。
    """
    problems: list[dict] = []

    # ---- I2 行情重复行 ----
    if prices_df is not None and getattr(prices_df, "empty", True) is False:
        dup_mask = prices_df.duplicated(subset=["date", "code"])
        n_dup = int(dup_mask.sum())
        if n_dup:
            problems.append({
                "id": "I2",
                "level": "block",
                "detail": f"行情存在 {n_dup} 行重复 (date, code) ⇒ 均线 / 动量会被算错",
            })

    # ---- 池内集合 ----
    pool: set[str] = set()
    if universe_df is not None and getattr(universe_df, "empty", True) is False:
        if "code" in universe_df.columns:
            u = universe_df.copy()
            u["code"] = u["code"].astype(str).str.strip().str.zfill(6)
            if "in_universe" in u.columns:
                mask = u["in_universe"].astype(str).str.lower().isin(["true", "1"])
                pool = set(u.loc[mask, "code"])
            elif "fetch_candidate" in u.columns:
                mask = u["fetch_candidate"].astype(str).str.lower().isin(["true", "1"])
                pool = set(u.loc[mask, "code"])

    spans = history_span_by_code(prices_df)
    have = set(spans)

    # ---- I1 池内缺行情 ----
    missing = sorted(pool - have)
    if missing:
        problems.append({
            "id": "I1",
            "level": "block",
            "detail": f"池内 {len(missing)} 只标的**没有行情**（数据缺口 ⇒ 会被当「不可评估」淘汰）："
                      + ",".join(missing[:20]),
            "codes": missing[:50],
        })

    # ---- I3 / I3b 历史跨度 ----
    if pool and spans:
        pool_spans = [(c, spans[c][2]) for c in sorted(pool) if c in spans]
        if pool_spans:
            values = sorted(v for _, v in pool_spans)
            median_span = values[len(values) // 2]
            short = [(c, v) for c, v in pool_spans if v < min_pool_history_days]
            if median_span < min_pool_history_days:
                problems.append({
                    "id": "I3",
                    "level": "block",
                    "detail": f"池内历史跨度**中位数 {median_span} 天** < {min_pool_history_days} 天"
                              f" ⇒ 行情历史疑似被短窗口刷新截断 ⇒ MA120 / mom120 会失效",
                })
            if short:
                problems.append({
                    "id": "I3b",
                    "level": "disclose",
                    "detail": f"{len(short)} 只池内标的历史跨度不足 {min_pool_history_days} 天"
                              f"（新股属正常；若是刷新截断则要查抓取窗口）",
                    "codes": [f"{c}({v}天)" for c, v in short[:20]],
                })

    # ---- I4 池内无成分记录 ----
    have_w: set[str] = set()
    if holdings_df is not None and getattr(holdings_df, "empty", True) is False:
        if "etf_code" in holdings_df.columns:
            have_w = set(holdings_df["etf_code"].astype(str).str.strip().str.zfill(6))
    no_w = sorted(pool - have_w)
    if no_w:
        bucket = {}
        if universe_df is not None and "fund_type_bucket" in universe_df.columns:
            u2 = universe_df.copy()
            u2["code"] = u2["code"].astype(str).str.strip().str.zfill(6)
            bucket = dict(zip(u2["code"], u2["fund_type_bucket"].astype(str)))
        equity_missing = [c for c in no_w if bucket.get(c) == "equity_cn"]
        problems.append({
            "id": "I4",
            "level": "disclose",
            "detail": f"池内 {len(no_w)} 只没有成分记录 ⇒ E 算不出"
                      + (f"；其中 **A 股权益型 {len(equity_missing)} 只**属**数据缺口**（非「无 A 股成分」）"
                         if equity_missing else "（均非 A 股权益型 ⇒ 正常：货币 / 商品 / 海外）"),
            "codes": no_w[:50],
            "equity_cn_missing": equity_missing[:50],
        })

    # ---- I5 W 快照混合日期 ----
    w_stats = holdings_snapshot_dates(holdings_df)
    if w_stats["mixed"]:
        problems.append({
            "id": "I5",
            "level": "disclose",
            "detail": f"W 快照日期**不一致**：最早 {w_stats['min']} / 最晚 {w_stats['max']}，"
                      f"陈旧 {w_stats['n_stale']} 只 ⇒ 只看 max 会**低估**滞后天数",
            "codes": w_stats["stale_codes"],
        })

    # ---- 目标日覆盖 ----
    if target_date and prices_df is not None and getattr(prices_df, "empty", True) is False:
        target = to_timestamp(target_date)
        latest = to_timestamp(max(spans[c][1] for c in spans)) if spans else None
        if target is not None and latest is not None and latest < target:
            problems.append({
                "id": "I4b",
                "level": "block",
                "detail": f"行情最新日 {latest.date()} 早于目标日 {target.date()}"
                          f" ⇒ 特征层会拿旧收盘价冒充目标日特征",
            })
    return problems
