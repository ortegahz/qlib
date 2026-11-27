#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-27（11-26 全量跑批 step4 判红修复）：交付层「**占席**」口径守卫。

【判红现场（11-26 跑批 · 闸门 `verify_artifacts`）】
    [FAIL] 规则层与交付层入选同源（TA 否决/顺延已对账）
        规则层 ['159928','512800','515170','515450']｜交付层 ['512800','515450']
        ｜期望 ['512800','515450','515650']｜TA 否决 [...]｜顺延 ['515650']｜维持席位 ['512800']
⇒ 交付层**实际有** `515650`（顺延补位**已占席**），但它的目标权重是 **0**
（「防御态有效槽位 1 ⇒ 本席位本轮不配权重（归现金）」）
⇒ 旧口径只取 `target_weight > 0` 的行 ⇒ 把这个**已被归零的席位**误判成「交付层没入选」
⇒ 一次**正确的** TA 否决 + 防御态降仓，被误报成「规则层与交付层不同源」
（并连带 `probe_verify_guards` 的 P0/N11 两条因全局退出码**连坐**而红）。

【本文件钉死】
① **占席** = 目标权重 > 0 **或** 该行**不是清仓行**
   （零权重但仍占席的席位**必须**计入 ⇒ 否则闸门会被自己的归零逻辑误伤）；
② **清仓行不占席位**（旧口径就有的语义：**不许**回退）；
③ 部分减仓（规则层 23.28% → 20%）**仍占席位**（目标权重 > 0）；
④ **变异反证（守卫有牙）**：席位**缺失** ⇒ 必须判红；席位**凭空多出** ⇒ 必须判红；
   清仓行（被清掉的持仓）**不许**被当成「凭空多出」。

【为什么单独一个文件】
`verify_artifacts.py` 是**只读产物自检脚本**（跑在服务器上、断言交付件），
本机没有生产快照 ⇒ 用**合成夹具**把「占席口径」这条逻辑单测出来。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _find_repo_root() -> Path:
    """向上找工程根（含 ``manu/etf_rotation/verify_artifacts.py`` 的那层）。

    🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``）
       ⇒ **不许**用固定的 ``parents[N]`` 定位。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "etf_rotation" / "verify_artifacts.py"
        if candidate.is_file():
            return parent
    raise RuntimeError("定位工程根失败：找不到 manu/etf_rotation/verify_artifacts.py")


ROOT = _find_repo_root()
MODULE_PATH = ROOT / "manu" / "etf_rotation" / "verify_artifacts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_verify_artifacts_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VA = _load_module()

#: 闸门断言名（改了断言名 ⇒ 用例必须失败 ⇒ 名字是契约的一部分）
CHECK_NAME = "规则层与交付层入选同源（TA 否决/顺延已对账）"

#: 11-26 真实形态（合成）：规则层 4 只、TA 否决 5 只、顺延 1 只、维持 1 只
_REC_HOLDINGS = ["159928", "512800", "515170", "515450"]
_REJECTED = ["515170", "563020", "512800", "159928", "512010"]
_SUBSTITUTED = ["515650"]
_ORPHANS = ["512800"]

#: 交付层 3 行（11-26 真实形态）：515650 **占席但权重 0**（防御态有效槽位归零）
_ACTIONS_1126 = [
    {"code": "515450", "qlib_action": "买入", "action": "买入",
     "target_weight": 0.1172126939442869, "target_weight_effective": 0.1172126939442869},
    {"code": "515650", "qlib_action": "买入", "action": "持有",
     "target_weight": 0.0, "target_weight_effective": 0.0},
    {"code": "512800", "qlib_action": "维持", "action": "维持",
     "target_weight": 0.23278730605571307, "target_weight_effective": 0.23278730605571307},
]


def _plan(actions: list[dict]) -> dict:
    return {
        "cash_flow": {
            "cash_begin": 23372.59, "sell_proceeds": 0.0, "sell_fees": 0.0,
            "buy_amount": 3477.6, "buy_fees": 5.0, "cash_end": 19889.99,
            "invested_weight_planned": 0.35, "target_cash_weight": 0.65,
            "unbuildable_slots": [],
        },
        "actions": actions,
        "ta_screening": {
            "rejected": [{"code": c} for c in _REJECTED],
            "substituted": [{"code": c} for c in _SUBSTITUTED],
            "orphaned_holdings": list(_ORPHANS),
        },
        "meta": {},
        "llm_conflicts": [],
    }


def _rec() -> dict:
    return {"holdings": [{"code": c} for c in _REC_HOLDINGS]}


def _seat_check(actions: list[dict], tmp_path: Path) -> tuple[bool, str]:
    """跑真的 `_check_final_plan` ⇒ 取「同源」那条断言的 (ok, detail)。"""
    results = VA._check_final_plan(_plan(actions), "", _rec(), tmp_path)
    hits = [r for r in results if r[0] == CHECK_NAME]
    assert hits, f"断言名变了（找不到 {CHECK_NAME!r}）⇒ 本守卫失配"
    return bool(hits[0][1]), str(hits[0][2])


# =============================================================================
# ① 占席口径
# =============================================================================
def test_零权重席位仍算入选():
    """11-26 真实形态：顺延席位权重被防御态归零 ⇒ **仍算入选**（不许判红）。"""
    seats, zero_weight = VA._delivery_seat_codes(_ACTIONS_1126)
    assert seats == ["512800", "515450", "515650"], seats
    assert zero_weight == ["515650"], zero_weight


def test_清仓行不占席位():
    """清仓行（被清掉的持仓）**不占席位**（旧语义不许回退）。"""
    actions = list(_ACTIONS_1126) + [
        {"code": "512999", "qlib_action": "卖出", "action": "卖出", "target_weight": 0.0},
    ]
    seats, _ = VA._delivery_seat_codes(actions)
    assert "512999" not in seats, seats


def test_部分减仓仍占席位():
    """部分减仓（规则层 23.28% → 20%）目标权重 > 0 ⇒ **仍占席位**。"""
    actions = [{"code": "512800", "qlib_action": "卖出", "action": "卖出",
                "target_weight": 0.2, "target_weight_effective": 0.2}]
    seats, zero_weight = VA._delivery_seat_codes(actions)
    assert seats == ["512800"], seats
    assert zero_weight == [], zero_weight


# =============================================================================
# ② 端到端：同源断言（11-26 真实形态）
# =============================================================================
def test_同源断言对11_26真实形态判绿(tmp_path):
    """交付层 3 席（含 1 个零权重席位）⇒ 与期望集合一致 ⇒ 判绿。"""
    ok, detail = _seat_check(_ACTIONS_1126, tmp_path)
    assert ok, detail
    assert "零权重席位" in detail, detail


def test_清仓行不算凭空多出(tmp_path):
    """交付层多一行「被清掉的持仓」⇒ **不许**判红（旧语义）。"""
    actions = list(_ACTIONS_1126) + [
        {"code": "512999", "qlib_action": "卖出", "action": "卖出", "target_weight": 0.0},
    ]
    ok, detail = _seat_check(actions, tmp_path)
    assert ok, detail


# =============================================================================
# ③ 变异反证（守卫有牙）
# =============================================================================
def test_变异_席位缺失必须判红(tmp_path):
    """把占席席位从交付层删掉 ⇒ 同源断言**必须**判红（否则闸门没牙）。"""
    actions = [a for a in _ACTIONS_1126 if a["code"] != "515650"]
    ok, detail = _seat_check(actions, tmp_path)
    assert not ok, f"席位缺失竟然判绿了 ⇒ 闸门没牙：{detail}"


def test_变异_零权重席位被删也必须判红(tmp_path):
    """🔴 变异：**只**把零权重席位删掉（旧口径看不见它）⇒ 现在必须判红。"""
    actions = [a for a in _ACTIONS_1126 if a["code"] != "515650"]
    ok, detail = _seat_check(actions, tmp_path)
    assert not ok, detail
    assert "515650" in detail, detail


def test_变异_凭空多出席位必须判红(tmp_path):
    """交付层多一只**不在期望集合里**的票 ⇒ 同源断言**必须**判红。"""
    actions = list(_ACTIONS_1126) + [
        {"code": "512010", "qlib_action": "买入", "action": "买入", "target_weight": 0.05},
    ]
    ok, detail = _seat_check(actions, tmp_path)
    assert not ok, f"凭空多出的席位竟然判绿了 ⇒ 闸门没牙：{detail}"
