"""🔴 V2 超参搜索（`v2/hyperopt.py`）单测 —— **红线：搜索只许在 val 段做**。

本文件钉死（自带夹具，**不依赖网络 / qlib / 大文件**）：

  ① ``assert_val_only("2022-01-01", "2023-12-31")`` **通过**；
  ② 窗口一旦碰到 2024 年（或越过 val 边界、误用训练段）⇒ **必须 raise ValueError**
     （这是**硬红线**，不许只写在文档里）；
  ③ ``search`` 返回 ``best_params`` / ``trials``，且 objective **永远**只收到 val 窗口；
  ④ ``freeze_params`` 把参数冻结成只读 json（含 val_window / 搜索时间 / best_score）；
  ⑤ 变异反证：把边界挪到 2024 之后 ⇒ 2024 窗口**必被放行**（证明边界就是唯一闸门）。

依据：用户权威规格 ``V2_SPEC_pasted_text.txt`` 第二层（时间线红线）
+ ``config_v2.VALID`` / ``config_v2.TEST``。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_V2_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import hyperopt  # noqa: E402

#: 🔴 越过红线的窗口（任一命中都必须被拒）
FORBIDDEN_WINDOWS = [
    ("2024-01-01", "2024-12-31"),      # 完全落在测试段
    ("2022-01-01", "2024-01-01"),      # 终点触到测试段起点
    ("2023-06-01", "2024-06-30"),      # 跨过 val/测试边界
    ("2026-11-03", "2026-12-31"),      # 测试段末端
    ("2016-01-01", "2021-12-31"),      # 误用训练段
    ("2021-12-01", "2022-06-30"),      # 起点早于 val
]


# --------------------------------------------------------------------- ① val 窗口放行
def test_val窗口必须放行() -> None:
    hyperopt.assert_val_only("2022-01-01", "2023-12-31")


def test_val窗口子区间必须放行() -> None:
    hyperopt.assert_val_only("2022-06-01", "2023-06-30")


# --------------------------------------------------------------------- ② 红线：越界必拒
@pytest.mark.parametrize("start,end", FORBIDDEN_WINDOWS)
def test_越界窗口必须raise(start: str, end: str) -> None:
    with pytest.raises(ValueError):
        hyperopt.assert_val_only(start, end)


def test_越界报错必须说明原因() -> None:
    """错误信息必须**说清越界在哪**（红线可审计）。"""
    with pytest.raises(ValueError) as exc:
        hyperopt.assert_val_only("2024-01-01", "2024-12-31")
    msg = str(exc.value)
    assert "2024" in msg and ("val" in msg or "测试段" in msg), (
        f"报错必须点明越界窗口与 val 段；实得 {msg!r}"
    )


def test_窗口倒挂必须raise() -> None:
    with pytest.raises(ValueError):
        hyperopt.assert_val_only("2023-12-31", "2022-01-01")


# --------------------------------------------------------------------- ③ search
def test_search返回best_params与trials() -> None:
    def objective(params, window):
        return {"score": float(params["a"] - params["b"]), "n": 1}

    out = hyperopt.search(objective, {"a": [1, 2, 3], "b": [0, 1]})
    assert out["best_params"] == {"a": 3, "b": 0}, (
        f"best_params 必须是 score 最大的组合；实得 {out['best_params']!r}"
    )
    assert out["best_score"] == pytest.approx(3.0)
    assert len(out["trials"]) == 6, f"trials 必须覆盖整个网格；实得 {len(out['trials'])}"
    assert out["val_window"] == ["2022-01-01", "2023-12-31"]
    assert all(t["status"] == "ok" for t in out["trials"])


def test_search传给objective的窗口永远是val窗口() -> None:
    """🔴 核心红线：objective **只**能拿到 val 窗口（拿不到 test 段）。"""
    seen: list[tuple] = []

    def objective(params, window):
        seen.append(tuple(window))
        return {"score": 1.0}

    hyperopt.search(objective, {"a": [1, 2]})
    assert seen, "objective 一次都没被调用 ⇒ 断言不适用"
    for window in seen:
        assert tuple(window) == ("2022-01-01", "2023-12-31"), (
            f"objective 收到的窗口必须恒为 val 窗口；实得 {window!r}"
        )


def test_search窗口越界时必须在调objective前就raise() -> None:
    """🔴 越界 ⇒ **一个 objective 都不许调**（fail-closed）。"""
    calls: list[int] = []

    def objective(params, window):
        calls.append(1)
        return {"score": 1.0}

    with pytest.raises(ValueError):
        hyperopt.search(objective, {"a": [1]}, val_window=("2024-01-01", "2026-11-03"))
    assert not calls, f"越界时 objective 一次都不许被调用；实得 {len(calls)} 次"


def test_search超max_trials截断并记录() -> None:
    def objective(params, window):
        return {"score": float(params["a"])}

    out = hyperopt.search(objective, {"a": [1, 2, 3, 4, 5]}, max_trials=2)
    assert out["n_trials_total"] == 5
    assert out["n_trials_run"] == 2
    assert out["truncated"] is True
    assert len(out["trials"]) == 2


def test_search全部失败必须raise() -> None:
    def objective(params, window):
        raise RuntimeError("故意失败")

    with pytest.raises(RuntimeError):
        hyperopt.search(objective, {"a": [1]})


# --------------------------------------------------------------------- ④ 冻结
def test_freeze_params写只读json(tmp_path: pathlib.Path) -> None:
    out_path = tmp_path / "frozen_params.json"
    path = hyperopt.freeze_params(
        {"hold_buffer": 2, "top_k": 5, "best_score": 0.42}, out_path
    )
    assert path == out_path and path.is_file()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["params"] == {"hold_buffer": 2, "top_k": 5}
    assert record["best_score"] == pytest.approx(0.42)
    assert record["val_window"] == ["2022-01-01", "2023-12-31"]
    assert record["frozen_at"], "冻结文件必须带搜索时间"
    assert (path.stat().st_mode & 0o222) == 0, "冻结文件必须只读（单盲阶段不许改）"


def test_freeze_params拿search返回值也能冻(tmp_path: pathlib.Path) -> None:
    res = hyperopt.search(lambda p, w: {"score": float(p["a"])}, {"a": [1, 2]})
    path = hyperopt.freeze_params(res, tmp_path / "frozen2.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["params"] == {"a": 2}
    assert record["best_score"] == pytest.approx(2.0)


# --------------------------------------------------------------------- ⑤ 变异反证
def test_变异反证_边界挪到2024之后_2024窗口必被放行(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """变异：把 val 边界挪到 2024 之后（= 有人把红线放宽了）
    ⇒ 2024 窗口**不再被拒** ⇒ 证明「边界常量就是唯一闸门」，本测试有牙。"""
    monkeypatch.setattr(hyperopt, "VAL_WINDOW", ("2024-01-01", "2026-11-03"))
    monkeypatch.setattr(hyperopt, "TEST_WINDOW", ("2027-01-01", "2028-12-31"))
    hyperopt.assert_val_only("2024-01-01", "2024-12-31")   # 变异后**不抛错**
    with pytest.raises(ValueError):
        hyperopt.assert_val_only("2027-01-01", "2027-12-31")  # 新边界之外仍被拒
