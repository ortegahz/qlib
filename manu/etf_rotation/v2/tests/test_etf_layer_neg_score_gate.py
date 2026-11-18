"""🔴 V2 ETF 层 **负分空仓保护**（neg-score gate）的钉死测试。

【本文件钉死（自带夹具，不碰真实行情 / 不联网 / 不跑重量级回测）】

  ① **默认关闭** ⇒ 目标权重之和仍 = 1.0（**既有调用方行为逐字不变**）；
  ② 闸门**开启**且候选池 E 的中位数**为负** ⇒ 目标权重**按比例削减**（剩余 ⇒ 现金）；
  ③ ``min_exposure`` 生效（不许清零 ⇒ 保留底仓）；``scale`` 越小 ⇒ 削减越狠；
  ④ E **为正** ⇒ **不许**削减（闸门不得反向）；
  ⑤ ``neg_score_exposure`` 的**边界**：``NaN`` / ``None`` / ``scale<=0`` ⇒ 失效保护返回 1.0；
  ⑥ **变异反证**：把源码里的敞口换算改坏 ⇒ ② 必须转红
     （⛔ 绝不落盘改真源码：源码 → 内存改 → tmp_path → import 副本）。

【为什么要有这个闸门（2026-11-18 目标文档 Step 3）】
  V2 单盲：个股 RankIC **0.1271** 但组合 **−12.93% / 回撤 −54.93%** ⇒ 病根在 ETF 轮动层；
  当**候选 ETF 的穿透分 E 普遍转负**（成分股整体跑输打分池）时，
  **按比例削减权益到现金** ⇒ 不在系统性熊市里满仓硬扛。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import etf_layer as EL  # noqa: E402

ETF_LAYER_SOURCE = _V2_DIR / "etf_layer.py"

#: 夹具 ETF（**不碰真实行情**）
CODES = ("510300", "510500", "510880", "159915", "159611")
NAMES = {
    "510300": "红利ETF", "510500": "银行ETF", "510880": "医药ETF",
    "159915": "通信ETF", "159611": "电力ETF",
}
INST = {code: f"SH60000{i}" for i, code in enumerate(CODES)}
N_DAYS = 120


def _fixture(alpha: dict[str, float]) -> dict:
    """合成行情 + 个股分数 + 成分股权重（``alpha`` 决定 E 的相对强弱）。"""
    days = pd.bdate_range("2021-01-04", periods=N_DAYS)
    close = pd.DataFrame(
        {code: 10.0 + 0.01 * np.arange(N_DAYS) for code in CODES}, index=days)
    amount = pd.DataFrame(1.0e8, index=days, columns=close.columns)
    dates = list(days[100::2])
    scores = pd.DataFrame(
        [{"date": day, "instrument": INST[code], "score": alpha[code]}
         for day in days for code in CODES])
    weights = {code: {INST[code]: 1.0} for code in CODES}
    return {
        "days": days, "dates": dates, "scores": scores, "weights": weights,
        "close": close, "amount": amount,
        "features": EL.compute_features(close, amount),
        "names": dict(NAMES),
    }


#: 🔴 负向 E 池：一只强正 + 四只负 ⇒ 候选池 E 的**中位数显著为负**
NEGATIVE_ALPHA = {"510300": 10.0, "510500": -1.0, "510880": -1.0,
                  "159915": -1.0, "159611": -1.0}
#: 🔴 正向 E 池：全部为正 ⇒ 中位数为正
POSITIVE_ALPHA = {code: 1.0 + i for i, code in enumerate(CODES)}
#: 🔴 **温和**负 E 池（等距 + 小倾斜 ⇒ 候选池 E 的中位数 ≈ −0.07 ⇒ 不会被直接打到下限）
MILD_NEGATIVE_ALPHA = {
    "510300": -1.05, "510500": -0.55, "510880": -0.15,
    "159915": 0.45, "159611": 0.95,
}


def _build(fixture: dict, layer=None, **kwargs):
    layer = EL if layer is None else layer
    kwargs.setdefault("top_k", 3)
    kwargs.setdefault("hold_buffer", 0)
    kwargs.setdefault("alpha_weight", 1.0)
    kwargs.setdefault("momentum_weight", 0.0)
    kwargs.setdefault("min_history_days", 1)
    kwargs.setdefault("entry_ma_window", 0)
    kwargs.setdefault("exit_ma_window", 0)
    return layer.build_targets(
        fixture["dates"], fixture["scores"], fixture["weights"],
        fixture["close"], fixture["features"], names=fixture["names"],
        rebalance_signal_stride=1, **kwargs,
    )


def _weights_sum(targets: dict) -> float:
    """所有调仓日的目标权重之和（满仓 = 1.0；< 1 ⇒ 剩余是现金）。"""
    sums = [float(sum(w.values())) for w in targets.values()]
    return float(np.mean(sums)) if sums else 0.0


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把 ``etf_layer`` 源码读出来 → 内存里改 → 写到 tmp_path → import 副本。"""
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


# --------------------------------------------------------------------- 主断言
def test_默认关闭时权重仍满仓():
    """① 默认（闸门关闭）⇒ 权重和 = 1.0（生产行为不变）。"""
    fx = _fixture(NEGATIVE_ALPHA)
    targets, _diag = _build(fx)
    assert targets, "🔴 夹具没选出任何标的"
    assert abs(_weights_sum(targets) - 1.0) < 1e-9, (
        f"🔴 默认权重和 { _weights_sum(targets) } ≠ 1.0 ⇒ 闸门默认没关"
    )


def test_负分池开启闸门后按比例削减():
    """② E 中位数为负 ⇒ 权重被削减（剩余 ⇒ 现金）。"""
    fx = _fixture(NEGATIVE_ALPHA)
    targets, diag = _build(fx, neg_score_gate=True, neg_score_gate_scale=0.05,
                           neg_score_gate_min_exposure=0.0)
    assert _weights_sum(targets) < 1.0, "🔴 E 普遍为负却一点没削减 ⇒ 闸门没生效"
    stat = float(diag["neg_gate_stat"].iloc[0])
    assert stat < 0, f"🔴 夹具的中位数 E 应为负（实得 {stat}）"
    assert "neg_exposure" in diag.columns, "🔴 诊断表缺 neg_exposure 列"


def test_min_exposure_保底与_scale_单调():
    """③ ``min_exposure`` 保底；``scale`` 越小 ⇒ 削减越狠。"""
    fx = _fixture(NEGATIVE_ALPHA)
    t_floor, _ = _build(fx, neg_score_gate=True, neg_score_gate_scale=0.05,
                         neg_score_gate_min_exposure=0.3)
    assert abs(_weights_sum(t_floor) - 0.3) < 1e-9, (
            f"🔴 min_exposure=0.3 时权重和应为 0.3（实得 {_weights_sum(t_floor)}）"
    )
    # 温和负分池：scale 越小 ⇒ 削减越狠（不被下限截断 ⇒ 可比）
    fx_mild = _fixture(MILD_NEGATIVE_ALPHA)
    t_lo, _ = _build(fx_mild, neg_score_gate=True, neg_score_gate_scale=0.20,
                     neg_score_gate_min_exposure=0.0)
    t_hi, _ = _build(fx_mild, neg_score_gate=True, neg_score_gate_scale=0.50,
                     neg_score_gate_min_exposure=0.0)
    s_lo = _weights_sum(t_lo)
    s_hi = _weights_sum(t_hi)
    assert s_lo < s_hi, f"🔴 scale 越小削减越狠 ⇒ 0.20 应比 0.50 更小（{s_lo} vs {s_hi}）"


def test_正分池不削减():
    """④ E 为正 ⇒ 闸门**不许**反向削减。"""
    fx = _fixture(POSITIVE_ALPHA)
    targets, _diag = _build(fx, neg_score_gate=True, neg_score_gate_scale=0.05,
                            neg_score_gate_min_exposure=0.0)
    assert abs(_weights_sum(targets) - 1.0) < 1e-9, (
        f"🔴 E 为正却削减了（权重和 { _weights_sum(targets) }）"
    )


def test_exposure_边界与失效保护():
    """⑤ ``neg_score_exposure`` 的边界。"""
    assert EL.neg_score_exposure(None) == 1.0, "🔴 None 必须失效保护返回 1.0"
    assert EL.neg_score_exposure(float("nan")) == 1.0, "🔴 NaN 必须失效保护返回 1.0"
    assert EL.neg_score_exposure(-0.5, scale=0.0) == 1.0, "🔴 scale<=0 必须失效"
    assert EL.neg_score_exposure(0.0, scale=0.05) == 1.0, "🔴 stat=0 ⇒ 敞口 1.0"
    assert EL.neg_score_exposure(-0.05, scale=0.05, min_exposure=0.0) == 0.0, (
        "🔴 stat=-scale ⇒ 敞口应归零"
    )
    assert EL.neg_score_exposure(-1.0, scale=0.05, min_exposure=0.4) == 0.4, (
        "🔴 敞口不许跌破 min_exposure"
    )
    assert EL.neg_score_exposure(0.5, scale=0.05) == 1.0, "🔴 stat>0 ⇒ 敞口封顶 1.0"


def test_变异反证_改坏敞口换算必须转红(tmp_path):
    """⑥ 把敞口换算改坏（恒返回 1.0）⇒ ②③④ 必须转红。"""
    mutant = _mutated_module(
        tmp_path, [("1.0 + s / sc, lo, 1.0)", "1.0, lo, 1.0)")], "gate_broken")
    fx = _fixture(NEGATIVE_ALPHA)
    targets, _diag = _build(fx, layer=mutant, neg_score_gate=True,
                            neg_score_gate_scale=0.05, neg_score_gate_min_exposure=0.0)
    assert _weights_sum(targets) >= 1.0, (
        "🔴 变异体居然还削减了 ⇒ 变异没生效，测试是假的"
    )
