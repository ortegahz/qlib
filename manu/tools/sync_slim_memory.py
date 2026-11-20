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
⇒ 贴给别的模型的就是**过期背景** ⇒ 决策建立在陈旧事实。
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


def _check_rules_index() -> list[str]:
    """跑「速览表覆盖 ＋ 行数标注新鲜度」闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十四.126` 第 4 条）：
       「改了 `rules.md` ⇒ `memory.md §0` 速览表必须同步（并刷新行数标注）」是**铁律**，
       而铁律若只靠人记 ⇒ 必然复发（本轮实测就漏了「十四」整段 + 行数标注停在 2417）。
       ⇒ 把它并进**四层同步闸门**（本工具 `--check`）⇒ 每次 memory 改动必跑 ⇒ 漏了就红。

    🔴 **只读**：检查器本身不写任何被检查的文件；这里只把它的结论并进本闸门的问题清单。
    🔴 检查器**不存在**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_memory_rules_index.py"
    if not checker.is_file():
        return [
            f"🔴 速览表/行数闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「速览表覆盖 ＋ 行数标注新鲜度」（`rules.md §十四.126`）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_memory_rules_index as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 速览表/行数闸门导入失败：{exc}（`rules.md §十四.126`）"]

    defects = checker_mod.analyze(
        MASTER.read_text(encoding="utf-8"),
        COMPACT.read_text(encoding="utf-8"),
        RULES.read_text(encoding="utf-8"),
    )
    return [
        f"🔴 速览表/行数标注缺陷（`rules.md §十四.126`）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


def _check_stage_count() -> list[str]:
    """跑「阶段数 / 位次」计数类口径闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十四.127` 第 5 条）：
       「计数类口径只许实测写入」是**铁律**，而铁律若只靠人记 ⇒ 必然复发
       （本轮实测：`run_gates.sh` 头注释「26/27」与实测 28 不符、memory 现口径停在 26、
       「26 阶段绿」抄错、4 处位次漂移）⇒ 并进**四层同步闸门**（本工具 `--check`）
       ⇒ 每次 memory 改动必跑 ⇒ 漏了就红。

    🔴 **只读**：检查器本身不写任何被检查的文件；这里只把它的结论并进本闸门的问题清单。
    🔴 检查器**不存在 / 导入失败**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_memory_stage_count.py"
    if not checker.is_file():
        return [
            f"🔴 阶段数/位次闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「计数类口径必须实测且标明口径」（`rules.md §十四.127`）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_memory_stage_count as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 阶段数/位次闸门导入失败：{exc}（`rules.md §十四.127`）"]

    try:
        defects = checker_mod.analyze(str(REPO_ROOT))
    except checker_mod.InputError as exc:
        return [f"🔴 阶段数/位次闸门输入错误：{exc}（`rules.md §十四.127`）"]
    return [
        f"🔴 阶段数/位次口径缺陷（`rules.md §十四.127`）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


def _check_record_ledger() -> list[str]:
    """跑「逐日台账完整性」闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十四.122`）：
       用户亲定铁律要求把「**不可从开源数据复现的核心信息**」（当天 ETF 权重、动作、
       否决理由、账户口径…）落成**可复盘的事实记录**；载体 B ＝ 机器可读的逐日台账
       `manu/memory/records/etf_rotation_daily_ledger.csv`。
       🔴 既有守卫只覆盖**写入端语义**（幂等/作废），**台账文件本体没有机器闸门**
       ⇒ 列名漂移、同 (日期,代码) 两条「有效」、同一天账户口径分叉…**全部静默通过**
       ⇒ 并进**四层同步闸门**（本工具 `--check`）⇒ 每次 memory 改动必跑 ⇒ 漏了就红。

    🔴 **只读**：检查器本身不写任何被检查的文件。
    🔴 检查器**不存在 / 导入失败**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_record_ledger.py"
    if not checker.is_file():
        return [
            f"🔴 台账完整性闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「逐日台账完整性」（`rules.md §十四.122`）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_record_ledger as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 台账完整性闸门导入失败：{exc}（`rules.md §十四.122`）"]

    try:
        defects, _facts = checker_mod.analyze(str(REPO_ROOT))
    except checker_mod.InputError as exc:
        return [f"🔴 台账完整性闸门输入错误：{exc}（`rules.md §十四.122`）"]
    return [
        f"🔴 台账完整性缺陷（`rules.md §十四.122`）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


def _check_mirror_pairs() -> list[str]:
    """跑「镜像纪律（文件层 ＋ git 层成对放行）」闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十四.125`）：
       铁律一「白名单必须**成对**写」＋ 铁律三「镜像纪律必须验到 **git 层**」——
       这两条此前**只有人守**（每轮手工 `diff`/`cmp` 扫一遍）⇒ 而「只放行了一半」
       **本地永远测不出来**（两边都在本地）⇒ 提交后仓库里只剩一半 ⇒ **新克隆缺文件**
       （§十四.125 记载的真缺陷【二十】即此形态；修过但**没有机器闸门** ⇒ 必然复发）。
       ⇒ 并进**四层同步闸门**（本工具 `--check`）⇒ 每次 memory 改动必跑 ⇒ 漏了就红。

    🔴 **只读**：检查器本身不写任何被检查的文件。
    🔴 检查器**不存在 / 导入失败**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_mirror_pairs.py"
    if not checker.is_file():
        return [
            f"🔴 镜像纪律闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「镜像对逐字一致 ＋ 入库白名单成对放行」（`rules.md §十四.125`）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_mirror_pairs as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 镜像纪律闸门导入失败：{exc}（`rules.md §十四.125`）"]

    try:
        defects, _facts = checker_mod.analyze(str(REPO_ROOT))
    except checker_mod.NotApplicable as exc:
        # 🔴 **契约自洽**（同 `_check_record_carrier`）：「不适用」≠「镜像有问题」，
        #    但也**不等于可以放行** ⇒ fail-closed 报红 ＋ 如实写清红的原因。
        return [
            f"🔴 镜像纪律闸门**不适用**（{exc}）⇒ 无法验证 ⇒ 按 fail-closed 报红"
            "（`rules.md §十四.125`；**不适用 ≠ 镜像有问题**，是「查不了」）"
        ]
    except checker_mod.InputError as exc:
        return [f"🔴 镜像纪律闸门输入错误：{exc}（`rules.md §十四.125`）"]
    return [
        f"🔴 镜像纪律缺陷（`rules.md §十四.125`）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


def _check_record_carrier() -> list[str]:
    """跑「记忆载体入库纪律 ＋ 落点纪律」闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十四.122` 第 5 条 ＋ `§十四.125` 铁律二）：
       ① **落点纪律**：`manu/memory/` 下只许记忆文档 ＋ 逐日台账
         ⇒ 出现 `.pkl` / 行情 `.csv` / 其他运行产物 = 违规（撞「版本库只放源」）；
       ② **入库纪律**：台账**不许被 `.gitignore` 静默忽略**（上游全局 `*.csv`）
         ⇒ 不然「几个月后复盘」的事实源随工作区清理一起消失；
       ③ `.gitattributes` 的 `-text` 必须**成对**（否则行尾归一化让镜像对两侧字节分叉）。
       🔴 既有闸门 `check_mirror_pairs.py` 只管**镜像对**（`manu/<X>` ↔ `manu/etf_rotation/<X>`），
         而 `manu/memory/` **整棵树不在镜像体系里** ⇒ 上面的形态**没有任何闸门能报**
         ⇒ 必须挂在这里（每次改 memory 必跑的那条命令就够）。

    🔴 **只读**：检查器本身不写任何被检查的文件。
    🔴 检查器**不存在 / 导入失败**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_record_carrier.py"
    if not checker.is_file():
        return [
            f"🔴 载体纪律闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「记忆载体的入库纪律 ＋ 落点纪律」（`rules.md §十四.122` 第 5 条）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_record_carrier as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 载体纪律闸门导入失败：{exc}（`rules.md §十四.122` 第 5 条）"]

    try:
        defects, _facts = checker_mod.analyze(str(REPO_ROOT))
    except checker_mod.NotApplicable as exc:
        # 🔴 **契约自洽**：检查器 rc=2 的语义是「**不适用 ⇒ 不算载体有问题**」，但**「无法验证」≠「可以放行」**
        #    ⇒ 按 fail-closed 报红（**不许静默跳过**），但**如实写清红的原因**（不是「载体有缺陷」）。
        return [
            f"🔴 载体纪律闸门**不适用**（{exc}）⇒ 无法验证 ⇒ 按 fail-closed 报红"
            "（`rules.md §十四.122` 第 5 条；**不适用 ≠ 载体有问题**，是「查不了」）"
        ]
    except checker_mod.InputError as exc:
        return [f"🔴 载体纪律闸门输入错误：{exc}（`rules.md §十四.122` 第 5 条）"]
    return [
        f"🔴 载体纪律缺陷（`rules.md §十四.122` 第 5 条）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


def _check_artifact_placement() -> list[str]:
    """跑「产物落点双根化 ＋ 痕迹清零」闸门，返回问题清单（空 = 通过）。

    🔴 为什么挂在这里（`rules.md §十六` 第 7 条）：
       §十六 第 7 条原文「**验收方式（闸门 / 自检必须能断言，不许只写在文档里）**」，
       并逐条点名三条**必须能断言**的条件：
         ① 本项目在服务器上的文件**全部位于** `<DATASET_ROOT>` 或 `<RESULTS_ROOT>` 之下；
         ② `/mnt/data` 根**没有** `_etf_daily_*`；`siping/datasets/` 顶层**没有**本项目目录；
         ③ `siping/runs/` 顶层**本项目专属**目录**只存在** `<RESULTS_ROOT>` **一个**
            （别人的 `nogmc_*` / `npu20ms_*` / `ashare_topk` / `CANDIDATES` / `_ms6` / `_ms7`
            **不计、也不许删**）；
       另加 §十六 第 4 条 2026-11-19 夜补的「**`/tmp` 里的本项目件同样必须清零**」。
       🔴 这三条此前**只有人守**（既有工具只覆盖**写入端常量**与**仓库内载体**，
         **没有任何工具读回文件系统断言「落点真的只剩双根」**）⇒ 落点漂了没有任何闸门能报
         ⇒ 复发即断链（§十六 第 2 条已点名 3 个历史违例实例）。

    🔴 **只读**：检查器本身不写、不删任何被检查的文件。
    🔴 检查器**不存在 / 导入失败**时**不算通过**（静默跳过 = 假绿）⇒ 按缺陷报。
    🔴 **不适用 ≠ 不合格**（`rules.md §十四.117`）：服务器侧断言（② / ③ / `/tmp`）在
       **没有服务器根**的机器上**不执行** ⇒ 闸门**如实报「不适用」**，**不按缺陷红**
       （跨主机都适用的 A1 双根自洽 ＋ A6 落点字面量仍照常断言）。
    """
    checker = MEMORY_DIR.parent / "tools" / "check_artifact_placement.py"
    if not checker.is_file():
        return [
            f"🔴 落点纪律闸门缺失：{checker.name} 不存在 ⇒ 无法验证"
            "「落点双根化 ＋ 痕迹清零」（`rules.md §十六` 第 7 条）"
        ]
    try:
        if str(MEMORY_DIR.parent) not in sys.path:
            sys.path.insert(0, str(MEMORY_DIR.parent))
        from tools import check_artifact_placement as checker_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 导入失败也要报红，不许静默跳过
        return [f"🔴 落点纪律闸门导入失败：{exc}（`rules.md §十六` 第 7 条）"]

    try:
        defects, facts = checker_mod.analyze(str(REPO_ROOT))
    except checker_mod.InputError as exc:
        return [f"🔴 落点纪律闸门输入错误：{exc}（`rules.md §十六` 第 7 条）"]
    # 🔴 **不许无声变绿**（`rules.md §十四.117` 第 3 条：「不许折算成 FAIL，
    #    **也不许折算成 PASS 后当作『已验证』**」）：
    #    服务器侧断言（② / ③ / `/tmp` 痕迹清零）在本机**没有服务器根**时**不执行**
    #    ⇒ 必须**显式说出来**（跨主机都适用的 A1 双根自洽 ＋ A6 落点字面量仍照常断言）。
    if not facts.server_scope:
        sys.stdout.write(
            "⚠️ 落点纪律闸门：服务器侧断言（② `/mnt/data` 根 / ③ `runs/` 顶层 / `/tmp` 痕迹清零）"
            "在本机**不适用**（无 `/mnt/data/siping/runs`）⇒ **未执行**，"
            "**不当作已验证**（A1 双根自洽 ＋ A6 落点字面量已照常断言；"
            "`rules.md §十六` 第 7 条 / `§十四.117` 第 3 条）\n"
        )
    return [
        f"🔴 落点纪律缺陷（`rules.md §十六` 第 7 条）：{defect.as_dict().get('reason', defect)}"
        for defect in defects
    ] if defects else []


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

    # 🔴 2026-11-19（`rules.md §十四.126`）：**速览表同章节覆盖 ＋ 行数标注新鲜度**
    #    ⇒ 这两条必须「常驻」，不许只靠人记得跑（`check_memory_rules_index.py` 是只读检查器）。
    #    判据 = `memory.md §0` 速览表章节集合 ≡ `rules.md` 一级章节集合，且 5 个行数锚点 == 实测行数。
    index_problems = _check_rules_index()
    problems.extend(index_problems)

    # 🔴 2026-11-19（`rules.md §十四.127`）：**计数类口径（阶段数 / 位次）必须实测且标明口径**
    #    ⇒ 判据 = 现口径锚点数字 == `run_gates.sh` 实测 `run_stage` 数；「执行位次」标注 == 实测位次；
    #      脚本头注释 == 实测；工具标签不许写死；历史锚点必须逐字还在。
    stage_problems = _check_stage_count()
    problems.extend(stage_problems)

    # 🔴 2026-11-19（`rules.md §十四.122`）：**逐日台账完整性**（表头锚写入端 / 行结构 /
    #    唯一有效行 / 状态域 / 日期格式 / 权重域 / 同日口径一致 / 成交一致性 / 核心字段非空）
    #    ⇒ 台账是「几个月后复盘」的唯一结构化载体 ⇒ 必须「常驻」，不许只靠人记。
    ledger_problems = _check_record_ledger()
    problems.extend(ledger_problems)

    # 🔴 2026-11-19（`rules.md §十四.125`）：**镜像纪律**（文件层逐字一致 ＋ **git 层成对放行**）
    #    ⇒ 「白名单只放行了一半」**本地测不出来**（两边都在本地）⇒ 必须有常驻机器闸门。
    mirror_problems = _check_mirror_pairs()
    problems.extend(mirror_problems)

    # 🔴 2026-11-19（`rules.md §十四.122` 第 5 条 ＋ `§十四.125` 铁律二）：**记忆载体纪律**
    #    （台账不许被 `.gitignore` 静默忽略 / 记忆树不许混进运行产物 / `-text` 必须成对）
    #    ⇒ `manu/memory/` 整棵树**不在镜像体系里** ⇒ 镜像闸门管不到 ⇒ 必须单独挂。
    carrier_problems = _check_record_carrier()
    problems.extend(carrier_problems)

    # 🔴 2026-11-19（`rules.md §十六` 第 7 条）：**落点双根化 ＋ 痕迹清零**的验收断言
    #    （② `/mnt/data` 根无 `_etf_daily_*` / `datasets/` 顶层无本项目目录 /
    #      ③ `runs/` 顶层本项目专属目录只许 `<RESULTS_ROOT>` 一个 / `/tmp` 本项目件清零）
    #    ⇒ §十六 第 7 条明写「**闸门 / 自检必须能断言，不许只写在文档里**」⇒ 必须常驻。
    placement_problems = _check_artifact_placement()
    problems.extend(placement_problems)

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
