#!/usr/bin/env python3
"""manu/tools/fetch_qlib_dataset.py

在【服务器】上直接拉取 Qlib A 股数据集，绕开被掐断的 github.com 重定向链路。

背景 / 为什么不能直接 `curl https://github.com/.../releases/latest/download/xxx`:
  服务器到 github.com 的 302 重定向链会挂死（实测 15s 0 字节, code=000）；
  但 api.github.com 秒回, 且 release-assets.githubusercontent.com 直连 0.12s 可达。
  => 方案: api.github.com 解析资产 -> 拿签名直链 -> 直连 release-assets 下载。

特性:
  * 断点续传（curl -C -），中断后重跑即可接着下
  * 签名 URL 有有效期（约 1h），过期/停滞自动重新解析并重试
  * 严格按 GitHub API 报告的 size 校验完整性，不完整不算成功
  * 落地 manifest.json（源、字节数、md5、耗时），供后续实验引用数据版本
  * 零第三方依赖（纯 stdlib），任意 python3 可跑

用法:
  python manu/tools/fetch_qlib_dataset.py --source chenditc
  python manu/tools/fetch_qlib_dataset.py --source sunsetwolf_cn --source sunsetwolf_simple
  python manu/tools/fetch_qlib_dataset.py --source chenditc --verify-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.github.com"
UA = "manu-qlib-fetch/1.0"


@dataclass
class Source:
    """一个可下载的数据源。"""

    key: str
    repo: str
    tag: str
    asset: str
    dest_dir: str
    note: str = ""
    expected_size: int | None = None
    signed_url: str = ""
    attempts: int = 0
    history: list[str] = field(default_factory=list)


# 服务器上的数据集落盘根目录（根分区只够工程目录, 数据统一落 /mnt/data）
DEFAULT_ROOT = "/mnt/data/siping/datasets/qlib"

SOURCES: dict[str, dict] = {
    # 社区全量: 全 A 股日频 + 财务 + 指数, 含 csi300/csi500/all 股票池
    "chenditc": {
        "repo": "chenditc/investment_data",
        "tag": "latest",
        "asset": "qlib_bin.tar.gz",
        "note": "全 A 股日频+财务 (含 all/csi300/csi500 股票池)",
    },
    # SunsetWolf v3: 官方 scripts/get_data.py 对应的完整 cn 日频
    "sunsetwolf_cn": {
        "repo": "SunsetWolf/qlib_dataset",
        "tag": "v3",
        "asset": "qlib_data_cn_1d_latest.zip",
        "note": "官方 get_data.py 同源完整 cn 日频",
    },
    # SunsetWolf v2: 精简版, 体积小, 适合先冒烟跑通流水线
    "sunsetwolf_simple": {
        "repo": "SunsetWolf/qlib_dataset",
        "tag": "v2",
        "asset": "qlib_data_simple_cn_1d_latest.zip",
        "note": "精简版 cn 日频, 体积小",
    },
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _api_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_asset(repo: str, tag: str, asset: str) -> tuple[str, int]:
    """通过 GitHub API 解析资产 -> (签名直链, 精确字节数)。

    签名直链形如 https://release-assets.githubusercontent.com/github-production-release-asset/...
    它绕开了会挂死的 github.com 302 链, 且直连速度可达数十 MB/s。
    """
    if tag == "latest":
        meta = json.loads(_api_get(f"{API}/repos/{repo}/releases/latest"))
    else:
        meta = json.loads(_api_get(f"{API}/repos/{repo}/releases/tags/{tag}"))

    item = None
    for a in meta.get("assets", []):
        if a.get("name") == asset:
            item = a
            break
    if item is None:
        names = [a.get("name") for a in meta.get("assets", [])]
        raise RuntimeError(f"资产 {asset} 不在 {repo}@{tag}; 可选: {names}")

    size = int(item["size"])
    asset_id = int(item["id"])
    api_url = item["url"]

    # GET asset 且 Accept: octet-stream -> 302 里带真正的 release-assets 直链
    req = urllib.request.Request(api_url, headers={"User-Agent": UA, "Accept": "application/octet-stream"})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            raise RuntimeError(f"预期 302 但拿到 {resp.status}")
    except urllib.error.HTTPError as exc:
        if exc.code != 302:
            raise RuntimeError(f"解析签名直链失败, HTTP {exc.code}") from exc
        signed = exc.headers.get("Location", "")
    if not signed.startswith("https://release-assets.githubusercontent.com/"):
        raise RuntimeError(f"直链域名异常: {signed[:80]}")
    return signed, size


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def download_once(signed_url: str, dest: Path, expected: int, min_speed: int, speed_time: int) -> int:
    """跑一轮续传下载。返回退出码 (0 = 本轮完成)。

    优先用 aria2c: 单连接 curl 实测仅 ~64KB/s, aria2c 16 连接实测 ~1.6MiB/s (约 25 倍)。
    两者都支持 -C 断点续传, 且都直接打签名直链 (不经 github.com 重定向)。
    """
    use_aria = shutil.which("aria2c") is not None
    if use_aria:
        cmd = [
            "aria2c", "-c", "-x", "16", "-s", "16", "-k", "1M",
            "--file-allocation=none",
            "--console-log-level=warn", "--summary-interval=30",
            "--connect-timeout=20", "--timeout=60",
            "--max-tries=3", "--retry-wait=3",
            # 注意: aria2 1.35 没有 --lowest-speed-limit/--lowest-speed-time (那是 curl 的选项),
            # 误加会让 aria2c 直接以 rc=28 退出, 之前 13 轮空转就是这么来的。
            # 停滞检测改由外层轮次比较字节数完成。
            "-d", str(dest.parent), "-o", dest.name, signed_url,
        ]
    else:
        cmd = [
            "curl", "-L", "--fail", "-C", "-",
            "--connect-timeout", "20", "--max-time", "0",
            "--retry", "2", "--retry-delay", "3",
            "--speed-limit", str(min_speed), "--speed-time", str(speed_time),
            "-o", str(dest), signed_url,
        ]
    log(f"    引擎: {'aria2c(16 连接)' if use_aria else 'curl(单连接)'}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and ("%" in line or "ERR" in line or "error" in line.lower() or "Download Results" in line):
            log(f"    {line}")
    proc.wait()
    got = dest.stat().st_size if dest.exists() else 0
    log(f"    本轮结束: {got:,} / {expected:,} 字节 ({100.0 * got / expected:.1f}%)")
    return proc.returncode


def fetch(src: Source, min_speed: int, speed_time: int, max_rounds: int, verify_only: bool) -> bool:
    dest = Path(src.dest_dir) / src.asset
    dest.parent.mkdir(parents=True, exist_ok=True)

    if verify_only:
        if src.expected_size is None:
            _, src.expected_size = resolve_asset(src.repo, src.tag, src.asset)
        size = dest.stat().st_size if dest.exists() else 0
        ok = size == src.expected_size
        log(f"[{src.key}] 校验: 本地 {size:,} / 期望 {src.expected_size:,} -> {'OK' if ok else '不完整'}")
        return ok

    for round_no in range(1, max_rounds + 1):
        src.attempts = round_no
        # 先解析资产拿到精确字节数, 再打印进度 (expected_size 初始为 None)
        try:
            signed, size = resolve_asset(src.repo, src.tag, src.asset)
        except Exception as exc:  # noqa: BLE001
            log(f"[{src.key}] 解析直链失败: {exc}; 5s 后重试")
            time.sleep(5)
            continue
        src.expected_size = size
        src.signed_url = signed

        have = dest.stat().st_size if dest.exists() else 0
        if have == size:
            log(f"[{src.key}] 文件已完整 {have:,} 字节, 跳过下载")
            return True
        log(f"[{src.key}] 第 {round_no}/{max_rounds} 轮 | 已有 {have:,} / 期望 {size:,} "
            f"({100.0 * have / size:.1f}%)")
        log(f"[{src.key}] 直链已解析 (签名直链, 绕过 github.com 重定向)")

        rc = download_once(signed, dest, size, min_speed, speed_time)
        now = dest.stat().st_size if dest.exists() else 0
        if src.expected_size and now == src.expected_size:
            log(f"[{src.key}] ✅ 下载完成 {now:,} 字节")
            # aria2c 的续传控制文件已无意义, 删掉避免下次误判为未完成
            ctrl = dest.with_suffix(dest.suffix + ".aria2")
            if ctrl.exists():
                ctrl.unlink()
            return True
        src.history.append(f"round{round_no}: rc={rc} {now:,}/{size:,}")
        progressed = now > have
        log(f"[{src.key}] 未完成 (rc={rc}), 换新签名直链继续下一轮"
            f"{'' if progressed else ' [本轮 0 字节, 退避等待]'}")
        # 本轮 0 字节 => 链路大概率在恶化, 退避更久, 避免空转刷 API
        time.sleep(3 if progressed else min(3 + round_no * 2, 25))
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="在服务器上直连 release-assets 下载 Qlib 数据集")
    ap.add_argument("--source", action="append", choices=sorted(SOURCES), help="可重复; 默认全部")
    ap.add_argument("--dest-dir", default=DEFAULT_ROOT, help="落盘目录")
    ap.add_argument("--min-speed", type=int, default=50 * 1024, help="最低速度 B/s, 低于则判定停滞并重连")
    ap.add_argument("--speed-time", type=int, default=30, help="停滞判定秒数")
    ap.add_argument("--max-rounds", type=int, default=40, help="单源最大重试轮数")
    ap.add_argument("--verify-only", action="store_true", help="只校验本地文件完整性")
    args = ap.parse_args()

    if shutil.which("curl") is None:
        log("❌ 服务器缺 curl")
        return 2

    keys = args.source or ["chenditc", "sunsetwolf_cn", "sunsetwolf_simple"]
    sources = [
        Source(key=k, repo=SOURCES[k]["repo"], tag=SOURCES[k]["tag"], asset=SOURCES[k]["asset"],
               dest_dir=args.dest_dir, note=SOURCES[k]["note"])
        for k in keys
    ]

    results = []
    ok_all = True
    for src in sources:
        log(f"=== 源 {src.key} | {src.repo}@{src.tag} | {src.asset} | {src.note} ===")
        t0 = time.time()
        ok = fetch(src, args.min_speed, args.speed_time, args.max_rounds, args.verify_only)
        elapsed = time.time() - t0
        dest = Path(src.dest_dir) / src.asset
        entry = {
            "key": src.key,
            "repo": src.repo,
            "tag": src.tag,
            "asset": src.asset,
            "note": src.note,
            "path": str(dest),
            "ok": ok,
            "bytes": dest.stat().st_size if dest.exists() else 0,
            "expected_bytes": src.expected_size,
            "elapsed_sec": round(elapsed, 1),
            "rounds": src.attempts,
            "history": src.history,
        }
        if ok and dest.exists():
            entry["md5"] = md5_of(dest)
            log(f"[{src.key}] md5={entry['md5']}")
        results.append(entry)
        ok_all = ok_all and ok

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename,
        "dest_dir": args.dest_dir,
        "sources": results,
    }
    mpath = Path(args.dest_dir) / "fetch_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"manifest -> {mpath}")
    log("✅ 全部完成" if ok_all else "⚠️ 存在未完成源")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
