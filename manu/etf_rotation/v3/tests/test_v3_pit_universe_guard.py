"""🔴 V3 **逐日 PIT 训练池**（新口径）的守卫测试（合成数据，**不依赖 qlib / 不训练**）。

【背景（实测真值）】
  * 训练池目标区间 = **1200~1800 只**大中盘（ETF 成分股并集 ∩ 中证800∪中证1000）；
  * 现状缺陷：`universe.read_instruments_file` **只取第 1 列、丢掉 start/end 区间**
    ⇒ 池变成「全历史在册过」的静态并集 ⇒ 实测 **2635 只**（超目标 46%），
    训练段 **297/2635 = 11.3%** 的标的**整段不在**名单内；
  * 逐日 PIT 池实测：min **1279** / 中位 **1402** / max **1628**（100% 落在目标区间）；
  * 🔴 qlib handler **忽略 instruments 文件的日期列** ⇒ **必须用 pandas 侧掩码**；
  * 🔴 **打分侧不能按日筛**（打分池也 PIT ⇒ 2019-04-02 平均 62.4% / 中位 68.7% 的
    ETF 权重无分可映射；现役静态池只丢 1.53%）⇒ **训练样本按当日 PIT 筛，
    打分池保持 ETF 全并集口径**。

【本文件钉的判定】
  ① **PIT 面板遵守日期区间**（合成 instruments 文件 ⇒ 区间内成员 True、区间外 False）；
  ② **默认模式输出逐位不变**（键集合 ＋ 值 ＋ **不带** PIT 面板键）；
  ③ **训练帧掩码**：不在当日池内的 (date, instrument) 行被剔除、在池内的保留；
  ④ **打分路径不施加 PIT 掩码**（🔴 **行为断言**，不是源码字符串检查）：
     给打分路径喂**成员面板** ⇒ 打分行数**不减少**、行集合**不变**
     （PIT 面板在打分侧**只作拟合掩码**：拟合子集＝当日 PIT 成员，变换全集＝打分池）；
  ⑤ **默认臂列表仍是 6 臂**（`v3_pit_pool` **不在**默认臂里 ⇒ 默认跑批不许变 7 臂）；
  ⑥ **`v3_pit_pool` 相对 `v3_full` 只差训练池口径**（单变量守卫：超参逐项相同）；
  ⑦ **默认口径常量不许动**（`UNIVERSE_MODE` 仍是静态并集口径）；
  ⑧ **掩码早于截面中心化**（当日池的截面均值才算得对）。

【2026-11-27 reviewer 审查修复 · 本文件新增的行为断言（I4）】
  ★ 上一版的「打分路径不含 PIT 掩码」是**源码字符串检查** ⇒ reviewer 的变异测试证明
    它**抓不住**三类回归（打分侧偷偷加当日掩码 / 面板日期轴改自然日 /
    掩码挪到 ΣW 权重之后）
    ⇒ 本文件把这类检查**全部换成行为断言**：
  ⑨ **打分路径喂成员面板 ⇒ 打分行数不减少**（⇒ 抓「打分侧偷偷加当日掩码」）；
  ⑩ **掩码早于 ΣW 权重计算**（记账口径 ＋ 组内权重均值＝1
      ⇒ 抓「掩码挪到 ΣW 权重之后」）；
  ⑪ **面板日期轴 ＝ 交易日历**（逐日相等、不含周末 ⇒ 抓「面板日期轴改自然日」）；
  ⑫ **manifest 两处 `universe.mode` 一致**（B2 ⇒ 抓「口径卡硬编码静态口径」）；
  ⑬ **训练侧残差 ≡ 打分侧残差**（B1 ⇒ 抓「两侧拟合子集不同源」）。

🔴 本文件**只读**、不联网、不读 /mnt/data、不训练、不起任何跑批。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import config_v3 as C
import numpy as np
import pandas as pd
import pytest
import train_s as TS
import universe

#: `manu/etf_rotation/v3/` 目录
_V3_DIR = Path(__file__).resolve().parents[1]
#: `manu/tools/`（探针所在处）
_TOOLS_DIR = _V3_DIR.parent.parent / "tools"
# 🔴 **V3 目录已在 ``sys.path`` 上**（同目录 ``conftest.py`` 把它挂进去的，V2 先、V3 后）
#    ⇒ 这里直接扁平导入即可（不需要 sys.path 手术 ⇒ 也不需要 noqa 豁免）。

#: 🔴 **默认口径的返回键集合**（改动前的既有结构 ⇒ 默认模式必须逐位不变）
LEGACY_UNIVERSE_KEYS = {
    "mode",
    "instruments",
    "sum_w",
    "n_stocks",
    "etf_codes",
    "used_fallback",
    "holdings_snapshot_date",
}

#: 🔴 **默认臂列表**（6 臂；新臂只能显式指定才跑）
LEGACY_ARMS = {
    "control_v2",
    "v3_full",
    "v3_mse",
    "v3_no_neutralize",
    "v3_no_weight",
    "v3_no_rolling",
}

#: 🔴 **单变量守卫**：两臂之间必须逐项相同的「参数键」
SINGLE_VARIABLE_PARAM_KEYS = (
    "objective_label",
    "neutralize",
    "center_mode",
    "weight_mode",
    "rolling",
    "source",
)


def _load_probe():
    """按**文件路径**加载探针模块（探针在 ``manu/tools/``，不在包内）。"""
    path = _TOOLS_DIR / "v3_comparison_probe.py"
    spec = importlib.util.spec_from_file_location("v3_comparison_probe", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写错才会到这
        raise ImportError(f"🔴 无法加载探针模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["v3_comparison_probe"] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------- 合成夹具
def _holdings() -> pd.DataFrame:
    """合成成分股表（3 只：SH600000 / SZ600002 在大中盘、SH600003 不在）。"""
    return pd.DataFrame(
        {
            "etf_code": ["510300", "510300", "510300"],
            "stock_code": ["600000.SH", "600002.SZ", "600003.SH"],
            "weight_pct": [10.0, 5.0, 7.0],
        }
    )


def _cap_files(tmp_path: Path) -> dict[str, Path]:
    """合成「大中盘」成分表（带日期区间）⇒ csi800 / csi1000。"""
    csi800 = tmp_path / "csi800.txt"
    csi800.write_text(
        "SH600000\t2016-01-01\t2023-12-31\nSH600001\t2016-01-01\t2023-12-31\n",
        encoding="utf-8",
    )
    csi1000 = tmp_path / "csi1000.txt"
    csi1000.write_text("SZ600002\t2016-01-01\t2023-12-31\n", encoding="utf-8")
    return {"csi800": csi800, "csi1000": csi1000}


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


N_DATES = 3
N_INSTS = 4
FEATURES = ("f0", "f1")


def _panel_frame(labels: list[float] | None = None) -> pd.DataFrame:
    """合成 (日期 × 标的) 面板；``labels`` 可指定逐格标签（默认 (di, ii) 递增）。"""
    dates = pd.bdate_range("2020-01-01", periods=N_DATES)
    insts = [f"SH{600000 + i}" for i in range(N_INSTS)]
    rows = []
    for di, date in enumerate(dates):
        for ii, inst in enumerate(insts):
            rows.append({
                "datetime": date,
                "instrument": inst,
                FEATURES[0]: float(di) + ii * 0.01,
                FEATURES[1]: float(ii) - di * 0.5,
                "LABEL0": float(di * 10 + ii) if labels is None else float(labels[di * N_INSTS + ii]),
            })
    return pd.DataFrame(rows).set_index(["datetime", "instrument"])


def _universe_with_panel(panel: pd.DataFrame) -> dict:
    """带 PIT 面板的 ``universe_info``（其余键与默认口径同构）。"""
    insts = [str(c) for c in panel.columns]
    return {
        "mode": C.UNIVERSE_MODE_PIT,
        "instruments": insts,
        "sum_w": {inst: float(i + 1) for i, inst in enumerate(insts)},
        "n_stocks": len(insts),
        "etf_codes": ["510300"],
        "used_fallback": False,
        "holdings_snapshot_date": str(C.HOLDINGS_SNAPSHOT_DATE),
        C.PIT_MEMBERSHIP_KEY: panel,
    }


@pytest.fixture()
def spec() -> _Spec:
    return _Spec(train_start="2020-01-01", train_end="2020-01-31",
                 fit_start="2020-01-01", fit_end="2020-01-31")


# ----------------------------------------------------------------- 打分侧合成夹具
N_SCORE_DATES = 4
N_SCORE_INST = 120
SCORE_FEATURES = ("f0", "f1", "f2")


def _synthetic(n_dates: int = N_SCORE_DATES, n_inst: int = N_SCORE_INST):
    """合成「规模档 + 流动性 + 特征 + 标签」四件套（全部本地生成，不碰 qlib）。"""
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2020-03-02", periods=n_dates)
    instruments = [f"SZ{600000 + i:06d}" for i in range(n_inst)]
    tier_values = np.array([[i % 4 for i in range(n_inst)] for _ in range(n_dates)])
    tier_panel = pd.DataFrame(tier_values, index=dates, columns=instruments)
    liquidity = pd.DataFrame(
        rng.normal(size=(n_dates, n_inst)), index=dates, columns=instruments
    )
    x = np.empty((n_dates, n_inst, len(SCORE_FEATURES)))
    x[:, :, 0] = (1.5 * (tier_values == 0) - 0.8 * (tier_values == 1)
                  + rng.normal(scale=0.3, size=(n_dates, n_inst)))
    x[:, :, 1] = (2.0 * liquidity.to_numpy()
                  + rng.normal(scale=0.3, size=(n_dates, n_inst)))
    x[:, :, 2] = rng.normal(scale=0.3, size=(n_dates, n_inst))
    y = rng.normal(size=(n_dates, n_inst))
    return dates, instruments, tier_panel, liquidity, x, y


def _wide_frame(dates, instruments, x: np.ndarray, y: np.ndarray) -> pd.DataFrame:
    """把 (日期 × 标的 × 特征) 面板转成 qlib handler 风格的宽表。"""
    index = pd.MultiIndex.from_product(
        [pd.DatetimeIndex(dates), list(instruments)], names=["datetime", "instrument"]
    )
    columns = pd.MultiIndex.from_tuples([(c,) for c in SCORE_FEATURES] + [("LABEL0",)])
    data = np.concatenate([x.reshape(-1, x.shape[2]), y.reshape(-1, 1)], axis=1)
    return pd.DataFrame(data, index=index, columns=columns)


def _pit_panel(dates, instruments) -> pd.DataFrame:
    """逐日成员面板：**每 4 只里有 1 只不是成员**。

    🔴 **为什么这样设计**：非成员那一档（``tier_csi300``）在**成员里恒定**、
    在**全体**里变动 ⇒ 打分侧若按「自己的面板」挑控制变量列，就会与训练侧分叉
    ⇒ 两侧系数不同源 ⇒ 残差不等 ⇒ 本文件 ⑬ 那条断言就是它的牙齿。
    """
    data = np.array(
        [[bool(i % 4) for i in range(len(instruments))] for _ in dates], dtype=bool
    )
    return pd.DataFrame(data, index=pd.DatetimeIndex(dates), columns=list(instruments))


class _CapturingBooster:
    """假 booster：记录每次 predict 收到的特征矩阵（用来对拍打分侧口径）。"""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def predict(self, X):
        arr = np.asarray(X, dtype=float)
        self.calls.append(arr.copy())
        # 🔴 分数必须有区分度（``_assert_non_degenerate`` 要求 ≥10 个唯一值）
        return arr[:, 0] * 0.5 + np.arange(len(arr)) * 1e-4


def _run_score(tmp_path, frame, *, neutralize, tier_panel, liquidity_panel, score_dates,
               pit_membership=None):
    """跑一次打分（monkeypatch 掉 handler 构造），返回 (booster, 打分 meta)。"""
    booster = _CapturingBooster()
    original = TS.build_score_handler
    TS.build_score_handler = lambda *a, **k: _FakeHandler(frame)
    try:
        meta = TS.score_universe(
            booster, object(), (), "SYNTH", tmp_path, "unit",
            pd.Timestamp(score_dates[0]).strftime("%Y-%m-%d"),
            pd.Timestamp(score_dates[-1]).strftime("%Y-%m-%d"),
            chunk_months=12, with_fwd=False, score_dates=[str(d) for d in score_dates],
            neutralize=neutralize, tier_panel=tier_panel,
            liquidity_panel=liquidity_panel, pit_membership=pit_membership,
        )
    finally:
        TS.build_score_handler = original
    return booster, meta


# ----------------------------------------------------------------- ① PIT 面板
class TestPitPanel:
    def test_面板遵守日期区间(self, tmp_path: Path) -> None:
        """① 区间内成员 = True；区间外 = False（🔴 静态并集会把两者都留成 True）。"""
        files = _cap_files(tmp_path)
        files["csi800"].write_text(
            "SH600000\t2020-01-01\t2020-06-30\nSH600002\t2019-01-01\t2019-12-31\n",
            encoding="utf-8",
        )
        dates = ["2020-01-02", "2020-06-30", "2020-07-01", "2019-06-03"]
        panel = universe.build_pit_membership_panel(
            dates, ["SH600000", "SH600002", "SZ600001"], files=files
        )
        assert panel.shape == (4, 3)
        # 区间内（含端点）⇒ 成员
        assert bool(panel.loc[pd.Timestamp("2020-01-02"), "SH600000"]) is True
        assert bool(panel.loc[pd.Timestamp("2020-06-30"), "SH600000"]) is True
        assert bool(panel.loc[pd.Timestamp("2019-06-03"), "SH600002"]) is True
        # 区间外 ⇒ 非成员（🔴 静态并集口径下这里会是 True ⇒ 本条就是「真缺陷」的牙齿）
        assert bool(panel.loc[pd.Timestamp("2020-07-01"), "SH600000"]) is False
        assert bool(panel.loc[pd.Timestamp("2020-01-02"), "SH600002"]) is False
        assert bool(panel.loc[pd.Timestamp("2019-06-03"), "SZ600001"]) is False

    def test_面板按日可变_同一只票可进可退(self, tmp_path: Path) -> None:
        """① 同一只票在不同年份可进可退（🔴 「今天的档位倒推历史」是错的）。"""
        files = _cap_files(tmp_path)
        files["csi800"].write_text(
            "SH600000\t2016-01-01\t2016-12-31\nSH600000\t2018-01-01\t2018-12-31\n",
            encoding="utf-8",
        )
        dates = ["2016-06-01", "2017-06-01", "2018-06-01"]
        panel = universe.build_pit_membership_panel(dates, ["SH600000"], files=files)
        assert bool(panel.loc[pd.Timestamp("2016-06-01"), "SH600000"]) is True
        assert bool(panel.loc[pd.Timestamp("2017-06-01"), "SH600000"]) is False
        assert bool(panel.loc[pd.Timestamp("2018-06-01"), "SH600000"]) is True

    def test_成分表缺失即报错(self, tmp_path: Path) -> None:
        """① fail-closed：成分表缺失 ⇒ raise（不许静默当「非成员」）。"""
        with pytest.raises(FileNotFoundError):
            universe.build_pit_membership_panel(
                ["2020-01-02"], ["SH600000"],
                files={"csi800": tmp_path / "不存在.txt"},
            )


# ----------------------------------------------------------------- ② 默认模式不变
class TestDefaultModeUnchanged:
    def test_默认模式返回键与值逐位不变(self, monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
        """② 默认模式的键集合 ＋ 值 ＝ 改动前（且**不带** PIT 面板键）。"""
        monkeypatch.setattr(C, "LARGE_CAP_INSTRUMENT_FILES", _cap_files(tmp_path))
        monkeypatch.setattr(C, "MIN_UNIVERSE_SIZE", 1)
        result = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE)
        assert set(result) == LEGACY_UNIVERSE_KEYS
        assert not any("pit" in key for key in result)
        assert result["mode"] == C.UNIVERSE_MODE
        assert result["instruments"] == ["SH600000", "SZ600002"]
        assert result["sum_w"] == {"SH600000": 10.0, "SZ600002": 5.0}
        assert result["n_stocks"] == 2
        assert result["etf_codes"] == ["510300"]
        assert result["used_fallback"] is False
        assert result["holdings_snapshot_date"] == C.HOLDINGS_SNAPSHOT_DATE

    def test_PIT口径其余键与默认口径同构(self, monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
        """② PIT 口径**只多带**一个 PIT 面板键 ⇒ 其余键与默认口径逐字相等。"""
        monkeypatch.setattr(C, "LARGE_CAP_INSTRUMENT_FILES", _cap_files(tmp_path))
        monkeypatch.setattr(C, "MIN_UNIVERSE_SIZE", 1)
        dates = ["2020-01-02", "2020-06-30"]
        default = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE)
        pit = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE_PIT,
                                      pit_dates=dates)
        assert set(pit) == LEGACY_UNIVERSE_KEYS | {C.PIT_MEMBERSHIP_KEY}
        for key in LEGACY_UNIVERSE_KEYS - {"mode"}:
            assert pit[key] == default[key], key
        assert pit["mode"] == C.UNIVERSE_MODE_PIT
        panel = pit[C.PIT_MEMBERSHIP_KEY]
        assert isinstance(panel, pd.DataFrame)
        assert list(panel.columns) == default["instruments"]
        assert [str(d.date()) for d in panel.index] == dates


# ----------------------------------------------------------------- ③ 训练帧掩码
class TestTrainingMask:
    def test_不在当日池内的行被剔除(self, spec: _Spec) -> None:
        """③ 逐日剔除「当日不在池内」的行；在池内的**保留**。"""
        handler = _FakeHandler(_panel_frame())
        panel = pd.DataFrame(
            True,
            index=pd.bdate_range("2020-01-01", periods=N_DATES),
            columns=[f"SH{600000 + i}" for i in range(N_INSTS)],
        )
        # 第 2 个交易日：SH600003 当日**不在池内**
        panel.iloc[1, panel.columns.get_loc("SH600003")] = False
        frame = TS.prepare_training_frame(
            handler, spec, _universe_with_panel(panel), neutralize=False
        )
        kept = set(zip(pd.to_datetime(frame["dates"]), frame["instruments"]))
        dropped_day = pd.Timestamp(pd.bdate_range("2020-01-01", periods=N_DATES)[1])
        assert (dropped_day, "SH600003") not in kept
        assert (dropped_day, "SH600000") in kept
        assert len(frame["y"]) == N_DATES * N_INSTS - 1
        # 🔴 掩码前后的行数必须如实记账（⇒ manifest 披露用）
        pit = frame["pit"]
        assert pit["enabled"] is True
        assert pit["n_rows_before"] == N_DATES * N_INSTS
        assert pit["n_rows_after"] == N_DATES * N_INSTS - 1
        assert pit["n_rows_dropped"] == 1

    def test_掩码早于截面中心化(self, spec: _Spec) -> None:
        """③ 截面均值按**当日池**算（掩码若晚于中心化 ⇒ 均值不再为 0）。"""
        labels = [0.0, 1.0, 2.0, 10.0] + [0.0] * (N_DATES * N_INSTS - 4)
        handler = _FakeHandler(_panel_frame(labels=labels))
        panel = pd.DataFrame(
            True,
            index=pd.bdate_range("2020-01-01", periods=N_DATES),
            columns=[f"SH{600000 + i}" for i in range(N_INSTS)],
        )
        # 首日只留 SH600000（标签 0）与 SH600003（标签 10）⇒ 当日池均值 = 5
        panel.iloc[0, :] = False
        panel.iloc[0, panel.columns.get_loc("SH600000")] = True
        panel.iloc[0, panel.columns.get_loc("SH600003")] = True
        frame = TS.prepare_training_frame(
            handler, spec, _universe_with_panel(panel), neutralize=False
        )
        first_day = pd.Timestamp(pd.bdate_range("2020-01-01", periods=N_DATES)[0])
        mask = pd.to_datetime(frame["dates"]) == first_day
        block = np.asarray(frame["y"][mask])
        assert block.size == 2
        assert abs(float(block.mean())) < 1e-9, (
            "🔴 当日池的截面均值不是 0 ⇒ 掩码没有早于中心化生效"
        )
        assert abs(float(block.min()) + 5.0) < 1e-9 and abs(float(block.max()) - 5.0) < 1e-9

    def test_面板盖不住训练日期即报错(self, spec: _Spec) -> None:
        """③ fail-closed：面板日期与训练日期对不上 ⇒ raise（不许静默丢样本）。"""
        handler = _FakeHandler(_panel_frame())
        panel = pd.DataFrame(
            True,
            index=pd.bdate_range("2019-01-01", periods=N_DATES),
            columns=[f"SH{600000 + i}" for i in range(N_INSTS)],
        )
        with pytest.raises(RuntimeError):
            TS.prepare_training_frame(
                handler, spec, _universe_with_panel(panel), neutralize=False
            )


# ----------------------------------------------------------------- ④ 打分侧不筛
class TestScoringSideUnmasked:
    def test_打分池与默认口径逐字相同(self, monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
        """④ PIT 口径的 ``instruments``（= 打分池的来源）与默认口径**逐字相同**。"""
        monkeypatch.setattr(C, "LARGE_CAP_INSTRUMENT_FILES", _cap_files(tmp_path))
        monkeypatch.setattr(C, "MIN_UNIVERSE_SIZE", 1)
        default = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE)
        pit = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE_PIT,
                                      pit_dates=["2020-01-02"])
        assert pit["instruments"] == default["instruments"]
        assert pit["sum_w"] == default["sum_w"]
        assert pit["n_stocks"] == default["n_stocks"]

    def test_打分路径喂成员面板打分行数不减少(self, tmp_path: Path) -> None:
        """⑨ 打分路径喂**成员面板** ⇒ 打分行数**不减少**（🔴 行为断言）。

        🔴 **为什么不许用源码字符串检查**：reviewer 的变异测试证明「打分侧偷偷
        加当日掩码」这一类回归**抓不住**（源码里照样没有
        ``_apply_pit_membership_mask``，
        只是把掩码换了种写法）⇒ 这里改成**行为断言**：
        喂面板前后，**打分结果的行数与行集合必须逐位相同**。
        """
        dates, instruments, tier_panel, liquidity, x, y = _synthetic()
        frame = _wide_frame(dates, instruments, x, y)
        panel = _pit_panel(dates, instruments)
        plain_booster, plain_meta = _run_score(
            tmp_path, frame, neutralize=True, tier_panel=tier_panel,
            liquidity_panel=liquidity, score_dates=dates, pit_membership=None,
        )
        masked_booster, masked_meta = _run_score(
            tmp_path, frame, neutralize=True, tier_panel=tier_panel,
            liquidity_panel=liquidity, score_dates=dates, pit_membership=panel,
        )
        assert masked_meta["n_rows"] == plain_meta["n_rows"], (
            "🔴 打分路径喂了成员面板后行数变少 ⇒ 打分侧把 PIT 掩码当成了行过滤器"
            "（打分池必须**不按日筛**）"
        )
        for k in range(len(dates)):
            assert masked_booster.calls[k].shape[0] == plain_booster.calls[k].shape[0]
        # 🔴 行集合也必须一致（不只是行数）
        scores = pd.read_csv(masked_meta["scores_path"], dtype={"instrument": str})
        assert set(scores["instrument"]) == set(instruments)

    def test_打分路径喂成员面板不筛掉非成员(self, tmp_path: Path) -> None:
        """⑨b 喂成员面板后，**当日非成员**的标的照样有分（ETF 权重有分可映射）。"""
        dates, instruments, tier_panel, liquidity, x, y = _synthetic()
        frame = _wide_frame(dates, instruments, x, y)
        panel = _pit_panel(dates, instruments)
        _booster, meta = _run_score(
            tmp_path, frame, neutralize=True, tier_panel=tier_panel,
            liquidity_panel=liquidity, score_dates=dates, pit_membership=panel,
        )
        scores = pd.read_csv(meta["scores_path"], dtype={"instrument": str})
        non_members = [c for c in instruments if not bool(panel.iloc[0][c])]
        assert non_members, "🔴 合成面板必须有非成员，否则本断言无意义"
        got = set(scores["instrument"])
        for inst in non_members:
            assert inst in got, f"🔴 非成员 {inst} 被打分侧剔掉了 ⇒ 打分池被按日筛了"


# ----------------------------------------------------------------- ⑩⑪⑫⑬ 行为断言
class TestPitBehaviorAssertions:
    """🔴 **I4**：把「源码字符串检查」换成**行为断言**（reviewer 的变异测试证明
    源码检查抓不住三类回归：打分侧偷偷加当日掩码 / 面板日期轴改自然日 /
    掩码挪到 ΣW 权重之后）。"""

    def test_掩码早于ΣW权重计算(self, spec: _Spec) -> None:
        """⑩ 掩码**早于** ΣW 权重计算（⇒ 抓「掩码挪到 ΣW 权重之后」）。"""
        handler = _FakeHandler(_panel_frame())
        n_raw = N_DATES * N_INSTS
        panel = pd.DataFrame(
            True,
            index=pd.bdate_range("2020-01-01", periods=N_DATES),
            columns=[f"SH{600000 + i}" for i in range(N_INSTS)],
        )
        panel.iloc[1, panel.columns.get_loc("SH600003")] = False
        frame = TS.prepare_training_frame(
            handler, spec, _universe_with_panel(panel), neutralize=False
        )
        pit = frame["pit"]
        # 🔴 掩码的记账口径必须是「**掩码前**的行数」（＝handler 原始行数）
        #    ⇒ 掩码若被挪到 ΣW 权重过滤之后，这个数会变小 ⇒ 本条转红
        assert pit["n_rows_before"] == n_raw
        assert pit["n_rows_dropped"] == 1
        assert pit["n_rows_after"] == n_raw - 1
        # 🔴 ΣW 权重在**掩码之后**做组内均值归一 ⇒ 每组权重均值 = 1
        weights = np.asarray(frame["weights"], dtype=float)
        pos = 0
        for size in frame["groups"]:
            block = weights[pos:pos + size]
            assert abs(float(block.mean()) - 1.0) < 1e-9, (
                "🔴 组内权重均值 ≠ 1 ⇒ ΣW 归一没有发生在掩码之后"
            )
            pos += size

    def test_面板日期轴等于交易日历而非自然日(self, monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
        """⑪ PIT 面板的日期轴必须**逐日等于交易日历**（⇒ 抓「面板日期轴改自然日」）。"""
        files = _cap_files(tmp_path)
        monkeypatch.setattr(C, "LARGE_CAP_INSTRUMENT_FILES", files)
        monkeypatch.setattr(C, "MIN_UNIVERSE_SIZE", 1)
        trading = [d.strftime("%Y-%m-%d")
                   for d in pd.bdate_range("2020-01-01", periods=8)]
        calendar = tmp_path / "day.txt"
        calendar.write_text("\n".join(trading) + "\n", encoding="utf-8")
        monkeypatch.setattr(C, "CALENDAR_FILE", calendar)
        monkeypatch.setattr(C, "PIT_PANEL_START", "2020-01-01")
        monkeypatch.setattr(C, "PIT_PANEL_END", "2020-01-31")
        pit = universe.build_universe(["510300"], _holdings(), mode=C.UNIVERSE_MODE_PIT)
        panel = pit[C.PIT_MEMBERSHIP_KEY]
        got = [str(pd.Timestamp(d).date()) for d in panel.index]
        assert got == trading, (
            f"🔴 PIT 面板的日期轴不是交易日历：{got}（自然日轴会多出周末）"
        )
        assert not any(pd.Timestamp(d).weekday() >= 5 for d in got)

    def test_manifest两处universe_mode一致(self) -> None:
        """⑫ B2：manifest 顶层 ``universe.mode`` ＝ 口径卡 ``universe.mode``。"""
        for mode in (C.UNIVERSE_MODE, C.UNIVERSE_MODE_PIT):
            info = {"mode": mode, "n_stocks": 2635}
            section, card = TS._manifest_universe_and_caliber(
                info,
                universe_name="v3_etf_pool",
                instruments_path=Path("/tmp/does-not-matter.txt"),
                listed=["SH600000"],
                pit_stats={"enabled": mode == C.UNIVERSE_MODE_PIT},
                score_meta={"score_universe": "v3_etf_pool"},
                pit_calendar_meta={"enabled": False},
            )
            assert section["mode"] == mode
            assert card["universe"]["mode"] == section["mode"], (
                f"🔴 口径卡的 universe.mode={card['universe']['mode']} ≠ "
                f"manifest 的 universe.mode={section['mode']}"
                f" ⇒ 同一份产物里两个矛盾口径"
            )

    def test_训练侧残差等于打分侧残差(self, tmp_path: Path) -> None:
        """⑬ B1：同一 (date, feature) ⇒ 训练侧残差 **≡** 打分侧残差（无 skew）。"""
        dates, instruments, tier_panel, liquidity, x, y = _synthetic()
        frame = _wide_frame(dates, instruments, x, y)
        panel = _pit_panel(dates, instruments)

        # ---- 训练侧：面板只含「当日 PIT 成员」（= prepare_training_frame 的口径）----
        long = (TS._normalize_long_frame(frame)
                .sort_values(["date", "instrument"]).reset_index(drop=True))
        member = np.array(
            [bool(panel.loc[pd.Timestamp(d), inst])
             for d, inst in zip(pd.to_datetime(long["date"]), long["instrument"])]
        )
        long = long.loc[member].reset_index(drop=True)
        x_panel, date_pos, inst_pos, d_axis, i_axis = TS._long_to_panel(
            long, list(SCORE_FEATURES)
        )
        y_panel = np.full((len(d_axis), len(i_axis)), np.nan)
        y_panel[date_pos, inst_pos] = long["LABEL0"].to_numpy(dtype=float)
        fit_mask = TS._pit_fit_mask(panel, d_axis, i_axis, "训练侧")
        x_res, _y_res, _stats = TS._neutralize_panels(
            x_panel, y_panel, d_axis, i_axis, tier_panel, liquidity, fit_mask=fit_mask
        )

        # ---- 打分侧：整个打分池（成员面板只作拟合掩码）----
        booster, meta = _run_score(
            tmp_path, frame, neutralize=True, tier_panel=tier_panel,
            liquidity_panel=liquidity, score_dates=dates, pit_membership=panel,
        )
        assert meta["neutralize"]["enabled"] is True
        for k, d in enumerate(dates):
            ts = pd.Timestamp(d)
            got = booster.calls[k]
            assert got.shape[0] == len(instruments), (
                f"🔴 {ts.date()}：打分行数 {got.shape[0]} ≠ 池子 {len(instruments)}"
            )
            di = d_axis.get_loc(ts)
            for j, inst in enumerate(instruments):
                if not bool(panel.loc[ts, inst]):
                    continue
                expected = x_res[di, i_axis.get_loc(inst), :]
                actual = got[j, :]
                assert np.allclose(actual, expected, rtol=1e-10, atol=1e-10), (
                    f"🔴 {ts.date()} {inst}：打分侧残差 ≠ 训练侧残差 ⇒ train/serve skew"
                )


# ----------------------------------------------------------------- ⑤⑥ 臂纪律
class TestArmDiscipline:
    def test_默认臂列表仍是6臂(self) -> None:
        """⑤ 默认臂**不许**变 7 臂（新臂只能显式指定才跑）。"""
        probe = _load_probe()
        defs = probe.arm_definitions()
        assert len(defs) == 6
        assert set(defs) == LEGACY_ARMS
        assert "v3_pit_pool" not in defs
        # 🔴 opt-in 臂存在，但**只在显式指定时**可达
        assert "v3_pit_pool" in probe.all_arm_definitions()
        assert "v3_pit_pool" in probe.opt_in_arm_definitions()

    def test_默认命令行不跑新臂(self) -> None:
        """⑤ CLI 默认（不给 ``--arm`` / 不给 ``--arms``）⇒ 不含新臂。"""
        probe = _load_probe()
        args = probe.parse_args([])
        assert "v3_pit_pool" not in str(args.arms).split(",")
        assert args.arm is None
        explicit = probe.parse_args(["--arm", "v3_pit_pool"])
        assert explicit.arm == "v3_pit_pool"

    def test_v3_pit_pool相对v3_full只差训练池口径(self) -> None:
        """⑥ 单变量守卫：超参逐项相同 ⇒ 差异只在训练池口径。"""
        probe = _load_probe()
        defs = probe.all_arm_definitions()
        full = defs["v3_full"]
        pit = defs["v3_pit_pool"]
        for key in SINGLE_VARIABLE_PARAM_KEYS:
            assert pit[key] == full[key], f"🔴 两臂在 {key} 上不一致 ⇒ 不是单变量"
        assert pit["universe_mode"] == C.UNIVERSE_MODE_PIT
        assert full.get("universe_mode", C.UNIVERSE_MODE) == C.UNIVERSE_MODE
        diff = {k for k in set(full) | set(pit) if full.get(k) != pit.get(k)}
        assert diff == {"label", "universe", "universe_mode"}, (
            f"🔴 差异键超出「口径描述 + 训练池口径」：{sorted(diff)}"
        )

    def test_训练池口径由臂决定(self) -> None:
        """⑥ 训练链**必须**按臂的口径建池（默认臂 ⇒ 仍走 ``config_v3.UNIVERSE_MODE``）。"""
        source = (_TOOLS_DIR / "v3_comparison_probe.py").read_text(encoding="utf-8")
        assert 'arm.get("universe_mode")' in source
        worker = (_TOOLS_DIR / "v3_fold_worker.py").read_text(encoding="utf-8")
        assert '"--universe-mode"' in worker


# ----------------------------------------------------------------- ⑦ 默认口径常量
def test_默认口径常量不许动() -> None:
    """⑦ 新口径**默认关闭**：`UNIVERSE_MODE` 仍是静态并集口径。"""
    assert C.UNIVERSE_MODE == "etf_constituent_large"
    assert C.UNIVERSE_MODE_LARGE == "etf_constituent_large"
    assert C.UNIVERSE_MODE_PIT == "etf_constituent_large_pit"
    assert C.UNIVERSE_MODE_PIT != C.UNIVERSE_MODE
    # 🔴 口径卡必须写清「打分池不按日筛」与「规模中性化语义翻转」
    card = C.v3_caliber_card()["universe"]
    assert card["mode"] == C.UNIVERSE_MODE
    assert card["mode_pit"] == C.UNIVERSE_MODE_PIT
    assert "不按日筛" in card["scoring_pool"]
    assert "45.98%" in card["tier_note"]
    assert C.UNIVERSE_MODE_PIT_DESC in card["membership"]["pit"]


# ------------------------------------------------------------ reviewer 审查修复专项
class TestReviewFixes:
    """🔴 2026-11-27 reviewer 审查发现（BLOCKING ×2 + IMPORTANT ×5）的**行为断言**。

    ★ 每条断言都对着**缺陷本身**写（不是对着源码字符串写）⇒ 缺陷还在 ⇒ 必转红。
    """

    # ---- I1：掩码**标的侧** fail-closed
    def test_I1_面板缺标的即报错_不许静默当非成员(self, spec: _Spec) -> None:
        """I1：面板里**根本没有**的标的 ⇒ raise（旧实现静默当「当日非成员」）。"""
        handler = _FakeHandler(_panel_frame())
        panel = pd.DataFrame(
            True,
            index=pd.bdate_range("2020-01-01", periods=N_DATES),
            columns=[f"SH{600000 + i}" for i in range(N_INSTS)],
        )
        panel = panel.drop(columns=["SH600003"])
        with pytest.raises(RuntimeError) as ei:
            TS.prepare_training_frame(
                handler, spec, _universe_with_panel(panel), neutralize=False
            )
        assert "SH600003" in str(ei.value), (
            f"🔴 报错信息必须点名缺失标的：{ei.value}"
        )

    # ---- I2：打分池守卫阈值必须来自整段 OOS 实测
    def test_I2_阈值来自整段OOS实测且落在分离带内(self) -> None:
        """I2①：阈值**不许**来自单日；必须落在实测分离带 (0.69154, 0.82708] 内。"""
        legacy = 1 - C.SCORE_POOL_MAX_WEIGHT_LOSS - C.SCORE_POOL_COVERAGE_TOL
        assert legacy == pytest.approx(0.9797), (
            f"🔴 历史留档的单日阈值被改了：{legacy}"
        )
        assert not (0.69154 < legacy < 0.82708), (
            "🔴 现行阈值若等于单日阈值 0.9797 ⇒ 一个坏日就能让整臂算力作废"
        )
        assert 0.69154 < C.SCORE_POOL_COVERAGE_MIN_MEDIAN < 0.82708
        assert C.SCORE_POOL_COVERAGE_MIN_DAY == pytest.approx(
            0.63183 - C.SCORE_POOL_COVERAGE_TOL, abs=1e-5
        )
        assert C.SCORE_POOL_MIN_SIGNAL_DAYS_FOR_MEDIAN == 10
        assert "2019-01-01~2023-12-31" in C.SCORE_POOL_COVERAGE_MEASUREMENT_NOTE

    @staticmethod
    def _写打分文件(tmp_path: Path, dates, n_scored: int, n_total: int) -> Path:
        """写一份合成打分结果（每个信号日只有前 ``n_scored`` 只标的**有分**）。"""
        insts = [f"SZ{600000 + i:06d}" for i in range(n_total)]
        rows = []
        for d in dates:
            for inst in insts[:n_scored]:
                rows.append({"date": str(pd.Timestamp(d).date()), "instrument": inst})
        path = tmp_path / "scores.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

    @staticmethod
    def _打分池(n_total: int) -> dict:
        """静态打分池的 ``info``（ΣW 等权 ⇒ 覆盖率 ＝ 有分标的数 / 总数）。"""
        insts = [f"SZ{600000 + i:06d}" for i in range(n_total)]
        return {"sum_w": {inst: 1.0 for inst in insts}}

    def test_I2_信号日数不足10时不启用中位数判定(self, tmp_path: Path) -> None:
        """I2②：信号日数 < 10 ⇒ **不许**用中位数一刀切（改用结构性检查为主）。"""
        dates = pd.bdate_range("2020-03-02", periods=3)
        # 覆盖率 0.7：低于中位数阈值 0.75931、高于逐日底线 0.62683
        path = self._写打分文件(tmp_path, dates, n_scored=7, n_total=10)
        out = TS._assert_score_pool_covers_etf_union(
            self._打分池(10), {"scores_path": path}
        )
        assert out["etf_union_weight_coverage_median"] == pytest.approx(0.7)
        assert out["coverage_median_check_applied"] is False, (
            "🔴 信号日数只有 3 天却用了中位数一刀切"
        )
        assert "信号日数 3 < 10" in out["coverage_median_check_skipped_reason"]

    def test_I2_信号日数足够时中位数判定生效(self, tmp_path: Path) -> None:
        """I2②：信号日数 ≥ 10 ⇒ 中位数判定**必须**生效（否则阈值形同虚设）。"""
        dates = pd.bdate_range("2020-03-02", periods=12)
        path = self._写打分文件(tmp_path, dates, n_scored=7, n_total=10)
        with pytest.raises(RuntimeError) as ei:
            TS._assert_score_pool_covers_etf_union(
                self._打分池(10), {"scores_path": path}, context="fold3"
            )
        msg = str(ei.value)
        assert "中位数" in msg
        assert "fold3" in msg, f"🔴 失败信息必须带折号/窗口：{msg}"
        assert "信号日数 12" in msg, f"🔴 失败信息必须带信号日数：{msg}"

    def test_I2_失败信息带最差日期信号日数与折号(self, tmp_path: Path) -> None:
        """I2③：逐日底线被击穿 ⇒ 报错信息必须带**最差日期 + 信号日数 + 折号/窗口**。"""
        dates = pd.bdate_range("2020-03-02", periods=3)
        path = self._写打分文件(tmp_path, dates, n_scored=5, n_total=10)
        with pytest.raises(RuntimeError) as ei:
            TS._assert_score_pool_covers_etf_union(
                self._打分池(10), {"scores_path": path}, context="fold2/窗口2019Q3"
            )
        msg = str(ei.value)
        assert "2020-03-02" in msg, f"🔴 失败信息必须带最差日期：{msg}"
        assert "信号日数 3" in msg, f"🔴 失败信息必须带信号日数：{msg}"
        assert "fold2" in msg, f"🔴 失败信息必须带折号/窗口：{msg}"

    # ---- I5：探针 meta **按臂**记录口径与规模
    def test_I5_探针meta按臂记录口径与规模(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """I5：PIT 臂**不得**被写成静态池 ⇒ meta 必须按臂记口径与规模。"""
        probe = _load_probe()
        defs = {
            "v3_full": {"universe_mode": C.UNIVERSE_MODE},
            "v3_pit_pool": {"universe_mode": C.UNIVERSE_MODE_PIT},
        }

        def fake_build_universe(etf_codes, holdings, mode=None, provider_uri=None):
            info = {"mode": mode, "n_stocks": 2635}
            if mode == C.UNIVERSE_MODE_PIT:
                panel = pd.DataFrame(
                    True,
                    index=pd.bdate_range("2020-01-01", periods=3),
                    columns=["SH600000", "SH600001"],
                )
                panel.iloc[0, 0] = False
                info[C.PIT_MEMBERSHIP_KEY] = panel
            return info

        monkeypatch.setattr(universe, "build_universe", fake_build_universe)
        mode, size, daily = probe.per_arm_universe_meta(
            list(defs), defs, {"n_stocks": 2635}, {"SH600000", "SH600001"},
            ["510300"], None, "uri://synthetic",
        )
        assert mode["v3_full"] == C.UNIVERSE_MODE
        assert mode["v3_pit_pool"] == C.UNIVERSE_MODE_PIT, (
            "🔴 PIT 臂的口径被写成了静态口径 ⇒ 披露 14 会把 PIT 臂说成静态池"
        )
        assert size["v3_pit_pool"]["static_pool"] == 2635
        assert daily["v3_pit_pool"]["min"] == 1
        assert daily["v3_pit_pool"]["max"] == 2
        assert daily["v3_full"] == {}, "🔴 静态臂不该有「逐日成员数」"

    # ---- NIT9：`--arm` 与 `--arms` 同时给出 ⇒ raise
    def test_NIT9_arm与arms同时给出即报错(self) -> None:
        """NIT9：``--arm`` 与 ``--arms`` 同时给出 ⇒ raise（旧实现静默覆盖）。"""
        probe = _load_probe()
        with pytest.raises(ValueError):
            probe.main(["--arm", "v3_pit_pool", "--arms", "v3_full,v3_mse"])
        # 🔴 只给一个 ⇒ 不许报错
        probe.parse_args(["--arm", "v3_pit_pool"])
        probe.parse_args(["--arms", "v3_full"])

    # ---- NIT10：两列行（代码 + start）⇒ 自 start 起长期有效
    def test_NIT10_两列成分表行按自start起长期有效(self, tmp_path: Path) -> None:
        """NIT10：两列行**不许**当成「长期有效」（那是静默放大池子）。"""
        path = tmp_path / "csi800.txt"
        path.write_text("SH600000\t2020-01-01\n", encoding="utf-8")
        code, start, end = universe.read_instrument_intervals(path)[0]
        assert code == "SH600000"
        assert start == pd.Timestamp("2020-01-01")
        assert end == pd.Timestamp.max
        panel = universe.build_pit_membership_panel(
            ["2019-12-31", "2020-01-02"], ["SH600000"],
            files={"csi800": path, "csi1000": path},
        )
        assert bool(panel.loc[pd.Timestamp("2019-12-31"), "SH600000"]) is False, (
            "🔴 两列行被当成「长期有效」⇒ start 之前的日子也被算成成员 ⇒ 池子被静默放大"
        )
        assert bool(panel.loc[pd.Timestamp("2020-01-02"), "SH600000"]) is True
