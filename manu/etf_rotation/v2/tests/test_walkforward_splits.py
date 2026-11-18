"""🔴 V2 **5 折滚动前推（expanding window）**切分的钉死测试 —— 2026-11-18。

**背景（用户 2026-11-18 原话）**：
  原验证段只有 2022–2023 两年（484 个交易日）⇒ 月频只有 ~24 个调仓点
  ⇒ 拿 24 个样本点去搜 240 组超参 = **必然过拟合**；且这两年是单边熊市
  ⇒ 搜出来必然是「蜗牛参数」（cadence=90 + entry_ma=120）。
  ⇒ 改用 **5 折 expanding window**：训练段逐年变长，验证年逐年前推
  （2019 / 2020 / 2021 / 2022 / 2023）⇒ 拼成 2019–2023 的纯 OOS 验证段。

【本文件钉死】
  ① 5 折的验证年 = 2019 / 2020 / 2021 / 2022 / 2023，**连续无缝、无重叠**；
  ② 每折训练段 = **expanding**（逐年变长，训练末样本日严格递增）；
  ③ 每折「训练末样本的标签窗口结束日」**严格早于**验证段首日（embargo 生效）；
  ④ 每折 embargo **≥ 20 个交易日**（规格下限）；
  ⑤ 🔴 **任何一折都不许触碰测试段**（``valid_end < 2024-01-01``）；
  ⑥ 🔴 **偏差披露**：训练段起点 = 2016-01-01（规格写 2015；数据从 2016 起）
     ⇒ 摘要里必须写明「折 1 训练段少 1 年」；
  ⑦ 默认切分（``split()``）**逐字不变**（既有调用方不受影响）；
  ⑧ 【变异反证】把 embargo 倒推的 ``emb + 1`` 改成 ``emb`` ⇒ ③ 必须转红。

⛔ **只用自造夹具** ⇒ 不联网、不跑重量级回测。
⛔ **绝不落盘改真源码**（变异 = 源码 → 内存改 → tmp_path → import 副本）。
"""

from __future__ import annotations

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


# --------------------------------------------------------------------- ① 折定义


class TestFoldDefinition:
    def test_五折验证年逐年前推(self) -> None:
        years = [f[3][:4] for f in C.WALK_FORWARD_FOLDS]
        assert years == ["2019", "2020", "2021", "2022", "2023"]
        assert len(C.WALK_FORWARD_FOLDS) == 5

    def test_五折验证窗连续无缝无重叠(self) -> None:
        """5 折的**交易日**必须无缝首尾相接（不许有洞、不许重叠）。"""
        cal = [d.isoformat() for d in S.trading_calendar()]
        seen: list[str] = []
        for fid, train_end_raw, vs, ve in C.WALK_FORWARD_FOLDS:
            spec = S.build_fold_spec(fid)
            assert spec.valid_start == vs and spec.valid_end == ve
            days = [d for d in cal if vs <= d <= ve]
            assert days, f"折 {fid} 的验证窗内没有交易日"
            seen.extend(days)
        assert seen == sorted(seen)                      # 升序、无重叠
        assert len(seen) == len(set(seen))               # 无重复
        whole = [d for d in cal
                 if C.WALK_FORWARD_OOS_WINDOW[0] <= d <= C.WALK_FORWARD_OOS_WINDOW[1]]
        assert seen == whole                             # 五折拼起来 == 整个 OOS 窗口

    def test_训练段逐折变长(self) -> None:
        ends = [S.build_fold_spec(f[0]).train_end for f in C.WALK_FORWARD_FOLDS]
        assert ends == sorted(ends)
        assert len(set(ends)) == len(ends)

    def test_默认切分逐字不变(self) -> None:
        """🔴 新增折逻辑**不许**动默认切分（既有调用方零影响）。"""
        spec = S.split()
        assert spec.train_start == "2016-01-01"
        assert spec.train_end == "2021-11-26"       # embargo=25 交易日裁剪后的末日
        assert spec.valid_start == "2022-01-01"
        assert spec.valid_end == "2023-12-31"


# --------------------------------------------------------------------- ② 折级红线


class TestFoldDiscipline:
    @pytest.mark.parametrize("fid", [1, 2, 3, 4, 5])
    def test_每折训练与验证不相交(self, fid: int) -> None:
        spec = S.assert_fold_only(fid)
        assert spec.train_end < spec.valid_start
        assert spec.fit_end == spec.train_end
        assert spec.fit_start == spec.train_start

    @pytest.mark.parametrize("fid", [1, 2, 3, 4, 5])
    def test_标签窗口不跨界(self, fid: int) -> None:
        """🔴 核心：训练末样本的标签窗口结束日必须 **严格早于** 验证段首日。"""
        spec = S.assert_fold_only(fid)
        lbl_end = S.label_window_end(S._d(spec.train_end))
        assert lbl_end.isoformat() < spec.valid_start

    @pytest.mark.parametrize("fid", [1, 2, 3, 4, 5])
    def test_embargo_不少于二十个交易日(self, fid: int) -> None:
        spec = S.assert_fold_only(fid)
        upper = S.trading_days_before(S._d(spec.train_end_raw), int(C.EMBARGO_DAYS))
        assert S._d(spec.train_end) <= upper

    @pytest.mark.parametrize("fid", [1, 2, 3, 4, 5])
    def test_绝不触碰测试段(self, fid: int) -> None:
        spec = S.assert_fold_only(fid)
        assert spec.valid_end < C.TEST[0]
        assert spec.test_end < C.TEST[0]

    def test_非法折号报错(self) -> None:
        with pytest.raises(ValueError):
            S.build_fold_spec(0)
        with pytest.raises(ValueError):
            S.build_fold_spec(99)

    def test_变异反证_折表伸进测试段必须报红(self, monkeypatch) -> None:
        """🔴 把折 5 的验证年改成 2024 ⇒ 必须报红（不许静默放行）。"""
        bad = list(C.WALK_FORWARD_FOLDS)
        bad[-1] = (5, "2022-12-31", "2024-01-01", "2024-12-31")
        monkeypatch.setattr(C, "WALK_FORWARD_FOLDS", tuple(bad))
        with pytest.raises(AssertionError):
            S.assert_fold_only(5)

    def test_变异反证_embargo少一天必须报红(self, tmp_path: Path) -> None:
        """🔴 embargo 倒推从 ``emb + 1`` 改成 ``emb`` ⇒ 标签窗口跨界 ⇒ 必须报红。"""
        module = _mutated_module(
            tmp_path,
            [("trading_days_before(_d(valid_start), emb + 1)",
              "trading_days_before(_d(valid_start), emb)")],
            "emb_off_by_one",
        )
        for fid in (1, 2, 3, 4, 5):
            with pytest.raises(AssertionError):
                module.assert_fold_only(fid)


# --------------------------------------------------------------------- ③ 摘要披露


class TestSummaryDisclosure:
    def test_摘要含五折与拼接窗口(self) -> None:
        s = S.walk_forward_summary()
        assert s["n_folds"] == 5
        assert s["oos_window"] == ["2019-01-01", "2023-12-31"]
        assert [f["fold_id"] for f in s["folds"]] == [1, 2, 3, 4, 5]

    def test_摘要披露训练起点偏差与embargo(self) -> None:
        s = S.walk_forward_summary()
        assert s["train_start"] == "2016-01-01"
        assert "2015" in s["deviation_train_start"]
        assert s["embargo_days_effective"] == 25
        assert s["embargo_days_spec_min"] == 20
        assert s["locked_params"]["rebalance_every_n_days"] == 20
        assert s["locked_params"]["top_k"] == 5
        assert list(s["locked_params"]["hold_buffer_grid"]) == [2, 3, 5]

    def test_五折验证窗拼起来正好是OOS窗口(self) -> None:
        s = S.walk_forward_summary()
        assert s["folds"][0]["valid_start"] == C.WALK_FORWARD_OOS_WINDOW[0]
        assert s["folds"][-1]["valid_end"] == C.WALK_FORWARD_OOS_WINDOW[1]
