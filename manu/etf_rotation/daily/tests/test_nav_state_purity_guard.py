"""🔴 2026-11-27 机器化守卫：**净值序列纯净性**（不许测试污染生产净值序列）。

【真缺陷（已实测）】
  ``manu/etf_rotation/daily/tests/test_stage4_llm_controls.py`` 的
  ``_run_pipeline_until_stage4`` 跑 ``AR.run_pipeline(asof="2026-12-01", ...)``
  ⇒ 走到 ``agents_rebalance.py:1480`` 的 ``nav_mod.prepare_daily_nav(...)``，
    而该调用**故意不传 path=**（生产就该写生产路径）
  ⇒ ``nav_state.state_path()`` 落到**生产**路径 ``DC.NAV_STATE_FILE``
    （server 的 ``.../_state/nav_history.json``）
  ⇒ 每跑一次 daily 测试，就把一个 **asof=2026-12-01（未来日期！）** 的合成点
    写进**生产**净值序列（实测：跑测试前后生产文件 ``updated_at`` 变化）。

【两个后果（生产层根因，见 ``daily/nav_state.py``）】
  ① **污染净值序列评估**（序列只增不减 ⇒ 未来点永久留在序列里）；
  ② **抬高历史峰值 ⇒ 钝化急跌刹车**（``peak = max(points.values())`` 取全序列最大值
     ⇒ 污染值永久抬高 peak ⇒ 回撤被摊薄 ⇒ 急跌刹车该触发时不触发）。

【本守卫钉两件事】
  ① **静态守卫**：凡是会走到净值序列的测试文件，必须把净值状态**隔离到 tmp_path**
     （``prepare_daily_nav(..., path=...)`` 显式传参，**或** monkeypatch
     ``DC.NAV_STATE_FILE``）⇒ 否则报红并指出**文件与行号**；
  ② **行为守卫**（单元级，全用 tmp_path）：``prepare_daily_nav`` 必须**剔除未来点**
     （日期 > asof）、**留痕**、且 ``peak`` **只**由 ≤ asof 的点决定。

【为什么本文件自带 importlib 装载】
  镜像纪律要求本文件**成对存在**：
    * 活链 ``manu/etf_rotation/daily/tests/``
    * 镜像 ``manu/daily/tests/``（**已登记的过期副本**：那份连 ``nav_state.py`` 都没有）
  ⇒ 裸 ``import nav_state`` 在镜像侧会 ImportError ⇒ 本文件**自己**定位仓库根，
    按**绝对路径**装载**活链**的 ``daily_config`` / ``nav_state``，
    且**不改动** ``sys.modules`` 里别人正在用的模块对象 ⇒ 两处行为一致。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest


# ----------------------------------------------------------------- 定位活链
def _repo_root() -> Path:
    """从本文件**向上**找仓库根（含 ``manu/etf_rotation/daily/nav_state.py`` 的那层）。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位（与
    ``manu/etf_rotation/tests/test_mirror_pairs_guard.py`` 同一范式）。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "etf_rotation" / "daily" / "nav_state.py").is_file():
            return parent
    raise RuntimeError("定位仓库根失败：找不到 manu/etf_rotation/daily/nav_state.py")


REPO_ROOT = _repo_root()
DAILY_DIR = REPO_ROOT / "manu" / "etf_rotation" / "daily"

#: 静态守卫扫描的三个测试目录（相对仓库根）
GUARDED_TEST_DIRS = (
    "manu/etf_rotation/daily/tests",
    "manu/etf_rotation/tests",
    "manu/tests",
)

#: 会（或可能）走到净值序列的调用名
NAV_CALL_NAMES = frozenset({"run_pipeline", "prepare_daily_nav"})

#: 「前置判定」开关名（``preflight_only`` 是现役名；``dry_run`` 是它的旧名，
#:    也出现在「变异反证」用的源码副本里 ⇒ 两者都算「到不了净值阶段」）
PREFLIGHT_KWARGS = frozenset({"preflight_only", "dry_run"})


def _load_module(name: str, path: Path):
    """按**绝对路径**装载一个模块（**私有名** ⇒ 不污染 ``sys.modules``）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"装载失败：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_live_nav_state():
    """装载**活链**的 ``daily_config`` / ``nav_state``（镜像侧也能用）。"""
    config = _load_module("_nav_purity_guard_daily_config", DAILY_DIR / "daily_config.py")
    saved_path = list(sys.path)
    if str(DAILY_DIR) not in sys.path:
        sys.path.insert(0, str(DAILY_DIR))
    try:
        nav_state = _load_module("_nav_purity_guard_nav_state", DAILY_DIR / "nav_state.py")
    finally:
        sys.path[:] = saved_path
    # 🔴 ``nav_state`` 顶层 ``import daily_config as DC`` ⇒ 这里换成**私有**那份，
    #    避免污染（或被污染）``sys.modules["daily_config"]``。
    nav_state.DC = config
    return config, nav_state


# ----------------------------------------------------------------- 静态扫描
def _call_name(node: ast.Call) -> str:
    """取调用名（``foo.bar(...)`` ⇒ ``bar``；``bar(...)`` ⇒ ``bar``）。"""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _kwarg(node: ast.Call, name: str):
    """取关键字实参的**值节点**（没有该关键字 ⇒ ``None``）。"""
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_none_literal(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_true_literal(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _file_patches_nav_state_file(tree: ast.Module) -> bool:
    """该文件是否 monkeypatch 了 ``DC.NAV_STATE_FILE``（任意 ``setattr`` 写法都认）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "setattr":
            continue
        args = list(node.args)
        if len(args) >= 2:
            target = args[1]
            if isinstance(target, ast.Constant) and target.value == "NAV_STATE_FILE":
                return True
        for keyword in node.keywords:
            if keyword.arg != "name":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value == "NAV_STATE_FILE":
                return True
    return False


def _preflight_returns_before_nav() -> bool:
    """核验：``run_pipeline`` 的前置判定分支**先于**净值阶段返回。

    ⇒ 只有源码里 ``if preflight_only:`` 出现在 ``prepare_daily_nav(`` **之前**，
      「前置判定调用点不可能写净值序列」这个豁免才成立（**豁免必须可证伪**）。
    """
    source = (DAILY_DIR / "agents_rebalance.py").read_text(encoding="utf-8")
    branch = source.find("if preflight_only:")
    nav_call = source.find("prepare_daily_nav(")
    return branch != -1 and nav_call != -1 and branch < nav_call


def _analyze_source(source: str) -> list[dict]:
    """扫描一份源码 ⇒ 返回缺陷清单（空列表 = 干净）。

    判定口径（与需求逐条对齐）：
      * 出现 ``run_pipeline(...)`` / ``prepare_daily_nav(...)`` 调用点的文件，
        必须满足「**每个**调用点都显式传了 ``path=``」**或**「该文件 monkeypatch 了
        ``DC.NAV_STATE_FILE``」；
      * ``path=`` 认**变量形式**（``path=state`` / ``path=tmp_path / "s.json"`` …），
        但 ``path=None`` **不算**隔离（那等于写生产路径）；
      * ``run_pipeline(..., preflight_only=True)`` / ``dry_run=True`` 的调用点
        **到不了净值阶段**（源码已核验：前置判定分支先返回）⇒ 不算污染源。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    patched = _file_patches_nav_state_file(tree)
    defects: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name not in NAV_CALL_NAMES:
            continue
        path_value = _kwarg(node, "path")
        if path_value is not None and not _is_none_literal(path_value):
            continue                                     # ① 该调用点自己隔离了
        if name == "run_pipeline":
            preflight = any(
                _is_true_literal(value)
                for value in (_kwarg(node, key) for key in PREFLIGHT_KWARGS)
                if value is not None
            )
            if preflight and _preflight_returns_before_nav():
                continue                                 # ② 到不了净值阶段
        if patched:
            continue                                     # ③ 整个文件已重定向
        defects.append({"line_no": int(node.lineno), "call": name})
    return defects


def _analyze_file(path: Path) -> list[dict]:
    """扫描一个测试文件 ⇒ 缺陷清单。"""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return _analyze_source(source)


# ----------------------------------------------------------------- 夹具
def _account(total: float = 5000.0, cash: float = 1000.0,
             shares: int = 1000, mv: float = 1000.0) -> dict:
    return {"total_assets": total, "cash": cash,
            "positions": [{"code": "512800", "shares": shares, "market_value": mv}]}


def _prices_csv(tmp_path: Path, rows: list[tuple[str, str, float]]) -> Path:
    path = tmp_path / "etf_prices.csv"
    pd.DataFrame([{"date": d, "code": c, "close": v} for d, c, v in rows]).to_csv(
        path, index=False)
    return path


# =============================================================================
# 1. 静态守卫
# =============================================================================
def test_静态守卫_净值序列调用点必须隔离到tmp_path() -> None:
    """🔴 凡是会走到净值序列的测试文件，必须把状态文件重定向到 tmp_path。"""
    defects: list[dict] = []
    for rel in GUARDED_TEST_DIRS:
        directory = REPO_ROOT / rel
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("test_*.py")):
            for item in _analyze_file(path):
                item["file"] = str(path.relative_to(REPO_ROOT))
                defects.append(item)
    assert not defects, (
        "🔴 净值序列污染守卫：下列测试调用点会把净值写进**生产**序列"
        "（``DC.NAV_STATE_FILE``）⇒ 污染净值评估 + 抬高峰值钝化急跌刹车。\n"
        "   修法二选一：① ``prepare_daily_nav(..., path=<tmp_path 里的文件>)``；\n"
        "             ② 在该文件 monkeypatch ``DC.NAV_STATE_FILE`` 到 tmp_path。\n"
        + "\n".join(
            f"   · {d['file']}:{d['line_no']}  {d['call']}(...)" for d in defects
        )
    )


def test_守卫本身有牙齿_漏掉重定向必须被抓出来() -> None:
    """🔴 **变异反证**：守卫不能恒绿，也不能恒红。

    * 不传 ``path=`` 又没重定向 ⇒ **必须**被抓出来；
    * 传了 ``path=None``（= 写生产路径）⇒ **必须**被抓出来；
    * 重定向过（monkeypatch）或显式传 ``path=``（变量形式）⇒ **不许**被抓出来。
    """
    leaky = (
        "import daily_config as DC\n"
        "def t(tmp_path):\n"
        "    NS.prepare_daily_nav(asof='2026-11-20', account={}, prices_csv=None, notes=[])\n"
    )
    assert _analyze_source(leaky), "🔴 守卫失效：不传 path= 又没重定向的调用点必须被抓出来"

    leaky_none = (
        "def t():\n"
        "    NS.prepare_daily_nav(asof='2026-11-20', account={}, prices_csv=None,"
        " notes=[], path=None)\n"
    )
    assert _analyze_source(leaky_none), "🔴 path=None 等于写生产路径 ⇒ 必须被抓出来"

    patched = (
        "import daily_config as DC\n"
        "def t(tmp_path, monkeypatch):\n"
        "    monkeypatch.setattr(DC, 'NAV_STATE_FILE', tmp_path / 'nav_history.json')\n"
        "    NS.prepare_daily_nav(asof='2026-11-20', account={}, prices_csv=None, notes=[])\n"
    )
    assert not _analyze_source(patched), "🔴 守卫误报：重定向过的文件不该被抓出来"

    with_path = (
        "def t(state):\n"
        "    NS.prepare_daily_nav(asof='2026-11-20', account={}, prices_csv=None,"
        " notes=[], path=state)\n"
    )
    assert not _analyze_source(with_path), "🔴 守卫误报：显式传 path= 的调用点不该被抓出来"


# =============================================================================
# 2. 行为守卫（单元级，全用 tmp_path）
# =============================================================================
def test_行为守卫_未来点必须被剔除且峰值只由已发生观测决定(
    tmp_path: Path, monkeypatch
) -> None:
    """🔴 未来点（日期 > asof）必须剔除 + 留痕；``peak`` **只**由 ≤ asof 的点决定。"""
    _config, nav_state = _load_live_nav_state()
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_THRESHOLD", 0.15)
    monkeypatch.setattr(nav_state.DC, "ACCOUNT_SNAPSHOT_DATE", "2026-11-05")

    state = tmp_path / "nav_history.json"
    state.write_text(json.dumps({
        "points": {"2026-11-17": 30000.0, "2026-11-18": 31000.0, "2026-12-01": 99999.0},
        "updated_at": "2026-11-19T00:00:00",
    }), encoding="utf-8")

    prices = _prices_csv(tmp_path, [("2026-11-20", "512800", 2.0)])
    notes: list[str] = []
    out = nav_state.prepare_daily_nav(
        asof="2026-11-20", account=_account(), prices_csv=prices, notes=notes, path=state,
    )

    # ① 未来点被剔除（返回值留痕）
    assert out["dropped_future_points"] == ["2026-12-01"], (
        f"🔴 未来点必须留痕在返回值里，实得 {out.get('dropped_future_points')!r}")
    # ② 峰值**只**由 ≤ asof 的点决定（99999 那个未来点不许抬高峰值）
    assert out["peak"] == pytest.approx(31000.0), (
        f"🔴 峰值必须只由 ≤ asof 的点决定，实得 {out['peak']}")
    assert out["peak_date"] == "2026-11-18", f"峰值日期错：{out['peak_date']}"
    assert out["nav"] == pytest.approx(3000.0), f"净值错：{out['nav']}"
    # ③ 落盘的状态文件里也不许再有未来点
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "2026-12-01" not in saved["points"], (
        f"🔴 未来点不许留在落盘序列里，实得 {sorted(saved['points'])}")
    assert saved["points"]["2026-11-18"] == pytest.approx(31000.0)
    # ④ notes 里必须有可读披露
    assert any("剔除未来点" in note for note in notes), (
        f"🔴 剔除未来点必须在 notes 里留痕，实得 {notes}")


def test_行为守卫_账户快照种子日晚于asof时不许种(
    tmp_path: Path, monkeypatch
) -> None:
    """🔴 种子点受**同一条规则**约束：晚于 asof 的账户快照日不许进净值序列。"""
    _config, nav_state = _load_live_nav_state()
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_THRESHOLD", 0.15)
    monkeypatch.setattr(nav_state.DC, "ACCOUNT_SNAPSHOT_DATE", "2026-12-15")  # 🔴 未来

    state = tmp_path / "nav_history.json"
    prices = _prices_csv(tmp_path, [("2026-11-20", "512800", 2.0)])
    notes: list[str] = []
    out = nav_state.prepare_daily_nav(
        asof="2026-11-20", account=_account(total=99999.0), prices_csv=prices,
        notes=notes, path=state,
    )

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "2026-12-15" not in saved["points"], (
        f"🔴 未来日期的种子点不许进净值序列，实得 {sorted(saved['points'])}")
    assert out["peak"] == pytest.approx(3000.0), (
        f"🔴 未来种子点不许抬高峰值，实得 {out['peak']}")
    assert any("种子点未写入" in note for note in notes), (
        f"🔴 跳过未来种子必须在 notes 里留痕，实得 {notes}")


def test_行为守卫_无未来点时峰值与点数不受影响(
    tmp_path: Path, monkeypatch
) -> None:
    """🔴 无未来点 ⇒ 峰值 / 点数 / 回撤与改前**逐位一致**（只多一个空的留痕字段）。"""
    _config, nav_state = _load_live_nav_state()
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_ENABLED", True)
    monkeypatch.setattr(nav_state.DC, "DRAWDOWN_BRAKE_THRESHOLD", 0.15)
    monkeypatch.setattr(nav_state.DC, "ACCOUNT_SNAPSHOT_DATE", "2026-11-05")

    state = tmp_path / "nav_history.json"
    state.write_text(json.dumps({
        "points": {"2026-11-17": 30000.0, "2026-11-18": 31000.0},
        "updated_at": "2026-11-19T00:00:00",
    }), encoding="utf-8")

    prices = _prices_csv(tmp_path, [("2026-11-20", "512800", 2.0)])
    out = nav_state.prepare_daily_nav(
        asof="2026-11-20", account=_account(), prices_csv=prices, notes=[], path=state,
    )

    assert out["dropped_future_points"] == [], "无未来点 ⇒ 留痕字段必须为空"
    assert out["peak"] == pytest.approx(31000.0)
    assert out["peak_date"] == "2026-11-18"
    assert out["nav"] == pytest.approx(3000.0)
    assert out["drawdown"] == pytest.approx(3000.0 / 31000.0 - 1.0)
    # 回撤 −90% ⇒ 远超阈值 ⇒ 刹车**照样**触发（证明本条只动峰值口径，没动刹车语义）
    assert out["in_brake"] is True
    assert out["n_points"] == 4, f"种子 + 2 个历史点 + 当日点，实得 {out['n_points']}"
