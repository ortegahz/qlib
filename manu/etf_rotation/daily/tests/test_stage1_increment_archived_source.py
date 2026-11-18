"""🔴 回归：同一天**第二次**跑全量时，「自建增量日」不得抛 UnboundLocalError。

**真 bug（2026-11-17 夜跑实测）**
    `daily/stage1_rolling_increment.py::fetch_missing_bars` 的「当天」分支里，
    `source` **只在 `path is None`（当天还没归档）时**被赋值；
    而 `stats[day] = {"source": source, ...}` 是**无条件**执行的
    ⇒ 当天 bar **已经归档**（= 同一天第二次跑）时 ⇒ `UnboundLocalError:
       cannot access local variable 'source'`。

**后果链**
    自建增量日整体失败 ⇒ 派生目录算成 `None` ⇒ 校验回落到**基座**末日
    （把「派生库已覆盖信号日」误报成「数据滞后 / 打分日只能落在覆盖末日」）。

**本文件钉死**
    1. 复用已归档快照 ⇒ 不抛异常，且 `stats[day]["source"]` 有值；
    2. **变异反证**：把修复行从源码里去掉 ⇒ 必须复现 `UnboundLocalError`。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

_DAILY = Path(__file__).resolve().parents[1]
if str(_DAILY) not in sys.path:
    sys.path.insert(0, str(_DAILY))

import stage1_rolling_increment as SIR  # noqa: E402

DAY = "2026-11-16"          # 固定的历史日 ⇒ 不受「当天是否已收盘」影响
_FIX_LINE = '        source = "archived(已归档)"\n'


def _make_archive(monkeypatch, tmp_path: Path) -> Path:
    archive = tmp_path / "snapshot_archive"
    archive.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(SIR, "SNAPSHOT_ARCHIVE", archive)
    path = archive / f"snapshot_{DAY.replace('-', '')}.csv"
    pd.DataFrame(
        {
            "code": ["000001", "600000"],
            "date": [DAY, DAY],
            "open": [1.0, 2.0],
            "high": [1.1, 2.1],
            "low": [0.9, 1.9],
            "close": [1.05, 2.05],
            "volume": [1000.0, 2000.0],
            "amount": [1.0e6, 2.0e6],
        }
    ).to_csv(path, index=False, encoding="utf-8")
    return path


def test_reuse_archived_snapshot_does_not_raise(monkeypatch, tmp_path):
    """复用已归档快照 ⇒ 取数成功、source 有值（不许 UnboundLocalError）。"""
    _make_archive(monkeypatch, tmp_path)
    out = SIR.fetch_missing_bars(
        [DAY], base_dir=tmp_path / "base", out_dir=tmp_path / "work", today=DAY,
    )
    stats = out["stats"][DAY]
    assert int(stats["rows"]) == 2, f"复用已归档快照必须拿到 2 行，实际 {stats}"
    assert stats["source"], "source 不许为空（原 bug：未赋值 ⇒ UnboundLocalError）"


def test_mutation_removing_fix_reproduces_unboundlocalerror(tmp_path):
    """**变异反证**：去掉修复行 ⇒ 必须复现 UnboundLocalError（证明本测试有效）。"""
    src = Path(SIR.__file__)
    text = src.read_text(encoding="utf-8")
    assert _FIX_LINE in text, (
        "找不到修复行 `source = \"archived(已归档)\"` ⇒ 源码结构变了，"
        "请同步更新本测试的变异点"
    )
    mutated = text.replace(_FIX_LINE, "")
    assert mutated != text

    module = types.ModuleType("sir_mutant")
    module.__file__ = str(src)
    exec(compile(mutated, str(src), "exec"), module.__dict__)  # noqa: S102

    archive = tmp_path / "snapshot_archive"
    archive.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"code": ["000001"], "date": [DAY], "close": [1.05],
                  "volume": [1000.0], "amount": [1.0e6]}).to_csv(
        archive / f"snapshot_{DAY.replace('-', '')}.csv", index=False, encoding="utf-8")
    module.SNAPSHOT_ARCHIVE = archive

    with pytest.raises(UnboundLocalError):
        module.fetch_missing_bars(
            [DAY], base_dir=tmp_path / "base", out_dir=tmp_path / "work2", today=DAY,
        )
