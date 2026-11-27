#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔴 2026-11-27（11-26 全量跑批 step4 判红修复）：`probe_llm_parser` 的 **A16** 判定守卫。

【判红现场】
    [FAIL] A16 官方决策：目标权重不得等于该行任一「盈亏/价格/当前语境」的百分数（正负都算）
        有目标的行 6｜**非真空行 0**（含盈亏语境）｜违例 []｜非真空行数 = 0 ⇒ 本项**不适用**
        （不许假装通过）

【归因（实测）】
11-26 官方决策的**逐票表里根本没有盈亏/价格/当前语境**的百分数 ——
只有 `mom120 +17.34%` / `E +0.2301` / `拥挤度 94% 分位` / `重叠 0.645` / `相关 0.938`
⇒ 全部**不是**盈亏语境 ⇒ A16 在该决策上**真空**（不是检测器坏了）。

【修法】
* 真空 ⇒ 判 **N/A**（§十四.117「**不适用 ≠ 不合格**」）⇒ 不许把「不适用」当 FAIL，
  否则任何不含盈亏语境的决策都会把闸门**永久钉死**；
* 🔴 但「真空」必须**自证不是检测器坏了** ⇒ 合成行自检（一行含「浮亏 18.91%」）：
  取不到任何盈亏百分数 ⇒ **检测器坏了** ⇒ 判 **FAIL**（不许把红灯洗成「不适用」）。

【本文件钉死】
① 非真空 + 无违例 ⇒ PASS；
② 非真空 + 目标权重等于该行任一盈亏率 ⇒ **FAIL**；
③ 真空 + 检测器自检有效 ⇒ N/A；
④ 真空 + 检测器自检失效 ⇒ **FAIL**（**变异反证**：守卫有牙）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _find_repo_root() -> Path:
    """向上找工程根（含 ``manu/etf_rotation/probes/probe_llm_parser.py`` 的那层）。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "etf_rotation" / "probes" / "probe_llm_parser.py"
        if candidate.is_file():
            return parent
    raise RuntimeError("定位工程根失败：找不到 manu/etf_rotation/probes/probe_llm_parser.py")


ROOT = _find_repo_root()
PROBE_PATH = ROOT / "manu" / "etf_rotation" / "probes" / "probe_llm_parser.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("_probe_llm_parser_under_test", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


P = _load_probe()

#: 11-26 真实形态：**没有**任何盈亏语境百分数（只有 mom/E/拥挤度/重叠/相关）
_VIEWS_1126 = {
    "512800": [{"who": "bull", "target_pct": 20.0, "pnl_pcts": []}],
    "159928": [{"who": "bull", "target_pct": 20.0, "pnl_pcts": []}],
    "515450": [{"who": "bull", "target_pct": 20.0, "pnl_pcts": []}],
}


def test_非真空且无违例判PASS():
    views = {"512800": [{"who": "bull", "target_pct": 20.0, "pnl_pcts": [18.91]}]}
    status, detail = P._a16_verdict(views, lambda: [18.91])
    assert status == "PASS", detail


def test_非真空且目标等于盈亏率判FAIL():
    """目标权重 == 该行盈亏率 ⇒ 违例 ⇒ FAIL（正负都算）。"""
    views = {"512800": [{"who": "bull", "target_pct": 18.91, "pnl_pcts": [18.91]}]}
    status, detail = P._a16_verdict(views, lambda: [18.91])
    assert status == "FAIL", detail
    assert "512800" in detail, detail


def test_真空且检测器有效判NA():
    """11-26 真实形态：真空 ＋ 检测器仍有效 ⇒ N/A（不适用 ≠ 不合格）。"""
    status, detail = P._a16_verdict(_VIEWS_1126, lambda: [18.91])
    assert status == "N/A", detail
    assert "不适用" in detail, detail


def test_真空且检测器坏了判FAIL():
    """🔴 变异反证：真空 ＋ 检测器自检失效 ⇒ **必须 FAIL**（不许洗成 N/A）。"""
    status, detail = P._a16_verdict(_VIEWS_1126, lambda: [])
    assert status == "FAIL", detail
    assert "检测器坏了" in detail, detail


def test_真空判定不许吞掉违例():
    """🔴 只要**任何一行**带盈亏语境 ⇒ 就不是真空 ⇒ 违例必须判红。"""
    views = {
        "512800": [{"who": "bull", "target_pct": 18.91, "pnl_pcts": [18.91]}],
        "515450": [{"who": "bull", "target_pct": 20.0, "pnl_pcts": []}],
    }
    status, detail = P._a16_verdict(views, lambda: [18.91])
    assert status == "FAIL", detail
    assert "512800" in detail, detail
