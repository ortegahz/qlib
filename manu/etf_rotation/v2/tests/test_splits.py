"""🔴 V2 时间切分与 **embargo 硬隔离**的回归测试。

**事故背景（为什么要钉这些）**：
V2 的标签退出窗口末端是 **t+25** ⇒ 若只裁 **20 个交易日**，
最后一个训练样本的标签窗口会伸进验证段（结构性泄露）。
⇒ 隔离带必须取 `max(20, 25) = 25` 个交易日，
且**必须有一条会在泄露发生时真的报红**的硬断言
（``assert_no_label_overlap``）—— 否则「embargo」只是注释。

【本文件钉的判定】
  ① 有效隔离带 = 25 个交易日（≥ 规格下限 20，且 ≥ 标签窗口末端）；
  ② 训练末样本的标签窗口结束日 **严格早于** 验证集首样本日；
  ③ `fit_end` 必须等于**裁剪后**的训练末日（不许仍写 2021-12-31）；
  ④ 🔴 **字面 20 天的口径必须报红**（这是「embargo 真在起作用」的证据）：
     用 20 天时，训练末样本的标签窗口会伸到 2022-01-10 ⇒ 断言必须抛错。

【变异反证】改坏源码里的关键判定 ⇒ 对应断言**必须转红**。
⛔ **绝不落盘改真源码**（真源码是主 agent 的写集）。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path

import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import splits as S  # noqa: E402

SPLITS_SOURCE = _V2_DIR / "splits.py"


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    text = SPLITS_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_splits_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- ① 口径


class TestSplitCaliber:
    def test_名义切分与规格一致(self) -> None:
        assert C.TRAIN == ("2016-01-01", "2021-12-31")
        assert C.VALID == ("2022-01-01", "2023-12-31")
        assert C.TEST == ("2024-01-01", "2026-11-03")

    def test_有效隔离带必须不小于规格下限与标签窗口末端(self) -> None:
        eff = S.effective_embargo_days()
        assert eff >= C.EMBARGO_DAYS, f"隔离带 {eff} < 规格下限 {C.EMBARGO_DAYS}"
        assert eff >= C.LABEL_HORIZON_DAYS, (
            f"隔离带 {eff} < 标签窗口末端 {C.LABEL_HORIZON_DAYS} ⇒ 标签会跨界"
        )
        assert eff == 25

    def test_训练末样本被裁剪且不晚于名义末日(self) -> None:
        spec = S.split()
        assert spec.train_end_raw == "2021-12-31"
        assert spec.train_end < spec.train_end_raw
        assert spec.train_end <= S.trading_days_before(_dt.date(2021, 12, 31), 20).isoformat()

    def test_验证与测试段不被裁剪(self) -> None:
        spec = S.split()
        assert spec.valid_start == "2022-01-01"
        assert spec.valid_end == "2023-12-31"
        assert spec.test_start == "2024-01-01"
        assert spec.test_end == "2026-11-03"

    def test_fit_end必须等于裁剪后的训练末日(self) -> None:
        spec = S.split()
        assert spec.fit_end == spec.train_end
        assert spec.fit_end != spec.train_end_raw, "fit_end 不许仍写名义末日 2021-12-31"

    def test_训练标签窗口不得伸进验证段(self) -> None:
        spec = S.split()
        last_train = _dt.date.fromisoformat(spec.train_end)
        val_start = _dt.date.fromisoformat(spec.valid_start)
        assert S.label_window_end(last_train) < val_start

    def test_口径摘要同时给出20与25(self) -> None:
        summary = S.embargo_summary()
        assert summary["embargo_days_spec_min"] == 20
        assert summary["embargo_days_effective"] == 25
        assert summary["label_horizon_days"] == 25
        assert summary["no_label_overlap"] is True
        assert "20" in summary["note"] and "25" in summary["note"]


# --------------------------------------------------------------------- ② 硬隔离


class TestNoLeak:
    def test_字面20天口径必须报红(self) -> None:
        """🔴 **embargo 真在起作用的证据**：20 天时标签跨界 ⇒ 断言必须抛错。"""
        spec = S.build_split(embargo_days=20)
        with pytest.raises(AssertionError) as exc:
            S.assert_no_label_overlap(spec)
        assert "embargo" in str(exc.value)

    def test_字面20天口径的标签窗口确实伸进验证段(self) -> None:
        spec = S.build_split(embargo_days=20)
        last_train = _dt.date.fromisoformat(spec.train_end)
        val_start = _dt.date.fromisoformat(spec.valid_start)
        assert S.label_window_end(last_train) >= val_start

    def test_零隔离带必须报红(self) -> None:
        spec = S.build_split(embargo_days=1)
        with pytest.raises(AssertionError):
            S.assert_no_label_overlap(spec)

    def test_全套断言在默认切分上必须通过(self) -> None:
        S.assert_split_discipline(S.split())


# --------------------------------------------------------------------- ③ 变异反证


def test_变异反证_把隔离带硬编码成20_默认切分必须报红(tmp_path: Path) -> None:
    mutant = _mutated_module(
        tmp_path,
        [("    return max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))",
          "    return int(C.EMBARGO_DAYS)")],
        "embargo20",
    )
    assert mutant.effective_embargo_days() == 20
    with pytest.raises(AssertionError):
        mutant.assert_no_label_overlap(mutant.build_split())


def test_变异反证_去掉标签跨界断言_硬隔离必须失守(tmp_path: Path) -> None:
    """把 ``assert_no_label_overlap`` 掏空 ⇒ 字面 20 天口径就**不再报红**
    ⇒ 这条测试证明：那条断言**不是装饰**。"""
    mutant = _mutated_module(
        tmp_path,
        [("    if lbl_end >= val_start:", "    if False:")],
        "no_assert",
    )
    spec = mutant.build_split(embargo_days=20)
    mutant.assert_no_label_overlap(spec)  # 变异体不抛错 ⇒ 断言被掏空了


def test_变异反证_把fit_end写回名义末日_必须被断言拦下(tmp_path: Path) -> None:
    mutant = _mutated_module(
        tmp_path,
        [("        fit_end=train_end.isoformat(),", "        fit_end=train_end_raw.isoformat(),")],
        "fit_end_raw",
    )
    spec = mutant.build_split()
    with pytest.raises(AssertionError):
        mutant.assert_split_discipline(spec)
