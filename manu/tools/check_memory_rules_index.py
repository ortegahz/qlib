#!/usr/bin/env python
r"""只读检查 memory「铁律速览入口」与铁律正文的**一一对应** ＋ `rules.md` **行数标注**的新鲜度。

用途
    `manu/memory/rules.md` 是铁律**正文**（一级章节 `## 〇、` … `## 十六、` ＋ 附录 A/B）；
    `manu/memory/memory.md` 的 `## 0. 铁律速览` 表是给读者看的**入口速览**，
    必须与正文的一级铁律章节**一一对应**（多一行 = 陈旧；少一行 = 漏章）。
    另外 memory 里多处手写了「`rules.md` **N 行**」的**当前指针**，
    这个 N 是 2026-11-04 的旧值 ⇒ 按行号跳转会**跳空**（违反 `rules.md §十四.93` 第 2 条 ①②）。

用法
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_rules_index.py
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_rules_index.py \
        --project-dir /tmp/pycharm_project_658d3fc4 \
        --json-out /mnt/data/siping/runs/etf_rotation_results/_memidx_check/result.json

判定口径（检查 A —— 章节覆盖）
    1. `rules.md` 的一级铁律章节标签集合：正则 ``^##\s+(〇(?:之[二三])?|[一二三四五六七八九十]+)、``。
       🔴 **附录 A / 附录 B 天然不匹配**（标签是「附录」，不在字符类里）⇒ **天然豁免**，无需额外白名单。
    2. `memory.md` 的 `## 0. 铁律速览` 表的行标签集合：先定位 ``^##\s*0\.\s*铁律速览`` 标题行，
       **其后第一个连续的 `|` 开头表格块**，取每行**第一列**；
       跳过**表头行**（第一列是 `#`）与**分隔行**（形如 `|:--|:--|`）。
    3. 两侧集合必须**完全相等**：
       - 少 ⇒ 缺陷「速览表漏了章节 X」；
       - 多 ⇒ 缺陷「速览表有陈旧行 X（rules.md 已无此章节）」。

判定口径（检查 B —— `rules.md` 行数标注新鲜度）
    🔴 **不许全文 grep**：memory 里还有**历史记录**行写着 `rules.md`（1891 → **1959 行**）——
       那是**历史事实**，「订正」它等于**篡改历史** ⇒ 只认**结构锚点**上的**当前指针**，
       历史行既不算缺陷、也不许被当成当前标注。
    5 个**当前指针**锚点（锚点是**结构特征**，不是行号；每个锚点里的 `**N 行**` 必须
    == `rules.md` 的实测行数 `len(text.splitlines())`）：

    | 锚点 | 文件 | 结构特征 |
    |:--|:--|:--|
    | A1 | `memory.md` | `## 0. 铁律速览` 那一**标题行**里的 `**N 行**` |
    | A2 | `memory.md` | `### 1.1` 区块内的表格行，形如 `rules.md **N 行**` |
    | A3 | `memory.md` | 以 `| **铁律正文** |` **开头**的那一行里的 `**N 行**` |
    | A4 | `memory_compact.md` | 文件**前 5 行**内、形如 `rules.md`（**N 行**） 的那一行里的 `**N 行**` |
    | A5 | `memory_compact.md` | 以 `| 铁律正文 |` **开头**的那一行里的 `**N 行**` |

    - 锚点**找不到** ⇒ 缺陷「锚点缺失/标注丢失」（**不许静默跳过**：静默跳过 = 标注丢失没人知道）。
    - 数字 != 实测行数 ⇒ 缺陷「行数标注陈旧：标注 X，实际 Y」。
    - 🔴 **A2 的正则必须排除历史行**：历史行里 `rules.md` 后面跟的是**反引号 ＋ `（`**
      （如 `` `rules.md`（1891 → **1959 行**） ``），而**当前指针**是
      **`rules.md` + 空白 + `**`** ⇒ 用这个差异当判据（正则要求 `rules.md` 与 `**` 之间
      **只有空白**）⇒ 历史行永远不会被读成当前标注。

豁免（**显式豁免**，不是「碰巧没扫到」）
    🔴 **`manu/memory/.slim_memory.md` 明确不在检查范围**：
       - 它的 `## 0. 铁律速览` 表是**精选子集**（故意只有 12 行，贴给别的模型用的最小包），
         **不是** 1:1 的章节镜像 ⇒ 用检查 A 去卡它属于**误伤**；
       - 它的 `rules.md` 行数写在 `<!-- AUTO:BEGIN -->` 块里，由
         `manu/tools/sync_slim_memory.py` **自动标注**（不许手写）⇒ 用检查 B 去卡它同样是误伤。
       ⇒ 本脚本**只读** `rules.md` / `memory.md` / `memory_compact.md` 三个文件，**不碰**精简档。

为什么需要
    ① 速览表**整段漏掉一节**（实测漏了「十四」＝ ETF 轮动线铁律，本线全部约束的主入口）
       ⇒ 读者以为本线**无铁律** ⇒ 这类缺陷**不会报错**，只会**默默误导**。
    ② 行数标注停在旧值 ⇒ 读者按行号跳转会**跳空**（`rules.md §十四.93` 第 2 条 ①② 要求新鲜度链路）。
    把它做成**只读**闸门，就能在写 memory 的当下把坏数据挡下来，而不是等读者跳空才发现。

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 用法/输入错误（文件读不到等，抛中文异常 ⇒ stderr）。

本脚本**只读**：不修改任何被检查的文件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_658d3fc4"

RULES_REL = "manu/memory/rules.md"
MASTER_REL = "manu/memory/memory.md"
COMPACT_REL = "manu/memory/memory_compact.md"

PREVIEW_CHARS = 80

# ---- 检查 A：章节覆盖 ----
#: `rules.md` 的一级铁律章节标签；🔴 附录 A/B（标签「附录」不在字符类里）⇒ **天然豁免**
RULES_CHAPTER_RE = re.compile(r"^##\s+(〇(?:之[二三])?|[一二三四五六七八九十]+)、")
#: `memory.md` 的速览表标题行
MASTER_SECTION0_RE = re.compile(r"^##\s*0\.\s*铁律速览")
#: 表格分隔行的单元格（`:--` / `:--:` / `--` / `---`，两侧可各带一个对齐冒号）
SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")

# ---- 检查 B：行数标注锚点 ----
#: 通用行数标注形态 `**N 行**`
LINE_COUNT_RE = re.compile(r"\*\*(\d+)\s*行\*\*")
#: A2：`### 1.1` 区块内的当前指针表格行 `rules.md **N 行**`
#: 🔴 `rules\.md` 与 `**` 之间**只允许空白** ⇒ 历史行（`rules.md` 后跟反引号＋`（`）永远不匹配
A2_ROW_RE = re.compile(r"rules\.md[ \t]+\*\*(\d+)\s*行\*\*")
#: A4：速览卡**前 5 行**内的当前指针 `rules.md`（**N 行**）
A4_LINE_RE = re.compile(r"rules\.md`?[ \t]*（\*\*(\d+)\s*行\*\*）")
#: A2 的区块锚点：`### 1.1` 标题行
MASTER_S11_RE = re.compile(r"^###\s*1\.1(?:\s|$)")

REASON_MISSING_CHAPTER = "速览表漏了章节 {}"
REASON_STALE_ROW = "速览表有陈旧行 {}（rules.md 已无此章节）"
REASON_ANCHOR_MISSING = "锚点缺失/标注丢失"
REASON_ANCHOR_STALE = "行数标注陈旧：标注 {}，实际 {}"
REASON_NO_TABLE = "速览表缺失：找不到 `## 0. 铁律速览` 的表格块"


class InputError(Exception):
    """用法/输入错误（文件缺失、读不了、输出路径非法）⇒ 退出码 2。"""


@dataclass
class Defect:
    """一条速览表 / 行数标注缺陷。"""

    file: str
    line_no: int
    reason: str
    preview: str
    anchor: str = ""

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line_no,
            "anchor": self.anchor,
            "reason": self.reason,
            "preview": self.preview,
        }


@dataclass
class AnchorHit:
    """一个当前指针锚点的命中结果（行号 + 标注值）。"""

    anchor: str
    line_no: int
    value: int
    preview: str


@dataclass
class FileReport:
    """单个文件的检查结果。"""

    display_path: str
    role: str
    counters: dict = field(default_factory=dict)
    defects: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "file": self.display_path,
            "role": self.role,
            "counters": dict(self.counters),
            "defect_count": len(self.defects),
            "defects": [defect.as_dict() for defect in self.defects],
        }


@dataclass
class CheckResult:
    """一次完整检查的结果（按文件分组）。"""

    reports: list = field(default_factory=list)

    @property
    def defects(self) -> list:
        collected = []
        for report in self.reports:
            collected.extend(report.defects)
        return collected


def make_preview(raw: str) -> str:
    """缺陷行预览：行首 80 字符。"""
    return raw[:PREVIEW_CHARS].rstrip()


def extract_rules_chapters(rules_text: str) -> list:
    """`rules.md` 的一级铁律章节 ⇒ [(行号, 标签)]；附录 A/B 不匹配 ⇒ 天然豁免。"""
    chapters = []
    for line_no, raw in enumerate(rules_text.splitlines(), start=1):
        matched = RULES_CHAPTER_RE.match(raw)
        if matched:
            chapters.append((line_no, matched.group(1)))
    return chapters


def find_section0_table(master_lines: list) -> tuple:
    """定位 `## 0. 铁律速览` 标题行 + 其后第一个连续的 `|` 表格块。

    返回 (标题行号, [(行号, 原始行)])；标题行号 = 0 表示没找到标题；
    表格块为空表示标题后面没有表格（⇒ 「速览表缺失」缺陷）。
    """
    heading_line = 0
    start = -1
    for index, raw in enumerate(master_lines):
        if MASTER_SECTION0_RE.match(raw):
            heading_line = index + 1
            start = index + 1
            break
    if start < 0:
        return 0, []

    rows = []
    index = start
    while index < len(master_lines):
        raw = master_lines[index]
        if raw.strip().startswith("|"):
            rows.append((index + 1, raw))
            index += 1
            continue
        if rows:
            break  # 表格块结束
        if raw.startswith("#"):
            break  # 撞到下一个标题 ⇒ §0 段里没有表格
        index += 1
    return heading_line, rows


def extract_table_labels(rows: list) -> list:
    """取表格块每行的**第一列** ⇒ [(行号, 标签, 行预览)]；跳过表头行与分隔行。"""
    labels = []
    for line_no, raw in rows:
        stripped = raw.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        if first == "#":
            continue  # 表头行
        if all(SEPARATOR_CELL_RE.match(cell) for cell in cells):
            continue  # 分隔行
        if not first:
            continue
        labels.append((line_no, first, make_preview(raw)))
    return labels


def _anchor_a1(master_lines: list) -> tuple:
    """A1：`## 0. 铁律速览` **标题行**里的 `**N 行**`。"""
    for line_no, raw in enumerate(master_lines, start=1):
        if MASTER_SECTION0_RE.match(raw):
            hits = [
                AnchorHit("A1", line_no, int(value), make_preview(raw))
                for value in LINE_COUNT_RE.findall(raw)
            ]
            return hits, line_no
    return [], 0


def _anchor_a2(master_lines: list) -> tuple:
    """A2：`### 1.1` 区块内的表格行，形如 `rules.md **N 行**`。

    🔴 正则要求 `rules.md` 与 `**` 之间**只有空白** ⇒ 历史行
    （`` `rules.md`（1891 → **1959 行**） ``）里 `rules.md` 后面跟的是**反引号 ＋ `（`**
    ⇒ 永远不匹配 ⇒ **历史事实不会被当成当前标注**（更不会被「订正」＝篡改）。
    """
    start = -1
    for index, raw in enumerate(master_lines):
        if MASTER_S11_RE.match(raw):
            start = index
            break
    if start < 0:
        return [], 0

    end = start + 1
    while end < len(master_lines) and not master_lines[end].startswith("#"):
        end += 1

    hits = []
    for index in range(start, end):
        raw = master_lines[index]
        if not raw.strip().startswith("|"):
            continue  # 只认**表格行**
        for value in A2_ROW_RE.findall(raw):
            hits.append(AnchorHit("A2", index + 1, int(value), make_preview(raw)))
    return hits, start + 1


def _anchor_a3(master_lines: list) -> tuple:
    """A3：以 `| **铁律正文** |` **开头**的那一行里的 `**N 行**`。"""
    hits = []
    for line_no, raw in enumerate(master_lines, start=1):
        if raw.strip().startswith("| **铁律正文** |"):
            for value in LINE_COUNT_RE.findall(raw):
                hits.append(AnchorHit("A3", line_no, int(value), make_preview(raw)))
    return hits, 0


def _anchor_a4(compact_lines: list) -> tuple:
    """A4：速览卡**前 5 行**内、形如 `rules.md`（**N 行**） 的那一行里的 `**N 行**`。"""
    hits = []
    for line_no, raw in enumerate(compact_lines[:5], start=1):
        for value in A4_LINE_RE.findall(raw):
            hits.append(AnchorHit("A4", line_no, int(value), make_preview(raw)))
    return hits, 1


def _anchor_a5(compact_lines: list) -> tuple:
    """A5：以 `| 铁律正文 |` **开头**的那一行里的 `**N 行**`。"""
    hits = []
    for line_no, raw in enumerate(compact_lines, start=1):
        if raw.strip().startswith("| 铁律正文 |"):
            for value in LINE_COUNT_RE.findall(raw):
                hits.append(AnchorHit("A5", line_no, int(value), make_preview(raw)))
    return hits, 0


def check_chapter_coverage(master_text: str, rules_text: str) -> tuple:
    """检查 A：速览表章节标签集合 vs `rules.md` 一级章节标签集合。

    返回 (缺陷列表, 速览表标签列表, 表格行数)。
    """
    rules_chapters = extract_rules_chapters(rules_text)
    rules_labels = {label for _, label in rules_chapters}

    master_lines = master_text.splitlines()
    heading_line, rows = find_section0_table(master_lines)
    heading_preview = make_preview(master_lines[heading_line - 1]) if heading_line else ""
    if not rows:
        defect = Defect(MASTER_REL, heading_line or 1, REASON_NO_TABLE, heading_preview, anchor="A0")
        return [defect], [], 0

    labels = extract_table_labels(rows)
    memory_labels = {label for _, label, _ in labels}

    defects = []
    for _, label in rules_chapters:
        if label not in memory_labels:
            defects.append(
                Defect(
                    MASTER_REL,
                    heading_line or 1,
                    REASON_MISSING_CHAPTER.format(label),
                    heading_preview,
                    anchor="A0",
                )
            )
    for line_no, label, preview in labels:
        if label not in rules_labels:
            defects.append(
                Defect(
                    MASTER_REL,
                    line_no,
                    REASON_STALE_ROW.format(label),
                    preview,
                    anchor="A0",
                )
            )
    return defects, labels, len(rows)


def check_line_count_anchors(master_text: str, compact_text: str, rules_text: str) -> tuple:
    """检查 B：5 个**当前指针**锚点的 `**N 行**` 是否 == `rules.md` 实测行数。

    返回 (缺陷列表, 命中字典 {锚点: [AnchorHit, ...]})。
    """
    actual_lines = len(rules_text.splitlines())
    master_lines = master_text.splitlines()
    compact_lines = compact_text.splitlines()

    anchor_specs = (
        ("A1", MASTER_REL, _anchor_a1(master_lines)),
        ("A2", MASTER_REL, _anchor_a2(master_lines)),
        ("A3", MASTER_REL, _anchor_a3(master_lines)),
        ("A4", COMPACT_REL, _anchor_a4(compact_lines)),
        ("A5", COMPACT_REL, _anchor_a5(compact_lines)),
    )

    defects = []
    hits_by_anchor = {}
    for anchor_id, display_path, (hits, region_line) in anchor_specs:
        hits_by_anchor[anchor_id] = hits
        if not hits:
            preview = f"（未找到锚点 {anchor_id}：{display_path} 里没有当前指针标注）"
            defects.append(
                Defect(display_path, region_line or 1, REASON_ANCHOR_MISSING, preview, anchor=anchor_id)
            )
            continue
        for hit in hits:
            if hit.value != actual_lines:
                defects.append(
                    Defect(
                        display_path,
                        hit.line_no,
                        REASON_ANCHOR_STALE.format(hit.value, actual_lines),
                        hit.preview,
                        anchor=anchor_id,
                    )
                )
    return defects, hits_by_anchor


def run_checks(master_text: str, compact_text: str, rules_text: str) -> CheckResult:
    """跑全部检查，返回按文件分组的结果。"""
    rules_chapters = extract_rules_chapters(rules_text)
    actual_lines = len(rules_text.splitlines())

    chapter_defects, labels, table_rows = check_chapter_coverage(master_text, rules_text)
    anchor_defects, hits_by_anchor = check_line_count_anchors(master_text, compact_text, rules_text)

    master_defects = [d for d in chapter_defects + anchor_defects if d.file == MASTER_REL]
    compact_defects = [d for d in chapter_defects + anchor_defects if d.file == COMPACT_REL]

    master_anchors = [aid for aid in ("A1", "A2", "A3") if hits_by_anchor.get(aid)]
    compact_anchors = [aid for aid in ("A4", "A5") if hits_by_anchor.get(aid)]

    reports = [
        FileReport(
            RULES_REL,
            "铁律正文",
            {
                "一级铁律章节": f"{len(rules_chapters)} 个",
                "实测行数": f"{actual_lines} 行",
            },
        ),
        FileReport(
            MASTER_REL,
            "主档",
            {
                "速览表标签": f"{len(labels)} 个",
                "速览表表格行": f"{table_rows} 行",
                "当前指针锚点": f"{len(master_anchors)} 个（{'、'.join(master_anchors) or '无'}）",
            },
            master_defects,
        ),
        FileReport(
            COMPACT_REL,
            "速览卡",
            {
                "当前指针锚点": f"{len(compact_anchors)} 个（{'、'.join(compact_anchors) or '无'}）",
            },
            compact_defects,
        ),
    ]
    return CheckResult(reports)


def analyze(master_text: str, compact_text: str, rules_text: str) -> list:
    """总入口：检查 A ＋ 检查 B ⇒ 缺陷列表（每个 `Defect` 有 `as_dict()`）。"""
    return run_checks(master_text, compact_text, rules_text).defects


def resolve_path(rel_path: str, project_dir: str) -> str:
    """相对路径**严格**按 `--project-dir` 解析；绝对路径直接用。

    🔴 刻意**不**回退到当前工作目录：`--project-dir` 指错时必须**报出来**（退出码 2），
    回退会把「工程目录没指对」伪装成「检查通过」。
    """
    if os.path.isabs(rel_path):
        candidates = [rel_path]
    else:
        candidates = [os.path.join(project_dir, rel_path)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise InputError(f"找不到待检查文件：{rel_path}（已尝试：{'、'.join(candidates)}）")


def read_text(path: str) -> str:
    """读文件；读不到 ⇒ 中文异常（⇒ 退出码 2）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError(f"读不到待检查文件：{path}（{exc}）") from exc


def render_defect(defect: Defect) -> str:
    """缺陷行文本：`[DEFECT] <文件>:<行号> <锚点>｜<原因>｜<行首 80 字符>`。"""
    anchor_part = f"锚点 {defect.anchor}｜" if defect.anchor else ""
    return f"[DEFECT] {defect.file}:{defect.line_no} {anchor_part}{defect.reason}｜{defect.preview}"


def render_file_summary(report: FileReport) -> str:
    """单文件摘要：`[FILE] <文件>｜<计数项>｜缺陷 N 个`。"""
    counters = "｜".join(f"{key} {value}" for key, value in report.counters.items())
    return f"[FILE] {report.display_path}｜{counters}｜缺陷 {len(report.defects)} 个"


def build_payload(result: CheckResult, project_dir: str) -> dict:
    """结构化结果（供 `--json-out`）。"""
    defects = result.defects
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "exempted": [
            "manu/memory/.slim_memory.md（速览表是精选子集 ＋ 行数由 sync_slim_memory.py 自动标注）"
        ],
        "files": [report.as_dict() for report in result.reports],
        "summary": {
            "file_count": len(result.reports),
            "defect_count": len(defects),
        },
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读检查 memory 主档「铁律速览」表与 rules.md 一级章节是否一一对应，"
            "以及 rules.md 行数标注（5 个当前指针锚点）是否新鲜。"
        )
    )
    parser.add_argument(
        "--project-dir",
        default=DEFAULT_PROJECT_DIR,
        help=f"工程根目录，用于定位 manu/memory/ 下的三个文件（默认 {DEFAULT_PROJECT_DIR}）",
    )
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        rules_text = read_text(resolve_path(RULES_REL, args.project_dir))
        master_text = read_text(resolve_path(MASTER_REL, args.project_dir))
        compact_text = read_text(resolve_path(COMPACT_REL, args.project_dir))

        result = run_checks(master_text, compact_text, rules_text)

        for report in result.reports:
            for defect in report.defects:
                print(render_defect(defect))
        for report in result.reports:
            print(render_file_summary(report))

        defect_count = len(result.defects)
        print(f"合计：文件 {len(result.reports)} 个｜缺陷 {defect_count} 个")

        if args.json_out:
            payload = build_payload(result, args.project_dir)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
                with open(args.json_out, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
            except OSError as exc:
                raise InputError(f"写出 JSON 结果失败：{args.json_out}（{exc}）") from exc
    except InputError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    return 1 if defect_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
