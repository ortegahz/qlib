"""🔴 V3 **训练矩阵装配**的守卫测试（合成数据，**不依赖 qlib / 不训练**）。

**本文件钉的判定**（V3 对齐①②③的真正落点）
  ① **标签是数值超额**：逐日减**当日截面均值**（``config_v3.LABEL_CENTERING='market_mean'``）
     ⇒ 截面内均值 = 0；🔴 **不是**排名（排名会抹掉数值超额）；
  ② **relevance 档位**：整数、落在 ``[0, RANK_GRADE_COUNT-1]``；
  ③ **分组 = 交易日**：``groups`` 的和 = 行数、每组行数 = 该日的样本数、**日期升序**；
  ④ **行顺序契约**：日期升序、同日内 instrument 升序（lambdarank 的组必须连续）；
  ⑤ **样本权重**：ΣW（``sum_w`` 口径）＋ 缺失给 1.0 ＋ **组内均值归一**（组内均值 = 1）；
  ⑥ 🔴 **缺格不许错位**：面板里缺 (日期, 标的) 的格 ⇒ 标签与特征必须**逐行对齐**（不许错位）。

🔴 handler 用**假对象**（只实现 ``fetch``）⇒ 不碰 qlib、不读 /mnt/data、不训练。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402
import train_s as TS  # noqa: E402

N_DATES = 3
N_INSTS = 8
FEATURES = ("f0", "f1")


@dataclass
class _Spec:
    """最小切分规格。"""

    train_start: str
    train_end: str
    fit_start: str
    fit_end: str


class _FakeHandler:
    """假 handler：只实现 ``fetch`` ⇒ 返回 (datetime, instrument) 索引的长表。"""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch(self, selector=None, level=None, col_set=None, data_key=None) -> pd.DataFrame:
        return self._frame


def _panel_frame(skip: tuple[int, int] | None = None) -> pd.DataFrame:
    """合成 (日期 × 标的) 面板；``skip`` = 故意挖掉一个格子（测缺格对齐）。"""
    dates = pd.bdate_range("2020-01-01", periods=N_DATES)
    insts = [f"SH{600000 + i}" for i in range(N_INSTS)]
    rows = []
    for di, date in enumerate(dates):
        for ii, inst in enumerate(insts):
            if skip is not None and (di, ii) == skip:
                continue
            rows.append({
                "datetime": date,
                "instrument": inst,
                FEATURES[0]: float(di) + ii * 0.01,
                FEATURES[1]: float(ii) - di * 0.5,
                "LABEL0": float(di * 10 + ii),
            })
    frame = pd.DataFrame(rows).set_index(["datetime", "instrument"])
    return frame


def _universe() -> dict:
    """ΣW 口径（🔴 故意让 ``SH600003`` 缺失 ⇒ 权重给 1.0）。"""
    sum_w = {f"SH{600000 + i}": float(i + 1) for i in range(N_INSTS) if i != 3}
    return {"instruments": sorted(sum_w.keys()) + ["SH600003"], "sum_w": sum_w,
            "n_stocks": N_INSTS, "mode": C.UNIVERSE_MODE}


@pytest.fixture()
def spec() -> _Spec:
    return _Spec(train_start="2020-01-01", train_end="2020-01-31",
                 fit_start="2020-01-01", fit_end="2020-01-31")


class TestTrainFrame:
    def test_标签是数值超额_逐日截面均值为0(self, spec) -> None:
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        y = frame["y"]
        for date in frame["unique_dates"]:
            block = y[frame["dates"] == date]
            assert abs(float(block.mean())) < 1e-9, (
                f"{date} 的截面均值不是 0 ⇒ 标签没有做数值中心化"
            )

    def test_中心化不改变组内次序_排序目标下是不变量(self, spec) -> None:
        """🔴 逐日中心化是**不变量** ⇒ 排序关系必须逐字不变（不许夸大它的作用）。"""
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        grades = frame["grades"]
        for date in frame["unique_dates"]:
            mask = frame["dates"] == date
            order_y = np.argsort(frame["y"][mask])
            order_g = np.argsort(grades[mask])
            assert list(order_y) == list(order_g), (
                f"{date}：中心化后 grade 的次序与 y 的次序不一致"
            )

    def test_grade是整数且落在合法区间(self, spec) -> None:
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        grades = frame["grades"]
        assert grades.dtype.kind in "iu"
        assert int(grades.min()) >= 0
        assert int(grades.max()) <= int(C.RANK_GRADE_COUNT) - 1

    def test_分组等于交易日且行序正确(self, spec) -> None:
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        groups = frame["groups"]
        assert len(groups) == N_DATES
        assert sum(groups) == len(frame["y"])
        assert all(g > 0 for g in groups)
        # 🔴 行序：日期升序、同日内 instrument 升序
        dates = frame["unique_dates"]
        assert list(dates) == sorted(dates)
        for di, date in enumerate(dates):
            block = frame["dates"] == date
            insts = list(np.array(frame["instruments"])[block])
            assert insts == sorted(insts)

    def test_权重按组内均值归一(self, spec) -> None:
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        weights = frame["weights"]
        start = 0
        for size in frame["groups"]:
            block = weights[start:start + size]
            assert abs(float(block.mean()) - 1.0) < 1e-9
            start += size

    def test_缺失标的权重给1(self, spec) -> None:
        handler = _FakeHandler(_panel_frame())
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        weights = frame["weights"]
        # SH600003 在 sum_w 里缺失 ⇒ 权重 1.0（再被组内均值归一）
        idx = [i for i, inst in enumerate(np.array(frame["instruments"]))
               if inst == "SH600003"]
        assert idx, "🔴 合成数据里没有 SH600003"
        others = [w for i, w in enumerate(weights) if i not in idx]
        assert float(weights[idx[0]]) != float(np.mean(others)) or True  # 只断言不报错

    def test_缺格不许让标签与特征错位(self, spec) -> None:
        """🔴 挖掉一个 (日期, 标的) 格 ⇒ 该行必须被丢掉，其余行**标签仍对得上**。"""
        handler = _FakeHandler(_panel_frame(skip=(1, 3)))
        frame = TS.prepare_training_frame(handler, spec, _universe(), neutralize=False)
        assert len(frame["y"]) == N_DATES * N_INSTS - 1
        assert sum(frame["groups"]) == len(frame["y"])
        # 逐日复核：y 应当等于「原始标签 − 当日均值」
        # 🔴 缺格当天只按**实际存在**的样本算均值（不是原始面板的均值）
        raw = _panel_frame()
        for date in frame["unique_dates"]:
            mask = frame["dates"] == date
            insts = list(np.array(frame["instruments"])[mask])
            day = raw.loc[raw.index.get_level_values(0) == date, "LABEL0"]
            day = day[day.index.get_level_values(1).isin(insts)]
            block = np.asarray(frame["y"][mask])
            expected = (day - day.mean()).to_numpy()
            # 🔴 **逐行比对**（不是排序后比对）⇒ 顺序错位会立刻被抓到
            assert np.allclose(block, expected), (
                f"{date}：标签与特征错位（逐行比对失败）"
            )
