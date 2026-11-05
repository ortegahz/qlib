#!/usr/bin/env python3
"""manu/tools/sync_slim_memory.py —— **精简档（slim）新鲜度闸门 + 自动块刷新**。

【为什么需要】
`manu/memory/` 是**四层**记忆体系：

| 层 | 文件 | 定位 |
|:--|:--|:--|
| 精简档 | `.slim_memory.md` | **可直接贴给别的模型**的主索引（本工具的守门对象） |
| 速览卡 | `memory_compact.md` | 5 分钟读完事实与数字 |
| 主档 | `memory.md` | 全量事实与变更记录 |
| 铁律 | `rules.md` | 规约正文 |

🔴 **「有必要的时候更新 memory」必须覆盖到精简档**：只更新主档而精简档停在昨天，
⇒ 贴给别的模型的就是**过期背景** ⇒ 决策建立在陈旧事实���。
⇒ 本工具把这条义务变成**可执行的闸门**：

* `--check`（默认只查）：比对 AUTO 块里的行数标注 + 检查精简档的 mtime 是否
  **不早于** 主档 / 速览卡 / 铁律 ⇒ 任一不满足 ⇒ **退出码 1**；
* 不带参数：重写 AUTO 块（行数标注 + 生成时间）并把 mtime 推到最新。

【用法】
```bash
python3 manu/tools/sync_slim_memory.py --check   # 闸门（退出码 0 = 新鲜）
python3 manu/tools/sync_slim_memory.py            # 刷新 AUTO 块
```
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = REPO_ROOT / "manu" / "memory"
SLIM = MEMORY_DIR / ".slim_memory.md"
MASTER = MEMORY_DIR / "memory.md"
COMPACT = MEMORY_DIR / "memory_compact.md"
RULES = MEMORY_DIR / "rules.md"

BEGIN = "<!-- AUTO:BEGIN -->"
END = "<!-- AUTO:END -->"

#: AUTO 块里的行数标注格式（改格式时同步改这里）
_ROWS = (
    ("| **精简档（本文件）** | `manu/memory/.slim_memory.md` | {slim} | **贴给别的模型 / 新会话第一眼** |\n"
     "| 速览卡 | `manu/memory/memory_compact.md` | {compact} | 5 分钟读完事实与数字 |\n"
     "| 主档 | `manu/memory/memory.md` | {master} | 全量事实与变更记录 |\n"
     "| 铁律 | `manu/memory/rules.md` | {rules} | 规约正文（**必读**） |\n")
)

#: 行 ⇒ **唯一**定位串（🔴 必须逐行唯一定位：``.slim_memory.md`` 是 ``memory.md`` 的后缀，
#: 用 ``memory\.md`` 这种正则会先匹配到精简档那一行 ⇒ 主档行数会被读成精简档的行数）
_ROW_KEYS = {
    "slim": "manu/memory/.slim_memory.md",
    "compact": "manu/memory/memory_compact.md",
    "master": "manu/memory/memory.md",
    "rules": "manu/memory/rules.md",
}


def _n_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def _stamp() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def build_block() -> str:
    """生成 AUTO 块正文（**行数取真实值**，不许手写）。"""
    rows = _ROWS.format(
        slim=_n_lines(SLIM),
        compact=_n_lines(COMPACT),
        master=_n_lines(MASTER),
        rules=_n_lines(RULES),
    )
    return (
        f"{BEGIN}\n"
        f"> **版本**：{_stamp()} · **本文件由 `manu/tools/sync_slim_memory.py` 自动标注行数**\n"
        f"{rows}"
        f"{END}\n"
    )


def read_block(text: str) -> str | None:
    """读出当前 AUTO 块（不含标记行本身）。"""
    if BEGIN not in text or END not in text:
        return None
    start = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    return text[start:end]


def _parse_rows(block: str) -> dict[str, int | None]:
    r"""从 AUTO 块正文里逐行解析出四个行数标注。

    🔴 **逐行唯一定位**（不能用 ``memory\.md`` 这类正则：``.slim_memory.md`` 是 ``memory.md``
    的后缀，先匹配到精简档那一行 ⇒ 主档行数会被读成精简档的行数）。
    """
    out: dict[str, int | None] = {key: None for key in _ROW_KEYS}
    for line in block.splitlines():
        for key, token in _ROW_KEYS.items():
            if token not in line:
                continue
            found = re.search(re.escape("`" + token + "`") + r"\s*\|\s*(\d+)\s*\|", line)
            if found:
                out[key] = int(found.group(1))
    return out


def _splice(text: str, block: str) -> str:
    """把 AUTO 块替换进正文（保持行数稳定 ⇒ 行数标注不会自我漂移）。"""
    if BEGIN in text and END in text:
        start = text.index(BEGIN)
        end = text.index(END) + len(END)
        return text[:start] + block.rstrip("\n") + text[end:]
    head = text.splitlines()[0]
    return head + "\n\n" + block + "\n" + text[len(head):].lstrip("\n")


def refresh() -> int:
    """重写 AUTO 块并落盘（**不动点迭代**）。返回退出码。

    🔴 为什么必须迭代：精简档的行数标注写在**精简档自己**里面 ⇒ 写 AUTO 块会改变精简档的行数
    ⇒ 一次性写入必然对不上（首次插入 AUTO 块会凭空多出若干行）。
    ⇒ 反复「写 → 重算 → 再写」，直到标注值 == 实际行数（最多 6 轮；实际 2 轮内收敛）。
    """
    if not SLIM.exists():
        sys.stderr.write(f"❌ 精简档不存在：{SLIM}\n")
        return 2

    recorded: int | None = None
    for _round in range(6):
        text = SLIM.read_text(encoding="utf-8")
        new_text = _splice(text, build_block())
        SLIM.write_text(new_text, encoding="utf-8")
        block = read_block(new_text) or ""
        recorded = _parse_rows(block).get("slim")
        if recorded == _n_lines(SLIM):
            sys.stdout.write(f"✅ 已刷新 AUTO 块：{SLIM}（第 {_round + 1} 轮收敛）\n")
            return 0

    sys.stderr.write(
        f"❌ AUTO 块行数标注未收敛：标注 = {recorded}，实际 = {_n_lines(SLIM)}\n"
        "   ⇒ 检查 AUTO 块结构是否被手工改动。\n"
    )
    return 1


def check() -> int:
    """新鲜度闸门。退出码 0 = 新鲜，1 = 陈旧。"""
    problems: list[str] = []
    if not SLIM.exists():
        sys.stderr.write(f"❌ 精简档不存在：{SLIM}\n")
        return 2
    text = SLIM.read_text(encoding="utf-8")
    block = read_block(text)
    if block is None:
        problems.append("AUTO 块缺失（找不到 AUTO:BEGIN / AUTO:END 标记）")
    else:
        expected = {
            "slim": _n_lines(SLIM),
            "compact": _n_lines(COMPACT),
            "master": _n_lines(MASTER),
            "rules": _n_lines(RULES),
        }
        parsed = _parse_rows(block)
        for key, value in expected.items():
            got = parsed.get(key)
            if got is None:
                problems.append(f"AUTO 块里找不到 {key} 的行数标注")
                continue
            if got != value:
                problems.append(
                    f"{key} 行数标注 = {got}，实际 = {value} ⇒ 不同步"
                )

    # 🔴 新鲜度**链路**：主档 → 速览卡 → 精简档（下游必须不早于上游更新）
    slim_mtime = SLIM.stat().st_mtime
    for label, path in (("主档", MASTER), ("速览卡", COMPACT), ("铁律", RULES)):
        if not path.exists():
            continue
        if path.stat().st_mtime > slim_mtime + 1:
            problems.append(
                f"{label}（{path.name}）比精简档更新 ⇒ 精简档**陈旧**，必须同轮刷新"
            )
    if MASTER.exists() and COMPACT.exists():
        if MASTER.stat().st_mtime > COMPACT.stat().st_mtime + 1:
            problems.append(
                f"速览卡（{COMPACT.name}）比主档旧 ⇒ 速览卡**陈旧**，"
                "必须同轮刷新（精简档 + 速览卡都要实时跟上主档）"
            )

    if problems:
        sys.stderr.write("❌ 精简档新鲜度闸门未通过：\n")
        for item in problems:
            sys.stderr.write(f"   - {item}\n")
        sys.stderr.write(
            "   ⇒ 修法：先改 `manu/memory/.slim_memory.md`（补齐事实），"
            "再跑 `python3 manu/tools/sync_slim_memory.py` 刷新 AUTO 块。\n"
        )
        return 1
    sys.stdout.write(
        f"✅ 精简档新鲜度闸门通过：{SLIM.relative_to(REPO_ROOT)}"
        f"（主档 {_n_lines(MASTER)} 行 / 速览卡 {_n_lines(COMPACT)} 行 / 铁律 {_n_lines(RULES)} 行）\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="精简档（.slim_memory.md）新鲜度闸门 + AUTO 块刷新")
    ap.add_argument("--check", action="store_true", help="只检查（闸门模式，退出码 0 = 新鲜）")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    return refresh()


if __name__ == "__main__":
    sys.exit(main())
