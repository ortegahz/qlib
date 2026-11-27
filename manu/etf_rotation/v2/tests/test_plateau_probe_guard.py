"""🔴 V2 **参数高原探针（参数邻域扰动）**的守卫测试。

【本文件钉死（自带夹具；不碰真实行情 / 不联网 / 不跑重量级回测）】

  ① ``etf_layer`` 的**动量窗口覆盖**默认 = ``None``
     ⇒ **默认路径逐位不变**（与「加覆盖之前」的实现逐位一致 ⇒ 金标 md5 锚点）；
     且**显式**把模块常量当覆盖传进去 ⇒ 结果与默认路径**逐位一致**。
  ② ``v2_plateau_probe.py`` 源码里**不出现**封存测试段的日期字面量（静态扫）
     ⇒ 且 OOS 窗口 **恒等于** ``config_v2.WALK_FORWARD_OOS_WINDOW``（2019–2023）。
  ③ 高原 / 尖刺的**判定纯函数**：构造的「平高原」样例 ⇒ ``plateau``；
     构造的「孤尖刺」样例 ⇒ ``spike``；构造的「半塌」样例 ⇒ ``mixed``。

【为什么要有 ①】``etf_layer`` 是**封存链路**的一部分（V2 冠军参数就在它上面跑出来）
⇒ 任何「加个可选覆盖」的动作都**必须**证明默认路径一个比特都没变。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import etf_layer as EL  # noqa: E402

#: 探针源码（``manu/tools/v2_plateau_probe.py``）
_PROBE_PATH = _V2_DIR.parents[1] / "tools" / "v2_plateau_probe.py"

#: 🔴 **金标 md5** = 「动量窗口覆盖」加进来**之前**的默认路径产出
#:    （对 ``build_targets`` 的目标权重字典取 ``repr`` 后 md5）。
#:    ⇒ 默认路径一旦被改动 ⇒ 这个锚点立刻转红。
GOLDEN_TARGETS_MD5 = "68c958695f58b9ebf6564f93cd24d815"

#: 夹具 ETF（**不碰真实行情**）
CODES = ("510300", "510500", "510880", "159915", "159611")
NAMES = {"510300": "红利ETF", "510500": "银行ETF", "510880": "医药ETF",
         "159915": "通信ETF", "159611": "电力ETF"}
INST = {code: f"SH60000{i}" for i, code in enumerate(CODES)}
ALPHA = {"510300": 1.0, "510500": 0.5, "510880": 0.0, "159915": -0.5, "159611": -1.0}
N_DAYS = 300


def _seg(n: int, v0: float, v1: float) -> np.ndarray:
    return np.linspace(v0, v1, n)


def _fixture() -> dict:
    """合成行情 + 个股分数 + 成分股权重。

    🔴 夹具设计要点：**动量接力**（159611 → 510500 → 159915 各主导一段）
       ⇒ 选股结果**随动量窗口变化** ⇒ 「覆盖机制生效」可被行为断言抓住。
       且 ``hold_buffer=0``（否则 band = top_k + buffer 覆盖全部候选 ⇒ 选股永远不变）。
    """
    days = pd.bdate_range("2021-01-04", periods=N_DAYS)
    paths = {
        "510300": np.concatenate([_seg(100, 10.0, 11.0), _seg(100, 11.0, 9.0), _seg(100, 9.0, 10.0)]),
        "510500": np.concatenate([_seg(100, 10.0, 10.5), _seg(100, 10.5, 15.0), _seg(100, 15.0, 15.5)]),
        "510880": np.concatenate([_seg(150, 8.0, 9.0), _seg(150, 9.0, 10.0)]),
        "159915": np.concatenate([_seg(200, 7.0, 7.5), _seg(100, 7.5, 13.0)]),
        "159611": np.concatenate([_seg(100, 6.0, 13.0), _seg(200, 13.0, 13.5)]),
    }
    close = pd.DataFrame(paths, index=days)
    amount = pd.DataFrame(1.0e8, index=days, columns=close.columns)
    dates = list(days[119::2])
    scores = pd.DataFrame(
        [{"date": day, "instrument": INST[code], "score": ALPHA[code]}
         for day in days for code in CODES]
    )
    weights = {code: {INST[code]: 1.0} for code in CODES}
    return {"days": days, "dates": dates, "scores": scores, "weights": weights,
            "close": close, "amount": amount, "names": dict(NAMES)}


def _build(fx: dict, layer=None, momentum_weights=None, **kwargs):
    layer = EL if layer is None else layer
    feats = layer.compute_features(fx["close"], fx["amount"],
                                   momentum_weights=momentum_weights)
    kwargs.setdefault("top_k", 3)
    kwargs.setdefault("hold_buffer", 0)
    kwargs.setdefault("alpha_weight", 0.5)
    kwargs.setdefault("momentum_weight", 0.5)
    kwargs.setdefault("entry_ma_window", 0)
    kwargs.setdefault("exit_ma_window", 0)
    kwargs.setdefault("min_history_days", 1)
    kwargs.setdefault("min_coverage", 0.0)
    return layer.build_targets(
        fx["dates"], fx["scores"], fx["weights"], fx["close"], feats,
        names=fx["names"], rebalance_signal_stride=1,
        momentum_weights=momentum_weights, **kwargs,
    )


def _load_probe():
    """import 探针模块（**只取纯函数**，不跑 main）。"""
    spec = importlib.util.spec_from_file_location("v2_plateau_probe", _PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["v2_plateau_probe"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------- ① 默认路径不变
def test_默认路径与加覆盖之前逐位一致_金标md5锚点():
    """① 默认路径（不传覆盖）的产出必须与**改动前**逐位一致（金标 md5）。"""
    fx = _fixture()
    targets, _diag = _build(fx)
    got = hashlib.md5(repr(targets).encode("utf-8")).hexdigest()
    assert got == GOLDEN_TARGETS_MD5, (
        f"🔴 默认路径变了：md5 {got} ≠ 金标 {GOLDEN_TARGETS_MD5}\n"
        f"   ⇒ 「动量窗口覆盖」的默认值必须是 None（不许改默认口径）"
    )


def test_显式传模块常量与不传覆盖逐位一致():
    """① 显式把 :data:`etf_layer.MOMENTUM_WEIGHTS` 当覆盖传进去 ⇒ 与默认路径逐位一致。"""
    fx = _fixture()
    default_targets, default_diag = _build(fx)
    explicit_targets, explicit_diag = _build(fx, momentum_weights=dict(EL.MOMENTUM_WEIGHTS))
    assert default_targets == explicit_targets, "🔴 覆盖机制改变了目标权重"
    pd.testing.assert_frame_equal(default_diag, explicit_diag, check_exact=True)


def test_特征默认与显式覆盖逐位一致():
    """① ``compute_features``：默认（None）与显式模块常量 ⇒ 逐位一致。"""
    fx = _fixture()
    default = EL.compute_features(fx["close"], fx["amount"])
    explicit = EL.compute_features(fx["close"], fx["amount"],
                                   momentum_weights=dict(EL.MOMENTUM_WEIGHTS))
    assert sorted(default.keys()) == sorted(explicit.keys())
    for key in default:
        pd.testing.assert_frame_equal(default[key], explicit[key], check_exact=True)


def test_覆盖机制真的生效_换窗口会改变选股():
    """① 反证：覆盖**不是**空操作（换动量窗口 ⇒ 选股结果必须变）。"""
    fx = _fixture()
    base, _ = _build(fx)
    changed = 0
    for weights in ({20: 0.2, 60: 0.5, 100: 0.3},
                    {20: 0.2, 60: 0.5, 140: 0.3},
                    {20: 0.2, 50: 0.5, 120: 0.3},
                    {20: 0.2, 70: 0.5, 120: 0.3}):
        other, _ = _build(fx, momentum_weights=weights)
        if other != base:
            changed += 1
    assert changed >= 1, (
        "🔴 换了动量窗口，选股结果却不变 ⇒ 覆盖没接进选股（空操作）"
    )


# --------------------------------------------------------------------- ② 封存段不碰
def test_探针源码不含封存测试段的日期字面量():
    """② 静态扫：探针源码里不许出现封存测试段的任何日期字面量。"""
    source = _PROBE_PATH.read_text(encoding="utf-8")
    # (a) 完整 ISO 日期：年份必须早于封存段起点
    for match in re.finditer(r"(?<![0-9A-Za-z_])(\d{4})-(\d{2})-(\d{2})(?![0-9A-Za-z_])", source):
        year = int(match.group(1))
        assert year < int(C.TEST[0][:4]), (
            f"🔴 探针源码里出现封存段的日期字面量：{match.group(0)}"
        )
    # (b) 独立的四位年份（文件名前后缀不算日期字面量）
    for match in re.finditer(r"(?<![0-9A-Za-z_])((?:19|20)\d{2})(?![0-9A-Za-z_])", source):
        year = int(match.group(1))
        assert year < int(C.TEST[0][:4]), (
            f"🔴 探针源码里出现封存段的年份字面量：{match.group(0)}"
        )
    # (c) 探针**不许**引用封存段常量本身
    assert not re.search(r"\bC\.TEST\b", source), "🔴 探针不许引用 config_v2.TEST（封存段）"
    assert not re.search(r"\bconfig_v2\.TEST\b", source)


def test_探针的OOS窗口恒等于配置的滚动前推窗口():
    """② 探针的 OOS 窗口必须**从配置取**（不许写死）⇒ 等于 2019–2023。"""
    source = _PROBE_PATH.read_text(encoding="utf-8")
    assert "WALK_FORWARD_OOS_WINDOW" in source, (
        "🔴 探针源码里没有 WALK_FORWARD_OOS_WINDOW ⇒ OOS 窗口可能写死了"
    )
    assert tuple(C.WALK_FORWARD_OOS_WINDOW) == ("2019-01-01", "2023-12-31"), (
        f"🔴 配置里的 OOS 窗口变了：{list(C.WALK_FORWARD_OOS_WINDOW)}"
    )
    assert tuple(C.WALK_FORWARD_FOLDS[0][2:]) == ("2019-01-01", "2019-12-31")
    assert tuple(C.WALK_FORWARD_FOLDS[-1][2:]) == ("2023-01-01", "2023-12-31")
    # 探针必须逐折调 ``splits.assert_fold_only``（折级红线）
    assert "assert_fold_only" in source, "🔴 探针必须逐折调 splits.assert_fold_only"


# --------------------------------------------------------------------- ③ 判定纯函数
def test_判定函数_平高原判为plateau():
    """③ 构造的「平高原」：两个最近邻同号且保持度 ≥ 0.7 ⇒ ``plateau``。"""
    probe = _load_probe()
    items = [
        {"variant_id": "champ", "excess": 0.070},
        {"variant_id": "nb1", "excess": 0.066},
        {"variant_id": "nb2", "excess": 0.050},
    ]
    out = probe.classify_axis(items, 0)
    assert out["verdict"] == "plateau", f"🔴 应判 plateau，实得 {out}"
    assert out["n_good_neighbors"] == 2
    assert out["sign_flip"] is False and out["low_retention"] is False


def test_判定函数_孤尖刺判为spike():
    """③ 构造的「孤尖刺」：最近邻保持度塌到 0.3 以下 ⇒ ``spike``。"""
    probe = _load_probe()
    items = [
        {"variant_id": "champ", "excess": 0.070},
        {"variant_id": "nb1", "excess": 0.010},
        {"variant_id": "nb2", "excess": 0.005},
    ]
    out = probe.classify_axis(items, 0)
    assert out["verdict"] == "spike", f"🔴 应判 spike，实得 {out}"
    assert out["low_retention"] is True
    assert out["strict_argmax"] is True
    assert out["drop_to_neighbor"] > 0.5


def test_判定函数_半塌邻域判为mixed():
    """③ 构造的「半塌」：只有一个邻居保持度够 ⇒ ``mixed``（既不是高原也不是尖刺）。"""
    probe = _load_probe()
    items = [
        {"variant_id": "champ", "excess": 0.070},
        {"variant_id": "nb1", "excess": 0.045},
        {"variant_id": "nb2", "excess": 0.050},
    ]
    out = probe.classify_axis(items, 0)
    assert out["verdict"] == "mixed", f"🔴 应判 mixed，实得 {out}"
    assert out["n_good_neighbors"] == 1


def test_判定函数_邻居超额变号判为spike():
    """③ 最近邻超额**变号** ⇒ ``spike``。"""
    probe = _load_probe()
    items = [
        {"variant_id": "champ", "excess": 0.070},
        {"variant_id": "nb1", "excess": -0.010},
        {"variant_id": "nb2", "excess": 0.050},
    ]
    out = probe.classify_axis(items, 0)
    assert out["verdict"] == "spike", f"🔴 应判 spike，实得 {out}"
    assert out["sign_flip"] is True


def test_保持度分母近零时报n_a():
    """③ 参考超额近零 ⇒ 保持度必须报 n/a（不许算假数）。"""
    probe = _load_probe()
    assert probe.retention(0.01, 0.0) is None
    assert probe.retention(0.01, 1e-9) is None
    assert probe.retention(None, 0.07) is None
    assert probe.retention(0.035, 0.07) == pytest.approx(0.5)


# --------------------------------------------------------------------- 配对统计
def test_配对统计_均值与自助CI():
    """配对统计自检：差序列均值 = 真实均值；自助 CI 覆盖真值（构造 iid 序列）。"""
    probe = _load_probe()
    rng = np.random.default_rng(7)
    base = rng.normal(0.0, 0.01, size=800)
    delta = rng.normal(0.002, 0.001, size=800)
    shift = base + delta
    idx = pd.bdate_range("2019-01-01", periods=800)
    ret_ref = pd.Series(base, index=idx)
    ret_var = pd.Series(shift, index=idx)
    st = probe.paired_stats(ret_var, ret_ref)
    assert st["n_paired_days"] == 800
    assert st["mean_diff"] == pytest.approx(float(delta.mean()), rel=1e-9)
    assert st["boot_ci_lo"] < st["mean_diff"] < st["boot_ci_hi"], (
        f"🔴 自助 CI 没覆盖样本均值：[{st['boot_ci_lo']}, {st['boot_ci_hi']}]"
    )
    assert st["nw_t_stat"] > 0.0


def test_邻域选取按参数距离取最近():
    """最近邻 = 按轴上网格距离最近的 k 个（冠军在端点时取同侧往下数）。"""
    probe = _load_probe()
    assert probe.nearest_neighbor_indices(6, 2, 2) == [1, 3]
    assert probe.nearest_neighbor_indices(4, 0, 2) == [1, 2]
    assert probe.nearest_neighbor_indices(3, 1, 2) == [0, 2]
