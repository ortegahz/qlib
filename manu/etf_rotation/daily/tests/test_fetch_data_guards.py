"""`drop_unfinalized_bars` 防污染守卫的回归测试（2026-11-10 真 bug）。

【为什么需要】
`drop_unfinalized_bars` 的职责 = 丢掉**还没定型**的那根 bar。
「还没定型」只可能发生在**今天** ⇒ 两个基准必须钉死：

1. **掩码锚 today**（不是 asof）：旧实现锚 asof + 只看墙钟
   ⇒ 当 asof 是**过去的交易日**（补跑 / 回溯）时，**早已收盘**的 bar 被当成盘中数据误丢
   ⇒ 行情底座比信号日差一天 ⇒ 下游 `_data_freshness_guard` fail-closed ⇒ 整轮白跑；
2. **时钟锚市场时区**（`DC.MARKET_TZ` = 东八区，不是机器本地时区）
   ⇒ 机器跑在 UTC 容器里时，07:00 UTC（= 15:00 CST，A 股**已收盘**）
   仍被判成「未收盘」⇒ 同一类事故原样复现。

【真实事故（服务器实跑）】
asof=2026-11-09、墙钟 2026-11-10 10:37（< 15:05）
⇒ 11-09 已收盘的 bar 被丢 ⇒ 底座只到 2026-11-06
⇒ `analyze.py::_data_freshness_guard` 报「行情数据够不到信号日」⇒ 拒绝出报告。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import daily_config as DC  # noqa: E402
import fetch_data as FD  # noqa: E402

#: 东八区（市场时区）
CST = timezone(timedelta(hours=8))


class _FakeDatetime:
    """替换 `FD.datetime`，让「现在」可控（其余属性照常转发）。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment
        #: 记录生产代码传进来的 tz ⇒ 用来断言「时钟锚市场时区」
        self.requested_tz: object = None

    def now(self, tz=None):  # noqa: ANN001
        self.requested_tz = tz
        if tz is None:
            return self._moment
        return self._moment.astimezone(tz)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(datetime, name)


def _frame(*dates: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["510300"] * len(dates),
            "date": list(dates),
            "close": [1.0] * len(dates),
        }
    )


@pytest.fixture()
def patch_now(monkeypatch):
    def _apply(moment: datetime) -> _FakeDatetime:
        fake = _FakeDatetime(moment)
        monkeypatch.setattr(FD, "datetime", fake)
        return fake

    return _apply


def test_past_asof_keeps_finalized_bar(patch_now) -> None:
    """🔴 核心回归：asof 是**过去的交易日** ⇒ 已收盘的 bar **必须保留**。"""
    patch_now(datetime(2026, 11, 10, 10, 37, tzinfo=CST))
    df = _frame("2026-11-05", "2026-11-06", "2026-11-09")

    kept, note = FD.drop_unfinalized_bars(df, "2026-11-09")

    assert len(kept) == 3
    assert "2026-11-09" in set(kept["date"])
    assert note is None


def test_today_asof_before_finalize_drops_bar(patch_now) -> None:
    """asof 就是**今天**且尚未过 15:05 ⇒ 今天的盘中 bar 仍要丢（防污染语义不变）。"""
    patch_now(datetime(2026, 11, 10, 10, 37, tzinfo=CST))
    df = _frame("2026-11-09", "2026-11-10")

    kept, note = FD.drop_unfinalized_bars(df, "2026-11-10")

    assert list(kept["date"]) == ["2026-11-09"]
    assert note is not None
    assert "未收盘" in note


def test_today_asof_after_finalize_keeps_bar(patch_now) -> None:
    """asof 是今天但已过 15:05 ⇒ 收盘 bar 已定型 ⇒ 保留。"""
    patch_now(datetime(2026, 11, 10, 15, 30, tzinfo=CST))
    df = _frame("2026-11-09", "2026-11-10")

    kept, note = FD.drop_unfinalized_bars(df, "2026-11-10")

    assert len(kept) == 2
    assert note is None


def test_past_asof_keeps_other_rows(patch_now) -> None:
    """asof 过去的场景下，**其它**日期的行也不能被动（含 asof 当日那行）。"""
    patch_now(datetime(2026, 11, 10, 9, 0, tzinfo=CST))
    df = _frame("2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09")

    kept, note = FD.drop_unfinalized_bars(df, "2026-11-09")

    assert len(kept) == 4
    assert note is None


def test_future_asof_drops_today_intraday_bar(patch_now) -> None:
    """🔴 asof 是**未来**时：帧里可能混进**今天**的盘中 bar ⇒ 同样要丢。"""
    patch_now(datetime(2026, 11, 10, 10, 37, tzinfo=CST))
    df = _frame("2026-11-09", "2026-11-10")

    kept, note = FD.drop_unfinalized_bars(df, "2026-11-11")

    assert list(kept["date"]) == ["2026-11-09"]
    assert note is not None
    assert "2026-11-10" in note


def test_clock_is_anchored_to_market_timezone(patch_now) -> None:
    """🔴 时钟必须锚**市场时区**（`DC.MARKET_TZ`），不是机器本地时区。"""
    fake = patch_now(datetime(2026, 11, 10, 10, 37, tzinfo=CST))
    FD.drop_unfinalized_bars(_frame("2026-11-09"), "2026-11-09")

    assert fake.requested_tz is DC.MARKET_TZ
    assert DC.MARKET_TZ.utcoffset(None) == timedelta(hours=8)
