"""🔴 2026-11-25 新增：**V1 冻结基线清单（md5）**守卫 —— B1 闸门的牙齿就住在这里。

【背景 · B1 判据换过两轮，都是"常年红"】
  ① 硬编码一个 epoch 时间戳当 mtime 基线 ⇒ ``analyze.py``（每日链分析层，合法演进）
     的 mtime 永远晚于基线 ⇒ **常年红 + 告警疲劳**；
  ② 改用 ``git status --porcelain`` 判「内容口径」⇒ 服务器工程目录的 git 检出
     **落后于**开发仓 ⇒ 对冻结件天然显示 `` M`` ⇒ **另一种常年红**（而且会被读成
     「V1 被破坏」⇒ 指向错误结论）。
⇒ 现行口径：**md5 基线清单**（``manu/etf_rotation/v2/probes/v1_frozen_md5.json``，
  就放在**探针自己旁边** ⇒ 随 ``manu/`` 一起 rsync 到服务器 ⇒ 服务器**没 git 也有牙齿**）。

【本文件钉死了什么】
  T1 清单存在、可解析、``files`` 非空（fail-closed 的前提）
  T2 清单里每个 md5 == 当前磁盘文件 md5（**故意改坏会红** ⇒ 逼人显式重新生成清单）
  T3 冻结集**恰好**是 config/backtest/qlib_scores ＋ engine/*.py，**不含** analyze.py
  T4 **有牙**：造临时目录（改一个字 / 删一个文件）⇒ 纯函数必须报 FAIL
  T5 探针源码里**没有**硬编码 epoch 时间戳（不许再回退硬编码基线）
  T6 探针源码里 ``analyze.py`` 只出现在**披露**上下文（不许混进冻结集）

【本机可跑】不依赖服务器工程根（``/tmp/pycharm_project_3b68a403``）、不依赖 ``/mnt/data``。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

#: 探针与清单在仓库里的相对路径（相对**仓库根**）
PROBE_RELPATH = Path("manu/etf_rotation/v2/probes/probe_v2_artifacts.py")
MANIFEST_RELPATH = Path("manu/etf_rotation/v2/probes/v1_frozen_md5.json")
#: 冻结集里**逐个点名**的文件（相对仓库根）
FROZEN_SINGLE_RELPATHS = (
    "manu/etf_rotation/config.py",
    "manu/etf_rotation/backtest.py",
    "manu/etf_rotation/qlib_scores.py",
)
#: 冻结集里**整目录**的文件（相对仓库根）
FROZEN_DIR_RELPATH = "manu/etf_rotation/engine"
#: 🔴 每日链分析层（允许演进 ⇒ **不许**进冻结集）
DAILY_CHAIN_RELPATH = "manu/etf_rotation/analyze.py"
#: 🔴 硬编码 epoch 时间戳（10 位整数秒）—— 探针源码里**不许**出现
EPOCH_LITERAL_RE = re.compile(r"\b1[0-9]{9}(\.[0-9]+)?\b")
#: 🔴 ``analyze.py`` 只许出现在这些**披露**标记旁边
DISCLOSURE_MARKERS = ("披露", "DAILY_CHAIN", "每日链")


def _repo_root() -> Path:
    """向上找仓库根（含探针文件）—— 不写死 ``parents[N]``（本文件可能被镜像到别处）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / PROBE_RELPATH).is_file():
            return parent
    raise RuntimeError("找不到仓库根（缺 %s）" % PROBE_RELPATH)


REPO_ROOT = _repo_root()


def _load_probe():
    """按**路径**加载探针模块（``manu/`` 不在默认 sys.path 上）。

    🔴 探针自己会把 ``manu/etf_rotation/v2/`` 插进 ``sys.path`` ⇒ 加载完**还原**
       （否则 v2 的 ``features.py`` / ``report.py`` 会污染整个 pytest 会话的顶层 import）。
    """
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "probe_v2_artifacts_under_test", REPO_ROOT / PROBE_RELPATH
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


P = _load_probe()


def _md5(path: Path) -> str:
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _manifest() -> dict:
    return json.loads((REPO_ROOT / MANIFEST_RELPATH).read_text(encoding="utf-8"))


def _expected_frozen_set() -> set:
    """⇒ 冻结集应当**恰好**是哪些文件（从磁盘现状派生，不手写清单）。"""
    expected = set(FROZEN_SINGLE_RELPATHS)
    engine_dir = REPO_ROOT / FROZEN_DIR_RELPATH
    for path in sorted(engine_dir.rglob("*.py")):
        expected.add(str(path.relative_to(REPO_ROOT)))
    return expected


def _by_rel(results) -> dict:
    """⇒ 纯函数结果 ⇒ ``{相对路径: (是否通过, 明细)}``。"""
    return {rel: (ok, detail) for rel, ok, detail in results}


def _regen_hint() -> str:
    """⇒ 清单里写着的**重新生成**口径（失败信息里原样带上 ⇒ 下一个人照抄即可）。"""
    return str(_manifest().get("_comment", ""))


# ---------------------------------------------------------------- T1
def test_t1_manifest_exists_and_is_parsable() -> None:
    """T1 清单存在、可解析、``files`` 非空。"""
    path = REPO_ROOT / MANIFEST_RELPATH
    assert path.is_file(), "V1 冻结基线清单不存在：%s（清单是 B1 的主判据 ⇒ 必须存在）" % path
    payload = _manifest()
    assert isinstance(payload, dict), "清单顶层必须是对象"
    files = payload.get("files")
    assert isinstance(files, dict) and files, "清单里 files 必须是非空对象"
    assert payload.get("generated_at"), "清单必须记 generated_at（便于判断基线新鲜度）"
    assert payload.get("_comment"), "清单必须写 _comment（说明怎么重新生成）"


# ---------------------------------------------------------------- T2
def test_t2_manifest_md5_matches_disk() -> None:
    """T2 清单里每个 md5 == 当前磁盘文件 md5（**改坏会红** ⇒ 逼人显式重新生成清单）。"""
    files = _manifest()["files"]
    mismatches = []
    for rel, expected in sorted(files.items()):
        path = REPO_ROOT / rel
        if not path.is_file():
            mismatches.append("%s：文件缺失" % rel)
            continue
        actual = _md5(path)
        if actual != expected:
            mismatches.append("%s：expected=%s actual=%s" % (rel, expected, actual))
    assert not mismatches, (
        "V1 冻结基线清单与磁盘漂移（%s）⇒ 必须**显式重新生成**清单（不许手改 md5）：\n%s"
        % ("；".join(mismatches), _regen_hint())
    )


# ---------------------------------------------------------------- T3
def test_t3_frozen_set_is_exact_and_excludes_analyze() -> None:
    """T3 冻结集**恰好**是 config/backtest/qlib_scores ＋ engine/*.py，且**不含** analyze.py。"""
    files = set(_manifest()["files"])
    expected = _expected_frozen_set()
    assert files == expected, (
        "冻结集不匹配：\n  清单多出：%s\n  清单缺少：%s"
        % (sorted(files - expected), sorted(expected - files))
    )
    assert DAILY_CHAIN_RELPATH not in files, "analyze.py 属每日链分析层 ⇒ **不许**进冻结集"
    assert not any(rel.endswith("analyze.py") for rel in files), "analyze.py 不许进冻结集"


# ---------------------------------------------------------------- T4
def test_t4_guard_has_teeth(tmp_path: Path) -> None:
    """T4 **有牙**：改一个字 / 删一个文件 ⇒ 纯函数必须报 FAIL。"""
    manifest_files = _manifest()["files"]
    sandbox = tmp_path / "project_root"
    for rel in manifest_files:
        target = sandbox / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO_ROOT / rel).read_bytes())

    # ① 原样 ⇒ 全通过（先证明沙箱本身是绿的）
    baseline = P.compare_frozen_manifest(sandbox, manifest_files)
    assert baseline, "纯函数没有任何结果 ⇒ 判据没跑起来"
    assert all(ok for _, ok, _ in baseline), "原样副本竟然不通过：%s" % baseline

    # ② 改一个字 ⇒ 必须报 FAIL，且明细写 expected vs actual
    victim = min(manifest_files)
    target = sandbox / victim
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    after_edit = _by_rel(P.compare_frozen_manifest(sandbox, manifest_files))
    ok, detail = after_edit[victim]
    assert not ok, "冻结件被改了一个字却没报红 ⇒ 守卫没牙"
    assert "expected=" in detail and "actual=" in detail, "明细必须写 expected vs actual：%s" % detail

    # ③ 删一个文件 ⇒ 必须报 FAIL（明细点名缺失路径）
    missing = max(manifest_files)
    (sandbox / missing).unlink()
    after_delete = _by_rel(P.compare_frozen_manifest(sandbox, manifest_files))
    ok, detail = after_delete[missing]
    assert not ok, "冻结件被删了却没报红 ⇒ 守卫没牙"
    assert "缺失" in detail, "明细必须点名文件缺失：%s" % detail


# ---------------------------------------------------------------- T4b
def test_t4b_unreadable_manifest_is_fail_closed(tmp_path: Path) -> None:
    """T4b 清单读不到 / 解析失败 / 为空 ⇒ 必须 fail-closed（不许静默通过）。"""
    missing = P.load_frozen_manifest(tmp_path / "nope.json")
    assert missing[0] is None and missing[1], "清单不存在时必须返回失败原因"

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    parsed = P.load_frozen_manifest(broken)
    assert parsed[0] is None and parsed[1], "清单解析失败时必须返回失败原因"

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"files": {}}), encoding="utf-8")
    parsed = P.load_frozen_manifest(empty)
    assert parsed[0] is None and parsed[1], "清单 files 为空时必须返回失败原因"


# ---------------------------------------------------------------- T5
def test_t5_probe_has_no_hardcoded_epoch_timestamp() -> None:
    """T5 探针源码里**没有**硬编码 epoch 时间戳（🔴 不许回退硬编码基线）。"""
    source = (REPO_ROOT / PROBE_RELPATH).read_text(encoding="utf-8")
    hits = EPOCH_LITERAL_RE.findall(source)
    assert not hits, (
        "探针源码里出现硬编码 epoch 时间戳 %s ⇒ mtime 基线必须**动态**读 V2 冻结件"
        "（``frozen_params_wf.json`` → ``frozen_params.json``）" % hits
    )


# ---------------------------------------------------------------- T6
def test_t6_analyze_py_is_disclosure_only() -> None:
    """T6 ``analyze.py`` 只出现在**披露**上下文（不许出现在冻结集常量里）。"""
    assert "analyze.py" not in P.V1_FROZEN_FILES, "analyze.py 不许进 V1 冻结文件常量"
    assert "analyze.py" not in P.V1_FROZEN_DIRS, "analyze.py 不许进 V1 冻结目录常量"
    files = _manifest()["files"]
    assert not any("analyze.py" in rel for rel in files), "analyze.py 不许进清单"

    source = (REPO_ROOT / PROBE_RELPATH).read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if "analyze.py" in line and not any(marker in line for marker in DISCLOSURE_MARKERS):
            offenders.append("%d: %s" % (lineno, line.strip()))
    assert not offenders, (
        "analyze.py 只许出现在**披露**上下文（%s）：\n%s"
        % ("、".join(DISCLOSURE_MARKERS), "\n".join(offenders))
    )


# ---------------------------------------------------------------- 额外护栏
def test_probe_project_root_default_is_unchanged() -> None:
    """🔴 项目根默认值**不许改**（改了等于换判据语义）；环境变量仍可覆盖。"""
    assert P.DEFAULT_PROJECT_ROOT == "/tmp/pycharm_project_3b68a403"
    saved = {key: value for key, value in __import__("os").environ.items()
             if key == "ETF_V2_PROJECT_ROOT"}
    try:
        __import__("os").environ["ETF_V2_PROJECT_ROOT"] = "/somewhere/else"
        assert str(P._project_root()) == "/somewhere/else"
    finally:
        if "ETF_V2_PROJECT_ROOT" in saved:
            __import__("os").environ["ETF_V2_PROJECT_ROOT"] = saved["ETF_V2_PROJECT_ROOT"]
        else:
            __import__("os").environ.pop("ETF_V2_PROJECT_ROOT", None)
    assert str(P._project_root()) == P.DEFAULT_PROJECT_ROOT


def test_manifest_paths_are_project_root_relative() -> None:
    """🔴 清单里的路径必须相对**项目根**（不是仓库根、不是绝对路径）。"""
    for rel in _manifest()["files"]:
        assert not rel.startswith("/"), "清单路径不许是绝对路径：%s" % rel
        assert rel.startswith("manu/etf_rotation/"), "清单路径必须相对项目根：%s" % rel


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
