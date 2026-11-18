"""🔴 V2 **val 段 cadence/buffer 扩格探针**的护栏测试（2026-11-18 夜班）。

【背景】
  正式搜索的 cadence 候选只有 {20,40,60,90}、Rank Buffer 只到 4
  ⇒ 「再放缓节奏 / 再放宽缓冲带」这一格**根本没被搜过**
  ⇒ 夜班新增 `manu/tools/v2_val_cadence_probe.py` **只在 val 段**回答
  「年化换手 ≤4× 到底可达吗？」。

【本文件钉死 4 条护栏】
  ① **只读 val 段**：探针源码**不许** import `evaluate`、**不许**调 `stage_blind`
     ⇒ 测试段与一次性单盲 token **不可能**被碰；
  ② **不许改仓库代码**：cadence 候选**只在内存里**扩 ⇒ `config_v2.py` 的 md5 不变；
  ③ **非法 cadence 直接报错**（不是 10 的整数倍 ⇒ 调仓栅格会漂 ⇒ 必须 raise）；
  ④ **已封存产物守卫**：探针跑前跑后比对单盲 token / 登记表 / 冻结 / 正式搜索产物的 md5。

⛔ 不联网、不跑回测、不读真实产物（除 `--dry-run` 的只读动作）。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL = REPO_ROOT / "manu" / "tools" / "v2_val_cadence_probe.py"
PYTHON = sys.executable


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dry_run(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(TOOL), "--dry-run", *extra],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )


def test_1_探针源码不许碰单盲与测试段() -> None:
    """🔴 探针**不许** import `evaluate`、**不许**调 `stage_blind`。"""
    src = TOOL.read_text(encoding="utf-8")
    assert "import evaluate" not in src, "🔴 探针不许 import evaluate（会碰一次性单盲 token）"
    assert "stage_blind" not in src, "🔴 探针不许调 stage_blind（会重跑一次性单盲段）"
    assert "C.TEST" not in src, "🔴 探针**不许**引用测试段窗口（`C.TEST`）"
    assert "stage_search" in src, (
        "🔴 探针必须复用已审计的 `run_v2.stage_search`（它内部才有 `assert_val_only` 真闸门）")
    assert "val_window" not in src.split("def main")[0], (
        "🔴 探针**不许**自带窗口参数（窗口只能由 `stage_search` 从 `config_v2.VALID` 取）")


def test_2_探针不许改仓库代码_config_v2_md5不变() -> None:
    """🔴 cadence 候选**只在内存里**扩 ⇒ `config_v2.py` 的 md5 必须不变。"""
    cfg = REPO_ROOT / "manu" / "etf_rotation" / "v2" / "config_v2.py"
    before = _md5(cfg)
    proc = _dry_run("--cadence-grid", "90,120,150")
    assert proc.returncode == 0, f"🔴 dry-run 失败：{proc.stderr[-500:]}"
    assert _md5(cfg) == before, "🔴 探针**不许**改仓库代码（config_v2.py md5 变了）"


def test_3_非法cadence_必须直接报错() -> None:
    """🔴 cadence 必须是 10 的整数倍（否则调仓栅格会漂）⇒ 非法的必须**报错**。"""
    proc = _dry_run("--cadence-grid", "125")
    assert proc.returncode != 0, "🔴 非法 cadence（125）必须报错，不许静默取整"
    assert "整数倍" in (proc.stdout + proc.stderr), (
        f"🔴 报错信息应说明「整数倍」：{(proc.stdout + proc.stderr)[-300:]}")


def test_4_已封存产物守卫名单齐全() -> None:
    """🔴 探针必须把**已封存的单盲/冻结/正式搜索产物**纳入跑前跑后 md5 比对。"""
    src = TOOL.read_text(encoding="utf-8")
    for name in ("single_blind_test_2024_2026.json", "_single_blind_registry.json",
                 "frozen_params.json", "arm_search_val.json", "val_search.json"):
        assert name in src, f"🔴 守卫名单缺 {name}（探针必须证明没动它）"
    assert "changed" in src and "不合格" in src, (
        "🔴 探针必须在检测到封存产物被改动时**硬失败**（不许只打印一行）")
