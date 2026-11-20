#!/usr/bin/env python3
"""只读检查「记忆载体（memory 四层 ＋ record 载体）」的**入库纪律**与**落点纪律**。

为什么需要（`rules.md §十四.122` 第 5 条 ＋ `§十四.125` 铁律二）
    §十四.122 第 5 条「落点纪律」（用户亲定铁律的落地侧）：
      ① `manu/memory/` 下**只许记忆文档 ＋ 逐日记录**（`.md` ＋ 逐日台账 `.csv`）
         ⇒ 出现 `.pkl` / 行情 `.csv` / 其他运行产物 = 违规（撞「版本库只放源」铁律十六）；
      ② **台账 `.csv` 入库前必须验「没被 `.gitignore` 静默误杀」**
         （上游全局 `*.csv` ⇒ 台账不入库 ⇒ 事实源随工作区清理一起消失）。
    §十四.125 铁律二「新增载体（目录 / 文件）落地的同一轮，必须验『会不会被提交』」
      ⇒ 权威验法只看**忽略状态**（`git check-ignore --no-index`），**不判索引**
      （用户明早手动 `git add` ⇒ 「未跟踪」是正常暂存态，拿它当缺陷 = 恒假红）。

    🔴 **病根（实测）**：这两条此前**只有人守** ——
      * `check_mirror_pairs.py` 只管**镜像对**（`manu/<X>` ↔ `manu/etf_rotation/<X>`），
        而 `manu/memory/` **整棵树不在镜像体系里**（`manu/etf_rotation/memory/` 不存在）
        ⇒ 「台账被全局 `*.csv` 静默忽略」这种形态**没有任何闸门能报**（真缺陷【十九】的复发点）；
      * `.gitattributes` 的 `-text` **全仓没有任何工具检查**
        （`grep -rn gitattributes manu/tools/*.py` = 0 命中）
        ⇒ 铁律一「`-text` 同样要成对」也是**靠人记** ⇒ 复发即断链。
    本工具补上这一层（**纯只读**）。

判定口径（期望值**全部派生**，不另立事实源）
    * **检查 A（载体不被忽略）**：`manu/memory/` 下**每个文件**都不得落在忽略规则里
      （`git check-ignore --no-index`）⇒ 记忆事实源不会随工作区清理一起消失；
      逐日台账还**必须被显式放行**（命中 `!` 规则）——「登记制」，
      不许靠「恰好没被全局 `*.csv` 命中」这种侥幸。
    * **检查 B（`-text` 成对）**：用 `git check-attr text`（**权威口径**，不手写模式匹配）
      判每对镜像的 `text` 属性 ⇒ **一侧 `unset` 而另一侧不是** = 缺陷
      （行尾归一化会让两侧字节分叉 ⇒ 提交后镜像对不再逐字一致 ⇒ 新克隆缺一半）。
      逐日台账必须 `-text`（§十四.122 第 5 条明写「必要时补 `.gitattributes` 的 `-text`」）。
      配对集合**从 `check_mirror_pairs.py` 导入**（同一事实源 ⇒ 两处不会漂移）。
    * **检查 C（文件类型白名单）**：`manu/memory/` 下只许
      记忆文档（`*.md`，含 `archive/*.md.bak.*` 备份）＋ 逐日台账（`records/etf_rotation_daily_ledger.csv`）
      ⇒ 其余（`.pkl` / `.json` / 其它 `.csv` / 运行产物）= 违规。
    * **检查 D（体积上限）**：`manu/memory/records/` 下单文件 ≤ **2 MiB**、目录合计 ≤ **16 MiB**
      ⇒ 防「把大产物拷进 records/」（§十四.122 第 5 条：引用大产物**只写路径**）。
    🔴 **本工具只读**：不写、不改任何被检查的文件（测试里有 md5 前后对照）。

用法
    python3 manu/tools/check_record_carrier.py --project-dir .
    python3 manu/tools/check_record_carrier.py --project-dir . --json-out /tmp/carrier.json

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 不适用/输入错误（不在 git 工作区、`git` 不可用等）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_658d3fc4"

#: 记忆树根（相对工程根）
MEMORY_ROOT_REL = "manu/memory"

#: 逐日台账（**登记制**：唯一登记在册的结构化载体）
LEDGER_REL = "manu/memory/records/etf_rotation_daily_ledger.csv"

#: record 目录（逐日人读记录 ＋ 逐日台账）
RECORDS_DIR_REL = "manu/memory/records"

#: 记忆文档白名单：`.md`（含 `archive/*.md.bak.*` 备份）
MEMORY_DOC_SUFFIX = ".md"
#: 记忆文档备份（形如 `rules.md.bak.20261116094426`）
MEMORY_DOC_BACKUP_MARK = ".md.bak."
#: 备份目录（**唯一**允许放记忆文档备份的地方）
ARCHIVE_DIR_REL = "manu/memory/archive"
#: 备份名正则：`<任意名>.md.bak.<戳>`（与 `rules.md §十四.122` 第 7 条正文同口径）
MEMORY_DOC_BACKUP_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.md\.bak\.[A-Za-z0-9_\-]+$")
#: **不许**出现在备份名里的产物扩展名（防 `x.pkl.md.bak.1` 这类伪装）
ARTIFACT_SUFFIXES = (".pkl", ".csv", ".json", ".parquet", ".zip", ".log", ".npy", ".h5", ".pkl")

#: 体积上限（bytes）：单文件 2 MiB / records/ 目录合计 16 MiB
MAX_RECORD_FILE_BYTES = 2 * 1024 * 1024
MAX_RECORDS_TOTAL_BYTES = 16 * 1024 * 1024

#: 跳过的目录（编译产物 / 版本库垃圾）
SKIP_DIR_NAMES = ("__pycache__", ".git", ".pytest_cache")


class InputError(Exception):
    """输入/用法错误 ⇒ 退出码 2。"""


class NotApplicable(Exception):
    """环境不适用（例如不在 git 工作区）⇒ 退出码 2，且**不算不合格**。"""


@dataclass
class Defect:
    """一条缺陷（字段与 `check_mirror_pairs.py` 同构）。"""

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
class CarrierFacts:
    """载体纪律的**实测**口径。"""

    memory_files: int = 0
    ignored_files: int = 0
    whitelisted_files: int = 0
    ledger_whitelisted: bool = False
    ledger_text_unset: bool = False
    attr_pairs: int = 0
    records_files: int = 0
    records_bytes: int = 0
    largest_record_bytes: int = 0
    sample: list = field(default_factory=list)


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
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc
    return hasher.hexdigest()


def _git_check_ignore(project_dir: str, paths: list) -> tuple:
    """一次 `git check-ignore -v --no-index --stdin -z` ⇒ (显式放行表, 忽略表)。

    🔴 `--no-index`：**只认忽略规则、不认索引状态** ⇒ 判「将来提交会不会只进去一半」
      （用户明早手动 `git add` ⇒ 索引口径会恒假红，见 `§十四.125` 铁律三）。
    🔴 显式放行 ＝ 命中的最后一条规则以 `!` 开头（`git check-ignore -v` 会把否定规则也打出来）。
    """
    if not paths:
        return {}, {}
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-v", "--no-index", "--stdin", "-z"],
            input="\0".join(paths).encode("utf-8"),
            capture_output=True,
            cwd=project_dir,
            check=False,
        )
    except OSError as exc:
        raise NotApplicable("git 不可用：%s" % exc) from exc
    if completed.returncode not in (0, 1):
        raise NotApplicable(
            "git check-ignore 不可用（rc=%d）：%s"
            % (completed.returncode, completed.stderr.decode("utf-8", "replace")[:200])
        )
    whitelisted, ignored = {}, {}
    # 🔴 `git check-ignore -v --stdin -z` 的输出是 **4 元组**（NUL 分隔）：
    #    `source NUL lineno NUL pattern NUL pathname NUL`
    #    （**不是** `pattern\tpath` ！⇒ 按 tab 解析会**静默跳过全部命中** ⇒ 假绿）
    tokens = [token for token in completed.stdout.decode("utf-8", "replace").split("\0") if token]
    for index in range(0, len(tokens) - 3, 4):
        pattern = tokens[index + 2]
        path = tokens[index + 3]
        if pattern.startswith("!"):
            whitelisted[path] = pattern
        else:
            ignored[path] = pattern
    return whitelisted, ignored


def _git_check_attr_text(project_dir: str, paths: list) -> dict:
    """一次 `git check-attr text -- <paths>` ⇒ {相对路径: 属性值}。

    🔴 **权威口径**：直接问 git「这个文件的 `text` 属性是什么」，
      不手写 `.gitattributes` 的模式匹配（手写 = 复制一份 git 的语义 ⇒ 必然漂移）。
    🔴 值为 `unset` ⇒ 命中 `-text`（关掉行尾归一化）；`unspecified` ⇒ 没命中。
    """
    if not paths:
        return {}
    try:
        completed = subprocess.run(
            ["git", "check-attr", "text", "--"] + list(paths),
            capture_output=True,
            cwd=project_dir,
            check=False,
        )
    except OSError as exc:
        raise NotApplicable("git 不可用：%s" % exc) from exc
    if completed.returncode != 0:
        raise NotApplicable(
            "git check-attr 不可用（rc=%d）：%s"
            % (completed.returncode, completed.stderr.decode("utf-8", "replace")[:200])
        )
    result = {}
    for raw in completed.stdout.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(": ")
        if len(parts) < 3:
            continue
        result[": ".join(parts[:-2])] = parts[-1]
    return result


def _load_mirror_module():
    """导入 `check_mirror_pairs.py`（**配对集合的单一事实源** ⇒ 两处不会漂移）。"""
    path = Path(__file__).resolve().with_name("check_mirror_pairs.py")
    if not path.is_file():
        raise InputError("找不到配对事实源：%s" % path)
    spec = importlib.util.spec_from_file_location("check_mirror_pairs", path)
    if spec is None or spec.loader is None:
        raise InputError("配对事实源导入失败：%s" % path)
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须**先注册进 `sys.modules`**：`check_mirror_pairs.py` 用了 `dataclass`，
    #    dataclass 解析注解时会回查 `sys.modules[cls.__module__].__dict__`
    #    ⇒ 不注册 ⇒ 报 `'NoneType' object has no attribute '__dict__'`。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 —— 导入失败 ⇒ 退出码 2，不许静默跳过
        raise InputError("配对事实源导入失败：%s（%s）" % (path, exc)) from exc
    return module


def _collect_memory_files(project_dir: str) -> list:
    """`manu/memory/` 下的全部文件（相对工程根）。"""
    root = os.path.join(project_dir, MEMORY_ROOT_REL)
    if not os.path.isdir(root):
        raise InputError("找不到记忆树：%s" % root)
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in sorted(filenames):
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, project_dir).replace(os.sep, "/")
            collected.append((relative, absolute))
    return sorted(collected)


def _classify_memory_file(relative: str) -> str:
    """归类 `manu/memory/` 下的文件 ⇒ 'ledger' / 'doc' / 'violation'。

    🔴 **备份只认一种形态**：`manu/memory/archive/` 目录 ＋ `<名>.md.bak.<戳>` ＋ 名字里**不含产物扩展名**
      （否则 `x.pkl.md.bak.1` 这类伪装会被当成记忆文档放行）。
    """
    if relative == LEDGER_REL:
        return "ledger"
    name = os.path.basename(relative)
    if name.endswith(MEMORY_DOC_SUFFIX):
        return "doc"
    if relative.startswith(ARCHIVE_DIR_REL + "/") and MEMORY_DOC_BACKUP_RE.match(name):
        stem = name.split(MEMORY_DOC_BACKUP_MARK, 1)[0]
        if not any(stem.endswith(suffix) for suffix in ARTIFACT_SUFFIXES):
            return "doc"
    return "violation"


def analyze(project_dir: str) -> tuple:
    """跑全部检查 ⇒ (defects, facts)。"""
    memory_files = _collect_memory_files(project_dir)
    if not memory_files:
        raise InputError("记忆树是空的：%s" % MEMORY_ROOT_REL)

    facts = CarrierFacts(memory_files=len(memory_files))
    defects = []

    relative_paths = [relative for relative, _absolute in memory_files]
    whitelisted, ignored = _git_check_ignore(project_dir, relative_paths)
    facts.whitelisted_files = len(whitelisted)
    facts.ignored_files = len(ignored)

    # ---------- 检查 A：载体不被忽略 ----------
    for relative in relative_paths:
        if relative in ignored:
            defects.append(
                Defect(
                    file=relative,
                    line_no=0,
                    check="A/载体被忽略",
                    reason=(
                        "记忆载体落在 `.gitignore` 的忽略规则里（命中 `%s`）"
                        "⇒ 不会进版本库 ⇒ 几个月后复盘时事实源随工作区清理一起消失"
                        "（`rules.md §十四.122` 第 5 条 / `§十四.125` 铁律二）"
                    )
                    % ignored[relative],
                    preview=relative,
                )
            )

    # ---------- 检查 A2：逐日台账必须被显式放行（登记制） ----------
    ledger_absolute = os.path.join(project_dir, LEDGER_REL)
    if not os.path.isfile(ledger_absolute):
        defects.append(
            Defect(
                file=LEDGER_REL,
                line_no=0,
                check="A2/台账缺失",
                reason=(
                    "逐日台账不存在：`%s` ⇒ 「几个月后复盘」的唯一结构化载体缺失"
                    "（`rules.md §十四.122` 载体 B）"
                ),
                preview=LEDGER_REL,
            )
        )
    else:
        facts.ledger_whitelisted = LEDGER_REL in whitelisted
        if not facts.ledger_whitelisted:
            defects.append(
                Defect(
                    file=LEDGER_REL,
                    line_no=0,
                    check="A2/台账未显式放行",
                    reason=(
                        "逐日台账**没有被 `.gitignore` 显式放行**（缺 `!` 规则）"
                        "⇒ 上游全局 `*.csv` 一旦恢复/新增就会静默忽略台账"
                        "⇒ 本工具不许靠「恰好没命中」这种侥幸（`rules.md §十四.125` 铁律二·登记制）"
                    ),
                    preview=LEDGER_REL,
                )
            )

    # ---------- 检查 B：`-text` 成对（镜像对）＋ 台账必须 `-text` ----------
    mirror = _load_mirror_module()
    pairs = mirror._collect_fs_pairs(project_dir)  # noqa: SLF001 —— 单一事实源
    pairs.update(mirror._collect_whitelist_pairs(project_dir))  # noqa: SLF001
    attr_paths = []
    for relative in sorted(pairs):
        for absolute in pairs[relative]:
            attr_paths.append(os.path.relpath(absolute, project_dir).replace(os.sep, "/"))
    attr_paths = sorted(set(attr_paths))
    attrs = _git_check_attr_text(project_dir, attr_paths)
    for relative in sorted(pairs):
        source, target = pairs[relative]
        source_rel = os.path.relpath(source, project_dir).replace(os.sep, "/")
        target_rel = os.path.relpath(target, project_dir).replace(os.sep, "/")
        # 🔴 **只对两侧都存在的镜像对判 `-text` 成对**：缺一侧是「成对存在」的业务
        #    （`check_mirror_pairs.py` 的检查 A）⇒ 不许在本闸门里越界重复报。
        if not (os.path.isfile(source) and os.path.isfile(target)):
            continue
        source_attr = attrs.get(source_rel, "unspecified")
        target_attr = attrs.get(target_rel, "unspecified")
        facts.attr_pairs += 1
        if (source_attr == "unset") != (target_attr == "unset"):
            defects.append(
                Defect(
                    file=source_rel,
                    line_no=0,
                    check="B/-text 未成对",
                    reason=(
                        "镜像对两侧的 `.gitattributes` `-text` **不成对**"
                        "（`%s`=%s ／ `%s`=%s）⇒ 行尾归一化会让两侧字节分叉"
                        "⇒ 提交后镜像对不再逐字一致（`rules.md §十四.125` 铁律一）"
                    )
                    % (source_rel, source_attr, target_rel, target_attr),
                    preview="%s ↔ %s" % (source_rel, target_rel),
                )
            )

    ledger_attr = _git_check_attr_text(project_dir, [LEDGER_REL]).get(LEDGER_REL, "unspecified")
    facts.ledger_text_unset = ledger_attr == "unset"
    if os.path.isfile(ledger_absolute) and not facts.ledger_text_unset:
        defects.append(
            Defect(
                file=LEDGER_REL,
                line_no=0,
                check="B/台账缺 -text",
                reason=(
                    "逐日台账**没有 `.gitattributes` 的 `-text`**（当前 `%s`）"
                    "⇒ CRLF 转换会让「产物 ↔ 台账」的指纹对不上（`rules.md §十四.122` 第 5 条）"
                )
                % ledger_attr,
                preview=LEDGER_REL,
            )
        )

    # ---------- 检查 C：`manu/memory/` 文件类型白名单 ----------
    for relative, absolute in memory_files:
        kind = _classify_memory_file(relative)
        if kind == "ledger":
            continue
        if kind == "doc":
            continue
        defects.append(
            Defect(
                file=relative,
                line_no=0,
                check="C/载体类型越界",
                reason=(
                    "`manu/memory/` 下只许**记忆文档（`.md`）＋ 逐日台账（`%s`）**"
                    "⇒ 该文件是运行产物/其它类型 ⇒ 违规"
                    "（`rules.md §十四.122` 第 5 条「版本库只放源」）"
                )
                % LEDGER_REL,
                preview=relative,
            )
        )

    # ---------- 检查 D：records/ 体积上限 ----------
    records_files = []
    for relative, absolute in memory_files:
        if not relative.startswith(RECORDS_DIR_REL + "/"):
            continue
        try:
            size = os.path.getsize(absolute)
        except OSError as exc:
            raise InputError("量不到文件大小：%s（%s）" % (absolute, exc)) from exc
        records_files.append((relative, size))
        facts.records_files += 1
        facts.records_bytes += size
        facts.largest_record_bytes = max(facts.largest_record_bytes, size)
        if size > MAX_RECORD_FILE_BYTES:
            defects.append(
                Defect(
                    file=relative,
                    line_no=0,
                    check="D/单文件超限",
                    reason=(
                        "record 单文件 %d 字节 > 上限 %d 字节 ⇒ 大产物不许拷进 records/"
                        "（引用只写路径，`rules.md §十四.122` 第 5 条）"
                    )
                    % (size, MAX_RECORD_FILE_BYTES),
                    preview=relative,
                )
            )
    if facts.records_bytes > MAX_RECORDS_TOTAL_BYTES:
        defects.append(
            Defect(
                file=RECORDS_DIR_REL,
                line_no=0,
                check="D/目录合计超限",
                reason=(
                    "records/ 目录合计 %d 字节 > 上限 %d 字节 ⇒ 大产物不许拷进 records/"
                    "（`rules.md §十四.122` 第 5 条）"
                )
                % (facts.records_bytes, MAX_RECORDS_TOTAL_BYTES),
                preview=RECORDS_DIR_REL,
            )
        )

    facts.sample = [relative for relative, _absolute in memory_files][:8]
    return defects, facts


def build_payload(defects: list, facts: CarrierFacts, project_dir: str) -> dict:
    """结构化结果（供 `--json-out`）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "summary": {
            "defect_count": len(defects),
            "memory_files": facts.memory_files,
            "ignored_files": facts.ignored_files,
            "whitelisted_files": facts.whitelisted_files,
            "ledger_whitelisted": facts.ledger_whitelisted,
            "ledger_text_unset": facts.ledger_text_unset,
            "attr_pairs": facts.attr_pairs,
            "records_files": facts.records_files,
            "records_bytes": facts.records_bytes,
        },
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查「记忆载体的入库纪律 ＋ 落点纪律」"
        "（rules.md §十四.122 第 5 条 / §十四.125 铁律二）。"
    )
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help="工程根目录")
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        defects, facts = analyze(args.project_dir)
    except InputError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 2
    except NotApplicable as exc:
        print("[NOT-APPLICABLE] %s" % exc)
        print("合计：缺陷 0 个（不适用 ≠ 不合格）")
        return 2

    print(
        "[载体] 记忆文件 %d 个｜被忽略 %d 条｜显式放行 %d 条｜台账放行=%s 台账-text=%s"
        "｜镜像对 %d 组（已查 -text）｜records/ %d 个文件 / %d 字节"
        % (
            facts.memory_files,
            facts.ignored_files,
            facts.whitelisted_files,
            "是" if facts.ledger_whitelisted else "否",
            "是" if facts.ledger_text_unset else "否",
            facts.attr_pairs,
            facts.records_files,
            facts.records_bytes,
        )
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
