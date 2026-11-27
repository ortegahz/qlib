"""🔴 V3 时间切分 + embargo 硬隔离的回归测试。

【本文件钉的判定】
  ① ``effective_embargo_days()`` = **25**（= max(EMBARGO_DAYS, LABEL_HORIZON_DAYS)）；
  ② **5 折的 train_end / valid 区间正确**（临时日历 ⇒ 固定期望值，可复算）；
  ③ 🔴 embargo 裁剪**基于真实交易日历**（不是自然日）；
  ④ 🔴 **绝不触碰 2024+ 单盲封存段** —— 构造越界折 ⇒ **必须 raise**（变异反证）；
  ⑤ 训练段与验证段相交 / 标签窗口跨界 ⇒ **必须 raise**；
  ⑥ 交易日历文件缺失 ⇒ raise（不许拿自然日凑数）。

【为什么用临时日历】本机**没有** qlib 数据集 ⇒ 测试自己造一份**工作日日历**
（`pandas.bdate_range`），把期望值写死 ⇒ 断的是「算法语义」，不是「某台机器的数据」。
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_v3  # noqa: E402
import splits as v3_splits  # noqa: E402

#: 🔴 合成工作日日历下的**固定期望值**（train_end = 验证段首日往前 26 个交易日）
EXPECTED_TRAIN_END = {
    1: "2018-11-26",
    2: "2019-11-26",
    3: "2020-11-26",
    4: "2021-11-26",
    5: "2022-11-25",
}


@pytest.fixture()
def synthetic_calendar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一份**工作日**交易日历并挂到 V3 的 splits 模块上（🔴 只改内存，不落真源码）。"""
    path = tmp_path / "day.txt"
    days = pd.bdate_range("2015-01-01", "2024-12-31")
    path.write_text(
        "\n".join(day.date().isoformat() for day in days) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(v3_splits, "CALENDAR_FILE", str(path))
    return path


def test_embargo等于25() -> None:
    """🔴 embargo = max(EMBARGO_DAYS, LABEL_HORIZON_DAYS) = 25（标签窗口末端 = t+25）。"""
    assert config_v3.EMBARGO_DAYS == 20
    assert config_v3.LABEL_HORIZON_DAYS == 25
    assert v3_splits.effective_embargo_days() == 25


def test_五折区间正确(synthetic_calendar: Path) -> None:
    """🔴 5 折的训练段 / 验证段 / 训练末日全部可对表。"""
    table = {int(entry[0]): entry for entry in config_v3.WALK_FORWARD_FOLDS}
    assert sorted(table) == [1, 2, 3, 4, 5]
    for fold_id in (1, 2, 3, 4, 5):
        spec = v3_splits.build_fold_spec(fold_id)
        entry = table[fold_id]
        assert spec.fold_id == fold_id
        assert spec.train_start == config_v3.TRAIN[0] == "2016-01-01"
        assert spec.train_end_raw == str(entry[1])
        assert spec.valid_start == str(entry[2])
        assert spec.valid_end == str(entry[3])
        assert spec.train_end == EXPECTED_TRAIN_END[fold_id]
        assert spec.fit_start == config_v3.FIT_START
        assert spec.fit_end == spec.train_end  # 🔴 fit_end 必须收窄到裁剪后的末日
        assert spec.embargo_days == 25
        # 🔴 折级红线自身必须通过
        v3_splits.assert_fold_discipline(spec)
    with pytest.raises(ValueError):
        v3_splits.build_fold_spec(99)


def test_embargo按交易日而非自然日(synthetic_calendar: Path) -> None:
    """🔴 26 个**交易日**的间隔 > 26 个**自然日** ⇒ 证明用的是交易日历。"""
    spec = v3_splits.build_fold_spec(1)
    calendar = pd.DatetimeIndex(
        pd.to_datetime(synthetic_calendar.read_text(encoding="utf-8").split())
    )
    start = int(calendar.searchsorted(pd.Timestamp(spec.valid_start), side="left"))
    prior = calendar[:start]
    gap_days = len(prior) - int(calendar.searchsorted(pd.Timestamp(spec.train_end), side="left"))
    assert gap_days == 26  # embargo(25) + 1
    natural_gap = (pd.Timestamp(spec.valid_start) - pd.Timestamp(spec.train_end)).days
    assert natural_gap > 26  # 🔴 自然日更长 ⇒ 用自然日裁会把 embargo 缩水


def test_默认切分(synthetic_calendar: Path) -> None:
    """🔴 默认切分 = train 2016~2021 / valid 2022~2023（训练末日已裁剪）。"""
    spec = v3_splits.split()
    assert spec.fold_id is None
    assert spec.train_start == config_v3.TRAIN[0]
    assert spec.train_end_raw == config_v3.TRAIN[1] == "2021-12-31"
    assert spec.valid_start == config_v3.VALID[0] == "2022-01-01"
    assert spec.valid_end == config_v3.VALID[1] == "2023-12-31"
    assert spec.train_end == "2021-11-26"
    assert spec.fit_end == spec.train_end
    assert spec.embargo_days == 25


def test_越到2024必须raise(synthetic_calendar: Path) -> None:
    """🔴 **变异反证**：把验证段末日挪进单盲封存段 ⇒ 必须 raise。"""
    spec = v3_splits.build_fold_spec(1)
    bad = dataclasses.replace(spec, valid_end="2024-06-30")
    with pytest.raises(AssertionError):
        v3_splits.assert_fold_discipline(bad)
    bad_train = dataclasses.replace(spec, train_end="2024-01-02", fit_end="2024-01-02")
    with pytest.raises(AssertionError):
        v3_splits.assert_fold_discipline(bad_train)
    # 🔴 **隔离 ④**：fold_id=None ⇒ ①（折号/配置一致性）不参与判定
    #    ⇒ 只可能由「绝不触碰单盲封存段」这条红线转红（删掉 ④ ⇒ 本断言必失败）
    blind = dataclasses.replace(spec, fold_id=None, valid_end="2024-06-30")
    with pytest.raises(AssertionError):
        v3_splits.assert_fold_discipline(blind)


def test_训练段与验证段相交必须raise(synthetic_calendar: Path) -> None:
    """🔴 训练末日 ≥ 验证段首日 ⇒ 必须 raise。"""
    spec = v3_splits.build_fold_spec(1)
    bad = dataclasses.replace(spec, train_end=spec.valid_start, fit_end=spec.valid_start)
    with pytest.raises(AssertionError):
        v3_splits.assert_fold_discipline(bad)


def test_标签窗口跨界必须raise(synthetic_calendar: Path) -> None:
    """🔴 训练末样本的标签窗口伸进验证段 ⇒ 必须 raise（embargo 不足）。"""
    spec = v3_splits.build_fold_spec(1)
    late = v3_splits.trading_days_before(spec.valid_start, 1, calendar_file=str(synthetic_calendar))
    bad = dataclasses.replace(spec, train_end=late, fit_end=late)
    with pytest.raises(AssertionError):
        v3_splits.assert_fold_discipline(bad)


def test_折间验证年递增(synthetic_calendar: Path) -> None:
    """🔴 折号越大 ⇒ 验证年越晚（① 的另一半）。"""
    previous = None
    for fold_id in (1, 2, 3, 4, 5):
        spec = v3_splits.build_fold_spec(fold_id)
        if previous is not None:
            assert spec.valid_start > previous.valid_start
            assert spec.valid_start > previous.valid_end
        previous = spec


def test_交易日历缺失即报错(tmp_path: Path) -> None:
    """🔴 日历文件不存在 ⇒ raise（不许拿自然日凑 embargo）。"""
    with pytest.raises(FileNotFoundError):
        v3_splits.trading_days_before("2022-01-01", 5, calendar_file=tmp_path / "没有.txt")
