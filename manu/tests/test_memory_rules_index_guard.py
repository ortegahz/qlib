"""🔴 2026-11-19 新增：`memory.md §0 铁律速览` ↔ `rules.md` **一一对应** ＋ **行数标注新鲜度**的守卫。

【背景】
两处**真实缺陷**（数据侧由主 Agent 修，本文件只做**只读守门**）：

① `manu/memory/rules.md` 是铁律**正文**（一级章节 `## 〇、` … `## 十六、` ＋ 附录 A/B）；
   `manu/memory/memory.md` 的 `## 0. 铁律速览` 表是给读者的**入口速览**，
   必须与正文一级章节**一一对应**。实测**整段漏了「十四」**（ETF 轮动线铁律，
   本线全部约束的主入口）⇒ 读者以为本线**无铁律**。
② `memory.md` / `memory_compact.md` 里手写的「`rules.md` **N 行**」是 2026-11-04 的**旧值**
   ⇒ 按行号跳转会**跳空**（违反 `rules.md §十四.93` 第 2 条 ①②）。

🔴 **判据陷阱**：memory 里还有**历史记录**行写着 `rules.md`（1891 → **1959 行**）——
   那是**历史事实**，「订正」它等于**篡改历史** ⇒ 守卫**不许全文 grep**，
   只能锚在**结构锚点**（5 个当前指针）上 ⇒ 历史行**既不算缺陷、也不许被当成当前标注**。

【被测对象】
`manu/tools/check_memory_rules_index.py`（**纯只读**闸门，不写任何被检查的文件）。

【测试风格】
照抄本目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）⇒ 见 ``test_daily_record_ledger.py``。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【变异反证（守卫有牙）】
D1 速览表删掉「十四」⇒ 必须报「漏了章节 十四」；
D2 某个锚点的行数标注改错（+1）⇒ 必须报「行数标注陈旧」；
D3 速览表塞进 rules.md 里没有的章节 ⇒ 必须报「陈旧行」；
D4 删掉 A4（速览卡页首那行）⇒ 必须报「锚点缺失」（**不许静默跳过**）；
D5 🔴 **反例**：塞进**历史记录**行 `rules.md`（1891 → **1959 行**）⇒ 必须**仍然 rc 0**
（历史行不许被当成当前标注，也不许被"订正"）。

【live 集成断言】
对**真实工程目录**跑工具 ⇒ 断言 rc == 0（**不许 skip、不许弱化**）。
若该条红 ⇒ 是主 Agent 正在并行修 `manu/memory/memory.md` 的 §0 表 / 行数标注，
**不是**本守卫的 bug（D1~D5 已用 fixture 逐条自证工具行为正确）。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_memory_rules_index.py``。

    🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
    两者必须逐字一致）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索
    ⇒ 两侧都能真正跑到被测工具（否则镜像侧直接 FileNotFoundError ⇒ 收集错误）。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_memory_rules_index.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 manu/tools/check_memory_rules_index.py")


#: 被测工具的路径（``manu/tools/check_memory_rules_index.py``）
TOOL_PATH = _find_tool()

#: 真实工程目录（工具的 ``manu/tools/`` 在 ``<root>/manu/tools/`` ⇒ 上溯两级）
PROJECT_DIR = TOOL_PATH.parents[2]


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在 ``sys.path`` 默认位置）。"""
    spec = importlib.util.spec_from_file_location("check_memory_rules_index_under_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()

#: fixture 里 `rules.md` 的一级铁律章节标签（与真实文件的编号序列一致）
CHAPTER_LABELS = (
    "〇",
    "〇之二",
    "〇之三",
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
)

#: 🔴 历史记录行（**历史事实，不许"订正"**）：带反引号形态（真实文件里的写法）
HISTORY_ROW = "| 历史记录 | 触发：`rules.md`（1891 → **1959 行**）/ `memory.md` 被改 |"

#: 🔴 历史记录行：**不带**反引号形态（用来逼 A4 的正则也别误伤）
HISTORY_LINE = "🔴 历史记录：2026-11-04 那天 rules.md（1891 → **1959 行**）被改"


def _rules_text() -> str:
    """fixture 的 `rules.md`：19 个一级铁律章节 ＋ 附录 A/B（**天然豁免**）。"""
    lines = ["# manu/memory/rules.md — 铁律正文（fixture）", ""]
    for label in CHAPTER_LABELS:
        lines.append(f"## {label}、【fixture】章节 {label}")
        lines.append("")
        lines.append(f"- **铁律正文**：这是 {label} 的正文（fixture）。")
        lines.append("")
    lines.append("## 附录 A、环境速查（fixture）")
    lines.append("")
    lines.append("- 附录不参与章节覆盖检查（**天然豁免**：标签「附录」不在章节正则的字符类里）。")
    lines.append("")
    lines.append("## 附录 B、从 yolo 工程移植的条款映射（fixture）")
    lines.append("")
    lines.append("- 附录不参与章节覆盖检查（**天然豁免**）。")
    lines.append("")
    return "\n".join(lines)


def _master_text(line_count: int) -> str:
    """fixture 的 `memory.md`：§0 速览表覆盖全部章节 ＋ 5 个锚点里的 3 个（A1/A2/A3）。"""
    lines = [
        "# manu/memory/memory.md — 主索引（fixture）",
        "",
        "> **定位**：**活文档**（fixture）。",
        "",
        f"## 0. 铁律速览（正文在 `manu/memory/rules.md`，**{line_count} 行**，**必读**）",
        "",
        "| # | 铁律 | 一句话 |",
        "|:--|:--|:--|",
    ]
    for label in CHAPTER_LABELS:
        lines.append(f"| {label} | **fixture 章节 {label}** | 一句话 |")
    lines += [
        "",
        "## 1. 当前状态快照",
        "",
        "### 1.1 工程状态",
        "",
        "| 项 | 状态 |",
        "|:--|:--|",
        f"| **本工程 `manu/` 体系** | ✅ **已建立**：`memory/`"
        f"（rules.md **{line_count} 行** + memory.md + memory_compact.md） |",
        "",
        "### 1.2 三条工作线",
        "",
        "| 线 | 状态 |",
        "|:--|:--|",
        "",
        "## 2. 铁律导航",
        "",
        "| 项 | 位置 | 说明 |",
        "|:--|:--|:--|",
        f"| **铁律正文** | `manu/memory/rules.md` | 全部不可协商约束（**{line_count} 行**） |",
        "",
    ]
    return "\n".join(lines)


def _compact_text(line_count: int) -> str:
    """fixture 的 `memory_compact.md`：页首 A4 指针 ＋ 表内 A5 指针。"""
    lines = [
        "# manu/memory/memory_compact.md — 5 分钟速览卡（fixture）",
        "",
        f"> **新会话第一眼读这个。** 铁律在 `manu/memory/rules.md`（**{line_count} 行**）。",
        "> **版本**：2026-11-05（fixture）",
        "",
        "| 项 | 位置 |",
        "|:--|:--|",
        f"| 铁律正文 | `manu/memory/rules.md`（**{line_count} 行**） |",
        "",
    ]
    return "\n".join(lines)


def _write_fixture(tmp_path: Path, *, master_mutator=None, compact_mutator=None) -> Path:
    """造一套**自洽**的 memory 三件套（临时目录），返回工程目录。

    自洽 ＝ §0 表覆盖 `rules.md` 全部章节 ＋ 5 个当前指针锚点的 `**N 行**` 全都等于
    `rules.md` 的实测行数 ⇒ 守卫必须**零缺陷**。
    """
    rules_text = _rules_text()
    line_count = len(rules_text.splitlines())
    master_text = _master_text(line_count)
    compact_text = _compact_text(line_count)
    if master_mutator is not None:
        master_text = master_mutator(master_text)
    if compact_mutator is not None:
        compact_text = compact_mutator(compact_text)

    memory_dir = tmp_path / "manu" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "rules.md").write_text(rules_text, encoding="utf-8")
    (memory_dir / "memory.md").write_text(master_text, encoding="utf-8")
    (memory_dir / "memory_compact.md").write_text(compact_text, encoding="utf-8")
    return tmp_path


def _run_cli(project_dir: Path) -> subprocess.CompletedProcess:
    """跑被测工具的 CLI（退出码语义 = 守卫的对外契约）。"""
    return subprocess.run(
        [sys.executable, str(TOOL_PATH), "--project-dir", str(project_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def _drop_chapter_row(label: str):
    """变异：把 §0 速览表里某一章节的那一行删掉。"""

    def mutator(text: str) -> str:
        lines = text.split("\n")
        kept = [line for line in lines if not line.startswith(f"| {label} |")]
        assert len(kept) == len(lines) - 1, f"没找到要删的 §0 行：{label}"
        return "\n".join(kept)

    return mutator


def _stale_anchor_row():
    """变异：把 A2 锚点（§1.1 表格行）的行数标注改成错值（+1）。"""

    def mutator(text: str) -> str:
        matched = re.search(r"（rules\.md \*\*(\d+) 行\*\*", text)
        assert matched is not None, "没找到 A2 锚点行"
        wrong = int(matched.group(1)) + 1
        return text.replace(matched.group(0), f"（rules.md **{wrong} 行**", 1)

    return mutator


def _add_stale_chapter_row():
    """变异：往 §0 速览表塞一行 `rules.md` 里**没有**的章节标签。"""

    def mutator(text: str) -> str:
        anchor = "| 十六 | **fixture 章节 十六** | 一句话 |"
        assert anchor in text, "没找到 §0 表的末行"
        return text.replace(anchor, anchor + "\n| 十七 | **fixture 章节 十七** | 一句话 |", 1)

    return mutator


def _drop_head_pointer():
    """变异：把 A4 锚点（速览卡**页首**那行）删掉。"""

    def mutator(text: str) -> str:
        lines = text.split("\n")
        kept = [line for line in lines if "新会话第一眼读这个" not in line]
        assert len(kept) == len(lines) - 1, "没找到 A4 锚点行"
        return "\n".join(kept)

    return mutator


def _inject_history():
    """变异：往**正文**塞**历史记录**行（🔴 历史事实，不许被当成当前标注）。"""

    def master_mutator(text: str) -> str:
        lines = text.split("\n")
        out = []
        inserted = False
        for line in lines:
            out.append(line)
            if line.startswith("| **本工程 `manu/` 体系** |"):
                # 塞进 §1.1 区块（A2 正则最狠的位置）⇒ 历史行必须**不**被当成当前指针
                out.append(HISTORY_ROW)
                inserted = True
        assert inserted, "没找到 §1.1 的 A2 锚点行"
        return "\n".join(out)

    def compact_mutator(text: str) -> str:
        lines = text.split("\n")
        # 塞进**前 5 行**（A4 的扫描范围）⇒ 不带反引号的历史形态也不许被当成当前指针
        lines.insert(4, HISTORY_LINE)
        return "\n".join(lines)

    return master_mutator, compact_mutator


def test_正例_自洽三件套_零缺陷(tmp_path: Path) -> None:
    """正例：fixture 三件套自洽 ⇒ 工具 rc 0 ＋ 零缺陷。"""
    project = _write_fixture(tmp_path)
    result = _run_cli(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[DEFECT]" not in result.stdout
    assert "缺陷 0 个" in result.stdout


def test_可导入_analyze_返回缺陷列表且每个缺陷有_as_dict(tmp_path: Path) -> None:
    """工具**必须可 import**（测试要用）：`analyze()` 暴露缺陷列表，`Defect.as_dict()` 可序列化。"""
    project = _write_fixture(tmp_path)
    memory_dir = project / "manu" / "memory"
    master_text = (memory_dir / "memory.md").read_text(encoding="utf-8")
    compact_text = (memory_dir / "memory_compact.md").read_text(encoding="utf-8")
    rules_text = (memory_dir / "rules.md").read_text(encoding="utf-8")

    defects = TOOL.analyze(master_text, compact_text, rules_text)
    assert defects == []
    assert hasattr(TOOL, "analyze")
    assert hasattr(TOOL.Defect, "as_dict")


def test_变异反证_D1_速览表删掉十四_必须报漏章(tmp_path: Path) -> None:
    """🔴 变异反证 D1：§0 表删掉「十四」⇒ 必须 rc ≠ 0 且**点名「十四」**。"""
    project = _write_fixture(tmp_path, master_mutator=_drop_chapter_row("十四"))
    result = _run_cli(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "速览表漏了章节 十四" in result.stdout
    assert "[DEFECT]" in result.stdout


def test_变异反证_D2_锚点行数标注改错_必须报陈旧(tmp_path: Path) -> None:
    """🔴 变异反证 D2：A2 锚点的行数标注改成 +1 ⇒ 必须 rc ≠ 0 且报「行数标注陈旧」。"""
    project = _write_fixture(tmp_path, master_mutator=_stale_anchor_row())
    result = _run_cli(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "行数标注陈旧" in result.stdout
    assert "锚点 A2" in result.stdout


def test_变异反证_D3_速览表塞入不存在的章节_必须报陈旧行(tmp_path: Path) -> None:
    """🔴 变异反证 D3：§0 表多一行 rules.md 里没有的章节 ⇒ 必须 rc ≠ 0 且报「陈旧行」。"""
    project = _write_fixture(tmp_path, master_mutator=_add_stale_chapter_row())
    result = _run_cli(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "速览表有陈旧行 十七（rules.md 已无此章节）" in result.stdout


def test_变异反证_D4_删掉A4锚点_必须报锚点缺失(tmp_path: Path) -> None:
    """🔴 变异反证 D4：删掉 A4（速览卡页首那行）⇒ 必须 rc ≠ 0 且报「锚点缺失」（不许静默跳过）。"""
    project = _write_fixture(tmp_path, compact_mutator=_drop_head_pointer())
    result = _run_cli(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "锚点缺失/标注丢失" in result.stdout
    assert "锚点 A4" in result.stdout


def test_反例_D5_历史记录行不许被当成当前标注(tmp_path: Path) -> None:
    """🔴 反例 D5：正文塞 `rules.md`（1891 → **1959 行**）**历史记录** ⇒ 必须**仍然 rc 0**。

    历史行是**历史事实**（订正它 = 篡改）⇒ 守卫既不许把它当缺陷，也不许把它当当前标注。
    """
    master_mutator, compact_mutator = _inject_history()
    project = _write_fixture(
        tmp_path,
        master_mutator=master_mutator,
        compact_mutator=compact_mutator,
    )
    result = _run_cli(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[DEFECT]" not in result.stdout
    # 1959 是历史值，**绝不许**被当成当前标注去和 rules.md 的实测行数比
    assert "1959" not in result.stdout


def test_用法错误_工程目录指错_退出码2(tmp_path: Path) -> None:
    """退出码语义：2 = 用法/输入错误（文件读不到 ⇒ 中文异常 ⇒ stderr）。"""
    result = _run_cli(tmp_path / "不存在的工程目录")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "[ERROR]" in result.stderr


def test_集成_真实工程目录_零缺陷() -> None:
    """live 集成断言：对**真实工程目录**跑工具 ⇒ rc == 0（**不许 skip、不许弱化**）。

    🔴 若该条红 ⇒ 是主 Agent 正在并行修 `manu/memory/memory.md` 的 §0 表 / 行数标注，
    **不是**本守卫的 bug（D1~D5 已用 fixture 逐条自证工具行为正确）。
    """
    result = _run_cli(PROJECT_DIR)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[DEFECT]" not in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
