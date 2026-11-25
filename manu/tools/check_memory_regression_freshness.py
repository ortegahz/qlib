#!/usr/bin/env python3
"""只读检查「回归证据是否覆盖**当前** memory 四层」——把 `rules.md §十四.128` 第 6 条机器化。

为什么需要（`rules.md §十四.128` 第 6 条）
    「**回归绿灯**」只覆盖**跑那一刻**的文件状态 ⇒ 改完 memory 四层之后，
    先前的绿灯**不再作数**（实测：位次双口径改写发生在回归之后 ⇒ 守卫测试 M2 的
    断言字面串当场过期，要到下一轮复跑才暴露）。
    这条铁律此前**只有人守、没有机器闸门** ⇒ 本工具补上。

判定口径
    * **证据来源**：`<回归证据根>` 下的 `pytest.log`（规范口径回归日志），
      证据根缺省 = `ETF_ROTATION_RESULTS_ROOT`（= `daily_config.ETF_ROTATION_RESULTS_ROOT` 同口径）。
    * **证据选取**：按 `pytest.log` 的 **mtime 取最新**（不看目录名排序，避免 `_r9` / `_r10` 之类
      命名漂移导致选错证据）。
    * **检查 A（内容口径，主）**：日志里若带 `[MEMORY-MD5]` 证据头 ⇒ 逐个比对
      四层 memory 的**当前 md5** 与**当时记录的 md5** ⇒ 不一致 ＝ 「改了 memory 未重跑回归」。
      🔴 内容口径**优先于**时间口径：文件被 touch（mtime 变新）但内容没变 ⇒ **不报红**。
    * **检查 A'（时间口径，回退）**：日志**没有** `[MEMORY-MD5]` 头（旧日志）⇒ 退化成
      「四层任一 mtime **新于** 日志 mtime ⇒ 报红」。
    * **检查 B（回归红）**：最新日志的 pytest 摘要里 `failed` / `error` 计数 > 0 ⇒ 报红
      （**红灯不能当证据**）。
    * 🔴 **不适用 ≠ 不合格**（同 `§十四.117`）：证据根不存在 / 根下没有 `pytest.log` /
      日志摘要不可解析 ⇒ 退出码 **2「不适用」**，**不报缺陷**（host 没有回归证据属正常）。
    * 🔴 **本工具只读**：不写、不改任何被检查的文件（测试里有 md5 前后对照）。

用法
    python3 manu/tools/check_memory_regression_freshness.py --project-dir .
    python3 manu/tools/check_memory_regression_freshness.py --project-dir . \
        --regression-root /mnt/data/siping/runs/etf_rotation_results
    python3 manu/tools/check_memory_regression_freshness.py --emit-header --project-dir .

    # 跑回归时把证据头一起写进日志（本工具自带发射器 ⇒ 头/校验器格式不会分叉）：
    { python3 manu/tools/check_memory_regression_freshness.py --emit-header --project-dir . ; \
      /home/huangzhe/anaconda3/envs/ql/bin/python -m pytest manu/etf_rotation/tests \
        manu/etf_rotation/daily/tests manu/etf_rotation/v2/tests -q ; } \
      > <证据根>/_regression_<stamp>/pytest.log 2>&1

退出码
    0 = 无缺陷（证据新鲜且绿）；1 = 有缺陷；2 = 不适用/输入错误（证据不足、文件读不到）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_3b68a403"

#: 回归证据根（单一事实源 = `daily_config.ETF_ROTATION_RESULTS_ROOT`，同一环境变量）
_RESULTS_ROOT = os.environ.get(
    "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
)
DEFAULT_REGRESSION_ROOT = _RESULTS_ROOT

#: memory 四层（相对工程根）——「铁律四层同步」的同一组文件
MEMORY_FILES = (
    "manu/memory/rules.md",
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/.slim_memory.md",
)

#: 证据头标签（**发射器与校验器同源** ⇒ 格式不会分叉）
HEADER_TAG = "[MEMORY-MD5]"

#: 证据头行：`[MEMORY-MD5] <路径>=<32 hex> <路径>=<32 hex> ...`
HEADER_LINE = re.compile(r"^\s*" + re.escape(HEADER_TAG) + r"\s+(?P<fields>.+?)\s*$", re.MULTILINE)
HEADER_FIELD = re.compile(r"^(?P<path>\S+?)=(?P<digest>[0-9a-fA-F]{32})$")

#: pytest 摘要计数（取**最后一个**摘要行）
_SUMMARY_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "error": re.compile(r"(\d+)\s+error"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
}


class InputError(Exception):
    """输入/用法错误 ⇒ 退出码 2。"""


@dataclass
class Defect:
    """一条缺陷（字段与 `check_memory_stage_count.py` 同构）。"""

    file: str
    line_no: int
    check: str
    reason: str
    preview: str

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "line_no": self.line_no,
            "check": self.check,
            "reason": self.reason,
            "preview": self.preview,
        }


@dataclass
class Summary:
    """pytest 摘要计数。"""

    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0
    raw: str = ""

    @property
    def is_green(self) -> bool:
        return self.failed == 0 and self.error == 0 and self.passed > 0


@dataclass
class Evidence:
    """一份回归证据（一份 `pytest.log`）。"""

    path: str
    mtime: float
    header: dict = field(default_factory=dict)
    summary: Summary | None = None

    @property
    def has_header(self) -> bool:
        return bool(self.header)


@dataclass
class Analysis:
    """总入口的返回物。"""

    defects: list = field(default_factory=list)
    evidence: Evidence | None = None
    applicable: bool = True
    note: str = ""


def read_text(path: str) -> str:
    """读文件；读不到 ⇒ 中文异常（⇒ 退出码 2）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc


def file_digest(path: str) -> str:
    """文件 md5（十六进制）；读不到 ⇒ 中文异常（⇒ 退出码 2）。"""
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc
    return hasher.hexdigest()


def build_header(project_dir: str) -> str:
    """生成证据头行（四层 memory 的当前 md5）——供写回归日志时嵌入。"""
    parts = []
    for rel in MEMORY_FILES:
        parts.append("%s=%s" % (rel, file_digest(os.path.join(project_dir, rel))))
    return "%s %s" % (HEADER_TAG, " ".join(parts))


def parse_header(text: str) -> dict:
    """从日志文本里解析 `[MEMORY-MD5]` 证据头 ⇒ {相对路径: md5}；没有 ⇒ 空 dict。"""
    matched = HEADER_LINE.search(text)
    if not matched:
        return {}
    header = {}
    for token in matched.group("fields").split():
        field_match = HEADER_FIELD.match(token)
        if field_match:
            header[field_match.group("path")] = field_match.group("digest").lower()
    return header


def parse_summary(text: str) -> Summary | None:
    """解析 pytest 摘要（取**最后**一条含计数的行）⇒ 解析不出来 ⇒ `None`。"""
    best = None
    for line in text.splitlines():
        if " passed" not in line and " failed" not in line and " error" not in line:
            continue
        counts = {}
        for key, pattern in _SUMMARY_PATTERNS.items():
            found = pattern.search(line)
            counts[key] = int(found.group(1)) if found else 0
        if counts["passed"] or counts["failed"] or counts["error"]:
            best = Summary(raw=line.strip(), **counts)
    return best


def find_evidence(regression_root: str, regression_log: str | None = None) -> Evidence | None:
    """找**最新**一份回归证据（按 `pytest.log` 的 mtime）⇒ 没有 ⇒ `None`。"""
    if regression_log:
        if not os.path.isfile(regression_log):
            raise InputError("指定的回归日志不存在：%s" % regression_log)
        candidates = [regression_log]
    else:
        if not os.path.isdir(regression_root):
            return None
        candidates = []
        for dirpath, dirnames, filenames in os.walk(regression_root):
            # 🔴 只扫浅层（回归证据根下 2 层）⇒ 不许扫进别人链的大目录
            depth = dirpath[len(regression_root):].count(os.sep)
            if depth >= 2:
                dirnames[:] = []
            if "pytest.log" in filenames:
                candidates.append(os.path.join(dirpath, "pytest.log"))
        if not candidates:
            return None

    newest = max(candidates, key=lambda item: os.path.getmtime(item))
    text = read_text(newest)
    return Evidence(
        path=newest,
        mtime=os.path.getmtime(newest),
        header=parse_header(text),
        summary=parse_summary(text),
    )


def _preview(rel: str, project_dir: str) -> str:
    """给缺陷行一点可读上下文（首行前 80 字符）。"""
    try:
        with open(os.path.join(project_dir, rel), "r", encoding="utf-8") as handle:
            first = handle.readline().strip()[:80]
        return first
    except OSError:
        return ""


def analyze(project_dir: str, regression_root: str | None = None, regression_log: str | None = None) -> Analysis:
    """总入口 ⇒ 缺陷列表 + 证据事实。"""
    root = regression_root or DEFAULT_REGRESSION_ROOT
    evidence = find_evidence(root, regression_log)
    if evidence is None:
        return Analysis(
            defects=[],
            evidence=None,
            applicable=False,
            note="回归证据根 %s 下没有 pytest.log ⇒ 不适用（本机/host 无回归证据属正常）" % root,
        )

    defects = []
    current = {}
    for rel in MEMORY_FILES:
        current[rel] = file_digest(os.path.join(project_dir, rel))

    if evidence.has_header:
        # 🔴 检查 A（内容口径，主）：逐个比对 md5
        for rel in MEMORY_FILES:
            recorded = evidence.header.get(rel)
            if recorded is None:
                defects.append(
                    Defect(
                        rel,
                        0,
                        "R-回归证据缺登记",
                        "证据头里没有登记本层 ⇒ 无法判定该层是否被回归覆盖：%s" % rel,
                        _preview(rel, project_dir),
                    )
                )
            elif recorded != current[rel]:
                defects.append(
                    Defect(
                        rel,
                        0,
                        "R-回归证据过期",
                        "回归时的 md5=%s，当前 md5=%s ⇒ 改了 memory 未同轮重跑回归" % (recorded, current[rel]),
                        _preview(rel, project_dir),
                    )
                )
    else:
        # 🔴 检查 A'（时间口径，回退）：旧日志没有证据头 ⇒ 比 mtime
        for rel in MEMORY_FILES:
            path = os.path.join(project_dir, rel)
            try:
                mtime = os.path.getmtime(path)
            except OSError as exc:
                raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc
            if mtime > evidence.mtime:
                defects.append(
                    Defect(
                        rel,
                        0,
                        "R-回归证据过期",
                        "memory mtime=%s 新于回归日志 mtime=%s ⇒ 改了 memory 未同轮重跑回归"
                        % (datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
                           datetime.fromtimestamp(evidence.mtime).astimezone().isoformat(timespec="seconds")),
                        _preview(rel, project_dir),
                    )
                )

    # 🔴 检查 B：红灯不能当证据
    if evidence.summary is None:
        return Analysis(
            defects=defects,
            evidence=evidence,
            applicable=False,
            note="回归日志 %s 的 pytest 摘要不可解析 ⇒ 证据不足，不适用" % evidence.path,
        )
    if not evidence.summary.is_green:
        defects.append(
            Defect(
                evidence.path,
                0,
                "R-回归红",
                "最新回归是红的（failed=%d error=%d passed=%d）⇒ 红灯不能当证据"
                % (evidence.summary.failed, evidence.summary.error, evidence.summary.passed),
                evidence.summary.raw[:80],
            )
        )

    return Analysis(defects=defects, evidence=evidence, applicable=True)


def build_payload(analysis: Analysis, project_dir: str, regression_root: str | None) -> dict:
    """结构化结果（供 `--json-out`）。"""
    evidence = analysis.evidence
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "regression_root": regression_root or DEFAULT_REGRESSION_ROOT,
        "applicable": analysis.applicable,
        "note": analysis.note,
        "evidence": None
        if evidence is None
        else {
            "path": evidence.path,
            "mtime": datetime.fromtimestamp(evidence.mtime).astimezone().isoformat(timespec="seconds"),
            "has_header": evidence.has_header,
            "header": evidence.header,
            "summary": None
            if evidence.summary is None
            else {
                "passed": evidence.summary.passed,
                "failed": evidence.summary.failed,
                "error": evidence.summary.error,
                "skipped": evidence.summary.skipped,
                "raw": evidence.summary.raw,
            },
        },
        "summary": {"defect_count": len(analysis.defects)},
        "defects": [defect.as_dict() for defect in analysis.defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读检查「回归证据是否覆盖当前 memory 四层」（rules.md §十四.128 第 6 条的机器闸门）。"
        )
    )
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help="工程根目录")
    parser.add_argument(
        "--regression-root",
        default=None,
        help="回归证据根（默认 %s）" % DEFAULT_REGRESSION_ROOT,
    )
    parser.add_argument(
        "--regression-log",
        default=None,
        help="可选：直接指定某一份 pytest.log（优先于 --regression-root 的自动选取）",
    )
    parser.add_argument(
        "--emit-header",
        action="store_true",
        help="只打印 [MEMORY-MD5] 证据头行（供写回归日志时嵌入）",
    )
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        if args.emit_header:
            print(build_header(args.project_dir))
            return 0
        analysis = analyze(args.project_dir, args.regression_root, args.regression_log)
    except InputError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 2

    evidence = analysis.evidence
    if evidence is not None:
        print("[证据] %s（%s）" % (evidence.path, datetime.fromtimestamp(evidence.mtime).astimezone().isoformat(timespec="seconds")))
        print("[证据] 口径：%s" % ("md5 内容口径（带 [MEMORY-MD5] 头）" if evidence.has_header else "mtime 回退口径（旧日志，无证据头）"))
        if evidence.summary is not None:
            print(
                "[证据] 摘要：%d passed / %d failed / %d error / %d skipped"
                % (evidence.summary.passed, evidence.summary.failed, evidence.summary.error, evidence.summary.skipped)
            )
    if not analysis.applicable:
        print("[NOT-APPLICABLE] %s" % analysis.note)
        print("合计：缺陷 0 个（不适用 ≠ 不合格）")

    for defect in analysis.defects:
        location = "%s:%d" % (defect.file, defect.line_no) if defect.line_no else defect.file
        print("[DEFECT] %s %s｜%s｜%s" % (location, defect.check, defect.reason, defect.preview))
    if analysis.applicable:
        print("合计：缺陷 %d 个" % len(analysis.defects))

    if args.json_out:
        payload = build_payload(analysis, args.project_dir, args.regression_root)
        try:
            parent = os.path.dirname(os.path.abspath(args.json_out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            print("[ERROR] 写出 JSON 结果失败：%s（%s）" % (args.json_out, exc), file=sys.stderr)
            return 2

    if analysis.defects:
        return 1
    return 0 if analysis.applicable else 2


if __name__ == "__main__":
    raise SystemExit(main())
