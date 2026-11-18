"""manu/tools/fetch_qlib_dataset.py 的下载器回归测试。

【为什么要有这个文件】
2026-11-16 服务器实跑事故：上游 `chenditc/investment_data@latest` 被**重新上传**，
资产字节数从 571,492,836 变成 571,459,530（内容等价）。aria2c 默认
`--auto-file-renaming=true`，目标文件已存在时它自动改名写 `qlib_bin.tar.1.gz`，
而下载器只检查 `qlib_bin.tar.gz` 的大小 ⇒ 每一轮都判「未完成」⇒
每轮空转约 12 分钟 x 40 轮 ≈ 8 小时死循环。

这两条断言把这个坑钉死：
  * aria2c 必须带 `--auto-file-renaming=false`（禁止改名，直接写 dest）；
  * 「本地字节数 >= 期望字节数」必须判为**已完整**（上游 re-upload 变小属正常）。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

#: `manu/tools/fetch_qlib_dataset.py` 的绝对路径
_DOWNLOADER_PATH = Path(__file__).resolve().parents[3] / "tools" / "fetch_qlib_dataset.py"


def _load_downloader():
    """按绝对路径加载下载器（`manu/tools/` 不是包）。

    🔴 必须先登记进 `sys.modules` 再执行：那份下载器是 `@dataclass` +
    `from __future__ import annotations`，dataclasses 反查 `sys.modules[cls.__module__]`
    时拿不到会炸（2026-11-05 服务器实测）。
    """
    spec = importlib.util.spec_from_file_location("manu_fetch_qlib_dataset_t", _DOWNLOADER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture()
def dl():
    return _load_downloader()


# =============================================================================
# ① aria2c 必须禁止自动改名
# =============================================================================
def test_aria2_extra_args_disable_auto_renaming(dl) -> None:
    assert "--auto-file-renaming=false" in dl.ARIA2_EXTRA_ARGS
    assert "--allow-overwrite=true" in dl.ARIA2_EXTRA_ARGS


def test_download_once_cmd_has_no_auto_rename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """download_once 真正下发的 aria2c 命令里必须含禁止改名的参数。"""
    recorded: list[list[str]] = []

    class _FakeProc:
        returncode = 0
        stdout = iter(())

        def __init__(self) -> None:
            pass

        def wait(self) -> int:
            return 0

    def _fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN202
        recorded.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(dl.shutil, "which", lambda name: "/usr/bin/aria2c" if name == "aria2c" else None)
    monkeypatch.setattr(dl.subprocess, "Popen", _fake_popen)

    dest = tmp_path / "qlib_bin.tar.gz"
    dl.download_once("https://example.invalid/a", dest, 100, 50 * 1024, 30)

    assert recorded, "aria2c 路径必须真的起进程"
    cmd = recorded[0]
    assert "--auto-file-renaming=false" in cmd
    assert "--allow-overwrite=true" in cmd
    # 不许再出现会触发改名的默认行为
    assert "--auto-file-renaming=true" not in cmd


# =============================================================================
#② 改名残留 / .aria2 控制文件的发现与清理
# =============================================================================
def test_renamed_siblings_detects_aria2_residue(dl, tmp_path: Path) -> None:
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"good")
    for name in ("qlib_bin.tar.1.gz", "qlib_bin.tar.2.gz", "qlib_bin.tar.3.gz"):
        (tmp_path / name).write_bytes(b"junk")
    (tmp_path / "qlib_bin.tar.gz.aria2").write_bytes(b"ctrl")
    (tmp_path / "other_file.txt").write_bytes(b"keep")

    found = {p.name for p in dl.renamed_siblings(dest)}
    assert found == {
        "qlib_bin.tar.1.gz",
        "qlib_bin.tar.2.gz",
        "qlib_bin.tar.3.gz",
        "qlib_bin.tar.gz.aria2",
    }


def test_cleanup_artifacts_removes_residue_keeps_dest(dl, tmp_path: Path) -> None:
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"good")
    (tmp_path / "qlib_bin.tar.1.gz").write_bytes(b"junk")
    (tmp_path / "qlib_bin.tar.gz.aria2").write_bytes(b"ctrl")

    removed = dl.cleanup_artifacts(dest)
    assert set(removed) == {"qlib_bin.tar.1.gz", "qlib_bin.tar.gz.aria2"}
    assert dest.exists(), "正式文件必须保留"
    assert dest.read_bytes() == b"good"


# =============================================================================
# ③ 「本地 >= 期望」必须判为已完整（上游 re-upload 变小）
# =============================================================================
def test_fetch_skips_download_when_local_not_smaller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """本地 571,492,836 B（旧的 re-upload 前资产） >= 期望 571,459,530 B ⇒ 直接判完整，不下载。"""
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"x" * 1000)
    called: list[int] = []

    monkeypatch.setattr(dl, "resolve_asset", lambda repo, tag, asset: ("https://example.invalid/a", 900))
    monkeypatch.setattr(dl, "gzip_ok", lambda path: True)
    monkeypatch.setattr(
        dl,
        "download_once",
        lambda *a, **k: called.append(1) or 0,
    )

    src = dl.Source(key="chenditc", repo="r", tag="latest", asset="qlib_bin.tar.gz", dest_dir=str(tmp_path))
    assert dl.fetch(src, 50 * 1024, 30, 3, False) is True
    assert not called, "本地已完整时绝不能再起下载"


def test_fetch_accepts_local_larger_than_expected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """本地比期望**大**（上游 re-upload 后资产变小）⇒ 同样判完整。"""
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"x" * 1000)
    called: list[int] = []

    monkeypatch.setattr(dl, "resolve_asset", lambda repo, tag, asset: ("https://example.invalid/a", 500))
    monkeypatch.setattr(dl, "gzip_ok", lambda path: True)
    monkeypatch.setattr(dl, "download_once", lambda *a, **k: called.append(1) or 0)

    src = dl.Source(key="chenditc", repo="r", tag="latest", asset="qlib_bin.tar.gz", dest_dir=str(tmp_path))
    assert dl.fetch(src, 50 * 1024, 30, 3, False) is True
    assert not called


def test_fetch_redownloads_when_gzip_broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """字节数够但 gzip 校验失败 ⇒ 必须删掉重下，不能拿坏文件当完整。"""
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"x" * 1000)
    monkeypatch.setattr(dl, "resolve_asset", lambda repo, tag, asset: ("https://example.invalid/a", 900))
    monkeypatch.setattr(dl, "gzip_ok", lambda path: False)

    def _fake_download(signed, dest_path, expected, min_speed, speed_time):  # noqa: ANN001
        dest_path.write_bytes(b"y" * 900)
        return 0

    monkeypatch.setattr(dl, "download_once", _fake_download)
    monkeypatch.setattr(dl, "gzip_ok", lambda path: False if path.stat().st_size == 1000 else True)

    src = dl.Source(key="chenditc", repo="r", tag="latest", asset="qlib_bin.tar.gz", dest_dir=str(tmp_path))
    assert dl.fetch(src, 50 * 1024, 30, 3, False) is True
    assert dest.stat().st_size == 900


# =============================================================================
# ④ 下载完成后判定用 >=
# =============================================================================
def test_download_completion_uses_greater_or_equal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """下载结束后本地字节数 >= 期望（多几个字节也算完整）⇒ 判定成功。"""
    dest = tmp_path / "qlib_bin.tar.gz"

    monkeypatch.setattr(dl, "resolve_asset", lambda repo, tag, asset: ("https://example.invalid/a", 900))

    def _fake_download(signed, dest_path, expected, min_speed, speed_time):  # noqa: ANN001
        dest_path.write_bytes(b"y" * 950)
        return 0

    monkeypatch.setattr(dl, "download_once", _fake_download)
    src = dl.Source(key="chenditc", repo="r", tag="latest", asset="qlib_bin.tar.gz", dest_dir=str(tmp_path))
    assert dl.fetch(src, 50 * 1024, 30, 3, False) is True
    assert dest.stat().st_size == 950


def test_cleanup_only_cli_entrypoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dl) -> None:
    """`--cleanup-only` 只清理副产物, 不联网下载。"""
    dest = tmp_path / "qlib_bin.tar.gz"
    dest.write_bytes(b"good")
    (tmp_path / "qlib_bin.tar.1.gz").write_bytes(b"junk")

    monkeypatch.setattr(sys, "argv", ["fetch_qlib_dataset.py", "--cleanup-only", "--dest-dir", str(tmp_path)])
    assert dl.main() == 0
    assert not (tmp_path / "qlib_bin.tar.1.gz").exists()
    assert dest.exists()
