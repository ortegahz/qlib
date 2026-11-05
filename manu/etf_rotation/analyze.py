"""ETF 分析报告生成器（TradingAgents 多智能体辩论的**输入材料**）。

【定位】
`recommend.py` 产出「调仓清单」（给人工下单）；本文件产出「**分析报告**」
（给 TradingAgents 多智能体辩论 / 人读）：把当期的**证据**一次性摊开 ——
个股分数 S 的分布与退化检查、S 对前瞻收益的 RankIC、ETF 暴露分 E 的覆盖情况、
三道过滤网**逐道**的淘汰名单、候选清单逐只拆解、现有持仓逐只诊断、
下一个调仓节点、风险披露。

【口径与铁律（rules.md）】
- **只做「读产物 → 统计 → 出报告」**，不碰任何 qlib 建模环节（铁律六：自研代码在 `manu/`）。
- **本地优先**：本文件只在本地工程里改；同步到服务器由主 agent 负责（铁律〇）。
- **无未来函数**：所有统计只用 `asof`（含）之前可得的数据（铁律十二·12.2）。
- **退化守卫**：个股分数塌成常数 ⇒ 直接抛错，**宁可不产出，也绝不产出假结果**（铁律十四·5）。
- **不静默失败**：必需输入（scores / prices / universe / holdings / 交易日历）读不到
  ⇒ 抛中文可读异常，绝不 `fillna(0)` 蒙混过关。
- **可复现**：固定 `PYTHONHASHSEED=0`；所有输出顺序显式排序，不用无序集合遍历决定顺序。

【与 engine/ 的关系（重要）】
决策链的**所有者**是 `engine/portfolio.build_portfolio`。本文件为了给出
「逐道淘汰名单 / 候选拆解 / 持仓诊断」这类**可解释性**明细，会在分析侧重放一遍过滤链，
但每一步都调用 **engine 里同一批函数**（不重写口径），并在最后**断言**
分析侧的入选名单与 `build_portfolio` 的完全一致 —— 不一致就抛错，
绝不输出与引擎决策对不上的报告。

【用法（服务器）】
    export PYTHONHASHSEED=0
    python manu/etf_rotation/analyze.py --asof 2026-11-03 \
        --out-dir /mnt/data/siping/runs/etf_rotation \
        --account-snapshot /mnt/data/siping/runs/etf_rotation/account_snapshot.csv

【输出】
- `<out-dir>/etf_analysis_report.json`：结构化（下游机器读）
- `<out-dir>/etf_analysis_report.md`：中文报告（TradingAgents / 人读）

【免责】本报告是模型打分与规则筛选的**输出**，**不构成投资建议**。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "data"))
import config as C  # noqa: E402
import signal_policy as SP  # noqa: E402
import microstructure as MS  # noqa: E402
import lookahead as LA  # noqa: E402
from engine.features import (  # noqa: E402
    add_relative_strength,
    compute_etf_features,
    load_prices,
    momentum_score,
)
from engine.filters import (  # noqa: E402
    apply_liquidity_filter,
    apply_theme_cap,
    build_theme_and_conflicts,
)
from engine.portfolio import (  # noqa: E402
    _is_defensive,
    _ma_ok_map,
    _z,
    build_portfolio,
    load_universe,
)
from engine.weights import load_holdings, map_scores_to_etfs  # noqa: E402

#: 账户快照契约由 `manu/etf_rotation/account.py` 提供。
#: 🔴 2026-11-04 夜实测：旧代码在这里 `except Exception: ACC = None` ⇒
#:    导入一失败，`total_assets` 变 0 ⇒ 回退成「持仓市值当总资产」（14,335.80）
#:    且 cash=0 ⇒ 所有权重变成 100% 口径，**全程不报错**。
#:    ⇒ account.py 是本仓必需模块，导入失败必须直接抛（铁律 §12.4 守卫必须能被证伪）。
try:
    import account as ACC  # type: ignore
except Exception as _acc_exc:
    raise RuntimeError(f"account 模块导入失败（{_acc_exc!r}）⇒ 账户口径不可用，拒绝出报告") from _acc_exc

# ---------------------------------------------------------------- 本文件自有常量
#: 报告产物文件名（**不写进 config.py**：该文件由其他 agent 并行维护）
REPORT_JSON_NAME = "etf_analysis_report.json"
REPORT_MD_NAME = "etf_analysis_report.md"

#: RankIC 的前瞻窗口（**交易日**）。config.py 由其他 agent 并行维护 ⇒ 常量就地定义并注明。
IC_HORIZONS: tuple[int, ...] = (5, 20)
#: 标签对齐的前瞻窗口：`stock_scores.csv` 的 `fwd_ret` 就是 **1 日**前瞻收益
#: （表达式 `Ref($vwap,-2)/Ref($vwap,-1)-1`，见 `qlib_scores.fetch_fwd_returns`）。
IC_LABEL_HORIZON = 1
#: RankIC 最少需要的截面数（少于它 ⇒ 输出 null +「样本不足」，**不许编数字**）
MIN_IC_CROSS_SECTIONS = 5
#: 个股分数「唯一值过少」的告警阈值（与 `qlib_scores.assert_non_degenerate` 同口径）
UNIQUE_VALUE_WARN_THRESHOLD = 10
#: 账户模块缺失时的兜底快照路径
DEFAULT_ACCOUNT_SNAPSHOT_CSV = Path(__file__).resolve().parent / "data" / "account_snapshot.csv"
#: 回测报告文件名（只读引用，用于风险披露里引用**实测**回撤数字；不存在就不引用）
BACKTEST_GLOB = "etf_rotation_backtest*.json"


# ---------------------------------------------------------------- 通用小工具
def _now_iso() -> str:
    """本地时区的当前时间（秒级 ISO8601）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fingerprint(path: Path | str) -> dict:
    """输入文件指纹：路径 / 存在性 / 大小 / 修改时间（用于「用的是哪份数据」）。"""
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    st = p.stat()
    return {
        "path": str(p),
        "exists": True,
        "size_bytes": int(st.st_size),
        "mtime_local": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
    }


def _require_input(path: Path | str, what: str) -> Path:
    """必需输入不存在 ⇒ 抛中文可读异常（不静默失败）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"必需输入缺失：{what} —— 路径 `{p}` 不存在 ⇒ 无法生成分析报告"
            f"（不静默失败：绝不 fillna(0) 蒙混过关）"
        )
    return p


def _data_freshness_guard(
    prices: pd.DataFrame,
    asof: pd.Timestamp,
    prices_path: Path,
    holdings: pd.DataFrame | None = None,
    universe: pd.DataFrame | None = None,
    max_stale_listed: int = 20,
) -> dict:
    """🔴 **数据新鲜度守卫**（M-R，2026-11-05 05:2x 新增）。

    【为什么必须有这条守卫】
    特征层 `engine/features.compute_etf_features` 的口径是「按 `date <= asof` 截断后取最后一条」，
    ⇒ **行情数据够不到信号日时，它会拿更早的收盘价算出 MA/动量，却把结果标成「信号日」的特征**
    ⇒ **静默降级**（违反「宁可不产出，不产出假结果」§十四.5）。
    🔴 实测场景：下一调仓节点是 **2026-12-01**，而 `etf_prices.csv` 只到 **2026-11-04**
    （且一键流水线**从不刷新**它）⇒ 没有这条守卫就会产出一份「信号日 2026-12-01」的**假报告**。

    【口径（硬拦 / 只披露，分清）】
    * **硬拦**：ETF 行情的**市场级最新日**必须 **>= 信号日** ⇒ 否则抛中文异常（附刷新命令）。
    * **只披露**：**个别 ETF** 的最后一根 bar 早于信号日（停牌 / 新上市 / 退市）⇒ 正常现象
      ⇒ 计入 `n_etfs_stale` 并列出最陈旧的若干只，**不许**当成错误。
    * **只披露**：W 快照（`etf_holdings.csv` 的 `fetch_date`）**允许晚于**信号日
      （份额披露天然滞后）⇒ 只报滞后天数；真正的危害由前视审计量化（见 `lookahead_audit`）。
    """
    asof = pd.Timestamp(asof).normalize()
    px = prices.copy()
    px["date"] = pd.to_datetime(px["date"])
    px_max = px["date"].max()
    lag_days = int((asof - px_max).days)  # 正数 = 数据比信号日旧（危险）
    bars_at_asof = int((px["date"] == asof).sum())

    if lag_days > 0:
        raise ValueError(
            f"🔴 **ETF 行情数据够不到信号日**：`{prices_path}` 最新只到 {px_max.date()}，"
            f"而信号日是 {asof.date()}（数据落后 **{lag_days}** 个自然日）"
            f" ⇒ 拒绝出报告。\n"
            f"   原因：特征层按「`date <= asof` 截断后取最后一条」计算 MA/动量，"
            f"数据不够时它会拿**旧收盘价**当信号日特征 ⇒ 静默降级 ⇒ **假结果**。\n"
            f"   处置（二选一）：\n"
            f"     ① 刷新 ETF 行情（akshare 环境；**绝对不要往 ql env 装 akshare**）：\n"
            f"        /home/huangzhe/anaconda3/envs/ak/bin/python "
            f"manu/etf_rotation/data/fetch_etf_prices.py --resume\n"
            f"        （`--end` **留空** = 自动解析为「今天之前（含）的最近一个交易日」；"
            f"**不要钉死日期**）\n"
            f"     ② 把信号日改成行情已覆盖的日期（≤ {px_max.date()}）。"
        )

    if bars_at_asof == 0:
        raise ValueError(
            f"🔴 **ETF 行情在信号日当天没有任何 bar**：`{prices_path}` 覆盖到 {px_max.date()}，"
            f"但 **{asof.date()} 当天一只 ETF 的 bar 都没有**"
            f" ⇒ 特征层会退回用**更早的收盘价**算 MA/动量，却标成信号日 ⇒ **静默降级** ⇒ 拒绝出报告。\n"
            f"   处置：先补齐 {asof.date()} 当天的 ETF 日线（fetch_etf_prices.py），"
            f"或把信号日改成行情**当天确有 bar** 的日期。"
        )

    # ---- 个别 ETF 的陈旧度（停牌/新上市/退市 ⇒ 正常，只披露） ----
    last_by_code = px.groupby(px["code"].astype(str).str.zfill(6))["date"].max()
    stale = last_by_code[last_by_code < asof].sort_values()
    stale_rows = [
        {
            "code": str(code),
            "last_date": pd.Timestamp(dt).date().isoformat(),
            "lag_calendar_days": int((asof - pd.Timestamp(dt)).days),
        }
        for code, dt in stale.head(max_stale_listed).items()
    ]

    def _fetch_date_range(df: pd.DataFrame | None, code_col: str | None = None) -> dict:
        """取成分/清单表的抓取日期口径：**最早 / 最晚 / 陈旧只数 / 是否混合**。

        🔴 **必须同时给 min 和 max**（M-S1 修）：刷新后可能出现「**混合快照日期**」
        （部分 code 抓成功、部分失败仍保留旧行）⇒ 只看 max 会把滞后天数**低估**
        （最旧的一只可能陈旧 27 天，表面却显示 0 天）⇒ 披露偏乐观。
        """
        empty = {"min": None, "max": None, "n_codes": 0, "n_stale": 0, "mixed": False, "stale_codes": []}
        if df is None or df.empty or "fetch_date" not in df.columns:
            return empty
        vals = pd.to_datetime(df["fetch_date"], errors="coerce").dropna()
        if vals.empty:
            return empty
        if code_col and code_col in df.columns:
            keys = df[code_col].astype(str).str.strip().str.zfill(6)
            per_code = pd.DataFrame({"code": keys, "d": vals}).groupby("code")["d"].max()
        else:
            per_code = pd.Series([vals.max()])
        mn = per_code.min()
        mx = per_code.max()
        stale = per_code[per_code < mx]
        return {
            "min": mn.date().isoformat(),
            "max": mx.date().isoformat(),
            "n_codes": int(len(per_code)),
            "n_stale": int(len(stale)),
            "mixed": bool(len(stale) > 0),
            "stale_codes": [str(c) for c in stale.index.tolist()[:20]],
        }

    w_range = _fetch_date_range(holdings, "etf_code")
    w_date = w_range["max"]
    u_range = _fetch_date_range(universe, "code")
    u_date = u_range["max"]
    return {
        "signal_date": asof.date().isoformat(),
        "rule": "硬拦①ETF 行情最新日 >= 信号日；硬拦②信号日当天至少有一只 ETF 的 bar；否则拒绝出报告（不产出假结果）。只披露：个别 ETF 陈旧数、W 快照滞后天数",
        "etf_prices_max_date": px_max.date().isoformat(),
        "etf_prices_covers_signal_date": True,
        "n_bars_at_signal_date": bars_at_asof,
        "etf_prices_lag_calendar_days": lag_days,
        "coverage_at_signal_date_pct": (
            round(bars_at_asof / float(len(last_by_code)) * 100.0, 2)
            if len(last_by_code) else None
        ),
        "n_etfs_total": int(len(last_by_code)),
        "n_etfs_stale": int(len(stale)),
        "stale_etfs": stale_rows,
        "w_snapshot_date": w_date,
        "w_snapshot_date_min": w_range["min"],
        "w_snapshot_n_codes": w_range["n_codes"],
        "w_snapshot_n_stale_codes": w_range["n_stale"],
        "w_snapshot_mixed_dates": w_range["mixed"],
        "w_snapshot_stale_codes": w_range["stale_codes"],
        "w_snapshot_lag_calendar_days_max": (
            int((pd.Timestamp(w_date) - asof).days) if w_date else None
        ),
        "w_snapshot_lag_calendar_days_min": (
            int((pd.Timestamp(w_range["min"]) - asof).days) if w_range["min"] else None
        ),
        "w_snapshot_lag_calendar_days": (
            int((pd.Timestamp(w_date) - asof).days) if w_date else None
        ),
        "universe_fetch_date": u_date,
        "universe_lag_calendar_days": (
            int((pd.Timestamp(u_date) - asof).days) if u_date else None
        ),
    }

def _yi(value: object, digits: int = 2) -> str:
    """把「元」格式化成「亿元」（None / NaN ⇒ 「-」；便于人读数量级）。"""
    if value is None:
        return "-"
    try:
        value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(value):
        return "-"
    return f"{value / 1e8:.{digits}f}"


def _clean(obj: object) -> object:
    """把 numpy 标量 / NaN / Timestamp 递归转成可 JSON 序列化的原生类型。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if np.isfinite(value) else None
    if isinstance(obj, pd.Timestamp):
        return obj.date().isoformat()
    if obj is pd.NaT:
        return None
    return obj


def _num(value: object, digits: int = 4) -> str:
    """数字格式化（None / NaN ⇒ 「-」）。"""
    if value is None:
        return "-"
    try:
        value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(value):
        return "-"
    return f"{value:.{digits}f}"


def _pct(value: object, digits: int = 2) -> str:
    """百分比格式化（None / NaN ⇒ 「-」）。"""
    if value is None:
        return "-"
    try:
        value = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


def _ensure_hash_seed() -> None:
    """固定 ``PYTHONHASHSEED=0``（未设置时自重入一次；已设成别的值则告警）。"""
    current = os.environ.get("PYTHONHASHSEED")
    if current == "0":
        return
    if current is None:
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable] + sys.argv)
    print(f"[WARN] PYTHONHASHSEED={current!r}（期望 '0'）⇒ 本次运行的哈希序可能与历史不同，"
          f"请用 `export PYTHONHASHSEED=0` 重跑以保证可复现")


#: akshare 抓的**完整**交易日历（含未来交易日）。
#: 🔴 qlib 自带的 `calendars/day.txt` **只到最后一个有行情的交易日**（当前 2026-11-03），
#:    **不含未来** ⇒ 单靠它无法推导「下一个自然月的第一个交易日」。
#:    生成方式（用 `ak` env，🔴 akshare 绝不能装进 `ql` env）：
#:      /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_trade_calendar.py
DEFAULT_TRADE_CALENDAR_CSV = Path("/mnt/data/siping/datasets/etf/trade_calendar.csv")


def _default_calendar_path() -> Path:
    """qlib 交易日历路径（从**个股线 config** 的 PROVIDER_URI 推导 ⇒ 单一事实源）。"""
    base = getattr(C.STOCK, "PROVIDER_URI", None)
    if base:
        return Path(str(base)) / "calendars" / "day.txt"
    return Path(str(C.STOCK.DATASET_ROOT)) / "cn_data" / "calendars" / "day.txt"


# ---------------------------------------------------------------- 输入装载
def _load_scores(path: Path) -> pd.DataFrame:
    """读个股分数 S（列：date, instrument, score[, fwd_ret]）。"""
    df = pd.read_csv(path, dtype={"instrument": str})
    for col in ("date", "instrument", "score"):
        if col not in df.columns:
            raise ValueError(f"个股分数文件 `{path}` 缺少必需列 `{col}`（实际列：{list(df.columns)}）")
    df["date"] = pd.to_datetime(df["date"])
    df["instrument"] = df["instrument"].astype(str)
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    if "fwd_ret" in df.columns:
        df["fwd_ret"] = pd.to_numeric(df["fwd_ret"], errors="coerce")
    return df.sort_values(["date", "instrument"]).reset_index(drop=True)


def _scores_asof_missing_message(
    asof: pd.Timestamp,
    scores: pd.DataFrame,
    scores_path: Path,
) -> str:
    """「信号日在个股分数里没有记录」时的**可执行**错误信息（M-S2 新增）。

    🔴 为什么要增强（2026-11-05 06:2x 实测）：
    原信息只有一句「没有记录（可用日期：X ~ Y）」⇒ 用户在**下一个调仓节点日**
    （2026-12-01）看到它，仍然不知道**根因**与**怎么办**：
    * 根因 A：**qlib 数据集覆盖不到那一天**（个股行情在 qlib 里，`day.txt` 只到数据末日）
      ⇒ 要往前推必须**重建 qlib 数据集**（**重活，需用户当轮授权**）；
    * 根因 B：**打分日是相位栅格**，不是任意交易日
      （相位 = 每 N 个交易日取一个 + 末日例外）⇒ 该日可能**根本不会被打分**；
    ⇒ 处置二选一：改用**附近已有的打分日**，或先让数据集 / 栅格覆盖目标日。

    🔴 本函数**只改错误信息**，不碰报告 JSON ⇒ 报告内容指纹不变 ⇒ sha1 链不断。
    """
    target = pd.Timestamp(asof).normalize()
    dates = sorted({pd.Timestamp(d).normalize() for d in scores["date"].unique()})
    prev = [d for d in dates if d < target]
    nxt = [d for d in dates if d > target]
    near = ", ".join(
        [str(d.date()) for d in prev[-2:]] + [str(d.date()) for d in nxt[:2]]
    ) or "（无）"
    data_max = SP.calendar_max_date(_default_calendar_path())
    cal_note = (
        f"qlib 数据集覆盖末日 = **{data_max}**（个股行情只到这一天）"
        if data_max else "读不到 qlib 数据集覆盖末日（`calendars/day.txt`）"
    )
    reach = ""
    if data_max is not None and str(data_max) < str(target.date()):
        reach = (
            f"   ⇒ **根因 A：qlib 数据集覆盖不到 {target.date()}**"
            f"（差 {int((target - pd.Timestamp(data_max)).days)} 个自然日）"
            f" ⇒ 个股层(S)根本打不到这一天 ⇒ 要往前推必须**重建 qlib 数据集**"
            f"（**重活，需用户当轮授权**）。\n"
        )
    return (
        f"🔴 信号日 {target.date()} 在个股分数 `{scores_path}` 里没有记录\n"
        f"   （可用日期：{dates[0].date()} ~ {dates[-1].date()}，共 {len(dates)} 个打分日）\n"
        f"   {cal_note}\n"
        f"{reach}"
        f"   ⇒ **根因 B：打分日是相位栅格**（每 N 个交易日取一个 + 末日例外）"
        f" ⇒ 该日可能根本不会被打分。\n"
        f"   附近的打分日：**{near}**\n"
        f"   处置（二选一）：\n"
        f"     ① 把 --asof 改成上面的**附近打分日**（不需要重建任何东西）；\n"
        f"     ② 若必须用 {target.date()}，须先更新 qlib 数据集"
        f"（**重活，需用户当轮授权**）并确认该日落在打分栅格上。"
    )


def _load_account_snapshot(path: Path | None) -> tuple[pd.DataFrame, Path | None]:
    """读账户快照（列：code, name, shares, price, market_value, pnl, pnl_pct）。

    优先用 `account.load_account_snapshot`（账户口径的**所有者**）；模块缺失时退回本地读 CSV。
    """
    resolved = Path(path) if path else getattr(ACC, "ACCOUNT_SNAPSHOT_CSV", DEFAULT_ACCOUNT_SNAPSHOT_CSV)
    if not Path(resolved).exists():
        raise FileNotFoundError(
            f"账户快照缺失：`{resolved}` 不存在 ⇒ 无法诊断现有持仓"
            f"（可用 --account-snapshot 指定路径；账户模块缺失时默认路径为 {DEFAULT_ACCOUNT_SNAPSHOT_CSV}）"
        )
    loader = getattr(ACC, "load_account_snapshot", None)
    if callable(loader):
        try:
            df = loader(resolved)
        except Exception as exc:  # 账户模块自带对账守卫 ⇒ 错误必须原样透出
            raise ValueError(f"账户快照 `{resolved}` 读取/对账失败：{exc}") from exc
    else:
        df = pd.read_csv(resolved, dtype={"code": str})
    df = df.copy()
    if "code" not in df.columns:
        raise ValueError(f"账户快照 `{resolved}` 缺少必需列 `code`（实际列：{list(df.columns)}）")
    df["code"] = df["code"].astype(str).str.zfill(6)
    if "name" not in df.columns:
        df["name"] = ""
    for col in ("shares", "price", "market_value", "pnl", "pnl_pct", "cost"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan
    # 🔴 本文件**唯一允许的非数值 fillna**：**名称列**缺失补空串（纯展示用，不参与任何计算口径）
    #    （数值列的唯一 fillna 在「分数合成」段：穿透为 0 的 ETF 用池内均值补 ⇒ 那里另有说明）
    df["name"] = df["name"].fillna("").astype(str)
    return df.sort_values("code").reset_index(drop=True), Path(resolved)


def _read_calendar(path: Path) -> list[pd.Timestamp]:
    """读 qlib 交易日历（一行一个 `YYYY-MM-DD`）。

    文件不存在 ⇒ 抛中文异常（**不静默跳过**）；行格式非法 ⇒ 同样抛错。
    """
    if not path.exists():
        raise FileNotFoundError(
            f"qlib 交易日历缺失：`{path}` 不存在 ⇒ 无法推导下一个调仓节点"
            f"（可用 --calendar 指定路径；服务器上的路径是 "
            f"/mnt/data/siping/datasets/qlib/cn_data/calendars/day.txt）"
        )
    dates: list[pd.Timestamp] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            dates.append(pd.Timestamp(line))
        except Exception as exc:  # pragma: no cover - 防御
            raise ValueError(f"交易日历 `{path}` 第 {lineno} 行不是合法日期：{line!r}") from exc
    if not dates:
        raise ValueError(f"交易日历 `{path}` 是空文件 ⇒ 无法推导下一个调仓节点")
    return sorted(dates)


def _read_extra_calendar(path: Path) -> list[pd.Timestamp]:
    """读 akshare 的完整交易日历 CSV（列 `date`）。文件缺失 ⇒ 返回空列表（不是错误）。"""
    if not path or not Path(path).exists():
        return []
    df = pd.read_csv(path, dtype=str)
    col = "date" if "date" in df.columns else df.columns[0]
    out: list[pd.Timestamp] = []
    bad_dates = 0
    for v in df[col].dropna().astype(str):
        v = v.strip()[:10]
        if len(v) == 10 and v[4] == "-" and v[7] == "-":
            try:
                out.append(pd.Timestamp(v))
            except Exception:  # pragma: no cover - 防御
                bad_dates += 1
                continue
    if bad_dates:
        # 🔴 静默丢弃会让「下一个调仓节点」被算错 ⇒ 必须报出来
        print(f"[WARN] 附加交易日历里有 {bad_dates} 行日期无法解析并被丢弃 ⇒ 调仓节点推导可能偏")
    return sorted(set(out))


def _merge_calendars(primary: list[pd.Timestamp], extra: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """合并两份日历（qlib 历史权威 + akshare 覆盖未来），去重升序。"""
    return sorted(set(primary) | set(extra))


def _next_rebalance(cal: list[pd.Timestamp], asof: pd.Timestamp, every_n: int) -> dict:
    """下一个调仓节点 = **下一个自然月的第一个交易日**（并校验该日确实在日历里）。"""
    future = [d for d in cal if d > asof]
    if not future:
        raise ValueError(
            f"交易日历里没有 {asof.date()} 之后的交易日 ⇒ 无法推导下一个调仓节点"
            f"（日历可能未覆盖该日期）"
        )
    year, month = asof.year, asof.month + 1
    if month > 12:
        year, month = year + 1, 1
    month_days = [d for d in future if (d.year, d.month) == (year, month)]
    if not month_days:
        raise ValueError(
            f"交易日历里没有 {year}-{month:02d} 的交易日 ⇒ 无法推导下一个自然月的第一个交易日"
        )
    target = month_days[0]
    if target not in set(cal):  # 额外校验：目标日必须真的在日历里
        raise ValueError(f"推导出的调仓日 {target.date()} 不在交易日历中 ⇒ 内部一致性校验失败")
    n_days = sum(1 for d in future if d <= target)
    cycle_date = future[every_n - 1] if every_n > 0 and len(future) >= every_n else None
    # 🔴 M-J（2026-11-05 夜实测）：**信号日 ≠ 执行日**。
    #   config.EXECUTION_LAG_DAYS = 1 ⇒ 信号用 T 日收盘后数据、**执行在 T+1 日收盘**
    #   ⇒ 12-01 是**信号日**，真正下单日 = **12-02**。
    #   旧产物只给 target_date（且计划件把它写成「调仓节点」）⇒ 读者会以为 12-01 当天下单
    #   ⇒ 那等于拿 **11-03 的旧信号**、用 12-01 的价格成交 ⇒ 比回测口径早一天，
    #   **不在回测假设内**（§十四.7 持仓区间左开右闭也是按 exec 日算的）。
    #   ⇒ 这里显式把**执行日**算出来；日历覆盖不到时给 None + 说明，**不许编日期**。
    _lag = int(C.EXECUTION_LAG_DAYS)
    _after = [d for d in cal if d > target]
    _exec_date = _after[_lag - 1] if _lag >= 1 and len(_after) >= _lag else None
    _exec_note = (
        f"信号日 {target.date().isoformat()} 收盘出信号 ⇒ 执行日 "
        f"{_exec_date.date().isoformat()}（T+{_lag} 收盘执行）"
        if _exec_date is not None
        else (
            f"⚠️ 交易日历未覆盖信号日 {target.date().isoformat()} 之后的第 {_lag} 个交易日 "
            f"⇒ **执行日算不出来**（不许编日期）"
        )
    )
    return {
        "asof": asof.date().isoformat(),
        "rule": "下一个自然月的第一个交易日（qlib 历史日历 ∪ akshare 完整日历）",
        "target_date": target.date().isoformat(),
        "signal_date": target.date().isoformat(),
        "execution_date": _exec_date.date().isoformat() if _exec_date is not None else None,
        "execution_lag_days": _lag,
        "execution_rule": (
            f"T 日收盘出信号、T+{_lag} 收盘执行（config.EXECUTION_LAG_DAYS={_lag}）"
        ),
        "execution_note": _exec_note,
        "target_month": f"{year}-{month:02d}",
        "trading_days_until": int(n_days),
        "in_calendar_verified": True,
        "calendar_path": None,  # 由调用方补
        "calendar_last_date": cal[-1].date().isoformat(),
        "cycle_signal_date": cycle_date.date().isoformat() if cycle_date is not None else None,
        "cycle_rule": (
            f"每 {every_n} 个交易日一个信号日（= 部署节奏，config.DEPLOYED_REBALANCE_EVERY_N_DAYS；"
            f"信号日基础步长 {C.REBALANCE_EVERY_N_DAYS} 交易日 × 倍数 "
            f"{C.REBALANCE_STRIDE_MULTIPLIER}）"
        ),
        "cycle_matches_target": (
            cycle_date is not None and cycle_date.date().isoformat() == target.date().isoformat()
        ),
    }


# ---------------------------------------------------------------- §2 个股分数 S 的横截面统计
def _stock_score_stats(scores: pd.DataFrame, asof: pd.Timestamp) -> tuple[dict, list[str]]:
    """asof 当天的个股分数横截面统计 + **退化断言**。

    🔴 退化守卫（铁律十四·5）：分数塌成常数 ⇒ 直接抛错（宁可不产出，也不产出假结果）。
    """
    day = scores[scores["date"] == asof]
    if day.empty:
        raise ValueError(f"{asof.date()} 在个股分数里没有任何记录 ⇒ 无法出报告")
    values = day["score"].astype(float)
    finite = values[np.isfinite(values)]
    if finite.empty:
        raise ValueError(f"{asof.date()} 的个股分数全部非有限值 ⇒ 分数已退化，拒绝出报告")
    unique = int(finite.nunique())
    std = float(finite.std(ddof=0))
    is_constant = bool(std == 0.0 or unique <= 1)
    stats = {
        "date": asof.date().isoformat(),
        "n_stocks": int(len(finite)),
        "n_rows_raw": int(len(day)),
        "n_non_finite": int(len(values) - len(finite)),
        "mean": float(finite.mean()),
        "std": std,
        "median": float(finite.median()),
        "min": float(finite.min()),
        "p25": float(finite.quantile(0.25)),
        "p50": float(finite.quantile(0.50)),
        "p75": float(finite.quantile(0.75)),
        "max": float(finite.max()),
        "unique_values": unique,
        "is_constant": is_constant,
        "degenerate_check": {
            "passed": not is_constant,
            "rule": "唯一值数 > 1 且 标准差 > 0（否则分数退化为常数 ⇒ 拒绝出报告）",
            "unique_values": unique,
            "std": std,
            "warn_threshold_unique": UNIQUE_VALUE_WARN_THRESHOLD,
            # 🔴 2026-11-05 05:0x 实测修：warn_note 原先**无条件**拼这句，
            #    于是唯一值 5574（远大于阈值 10）时也会渲染成
            #    「唯一值数 5574 < 10 ⇒ 分数信息量偏低（WARN，不阻断）」
            #    —— **假断言**（交付件里写着 5574 < 10）。
            #    ⇒ 修法：按实际比较分支渲染，命中才写 WARN，没命中就明写「未触发」。
            "warn_note": (
                f"唯一值数 {unique} < {UNIQUE_VALUE_WARN_THRESHOLD} ⇒ 分数信息量偏低（WARN，不阻断）"
                if unique < UNIQUE_VALUE_WARN_THRESHOLD
                else f"唯一值数 {unique} ≥ {UNIQUE_VALUE_WARN_THRESHOLD} ⇒ 未触发低区分度 WARN"
            ),
            "warn_triggered": bool(unique < UNIQUE_VALUE_WARN_THRESHOLD),
        },
        "note": "只用 asof 当天横截面；不含任何 asof 之后的信息",
    }
    if is_constant:
        raise ValueError(
            f"个股分数在 {asof.date()} **退化为常数**（唯一值 {unique}、标准差 {std:.6g}）"
            f" ⇒ 拒绝生成分析报告（退化守卫：宁可不产出，也绝不产出假结果）"
        )
    warnings: list[str] = []
    if unique < UNIQUE_VALUE_WARN_THRESHOLD:
        warnings.append(
            f"[WARN] {asof.date()} 个股分数唯一值仅 {unique} 个（< {UNIQUE_VALUE_WARN_THRESHOLD}）"
            f" ⇒ 横截面区分度偏低，E=W×S 的暴露分可能不可靠"
        )
    return stats, warnings


# ---------------------------------------------------------------- §3 RankIC（评估指标）
def _aggregate_ic(per_date: list[dict]) -> dict:
    """把逐日 IC 汇总成 mean / std / ICIR / t（样本不足 ⇒ 全 null）。"""
    ics = [d["rank_ic"] for d in per_date if d.get("rank_ic") is not None]
    n = len(ics)
    if n < MIN_IC_CROSS_SECTIONS:
        return {
            "status": "insufficient_sample",
            "reason": f"样本不足：只有 {n} 个有效截面（< {MIN_IC_CROSS_SECTIONS}）⇒ 不输出 IC 数字",
            "mean_rank_ic": None,
            "std_rank_ic": None,
            "icir": None,
            "t_stat": None,
            "n_cross_sections": n,
            "per_date": per_date,
        }
    arr = np.asarray(ics, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    icir = float(mean / std) if std > 0 else None
    t_stat = float(mean / (std / np.sqrt(n))) if std > 0 else None
    return {
        "status": "ok",
        "reason": None,
        "mean_rank_ic": mean,
        "std_rank_ic": std,
        "icir": icir,
        "t_stat": t_stat,
        "n_cross_sections": n,
        "mean_n_stocks": float(np.mean([d["n"] for d in per_date if d.get("rank_ic") is not None])),
        "per_date": per_date,
    }


def _rank_ic(
    scores: pd.DataFrame,
    asof: pd.Timestamp,
    stock_prices: pd.DataFrame | None,
) -> dict:
    """个股分 S 对**未来 N 日收益**的 RankIC（只用 asof 及之前可得的信息）。

    口径
    ----
    - **N=1（标签对齐）**：用 `stock_scores.csv` 自带的 `fwd_ret`（= `Ref($vwap,-2)/Ref($vwap,-1)-1`，
      即标签用的 1 日前瞻收益）⇒ 逐日横截面 Spearman，再对时间序列汇总。
    - **N=5 / N=20（多日）**：必须有**个股日线**（`--stock-prices`，列 date/instrument/close），
      才能构造真实的 N 交易日前瞻收益；前瞻收益的**实现日必须 ≤ asof**（否则样本未落地，不入统计）。
      没有个股日线 ⇒ 输出 `null` + 明确原因，**不编数字**。

    🔴 本节是**评估指标**，**不参与选股决策**（铁律十二：评估区间不参与选参）。
    """
    day_scores = scores[scores["date"] <= asof]
    score_dates = sorted(pd.Timestamp(d) for d in day_scores["date"].unique())
    result: dict = {
        "evaluation_only": True,
        "note": "RankIC 是**评估指标**，不参与选股决策；所有前瞻收益只统计在 asof 前已实现的样本。",
        "asof": asof.date().isoformat(),
        "n_score_dates": len(score_dates),
        "score_date_range": (
            [score_dates[0].date().isoformat(), score_dates[-1].date().isoformat()]
            if score_dates
            else None
        ),
        "definition": {
            "rank_ic": "逐日横截面 Spearman 秩相关 corr(score_t, fwd_ret_t)，再对时间序列求均值",
            "icir": "mean(IC) / std(IC)（std 用 ddof=1）",
            "t_stat": "mean(IC) / (std(IC) / sqrt(n_dates))",
            "horizons_unit": "交易日",
        },
        "has_fwd_ret_column": "fwd_ret" in day_scores.columns,
        "has_stock_prices": stock_prices is not None,
        "horizons": {},
    }
    if not score_dates:
        result["horizons"] = {
            str(h): _aggregate_ic([]) for h in (IC_LABEL_HORIZON, *IC_HORIZONS)
        }
        return result

    # ---- 个股日线透视（asof 截断）：N=1 与 N=5/20 共用 --------------------
    price_pivot = None
    price_dates: list[pd.Timestamp] = []
    if stock_prices is not None:
        px = stock_prices[stock_prices["date"] <= asof].copy()
        if not px.empty:
            price_pivot = px.pivot_table(
                index="date", columns="instrument", values="close", aggfunc="last"
            ).sort_index()
            price_dates = [pd.Timestamp(d) for d in price_pivot.index]

    # 🔴 前瞻收益必须在 asof 前**已实现**（铁律 §12.2 无未来函数）：
    #    stock_scores.csv 的 fwd_ret 由 qlib 的 Ref(N<0) 构造，查询窗会向右扩展；
    #    一旦底层数据多出 asof 之后的交易日，asof 当天的 fwd_ret 就会含未来信息
    #    ⇒ 用 asof 截断的个股日线判定「最后一个可实现的 score 日期」，晚于它的截面一律剔除。
    realized_cutoff = price_dates[-2] if len(price_dates) >= 2 else None
    # ---- N=1：标签对齐的前瞻收益（stock_scores.csv 自带）----
    label_per_date: list[dict] = []
    if "fwd_ret" in day_scores.columns:
        for date in score_dates:
            if realized_cutoff is not None and date > realized_cutoff:
                # 前瞻 1 日的收益在 asof 前未实现 ⇒ 不入统计（不许用未来信息）
                label_per_date.append({"date": date.date().isoformat(), "n": 0, "rank_ic": None})
                continue
            grp = day_scores[day_scores["date"] == date][["instrument", "score", "fwd_ret"]]
            grp = grp.dropna(subset=["score", "fwd_ret"])
            n = int(len(grp))
            if n < 3:
                label_per_date.append({"date": date.date().isoformat(), "n": n, "rank_ic": None})
                continue
            ic = grp["score"].corr(grp["fwd_ret"], method="spearman")
            label_per_date.append(
                {
                    "date": date.date().isoformat(),
                    "n": n,
                    "rank_ic": None if not np.isfinite(ic) else float(ic),
                }
            )
    else:
        label_per_date = [
            {"date": d.date().isoformat(), "n": 0, "rank_ic": None} for d in score_dates
        ]

    label_agg = _aggregate_ic(label_per_date)
    if "fwd_ret" not in day_scores.columns:
        label_agg["status"] = "data_unavailable"
        label_agg["reason"] = (
            "个股分数文件没有 `fwd_ret` 列 ⇒ 无法构造 1 日前瞻收益 ⇒ 输出 null"
            "（可用 --stock-prices 提供个股日线后改用多日前瞻收益口径）"
        )
    else:
        label_agg["source"] = "stock_scores.csv 的 fwd_ret（1 日前瞻收益，标签对齐）"
    result["horizons"][str(IC_LABEL_HORIZON)] = label_agg

    # ---- N=5 / N=20：多日前瞻收益（需要个股日线）----
    for horizon in IC_HORIZONS:
        if price_pivot is None:
            result["horizons"][str(horizon)] = {
                "status": "data_unavailable",
                "reason": (
                    f"输入里没有**个股日线** ⇒ 无法构造 {horizon} 交易日的前瞻收益 ⇒ 输出 null"
                    f"（可用 --stock-prices 提供 date/instrument/close 的日线 CSV 后重算）"
                ),
                "mean_rank_ic": None,
                "std_rank_ic": None,
                "icir": None,
                "t_stat": None,
                "n_cross_sections": 0,
                "per_date": [],
            }
            continue
        pos_of = {d: i for i, d in enumerate(price_dates)}
        per_date: list[dict] = []
        for date in score_dates:
            if date not in pos_of:
                per_date.append({"date": date.date().isoformat(), "n": 0, "rank_ic": None})
                continue
            pos = pos_of[date]
            if pos + horizon >= len(price_dates):
                # 前瞻收益在 asof 前**未实现** ⇒ 不入统计（不许用未来信息）
                per_date.append({"date": date.date().isoformat(), "n": 0, "rank_ic": None})
                continue
            fwd = price_pivot.iloc[pos + horizon] / price_pivot.iloc[pos] - 1.0
            grp = day_scores[day_scores["date"] == date][["instrument", "score"]].copy()
            grp["fwd"] = grp["instrument"].map(fwd)
            grp = grp.dropna(subset=["score", "fwd"])
            n = int(len(grp))
            if n < 3:
                per_date.append({"date": date.date().isoformat(), "n": n, "rank_ic": None})
                continue
            ic = grp["score"].corr(grp["fwd"], method="spearman")
            per_date.append(
                {
                    "date": date.date().isoformat(),
                    "n": n,
                    "rank_ic": None if not np.isfinite(ic) else float(ic),
                }
            )
        agg = _aggregate_ic(per_date)
        agg["source"] = (
            f"个股日线（--stock-prices）构造的 {horizon} 交易日前瞻收益；"
            f"实现日必须 ≤ {asof.date()}"
        )
        result["horizons"][str(horizon)] = agg
    return result


# ---------------------------------------------------------------- §4~§6 过滤链重放（分析侧）
def _stage(stage: str, title: str, kind: str, rule: str, n_in: int, n_out: int,
           eliminated: list[dict], note: str | None = None) -> dict:
    """统一的过滤道记录（进 → 出 + 被淘汰名单）。"""
    return {
        "stage": stage,
        "title": title,
        "kind": kind,
        "rule": rule,
        "n_in": int(n_in),
        "n_out": int(n_out),
        "n_eliminated": int(len(eliminated)),
        "eliminated": eliminated,
        "note": note,
    }


def _row_brief(row: pd.Series, reason: str, detail: dict | None = None) -> dict:
    """把一只 ETF 的淘汰/入选明细压成**最小可读结构**（代码/名称/原因/明细）。

    用途：过滤网逐道淘汰名单与候选清单共用同一形状 ⇒ 报告 md 与 json 口径一致。
    """
    return {
        "code": str(row["code"]),
        "name": str(row.get("name", "")),
        "reason": reason,
        "detail": detail or {},
    }


def _liquidity_reason(row: pd.Series) -> str:
    """过滤网 1 的淘汰原因：**「查不到行情」与「阈值不达标」必须分开说**。

    🔴 2026-11-04 夜实测事故：159929 在 etf_prices.csv 里 0 行 ⇒ `n_days`/`amount_20d_mean`
    都是 NaN ⇒ 与阈值比较恒为 False ⇒ `filter_reason` 是空串 ⇒ 旧代码兜底成
    「流动性不达标」（假的）⇒ 又叠加 MA 判定的 `.get(code, False)` ⇒ 给一只真实持仓
    编出「流动性不达标 + 已跌破 MA60 ⇒ 强制退出」的理由。
    ⇒ 修法：先判「有无行情数据」，再谈阈值。
    """
    def _bad(x) -> bool:
        """判断单个数值是否**不可用**（None / NaN / ±inf）。"""
        if x is None:
            return True
        try:
            return not np.isfinite(float(x))
        except (TypeError, ValueError):
            return True

    if _bad(row.get("n_days")) and _bad(row.get("amount_20d_mean")):
        return "无行情数据（不在本次行情抓取覆盖内）⇒ 不可评估，非阈值淘汰"
    return str(row.get("filter_reason", "") or "流动性不达标")


def _rebuild_chain(
    *,
    asof: pd.Timestamp,
    prices: pd.DataFrame,
    holdings: pd.DataFrame,
    scores: pd.DataFrame,
    universe: pd.DataFrame,
    current_holdings: dict[str, float],
    alpha_weight: float | None,
    momentum_weight: float | None,
    hold_buffer: int | None,
    pool_mode: str | None,
    entry_ma_window: int | None,
    exit_ma_window: int | None,
    brake_mode: bool = False,
) -> dict:
    """在分析侧重放 `engine/portfolio.build_portfolio` 的过滤链（调用同一批 engine 函数）。

    🔴 目的是**可解释性**（逐道淘汰名单 / 候选拆解 / 持仓诊断），不是另起一套口径；
    返回的入选名单会被与 `build_portfolio` 的结果**逐位断言比对**。
    """
    stages: list[dict] = []
    held = {str(c) for c in current_holdings}

    # ---- 过滤网 1 · 流动性（复用 engine/filters.apply_liquidity_filter）----
    feats_all = compute_etf_features(prices, asof)
    merged = apply_liquidity_filter(universe, feats_all)
    pass_liq = merged[merged["pass_liquidity"]].copy()
    eliminated_liq = [
        _row_brief(
            r,
            _liquidity_reason(r),
            {
                "amount_20d_mean": _clean(r.get("amount_20d_mean")),
                "float_mcap": _clean(r.get("float_mcap")),
                "n_days": _clean(r.get("n_days")),
                "threshold_amount": C.MIN_AVG_AMOUNT_20D,
                "threshold_float_mcap": C.MIN_FLOAT_MCAP,
                "threshold_history_days": C.MIN_HISTORY_DAYS,
            },
        )
        for _, r in merged[~merged["pass_liquidity"]].sort_values("code").iterrows()
    ]
    # 🔴 过滤网 1 的淘汰计数要分「行情口径」与「快照口径」两套（2026-11-04 夜审）
    _n_no_px = sum(1 for _r in eliminated_liq if "无行情数据" in str(_r.get("reason", "")))
    _n_also_amt_fail = 0
    for _r in eliminated_liq:
        if "无行情数据" not in str(_r.get("reason", "")):
            continue
        _row_u = universe[universe["code"].astype(str) == str(_r.get("code", "")).zfill(6)]
        if len(_row_u):
            _amt = _row_u.iloc[0].get("avg_amount_20d")
            try:
                if _amt is not None and np.isfinite(float(_amt)) and float(_amt) < C.MIN_AVG_AMOUNT_20D:
                    _n_also_amt_fail += 1
            except (TypeError, ValueError):
                pass
    n_no_px = _n_no_px
    n_also_amt_fail = _n_also_amt_fail
    n_px_codes = (
        int(prices["code"].astype(str).nunique())
        if prices is not None and len(prices) and "code" in prices.columns
        else 0
    )
    stages.append(
        _stage(
            "filter_1_liquidity",
            "过滤网 1 · 流动性 / 规模 / 历史长度",
            "filter_net",
            f"20 日均额 ≥ {C.MIN_AVG_AMOUNT_20D:,.0f} 元 且 流通市值 ≥ {C.MIN_FLOAT_MCAP:,.0f} 元 "
            f"且 历史 ≥ {C.MIN_HISTORY_DAYS} 交易日",
            len(merged),
            len(pass_liq),
            eliminated_liq,
            note=(
                "🔴 **两套数据源的淘汰计数必须一起看**（2026-11-04 夜审）："
                f"本 stage 用**行情文件**（etf_prices.csv，覆盖 {n_px_codes} 只）判定 ⇒ "
                f"{n_no_px} 只因「无行情数据 ⇒ 不可评估」被淘汰；"
                f"但候选池快照（etf_universe.csv 的静态 20 日均额）显示其中 **{n_also_amt_fail}** 只"
                f"**本来也达不到 {C.MIN_AVG_AMOUNT_20D:,.0f} 元的均额门槛** ⇒ "
                "真实淘汰集合不小于「行情口径」，两者不可互相替代。"
            ),
        )
    )

    cand = pass_liq
    cand = add_relative_strength(cand)
    cand = momentum_score(cand)

    # ---- E = W × S（复用 engine/weights.map_scores_to_etfs）----
    alpha = map_scores_to_etfs(holdings, scores, asof)
    alpha_cols = ["etf_code", "alpha_score", "coverage", "coverage_ok", "n_stocks_scored"]
    cand = cand.merge(
        alpha[alpha_cols].rename(columns={"etf_code": "code"}), on="code", how="left"
    )
    cand["alpha_score"] = cand["alpha_score"].astype(float)
    cand["coverage_ok"] = cand["coverage_ok"].eq(True)
    bucket_col = "fund_type_bucket" if "fund_type_bucket" in cand.columns else None
    buckets = (
        cand[bucket_col].astype(str)
        if bucket_col
        else pd.Series([""] * len(cand), index=cand.index)
    )
    cand["is_defensive"] = [
        _is_defensive(n, c, b)
        for n, c, b in zip(cand["name"].astype(str), cand["code"].astype(str), buckets)
    ]
    # 全池视图（含被流动性淘汰的标的）⇒ 持仓诊断要能查到它们的 E / 动量
    lookup = merged.merge(
        alpha[alpha_cols].rename(columns={"etf_code": "code"}), on="code", how="left"
    )
    lookup["alpha_score"] = lookup["alpha_score"].astype(float)
    lookup["coverage_ok"] = lookup["coverage_ok"].eq(True)

    # ---- 池属性口径（e_covered）----
    mode = pool_mode or C.POOL_MODE
    n_before = len(cand)
    if mode == "e_covered":
        keep_mask = cand["coverage_ok"].eq(True) | cand["is_defensive"].eq(True)
        dropped = cand[~keep_mask]
        # 🔴 **「算不出 E」有两种完全不同的原因，不能混为一谈**（2026-11-04 夜实测）：
        #    ① 该 ETF 在 `etf_holdings.csv` 里**一行成分都没有** ⇒ 是**数据缺口**
        #       （例：159929 医药ETF汇添富，持仓 2500 股，成分从未抓过）
        #       ⇒ 结论只能写「成分数据缺失」，**不能**写成「该 ETF 无 A 股成分」
        #       （那是把数据缺口说成了标的产品属性，理由经不起追问）；
        #    ② 有成分记录，但**成分股没有一只能在 qlib 个股池里**（港股 / 美股等）
        #       ⇒ 这才是真实的「穿透为 0」。
        codes_with_holdings = {
            str(c).zfill(6) for c in holdings["etf_code"].astype(str).unique()
        }
        eliminated_attr = [
            _row_brief(
                r,
                (
                    (
                        f"**成分数据缺失**（`etf_holdings.csv` 无该 ETF 记录 ⇒ E 算不出来）；"
                        "这是数据缺口，不是「无 A 股成分」的结论"
                        if str(r.get("code", "")).zfill(6) not in codes_with_holdings
                        else "E 算不出（有成分记录，但成分股没有一只能在 qlib 个股池里 ⇒ 真实穿透为 0）"
                    )
                    + "，且非防守资产"
                    if r.get("coverage") is None or not np.isfinite(float(r.get("coverage", np.nan)))
                    else f"E 算不出（coverage={_pct(r.get('coverage'))} < "
                         f"{C.MIN_E_COVERAGE:.0%}，且非防守资产）"
                ),
                {
                    "coverage": _clean(r.get("coverage")),
                    "min_coverage": C.MIN_E_COVERAGE,
                    "has_holdings_record": str(r.get("code", "")).zfill(6) in codes_with_holdings,
                },
            )
            for _, r in dropped.sort_values("code").iterrows()
        ]
        cand = cand[keep_mask].copy()
    else:
        eliminated_attr = []
    stages.append(
        _stage(
            "extra_pool_attribute",
            "附加 · 标的池属性口径（能穿透才入池）",
            "extra",
            f"POOL_MODE={mode}：coverage ≥ {C.MIN_E_COVERAGE:.0%} 或 属防守资产",
            n_before,
            len(cand),
            eliminated_attr,
            note="不是三道过滤网之一，但会真实淘汰标的 ⇒ 单列披露",
        )
    )

    # ---- 破位过滤（入场 MA / 持仓强制退出 MA）----
    entry_win = C.ENTRY_MA_FILTER_WINDOW if entry_ma_window is None else int(entry_ma_window)
    exit_win = C.EXIT_MA_FILTER_WINDOW if exit_ma_window is None else int(exit_ma_window)
    held_codes = {str(c) for c in (current_holdings or {})}
    entry_ma = _ma_ok_map(prices, asof, entry_win)
    cand["entry_ma_ok"] = [
        (bool(entry_ma.get(str(c), False)) if entry_ma else True) or bool(d)
        for c, d in zip(cand["code"], cand["is_defensive"])
    ]
    #: 🔴 入场破位过滤的**引擎统计总体** = 池属性过滤后、绝对动量之前（实测 203 只）。
    #:    报告自己的 stage 是在**绝对动量之后**的池上统计的（31 只 ⇒ 淘汰 0），
    #:    与引擎 `stats.n_entry_ma_reject`（146）不是同一个总体 ⇒ 两个数并存会打架。
    #:    现在把引擎总体的淘汰数也一并披露（口径写清楚）。
    entry_ma_reject_attr_pop = int(
        ((~cand["entry_ma_ok"]) & (~cand["code"].astype(str).isin(held_codes))).sum()
    )
    exit_ma = _ma_ok_map(prices, asof, exit_win)
    exit_codes = {
        str(c) for c in current_holdings if not exit_ma.get(str(c), True)
    } if exit_win else set()
    cand["exit_ma_ok"] = [bool(exit_ma.get(str(c), True)) for c in cand["code"]]

    # ---- 组合级回撤刹车（净值回撤超阈值 ⇒ 整个组合切防守资产）----
    if brake_mode:
        n_in_brake = len(cand)
        dropped_brake = cand[~cand["is_defensive"]]
        cand = cand[cand["is_defensive"]].copy().reset_index(drop=True)
        stages.append(
            _stage(
                "extra_drawdown_brake",
                "附加 · 组合级回撤刹车",
                "extra",
                f"账户净值自峰值回撤 ≥ {C.DRAWDOWN_BRAKE_THRESHOLD:.0%} ⇒ 组合切防守资产",
                n_in_brake,
                len(cand),
                [
                    _row_brief(r, "组合处于回撤刹车状态 ⇒ 非防守资产一律剔除", {})
                    for _, r in dropped_brake.sort_values("code").iterrows()
                ],
                note="刹车开启时只按 ETF 自身动量排序（E 对防守资产无意义）",
            )
        )

    # ---- 绝对动量（权益类 mom120 > 门槛；防守资产豁免）----
    equity = cand[~cand["is_defensive"]]
    abs_ok = equity[equity["mom120"] > C.ABSOLUTE_MOMENTUM_THRESHOLD]
    relaxed = False
    n_in_abs = len(cand)
    if abs_ok.empty:
        pool = cand.copy()
        relaxed = True
    elif len(abs_ok) < C.MIN_N_HOLDINGS:
        pool = cand.copy()
        relaxed = True
    else:
        pool = pd.concat([abs_ok, cand[cand["is_defensive"]]], ignore_index=True)
    eliminated_abs = []
    if not relaxed:
        failed = equity[equity["mom120"] <= C.ABSOLUTE_MOMENTUM_THRESHOLD]
        eliminated_abs = [
            _row_brief(
                r,
                f"120 日动量 {_pct(r.get('mom120'))} ≤ {C.ABSOLUTE_MOMENTUM_THRESHOLD:.0%}"
                f"（绝对动量门槛）",
                {"mom120": _clean(r.get("mom120"))},
            )
            for _, r in failed.sort_values("code").iterrows()
        ]
    stages.append(
        _stage(
            "extra_absolute_momentum",
            "附加 · 绝对动量门槛（权益类）",
            "extra",
            f"mom120 > {C.ABSOLUTE_MOMENTUM_THRESHOLD:.0%}（防守资产豁免）",
            n_in_abs,
            len(pool),
            eliminated_abs,
            note=(
                "通过数不足 ⇒ 引擎放宽该门槛，本道未淘汰任何标的"
                if relaxed
                else None
            ),
        )
    )

    # ---- 入场破位过滤（新开仓不许买破位的）----
    n_in_entry = len(pool)
    block = (
        pd.Series(False, index=pool.index)
        if brake_mode
        else ((~pool["entry_ma_ok"]) & (~pool["code"].astype(str).isin(held)))
    )
    eliminated_entry = [
        _row_brief(r, f"收盘价跌破 MA{entry_win}（破位）⇒ 不许新开仓", {"entry_ma_window": entry_win})
        for _, r in pool[block].sort_values("code").iterrows()
    ]
    pool = pool[~block].copy().reset_index(drop=True)
    stages.append(
        _stage(
            "extra_entry_ma",
            f"附加 · 入场破位过滤（MA{entry_win}）",
            "extra",
            f"新开仓标的收盘价须站上 MA{entry_win}（防守资产豁免；已持有的留给 MA{exit_win} 规则）",
            n_in_entry,
            len(pool),
            eliminated_entry,
            note=(
                f"本 stage 的统计总体是**绝对动量过滤之后**的 {n_in_entry} 只 ⇒ 本 stage 淘汰 "
                f"{n_in_entry - len(pool)} 只；🔴 **引擎的同一条规则统计总体不同**"
                f"（池属性过滤后、绝对动量之前；该口径下破位淘汰 **{entry_ma_reject_attr_pop}** 只，见 engine.stats."
                "n_entry_ma_reject）⇒ 两个数不是同一总体，勿混用。"
            ),
        )
    )

    # ---- 持仓强制退出（跌破 MA{exit_win} ⇒ 无条件卖出）----
    n_in_exit = len(pool)
    eliminated_exit = [
        _row_brief(
            pool[pool["code"].astype(str) == code].iloc[0],
            f"持仓跌破 MA{exit_win} ⇒ 本期无条件卖出（不看排名）",
            {"exit_ma_window": exit_win},
        )
        for code in sorted(exit_codes)
        if len(pool[pool["code"].astype(str) == code])
    ]
    # 🔴 **被更早一道网淘汰的持仓同样要出现在这张表里**（2026-11-04 夜实测）：
    #    159929 / 513330 / 513770 三只持仓都跌破了 MA{exit_win} ⇒ 引擎把它们**强制退出**，
    #    但它们早已被「池属性口径」淘汰 ⇒ 不在 pool 里 ⇒ 旧代码只写「淘汰 0 只」，
    #    而同一份报告的诊断表又显示这 3 只 MA60 ❌ ⇒ 自相矛盾。
    for code in sorted(exit_codes):
        if len(pool[pool["code"].astype(str) == code]):
            continue
        lrow = lookup[lookup["code"].astype(str) == code]
        row = lrow.iloc[0] if len(lrow) else None
        eliminated_exit.append(
            _row_brief(
                row if row is not None else pd.Series({"code": code, "name": code}),
                f"持仓跌破 MA{exit_win} ⇒ 本期无条件卖出（不看排名）；"
                "**该标的已在更早一道网被淘汰**，此处单列以免漏掉强制退出事实",
                {"exit_ma_window": exit_win, "already_eliminated_earlier": True},
            )
        )
    if exit_codes:
        pool = pool[~pool["code"].astype(str).isin(exit_codes)].copy().reset_index(drop=True)
    stages.append(
        _stage(
            "extra_exit_ma",
            f"附加 · 持仓强制退出（MA{exit_win}）",
            "extra",
            f"已持仓且跌破 MA{exit_win} ⇒ 无条件卖出",
            n_in_exit,
            len(pool),
            eliminated_exit,
        )
    )

    # ---- 分数合成（与 engine/portfolio 同口径）----
    # 🔴 本文件**唯一的数值 fillna**（另一处 fillna 是名称列补空串，见 `_load_account_snapshot`）：
    #    **穿透为 0** 的 ETF（无 A 股成分 ⇒ E 算不出来）用池内均值补，
    #    标准化后恰为 z_alpha = 0（中性分，不奖不惩）⇒ 只能靠自身动量入选。
    #    ⚠️ 这是「兜底席」的来源（本次 31 只池里 **16 只**）⇒ 报告已逐只点名、风险披露也点名，
    #    **不许**改成 fillna(0)（那会变成最差分、白白浪费席位），也不许悄悄删掉这条口径。
    pool["z_alpha"] = _z(pool["alpha_score"].fillna(pool["alpha_score"].mean()))
    pool["z_mom"] = _z(pool["momentum_score_trend_adj"])
    w_alpha = C.ALPHA_SCORE_WEIGHT if alpha_weight is None else float(alpha_weight)
    w_mom = C.MOMENTUM_SCORE_WEIGHT if momentum_weight is None else float(momentum_weight)
    if brake_mode:
        w_alpha, w_mom = 0.0, 1.0      # 防守模式下只按自身动量排（E 对防守资产无意义）
    pool["final_score"] = w_alpha * pool["z_alpha"] + w_mom * pool["z_mom"]

    # ---- 过滤网 2 · 同质化去重 ＋ 过滤网 3 · 主题多样性（复用 engine/filters）----
    names = {str(r["code"]): str(r["name"]) for _, r in pool.iterrows()}
    theme_map, conflict_pairs = build_theme_and_conflicts(
        holdings, pool["code"].astype(str).tolist(), names=names, prices=prices, asof=asof
    )
    buf = C.HOLD_BUFFER if hold_buffer is None else int(hold_buffer)
    n_in_theme = len(pool)
    pool = apply_theme_cap(
        pool, theme_map, keep_codes=held, hold_buffer=buf, conflicts=conflict_pairs
    )
    selected_codes = sorted(str(c) for c in pool.loc[pool["selected"], "code"])
    selected_themes = {
        str(r["theme_id"]) for _, r in pool[pool["selected"]].iterrows()
    }
    dedup_blocked: list[dict] = []
    theme_blocked: list[dict] = []
    other_blocked: list[dict] = []
    for _, r in pool[~pool["selected"]].sort_values("final_score", ascending=False).iterrows():
        code = str(r["code"])
        conflict_hit = sorted(
            other
            for other in selected_codes
            if frozenset({code, other}) in conflict_pairs
        )
        theme_hit = str(r["theme_id"]) in selected_themes
        if conflict_hit:
            dedup_blocked.append(
                _row_brief(
                    r,
                    "与已入选标的同质（持仓重叠 ≥ "
                    f"{C.DEDUP_OVERLAP_THRESHOLD:.2f} 或 120 日相关性 ≥ {C.THEME_CORR_THRESHOLD:.2f}）",
                    {"conflict_with": conflict_hit},
                )
            )
        elif theme_hit:
            theme_blocked.append(
                _row_brief(
                    r,
                    f"主题族「{r['theme_id']}」已达上限（MAX_PER_THEME={C.MAX_PER_THEME}）",
                    {"theme_id": str(r["theme_id"])},
                )
            )
        else:
            reason = (
                "已持仓但未进入目标名额（分数回落 / 被同族标的占位）"
                if code in held
                else f"分数排名未进入目标名额（目标 {C.TARGET_N_HOLDINGS} 只 + 迟滞缓冲 {buf}）"
            )
            other_blocked.append(_row_brief(r, reason, {"final_score": _clean(r["final_score"])}))

    stages.append(
        _stage(
            "filter_2_3_dedup_and_theme",
            "过滤网 2 · 同质化去重　＋　过滤网 3 · 主题多样性",
            "filter_net",
            f"同质冲突对（重叠 ≥ {C.DEDUP_OVERLAP_THRESHOLD:.2f} 或 corr120 ≥ "
            f"{C.THEME_CORR_THRESHOLD:.2f}）逐对拦截；同族最多 {C.MAX_PER_THEME} 只",
            n_in_theme,
            len(selected_codes),
            dedup_blocked + theme_blocked + other_blocked,
            note=(
                "两网在引擎里是**同一次贪心**（按 final_score 降序）⇒ 这里的淘汰归因是"
                "「事后判定」，不是各自独立跑一遍；优先顺序：同质冲突 > 同族已满 > 未进名额"
            ),
        )
    )

    return {
        "stages": stages,
        "pool": pool,
        "lookup": lookup,
        "alpha": alpha,
        "theme_map": theme_map,
        "conflicts": conflict_pairs,
        "entry_ma": entry_ma,
        "exit_ma": exit_ma,
        "exit_codes": sorted(exit_codes),
        "relaxed": relaxed,
        "weights_used": {"alpha": w_alpha, "momentum": w_mom},
        "hold_buffer": buf,
        "pool_mode": mode,
        "selected_codes": selected_codes,
        "dedup_blocked": dedup_blocked,
        "theme_blocked": theme_blocked,
        "other_blocked": other_blocked,
    }


# ---------------------------------------------------------------- §6 候选清单
def _reason_map(stages: list[dict]) -> dict[str, dict]:
    """{code: {"stage":…, "title":…, "reason":…}} —— 某只标的**第一次**被淘汰的原因。"""
    out: dict[str, dict] = {}
    for st in stages:
        for item in st["eliminated"]:
            code = str(item["code"])
            if code not in out:
                out[code] = {
                    "stage": st["stage"],
                    "stage_title": st["title"],
                    "reason": item["reason"],
                    "detail": item.get("detail", {}),
                }
    return out


def _build_candidates(
    pool: pd.DataFrame, top_n: int, n_constituents: dict[str, int], reasons: dict[str, dict]
) -> list[dict]:
    """候选 Top-N（逐只拆解，便于人工核对）。"""
    ordered = pool.sort_values("final_score", ascending=False, na_position="last")
    out: list[dict] = []
    for rank, (_, r) in enumerate(ordered.head(top_n).iterrows(), 1):
        code = str(r["code"])
        out.append(
            {
                "rank": rank,
                "code": code,
                "name": str(r.get("name", "")),
                "e_score": _clean(r.get("alpha_score")),
                "coverage": _clean(r.get("coverage")),
                "n_constituents": int(n_constituents.get(code, 0)),
                "n_constituents_scored": _clean(r.get("n_stocks_scored")),
                "mom20": _clean(r.get("mom20")),
                "mom60": _clean(r.get("mom60")),
                "mom120": _clean(r.get("mom120")),
                "momentum_score": _clean(r.get("momentum_score_trend_adj")),
                "z_alpha": _clean(r.get("z_alpha")),
                "z_mom": _clean(r.get("z_mom")),
                "final_score": _clean(r.get("final_score")),
                "above_ma60": bool(r.get("above_ma60", False)),
                "entry_ma_ok": bool(r.get("entry_ma_ok", True)),
                "exit_ma_ok": None if r.get("exit_ma_ok") is None else bool(r.get("exit_ma_ok")),
                "broken": not bool(r.get("entry_ma_ok", True)),
                "theme_id": str(r.get("theme_id", code)),
                "is_defensive": bool(r.get("is_defensive", False)),
                "selected": bool(r.get("selected", False)),
                "not_selected_reason": None if bool(r.get("selected", False)) else reasons.get(code, {}).get("reason"),
            }
        )
    return out


# ---------------------------------------------------------------- §7 现有持仓逐只诊断
def _diagnose_holdings(
    snapshot: pd.DataFrame,
    chain: dict,
    universe: pd.DataFrame,
    total_assets: float,
    prices: pd.DataFrame | None = None,
) -> tuple[list[dict], list[dict]]:
    """逐只诊断现有持仓：还在不在池子、被哪道网淘汰、是否破位、E / 动量多少、盈亏多少。"""
    reasons = _reason_map(chain["stages"])
    pool = chain["pool"]
    pool_codes = {str(c) for c in pool["code"]}
    selected = set(chain["selected_codes"])
    universe_codes = {str(c) for c in universe["code"]}
    entry_ma = chain["entry_ma"]
    exit_ma = chain["exit_ma"]
    actions: list[dict] = []

    rows: list[dict] = []
    for _, h in snapshot.iterrows():
        code = str(h["code"])
        market_value = float(h["market_value"]) if np.isfinite(h["market_value"]) else float("nan")
        weight = market_value / total_assets if total_assets > 0 else float("nan")
        in_pool = code in pool_codes
        prow = pool[pool["code"].astype(str) == code]
        prow = prow.iloc[0] if len(prow) else None
        # 🔴 持仓**不在候选池里**时，池子查不到它的特征 ⇒ 回退到「全池视图」lookup
        #    （lookup 含被流动性淘汰的标的）⇒ 否则「E 值 / 动量」对持仓永远是 null，
        #    报告答不出「这只持仓的动量到底是多少」。
        lrow = chain["lookup"][chain["lookup"]["code"].astype(str) == code]
        lrow = lrow.iloc[0] if len(lrow) else None
        #: 特征取值源：先池子（带 final_score / theme_id），没有则用全池视图
        frow = prow if prow is not None else lrow
        reason = reasons.get(code)
        # 🔴 无行情数据的 code 根本不在 MA map 里 ⇒ 不能用 `.get(code, False)`
        # （那会把「查不到」当成「跌破」，凭空造出强制退出理由）。None = 不可判定。
        ma20_ok = entry_ma.get(code) if entry_ma else None
        ma60_ok = exit_ma.get(code) if exit_ma else None
        px_codes = set()
        if prices is not None and len(prices):
            px_codes = set(prices["code"].astype(str).unique())
        has_px = code in px_codes
        if code in selected:
            action = "持有"
        elif in_pool:
            action = "买入候选"
        else:
            action = "卖出"
        rows.append(
            {
                "code": code,
                "name": str(h.get("name", "")),
                "shares": _clean(h.get("shares")),
                "price": _clean(h.get("price")),
                "market_value": _clean(h.get("market_value")),
                "pnl": _clean(h.get("pnl")),
                "pnl_pct": _clean(h.get("pnl_pct")),
                "current_weight": _clean(weight),
                "in_universe": code in universe_codes,
                "passed_liquidity": bool(
                    chain["lookup"][chain["lookup"]["code"].astype(str) == code]["pass_liquidity"].iloc[0]
                )
                if len(chain["lookup"][chain["lookup"]["code"].astype(str) == code])
                else False,
                "in_candidate_pool": in_pool,
                "selected": code in selected,
                "rejected_by_stage": reason["stage"] if reason else None,
                "rejected_by_stage_title": reason["stage_title"] if reason else None,
                "reject_reason": reason["reason"] if reason else None,
                "reject_detail": reason["detail"] if reason else None,
                "ma20_ok": ma20_ok,
                "ma60_ok": ma60_ok,
                "broken": (ma20_ok is False) or (ma60_ok is False),
                "price_data_available": bool(has_px),
                "e_score": _clean(frow.get("alpha_score")) if frow is not None else None,
                "coverage": _clean(frow.get("coverage")) if frow is not None else None,
                "mom20": _clean(frow.get("mom20")) if frow is not None else None,
                "mom60": _clean(frow.get("mom60")) if frow is not None else None,
                "mom120": _clean(frow.get("mom120")) if frow is not None else None,
                "momentum_score": _clean(frow.get("momentum_score_trend_adj")) if frow is not None else None,
                "final_score": _clean(prow.get("final_score")) if prow is not None else None,
                "theme_id": str(prow.get("theme_id", code)) if prow is not None else None,
                "feature_source": "候选池" if prow is not None else (
                    "全池视图（未入池）" if lrow is not None else "无"),
                "action": action,
            }
        )
        actions.append(
            {
                "code": code,
                "name": str(h.get("name", "")),
                "action": action,
                "current_weight": _clean(weight),
                "market_value": _clean(h.get("market_value")),
                "reason": reason["reason"] if reason else (
                    "入选本期组合" if code in selected else "仍在候选池但未进入目标名额"
                ),
            }
        )
    return rows, actions


# ---------------------------------------------------------------- §9 风险披露（必填）
def _load_backtest(out_dir: Path, explicit: str | None = None) -> dict | None:
    """只读引用回测报告（风险披露里引用**实测**数字）；找不到就返回 None（不编数字）。"""
    # 🔴 旧代码用 `sorted(glob())[-1]` 挑回测产物 ⇒ 只要新增一个字典序更小的文件，
    #    报告就会静默改引别的 arm 的数字（口径漂移且无人察觉）⇒ 改为**显式指定**。
    if explicit:
        path = Path(explicit)
        if not path.exists():
            return None
    else:
        files = sorted(out_dir.glob(BACKTEST_GLOB))
        # 🔴 F3（2026-11-04 夜，专题档 §20.4）：回测产物曾**只**按输出目录找
        #    ⇒ 换一个输出目录重跑，报告会**静默**退回「回测产物不可得 ⇒ 不引用任何回测数字」
        #    ⇒ 风险披露少 3 条数字、且**不报错**（实测复现实验首轮就踩到）。
        #    修法：输出目录扫不到时，回退到**产物根目录**（现役 arm 所在地）⇒ 报告内容与输出目录解耦。
        if not files:
            _root = Path(C.ETF_RUN_ROOT)
            if _root.is_dir():
                files = sorted(_root.glob(BACKTEST_GLOB))
        if not files:
            return None
        path = files[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {"path": str(path), "data": data}


def _backtest_summary(backtest: dict | None) -> dict | None:
    """从回测产物里抽「**唯一可引用**」的口径真值（数字 + 区间 + 口径打包）。

    🔴 **产物不可得 ⇒ 返回 ``None``**（不编数字）；调用方负责渲染成「不可得」。
    🔴 本函数**只读**：不重算回测、不改任何产物。
    🔴 为什么要有它（2026-11-05 00:4x 实测）：报告 ``risk_disclosure`` 里分段稳定性
       只写了「符号相反」**没带数字**，且指向的档案小节是**旧口径**
       ⇒ 只读报告的人拿不到 H1/H2 实测值 ⇒ 本函数让真值**随报告一起带走**。
    """
    if not backtest:
        return None
    data = backtest.get("data") or {}
    pnet = data.get("portfolio_net") or {}
    bmk = data.get("benchmark_510300") or {}
    exc = data.get("excess_vs_510300") or {}
    exc_a = data.get("excess_arith_vs_510300") or {}
    cost = data.get("cost_assumption") or {}
    reb = data.get("rebalance") or {}
    brake = data.get("brake") or {}
    evalw = data.get("eval_window") or {}

    def _block(src: dict) -> dict:
        """从一个净值块里取关键指标（缺就是 None，**不补 0**）。"""
        return {
            "annualized_return": src.get("annualized_return"),
            "total_return": src.get("total_return"),
            "max_drawdown": src.get("max_drawdown"),
            "volatility": src.get("volatility"),
            "sharpe": src.get("sharpe"),
            "n_days": src.get("n_days"),
        }

    def _segment(key: str, block: dict) -> dict:
        """从一个分段块里取组合/基准/超额的年化（缺就是 None，不补 0）。"""
        # 🔴 分段块的 n_days 在子块里（portfolio/benchmark/excess 各自都有），
        #    顶层没有 ⇒ 先取顶层，再回落到 portfolio 子块（实测 352 / 327）。
        n_days = block.get("n_days")
        if n_days is None:
            n_days = (block.get("portfolio") or {}).get("n_days")
        out = {"key": key, "n_days": n_days}
        for name in ("portfolio", "benchmark", "excess"):
            sub = block.get(name) or {}
            out[f"{name}_annualized"] = sub.get("annualized_return")
            out[f"{name}_max_drawdown"] = sub.get("max_drawdown")
        return out

    segments = [_segment(k, v) for k, v in (data.get("segment_report") or {}).items()]
    exc_vals = [s.get("excess_annualized") for s in segments]
    exc_vals = [v for v in exc_vals if v is not None]
    sign_opposite = bool(exc_vals) and any(v > 0 for v in exc_vals) and any(v < 0 for v in exc_vals)

    return {
        "source_path": backtest.get("path"),
        "scenario": data.get("scenario"),
        "eval_window": {
            "start": evalw.get("start"),
            "end": evalw.get("end"),
            "n_days": evalw.get("n_days"),
        },
        "portfolio": _block(pnet),
        "benchmark_510300": _block(bmk),
        "excess_relative": _block(exc),
        "excess_arithmetic_annualized": exc_a.get("annualized_return"),
        "excess_definition": data.get("excess_definition"),
        "cost": {
            "commission_one_side": cost.get("commission_one_side"),
            "slippage_one_side": cost.get("slippage_one_side"),
            "annualized_one_way_turnover": cost.get("annualized_one_way_turnover"),
            "total_cost_drag": cost.get("total_cost_drag"),
        },
        "rebalance": {
            "every_n_days": reb.get("every_n_days"),
            "n_rebalances": reb.get("n_rebalances"),
            "n_rebalances_in_window": reb.get("n_rebalances_in_window"),
            "n_failed_rebalances": data.get("n_failed_rebalances"),
            "n_carried_forward_rebalances": data.get("n_carried_forward_rebalances"),
            "n_uncovered_days": data.get("n_uncovered_days"),
        },
        "brake_n_events": brake.get("n_events"),
        "segments": segments,
        "segment_sign_opposite": sign_opposite,
        "bias_disclosures": data.get("bias_disclosures") or [],
    }


#: 同主题换产品的**三种口径**（口径互不相同 ⇒ 披露时必须各自点名，**不许混称**）：
#:   ① |final_gap| 最小     ⇒ 最终分差最近 ⇒ **最容易翻**
#:   ② margin_realistic 降序 ⇒ 现实档余量最大 ⇒ **最难翻（现实档）**
#:   ③ margin_extreme 降序   ⇒ 极端档余量最大 ⇒ **最难翻（极端档）**
#: 🔴 旧实现把「极端档余量最大」的那一对叫成「最接近的一对」⇒ 披露自相矛盾
#:    （reviewer 实测：被点名的那对现实档翻转阈值 0.577175 ≫ 真·最接近那对的 0.254515，
#:    也就是说真·最接近的那对**更容易翻**）⇒ 本表把三个口径彻底拆开。
_SAME_THEME_CALIBERS = (
    {
        "key": "same_theme_closest_by_gap",
        # name：点名**第一名**时用的说法；order_desc：整张表的排序口径说明（列表里每条都带名次）
        "name": "最终分差最近的一对（|final_gap| 最小 ⇒ 最容易翻）",
        "order_desc": "最终分差最近（|final_gap| 升序 ⇒ 最容易翻在前）",
        "extra_margins": (),
    },
    {
        "key": "same_theme_tightest_realistic",
        "name": "现实档余量最大的一对（最难翻·现实档）",
        "order_desc": "现实档余量最大（margin_realistic 降序 ⇒ 最难翻在前）",
        "extra_margins": (),
    },
    {
        "key": "same_theme_tightest_extreme",
        "name": "极端档余量最大的一对（最难翻·极端档）",
        "order_desc": "极端档余量最大（margin_extreme 降序 ⇒ 最难翻在前）",
        "extra_margins": (("极端档余量", "margin_extreme"),),
    },
)


def _same_theme_calibers(fl: dict) -> list[dict]:
    """取同主题配对三个口径各的第一名（口径定义见 :data:`_SAME_THEME_CALIBERS`）。

    🔴 **不编数字**：某个口径缺配对列表、或其第一名的 ``final_gap`` /
    ``swap_threshold_realistic`` / ``margin_realistic`` 任一取不到 ⇒ 该口径
    **整体标为不可得**（返回 ``pair=None`` + ``reason``），**不许** fillna(0)、
    也不许用别的口径的数字顶替。
    """
    out: list[dict] = []
    for cal in _SAME_THEME_CALIBERS:
        key, name = cal["key"], cal["name"]
        lst = fl.get(key) or []
        if not lst:
            out.append({"key": key, "name": name, "pair": None, "cal": cal,
                        "reason": "该口径的配对列表不可得（报告里没有这一口径）"})
            continue
        pair = lst[0]
        missing = [f for f in ("final_gap", "swap_threshold_realistic", "margin_realistic")
                   if pair.get(f) is None]
        if missing:
            out.append({"key": key, "name": name, "pair": None, "cal": cal,
                        "reason": f"该口径第一名的字段不可得（缺 {' / '.join(missing)}）"})
            continue
        out.append({"key": key, "name": name, "pair": pair, "cal": cal, "reason": None})
    return out


def _w_lookahead_clause(la: dict | None) -> str:
    """把 W 快照前视的**实测数字**拼成一句可读的话。

    🔴 算不出来就明说「不可得」，**不编数字**（rules.md §十四.66）。
    🔴 结论由**余量**驱动：余量 > 0 才算「可能改」，余量 ≤ 0 ⇒ **改不了**。
    """
    if not la:
        return ""
    w = la.get("w_snapshot") or {}
    eb = la.get("e_bound") or {}
    fl = la.get("flip_test") or {}
    parts: list[str] = []
    gap = w.get("gap_days")
    if gap is not None:
        parts.append(
            f"🔴 **快照日 {w.get('snapshot_date')} 晚于信号日 {w.get('asof')} 共 {gap} 天**"
        )
    if w.get("n_snapshot_dates") is not None:
        parts.append(
            f"本地只有 **{w.get('n_snapshot_dates')} 个快照日** ⇒ 成分集漂移**不可直接观测**"
            f"（{w.get('unobservable_reason')}）"
        )
    if eb.get("max_delta_e_realistic") is not None:
        parts.append(
            f"实测上界（单只成分股进出，池内 σ(E)={_num(eb.get('pool_sigma_e'), 4)}）："
            f"E 误差 **现实档 ≤ {_num(eb.get('max_delta_e_realistic'), 4)}** / "
            f"**极端档 ≤ {_num(eb.get('max_delta_e_extreme'), 4)}**"
        )
    if fl.get("max_delta_final_realistic") is not None:
        parts.append(
            f"传导到最终分 **Δfinal ≤ {_num(fl.get('max_delta_final_realistic'), 4)}** / "
            f"**≤ {_num(fl.get('max_delta_final_extreme'), 4)}**"
        )
    same_margin = fl.get("max_same_theme_margin_realistic")
    cross_margin = fl.get("max_cross_theme_margin_realistic")
    seg: list[str] = []
    if cross_margin is None:
        seg.append("换主题：池内**没有第三个主题的候选** ⇒ 主题构成**不可能变**")
    elif cross_margin > 0:
        seg.append(f"换主题：**可能被改**（余量 {cross_margin:+.4f} > 0）")
    else:
        seg.append(f"换主题：**改不了**（余量 {cross_margin:+.4f} ≤ 0）")
    if same_margin is not None:
        if same_margin > 0:
            seg.append(f"同主题换产品：**可能被改**（余量 {same_margin:+.4f} > 0）")
        else:
            seg.append(f"同主题换产品：**改不了**（余量 {same_margin:+.4f} ≤ 0）")
    if seg:
        parts.append("排名稳健性（现实档）：" + "、".join(seg))
    if same_margin is not None and same_margin <= 0:
        # 🔴 同主题换产品**三个口径同时点名**（口径互不相同，不许把「极端档余量最大」
        #    的一对说成「最接近」—— 那是 reviewer 实测出来的旧披露 bug）。
        seg_cal: list[str] = []
        for item in _same_theme_calibers(fl):
            pair = item["pair"]
            if pair is None:
                seg_cal.append(f"{item['name']}**不可得**（{item['reason']}）")
                continue
            extra = ""
            for name, field in item["cal"]["extra_margins"]:
                val = pair.get(field)
                extra += f"，{name} {val if val is not None else '不可得'}"
            seg_cal.append(
                f"{item['name']}是 **{pair.get('selected')} ↔ {pair.get('challenger')}**"
                f"（最终分差 {pair.get('final_gap')}，翻转需 "
                f"{pair.get('swap_threshold_realistic')} 的分差变化，现实档余量 "
                f"{pair.get('margin_realistic')}{extra}）"
            )
        if seg_cal:
            parts.append(
                "同主题换产品的三个口径（各回答不同的问题，不许互相混称）："
                + "；".join(seg_cal)
                + f" ⇒ 现实档余量最大的一对余量 {same_margin:+.4f} ≤ 0 ⇒ 换不动"
            )
    if not parts:
        return "（前视审计不可得 ⇒ 本条不给数字）"
    return " ⇒ " + "；".join(parts) + "。"


def _risk_disclosure(
    *,
    asof: pd.Timestamp,
    chain: dict,
    n_etfs_no_e: int,
    n_universe_rows: int,
    n_pool: int,
    holdings: list[dict],
    total_assets: float,
    cash: float,
    backtest: dict | None,
    score_stats: dict | None = None,
    rank_ic: dict | None = None,
    micro: dict | None = None,
    lookahead: dict | None = None,
) -> list[dict]:
    """逐条风险披露（**禁止空数组**；数字只来自实测产物，缺产物就明说「产物不可得」）。

    🔴 **每条都要带数字**：``score_stats``（退化守卫实测）/ ``rank_ic``（上游区分度）/
    ``backtest``（回测真值）/ ``micro``（折溢价·流动性·跟踪误差代理）任一不可得时，
    对应文案明写「不可得」，**不许只写结论不给数字**（rules.md §十四.66）。
    """
    bt = backtest["data"] if backtest else None
    bt_path = backtest["path"] if backtest else None
    items: list[dict] = []

    # ---- 3.1 分段稳定性真值（现役口径）----
    # 🔴 指针必须指向 **§18.4**（唯一可引用）；§11 / §11.2 是聚类重构**前**的老口径，已作废。
    seg_rows: list[dict] = []
    seg_text = "回测产物不可得 ⇒ 分段稳定性**无法量化**（不给数字）。"
    if bt:
        seg_report = bt.get("segment_report") or {}
        cut = bt.get("segment_cut")
        evalw = bt.get("eval_window") or {}
        for key in sorted(seg_report.keys()):
            blk = seg_report[key] or {}
            sub = blk.get("excess") or {}
            seg_rows.append({
                "key": key,
                "n_days": sub.get("n_days") or blk.get("n_days"),
                "portfolio_annualized": (blk.get("portfolio") or {}).get("annualized_return"),
                "benchmark_annualized": (blk.get("benchmark") or {}).get("annualized_return"),
                "excess_annualized": sub.get("annualized_return"),
            })
        if len(seg_rows) >= 2:
            seg_a, seg_b = seg_rows[0], seg_rows[-1]
            seg_text = (
                f"H1（{evalw.get('start')}~{cut}，{seg_a['n_days']} 个交易日）"
                f"组合年化 {_pct(seg_a['portfolio_annualized'])}"
                f" vs 基准 {_pct(seg_a['benchmark_annualized'])}"
                f" ⇒ 超额 **{_pct(seg_a['excess_annualized'])}**；"
                f"H2（{cut}~{evalw.get('end')}，{seg_b['n_days']} 个交易日）"
                f"组合年化 {_pct(seg_b['portfolio_annualized'])}"
                f" vs 基准 {_pct(seg_b['benchmark_annualized'])}"
                f" ⇒ 超额 **{_pct(seg_b['excess_annualized'])}**"
            )

    # ---- 3.2 上游退化守卫的实测数字 ----
    up_text = "个股分数统计不可得 ⇒ 退化状态**无法量化**（不给数字）。"
    if score_stats:
        dgc = score_stats.get("degenerate_check") or {}
        ic1 = ((rank_ic or {}).get("horizons") or {}).get("1", {})
        up_text = (
            f"本次 asof {asof.date()} 横截面 {score_stats.get('n_stocks')} 只个股、"
            f"分数唯一值 {score_stats.get('unique_values')} 个、"
            f"标准差 {_num(score_stats.get('std'), 6)}、"
            f"区间 [{_num(score_stats.get('min'), 6)}, {_num(score_stats.get('max'), 6)}]"
            f" ⇒ 退化检查**{'通过' if dgc.get('passed') else '未通过'}**"
            f"（判据：{dgc.get('rule')}）"
        )
        if ic1.get("mean_rank_ic") is not None:
            up_text += (
                f"；1 日前瞻 RankIC 均值 {_num(ic1.get('mean_rank_ic'), 4)}"
                f"（ICIR {_num(ic1.get('icir'), 3)}，{ic1.get('n_cross_sections')} 个截面）"
            )
        up_text += " ⇒ 上游当前**有区分度**。"

    # ---- 3.3 ETF 微观结构实测（折溢价 / 流动性 / 跟踪误差代理）----
    prem = (micro or {}).get("premium_discount") or {}
    liq = (micro or {}).get("liquidity") or {}
    vol = (micro or {}).get("etf_volatility") or {}
    te = (micro or {}).get("tracking_error_proxy") or {}
    eq = prem.get("equity_only") or {}
    amt = liq.get("avg_amount_20d_selfcalc") or {}
    tor = liq.get("turnover_rate_pct") or {}
    veq = vol.get("equity_only") or {}
    tes = te.get("te_annualized_pct") or {}
    etf_parts: list[str] = []
    if prem.get("available"):
        etf_parts.append(
            f"候选 {prem.get('n_candidates')} 只（**权益类 {eq.get('n')} 只**），"
            f"折价率中位 {_num(eq.get('median'), 2)}%、"
            f"区间 [{_num(eq.get('min'), 2)}%, {_num(eq.get('max'), 2)}%]、"
            f"|均值| {_num(eq.get('mean_abs'), 2)}%；"
            f"{prem.get('n_missing_iopv')} 只因无 IOPV 不可算；"
            f"字段与自算最大偏差 {_num(prem.get('cross_check_max_abs_dev_pp'), 4)} pp"
        )
    if amt:
        etf_parts.append(
            f"20 日均成交额层内跨度 **{_num(liq.get('avg_amount_20d_max_over_min'), 1)} 倍**"
            f"（{_yi(amt.get('min'))} ~ {_yi(amt.get('max'))} 亿元）、"
            f"换手率 {_num(tor.get('min'), 2)}% ~ {_num(tor.get('max'), 2)}%"
        )
    if veq:
        etf_parts.append(
            f"ETF 自身 60 日年化波动（权益类 {veq.get('n')} 只）"
            f"{_num(veq.get('min'), 2)}% ~ {_num(veq.get('max'), 2)}%"
            f"（中位 {_num(veq.get('median'), 2)}%）"
        )
    if te.get("available") and tes:
        etf_parts.append(
            "**跟踪误差代理**（年化 std(ETF 日收益 − 静态成分加权日收益)）"
            f"{_num(tes.get('min'), 2)}% ~ {_num(tes.get('max'), 2)}%"
            f"（中位 {_num(tes.get('median'), 2)}%，"
            f"{te.get('n_computable')}/{te.get('n_candidates')} 只可算；"
            f"{te.get('n_not_computable')} 只不可得 —— 如货币 ETF 在 etf_holdings.csv 无成分记录）"
        )
    etf_text = "；".join(etf_parts) if etf_parts else "ETF 微观结构实测不可得（缺 etf_spot.csv / 个股日线）"
    # 换算成「假设口径」的倍数（bp vs 单边滑点 5bp）—— 只在有实测值时给
    te_bp: float | None = None
    pm_bp: float | None = None
    if tes.get("median") is not None:
        te_bp = float(tes["median"]) * 100.0
    if eq.get("median") is not None:
        pm_bp = float(eq["median"]) * 100.0
    slip_bp = float(C.ETF_SLIPPAGE) * 10000.0
    cmp_parts: list[str] = []
    if te_bp is not None:
        cmp_parts.append(
            f"跟踪误差代理中位 {te_bp:.0f}bp 是**假设单边滑点 {slip_bp:.0f}bp 的 {te_bp / slip_bp:.1f} 倍**"
        )
    if pm_bp is not None:
        cmp_parts.append(
            f"中位折价 {pm_bp:.0f}bp 是滑点假设的 {pm_bp / slip_bp:.1f} 倍"
        )
    cmp_text = ("；".join(cmp_parts) + " ⇒ 「未建模」**不是零成本**，"
                "滑点假设对跟踪偏离型风险**低估**") if cmp_parts else ""

    if bt:
        pnet = bt.get("portfolio_net", {}) or {}
        exc = bt.get("excess_vs_510300", {}) or {}
        brake = bt.get("brake", {}) or {}
        evalw = bt.get("eval_window", {}) or {}
        mdd = pnet.get("max_drawdown")
        # 期末回撤 / 峰值日 / 谷底日：从回测落盘的 **nav CSV** 算（不用 backtest JSON 里的
        # final_drawdown 字段 —— 那是 DataFrame 的 repr 字符串，塞进报告里没法读）。
        final_dd = None
        peak_date = None
        trough_date = None
        nav_csv = bt.get("nav_csv")
        if nav_csv and Path(nav_csv).exists():
            try:
                nav = pd.read_csv(nav_csv, parse_dates=["date"]).set_index("date")
                nav = nav.dropna(subset=["nav_portfolio"])
                running_peak = nav["nav_portfolio"].cummax()
                dd = nav["nav_portfolio"] / running_peak - 1.0
                final_dd = float(dd.iloc[-1])
                peak_date = str(nav["nav_portfolio"].idxmax().date())
                trough_date = str(dd.idxmin().date())
            except Exception:  # noqa: BLE001 —— 读不到就留空，不许编数字
                final_dd = None
                peak_date = None
                trough_date = None
        items.append(
            {
                "id": "in_sample_selection_bias",
                "title": "样本内选择偏差：参数是**在同一段 test 区间上扫出来**的",
                "severity": "high",
                "detail": (
                    f"引用的回测产物 `{bt_path}` 的评估区间为 "
                    f"{evalw.get('start')} ~ {evalw.get('end')}（{evalw.get('n_days')} 个交易日），"
                    f"而调仓周期 / α 权重 / 迟滞缓冲等超参是在**同一段区间**上做网格选出来的"
                    f" ⇒ 这里的超额是**样本内最优**，不是样本外期望；"
                    f"无独立 holdout 段；有效调仓期数仅 "
                    f"{(bt.get('rebalance') or {}).get('n_rebalances_in_window')} 期 ⇒ "
                    f"统计效力很弱。（截面超额 {_pct(exc.get('annualized_return'))}）"
                ),
            }
        )
        items.append(
            {
                "id": "current_drawdown_position",
                "title": "当前回撤位置：组合正处在历史最大回撤区间内",
                "severity": "high",
                "detail": (
                    f"回测实测最大回撤 {_pct(mdd)}"
                    + (f"，**期末净值自峰值回撤 {_pct(final_dd)}**" if final_dd is not None else "")
                    + (f"（峰值日 {peak_date} / 谷底日 {trough_date}）"
                       if peak_date and trough_date else "")
                    + " ⇒ 仍处在**样本内最差区间**（最大回撤谷底 "
                    + f"{_pct(mdd)}），此时的任何「最优参数」都应打折看待；"
                    "且等权 5 只主题 ETF 也会单日大跌，历史最差单日幅度需另行核对。"
                ),
            }
        )
    else:
        items.append(
            {
                "id": "in_sample_selection_bias",
                "title": "样本内选择偏差：参数是**在同一段 test 区间上扫出来**的",
                "severity": "high",
                "detail": (
                    "回测产物不可得（未在本次分析目录找到 `etf_rotation_backtest*.json`）"
                    "⇒ **本报告不引用任何回测数字**（不编数字）；"
                    "但超参网格在同一段 test 区间上选出的事实不变 ⇒ 其超额属样本内最优，非样本外期望。"
                ),
            }
        )
        items.append(
            {
                "id": "current_drawdown_position",
                "title": "当前回撤位置：回撤数字缺失（不可编造）",
                "severity": "high",
                "detail": (
                    "回测产物不可得 ⇒ 无法给出当前回撤位置与最大回撤数字；"
                    "该信息必须从 `manu/memory/etf_rotation_20261104.md` 的实测记录核对后再引用。"
                ),
            }
        )

    items.append(
        {
            "id": "w_snapshot_bias",
            "title": "W 快照偏差：成分股权重是**当前快照**，含前视偏差",
            "severity": "high",
            "detail": (
                f"W 的原料是 ETF 最新成分股权重（快照日 {chain.get('w_snapshot_date') or '未知'}）"
                f" ⇒ **成分股的历史变更不可知**，回测里等于假设「今天的成分股过去也一直在」"
                f"（指数成分约半年一调）⇒ 偏差非零；"
                f"本报告的 E 只是**当期暴露快照**，不是历史可复现的暴露。"
                + _w_lookahead_clause(lookahead)
            ),
        }
    )
    items.append(
        {
            "id": "pool_attribute_limits",
            "title": "池属性限制：候选池只有「能穿透」的那一类 ETF",
            "severity": "medium",
            "detail": (
                f"标的池口径 POOL_MODE={chain['pool_mode']} ⇒ 只有 coverage ≥ "
                f"{C.MIN_E_COVERAGE:.0%} 或属防守资产的 ETF 能入池；候选池文件共 {n_universe_rows} 行，"
                f"经流动性/属性过滤后剩 {n_pool} 只 ⇒ **选股范围被人为收窄**，"
                f"跨类资产（商品 / 跨境 / 货币）的分散收益被系统性放弃。"
            ),
        }
    )
    items.append(
        {
            "id": "non_lookthrough_etf",
            "title": "ETF 不可穿透：无 A 股成分的品种算不出 E",
            "severity": "high",
            "detail": (
                f"本次共 {n_etfs_no_e} 只 ETF 的 coverage < {C.MIN_E_COVERAGE:.0%}"
                f"（美股 / 港股通 / 实物金 / 商品期货等）⇒ 它们的 E = W×S **算不出来**，"
                f"只能靠**自身动量**入选 ⇒ 等于绕过了个股层建模（违反「不许跳过 E=W×S」的约束）；"
                f"报告里已逐只标注 coverage，请人工核对是否接受。"
            ),
        }
    )
    items.append(
        {
            "id": "survivorship_bias",
            "title": "幸存者偏差：候选池是**当前存续**的 ETF",
            "severity": "high",
            "detail": (
                f"候选池文件里的 {n_universe_rows} 行是**当前仍在交易的 ETF** ⇒ "
                "已清盘 / 合并 / 退市的 ETF **不在池里** ⇒ "
                "回测的选股空间被系统性高估（历史上可选的品种比现在多），"
                "且存续者往往已经被市场筛过一轮 ⇒ 回测超额含幸存者偏差。"
                "⇒ 本报告的所有年化 / 超额都要打这个折扣。"
            ),
        }
    )
    _no_px = [h for h in holdings if h.get("price_data_available") is False]
    if _no_px:
        items.append(
            {
                "id": "holding_without_price_data",
                "title": "⚠️ 有持仓的标的是**因为「查不到行情数据」**被排除的，不是信号说了算",
                "severity": "high",
                "detail": (
                    "本次持仓里 "
                    + "、".join(f"{h.get('code')} {h.get('name')}" for h in _no_px)
                    + " 在 etf_prices.csv 里没有任何行 ⇒ 它的 MA / 动量 / 流动性**全部无法评估** "
                    "⇒ 规则层按「不可评估 ⇒ 不入选 ⇒ 退出」处理。"
                    "🔴 这**不是**模型看空，而是**数据缺口**（上游只抓了 "
                    f"{n_universe_rows} 行候选池里的少数标的的行情）。"
                    "⇒ 若用户想保留这类标的，必须先补抓它的行情再重跑本报告，"
                    "否则这条清仓理由经不起追问。"
                ),
            }
        )
    items.append(
        {
            "id": "turnover_and_cost",
            "title": "换手与成本：月频也要真换手，人工执行有摩擦",
            "severity": "medium",
            "detail": (
                f"成本口径：佣金 {C.ETF_COMMISSION * 10000:.1f}bp/边 + 滑点 {C.ETF_SLIPPAGE * 10000:.1f}bp/边"
                f"（压力情景 {C.ETF_COMMISSION_STRESS * 10000:.1f}bp + {C.ETF_SLIPPAGE_STRESS * 10000:.1f}bp）；"
                f"迟滞缓冲 hold_buffer={chain['hold_buffer']} 用于压换手，但**换手仍是主要成本项**；"
                f"人工收盘前挂单的成交偏差（集合竞价、盘口深度、涨跌停排队）**未建模**。"
            ),
        }
    )
    items.append(
        {
            "id": "strategy_capacity",
            "title": "策略容量：小资金可执行，放大后冲击成本非线性",
            "severity": "medium",
            "detail": (
                f"当前账户总资产 {_num(total_assets, 2)} 元、现金 {_num(cash, 2)} 元"
                f" ⇒ 单笔规模很小；等权 {C.TARGET_N_HOLDINGS} 只、单只上限 40%；"
                f"但**流动性过滤只卡在日均成交额 {C.MIN_AVG_AMOUNT_20D:,.0f} 元**，"
                f"资金放大到千万级时冲击成本会非线性上升 ⇒ 容量结论**不可外推**。"
            ),
        }
    )
    items.append(
        {
            "id": "segment_instability",
            "title": "分段不稳定：超额高度依赖单一区制",
            "severity": "high",
            "detail": (
                f"同一配置的半年度超额**符号相反**：{seg_text}"
                "⇒ 结论强度只在「月频 + α/动量各半 + 迟滞优于纯动量与双周」这一条上成立；"
                "「年化 / 超额能到多少」属区制依赖，不可当承诺。"
                "（数字取自**现役回测产物**；唯一可引用的口径真值见 "
                "`manu/memory/etf_rotation_20261104.md` **§18.4** —— "
                "§11 / §11.2 是聚类重构**前**的老口径，**已作废**。）"
            ),
        }
    )
    items.append(
        {
            "id": "no_drawdown_brake",
            "title": "急跌无刹车：绝对动量过滤（mom120>0）在急跌里常常不触发",
            "severity": "high",
            "detail": (
                "120 日动量在急跌段仍可能为正 ⇒ 该过滤**不是**回撤保护；"
                "当前 v2 靠「入场 MA20 破位过滤 + 持仓 MA60 强制退出」部分缓解，"
                "但仍**没有组合级硬止损** ⇒ 极端行情下回撤可能再次显著扩大。"
            ),
        }
    )
    items.append(
        {
            "id": "holdings_concentration",
            "title": "现有持仓集中度与主题重叠",
            "severity": "medium",
            "detail": (
                f"账户现有 {len(holdings)} 只持仓；主题上限 {C.MAX_PER_THEME} 只/族；"
                f"若多只持仓落在同一主题族（红利 / 银行 / 医药…）⇒ 实际风险敞口远大于只数暗示的水平；"
                f"逐只诊断见 `current_holdings_diagnosis`。"
            ),
        }
    )
    items.append(
        {
            "id": "upstream_degradation",
            "title": "上游退化风险：个股分数一旦塌成常数，本报告全部结论失效",
            "severity": "high",
            "detail": (
                f"{up_text}"
                "🔴 但该保证**只对本次快照成立**：上游模型、"
                "归一化统计量（FrozenZScoreNorm）一旦被改动、或数据缺口扩大"
                "（成分股 coverage 下降），分数会**静默失真** ⇒ "
                "每次调仓前都应重跑本报告并核对退化检查与 RankIC，"
                "不要拿上一次的报告数字当今天的结论。"
            ),
        }
    )
    items.append(
        {
            "id": "etf_tracking_and_premium",
            "title": "ETF 自身风险：跟踪误差、折溢价与流动性分层未建模",
            "severity": "medium",
            "detail": (
                "本线用**日线收盘价**计算动量与破位，**未建模**折溢价率、跟踪误差、"
                "成分股停牌与指数调仓冲击 ⇒ 实际成交价可能与信号价显著偏离。"
                f"实测：{etf_text}。"
                + (f"🔴 换算成假设口径：{cmp_text}。" if cmp_text else "")
            ),
        }
    )
    return items


# ---------------------------------------------------------------- Markdown 渲染
def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """把表头与行渲染成 **Markdown 表格**的行列表（**不**含首尾空行）。"""
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def _render_markdown(rep: dict) -> str:
    """把结构化报告渲染成人读的中文 Markdown（**这份就是喂给 TradingAgents 的正文**）。"""
    meta = rep["meta"]
    lines: list[str] = []
    add = lines.append

    add(f"# ETF 轮动分析报告 · 信号日 {meta['asof']}")
    add("")
    add(
        f"> ⚠️ **免责声明：{rep['disclaimer']}**  \n"
        f"> 生成时间 {meta['generated_at']} · 执行机 {meta['host']} · "
        f"Python {meta['python_version']} · 数据源 `stock_scores.csv` / `etf_prices.csv` / "
        f"`etf_universe.csv` / `etf_holdings.csv`"
    )
    add("")

    # ---- 〇、结论先行 ----
    add("## 〇、结论先行")
    add("")
    actions = rep.get("recommendation", {}).get("actions", [])
    # 🔴 词表必须与 recommendation.actions 的取值一致（买入/卖出/持有）；
    #    旧代码写 "买入候选"（只存在于持仓诊断表）⇒ buys 恒为空 ⇒ 结论先行说「无买入」。
    buys = [a for a in actions if a["action"] == "买入"]
    sells = [a for a in actions if a["action"] == "卖出"]
    keeps = [a for a in actions if a["action"] == "持有"]
    ss = rep["stock_scores"]
    ic1 = rep["rank_ic"]["horizons"].get("1", {})
    nxt = rep["next_rebalance"]
    add(
        f"1. **个股层信号是有效的**：`{meta['asof']}` 全市场 {ss['n_stocks']} 只个股打分，"
        f"唯一值 {ss['unique_values']} 个、标准差 {_num(ss['std'], 4)} ⇒ "
        f"退化检查{'**通过**' if ss['degenerate_check']['passed'] else '**未通过**'}；"
        f"1 日前瞻 RankIC 均值 {_num(ic1.get('mean_rank_ic'), 4)}"
        f"（ICIR {_num(ic1.get('icir'), 3)}，{ic1.get('n_cross_sections')} 个截面）"
        f" ⇒ E=W×S 的上游**有区分度**。"
    )
    if buys or sells or keeps:
        add(
            f"2. **当前持仓应「买 {len(buys)} / 卖 {len(sells)} / 持 {len(keeps)}」**"
            f"（详见 §七逐只诊断）："
            + (
                "买入 " + "、".join(f"{a['code']} {a['name']}" for a in buys) + "；"
                if buys
                else "无买入；"
            )
            + (
                "卖出 " + "、".join(f"{a['code']} {a['name']}" for a in sells) + "；"
                if sells
                else "无卖出；"
            )
            + (
                "继续持有 " + "、".join(f"{a['code']} {a['name']}" for a in keeps) + "。"
                if keeps
                else "无继续持有。"
            )
        )
    else:
        add("2. **当前持仓应「买 0 / 卖 0 / 持 0」**：账户快照不可得或为空仓 ⇒ 无持仓诊断。")
    add(
        f"3. **下一个调仓节点（信号日）**：{nxt['target_date']}"
        f"（{nxt['rule']}），距信号日还有 **{nxt['trading_days_until']} 个交易日**。"
        + (
            f"🔴 **执行日 = {nxt['execution_date']}**（T 日收盘出信号 / T+1 收盘执行，"
            f"`execution_lag_days={nxt.get('execution_lag_days')}`）"
            f"⇒ **12-01 只是信号日，真正下单在 12-02 收盘**；"
            f"股数须按 **12-01 收盘价**重算（本报告里的股数是按 {nxt['asof']} 收盘价算的样例）。"
            if nxt.get("execution_date") else
            "⚠️ **执行日算不出来**（交易日历未覆盖）⇒ 不许编日期。"
        )
    )
    add(
        f"4. **目标组合**：{rep['recommendation'].get('n_selected', 0)} 只"
        f"（等权上限 {int(1.0 / max(C.TARGET_N_HOLDINGS, 1) * 100)}%，单只 ≤ 40%），"
        f"候选池 {rep['filter_stats']['n_candidate_pool']} 只 ⇒ 明细见 §六。"
    )
    add(
        f"5. **风险不可忽略**：{len(rep['risk_disclosure'])} 条已逐条披露（§九），"
        f"其中「样本内选择偏差 / W 快照偏差 / 无 A 股成分 ETF 算不出 E / 急跌无刹车」"
        f"为高等级 ⇒ **本报告不构成投资建议**。"
    )
    add("")

    # ---- 一、数据与参数口径 ----
    add("## 一、数据与参数口径")
    add("")
    params = meta["params"]
    rows = [
        ["信号日 asof", meta["asof"], "个股分数里最新的一天（可用 --asof 覆盖）"],
        ["标的池口径 pool_mode", params["pool_mode"], "只允许 coverage 达标或防守资产的 ETF 入池"],
        ["α 权重（E）", params["alpha_weight"], "E=W×S 与 ETF 自身动量的合成权重"],
        ["动量权重", params["momentum_weight"], "ETF 自身 20/60/120 日动量分权重"],
        ["目标只数 topk", params["target_n_holdings"], "组合规模（3~5 只）"],
        ["迟滞缓冲 hold_buffer", params["hold_buffer"], "已持有标的的保位缓冲（压换手）"],
        ["入场破位均线", params["entry_ma_window"], "收盘价跌破 ⇒ 不许新开仓"],
        ["持仓强制退出均线", params["exit_ma_window"], "跌破 ⇒ 无条件卖出"],
        ["E 最低覆盖率", params["min_e_coverage"], "低于此值 ⇒ E 不可信 ⇒ 不入池"],
        ["调仓周期（部署口径）", f"{params['rebalance_every_n_days']} 个交易日（月频）",
         "config.DEPLOYED_REBALANCE_EVERY_N_DAYS = 10×2；铁律 14.9"],
        ["信号→执行", f"{params['execution_lag_days']} 个交易日", "T 日收盘信号，T+1 收盘执行"],
        ["成本（单边）", f"佣金 {params['commission_bp']:.1f}bp + 滑点 {params['slippage_bp']:.1f}bp",
         f"压力情景 {params['commission_stress_bp']:.1f}bp + {params['slippage_stress_bp']:.1f}bp"],
        ["RankIC 窗口", "、".join(str(h) for h in params["ic_horizons"]) + " 交易日",
         "config.py 由其他 agent 维护 ⇒ 常量就地定义于 analyze.py"],
    ]
    lines.extend(_md_table(["项", "生效值", "说明"], rows))
    add("")
    add("**输入文件指纹（用的是哪份数据）**")
    add("")
    rows = []
    for name, fp in meta["inputs"].items():
        rows.append(
            [
                name,
                fp.get("path", "-"),
                "是" if fp.get("exists") else "**否**",
                f"{fp.get('size_bytes', 0):,}" if fp.get("exists") else "-",
                fp.get("mtime_local", "-"),
            ]
        )
    lines.extend(_md_table(["输入", "路径", "存在", "字节", "修改时间"], rows))
    add("")

    # ---- 数据新鲜度（M-R，2026-11-05 05:2x）----
    #    🔴 口径：行情够不到信号日 ⇒ **拒绝出报告**（守卫在 main() 里已抛异常）；
    #    这里渲染的是**已经通过守卫之后**的披露（个股陈旧 / W 滞后）。
    dfm = meta.get("data_freshness") or {}
    if dfm:
        add("**数据新鲜度（输入数据够不够得到信号日）**")
        add("")
        rows = [
            ["ETF 行情最新日", dfm.get("etf_prices_max_date", "-"),
             f"信号日 {dfm.get('signal_date')}｜落后 {dfm.get('etf_prices_lag_calendar_days')} 天"
             "（负数 = 数据比信号日还新；正数 = 数据够不到信号日 ⇒ **已被守卫拦下，不出报告**）"],
            ["陈旧 ETF 数", f"{dfm.get('n_etfs_stale')} / {dfm.get('n_etfs_total')}",
             "个别 ETF 最后一根 bar 早于信号日（停牌 / 新上市 / 退市）⇒ 只披露，不拦"],
            ["W 快照日（最早 ~ 最晚）",
             (f"{dfm.get('w_snapshot_date_min')} ~ {dfm.get('w_snapshot_date')}"
              if dfm.get("w_snapshot_date_min") and dfm.get("w_snapshot_date_min") != dfm.get("w_snapshot_date")
              else (dfm.get("w_snapshot_date") or "-")),
             (f"滞后信号日 {dfm.get('w_snapshot_lag_calendar_days_max')} 天"
              "（份额披露天然滞后）⇒ 其危害由「八之四 前视审计」量化"
              + (f"；🔴 **快照日期不一致**：{dfm.get('w_snapshot_n_stale_codes')} 只仍停在更早的日期"
                 "（刷新部分失败）⇒ 只看最晚会**低估**滞后" if dfm.get("w_snapshot_mixed_dates") else ""))],
            ["候选池快照日", dfm.get("universe_fetch_date") or "-",
             f"滞后信号日 {dfm.get('universe_lag_calendar_days')} 天"],
        ]
        lines.extend(_md_table(["项", "值", "说明"], rows))
        stale_rows = dfm.get("stale_etfs") or []
        if stale_rows:
            add("")
            detail = "、".join(
                f"{x['code']}（止于 {x['last_date']}，滞后 {x['lag_calendar_days']} 天）"
                for x in stale_rows
            )
            add("陈旧 ETF 明细（最多 20 只）：" + detail)
        add("")


    # ---- 二、个股分数 ----
    add("## 二、个股分数 S 的横截面分布与退化检查")
    add("")
    rows = [
        ["只数", f"{ss['n_stocks']}", "asof 当天有分数的个股数"],
        # 🔴 2026-11-05 05:0x 实测修：原先这格只写「< 10 ⇒ WARN」，
        #    与实际值 5574 并排 ⇒ 读者会读成「5574 < 10 ⇒ 已 WARN」⇒ **误导**。
        #    ⇒ 改法：把**实际比较结论**写进「说明」列（命中/未命中二选一）。
        ["唯一值数", f"{ss['unique_values']}",
         (f"< {rep['meta']['params']['unique_value_warn_threshold']} ⇒ 已触发 WARN（区分度偏低）"
          if ss["unique_values"] < rep["meta"]["params"]["unique_value_warn_threshold"]
          else f"≥ {rep['meta']['params']['unique_value_warn_threshold']} ⇒ 未触发低区分度 WARN")],
        ["均值", _num(ss["mean"], 4), ""],
        ["标准差", _num(ss["std"], 4), "为 0 ⇒ 分数退化成常数 ⇒ 拒绝出报告"],
        ["中位数", _num(ss["median"], 4), ""],
        ["min / p25 / p50 / p75 / max",
         " / ".join(_num(ss[k], 4) for k in ("min", "p25", "p50", "p75", "max")), "横截面分位"],
        ["退化断言", "**通过**" if ss["degenerate_check"]["passed"] else "**未通过**",
         ss["degenerate_check"]["rule"]],
    ]
    lines.extend(_md_table(["统计量", "值", "说明"], rows))
    add("")
    if rep["warnings"]:
        add("**告警**")
        add("")
        for w in rep["warnings"]:
            add(f"- {w}")
        add("")

    # ---- 三、RankIC ----
    add("## 三、RankIC（评估指标，**不参与选股决策**）")
    add("")
    add(f"> {rep['rank_ic']['note']}")
    add("")
    rows = []
    for horizon, agg in rep["rank_ic"]["horizons"].items():
        rows.append(
            [
                f"N={horizon} 交易日",
                _num(agg.get("mean_rank_ic"), 4),
                _num(agg.get("std_rank_ic"), 4),
                _num(agg.get("icir"), 3),
                _num(agg.get("t_stat"), 2),
                str(agg.get("n_cross_sections")),
                agg.get("reason") or agg.get("source") or "",
            ]
        )
    lines.extend(_md_table(["窗口", "均值 RankIC", "标准差", "ICIR", "t 值", "截面数", "说明"], rows))
    add("")

    # ---- 四、ETF 暴露分 ----
    es = rep["etf_scores"]
    add("## 四、ETF 暴露分 E = W × S")
    add("")
    add(f"- 口径：{es['definition']}")
    _w_min = (rep.get("meta", {}).get("data_freshness") or {}).get("w_snapshot_date_min")
    _w_mix = (rep.get("meta", {}).get("data_freshness") or {}).get("w_snapshot_mixed_dates")
    _w_text = es.get("w_snapshot_date") or "未知（文件无 fetch_date 字段，取文件修改时间）"
    if _w_min and _w_min != es.get("w_snapshot_date"):
        _w_text = f"{_w_min} ~ {es.get('w_snapshot_date')}"
    add(f"- W 快照日：**{_w_text}**（来源 `{es.get('w_source')}`）"
        + ("；🔴 快照日期**不一致**（部分 code 仍停在更早日期）⇒ 只看最晚会低估滞后"
           if _w_mix else ""))
    add(f"- 有 E 的 ETF {es['n_etfs_with_e']} 只；无 E / coverage 不足 {es['n_etfs_without_e']} 只")
    add("")
    rows = []
    for row in es["rows"][:30]:
        rows.append(
            [
                row["code"],
                row["name"][:18],
                _num(row.get("e_score"), 4),
                str(row.get("e_rank") or "-"),
                _pct(row.get("coverage")),
                str(row.get("n_constituents")),
                str(row.get("n_constituents_scored")),
                "✅" if row.get("coverage_ok") else "❌",
            ]
        )
    lines.extend(_md_table(
        ["代码", "名称", "E", "E 排名", "coverage", "成分数", "有分成分数", "可用"],
        rows,
    ))
    if len(es["rows"]) > 30:
        add("")
        add(f"> 表内只列前 30 行，完整清单见 JSON 的 `etf_scores.rows`（共 {len(es['rows'])} 行）。")
    add("")

    # ---- 五、过滤漏斗 ----
    add("## 五、过滤漏斗（逐道：进 → 出）")
    add("")
    fs = rep["filter_stats"]
    if fs.get("n_universe_rows"):
        add(f"> ⚠️ **口径（铁律 §12.4 覆盖率必须打印）**：候选池文件共 "
            f"{fs['n_universe_rows']} 行，其中**只有 {fs.get('n_with_price_data', '—')} 只"
            f"有行情数据**（覆盖 {fs.get('price_coverage_pct', '—')}%）"
            f"，其余 {fs.get('n_without_price_data', '—')} 只**根本无行情** ⇒ "
            f"第一道「{fs['stages'][0]['title'] if fs.get('stages') else '流动性'}」淘汰的 "
            f"{fs['stages'][0]['n_eliminated'] if fs.get('stages') else '—'} 只里，"
            f"**多数是「查不到行情」而不是「阈值不达标」** ⇒ "
            f"实际可评估面只有 {fs.get('n_with_price_data', '—')}/{fs['n_universe_rows']}。")
        add("")
    rows = []
    for st in rep["filter_stats"]["stages"]:
        rows.append(
            [
                st["title"],
                st["kind"],
                str(st["n_in"]),
                str(st["n_out"]),
                str(st["n_eliminated"]),
                st["rule"],
            ]
        )
    lines.extend(_md_table(["过滤道", "类别", "进", "出", "淘汰", "规则"], rows))
    add("")
    for st in rep["filter_stats"]["stages"]:
        if not st["eliminated"]:
            continue
        add(f"**{st['title']}** —— 淘汰 {st['n_eliminated']} 只")
        add("")
        rows = [[i["code"], i["name"][:18], i["reason"]] for i in st["eliminated"][:40]]
        lines.extend(_md_table(["代码", "名称", "淘汰原因"], rows))
        if len(st["eliminated"]) > 40:
            add("")
            add(f"> 只列前 40 只，完整名单见 JSON 的 `filter_stats.stages[].eliminated`。")
        add("")

    # ---- 六、候选清单 ----
    add("## 六、候选清单（Top-N，供人工核对）")
    add("")
    rows = []
    for c in rep["candidates"]:
        rows.append(
            [
                str(c["rank"]),
                c["code"],
                c["name"][:18],
                _num(c.get("e_score"), 3),
                _pct(c.get("coverage")),
                _pct(c.get("mom20")),
                _pct(c.get("mom60")),
                _pct(c.get("mom120")),
                _num(c.get("momentum_score"), 3),
                _num(c.get("final_score"), 3),
                "是" if c.get("broken") else "否",
                c.get("theme_id") or "-",
                "✅ 入选" if c.get("selected") else (c.get("not_selected_reason") or "未入选"),
            ]
        )
    lines.extend(_md_table(
        ["#", "代码", "名称", "E", "coverage", "mom20", "mom60", "mom120", "动量分", "总分",
         "破位", "主题簇", "结论"],
        rows,
    ))
    add("")

    # ---- 七、持仓诊断 ----
    add("## 七、现有持仓逐只诊断")
    add("")
    diag = rep["current_holdings_diagnosis"]
    if not diag.get("available"):
        add(f"> ⚠️ 账户快照不可得：{diag.get('reason')}")
        add("")
    else:
        rows = []
        for h in diag["holdings"]:
            rows.append(
                [
                    h["code"],
                    h["name"][:18],
                    _num(h.get("market_value"), 0),
                    _pct(h.get("pnl_pct")),
                    _pct(h.get("current_weight")),
                    "是" if h.get("in_universe") else "否",
                    "是" if h.get("in_candidate_pool") else "否",
                    "是" if h.get("selected") else "否",
                    ("MA20 " + ("✅" if h.get("ma20_ok") else "❌")
                     + " / MA60 " + ("✅" if h.get("ma60_ok") else "❌")),
                    _num(h.get("e_score"), 3),
                    _pct(h.get("mom120")),
                    h.get("reject_reason") or "—",
                    h.get("action"),
                ]
            )
        lines.extend(_md_table(
            ["代码", "名称", "市值", "盈亏率", "权重", "在池", "在候选", "入选", "破位",
             "E", "mom120", "没进的原因", "动作"],
            rows,
        ))
        add("")

    # ---- 八、下一个调仓节点 ----
    add("## 八、下一个调仓节点")
    add("")
    rows = [
        ["推导规则", nxt["rule"]],
        ["目标日", nxt["target_date"]],
        ["**信号日**", f"{nxt.get('signal_date') or nxt['target_date']}（目标日即信号日）"],
        ["**执行日（T+1 收盘）**",
         (f"**{nxt['execution_date']}**" if nxt.get("execution_date")
          else "⚠️ 算不出来（日历未覆盖）")],
        ["信号→执行口径", nxt.get("execution_rule") or "-"],
        ["目标月", nxt["target_month"]],
        ["距**信号日**交易日数（自信号日 asof 起算）", f"{nxt['trading_days_until']} 个"],
        ["日历校验", "✅ 该日在交易日历中" if nxt["in_calendar_verified"] else "❌"],
        ["日历文件", nxt.get("calendar_path") or "-"],
        [f"按部署节奏（每 {params['rebalance_every_n_days']} 交易日）的下一信号日",
         nxt.get("cycle_signal_date") or "日历长度不足"],
    ]
    lines.extend(_md_table(["项", "值"], rows))
    add("")

    # ---- 八之二、回测口径真值（唯一可引用；含分段稳定性） ----
    add("## 八之二、回测口径真值（**唯一可引用**；含分段稳定性）")
    add("")
    bs = (rep.get("meta") or {}).get("backtest_summary")
    if not bs:
        add("> ⚠️ 回测产物不可得 ⇒ 本节**不引用任何回测数字**（不编数字）；"
            "口径请查 `manu/memory/etf_rotation_20261104.md` §18.4。")
        add("")
    else:
        ew = bs.get("eval_window") or {}
        pf = bs.get("portfolio") or {}
        bm = bs.get("benchmark_510300") or {}
        ex = bs.get("excess_relative") or {}
        co = bs.get("cost") or {}
        rb = bs.get("rebalance") or {}
        rows = [
            ["口径产物", f"`{bs.get('source_path')}`"],
            ["场景", str(bs.get("scenario") or "-")],
            ["评估窗口", f"{ew.get('start')} ~ {ew.get('end')}（{ew.get('n_days')} 天）"],
            ["组合年化", _pct(pf.get("annualized_return"))],
            ["相对基准超额（几何）", f"{_pct(ex.get('annualized_return'))}"
                                  f"（算术口径 {_pct(bs.get('excess_arithmetic_annualized'))}）"],
            ["组合最大回撤", f"{_pct(pf.get('max_drawdown'))}"
                            f"（基准 {_pct(bm.get('max_drawdown'))}）"],
            ["年化单边换手", f"{_num(co.get('annualized_one_way_turnover'), 2)}×"
                            f"（成本拖累 {_pct(co.get('total_cost_drag'))}）"],
            ["基准 510300 年化", f"{_pct(bm.get('annualized_return'))}"
                                "（回归探针：必须恒为 12.11%）"],
            ["有效调仓期数", f"{rb.get('n_rebalances_in_window')} 期"
                            f"（信号日 {rb.get('n_rebalances')} 个）"
                            f"｜构建失败 {rb.get('n_failed_rebalances')}"
                            f"｜沿用上一期 {rb.get('n_carried_forward_rebalances')}"
                            f"｜未覆盖天数 {rb.get('n_uncovered_days')}"],
        ]
        lines.extend(_md_table(["项", "值"], rows))
        add("")
        segs = bs.get("segments") or []
        if segs:
            add("**分段稳定性（铁律 §十四.11 要求必须附）**")
            add("")
            srows = []
            for sg in segs:
                ndays = sg.get("n_days")
                srows.append([
                    sg.get("key"),
                    str(ndays) if ndays is not None else "-",
                    _pct(sg.get("portfolio_annualized")),
                    _pct(sg.get("benchmark_annualized")),
                    _pct(sg.get("excess_annualized")),
                ])
            lines.extend(_md_table(["分段", "天数", "组合年化", "基准年化", "超额年化"], srows))
            add("")
            if bs.get("segment_sign_opposite"):
                add("⚠️ **H1/H2 超额符号相反 ⇒ 样本内选择偏差未消，不能当预期收益**"
                    "（超参与评估区间同段扫出，无独立 holdout 段）。")
                add("")
        add("口径变更史与「旧口径作废」声明见 `manu/memory/etf_rotation_20261104.md` "
            "**§18.4**（§11 / §11.2 是聚类重构**前**的老口径，**已作废**）。")
        add("")

    # ---- 八之三、ETF 微观结构实测（折溢价 / 流动性分层 / 跟踪误差代理） ----
    micro = rep.get("etf_microstructure") or {}
    add("## 八之三、ETF 微观结构实测（折溢价 · 流动性分层 · 跟踪误差代理）")
    add("")
    if not micro:
        add("⚠️ 微观结构实测**不可得**（未生成该段）⇒ 不得据此下任何结论。")
        add("")
    else:
        samp = micro.get("sample") or {}
        prem_s = micro.get("premium_discount") or {}
        liq_s = micro.get("liquidity") or {}
        vol_s = micro.get("etf_volatility") or {}
        te_s = micro.get("tracking_error_proxy") or {}
        add(f"样本：报告 top-N 候选 **{samp.get('n_candidates')} 只**（asof {samp.get('asof')}）；"
            f"其中货币 ETF {samp.get('n_money_market')} 只"
            f"（{', '.join(samp.get('money_market_codes') or []) or '无'}）；"
            f"回看窗口 {samp.get('lookback_trading_days')} 个交易日。")
        add("")
        if prem_s.get("available"):
            eq_s = prem_s.get("equity_only") or {}
            add(f"- **折溢价率**（{prem_s.get('source')}）：权益类 {eq_s.get('n')} 只中位 "
                f"**{_num(eq_s.get('median'), 2)}%**、区间 [{_num(eq_s.get('min'), 2)}%, "
                f"{_num(eq_s.get('max'), 2)}%]、|均值| {_num(eq_s.get('mean_abs'), 2)}%；"
                f"另 {prem_s.get('n_missing_iopv')} 只因无 IOPV **不可算**；"
                f"字段与自算 (最新价/IOPV−1) 的最大偏差 "
                f"**{_num(prem_s.get('cross_check_max_abs_dev_pp'), 4)} pp** ⇒ 字段口径可信。"
                f"（快照日 {prem_s.get('spot_snapshot_date')}；"
                f"{prem_s.get('money_market_note')}）")
        else:
            add(f"- **折溢价率**：⚠️ **不可得** —— {prem_s.get('reason')}")
        amt_s = liq_s.get("avg_amount_20d_selfcalc") or {}
        if amt_s:
            add(f"- **流动性分层**：20 日均成交额层内跨度 "
                f"**{_num(liq_s.get('avg_amount_20d_max_over_min'), 1)} 倍**"
                f"（{_yi(amt_s.get('min'))} ~ {_yi(amt_s.get('max'))} 亿元，"
                f"中位 {_yi(amt_s.get('median'))} 亿元）；换手率 "
                f"{_num((liq_s.get('turnover_rate_pct') or {}).get('min'), 2)}% ~ "
                f"{_num((liq_s.get('turnover_rate_pct') or {}).get('max'), 2)}%。"
                f"过滤阈值只卡日均成交额下限 "
                f"{_yi(liq_s.get('filter_threshold_avg_amount_20d'))} 亿元 ⇒ "
                f"{liq_s.get('note')}")
        veq_s = vol_s.get("equity_only") or {}
        if veq_s:
            add(f"- **ETF 自身波动**（{vol_s.get('definition')}）：权益类 {veq_s.get('n')} 只 "
                f"{_num(veq_s.get('min'), 2)}% ~ {_num(veq_s.get('max'), 2)}%"
                f"（中位 {_num(veq_s.get('median'), 2)}%）；{vol_s.get('equity_only_note')}")
        if te_s.get("available"):
            tes_s = te_s.get("te_annualized_pct") or {}
            cor_s = te_s.get("daily_return_correlation") or {}
            add(f"- **跟踪误差代理**（{te_s.get('definition')}）："
                f"**{_num(tes_s.get('min'), 2)}% ~ {_num(tes_s.get('max'), 2)}%**"
                f"（中位 {_num(tes_s.get('median'), 2)}%），可算 "
                f"{te_s.get('n_computable')}/{te_s.get('n_candidates')} 只；"
                f"日收益相关系数 {_num(cor_s.get('min'), 4)} ~ {_num(cor_s.get('max'), 4)}"
                f"（中位 {_num(cor_s.get('median'), 4)}）。")
            add(f"  - ⚠️ {te_s.get('caveat')}")
            for code, why in sorted((te_s.get("not_computable_reason") or {}).items()):
                add(f"  - {code}：**不可得** —— {why}")
        else:
            add(f"- **跟踪误差代理**：⚠️ **不可得** —— {te_s.get('reason')}")
        for note in micro.get("notes") or []:
            add(f"- ℹ️ {note}")
        add("")

    # ---- 八之四、前视审计（无未来函数台账 · W 快照前视的 E 上界 · 排名稳健性） ----
    la = rep.get("lookahead_audit") or {}
    add("## 八之四、前视审计（无未来函数台账 · W 快照前视的 E 上界 · 排名稳健性）")
    add("")
    if not la:
        add("⚠️ 前视审计**不可得**（未生成该段）⇒ 不得据此下任何结论。")
        add("")
    else:
        inp = la.get("inputs") or {}
        w = la.get("w_snapshot") or {}
        eb = la.get("e_bound") or {}
        fl = la.get("flip_test") or {}
        add(f"信号日 **asof {la.get('asof')}**；{inp.get('summary')}。")
        add("")
        add("| 输入 | 日期字段 | 文件最晚日 | **实际消费日** | 相对 asof | 角色 | 决策前视 | 用在哪 |")
        add("|:--|:--|:--|:--|--:|:--|:--:|:--|")
        for e in (inp.get("entries") or []):
            flag = {"blocking": "🔴 是", "info": "⚪ 否（未来行被截断/被门控）"}.get(
                e.get("severity"), "否")
            eff = e.get("effective_date")
            add(f"| {e.get('name')} | {e.get('date_field')} | {e.get('file_date')} | "
                f"{eff if eff else '（未被消费）'} | "
                f"{e.get('effective_delta_days_vs_asof')} 天 | {e.get('role')} | {flag} | "
                f"{e.get('used_for')} |")
        add("")
        if w.get("gap_days") is not None:
            add(f"- 🔴 **W 快照前视**：快照日 **{w.get('snapshot_date')}** 晚于信号日 "
                f"**{w.get('asof')}** 共 **{w.get('gap_days')} 天**；"
                f"本地只有 **{w.get('n_snapshot_dates')} 个快照日** ⇒ {w.get('unobservable_reason')} "
                f"⇒ **不可修（fixable=false），只能量化**。")
        if eb.get("max_delta_e_realistic") is not None:
            add(f"- **E 误差上界**（口径：{eb.get('definition')}）：候选 {eb.get('n_etfs')} 只中 "
                f"**现实档 ≤ {_num(eb.get('max_delta_e_realistic'), 4)}**、"
                f"**极端档 ≤ {_num(eb.get('max_delta_e_extreme'), 4)}**"
                f"（池内 σ(E) = {_num(eb.get('pool_sigma_e'), 4)}）。")
        add(f"- **排名稳健性**：{fl.get('conclusion')}")
        comp = fl.get("theme_composition") or {}
        if comp:
            add(f"  - 当前入选主题构成：" + "、".join(f"{k}×{v}" for k, v in comp.items()))
        add("  - **同主题换产品的三种口径**（各回答不同的问题，**不许互相混称**）："
            "① 最终分差最近（|final_gap| 最小）⇒ **最容易翻**；"
            "② 现实档余量最大 ⇒ **最难翻（现实档）**；"
            "③ 极端档余量最大 ⇒ **最难翻（极端档）**。")
        for cal in _SAME_THEME_CALIBERS:
            lst = fl.get(cal["key"]) or []
            if not lst:
                add(f"  - **{cal['order_desc']}**：**不可得**"
                    f"（报告里没有这一口径的配对列表 ⇒ 该口径不写数字）")
                continue
            for rank, c in enumerate(lst[:3], start=1):
                thr = c.get("swap_threshold_realistic")
                mrg = c.get("margin_realistic")
                thr_txt = f"{thr}" if thr is not None else "**不可得**"
                mrg_txt = f"**{mrg}**" if mrg is not None else "**不可得**"
                extra_txt = ""
                for name, field in cal["extra_margins"]:
                    v = c.get(field)
                    extra_txt += f"、{name} " + (f"**{v}**" if v is not None else "**不可得**")
                add(f"  - **{cal['order_desc']}·第 {rank} 名**："
                    f"**{c.get('selected')}（{c.get('selected_name')}）** vs "
                    f"**{c.get('challenger')}（{c.get('challenger_name')}）**，"
                    f"最终分差 **{c.get('final_gap')}**，翻转需 "
                    f"{thr_txt} 的分差变化 ⇒ 现实档余量 {mrg_txt}{extra_txt}"
                    f"（≤ 0 ⇒ 换不动；换产品 ⇒ 敞口不变，只换供应商）")
        add("")

    # ---- 九、风险披露 ----
    add("## 九、风险披露（必读，逐条）")
    add("")
    for i, item in enumerate(rep["risk_disclosure"], 1):
        sev = {"high": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}.get(item.get("severity", ""), "")
        add(f"{i}. **[{sev}] {item['title']}**")
        add(f"   {item['detail']}")
        add("")

    # ---- 十、免责 ----
    add("## 十、免责声明")
    add("")
    add(rep["disclaimer"])
    add("")
    add(
        "附：本报告由 `manu/etf_rotation/analyze.py` 自动生成（口径与 `manu/memory/rules.md` §十四一致）；"
        "所有数字只来自本次实际读到的产物，**读不到的部分一律标注「不可得」而非估算**。"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI
def parse_args() -> argparse.Namespace:
    """解析命令行参数（信号日 / 输入路径 / 输出目录 / 候选只数 / 账户快照）。"""
    p = argparse.ArgumentParser(description="ETF 分析报告生成器（TradingAgents 辩论输入材料）")
    p.add_argument("--asof", default=None, help="信号日（含），默认取个股分数里最新的一天")
    p.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT), help="报告落盘目录")
    p.add_argument("--prices", default=None, help="ETF 行情 CSV（默认 etf_prices.csv）")
    p.add_argument("--spot", default=None,
                   help="ETF 点时刻快照 CSV（默认 etf_spot.csv；折溢价率/换手率来源，"
                        "不可得时该段标「不可得」而不是补 0）")
    p.add_argument("--universe", default=None, help="ETF 候选池 CSV（默认 etf_universe.csv）")
    p.add_argument("--holdings-csv", default=None, help="ETF 持仓（W 原料）CSV（默认 etf_holdings.csv）")
    p.add_argument("--scores", default=None, help="个股分数 CSV（默认 stock_scores.csv）")
    p.add_argument("--account-snapshot", default=None, help="账户快照 CSV（默认取 account.ACCOUNT_SNAPSHOT_CSV）")
    p.add_argument("--stock-prices", default=None,
                   help="个股日线 CSV（date,instrument,close）—— 用于构造多日前瞻收益的 RankIC")
    p.add_argument("--calendar", default=None, help="qlib 交易日历 day.txt（默认由个股线 config 推导）")
    p.add_argument("--trade-calendar", default=None,
                   help="akshare 完整交易日历 CSV（**含未来交易日**，qlib 日历不含未来）")
    p.add_argument("--top-n", type=int, default=15, help="候选展示条数（默认 15）")
    p.add_argument("--backtest", default=None,
                   help="显式指定要引用的回测产物 JSON（默认取产物目录里最新的一个；"
                        "显式指定可避免新增文件后静默改引别的 arm 口径）")
    p.add_argument("--pool-mode", default=None, help="标的池口径：e_covered（默认）/ all")
    p.add_argument("--alpha-weight", type=float, default=None, help="E（W×S）的合成权重")
    p.add_argument("--momentum-weight", type=float, default=None, help="ETF 自身动量分的合成权重")
    p.add_argument("--hold-buffer", type=int, default=None, help="迟滞缓冲（默认 config.HOLD_BUFFER）")
    p.add_argument("--entry-ma-window", type=int, default=None, help="入场破位过滤均线（0=关）")
    p.add_argument("--exit-ma-window", type=int, default=None, help="持仓强制退出均线（0=关）")
    p.add_argument("--nav", type=float, default=None, help="账户实际净值（配合 --nav-peak 判断回撤刹车）")
    p.add_argument("--nav-peak", type=float, default=None, help="账户历史最高净值")
    return p.parse_args()


def main() -> int:
    """报告生成入口：读产物 → 统计 → 落 `etf_analysis_report.json` + `.md`。

    退出码：0 = 报告已产出；非 0 = 必需输入缺失/退化 ⇒ 抛错（宁可不产出，不产出假结果）。
    """
    _ensure_hash_seed()
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / REPORT_JSON_NAME
    md_path = out_dir / REPORT_MD_NAME

    # ---- 1. 必需输入（读不到 ⇒ 抛中文异常，不静默失败）----
    scores_path = _require_input(args.scores or C.STOCK_SCORES_CSV, "个股分数 S（stock_scores.csv）")
    prices_path = _require_input(args.prices or C.ETF_PRICES_CSV, "ETF 行情（etf_prices.csv）")
    universe_path = _require_input(args.universe or C.ETF_UNIVERSE_CSV, "ETF 候选池（etf_universe.csv）")
    holdings_path = _require_input(args.holdings_csv or C.ETF_HOLDINGS_CSV, "ETF 持仓 W 原料（etf_holdings.csv）")

    prices = load_prices(prices_path)
    universe = load_universe(universe_path)
    holdings = load_holdings(holdings_path)
    scores = _load_scores(scores_path)
    if prices.empty:
        raise ValueError(f"ETF 行情 `{prices_path}` 没有任何数据 ⇒ 无法出报告")
    if universe.empty:
        raise ValueError(f"ETF 候选池 `{universe_path}` 没有任何数据 ⇒ 无法出报告")
    if holdings.empty:
        raise ValueError(f"ETF 持仓 `{holdings_path}` 没有任何数据 ⇒ E = W×S 算不出来 ⇒ 无法出报告")

    asof = pd.Timestamp(args.asof).normalize() if args.asof else pd.Timestamp(scores["date"].max()).normalize()
    if asof not in set(scores["date"]):
        raise ValueError(_scores_asof_missing_message(asof, scores, scores_path))
    print(f"[INFO] 信号日 asof = {asof.date()}｜候选池 {len(universe)} 行｜个股分数 {len(scores)} 行")

    # 🔴 数据新鲜度守卫（M-R，2026-11-05 05:2x）：行情够不到信号日 ⇒ 拒绝出报告。
    #    口径见 `_data_freshness_guard` 的 docstring（硬拦行情 / 只披露个股陈旧与 W 滞后）。
    data_freshness = _data_freshness_guard(
        prices, asof, prices_path, holdings=holdings, universe=universe
    )
    print(
        f"[INFO] 数据新鲜度：ETF 行情最新 {data_freshness['etf_prices_max_date']}"
        f"｜陈旧 ETF {data_freshness['n_etfs_stale']}/{data_freshness['n_etfs_total']} 只"
        f"｜W 快照 {data_freshness['w_snapshot_date']}"
        f"（滞后 {data_freshness['w_snapshot_lag_calendar_days']} 天，已量化披露）"
    )

    # ---- 2. 可选输入 ----
    stock_prices = None
    # 🔴 F1（2026-11-04 夜，专题档 §20.4）：记住**实际使用**的个股日线路径 ——
    #    未显式传 `--stock-prices` 时会回退到 `<run-root>/stock_prices.csv`，
    #    但下面 meta 的指纹取的是 `args.stock_prices` 原值 ⇒ 会记成「（未提供）/exists=false」
    #    ⇒ 元数据与实际用的文件不符（审计会误判「RankIC 无日线支撑」）。
    sp_path: Path | None = None
    # 🔴 IMPORTANT（2026-11-04 夜审）：`--stock-prices` 原本没有默认值 ⇒ 默认跑法下
    #    rank_ic 的 N=5 / N=20 永远是 null（"样本不足"），而 stock_prices.csv 就在
    #    运行目录里 ⇒ 默认指过去，存在即用（显式指定仍然优先）。
    _sp_arg = args.stock_prices
    if not _sp_arg:
        _sp_default = Path(C.ETF_RUN_ROOT) / "stock_prices.csv"
        if _sp_default.exists():
            _sp_arg = str(_sp_default)
    if _sp_arg:
        sp_path = _require_input(_sp_arg, "个股日线（--stock-prices）")
        stock_prices = pd.read_csv(sp_path, dtype={"instrument": str})
        stock_prices["date"] = pd.to_datetime(stock_prices["date"])
        stock_prices["instrument"] = stock_prices["instrument"].astype(str)
        stock_prices["close"] = pd.to_numeric(stock_prices["close"], errors="coerce")

    # 🔴 M-K（2026-11-05 夜）：点时刻快照（折溢价 / 换手率）—— 缺了不算数字，
    #    microstructure 会把该段标成「不可得」并给原因，**不许补 0**。
    spot = MS.load_spot(args.spot or C.ETF_SPOT_CSV)
    if spot is None:
        print(f"[WARN] etf_spot.csv 不可得（{args.spot or C.ETF_SPOT_CSV}）"
              " ⇒ 折溢价率/换手率无法实测，报告里明写「不可得」")

    calendar_path = Path(args.calendar) if args.calendar else _default_calendar_path()
    calendar = _read_calendar(calendar_path)
    # 🔴 qlib 日历不含未来 ⇒ 必须并上 akshare 的完整日历，否则推不出下一个自然月的首日
    extra_calendar_path = Path(args.trade_calendar) if args.trade_calendar else DEFAULT_TRADE_CALENDAR_CSV
    extra_calendar = _read_extra_calendar(extra_calendar_path)
    if not extra_calendar:
        print(f"[WARN] akshare 完整交易日历缺失：{extra_calendar_path} ⇒ 下一调仓节点可能推不出来"
              f"（用 ak env 跑 manu/etf_rotation/data/fetch_trade_calendar.py 生成）")
    else:
        calendar = _merge_calendars(calendar, extra_calendar)
        print(f"[INFO] 交易日历已合并：qlib {len(_read_calendar(calendar_path))} 天 + "
              f"akshare {len(extra_calendar)} 天 ⇒ 合计 {len(calendar)} 天"
              f"（末日 {calendar[-1].date()}）")
    # 部署节奏 = 20 个交易日（月频，铁律 14.9），不是信号日基础步长 10
    next_rebalance = _next_rebalance(calendar, asof, C.DEPLOYED_REBALANCE_EVERY_N_DAYS)

    account_available = True
    account_reason = None
    try:
        snapshot, snapshot_path = _load_account_snapshot(
            Path(args.account_snapshot) if args.account_snapshot else None
        )
    except FileNotFoundError as exc:
        # 🔴 IMPORTANT（2026-11-04 夜审）：规格要求「读不到必需输入 ⇒ 抛异常 + 中文错误」。
        #    旧代码在**默认路径**下降级成「空诊断 + WARN」仍然出报告 ⇒ §七 变成空的，
        #    交付件里会出现「0 只持仓诊断」却照样给出调仓清单。
        #    现在：显式指定或默认路径拿不到账户快照，一律抛中文异常（宁可不产出）。
        raise FileNotFoundError(
            f"账户快照读不到：{exc}｜默认路径 = {ACC.ACCOUNT_SNAPSHOT_CSV}；"
            "请先生成 manu/etf_rotation/data/account_snapshot.csv（或用 --account-snapshot 指定）"
        ) from exc

    total_assets = float(getattr(ACC, "TOTAL_ASSETS", 0.0) or 0.0)
    cash = float(getattr(ACC, "CASH", 0.0) or 0.0)
    if total_assets <= 0 and not snapshot.empty:
        total_assets = float(np.nansum(snapshot["market_value"].to_numpy(dtype=float)))
    current_weights: dict[str, float] = {}
    if not snapshot.empty and total_assets > 0:
        for _, row in snapshot.iterrows():
            mv = float(row["market_value"])
            if np.isfinite(mv):
                current_weights[str(row["code"])] = mv / total_assets

    # ---- 3. 个股分数横截面统计 + 退化守卫 ----
    score_stats, warnings = _stock_score_stats(scores, asof)
    for w in warnings:
        print(w)

    # ---- 4. 重放过滤链（分析侧）+ 调用引擎做权威决策 ----
    nav_state = None
    if args.nav is not None and args.nav_peak is not None:
        nav = float(args.nav)
        peak = float(args.nav_peak)
        dd = nav / peak - 1.0 if peak > 0 else 0.0
        nav_state = {
            "nav": nav,
            "peak": peak,
            "in_brake": dd <= -C.DRAWDOWN_BRAKE_THRESHOLD,
            "cooldown_left": 0,
        }
    chain = _rebuild_chain(
        asof=asof,
        prices=prices,
        holdings=holdings,
        scores=scores,
        universe=universe,
        current_holdings=current_weights,
        alpha_weight=args.alpha_weight,
        momentum_weight=args.momentum_weight,
        hold_buffer=args.hold_buffer,
        pool_mode=args.pool_mode,
        entry_ma_window=args.entry_ma_window,
        exit_ma_window=args.exit_ma_window,
        brake_mode=bool(nav_state and nav_state.get("in_brake")),
    )
    portfolio = build_portfolio(
        asof=asof,
        prices=prices,
        holdings=holdings,
        scores=scores,
        universe=universe,
        current_holdings=current_weights,
        alpha_weight=args.alpha_weight,
        momentum_weight=args.momentum_weight,
        hold_buffer=args.hold_buffer,
        pool_mode=args.pool_mode,
        nav_state=nav_state,
        entry_ma_window=args.entry_ma_window,
        exit_ma_window=args.exit_ma_window,
    )
    engine_selected = sorted(str(h["code"]) for h in portfolio["holdings"])
    if engine_selected != chain["selected_codes"]:
        raise RuntimeError(
            "分析侧重放的过滤链与引擎决策不一致 ⇒ 报告不可信，拒绝出报告。\n"
            f"  引擎 build_portfolio 选出：{engine_selected}\n"
            f"  分析侧重放选出：{chain['selected_codes']}\n"
            "  （engine/portfolio.py 的决策链若已变更，请同步更新 analyze.py）"
        )
    print(f"[INFO] 过滤链重放与引擎决策一致：入选 {len(engine_selected)} 只 {engine_selected}")

    # ---- 5. 各段落 ----
    rank_ic = _rank_ic(scores, asof, stock_prices)
    n_constituents = {str(k): int(v) for k, v in holdings.groupby("etf_code").size().items()}
    reasons = _reason_map(chain["stages"])
    candidates = _build_candidates(chain["pool"], int(args.top_n), n_constituents, reasons)
    holdings_diag, actions = _diagnose_holdings(snapshot, chain, universe, total_assets, prices)

    # E = W×S 的口径与快照日
    if "fetch_date" in holdings.columns:
        snap_dates = [str(d) for d in holdings["fetch_date"].dropna().unique()]
        w_snapshot_date = max(snap_dates) if snap_dates else None
    else:
        w_snapshot_date = datetime.fromtimestamp(holdings_path.stat().st_mtime).date().isoformat()
    alpha = chain["alpha"]
    etf_rows: list[dict] = []
    pool_codes = {str(c) for c in chain["pool"]["code"]}
    selected_codes = set(chain["selected_codes"])
    seen_codes: set[str] = set()
    #: 候选池（过了流动性但可能后面被别的网淘汰）的每一只 ETF 都要在报告里出现
    #: ⇒ 「算不出 E 的品种」能被点名（rules §十四·12 要求报告必须点名）
    for _, r in chain["lookup"].iterrows():
        if not bool(r.get("pass_liquidity", False)):
            continue
        code = str(r["code"])
        seen_codes.add(code)
        etf_rows.append(
            {
                "code": code,
                "name": str(r.get("name", "")),
                "e_score": _clean(r.get("alpha_score")),
                "coverage": _clean(r.get("coverage")),
                "coverage_ok": bool(r.get("coverage_ok", False)),
                "n_constituents": n_constituents.get(code, 0),
                "n_constituents_scored": _clean(r.get("n_stocks_scored")),
                "weight_scored_pct": None,
                "in_candidate_pool": code in pool_codes,
                "selected": code in selected_codes,
            }
        )
    #: 有 W 记录但没进候选池的 ETF（流动性等先被淘汰）也留一行，便于对照
    for _, a in alpha.iterrows():
        code = str(a["etf_code"])
        if code in seen_codes:
            continue
        etf_rows.append(
            {
                "code": code,
                "name": str(a.get("etf_name", "")),
                "e_score": _clean(a.get("alpha_score")),
                "coverage": _clean(a.get("coverage")),
                "coverage_ok": bool(a.get("coverage_ok", False)),
                "n_constituents": n_constituents.get(code, 0),
                "n_constituents_scored": _clean(a.get("n_stocks_scored")),
                "weight_scored_pct": _clean(a.get("weight_scored")),
                "in_candidate_pool": False,
                "selected": code in selected_codes,
            }
        )
    ranked = sorted(
        [r for r in etf_rows if r["e_score"] is not None],
        key=lambda r: (-float(r["e_score"]), r["code"]),
    )
    for i, r in enumerate(ranked, 1):
        r["e_rank"] = i
    for r in etf_rows:
        r.setdefault("e_rank", None)
    etf_rows.sort(key=lambda r: (r["e_rank"] is None, r["e_rank"] or 0, r["code"]))
    pool_rows = [r for r in etf_rows if r["in_candidate_pool"]]
    etf_scores = {
        "definition": "E = W × S：W = ETF 成分股权重快照，S = 个股层分数的截面 z-score（复用 engine/weights.map_scores_to_etfs）",
        "w_snapshot_date": w_snapshot_date,
        "w_source": str(holdings_path),
        "min_coverage": C.MIN_E_COVERAGE,
        "n_etfs_with_e": int(sum(1 for r in pool_rows if r["coverage_ok"])),
        "n_etfs_without_e": int(sum(1 for r in pool_rows if not r["coverage_ok"])),
        "n_etfs_no_w_record": int(
            sum(1 for r in pool_rows if r["coverage"] is None)
        ),
        "n_candidate_pool_total": len(pool_rows),
        "note": (
            "coverage = 有分数的成分股权重 / 总权重；coverage 不足 ⇒ E 不可信（不入池）；"
            "coverage 为空 ⇒ 该 ETF 在 W 里没有 A 股成分记录 ⇒ E 根本算不出来"
        ),
        "rows": etf_rows,
    }

    # 过滤网逐道计数
    stage_by_id = {st["stage"]: st for st in chain["stages"]}
    joint = stage_by_id["filter_2_3_dedup_and_theme"]
    per_net = {
        "filter_net_1_liquidity": {
            "title": "过滤网 1 · 流动性",
            "n_in": stage_by_id["filter_1_liquidity"]["n_in"],
            "n_out": stage_by_id["filter_1_liquidity"]["n_out"],
            "n_eliminated": stage_by_id["filter_1_liquidity"]["n_eliminated"],
            "eliminated": stage_by_id["filter_1_liquidity"]["eliminated"],
        },
        "filter_net_2_dedup": {
            "title": "过滤网 2 · 同质化去重",
            "n_in": joint["n_in"],
            # 🔴 IMPORTANT（2026-11-04 夜审）：2/3 号网在引擎里是**同一次贪心**
            #    ⇒ 它们的 n_out 不是漏斗口径（两网都从同一个 n_in 起算、互不串联），
            #    报 26/25 会和 stages 的 1664→289→203→31→5 打架。
            #    ⇒ 改名 n_not_eliminated_by_this_net，并在 attribution_note 里写明。
            "n_not_eliminated_by_this_net": joint["n_in"] - len(chain["dedup_blocked"]),
            "n_out": joint["n_in"] - len(chain["dedup_blocked"]),
            "n_eliminated": len(chain["dedup_blocked"]),
            "eliminated": chain["dedup_blocked"],
        },
        "filter_net_3_theme": {
            "title": "过滤网 3 · 主题多样性",
            "n_in": joint["n_in"],
            "n_not_eliminated_by_this_net": joint["n_in"] - len(chain["theme_blocked"]),
            "n_out": joint["n_in"] - len(chain["theme_blocked"]),
            "n_eliminated": len(chain["theme_blocked"]),
            "eliminated": chain["theme_blocked"],
        },
    }
    _px_codes = set(prices["code"].astype(str).unique()) if len(prices) else set()
    _uni_codes = set(universe["code"].astype(str).unique())
    _n_with_px = len(_uni_codes & _px_codes)
    _n_uni = int(len(universe))
    filter_stats = {
        "n_universe_rows": _n_uni,
        "n_with_price_data": int(_n_with_px),
        "n_without_price_data": int(len(_uni_codes) - _n_with_px),
        "price_coverage_pct": round(100.0 * _n_with_px / _n_uni, 2) if _n_uni else None,
        "n_pass_liquidity": stage_by_id["filter_1_liquidity"]["n_out"],
        "n_candidate_pool": int(len(chain["pool"])),
        "n_selected": len(chain["selected_codes"]),
        "pool_mode": chain["pool_mode"],
        "stages": chain["stages"],
        "per_net": per_net,
        "selected": [
            {
                "code": str(h["code"]),
                "name": str(h["name"]),
                "final_score": h["final_score"],
                "e_score": h["alpha_score"],
                "target_weight": h["target_weight"],
            }
            for h in portfolio["holdings"]
        ],
        "relaxed_absolute_momentum_filter": bool(portfolio.get("relaxed_absolute_momentum_filter")),
        "defensive_mode": bool(portfolio.get("defensive_mode")),
        "brake_mode": bool(portfolio.get("brake_mode")),
        "attribution_note": joint["note"],
    }

    # 建议（买 / 卖 / 持）
    target_weights = {str(h["code"]): float(h["target_weight"]) for h in portfolio["holdings"]}
    rec_actions: list[dict] = []
    for code in sorted(set(target_weights) | set(current_weights)):
        tgt = target_weights.get(code, 0.0)
        cur = float(current_weights.get(code, 0.0))
        delta = tgt - cur
        action = "买入" if delta > 1e-6 else ("卖出" if delta < -1e-6 else "持有")
        name = ""
        for h in portfolio["holdings"]:
            if str(h["code"]) == code:
                name = str(h["name"])
                break
        if not name:
            hit = universe[universe["code"].astype(str) == code]
            name = str(hit.iloc[0]["name"]) if len(hit) else code
        rec_actions.append(
            {
                "code": code,
                "name": name,
                "action": action,
                "target_weight": tgt,
                "current_weight": cur,
                "delta_weight": delta,
                "amount": round(delta * total_assets, 2) if total_assets > 0 else None,
                "reason": reasons.get(code, {}).get("reason", "入选本期组合"),
            }
        )
    recommendation = {
        "n_selected": int(portfolio["n_selected"]),
        "cash_weight": portfolio["cash_weight"],
        "weights_used": portfolio["weights_used"],
        "hold_buffer": portfolio["hold_buffer"],
        "actions": rec_actions,
        "holdings_diagnosis_actions": actions,
        "note": "动作基于账户快照权重与引擎目标权重之差；金额按账户总资产折算（口径见 meta）",
    }

    # 风险披露
    backtest = _load_backtest(out_dir, args.backtest)
    # 🔴 回测口径真值随报告一起带走（数字 + 区间 + 口径）；缺产物 ⇒ null + 告警，不编数字
    backtest_summary = _backtest_summary(backtest)
    if backtest_summary is None:
        warnings.append(
            "回测产物不可得（未找到 etf_rotation_backtest*.json）⇒ 报告**不引用任何回测数字**"
            "（meta.backtest_summary = null，MD「八之二」节标注「不可得」）"
        )
    # ---- ETF 微观结构实测（折溢价 / 流动性分层 / 跟踪误差代理）----
    # 🔴 M-K：让「未建模」这条风险披露带**实测数字**，而不是只有结论。
    #    样本 = 报告 top-N 候选（真正可能被下单的那一批），口径全部写在输出里。
    micro = MS.measure(
        [str(c.get("code", "")).zfill(6) for c in candidates if c.get("code")],
        prices=prices,
        universe=universe,
        holdings=holdings,
        spot=spot,
        stock_prices=stock_prices,
        asof=asof,
        liquidity_threshold=float(C.MIN_AVG_AMOUNT_20D),
    )
    prem_all = (micro.get("premium_discount") or {})
    te_sum = (micro.get("tracking_error_proxy") or {})
    print(f"[INFO] ETF 微观结构：候选 {micro['sample']['n_candidates']} 只｜"
          f"折溢价率可用={prem_all.get('available')}｜"
          f"跟踪误差代理可算={te_sum.get('n_computable')}/{te_sum.get('n_candidates')}")

    # ---- 前视审计（M-L）：无未来函数台账 + W 快照前视的 E 上界 + 排名稳健性 ----
    # 🔴 本链唯一**不可修**的前视：W 成分快照日晚于 asof（实测 2026-11-04 > 2026-11-03）。
    #    akshare 的 fund_etf_holdings_tx / fund_etf_pcf 取不到历史 PCF ⇒ asof 当日真实成分集
    #    **不可观测** ⇒ 只能量化披露，不能靠改代码消除。
    la_codes = sorted({str(c.get("code", "")).zfill(6) for c in candidates if c.get("code")})
    la_bounds = LA.w_snapshot_lookahead_bound(holdings, scores, asof, codes=la_codes)
    la_flip = LA.rank_flip_robustness(chain["pool"], la_bounds)
    la_inputs = LA.audit_input_dates(
        asof,
        prices=prices,
        universe=universe,
        holdings=holdings,
        scores=scores,
        stock_prices=stock_prices,
        backtest_window=(
            ((backtest_summary or {}).get("eval_window") or {}).get("start"),
            ((backtest_summary or {}).get("eval_window") or {}).get("end"),
        ) if backtest_summary else None,
        calendar_max_date=calendar[-1] if calendar else None,
    )
    _snap_dates: list[str] = []
    if "fetch_date" in holdings.columns:
        _snap_dates = sorted({
            str(d)[:10]
            for d in holdings["fetch_date"].dropna().astype(str).unique().tolist()
        })
    _gap = None
    if w_snapshot_date:
        _gap = int((pd.Timestamp(w_snapshot_date) - pd.Timestamp(asof)).days)
    _max_real = (
        float(la_bounds["delta_e_small_weight"].max())
        if len(la_bounds) and la_bounds["delta_e_small_weight"].notna().any() else None
    )
    _max_ext = (
        float(la_bounds["delta_e_extreme"].max())
        if len(la_bounds) and la_bounds["delta_e_extreme"].notna().any() else None
    )
    lookahead = {
        "asof": asof.date().isoformat(),
        "inputs": la_inputs,
        "w_snapshot": {
            "snapshot_date": w_snapshot_date,
            "asof": asof.date().isoformat(),
            "gap_days": _gap,
            "n_snapshot_dates": len(_snap_dates),
            "snapshot_dates": _snap_dates,
            "unobservable_reason": (
                "akshare 的 fund_etf_holdings_tx / fund_etf_pcf 只给当前快照、"
                "取不到历史 PCF ⇒ asof 当日的真实成分集**不可观测**"
            ),
            "fixable": False,
        },
        "e_bound": {
            "definition": (
                "E = Σ w_i·z_i / Σ w_i（i ∈ 已打分成分股）；若 asof 当日真实成分集与快照差一只 j，"
                "则 |ΔE| = |w_j·(z_j − E)| / (Σw − w_j)；「现实档」只在权重 ≤ "
                f"{LA.DEFAULT_SMALL_WEIGHT_CAP}% 的成分里取最大，「极端档」含最大权重成分股"
            ),
            "small_weight_cap": LA.DEFAULT_SMALL_WEIGHT_CAP,
            "n_etfs": int(len(la_bounds)),
            "per_etf": la_bounds.to_dict("records") if len(la_bounds) else [],
            "max_delta_e_realistic": _max_real,
            "max_delta_e_extreme": _max_ext,
            "pool_sigma_e": la_flip.get("sigma_e"),
        },
        "flip_test": la_flip,
    }
    print(f"[INFO] 前视审计：决策前视 {la_inputs['n_look_ahead']} 项（W 快照 +{_gap} 天）"
          f"｜E 上界 现实 ≤ {_num(_max_real, 4)} / 极端 ≤ {_num(_max_ext, 4)}")

    risk_disclosure = _risk_disclosure(
        asof=asof,
        chain={**chain, "w_snapshot_date": w_snapshot_date},
        n_etfs_no_e=etf_scores["n_etfs_without_e"],
        n_universe_rows=int(len(universe)),
        n_pool=int(len(chain["pool"])),
        holdings=holdings_diag,
        total_assets=total_assets,
        cash=cash,
        backtest=backtest,
        score_stats=score_stats,
        rank_ic=rank_ic,
        micro=micro,
        lookahead=lookahead,
    )
    if not risk_disclosure:
        raise RuntimeError("风险披露为空 ⇒ 违反「禁止空数组」，拒绝出报告")

    meta = {
        "asof": asof.date().isoformat(),
        "generated_at": _now_iso(),
        "host": platform.node(),
        "python_version": platform.python_version(),
        "script": str(Path(__file__).resolve()),
        "hash_seed": os.environ.get("PYTHONHASHSEED"),
        # 🔴 M-R（2026-11-05 05:2x）：输入数据的新鲜度（够不够得到信号日）。
        #    不进 TA 上下文（build_ta_context 只读 meta.asof）⇒ 加它**不会**断 sha1 链。
        "data_freshness": data_freshness,
        "inputs": {
            "stock_scores": _fingerprint(scores_path),
            "etf_prices": _fingerprint(prices_path),
            "etf_universe": _fingerprint(universe_path),
            "etf_holdings": _fingerprint(holdings_path),
            "account_snapshot": _fingerprint(snapshot_path) if snapshot_path else
            {"path": str(args.account_snapshot or "（未提供）"), "exists": False},
            "stock_prices": _fingerprint(sp_path) if sp_path else
            {"path": "（未提供）", "exists": False},
            "qlib_calendar": _fingerprint(calendar_path),
            "akshare_trade_calendar": _fingerprint(extra_calendar_path),
            "backtest_report": _fingerprint(backtest["path"]) if backtest else
            {"path": "（未找到）", "exists": False},
        },
        "params": {
            "pool_mode": chain["pool_mode"],
            "alpha_weight": chain["weights_used"]["alpha"],
            "momentum_weight": chain["weights_used"]["momentum"],
            "target_n_holdings": C.TARGET_N_HOLDINGS,
            "min_n_holdings": C.MIN_N_HOLDINGS,
            "hold_buffer": chain["hold_buffer"],
            "entry_ma_window": C.ENTRY_MA_FILTER_WINDOW if args.entry_ma_window is None else args.entry_ma_window,
            "exit_ma_window": C.EXIT_MA_FILTER_WINDOW if args.exit_ma_window is None else args.exit_ma_window,
            "min_e_coverage": C.MIN_E_COVERAGE,
            "min_avg_amount_20d": C.MIN_AVG_AMOUNT_20D,
            "min_float_mcap": C.MIN_FLOAT_MCAP,
            "min_history_days": C.MIN_HISTORY_DAYS,
            "max_per_theme": C.MAX_PER_THEME,
            "absolute_momentum_threshold": C.ABSOLUTE_MOMENTUM_THRESHOLD,
            "momentum_weights": {str(k): v for k, v in C.MOMENTUM_WEIGHTS.items()},
            "rebalance_every_n_days": C.DEPLOYED_REBALANCE_EVERY_N_DAYS,
            "signal_stride_base_n_days": C.REBALANCE_EVERY_N_DAYS,
            "signal_stride_multiplier": C.REBALANCE_STRIDE_MULTIPLIER,
            "execution_lag_days": C.EXECUTION_LAG_DAYS,
            "commission_bp": C.ETF_COMMISSION * 10000,
            "slippage_bp": C.ETF_SLIPPAGE * 10000,
            "commission_stress_bp": C.ETF_COMMISSION_STRESS * 10000,
            "slippage_stress_bp": C.ETF_SLIPPAGE_STRESS * 10000,
            "benchmark": C.ETF_BENCHMARK,
            "top_n": int(args.top_n),
            "ic_horizons": list(IC_HORIZONS),
            "ic_horizons_note": "config.py 由其他 agent 并行维护 ⇒ 常量就地定义于 analyze.py",
            "unique_value_warn_threshold": UNIQUE_VALUE_WARN_THRESHOLD,
            "min_ic_cross_sections": MIN_IC_CROSS_SECTIONS,
            "account_total_assets": total_assets,
            "account_cash": cash,
            "account_target_positions": int(getattr(ACC, "TARGET_POSITIONS", C.TARGET_N_HOLDINGS)),
            "account_target_position_weight": float(
                getattr(ACC, "TARGET_POSITION_WEIGHT", 1.0 / C.TARGET_N_HOLDINGS)
            ),
            "nav_state": nav_state,
        },
        # 🔴 回测口径真值（唯一可引用的数字来源；见 §18.4 专题档）
        "backtest_summary": backtest_summary,
    }

    report = {
        "meta": meta,
        "stock_scores": score_stats,
        "rank_ic": rank_ic,
        "etf_scores": etf_scores,
        "filter_stats": filter_stats,
        "candidates": candidates,
        "current_holdings_diagnosis": {
            "available": account_available,
            "reason": account_reason,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "total_assets": total_assets,
            "cash": cash,
            "n_holdings": len(holdings_diag),
            "holdings": holdings_diag,
        },
        "next_rebalance": {**next_rebalance, "calendar_path": str(calendar_path)},
        # 🔴 M-K：微观结构实测随报告一起带走（数字 + 区间 + 口径），不只留在文案里
        "etf_microstructure": micro,
        # 🔴 M-L：前视审计随报告一起带走（输入日期台账 + W 快照前视的 E 上界 + 排名稳健性）
        "lookahead_audit": lookahead,
        "risk_disclosure": risk_disclosure,
        "recommendation": recommendation,
        "warnings": warnings,
        "disclaimer": (
            "本报告由个股层 qlib 打分（S）经矩阵映射（E = W×S）与规则过滤自动生成，"
            "**不构成投资建议**，也不保证任何收益；历史统计不代表未来表现，"
            "使用者须自行判断并自行承担全部风险。"
        ),
    }

    json_path.write_text(
        json.dumps(_clean(report), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    print("=" * 78)
    print(f"ETF 分析报告 · 信号日 {asof.date()}")
    print(f"个股分数 {score_stats['n_stocks']} 只 / 唯一值 {score_stats['unique_values']} / "
          f"退化检查{'通过' if score_stats['degenerate_check']['passed'] else '未通过'}")
    print(f"候选池 {filter_stats['n_candidate_pool']} 只 ⇒ 入选 {filter_stats['n_selected']} 只 "
          f"{chain['selected_codes']}")
    print(f"下一个调仓节点 {next_rebalance['target_date']}（还有 "
          f"{next_rebalance['trading_days_until']} 个交易日）")
    print(f"风险披露 {len(risk_disclosure)} 条")
    print("=" * 78)
    print(f"已写出 {json_path}")
    print(f"已写出 {md_path}")
    print("⚠️ 本报告是模型打分与规则筛选的输出，**不构成投资建议**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
