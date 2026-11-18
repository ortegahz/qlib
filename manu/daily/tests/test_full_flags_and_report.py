"""🔴 2026-11-09 新增：`--full`（走全部流程）的覆盖面 + 报告披露。

两条独立审计发现：
  * **P1-3**：增量开关（``--no-increment`` / ``--increment-dry-run``）原先**不在**
    ``--full`` 管辖内 ⇒ 「从头到尾不遗漏任何环节」可以被这两个开关悄悄切掉。
  * **P1-5**：被 ``--full`` 覆盖的 skip 开关原先**只进 manifest**、报告里看不到
    ⇒ 「不许静默降级」在交付面失效。
"""

from __future__ import annotations

import main as daily_main
import report as daily_report


def _skipped_names(flags: dict) -> set[str]:
    return {str(item.get("name")) for item in (flags.get("skipped") or [])}


def test_full_overrides_no_increment() -> None:
    """① `--full` ⇒ 增量必须真跑（不能被 `--no-increment` 悄悄切掉）。"""
    args = daily_main.parse_args(["--asof", "2026-11-05", "--full", "--no-increment"])
    flags = daily_main.resolve_stage_flags(args)
    assert flags["increment"] is True
    assert flags["increment_dry_run"] is False
    assert "no_increment" in _skipped_names(flags), "被覆盖的开关必须登记披露"


def test_full_overrides_increment_dry_run() -> None:
    """② `--full` ⇒ 增量不能是 dry-run（dry-run = 这一环节其实没跑）。"""
    args = daily_main.parse_args(["--asof", "2026-11-05", "--full", "--increment-dry-run"])
    flags = daily_main.resolve_stage_flags(args)
    assert flags["increment"] is True
    assert flags["increment_dry_run"] is False
    assert "increment_dry_run" in _skipped_names(flags)


def test_without_full_increment_flags_are_respected() -> None:
    """③ 没有 `--full` ⇒ 用户的增量开关**照旧生效**（不被越权覆盖）。"""
    args = daily_main.parse_args(["--asof", "2026-11-05", "--no-increment"])
    flags = daily_main.resolve_stage_flags(args)
    assert flags["increment"] is False
    assert flags["skipped"] == []


def test_report_discloses_overridden_switches() -> None:
    """④ 报告的「快照与运行口径」表必须列出**本轮被覆盖的开关**。"""
    rows = daily_report._provenance_rows(
        {
            "snapshot": {"path": "/tmp/snap/20261105_120000"},
            "live_trade": False,
            "skipped_switches": [
                {"name": "skip_llm", "reason": "`--full` 强制全流程 ⇒ 覆盖 --skip-llm"},
            ],
        },
        False,
    )
    table = {row[0]: row[1] for row in rows}
    assert "本轮被覆盖的开关" in table, f"口径表必须有这一行，实得 {list(table)}"
    assert "skip_llm" in table["本轮被覆盖的开关"]


def test_report_says_none_when_nothing_overridden() -> None:
    """⑤ 没有被覆盖的开关 ⇒ 显式写「无」（不许留空让人猜）。"""
    rows = daily_report._provenance_rows({"snapshot": {"path": "/x"}, "live_trade": True}, True)
    table = {row[0]: row[1] for row in rows}
    assert table["本轮被覆盖的开关"] == "无"
