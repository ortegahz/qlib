"""🔴 S 层**生产权重**守卫（2026-11-19）。

背景：每日流水线的 S 层打分长期加载的是 **2026-11-04 训的验证模型**
（``fit 2016-01-01~2021-12-31``、valid 2022~2023）⇒ 训练段只覆盖到 2021 年底，
**不是**「用 2016~2026 全量行情重训」的生产模型 ⇒ 实盘用它打分 = 用验证模型出实盘信号。

本文件钉死三件事：
1. **生产切分的隔离带**：训练末日 = 数据末端 − 25 个交易日（标签窗口须完整落在数据内）；
2. **模型与归一化同源**：infer 端的 fit 窗必须来自**生产模型自己的 manifest**；
   🔴 **生产目录缺 manifest ⇒ 抛错（fail-closed）**，绝不回落旧口径
   （只有**非生产目录**的旧模型才允许回落）；
3. **不得回退到验证模型**：生产 manifest 一旦存在，infer 端**必须**读到它。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
#: 🔴 本文件是**镜像对**（`manu/tests/` ↔ `manu/etf_rotation/tests/` ⇒ 逐字一致）
#:    ⇒ 可能被**镜像侧**（`manu/tests/`）收集 ⇒ 仓库根要认得**两种**位置：
#:    活链侧（`manu/etf_rotation/tests/` ⇒ 上溯两级到 `manu/`）、
#:    镜像侧（`manu/tests/` ⇒ 上溯一级到 `manu/`）。
_ROOT = _HERE.parents[2] if _HERE.parent.parent.name == "etf_rotation" else _HERE.parents[1]

#: 🔴 **按文件路径加载活链** `manu/etf_rotation/qlib_scores.py`。
#:   不能靠 `import qlib_scores` —— `manu/` 与 `manu/etf_rotation/` **同名模块并存**
#:   ⇒ 裸 import 命中哪一份**取决于 sys.path 顺序** ⇒ 测到的可能不是活链。
#:   每日流水线实际执行的也是活链（`daily/agents_rebalance.py` 的
#:   `_RESEARCH_ROOT/qlib_scores.py`，`_RESEARCH_ROOT = manu/etf_rotation/`）。
_QS_PATH = _ROOT / "etf_rotation" / "qlib_scores.py"
_spec = importlib.util.spec_from_file_location("etf_rotation_qlib_scores", _QS_PATH)
QS = importlib.util.module_from_spec(_spec)
sys.modules["etf_rotation_qlib_scores"] = QS
_spec.loader.exec_module(QS)


def _calendar(n: int, end: str = "2026-11-18") -> list[str]:
    """造 n 个连续交易日（ISO），末位 = end。"""
    import datetime as dt

    d = dt.date.fromisoformat(end)
    out: list[str] = []
    cur = d
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur -= dt.timedelta(days=1)
    return sorted(out)


def test_生产切分_训练末日正好比数据末端早25个交易日():
    cal = _calendar(200)
    sp = QS.production_split("2026-11-18", 25, calendar_dates=cal)
    assert sp["train_end_raw"] == "2026-11-18"
    assert sp["train_end"] == cal[-1 - 25], sp
    assert sp["embargo_days"] == 25


def test_生产切分_训练段末端之后的样本一条都不许进训练():
    cal = _calendar(200)
    sp = QS.production_split("2026-11-18", 25, calendar_dates=cal)
    after = [d for d in cal if d > sp["train_end"]]
    assert len(after) == 25, (sp, len(after))
    assert max(after) == "2026-11-18"


def test_生产切分_归一化fit窗等于训练段_不引入末端分布():
    cal = _calendar(200)
    sp = QS.production_split("2026-11-18", 25, calendar_dates=cal)
    assert sp["fit_start"] == sp["train_start"]
    assert sp["fit_end"] == sp["train_end"]
    assert sp["fit_end"] < sp["train_end_raw"]


def test_生产切分_交易日历不够扣隔离带就拒绝():
    with pytest.raises(RuntimeError):
        QS.production_split("2026-11-18", 25, calendar_dates=_calendar(10))


def test_生产切分_默认隔离带与V2研究链同口径():
    """25 = max(EMBARGO_DAYS 20, LABEL_HORIZON_DAYS 25) ⇒ 与 V2 研究链同一事实源。"""
    sys.path.insert(0, str(_ROOT / "etf_rotation" / "v2"))
    import splits as SP  # noqa: E402

    assert SP.effective_embargo_days() == QS.PRODUCTION_EMBARGO_DAYS == 25


def test_infer端fit窗_读生产模型manifest(tmp_path):
    (tmp_path / QS.PRODUCTION_MANIFEST_NAME).write_text(
        json.dumps({
            "fit_segment": {"fit_start": "2016-01-01", "fit_end": "2026-10-14"},
            "train_segment": {"train_end_raw": "2026-11-18", "embargo_days": 25},
        }, ensure_ascii=False),
        encoding="utf-8")
    fs, fe, info = QS.resolve_fit_segment(tmp_path)
    assert (fs, fe) == ("2016-01-01", "2026-10-14"), (fs, fe)
    assert info["train_segment"]["embargo_days"] == 25


def test_infer端fit窗_没有manifest才回落旧口径且可查(tmp_path):
    fs, fe, info = QS.resolve_fit_segment(tmp_path)
    assert info == {}, info
    assert fs == QS.C.STOCK.FIT_START and fe == QS.C.STOCK.FIT_END


def test_生产模型manifest存在时绝不许回落验证模型口径(tmp_path):
    """🔴 核心守卫：生产 fit 窗 ≠ 旧验证 fit 窗 ⇒ 一旦读到就必须用它。"""
    (tmp_path / QS.PRODUCTION_MANIFEST_NAME).write_text(
        json.dumps({"fit_segment": {"fit_start": "2016-01-01", "fit_end": "2026-10-14"}}),
        encoding="utf-8")
    fs, fe, _ = QS.resolve_fit_segment(tmp_path)
    assert fe != QS.C.STOCK.FIT_END, "生产模型却读回了 2021-12-31 的验证口径 fit 窗"
    assert fe == "2026-10-14"


def test_生产模型默认目录_不是那个2021年底的验证模型():
    assert QS.PRODUCTION_MODEL_DIR != QS.DEFAULT_MODEL_DIR
    assert "prod" in str(QS.PRODUCTION_MODEL_DIR)


def test_生产目录缺manifest_必须抛错_不许回落旧fit窗(tmp_path, monkeypatch):
    """🔴 生产目录缺 manifest ⇒ 拒绝回落到 2021 年底的旧 fit 窗（静默劣化）。"""
    monkeypatch.setattr(QS, "PRODUCTION_MODEL_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        QS.resolve_fit_segment(tmp_path)


def test_非生产目录_仍允许回落旧口径_旧模型兼容路径():
    """🔴 只有**非生产目录**才允许回落（旧模型压根没有生产 manifest）。"""
    fs, fe, info = QS.resolve_fit_segment(Path("/tmp/does-not-exist-legacy-model"))
    assert info == {}
    assert fs == QS.C.STOCK.FIT_START and fe == QS.C.STOCK.FIT_END


def test_生产模型指纹必须自报model_md5与fit窗():
    """🔴 每日链的「模型一致」守卫靠这个指纹判断底座能不能增量拼接。"""
    fp = QS.production_fingerprint()
    assert fp["model_dir"].endswith(str(QS.PRODUCTION_MODEL_DIR.name))
    assert fp["fit_start"] and fp["fit_end"]
    assert fp["embargo_days"] == QS.PRODUCTION_EMBARGO_DAYS


def test_交叉校验参考件_优先取同源pred_prod(tmp_path):
    (tmp_path / "pred_prod.pkl").write_bytes(b"x")
    (tmp_path / "pred.pkl").write_bytes(b"x")

    class _A:
        validate_pred = ""
        model_dir = str(tmp_path)

    assert QS._resolve_validate_pred(_A()).name == "pred_prod.pkl"


def test_交叉校验参考件_没有prod件时回落同目录pred(tmp_path):
    (tmp_path / "pred.pkl").write_bytes(b"x")

    class _A:
        validate_pred = ""
        model_dir = str(tmp_path)

    assert QS._resolve_validate_pred(_A()).name == "pred.pkl"
