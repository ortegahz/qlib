#!/usr/bin/env python
"""数据新鲜度守卫回归探针（M-R，2026-11-05 05:2x）。

【为什么需要这道闸】
`analyze.py::_data_freshness_guard` 是「**宁可不产出，不产出假结果**」（§十四.5）在本线上的第一道闸。
特征层 `engine/features.compute_etf_features` 的口径是「按 `date <= asof` 截断后取最后一条」
⇒ **行情数据够不到信号日时**，它会拿**更早的收盘价**算 MA / 动量，却把结果标成「信号日」的特征
⇒ **静默降级** ⇒ 交付件会是一份「假报告」。
🔴 实测场景：下一调仓节点是 **2026-12-01**，而 `etf_prices.csv` 只到 **2026-11-04**
（且一键流水线**从不刷新**它）⇒ 没有这道闸就会产出一份「信号日 2026-12-01」的**假报告**。

【口径：硬拦 vs 只披露，必须分清】
* **硬拦①**：行情的最新日必须 **>= 信号日**；
* **硬拦②**：信号日**当天**至少要有一只 ETF 的 bar（防「文件够新、当天缺数据」的静默洞）；
* **只披露**：个别 ETF 陈旧（停牌 / 新上市 / 退市）—— 只计数 + 列明细，**不许**当成错误；
* **只披露**：W 快照滞后（份额披露天然滞后）—— 只报天数，危害由前视审计量化。

【🔴 本探针必须带真负例】
只测「正例 PASS」等于没测（§十四.36：断言写错 ⇒ 假阳性；负例没生效 ⇒ 假阴性）。
⇒ 这里构造**故意做坏**的行情，守卫**必须抛异常**；再构造「个别 ETF 陈旧」的行情，
  守卫**必须放行**且计数正确（不许把正常现象当错误拦下来）。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \
        manu/etf_rotation/probes/probe_data_freshness.py \
        [--run-root <落点根>/runs]

【副作用】只读正式产物，不修改；不写任何临时文件。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402  （落点常量单一事实源）
from analyze import _data_freshness_guard  # noqa: E402

#: 信号日（与正式产物的 asof 一致）
ASOF = pd.Timestamp("2026-11-03")
#: 假装的价格文件路径（只进异常文案，不落盘）
FAKE_PATH = Path("/tmp/_fake_etf_prices.csv")


class Checker:
    """收集断言结果（每条：id / 名称 / 是否通过 / 细节）。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str) -> None:
        """登记一条断言结果。"""
        self.items.append((check_id, name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, str, bool, str]]:
        """返回全部失败的断言。"""
        return [it for it in self.items if not it[2]]


def _mk_prices(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """造一份最小行情表（守卫只用到 date / code 两列）。"""
    df = pd.DataFrame(rows, columns=["date", "code"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _mk_holdings(fetch_date: str) -> pd.DataFrame:
    """造一份最小 W 快照表（守卫只用到 fetch_date 列）。"""
    return pd.DataFrame(
        [{"etf_code": "510300", "stock_code": "600519.SH", "weight_pct": 5.0, "fetch_date": fetch_date}]
    )


def _raises(fn, *args, **kwargs) -> tuple[bool, str]:
    """调用 fn；返回 (是否抛异常, 异常文案)。"""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 探针要的就是「有没有被拦下」
        return True, f"{type(exc).__name__}: {exc}"
    return False, ""


def run_checks(run_root: Path) -> list[tuple[str, str, bool, str]]:
    """跑全部断言，返回 [(id, 名称, 是否通过, 细节)]。"""
    checker = Checker()

    # ---------- F1 正例：行情覆盖到信号日 ⇒ 放行且字段自洽 ----------
    good = _mk_prices([
        ("2026-11-02", "510300"), ("2026-11-03", "510300"),
        ("2026-11-02", "511880"), ("2026-11-03", "511880"),
    ])
    out = _data_freshness_guard(good, ASOF, FAKE_PATH, holdings=_mk_holdings("2026-11-04"))
    checker.add(
        "F1", "正例：行情覆盖到信号日 ⇒ 放行且披露字段自洽",
        out["etf_prices_max_date"] >= out["signal_date"]
        and out["n_bars_at_signal_date"] == 2
        and out["n_etfs_stale"] == 0
        and out["coverage_at_signal_date_pct"] == 100.0,
        f"max_date={out['etf_prices_max_date']}｜bars@asof={out['n_bars_at_signal_date']}"
        f"｜陈旧={out['n_etfs_stale']}｜覆盖率={out['coverage_at_signal_date_pct']}%",
    )

    # ---------- F2 真负例①：行情整体落后 ⇒ 必须抛异常 ----------
    stale = _mk_prices([("2026-10-29", "510300"), ("2026-10-30", "510300")])
    raised, msg = _raises(_data_freshness_guard, stale, ASOF, FAKE_PATH)
    checker.add(
        "F2", "真负例①：行情最新日 < 信号日 ⇒ 守卫必须拒绝出报告",
        raised and "够不到信号日" in msg,
        f"抛异常={raised}｜文案含「够不到信号日」={'够不到信号日' in msg}",
    )

    # ---------- F3 真负例②：文件够新、但信号日当天无 bar ⇒ 必须抛异常 ----------
    gap = _mk_prices([
        ("2026-10-30", "510300"), ("2026-11-04", "510300"),
        ("2026-10-30", "511880"), ("2026-11-04", "511880"),
    ])
    raised, msg = _raises(_data_freshness_guard, gap, ASOF, FAKE_PATH)
    checker.add(
        "F3", "真负例②：信号日当天没有任何 bar（静默洞）⇒ 守卫必须拒绝出报告",
        raised and "没有任何 bar" in msg,
        f"抛异常={raised}｜文案含「没有任何 bar」={'没有任何 bar' in msg}",
    )

    # ---------- F4 只披露：个别 ETF 陈旧 ⇒ 放行，但必须计数 + 列明细 ----------
    one_stale = _mk_prices([
        ("2026-11-02", "510300"), ("2026-11-03", "510300"),
        ("2026-10-30", "511880"), ("2026-10-30", "599999"),
    ])
    out4 = _data_freshness_guard(one_stale, ASOF, FAKE_PATH)
    stale_codes = {x["code"] for x in out4["stale_etfs"]}
    checker.add(
        "F4", "只披露：个别 ETF 陈旧（停牌/新上市）⇒ 放行，且计数与明细正确",
        out4["n_etfs_stale"] == 2 and stale_codes == {"511880", "599999"},
        f"陈旧数={out4['n_etfs_stale']}（期望 2）｜明细代码={sorted(stale_codes)}",
    )

    # ---------- F5 只披露：W 快照晚于信号日 ⇒ 放行，但必须报滞后天数 ----------
    out5 = _data_freshness_guard(good, ASOF, FAKE_PATH, holdings=_mk_holdings("2026-11-04"))
    checker.add(
        "F5", "只披露：W 快照晚于信号日 ⇒ 放行，且滞后天数被记录",
        out5["w_snapshot_date"] == "2026-11-04" and out5["w_snapshot_lag_calendar_days"] == 1,
        f"w_snapshot={out5['w_snapshot_date']}｜滞后={out5['w_snapshot_lag_calendar_days']} 天（期望 1）",
    )

    # ---------- F6 正式产物：报告里必须带自洽的 data_freshness ----------
    report_path = run_root / "etf_analysis_report.json"
    if not report_path.exists():
        checker.add("F6", "正式报告含自洽的 meta.data_freshness", False, f"缺 {report_path}")
        checker.add("F7", "正式报告 MD 含「数据新鲜度」披露段", False, "报告不存在")
        return checker.items
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dfm = report.get("meta", {}).get("data_freshness") or {}
    rep_asof = str(report.get("meta", {}).get("asof", ""))
    checker.add(
        "F6", "正式报告含自洽的 meta.data_freshness",
        bool(dfm)
        and str(dfm.get("signal_date")) == rep_asof
        and str(dfm.get("etf_prices_max_date")) >= rep_asof
        and int(dfm.get("n_bars_at_signal_date", 0)) > 0
        and dfm.get("etf_prices_covers_signal_date") is True,
        f"asof={rep_asof}｜行情最新={dfm.get('etf_prices_max_date')}"
        f"｜bars@asof={dfm.get('n_bars_at_signal_date')}｜陈旧={dfm.get('n_etfs_stale')}",
    )

    # ---------- F7 正式产物：MD 必须有披露段 ----------
    md_path = run_root / "etf_analysis_report.md"
    md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    checker.add(
        "F7", "正式报告 MD 含「数据新鲜度」披露段",
        "数据新鲜度" in md and "够不够得到信号日" in md,
        f"含披露段={'数据新鲜度' in md}",
    )

    return checker.items


def main() -> int:
    """跑探针；退出码 0 = 全部断言通过，1 = 有断言失败。"""
    ap = argparse.ArgumentParser(description="数据新鲜度守卫回归探针")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    run_root = Path(args.run_root)
    items = run_checks(run_root)
    print("=" * 78)
    print("数据新鲜度守卫 · 回归探针（M-R）")
    print("=" * 78)
    for check_id, name, ok, detail in items:
        print(f"[{'PASS' if ok else 'FAIL'}] {check_id} {name}")
        print(f"        {detail}")
    failed = [it for it in items if not it[2]]
    print("-" * 78)
    print(f"合计 {len(items)} 项：PASS {len(items) - len(failed)} / FAIL {len(failed)}")
    if failed:
        print("❌ 守卫失效 ⇒ 流水线不许交付")
        return 1
    print("✅ 数据新鲜度守卫全绿（含 2 个真负例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
