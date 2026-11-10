#!/usr/bin/env python3
"""指纹登记表（`_ms5_md5.txt`）**安全重生成**器。

口径来源：`manu/memory/rules.md` §十四.92 第 7 条 / §十四.93 第 6 条 ——

  ① **先备份**当前指纹表；
  ② **从当前（已知良好）版本重建**（不许从更老的备份重建 —— 实测事故：会把新登记项丢掉）；
  ③ **硬断言登记路径多重集合完全一致**（只许改值，不许增删）；
  ④ **逐条复验**哈希与磁盘一致；
  ⑤ 缺文件即**中止**（不许写默认值 / 空值）。

表结构（74 行量级）：
  * 顶部若干 ``#`` 注释块（每轮重生成追加一块，说明这轮改了什么）；
  * **md5 段**：``<32 hex>  <路径>``，路径相对**仓库根**（源码/文档）；
  * **sha1 段**：``<40 hex>  <路径>``，路径相对**产物根**（交付件基线）。

用法（服务器）：
    python3 manu/tools/regen_fingerprint_table.py \
        --artifact-root <落点根>/runs \
        [--table <表路径>] [--dry-run]

退出码：0 = 重生成成功（值全部与磁盘一致）；1 = 校验失败 / 缺文件 / 路径集合漂移。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

#: 🔴 **结果落点根**：单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT` ⇒ 本工具按**同一个环境变量**取值（两边不会分叉）。
_RESULTS_ROOT = os.environ.get(
    "ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results"
)
#: 产物根缺省值（= 结果根；与 `daily_config.RESEARCH_RUN_ROOT` 同口径）
_DEFAULT_ARTIFACT_ROOT = _RESULTS_ROOT

#: 快照根目录名（架构迁移后：交付件在 `<canonical>/snapshots/<stamp>/`）
SNAPSHOT_DIR_NAME = "snapshots"
#: 快照目录的标志文件（存在 ⇒ 该目录是**不可变快照**，不许往里写任何东西）
SNAPSHOT_MANIFEST = "snapshot_manifest.json"
#: 指纹表文件名（canonical 根的「代码状态登记表」）
TABLE_NAME = "_ms5_md5.txt"
ENTRY_MD5 = re.compile(r"^(?P<hash>[0-9a-f]{32})\s{2}(?P<path>\S+)\s*$")
ENTRY_SHA1 = re.compile(r"^(?P<hash>[0-9a-f]{40})\s{2}(?P<path>\S+)\s*$")


def _digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _repo_root_default() -> Path:
    # manu/tools/regen_fingerprint_table.py ⇒ 仓库根 = 上溯两级
    return Path(__file__).resolve().parents[2]


def _is_snapshot_dir(path: Path) -> bool:
    """该目录是不是**不可变快照**（有 manifest ⇒ 是）。"""
    return (path / SNAPSHOT_MANIFEST).is_file()


def _resolve_table_path(artifact_root: Path, explicit: str) -> Path:
    """指纹表落点解析（🔴 指纹表是 canonical 根的登记表，**不在快照里**）。

    优先级：
    ① ``--table`` 显式指定；
    ② ``<artifact-root>/_ms5_md5.txt``（旧口径：artifact-root 本身就是 canonical 根）；
    ③ artifact-root 在 ``<canonical>/snapshots/<stamp>/`` 下 ⇒ ``<canonical>/_ms5_md5.txt``；
    ④ 兜底：``<artifact-root>/../_ms5_md5.txt``。
    """
    if explicit:
        return Path(explicit)
    direct = artifact_root / TABLE_NAME
    if direct.is_file():
        return direct
    candidates: list[Path] = []
    if artifact_root.parent.name == SNAPSHOT_DIR_NAME:
        candidates.append(artifact_root.parent.parent / TABLE_NAME)
    candidates.append(artifact_root.parent / TABLE_NAME)
    for cand in candidates:
        if cand.is_file():
            return cand
    return direct


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="指纹登记表安全重生成（只改值、不增删）")
    ap.add_argument("--artifact-root", default=str(_DEFAULT_ARTIFACT_ROOT),
                    help="产物根（sha1 段的相对路径基准；🔴 快照目录只作只读基准，不许写）")
    ap.add_argument("--table", default="",
                    help="指纹表路径（缺省 = <产物根>/_ms5_md5.txt；不在那儿则回落 canonical 根）")
    ap.add_argument("--repo-root", default="", help="仓库根（md5 段的相对路径基准）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--no-comment", action="store_true", help="不追加本轮说明注释块")
    args = ap.parse_args(argv)

    artifact_root = Path(args.artifact_root).resolve()
    table = _resolve_table_path(artifact_root, args.table)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else _repo_root_default()

    # 🔴 快照不可变守卫 ①：指纹表**与它的备份**都不许落进快照目录
    #    （架构迁移后交付件在快照里；往快照里写登记表 = 破了「跑完即冻结」不变式）
    if _is_snapshot_dir(table.parent):
        print(f"[FAIL] 指纹表落点位于**不可变快照**目录：{table.parent}"
              f"\n       ⇒ 快照跑完即冻结，不许写入；请用 --table 指向 canonical 根的指纹表"
              f"（缺省 {TABLE_NAME} 应在 <canonical>/ 下）")
        return 2

    # 🔴 快照不可变守卫 ②：--artifact-root **不许是快照目录**。
    #    指纹表的 sha1 段登记的是「**canonical 基线**的交付件」⇒ 以快照为基准
    #    会把 canonical 登记表静默改写成「这一份快照内容」的哈希（口径错位）。
    if _is_snapshot_dir(artifact_root):
        print(f"[FAIL] --artifact-root 指向**不可变快照**目录：{artifact_root}"
              f"\n       ⇒ 指纹表登记的是 canonical 基线的交付件；快照是每次运行的产物"
              f"（不是基线）⇒ 请用 canonical 根（--artifact-root <canonical>）。")
        return 2

    if not table.is_file():
        print(f"[FAIL] 指纹表不存在：{table}")
        return 1

    original = table.read_text(encoding="utf-8")
    lines = original.splitlines()

    md5_entries: list[tuple[int, str, str]] = []   # (行号, 路径, 原值)
    sha1_entries: list[tuple[int, str, str]] = []
    first_entry_line = None
    for idx, line in enumerate(lines):
        m5 = ENTRY_MD5.match(line)
        s1 = ENTRY_SHA1.match(line)
        if m5:
            md5_entries.append((idx, m5.group("path"), m5.group("hash")))
            first_entry_line = idx if first_entry_line is None else first_entry_line
            continue
        if s1:
            sha1_entries.append((idx, s1.group("path"), s1.group("hash")))
            first_entry_line = idx if first_entry_line is None else first_entry_line

    if not md5_entries or not sha1_entries:
        print("[FAIL] 指纹表里没解析出 md5 段 / sha1 段 ⇒ 中止（不许凭空重建）")
        return 1

    # ---- ③ 硬断言：登记路径多重集合（**改值前**先记下来，重建后逐条比对）----
    paths_before = sorted([p for _, p, _ in md5_entries] + [p for _, p, _ in sha1_entries])
    dup = {p for p in paths_before if paths_before.count(p) > 1}
    if dup:
        print(f"[FAIL] 登记路径有重复：{sorted(dup)} ⇒ 中止")
        return 1

    # ---- ⑤ 缺文件即中止（不许写默认值 / 空值）----
    missing: list[str] = []
    for _, rel, _ in md5_entries:
        if not (repo_root / rel).is_file():
            missing.append(f"md5 段：{rel}")
    for _, rel, _ in sha1_entries:
        if not (artifact_root / rel).is_file():
            missing.append(f"sha1 段：{rel}")
    if missing:
        print("[FAIL] 登记项对应文件缺失 ⇒ 中止（不许写默认值/空值）：")
        for m in missing:
            print(f"        {m}")
        return 1

    new_lines = list(lines)
    changed: list[tuple[str, str, str, str]] = []
    for idx, rel, old in md5_entries:
        new = _digest(repo_root / rel, "md5")
        new_lines[idx] = f"{new}  {rel}"
        if new != old:
            changed.append(("md5", rel, old, new))
    for idx, rel, old in sha1_entries:
        new = _digest(artifact_root / rel, "sha1")
        new_lines[idx] = f"{new}  {rel}"
        if new != old:
            changed.append(("sha1", rel, old, new))

    # ---- ③ 硬断言：登记路径多重集合完全一致（重建后逐条复核）----
    paths_after = sorted(
        [m.group("path") for m in (ENTRY_MD5.match(ln) for ln in new_lines) if m]
        + [m.group("path") for m in (ENTRY_SHA1.match(ln) for ln in new_lines) if m]
    )
    if paths_after != paths_before:
        print("[FAIL] 登记路径多重集合变了 ⇒ 中止（只许改值，不许增删）")
        print(f"        before={len(paths_before)} after={len(paths_after)}")
        for extra in sorted(set(paths_after) - set(paths_before)):
            print(f"        + 新增 {extra}")
        for gone in sorted(set(paths_before) - set(paths_after)):
            print(f"        - 丢失 {gone}")
        return 1

    # ---- ④ 逐条复验：写回后再算一遍，必须与表里写的完全一致 ----
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    comment_block = [
        f"# {stamp} 重生成（指纹表安全重生成器 regen_fingerprint_table.py）"
        f"⇒ 登记项集合恒定 {len(paths_before)} 条"
        f"（{len(md5_entries)} md5 + {len(sha1_entries)} sha1）⇒ 只许改值、不许增删",
    ]
    if changed:
        comment_block.append(
            f"#   本轮改值 {len(changed)} 条："
            + "；".join(f"{k}:{p}" for k, p, _o, _n in changed)
        )
    else:
        comment_block.append("#   本轮**零漂移**（全部登记值与磁盘一致）")

    if first_entry_line is not None and not args.no_comment:
        for off, cl in enumerate(comment_block):
            new_lines.insert(first_entry_line + off, cl)

    new_table = "\n".join(new_lines) + "\n"

    print(f"[INFO] 指纹表：{table}")
    print(f"[INFO] 登记项：{len(md5_entries)} md5 + {len(sha1_entries)} sha1 = {len(paths_before)} 条")
    print(f"[INFO] 本轮改值 {len(changed)} 条：")
    for kind, rel, old, new in changed:
        print(f"        [{kind}] {rel}\n              {old} ⇒ {new}")
    if not changed:
        print("        （无）")

    if args.dry_run:
        print("[INFO] --dry-run ⇒ 未写盘")
        return 0

    # ---- ① 先备份（🔴 备份落在**表旁边**，不是 artifact_root ——
    #    artifact_root 可能就是快照 ⇒ 备份落进去就破了不可变不变式）----
    backup = table.parent / f"_backup_preFPRegen_{stamp}_{TABLE_NAME}"
    shutil.copy2(table, backup)
    print(f"[INFO] ① 已备份：{backup.name}")

    tmp = table.with_suffix(table.suffix + ".tmp")
    tmp.write_text(new_table, encoding="utf-8")
    os.replace(tmp, table)
    print(f"[INFO] 已写回：{table}")

    # ---- ④ 写回后逐条复验 ----
    final = table.read_text(encoding="utf-8")
    bad: list[str] = []
    for line in final.splitlines():
        m5 = ENTRY_MD5.match(line)
        s1 = ENTRY_SHA1.match(line)
        if m5:
            p = repo_root / m5.group("path")
            if not p.is_file() or _digest(p, "md5") != m5.group("hash"):
                bad.append(f"md5 {m5.group('path')}")
        elif s1:
            p = artifact_root / s1.group("path")
            if not p.is_file() or _digest(p, "sha1") != s1.group("hash"):
                bad.append(f"sha1 {s1.group('path')}")
    if bad:
        print(f"[FAIL] 写回后复验不一致（{len(bad)} 条）⇒ 中止：{bad}")
        return 1
    print(f"[OK] 写回后逐条复验通过（{len(paths_before)} 条与磁盘一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
