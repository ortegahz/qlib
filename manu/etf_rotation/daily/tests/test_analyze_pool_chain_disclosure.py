# -*- coding: utf-8 -*-
"""🔴 **候选池收窄口径**的**事实回归网**（2026-11-25 · 点 3）

【为什么要有这个文件】
  2026-11-24 夜 V2 full 交付报告暴露的报表小瑕疵之一（属「披露说假话」）：
  风险披露里写「候选池文件共 1664 行，**经流动性/属性过滤后剩 18 只** ⇒ 选股范围被人为收窄」，
  并且 `non_lookthrough_etf` 写「本次共 **0** 只 ETF 的 coverage < 50% … 已全部被剔除」。

  实测真因（服务器 2026-11-24 快照 `filter_stats.stages` 逐道 n_in/n_out）：
      1664 → 292（流动性/规模/历史；其中 **1367 只是没行情数据**，不是阈值淘汰）
      292 → 190（coverage 闸门 e_covered；真实淘汰 102 只 = 数据缺口 18 + 穿透不足 84）
      190 → **18**（绝对动量闸门 mom120>0；**砍掉 172 只**）← **真正的瓶颈**
      18  → 5  （同质去重 ＋ 同族）
  ⇒ 「池窄」的锅**不在 e_covered**，而在**绝对动量风控**（属 V2 冻结的选股口径 ⇒ 不动）。

【本文件覆盖】
  ① 有 ``filter_stats`` ⇒ 披露按**实测分解**写，并点名真正的瓶颈（绝对动量闸门）；
  ② ``non_lookthrough_etf`` 用 **coverage 闸门真实淘汰数**（102）而不是恒为 0 的伪计数；
  ③ 无 ``filter_stats`` ⇒ **回落旧表述**（fail-safe，不编数字）；
  ④ **变异反证**：把瓶颈识别改坏 ⇒ ① 必须转红。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

_ETF_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ETF_DIR))

import config as C  # noqa: E402

_ANALYZE_SRC = _ETF_DIR / "analyze.py"


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ANALYZE = _load_by_path("etf_rotation_analyze_poolchain_target", _ANALYZE_SRC)

#: 🔴 夹具 = 服务器 2026-11-24 快照的**实测**过滤链（不许编数字）
_STAGES = [
    {"stage": "filter_1_liquidity", "title": "过滤网 1 · 流动性", "n_in": 1664, "n_out": 292,
     "eliminated": [
         {"code": "158000", "detail": {"n_days": None}},          # 无行情数据（覆盖缺口）
         {"code": "158001", "detail": {"n_days": None}},          # 无行情数据（覆盖缺口）
         {"code": "510001", "detail": {"n_days": 30}},            # 历史不足
     ]},
    {"stage": "extra_pool_attribute", "title": "附加 · 标的池属性口径", "n_in": 292, "n_out": 190,
     "eliminated": [
         {"code": "159929", "detail": {"has_holdings_record": False}},   # 成分数据缺失
         {"code": "159001", "detail": {"has_holdings_record": True}},    # 穿透不足
     ]},
    {"stage": "extra_absolute_momentum", "title": "附加 · 绝对动量门槛", "n_in": 190, "n_out": 18,
     "eliminated": []},
    {"stage": "filter_2_3_dedup_and_theme", "title": "过滤网 2 · 同质化去重 ＋ 过滤网 3 · 主题多样性",
     "n_in": 18, "n_out": 5, "eliminated": []},
]

_FILTER_STATS = {
    "n_universe_rows": 1664,
    "n_with_price_data": 297,
    "n_without_price_data": 1367,
    "n_candidate_pool": 18,
    "stages": _STAGES,
}


def _risk(filter_stats: dict | None) -> list[dict]:
    return _ANALYZE._risk_disclosure(
        asof=pd.Timestamp("2026-11-24"),
        chain={"pool_mode": "e_covered", "hold_buffer": 2},
        n_etfs_no_e=0,
        n_universe_rows=1664,
        n_pool=18,
        holdings=[],
        total_assets=30425.00,
        cash=23371.80,
        backtest=None,
        score_stats=None,
        rank_ic=None,
        micro=None,
        lookahead=None,
        ma_windows=(0, 0),
        filter_stats=filter_stats,
    )


def _item(risk: list[dict], rid: str) -> dict:
    got = [r for r in risk if str(r.get("id")) == rid]
    assert got, f"风险披露里没有 id={rid} 的条目"
    return got[0]


# ---------------------------------------------------------------- §1 真实分解
def test_pool_attribute_limits_names_real_bottleneck() -> None:
    detail = str(_item(_risk(_FILTER_STATS), "pool_attribute_limits").get("detail") or "")
    assert "真正把池压窄的是「附加 · 绝对动量门槛」" in detail, \
        f"披露必须点名真正的瓶颈（绝对动量闸门）：{detail!r}"
    assert "190→18" in detail, f"披露必须给出瓶颈那一道的实测 n_in→n_out：{detail!r}"
    assert "1664" in detail and "292" in detail and "190" in detail, \
        f"披露必须给出逐道分解：{detail!r}"
    assert "1367" in detail, f"必须区分「行情覆盖缺口」（1367 只）与阈值淘汰：{detail!r}"
    assert "102" in detail, f"必须写出 coverage 闸门的真实淘汰数（102）：{detail!r}"


def test_non_lookthrough_uses_real_drop_count() -> None:
    detail = str(_item(_risk(_FILTER_STATS), "non_lookthrough_etf").get("detail") or "")
    assert "102" in detail, f"必须用 coverage 闸门真实淘汰数（102）：{detail!r}"
    assert "数据缺失" in detail or "成分数据缺失" in detail, f"必须分因（数据缺口 vs 穿透不足）：{detail!r}"
    assert "共 0 只" not in detail, f"不许再出现恒为 0 的伪计数：{detail!r}"


def test_no_filter_stats_falls_back_to_old_wording() -> None:
    detail = str(_item(_risk(None), "pool_attribute_limits").get("detail") or "")
    assert "经流动性/属性过滤后剩 18 只" in detail, \
        f"拿不到 filter_stats ⇒ 必须回落旧表述（fail-safe）：{detail!r}"
    assert "真正把池压窄的是" not in detail, "拿不到实测分解时**不许**编瓶颈名"


# ---------------------------------------------------------------- §2 变异反证
def test_mutation_breaks_bottleneck_detection(tmp_path: Path) -> None:
    src = _ANALYZE_SRC.read_text(encoding="utf-8")
    needle = "if _bottleneck is None or _drop > _bottleneck[1]:"
    assert needle in src, "🔴 活链里找不到瓶颈识别行 ⇒ 源码结构变了，变异反证失效"
    mutated = src.replace(needle, "if False:", 1)
    assert mutated != src
    tmp_src = tmp_path / "analyze_mutated_pool.py"
    tmp_src.write_text(mutated, encoding="utf-8")
    mut = _load_by_path("etf_rotation_analyze_poolchain_mutant", tmp_src)
    try:
        with pytest.raises(AssertionError):
            risk = mut._risk_disclosure(
                asof=pd.Timestamp("2026-11-24"),
                chain={"pool_mode": "e_covered", "hold_buffer": 2},
                n_etfs_no_e=0,
                n_universe_rows=1664,
                n_pool=18,
                holdings=[],
                total_assets=30425.00,
                cash=23371.80,
                backtest=None,
                score_stats=None,
                rank_ic=None,
                micro=None,
                lookahead=None,
                ma_windows=(0, 0),
                filter_stats=_FILTER_STATS,
            )
            detail = str(_item(risk, "pool_attribute_limits").get("detail") or "")
            assert "真正把池压窄的是「附加 · 绝对动量门槛」" in detail, \
                "变异后应点不出瓶颈 ⇒ 断言必须红"
    finally:
        sys.modules.pop("etf_rotation_analyze_poolchain_mutant", None)
