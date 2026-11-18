"""🔴 2026-11-18 用户目标文档「为什么 V2 在盲测里大亏」的两条**根因**防线。

【本文件钉死什么】

  ① **cadence 锁死 = 20 个交易日（严格月频）**：
     2026-11-17 夜班那次冻结选出了 **cadence=90**（每 4 个半月才动一次）
     ⇒ 一个 20 日预测极准的模型（个股 RankIC **0.1271** / ICIR 1.105 / t=9.04）
     被锁死 4 个半月 ⇒ 单盲组合 **−12.93% / 回撤 −54.93%**。
     ⇒ 冻结前必须有**硬断言**：cadence ∉ 锁定档 ⇒ 直接 raise（fail-closed）。

  ② **换手惩罚项必须有上限**：
     旧目标函数 ``score = 超额 − 0.05 × max(0, 换手 − 4.0)`` 在 cadence=20 下
     恒有惩罚 ≈ **0.416**，而超额差异只有 0.05~0.20
     ⇒ **惩罚项把超额完全盖住** ⇒ 目标函数退化成「最小化换手」
     ⇒ 网格搜索为了凑「≤4×」红线把调仓周期拉到 90 天。
     ⇒ 惩罚项必须**有上限**（`config_v2.TURNOVER_PENALTY_CAP`），
     只当「超额接近时的 tie-breaker」，**不许**再买断 alpha。

【变异反证】
  ③ 把上限去掉（``TURNOVER_PENALTY_CAP`` → 无穷大）⇒ 断言②必须转红。
  ④ 把锁定档放开（``REBALANCE_CADENCE_GRID`` → 含 90）⇒ 断言①必须转红。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import run_v2 as RV  # noqa: E402


# --------------------------------------------------------------------- ① cadence 锁定
def test_cadence网格锁死为20() -> None:
    """🔴 调仓节奏必须锁死 = 20 个交易日（严格月频）。"""
    assert tuple(C.REBALANCE_CADENCE_GRID) == (20,), (
        f"🔴 cadence 网格必须是 (20,)：{C.REBALANCE_CADENCE_GRID!r}"
    )
    assert tuple(C.REBALANCE_CADENCE_GRID_LEGACY) == (20, 40, 60, 90), (
        "🔴 旧网格只许留作审计对表，不许参与搜索"
    )


@pytest.mark.parametrize("bad", [40, 60, 90, 120, 150])
def test_冻结前硬断言拒绝非20的cadence(bad: int) -> None:
    """🔴 冻结前：cadence ∉ 锁定档 ⇒ 直接 raise（fail-closed）。"""
    with pytest.raises(RuntimeError):
        RV.assert_cadence_locked(bad)


def test_冻结前硬断言接受20() -> None:
    assert RV.assert_cadence_locked(20) == 20


def test_冻结前硬断言拒绝None() -> None:
    """🔴 cadence 缺失 ⇒ **不许**静默回落到常量。"""
    with pytest.raises(RuntimeError):
        RV.assert_cadence_locked(None)


def test_变异反证_放开锁定档后90就会被放行(monkeypatch) -> None:
    """🔴 这道防线**确实是**由「锁定档」驱动的：把锁定档放开 ⇒ cadence=90 被放行。

    ⇒ 反过来证明：默认锁定档 = (20,) 时，90 **必然**被拒（见上一条）。
    """
    monkeypatch.setattr(C, "REBALANCE_CADENCE_GRID", (20, 40, 60, 90))
    assert RV.assert_cadence_locked(90) == 90, (
        "🔴 锁定档放开后 cadence=90 应当被放行 ⇒ 说明这条防线**只**认锁定档"
    )


def test_冻结阶段拒绝cadence90(tmp_path) -> None:
    """🔴 **冻结级**防线：搜索产物里若带 cadence=90 ⇒ 冻结必须直接 raise。

    🔴 这就是 2026-11-17 夜班那次事故的**最后一道拦截**
    （当时 cadence=90 被冻结 ⇒ 单盲 −12.93% / 回撤 −54.93%）。
    """
    import argparse
    import json

    payload = {
        "best_params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                        "rebalance_every_n_days": 90},
        "best_score": 0.4,
        "n_trials_run": 1, "n_trials_total": 1, "truncated": False,
        "val_window": list(C.VALID),
        "trials": [{
            "params": {"smoothing_arm": "A_half_life_20", "top_k": 5, "hold_buffer": 2,
                       "rebalance_every_n_days": 90},
            "score": 0.4, "objective": {}, "status": "ok",
        }],
    }
    (tmp_path / "arm_search_val.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        RV.stage_freeze(argparse.Namespace(), tmp_path)
    assert "cadence" in str(exc.value), (
        f"🔴 报错必须点名 cadence：{exc.value!r}")


# --------------------------------------------------------------------- ② 换手惩罚有上限
def test_换手惩罚在12_32倍时被截断() -> None:
    """🔴 12.32× 换手（旧口径实测最低）⇒ 惩罚必须是 **0.02**，不是 0.416。"""
    penalty = C.turnover_penalty(12.32)
    assert penalty == pytest.approx(C.TURNOVER_PENALTY_CAP), (
        f"🔴 惩罚必须被上限截断：得到 {penalty}"
    )
    assert penalty < 0.05, (
        "🔴 惩罚项必须小于超额差异的典型量级（0.05~0.20），否则它会把 alpha 盖住"
    )


def test_换手惩罚不超过目标时为0() -> None:
    assert C.turnover_penalty(3.0) == 0.0
    assert C.turnover_penalty(4.0) == 0.0


def test_换手惩罚对非有限值稳健() -> None:
    assert C.turnover_penalty(float("nan")) == 0.0
    assert C.turnover_penalty(float("inf")) == 0.0
    assert C.turnover_penalty(None) == 0.0


#: 🔴 **事故形状**（2026-11-17 夜班那次冻结的真实对比）：
#:    * 「蜗牛」trial：超额 0.15、换手 **5.3×**（cadence=90 的量级）
#:    * 「月频」trial：超额 0.20、换手 **12.32×**（cadence=20 的量级）
#:    旧口径（惩罚无上限）：蜗牛 0.15−0.065=**0.085** vs 月频 0.20−0.416=**−0.216**
#:    ⇒ **蜗牛胜出** ⇒ 这就是 cadence=90 被冻结的机制。
#:    新口径（惩罚有上限 0.02）：蜗牛 0.15−0.02=**0.13** vs 月频 0.20−0.02=**0.18**
#:    ⇒ 月频胜出 ⇒ 与用户红线「锁死 cadence=20」一致。
_SNAIL = (0.15, 5.3)     # (超额, 年化换手)
_MONTHLY = (0.20, 12.32)  # (超额, 年化换手)


def test_惩罚不再压倒超额() -> None:
    """🔴 **决定性断言**：超额更好的「月频」trial 必须胜出。

    惩罚项不许把超额盖住 —— 那正是搜出 cadence=90 的机制。
    """
    exc_a, turn_a = _MONTHLY
    exc_b, turn_b = _SNAIL
    score_a = exc_a - C.turnover_penalty(turn_a)
    score_b = exc_b - C.turnover_penalty(turn_b)
    assert score_a > score_b, (
        f"🔴 超额更好的月频 trial 必须胜出：{score_a} vs {score_b}"
        "（惩罚项不许把超额盖住 —— 那正是搜出 cadence=90 的机制）"
    )


def test_变异反证_去掉上限后必须重新让蜗牛胜出(monkeypatch) -> None:
    """🔴 把上限去掉 ⇒ **蜗牛重新胜出** ⇒ 上一条断言必须转红（证明上限是有效的）。"""
    monkeypatch.setattr(C, "TURNOVER_PENALTY_CAP", float("inf"))
    exc_a, turn_a = _MONTHLY
    exc_b, turn_b = _SNAIL
    score_a = exc_a - C.turnover_penalty(turn_a)
    score_b = exc_b - C.turnover_penalty(turn_b)
    assert score_a <= score_b, (
        f"🔴 去掉上限后应当重新出现「蜗牛胜出」：{score_a} vs {score_b}"
        "⇒ 上限那条断言没有牙"
    )


def test_闸门兜底默认不是min_turnover() -> None:
    """🔴 默认兜底必须是「按主目标取分」，**不是**「换手最低者」（后者 = 旧事故）。"""
    assert C.TURNOVER_GATE_ON_MISS == "best_score", (
        f"🔴 兜底口径必须是 best_score：{C.TURNOVER_GATE_ON_MISS!r}"
    )
