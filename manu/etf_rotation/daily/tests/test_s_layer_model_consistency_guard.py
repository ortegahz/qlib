"""🔴 S 层「**模型一致**」守卫（2026-11-19）。

【为什么要有这个文件】
每日链阶段 2 是**增量**打分：拿「上一份快照（或 canonical）的分数」当底座，
只重算底座最大日期之后的那一段。

🔴 2026-11-19 抓到的真隐患：S 层**换了权重**（从 2021 年底验证模型换成
2016~2026 全量重训的生产模型）⇒ 归一化统计量变了 ⇒ **全部分数都变了**
⇒ 若还拿**旧权重的底座**做增量拼接 ⇒ 一份分数里**混着两套模型**
⇒ 症状是「看起来正常的坏结果」（不报错、不崩，只是分数尺度对不上）。

⇒ 本文件钉死三件事：
  ① **指纹对不上 ⇒ 整段重算**（不许增量拼接，也不许把旧底座并进来）；
  ② **指纹对得上 ⇒ 照旧增量**（不许把正常的增量路径也打成全量）；
  ③ 指纹**读不到**（老底座 / 问不到当前模型）⇒ 一律判为「不可增量」（fail-closed）。

【口径】完全离线：全部用 ``tmp_path`` 造夹具，**不跑 qlib、不调 LLM**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_HERE = Path(__file__).resolve().parent
_DAILY = _HERE.parent
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import agents_rebalance as AR  # noqa: E402


_FP = {
    "model_md5": "39fea96414e8adcfb731a992e57127ac",
    "fit_start": "2016-01-01",
    "fit_end": "2026-10-14",
}


def _write_base(scores_path: Path, *, fingerprint: dict | None, dates=("2024-01-09",)) -> None:
    """造一份「底座分数」（可选带指纹旁挂文件）。"""
    scores_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "date": list(dates),
            "instrument": ["600000"] * len(dates),
            "score": [0.1] * len(dates),
        }
    ).to_csv(scores_path, index=False)
    if fingerprint is not None:
        (scores_path.parent / AR.SCORES_FINGERPRINT_NAME).write_text(
            json.dumps(fingerprint, ensure_ascii=False), encoding="utf-8")


def _fake_run_cmd(captured: list[list[str]], *, new_dates=("2026-11-18",)):
    """伪造 ``_run_cmd``：记录命令，并在 ``--out-dir`` 里造出产物。"""

    def _run(cmd, step=None):
        captured.append(list(cmd))
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "date": list(new_dates),
                "instrument": ["600000"] * len(new_dates),
                "score": [9.9] * len(new_dates),
            }
        ).to_csv(out_dir / "stock_scores.csv", index=False)
        (out_dir / AR.SCORES_FINGERPRINT_NAME).write_text(
            json.dumps(_FP, ensure_ascii=False), encoding="utf-8")
        return "ok"

    return _run


# ---------------------------------------------------------------- 指纹判定单元
def test_指纹一致_判为同一套模型():
    assert AR._fingerprint_matches(dict(_FP), dict(_FP)) is True


def test_指纹模型不同_判为不同套():
    other = dict(_FP, model_md5="deadbeef")
    assert AR._fingerprint_matches(other, dict(_FP)) is False


def test_指纹fit窗不同_判为不同套():
    other = dict(_FP, fit_end="2021-12-31")
    assert AR._fingerprint_matches(other, dict(_FP)) is False


def test_任一侧指纹缺失_判为不同套():
    """🔴 老底座没有指纹 ⇒ **不许**当成「同一套」⇒ 必须全量重算。"""
    assert AR._fingerprint_matches(None, dict(_FP)) is False
    assert AR._fingerprint_matches(dict(_FP), None) is False
    assert AR._fingerprint_matches(None, None) is False


# ---------------------------------------------------------------- 阶段 2 接线
def test_指纹对不上_必须全量重算且不许并入旧底座(tmp_path, monkeypatch):
    """🔴 核心守卫：换了权重 ⇒ 旧底座整段作废。"""
    scores_path = tmp_path / "stock_scores.csv"
    _write_base(scores_path, fingerprint=dict(_FP, model_md5="old-model-md5"))
    captured: list[list[str]] = []
    notes: list[str] = []
    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd(captured))
    monkeypatch.setattr(AR, "_current_model_fingerprint", lambda: dict(_FP))

    AR._refresh_scores(scores_path=scores_path, notes=notes, end_date="2026-11-18")

    cmd = captured[0]
    assert "--start" not in cmd, f"指纹对不上还传了 --start（增量拼接 ⇒ 两套模型混拼）：{cmd}"
    assert any("权重已换" in n for n in notes), notes
    merged = pd.read_csv(scores_path)
    # 旧底座那行（score=0.1）不许还在
    assert not (merged["score"].round(6) == 0.1).any(), merged.head()
    assert 9.9 in set(merged["score"])


def test_指纹对得上_照旧增量且旧底座保留(tmp_path, monkeypatch):
    scores_path = tmp_path / "stock_scores.csv"
    _write_base(scores_path, fingerprint=dict(_FP))
    captured: list[list[str]] = []
    notes: list[str] = []
    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd(captured))
    monkeypatch.setattr(AR, "_current_model_fingerprint", lambda: dict(_FP))

    AR._refresh_scores(scores_path=scores_path, notes=notes, end_date="2026-11-18")

    cmd = captured[0]
    assert "--start" in cmd, f"指纹一致却没走增量（白跑全量）：{cmd}"
    merged = pd.read_csv(scores_path)
    assert (merged["score"].round(6) == 0.1).any(), merged.head()
    assert 9.9 in set(merged["score"])


def test_老底座没有指纹_也必须全量重算(tmp_path, monkeypatch):
    """🔴 没有指纹旁挂文件（老底座）⇒ fail-closed ⇒ 全量。"""
    scores_path = tmp_path / "stock_scores.csv"
    _write_base(scores_path, fingerprint=None)
    captured: list[list[str]] = []
    notes: list[str] = []
    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd(captured))
    monkeypatch.setattr(AR, "_current_model_fingerprint", lambda: dict(_FP))

    AR._refresh_scores(scores_path=scores_path, notes=notes, end_date="2026-11-18")

    assert "--start" not in captured[0], captured[0]


def test_重跑完必须把指纹旁挂文件落到快照里(tmp_path, monkeypatch):
    """🔴 指纹不落盘 ⇒ 下一轮读不到 ⇒ 每轮都白跑全量。"""
    scores_path = tmp_path / "stock_scores.csv"
    _write_base(scores_path, fingerprint=dict(_FP))
    captured: list[list[str]] = []
    monkeypatch.setattr(AR, "_run_cmd", _fake_run_cmd(captured))
    monkeypatch.setattr(AR, "_current_model_fingerprint", lambda: dict(_FP))

    AR._refresh_scores(scores_path=scores_path, notes=[], end_date="2026-11-18")

    fp = scores_path.parent / AR.SCORES_FINGERPRINT_NAME
    assert fp.is_file(), "指纹旁挂文件没落盘"
    assert json.loads(fp.read_text(encoding="utf-8"))["model_md5"] == _FP["model_md5"]
