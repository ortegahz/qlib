"""snapshot 产物**版本标记**（V2 现役 / V3 研发并存）的守卫测试。

【为什么需要】
用户原话（2026-11-26）：「**v2 还保留，我目前在用**，生成 snapshot 的时候，
**要注意区分是哪个版本的输出**，这一点也要写到 rule 里面」
⇒ 快照产物（manifest / 报告口径表 / 执行卡 / 分数旁挂文件）必须**自带版本标签**，
   否则 V2 与 V3 的产出混在一起 ⇒ 事后分不清哪份是哪一版算的。

【口径】
* 单一事实源 = ``daily_config.MODEL_VERSION``（默认 "V2"），非法值**导入即报错**（fail-closed）；
* 读侧对**缺字段**的老产物一律回落 "V2" ⇒ **V2 的现有判定零影响**；
* 快照目录名**不改**（字典序 == 时间序，是 ``select_artifact_root.sh`` /
  ``daily/snapshot.py`` / ``check_report_publish_sync.py`` 三处的依赖）。

【镜像副本】
本文件在 ``manu/etf_rotation/daily/tests/`` 与 ``manu/daily/tests/`` **逐字一致**（镜像纪律）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import agents_rebalance  # noqa: E402
import daily_config as DC  # noqa: E402
import snapshot as snap_mod  # noqa: E402


#: 🔴 ``manu/daily/`` 是**已登记过期副本**（镜像检查**显式豁免**；活链只有
#:    ``manu/etf_rotation/daily/``）⇒ 该副本**尚未同步**本特性 ⇒ 守卫在**副本侧跳过**
#:    （等副本重新同步后**自动生效**）。
#: 🔴 注意：这个跳过条件**只能在已登记过期副本里成立**
#:    ⇒ **活链侧不会跳过**（不影响下面的变异反证）。
_IS_STALE_MIRROR = DAILY_DIR.parent.name == "manu" and DAILY_DIR.name == "daily"
_HAS_MODEL_VERSION = hasattr(DC, "MODEL_VERSION")
pytestmark = pytest.mark.skipif(
    _IS_STALE_MIRROR and not _HAS_MODEL_VERSION,
    reason="manu/daily/ 是已登记过期副本（尚未同步 model_version 特性）"
    "⇒ 守卫在活链侧生效",
)


def _manifest_model_version(payload: dict) -> str:
    """**守卫本体**：从 manifest payload 取版本标签（缺键 ⇒ 直接转红**）。"""
    assert "model_version" in payload, "manifest 缺 model_version 键"
    return str(payload["model_version"])


def _write_old_manifest(root: Path, run_id: str = "20261120_160000") -> Path:
    """落一份**老 manifest**（没有 ``model_version`` 键）。"""
    path = root / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / snap_mod.MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "run_id": run_id, "asof": "2026-11-20"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# =============================================================================
# 1. manifest：必带 model_version 键 + 默认 V2
# =============================================================================
def test_manifest_必带_model_version_键(tmp_path: Path) -> None:
    """快照 manifest **必须**带 ``model_version`` 键（不得缺失）。"""
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26")
    payload = snap.manifest_payload()

    assert "model_version" in payload, "manifest 缺 model_version 键"
    assert _manifest_model_version(payload) == "V2", payload.get("model_version")


def test_默认值是_V2(tmp_path: Path) -> None:
    """不带参数时默认 ="V2"（**现役口径**）。"""
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26")

    assert snap.model_version == "V2"
    assert _manifest_model_version(snap.manifest_payload()) == "V2"


def test_create_snapshot_传_V3_manifest_就是_V3(tmp_path: Path) -> None:
    """``create_snapshot(..., model_version="V3")`` ⇒ manifest 里是 "V3"。"""
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26", model_version="V3")

    assert _manifest_model_version(snap.manifest_payload()) == "V3"
    snap.finalize()
    on_disk = json.loads((snap.path / snap_mod.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert on_disk["model_version"] == "V3"


# =============================================================================
# 2. 老快照兼容（缺字段 ⇒ 回落 V2）
# =============================================================================
def test_老_manifest_缺键_读侧回落_V2(tmp_path: Path) -> None:
    """老快照（没这个键）读侧一律回落 "V2" ⇒ **V2 现有判定零影响**。"""
    old = _write_old_manifest(tmp_path)

    assert snap_mod.manifest_model_version(old) == "V2"


def test_快照目录名不受影响(tmp_path: Path) -> None:
    """加版本标签**不得**改快照目录名（字典序 == 时间序）。"""
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26", model_version="V3")

    assert re.fullmatch(r"\d{8}_\d{6}", snap.run_id), snap.run_id
    assert snap.path.name == snap.run_id


# =============================================================================
# 3. 🔴 非法版本标签 ⇒ 必须报错（fail-closed）
# =============================================================================
def test_非法_MODEL_VERSION_必须_raise(monkeypatch) -> None:
    """``MODEL_VERSION`` 被改成非法值 ⇒ **必须 raise**（不许静默回落）。"""
    monkeypatch.setattr(DC, "MODEL_VERSION", "V9")

    with pytest.raises(ValueError):
        DC.validate_model_version(DC.MODEL_VERSION)


def test_非法环境变量_导入即报错(monkeypatch) -> None:
    """环境变量给非法值 ⇒ **导入 daily_config 即报错**（启动期 fail-closed）。"""
    monkeypatch.setenv("ETF_ROTATION_MODEL_VERSION", "V9")
    spec = importlib.util.spec_from_file_location(
        "daily_config_bad_version_probe", DAILY_DIR / "daily_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    try:
        with pytest.raises(ValueError):
            spec.loader.exec_module(module)
    finally:
        sys.modules.pop("daily_config_bad_version_probe", None)


def test_允许值口径() -> None:
    """允许的版本另行列（改它 ⇒ 守卫应转红）。"""
    assert DC.MODEL_VERSIONS == ("V1", "V2", "V3")
    assert DC.MODEL_VERSION in DC.MODEL_VERSIONS


# =============================================================================
# 4. 🔴 变异反证：删掉 manifest 的 model_version 键 ⇒ 守卫必转红
# =============================================================================
def test_变异反证_删掉_manifest_的版本键_守卫必转红(monkeypatch, tmp_path: Path) -> None:
    """把生产侧 ``manifest_payload()`` 的 ``model_version`` 键删掉 ⇒ **守卫必转红**。"""
    original = snap_mod.Snapshot.manifest_payload

    def _mutated(self):  # type: ignore[no-untyped-def]
        payload = original(self)
        payload.pop("model_version", None)
        return payload

    monkeypatch.setattr(snap_mod.Snapshot, "manifest_payload", _mutated)
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26")

    with pytest.raises(AssertionError):
        _manifest_model_version(snap.manifest_payload())


# =============================================================================
# 5. 分数旁挂文件 ``stock_scores.version.json``
# =============================================================================
def test_stock_scores_version_json_幂等(tmp_path: Path) -> None:
    """写两次 ⇒ 内容**完全一致**（不刷新 ``written_at``）。"""
    notes: list[str] = []

    first = agents_rebalance.write_scores_version(tmp_path, model_version="V2", notes=notes)
    payload_first = json.loads(first.read_text(encoding="utf-8"))
    assert payload_first["model_version"] == "V2"
    assert payload_first["scores_file"] == "stock_scores.csv"
    assert payload_first["written_at"]

    second = agents_rebalance.write_scores_version(tmp_path, model_version="V2", notes=notes)

    assert second == first
    assert json.loads(second.read_text(encoding="utf-8")) == payload_first


def test_stock_scores_version_json_不覆盖已有的不同标签(tmp_path: Path) -> None:
    """已有**不同**标签 ⇒ **保留首次**（行为确定）＋ 必须显式披露。"""
    agents_rebalance.write_scores_version(tmp_path, model_version="V2")
    notes: list[str] = []

    agents_rebalance.write_scores_version(tmp_path, model_version="V3", notes=notes)

    payload = json.loads(
        (tmp_path / agents_rebalance.SCORES_VERSION_NAME).read_text(encoding="utf-8")
    )
    assert payload["model_version"] == "V2", "已有标签不许被覆盖"
    assert notes, "标签不一致必须显式披露"


def test_stock_scores_version_json_不动指纹文件(tmp_path: Path) -> None:
    """只读指纹：**不许**改 ``stock_scores.fingerprint.json``（V1 md5 基线锁死）。"""
    fingerprint = tmp_path / agents_rebalance.SCORES_FINGERPRINT_NAME
    fingerprint.write_text(
        json.dumps({"model_md5": "39fea96414e8adcfb731a992e57127ac"}, ensure_ascii=False),
        encoding="utf-8",
    )
    before = fingerprint.read_bytes()

    agents_rebalance.write_scores_version(tmp_path, model_version="V2")

    assert fingerprint.read_bytes() == before, "指纹文件被改了"
    payload = json.loads(
        (tmp_path / agents_rebalance.SCORES_VERSION_NAME).read_text(encoding="utf-8")
    )
    assert payload["source_fingerprint_md5"] == hashlib.md5(before).hexdigest()
    assert (tmp_path / agents_rebalance.SCORES_VERSION_NAME).is_file()


# =============================================================================
# 6. 执行卡 + 报告口径表 + CLI
# =============================================================================
def test_执行卡带_model_version(tmp_path: Path) -> None:
    """执行卡**必须**带 ``model_version``（原本完全无版本字段）。"""
    card: dict = {"orders": []}

    path = agents_rebalance.persist_execution_card(
        card, tmp_path, "2026-11-26", model_version="V3"
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["model_version"] == "V3"


def test_报告口径表带打分模型版本() -> None:
    """报告的「快照与运行口径」表必须有「打分模型版本」一行。"""
    import report as report_mod

    rows = report_mod._provenance_rows(
        {"snapshot": {"path": "", "model_version": "V3"}}, live_trade=False
    )
    values = {str(row[0]): str(row[1]) for row in rows}

    assert "打分模型版本" in values, values
    assert values["打分模型版本"] == "V3"


def test_报告从_manifest_读版本标签(tmp_path: Path) -> None:
    """报告从**快照 manifest**读版本标签（不靠内存参数）。"""
    import report as report_mod

    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-26", model_version="V3")
    snap.finalize()
    rows = report_mod._provenance_rows({"snapshot": {"path": str(snap.path)}}, live_trade=False)
    values = {str(row[0]): str(row[1]) for row in rows}

    assert values["打分模型版本"] == "V3"


def test_报告读老_manifest_回落_V2(tmp_path: Path) -> None:
    """报告读**老 manifest**（缺键）⇒ 回落现役口径（缺省 = "V2"）。"""
    import report as report_mod

    old = _write_old_manifest(tmp_path)
    rows = report_mod._provenance_rows({"snapshot": {"path": str(old)}}, live_trade=False)
    values = {str(row[0]): str(row[1]) for row in rows}

    assert values["打分模型版本"] == DC.MODEL_VERSION


def test_cli_model_version_参数() -> None:
    """CLI ``--model-version``：默认 = ``daily_config.MODEL_VERSION``，非法值不许过。"""
    import main as daily_main

    assert daily_main.parse_args([]).model_version == DC.MODEL_VERSION
    assert daily_main.parse_args(["--model-version", "V3"]).model_version == "V3"
    with pytest.raises(SystemExit):
        daily_main.parse_args(["--model-version", "V9"])
