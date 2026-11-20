"""🔴 2026-11-19 新增：「逐日台账」（§十四.122 载体 B）的**完整性**守卫。

【背景】
`rules.md §十四.122`（用户亲定铁律）要求把「**不可从开源数据复现的核心信息**」
（当天 ETF 权重、动作、否决理由、账户口径…）落成**可复盘的事实记录**；
其载体 B ＝ 机器可读的逐日台账 `manu/memory/records/etf_rotation_daily_ledger.csv`。
🔴 既有守卫（`test_daily_record_ledger.py`）只覆盖**写入端语义**（幂等/作废逻辑），
**台账文件本体没有任何机器闸门** ⇒ 列名被改、同一 (日期,代码) 出现两条「有效」、
同一天账户口径分叉、成交态与成交价自相矛盾…**全部不会报错**，只会**默默误导复盘**。

【被测对象】
`manu/tools/check_record_ledger.py`（**纯只读**闸门，不写、不改台账）。

【测试风格】
照抄同目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【变异反证（守卫有牙）】
M1 改台账**表头** ⇒ 必须报「A-表头漂移」；
M2 某行**字段数**不对 ⇒ 必须报「B-行结构」；
M3 同一 (日期,代码) 出现**两条「有效」** ⇒ 必须报「C-重复有效行」；
M4 `记录状态` 写成非法值 ⇒ 必须报「D-状态域」；
M5 `日期` 格式非法 ⇒ 必须报「E-日期格式」；
M6 权重越界（>1）⇒ 必须报「F-权重域」；
M7 同一天 `账户总资产` 分叉 ⇒ 必须报「G-同日语义口径」；
M8 未成交行却写成交价 ⇒ 必须报「H-成交一致性」；
M9 核心字段留空 ⇒ 必须报「I-核心字段缺失」；
M10 🔴 **反例**：「作废」的**历史行原样保留** ⇒ 必须**仍然 rc 0**
   （历史演化轨迹不许被当成重复有效行）；
M11 工具**只读** ⇒ 跑完台账 md5 必须一字不变。

【live 集成断言】
对**真实台账**跑工具 ⇒ 断言 rc == 0（**不许 skip、不许弱化**）。
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_record_ledger.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_record_ledger.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_record_ledger.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_record_ledger.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()

#: 台账相对工程根的落点
LEDGER_REL = "manu/memory/records/etf_rotation_daily_ledger.csv"

#: 写入端（表头事实源）
WRITER_REL = "manu/tools/append_daily_record.py"


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_record_ledger_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ＋
    #    ``from __future__ import annotations`` ⇒ dataclasses 会回查
    #    ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _md5(path: Path) -> str:
    hasher = hashlib.md5()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把真实工程里的台账 ＋ 写入端复制一份到临时目录（之后只改副本）。"""
    project = tmp_path / "project"
    for rel in (LEDGER_REL, WRITER_REL):
        source = PROJECT_DIR / rel
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return project


def _read_rows(sandbox: Path) -> tuple:
    """读出 (表头, 数据行列表)。"""
    path = sandbox / LEDGER_REL
    raw = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    return raw[0], raw[1:]


def _write_rows(sandbox: Path, header: list, rows: list) -> None:
    """写回 (表头, 数据行)。"""
    path = sandbox / LEDGER_REL
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _defect_checks(defects) -> set:
    return {defect.check for defect in defects}


def _reasons(defects) -> str:
    return " ｜ ".join(defect.reason for defect in defects)


# --------------------------------------------------------------------------- #
# 基线：真实状态必须是绿的
# --------------------------------------------------------------------------- #
def test_sandbox_baseline_is_green(sandbox: Path) -> None:
    """复制出来的基线必须 rc 0（否则下面的变异反证没有意义）。"""
    defects, facts = TOOL.analyze(str(sandbox))
    assert not defects, _reasons(defects)
    assert facts.row_count >= 1
    assert facts.effective_count >= 1


def test_live_ledger_is_green() -> None:
    """live 集成：真实台账必须 rc 0（不许 skip、不许弱化）。"""
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--project-dir", str(PROJECT_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


# --------------------------------------------------------------------------- #
# 变异反证
# --------------------------------------------------------------------------- #
def test_m1_header_must_match_writer(sandbox: Path) -> None:
    """M1：改台账表头 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    assert "目标权重" in header
    broken = list(header)
    broken[broken.index("目标权重")] = "目标占比"
    _write_rows(sandbox, broken, rows)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "表头漂移却没人报错 ⇒ 守卫没牙"
    assert "A-表头漂移" in _defect_checks(defects), _reasons(defects)


def test_m2_row_field_count_must_match(sandbox: Path) -> None:
    """M2：某行字段数不对 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    broken[0] = broken[0][:-2]
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "行结构坏了却没人报错 ⇒ 守卫没牙"
    assert "B-行结构" in _defect_checks(defects), _reasons(defects)


def test_m3_duplicate_effective_row_must_be_flagged(sandbox: Path) -> None:
    """M3：同一 (日期,代码) 出现两条「有效」⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    first = list(broken[0])
    broken.append(first)
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "重复有效行却没人报错 ⇒ 守卫没牙"
    assert "C-重复有效行" in _defect_checks(defects), _reasons(defects)


def test_m4_status_domain_must_be_enforced(sandbox: Path) -> None:
    """M4：`记录状态` 写成非法值 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("记录状态")
    broken[0][idx] = "已作废"
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "状态域非法却没人报错 ⇒ 守卫没牙"
    assert "D-状态域" in _defect_checks(defects), _reasons(defects)


def test_m5_date_format_must_be_enforced(sandbox: Path) -> None:
    """M5：日期格式非法 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("日期")
    broken[0][idx] = "2026/11/18"
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "日期格式非法却没人报错 ⇒ 守卫没牙"
    assert "E-日期格式" in _defect_checks(defects), _reasons(defects)


def test_m6_weight_out_of_range_must_be_flagged(sandbox: Path) -> None:
    """M6：权重越界（>1）⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("目标权重")
    broken[0][idx] = "1.5"
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "权重越界却没人报错 ⇒ 守卫没牙"
    assert "F-权重域" in _defect_checks(defects), _reasons(defects)


def test_m7_same_day_caliber_must_be_consistent(sandbox: Path) -> None:
    """M7：同一天 `账户总资产` 分叉 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("账户总资产")
    broken[0][idx] = "30035.58"
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "同日语义口径分叉却没人报错 ⇒ 守卫没牙"
    assert "G-同日语义口径" in _defect_checks(defects), _reasons(defects)


def test_m8_trade_consistency_must_be_enforced(sandbox: Path) -> None:
    """M8：未成交行却写成交价 ⇒ 必须报红。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("成交价")
    broken[0][idx] = "0.911"
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "成交态自相矛盾却没人报错 ⇒ 守卫没牙"
    assert "H-成交一致性" in _defect_checks(defects), _reasons(defects)


def test_m9_core_field_must_not_be_blank(sandbox: Path) -> None:
    """M9：核心字段留空 ⇒ 必须报红（缺失必须显式写「缺」）。"""
    header, rows = _read_rows(sandbox)
    broken = [list(row) for row in rows]
    idx = list(header).index("来源")
    broken[0][idx] = ""
    _write_rows(sandbox, list(header), broken)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "核心字段留空却没人报错 ⇒ 守卫没牙"
    assert "I-核心字段缺失" in _defect_checks(defects), _reasons(defects)


def test_m10_void_history_row_must_not_be_flagged(sandbox: Path) -> None:
    """M10（反例）：「作废」的历史行**原样保留** ⇒ 必须仍然 rc 0。"""
    defects, facts = TOOL.analyze(str(sandbox))
    assert not defects, _reasons(defects)
    assert facts.effective_count < facts.row_count, "本仓台账里应当存在「作废」的历史行 ⇒ 夹具失效"


def test_m11_tool_is_read_only() -> None:
    """M11：工具只读 ⇒ 跑完台账 md5 必须一字不变。"""
    ledger = PROJECT_DIR / LEDGER_REL
    before = _md5(ledger)
    TOOL.analyze(str(PROJECT_DIR))
    assert _md5(ledger) == before, "只读工具竟然改了台账"


def test_m12_missing_ledger_is_input_error(sandbox: Path) -> None:
    """M12：台账文件缺失 ⇒ 输入错误（rc 2），不许当成「绿」。"""
    (sandbox / LEDGER_REL).unlink()
    with pytest.raises(TOOL.InputError):
        TOOL.analyze(str(sandbox))


def test_m13_missing_writer_is_input_error(sandbox: Path) -> None:
    """M13：写入端缺失（表头事实源没了）⇒ 输入错误（rc 2），不许当成「绿」。"""
    (sandbox / WRITER_REL).unlink()
    with pytest.raises(TOOL.InputError):
        TOOL.analyze(str(sandbox))


def test_m14_header_is_anchored_to_writer(sandbox: Path) -> None:
    """M14：期望值必须**锚写入端**（不许在检查器里复制一份列名 ⇒ 两处会分叉）。"""
    writer = TOOL.load_writer(str(sandbox))
    header, _ = _read_rows(sandbox)
    assert tuple(header) == tuple(writer.LEDGER_HEADER), "台账表头与写入端 LEDGER_HEADER 不一致"

def _load_sync_module():
    """按路径加载 `manu/tools/sync_slim_memory.py`（四层同步闸门本体）。"""
    path = PROJECT_DIR / "manu" / "tools" / "sync_slim_memory.py"
    spec = importlib.util.spec_from_file_location("sync_slim_memory_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_m15_wiring_propagates_ledger_defects(monkeypatch) -> None:
    """M15：四层闸门必须真的接上台账闸门 ⇒ 台账坏了 ⇒ 四层闸门必须红（不许只靠人记）。

    🔴 用**假缺陷**验证接线会传红（**不碰真实台账** ⇒ 不污染仓库文件）。
    """
    sync_mod = _load_sync_module()
    assert hasattr(sync_mod, "_check_record_ledger"), "sync_slim_memory.py 没接台账闸门 ⇒ §十四.122 只靠人记"
    tools_root = str(PROJECT_DIR / "manu")
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    import tools.check_record_ledger as checker_mod  # noqa: PLC0415

    monkeypatch.setattr(
        checker_mod,
        "analyze",
        lambda *args, **kwargs: (
            [checker_mod.Defect("台账", 1, "Z-假缺陷", "接线反证用的假缺陷", "")],
            checker_mod.LedgerFacts(),
        ),
    )
    problems = sync_mod._check_record_ledger()
    assert problems, "台账有缺陷，四层闸门却没红 ⇒ 接线没生效"
    # 接线会把缺陷的 **reason** 拼进问题清单 ⇒ 这里断言 reason 文本（不是 check 标签）
    assert "接线反证用的假缺陷" in problems[0], problems[0]


def test_m16_wiring_is_green_on_live_project() -> None:
    """M16：真实工程上，四层闸门接的台账闸门必须**无问题**。"""
    sync_mod = _load_sync_module()
    assert sync_mod._check_record_ledger() == [], "真实台账竟然有完整性缺陷"
