"""`calendar_gate.py` / `fetch_data.py` 的 pytest 自检（**完全离线**：不联网、不调 akshare）。

【为什么必须离线】
每日流水线的取数会**真的**去 akshare 抓行情（子进程跑 ak env）⇒
测试里一旦误触发就是几百次外网请求 + IP 封禁风险 ⇒ 所有用例都走**临时文件**或 dry-run。

【覆盖清单（对应交付规格）】
1. `is_trading_day` 对**工作日** True、对**周末** False；
2. 日历文件**不存在** ⇒ 抛 `CalendarUnavailable`（**不是**返回 False）—— fail-closed 的核心断言；
3. 日历**为空** ⇒ 同样抛 `CalendarUnavailable`；
4. 非法日期串 ⇒ 抛 `ValueError`；
5. 日历覆盖不到目标日期 ⇒ 抛 `CalendarUnavailable`；
6. `resolve_mode`：`2026-12-01` ⇒ B；`2026-11-06` / `2026-11-30` / `2026-12-02` ⇒ A；
7. `is_silent_defense_window`：`2026-11-06` True、`2026-10-30` False、`2026-12-01` False；
8. `next_rebalance_node("2026-11-04")` ⇒ 节点日 2026-12-01、执行日 2026-12-02（**临时日历**，不依赖服务器）；
9. `trading_days_between` 左开右闭；
10. `fetch_etf_daily_bars(dry_run=True)` **不联网、不起子进程**；
11. `read_holdings_snapshot` 对临时 CSV 能算 min/max fetch_date 与 stale_days。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_DAILY_DIR = Path(__file__).resolve().parents[1]
if str(_DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILY_DIR))

import calendar_gate as CG  # noqa: E402
import fetch_data as FD  # noqa: E402

#: 临时日历的覆盖区间（含 2026-11 的整周 + 2026-12 的头几个交易日）
_CAL_START = date(2026, 11, 2)
_CAL_END = date(2026, 12, 4)


def _weekdays(start: date, end: date) -> list[str]:
    """区间内的**工作日**（周一~周五）⇒ 用来当临时交易日历。"""
    out: list[str] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


@pytest.fixture()
def calendar_csv(tmp_path: Path) -> Path:
    """临时交易日历 CSV（单列 `date`，与服务器 `trade_calendar.csv` 同格式）。"""
    path = tmp_path / "trade_calendar.csv"
    days = _weekdays(_CAL_START, _CAL_END)
    path.write_text("date\n" + "\n".join(days) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def empty_calendar_csv(tmp_path: Path) -> Path:
    """**空**日历 CSV（只有表头 ⇒ 解析不出任何日期）。"""
    path = tmp_path / "empty_calendar.csv"
    path.write_text("date\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 1. 交易日判定
def test_is_trading_day_weekday_and_weekend(calendar_csv: Path) -> None:
    """工作日 ⇒ True；周末 ⇒ False（**日历正常**时才可以返回 False）。"""
    assert CG.is_trading_day("2026-11-04", calendar_csv) is True
    assert CG.is_trading_day("2026-11-07", calendar_csv) is False  # 周六
    assert CG.is_trading_day("2026-11-08", calendar_csv) is False  # 周日


def test_is_trading_day_missing_calendar_raises(tmp_path: Path) -> None:
    """🔴 日历文件**不存在** ⇒ 抛 `CalendarUnavailable`，**绝不**返回 False。

    这是 fail-closed 的核心：返回 False 的语义是「今天休市 ⇒ 静默退出 0」，
    日历挂了却走这条分支 ⇒ 每个交易日都被静默跳过 ⇒ 一个月没有信号也没有告警。
    """
    missing = tmp_path / "not_exist.csv"
    with pytest.raises(CG.CalendarUnavailable):
        CG.is_trading_day("2026-11-04", missing)


def test_is_trading_day_empty_calendar_raises(empty_calendar_csv: Path) -> None:
    """日历**为空** ⇒ 同样抛 `CalendarUnavailable`（空列表会把每天都判成「非交易日」）。"""
    with pytest.raises(CG.CalendarUnavailable):
        CG.is_trading_day("2026-11-04", empty_calendar_csv)


def test_is_trading_day_bad_format_raises_value_error(calendar_csv: Path) -> None:
    """日期串非法 ⇒ 抛 `ValueError`（中文消息），不许静默当成「非交易日」。"""
    with pytest.raises(ValueError):
        CG.is_trading_day("2026-13-01", calendar_csv)
    with pytest.raises(ValueError):
        CG.is_trading_day("20261104", calendar_csv)


def test_is_trading_day_beyond_calendar_raises(calendar_csv: Path) -> None:
    """目标日晚于日历最后一天 ⇒ 抛 `CalendarUnavailable`（覆盖不到 ⇒ 拒绝判定）。"""
    with pytest.raises(CG.CalendarUnavailable):
        CG.is_trading_day("2026-12-31", calendar_csv)


# ---------------------------------------------------------------- 2. 模式分流
def test_resolve_mode() -> None:
    """只有锁死的信号日 `DC.NEXT_SIGNAL_DATE`（2026-12-01）走模式 B。"""
    assert CG.resolve_mode("2026-12-01") == CG.MODE_B
    assert CG.resolve_mode("2026-11-06") == CG.MODE_A
    assert CG.resolve_mode("2026-11-30") == CG.MODE_A
    assert CG.resolve_mode("2026-12-02") == CG.MODE_A


def test_is_silent_defense_window() -> None:
    """静默防守窗口（含端点）2026-11-06 ~ 2026-11-30。"""
    assert CG.is_silent_defense_window("2026-11-06") is True
    assert CG.is_silent_defense_window("2026-10-30") is False
    assert CG.is_silent_defense_window("2026-12-01") is False


def test_describe_mode_keys() -> None:
    """`describe_mode` 的键固定（下游 `main.py` / 邮件模板按它取值）。"""
    info = CG.describe_mode("2026-12-01")
    assert set(info) == {
        "date", "mode", "is_silent_defense", "is_signal_date", "execution_date", "reason",
    }
    assert info["mode"] == CG.MODE_B
    assert info["is_signal_date"] is True
    assert info["execution_date"] == "2026-12-02"
    assert info["reason"]


# ---------------------------------------------------------------- 3. 日期工具
def test_next_rebalance_node(calendar_csv: Path) -> None:
    """下一个自然月的**首个交易日** = 节点日；执行日 = 节点日 + 1 个交易日。"""
    node = CG.next_rebalance_node("2026-11-04", calendar_csv)
    assert node["target_date"] == "2026-12-01"
    assert node["signal_date"] == "2026-12-01"
    assert node["execution_date"] == "2026-12-02"
    assert node["trading_days_until"] > 0


def test_trading_days_between_is_left_open_right_closed(calendar_csv: Path) -> None:
    """区间口径是**左开右闭** `(start, end]`（与研究链持仓收益区间一致）。"""
    assert CG.trading_days_between("2026-11-04", "2026-11-06", calendar_csv) == [
        "2026-11-05",
        "2026-11-06",
    ]
    assert CG.count_trading_days("2026-11-04", "2026-11-06", calendar_csv) == 2


def test_next_and_prev_trading_day(calendar_csv: Path) -> None:
    """下一 / 上一交易日都**严格**跳过目标日本身（周末 ⇒ 跳到周一）。"""
    assert CG.next_trading_day("2026-11-06", calendar_csv) == "2026-11-09"
    assert CG.prev_trading_day("2026-11-09", calendar_csv) == "2026-11-06"


# ---------------------------------------------------------------- 4. fetch_data
def test_fetch_etf_daily_bars_dry_run_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 dry-run ⇒ **不联网、不起子进程**（任何 `subprocess.run` 调用都直接炸）。"""

    def _boom(*args, **kwargs):  # pragma: no cover - 被调用即测试失败
        raise AssertionError("dry-run 不许起子进程")

    monkeypatch.setattr(FD.subprocess, "run", _boom)
    info = FD.fetch_etf_daily_bars(dry_run=True)
    assert info["dry_run"] is True
    assert info["out_csv"] is None
    assert info["rows"] == 0
    assert info["codes_ok"] == []
    assert info["codes_failed"] == []


def test_fetch_data_module_never_imports_akshare() -> None:
    """🔴 父进程（ql env）**不许** import akshare（akshare 绝不能装进 ql env）。"""
    assert "akshare" not in sys.modules


def test_read_holdings_snapshot(tmp_path: Path) -> None:
    """W 快照**只读**：能算出 min/max fetch_date 与 stale_days（相对 asof）。"""
    path = tmp_path / "etf_holdings.csv"
    path.write_text(
        "etf_code,stock_code,weight_pct,source,fetch_date\n"
        "512800,600000,10.5,tx,2026-11-01\n"
        "512800,600016,9.5,tx,2026-11-01\n"
        "159915,300750,8.0,tx,2026-11-03\n",
        encoding="utf-8",
    )
    info = FD.read_holdings_snapshot(path, asof="2026-11-05")
    assert info["exists"] is True
    assert info["n_rows"] == 3
    assert info["n_codes"] == 2
    assert info["min_fetch_date"] == "2026-11-01"
    assert info["max_fetch_date"] == "2026-11-03"
    assert info["stale_days"] == 2
    assert info["stale"] is False
    assert info["codes"] == ["159915", "512800"]
    # 披露阈值 5 天：512800 的快照停在 11-01 ⇒ asof=11-08 时它已陈旧 7 天
    assert FD.holdings_needing_refresh(path, asof="2026-11-05") == []
    assert FD.holdings_needing_refresh(path, asof="2026-11-08") == ["512800"]


def test_write_daily_prices_and_merge(tmp_path: Path) -> None:
    """当日增量落盘 ⇒ 合并时**增量覆盖** canonical（同一 code+date 取新的）。"""
    import pandas as pd

    canonical = tmp_path / "etf_prices.csv"
    canonical.write_text(
        "date,code,open,high,low,close,volume,amount\n"
        "2026-11-04,512800,1.0,1.1,0.9,1.05,100,105\n",
        encoding="utf-8",
    )
    run_root = tmp_path / "daily" / "20261105"
    run_root.mkdir(parents=True)
    daily = pd.DataFrame(
        [
            {"code": "512800", "date": "2026-11-04", "open": 1.0, "high": 1.1,
             "low": 0.9, "close": 1.06, "volume": 200, "amount": 212, "pct_chg": 0.95},
            {"code": "512800", "date": "2026-11-05", "open": 1.06, "high": 1.2,
             "low": 1.05, "close": 1.18, "volume": 300, "amount": 354, "pct_chg": 11.3},
        ]
    )
    path = FD.write_daily_prices(daily, run_root, "2026-11-05")
    assert path.name == "etf_prices_daily_20261105.csv"
    merged = FD.load_effective_prices(asof="2026-11-05", run_root=run_root, canonical_csv=canonical)
    assert len(merged) == 2
    assert merged.loc[
        (merged["date"] == "2026-11-04"), "close"
    ].iloc[0] == 1.06  # 增量覆盖 canonical
    # 按 (code, date) 升序
    assert list(zip(merged["code"], merged["date"])) == [
        ("512800", "2026-11-04"), ("512800", "2026-11-05"),
    ]
