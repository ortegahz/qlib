# -*- coding: utf-8 -*-
"""🔴 **接缝端到端探针的常驻回归网**（2026-11-18 续六）

【为什么要有这个文件】
  ``manu/tools/v2_seam_e2e_probe.py`` 是 V2 冻结件 → 每日实盘链的**端到端联调探针**：
  它用**同一份真实快照**跑三档（``A_v1`` 接缝关 / ``B_frozen`` 接缝开 / ``C_brake`` 急跌刹车），
  真的起 ``analyze.py`` / ``recommend.py`` / ``final_rebalance_plan.py`` **三个子进程**，
  再逐项断言「冻结件口径真的落到每日链」。

  🔴 2026-11-18 之前，这个探针**只能人手跑** ⇒ 它的一致性判定
     （``brake_chain_rc`` / ``brake_defensive_only`` / ``control_not_in_brake`` …）
     **全都没有回归网** ⇒ 谁把其中一条判定删掉、把 rc 判定改恒真、把 C 臂删掉，
     **pytest 一声不响** ⇒ 「接缝还通不通」这件事**没有任何自动化证据**。
  ⇒ 本文件把「探针**必须**守住哪些判定」变成**常驻断言**。

【两层结构（分工明确）】
  **第 1 层 · 常驻契约守卫**（秒级，永远跑，不需要任何数据）
      断言探针源码**必须**包含哪些一致性判定、必须跑哪三个臂、必须真的起哪几个子进程、
      一致性不通过**必须**返回非零退出码。
      ⇒ 它的作用是「**探针不许被悄悄阉割**」。
  **第 2 层 · 真跑端到端**（需要服务器侧真实快照 + qlib 数据 ⇒ **有数据就默认跑**）
      服务器上只要**有输入完整的快照**，这两条就会真的起探针 ⇒ 断言 ``rc == 0``
      ＋ ``seam_conformance_all_ok`` ＋ 逐项 ``ok`` 为 True。
      🔴 **没有它，第 1 层只是「字符串在不在」** ⇒ 它才是「接缝真的通」的**链级证据**。
      🔴 **设 ``ETF_ROTATION_E2E_SEAM=0`` 可以关掉**（本地没数据时无所谓；
         那里本来就因为「找不到完整快照」而 skip）。

【怎么跑第 2 层（服务器）】
    cd /tmp/pycharm_project_3b68a403
    export ETF_ROTATION_E2E_SEAM=1
    python -m pytest manu/etf_rotation/v2/tests/test_v2_seam_e2e_probe.py -q
  ⇒ 约 2~4 分钟（要起 6 次子进程）。

【🔴 口径边界（如实披露）】
  * 第 2 层测的是**接缝 + 刹车的因果链**（参数不变 ⇒ 行为变 ⇒ 行为只因刹车而变），
    **不是**在评估「真实市场里刹车救不救得回来」；
  * ``C_brake`` 档是**人为把净值打到峰值一半**造出来的急跌场景；
  * 阶段 5 的**最终名单** = 规则层候选池 × TradingAgents 决策件
    ⇒ 快照**无决策件**时阶段 5 整段跑不了 ⇒ 该档两项判定**标记为不适用**（不是 FAIL）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: 仓库根（``manu/etf_rotation/v2/tests/`` → 上溯四级）
REPO_ROOT = Path(__file__).resolve().parents[4]
#: 结果根（与 ``daily/daily_config.py`` 同口径：环境变量优先）
RESULTS_ROOT = Path(
    os.environ.get("ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results")
)
#: 🔴 被守卫的探针（**不许**另写一份探针 ⇒ 守卫的就是**生产用**那一份）
PROBE = REPO_ROOT / "manu" / "tools" / "v2_seam_e2e_probe.py"

#: 第 2 层的显式开关（**默认不跑** ⇒ 没有服务器数据时必须 skip，不许假绿）
E2E_ENV = "ETF_ROTATION_E2E_SEAM"

#: 探针**必须**做的一致性判定：判定名 ⇒ 源码里必须出现的证据片段
#: 🔴 每一条都对应接缝链上一条**曾经出过问题**的不变量 ⇒ 少一条就少一道防线。
_REQUIRED_CONFORMANCE: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 🔴 急跌日 analyze/recommend **不许崩**（V1 缺陷 B：急跌日整轮 TypeError）
    ("brake_chain_rc", ('"brake_chain_rc"', "rc_ok = cb_rc == (0, 0)")),
    # 🔴 急跌 ⇒ 切防守资产，**不许退化成 100% 现金**（V1 缺陷 A：入选 = []）
    ("brake_defensive_only", ('"brake_defensive_only"',)),
    # 🔴 对照组**不许**刹车（同参数同峰值、只因净值没跌）⇒ 证明差异来自刹车而非参数
    ("control_not_in_brake", ('"control_not_in_brake"',)),
    # 🔴 刹车**必须**改变入选（否则上面几条全是恒真）
    ("brake_changes_selection", ('"brake_changes_selection"',)),
    # 🔴 刹车状态**必须**真的触发（nav_state 落到规则层）
    ("brake_in_brake_true", ('"brake_in_brake_true"',)),
    ("brake_mode_flag", ('"brake_mode_flag"',)),
    # 🔴 阶段 5 规则层对账 rc / TA 宏观降仓带（接缝必须**穿到阶段 5**）
    ("stage5_rc", ('"stage5_rc"',)),
    ("macro_derisk_band", ('"macro_derisk_band"',)),
)

#: 探针**必须**跑满的三档（接缝关 / 接缝开 / 急跌刹车）
_REQUIRED_ARMS: tuple[str, ...] = ("A_v1", "B_frozen", "C_brake")
#: 探针**必须**真的起的子进程（不许只读产物、不许自己模拟）
_REQUIRED_SUBPROCESSES: tuple[str, ...] = ("analyze.py", "recommend.py", "final_rebalance_plan.py")
#: 一致性不通过 ⇒ **必须**返回非零退出码（否则 CI 拿不到信号）
_REQUIRED_EXIT = "return 0 if all_ok else 1"
#: JSON 产物里**必须**有这两个键（测试要读它们）
_REQUIRED_JSON_KEYS: tuple[str, ...] = ("seam_conformance", "seam_conformance_all_ok")


def _contract_violations(src: str) -> list[str]:
    """探针源码**必须**满足的契约；返回违规项清单（**空 = 满足**）。

    🔴 做成**函数**而不是写死在一堆 ``assert`` 里 ⇒ 变异反证可以拿**改坏的副本**
       喂进来 ⇒ 证明守卫**真的咬得住**（而不是恒真）。
    """
    out: list[str] = []
    for item, needles in _REQUIRED_CONFORMANCE:
        for needle in needles:
            if needle not in src:
                out.append(f"{item}: 探针源码缺少 {needle!r}")
    for arm in _REQUIRED_ARMS:
        if f'"{arm}"' not in src:
            out.append(f"探针缺少档位 {arm!r}（接缝关/接缝开/急跌刹车 必须三档都在）")
    for script in _REQUIRED_SUBPROCESSES:
        if script not in src:
            out.append(f"探针没有真的起 {script!r} 子进程（不许只读产物）")
    if _REQUIRED_EXIT not in src:
        out.append("一致性不通过时必须返回非零退出码（找不到 " + _REQUIRED_EXIT + "）")
    for key in _REQUIRED_JSON_KEYS:
        if f'"{key}"' not in src:
            out.append(f"探针 JSON 产物缺少键 {key!r}")
    return out


def _probe_src() -> str:
    assert PROBE.is_file(), f"🔴 接缝探针不存在：{PROBE}（守卫失效 ⇒ 必须报红）"
    return PROBE.read_text(encoding="utf-8")


# =============================================================================
# 第 1 层 · 常驻契约守卫（永远跑）
# =============================================================================
def test_接缝探针必须守住全部一致性判定() -> None:
    """🔴 探针**必须**保留接缝链上每一条曾出过问题的不变量判定。"""
    violations = _contract_violations(_probe_src())
    assert not violations, (
        "🔴 接缝探针被削弱了（一致性判定缺失 ⇒ 探针会「绿着但没测」）：\n  "
        + "\n  ".join(violations)
    )


def test_接缝探针必须真的起三个子进程且跑满三档() -> None:
    """🔴 探针必须**起子进程**跑 ``analyze.py`` / ``recommend.py`` / ``final_rebalance_plan.py``。

    🔴 不许退化成「读现成产物假装跑过」⇒ 那样 rc 永远是上一轮的旧值。
    """
    src = _probe_src()
    assert "subprocess.run" in src, "🔴 探针必须真的起子进程"
    for script in _REQUIRED_SUBPROCESSES:
        assert script in src, f"🔴 探针必须起 {script} 子进程"
    for arm in _REQUIRED_ARMS:
        assert f'"{arm}"' in src, f"🔴 探针必须跑满三档，缺 {arm}"


def test_接缝探针一致性不通过必须返回非零退出码() -> None:
    """🔴 一致性判定不通过 ⇒ 探针**必须**返回非零（CI 才有信号）。"""
    assert _REQUIRED_EXIT in _probe_src(), (
        "🔴 探针必须在一致性不通过时返回非零退出码（当前找不到 " + _REQUIRED_EXIT + "）"
    )


# =============================================================================
# 第 1 层 · 变异反证（证明守卫有牙齿，不是恒真）
# =============================================================================
def _mutate(src: str, old: str, new: str) -> str:
    assert old in src, f"🔴 前提不成立：探针源码里找不到 {old!r}（守卫可能已失效）"
    return src.replace(old, new)


def test_变异反证_把rc判定改恒真必须被抓到() -> None:
    """🔴 把 ``rc_ok = cb_rc == (0, 0)`` 改成恒真 ⇒ 守卫必须报红。"""
    src = _probe_src()
    broken = _mutate(src, "rc_ok = cb_rc == (0, 0)", "rc_ok = True")
    violations = _contract_violations(broken)
    assert any("brake_chain_rc" in v for v in violations), (
        f"🔴 变异反证失效：把 rc 判定改恒真后守卫没报红 ⇒ 守卫是摆设；实得 {violations}"
    )


def test_变异反证_删掉防守资产判定必须被抓到() -> None:
    """🔴 删掉 ``brake_defensive_only`` ⇒ 守卫必须报红。"""
    src = _probe_src()
    broken = _mutate(src, '"brake_defensive_only"', '"brake_defensive_only_removed"')
    violations = _contract_violations(broken)
    assert any("brake_defensive_only" in v for v in violations), (
        f"🔴 变异反证失效：实得 {violations}"
    )


def test_变异反证_删掉对照组判定必须被抓到() -> None:
    """🔴 删掉 ``control_not_in_brake`` ⇒ 守卫必须报红。"""
    src = _probe_src()
    broken = _mutate(src, '"control_not_in_brake"', '"control_not_in_brake_removed"')
    violations = _contract_violations(broken)
    assert any("control_not_in_brake" in v for v in violations), (
        f"🔴 变异反证失效：实得 {violations}"
    )


def test_变异反证_删掉刹车档必须被抓到() -> None:
    """🔴 删掉 ``C_brake`` 档 ⇒ 守卫必须报红（否则急跌档根本没人跑）。"""
    src = _probe_src()
    broken = _mutate(src, '"C_brake"', '"C_brake_removed"')
    violations = _contract_violations(broken)
    assert any("C_brake" in v for v in violations), (
        f"🔴 变异反证失效：实得 {violations}"
    )


def test_变异反证_退出码恒零必须被抓到() -> None:
    """🔴 把 ``return 0 if all_ok else 1`` 改成恒零 ⇒ 守卫必须报红。"""
    src = _probe_src()
    broken = _mutate(src, _REQUIRED_EXIT, "return 0")
    violations = _contract_violations(broken)
    assert any("退出码" in v for v in violations), (
        f"🔴 变异反证失效：实得 {violations}"
    )


def test_变异反证_不起子进程必须被抓到() -> None:
    """🔴 把子进程脚本名改掉 ⇒ 守卫必须报红（不许「读产物假装跑过」）。"""
    src = _probe_src()
    broken = _mutate(src, "analyze.py", "analyze_pretend.py")
    violations = _contract_violations(broken)
    assert any("analyze.py" in v for v in violations), (
        f"🔴 变异反证失效：实得 {violations}"
    )


# =============================================================================
# 第 2 层 · 真跑端到端（需要服务器侧真实数据 ⇒ 显式开关）
# =============================================================================
def test_真跑接缝探针必须全绿(tmp_path: Path) -> None:
    """🔴 真跑探针 ⇒ ``rc == 0`` ＋ ``seam_conformance_all_ok`` ＋ 每项 ``ok`` 为 True。

    🔴 这一条才是「**接缝真的通**」的链级证据（第 1 层只是「探针没被阉割」）。
    🔴 **服务器上有输入完整的快照就默认跑**（约 100 秒）⇒ 不许依赖「谁记得手动跑」。
    """
    if os.environ.get(E2E_ENV) == "0":
        pytest.skip(f"🔴 {E2E_ENV}=0 ⇒ 显式关掉端到端回归")
    snaps = _complete_snapshots(require_decision=False)
    if not snaps:
        pytest.skip(f"🔴 {RESULTS_ROOT / 'snapshots'} 下没有**输入完整**的快照 ⇒ 本项不适用")
    json_out = tmp_path / "seam_e2e.json"
    cmd = [
        sys.executable, str(PROBE),
        "--snapshot-dir", str(snaps[0]),
        "--json-out", str(json_out),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=3600,
    )
    assert proc.returncode == 0, (
        f"🔴 接缝端到端探针返回 {proc.returncode}（一致性判定没过）：\n"
        f"--- stdout 尾部 ---\n{(proc.stdout or '')[-3000:]}\n"
        f"--- stderr 尾部 ---\n{(proc.stderr or '')[-2000:]}"
    )
    assert json_out.is_file(), f"🔴 探针没写 JSON 产物：{json_out}"
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data.get("seam_conformance_all_ok") is True, (
        f"🔴 探针自报不一致：{data.get('seam_conformance')}"
    )
    items = {c["item"]: c for c in data.get("seam_conformance", [])}
    for must in (
        "brake_chain_rc", "brake_defensive_only", "control_not_in_brake",
        "brake_changes_selection", "brake_in_brake_true", "brake_mode_flag",
        # 🔴 阶段 5 三项：快照**无决策件**时标记为「不适用」(ok=True)，有决策件时必须是 PASS
        "stage5_rc", "stage5_weights_used", "macro_derisk_band",
    ):
        assert must in items, f"🔴 探针产物缺判定项 {must!r}"
        assert items[must].get("ok") is True, (
            f"🔴 判定项 {must!r} 未通过：got={items[must].get('got')!r}"
        )
    # 🔴 rc 必须**逐项**为 0（急跌日不许崩）
    rc_got = items["brake_chain_rc"].get("got") or {}
    assert rc_got.get("analyze_rc") == 0, f"🔴 急跌档 analyze rc 非 0：{rc_got}"
    assert rc_got.get("recommend_rc") == 0, f"🔴 急跌档 recommend rc 非 0：{rc_got}"


def _complete_snapshots(*, require_decision: bool) -> list[Path]:
    """**输入完整**的快照（新 → 旧）。

    🔴 每日链是**增量写**快照的 ⇒ 刚跑完 / 跑到一半时，**最新那个快照可能是半写的**
       （缺 ``stock_scores.csv`` 或 ``account_snapshot.csv``）⇒ 拿它去跑 ⇒ **假失败**。
    🔴 这里**独立实现**一遍完整性判据（不 import 探针）⇒ 与探针形成**交叉核对**。
    """
    snaps = RESULTS_ROOT / "snapshots"
    if not snaps.is_dir():
        return []
    out: list[Path] = []
    for snap in sorted((p for p in snaps.iterdir() if p.is_dir()), reverse=True):
        if not any(snap.glob("etf_prices_effective_*.csv")):
            continue
        if not (snap / "stock_scores.csv").is_file():
            continue
        if not (snap / "account_snapshot.csv").is_file():
            continue
        if require_decision and not (snap / "tradingagents_decision.md").is_file():
            continue
        out.append(snap)
    return out


def _latest_decision_snapshot() -> Path | None:
    """最新一个**带 TradingAgents 决策件**的快照（阶段 5 才跑得起来）。

    🔴 为什么需要它：快照**没有**决策件 ⇒ 阶段 5 **整段跑不了** ⇒ 那三项判定是
       「**不适用**」⇒ 「**冻结件真的穿到阶段 5**」这件事就**没被验证**。
       ⇒ 必须再跑一档**有决策件**的快照，把阶段 5 真跑一遍。
    """
    snaps = RESULTS_ROOT / "snapshots"
    if not snaps.is_dir():
        return None
    cands = sorted(p for p in snaps.iterdir() if p.is_dir())
    for snap in reversed(cands):
        if (snap / "tradingagents_decision.md").is_file():
            return snap
    return None


def test_带决策件的快照必须真跑到阶段5(tmp_path: Path) -> None:
    """🔴 用**带决策件**的快照再跑一遍 ⇒ 阶段 5 三项**必须是真跑**，不是「不适用」。

    🔴 这一条才是「**V2 冻结件真的穿过阶段 3/4 到达阶段 5**」的**链级证据**：
    只有它能证明**权重口径**（alpha/momentum）与 **TA 宏观降仓带**在阶段 5 同时生效。
    """
    if os.environ.get(E2E_ENV) == "0":
        pytest.skip(f"🔴 {E2E_ENV}=0 ⇒ 显式关掉端到端回归")
    snap = _latest_decision_snapshot()
    if snap is None:
        pytest.skip(f"🔴 {RESULTS_ROOT / 'snapshots'} 下没有带决策件的快照 ⇒ 本项不适用")
    json_out = tmp_path / "seam_e2e_stage5.json"
    cmd = [
        sys.executable, str(PROBE),
        "--snapshot-dir", str(snap),
        "--require-decision",
        "--json-out", str(json_out),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=3600,
    )
    assert proc.returncode == 0, (
        f"🔴 带决策件快照（{snap.name}）的接缝端到端返回 {proc.returncode}：\n"
        f"--- stdout 尾部 ---\n{(proc.stdout or '')[-3000:]}\n"
        f"--- stderr 尾部 ---\n{(proc.stderr or '')[-2000:]}"
    )
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data.get("seam_conformance_all_ok") is True, (
        f"🔴 探针自报不一致：{data.get('seam_conformance')}"
    )
    items = {c["item"]: c for c in data.get("seam_conformance", [])}
    # 🔴 阶段 5 必须**真的跑了**（got 是数值，不是 {"skipped": True}）
    stage5_rc = items["stage5_rc"].get("got")
    assert stage5_rc == 0, (
        f"🔴 阶段 5 没有真跑（got={stage5_rc!r}）⇒ 本条用例失去意义"
    )
    weights = items["stage5_weights_used"].get("got") or {}
    assert "alpha" in weights and "momentum" in weights, (
        f"🔴 阶段 5 权重口径没落盘：{weights!r}"
    )
    assert weights["alpha"] == 1.0 and weights["momentum"] == 0.0, (
        f"🔴 阶段 5 权重口径不是 V2 冠军的纯 E（alpha=1.0/momentum=0.0）：{weights!r}"
    )
    band = items["macro_derisk_band"].get("got")
    assert isinstance(band, (int, float)), (
        f"🔴 TA 宏观降仓带没有真跑（got={band!r}）"
    )
    assert float(band) <= 0.35, f"🔴 权益上限带硬顶 0.35，生效值 {band!r} 超了"

