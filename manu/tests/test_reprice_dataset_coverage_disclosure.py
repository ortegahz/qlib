#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🛡️ 节点日重算的「数据集覆盖」披露**不得出假红灯**（2026-11-13 全量跑批实证）。

【钉的是什么】
🔴 **2026-11-13 实测事故**：节点日重算（`reprice_node_day.py`）的交付面披露写着
   「qlib 数据集只到 **2026-11-09** ⇒ 节点日 2026-11-12 的 S 层**不存在**」
   ⇒ **假红灯**。真相是：阶段 1「自建增量日」派生出的 `cn_data_live`
   日历末日 = **2026-11-12**，S 层**已覆盖**到信号日/节点日
   （实测 `stock_scores.csv` 最大日期 = 2026-11-12、日志明写「S 层已覆盖到信号日」）。

【根因】原实现只读 `C.PROVIDER_URI`（= **上游基座** `cn_data`）⇒ 派生目录对它**不可见**
   ⇒ 研究链与每日链**各写各的 provider 口径** ⇒ 口径分叉。
【修法】改用**单一事实源** `manu/etf_rotation/qlib_provider.py::select_provider()`
   （谁新用谁），与每日链 `daily/run_qlib_score.select_provider` 同源。

【本测试怎么保证有牙】
   ① 用**真实**的 provider 选择逻辑（只替换两个目录函数的返回值，不 stub 选择规则）；
   ② 断言落到**派生目录**的末日，且披露里**不得**出现「不存在」；
   ③ 真负例：派生目录不存在 / 不比基座新 ⇒ 必须退回基座口径、必须说「不存在」；
   ④ 变异反证：把实现改回「只读 `C.PROVIDER_URI`」⇒ ① ② 必转红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qlib_provider as QP  # noqa: E402
import reprice_node_day as RND  # noqa: E402

NODE_DATE = "2026-11-12"
BASE_MAX = "2026-11-09"
LIVE_MAX = "2026-11-12"


def _write_calendar(provider: Path, last_day: str) -> Path:
    cal = provider / "calendars" / "day.txt"
    cal.parent.mkdir(parents=True, exist_ok=True)
    cal.write_text(f"2026-11-06\n2026-11-09\n{last_day}\n", encoding="utf-8")
    return cal


def _make_tree(tmp_path: Path, *, with_live: bool, live_last: str = LIVE_MAX):
    base = tmp_path / "cn_data"
    _write_calendar(base, BASE_MAX)
    live = tmp_path / "cn_data_live"
    if with_live:
        _write_calendar(live, live_last)
    return base, live


def test_派生目录更新时_必须用派生口径披露_S层存在(tmp_path, monkeypatch):
    """🔴 主用例：派生目录末日 > 基座 ⇒ 披露必须说「节点日 S 层存在」。"""
    base, live = _make_tree(tmp_path, with_live=True)
    monkeypatch.setattr(QP, "base_dir", lambda: base)
    monkeypatch.setattr(QP, "live_dir", lambda: live)

    max_date, source = RND._effective_provider()
    assert max_date == LIVE_MAX, f"必须用派生目录的末日 {LIVE_MAX}，实得 {max_date}"
    assert source == "cn_data_live", source

    note = RND._dataset_coverage_note(NODE_DATE, max_date, source)
    assert "**存在**" in note, note
    assert "不存在" not in note, f"🔴 不得出假红灯：{note}"
    assert LIVE_MAX in note, note


def test_派生目录不存在时_退回基座口径_必须说不存在(tmp_path, monkeypatch):
    """真负例：没有派生目录 ⇒ 基座末日 < 节点日 ⇒ 必须说「不存在」。"""
    base, live = _make_tree(tmp_path, with_live=False)
    monkeypatch.setattr(QP, "base_dir", lambda: base)
    monkeypatch.setattr(QP, "live_dir", lambda: live)

    max_date, source = RND._effective_provider()
    assert max_date == BASE_MAX, max_date
    note = RND._dataset_coverage_note(NODE_DATE, max_date, source)
    assert "**不存在**" in note, note
    assert "**存在**" not in note, note


def test_派生目录不比基座新时_必须用基座口径(tmp_path, monkeypatch):
    """真负例：派生目录**不更新** ⇒ 谁新用谁的规则必须退回基座。"""
    base, live = _make_tree(tmp_path, with_live=True, live_last="2026-11-05")
    monkeypatch.setattr(QP, "base_dir", lambda: base)
    monkeypatch.setattr(QP, "live_dir", lambda: live)

    max_date, source = RND._effective_provider()
    assert max_date == BASE_MAX, max_date
    assert source == "cn_data", source
    note = RND._dataset_coverage_note(NODE_DATE, max_date, source)
    assert "**不存在**" in note, note


def test_读不到日历时_不得编数字(tmp_path, monkeypatch):
    """真负例：两个目录都读不到日历 ⇒ 末日为 None（**不猜**）。"""
    base = tmp_path / "cn_data"
    base.mkdir()
    live = tmp_path / "cn_data_live"
    live.mkdir()
    monkeypatch.setattr(QP, "base_dir", lambda: base)
    monkeypatch.setattr(QP, "live_dir", lambda: live)

    max_date, source = RND._effective_provider()
    assert max_date is None, max_date
    note = RND._dataset_coverage_note(NODE_DATE, max_date, source)
    assert "读不到" in note, note
