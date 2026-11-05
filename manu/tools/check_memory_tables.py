#!/usr/bin/env python
r"""只读检查 memory 主档里 markdown 表格的**结构**完整性。

用途
    `manu/memory/memory.md` / `manu/memory/memory_compact.md` 是大量 markdown 表格的主档。
    表格行里如果出现**未转义**的 `|`（例如正文里的 `` `ls | true` ``），markdown 会把它当成
    **单元格分隔符** ⇒ 那一行被切成比表头更多的单元格 ⇒ **表格渲染错位**（列整体错列）。
    本脚本按**结构**逐表块统计未转义竖线数，把「列数与表头不一致」的行报成缺陷。

用法
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_tables.py
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_tables.py \
        --project-dir /tmp/pycharm_project_658d3fc4 \
        --files manu/memory/memory.md --files manu/memory/memory_compact.md \
        --json-out /mnt/data/siping/runs/_memtbl_check/result.json
    （`--files` 可重复传入；相对路径按 `--project-dir` 解析，找不到再按当前工作目录解析。）

判定口径（关键）
    1. **未转义竖线**：逐字符扫描，遇 `\` 跳过下一个字符。
       ⇒ `\|` 是转义竖线（不算）；`\\|` 是「转义的反斜杠 + 分隔符」⇒ **算未转义竖线**。
    2. 🔴 **转义竖线 `\|` 不算缺陷**：正文里引述表格表头（例如「代码\|当前权重\|机械目标」）
       是**故意**写成 `\|` 的，渲染出来就是字面竖线，属正常内容。
       所以判定必须基于**未转义**竖线数，**不能用 `line.count("|")`**。
    3. **表块** = 连续的表格行（行首为 `|`）；被非表格行（空行、`---`、标题行、正文行）隔断，
       块与块之间互不比较（不同表本来就列数不同 ⇒ 列数不同**不是**缺陷）。
       围栏代码块（``` / ~~~）内的行不算表格行。
    4. **列数** = 该行未转义竖线数 − 1。
       - 块内第 2 行是分隔行（形如 `|:--|:--|`）⇒ 列数取**分隔行**的列数（分隔行是权威标尺）；
       - 块内**没有**分隔行 ⇒ 列数取块内各行列数的**众数**（并列时取第一行的列数）。
         为什么用众数而不是「第一行的列数」：第一行本身可能就是那条含未转义竖线的坏行
         （实测 `manu/memory/memory.md` 第 228 行即是），若拿第一行当标尺，
         坏行不会被报出来，反而把同一块里正常的行全报成缺陷；众数才能定位**唯一的少数派坏行**。
    5. 块内**每一行**的列数必须 == 表头列数，不等 ⇒ 报 1 条缺陷。
    6. **分隔行单独校验**：列数须与表头一致，且每个单元格形如 `:--` / `:--:` / `--` / `---`
       （允许两侧各带一个对齐冒号）。

为什么需要
    表格错位**不会报错**，只会渲染错：肉眼看过去「还是一张表」，但某一列的内容会被读成另一列。
    memory 主档是事实层主索引（人和 LLM 都在读），一旦列错位，读者会把
    「机械目标」读成「当前权重」这类**真值误读**。把它做成**只读**的结构检查，
    就能在写 memory 的当下把坏行挡下来，而不是等渲染后靠肉眼抓。
    本脚本**只读**：不修改任何被检查的文件。

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 用法/输入错误（文件读不到、路径非法等，抛中文异常）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_658d3fc4"
DEFAULT_MEMORY_FILES = ("manu/memory/memory.md", "manu/memory/memory_compact.md")

# 分隔行单元格：规范形式（`:--` / `:--:` / `--` / `---`，两侧可各带一个对齐冒号）
SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
# 分隔行「像不像」分隔行（只由 `-` 与 `:` 组成）——用于识别分隔行，再单独做严格校验
SEPARATOR_LIKE_CELL_RE = re.compile(r"^:?-+:?$")
FENCE_RE = re.compile(r"^(```|~~~)")

PREVIEW_CHARS = 80
REASON_COLUMN_MISMATCH = "列数与表头不一致"
REASON_SEPARATOR_COLUMNS = "分隔行列数与表头不一致"
REASON_SEPARATOR_FORMAT = "分隔行单元格格式非法"


class InputError(Exception):
    """用法/输入错误（文件缺失、读不了、输出路径非法）⇒ 退出码 2。"""


@dataclass
class Defect:
    """一条表格结构缺陷。"""

    line_no: int
    expected_columns: int
    actual_columns: int
    reason: str
    preview: str

    def as_dict(self, display_path: str) -> dict:
        return {
            "file": display_path,
            "line": self.line_no,
            "expected_columns": self.expected_columns,
            "actual_columns": self.actual_columns,
            "reason": self.reason,
            "preview": self.preview,
        }


@dataclass
class Block:
    """一个表块（连续的表格行）。"""

    start_line: int
    end_line: int
    row_count: int
    columns: int
    has_separator: bool

    def as_dict(self) -> dict:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "row_count": self.row_count,
            "columns": self.columns,
            "has_separator": self.has_separator,
        }


@dataclass
class FileReport:
    """单个文件的检查结果。"""

    display_path: str
    path: str
    blocks: list[Block] = field(default_factory=list)
    defects: list[Defect] = field(default_factory=list)
    table_rows: int = 0

    def as_dict(self) -> dict:
        return {
            "file": self.display_path,
            "path": self.path,
            "table_rows": self.table_rows,
            "block_count": len(self.blocks),
            "defect_count": len(self.defects),
            "blocks": [block.as_dict() for block in self.blocks],
            "defects": [defect.as_dict(self.display_path) for defect in self.defects],
        }


def unescaped_pipe_positions(text: str) -> list[int]:
    """返回所有**未转义**竖线的下标。

    逐字符扫描：遇 `\\` 则跳过下一个字符（该字符被反斜杠转义，不参与判定）。
    ⇒ `\\|` 里的竖线是未转义的（第一个反斜杠转义的是第二个反斜杠）。
    """
    positions: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "|":
            positions.append(index)
        index += 1
    return positions


def count_unescaped_pipes(text: str) -> int:
    """统计未转义竖线数（不是 `text.count("|")`）。"""
    return len(unescaped_pipe_positions(text))


def row_cells(text: str) -> list[str]:
    """按未转义竖线切出单元格；行首/行尾的竖线只作边框，不算单元格。"""
    stripped = text.strip()
    positions = unescaped_pipe_positions(stripped)
    cells: list[str] = []
    cursor = 0
    for position in positions:
        cells.append(stripped[cursor:position])
        cursor = position + 1
    cells.append(stripped[cursor:])
    if positions and positions[0] == 0:
        cells = cells[1:]
    if positions and positions[-1] == len(stripped) - 1:
        cells = cells[:-1]
    return cells


def is_table_row(text: str) -> bool:
    """判断一行是否为表格行（行首 `|` 且至少有一个未转义竖线）。"""
    stripped = text.strip()
    if not stripped.startswith("|"):
        return False
    return count_unescaped_pipes(stripped) >= 1


def collect_table_rows(lines: list[str]) -> list[tuple[int, str]]:
    """收集所有表格行（跳过围栏代码块内部），返回 [(行号, 原始行)]。"""
    rows: list[tuple[int, str]] = []
    in_fence = False
    for line_no, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if is_table_row(raw):
            rows.append((line_no, raw))
    return rows


def group_blocks(rows: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """把表格行按「连续行」分成表块（被非表格行隔断 ⇒ 断开）。"""
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for row in rows:
        if current and row[0] == current[-1][0] + 1:
            current.append(row)
            continue
        if current:
            blocks.append(current)
        current = [row]
    if current:
        blocks.append(current)
    return blocks


def majority(values: list[int]) -> int:
    """取众数；并列时取第一行的取值（与「第一行是标尺」的口径一致）。"""
    if not values:
        return 0
    counts = Counter(values)
    best = max(counts.values())
    tied = [value for value, count in counts.items() if count == best]
    if len(tied) == 1:
        return tied[0]
    if values[0] in tied:
        return values[0]
    return min(tied)


def make_preview(raw: str) -> str:
    """缺陷行预览：行首 80 字符。"""
    return raw[:PREVIEW_CHARS].rstrip()


def analyze_block(rows: list[tuple[int, str]]) -> tuple[Block, list[Defect]]:
    """分析一个表块：定列数、逐行校验列数、单独校验分隔行。"""
    defects: list[Defect] = []
    counts = [count_unescaped_pipes(raw.strip()) - 1 for _, raw in rows]
    header_columns = counts[0]
    columns = header_columns
    has_separator = False

    if len(rows) >= 2:
        separator_cells = [cell.strip() for cell in row_cells(rows[1][1])]
        if separator_cells and all(SEPARATOR_LIKE_CELL_RE.match(cell) for cell in separator_cells):
            has_separator = True
            separator_columns = counts[1]
            if separator_columns == header_columns:
                columns = separator_columns
            else:
                # 表头与分隔行列数打架 ⇒ 以表头为标尺，并把分隔行单独报出来
                columns = header_columns
                defects.append(
                    Defect(
                        rows[1][0],
                        header_columns,
                        separator_columns,
                        REASON_SEPARATOR_COLUMNS,
                        make_preview(rows[1][1]),
                    )
                )
            if any(not SEPARATOR_CELL_RE.match(cell) for cell in separator_cells):
                defects.append(
                    Defect(
                        rows[1][0],
                        columns,
                        separator_columns,
                        REASON_SEPARATOR_FORMAT,
                        make_preview(rows[1][1]),
                    )
                )

    if not has_separator:
        # 没有分隔行 ⇒ 用块内众数当标尺（第一行可能就是那条坏行）
        columns = majority(counts)

    for position, (row, count) in enumerate(zip(rows, counts)):
        if has_separator and position == 1:
            continue  # 分隔行已单独校验
        if count != columns:
            defects.append(
                Defect(row[0], columns, count, REASON_COLUMN_MISMATCH, make_preview(row[1]))
            )

    block = Block(
        start_line=rows[0][0],
        end_line=rows[-1][0],
        row_count=len(rows),
        columns=columns,
        has_separator=has_separator,
    )
    return block, defects


def analyze_file(path: str, display_path: str) -> FileReport:
    """读取并分析单个 markdown 文件（只读）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError as exc:
        raise InputError(f"文件不存在：{display_path}（解析到 {path}）") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"文件不是合法 UTF-8 文本：{display_path}（{exc}）") from exc
    except OSError as exc:
        raise InputError(f"读取文件失败：{display_path}（{exc}）") from exc

    report = FileReport(display_path=display_path, path=path)
    for block_rows in group_blocks(collect_table_rows(text.split("\n"))):
        block, defects = analyze_block(block_rows)
        report.blocks.append(block)
        report.defects.extend(defects)
        report.table_rows += block.row_count
    return report


def resolve_file(raw_path: str, project_dir: str) -> str:
    """把 `--files` 的取值解析成实际路径：绝对路径直接用；相对路径先按工程目录再按当前目录找。"""
    if os.path.isabs(raw_path):
        candidates = [raw_path]
    else:
        candidates = [os.path.join(project_dir, raw_path), os.path.abspath(raw_path)]
    unique_candidates = list(dict.fromkeys(candidates))
    for candidate in unique_candidates:
        if os.path.isfile(candidate):
            return candidate
    raise InputError(f"找不到待检查文件：{raw_path}（已尝试：{'、'.join(unique_candidates)}）")


def render_defect(report: FileReport, defect: Defect) -> str:
    """缺陷行文本：`[DEFECT] <文件>:<行号> 期望列数=X 实际列数=Y｜<行首 80 字符>`。"""
    head = (
        f"[DEFECT] {report.display_path}:{defect.line_no} "
        f"期望列数={defect.expected_columns} 实际列数={defect.actual_columns}"
    )
    line = f"{head}｜{defect.preview}"
    if defect.reason != REASON_COLUMN_MISMATCH:
        line += f"（原因：{defect.reason}）"
    return line


def render_file_summary(report: FileReport) -> str:
    """单文件摘要：表块数 / 表格行数 / 缺陷数 / 各表块列数分布。"""
    columns = "、".join(str(block.columns) for block in report.blocks)
    return (
        f"[FILE] {report.display_path}｜表块 {len(report.blocks)} 个｜表格行 {report.table_rows} 行｜"
        f"缺陷 {len(report.defects)} 个｜各表块列数 [{columns}]"
    )


def build_payload(reports: list[FileReport], project_dir: str) -> dict:
    """结构化结果（供 --json-out）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "files": [report.as_dict() for report in reports],
        "summary": {
            "file_count": len(reports),
            "block_count": sum(len(report.blocks) for report in reports),
            "table_row_count": sum(report.table_rows for report in reports),
            "defect_count": sum(len(report.defects) for report in reports),
        },
    }


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读检查 memory 主档里 markdown 表格的结构完整性"
            "（未转义竖线导致的列数错位）。转义竖线 \\| 不算缺陷。"
        )
    )
    parser.add_argument(
        "--files",
        action="append",
        default=None,
        help=(
            "待检查的 markdown 文件，可重复传入"
            f"（默认：{'、'.join(DEFAULT_MEMORY_FILES)}；相对路径按 --project-dir 解析）"
        ),
    )
    parser.add_argument(
        "--project-dir",
        default=DEFAULT_PROJECT_DIR,
        help=f"工程根目录，用于定位默认的 memory 文件（默认 {DEFAULT_PROJECT_DIR}）",
    )
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    targets = args.files if args.files else list(DEFAULT_MEMORY_FILES)
    try:
        reports = []
        for target in targets:
            path = resolve_file(target, args.project_dir)
            reports.append(analyze_file(path, target))

        for report in reports:
            for defect in report.defects:
                print(render_defect(report, defect))
        for report in reports:
            print(render_file_summary(report))

        block_count = sum(len(report.blocks) for report in reports)
        row_count = sum(report.table_rows for report in reports)
        defect_count = sum(len(report.defects) for report in reports)
        print(
            f"合计：文件 {len(reports)} 个｜表块 {block_count} 个｜"
            f"表格行 {row_count} 行｜缺陷 {defect_count} 个"
        )

        if args.json_out:
            payload = build_payload(reports, args.project_dir)
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
