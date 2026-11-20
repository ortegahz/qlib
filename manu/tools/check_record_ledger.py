#!/usr/bin/env python3
"""只读检查「逐日台账」`manu/memory/records/etf_rotation_daily_ledger.csv` 的**完整性**。

为什么需要（`rules.md §十四.122`）
    §十四.122（用户 2026-11-19 亲定铁律）要求把「**不可从开源数据复现的核心信息**」
    （当天 ETF 权重、动作、否决理由、账户口径…）落成**可复盘的事实记录**；
    其「载体 B」＝ **机器可读的逐日台账**（`etf_rotation_daily_ledger.csv`，append-only）。
    🔴 **既有守卫只覆盖「写入端语义」**（`test_daily_record_ledger.py` 的幂等/作废逻辑），
    **台账文件本体没有任何机器闸门** ⇒ 列名被改、同一 (日期,代码) 出现两条「有效」、
    同一天账户口径分叉、成交态与成交价自相矛盾…**全部不会报错**，只会**默默误导复盘**。
    本工具补上这一层。

判定口径（期望值**锚写入端**，不另立事实源）
    * **表头**：`LEDGER_HEADER` **从写入端 `manu/tools/append_daily_record.py` 直接取**
      （同 `§十四.123`「守卫期望值不许取自被检对象自述」的同源思路：写入端才是事实源）
      ⇒ 表头与写入端**不可能分叉**。
    * **检查 A（表头）**：CSV 表头必须与写入端 `LEDGER_HEADER` **逐项同序**。
    * **检查 B（行结构）**：每行字段数必须 == 表头列数（多列/少列都算坏行）。
    * **检查 C（唯一有效行）**：同一 `(日期, 代码)` 至多一条「有效」行
      （「作废」行是**保留演化轨迹**的正常历史，不算重复）。
    * **检查 D（状态域）**：`记录状态` 只能是 `有效` / `作废`。
    * **检查 E（日期格式）**：`日期` 必须 `YYYY-MM-DD` 且是合法日期。
    * **检查 F（权重域）**：`当前权重` / `目标权重` / `目标权重生效` 必须是 `[0, 1]` 内的数。
    * **检查 G（同日口径一致）**：同一 `日期` 的所有行，`账户总资产` / `账户现金` 必须一致
      （同一天的口径分叉 ⇒ 复盘时对不上账）。
    * **检查 H（成交一致性）**：`是否成交` 为「否…」⇒ `成交价`/`成交金额` 必须是空或「不适用」；
      为成交态 ⇒ `成交价`/`成交金额` 必须是**数字**（不许空）。
    * **检查 I（核心字段非空）**：核心字段不许留空（**缺失必须显式写「缺」**，见写入端 `MISSING`）。
    * 🔴 **本工具只读**：不写、不改台账（测试里有 md5 前后对照）。

用法
    python3 manu/tools/check_record_ledger.py --project-dir .
    python3 manu/tools/check_record_ledger.py --project-dir . \
        --ledger manu/memory/records/etf_rotation_daily_ledger.csv

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 用法/输入错误（文件读不到、写入端导入失败等）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_658d3fc4"

#: 台账默认落点（相对工程根）
LEDGER_REL = "manu/memory/records/etf_rotation_daily_ledger.csv"

#: 写入端（**表头与「缺」标记的单一事实源**）
WRITER_REL = "manu/tools/append_daily_record.py"

#: `记录状态` 的合法取值
VALID_STATUS = ("有效", "作废")

#: 核心字段（**不许留空**；缺失必须显式写「缺」）
CORE_FIELDS = (
    "日期",
    "代码",
    "动作",
    "当前股数",
    "当前权重",
    "目标权重",
    "账户总资产",
    "账户现金",
    "来源",
)

#: 权重域字段（必须在 `[0, 1]`）
WEIGHT_FIELDS = ("当前权重", "目标权重", "目标权重生效")

#: 日期格式
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 「未成交」的写法前缀（写入端观察态统一写「否（观察态不下单）」）
NOT_TRADED_PREFIX = "否"


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
class LedgerFacts:
    """台账的**实测**口径。"""

    path: str = ""
    header: tuple = ()
    row_count: int = 0
    effective_count: int = 0
    dates: list = field(default_factory=list)


def read_text(path: str) -> str:
    """读文件；读不到 ⇒ 中文异常（⇒ 退出码 2）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc


def file_digest(path: str) -> str:
    """文件 md5（测试用它证明「只读」）。"""
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc
    return hasher.hexdigest()


def load_writer(project_dir: str):
    """按路径加载**写入端**（`manu/tools/append_daily_record.py`）⇒ 取 `LEDGER_HEADER` / `MISSING`。

    🔴 期望值必须**锚事实源**（写入端），不许在本工具里复制一份列名 ⇒ 否则两处会分叉。
    """
    path = os.path.join(project_dir, WRITER_REL)
    if not os.path.isfile(path):
        raise InputError("找不到写入端：%s（表头事实源缺失 ⇒ 无法判定）" % path)
    spec = importlib.util.spec_from_file_location("append_daily_record_under_test", path)
    if not spec or not spec.loader:
        raise InputError("写入端无法加载：%s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    header = getattr(module, "LEDGER_HEADER", None)
    if not header:
        raise InputError("写入端里没有 LEDGER_HEADER ⇒ 表头事实源不可解析：%s" % path)
    return module


def _is_number(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def _preview(row: dict, key: str = "代码") -> str:
    """缺陷行给一点可读上下文。"""
    return "日期=%s 代码=%s 动作=%s" % (row.get("日期", ""), row.get("代码", ""), row.get("动作", ""))


def analyze(project_dir: str, ledger_path: str | None = None) -> tuple:
    """总入口 ⇒ (缺陷列表, 台账实测口径)。"""
    writer = load_writer(project_dir)
    header = tuple(writer.LEDGER_HEADER)
    missing_marker = getattr(writer, "MISSING", "缺")

    path = ledger_path or os.path.join(project_dir, LEDGER_REL)
    if not os.path.isfile(path):
        raise InputError("找不到台账文件：%s（§十四.122 的载体 B 缺失）" % path)

    text = read_text(path)
    try:
        raw_rows = list(csv.reader(text.splitlines()))
    except csv.Error as exc:
        raise InputError("台账 CSV 解析失败：%s（%s）" % (path, exc)) from exc
    if not raw_rows:
        raise InputError("台账是空文件：%s" % path)

    defects: list = []
    actual_header = tuple(raw_rows[0])
    body = raw_rows[1:]

    # ── 检查 A：表头必须与写入端逐项同序 ─────────────────────────────
    if actual_header != header:
        missing = [name for name in header if name not in actual_header]
        extra = [name for name in actual_header if name not in header]
        defects.append(
            Defect(
                LEDGER_REL,
                1,
                "A-表头漂移",
                "表头与写入端 LEDGER_HEADER 不一致（缺 %s ／ 多 %s）" % (missing or "无", extra or "无"),
                ",".join(actual_header[:8]) + "…",
            )
        )

    # ── 检查 B：每行字段数必须 == 表头列数 ───────────────────────────
    width = len(actual_header)
    for offset, raw in enumerate(body):
        if not any(cell.strip() for cell in raw):
            continue
        if len(raw) != width:
            defects.append(
                Defect(
                    LEDGER_REL,
                    offset + 2,
                    "B-行结构",
                    "第 %d 条数据行字段数 %d ≠ 表头列数 %d" % (offset + 1, len(raw), width),
                    ",".join(raw[:6]) + "…",
                )
            )

    # 解析成字典（按表头名对齐；缺列补空串）
    rows = []
    for offset, raw in enumerate(body):
        if not any(cell.strip() for cell in raw):
            continue
        padded = list(raw) + [""] * (width - len(raw))
        row = {name: padded[index] for index, name in enumerate(actual_header)}
        row["__line__"] = offset + 2
        rows.append(row)

    # ── 检查 D：状态域 ───────────────────────────────────────────────
    for row in rows:
        status = row.get("记录状态", "")
        if status not in VALID_STATUS:
            defects.append(
                Defect(
                    LEDGER_REL,
                    row["__line__"],
                    "D-状态域",
                    "记录状态=%r 不在合法取值 %s 内" % (status, list(VALID_STATUS)),
                    _preview(row),
                )
            )

    # ── 检查 E：日期格式 ─────────────────────────────────────────────
    for row in rows:
        date = row.get("日期", "")
        ok = bool(DATE_RE.match(date))
        if ok:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                ok = False
        if not ok:
            defects.append(
                Defect(
                    LEDGER_REL,
                    row["__line__"],
                    "E-日期格式",
                    "日期=%r 不是合法的 YYYY-MM-DD" % date,
                    _preview(row),
                )
            )

    # ── 检查 F：权重域 ───────────────────────────────────────────────
    for row in rows:
        for name in WEIGHT_FIELDS:
            value = (row.get(name) or "").strip()
            if value == "":
                continue
            if not _is_number(value):
                defects.append(
                    Defect(
                        LEDGER_REL,
                        row["__line__"],
                        "F-权重域",
                        "%s=%r 不是数字" % (name, value),
                        _preview(row),
                    )
                )
                continue
            number = float(value)
            if number < 0.0 or number > 1.0:
                defects.append(
                    Defect(
                        LEDGER_REL,
                        row["__line__"],
                        "F-权重域",
                        "%s=%s 越界（权重必须落在 [0, 1]）" % (name, value),
                        _preview(row),
                    )
                )

    # ── 检查 I：核心字段非空 ─────────────────────────────────────────
    for row in rows:
        for name in CORE_FIELDS:
            if name not in row:
                continue
            if (row.get(name) or "").strip() == "":
                defects.append(
                    Defect(
                        LEDGER_REL,
                        row["__line__"],
                        "I-核心字段缺失",
                        "核心字段 %s 留空（缺失必须显式写「%s」）" % (name, missing_marker),
                        _preview(row),
                    )
                )

    # ── 检查 H：成交一致性 ───────────────────────────────────────────
    for row in rows:
        traded = (row.get("是否成交") or "").strip()
        price = (row.get("成交价") or "").strip()
        amount = (row.get("成交金额") or "").strip()
        if traded.startswith(NOT_TRADED_PREFIX):
            for name, value in (("成交价", price), ("成交金额", amount)):
                if value not in ("", "不适用"):
                    defects.append(
                        Defect(
                            LEDGER_REL,
                            row["__line__"],
                            "H-成交一致性",
                            "未成交行却有 %s=%r" % (name, value),
                            _preview(row),
                        )
                    )
        else:
            for name, value in (("成交价", price), ("成交金额", amount)):
                if not _is_number(value):
                    defects.append(
                        Defect(
                            LEDGER_REL,
                            row["__line__"],
                            "H-成交一致性",
                            "成交态行的 %s=%r 不是数字" % (name, value),
                            _preview(row),
                        )
                    )

    # ── 检查 C：同 (日期, 代码) 至多一条「有效」 ──────────────────────
    seen = {}
    for row in rows:
        if row.get("记录状态") != "有效":
            continue
        key = (row.get("日期", ""), row.get("代码", ""))
        seen.setdefault(key, []).append(row["__line__"])
    for key, lines in seen.items():
        if len(lines) > 1:
            defects.append(
                Defect(
                    LEDGER_REL,
                    lines[0],
                    "C-重复有效行",
                    "同一 (日期=%s, 代码=%s) 有 %d 条「有效」行（行 %s）⇒ 复盘会重复计权"
                    % (key[0], key[1], len(lines), lines),
                    "日期=%s 代码=%s" % (key[0], key[1]),
                )
            )

    # ── 检查 G：同日账户口径一致 ─────────────────────────────────────
    caliber = {}
    for row in rows:
        date = row.get("日期", "")
        if not date:
            continue
        caliber.setdefault(date, []).append(row)
    for date, same_day in caliber.items():
        for name in ("账户总资产", "账户现金"):
            values = {(row.get(name) or "").strip() for row in same_day}
            if len(values) > 1:
                defects.append(
                    Defect(
                        LEDGER_REL,
                        same_day[0]["__line__"],
                        "G-同日语义口径",
                        "同一天 %s 的 %s 取值不一致：%s" % (date, name, sorted(values)),
                        "日期=%s" % date,
                    )
                )

    facts = LedgerFacts(
        path=path,
        header=actual_header,
        row_count=len(rows),
        effective_count=sum(1 for row in rows if row.get("记录状态") == "有效"),
        dates=sorted({row.get("日期", "") for row in rows}),
    )
    return defects, facts


def build_payload(defects: list, facts: LedgerFacts, project_dir: str) -> dict:
    """结构化结果（供 `--json-out`）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "ledger_path": facts.path,
        "summary": {
            "defect_count": len(defects),
            "row_count": facts.row_count,
            "effective_count": facts.effective_count,
            "dates": facts.dates,
            "column_count": len(facts.header),
        },
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查「逐日台账」（manu/memory/records/etf_rotation_daily_ledger.csv）的完整性。"
    )
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help="工程根目录")
    parser.add_argument("--ledger", default=None, help="可选：台账路径（默认工程根下的标准落点）")
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        defects, facts = analyze(args.project_dir, args.ledger)
    except InputError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 2

    print(
        "[台账] %s｜列数 %d｜数据行 %d｜有效行 %d｜日期 %s"
        % (facts.path, len(facts.header), facts.row_count, facts.effective_count, "、".join(facts.dates))
    )
    for defect in defects:
        location = "%s:%d" % (defect.file, defect.line_no) if defect.line_no else defect.file
        print("[DEFECT] %s %s｜%s｜%s" % (location, defect.check, defect.reason, defect.preview))
    print("合计：缺陷 %d 个" % len(defects))

    if args.json_out:
        payload = build_payload(defects, facts, args.project_dir)
        try:
            parent = os.path.dirname(os.path.abspath(args.json_out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.json_out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            print("[ERROR] 写出 JSON 结果失败：%s（%s）" % (args.json_out, exc), file=sys.stderr)
            return 2

    return 1 if defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
