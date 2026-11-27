"""🔴 V3 **滚动重训**（滚动窗口切分）的守卫测试。

**本文件钉的判定**
  ① 用**临时交易日历**（不读 /mnt/data、不联网）切窗口 ⇒ 窗口数与边界逐个可验；
  ② **expanding window**：所有窗口的 ``train_start`` 相同，``train_end`` 随时间变长；
  ③ **embargo 照旧从验证起点倒推**：训练末样本与验证首日之间**恰好**隔 ``embargo`` 个交易日
     （🔴 自然日 ≠ 交易日 ⇒ 必须按交易日历数）；
  ④ 🔴 **变异反证**：``every_n_trading_days=0``（或负数）⇒ 必须 raise（不许静默退化成 1）。

🔴 **不依赖 qlib、不读服务器数据、不训练**（本文件只测「切窗口」这一层）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402
import rolling as R  # noqa: E402

#: 临时日历的交易日数（工作日）
N_CAL = 120
#: 验证段在临时日历里的起点下标
VALID_OFFSET = 60


@dataclass
class _Spec:
    """最小切分规格（字段与 ``FoldSpec`` 同名同义）。"""

    train_start: str
    train_end: str
    train_end_raw: str
    valid_start: str
    valid_end: str


@pytest.fixture()
def calendar_file(tmp_path: Path) -> tuple[Path, list[str]]:
    """写一份**临时**交易日历（120 个工作日）⇒ 返回 (路径, 日期列表)。"""
    days = pd.bdate_range("2020-01-01", periods=N_CAL)
    path = tmp_path / "day.txt"
    path.write_text("\n".join(d.strftime("%Y-%m-%d") for d in days) + "\n", encoding="utf-8")
    return path, [d.strftime("%Y-%m-%d") for d in days]


@pytest.fixture()
def spec(calendar_file) -> _Spec:
    """验证段 = 临时日历的后 60 个交易日。"""
    _, days = calendar_file
    return _Spec(
        train_start=days[0],
        train_end=days[VALID_OFFSET - 30],
        train_end_raw=days[VALID_OFFSET - 30],
        valid_start=days[VALID_OFFSET],
        valid_end=days[-1],
    )


class TestRollingWindows:
    def test_窗口数与边界正确(self, spec, calendar_file) -> None:
        path, days = calendar_file
        every_n = 20
        windows = R.rolling_windows(spec, every_n, calendar_file=path)
        valid_days = days[VALID_OFFSET:]
        assert len(windows) == (len(valid_days) + every_n - 1) // every_n
        for i, window in enumerate(windows):
            chunk = valid_days[i * every_n:(i + 1) * every_n]
            assert window["window_id"] == i + 1
            assert window["valid_start"] == chunk[0]
            assert window["valid_end"] == chunk[-1]
            assert window["n_valid_days"] == len(chunk)

    def test_窗口首尾相接且不重叠(self, spec, calendar_file) -> None:
        path, days = calendar_file
        windows = R.rolling_windows(spec, 20, calendar_file=path)
        for prev, cur in zip(windows, windows[1:]):
            assert prev["valid_end"] < cur["valid_start"]
        assert windows[0]["valid_start"] == days[VALID_OFFSET]
        assert windows[-1]["valid_end"] == days[-1]

    def test_expanding_window_训练段随时间变长(self, spec, calendar_file) -> None:
        path, _ = calendar_file
        windows = R.rolling_windows(spec, 20, calendar_file=path)
        assert len({w["train_start"] for w in windows}) == 1
        assert all(w["train_start"] == spec.train_start for w in windows)
        ends = [w["train_end"] for w in windows]
        assert ends == sorted(ends)
        assert len(set(ends)) == len(ends)

    def test_embargo按交易日从验证起点倒推(self, spec, calendar_file) -> None:
        path, days = calendar_file
        embargo = max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))
        windows = R.rolling_windows(spec, 20, calendar_file=path)
        for window in windows:
            gap = [d for d in days if window["train_end"] < d < window["valid_start"]]
            assert len(gap) == embargo, (
                f"窗口 {window['window_id']} 的 embargo 实际 {len(gap)} 个交易日 ≠ {embargo}"
            )
            assert window["train_end"] < window["valid_start"]

    def test_窗口绝不触碰单盲封存段(self, spec, calendar_file) -> None:
        path, _ = calendar_file
        windows = R.rolling_windows(spec, 20, calendar_file=path)
        for window in windows:
            assert window["valid_end"] < str(C.BLIND_SEALED_WINDOW[0])
            assert window["train_end"] < str(C.BLIND_SEALED_WINDOW[0])


class TestMutation:
    def test_变异反证_步长为0必须raise(self, spec, calendar_file) -> None:
        path, _ = calendar_file
        with pytest.raises(ValueError, match="every_n_trading_days"):
            R.rolling_windows(spec, 0, calendar_file=path)

    def test_变异反证_负步长必须raise(self, spec, calendar_file) -> None:
        path, _ = calendar_file
        with pytest.raises(ValueError, match="every_n_trading_days"):
            R.rolling_windows(spec, -5, calendar_file=path)

    def test_验证段没有交易日必须raise(self, spec, calendar_file) -> None:
        path, _ = calendar_file
        empty = _Spec(
            train_start=spec.train_start,
            train_end=spec.train_end,
            train_end_raw=spec.train_end_raw,
            valid_start="2099-01-01",
            valid_end="2099-12-31",
        )
        with pytest.raises(RuntimeError, match="没有交易日"):
            R.rolling_windows(empty, 20, calendar_file=path)
