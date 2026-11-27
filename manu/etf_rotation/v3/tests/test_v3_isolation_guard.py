"""🔴 V3 **隔离守卫**测试：V3 绝不许改 V2 / 不许 import V1 冻结件 / 绝不开行业中性化。

**本文件钉的判定**
  ① **V2 冻结件 md5 与磁盘一致**（清单 = ``v3/probes/v2_frozen_md5.json``）
     ⇒ 🔴 这就是「V3 没改 V2」的 **teeth**：谁改了 V2 ⇒ 这条测试立刻转红；
  ② **V3 源码没有 import V1 冻结件**（AST 扫描：``config`` / ``backtest`` /
     ``qlib_scores`` / ``engine.*``）⇒ 🔴 附**变异反证**（造一个坏 import ⇒ 必须被抓住）；
  ③ ``INDUSTRY_NEUTRALIZATION_ENABLED`` **恒为 False**（monkeypatch 成 True ⇒ 必须转红）。

🔴 本文件**只读**、不联网、不读 /mnt/data、不训练。
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402
import neutralize  # noqa: E402

#: 🔴 **V1 冻结件**的模块名片段（V1 已被 v2/probes/v1_frozen_md5.json 锁死）
V1_BANNED = ("config", "backtest", "qlib_scores", "engine")


def _project_root() -> Path:
    """项目根（含 ``manu/etf_rotation/v2/config_v2.py`` 的那一层）。"""
    for candidate in [_V3_DIR, *_V3_DIR.parents]:
        if (candidate / "manu" / "etf_rotation" / "v2" / "config_v2.py").is_file():
            return candidate
    raise FileNotFoundError("🔴 找不到项目根")


def _manifest_path() -> Path:
    """V2 冻结件 md5 清单。"""
    return _V3_DIR / "probes" / "v2_frozen_md5.json"


def _md5(path: Path) -> str:
    """文件 md5。"""
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _banned_imports(source: str) -> list[str]:
    """扫出一份源码里所有 import 到的 **V1 冻结件** 名字。"""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
            names.extend(alias.name for alias in node.names)
    hits = []
    for name in names:
        parts = name.split(".")
        if any(part in V1_BANNED for part in parts):
            hits.append(name)
    return hits


# ----------------------------------------------------------------- ① V2 冻结件
class TestV2Frozen:
    def test_清单可读且非空(self) -> None:
        manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
        assert manifest.get("files"), "🔴 V2 冻结件清单为空 ⇒ 判据失效"

    def test_每个冻结件的md5与磁盘一致(self) -> None:
        manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
        root = _project_root()
        drift = []
        for rel, expected in manifest["files"].items():
            target = root / rel
            if not target.is_file():
                drift.append(f"{rel}: 文件缺失")
                continue
            actual = _md5(target)
            if actual != expected:
                drift.append(f"{rel}: 期望 {expected}，实测 {actual}")
        assert not drift, (
            "🔴 V2 冻结件被改动（V2 是**现役**口径，V3 绝不许就地改写）：\n  "
            + "\n  ".join(drift)
        )

    def test_冻结集覆盖V2关键文件(self) -> None:
        manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
        names = {Path(rel).name for rel in manifest["files"]}
        for expected in ("config_v2.py", "labels.py", "features.py", "splits.py",
                         "etf_layer.py", "train_s.py", "backtest_v2.py", "metrics.py",
                         "run_v2.py"):
            assert expected in names, f"🔴 冻结清单缺 {expected}"


# ----------------------------------------------------------------- ② V1 冻结 import
class TestNoV1Imports:
    def test_V3源码不import_V1冻结件(self) -> None:
        hits = []
        for path in sorted(_V3_DIR.rglob("*.py")):
            found = _banned_imports(path.read_text(encoding="utf-8"))
            if found:
                hits.append(f"{path.name}: {found}")
        assert not hits, "🔴 V3 源码 import 了 V1 冻结件：\n  " + "\n  ".join(hits)

    def test_变异反证_坏import必须被抓住(self) -> None:
        """🔴 造一个「import qlib_scores / from engine import features」的坏源码
        ⇒ 扫描器**必须**报出来 ⇒ 证明上面那条判定有牙。"""
        bad = "import qlib_scores\nfrom engine import features\n"
        hits = _banned_imports(bad)
        assert "qlib_scores" in hits
        assert "engine" in hits

    def test_变异反证_相对import的根config也要被抓住(self) -> None:
        bad = "from . import config\n"
        assert _banned_imports(bad), "🔴 相对 import 的根 config 没被抓住 ⇒ 扫描器有洞"

    def test_正常源码不得误报(self) -> None:
        ok = "import config_v3 as C\nfrom . import universe\nimport numpy as np\n"
        assert not _banned_imports(ok)


# ----------------------------------------------------------------- ③ 行业中性化
class TestIndustryNeutralization:
    def test_常量恒为False(self) -> None:
        assert C.INDUSTRY_NEUTRALIZATION_ENABLED is False

    def test_变异反证_改成True必须转红(self, monkeypatch) -> None:
        """🔴 把常量 monkeypatch 成 True ⇒ 守卫**必须** raise（不许悄悄跑行业中性化）。"""
        monkeypatch.setattr(C, "INDUSTRY_NEUTRALIZATION_ENABLED", True)
        with pytest.raises(AssertionError, match="行业中性化"):
            neutralize.assert_no_industry_neutralization()

    def test_常量恢复False后守卫放行(self, monkeypatch) -> None:
        monkeypatch.setattr(C, "INDUSTRY_NEUTRALIZATION_ENABLED", False)
        neutralize.assert_no_industry_neutralization()
