"""🔴 V2 ETF 层：**行情面板起点之前**的信号日必须**被跳过**（不许抛 IndexError）。

【本文件钉死】
  ① 信号日早于 ETF 行情面板首日 ⇒ ``build_targets`` **不抛异常**、**不产生该日目标**；
  ② 面板内的正常信号日**逐字不变**（守卫不污染正常路径）；
  ③ **变异反证**：把守卫改坏（``if False``）⇒ 断言①必须转红（IndexError 复现）。

【被修的 bug（实测崩溃，不是猜测）】
  2026-11-18 10:04，5 折滚动前推全量跑在**阶段 B**（逐折回测）崩了::

      File "manu/etf_rotation/v2/etf_layer.py", line 470, in build_targets
        last_close = px.loc[px.index <= day].iloc[-1]
      IndexError: single positional indexer is out-of-bounds

  根因：5 折验证段的**预热/锚点信号日**可能落在 ETF 行情面板起点**之前**
  （封存面板起点 = 2020-01-02；折 2 的锚点落在 2019 年）
  ⇒ ``px.loc[px.index <= day]`` 为空 ⇒ ``iloc[-1]`` 越界。

【夹具】合成价格（不联网、不读真实数据）：3 只 ETF × 60 个交易日，
  外加**一个早于面板首日**的信号日（且**故意给了当日的个股分数**
  ⇒ 保证能走到 ``build_targets`` 的价格取数那一行，而不是被"没有分数"提前短路）。
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

CODES = ("510300", "510500", "510880")
NAMES = {"510300": "红利ETF", "510500": "银行ETF", "510880": "医药ETF"}
ALPHA = {"510300": 1.0, "510500": 0.5, "510880": -0.5}
INST = {code: f"SH60000{i}" for i, code in enumerate(CODES)}
N_DAYS = 60
#: 面板首日 = 2021-01-04（下面 60 个交易日）；**早于它**的信号日
PANEL_START = pd.Timestamp("2021-01-04")
EARLY_DAY = pd.Timestamp("2020-12-01")


def _fixture() -> dict:
    days = pd.bdate_range(PANEL_START, periods=N_DAYS)
    close = pd.DataFrame(
        {c: 1.0 + 0.001 * (i + 1) * np.arange(N_DAYS) for i, c in enumerate(CODES)},
        index=days,
    )
    amount = pd.DataFrame(1.0e8, index=days, columns=close.columns)
    score_days = [EARLY_DAY, *days]
    scores = pd.DataFrame(
        [{"date": day, "instrument": INST[code], "score": ALPHA[code]}
         for day in score_days for code in CODES]
    )
    weights = {code: {INST[code]: 1.0} for code in CODES}
    return {
        "days": days,
        "scores": scores,
        "weights": weights,
        "close": close,
        "amount": amount,
        "features": EL.compute_features(close, amount),
        "names": dict(NAMES),
    }


def _build(fixture: dict, dates: list[pd.Timestamp], layer=None):
    layer = layer if layer is not None else EL
    return layer.build_targets(
        dates, fixture["scores"], fixture["weights"], fixture["close"],
        fixture["features"], names=fixture["names"],
        top_k=2, hold_buffer=0, alpha_weight=1.0, momentum_weight=0.0,
        min_history_days=1, rebalance_signal_stride=1,
    )


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str,
                   optional: bool = False):
    """🔴 把 ``etf_layer`` 源码读出来 → 内存改 → 写到 tmp_path → import 副本（绝不动真源码）。

    ``optional=True`` ⇒ 锚点不存在时**跳过**（用于「源码可能是变体 A 也可能是变体 B」的场景），
    但**至少要改到一处**，否则视为变异失败（⇒ 测试会误判）。
    """
    text = ETF_LAYER_SOURCE.read_text(encoding="utf-8")
    applied = 0
    for old, new in edits:
        if old not in text:
            if optional:
                continue
            raise AssertionError(f"变异锚点不存在（源码已变）：{old!r}")
        text = text.replace(old, new, 1)
        applied += 1
    assert applied >= 1, "🔴 变异一个都没生效（源码形态与预期不符）"
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- 主断言
def test_面板起点前的信号日_不抛异常且不产生持仓() -> None:
    """① 早于面板首日的信号日 ⇒ 不抛 IndexError、该日**没有任何持仓**。

    🔴 断言写成「**该日不得有非空权重**」（而不是"键必须不存在"）：
       本 bug 有**两层防御**（① 早退跳过该日 ② ``last_close`` 取不到时降级成 NaN）
       ⇒ 无论哪一层生效，结果都必须是「那天不持仓」⇒ 断言对两层都成立。
    """
    fx = _fixture()
    targets, _diag = _build(fx, [EARLY_DAY])
    key = EARLY_DAY.strftime("%Y-%m-%d")
    assert not targets.get(key), (
        f"🔴 面板起点之前的日子**不该**有持仓（实得 {targets.get(key)}）"
    )


def test_面板起点前的信号日_与面板内信号日混跑_只丢面板前那一天() -> None:
    """② 混合输入（面板前 + 面板内）⇒ 面板内那天的目标**照常产出**，守卫不误伤。"""
    fx = _fixture()
    inside = fx["days"][-1]
    targets, _diag = _build(fx, [EARLY_DAY, inside])
    key_in = inside.strftime("%Y-%m-%d")
    key_out = EARLY_DAY.strftime("%Y-%m-%d")
    assert key_in in targets, "🔴 面板内的信号日被误伤"
    assert not targets.get(key_out), "🔴 面板起点前的信号日不该有持仓"
    assert targets[key_in], "🔴 面板内信号日的目标权重为空"


# --------------------------------------------------------------------- 变异反证
def test_变异反证_关掉守卫_必须复现IndexError(tmp_path: Path) -> None:
    """③ 把守卫改坏（``if False``）⇒ 断言①必须转红（IndexError 复现 ⇒ 守卫确实是拦门的那道）。"""
    # 🔴 关掉防御才能复现 2026-11-18 10:04 的原始崩溃。
    #    防御有两层（任一层生效就不会崩）：
    #    ① ``if hist.empty: continue``（跳过该日）—— 主防御
    #    ② ``last_close = ... if len(hist) else None``（取不到时降级 NaN）—— 兜底
    #    ⇒ 两个变体**都**要关掉，测试对「源码是哪个变体」保持稳健。
    mutant = _mutated_module(
        tmp_path,
        [
            ("        if hist.empty:", "        if False:"),
            ("last_close = hist.iloc[-1] if len(hist) else None",
             "last_close = hist.iloc[-1]"),
        ],
        "panel_guard_off",
        optional=True,
    )
    fx = _fixture()
    with pytest.raises(IndexError):
        _build(fx, [EARLY_DAY], layer=mutant)
