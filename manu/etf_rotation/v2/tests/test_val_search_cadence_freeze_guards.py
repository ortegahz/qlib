"""🔴 V2 **cadence 冻结链守卫**的钉死测试 —— 2026-11-17 夜班审计补。

**背景（审计挖出的 3 个真缺口）**
  ① **冻结静默回落**：``stage_freeze`` 旧口径是
     ``best.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)``
     ⇒ 搜索产物是 cadence 之前的旧产物时**静默写 20**
     ⇒ 而 ``stage_blind`` 的守卫只看「键在不在」⇒ **用错 cadence 烧掉一次性单盲 token**。
  ② **冻结静默混冻**：``val_ma_sweep.json`` 存在时，``stage_freeze`` 把它的
     ``entry_ma_window`` / ``exit_ma_window`` 并进冻结产物，**不校验**它是不是本轮
     （臂 / Top-K / 缓冲带 / cadence）同源 ⇒ 「cadence=20 上下文里扫出来的 MA 窗口」
     与「cadence=60 下选出的其余参数」被拼在一起 ⇒ 冻结参数内部不自洽。
  ③ **样本量门槛看不见**：``config_v2.CADENCE_MIN_REBALANCES = 12`` 只产出一句文本，
     选参闸门输出里**看不到** ⇒ 门槛等于纯标签。

【本文件钉死】
  ① 搜索产物缺 cadence ⇒ 冻结**必须硬报错**（不许回落常量）；
  ② 【变异反证】把硬报错改回落常量 ⇒ ①必须转红（证明这道闸门有牙）；
  ③ MA 二次扫产物**陈旧**（``fixed_from_round1`` 无 cadence 键）或**不同源**
     （cadence / 臂 / Top-K / 缓冲带不一致）⇒ 冻结**必须硬报错**；
  ④ 选参闸门输出**必须暴露**样本量门槛（``chosen_n_rebalances`` /
     ``chosen_sample_sufficient`` / ``n_trials_low_sample`` /
     ``min_rebalances_for_selection``）。

⛔ **只用自造夹具** ⇒ 不联网、不跑重量级回测、不读真实行情。
⛔ **绝不落盘改真源码**（变异 = 源码 → 内存改 → tmp_path → import 副本）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import run_v2 as RV  # noqa: E402

RUNV2_SOURCE = _V2_DIR / "run_v2.py"


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码读出来 → 内存里改 → 写到 tmp_path → import 副本（真源码不动）。"""
    text = RUNV2_SOURCE.read_text(encoding="utf-8")
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


def _fake_search_payload(cadence: int | None = 20, arm: str = "A_half_life_20",
                          top_k: int = 5, hold_buffer: int = 2) -> dict:
    """🔴 自造「val 段搜索产物」载荷（``cadence=None`` ⇒ **不含** cadence 键）。"""
    best: dict = {"smoothing_arm": arm, "top_k": top_k, "hold_buffer": hold_buffer}
    trials = []
    if cadence is not None:
        best["rebalance_every_n_days"] = int(cadence)
    trials.append({
        "params": dict(best), "score": 0.4,
        "objective": {"annualized_turnover": 3.0, "n_rebalances": 8},
        "status": "ok",
    })
    fake = {
        "best_params": best,
        "best_score": 0.4,
        "n_trials_run": 1,
        "n_trials_total": 1,
        "truncated": False,
        "val_window": list(C.VALID),
        "trials": trials,
    }
    grid = RV.build_search_grid(list(C.TOPK_GRID), list(C.HOLD_BUFFER_GRID))
    return RV.build_arm_search_payload(fake, grid, "ablate_roc5")


def _write_search(tmp_path: Path, payload: dict) -> None:
    """把搜索产物写进 ``tmp_path``（供 ``stage_freeze`` 读）。"""
    (tmp_path / "arm_search_val.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- ① 缺 cadence ⇒ 硬报错
def test_搜索产物缺cadence_冻结必须硬报错(tmp_path: Path) -> None:
    """🔴 旧口径（cadence 之前的搜索产物）⇒ **必须抛错**，不许静默回落常量 20。"""
    _write_search(tmp_path, _fake_search_payload(cadence=None))
    with pytest.raises(RuntimeError) as exc:
        RV.stage_freeze(argparse.Namespace(), tmp_path)
    assert "rebalance_every_n_days" in str(exc.value), (
        f"🔴 报错必须点名 cadence 字段：{exc.value!r}")
    assert not (tmp_path / "frozen_params.json").is_file(), (
        "🔴 报错时**不许**留下冻结产物（否则下游会拿旧产物继续跑）")


def test_变异反证_冻结回落常量_硬报错必须转红(tmp_path: Path) -> None:
    """🔴 把「缺 cadence 硬报错」改回落常量 ⇒ ①必须转红（证明这道闸门有牙）。"""
    mutant = _mutated_module(
        tmp_path,
        [('    if "rebalance_every_n_days" not in best:\n',
          '    if False:\n'),
         ('        "rebalance_every_n_days": int(best["rebalance_every_n_days"]),\n',
          '        "rebalance_every_n_days": int(best.get("rebalance_every_n_days",\n'
          '                                                C.REBALANCE_EVERY_N_DAYS)),\n')],
        "runv2_freeze_fallback_const",
    )
    _write_search(tmp_path, _fake_search_payload(cadence=None))
    # 真源码：必须报错
    with pytest.raises(RuntimeError):
        RV.stage_freeze(argparse.Namespace(), tmp_path)
    # 变异体：**不许**再报错（否则硬报错没被测到）
    mutant.stage_freeze(argparse.Namespace(), tmp_path)
    frozen = json.loads((tmp_path / "frozen_params.json").read_text(encoding="utf-8"))
    assert int(frozen["params"]["rebalance_every_n_days"]) == int(C.REBALANCE_EVERY_N_DAYS), (
        "🔴 变异体应当回落到常量 20（否则这条变异反证没意义）")


# ---------------------------------------------------------------- ② MA 二次扫不同源 ⇒ 硬报错
def _write_ma_sweep(tmp_path: Path, fixed_from_round1: dict) -> None:
    """写一份「MA 二次扫」产物（``fixed_from_round1`` 决定是否同源）。"""
    (tmp_path / "val_ma_sweep.json").write_text(json.dumps({
        "val_window": list(C.VALID),
        "grid": {"entry_ma_window": [40, 60], "exit_ma_window": [40, 60]},
        "best_params": {"entry_ma_window": 150, "exit_ma_window": 150},
        "fixed_from_round1": fixed_from_round1,
        "n_trials": 4,
    }, ensure_ascii=False), encoding="utf-8")


def test_MA二次扫产物陈旧_冻结必须硬报错(tmp_path: Path) -> None:
    """🔴 旧口径 MA 产物（``fixed_from_round1`` **没有** cadence 键）⇒ 硬报错。"""
    _write_search(tmp_path, _fake_search_payload(cadence=20))
    _write_ma_sweep(tmp_path, {"smoothing_arm": "A_half_life_20", "top_k": 5,
                               "hold_buffer": 2})
    with pytest.raises(RuntimeError) as exc:
        RV.stage_freeze(argparse.Namespace(), tmp_path)
    assert "val_ma_sweep.json" in str(exc.value), (
        f"🔴 报错必须点名 MA 二次扫产物：{exc.value!r}")


def test_MA二次扫cadence不同源_冻结必须硬报错(tmp_path: Path) -> None:
    """🔴 MA 二次扫固定的 cadence ≠ 本轮胜出 cadence ⇒ 硬报错（不许混冻）。"""
    _write_search(tmp_path, _fake_search_payload(cadence=20))
    _write_ma_sweep(tmp_path, {"smoothing_arm": "A_half_life_20", "top_k": 5,
                               "hold_buffer": 2, "rebalance_every_n_days": 40})
    with pytest.raises(RuntimeError) as exc:
        RV.stage_freeze(argparse.Namespace(), tmp_path)
    assert "cadence" in str(exc.value), (
        f"🔴 报错必须点名 cadence 不一致：{exc.value!r}")


def test_MA二次扫同源_冻结必须成功(tmp_path: Path) -> None:
    """🔴 同源（臂 / Top-K / 缓冲带 / cadence 全一致）⇒ 冻结**必须成功**（不许误杀）。"""
    _write_search(tmp_path, _fake_search_payload(cadence=20))
    _write_ma_sweep(tmp_path, {"smoothing_arm": "A_half_life_20", "top_k": 5,
                               "hold_buffer": 2, "rebalance_every_n_days": 20})
    RV.stage_freeze(argparse.Namespace(), tmp_path)
    frozen = json.loads((tmp_path / "frozen_params.json").read_text(encoding="utf-8"))
    assert int(frozen["params"]["rebalance_every_n_days"]) == 20
    assert int(frozen["params"]["entry_ma_window"]) == 150
    assert int(frozen["params"]["exit_ma_window"]) == 150


# ---------------------------------------------------------------- ③ 闸门必须暴露样本量门槛
def test_选参闸门必须暴露样本量门槛() -> None:
    """🔴 ``CADENCE_MIN_REBALANCES`` 必须**让选参逻辑看得见**（不许只是标签）。"""
    trials = [
        {"params": {"rebalance_every_n_days": 90}, "score": 0.50,
         "objective": {"annualized_turnover": 3.0, "n_rebalances": 6}, "status": "ok"},
        {"params": {"rebalance_every_n_days": 20}, "score": 0.20,
         "objective": {"annualized_turnover": 12.3, "n_rebalances": 23}, "status": "ok"},
    ]
    gated = RV.apply_turnover_gate({"trials": trials}, float(C.TURNOVER_TARGET))
    gate = gated["gate"]
    assert gate["min_rebalances_for_selection"] == int(C.CADENCE_MIN_REBALANCES)
    assert gate["chosen_n_rebalances"] == 6, (
        f"🔴 闸门必须报出选中 trial 的调仓次数：{gate.get('chosen_n_rebalances')!r}")
    assert gate["chosen_sample_sufficient"] is False, (
        "🔴 6 次调仓 < 门槛 12 ⇒ 样本必须被标成**不足**")
    assert gate["n_trials_low_sample"] == 1, (
        f"🔴 样本不足的 trial 数必须报出来：{gate.get('n_trials_low_sample')!r}")
    assert "样本不足" in str(gate.get("sample_note")), (
        f"🔴 闸门必须带样本不足的说明：{gate.get('sample_note')!r}")
    # 🔴 选参**行为不许被悄悄改成过滤器**（那会让用户的「≤4×」红线不可达）
    assert int(gated["best_params"]["rebalance_every_n_days"]) == 90, (
        "🔴 样本量只做披露，不许把 cadence=90 从候选池里剔掉"
        "（剔掉 ⇒ ≤4× 不可达 ⇒ 那是改口径）")
