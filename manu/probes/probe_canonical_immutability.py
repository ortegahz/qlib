"""🔴 canonical 只读性闸门：**跑真实流水线** ⇒ canonical 三项指纹必须完全一致。

【为什么要有这个探针】
`manu/memory/rules.md` §15.3.3 写死：
  「🔴 探针**必须能证明**这一点：跑前后对 canonical 取 **size + sha1 + mtime**
    三项指纹 ⇒ **必须完全一致**」
而 `probe_snapshot_contract` 的 S4 只证明了「`Snapshot.write_text` 不碰 canonical」
（合成夹具，**流水线一次都没跑**）⇒ 证明力不够。

【本探针怎么证明】
真的跑一次 `daily/main.py`（**观察态**，且只带四个「不干活」开关：
跳过取数 / 跳过 LLM / 不重算 S 层 / 不换库 ⇒ 即使实现坏了也起不了重活），
跑**前后**对 canonical 那一组文件取 **size + sha1 + mtime** 三项指纹 ⇒ 必须完全一致。

【隔离】
* 快照根 / 报告目录 / 运行根 / 锁文件 **全部重定向到临时目录** ⇒ 不污染任何生产落点；
* canonical **不重定向**（就是要比对生产那几份 ⇒ 证明「真的没被写」）；
* SMTP 凭据指向不存在的路径 ⇒ 绝不可能发信。

【退出码】
0 = 通过（canonical 三项指纹完全一致）；1 = 失败（被改了 / 流水线报错）；2 = 不适用
（环境不具备：无 canonical 输入 ⇒ **不是通过**）。

【用法】
    ~/anaconda3/envs/uav/bin/python manu/etf_rotation/probes/probe_canonical_immutability.py
    [--asof 2026-11-05] [--repo-root /path/to/repo]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE.parent / "daily"))   # `snapshot.py` 在 daily/ 下

import config as C  # noqa: E402  （落点常量单一事实源）
import snapshot as SNAP  # noqa: E402

#: canonical 那一组全局文件（`rules.md` §15.3.3 点名的三样 + 计划件）
CANONICAL_NAMES = (
    "stock_scores.csv",
    "tradingagents_decision.md",
    "tradingagents_decision.json",
    "rebalance_recommendation.json",
    "final_rebalance_plan.json",
)

#: 只带这四个「不干活」开关 ⇒ 子进程**不可能**起重活（取数 / LLM / S 层重算 / 换库）
SAFE_FLAGS = (
    "--skip-fetch",
    "--skip-llm",
    "--no-refresh-scores",
    "--no-update-qlib-bin",
)


def _research_root() -> Path:
    """canonical 所在的产物根（**不重定向** —— 就是要比对生产那几份）。"""
    env_root = os.environ.get("ETF_ROTATION_RESEARCH_RUN_ROOT")
    if env_root:
        return Path(env_root)
    return C.ETF_RUN_ROOT


def _canonical_paths() -> list[Path]:
    root = _research_root()
    return [root / name for name in CANONICAL_NAMES]


def _fingerprint(paths: list[Path]) -> dict:
    return SNAP.root_global_fingerprint(paths)


def _same(before: dict, after: dict) -> list[str]:
    """两项指纹逐文件比对 ⇒ 返回**不一致**的文件名列表。"""
    bad: list[str] = []
    for key, b in before.items():
        a = after.get(key) or {}
        for field in ("exists", "size", "sha1", "mtime"):
            if b.get(field) != a.get(field):
                bad.append(f"{Path(key).name}:{field}({b.get(field)}→{a.get(field)})")
                break
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="canonical 只读性闸门（跑真实流水线 + 三项指纹）")
    ap.add_argument("--asof", default="2026-11-05", help="信号日（默认 2026-11-05）")
    ap.add_argument("--repo-root", default=str(_HERE.parent.parent.parent),
                    help="工程根（含 manu/etf_rotation/daily/main.py 的那个）")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    main_py = repo_root / "manu" / "etf_rotation" / "daily" / "main.py"
    if not main_py.is_file():
        print(f"[不适用] 找不到 {main_py} ⇒ 本阶段不适用（≠ 通过）")
        return 2

    canon = _canonical_paths()
    existing = [p for p in canon if p.is_file()]
    if not existing:
        print(f"[不适用] canonical 输入不存在（{_research_root()}）⇒ 本阶段不适用（≠ 通过）")
        return 2

    before = _fingerprint(canon)
    print(f"canonical 输入 {len(existing)} 份，跑前指纹已取："
          + ", ".join(sorted(p.name for p in existing)))

    with tempfile.TemporaryDirectory(prefix="probe_canon_immutable_") as tmp:
        base = Path(tmp)
        env = dict(os.environ)
        env.update({
            "ETF_ROTATION_RUN_ROOT": str(base / "run"),
            "ETF_ROTATION_LOG_DIR": str(base / "logs"),
            "ETF_ROTATION_SNAPSHOT_ROOT": str(base / "snapshots"),
            "ETF_ROTATION_REPORT_DIR": str(base / "report"),
            "ETF_ROTATION_LOCK_FILE": str(base / "daily.lock"),
            "ETF_ROTATION_SMTP_SECRETS": str(base / "no_smtp.env"),
            "ETF_ROTATION_SMTP_HOST": "127.0.0.1",
            "ETF_ROTATION_SMTP_PORT": "1",
            "ETF_ROTATION_REPO_ROOT": str(repo_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        cmd = [sys.executable, str(main_py), "--asof", args.asof, *SAFE_FLAGS]
        print("跑流水线（观察态 + 四个 skip 开关）：" + " ".join(cmd))
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)

    after = _fingerprint(canon)
    bad = _same(before, after)

    print("=" * 70)
    if bad:
        print(f"[FAIL] canonical 被改了（{len(bad)} 处）⇒ 每日链**不写**全局文件的铁律被破坏：")
        for item in bad:
            print(f"    - {item}")
        return 1
    print(f"[PASS] canonical {len(canon)} 份文件的 size+sha1+mtime **三项完全一致**"
          f"（跑前 = 跑后）⇒ 每日链只把它当**一次性只读种子**")
    if proc.returncode != 0:
        print(f"[WARN] 流水线退出码 = {proc.returncode}（不影响上面这条指纹结论）")
        tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-1500:]
        print("---- 子进程输出尾部 ----")
        print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
