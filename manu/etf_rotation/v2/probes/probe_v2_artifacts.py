#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V2 闸门探针 · **产物存在性 / V1 冻结 / 目录洁癖 / 报告口径**。

【每类断言钉死了什么】
A 类 · 产物存在性（V2 产物根）
  A1 产物根存在（默认 ``/mnt/data/siping/runs/etf_rotation_results/v2``）
  A2 关键产物清单齐全（缺一个就点名）
  A3 单盲 token 存在即视为「测试段已跑过」（fail-closed 的物理凭证）

B 类 · **V1 冻结**（V2 绝不许就地改写 V1）
  B1 V1 四个关键文件（config/backtest/qlib_scores/analyze）**mtime** 未被本夜班改写
  B2 V1 的既有产物仍在（指纹表 / 交付报告）

C 类 · 报告口径
  C1 成绩单必须出现「扣费年化 / 最大回撤 / RankIC」三个指标关键词
  C2 成绩单必须出现 embargo 与「只跑一次 / 单盲」的披露
  C3 成绩单必须出现 V1 baseline 的对照数字（16.57× / −32.29% 等）

D 类 · 目录洁癖
  D1 V2 产物**只**落在 runs/ 与 datasets/manu/ 下（不许散落在工程根或 /mnt/data 根）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    line = f"[PASS] {name}" + (f"｜{detail}" if detail else "")
    if ok:
        PASSED.append(line)
        print(line, flush=True)
    else:
        line = f"[FAIL] {name}" + (f"｜{detail}" if detail else "")
        FAILED.append(line)
        print(line, flush=True)


def _opt(name: str, default: str) -> str:
    return os.environ.get(name, default)


def probe_artifacts(root: Path) -> None:
    check("A1 V2 产物根存在", root.is_dir(), str(root))
    if not root.is_dir():
        return
    # 🔴 中途跑闸门时允许「产物尚未产出」；**最终**闸门不许跳过
    #    （跳过开关由环境变量显式给出 ⇒ 不会悄悄放宽）。
    if os.environ.get("ETF_V2_SKIP_ARTIFACT_CHECK") == "1":
        print("[INFO] 已显式跳过产物存在性检查（ETF_V2_SKIP_ARTIFACT_CHECK=1）")
        return
    expected = [
        "ablation_choice.json",
        "val_search.json",
        "frozen_params.json",
        "single_blind_test_2024_2026.json",
        "V2_SCOREBOARD.md",
    ]
    missing = [f for f in expected if not (root / f).is_file()]
    check("A2 关键产物清单齐全", not missing,
          "齐全" if not missing else f"缺：{missing}")
    token = root / "single_blind_test_2024_2026.json"
    if token.is_file():
        check("A3 单盲 token 存在即视为测试段已跑过", True, str(token))
    else:
        check("A3 单盲 token 存在即视为测试段已跑过", False,
              f"缺 {token} ⇒ 单盲段尚未产出（若本轮未到该阶段，属预期）")


def probe_v1_frozen() -> None:
    proj = Path("/tmp/pycharm_project_658d3fc4")
    etf = proj / "manu" / "etf_rotation"
    # 🔴 本夜班开始时间（V1 冻结基线）：早于它 = V1 未被本轮改写
    baseline_ts = 1794908100.0  # 2026-11-17 17:35（Asia/Shanghai；与主机验收脚本同口径）
    import datetime as _dt
    for fname in ("config.py", "backtest.py", "qlib_scores.py", "analyze.py"):
        path = etf / fname
        if not path.is_file():
            check(f"B1 V1 冻结文件 {fname} 存在", False, str(path))
            continue
        ts = path.stat().st_mtime
        stamp = _dt.datetime.fromtimestamp(ts).strftime("%F %T")
        check(f"B1 V1 冻结文件 {fname} 未被本夜班改写", ts < baseline_ts, f"mtime={stamp}")
    results = Path("/mnt/data/siping/runs/etf_rotation_results")
    for fname in ("_ms5_md5.txt", "reports/report_20261117.md"):
        check(f"B2 V1 既有产物仍在：{fname}", (results / fname).is_file(), str(results / fname))


def probe_report(root: Path) -> None:
    path = root / "V2_SCOREBOARD.md"
    if not path.is_file():
        if os.environ.get("ETF_V2_SKIP_ARTIFACT_CHECK") == "1":
            print("[INFO] 已显式跳过成绩单检查（ETF_V2_SKIP_ARTIFACT_CHECK=1）")
            return
        check("C1 成绩单存在", False, str(path))
        return
    text = path.read_text(encoding="utf-8")
    for key in ("扣费年化", "最大回撤", "RankIC"):
        check(f"C1 成绩单含指标关键词「{key}」", key in text)
    check("C2 成绩单含 embargo 与单盲披露",
          ("embargo" in text) and ("只跑一次" in text or "单盲" in text))
    check("C3 成绩单含 V1 baseline 对照数字",
          ("16.57" in text) or ("−32.29" in text) or ("-32.29" in text))


def probe_hygiene(root: Path) -> None:
    """V2 产物只许落在 runs / datasets 下。"""
    ok = str(root).startswith("/mnt/data/siping/runs/")
    check("D1 V2 产物根落在 runs/ 下", ok, str(root))
    stray = []
    for name in ("etf_v2", "v2_run", "codex_v2"):
        for base in (Path("/tmp/pycharm_project_658d3fc4"), Path("/mnt/data")):
            p = base / name
            if p.exists():
                stray.append(str(p))
    check("D1b 工程根 / /mnt/data 根无 V2 散落产物", not stray,
          "无散落" if not stray else f"发现：{stray}")


def main() -> int:
    root = Path(_opt("ETF_V2_RUN_ROOT", str(C.DEFAULT_V2_RUN_ROOT)))
    print("=== probe_v2_artifacts · 产物 / V1 冻结 / 目录洁癖 ===")
    probe_artifacts(root)
    probe_v1_frozen()
    probe_report(root)
    probe_hygiene(root)
    print("")
    print(f"合计 {len(PASSED) + len(FAILED)} 项：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    if FAILED:
        print("")
        for line in FAILED:
            print(line)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
