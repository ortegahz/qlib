#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V2 闸门探针 · **产物存在性 / V1 冻结 / 目录洁癖 / 报告口径**。

【每类断言钉死了什么】
A 类 · 产物存在性（V2 产物根）
  A1 产物根存在（默认 ``/mnt/data/siping/runs/etf_rotation_results/v2``）
  A2 关键产物清单齐全（缺一个就点名）
  A3 单盲 token 存在即视为「测试段已跑过」（fail-closed 的物理凭证）

B 类 · **V1 冻结**（V2 绝不许就地改写 V1）
  B1 V1 冻结件（config.py / backtest.py / qlib_scores.py / engine/*.py）
     **md5 基线清单**逐文件比对（清单 = 探针**自己旁边**的 ``v1_frozen_md5.json``
     ⇒ 随 manu/ 一起 rsync 到服务器 ⇒ 服务器**没 git 也有牙齿**）
  B1 附 · **git 内容口径只作披露**（服务器 git 检出可能**落后**于开发仓
     ⇒ `` M`` **≠ V1 被破坏**，只说明服务器检出落后于开发仓）
  B1 附 · **mtime 只作披露**（checkout / rsync 同步都会刷新 mtime ⇒ 不能当判据）
  B1 附 · **analyze.py 属每日链分析层**（允许演进）⇒ 只披露，**不判红**
  B2 V1 的既有产物仍在（指纹表 / 交付报告）

C 类 · 报告口径
  C1 成绩单必须出现「扣费年化 / 最大回撤 / RankIC」三个指标关键词
  C2 成绩单必须出现 embargo 与「只跑一次 / 单盲」的披露
  C3 成绩单必须出现 V1 baseline 的对照数字（16.57× / −32.29% 等）

D 类 · 目录洁癖
  D1 V2 产物**只**落在 runs/ 与 datasets/manu/ 下（不许散落在工程根或 /mnt/data 根）

【2026-11-25 修订 · B1 判据换口径（第二轮）】
🔴 **第一轮旧实现是假红**：硬编码一个 epoch 时间戳当 mtime 基线 ⇒ 每日链分析层的
   ``analyze.py``（2026-11-25 的 V2 归因披露改动**合法**改过）的 mtime 永远晚于
   任何历史基线 ⇒ **常年红 + 告警疲劳**。
🔴 **第二轮又挖出新病**：把「git 内容口径」当主判据也**不可行** —— 服务器工程目录的
   git 检出**落后于**开发仓 ⇒ ``git status --porcelain`` 对冻结件天然显示 `` M``
   ⇒ 用它判红 = **另一种常年红**（而且指向错误结论：会被读成「V1 被破坏」）。
⇒ **现行口径**：主判据 = **md5 基线清单**（**对环境免疫**：不依赖 git、不看 mtime）；
   **git / mtime 一律只披露、不判红**；基线时间戳**动态**从 V2 冻结件
   （``frozen_params_wf.json`` → ``frozen_params.json``）的 ``frozen_at`` 读
   ⇒ **读不到就 fail-soft 跳过披露**，🔴 **绝不回退任何硬编码时间戳**。
   清单**读不到 / 解析失败 / 为空 ⇒ FAIL**（fail-closed），绝不静默通过。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

#: 🔴 V1 **冻结件**（V2 绝不许就地改写）—— 清单不可得时的**兜底披露**用
#:    （**主判据**在 ``v1_frozen_md5.json`` 清单里）
V1_FROZEN_FILES: tuple[str, ...] = ("config.py", "backtest.py", "qlib_scores.py")
#: 🔴 V1 冻结件目录（目录下**所有** .py 都算冻结件）
V1_FROZEN_DIRS: tuple[str, ...] = ("engine",)
#: 🔴 每日链分析层（**允许演进** ⇒ 只披露、不判红）
DAILY_CHAIN_FILES: tuple[str, ...] = ("analyze.py",)
#: 🔴 冻结件时间基线来源（优先前者，回退后者）
BASELINE_FILES: tuple[str, ...] = ("frozen_params_wf.json", "frozen_params.json")
#: 🔴 冻结件 `frozen_at` 的口径时区（Asia/Shanghai，无 DST ⇒ 固定 +08:00 即可）
_CST = datetime.timezone(datetime.timedelta(hours=8))
#: 🔴 B1 主判据：**V1 冻结件 md5 基线清单**（对环境免疫：不依赖 git、不看 mtime）
FROZEN_MANIFEST_NAME: str = "v1_frozen_md5.json"
#: 🔴 清单位置：**探针自己旁边** ⇒ 随 manu/ 一起 rsync 到服务器 ⇒ 服务器**没 git 也有牙齿**
MANIFEST_PATH: Path = Path(__file__).resolve().parent / FROZEN_MANIFEST_NAME
#: 🔴 项目根默认值（服务器工程根）—— 只由 ``ETF_V2_PROJECT_ROOT`` 覆盖，**不许改判据语义**
DEFAULT_PROJECT_ROOT: str = "/tmp/pycharm_project_3b68a403"


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


def _project_root() -> Path:
    """⇒ 项目根（``ETF_V2_PROJECT_ROOT`` 优先 ⇒ 默认服务器工程根）。

    🔴 默认值**不许改**（那是服务器工程目录，改了等于换判据语义）。
    """
    return Path(_opt("ETF_V2_PROJECT_ROOT", DEFAULT_PROJECT_ROOT))


def md5_of(path: Path) -> str:
    """⇒ 文件内容的 md5（分块读 ⇒ 不吃内存）。"""
    hasher = hashlib.md5()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_frozen_manifest(path: Path) -> tuple:
    """⇒ ``(files 映射, 失败原因)``；**读不到 / 解析失败 / 为空 ⇒ (None, 原因)**。

    🔴 **fail-closed**：清单不可用 ⇒ 上层**必须判红**（绝不静默通过）。
    """
    path = Path(path)
    if not path.is_file():
        return None, f"清单文件不存在：{path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"清单解析失败：{path}（{exc!r}）"
    if not isinstance(payload, dict):
        return None, f"清单结构非法（顶层不是对象）：{path}"
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        return None, f"清单里 files 为空或非法：{path}"
    bad = []
    for key, value in files.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            bad.append(str(key))
    if bad:
        return None, f"清单里 files 条目非法（键/值必须是非空字符串）：{sorted(bad)}"
    return dict(files), ""


def compare_frozen_manifest(project_root: Path, manifest_files: dict) -> list:
    """**纯函数**：逐文件比对 md5 ⇒ ``[(相对路径, 是否通过, 明细), ...]``。

    * 文件缺失 ⇒ 不通过（明细写路径）
    * md5 不符 ⇒ 不通过（明细写 expected vs actual）
    * md5 相等 ⇒ 通过
    """
    results = []
    for rel in sorted(manifest_files):
        expected = manifest_files[rel]
        path = Path(project_root) / rel
        if not path.is_file():
            results.append((rel, False, f"文件缺失：{path}"))
            continue
        try:
            actual = md5_of(path)
        except OSError as exc:
            results.append((rel, False, f"读取失败：{path}（{exc!r}）"))
            continue
        if actual != expected:
            results.append((rel, False, f"md5 不符：expected={expected} actual={actual}"))
        else:
            results.append((rel, True, f"md5 一致：{actual}"))
    return results


def _fmt_ts(ts: float) -> str:
    """⇒ 可读时间戳（固定 +08:00 口径 ⇒ 与冻结件 ``frozen_at`` 同一时区）。"""
    return datetime.datetime.fromtimestamp(ts, tz=_CST).strftime("%F %T")


def _parse_frozen_at(raw: str) -> float:
    """ISO 字符串 ⇒ epoch 秒（naive 按 +08:00 解释 ⇒ 与主机口径一致）。"""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = datetime.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_CST)
    return stamp.timestamp()


def _load_baseline(root: Path) -> tuple[float | None, str]:
    """从 V2 产物根读冻结件 ⇒ **动态**基线（epoch 秒）。

    ⇒ ``(基线 epoch, 来源说明)``；**读不到 / 缺字段 / 解析失败 ⇒ (None, 原因)**
    ⇒ 上层 fail-soft **跳过 mtime 披露**；🔴 **绝不回退任何硬编码时间戳**。
    """
    reasons: list[str] = []
    for fname in BASELINE_FILES:
        path = root / fname
        if not path.is_file():
            reasons.append(f"{path} 不存在")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{path} 解析失败（{exc!r}）")
            continue
        raw = payload.get("frozen_at") if isinstance(payload, dict) else None
        if not raw:
            reasons.append(f"{path} 缺字段 frozen_at")
            continue
        try:
            ts = _parse_frozen_at(str(raw))
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{path} 的 frozen_at={raw!r} 解析失败（{exc!r}）")
            continue
        return ts, f"{path}（frozen_at={raw}）"
    return None, "；".join(reasons)


def _git_toplevel(proj: Path) -> Path | None:
    """⇒ 项目根所在的 git 仓库顶层；**不在 git 仓库内 / git 不可用 ⇒ None**。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[INFO] 调用 git 失败（{exc!r}）")
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None


def _git_status(toplevel: Path, relpaths: list[str]) -> tuple[str, str]:
    """⇒ ``git status --porcelain -- <路径>`` 的 (stdout, 错误说明)。

    错误说明非空 ⇒ 判据**没跑起来**（调用方据此 fail-closed 判红）。
    """
    cmd = ["git", "-C", str(toplevel), "status", "--porcelain", "--"] + relpaths
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"调用 git 失败：{exc!r}"
    if proc.returncode != 0:
        return "", f"git status rc={proc.returncode}：{proc.stderr.strip()[:200]}"
    return proc.stdout, ""


def _disclose_mtime(path: Path, label: str, baseline_ts: float | None) -> None:
    """mtime **只作披露**（🔴 不判红）：checkout / rsync 同步都会刷新 mtime。"""
    if not path.is_file():
        print(f"[INFO] mtime 披露｜{label}｜文件不存在（{path}）")
        return
    ts = path.stat().st_mtime
    later = "未知（基线不可得）" if baseline_ts is None else ("是" if ts >= baseline_ts else "否")
    print(f"[INFO] mtime 披露｜{label}｜mtime={_fmt_ts(ts)}｜晚于基线={later}（仅披露，不判红）")


def _git_rel(toplevel: Path, absolute: Path):
    """⇒ 绝对路径相对 git 顶层仓库的相对路径；**不在仓库内 ⇒ None**。"""
    try:
        return str(Path(absolute).resolve().relative_to(toplevel))
    except ValueError:
        return None


def _frozen_rel_paths(proj: Path, manifest_files) -> list:
    """⇒ 冻结件的**项目根相对**路径清单（清单可用 ⇒ 用清单；不可用 ⇒ 用内置常量兜底）。"""
    if manifest_files:
        return sorted(manifest_files)
    fallback = ["manu/etf_rotation/" + name for name in V1_FROZEN_FILES]
    for dname in V1_FROZEN_DIRS:
        dpath = proj / "manu" / "etf_rotation" / dname
        if dpath.is_dir():
            for path in sorted(dpath.rglob("*.py")):
                fallback.append("manu/etf_rotation/" + dname + "/" + str(path.relative_to(dpath)))
    return sorted(set(fallback))


def _disclose_git_status(proj: Path, manifest_files) -> None:
    """git 内容口径：**只披露**（🔴 绝不判红）。

    🔴 服务器工程目录的 git 检出可能**落后于**开发仓 ⇒ `` M`` **≠ V1 被破坏**，
       只说明服务器检出落后于开发仓 ⇒ 因此本条**只披露、不判红**。
    """
    toplevel = _git_toplevel(proj)
    if toplevel is None:
        print("[INFO] git 内容口径披露｜项目根不在 git 仓库内（或 git 不可用）"
              "⇒ 跳过 git 披露（🔴 只披露，不判红）")
        return
    rels = []
    for rel in _frozen_rel_paths(proj, manifest_files):
        git_rel = _git_rel(toplevel, proj / rel)
        if git_rel is not None:
            rels.append(git_rel)
    if not rels:
        print("[INFO] git 内容口径披露｜冻结集路径都不在 git 仓库顶层内 ⇒ 跳过 git 披露"
              "（🔴 只披露，不判红）")
        return
    out, err = _git_status(toplevel, rels)
    if err:
        print(f"[INFO] git 内容口径披露｜{err}（🔴 只披露，不判红）")
        return
    print(f"[INFO] git 内容口径披露｜git status --porcelain -- <{len(rels)} 个冻结件>：")
    print(out.strip() if out.strip() else "（空 ⇒ 与 HEAD 一致）")
    print("[INFO]   ↳ ` M` ≠ V1 被破坏，只说明服务器检出落后于开发仓"
          "（🔴 本条只披露，不判红；判红看上面的 md5 基线）")


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


def _probe_b1_frozen(proj: Path, root: Path) -> None:
    """B1 组（V1 冻结件）—— 主判据 = **md5 基线清单**（**对环境免疫**）。

    🔴 项目根不存在 ⇒ 整组跳过（不判红、不计失败）；
       但**清单不可得 ⇒ 判红**（fail-closed，绝不静默通过）。
    """
    etf = proj / "manu" / "etf_rotation"

    # ---- 1. 清单：读不到 / 解析失败 / 为空 ⇒ FAIL（fail-closed） ----
    manifest_files, manifest_err = load_frozen_manifest(MANIFEST_PATH)
    if manifest_files is None:
        check("B1 V1 冻结件 md5 基线清单可读", False, manifest_err)
    else:
        check("B1 V1 冻结件 md5 基线清单可读", True,
              f"{MANIFEST_PATH}｜{len(manifest_files)} 个文件在册")

    # 🔴 项目根不可用 ⇒ **整组自动跳过**（不判红、不计失败），并说明原因。
    if not proj.is_dir():
        print(f"[INFO] 项目根不存在（{proj}）⇒ 整组跳过 B1 文件比对（不判红、不计失败）；"
              f"可用环境变量 ETF_V2_PROJECT_ROOT 指向工程根")
        return

    # ---- 2. B1 **主判据**：逐文件 md5 比对（清单口径，对环境免疫） ----
    if manifest_files is None:
        print("[INFO] 清单不可得 ⇒ 跳过逐文件 md5 比对（已按 fail-closed 判红，见上）")
    else:
        for rel, ok, detail in compare_frozen_manifest(proj, manifest_files):
            check(f"B1 V1 冻结件内容未被改写（md5 基线）：{rel}", ok, detail)

    # ---- 3. 动态基线（**只**用于 mtime 披露；🔴 读不到就 fail-soft 跳过披露） ----
    baseline_ts, baseline_note = _load_baseline(root)
    if baseline_ts is None:
        print(f"[INFO] 基线不可得（{baseline_note}）⇒ 跳过 mtime 披露"
              f"（🔴 不回退任何硬编码时间戳）")
    else:
        print(f"[INFO] mtime 披露基线｜来源={baseline_note}｜"
              f"基线值={_fmt_ts(baseline_ts)}（epoch={baseline_ts:.0f}）")

    # ---- 4. git 内容口径：**只披露**（服务器检出可能落后 ⇒ ` M` ≠ V1 被破坏） ----
    _disclose_git_status(proj, manifest_files)

    # ---- 5. mtime 只作**披露**（checkout / rsync 同步都会刷新 mtime ⇒ 不能当判据） ----
    for rel in _frozen_rel_paths(proj, manifest_files):
        _disclose_mtime(proj / rel, rel, baseline_ts)

    # ---- 6. analyze.py：**每日链分析层**（允许演进）⇒ 只披露，**不判红** ----
    for fname in DAILY_CHAIN_FILES:
        path = etf / fname
        if not path.is_file():
            print(f"[INFO] 每日链分析层披露｜manu/etf_rotation/{fname}｜文件不存在（{path}）")
            continue
        git_state = "（git 不可用，跳过）"
        toplevel = _git_toplevel(proj)
        if toplevel is not None:
            rel = _git_rel(toplevel, path)
            if rel is not None:
                out, err = _git_status(toplevel, [rel])
                git_state = err if err else (out.strip() or "空（与 HEAD 一致）")
        print(f"[INFO] 每日链分析层披露｜manu/etf_rotation/{fname}｜git status={git_state}")
        print("[INFO]   ↳ 每日链分析层，允许演进；V1 回测数字的基准文件是"
              " config.py / backtest.py / qlib_scores.py / engine/*.py")
        _disclose_mtime(path, f"manu/etf_rotation/{fname}", baseline_ts)


def probe_v1_frozen(root: Path) -> None:
    proj = _project_root()
    _probe_b1_frozen(proj, root)
    # 🔴 B2 **永远执行**（不随 B1 的自动跳过一起被吞掉）
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
        for base in (_project_root(), Path("/mnt/data")):
            p = base / name
            if p.exists():
                stray.append(str(p))
    check("D1b 工程根 / /mnt/data 根无 V2 散落产物", not stray,
          "无散落" if not stray else f"发现：{stray}")


def main() -> int:
    root = Path(_opt("ETF_V2_RUN_ROOT", str(C.DEFAULT_V2_RUN_ROOT)))
    print("=== probe_v2_artifacts · 产物 / V1 冻结 / 目录洁癖 ===")
    probe_artifacts(root)
    probe_v1_frozen(root)
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
