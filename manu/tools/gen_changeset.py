#!/usr/bin/env python
"""🔴 生成 CHANGESET.md（服务器侧代码改动清单 + 每文件 md5 + **完整 diff**）。

用途：主机回灌对账（``pull_from_server.sh`` 只带工作树，diff 让人能逐行看改了什么）。

基线口径：
  * ``manu/etf_rotation/final_rebalance_plan.py`` ⇒ 基线取
    ``/mnt/data/siping/runs/etf_rotation_results/_backup_pre_pctfix_20261117/final_rebalance_plan.py``；
  * 其余**本夜班新增/修改**的文件 ⇒ 基线取 ``/dev/null``（全量新增）；
  * 改动判定：mtime ≥ ``--since``（默认 2026-11-17 17:35）且在 ``--scope`` 目录下。

用法：
    python manu/tools/gen_changeset.py --out /mnt/data/siping/runs/etf_rotation_results/codex_overnight/CHANGESET.md
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import os
from pathlib import Path

PROJ = Path("/tmp/pycharm_project_3b68a403")
RESULTS = Path("/mnt/data/siping/runs/etf_rotation_results")
BASELINE_FRP = RESULTS / "_backup_pre_pctfix_20261117" / "final_rebalance_plan.py"

DEFAULT_SCOPE = ("manu/etf_rotation", "manu/tools", "manu/memory")


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def changed_files(since: float, scopes: tuple[str, ...]) -> list[Path]:
    out: list[Path] = []
    for scope in scopes:
        base = PROJ / scope
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            try:
                if path.stat().st_mtime >= since:
                    out.append(path)
            except OSError:
                continue
    return out


def diff_for(path: Path) -> str:
    rel = path.relative_to(PROJ).as_posix()
    if rel == "manu/etf_rotation/final_rebalance_plan.py" and BASELINE_FRP.is_file():
        old = BASELINE_FRP.read_text(encoding="utf-8").splitlines(keepends=True)
        old_name = "a/" + rel + " (基线=_backup_pre_pctfix_20261117)"
    else:
        old = []
        old_name = "/dev/null"
    new = path.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = difflib.unified_diff(old, new, fromfile=old_name, tofile="b/" + rel, n=3)
    return "".join(diff)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", type=float, default=1794908100.0,
                    help="mtime 下限（epoch；默认 2026-11-17 17:35 Asia/Shanghai）")
    ap.add_argument("--scope", default=",".join(DEFAULT_SCOPE))
    ap.add_argument("--max-diff-lines", type=int, default=100000)
    args = ap.parse_args()

    scopes = tuple(s.strip() for s in args.scope.split(",") if s.strip())
    files = changed_files(args.since, scopes)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# CHANGESET · 服务器侧代码改动清单（2026-11-17 夜班）")
    lines.append("")
    lines.append(f"> 生成时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}"
                 f"｜工程根：`{PROJ}`")
    lines.append(f"> 改动判定：mtime ≥ {args.since}（= 2026-11-17 17:35 Asia/Shanghai）"
                 f"｜扫描范围：{', '.join(scopes)}")
    lines.append("")
    lines.append("> 🔴 **全程未 commit / 未 push**（工作树改动 ⇒ 主机回灌请用 "
                 "`pull_from_server.sh`，它只动工作树、绝不 commit）。")
    lines.append("")
    lines.append("## 一、文件清单")
    lines.append("")
    lines.append("| # | 文件 | 状态 | 行数 | 字节 | md5 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for i, path in enumerate(files, 1):
        rel = path.relative_to(PROJ).as_posix()
        n_lines = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        size = path.stat().st_size
        status = "修改" if rel == "manu/etf_rotation/final_rebalance_plan.py" else "新增"
        lines.append(f"| {i} | `{rel}` | {status} | {n_lines} | {size} | `{md5_of(path)}` |")
    lines.append("")
    lines.append(f"合计 **{len(files)}** 个文件。")
    lines.append("")
    lines.append("## 二、完整 diff")
    lines.append("")
    lines.append("> 基线口径：`final_rebalance_plan.py` ⇒ "
                 "`_backup_pre_pctfix_20261117/final_rebalance_plan.py`；"
                 "其余为本夜班**新增**文件（基线 = `/dev/null`）。")
    lines.append("")
    for path in files:
        rel = path.relative_to(PROJ).as_posix()
        body = diff_for(path)
        n = body.count("\n")
        if n > args.max_diff_lines:
            body = body[: args.max_diff_lines * 200] + "\n...（diff 过长，已截断）\n"
        lines.append(f"### `{rel}`")
        lines.append("")
        lines.append("```diff")
        lines.append(body.rstrip("\n"))
        lines.append("```")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] CHANGESET 已写出：{out_path}（{len(files)} 个文件，"
          f"{out_path.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
