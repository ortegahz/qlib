"""🔴 V2 单盲评估闸门 —— **2024–2026 只跑一次**（fail-closed）。

红线（用户权威规格 ``V2_SPEC_pasted_text.txt`` 第二层）：

    「测试集只跑『单盲验收』：所有参数彻底冻结成配置文件后，在 2024–2026 这段测试集上
      **只运行一次**，把输出的扣费年化、最大回撤和 RankIC 直接封存为 V2 的最终成绩单。」

🔴 **本模块的物理保证（不靠自觉，靠代码）**：
  1. ``run_single_blind()`` 执行前先查 token 文件；**已存在且未显式授权重跑 ⇒ 直接抛错**
     （fail-closed：宁可报错，也不许静默把测试集跑第二遍）；
  2. 显式 ``allow_rerun=True`` 时**必须**写明 ``reason``（空字符串 ⇒ 抛错），
     且理由会**写进 token 文件**（可审计）；
  3. token 文件记录 ``tag`` / ``fingerprint`` / 时间 / 结果摘要 ⇒ 成绩单可追溯到
     「哪一版参数、哪一次运行」。

用法::

    res = run_single_blind(run_root, "test_2024_2026", lambda: do_backtest())
    # 第二次调用 ⇒ 直接抛 SingleBlindAlreadyRun（除非 allow_rerun=True + reason=...）
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "SingleBlindViolation",
    "single_blind_token_path",
    "run_single_blind",
    "single_blind_status",
]


class SingleBlindViolation(RuntimeError):
    """🔴 单盲红线被触碰（重复跑测试段 / 重跑没写理由）。"""


# ----------------------------------------------------------------- 工具
def _now_iso() -> str:
    """当前时间（ISO8601，秒级）——token 里的「运行时间」。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _jsonable(obj: Any, depth: int = 0) -> Any:
    """把结果对象转成**可 JSON 序列化**的结构（Series/DataFrame 只留摘要）。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        if depth >= 2:
            return f"<dict: {len(obj)} keys>"
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if depth >= 2:
            return f"<list: {len(obj)} items>"
        return [_jsonable(v, depth + 1) for v in obj]
    if hasattr(obj, "shape") and hasattr(obj, "index"):     # Series / DataFrame
        try:
            head = obj.head(3)
            return {
                "__type__": type(obj).__name__,
                "n": int(len(obj)),
                "head": _jsonable(head.to_dict() if hasattr(head, "to_dict") else head, depth + 1),
            }
        except Exception:  # noqa: BLE001 —— 摘要失败也不许影响主流程
            return f"<{type(obj).__name__}>"
    if hasattr(obj, "item") and callable(obj.item):          # numpy 标量
        try:
            return obj.item()
        except Exception:  # noqa: BLE001
            pass
    return str(obj)[:500]


def _summarize(result: Any) -> dict:
    """结果摘要：顶层标量字段 + 一层嵌套里的标量字段（够成绩单用，不塞整个对象）。"""
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__, "result": _jsonable(result)}
    out: dict = {}
    for key, value in result.items():
        if isinstance(value, (bool, int, float, str)) or value is None:
            out[str(key)] = value
        elif isinstance(value, dict):
            sub = {k: v for k, v in value.items()
                   if isinstance(v, (bool, int, float, str)) or v is None}
            if sub:
                out[str(key)] = sub
    return out


def _read_token(path: Path) -> dict | None:
    """读 token json（损坏 ⇒ None，但**仍算跑过**）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------- 公开接口
def single_blind_token_path(run_root: Any, tag: str = "test_2024_2026") -> Path:
    """token 文件路径 = ``run_root / f"single_blind_{tag}.json"``。

    `run_root` 不存在时**只返回路径**（不建目录）⇒ 状态查询不会产生副作用。
    """
    tag = str(tag).strip()
    if not tag:
        raise ValueError("🔴 tag 不能为空")
    if "/" in tag or "\\" in tag or tag in (".", ".."):
        raise ValueError(f"🔴 tag 非法（不许含路径分隔符）：{tag!r}")
    return Path(run_root) / f"single_blind_{tag}.json"


def run_single_blind(
    run_root: Path,
    tag: str,
    fn: Callable[[], Any],
    *,
    allow_rerun: bool = False,
    reason: str = "",
    fingerprint: str = "",
) -> dict:
    """跑一次单盲评估并**封存**结果（第二次跑 ⇒ fail-closed 抛错）。

    参数
    ----
    run_root : 产物根目录（token 落这里）。
    tag : 本次单盲的标签（默认用 ``"test_2024_2026"``）。
    fn : 无参可调用对象，返回本次评估结果（通常是 dict）。
    allow_rerun : 是否显式授权重跑（默认 False）。
    reason : **allow_rerun=True 时必填**（空字符串 ⇒ 抛错）；理由写进 token。
    fingerprint : 本次配置的指纹（写进 token ⇒ 成绩单可追溯到「哪一版参数」）。

    返回
    ----
    ``fn()`` 的结果（dict 原样返回）。

    🔴 抛错情形：
      * token 已存在且 ``allow_rerun=False`` ⇒ ``SingleBlindViolation``（**不许静默重跑**）；
      * ``allow_rerun=True`` 但 ``reason`` 为空 ⇒ ``ValueError``（**在跑之前**就拒）。
    """
    path = single_blind_token_path(run_root, tag)
    exists = path.is_file()

    # ---- ② 重跑授权：理由必填（先校验参数，再动数据）----
    if allow_rerun and not str(reason).strip():
        raise ValueError(
            "🔴 allow_rerun=True 必须写明 reason（空字符串被拒）："
            "测试段重跑是红线事件，必须留下可审计的理由"
        )

    # ---- ① fail-closed：token 已存在且未授权 ⇒ 直接抛错 ----
    if exists and not allow_rerun:
        record = _read_token(path) or {}
        raise SingleBlindViolation(
            f"🔴 单盲段 {tag} **已经跑过**（token: {path}，"
            f"首次运行时间 {record.get('created_at', '未知')}，"
            f"fingerprint {record.get('fingerprint', '未知')}）"
            f"⇒ 测试段只许跑一次，不许静默重跑；确需重跑请显式 allow_rerun=True 并写明 reason"
        )

    # ---- ③ 预登记（fail-closed 的关键）：**开跑前**先落一份「running」凭证 ----
    #    旧口径只在 ``fn()`` **成功返回后**才写 token ⇒ 测试段**中途崩溃 ⇒ 零痕迹**
    #    ⇒ 可以再跑一次 ⇒ 红线「2024–2026 只跑一次」在**崩溃路径上失效**。
    #    现在：开跑前先落「running」凭证 ⇒ 崩了也留下「已消耗」痕迹
    #    ⇒ 下一次跑会被 ① 的 fail-closed 拦住（要重跑必须显式 allow_rerun=True + reason）。
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_token(path) if exists else None
    pre_record = {
        "tag": str(tag),
        "fingerprint": str(fingerprint),
        "created_at": (previous or {}).get("created_at") or _now_iso(),
        "last_run_at": _now_iso(),
        "status": "running",
        "allow_rerun": bool(allow_rerun),
        "reason": str(reason),
        "rerun_count": int((previous or {}).get("rerun_count", 0)),
        "result_summary": {},
        "result_json": None,
        "note": (
            "🔴 单盲**预登记**凭证：本次测试段**已开跑**（尚未产出结果）。"
            "本文件存在即视为**已消耗** ⇒ 即使崩溃也不许静默重跑"
            "（确需重跑请显式 allow_rerun=True 并写明 reason）。"
        ),
    }
    if path.exists():
        path.chmod(0o644)      # 上一轮可能是只读 ⇒ 先放开再写
    path.write_text(json.dumps(pre_record, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    path.chmod(0o444)          # 预登记也是只读 ⇒ 防止中途被手改
    previous = pre_record      # 封存记录的 created_at / rerun_count 以预登记为基准

    # ---- ④ 执行（崩溃也留痕）----
    try:
        result = fn()
    except BaseException as exc:
        failed = dict(pre_record)
        failed["status"] = "failed"
        failed["last_error"] = f"{type(exc).__name__}: {exc}"
        failed["last_run_at"] = _now_iso()
        if path.exists():
            path.chmod(0o644)
        path.write_text(json.dumps(failed, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8")
        path.chmod(0o444)
        raise

    # ---- ⑤ 写 token（封存）----
    record = {
        "tag": str(tag),
        "fingerprint": str(fingerprint),
        "created_at": (previous or {}).get("created_at") or _now_iso(),
        "last_run_at": _now_iso(),
        "status": "done",
        "allow_rerun": bool(allow_rerun),
        "reason": str(reason),
        "rerun_count": int((previous or {}).get("rerun_count", 0)) + (1 if allow_rerun else 0),
        "result_summary": _summarize(result),
        "result_json": _jsonable(result),
        "note": (
            "🔴 单盲封存凭证：2024–2026 测试段只许跑一次；"
            "本文件存在即视为已跑过（除非显式 allow_rerun=True 且写明 reason）。"
        ),
    }
    if path.exists():
        path.chmod(0o644)      # 上一轮可能是只读 ⇒ 先放开再写
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    path.chmod(0o444)          # 封存 ⇒ 只读

    return result


def single_blind_status(run_root: Any, tag: str = "test_2024_2026") -> dict:
    """单盲状态查询（**无副作用**：不建目录、不写文件）。

    返回 ``{"exists": bool, "record": dict | None}``；
    token 文件存在但损坏 ⇒ ``exists=True``、``record=None``（仍算「跑过」，不许重跑）。
    """
    path = single_blind_token_path(run_root, tag)
    if not path.is_file():
        return {"exists": False, "record": None}
    return {"exists": True, "record": _read_token(path)}
