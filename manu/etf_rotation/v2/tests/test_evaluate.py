"""🔴 V2 单盲闸门（`v2/evaluate.py`）单测 —— **2024–2026 只跑一次**。

本文件钉死（自带夹具，**不依赖网络 / qlib / 大文件**）：

  ① 第一次 ``run_single_blind`` 成功并**写 token**；
  ② 第二次（``allow_rerun=False``）**必须 raise**（fail-closed，不许静默重跑）；
  ③ ``allow_rerun=True`` 但 ``reason`` 为空 ⇒ **必须 raise**；
  ④ token 记录含 ``fingerprint``（成绩单可追溯到「哪一版参数」）；
  ⑤ 变异反证：把 token 路径指错 ⇒ 第二次**就不会**再抛错（证明闸门有牙）。

依据：用户权威规格 ``V2_SPEC_pasted_text.txt`` 第二层（测试集只跑一次）。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_V2_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import evaluate  # noqa: E402


def _result() -> dict:
    return {"annualized_return": 0.12, "max_drawdown": -0.21, "rank_ic": 0.05}


# --------------------------------------------------------------------- ① 首次跑
def test_首次跑必须成功并写token(tmp_path: pathlib.Path) -> None:
    res = evaluate.run_single_blind(
        tmp_path, "test_2024_2026", _result, fingerprint="fp-abc123"
    )
    assert res == _result(), "必须把 fn() 的结果原样返回"
    path = evaluate.single_blind_token_path(tmp_path, "test_2024_2026")
    assert path.is_file(), "第一次跑完必须落 token 文件"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["tag"] == "test_2024_2026"
    assert record["result_summary"]["annualized_return"] == pytest.approx(0.12)


def test_状态查询必须报告存在(tmp_path: pathlib.Path) -> None:
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)
    status = evaluate.single_blind_status(tmp_path, "test_2024_2026")
    assert status["exists"] is True
    assert isinstance(status["record"], dict)


def test_没跑过的状态是not_exists(tmp_path: pathlib.Path) -> None:
    status = evaluate.single_blind_status(tmp_path, "test_2024_2026")
    assert status == {"exists": False, "record": None}


# --------------------------------------------------------------------- ② 第二次必炸
def test_第二次跑必须raise(tmp_path: pathlib.Path) -> None:
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)
    with pytest.raises(evaluate.SingleBlindViolation):
        evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)


def test_第二次跑时_fn不许被调用(tmp_path: pathlib.Path) -> None:
    """fail-closed：**抛错必须发生在 fn 执行之前**（不许先跑完再报错）。"""
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)
    calls: list[int] = []

    def second():
        calls.append(1)
        return _result()

    with pytest.raises(evaluate.SingleBlindViolation):
        evaluate.run_single_blind(tmp_path, "test_2024_2026", second)
    assert not calls, f"第二次跑的 fn 一次都不许被调用；实得 {len(calls)} 次"


# --------------------------------------------------------------------- ③ 重跑必须写理由
def test_allow_rerun但reason为空必须raise(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        evaluate.run_single_blind(
            tmp_path, "test_2024_2026", _result, allow_rerun=True, reason=""
        )


def test_allow_rerun且reason非空必须放行并记录(tmp_path: pathlib.Path) -> None:
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)
    res = evaluate.run_single_blind(
        tmp_path, "test_2024_2026", _result,
        allow_rerun=True, reason="主机磁盘故障导致首跑产物丢失，经用户口头授权重跑",
    )
    assert res == _result()
    record = json.loads(
        evaluate.single_blind_token_path(tmp_path, "test_2024_2026").read_text(encoding="utf-8")
    )
    assert "磁盘故障" in record["reason"], "重跑理由必须写进 token（可审计）"
    assert record["allow_rerun"] is True


# --------------------------------------------------------------------- ④ fingerprint
def test_token必须含fingerprint(tmp_path: pathlib.Path) -> None:
    evaluate.run_single_blind(
        tmp_path, "test_2024_2026", _result, fingerprint="params-hash-2024"
    )
    record = json.loads(
        evaluate.single_blind_token_path(tmp_path, "test_2024_2026").read_text(encoding="utf-8")
    )
    assert record["fingerprint"] == "params-hash-2024", (
        f"token 必须记录 fingerprint；实得 {record['fingerprint']!r}"
    )
    assert record["created_at"], "token 必须带时间戳"
    assert record["rerun_count"] == 0


# --------------------------------------------------------------------- ⑤ 变异反证
def test_变异反证_token路径指错_第二次就不再raise(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """变异：把 token 路径指到别处 ⇒ 第二次**不再**抛错
    ⇒ 证明「抛错确实由 token 文件驱动」，本测试有牙。"""
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)
    monkeypatch.setattr(
        evaluate, "single_blind_token_path",
        lambda run_root, tag="test_2024_2026": pathlib.Path(run_root) / "other.json",
    )
    evaluate.run_single_blind(tmp_path, "test_2024_2026", _result)   # 变异后**不抛错**
    assert (tmp_path / "other.json").is_file(), (
        "变异后第二次跑确实放行了（token 写到了别处）⇒ 证明闸门由 token 文件驱动"
    )
