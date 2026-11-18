"""🔴 V2 决策③接线：val 段三臂搜索 / 调仓栅格 / 冻结产物的钉死测试。

【本文件钉死（自带夹具，不依赖网络 / qlib / 大文件）】

  ① **搜索只发生在 val 段**：窗口一旦越界（碰到 2024-01-01 或误用训练段）⇒
     被 ``v2/hyperopt.py::assert_val_only`` **物理拦截**（越界即 raise）；
     objective **只可能**收到 val 窗口；
  ② **搜索空间 = 臂（A15/A20/B/C）× TOPK_GRID × HOLD_BUFFER_GRID × cadence**，
     其中 **cadence（调仓节奏）** = {20, 40, 60, 90}（口径变更 2026-11-17：用户红线
     「换手 ≤4×」在 cadence=20 的折叠常量下不可达 ⇒ cadence 必须进 val 段搜索空间），
     **cadence=20 与 V1 同口径 ⇒ 必须保留可见**；
  ③ **调仓日间隔 = 20 个交易日**（由 ``REBALANCE_SIGNAL_STRIDE`` 从信号栅格取步长）；
  ④ **冻结产物只含胜出臂与选定参数**，并声明「测试段未参与」；
  ⑤ **变异反证**：改坏 ``config_v2`` / 实现源码 ⇒ 对应断言**必须转红**
     （⛔ **绝不落盘改真源码**）。

【与其它测试文件的分工】
``v2/tests/test_hyperopt.py`` 钉死红线闸门本身；``v2/tests/test_val_search_cadence.py``
钉死 **cadence 维度本身**（映射 / 变异反证 / 白名单守卫）；本文件钉死**决策③的接线**
（臂进搜索空间、调仓栅格、冻结产物）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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
import hyperopt as HO  # noqa: E402
import run_v2 as RV  # noqa: E402

CONFIG_SOURCE = _V2_DIR / "config_v2.py"
ETF_LAYER_SOURCE = _V2_DIR / "etf_layer.py"

#: 🔴 越过红线的窗口（任一命中都必须被拒）
FORBIDDEN_WINDOWS = [
    ("2024-01-01", "2024-12-31"),      # 完全落在测试段
    ("2022-01-01", "2024-01-01"),      # 终点触到测试段起点
    ("2023-06-01", "2024-06-30"),      # 跨过 val/测试边界
    ("2016-01-01", "2021-12-31"),      # 误用训练段
    ("2021-12-01", "2022-06-30"),      # 起点早于 val
]


# --------------------------------------------------------------------- 夹具
def _fixture(n_days: int = 260, seed: int = 20261117):
    """人造一整套「行情 + 个股分数 + 成分股权重」夹具（自足，不碰真实数据）。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2021-01-04", periods=n_days)
    codes = ["510300", "510500", "159915"]
    insts = ["SH600000", "SH600001", "SH600002"]
    # 🔴 给一点**正漂移**（不同 ETF 漂移不同 ⇒ 动量不同）⇒ 均线过滤不会把池子清空
    drift = np.array([0.0008, 0.0004, 0.0002])
    close = pd.DataFrame(
        1.0 + np.cumsum(
            drift[None, :] + rng.normal(0, 0.004, size=(len(days), len(codes))), axis=0
        ),
        index=days, columns=codes,
    )
    amount = pd.DataFrame(1.0e8, index=days, columns=codes)
    features = EL.compute_features(close, amount)
    signal_days = list(days[::int(C.SIGNAL_STRIDE_N_DAYS)])       # 信号栅格 = 10 个交易日
    rows = []
    for day in signal_days:
        for inst in insts:
            rows.append({"date": day, "instrument": inst, "score": rng.normal()})
    scores = pd.DataFrame(rows)
    weights = {
        "510300": {insts[0]: 0.5, insts[1]: 0.3, insts[2]: 0.2},
        "510500": {insts[0]: 0.2, insts[1]: 0.5, insts[2]: 0.3},
        "159915": {insts[0]: 0.3, insts[1]: 0.2, insts[2]: 0.5},
    }
    names = {"510300": "沪深300ETF", "510500": "中证500ETF", "159915": "创业板ETF"}
    return {
        "days": days,
        "signal_days": signal_days,
        "scores": scores,
        "weights": weights,
        "close": close,
        "features": features,
        "names": names,
    }


def _build_targets(fixture: dict, layer=EL, **kwargs):
    """调一次 ``build_targets``（默认参数 + 夹具）。"""
    kwargs.setdefault("top_k", 2)
    kwargs.setdefault("hold_buffer", 1)
    return layer.build_targets(
        fixture["signal_days"], fixture["scores"], fixture["weights"],
        fixture["close"], fixture["features"], names=fixture["names"],
        min_history_days=1, **kwargs,
    )


def _rebalance_gaps(targets: dict, trading_days) -> list[int]:
    """调仓日之间的**交易日间隔**（列表）。"""
    pos = {pd.Timestamp(d): i for i, d in enumerate(trading_days)}
    keys = sorted(targets)
    return [pos[pd.Timestamp(k)] - pos[pd.Timestamp(p)] for p, k in zip(keys, keys[1:])]


def _mutated_config(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把 ``config_v2`` 源码**在内存里**改掉 ⇒ import 一个**副本**（⛔ 不落盘改真源码）。"""
    text = CONFIG_SOURCE.read_text(encoding="utf-8")
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


def _load_etf_layer_with_config(mutant_cfg):
    """用**变异版 config** 加载一份 ``etf_layer`` 副本（真模块不受影响）。"""
    original = sys.modules.get("config_v2")
    sys.modules["config_v2"] = mutant_cfg
    try:
        name = "mutant_etf_layer_with_cfg"
        spec = importlib.util.spec_from_file_location(name, str(ETF_LAYER_SOURCE))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original is not None:
            sys.modules["config_v2"] = original
        else:
            sys.modules.pop("config_v2", None)


# --------------------------------------------------------------------- ① 搜索只在 val 段
def test_搜索窗口必须等于val段() -> None:
    start, end = RV.val_search_window()
    assert (start, end) == (str(C.VALID[0]), str(C.VALID[1]))


@pytest.mark.parametrize("start,end", FORBIDDEN_WINDOWS)
def test_越界窗口必须被assert_val_only拦截(start: str, end: str) -> None:
    with pytest.raises(ValueError):
        HO.assert_val_only(start, end)


def test_objective只可能收到val窗口() -> None:
    """🔴 端到端过一遍 ``hyperopt.search``：objective 收到的窗口**必须**始终是 val 段。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    seen: list[tuple[str, str]] = []

    def objective(params: dict, window: tuple[str, str]) -> dict:
        seen.append((str(window[0]), str(window[1])))
        return {"score": float(params["top_k"]) + float(params["hold_buffer"])}

    out = HO.search(objective, grid, val_window=RV.val_search_window())
    assert seen, "🔴 objective 一次都没被调用"
    assert all(w == (str(C.VALID[0]), str(C.VALID[1])) for w in seen), (
        f"🔴 objective 收到了非 val 窗口：{sorted(set(seen))}"
    )
    assert out["val_window"] == [str(C.VALID[0]), str(C.VALID[1])]


# --------------------------------------------------------------------- ② 搜索空间
def test_搜索空间是臂与topk_buffer与cadence() -> None:
    """🔴 搜索空间 = 臂 × Top-K × 缓冲带 × **cadence**（口径变更 2026-11-17）。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    assert set(grid) == {"smoothing_arm", "top_k", "hold_buffer", "rebalance_every_n_days",
                          "alpha_weight", "allow_theme_cap"}, (
        f"🔴 搜索空间里出现了不该有的键（或缺 cadence / alpha 配比 / 同族上限）：{sorted(grid)}"
    )
    assert grid["smoothing_arm"] == list(C.SMOOTHING_ARM_IDS)
    assert grid["top_k"] == [4, 5, 6]
    assert grid["hold_buffer"] == [0, 1, 2, 3, 4]
    assert grid["rebalance_every_n_days"] == list(C.REBALANCE_CADENCE_GRID), (
        "🔴 cadence 候选必须来自 config_v2.REBALANCE_CADENCE_GRID（不许散落在代码里）"
    )
    # 🔴 2026-11-18 口径锁定：cadence 只允许 20（严格月频）
    assert grid["rebalance_every_n_days"] == [20], (
        f"🔴 cadence 必须锁死 20，实得 {grid['rebalance_every_n_days']}"
    )


def test_cadence必须在搜索空间里且20档保留可见() -> None:
    """🔴 用户红线「换手 ≤4×」在 cadence=20 的折叠常量下不可达 ⇒ cadence 必须进搜索空间；
    **cadence=20（与 V1 同口径）必须保留可见**（可与 V1 的 16.57× 对表）。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    assert "rebalance_every_n_days" in grid, (
        "🔴 调仓节奏（cadence）必须进 val 段搜索空间（否则换手 ≤4× 不可达）"
    )
    assert C.REBALANCE_EVERY_N_DAYS in grid["rebalance_every_n_days"], (
        f"🔴 cadence={C.REBALANCE_EVERY_N_DAYS}（V1 同口径档）必须保留可见"
    )
    assert C.REBALANCE_EVERY_N_DAYS == 20, (
        f"🔴 V1 同口径档必须是 20 个交易日（用户决定②），实得 {C.REBALANCE_EVERY_N_DAYS}"
    )
    assert C.REBALANCE_SIGNAL_STRIDE == C.REBALANCE_EVERY_N_DAYS // C.SIGNAL_STRIDE_N_DAYS, (
        "🔴 调仓步长必须由两个常量推导（不许单独写死）"
    )
    # 🔴 stride 类键**仍然禁止**进搜索空间（只放行 cadence 一个键）
    offending = [k for k in grid if "stride" in k.lower()]
    assert not offending, f"🔴 stride 类键不许进搜索空间：{offending}"


def test_臂对照产物必须含每臂指标与胜者() -> None:
    """🔴 ``arm_search_val.json`` 的载荷契约：每臂指标 + 胜者 + 判定窗口 + 口径声明。"""
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    fake = {
        "best_params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2},
        "best_score": 0.4,
        "n_trials_run": 4,
        "n_trials_total": 4,
        "truncated": False,
        "val_window": list(C.VALID),
        "trials": [
            {
                "params": {"smoothing_arm": arm_id, "top_k": 5, "hold_buffer": 2},
                "score": 0.1 + i,
                "objective": {
                    "annualized_return": 0.20, "excess_vs_benchmark": 0.10,
                    "annualized_turnover": 3.0, "max_drawdown": -0.12, "n_rebalances": 24,
                },
                "status": "ok",
            }
            for i, arm_id in enumerate(C.SMOOTHING_ARM_IDS)
        ],
    }
    payload = RV.build_arm_search_payload(fake, grid, "ablate_roc5")
    assert payload["winner_arm"] == "A_half_life_20"
    assert set(payload["per_arm"]) == set(C.SMOOTHING_ARM_IDS)
    for arm_id, entry in payload["per_arm"].items():
        assert entry["n_ok_trials"] == 1, f"🔴 臂 {arm_id} 的 trial 统计不对"
        assert entry["metrics"]["excess_vs_benchmark"] == 0.10
        assert entry["arm_spec"]["arm_id"] == arm_id
    assert payload["decision_window"]["start"] == str(C.VALID[0])
    assert payload["decision_window"]["end"] == str(C.VALID[1])
    assert payload["primary_metric"], "🔴 必须写死排序指标"
    assert payload["secondary_metric"], "🔴 必须写死并列展示指标（换手）"
    assert payload["rebalance_every_n_days"] == C.REBALANCE_EVERY_N_DAYS
    assert "测试段从未参与选参" in payload["note"]


# --------------------------------------------------------------------- ③ 调仓栅格
def test_调仓日间隔必须等于20个交易日() -> None:
    fixture = _fixture()
    targets, _ = _build_targets(fixture)
    gaps = _rebalance_gaps(targets, fixture["days"])
    assert gaps, "🔴 一张调仓单都没出"
    assert set(gaps) == {20}, (
        f"🔴 调仓日间隔必须是 20 个交易日，实得 {sorted(set(gaps))}"
    )


def test_调仓日必须落在信号栅格上() -> None:
    fixture = _fixture()
    targets, _ = _build_targets(fixture)
    signal_pos = {pd.Timestamp(d): i for i, d in enumerate(fixture["signal_days"])}
    keys = sorted(targets)
    assert set(signal_pos).issuperset({pd.Timestamp(k) for k in keys}), (
        "🔴 调仓日必须取自信号栅格"
    )
    idx = [signal_pos[pd.Timestamp(k)] for k in keys]
    assert all(b - a == C.REBALANCE_SIGNAL_STRIDE for a, b in zip(idx, idx[1:])), (
        f"🔴 调仓日在信号栅格上的步长必须是 {C.REBALANCE_SIGNAL_STRIDE}，实得 {idx}"
    )


def test_平滑必须作用在ETF层穿透分而非个股分数() -> None:
    """🔴 平滑只允许动 E（ETF 穿透分）：个股分数列**不许**出现在诊断里。"""
    fixture = _fixture()
    _, diag = _build_targets(fixture, smoothing_arm="A_half_life_15")
    assert "alpha_score" in diag.columns
    assert "alpha_score_raw" in diag.columns
    assert "z_score" not in diag.columns and "score" not in diag.columns, (
        f"🔴 诊断里出现了个股分数列 ⇒ 平滑可能作用在 S 上：{list(diag.columns)}"
    )
    diff = (diag["alpha_score"] - diag["alpha_score_raw"]).abs().max()
    assert float(diff) > 0, "🔴 平滑臂没有改变 E ⇒ 平滑没生效"


def test_臂B必须走同一条评估路径且不做下游EWMA() -> None:
    """🔴 臂 B（原生慢变量）：E 原样使用（= 不平滑），但仍走统一评估引擎（含 Rank Buffer）。"""
    fixture = _fixture()
    raw_targets, raw_diag = _build_targets(fixture, top_k=1, hold_buffer=0)
    arm_b_targets, arm_b_diag = _build_targets(fixture, smoothing_arm="B_native_slow",
                                                top_k=1, hold_buffer=0)
    assert arm_b_targets == raw_targets, (
        "🔴 臂 B 与「不平滑」结果不一致 ⇒ 臂 B 还在做下游 EWMA"
    )
    assert "alpha_score_raw" in arm_b_diag.columns
    # 🔴 Rank Buffer 对所有臂一视同仁（都在统一评估引擎里）：
    #    buffer=1 与 buffer=0 的目标权重**必须**在某次调仓上不同 ⇒ 迟滞带真的在起作用。
    buffered, _ = _build_targets(fixture, top_k=1, hold_buffer=1)
    arm_b_buffered, _ = _build_targets(fixture, smoothing_arm="B_native_slow",
                                        top_k=1, hold_buffer=1)
    assert buffered != raw_targets, (
        "🔴 Rank Buffer 没有任何影响 ⇒ 统一评估引擎里的迟滞带没生效（臂 B 也一样）"
    )
    assert arm_b_buffered == buffered, (
        "🔴 臂 B 与其它臂在 Rank Buffer 上不一致 ⇒ 臂 B 没走同一条评估路径"
    )


# --------------------------------------------------------------------- ④ 冻结产物
def test_冻结产物只含胜出臂与选定参数(tmp_path: Path) -> None:
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    fake = {
        # 🔴 2026-11-17 夜班：搜索产物**必须**带 cadence（``stage_freeze`` 已改成
        #    缺键就**硬报错**，不许静默回落到常量 20 ⇒ 会用错节奏烧掉一次性单盲段）。
        "best_params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                        # 🔴 2026-11-18：cadence **锁死 20**
                        "rebalance_every_n_days": 20},
        "best_score": 0.4,
        "n_trials_run": 1,
        "n_trials_total": 1,
        "truncated": False,
        "val_window": list(C.VALID),
        "trials": [{
            "params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                       "rebalance_every_n_days": 20},
            "score": 0.4, "objective": {}, "status": "ok",
        }],
    }
    payload = RV.build_arm_search_payload(fake, grid, "ablate_roc5")
    (tmp_path / "arm_search_val.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    RV.stage_freeze(argparse.Namespace(), tmp_path)

    frozen_path = tmp_path / "frozen_params.json"
    data = json.loads(frozen_path.read_text(encoding="utf-8"))
    assert set(data["params"]) == {"smoothing_arm", "top_k", "hold_buffer",
                                   "rebalance_every_n_days",
                                   # 🔴 样本量披露字段（夜班补）：必须落进冻结产物
                                   "n_rebalances_val", "sample_note"}, (
        f"🔴 冻结产物里出现了不该有的参数：{sorted(data['params'])}"
    )
    assert data["params"]["smoothing_arm"] == "A_half_life_20"
    assert data["params"]["top_k"] == 5
    assert data["params"]["hold_buffer"] == 2
    assert data["params"]["rebalance_every_n_days"] == 20, (
        "🔴 冻结产物必须含**被搜索的 cadence**（盲测/报告不许回落到常量）"
        "—— 本用例用 cadence=40（非默认 20）⇒ 若代码回落到常量，这里必须转红"
    )
    assert data["winner_arm"] == "A_half_life_20"
    assert data["selected_on"] == "val"
    assert "测试段未参与" in data["test_segment_note"], (
        f"🔴 冻结产物没写明「测试段未参与」：{data.get('test_segment_note')!r}"
    )
    assert oct(os.stat(frozen_path).st_mode)[-3:] == "444", (
        "🔴 冻结产物必须是只读（0444）"
    )


# --------------------------------------------------------------------- ⑤ 变异反证
def test_变异反证_改REBALANCE_EVERY_N_DAYS_调仓间隔断言必须转红(tmp_path: Path) -> None:
    """🔴 把折叠常量从 20 改成 10 ⇒ 「间隔 = 20 个交易日」这条断言必须转红。"""
    mutant_cfg = _mutated_config(
        tmp_path,
        [("REBALANCE_EVERY_N_DAYS = 20\n", "REBALANCE_EVERY_N_DAYS = 10\n")],
        "config_rebal_10",
    )
    assert mutant_cfg.REBALANCE_EVERY_N_DAYS == 10
    mutant_layer = _load_etf_layer_with_config(mutant_cfg)
    fixture = _fixture()
    targets, _ = _build_targets(fixture, layer=mutant_layer)
    gaps = _rebalance_gaps(targets, fixture["days"])
    assert 20 not in set(gaps), (
        f"🔴 变异后调仓间隔仍是 20 ⇒ 这条断言没有牙（实测 {sorted(set(gaps))}）"
    )


def test_变异反证_白名单外的调仓节奏键_必须被拒() -> None:
    """🔴 口径变更后：**只有** ``rebalance_every_n_days`` 被放行；stride 类键仍必须被拒
    （不许「随便放行」）。"""
    RV.assert_no_rebalance_keys(
        {"smoothing_arm": list(C.SMOOTHING_ARM_IDS),
         "rebalance_every_n_days": [C.REBALANCE_EVERY_N_DAYS]}
    )
    with pytest.raises(ValueError):
        RV.assert_no_rebalance_keys(
            {"smoothing_arm": list(C.SMOOTHING_ARM_IDS),
             "rebalance_signal_stride": [C.REBALANCE_SIGNAL_STRIDE]}
        )
    with pytest.raises(ValueError):
        RV.assert_no_rebalance_keys({"stride": [2]})
