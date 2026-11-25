#!/usr/bin/env python3
"""只读检查「产物落点双根化 ＋ 痕迹清零」的**验收断言**。

为什么需要（`rules.md §十六` 第 7 条）
    §十六 第 7 条原文：「**验收方式（闸门 / 自检必须能断言，不许只写在文档里）**」，
    并逐条点名三条**必须能断言**的条件：
      ① 本项目在服务器上的文件**全部位于** `<DATASET_ROOT>` **或** `<RESULTS_ROOT>` 之下；
      ② `/mnt/data` 根**没有** `_etf_daily_*`；`/mnt/data/siping/datasets/` 顶层**没有**本项目目录；
      ③ `/mnt/data/siping/runs/` 下**本项目专属**目录**只存在** `<RESULTS_ROOT>` **一个**
         （别人的 `nogmc_*` / `npu20ms_*` / `ashare_topk` / `CANDIDATES` / `_ms6` / `_ms7`
         **不计、也不许删**）；
    §十六 第 4 条 2026-11-19 夜补：「**`/tmp` 里的本项目件同样必须清零**」
      （`/tmp/pytest-of-*` 是 pytest 自己管理的 tmp_path 根 ⇒ **故意不动**）。

    🔴 **病根（实测）**：这三条此前**只有人守** ——
      现有工具只覆盖**写入端常量**（`daily_config.py` 的 `ETF_ROTATION_OUTPUT_ROOT` /
      `ETF_ROTATION_RESULTS_ROOT`）与**仓库内载体**（`check_record_carrier.py` 管
      `manu/memory/`），**没有任何工具读回文件系统去断言「落点真的只剩双根」**
      ⇒ 落点漂了（产物写到 `/mnt/data` 根 / `datasets/etf` / `runs/etf_rotation/`）
      **没有任何闸门能报** ⇒ 复发即断链（§十六 第 2 条已点名 3 个历史违例实例）。
    本工具补上这一层（**纯只读**：不写、不删、不动任何被检查的文件）。

判定口径（期望值**全部派生**，不另立事实源）
    * **A1 双根自洽（跨主机都适用）**：`<DATASET_ROOT>` / `<RESULTS_ROOT>` 由
      `ETF_ROTATION_OUTPUT_ROOT` / `ETF_ROTATION_RESULTS_ROOT` 派生（默认同 §十六）；
      断言二者**绝对路径 / 互异 / 互不嵌套 / 都不在仓库工作区内 / 仓库也不在双根内 /
      都不等于「散落旧位置」顶层**；双根若落在共享目录里，则**必须**是规定的那一层
      （`<RESULTS_ROOT>` = `runs/` 的**直接子目录**；`<DATASET_ROOT>` = `datasets/<命名空间>/<项目>`）。
    * **A2 断言②a**：`/mnt/data` 顶层**没有** `_etf_daily_*`。
    * **A3 断言②b**：`siping/datasets/` 顶层**没有**本项目目录
      （`manu` 命名空间根允许；历史违例实例 `datasets/qlib/{cn_data_live,_stage1_work}` 单判）。
    * **A1b 命名空间内唯一**：`siping/datasets/` 之下**所有**命名空间容器里，
      本项目专属目录**只许** `<DATASET_ROOT>` **一个**（子项口径**比顶层更严** ——
      只认**明确词根** / `etf`＋日期 ⇒ 别的链的目录（`anti-uav*` / `arm_*` / `gmc_*` /
      `AVTrack` …）**不计、不碰**；实测服务器 30 个容器全扫 ⇒ **零误伤**）。
    * **A2 断言②a**：`/mnt/data` 顶层**没有** `_etf_daily_*`。
    * **A3 断言②b**：`siping/datasets/` 顶层**没有**本项目目录（**只认名字** ——
      **容器型目录一律豁免**：容器 ≠ 项目目录 ⇒ **零越权**；
      历史违例实例 `datasets/qlib/{cn_data_live,_stage1_work}` 单判）。
    * **A4 断言③**：`siping/runs/` 顶层**本项目专属**目录**只许** `<RESULTS_ROOT>` **一个**；
      **A4b 兜底**：顶层**共享目录**的下一层若藏着本项目专属目录（子项名命中归属证据）⇒ 同样是违例。
    * **A5 痕迹清零（`/tmp`）**：`/tmp` 顶层**没有**本项目件 —— 判据 ＝
      **归属证据名字 ∪ 共用词元＋取证 ∪ 内容标记（顶层普通文件）∪ 目录子项归属证据
      ∪ 目录内直接子文件的路径形态标记**；
      **隐藏项只免名字口径**（内容口径照判）＋ `pytest-of-*` / `pycharm_project_*` 豁免
      ＋ **他 uid 的件**与**本 uid 别的链的件**都**不计、不碰、不删**。
    * **A6 落点字面量必须由双 env 派生**（静态，跨主机都适用）：owner 清单里的
      **全部字符串常量**（AST **全量**扫描，**docstring 除外**）凡指向 `/mnt/data` 系的，
      都不许指向「散落旧位置」——**裸 `/mnt/data` 也算**（§十六 第 2 条点名的历史违例原形）；
      **双 env 派生本身必须还在**（`daily_config.py` 里两个 env 查得到）。

🔴 **适用性**：A2/A3/A4/A5 是**服务器侧**断言（`/mnt/data/siping/runs` 不存在 ⇒
   **不适用**，如实报「不适用」**不许**当成「通过」）；A1/A6 跨主机都适用。
   ⇒ **不适用 ≠ 不合格**（`rules.md §十四.117`）。

用法
    python3 manu/tools/check_artifact_placement.py --project-dir .
    python3 manu/tools/check_artifact_placement.py --project-dir . --json-out <RESULTS_ROOT>/_placement.json
    🔴 **不许把 `--json-out` 写进 `/tmp`**：产物 JSON 里含 `etf_rotation`（双根路径）
       ⇒ 会被 A5 的**内容口径**判成「本项目件」⇒ **闸门自己造出一条 `/tmp` 违例**

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 输入/用法错误。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_3b68a403"

#: 🔴 **双 env 的名字**（§十六 末尾「全部改成由**双 env** 派生」）
ENV_DATASET_ROOT = "ETF_ROTATION_OUTPUT_ROOT"
ENV_RESULTS_ROOT = "ETF_ROTATION_RESULTS_ROOT"

#: 🔴 **双根默认值**（§十六 第 1 条原文；字面量只许出现在本工具与 `daily_config.py`）
DEFAULT_DATASET_ROOT = "/mnt/data/siping/datasets/manu/etf_rotation"
DEFAULT_RESULTS_ROOT = "/mnt/data/siping/runs/etf_rotation_results"

#: 共享目录顶层（**别人的链共用** ⇒ 只读、只数、不许删）
MOUNT_ROOT = "/mnt/data"
SIPING_ROOT = "/mnt/data/siping"
DATASETS_SHARED_ROOT = "/mnt/data/siping/datasets"
RUNS_SHARED_ROOT = "/mnt/data/siping/runs"
TMP_ROOT = "/tmp"

#: 历史违例实例族（§十六 第 2 条**原文点名**）
LEGACY_WORK_PREFIX = "_etf_daily"

#: 🔴 **归属证据（名字）**：只认「**像 ETF 产物的名字**」，**不认**随机后缀里的 `etf`
#:   （独立审计实测：`/tmp/pymp-*/listener-etfccmhk` 这类**随机后缀**不是本项目件
#:    ⇒ 判据若用「名字里含 `etf`」就会**误报别人的件** = 越权）。
#:   ① **明确词根**：`etf_rotation` / `rotat` / `etf_prices` / `etf_universe` / `etf_spot` / `etf_holdings`；
#:   ② **`etf` 独立成词**（前后不是字母数字）：`etf` / `etf_scratch` / `my_etf_data`；
#:   ③ **`etf` 跟日期/数字**：`etf20261113` / `etf_20261113`；
#:   ⇒ `listener-etfccmhk`（随机后缀）**不算**；`codex-overnight-etf20261113` **算**。
PROJECT_NAME_TOKEN_LIST = (
    "etf_rotation",
    "rotat",
    "etf_prices",
    "etf_universe",
    "etf_spot",
    "etf_holdings",
)
#: `etf` **独立成词**
PROJECT_NAME_WORD_RE = re.compile(r"(?<![a-z0-9])etf(?![a-z0-9])")
#: `etf` **跟日期/数字**
PROJECT_NAME_DIGIT_RE = re.compile(r"etf[-_]?\d{6,8}")

#: 🔴 **多链共用词元**（`manu` / `qlib` 是同一用户**多条链共用**的 —— 无人机检测线同样用
#:   `manu/` 扩展层、同样跑 qlib ⇒ 命中**必须再取证**才算本项目，
#:   否则归「别的链的件」⇒ **不计、不碰、不删**（越权＝事故））。
PROJECT_SHARED_NAME_TOKENS = ("manu", "qlib")

#: `datasets/` 顶层**允许**的项目命名空间根（`<DATASET_ROOT>` 的第一层）
DATASET_NAMESPACE_DEFAULT = "manu"

#: `datasets/qlib/` 下的**历史违例实例**（§十六 第 2 条：那是本项目的自建增量日派生库）
LEGACY_QLIB_SUBDIRS = ("cn_data_live", "_stage1_work")

#: `/tmp` 豁免：
#:   `pytest-of-*` = pytest 自己管理的 tmp_path 根（§十六 第 4 条：**故意不动**）；
#:   `pycharm_project_*` = 工程工作副本族（**源码**，不是本项目产物）。
TMP_EXEMPT_PREFIXES = ("pytest-of-", "pycharm_project_")

#: 🔴 **登记制豁免**（**精确到文件名**，不是前缀 ⇒ 不会放过将来真叫 `vq_etf_*` 的本项目产物）：
#:   `vq_etf.txt` = **运行环境（CLI / QA 守护进程）自有的线程 turn 日志**
#:     —— 仓库内**零引用**（`grep -rn vq_etf` = 0 命中），
#:        内容只有「turn 编号 / 时间戳 / loop.log mtime」⇒ **不含任何本项目数据**；
#:        且它由**后台守护进程持续重写**（删掉一分钟内就回来）⇒ 「删了清零」这条路**走不通**。
#:     它之所以被报出来，是因为**名字口径**里 `etf` **独立成词**（`vq_etf.txt`）⇒ 命中
#:     ⇒ 与 `pytest-of-*` **同性质**（别的工具自己管理的件 ⇒ **故意不动**）。
TMP_EXEMPT_NAMES = ("vq_etf.txt",)

#: 🔴 **内容标记**（`/tmp` 本项目件的**内容口径**判据 —— 名字口径会漏检）
#:   🔴 病根（实测，独立审计抓出）：只按**名字词元**判 ⇒ 漏掉 **24** 个
#:   内容上明确属于本项目、名字却与本项目毫无关系的件（`v2verbose.log` / `seam_e2e_decision.json` /
#:   `write_memory_119.py` / `commit_v2.sh` …）⇒ 闸门报「`/tmp` 本项目件 = 0 个」
#:   = **假绿**（§十六 第 4 条「`/tmp` 里的本项目件同样必须清零」实质未达标）。
#:   ⇒ A5 判据 = **名字口径 ∪ 内容口径 ∪ 目录强词元口径**。
#:
#: 🔴🔴 **标记必须「链专属」**（同名 ≠ 同链 —— memory 里已登记的**误判防线**）：
#:   `manu/memory` 与**仓库根目录名**（`pycharm_project_*`）是同一用户**多条链共用**的
#:   （ETF 轮动线 / 红外弱小无人机检测线 **共用同一个仓库与 `manu/` 扩展层**）
#:   ⇒ 把它们当标记 ⇒ 会把**别人的链**的件（实测：`/tmp/memory.md`、`/tmp/memory_compact.md`、
#:   `/tmp/prompt_first.txt` = 无人机链的主索引）判成「本项目件」⇒ **越权**。
#:   ⇒ 只用**ETF 链专属**标记（`etf_rotation` 及其派生路径）。
CONTENT_MARKERS = (
    b"etf_rotation",
    b"siping/runs/etf_rotation_results",
    b"siping/datasets/manu/etf_rotation",
    b"manu/etf_rotation",
)

#: 🔴🔴 **目录内容口径专用标记集**（2026-11-20 复审补）：
#:   顶层**普通文件**用 `CONTENT_MARKERS` 全集；
#:   **目录**里的直接子文件**只认「路径形态」标记**（`siping/...` / `manu/...`）
#:   ⇒ 别的链的目录里**提到一次**「etf_rotation」这个词**不算**本项目件 ⇒ 防越权。
#:   （实测现场两处漏检件 `/tmp/inv/*.cand` 与 `/tmp/supplement.txt/supplement.txt`
#:     **都**含路径形态标记 ⇒ 新口径能报出来。）
CONTENT_MARKERS_PATH_FORM = (
    b"siping/runs/etf_rotation_results",
    b"siping/datasets/manu/etf_rotation",
    b"manu/etf_rotation",
)

#: 🔴 **目录子项的归属证据**（共享目录下一层判定用）
#:   🔴 **必须比顶层名字更严**：随机后缀（`listener-etfccmhk`）出现在**子项**里的概率更高
#:   ⇒ 目录子项只认**明确词根**（`etf_rotation` / `etf_prices` …）与 **`etf`＋日期**，
#:   **不认**「`etf` 独立成词」这一档。
PROJECT_CHILD_TOKEN_LIST = (
    "etf_rotation",
    "etf_prices",
    "etf_universe",
    "etf_spot",
    "etf_holdings",
    "rotat",
)
#: `etf` **跟日期/数字**（子项档）
PROJECT_CHILD_DIGIT_RE = re.compile(r"etf[-_]?\d{6,8}")

#: 内容扫描上限：单文件 ≤ **4 MiB**；读**头 64 KiB ＋ 尾 64 KiB**
#:   （🔴 只读头会漏：1.9 MB 的 memory 草稿，标记常常**只出现在尾部**；
#:    头＋尾是「有界代价下最大覆盖」的折中，且实测 588 个文件全扫仅 **0.2 秒**）
MAX_SCAN_FILE_BYTES = 4 * 1024 * 1024
SCAN_HEAD_BYTES = 65536

#: 🔴 **目录直接子项**扫描上限（有界代价：实测 `/tmp` 顶层 28,939 个目录
#:   全扫直接子文件 472 个 / 19.0 MB / **0.60 秒** ⇒ 可接受）
MAX_DIR_CHILD_SCAN = 64

#: 🔴 允许出现在 owner 代码里的**非产物**绝对路径（**只读输入**，不是本项目写出去的产物）
#:   `smtp.env` = 0600 权限的 SMTP 密钥 env 文件（`daily_config.py` 文档里已登记）
ALLOWED_EXTERNAL_LITERALS = ("/mnt/data/siping/secrets/smtp.env",)

#: 🔴 **owner 清单**（§十六 末尾「落地位置（owner）清单」——路径字面量只许出现在这些文件里）
OWNER_FILES = (
    "manu/etf_rotation/daily/daily_config.py",
    "manu/etf_rotation/daily/fetch_data.py",
    "manu/etf_rotation/config.py",
    "manu/etf_rotation/daily/stage1_rolling_increment.py",
    "manu/etf_rotation/qlib_provider.py",
)

#: 必须保留**双 env 派生**的那个文件（§十六 末尾 owner 清单第一条）
DUAL_ENV_FILE = "manu/etf_rotation/daily/daily_config.py"

#: 编译产物 / 版本库垃圾（扫仓库时跳过）
SKIP_DIR_NAMES = ("__pycache__", ".git", ".pytest_cache", "node_modules")


class InputError(Exception):
    """输入/用法错误 ⇒ 退出码 2。"""


@dataclass
class Defect:
    """一条缺陷（字段与 `check_record_carrier.py` 同构）。"""

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
class PlacementFacts:
    """落点纪律的**实测**口径。"""

    project_dir: str = ""
    dataset_root: str = ""
    results_root: str = ""
    server_scope: bool = False
    legacy_work_dirs: int = 0
    datasets_project_dirs: int = 0
    datasets_namespace: str = ""
    runs_project_dirs: int = 0
    runs_nested_project_dirs: int = 0
    runs_other_chains: int = 0
    tmp_project_items: int = 0
    tmp_exempt_items: int = 0
    #: 🔴 **他 uid 的件**（别的**用户**的）—— 不计、不碰、不删
    tmp_other_uid_items: int = 0
    #: 🔴 **本 uid、但不是本项目的件**（同一用户**别的链**的 scratch）—— 不计、不碰、不删
    tmp_other_chain_items: int = 0
    #: 🔴 **命名空间根下**多出来的本项目专属目录（A1b）
    datasets_namespace_project_dirs: int = 0
    #: 🔴 **目录内直接子文件内容**命中标记的 `/tmp` 条目数（A5 目录内容口径）
    tmp_content_dir_items: int = 0
    owner_files: int = 0
    owner_literals: int = 0
    owner_dual_env_ok: bool = False
    not_applicable: list = field(default_factory=list)


def _resolve_root(explicit: str, env_name: str, default: str) -> str:
    """解析一个根：CLI 覆盖 > env > 默认值。"""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    value = os.environ.get(env_name)
    if value:
        return os.path.abspath(os.path.expanduser(value))
    return os.path.abspath(default)


def _raw_root(explicit: str, env_name: str) -> str:
    """取**未经 `abspath`** 的原始根值（CLI 覆盖 > env）；都没有 ⇒ 空串。

    🔴 为什么要「原始值」：`_resolve_root` 恒 `abspath` ⇒ 直接判「绝对路径」
       是**死代码**（永远为真 ⇒ 删掉也不影响行为 ⇒ 零覆盖）。
       ⇒ 改成判**原始值**（`--dataset-root ./x` 或 `ETF_ROTATION_OUTPUT_ROOT=./x`）
       ⇒ **相对路径会静默变成「相对当前工作目录」** ⇒ 落点会随 cwd 漂 ⇒ 必须报。
    """
    if explicit:
        return explicit
    return os.environ.get(env_name) or ""


def _is_under(child: str, parent: str) -> bool:
    """`child` 是否在 `parent` 之下（严格子孙，自身不算）。"""
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    if child == parent:
        return False
    return child.startswith(parent.rstrip("/") + "/")


def _listdir(path: str) -> list:
    """列目录项名（读不到 ⇒ 空列表；调用方自己判适用性）。"""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _docstring_constants(tree: ast.AST) -> set:
    """收集 **docstring** 的 `ast.Constant` 节点 id ⇒ **不算**落点字面量。

    🔴 为什么要单独剔除：docstring 里出现 `/mnt/data` 根是**在讲禁令**，
       不是**在写落点** ⇒ 算进去就是**误报**（实测 `daily_config.py` 的模块 docstring
       里就写着「不许再往 `/mnt/data` 根…写」＋「默认 `/mnt/data/siping/secrets/smtp.env`」）。
    """
    nodes = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(id(first.value))
    return nodes


def _owner_code_literals(path: str) -> list:
    """取 owner 文件里的**全部字符串常量**（AST）⇒ [(lineno, value)]。

    🔴🔴 **2026-11-20 复审补（真缺陷）**：原先只取 `Path(<常量>)` 与
      `os.environ.get(<名字>, <常量默认值>)` **两种语法** ⇒ **`_WORK_ROOT = "/mnt/data"`
      这种裸串赋值**（§十六 第 2 条点名的**历史违例原形**）**完全隐形**
      ⇒ A6 形同虚设（把裸串改回去，闸门照样全绿）。
      ⇒ 现在改成 **AST 全量扫描**：文件里**每一个**字符串常量都收
      （**docstring 除外** —— docstring 里出现 `/mnt/data` 是在**讲禁令**，不是**在写落点**）。
    🔴 注释**天然不在 AST 里** ⇒ 不用额外处理。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError as exc:
        raise InputError("读不到 owner 文件：%s（%s）" % (path, exc)) from exc
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise InputError("owner 文件语法错：%s（%s）" % (path, exc)) from exc

    docstrings = _docstring_constants(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        found.append((node.lineno, node.value))
    return found


def _scan_head_tail(path: str, size: int) -> bytes:
    """读文件的**头 64 KiB ＋ 尾 64 KiB**（只读头会漏：标记常只出现在尾部）。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(SCAN_HEAD_BYTES)
            if size > SCAN_HEAD_BYTES * 2:
                handle.seek(-SCAN_HEAD_BYTES, os.SEEK_END)
                head += handle.read(SCAN_HEAD_BYTES)
    except OSError:
        return b""
    return head


def _content_marker_hit_file(path: str, size: int, markers: tuple) -> bool:
    """单个**普通文件**的内容有没有命中链专属标记。"""
    if not (0 < size <= MAX_SCAN_FILE_BYTES):
        return False
    head = _scan_head_tail(path, size)
    return any(marker in head for marker in markers)


def _content_marker_hit_dir(path: str, markers: tuple) -> bool:
    """**目录的直接子文件**里有没有命中链专属标记（🔴 深度 2，**有界**）。

    🔴🔴 **2026-11-20 复审补（真缺陷）**：原先的**内容口径只对顶层普通文件生效**
      （`os.path.isfile(path)`）⇒ **目录里的本项目件静默漏检**
      （实测现场漏掉 `/tmp/inv/`（5 个件）与 `/tmp/supplement.txt/`（1 个件）⇒ 闸门**假绿**）。
      ⇒ 现在**目录也走内容口径**：只扫**直接子文件**（≤ `MAX_DIR_CHILD_SCAN` 个），
        且**只认「路径形态」标记**（`CONTENT_MARKERS_PATH_FORM`）⇒ 防越权。
    """
    try:
        children = sorted(os.listdir(path))[:MAX_DIR_CHILD_SCAN]
    except OSError:
        return False
    for name in children:
        child = os.path.join(path, name)
        try:
            stat_result = os.stat(child)
        except OSError:
            continue
        if not os.path.isfile(child):
            continue
        if _content_marker_hit_file(child, stat_result.st_size, markers):
            return True
    return False


def _dual_env_literals(path: str) -> set:
    """owner 文件里 `os.environ.get(<名字>, …)` 用到的 env 名字集合。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError) as exc:
        raise InputError("读/解析 owner 文件失败：%s（%s）" % (path, exc)) from exc
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def _name_is_project(name: str) -> bool:
    """名字判「本项目专属」（**顶层条目**用）—— 只认**像 ETF 产物的名字**。

    🔴 「名字里含 `etf`」**不成立**（随机后缀 `listener-etfccmhk` 会误报 ⇒ 越权）。
    """
    lowered = name.lower()
    if any(token in lowered for token in PROJECT_NAME_TOKEN_LIST):
        return True
    if PROJECT_NAME_WORD_RE.search(lowered):
        return True
    if PROJECT_NAME_DIGIT_RE.search(lowered):
        return True
    return False


def _name_is_shared(name: str) -> bool:
    """名字命中**多链共用词元**（`manu` / `qlib`）⇒ 命中**必须再取证**才算本项目。"""
    lowered = name.lower()
    return any(token in lowered for token in PROJECT_SHARED_NAME_TOKENS)


def _child_name_is_project(name: str) -> bool:
    """**目录子项**判「本项目专属」（比顶层名字更严：只认明确词根 / `etf`＋日期）。"""
    lowered = name.lower()
    if any(token in lowered for token in PROJECT_CHILD_TOKEN_LIST):
        return True
    return bool(PROJECT_CHILD_DIGIT_RE.search(lowered))


def _has_strong_child(path: str) -> list:
    """目录的**直接子项**里有没有命中**归属证据**的名字 ⇒ 返回命中名单。

    🔴 这是「**名字外的形态**」的唯一兜底（深度 2）——
       独立审计抓出的真缺陷形态：`runs/codex_overnight/codex-overnight-etf20261113`
       （顶层目录名毫无本项目特征 ⇒ 只按顶层名字判**报不出来**）。
    🔴 **只扫直接子项**（深度 2）⇒ 有界代价：实测 `runs/` 顶层 132 个目录全扫 **0.4 秒**。
    """
    return [child for child in _listdir(path) if _child_name_is_project(child)]


def analyze(project_dir: str, dataset_root: str = None, results_root: str = None) -> tuple:
    """跑全部检查 ⇒ (defects, facts)。"""
    if not os.path.isdir(project_dir):
        raise InputError("工程根不存在：%s" % project_dir)

    # 🔴 **原始值**要在 `_resolve_root` **之前**留档（它会 `abspath` ⇒ 原始形态就丢了）
    explicit_dataset = dataset_root
    explicit_results = results_root
    dataset_root = _resolve_root(dataset_root, ENV_DATASET_ROOT, DEFAULT_DATASET_ROOT)
    results_root = _resolve_root(results_root, ENV_RESULTS_ROOT, DEFAULT_RESULTS_ROOT)

    facts = PlacementFacts(
        project_dir=os.path.abspath(project_dir),
        dataset_root=dataset_root,
        results_root=results_root,
    )
    defects = []

    # ================= A1 双根自洽（跨主机都适用） =================
    raw_dataset = _raw_root(explicit_dataset, ENV_DATASET_ROOT)
    raw_results = _raw_root(explicit_results, ENV_RESULTS_ROOT)
    for label, raw in (("<DATASET_ROOT>", raw_dataset), ("<RESULTS_ROOT>", raw_results)):
        if raw and not os.path.isabs(os.path.expanduser(raw)):
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/双根非绝对路径",
                    reason=(
                        "`%s` 的**原始值** `%s` **不是绝对路径** ⇒ `_resolve_root` 会把它"
                        "静默变成「相对当前工作目录」⇒ 落点随 cwd 漂"
                        "（`rules.md §十六` 第 1 条）" % (label, raw)
                    ),
                    preview="%s ｜ %s" % (dataset_root, results_root),
                )
            )
    if dataset_root == results_root:
        defects.append(
            Defect(
                file=DUAL_ENV_FILE,
                line_no=0,
                check="A1/双根合一",
                reason=(
                    "`<DATASET_ROOT>` 与 `<RESULTS_ROOT>` **不许是同一个目录**"
                    "（双根化 = 数据集根 + 结果根两个；`rules.md §十六` 第 1 条）"
                ),
                preview=dataset_root,
            )
        )
    if _is_under(dataset_root, results_root) or _is_under(results_root, dataset_root):
        defects.append(
            Defect(
                file=DUAL_ENV_FILE,
                line_no=0,
                check="A1/双根互相嵌套",
                reason=(
                    "`<DATASET_ROOT>` 与 `<RESULTS_ROOT>` **不许互相嵌套**"
                    "⇒ 清理其中一个会连带另一个（`rules.md §十六` 第 1 条「落点唯一化」）"
                ),
                preview="%s ｜ %s" % (dataset_root, results_root),
            )
        )
    # 🔴🔴 **2026-11-20 复审补（真缺陷）**：双根**落进 `/tmp`** 之前**没人管**
    #   ⇒ `--dataset-root /tmp/xxx` ⇒ A1 **零缺陷** ⇒ 产物直接落 `/tmp`
    #   ⇒ 与第 4 条「`/tmp` 里的本项目件必须清零」**自相矛盾**
    #   （且 A5 只看 `/tmp` **顶层**条目的**名字/子项名** ⇒ `/tmp/xxx` 里的本项目件**看不见**）。
    for name, root in (("<DATASET_ROOT>", dataset_root), ("<RESULTS_ROOT>", results_root)):
        if _is_under(root, TMP_ROOT):
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/双根落在 /tmp 之下",
                    reason=(
                        "`%s` 落在 `%s` 之下 ⇒ 产物落进 `/tmp` ⇒ 与第 4 条"
                        "「`/tmp` 里的本项目件必须清零」**自相矛盾**"
                        "（`rules.md §十六` 第 1/4 条）" % (name, TMP_ROOT)
                    ),
                    preview=root,
                )
            )
    for shared in (MOUNT_ROOT, SIPING_ROOT, DATASETS_SHARED_ROOT, RUNS_SHARED_ROOT):
        if dataset_root == shared or results_root == shared:
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/双根落在散落旧位置",
                    reason=(
                        "双根之一**等于共享顶层 `%s`** ⇒ 落点退化回「散落的旧位置」"
                        "（`rules.md §十六` 第 2/3 条：共享目录里只许有本项目的**专属**目录）"
                    )
                    % shared,
                    preview="%s ｜ %s" % (dataset_root, results_root),
                )
            )
    project_abs = os.path.abspath(project_dir)
    for name, root in (("<DATASET_ROOT>", dataset_root), ("<RESULTS_ROOT>", results_root)):
        if _is_under(project_abs, root):
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/仓库落在双根内",
                    reason=(
                        "工程工作区落在 `%s` 之下 ⇒ 跑产物会混进源码树（`rules.md §十六` 第 1 条）"
                        % name
                    ),
                    preview="%s ⊂ %s" % (project_abs, root),
                )
            )
        if _is_under(root, project_abs):
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/双根落在仓库内",
                    reason=(
                        "`%s` 落在工程工作区之下 ⇒ 产物会进版本库（撞 §十四.93「版本库只放源」）"
                        % name
                    ),
                    preview="%s ⊂ %s" % (root, project_abs),
                )
            )
    # 双根若落在共享目录里 ⇒ 必须是规定的那一层
    if _is_under(results_root, RUNS_SHARED_ROOT):
        if os.path.dirname(results_root) != RUNS_SHARED_ROOT:
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/结果根层级不对",
                    reason=(
                        "`<RESULTS_ROOT>` 落在 `runs/` 之下时**必须是它的直接子目录**"
                        "（`rules.md §十六` 第 2/3 条：共享目录里只许有本项目的**一个**专属目录）"
                    ),
                    preview=results_root,
                )
            )
    if _is_under(dataset_root, DATASETS_SHARED_ROOT):
        relative = os.path.relpath(dataset_root, DATASETS_SHARED_ROOT)
        if relative.count(os.sep) < 1:
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A1/数据集根层级不对",
                    reason=(
                        "`<DATASET_ROOT>` 落在 `datasets/` 之下时**必须是 `datasets/<命名空间>/<项目>`**"
                        "（`rules.md §十六` 第 2 条：顶层**不许**建本项目目录）"
                    ),
                    preview=dataset_root,
                )
            )
        else:
            facts.datasets_namespace = relative.split(os.sep)[0]

    # ================= 服务器侧断言（A2/A3/A4） =================
    server_scope = os.path.isdir(RUNS_SHARED_ROOT) and os.path.isdir(MOUNT_ROOT)
    facts.server_scope = server_scope
    if not server_scope:
        facts.not_applicable.append("A2/A3/A4/A5（无服务器根 `/mnt/data/siping/runs`）")

    # ---- A2 断言②a：`/mnt/data` 根没有 `_etf_daily_*` ----
    if server_scope:
        legacy = [name for name in _listdir(MOUNT_ROOT) if name.startswith(LEGACY_WORK_PREFIX)]
        facts.legacy_work_dirs = len(legacy)
        for name in legacy:
            defects.append(
                Defect(
                    file="%s/%s" % (MOUNT_ROOT, name),
                    line_no=0,
                    check="A2/散落旧位置",
                    reason=(
                        "`/mnt/data` 根**存在** `_etf_daily_*` ⇒ 违反落点唯一化"
                        "（`rules.md §十六` 第 2 条；历史违例实例 `_etf_daily_20261105/06/09`）"
                    ),
                    preview=os.path.join(MOUNT_ROOT, name),
                )
            )

    # ---- A3 断言②b：`datasets/` 顶层没有本项目目录 ----
    if server_scope:
        namespace = facts.datasets_namespace or DATASET_NAMESPACE_DEFAULT
        for name in _listdir(DATASETS_SHARED_ROOT):
            path = os.path.join(DATASETS_SHARED_ROOT, name)
            if facts.datasets_namespace and name == facts.datasets_namespace:
                # 🔴 `datasets/<命名空间>/` 是**本项目命名空间根**，§十六 第 1 条明写允许
                #   （🔴 只有**真的**声明出来（`facts.datasets_namespace` 非空）才豁免 ——
                #   否则 `datasets/manu` 这个**容器**会被下面的容器规则接管）
                continue
            lowered = name.lower()
            # 🔴🔴 **2026-11-20 复审修（越权收敛）**：**只认名字**。
            #   原先「共用词元（`manu` / `qlib`）⇒ 再取证（子项命中归属证据）」⇒
            #   会把**整棵多链共享的命名空间**（实测 `datasets/manu/` 有 **97** 条，
            #   本项目只占 **1** 条 `etf_rotation`）判成「本项目目录」⇒ **越权**。
            #   ⇒ **容器 ≠ 本项目目录** ⇒ 容器型目录**一律豁免**；
            #   「容器里的本项目目录」由 **A1b** 判（那里按**子项**口径，只认明确词根）。
            if not _name_is_project(name):
                continue
            facts.datasets_project_dirs += 1
            defects.append(
                Defect(
                    file=path,
                    line_no=0,
                    check="A3/datasets 顶层本项目目录",
                    reason=(
                        "`siping/datasets/` 顶层**存在本项目目录**（强词元命中 `%s`）"
                        "⇒ 违反落点唯一化（`rules.md §十六` 第 2 条；"
                        "本项目只许用命名空间 `%s/` 之下）"
                        % (lowered, namespace)
                    ),
                    preview=path,
                )
            )
        # 历史违例实例：`datasets/qlib/{cn_data_live,_stage1_work}`（qlib 这个名字不专属 ⇒ 单判内容）
        legacy_qlib = os.path.join(DATASETS_SHARED_ROOT, "qlib")
        if os.path.isdir(legacy_qlib):
            for sub in LEGACY_QLIB_SUBDIRS:
                path = os.path.join(legacy_qlib, sub)
                if os.path.exists(path):
                    facts.datasets_project_dirs += 1
                    defects.append(
                        Defect(
                            file=path,
                            line_no=0,
                            check="A3/datasets 顶层本项目目录",
                            reason=(
                                "`datasets/qlib/%s` 是本项目的自建增量日派生库"
                                "⇒ 落点**不许**留在 `datasets/` 顶层（`rules.md §十六` 第 2 条）"
                                % sub
                            ),
                            preview=path,
                        )
                    )

    # ---- A1b：命名空间根下**本项目专属目录只许 `<DATASET_ROOT>` 一个** ----
    #   🔴🔴 **2026-11-20 复审补（真缺陷）**：第 1 条「本项目文件**全部位于**双根之下」
    #   在**命名空间内部**从来没实现过 ⇒ `datasets/manu/etf_rotation_v2/` 这种
    #   **第二个本项目目录**（同一命名空间下）**闸门零缺陷**。
    if server_scope:
        allowed_abs = os.path.normpath(dataset_root)
        allowed_name = os.path.basename(allowed_abs)
        # 🔴 扫**所有**命名空间容器（不只已声明的那个）⇒ 「本项目目录落在**未声明**的
        #   命名空间下」也能报出来（实测服务器：30 个容器全扫 ⇒ **只有**
        #   `datasets/manu/etf_rotation` 命中 ⇒ **零误伤**）
        for container in _listdir(DATASETS_SHARED_ROOT):
            container_path = os.path.join(DATASETS_SHARED_ROOT, container)
            if not os.path.isdir(container_path):
                continue
            for name in _listdir(container_path):
                path = os.path.join(container_path, name)
                if not os.path.isdir(path):
                    continue
                if os.path.normpath(path) == allowed_abs:
                    continue
                # 🔴 子项口径**比顶层更严**：只认**明确词根** / `etf`＋日期
                #   ⇒ 别的链的目录（`anti-uav*` / `arm_*` / `gmc_*` / `AVTrack` …）**不计**
                if not _child_name_is_project(name):
                    continue
                facts.datasets_namespace_project_dirs += 1
                defects.append(
                    Defect(
                        file=path,
                        line_no=0,
                        check="A1b/命名空间里多出本项目目录",
                        reason=(
                            "命名空间根 `%s/` 下**多出本项目专属目录** `%s`"
                            "（子项名命中**归属证据**）⇒ 本项目目录**只许** `<DATASET_ROOT>`"
                            "（`%s`）**一个**（`rules.md §十六` 第 1/4 条："
                            "本项目文件**全部位于**双根之下）"
                            % (container, name, allowed_name)
                        ),
                        preview=path,
                    )
                )

    # ---- A4 断言③：`runs/` 顶层本项目专属目录只许 `<RESULTS_ROOT>` 一个 ----
    if server_scope:
        allowed = ""
        if os.path.dirname(results_root) == RUNS_SHARED_ROOT:
            allowed = os.path.basename(results_root)
        for name in _listdir(RUNS_SHARED_ROOT):
            path = os.path.join(RUNS_SHARED_ROOT, name)
            if allowed and name == allowed:
                continue
            lowered = name.lower()
            strong = _name_is_project(name)
            shared = _name_is_shared(name)
            if not strong and shared:
                # 🔴 共用词元（`manu` / `qlib`）⇒ **必须再取证**才算本项目
                strong = bool(_has_strong_child(path))
            if strong:
                facts.runs_project_dirs += 1
                defects.append(
                    Defect(
                        file=path,
                        line_no=0,
                        check="A4/runs 顶层多出本项目目录",
                        reason=(
                            "`siping/runs/` 顶层**多出本项目专属目录**（强词元命中 `%s`）"
                            "⇒ 共享目录里本项目**只许有** `<RESULTS_ROOT>` 一个"
                            "（`rules.md §十六` 第 2/3 条；别人的链不计、也不许删）"
                            % lowered
                        ),
                        preview=path,
                    )
                )
                continue
            # ---- A4b：**深度 2** 兜底（本项目专属目录藏在**共享**目录的下一层） ----
            nested = _has_strong_child(path)
            if nested:
                facts.runs_nested_project_dirs += 1
                defects.append(
                    Defect(
                        file=path,
                        line_no=0,
                        check="A4b/共享目录里藏着本项目专属目录",
                        reason=(
                            "`siping/runs/` 顶层的**共享目录** `%s/` 下一层**藏着本项目专属目录**"
                            "（子项名命中**归属证据**：%s）⇒ 本项目文件**只许存在于**双根之下"
                            "（`rules.md §十六` 第 4 条；**别人的链的件不许碰**——只报本项目的）"
                            % (name, "、".join(nested[:3]))
                        ),
                        preview=path,
                    )
                )
                continue
            facts.runs_other_chains += 1

    # ================= A5 痕迹清零（`/tmp`） =================
    if server_scope:
        current_uid = os.getuid()
        # 🔴 **内容标记**：只用**链专属**标记（仓库根目录名 / `manu/memory` 是**多链共用**的
        #    ⇒ 当标记会把**别人的链**的件误判成「本项目件」⇒ 越权，**不许**加）
        content_markers = tuple(CONTENT_MARKERS)
        content_markers_path_form = tuple(CONTENT_MARKERS_PATH_FORM)
        for name in _listdir(TMP_ROOT):
            path = os.path.join(TMP_ROOT, name)
            if any(name.startswith(prefix) for prefix in TMP_EXEMPT_PREFIXES):
                facts.tmp_exempt_items += 1
                continue
            # 🔴 **登记制豁免只挡「名字口径」，不挡「内容口径」**
            #   ⇒ 豁免名里若真塞进本项目数据（`etf_rotation` 等标记）⇒ **照样报 A5**
            #   ⇒ 豁免**不会**变成「同名即隐身」的洞（`pytest-of-*` 前缀豁免没这个性质）。
            name_exempt = name in TMP_EXEMPT_NAMES
            if name_exempt:
                facts.tmp_exempt_items += 1
            if os.path.abspath(path) == project_abs:
                continue
            try:
                stat_result = os.stat(path)
            except OSError:
                continue
            if stat_result.st_uid != current_uid:
                facts.tmp_other_uid_items += 1
                continue
            lowered = name.lower()
            hidden = name.startswith(".")
            # ---- 判据 1：**名字口径**（顶层条目名命中**强词元**） ----
            #   🔴 **隐藏项只免名字口径**（`.manu_known_hosts` = ssh 信任清单 = 配置件不是产物），
            #   **内容口径照判**（隐藏的产物照样是痕迹）。
            strong_name = (not hidden) and (not name_exempt) and _name_is_project(name)
            shared_name = (not hidden) and _name_is_shared(name)
            # ---- 判据 2：**内容口径**（顶层普通文件 / **目录的直接子文件**） ----
            #   🔴🔴 **2026-11-20 复审补（真缺陷）**：原先**只对顶层普通文件**生效
            #   （`os.path.isfile(path)`）⇒ **目录里的本项目件静默漏检**
            #   （实测现场漏掉 `/tmp/inv/`（5 件）与 `/tmp/supplement.txt/`（1 件）
            #     ⇒ 闸门报「`/tmp` 本项目件 = 0」＝ **假绿**）。
            #   ⇒ 现在**目录也走内容口径**：只扫**直接子文件**（≤ `MAX_DIR_CHILD_SCAN`）
            #     且**只认「路径形态」标记** ⇒ 防越权（别的链的目录提到一次词不算）。
            content_hit = False
            content_basis = ""
            if os.path.isfile(path):
                if _content_marker_hit_file(path, stat_result.st_size, content_markers):
                    content_hit = True
                    content_basis = "**内容**命中项目标记"
            else:
                if _content_marker_hit_dir(path, content_markers_path_form):
                    content_hit = True
                    content_basis = "**目录内直接子文件内容**命中项目标记"
                    facts.tmp_content_dir_items += 1
            # ---- 判据 3：**目录口径**（目录的直接子项名命中**强**词元） ----
            dir_hit = False
            if os.path.isdir(path):
                dir_hit = bool(_has_strong_child(path))
            # ---- 共用词元（`manu` / `qlib`）⇒ **必须再取证**才算本项目 ----
            corroborated = shared_name and (content_hit or dir_hit)
            name_hit = strong_name or corroborated
            if not (name_hit or content_hit or dir_hit):
                facts.tmp_other_chain_items += 1
                continue
            facts.tmp_project_items += 1
            basis = []
            if name_hit:
                basis.append("名字命中 `%s`" % lowered)
            if content_hit:
                basis.append(content_basis)
            if dir_hit:
                basis.append("子目录项名命中**强**词元")
            defects.append(
                Defect(
                    file=path,
                    line_no=0,
                    check="A5//tmp 痕迹未清零",
                    reason=(
                        "`/tmp` 顶层**还有本项目的件**（%s）"
                        "⇒ 违反痕迹清零（`rules.md §十六` 第 4 条 2026-11-19 夜补）"
                        "；`/tmp/pytest-of-*` 与 `pycharm_project_*` **故意不动**"
                        % " ＋ ".join(basis)
                    ),
                    preview=path,
                )
            )

    # ================= A6 落点字面量必须由双 env 派生（静态） =================
    allowed_prefixes = tuple(
        os.path.normpath(root)
        for root in (
            dataset_root,
            results_root,
            DEFAULT_DATASET_ROOT,
            DEFAULT_RESULTS_ROOT,
        )
    )
    for relative in OWNER_FILES:
        absolute = os.path.join(project_dir, relative)
        if not os.path.isfile(absolute):
            defects.append(
                Defect(
                    file=relative,
                    line_no=0,
                    check="A6/owner 文件缺失",
                    reason=(
                        "§十六 末尾的 owner 清单里的文件**不存在** ⇒ 落点字面量无源可查"
                        "（`rules.md §十六` 落地位置清单）"
                    ),
                    preview=relative,
                )
            )
            continue
        facts.owner_files += 1
        for lineno, literal in _owner_code_literals(absolute):
            if "manu/report" in literal and (
                literal.startswith("/") or literal.startswith("manu/")
            ):
                defects.append(
                    Defect(
                        file=relative,
                        line_no=lineno,
                        check="A6/报告落点回仓库",
                        reason=(
                            "代码里的落点字面量指向**仓库内 `manu/report/`**"
                            "⇒ 报告真值必须落 `<RESULTS_ROOT>/reports/`（`rules.md §十六` 第 3 条）"
                        ),
                        preview=literal,
                    )
                )
                continue
            # 🔴 **裸 `/mnt/data`** 也是违例（§十六 第 2 条点名的历史违例原形：
            #    `fetch_data.py` 的 `_WORK_ROOT = Path("/mnt/data")`）⇒ **等号也要进判定**
            if literal != MOUNT_ROOT and not literal.startswith(MOUNT_ROOT + "/"):
                continue
            normalized = os.path.normpath(literal)
            if normalized in ALLOWED_EXTERNAL_LITERALS:
                continue
            if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in allowed_prefixes):
                facts.owner_literals += 1
                continue
            facts.owner_literals += 1
            defects.append(
                Defect(
                    file=relative,
                    line_no=lineno,
                    check="A6/落点字面量越界",
                    reason=(
                        "落点字面量 `%s` **既不在 `<DATASET_ROOT>` 也不在 `<RESULTS_ROOT>` 之下**"
                        "⇒ 违反落点唯一化（`rules.md §十六` 第 1/2 条："
                        "全部改成由**双 env** 派生）" % literal
                    ),
                    preview=literal,
                )
            )

    # ---- A6b：双 env 派生本身必须还在 ----
    dual_env_path = os.path.join(project_dir, DUAL_ENV_FILE)
    if os.path.isfile(dual_env_path):
        env_names = _dual_env_literals(dual_env_path)
        facts.owner_dual_env_ok = ENV_DATASET_ROOT in env_names and ENV_RESULTS_ROOT in env_names
        if not facts.owner_dual_env_ok:
            defects.append(
                Defect(
                    file=DUAL_ENV_FILE,
                    line_no=0,
                    check="A6/双 env 派生被破坏",
                    reason=(
                        "`%s` 里查不到**双 env**（`%s` / `%s`）⇒ 落点不再可整体搬家"
                        "（`rules.md §十六` 末尾：全部改成由**双 env** 派生）"
                        % (DUAL_ENV_FILE, ENV_DATASET_ROOT, ENV_RESULTS_ROOT)
                    ),
                    preview="｜".join(sorted(env_names & {ENV_DATASET_ROOT, ENV_RESULTS_ROOT})) or "（无）",
                )
            )

    return defects, facts


def build_payload(defects: list, facts: PlacementFacts) -> dict:
    """结构化结果（供 `--json-out`）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": facts.project_dir,
        "summary": {
            "defect_count": len(defects),
            "dataset_root": facts.dataset_root,
            "results_root": facts.results_root,
            "server_scope": facts.server_scope,
            "legacy_work_dirs": facts.legacy_work_dirs,
            "datasets_project_dirs": facts.datasets_project_dirs,
            "datasets_namespace_project_dirs": facts.datasets_namespace_project_dirs,
            "runs_project_dirs": facts.runs_project_dirs,
            "runs_nested_project_dirs": facts.runs_nested_project_dirs,
            "runs_other_chains": facts.runs_other_chains,
            "tmp_project_items": facts.tmp_project_items,
            "tmp_exempt_items": facts.tmp_exempt_items,
            "tmp_other_uid_items": facts.tmp_other_uid_items,
            "tmp_other_chain_items": facts.tmp_other_chain_items,
            "tmp_content_dir_items": facts.tmp_content_dir_items,
            "owner_files": facts.owner_files,
            "owner_literals": facts.owner_literals,
            "owner_dual_env_ok": facts.owner_dual_env_ok,
            "not_applicable": list(facts.not_applicable),
        },
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查「产物落点双根化 ＋ 痕迹清零」的验收断言"
        "（rules.md §十六 第 2/3/4/7 条）。"
    )
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR, help="工程根目录")
    parser.add_argument("--dataset-root", default=None, help="覆盖 `<DATASET_ROOT>`（默认取 env）")
    parser.add_argument("--results-root", default=None, help="覆盖 `<RESULTS_ROOT>`（默认取 env）")
    parser.add_argument("--json-out", default=None, help="可选：把结构化结果写到这个 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list = None) -> int:
    args = parse_args(argv)
    try:
        defects, facts = analyze(args.project_dir, args.dataset_root, args.results_root)
    except InputError as exc:
        print("[ERROR] %s" % exc, file=sys.stderr)
        return 2

    print(
        "[落点] 双根自洽=%s｜服务器根可见=%s｜数据集根=%s｜结果根=%s"
        % (
            "否" if any(d.check.startswith("A1/") for d in defects) else "是",
            "是" if facts.server_scope else "否",
            facts.dataset_root,
            facts.results_root,
        )
    )
    if facts.server_scope:
        print(
            "[断言②] `/mnt/data` 根 `_etf_daily_*` = %d 个｜`datasets/` 顶层本项目目录 = %d 个"
            "｜命名空间内多出的本项目目录 = %d 个"
            % (
                facts.legacy_work_dirs,
                facts.datasets_project_dirs,
                facts.datasets_namespace_project_dirs,
            )
        )
        print(
            "[断言③] `runs/` 顶层本项目专属目录 = %d 个（只许 1 个）"
            "｜共享目录里藏的本项目专属目录 = %d 个｜别人的链 = %d 个（不计、不碰）"
            % (facts.runs_project_dirs, facts.runs_nested_project_dirs, facts.runs_other_chains)
        )
        print(
            "[痕迹清零] `/tmp` 本项目件 = %d 个（归属证据名字 ∪ 内容标记 ∪ 目录子项归属证据）"
            "｜pytest/工作副本豁免 = %d 个｜他 uid 的件 = %d 个（不计、不碰）"
            "｜本 uid 别的链的件 = %d 个（不计、不碰）"
            "｜其中**目录内子文件内容**命中 = %d 个"
            % (
                facts.tmp_project_items,
                facts.tmp_exempt_items,
                facts.tmp_other_uid_items,
                facts.tmp_other_chain_items,
                facts.tmp_content_dir_items,
            )
        )
    else:
        print("[不适用] 服务器侧断言（② / ③ / `/tmp` 痕迹清零）**未执行**：本机没有 `/mnt/data/siping/runs`")
    print(
        "[owner] 文件 %d 个｜落点字面量 %d 条｜双 env 派生=%s"
        % (facts.owner_files, facts.owner_literals, "在" if facts.owner_dual_env_ok else "不在")
    )
    for defect in defects:
        location = "%s:%d" % (defect.file, defect.line_no) if defect.line_no else defect.file
        print("[DEFECT] %s %s｜%s｜%s" % (location, defect.check, defect.reason, defect.preview))
    print("合计：缺陷 %d 个" % len(defects))

    if args.json_out:
        payload = build_payload(defects, facts)
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
