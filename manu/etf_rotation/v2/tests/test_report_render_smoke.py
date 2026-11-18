"""🔴 V2 **报告渲染冒烟测试** —— 2026-11-17 夜班审计补（**潜伏崩溃**）。

**背景（审计挖出的真缺陷）**
  `report.py` 的「年化换手（单边）」那行按 **6 个位置参数**调用 `_metric_row(...)`
  （多传了一个「×」单位后缀），而 `_metric_row` **根本没有** `suffix` 形参
  ⇒ `--stage report` 必抛 ``TypeError: _metric_row() takes from 3 to 5 positional
  arguments but 6 were given`` ⇒ **报告阶段必崩**。
  🔴 **为什么这条特别贵**：报告阶段排在**一次性盲测之后**
  ⇒ 一旦崩了，盲测 token 已消耗、成绩单却出不来 ⇒ 代价极大。

**本文件钉死**
  ① 用**自造夹具**渲染成绩单 ⇒ **不许抛异常**（缺数据 ⇒ 写「—」，不许崩）；
  ② 【变异反证】把 `suffix` 形参删掉 ⇒ ①必须转红（证明这条测试有牙）。

⛔ 不联网、不跑回测、不读真实产物（全部用 tmp_path 自造）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

REPORT_SOURCE = _V2_DIR / "report.py"


def _import_v2_report():
    """🔴 按**绝对路径**加载 `v2/report.py` ⇒ 免疫 `sys.modules['report']` 被抢占。

    🔴 2026-11-17 夜班实测（本条是**真回归**，不是风格问题）：
      `manu/etf_rotation/daily/tests/conftest.py` 会把 `daily/` 挂进 `sys.path`，
      而 `daily/tests/test_report.py` 的 `import report` **先**把
      `sys.modules['report']` 占成了 `daily/report.py`（它的 `write_report` 要两个参数）。
      ⇒ 本文件原先的 `import report as RP` 在**全量 pytest** 里拿到的是 daily 的模块
      ⇒ 2 条 FAIL（`TypeError: write_report() missing 1 required positional argument: 'path'`），
      而**单跑本文件却全绿**（单跑时 `sys.modules` 干净）。
      ⇒ 修法：不靠顶层裸名导入，改用 `spec_from_file_location` 按路径加载。
    """
    tag = "v2_report_under_test"
    spec = importlib.util.spec_from_file_location(tag, REPORT_SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[tag] = module
    spec.loader.exec_module(module)
    return module


RP = _import_v2_report()


def _load_module(path: Path, tag: str):
    spec = importlib.util.spec_from_file_location(tag, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[tag] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mutant(tmp_path_factory):
    """🔴 变异体：删掉 `_metric_row` 的 `suffix` 形参 ⇒ 回到「必崩」的旧口径。"""
    tmp_path = tmp_path_factory.mktemp("mutant")
    text = REPORT_SOURCE.read_text(encoding="utf-8")
    # 🔴 2026-11-18 夜班修正：**字面锚点太脆** ⇒ 只要给 `_cell` 补一句 docstring，
    #    锚点就匹配不上 ⇒ 变异 fixture 直接 ERROR（本次真的踩中了：闸门 pytest_v2 阶段报 ERROR）。
    #    改用**正则锚点**：只认「函数头 → 取值行 → 后缀行 → 收尾行」这组**骨架**，
    #    中间夹多少行 docstring 都不影响 ⇒ 变异体长期有效。
    edits = [
        (re.compile(r'def _metric_row\(label: str, v2: Any, v1: Any, digits: int = 4, pct: bool = False,\s*\n'
                    r'\s*suffix: str = ""\) -> str:'),
         'def _metric_row(label: str, v2: Any, v1: Any, digits: int = 4, pct: bool = False) -> str:'),
        (re.compile(r'    def _cell\(value: Any\) -> str:\n'
                    r'(?:[ \t]*[^\n]*\n)*?'
                    r'        text = _fmt\(value, digits, pct\)\n'
                    r'        return f"\{text\}\{suffix\}" if \(suffix and text != MISSING\) else text\n'
                    r'\n'
                    r'    return f"\| \{label\} \| \{_cell\(v2\)\} \| \{_cell\(v1\)\} \|"'),
         '    def _cell(value: Any) -> str:\n'
         '        return _fmt(value, digits, pct)\n'
         '\n'
         '    return f"| {label} | {_cell(v2)} | {_cell(v1)} |"'),
    ]
    for pattern, new in edits:
        text, n = pattern.subn(lambda _m, _new=new: _new, text, count=1)
        assert n == 1, f"🔴 变异锚点不存在（源码已变，正则匹配 0 次）：{pattern.pattern!r}"
    target = tmp_path / "mutant_report.py"
    target.write_text(text, encoding="utf-8")
    return _load_module(target, "mutant_report")


def _make_run_root(root: Path) -> None:
    """自造一个「产物根」：消融决策 ＋ 搜索产物 ＋ 冻结参数 ＋ 盲测凭证。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ablation_choice.json").write_text(json.dumps({
        "winner": "ablate_roc5", "val_window": ["2022-01-01", "2023-12-31"],
        "metric": "rank_ic_1d",
        "results": {"baseline_full": {"ic": 0.0708, "icir": 0.60, "t_stat": 4.0, "n_cross_sections": 46},
                    "ablate_roc5": {"ic": 0.0723, "icir": 0.62, "t_stat": 4.2, "n_cross_sections": 46}},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "val_search.json").write_text(json.dumps({
        "best_params": {"smoothing_arm": "A_half_life_15", "top_k": 4, "hold_buffer": 0,
                        "rebalance_every_n_days": 90},
        "best_score": -0.11, "grid": {"top_k": [4, 5, 6]}, "objective": "smoke",
    }, ensure_ascii=False), encoding="utf-8")
    (root / "frozen_params.json").write_text(json.dumps({
        "params": {"smoothing_arm": "A_half_life_15", "top_k": 4, "hold_buffer": 0,
                   "rebalance_every_n_days": 90, "entry_ma_window": 60, "exit_ma_window": 60,
                   "n_rebalances_val": 6, "sample_note": "smoke"},
        "best_score": -0.11, "model_name": "ablate_roc5", "winner_arm": "A_half_life_15",
        "rebalance_every_n_days": 90,
    }, ensure_ascii=False), encoding="utf-8")
    (root / "single_blind_test_2024_2026.json").write_text(json.dumps({
        "tag": "test_2024_2026", "status": "done", "created_at": "2026-11-17T00:00:00",
        "result_json": {
            "test_metrics": {"annualized_return": 0.01, "excess_vs_benchmark": 0.02,
                             "max_drawdown": -0.2, "annualized_turnover": 3.0,
                             "sharpe": 0.5, "calmar": 0.05, "total_cost_drag": 0.01},
            "val_metrics": {"annualized_return": 0.02, "annualized_turnover": 5.0},
            "rank_ic_test": {"ic": 0.05, "icir": 0.4, "t_stat": 3.0},
            "rank_ic_val": {"ic": 0.06},
        },
    }, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- ① 渲染不许崩
def test_成绩单渲染不许崩(tmp_path: Path) -> None:
    """🔴 `--stage report` **不许抛异常**（缺数据 ⇒ 写「—」）。"""
    root = tmp_path / "v2root"
    _make_run_root(root)
    out = RP.write_report(root)
    text = out.read_text(encoding="utf-8")
    assert "头条成绩单" in text, "🔴 成绩单必须含头条段"
    assert "年化换手（单边）" in text, "🔴 成绩单必须含换手行（🔴 这行就是崩溃点）"
    assert "默认口径 = 消融" in text, "🔴 成绩单必须披露默认口径"


def test_换手行必须带单位且两侧一致(tmp_path: Path) -> None:
    """🔴 换手行两侧都带「×」单位（口径与 `gen_v2_scorecard.py` 一致）。"""
    root = tmp_path / "v2root2"
    _make_run_root(root)
    text = RP.write_report(root).read_text(encoding="utf-8")
    row = [ln for ln in text.splitlines() if "年化换手（单边）" in ln]
    assert row, "🔴 找不到换手行"
    assert row[0].count("×") >= 2, (
        f"🔴 换手行的 V2 与 V1 两侧都应带「×」：{row[0]!r}")


# ---------------------------------------------------------------- ② 变异反证
def test_变异反证_删掉suffix形参_渲染必须转红(tmp_path: Path, mutant) -> None:
    """🔴 删掉 `suffix` 形参 ⇒ ①必须转红（证明这条测试有牙）。"""
    root = tmp_path / "v2root3"
    _make_run_root(root)
    with pytest.raises(TypeError):
        mutant.write_report(root)


def test_免疫同名模块污染_必须加载到v2的report(tmp_path: Path) -> None:
    """🔴 回归：`sys.modules['report']` 被**别的**同名模块抢占时，仍必须加载到 v2 的 report。

    🔴 这条对应 2026-11-17 夜班的真实事故：`daily/tests/test_report.py` 先执行
    `import report` ⇒ `sys.modules['report']` = `daily/report.py`
    ⇒ v2 的冒烟测试在**全量 pytest** 里 2 条 FAIL、单跑却全绿。
    🔴 有牙证明：把加载方式改回裸 `import report as RP` ⇒ 本条必红。
    """
    poison = types.ModuleType("report")
    poison.write_report = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("🔴 加载到了错误的 report 模块（daily 的同名模块污染了 sys.modules）"))
    old = sys.modules.get("report")
    sys.modules["report"] = poison
    try:
        mod = _import_v2_report()
        assert Path(mod.__file__).resolve() == REPORT_SOURCE.resolve(), (
            f"🔴 加载到的不是 v2/report.py：{mod.__file__}")
        root = tmp_path / "v2root4"
        _make_run_root(root)
        text = mod.write_report(root).read_text(encoding="utf-8")
        assert "头条成绩单" in text, "🔴 污染场景下成绩单仍必须渲染成功"
    finally:
        if old is None:
            sys.modules.pop("report", None)
        else:
            sys.modules["report"] = old
