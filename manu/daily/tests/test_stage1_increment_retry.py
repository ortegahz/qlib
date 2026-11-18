"""🔴 2026-11-13：「自建增量日」的**限流退避重试**必须真能重试（独立审查实证的阻塞缺陷）。

事故来源：2026-11-12 夜跑批，`stage1_increment` 撞东方财富限流
（`Push2RateLimitError`）⇒ qlib 基座停在旧末日 ⇒ S 层只能打到旧数据。

🔴 **独立审查实证的阻塞缺陷（C-1）**：旧实现把 `worker.write_text(...)` 放在重试循环
**外**、`worker.unlink(...)` 放在循环**内**的 `finally` ⇒ 第 1 次失败后 worker 已被删
⇒ 第 2 次子进程去打开**不存在的脚本** ⇒ rc=2「can't open file」
⇒ **退避重试变成死路**，而且 `Push2RateLimitError` 被这条假报错**掩盖**
⇒ 明早看到日志会去追「文件找不到 / 权限」这种假方向。

本文件钉死（用**假 akshare 解释器**，不联网、不碰真实行情）：

  ① 首次限流 ⇒ **真的重试** ⇒ 第二次成功 ⇒ 产物落盘、meta 正确；
  ② 重试时 **worker 必须存在**（子进程自己校验 ⇒ 缺文件就报错 ⇒ 断言转红）；
  ③ **非限流**失败 ⇒ **立刻抛**、**只调一次**（不许拿退避掩盖真错）；
  ④ 一直限流 ⇒ 抛出的异常里**必须留有 `Push2RateLimitError` 原文**（不许被假报错覆盖）；
  ⑤ 超时（`TimeoutExpired`）⇒ 归入「疑似限流」⇒ 也要退避重试。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ETF_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))
if str(_ETF_DIR / "daily") not in sys.path:
    sys.path.insert(0, str(_ETF_DIR / "daily"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "_s1ri_under_test", _ETF_DIR / "daily" / "stage1_rolling_increment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S1RI = _load()


#: 假 akshare 解释器：`mode=rate_then_ok` ⇒ 第一次限流、第二次成功；
#: `mode=always_rate` ⇒ 每次都限流；`mode=hard_fail` ⇒ 每次都报**非限流**错。
_FAKE_AK = """#!/usr/bin/env python3
import os, sys, pathlib
mode = os.environ.get("FAKE_AK_MODE", "rate_then_ok")
counter = pathlib.Path(os.environ["FAKE_AK_COUNTER"])
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
worker = pathlib.Path(sys.argv[1])
if not worker.is_file():
    sys.stderr.write("python3: can't open file '%s': [Errno 2] No such file or directory\\n" % worker)
    sys.exit(2)
if mode == "hard_fail":
    sys.stderr.write("ValueError: 不是限流，是真错\\n")
    sys.exit(1)
if mode == "always_rate" or (mode == "rate_then_ok" and n == 0):
    sys.stderr.write("akshare.utils.http.Push2RateLimitError: 东方财富 push2 行情服务器断开了连接\\n")
    sys.exit(1)
out = pathlib.Path(sys.argv[3])
out.write_text("code,close\\n512800,0.885\\n", encoding="utf-8")
sys.stdout.write('{"rows": 1}\\n')
"""


def _fake_ak(tmp_path, mode: str) -> tuple[str, pathlib.Path]:
    counter = tmp_path / "counter.txt"
    counter.write_text("0", encoding="utf-8")
    script = tmp_path / "fake_ak.py"
    script.write_text(_FAKE_AK, encoding="utf-8")
    script.chmod(0o755)
    os_env = {"FAKE_AK_MODE": mode, "FAKE_AK_COUNTER": str(counter)}
    return str(script), counter, os_env


def test_首次限流_必须真的重试并成功(tmp_path, monkeypatch):
    """① + ②：第 1 次限流 ⇒ 第 2 次**真的跑起来**（worker 必须在）⇒ 成功。"""
    script, counter, env = _fake_ak(tmp_path, "rate_then_ok")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    out_csv = tmp_path / "snap.csv"
    meta = S1RI.fetch_market_snapshot(
        "2026-11-12", out_csv, ak_python=script, prev_day="2026-11-11",
        timeout=60, retry_times=3, backoff_seconds=(0,),
    )
    assert int(counter.read_text()) == 2, "必须重试一次（限流 ⇒ 不能一次就放弃）"
    assert meta["day"] == "2026-11-12", meta
    assert out_csv.is_file(), "重试成功后产物必须落盘"


def test_非限流失败_必须立刻抛且只调一次(tmp_path, monkeypatch):
    """③：非限流错误**不许**被退避重试掩盖 ⇒ 只调一次就抛。"""
    script, counter, env = _fake_ak(tmp_path, "hard_fail")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(RuntimeError) as exc:
        S1RI.fetch_market_snapshot(
            "2026-11-12", tmp_path / "snap.csv", ak_python=script,
            prev_day="2026-11-11", timeout=60, retry_times=3, backoff_seconds=(0,),
        )
    assert int(counter.read_text()) == 1, "非限流失败不许重试"
    assert "不是限流" in str(exc.value), str(exc.value)


def test_一直限流_异常必须保留限流原文(tmp_path, monkeypatch):
    """④：重试到头 ⇒ 异常里**必须**留 `Push2RateLimitError` 原文（不许被假报错覆盖）。"""
    script, counter, env = _fake_ak(tmp_path, "always_rate")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(RuntimeError) as exc:
        S1RI.fetch_market_snapshot(
            "2026-11-12", tmp_path / "snap.csv", ak_python=script,
            prev_day="2026-11-11", timeout=60, retry_times=3, backoff_seconds=(0,),
        )
    assert "Push2RateLimitError" in str(exc.value), str(exc.value)
    assert int(counter.read_text()) == 3, "必须按 retry_times 重试到头"


def test_超时_也要退避重试(tmp_path, monkeypatch):
    """⑤：限流下请求挂死到超时 ⇒ 归入「疑似限流」⇒ 也要重试。"""
    script = tmp_path / "slow_ak.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n", encoding="utf-8"
    )
    script.chmod(0o755)
    with pytest.raises(RuntimeError) as exc:
        S1RI.fetch_market_snapshot(
            "2026-11-12", tmp_path / "snap.csv", ak_python=str(script),
            prev_day="2026-11-11", timeout=1, retry_times=2, backoff_seconds=(0,),
        )
    assert "TimeoutExpired" in str(exc.value), str(exc.value)
