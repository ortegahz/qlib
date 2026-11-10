"""🔴 2026-11-09 新增：canonical 只读性探针自身的正负例。

被探针测的是「跑真实流水线 ⇒ canonical 的 size+sha1+mtime 三项指纹不变」
（rules.md §15.3.3）。本文件测的是**探针自己**：
  * canonical 被改了 ⇒ 探针必须**报红**（rc=1）；
  * canonical 没被改 ⇒ 探针必须**报绿**（rc=0）；
  * 环境不具备（无 canonical）⇒ rc=2「不适用」（≠ 通过）。

（子进程用假 `subprocess.run` 顶替 ⇒ 不跑真流水线、不起任何重活。）
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from manu.etf_rotation.probes import probe_canonical_immutability as P  # type: ignore  # noqa: E402

#: 真实工程根（含 `manu/etf_rotation/daily/main.py`）—— 探针要认它
REPO_ROOT = str(Path(P.__file__).resolve().parents[3])


def _fake_proc(rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["main.py"], returncode=rc, stdout="", stderr="")


def _setup(tmp_path: Path, monkeypatch) -> Path:
    research_root = tmp_path / "research_root"
    research_root.mkdir(parents=True, exist_ok=True)
    canonical = research_root / "stock_scores.csv"
    canonical.write_text("code,score\n510300,1.0\n", encoding="utf-8")
    monkeypatch.setenv("ETF_ROTATION_RESEARCH_RUN_ROOT", str(research_root))
    return canonical


def test_not_applicable_without_canonical(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETF_ROTATION_RESEARCH_RUN_ROOT", str(tmp_path / "nothing"))
    rc = P.main(["--asof", "2026-11-05", "--repo-root", REPO_ROOT])
    assert rc == 2, f"环境不具备 ⇒ 必须 rc=2（不适用，≠ 通过），实得 {rc}"


def test_green_when_canonical_untouched(tmp_path, monkeypatch) -> None:
    canonical = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: _fake_proc(0))
    rc = P.main(["--asof", "2026-11-05", "--repo-root", REPO_ROOT])
    assert rc == 0, f"canonical 未被改 ⇒ 必须 rc=0，实得 {rc}"
    assert canonical.read_text(encoding="utf-8") == "code,score\n510300,1.0\n"


def test_red_when_canonical_written(tmp_path, monkeypatch) -> None:
    """🔴 变异负例：模拟「流水线把 canonical 改了」⇒ 探针必须报红。"""
    canonical = _setup(tmp_path, monkeypatch)

    def _evil_run(*a, **k):
        canonical.write_text("code,score\n510300,999.0\n", encoding="utf-8")
        return _fake_proc(0)

    monkeypatch.setattr(P.subprocess, "run", _evil_run)
    rc = P.main(["--asof", "2026-11-05", "--repo-root", REPO_ROOT])
    assert rc == 1, f"canonical 被改 ⇒ 必须 rc=1（报红），实得 {rc}"


def test_red_when_only_mtime_changed(tmp_path, monkeypatch) -> None:
    """🔴 只改 mtime（内容没变）也算**被碰过** ⇒ 必须报红（三项指纹缺一不可）。"""
    canonical = _setup(tmp_path, monkeypatch)

    def _touch_run(*a, **k):
        st = canonical.stat()
        os.utime(canonical, (st.st_atime + 10, st.st_mtime + 10))
        return _fake_proc(0)

    monkeypatch.setattr(P.subprocess, "run", _touch_run)
    rc = P.main(["--asof", "2026-11-05", "--repo-root", REPO_ROOT])
    assert rc == 1, f"只改 mtime 也算被碰过 ⇒ 必须 rc=1，实得 {rc}"
