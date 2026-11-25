#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-25：防御态「**有效槽位**」动态分配（解开「压降 ⇒ 席位碎屑 < 门槛」死锁）。

【钉的是什么】
压降态（生效权益上限 35%）下，规则层 5 席各 16%（合计 80%）被**等比压到** 35%
⇒ 每只只剩 7%（实测 30,425 元账户 ⇒ 2,129.75 元）
⇒ **低于新开仓门槛**（`config.MIN_TRADE_AMOUNT_NEW`）⇒ 一只都买不进 ⇒ 碎屑全归现金
⇒ 实测当夜：计划权益只剩 **6.98%**、现金 **93.19%**（「门槛数学死锁」）。

【新口径（只在压降态生效）】
有效槽位数 = ``floor(total_assets × 生效上限 / 新开仓门槛)``
（实测 30,425 × 35% / 门槛 ＝ 2.80 ⇒ floor 2 ⇒ **只留 2 席** ⇒ 每席 17.5%
≈ 5,324 元 ≥ 门槛 ⇒ 门槛不再拦）。

【本文件钉五条】
① 压降态（cap=0.35 / total_assets=30,425 / 5 席各 16%）⇒ 有效槽位 **2** 席、
   每席 **17.5%**（单只金额 ≥ 门槛）；
② 非压降态（cap=0.80）⇒ **与改动前逐字段相同**（旧等比缩放口径 ⇒ 产物逐字节不变）；
③ `total_assets` 缺失 ⇒ **不启用新逻辑**（回落现行为）；
④ 有效槽位 = 1 ⇒ 单只权重被**单只上限**截断，**差额归现金**（不许外借）；
⑤ **门槛值必须来自 `config.MIN_TRADE_AMOUNT_NEW`**（实现里不许写死字面量）。

【本文件有**两个镜像位置**（`manu/etf_rotation/tests/` 与 `manu/tests/`，
必须**逐字节一致**）⇒ 不许用固定的 `parents[N]` 定位 ⇒ 改成向上搜索。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

#: 🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 `parents[N]` 定位 ⇒ 改成向上搜索。
_ROOT = None
for _parent in Path(__file__).resolve().parents:
    if (_parent / "final_rebalance_plan.py").is_file():
        _ROOT = _parent
        break
assert _ROOT is not None, "找不到 final_rebalance_plan.py 所在目录"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config as C  # noqa: E402
import final_rebalance_plan as F  # noqa: E402

#: 账户总资产（实测口径：账户快照日 2026-11-23 收盘后 ⇒ 30,425.00 元）
_TOTAL_ASSETS = 30425.0
#: 压降态的生效权益上限（宏观降仓带的上沿）
_DEFENSIVE_CAP = 0.35
#: 规则层硬上限（1 − 现金预留比例）
_EQUITY_CAP = 0.80
#: 规则层 5 席各 16%（合计 80%）
_SEAT_TW = 0.16
_N_SEATS = 5


def _seats(n: int = _N_SEATS, tw: float = _SEAT_TW) -> list[dict]:
    """规则层动作（合成）：n 席各 tw。"""
    return [
        {"code": f"{600000 + i}", "name": f"ETF{i}", "action": "买入",
         "target_weight": tw, "current_weight": 0.0, "reason": "规则层入选"}
        for i in range(1, n + 1)
    ]


def _alloc(rule_actions, *, effective_cap=_DEFENSIVE_CAP, equity_cap=_EQUITY_CAP,
           orphan_weight=0.0, total_assets=_TOTAL_ASSETS, current_weights=None,
           min_trade_amount_new=None, max_single_weight=None):
    """跑一次**真函数**（门槛 / 单只上限默认取 config 的事实源，不写死字面量）。"""
    return F._defensive_effective_slots(
        rule_actions,
        effective_cap=effective_cap,
        equity_cap=equity_cap,
        orphan_weight=orphan_weight,
        total_assets=total_assets,
        current_weights=current_weights if current_weights is not None else {},
        min_trade_amount_new=(C.MIN_TRADE_AMOUNT_NEW
                              if min_trade_amount_new is None else min_trade_amount_new),
        max_single_weight=(C.MAX_SINGLE_WEIGHT
                           if max_single_weight is None else max_single_weight),
    )


def _legacy(rule_actions, effective_cap, equity_cap, orphan_weight):
    """**改动前**的口径（等比缩放）⇒ 用来逐字段比对「行为不变」那几条。"""
    seat_base = sum(float(F._f(a.get("target_weight"), 0.0) or 0.0)
                    for a in rule_actions)
    seat_cap = max(0.0, effective_cap - orphan_weight)
    scale = 1.0
    if seat_base > 0 and effective_cap < equity_cap - 1e-12:
        scale = min(1.0, seat_cap / seat_base)
    return [{**a, "target_weight": float(F._f(a.get("target_weight"), 0.0) or 0.0) * scale}
            for a in rule_actions], scale


# ───────────────────────── ① 压降态：有效槽位动态分配 ─────────────────────────
def test_压降态_有效槽位2席_每席17_5pct_单只金额过门槛():
    seats = _seats()
    out, facts = _alloc(seats, current_weights={"600001": 0.2318})
    es = facts["effective_slots"]
    assert es is not None, facts
    assert int(es["n_slots"]) == 2, es
    assert int(es["n_rule_seats"]) == _N_SEATS, es
    kept = [r for r in out if float(r["target_weight"]) > 0]
    assert len(kept) == 2, out
    for r in kept:
        assert abs(float(r["target_weight"]) - 0.175) < 1e-12, r
        #: 🔴 死锁的正解：每席金额**必须** ≥ 新开仓门槛（否则还是买不进）
        assert float(r["target_weight"]) * _TOTAL_ASSETS >= C.MIN_TRADE_AMOUNT_NEW, r
    #: 不变式①：合计 ≤ 生效上限
    assert sum(float(r["target_weight"]) for r in out) <= _DEFENSIVE_CAP + 1e-9, out


def test_压降态_有持仓的席位优先保留():
    seats = _seats()
    out, _facts = _alloc(seats, current_weights={"600005": 0.10})
    kept = [r["code"] for r in out if float(r["target_weight"]) > 0]
    assert len(kept) == 2, kept
    assert "600005" in kept, kept


def test_压降态_被剔除席位权重归0_带中文说明():
    seats = _seats()
    out, facts = _alloc(seats, current_weights={"600001": 0.2318})
    dropped = [r for r in out if float(r["target_weight"]) == 0.0]
    assert len(dropped) == _N_SEATS - 2, out
    for r in dropped:
        reason = str(r.get("reason") or "")
        assert "防御态有效槽位" in reason, r
        assert "归现金" in reason, r
    assert facts["effective_slots"]["dropped_codes"], facts


# ───────────────────────── ② 非压降态：行为逐字段不变 ─────────────────────────
def test_非压降态_原样返回_不启用新逻辑():
    seats = _seats()
    out, facts = _alloc(seats, effective_cap=_EQUITY_CAP)
    #: 🔴 新口径是「**筛选后**重分配」⇒ 非压降态 ⇒ **原样返回**（rows 一字不改）
    assert out == seats, "非压降态 ⇒ 必须原样返回（不改任何行）"
    #: 新口径没启用 ⇒ 调用方**不写任何新披露字段** ⇒ 产物逐字节不变
    assert facts["effective_slots"] is None, facts


def test_非压降态_即便总资产与门槛都齐也不启用():
    seats = _seats()
    out, facts = _alloc(seats, effective_cap=_EQUITY_CAP, total_assets=30425.0)
    assert facts["effective_slots"] is None, facts
    assert all(abs(float(r["target_weight"]) - _SEAT_TW) < 1e-12 for r in out), out


# ───────────────────────── ③ 总资产缺失：回落现行为 ─────────────────────────
def test_总资产缺失_原样返回_不启用新逻辑():
    seats = _seats()
    out, facts = _alloc(seats, total_assets=None)
    assert out == seats, "总资产缺失 ⇒ 必须原样返回（rows 不变）"
    assert facts["effective_slots"] is None, facts


def test_门槛缺失_原样返回_不启用新逻辑():
    seats = _seats()
    out, facts = _alloc(seats, min_trade_amount_new=0.0)
    assert out == seats, "门槛缺失 ⇒ 必须原样返回（rows 不变）"
    assert facts["effective_slots"] is None, facts


# ───────────────────────── ④ 有效槽位 = 1：单只上限截断 ─────────────────────────
def test_有效槽位1_单只权重被截到单只上限_差额归现金():
    #: 11,000 × 35% / 门槛 ＝ 1.01 ⇒ floor 1 ⇒ 只留 1 席
    seats = _seats()
    out, facts = _alloc(seats, total_assets=11000.0)
    es = facts["effective_slots"]
    assert int(es["n_slots"]) == 1, es
    assert abs(float(es["slot_weight"]) - float(C.MAX_SINGLE_WEIGHT)) < 1e-12, es
    assert sum(float(r["target_weight"]) for r in out) <= _DEFENSIVE_CAP + 1e-9, out
    #: 截断后的差额**归现金**（不许外借）
    expected_gap = _DEFENSIVE_CAP - float(C.MAX_SINGLE_WEIGHT)
    assert abs(float(es["cash_gap_weight"]) - expected_gap) < 1e-12, es


def test_维持行占额_席位预算先扣维持行():
    seats = _seats()
    out, facts = _alloc(seats, orphan_weight=0.10)
    es = facts["effective_slots"]
    assert es is not None, facts
    assert abs(float(es["seat_cap_weight"]) - (_DEFENSIVE_CAP - 0.10)) < 1e-12, facts
    assert sum(float(r["target_weight"]) for r in out) <= _DEFENSIVE_CAP + 1e-9, out


# ───────────────────────── ⑤ 门槛值来自 config（不许写死）─────────────────────────
def test_门槛值来自config_实现里不许写死字面量():
    source = inspect.getsource(F._defensive_effective_slots)
    assert "3800" not in source, "门槛字面量不许写死在实现里（必须取 config）"
    _, facts = _alloc(_seats())
    es = facts["effective_slots"]
    assert float(es["min_trade_amount_new"]) == float(C.MIN_TRADE_AMOUNT_NEW), es
    assert float(es["max_single_weight"]) == float(C.MAX_SINGLE_WEIGHT), es


def test_维持行占额大时_门槛数学改用席位预算_每席仍过门槛():
    """🔴 2026-11-25 审计**阻断项**回归：门槛数学必须用**席位预算**
    （＝ 生效上限 − 维持行占额），**不许**用生效上限。

    用生效上限算 ⇒ 槽位数被高估（2 席）⇒ 每席 0.075 ≈ 2,281.9 元 **< 3,800**
    ⇒ 两席全被 `min_trade_gate` 作废 ⇒ **死锁原样复发**（权益 0%）。
    """
    seats = _seats(n=2, tw=0.16)
    out, facts = _alloc(seats, orphan_weight=0.20)
    es = facts["effective_slots"]
    assert es is not None, facts
    #: 席位预算 = 0.35 − 0.20 = 0.15 ⇒ floor(30,425 × 0.15 / 3,800) = 1 ⇒ 只留 1 席
    assert abs(float(es["seat_cap_weight"]) - 0.15) < 1e-12, es
    assert int(es["raw_slots_floor"]) == 1, es
    assert int(es["n_slots"]) == 1, es
    #: 🔴 每席金额**必须** ≥ 新开仓门槛（否则死锁复发）
    held = [r for r in out if float(r["target_weight"]) > 0]
    assert held, out
    for r in held:
        assert float(r["target_weight"]) * _TOTAL_ASSETS >= C.MIN_TRADE_AMOUNT_NEW, r
    #: 合计 ≤ 席位预算（维持行占额先扣）
    assert sum(float(r["target_weight"]) for r in out) <= 0.15 + 1e-9, out


# ───────────────────────── ⑥ 存活席位重分配（🔴 筛选**之后**语义）─────────────────
def test_存活席位重分配_2席各175pct_每席5324元过门槛():
    """🔴 11-24 真机复跑坐实：规则层 5 席被 TA 否决 3 席 ⇒ 存活 2 席。
    旧口径（筛选前分配）⇒ 存活 2 席仍各 7%（2,130 元 < 3,800 元）⇒ 死锁不解。
    新口径（筛选后重分配）⇒ 存活 2 席各 17.5%（5,324 元）⇒ 门槛不再拦。"""
    seats = _seats(n=2, tw=0.07)
    out, facts = _alloc(seats)
    es = facts["effective_slots"]
    assert es is not None, facts
    assert int(es["n_slots"]) == 2, es
    assert int(es["n_rule_seats"]) == 2, es
    for r in out:
        assert abs(float(r["target_weight"]) - 0.175) < 1e-12, r
        assert float(r["target_weight"]) * _TOTAL_ASSETS >= C.MIN_TRADE_AMOUNT_NEW, r
    #: 合计 = 35%（生效上限），不多不少
    assert abs(sum(float(r["target_weight"]) for r in out) - _DEFENSIVE_CAP) < 1e-9, out


def test_存活席位数少于门槛数学时_全部保留_每席分得更多():
    """存活席位 < floor(...) ⇒ **全部保留**（不许硬砍）。"""
    seats = _seats(n=1, tw=0.07)
    out, facts = _alloc(seats)
    es = facts["effective_slots"]
    assert es is not None, facts
    assert int(es["n_slots"]) == 1, es
    assert int(es["raw_slots_floor"]) == 2, es
    #: 1 席 ⇒ 席预算 35% 全给它 ⇒ 但**单只上限 20%** 截断 ⇒ 差额 15% **归现金**
    assert abs(float(out[0]["target_weight"]) - float(C.MAX_SINGLE_WEIGHT)) < 1e-12, out
    assert abs(float(es["cash_gap_weight"]) - (_DEFENSIVE_CAP - float(C.MAX_SINGLE_WEIGHT))) < 1e-12, es


def test_卖出行为目标权重0_不算席位():
    """目标权重 = 0 的行（卖出 / 不建仓）⇒ **不是席位**，不许占槽位。"""
    seats = _seats(n=2, tw=0.07)
    seats.append({"code": "600009", "name": "ETF9", "action": "卖出",
                  "target_weight": 0.0, "current_weight": 0.05, "reason": "止盈"})
    out, facts = _alloc(seats)
    es = facts["effective_slots"]
    assert es is not None, facts
    assert int(es["n_rule_seats"]) == 2, es
    assert int(es["n_seat_rows"]) == 3, es
    assert abs(float(out[-1]["target_weight"])) < 1e-12, out
