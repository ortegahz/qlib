# -*- coding: utf-8 -*-
"""🔴 **分析侧重放链 ↔ 引擎决策链** 的**对齐回归网**（2026-11-18 续五）

【为什么要有这个文件】
  `analyze.py` 内部有一份「过滤链重放」（``_rebuild_chain``），用来给报告**逐道解释**
  「这只 ETF 是被哪一道网淘汰的」。它与真正的决策者 ``engine/portfolio.py::build_portfolio``
  是**两份独立实现** ⇒ 一旦**只改一边**，报告里的入选名单就会和引擎决策**悄悄分叉**。

  🔴 生产里**已经有**这道防线（``analyze.py::main``，约 3194 行）：
      ``if engine_selected != chain["selected_codes"]: raise RuntimeError(...)``
  ⇒ 不一致就**拒绝出报告**（fail-closed）。**这是对的**。

  🔴 但**没有任何 pytest 覆盖它** ⇒ 防线本身没人测 ⇒ 有人把
  「引擎侧改了、重放侧忘了改」这类改动合进来时，**只能等真实跑链时才发现**
  ⇒ 报告直接崩、实盘当天出不了报告。
  ⇒ 本文件把这条**已经存在于生产的不变量**变成**常驻回归网**。

【本文件覆盖三件事】
  ① **功能级**：同一份夹具下，``_rebuild_chain`` 与 ``build_portfolio`` 的入选名单
     **必须逐位相等**（刹车档 ＋ 对照档各一组）；
  ② **源码级**：刹车「防守资产旁路」在**两处实现**里必须**同时存在**
     （``analyze.py::_rebuild_chain`` ＋ ``engine/portfolio.py::build_portfolio``）
     ⇒ 只改一边 ⇒ 这里报红；
  ③ **变异反证**：把**临时副本**里的旁路条件改坏 ⇒ 上面那条守卫必须转红
     （证明它**有牙齿**，不是恒真）。

【🔴 口径边界（如实披露）】
  * 本文件测的是**决策链的选票**，**不是**整条实盘链（不跑 qlib / 不读生产快照）；
  * ``analyze.py::main`` 里那条「不一致就抛错」的生产断言**在 ``main()`` 内联**，
    依赖生产文件 ⇒ **无法在单测里直接调用** ⇒ 本文件用「两侧逐位相等」把**同一条不变量**
    在可测的层面钉住，并另用**源码守卫**钉住那条 RuntimeError **不许被删掉**。
"""
from __future__ import annotations

import importlib.util
import math
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

#: ``manu/etf_rotation/``（研究链根）—— 本文件在 ``daily/tests/`` 下 ⇒ 上溯两级
_ETF_DIR = Path(__file__).resolve().parents[2]
#: 🔴 必须排在**最前**：``manu/`` 与 ``manu/etf_rotation/`` 都**没有** ``__init__.py``
#:    ⇒ ``engine`` / ``config`` 等是**命名空间包** ⇒ 谁在 ``sys.path`` 更靠前谁赢
#:    ⇒ 这里显式把**活链**顶到最前，避免被镜像副本（``manu/engine/``）抢走。
sys.path.insert(0, str(_ETF_DIR))

import config as C  # noqa: E402


def _load_by_path(name: str, path: Path):
    """🔴 按**绝对路径**加载模块。

    🔴 为什么不用 ``import analyze``：``manu/analyze.py`` 与 ``manu/etf_rotation/analyze.py``
       **同名**（镜像对）⇒ 裸 import 的解析结果**取决于 sys.path 顺序**
       ⇒ 测的可能是**镜像副本**而不是活链 ⇒ 测出来的东西**不可信**。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: 活链的 ``analyze.py``（按绝对路径加载 ⇒ 不受同名遮蔽影响）
_ANALYZE = _load_by_path("etf_rotation_analyze_parity_target", _ETF_DIR / "analyze.py")
_PORTFOLIO = _load_by_path(
    "etf_rotation_engine_portfolio_parity_target", _ETF_DIR / "engine" / "portfolio.py"
)

#: 🔴 断言引擎模块解析到的**就是活链那份**（不是镜像副本）
#:    （镜像副本与活链**逐字一致**，但一旦守卫⑬ 被绕过，这里必须先炸）
assert Path(_PORTFOLIO.__file__).resolve().read_bytes() == (
    _ETF_DIR / "engine" / "portfolio.py"
).resolve().read_bytes(), "🔴 引擎模块没解析到活链那份 ⇒ 本文件测的就不是活链"

#: 🔴 两处实现各自的文件路径（源码级守卫用）
_ANALYZE_SRC = _ETF_DIR / "analyze.py"
_PORTFOLIO_SRC = _ETF_DIR / "engine" / "portfolio.py"


# ---------------------------------------------------------------- §1 夹具
#: 夹具 ETF：3 只权益 + 2 只防守（黄金 518880 / 国债 511010）
#: 🔴 与 ``test_nav_state_brake.py`` 同款构造 ⇒ 两份测试**互为对照**。
_BRAKE_ETFS: tuple[tuple[str, str, float, float, int, float, str], ...] = (
    ("510300", "样本甲ETF", 4.0, 0.0006, 37, 0.0, "equity_cn"),
    ("510500", "样本乙ETF", 6.0, 0.0008, 53, 1.1, "equity_cn"),
    ("512100", "样本丙ETF", 2.0, 0.0010, 71, 2.3, "equity_cn"),
    ("518880", "黄金ETF", 5.0, 0.0004, 41, 0.7, "commodity"),
    ("511010", "国债ETF", 110.0, 0.0001, 61, 2.0, "bond"),
)

#: 成分股（**只有权益 ETF 有** ⇒ 防守资产 coverage = 0 ⇒ 严格口径下会被剔光
#:    ⇒ 正好复现「急跌 ⇒ 退化成 100% 现金」那个坑）
_BRAKE_HOLDINGS: tuple[tuple[str, str, str, float], ...] = (
    ("510300", "样本甲ETF", "SH600000", 60.0),
    ("510300", "样本甲ETF", "SZ000001", 40.0),
    ("510500", "样本乙ETF", "SH600519", 50.0),
    ("510500", "样本乙ETF", "SZ000002", 50.0),
    ("512100", "样本丙ETF", "SH601318", 70.0),
    ("512100", "样本丙ETF", "SZ000651", 30.0),
)

_ASOF = pd.Timestamp("2026-11-06")


def _inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """夹具：``(prices, holdings, scores, universe)``（**纯内存**，不读生产 CSV）。"""
    n_days = max(int(C.MIN_HISTORY_DAYS) + 10, 130)
    days = list(pd.bdate_range(end=_ASOF, periods=n_days))
    rows: list[dict] = []
    for code, _name, base, drift, period, phase, _bucket in _BRAKE_ETFS:
        for index, day in enumerate(days):
            close = (
                base
                * (1 + drift * index)
                * (1 + 0.002 * math.sin(2 * math.pi * index / period + phase))
            )
            rows.append({
                "date": day, "code": code, "open": close, "high": close * 1.002,
                "low": close * 0.998, "close": close, "volume": 1.0e7, "amount": 1.2e8,
            })
    prices = pd.DataFrame(rows)
    universe = pd.DataFrame([
        {"code": code, "name": name, "avg_amount_20d": 1.2e8, "float_mcap": 1.0e10,
         "last_price": 3.0, "fund_type_bucket": bucket}
        for code, name, *_rest, bucket in _BRAKE_ETFS
    ])
    holdings = pd.DataFrame(
        list(_BRAKE_HOLDINGS), columns=["etf_code", "etf_name", "instrument", "weight_pct"],
    )
    instruments = sorted({row[2] for row in _BRAKE_HOLDINGS})
    score_rows: list[dict] = []
    for step, day in enumerate(days[-3:]):
        for j, instrument in enumerate(instruments):
            score_rows.append({
                "date": day, "instrument": instrument, "score": 1.0 + 0.3 * step - 0.1 * j,
            })
    scores = pd.DataFrame(score_rows)
    return prices, holdings, scores, universe


def _nav_state(in_brake: bool) -> dict:
    return {
        "nav": 500.0 if in_brake else 1000.0,
        "peak": 1000.0,
        "in_brake": bool(in_brake),
        "cooldown_left": 0,
    }


def _run_both(*, in_brake: bool) -> tuple[list[str], list[str]]:
    """同一份夹具、同一组参数，**同时**跑引擎决策链与分析侧重放链。

    🔴 参数**逐项对齐**：任何一个不等，两边选出不同票都可能是「参数不同」而不是「实现分叉」。
    """
    prices, holdings, scores, universe = _inputs()
    nav_state = _nav_state(in_brake)

    portfolio = _PORTFOLIO.build_portfolio(
        asof=_ASOF, prices=prices, holdings=holdings, scores=scores, universe=universe,
        current_holdings={}, alpha_weight=None, momentum_weight=None, hold_buffer=None,
        pool_mode="e_covered", nav_state=nav_state,
        entry_ma_window=0, exit_ma_window=0, alpha_override=None, smoothing_config=None,
        n_target=None, max_per_theme=None,
    )
    chain = _ANALYZE._rebuild_chain(
        asof=_ASOF, prices=prices, holdings=holdings, scores=scores, universe=universe,
        current_holdings={}, alpha_weight=None, momentum_weight=None, hold_buffer=None,
        pool_mode="e_covered", entry_ma_window=0, exit_ma_window=0,
        brake_mode=bool(in_brake), alpha_override=None, n_target=None, max_per_theme=None,
    )
    engine_codes = sorted(str(h["code"]) for h in portfolio["holdings"])
    replay_codes = sorted(str(c) for c in chain["selected_codes"])
    return engine_codes, replay_codes


# ---------------------------------------------------------------- §2 功能级：两侧逐位相等
def test_刹车档两侧必须选出同一批防守标的() -> None:
    """🔴 刹车档：引擎决策链与分析侧重放链的**入选名单必须逐位相等**。

    🔴 这正是生产里 ``analyze.py::main`` 那条 RuntimeError 在守的不变量。
    """
    engine_codes, replay_codes = _run_both(in_brake=True)
    assert engine_codes, "🔴 刹车档必须选出**防守资产**；空仓 = 退化成 100% 现金"
    assert engine_codes == replay_codes, (
        "🔴 刹车档两侧选票分叉（引擎 vs 重放）⇒ 生产里会「拒绝出报告」：\n"
        f"  引擎 build_portfolio：{engine_codes}\n"
        f"  分析侧重放 _rebuild_chain：{replay_codes}\n"
        "  （engine/portfolio.py 改了但 analyze.py 没同步，或反之）"
    )
    assert set(engine_codes) <= set(C.DEFENSIVE_CODES), (
        f"刹车档只许选防守资产 {tuple(C.DEFENSIVE_CODES)}，实得 {engine_codes}"
    )


def test_对照组两侧必须选出同一批标的() -> None:
    """🔴 对照组：不刹车时两侧也必须逐位相等，且**能选出权益资产**。

    🔴 对照组的作用：证明上一条不是「两边都空 ⇒ 恰好相等」的恒真。
    """
    engine_codes, replay_codes = _run_both(in_brake=False)
    assert engine_codes, "对照组：夹具本身必须能选出标的"
    assert engine_codes == replay_codes, (
        "🔴 对照组两侧选票分叉：\n"
        f"  引擎：{engine_codes}\n  重放：{replay_codes}"
    )
    assert any(code not in set(C.DEFENSIVE_CODES) for code in engine_codes), (
        f"对照组：权益资产应能入选，实得 {engine_codes}"
    )


# ---------------------------------------------------------------- §3 源码级守卫
#: 🔴 池过滤的「防守资产旁路」必须**原样**出现在两处实现里。
_KEEP_MASK = (
    'keep_mask = cand["coverage_ok"].eq(True) | cand["is_defensive"].eq(True)'
)
#: 🔴 旁路的**开关条件**：只有在**刹车档**才开（正常档一字不改 ⇒ V1 基线不漂）。
_BRAKE_GATE = (
    'if getattr(C, "POOL_ALLOW_DEFENSIVE_BYPASS", False) or brake_mode:'
)


def _has_defensive_bypass(src: str) -> bool:
    """源码里**同时**存在「coverage_ok | is_defensive」的 keep_mask 与「or brake_mode」开关。"""
    return _KEEP_MASK in src and _BRAKE_GATE in src


def test_刹车防守旁路的两处实现不许分叉() -> None:
    """🔴 ``analyze.py::_rebuild_chain`` 与 ``engine/portfolio.py::build_portfolio``
    的**刹车防守旁路必须同时存在**。

    🔴 为什么这是**独立**的一条：功能级那条要**整个夹具跑通**才能发现分叉；
       而「只改一边」最常见的后果是**行为变了**（不是崩溃）⇒ 功能级可能因为
       选票恰好相同而**漏过** ⇒ 需要一条**源码级**的牙齿。
    """
    for src_path in (_ANALYZE_SRC, _PORTFOLIO_SRC):
        src = src_path.read_text(encoding="utf-8")
        assert _has_defensive_bypass(src), (
            f"🔴 {src_path} 缺少「刹车档 ⇒ 防守资产旁路」：\n"
            f"  必须含：{_KEEP_MASK}\n"
            f"  必须含：{_BRAKE_GATE}\n"
            "  （只改一边 ⇒ 引擎决策与报告重放分叉 ⇒ 生产会拒绝出报告）"
        )


def test_分析侧必须复用引擎的防守判定() -> None:
    """🔴 ``analyze.py`` 必须**复用** ``engine.portfolio._is_defensive``，不许自己再写一份。

    🔴 两份「什么算防守资产」的判定 ⇒ 将来改了一份 ⇒ 引擎与重放对同一只 ETF 的
       分类不同 ⇒ 旁路行为分叉 ⇒ 又是「拒绝出报告」。
    """
    src = _ANALYZE_SRC.read_text(encoding="utf-8")
    assert re.search(r"from\s+engine\.portfolio\s+import\s*\(", src), (
        "🔴 analyze.py 必须从 engine.portfolio 导入（不许另起一份防守判定）"
    )
    assert "_is_defensive," in src, (
        "🔴 analyze.py 必须**复用** engine.portfolio._is_defensive ⇒ 不许自己再实现一份"
    )
    assert not re.search(r"^def\s+_is_defensive\b", src, flags=re.M), (
        "🔴 analyze.py 里不许自己再定义一个 _is_defensive（会和引擎那份漂移）"
    )


def test_生产里的逐位比对断言不许被删掉() -> None:
    """🔴 ``analyze.py::main`` 里那条「两侧不一致 ⇒ 拒绝出报告」的断言必须还在。

    🔴 这条断言是**生产里最后一道防线** ⇒ 它被删掉 ⇒ 分叉会**静默**进报告。
    """
    src = _ANALYZE_SRC.read_text(encoding="utf-8")
    assert "engine_selected != chain[\"selected_codes\"]" in src, (
        "🔴 analyze.py 必须保留「引擎 vs 重放」逐位比对"
    )
    assert "分析侧重放的过滤链与引擎决策不一致" in src, (
        "🔴 比对失败必须**抛错拒绝出报告**（不许降级成告警）"
    )
    assert re.search(r"raise\s+RuntimeError\(", src), "🔴 不一致必须抛 RuntimeError"


# ---------------------------------------------------------------- §4 变异反证（证明守卫有牙齿）
def test_变异反证_去掉重放链的刹车旁路必须被抓到() -> None:
    """🔴 把**临时副本**里的 ``or brake_mode`` 改坏 ⇒ 源码守卫必须报红。

    🔴 只动**临时目录里的副本** ⇒ **绝不碰真源码**。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "analyze_broken.py"
        src = _ANALYZE_SRC.read_text(encoding="utf-8")
        assert _BRAKE_GATE in src, "🔴 前提不成立：活链里找不到那条开关（守卫已失效）"
        broken.write_text(src.replace(_BRAKE_GATE, _BRAKE_GATE.replace("or brake_mode", "or False")), encoding="utf-8")
        assert not _has_defensive_bypass(broken.read_text(encoding="utf-8")), (
            "🔴 变异反证失效：把 `or brake_mode` 改成 `or False` 后守卫竟然没报红 ⇒ 守卫是摆设"
        )


def test_变异反证_删掉防守旁路必须被抓到() -> None:
    """🔴 把**临时副本**里的 keep_mask 旁路删掉 ⇒ 源码守卫必须报红。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "portfolio_broken.py"
        src = _PORTFOLIO_SRC.read_text(encoding="utf-8")
        assert _KEEP_MASK in src, "🔴 前提不成立：活链里找不到那条 keep_mask（守卫已失效）"
        broken.write_text(src.replace(_KEEP_MASK, 'keep_mask = cand["coverage_ok"].eq(True)'), encoding="utf-8")
        assert not _has_defensive_bypass(broken.read_text(encoding="utf-8")), (
            "🔴 变异反证失效：删掉防守旁路后守卫竟然没报红 ⇒ 守卫是摆设"
        )


def test_变异反证_刹车档退化成空仓必须被抓到() -> None:
    """🔴 变异反证：**对照组**若也全是防守资产 ⇒ 刹车档那条断言就失去意义。

    🔴 证明「只选防守资产」这条断言**真的咬住了刹车开关**，不是恒真。
    """
    engine_codes, _replay = _run_both(in_brake=False)
    assert not (engine_codes and set(engine_codes) <= set(C.DEFENSIVE_CODES)), (
        "🔴 变异反证失效：不刹车时竟然全是防守资产 ⇒ 刹车档断言恒真，没有牙齿"
    )
