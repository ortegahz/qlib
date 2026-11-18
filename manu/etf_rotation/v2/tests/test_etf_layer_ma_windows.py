"""🔴 V2 ETF 层 **MA 窗口（进场 / 退场）真的生效** 的钉死测试。

【本文件钉死（自带夹具，不碰真实行情 / 不联网 / 不跑重量级回测）】

  ① ``compute_features`` **按窗口**给均线（``ma{w}`` / ``above_ma{w}``），
     且 ``ma60`` 与**旧口径逐位一致**（默认调用 ⇒ 既有调用方行为不变）；
  ② ``build_targets`` 的**退场 MA**用 ``ma{exit_ma_window}`` ⇒
     ``exit ∈ {40, 80}`` 时**不再每次调仓全清仓**（相邻调仓持仓有延续），
     且结果与 ``exit=60`` **不同**；
  ③ ``build_targets`` 的**进场 MA**用 ``above_ma{entry_ma_window}`` ⇒
     ``entry ∈ {40, 80}`` 的目标权重**确实不同**；
  ④ 特征里没有对应窗口的均线 ⇒ **按需补算**（**绝不静默退回 ma60**）；
  ⑤ **变异反证**：把源码改坏 ⇒ 对应的主断言**必须转红**
     （⛔ **绝不落盘改真源码**：源码 → 内存改 → tmp_path → import 副本）。

【被修的 bug（审计已实测确认，不是猜测）】
  * 旧实现退场均线写成 ``ma_exit = r["ma60"] if exit_ma_window == MA_TREND_WINDOW else np.nan``
    ⇒ ``exit ≠ 60`` 时 ``ma_exit`` 恒为 NaN ⇒ **每次调仓把全部持仓无条件清仓**；
  * 旧实现进场过滤恒用 ``pool["above_ma60"]`` ⇒ ``entry_ma_window`` **完全无效**。

【夹具设计（合成价格 ⇒ MA 关系可预期）】
  * ``510300``（A）：单调上行 ⇒ **恒在** MA40/MA60/MA80 之上（永远选得上 ⇒ 持仓可延续）；
  * ``510500``（X）：``[50×200][200×20][50×20][95×20]`` ⇒ 末段 **上 MA40、下 MA60/MA80**；
  * ``510880``（Y）：``[50×180][200×20][80×20][60×20][100×20]`` ⇒ 末段 **上 MA40/MA60、下 MA80**；
  * ``159915``（B）：单调下行 ⇒ **恒在** 均线之下（进场闸门永远挡住它）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import etf_layer as EL  # noqa: E402

ETF_LAYER_SOURCE = _V2_DIR / "etf_layer.py"

#: 夹具用的合成 ETF（**不碰真实行情**）
CODES = ("510300", "510500", "510880", "159915")
NAMES = {
    "510300": "红利ETF",        # 族：红利
    "510500": "银行ETF",        # 族：银行
    "510880": "医药ETF",        # 族：医药
    "159915": "通信ETF",        # 族：通信
}
#: 穿透分 E 的**固定** alpha 分（⇒ 排名恒为 510300 > 510500 > 510880 > 159915）
ALPHA = {"510300": 1.0, "510500": 0.6, "510880": 0.4, "159915": -1.0}
INST = {code: f"SH60000{i}" for i, code in enumerate(CODES)}
#: 夹具交易日数（够算 MA80）
N_DAYS = 260


# --------------------------------------------------------------------- 夹具
def _fixture() -> dict:
    """自造一整套「行情 + 个股分数 + 成分股权重」夹具（自足 ⇒ 不联网、不读真实数据）。"""
    days = pd.bdate_range("2021-01-04", periods=N_DAYS)
    up = 1.0 + 0.005 * np.arange(N_DAYS)                        # A：单调上行
    down = 3.0 - 0.005 * np.arange(N_DAYS)                      # B：单调下行
    x = np.concatenate([np.full(200, 50.0), np.full(20, 200.0),
                        np.full(20, 50.0), np.full(20, 95.0)])    # X：末段 上 MA40 / 下 MA60
    y = np.concatenate([np.full(180, 50.0), np.full(20, 200.0), np.full(20, 80.0),
                        np.full(20, 60.0), np.full(20, 100.0)])   # Y：末段 上 MA60 / 下 MA80
    close = pd.DataFrame({"510300": up, "510500": x, "510880": y, "159915": down},
                         index=days)
    amount = pd.DataFrame(1.0e8, index=days, columns=close.columns)
    # 调仓日：末段每 2 个交易日一个（覆盖 X/Y 的 MA 关系切换点）
    dates = list(days[239::2])
    scores = pd.DataFrame(
        [{"date": day, "instrument": INST[code], "score": ALPHA[code]}
         for day in days for code in CODES]
    )
    weights = {code: {INST[code]: 1.0} for code in CODES}
    return {
        "days": days,
        "dates": dates,
        "scores": scores,
        "weights": weights,
        "close": close,
        "amount": amount,
        "features": EL.compute_features(close, amount),
        "names": dict(NAMES),
    }


def _build(fixture: dict, layer=None, features=None, **kwargs):
    """调一次 ``build_targets``（动量权重置 0 ⇒ 排名只由 alpha 分决定 ⇒ 夹具的差别只来自 MA 窗口）。"""
    layer = EL if layer is None else layer          # 🔴 调用时解析 ⇒ 变异副本可替换
    kwargs.setdefault("top_k", 3)
    kwargs.setdefault("hold_buffer", 0)
    kwargs.setdefault("alpha_weight", 1.0)
    kwargs.setdefault("momentum_weight", 0.0)
    kwargs.setdefault("min_history_days", 1)
    feats = fixture["features"] if features is None else features
    return layer.build_targets(
        fixture["dates"], fixture["scores"], fixture["weights"],
        fixture["close"], feats, names=fixture["names"],
        rebalance_signal_stride=1, **kwargs,
    )


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把 ``etf_layer`` 源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。"""
    text = ETF_LAYER_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- 取数小工具
def _overlap_pairs(targets: dict) -> int:
    """相邻调仓之间**持仓有延续**的次数（退场 MA 退化 ⇒ 每次全清仓 ⇒ 恒为 0）。"""
    keys = sorted(targets)
    return sum(1 for prev, cur in zip(keys, keys[1:])
               if set(targets[prev]) & set(targets[cur]))


def _held_dates(targets: dict, code: str) -> list[str]:
    """某只 ETF 被持有的调仓日列表。"""
    return [k for k in sorted(targets) if code in targets[k]]


def _dropped_dates(targets_a: dict, targets_b: dict, code: str) -> list[str]:
    """「A 持有、B 没持有」的调仓日列表（⇒ 两个窗口的差别落在该标的上）。"""
    return [k for k in sorted(targets_a)
            if code in targets_a[k] and code not in targets_b.get(k, {})]


# --------------------------------------------------------------------- 主断言（可被变异反证复用）
def _assert_ma_windows_distinct(feats: dict, close: pd.DataFrame) -> None:
    """① 主断言：不同窗口给出**不同**均线，且 ``ma60`` 与旧口径逐位一致。"""
    for win in (40, 60, 80):
        assert f"ma{win}" in feats, f"🔴 缺 ma{win}"
        assert f"above_ma{win}" in feats, f"🔴 缺 above_ma{win}"
    assert not feats["ma40"].equals(feats["ma60"]), "🔴 ma40 与 ma60 一样 ⇒ 窗口没生效"
    assert not feats["ma60"].equals(feats["ma80"]), "🔴 ma60 与 ma80 一样 ⇒ 窗口没生效"
    assert not feats["above_ma40"].equals(feats["above_ma60"]), "🔴 above_ma40 与 above_ma60 一样"
    # 旧口径（写死的 MA60）必须**逐位**一致
    old = close.astype("float64").sort_index().rolling(60, min_periods=60).mean()
    pd.testing.assert_frame_equal(feats["ma60"], old, check_exact=True)


def _assert_exit_windows_effective(t40: dict, t80: dict, t60: dict) -> None:
    """② 主断言：退场窗口 ``{40, 80}`` 不再每次全清仓，且与 ``60`` 结果不同。"""
    assert _overlap_pairs(t40) > 0, "🔴 exit=40 仍是每次调仓全清仓（相邻调仓零延续）"
    assert _overlap_pairs(t80) > 0, "🔴 exit=80 仍是每次调仓全清仓（相邻调仓零延续）"
    assert t40 != t60, "🔴 exit=40 与 exit=60 结果逐位相同 ⇒ exit 窗口仍然无效"
    assert t80 != t60, "🔴 exit=80 与 exit=60 结果逐位相同 ⇒ exit 窗口仍然无效"
    # 末段：X 上 MA40 但下 MA60 ⇒ exit=40 留得住、exit=60 被踢掉
    assert _held_dates(t40, "510500"), "🔴 exit=40 下 X（未破 MA40）不该被清仓"
    assert _dropped_dates(t40, t60, "510500"), (
        "🔴 没有任何一天出现「exit=40 留、exit=60 踢」⇒ exit 窗口没生效"
    )


def _assert_entry_windows_effective(t40: dict, t60: dict, t80: dict) -> None:
    """③ 主断言：进场窗口 ``{40, 80}`` 的目标权重**确实不同**。"""
    assert t40 != t60, "🔴 entry=40 与 entry=60 逐位相同 ⇒ entry 窗口仍然无效"
    assert t80 != t60, "🔴 entry=80 与 entry=60 逐位相同 ⇒ entry 窗口仍然无效"
    assert t40 != t80, "🔴 entry=40 与 entry=80 逐位相同"
    # 末段：X 上 MA40 / 下 MA60、Y 上 MA60 / 下 MA80 ⇒ 三档进场的池子各不相同
    last = sorted(t40)[-1]
    assert set(t40[last]) == {"510300", "510500", "510880"}, f"🔴 entry=40 的池子不对：{t40[last]}"
    assert set(t60[last]) == {"510300", "510880"}, f"🔴 entry=60 的池子不对：{t60[last]}"
    assert set(t80[last]) == {"510300"}, f"🔴 entry=80 的池子不对：{t80[last]}"
    # 权重也必须不同（不是只换键）
    assert t40[last]["510300"] != t60[last]["510300"], "🔴 entry 窗口没改到权重"


# --------------------------------------------------------------------- ① 特征
def test_不同窗口给出不同均线且ma60与旧口径逐位一致() -> None:
    """``compute_features`` 必须**按窗口**给均线；``ma60`` 保持旧口径逐位一致。"""
    fx = _fixture()
    feats = EL.compute_features(fx["close"], fx["amount"], ma_windows=(40, 60, 80))
    _assert_ma_windows_distinct(feats, fx["close"])
    # 默认调用（不传 ma_windows）⇒ 与旧口径逐位一致、键集合不变（既有调用方行为不变）
    default = EL.compute_features(fx["close"], fx["amount"])
    old = fx["close"].astype("float64").sort_index().rolling(60, min_periods=60).mean()
    pd.testing.assert_frame_equal(default["ma60"], old, check_exact=True)
    assert set(default) == {"mom20", "mom60", "mom120", "ma60", "above_ma60", "amount20d"}, (
        f"🔴 默认特征键集合变了：{sorted(default)}"
    )


# --------------------------------------------------------------------- ② 退场窗口
def test_退场窗口40_80不再每次全清仓且与60结果不同() -> None:
    """退场 MA 必须用 ``ma{exit_ma_window}``：``exit ∈ {40,80}`` 不再每次调仓全清仓。"""
    fx = _fixture()
    # 🔴 进场闸门关掉（entry=0）⇒ 只留退场闸门 ⇒ 观察到的差别**只**来自退场窗口
    t40, _ = _build(fx, entry_ma_window=0, exit_ma_window=40)
    t80, _ = _build(fx, entry_ma_window=0, exit_ma_window=80)
    t60, _ = _build(fx, entry_ma_window=0, exit_ma_window=60)
    _assert_exit_windows_effective(t40, t80, t60)


def test_退场窗口0表示不启用退场MA() -> None:
    """``exit_ma_window <= 0`` ⇒ 不启用退场闸门（保持既有语义）。"""
    fx = _fixture()
    t0, _ = _build(fx, entry_ma_window=0, exit_ma_window=0)
    assert t0, "🔴 exit=0 时不该产出空目标"
    assert _held_dates(t0, "510500"), "🔴 exit=0 时不该有任何强制清仓"


# --------------------------------------------------------------------- ③ 进场窗口
def test_进场窗口40_80的目标权重确实不同() -> None:
    """进场 MA 必须用 ``above_ma{entry_ma_window}`` ⇒ 不同窗口给出不同目标权重。"""
    fx = _fixture()
    # 🔴 退场闸门关掉（exit=0）⇒ 只留进场闸门 ⇒ 观察到的差别**只**来自进场窗口
    t40, _ = _build(fx, entry_ma_window=40, exit_ma_window=0)
    t60, _ = _build(fx, entry_ma_window=60, exit_ma_window=0)
    t80, _ = _build(fx, entry_ma_window=80, exit_ma_window=0)
    _assert_entry_windows_effective(t40, t60, t80)


def test_进场窗口0表示不启用进场MA() -> None:
    """``entry_ma_window <= 0`` ⇒ 不启用进场闸门（保持既有语义）。"""
    fx = _fixture()
    t0, _ = _build(fx, entry_ma_window=0, exit_ma_window=0)
    t60, _ = _build(fx, entry_ma_window=60, exit_ma_window=0)
    last = sorted(t0)[-1]
    assert len(t0[last]) > len(t60[last]), (
        f"🔴 entry=0 时不该有进场过滤：{t0[last]} vs {t60[last]}"
    )


# --------------------------------------------------------------------- ④ 补算 + 诊断
def test_特征里没有该窗口时按需补算且与预计算一致() -> None:
    """特征里没有 ``ma{win}`` ⇒ **按需补算**（绝不静默退回 ma60），结果与预计算逐位一致。"""
    fx = _fixture()
    feats_full = EL.compute_features(fx["close"], fx["amount"], ma_windows=(40, 60, 80))
    assert "ma40" in feats_full
    t_default, _ = _build(fx, entry_ma_window=40, exit_ma_window=40)
    t_full, _ = _build(fx, features=feats_full, entry_ma_window=40, exit_ma_window=40)
    assert t_default == t_full, "🔴 按需补算的结果与预计算不一致 ⇒ 两套口径分叉了"
    # 补算出来的必须是 MA40（不是 MA60）
    t60, _ = _build(fx, entry_ma_window=60, exit_ma_window=60)
    assert t_default != t60, "🔴 补算窗口没生效（退回了 ma60？）"


def test_诊断文案标出用的窗口() -> None:
    """诊断里必须看得出用的是哪个窗口（``跌破 MA40 ⇒ 不许新开仓`` 之类）。"""
    fx = _fixture()
    _, diag = _build(fx, entry_ma_window=40, exit_ma_window=80)
    assert "ma40" in diag.columns and "above_ma40" in diag.columns
    assert "ma80" in diag.columns and "above_ma80" in diag.columns
    reasons = set(diag["reason"].astype(str))
    assert any("MA40" in r for r in reasons), f"🔴 诊断里看不到 MA40：{sorted(reasons)}"
    assert any("MA80" in r for r in reasons), f"🔴 诊断里看不到 MA80：{sorted(reasons)}"


# --------------------------------------------------------------------- ⑤ 变异反证
def test_变异反证_退场均线退回nan分支_主断言必须转红(tmp_path: Path) -> None:
    """把退场均线改回 ``np.nan`` 分支 ⇒ 第②条主断言**必须转红**（每次调仓全清仓）。"""
    mutant = _mutated_module(
        tmp_path,
        [("                ma_exit = r[exit_col]\n",
          "                ma_exit = r[\"ma60\"] if exit_ma_window == MA_TREND_WINDOW else np.nan\n")],
        "ma_exit_nan",
    )
    fx = _fixture()
    t40, _ = _build(fx, layer=mutant, entry_ma_window=0, exit_ma_window=40)
    t80, _ = _build(fx, layer=mutant, entry_ma_window=0, exit_ma_window=80)
    t60, _ = _build(fx, layer=mutant, entry_ma_window=0, exit_ma_window=60)
    with pytest.raises(AssertionError):
        _assert_exit_windows_effective(t40, t80, t60)
    # 真源码（未变异）必须通过同一条主断言
    r40, _ = _build(fx, entry_ma_window=0, exit_ma_window=40)
    r80, _ = _build(fx, entry_ma_window=0, exit_ma_window=80)
    r60, _ = _build(fx, entry_ma_window=0, exit_ma_window=60)
    _assert_exit_windows_effective(r40, r80, r60)


def test_变异反证_进场过滤退回above_ma60_主断言必须转红(tmp_path: Path) -> None:
    """把进场过滤改回 ``above_ma60`` ⇒ 第③条主断言**必须转红**（entry 窗口彻底失效）。"""
    mutant = _mutated_module(
        tmp_path,
        [("            pool = pool[pool[f\"above_ma{entry_win}\"]].copy()\n",
          "            pool = pool[pool[\"above_ma60\"]].copy()\n")],
        "entry_ma60",
    )
    fx = _fixture()
    t40, _ = _build(fx, layer=mutant, entry_ma_window=40, exit_ma_window=0)
    t60, _ = _build(fx, layer=mutant, entry_ma_window=60, exit_ma_window=0)
    t80, _ = _build(fx, layer=mutant, entry_ma_window=80, exit_ma_window=0)
    with pytest.raises(AssertionError):
        _assert_entry_windows_effective(t40, t60, t80)
    # 真源码（未变异）必须通过同一条主断言
    r40, _ = _build(fx, entry_ma_window=40, exit_ma_window=0)
    r60, _ = _build(fx, entry_ma_window=60, exit_ma_window=0)
    r80, _ = _build(fx, entry_ma_window=80, exit_ma_window=0)
    _assert_entry_windows_effective(r40, r60, r80)


def test_变异反证_特征窗口写死60_主断言必须转红(tmp_path: Path) -> None:
    """把 ``_ma_panel`` 的窗口写死成 60 ⇒ 第①条主断言**必须转红**（不同窗口给不出不同均线）。"""
    mutant = _mutated_module(
        tmp_path,
        [("    win = int(window)\n", "    win = int(MA_TREND_WINDOW)\n")],
        "ma_window_hardcode",
    )
    fx = _fixture()
    feats = mutant.compute_features(fx["close"], fx["amount"], ma_windows=(40, 60, 80))
    with pytest.raises(AssertionError):
        _assert_ma_windows_distinct(feats, fx["close"])
    # 真源码（未变异）必须通过同一条主断言
    real = EL.compute_features(fx["close"], fx["amount"], ma_windows=(40, 60, 80))
    _assert_ma_windows_distinct(real, fx["close"])
