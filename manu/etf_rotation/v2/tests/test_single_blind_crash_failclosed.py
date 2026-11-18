"""🔴 V2 **单盲崩溃路径**的 fail-closed 守卫测试 —— 2026-11-17 夜班审计补。

**背景（审计发现 #3：真正的 fail-open）**
  `evaluate.run_single_blind` 旧口径是「`fn()` **成功返回后**才写 token」⇒
  测试段**中途崩溃 ⇒ 产物根里零痕迹** ⇒ 可以**再跑一次** ⇒
  红线「2024–2026 **只跑一次**」在**崩溃路径上失效**。
  `run_v2.stage_blind` 的固定路径登记表同理（也是「评估成功后才写」）。

**本文件钉死**
  ① `fn()` 执行**期间** ⇒ token 凭证**必须已经存在**（``status="running"``）；
  ② `fn()` **崩溃** ⇒ token 凭证**必须仍然存在**且标记 ``status="failed"`` + 记下异常；
  ③ 崩溃后再跑 ⇒ **必须**抛 `SingleBlindViolation`（不许静默重跑）；
  ④ 【变异反证】把「预登记 + 崩溃留痕」删掉（旧口径）⇒ ② / ③ 必须转红。

⛔ **只用自造夹具** ⇒ 不联网、不跑重量级回测、不读真实行情。
⛔ **绝不落盘改真源码**（变异 = 源码 → 内存改 → tmp_path → import 副本）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import evaluate as EV  # noqa: E402

EVAL_SOURCE = _V2_DIR / "evaluate.py"


def _load_module(path: Path, tag: str):
    spec = importlib.util.spec_from_file_location(tag, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[tag] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mutant(tmp_path_factory):
    """🔴 变异体：删掉「预登记」与「崩溃留痕」两处写盘 ⇒ 退回旧口径。"""
    tmp_path = tmp_path_factory.mktemp("mutant")
    text = EVAL_SOURCE.read_text(encoding="utf-8")
    edits = [
        ('    if path.exists():\n'
         '        path.chmod(0o644)      # 上一轮可能是只读 ⇒ 先放开再写\n'
         '    path.write_text(json.dumps(pre_record, ensure_ascii=False, indent=2, sort_keys=True),\n'
         '                    encoding="utf-8")\n'
         '    path.chmod(0o444)          # 预登记也是只读 ⇒ 防止中途被手改\n',
         "    pass  # MUTANT: 删掉预登记（旧口径：只有成功后才写 token）\n"),
        ('        if path.exists():\n'
         '            path.chmod(0o644)\n'
         '        path.write_text(json.dumps(failed, ensure_ascii=False, indent=2, sort_keys=True),\n'
         '                        encoding="utf-8")\n'
         '        path.chmod(0o444)\n',
         "        pass  # MUTANT: 删掉崩溃留痕\n"),
    ]
    for old, new in edits:
        assert old in text, f"🔴 变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / "mutant_evaluate.py"
    target.write_text(text, encoding="utf-8")
    return _load_module(target, "mutant_evaluate")


# ---------------------------------------------------------------- ① 运行期间凭证已存在
def test_执行期间token凭证必须已存在(tmp_path: Path) -> None:
    """🔴 `fn()` 还在跑的时候，token 凭证就**必须**已经落盘（status=running）。"""
    seen: dict = {}

    def _fn():
        path = EV.single_blind_token_path(tmp_path, "test_2024_2026")
        seen["exists"] = path.is_file()
        if path.is_file():
            seen["record"] = json.loads(path.read_text(encoding="utf-8"))
        return {"ok": True, "value": 42}

    EV.run_single_blind(tmp_path, "test_2024_2026", _fn, fingerprint="fp-1")
    assert seen.get("exists") is True, (
        "🔴 `fn()` 执行期间 token 凭证必须已经存在（否则崩溃 ⇒ 零痕迹 ⇒ 可重跑）")
    assert seen["record"].get("status") == "running", (
        f"🔴 运行期凭证必须标 status=running：{seen['record'].get('status')!r}")


# ---------------------------------------------------------------- ② 崩溃 ⇒ 凭证留痕
def test_崩溃后token凭证必须留痕(tmp_path: Path) -> None:
    """🔴 `fn()` 崩溃 ⇒ token 凭证**必须**仍在（status=failed + 记下异常）。"""

    def _boom():
        raise RuntimeError("模拟测试段中途崩溃")

    with pytest.raises(RuntimeError):
        EV.run_single_blind(tmp_path, "test_2024_2026", _boom, fingerprint="fp-2")
    path = EV.single_blind_token_path(tmp_path, "test_2024_2026")
    assert path.is_file(), (
        "🔴 崩溃后 token 凭证必须留痕（零痕迹 ⇒ 红线「只跑一次」在崩溃路径上失效）")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record.get("status") == "failed", (
        f"🔴 崩溃凭证必须标 status=failed：{record.get('status')!r}")
    assert "模拟测试段中途崩溃" in str(record.get("last_error")), (
        f"🔴 崩溃凭证必须记下异常：{record.get('last_error')!r}")


# ---------------------------------------------------------------- ③ 崩溃后再跑 ⇒ 必须拦
def test_崩溃后再跑必须被拦(tmp_path: Path) -> None:
    """🔴 崩溃后再跑 ⇒ **必须**抛 ``SingleBlindViolation``（不许静默重跑）。"""

    def _boom():
        raise RuntimeError("第一次崩溃")

    with pytest.raises(RuntimeError):
        EV.run_single_blind(tmp_path, "test_2024_2026", _boom, fingerprint="fp-3")

    calls = {"n": 0}

    def _second():
        calls["n"] += 1
        return {"ok": True}

    with pytest.raises(EV.SingleBlindViolation):
        EV.run_single_blind(tmp_path, "test_2024_2026", _second, fingerprint="fp-3")
    assert calls["n"] == 0, (
        "🔴 被拦下时**不许**真的再跑一次测试段（否则 token 形同虚设）")


# ---------------------------------------------------------------- ④ 变异反证
def test_变异反证_删掉预登记_崩溃留痕必须转红(tmp_path: Path, mutant) -> None:
    """🔴 删掉「预登记 + 崩溃留痕」⇒ ② / ③ 必须转红（证明这道闸门有牙）。"""

    def _boom():
        raise RuntimeError("模拟崩溃")

    with pytest.raises(RuntimeError):
        mutant.run_single_blind(tmp_path, "test_2024_2026", _boom, fingerprint="fp-4")
    path = mutant.single_blind_token_path(tmp_path, "test_2024_2026")
    assert not path.is_file(), (
        "🔴 变异体应当**不留任何凭证**（否则这条变异反证没意义）")
    # 变异体 ⇒ 崩溃后可以再跑（= 旧口径的漏洞）
    calls = {"n": 0}

    def _second():
        calls["n"] += 1
        return {"ok": True}

    mutant.run_single_blind(tmp_path, "test_2024_2026", _second, fingerprint="fp-4")
    assert calls["n"] == 1, (
        "🔴 变异体应当可以再跑一次（那正是这条闸门要堵的漏洞）")
