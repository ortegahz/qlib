#!/usr/bin/env python3
"""只读检查「镜像纪律」的**两个口径**：文件层逐字一致 ＋ **git 层成对放行**（`rules.md §十四.125`）。

为什么需要（`rules.md §十四.125`）
    §十四.125 的铁律一/三（用户亲定）：
      ① **镜像对的白名单必须「成对」写**（`manu/<X>` 与 `manu/etf_rotation/<X>` 要么都进、要么都不进）；
      ② **镜像纪律必须验到「git 层」**，不只验「文件层」——文件层 `diff` 一致**不足以**证明成立。
    🔴 **病根（实测）**：此前这两条**只有人守**：每轮靠手工 `diff`/`cmp` 扫一遍，
      而「白名单只放行了一半」这种形态**本地永远测不出来**（两边都在本地）
      ⇒ 提交后仓库里只剩一半 ⇒ **新克隆缺文件，镜像纪律在版本库里断了**
      （§十四.125 记载的真缺陷【二十】即此形态，修过但**没有机器闸门** ⇒ 必然复发）。

判定口径（**期望值全部派生，不手写配对清单**）
    * **配对集合 ＝ 文件系统自动配对 ∪ `.gitignore` 白名单派生配对**
      - 文件系统配对：`manu/etf_rotation/<X>` 与 `manu/<X>` **同相对路径都存在**的文件；
      - 白名单派生：`.gitignore` 里 `!manu/...` / `!manu/etf_rotation/...` 的**具体路径**条目
        ⇒ 它的镜像伙伴也必须存在（**白名单点名了 ⇒ 这一对必须成对**）。
    * **检查 A（成对存在）**：每一对的**两侧都必须存在**
      （只放行了一侧 ＝ 【二十】那个真缺陷的形态 ⇒ 提交后仓库里只有一半）。
    * **检查 B（逐字一致）**：每一对必须**逐字节相同**（豁免见下）。
    * **检查 C（入库状态成对）**：用 `git check-ignore --no-index`（**只认忽略规则、不认索引状态**）
      判「将来提交会不会只进去一半」⇒ 两侧的「会不会被提交」必须一致。
      🔴 **故意不判「索引现状」**：用户明早手动 `git add` ⇒ 「已跟踪 / 未跟踪」是**正常暂存态**，
      拿它当缺陷 ⇒ **恒假红** ⇒ 只判「将来会不会成对进去」。
    * **检查 D（白名单成对）**：两侧「是否被 `.gitignore` **显式放行**（`!` 规则）」必须一致
      （一侧显式放行、另一侧只靠通配 ⇒ 提交后就是一半）。
    * **检查 E（豁免清单新鲜度）**：豁免目录必须在工具里**显式登记**
      （`manu/daily/` ＝ **已登记过期副本**，见 §十四.125 末尾与交接摘要），
      且**必须仍然存在** ⇒ 不许让豁免变成逃逸口。
    * 🔴 **本工具只读**：不写、不改任何被检查的文件（测试里有 md5 前后对照）。

用法
    python3 manu/tools/check_mirror_pairs.py --project-dir .
    python3 manu/tools/check_mirror_pairs.py --project-dir . --json-out /tmp/mirror.json

退出码
    0 = 无缺陷；1 = 有缺陷；2 = 不适用/输入错误（不在 git 工作区、`git` 不可用等）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PROJECT_DIR = "/tmp/pycharm_project_3b68a403"

#: 镜像根：活链在 `manu/etf_rotation/`，镜像在 `manu/`（同相对路径）
MIRROR_ROOT_REL = "manu/etf_rotation"
MIRROR_HEAD_REL = "manu"

#: 🔴 **显式豁免目录**（相对 `manu/`）：`manu/daily/` ＝ **已登记过期副本**
#:    （交接摘要与 §十四.125 末尾均已登记 ⇒ **豁免是登记制，不是「碰巧没扫到」**）。
#:    豁免只作用于**检查 B（逐字一致）**；成对存在 / 入库状态仍要验。
EXEMPT_REL_DIRS = ("daily/",)

#: 跳过的目录（编译产物 / 版本库垃圾）
SKIP_DIR_NAMES = ("__pycache__", ".git", ".pytest_cache")


class InputError(Exception):
    """输入/用法错误 ⇒ 退出码 2。"""


class NotApplicable(Exception):
    """环境不适用（例如不在 git 工作区）⇒ 退出码 2，且**不算不合格**。"""


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
class MirrorFacts:
    """镜像纪律的**实测**口径。"""

    pairs: int = 0
    whitelist_pairs: int = 0
    exempt_pairs: int = 0
    ignored_paths: int = 0
    whitelisted_paths: int = 0
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
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise InputError("读不到待检查文件：%s（%s）" % (path, exc)) from exc
    return hasher.hexdigest()


def _git_check_ignore(project_dir: str, paths: list) -> tuple:
    """一次 `git check-ignore -v --no-index --stdin -z` ⇒ (显式放行表, 忽略表)。

    🔴 `--no-index`：**只认忽略规则、不认索引状态** ⇒ 判「将来提交会不会只进去一半」。
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
            "git check-ignore 不可用（rc=%d）：%s" % (completed.returncode, completed.stderr.decode("utf-8", "replace")[:200])
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


def _collect_fs_pairs(project_dir: str) -> dict:
    """文件系统自动配对：`manu/etf_rotation/<X>` ↔ `manu/<X>`（两侧都存在）。"""
    pairs = {}
    mirror_root = os.path.join(project_dir, MIRROR_ROOT_REL)
    head_root = os.path.join(project_dir, MIRROR_HEAD_REL)
    for dirpath, dirnames, filenames in os.walk(mirror_root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        for name in filenames:
            source = os.path.join(dirpath, name)
            relative = os.path.relpath(source, mirror_root)
            target = os.path.join(head_root, relative)
            if os.path.abspath(target).startswith(os.path.abspath(mirror_root) + os.sep):
                continue
            if not os.path.isfile(target):
                continue
            pairs[relative] = (source, target)
    return pairs


def _collect_whitelist_pairs(project_dir: str) -> dict:
    """`.gitignore` 白名单派生配对：**白名单点名了 ⇒ 这一对必须成对存在**。

    🔴 只认**具体路径**条目（带 `*`/`?` 的通配条目不派生配对：
       例如 `!manu/memory/records/*.csv` 是**单体载体**（无镜像伙伴），不是缺陷）。
    """
    gitignore = os.path.join(project_dir, ".gitignore")
    if not os.path.isfile(gitignore):
        return {}
    pairs = {}
    for raw in read_text(gitignore).splitlines():
        line = raw.strip()
        if not line.startswith("!"):
            continue
        pattern = line[1:].strip()
        if not pattern or "*" in pattern or "?" in pattern:
            continue
        if pattern.startswith(MIRROR_ROOT_REL + "/"):
            relative = pattern[len(MIRROR_ROOT_REL) + 1:]
            pairs[relative] = (
                os.path.join(project_dir, MIRROR_ROOT_REL, relative),
                os.path.join(project_dir, MIRROR_HEAD_REL, relative),
            )
        elif pattern.startswith(MIRROR_HEAD_REL + "/"):
            relative = pattern[len(MIRROR_HEAD_REL) + 1:]
            pairs[relative] = (
                os.path.join(project_dir, MIRROR_ROOT_REL, relative),
                os.path.join(project_dir, MIRROR_HEAD_REL, relative),
            )
    return pairs


def analyze(project_dir: str) -> tuple:
    """总入口 ⇒ (缺陷列表, 镜像实测口径)。"""
    head_root = os.path.join(project_dir, MIRROR_HEAD_REL)
    if not os.path.isdir(head_root):
        raise InputError("找不到镜像头目录：%s" % head_root)

    pairs = _collect_fs_pairs(project_dir)
    whitelist_pairs = _collect_whitelist_pairs(project_dir)
    for relative, pair in whitelist_pairs.items():
        pairs.setdefault(relative, pair)

    defects: list = []

    # ── 检查 E：豁免清单必须仍然存在（不许让豁免变成逃逸口）────────────
    for exempt in EXEMPT_REL_DIRS:
        path = os.path.join(head_root, exempt.rstrip("/"))
        if not os.path.exists(path):
            defects.append(
                Defect(
                    MIRROR_HEAD_REL + "/" + exempt.rstrip("/"),
                    0,
                    "E-豁免清单陈旧",
                    "登记的豁免目录 %s 已不存在 ⇒ 豁免清单必须同步订正（不许留着当逃逸口）" % exempt,
                    exempt,
                )
            )

    # ── 检查 A：成对存在 ───────────────────────────────────────────────
    for relative, (source, target) in sorted(pairs.items()):
        missing = [p for p in (source, target) if not os.path.isfile(p)]
        if missing:
            defects.append(
                Defect(
                    MIRROR_ROOT_REL + "/" + relative,
                    0,
                    "A-成对存在",
                    "镜像对缺一侧：%s ⇒ 提交后仓库里只有一半（§十四.125 真缺陷【二十】的形态）"
                    % "、".join(os.path.relpath(p, project_dir) for p in missing),
                    relative,
                )
            )

    # ── 检查 B：逐字一致（豁免目录除外）──────────────────────────────
    exempt_pairs = 0
    for relative, (source, target) in sorted(pairs.items()):
        if not (os.path.isfile(source) and os.path.isfile(target)):
            continue
        if relative.startswith(EXEMPT_REL_DIRS):
            exempt_pairs += 1
            continue
        if file_digest(source) != file_digest(target):
            defects.append(
                Defect(
                    MIRROR_ROOT_REL + "/" + relative,
                    0,
                    "B-逐字一致",
                    "镜像对两侧内容不一致 ⇒ 改一边必须同步另一边",
                    relative,
                )
            )

    # ── 检查 C / D：git 层（入库状态 + 白名单）必须成对 ────────────────
    all_paths = sorted({p for pair in pairs.values() for p in pair})
    whitelisted, ignored = _git_check_ignore(project_dir, all_paths)
    for relative, (source, target) in sorted(pairs.items()):
        if not (os.path.isfile(source) and os.path.isfile(target)):
            continue
        source_ignored = source in ignored
        target_ignored = target in ignored
        if source_ignored != target_ignored:
            defects.append(
                Defect(
                    MIRROR_ROOT_REL + "/" + relative,
                    0,
                    "C-入库状态成对",
                    "两侧「将来会不会被提交」不一致（%s 被忽略 / %s 不被忽略）⇒ 提交后仓库里只有一半"
                    % (
                        os.path.relpath(source if source_ignored else target, project_dir),
                        os.path.relpath(target if source_ignored else source, project_dir),
                    ),
                    relative,
                )
            )
        source_whitelisted = source in whitelisted
        target_whitelisted = target in whitelisted
        if source_whitelisted != target_whitelisted:
            defects.append(
                Defect(
                    MIRROR_ROOT_REL + "/" + relative,
                    0,
                    "D-白名单成对",
                    "两侧「是否被 .gitignore 显式放行」不一致 ⇒ 白名单必须成对写（§十四.125 铁律一）",
                    relative,
                )
            )

    facts = MirrorFacts(
        pairs=len(pairs),
        whitelist_pairs=len(whitelist_pairs),
        exempt_pairs=exempt_pairs,
        ignored_paths=len(ignored),
        whitelisted_paths=len(whitelisted),
        sample=sorted(pairs)[:5],
    )
    return defects, facts


def build_payload(defects: list, facts: MirrorFacts, project_dir: str) -> dict:
    """结构化结果（供 `--json-out`）。"""
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_dir": project_dir,
        "summary": {
            "defect_count": len(defects),
            "pairs": facts.pairs,
            "whitelist_pairs": facts.whitelist_pairs,
            "exempt_pairs": facts.exempt_pairs,
            "ignored_paths": facts.ignored_paths,
            "whitelisted_paths": facts.whitelisted_paths,
        },
        "defects": [defect.as_dict() for defect in defects],
    }


def parse_args(argv: list) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读检查「镜像纪律」（文件层逐字一致 ＋ git 层成对放行，rules.md §十四.125）。"
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
        "[镜像] 配对 %d 组（其中白名单派生 %d 组 / 豁免 %d 组）｜显式放行 %d 条｜被忽略 %d 条"
        % (facts.pairs, facts.whitelist_pairs, facts.exempt_pairs, facts.whitelisted_paths, facts.ignored_paths)
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
