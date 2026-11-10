#!/usr/bin/env python
"""数据新鲜度预检（M-R，2026-11-05 05:2x）：**在花一小时打分之前**先失败。

【为什么要有这一步】
一键流水线**只刷新个股分数**（`stock_scores.csv`），**从不刷新** ETF 行情 / 候选池 / W 快照
⇒ 到下一个调仓节点时，这三份输入很可能**还停在上一轮**。
而特征层 `engine/features.compute_etf_features` 的口径是「按 `date <= asof` 截断后取最后一条」
⇒ **行情够不到信号日时**，它会拿**更早的收盘价**算 MA / 动量，却把结果标成「信号日」的特征
⇒ **静默降级 ⇒ 交付件是一份假报告**。

🔴 本脚本是「**前置**版」守卫（`analyze.py::_data_freshness_guard` 是「**权威**版」）：
前者让流水线**早失败**（省掉一小时必然被拦下的打分），后者保证报告**本身**不会带病产出。

【判定口径】
* **ETF 行情**：最新日必须 **>= 信号日**（硬）⇒ 不满足 ⇒ 退出码 1。
* **ETF 行情（信号日当天）**：当天至少要有一只 ETF 的 bar（硬，防「文件够新、当天缺数据」）。
* **输入侧完整性**（M-S1 新增，硬）：池内标的缺行情 / 行情重复行 / 池内历史跨度中位数不足
  ⇒ 详见 `data/refresh_policy.py::check_dataset_integrity`（I1 / I2 / I3 硬拦；I3b / I4 / I5 只披露）。
* **候选池 / W 快照**：只**披露**滞后天数（份额披露天然滞后，危害由前视审计量化）⇒ 不拦；
  W 快照**同时给最早 / 最晚日期**（只看最晚会**低估**滞后，混合快照日期是刷新部分失败的典型症状）。
* **个股层(S)可达性**（M-S2 新增，**硬拦**）：信号日必须
  ① 落在**打分日栅格**里（相位 = 每 N 个交易日取一个 + 末日例外），且
  ② **qlib 数据集覆盖得到**（`calendars/day.txt` 的末日 ≥ 信号日）。
  🔴 实测（2026-11-05 06:0x）：下一调仓节点 **2026-12-01** **两条都不满足**
  —— qlib 数据集只到 **2026-11-03**，且 12-01 **不在打分日栅格**上
  （相位日是 11-04 / 11-18 / **12-02** / 12-16 / 12-30）
  ⇒ 没有这条预检，流水线会**白跑一小时打分**才在 analyze 阶段报错。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/data/preflight_freshness.py \
        [--asof 2026-12-01] [--run-root <落点根>/runs]

【退出码】0 = 全部达标；1 = 有硬拦项不达标（**流水线必须 die**）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_policy import check_dataset_integrity  # noqa: E402
import signal_policy as SP  # noqa: E402


def _max_date(path: Path, col: str) -> pd.Timestamp | None:
    """读某列的最大日期；文件缺失 / 列不存在 / 全空 ⇒ 返回 None（不编数字）。"""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, usecols=[col])
    except Exception:
        return None
    vals = pd.to_datetime(df[col], errors="coerce").dropna()
    if vals.empty:
        return None
    return vals.max().normalize()


def _qlib_calendar_path() -> Path:
    """qlib 数据集日历路径（`calendars/day.txt`）—— 从**打分器实际用的** PROVIDER_URI 推导。

    🔴 这是**数据集覆盖末日**的权威来源：个股行情只到最后一个有行情的交易日
    ⇒ 想打分到更晚的日期，必须**重建 qlib 数据集**（重活，需用户当轮授权）。

    🔴 M-S4 审计订正（2026-11-05 08:0x）：原来读 ``C.STOCK.PROVIDER_URI``（个股线），
       而**打分器 `qlib_scores.init_qlib()` 用的是 `C.PROVIDER_URI`**（本线自己的数据集）
       ⇒ 两者一旦不同（数据集切换 / 个股线换盘），守卫就会拿**另一个数据集**的日历
       判「数据集覆盖到哪」⇒ 口径漂移。⇒ 改成**以打分器的 PROVIDER_URI 为准**，
       个股线的那个只作兜底。
    """
    base = getattr(C, "PROVIDER_URI", None) or getattr(C.STOCK, "PROVIDER_URI", None)
    if base:
        return Path(str(base)) / "calendars" / "day.txt"
    return Path(str(getattr(C.STOCK, "DATASET_ROOT", ""))) / "cn_data" / "calendars" / "day.txt"


def _prospective_score_grid(
    cal_path: Path,
    asof: pd.Timestamp,
    every_n: int,
    start: str,
) -> list[str]:
    """算「本轮**将会**被打分的日期集合」（与 `qlib_scores.py` **同一套相位口径**）。

    口径复刻步骤（与 `qlib_scores.main()` 一一对应）：
      ① 日历查询窗 = ``[SCORE_START_TIME, max(SCORE_END_TIME, asof)]``；
      ② 相位栅格 = ``i % N == N-1``，再按 ``[TEST[0], asof]`` 截断；
      ③ **末日例外**：最后一个交易日必定入选。

    🔴 这就是 12-01 那天的关键：qlib 数据集只到 2026-11-03 ⇒
       查询窗再长也只会返回到 11-03 ⇒ **12-01 压根不会被打分**（0.7 秒就能判定）。

    🔴 M-S4 审计订正（2026-11-05 08:0x）：``every_n`` / ``start`` 原来**写死**在 config 里，
       而打分器用的是 **CLI 参数**（``--rebalance-every`` / ``--start``）
       ⇒ 一旦有人用 ``--rebalance-every 20`` 打分，守卫算的是 **10 日栅格**、
       打分器算的是 **20 日栅格** ⇒ 守卫可能放行一个打不出分的日子（或拦下打得出的）
       ⇒ **正是 M-S2 要消灭的失败模式**。⇒ 改成由调用方（CLI）传入，
       `run_pipeline.sh` 把**同一个值**同时传给打分器与预检 ⇒ 不可能漂移。
    """
    try:
        cal_dates = SP.read_calendar_dates(cal_path)
    except (FileNotFoundError, ValueError):
        return []
    lo = SP.parse_date(C.SCORE_START_TIME)
    hi = SP.parse_date(max(C.SCORE_END_TIME, asof.date().isoformat()))
    window = [d for d in cal_dates if (lo is None or d >= lo) and (hi is None or d <= hi)]
    return SP.score_grid_dates(
        window,
        every_n,
        start=start,
        end=asof.date().isoformat(),
    )


def _read_csv(path: Path) -> pd.DataFrame:
    """读一张输入表；文件不存在 / 读失败 ⇒ 返回空表（由体检逻辑决定后果，不在这里编数字）。"""
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype={"code": str, "etf_code": str})
    except Exception:
        return pd.DataFrame()


def main() -> int:
    """跑预检；退出码 0 = 达标，1 = 有硬拦项不达标。"""
    ap = argparse.ArgumentParser(description="ETF 轮动线 · 数据新鲜度预检")
    ap.add_argument("--asof", default=None, help="目标信号日（默认取候选池快照日）")
    ap.add_argument("--run-root", default=None, help="产物目录（用于读上下文）")
    ap.add_argument("--prices", default=None)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--holdings", default=None)
    ap.add_argument("--scores", default=None, help="个股分数 CSV（默认 config.STOCK_SCORES_CSV）")
    # 🔴 M-S4（2026-11-05 08:0x）：步长 / 起点必须与打分器**同源**
    #    （run_pipeline.sh 把同一个值同时传给 qlib_scores.py 与本脚本 ⇒ 不可能漂移）
    ap.add_argument("--rebalance-every", type=int, default=C.REBALANCE_EVERY_N_DAYS,
                    help="打分相位步长（交易日）；必须与 qlib_scores.py --rebalance-every 一致")
    ap.add_argument("--start", default=None,
                    help="打分起始日（默认 config.STOCK.TEST[0]）；必须与 qlib_scores.py --start 一致")
    args = ap.parse_args()

    prices_path = Path(args.prices or C.ETF_PRICES_CSV)
    universe_path = Path(args.universe or C.ETF_UNIVERSE_CSV)
    holdings_path = Path(args.holdings or C.ETF_HOLDINGS_CSV)
    scores_path = Path(args.scores or C.STOCK_SCORES_CSV)

    px_max = _max_date(prices_path, "date")
    u_date = _max_date(universe_path, "fetch_date")
    w_date = _max_date(holdings_path, "fetch_date")
    scores_max = _max_date(scores_path, "date")

    # 🔴 M-S2（2026-11-05 06:2x）：默认信号日改成 **个股分数末日**
    #   （原先用「候选池快照日」= 2026-11-04 ⇒ 比真正的信号日晚一天
    #     ⇒ 预检里 W 快照显示「与信号日同日」，而报告里写的是「晚于信号日 1 天」⇒ 两处口径打架）。
    #   ⇒ 与 analyze.py 的默认口径（scores max）对齐。
    asof = pd.Timestamp(args.asof).normalize() if args.asof else (scores_max or u_date)
    print("=" * 78)
    print("ETF 轮动线 · 数据新鲜度预检")
    print("=" * 78)
    if asof is None:
        print("🔴 目标信号日**无法确定**（既没给 --asof，候选池快照日也读不到）⇒ 拒绝继续")
        return 1
    print(f"目标信号日：{asof.date()}")
    rows: list[tuple[str, str, str, bool]] = []

    if px_max is None:
        rows.append(("ETF 行情", "读不到", "-", False))
    else:
        lag = int((asof - px_max).days)
        ok = lag <= 0
        rows.append((
            "ETF 行情", str(px_max.date()),
            f"落后 {lag} 天" + ("（数据比信号日还新）" if lag < 0 else ""),
            ok,
        ))

    def _lag_note(snapshot: pd.Timestamp) -> str:
        """滞后天数的人话写法（负数 = 快照比信号日**早**，别写成「滞后 −27 天」）。"""
        delta = int((snapshot - asof).days)
        if delta > 0:
            return f"晚于信号日 {delta} 天（只披露）"
        if delta < 0:
            return f"早于信号日 {-delta} 天（只披露）"
        return "与信号日同日（只披露）"

    if u_date is None:
        rows.append(("候选池快照", "读不到", "-", True))
    else:
        rows.append(("候选池快照", str(u_date.date()), _lag_note(u_date), True))

    if w_date is None:
        rows.append(("W 快照", "读不到", "-", True))
    else:
        rows.append(("W 快照", str(w_date.date()), _lag_note(w_date), True))

    for name, value, note, ok in rows:
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:<10s} 最新/快照 = {value:<12s} {note}")

    # ---- 个股层(S)可达性（M-S2 新增；**硬拦**）----
    print("-" * 78)
    print("个股层(S)信号日可达性（signal_policy.check_signal_feasibility）：")
    cal_path = _qlib_calendar_path()
    qlib_max = SP.calendar_max_date(cal_path)
    every_n = int(args.rebalance_every)
    grid_start = str(args.start or C.STOCK.TEST[0])
    grid = _prospective_score_grid(cal_path, asof, every_n, grid_start)
    findings = SP.check_signal_feasibility(
        asof=asof.date().isoformat(),
        score_grid=grid,
        qlib_calendar_max_date=qlib_max,
        every_n=every_n,
        scores_max_date=scores_max.date().isoformat() if scores_max is not None else None,
        prices_max_date=px_max.date().isoformat() if px_max is not None else None,
        deployed_grid=grid[:: C.REBALANCE_STRIDE_MULTIPLIER],
    )
    if not findings:
        print(f"  [OK  ] 信号日 {asof.date()} 在打分日栅格内，qlib 数据集覆盖得到"
              f"（覆盖末日 {qlib_max}）")
    for item in findings:
        tag = "FAIL" if item["level"] == SP.LEVEL_BLOCK else "INFO"
        print(f"  [{tag}] {item['id']:<4s} {item['detail']}")
    signal_blocks = [f for f in findings if f["level"] == SP.LEVEL_BLOCK]

    # ---- 输入侧完整性体检（M-S1 新增；硬拦项与「不适用」口径同 §十四.77）----
    integrity = check_dataset_integrity(
        _read_csv(prices_path), _read_csv(universe_path), _read_csv(holdings_path),
        target_date=asof.date().isoformat(),
    )
    if integrity:
        print("-" * 78)
        print("输入侧完整性体检（refresh_policy.check_dataset_integrity）：")
        for item in integrity:
            tag = "FAIL" if item["level"] == "block" else "INFO"
            print(f"  [{tag}] {item['id']:<5s} {item['detail']}")
            if item.get("codes"):
                print(f"          明细：{', '.join(str(c) for c in item['codes'][:20])}")
    integrity_blocks = [i for i in integrity if i["level"] == "block"]

    failed = [r for r in rows if not r[3]]
    if failed or integrity_blocks or signal_blocks:
        print("-" * 78)
        print("🔴 **预检不通过 ⇒ 流水线不许继续**（否则会产出「用旧价冒充信号日特征」的假报告）")
        print(f"   刷新 ETF 行情（akshare 环境；**绝对不要往 ql env 装 akshare**）：")
        print(f"     /home/huangzhe/anaconda3/envs/ak/bin/python "
              f"manu/etf_rotation/data/fetch_etf_prices.py --resume")
        print(f"     （`--end` **留空** = 自动解析为「今天之前（含）的最近一个交易日」；"
              f"**不要钉死日期**，钉死会让刷新变成静默空转）")
        print(f"   或把 --asof 改成行情已覆盖的日期（≤ {px_max.date() if px_max is not None else '?'}）")
        if integrity_blocks:
            print(f"   另有 {len(integrity_blocks)} 项**输入侧完整性**硬拦不达标：")
            for item in integrity_blocks:
                print(f"     · [{item['id']}] {item['detail']}")
        if signal_blocks:
            print(f"   另有 {len(signal_blocks)} 项**个股层(S)可达性**硬拦不达标"
                  f"（信号日 {asof.date()} 打不出分数 ⇒ 白跑）：")
            for item in signal_blocks:
                print(f"     · [{item['id']}] {item['detail']}")
        return 1

    print("-" * 78)
    print("✅ 预检通过（硬拦项：ETF 行情覆盖到信号日）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
