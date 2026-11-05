"""stage1_dataset.py —— 阶段 1（盘后数据定型）的**最后一环**：
把全市场个股日线装进本地 Qlib 二进制数据集。

【本模块解决什么】
月度调仓日（2026-12-01）的六阶段全流程里，**阶段 2 的 S 层打分**必须能算到**信号日当天**。
但现役 `cn_data` 的日历只到 2026-11-03 ⇒ 12-01 调仓时拿到的个股 Alpha 是近一个月前的旧数据
（这正是用户点名的「唯一硬伤」）。
本模块负责把数据集推到 12-01，让 `qlib_scores.py --mode infer --end-time 2026-12-01` 有数据可算。

【为什么是「整库换滚动快照」而不是「逐日增量写 bin」】
`manu/etf_rotation/daily/fetch_data.py::update_qlib_daily_bar` 那条路（spot 接口 → 增量写 bin）
必须自己处理**复权因子**：除权除息当天的事前校验做不出来，factor 错了会把整段历史改坏。
而 `chenditc/investment_data` 的 `latest` release 是一份**滚动快照**
（资产 `qlib_bin.tar.gz`，含 `calendars/ features/ instruments/`），
它自带官方口径的复权因子 ⇒ **整库换快照 = 因子一次到位 + 不污染历史**。
现役 `cn_data` 本来就是从这个 tarball 解出来的 ⇒ 结构一致、可直接对换。

【🔴 相位铁律：为什么换库前必须逐日比对日历前缀】
打分栅格是 `qlib 日历下标 % REBALANCE_EVERY_N_DAYS`
（见 `manu/etf_rotation/data/signal_policy.py`）。
日历一旦**中间插日 / 换日 / 少日**，历史下标全部右移 ⇒ **已归档的历史分数与新栅格错位**。
只有「新快照的日历 = 现役日历的严格前缀扩展」时，追加的交易日才只影响新日期、不动历史下标。
⇒ `prefix_identical=False` 一律 **fail-closed 拒绝换库**（不许「差不多」放行）。

【铁律（写死在本模块里）】
1. **默认 dry-run**：换库这类写盘动作默认**只报告不落盘**，真写必须显式 `dry_run=False`
   （`swap_provider` 与 `run_stage1` 的 `dry_run` 默认值**都是** `True`）。
2. **fail-closed**：日历前缀不一致 ⇒ 抛异常拒绝换库（理由写进异常消息）。
3. **大文件不落 /tmp**：`work_root` / `backup_dir` / `dest_dir`
   默认全在 `/mnt/data/siping/datasets/qlib/`
   （`/tmp` 会被 `systemd-tmpfiles` 清理，工程根也在根盘）。
4. **不许静默**：任何一步失败都抛异常，异常消息必须带**原始报错**；
   校验用的 qlib 若不可用 ⇒ 抛异常，**不许**静默跳过。
5. **不许碰服务器**：本模块只做文件级操作；
   下载走 `manu/tools/fetch_qlib_dataset.py`（已实现的那份，不重写）。
6. **解压绝不覆盖现役库**：快照一律先解到 `work_root` 下的临时目录，验过才换。

【公开 API】
| 函数 | 作用 |
|:--|:--|
| `inspect_provider` | 只读体检（日历首末日 / 交易日数 / 标的数 / features 目录数） |
| `compare_calendars` | 现役库 vs 新快照的日历比对（**相位闸门**） |
| `fetch_snapshot` | 复用 `manu/tools/fetch_qlib_dataset.py` 下载滚动快照 |
| `extract_snapshot` | 安全解压到临时目录（拒绝路径穿越 / 链接成员） |
| `swap_provider` | 安全换库（先比对 ⇒ 备份 ⇒ 换 ⇒ 失败回滚） |
| `verify_provider` | 用 qlib 读回校验（close/volume/factor 非 NaN + 日历含 probe_date） |
| `run_stage1` | 阶段 1 编排：体检 → 下载 → 解压 → 比对 → 换库 → 校验 |

【调用示例（服务器）】
```bash
# dry-run（默认）：体检 + 比对，不动磁盘
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/stage1_dataset.py \
    --asof 2026-12-01 --dry-run

# 真换库（需显式 --apply）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/stage1_dataset.py \
    --asof 2026-12-01 --apply
```
"""
from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: `manu/tools/`（**不是包** ⇒ 只能按绝对路径 importlib 加载）
_DOWNLOADER_PATH = _HERE.parents[1] / "tools" / "fetch_qlib_dataset.py"

#: qlib provider 的三个必备子目录
_REQUIRED_SUBDIRS = ("calendars", "features", "instruments")

#: 默认落盘位置（🔴 铁律三：大文件一律落 `/mnt/data`，**不落 /tmp**）
DEFAULT_PROVIDER_DIR = DC.QLIB_CN_DATA
DEFAULT_DEST_DIR = Path("/mnt/data/siping/datasets/qlib")
DEFAULT_WORK_ROOT = Path("/mnt/data/siping/datasets/qlib/_stage1_work")
DEFAULT_BACKUP_DIR = Path("/mnt/data/siping/datasets/qlib/_stage1_backup")

#: 下载参数（对齐 `manu/tools/fetch_qlib_dataset.py` 的 CLI 默认值）
_MIN_SPEED = 50 * 1024
_SPEED_TIME = 30
_MAX_ROUNDS = 40

#: 读回校验用的字段（qlib 表达式 ⇒ 对应 `features/<code>/<field>.day.bin`）
_VERIFY_FIELDS = ("$close", "$volume", "$factor")


# ---------------------------------------------------------------- 下载器加载
def _load_downloader():
    """按绝对路径加载 `manu/tools/fetch_qlib_dataset.py`（`manu/tools/` 不是包）。

    🔴 **复用而不是重写**：那份下载器已经处理了 GitHub API 解析签名直链、aria2c 16 连接、
    断点续传、字节数校验、manifest 落盘。重写一遍只会引入第二套会漂移的下载逻辑。

    🔴 **必须先把模块登记进 `sys.modules` 再执行**（等价于 `importlib.import_module`）：
    那份下载器是 `@dataclass` + `from __future__ import annotations`，
    而 `dataclasses` 装饰器解析字段注解时要反查 `sys.modules[cls.__module__].__dict__`；
    没登记 ⇒ `sys.modules.get(...)` 返回 `None` ⇒ 导入期就炸
    `AttributeError: 'NoneType' object has no attribute '__dict__'`（2026-11-05 服务器实测）。
    """
    if not _DOWNLOADER_PATH.exists():
        raise FileNotFoundError(f"下载器不存在: {_DOWNLOADER_PATH}")
    spec = importlib.util.spec_from_file_location("manu_fetch_qlib_dataset", _DOWNLOADER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载下载器: {_DOWNLOADER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 导入失败不留半截模块 ⇒ 下次调用能干净地重新加载
        sys.modules.pop(spec.name, None)
        raise
    return module


# ---------------------------------------------------------------- 出错可诊断
def _with_traceback(func):
    """装饰器：把 traceback 追加进异常消息（**异常类型不变**）⇒ 日志不再吞栈。

    🔴 为什么需要它：`main.py` 只打印 `f"{type(exc).__name__}: {exc}"`，
    没有 traceback ⇒ 线上报错等于黑箱（2026-11-05 的
    `AttributeError: 'NoneType' object has no attribute '__dict__'` 就是这么查不出来的）。
    🔴 只**追加**信息、不改语义：异常类型不变、原始报错仍在消息里 ⇒ 依旧 fail-closed。
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            tb = traceback.format_exc()
            message = f"{exc}\n{tb}" if tb else str(exc)
            exc.args = (message, *exc.args[1:])
            raise

    return wrapper


# ---------------------------------------------------------------- 读日历
def _read_calendar(provider_dir: str | Path) -> list[str]:
    """读 qlib 日历 `calendars/day.txt`（一行一个 `YYYY-MM-DD`，空行忽略）。

    与上游 `qlib/data/storage/file_storage.py::FileCalendarStorage._read_calendar` 口径一致。
    """
    cal_path = Path(provider_dir) / "calendars" / "day.txt"
    if not cal_path.exists():
        raise FileNotFoundError(f"缺日历文件: {cal_path}")
    lines = cal_path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _provider_root_of(staging: Path) -> Path | None:
    """在解压目录里找 provider 根（含 `calendars/ features/ instruments/` 的那一层）。"""
    if all((staging / sub).is_dir() for sub in _REQUIRED_SUBDIRS):
        return staging
    for child in sorted(staging.iterdir()):
        if child.is_dir() and all((child / sub).is_dir() for sub in _REQUIRED_SUBDIRS):
            return child
    return None


def _move_path(src: Path, dst: Path) -> None:
    """移动目录（同盘 `rename`，跨盘退回 `shutil.move`）。

    🔴 目标必须**不存在**：否则 `shutil.move` 会把源目录**塞进**目标目录里（变成 `dst/src`），
    换库就会变成「活库下面多了一层」⇒ 静默损坏。
    """
    if dst.exists():
        raise FileExistsError(f"目标已存在，拒绝移动（会造成嵌套）: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))


# ---------------------------------------------------------------- 体检
def inspect_provider(provider_dir: str | Path) -> dict:
    """只读体检一个 qlib provider 目录。

    返回 `{"calendar_first","calendar_max","n_days","n_instruments","n_features","ok","notes"}`。
    `notes` 是**字符串列表**（不是单串）⇒ 调用方可以逐条留痕。
    """
    root = Path(provider_dir)
    out: dict = {
        "calendar_first": None,
        "calendar_max": None,
        "n_days": 0,
        "n_instruments": 0,
        "n_features": 0,
        "ok": False,
        "notes": [],
    }
    if not root.is_dir():
        out["notes"].append(f"provider 目录不存在: {root}")
        return out
    if not (root / "calendars" / "day.txt").exists():
        out["notes"].append(f"缺 calendars/day.txt: {root}")
        return out

    days = _read_calendar(root)
    if days:
        out["calendar_first"] = days[0]
        out["calendar_max"] = days[-1]
    out["n_days"] = len(days)

    feats = root / "features"
    if feats.is_dir():
        out["n_features"] = sum(1 for item in feats.iterdir() if item.is_dir())
    else:
        out["notes"].append(f"缺 features/ 目录: {feats}")

    inst_all = root / "instruments" / "all.txt"
    if inst_all.exists():
        out["n_instruments"] = sum(
            1 for line in inst_all.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    else:
        out["notes"].append(f"缺 instruments/all.txt: {inst_all}")

    out["ok"] = bool(days) and out["n_features"] > 0 and inst_all.exists()
    out["notes"].append(
        f"日历 {out['calendar_first']}~{out['calendar_max']}（{out['n_days']} 个交易日）"
        f" / features 标的目录 {out['n_features']} / all.txt 标的 {out['n_instruments']}"
    )
    return out


# ---------------------------------------------------------------- 相位闸门
def compare_calendars(current_dir: str | Path, new_dir: str | Path) -> dict:
    """比较现役库与新快照的日历 —— **打分栅格相位的唯一闸门**。

    返回 `{"current_max","new_max","new_days","prefix_identical","phase_shift",
    "calendar_sane","safe_to_swap","reason"}`（外加几个诊断键）。

    🔴 `prefix_identical=False` ⇒ 相位会漂移 ⇒ `safe_to_swap=False`，
    调用方（`swap_provider`）据此**拒绝换库**，不许「差不多」放行。
    🔴 `calendar_sane=False` ⇒ 日历有重复日 / 乱序 ⇒ 下标↔日期会错位 ⇒ 同样拒绝。
    """
    cur = _read_calendar(current_dir)
    new = _read_calendar(new_dir)
    if not cur:
        raise RuntimeError(f"现役库日历为空: {Path(current_dir) / 'calendars' / 'day.txt'}")
    if not new:
        raise RuntimeError(f"新快照日历为空: {Path(new_dir) / 'calendars' / 'day.txt'}")

    n_cur, n_new = len(cur), len(new)
    first_diff = None
    for idx in range(min(n_cur, n_new)):
        if cur[idx] != new[idx]:
            first_diff = idx
            break

    # 严格前缀：现役日历必须逐日等于新日历的前 n_cur 天（且新日历不能更短）
    prefix_identical = n_new >= n_cur and cur == new[:n_cur]

    # 日历自洽性：qlib 的 bin 下标 ↔ 日期是**位置对应**的
    # ⇒ 日历里有重复日（同一日两个下标）或乱序（前后颠倒）都会让特征错位。
    dup_cur = len(set(cur)) != n_cur
    dup_new = len(set(new)) != n_new
    unsorted_cur = cur != sorted(cur)
    unsorted_new = new != sorted(new)
    calendar_sane = not (dup_cur or dup_new or unsorted_cur or unsorted_new)

    if prefix_identical:
        new_days = new[n_cur:]
    else:
        cur_set = set(cur)
        new_days = [day for day in new if day not in cur_set]

    current_max, new_max = cur[-1], new[-1]
    phase_shift = not prefix_identical

    if not calendar_sane:
        bad = []
        if dup_cur:
            bad.append("现役日历有重复日")
        if dup_new:
            bad.append("新快照日历有重复日")
        if unsorted_cur:
            bad.append("现役日历未按日期升序")
        if unsorted_new:
            bad.append("新快照日历未按日期升序")
        reason = f"日历不自洽（{'、'.join(bad)}）⇒ 下标↔日期对应关系会错位 ⇒ 拒绝换库"
        safe = False
    elif not prefix_identical:
        detail = (
            f"首个不一致位置 index={first_diff}"
            f"（current={cur[first_diff] if first_diff is not None else None}"
            f" vs new={new[first_diff] if first_diff is not None else None}）"
        )
        if n_new < n_cur:
            detail += f"；且新快照更短（{n_new} < {n_cur}）"
        reason = (
            f"日历前缀不一致（{detail}）⇒ 交易日历下标会漂移 ⇒ 打分栅格相位会漂移 ⇒ 拒绝换库"
        )
        safe = False
    elif new_max <= current_max:
        reason = (
            f"日历前缀一致（{n_cur} 个交易日）但新快照无新增交易日"
            f"（new_max={new_max} <= current_max={current_max}）⇒ 换库收益为零"
        )
        safe = False
    else:
        reason = (
            f"日历前缀一致（{n_cur} 个交易日），新快照新增 {len(new_days)} 个交易日"
            f"（{current_max} → {new_max}）⇒ 历史下标不动、相位不漂移"
        )
        safe = True

    return {
        "current_max": current_max,
        "new_max": new_max,
        "new_days": new_days,
        "prefix_identical": prefix_identical,
        "phase_shift": phase_shift,
        "calendar_sane": calendar_sane,
        "safe_to_swap": safe,
        "reason": reason,
        # —— 诊断键（不影响判定）——
        "current_len": n_cur,
        "new_len": n_new,
        "first_diff_index": first_diff,
        "first_diff_current": cur[first_diff] if first_diff is not None else None,
        "first_diff_new": new[first_diff] if first_diff is not None else None,
    }


# ---------------------------------------------------------------- 下载
def fetch_snapshot(
    dest_dir: str | Path,
    *,
    source: str = "chenditc",
    verify_only: bool = False,
) -> dict:
    """复用 `manu/tools/fetch_qlib_dataset.py` 下载滚动快照（**不自写下载器**）。

    返回 `{"path","bytes","ok","md5","elapsed_sec"}`。
    🔴 下载失败**抛异常**（消息含源 / 已下载字节数 / 重试历史）——不许返回 `ok=False` 让人忽略。
    """
    dl = _load_downloader()
    sources = getattr(dl, "SOURCES", {})
    if source not in sources:
        raise KeyError(f"未知数据源 {source!r}；可选: {sorted(sources)}")

    meta = sources[source]
    src = dl.Source(
        key=source,
        repo=meta["repo"],
        tag=meta["tag"],
        asset=meta["asset"],
        dest_dir=str(dest_dir),
        note=meta.get("note", ""),
    )
    started = time.time()
    try:
        ok = bool(dl.fetch(src, _MIN_SPEED, _SPEED_TIME, _MAX_ROUNDS, verify_only))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"下载 {source} 失败（原始报错）: {exc}") from exc
    elapsed = time.time() - started

    dest = Path(dest_dir) / meta["asset"]
    size = dest.stat().st_size if dest.exists() else 0
    if not ok:
        history = getattr(src, "history", [])
        raise RuntimeError(
            f"下载 {source} 失败: {dest} 只有 {size} 字节；重试历史={history}"
        )
    md5 = dl.md5_of(dest) if dest.exists() else None
    return {
        "path": str(dest),
        "bytes": size,
        "ok": True,
        "md5": md5,
        "elapsed_sec": round(elapsed, 1),
    }


# ---------------------------------------------------------------- 解压
def _safe_members(tf: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """过滤危险成员（路径穿越 / 绝对路径 / 链接）。"""
    members: list[tarfile.TarInfo] = []
    for member in tf.getmembers():
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts:
            raise RuntimeError(f"压缩包含非法路径（疑似穿越）: {name}")
        if member.issym() or member.islnk():
            raise RuntimeError(f"压缩包含链接成员（拒绝）: {name}")
        members.append(member)
    return members


def extract_snapshot(tar_path: str | Path, work_dir: str | Path) -> Path:
    """把 `qlib_bin.tar.gz` 解压到 `work_dir` 下的**临时目录**，返回 provider 根目录。

    🔴 **绝不许直接覆盖现役库**：解压目标固定是 `work_dir/extract_<UTC时间戳>/`，
    换库由 `swap_provider` 显式执行（默认 dry-run）。
    解压后必须能找到含 `calendars/ features/ instruments/` 的那一层，缺一个就抛异常。
    """
    tar_path = Path(tar_path)
    if not tar_path.exists():
        raise FileNotFoundError(f"压缩包不存在: {tar_path}")

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    # 目录名带纳秒后缀 ⇒ 同秒并发跑两次也不会撞名
    staging = work / (
        "extract_"
        + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + f"_{time.time_ns() % 1_000_000_000:09d}"
    )
    staging.mkdir(parents=True, exist_ok=False)

    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            members = _safe_members(tf)
            try:
                # Python 3.12+ / 3.10.12+：官方过滤层（比裸 extractall 更严）
                tf.extractall(staging, members=members, filter="data")
            except TypeError:
                # 老解释器没有 filter 参数（成员安全已由 _safe_members 保证）
                tf.extractall(staging, members=members)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"解压 {tar_path} 失败（原始报错）: {exc}") from exc

    root = _provider_root_of(staging)
    if root is None:
        listing = sorted(item.name for item in staging.iterdir())
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(
            f"解压后找不到 qlib provider 根目录（需含 {'/'.join(_REQUIRED_SUBDIRS)}）；"
            f"顶层内容={listing}"
        )
    if not (root / "calendars" / "day.txt").exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"provider 根目录缺 calendars/day.txt: {root}")
    return root


# ---------------------------------------------------------------- 换库
def swap_provider(
    current_dir: str | Path,
    new_dir: str | Path,
    *,
    backup_dir: str | Path,
    dry_run: bool = True,
) -> dict:
    """安全换库：比对 ⇒ 备份 ⇒ 换 ⇒ 失败回滚。

    返回 `{"swapped","dry_run","backup","notes"}`。
    🔴 默认 `dry_run=True` ⇒ **磁盘零改动**，只回报「打算怎么换」。
    """
    cur = Path(current_dir)
    new = Path(new_dir)
    bak = Path(backup_dir)
    if not new.is_dir():
        raise FileNotFoundError(f"新快照目录不存在: {new}")

    comparison = compare_calendars(cur, new)
    if not comparison["prefix_identical"]:
        raise RuntimeError(
            "拒绝换库（fail-closed）：日历前缀不一致 ⇒ 打分栅格相位会漂移。"
            f"current_max={comparison['current_max']}, new_max={comparison['new_max']}, "
            f"first_diff_index={comparison['first_diff_index']}"
            f"（current={comparison['first_diff_current']} vs new={comparison['first_diff_new']}）"
        )
    if not comparison.get("calendar_sane", True):
        raise RuntimeError(
            f"拒绝换库（fail-closed）：{comparison['reason']}"
        )

    # 🔴 **新快照无新增交易日 ⇒ 不换库**（2026-11-05 实测补的口径）：
    #    上游 `chenditc/latest` 的日历推进有滞后（实测它仍停在 2026-11-03），
    #    此时换库的**收益为零**（覆盖不到信号日），却要动**现役生产数据集**
    #    （旧库进备份、新快照上位）⇒ 无收益的风险动作 ⇒ 不做。
    #    ⚠️ 这**不是** fail-closed：日历不一致仍然硬拦（上面两处 raise）；
    #    这里只是「没必要就不换」，并把结论写进 notes 披露。
    if comparison.get("current_max") and comparison.get("new_max") and (
        comparison["new_max"] <= comparison["current_max"]
    ):
        return {
            "swapped": False,
            "dry_run": dry_run,
            "backup": None,
            "skipped": True,
            "reason": comparison["reason"],
            "notes": (
                f"跳过换库：新快照无新增交易日"
                f"（new_max={comparison['new_max']} <= current_max={comparison['current_max']}）"
                f"⇒ 换库收益为零 ⇒ **不触碰现役数据集**。{comparison['reason']}"
            ),
        }

    if dry_run:
        return {
            "swapped": False,
            "dry_run": True,
            "backup": None,
            "notes": (
                f"dry-run：磁盘零改动。计划 {cur} → {bak / 'cn_data_<UTC时间戳>'}，"
                f"{new} → {cur}。{comparison['reason']}"
            ),
        }

    bak.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = bak / f"cn_data_{stamp}"
    # 同秒内跑两次 ⇒ 备份名加序号，避免撞名（撞名会被 _move_path 拦成 FileExistsError）
    seq = 1
    while backup_path.exists():
        backup_path = bak / f"cn_data_{stamp}_{seq}"
        seq += 1
    moved_old = False
    try:
        if cur.exists():
            _move_path(cur, backup_path)
            moved_old = True
        _move_path(new, cur)
    except Exception as exc:  # noqa: BLE001
        rollback = "未发生回滚（旧库尚未移走）"
        if moved_old and backup_path.exists():
            try:
                # 🔴 跨盘 move 可能「拷一半」留下残缺的 cur ⇒ 先清干净再把旧库搬回来
                if cur.exists():
                    shutil.rmtree(cur, ignore_errors=True)
                _move_path(backup_path, cur)
                rollback = "旧库已回滚到原位"
            except Exception as rollback_exc:  # noqa: BLE001
                rollback = f"回滚失败（需人工介入）: {rollback_exc}"
        raise RuntimeError(f"换库失败（原始报错）: {exc}；{rollback}") from exc

    return {
        "swapped": True,
        "dry_run": False,
        "backup": str(backup_path),
        "notes": (
            f"已换库：旧库备份到 {backup_path}，新库已就位于 {cur}。{comparison['reason']}"
        ),
    }


# ---------------------------------------------------------------- 读回校验
def verify_provider(
    provider_dir: str | Path,
    probe_date: str,
    *,
    probe_instruments: tuple[str, ...] = ("sh600000", "sz000001"),
) -> dict:
    """用 qlib 读回校验数据集（**局部 import qlib**）。

    校验两件事：
    ① `calendars/day.txt` 含 `probe_date`；
    ② `D.features(..., start_time=probe_date, end_time=probe_date)` 取到的
       `$close/$volume/$factor` **不是 NaN**（NaN 说明当天没数据 / bin 没写全）。

    返回 `{"ok","checked","notes"}`。
    🔴 qlib 不可用 ⇒ **抛异常**，不许静默跳过（那等于没校验）。
    """
    root = Path(provider_dir)
    try:
        import qlib  # noqa: PLC0415
        from qlib.data import D  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"qlib 不可用，无法读回校验数据集（原始报错）: {exc}") from exc

    days = _read_calendar(root)
    if probe_date not in days:
        raise RuntimeError(
            f"calendars/day.txt 不含 probe_date={probe_date}"
            f"（末日={days[-1] if days else None}，共 {len(days)} 个交易日）"
        )

    try:
        qlib.init(provider_uri=str(root.resolve()))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"qlib.init(provider_uri={root}) 失败（原始报错）: {exc}") from exc

    fields = list(_VERIFY_FIELDS)
    try:
        frame = D.features(
            list(probe_instruments),
            fields,
            start_time=probe_date,
            end_time=probe_date,
            freq="day",
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"qlib 读回 {probe_date} 的 {fields} 失败（原始报错）: {exc}"
        ) from exc

    checked: list[dict] = []
    for inst in probe_instruments:
        row = None
        for key, values in frame.iterrows():
            inst_id = key[0] if isinstance(key, tuple) else key
            if str(inst_id).lower() == inst.lower():
                row = values
                break
        if row is None:
            raise RuntimeError(
                f"qlib 读回 {probe_date} 时没有返回标的 {inst}（fields={fields}）"
            )
        vals = {str(k): (None if pd.isna(v) else float(v)) for k, v in row.items()}
        missing = [k for k, v in vals.items() if v is None]
        if missing:
            raise RuntimeError(f"标的 {inst} 在 {probe_date} 的字段是 NaN: {missing}")
        checked.append(
            {"instrument": inst, "date": probe_date, **{k: round(v, 6) for k, v in vals.items()}}
        )

    return {
        "ok": True,
        "checked": checked,
        "notes": f"qlib 读回通过（{len(checked)} 个标的 × {len(fields)} 个字段）",
    }


# ---------------------------------------------------------------- akshare 交叉核对
def _ak_recent_trade_days(ak_python: str, limit: int = 10) -> list[str]:
    """在 ak env 里问 akshare 最近 N 个交易日（**只作披露，不作闸门**）。

    🔴 交易日门的唯一事实源是 `calendar_gate.py` + 本地 `trade_calendar.csv`；
    akshare 是外网源，会抖 ⇒ 这里只做交叉核对，失败只记 note，不阻断换库。
    """
    limit_i = max(1, int(limit))
    script = (
        "import json\n"
        "import akshare as ak\n"
        "df = ak.tool_trade_date_hist_sina()\n"
        f"days = [str(x) for x in list(df['trade_date'])[-{limit_i}:]]\n"
        "print(json.dumps(days))\n"
    )
    proc = subprocess.run(
        [str(ak_python), "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"akshare 子进程失败 rc={proc.returncode}: {proc.stderr.strip()[-500:]}"
        )
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("akshare 子进程无输出")
    return [str(x) for x in json.loads(lines[-1])]


# ---------------------------------------------------------------- 编排
@_with_traceback
def run_stage1(
    *,
    asof: str,
    provider_dir: str | Path = DEFAULT_PROVIDER_DIR,
    work_root: str | Path = DEFAULT_WORK_ROOT,
    backup_dir: str | Path = DEFAULT_BACKUP_DIR,
    dest_dir: str | Path = DEFAULT_DEST_DIR,
    do_fetch: bool = True,
    do_swap: bool = True,
    dry_run: bool = True,
    ak_python: str | None = None,
    progress=None,
) -> dict:
    """阶段 1 编排：体检 →（可选）下载 → 解压 → 比对 →（可选）换库 → 校验。

    返回 `{"asof","provider_dir","calendar_max_before","calendar_max_after","new_days",
    "downloaded","swapped","verified","dry_run","notes"}`。

    🔴 `dry_run=True`（**默认值**）⇒ **换库阶段磁盘零改动**（活库与备份目录原封不动），
    校验对象是**新快照**；真换库后校验对象是**现役库**（两者都会在 `notes` 里标明）。
    注：解压暂存目录（`work_root/extract_*`）在两种模式下都会生成 —— 它是**比对**所必需的中间物，
    不是对活库的写操作。
    """
    provider_path = Path(provider_dir)
    work_path = Path(work_root)
    backup_path = Path(backup_dir)
    dest_path = Path(dest_dir)
    notes: list[str] = []

    def _emit(message: str) -> None:
        if progress is not None:
            progress(message)

    # ① 体检
    _emit(f"[stage1] 体检现役库 {provider_path}")
    before = inspect_provider(provider_path)
    if not before["ok"]:
        raise RuntimeError(f"现役库体检不通过: {before['notes']}")
    notes.append(f"现役库: {before['notes'][-1]}")

    # ② 下载
    downloaded = False
    if do_fetch:
        _emit(f"[stage1] 下载滚动快照 -> {dest_path}")
        snapshot = fetch_snapshot(dest_path)
        downloaded = True
        tar_path = Path(snapshot["path"])
        notes.append(
            f"下载完成 {snapshot['bytes']} 字节 md5={snapshot['md5']} "
            f"耗时 {snapshot['elapsed_sec']}s -> {tar_path}"
        )
    else:
        archives = sorted(dest_path.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
        if not archives:
            raise FileNotFoundError(
                f"dest_dir={dest_path} 下没有 *.tar.gz（do_fetch=False ⇒ 不下载）"
            )
        tar_path = archives[-1]
        notes.append(f"复用已有压缩包 {tar_path}（未下载）")

    # ③ 解压（绝不覆盖现役库）
    _emit(f"[stage1] 解压 {tar_path} -> {work_path}")
    new_dir = extract_snapshot(tar_path, work_path)
    new_inspect = inspect_provider(new_dir)
    notes.append(f"新快照: {new_inspect['notes'][-1]}")

    # ④ 相位闸门
    _emit("[stage1] 比对日历（相位闸门）")
    comparison = compare_calendars(provider_path, new_dir)
    notes.append(comparison["reason"])
    if not comparison["prefix_identical"]:
        raise RuntimeError(
            "日历前缀不一致 ⇒ 拒绝换库（fail-closed，打分栅格相位会漂移）。"
            f"current_max={comparison['current_max']}, new_max={comparison['new_max']}, "
            f"first_diff_index={comparison['first_diff_index']}"
            f"（current={comparison['first_diff_current']}"
            f" vs new={comparison['first_diff_new']}）；"
            f"新快照已解压在 {new_dir}（供人工排查，确认后可直接手工换库）"
        )

    # ⑤ 换库
    swapped = False
    swap_skipped = False
    if do_swap:
        _emit(f"[stage1] 换库 dry_run={dry_run}")
        swap = swap_provider(
            provider_path, new_dir, backup_dir=backup_path, dry_run=dry_run
        )
        swapped = bool(swap["swapped"])
        notes.append(swap["notes"])
        swap_skipped = bool(swap.get("skipped"))
        if swap_skipped:
            notes.append(
                "🔴 **阶段 1 未换库**：上游快照日历未推进 ⇒ 阶段 2 的 S 层只能打到"
                "现役数据集的覆盖末日（信号日当天的个股 Alpha 本轮拿不到）"
            )
        if swapped:
            staging = new_dir.parent
            if staging.name.startswith("extract_") and staging != work_path:
                shutil.rmtree(staging, ignore_errors=True)
    else:
        notes.append("do_swap=False：只体检 + 比对，不换库")

    # ⑥ 校验（dry-run ⇒ 校验新快照；已换库 ⇒ 校验现役库）
    effective = provider_path if swapped else new_dir
    eff_days = _read_calendar(effective)
    probe = asof if asof in eff_days else (eff_days[-1] if eff_days else asof)
    covers_asof = probe == asof
    if not covers_asof:
        notes.append(
            f"⚠️ asof={asof} 不在有效库日历内（末日={eff_days[-1] if eff_days else None}）"
            f"⇒ 实际校验日={probe}（数据滞后，打分日只能落在 {probe}）"
        )
    _emit(f"[stage1] qlib 读回校验 probe_date={probe} @ {effective}")
    verified = verify_provider(effective, probe)
    notes.append(f"读回校验: {verified['notes']} | {verified['checked']}")

    # ⑦ akshare 交叉核对（只披露，不阻断）
    if ak_python:
        try:
            ak_days = _ak_recent_trade_days(ak_python)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"⚠️ akshare 交易日交叉核对失败（不阻断，仅披露）: {exc}")
        else:
            notes.append(
                f"akshare 最近交易日末日={ak_days[-1] if ak_days else None}"
                f" / 数据集末日={eff_days[-1] if eff_days else None}"
            )

    return {
        "asof": asof,
        "provider_dir": str(provider_path),
        "calendar_max_before": before["calendar_max"],
        "calendar_max_after": eff_days[-1] if eff_days else None,
        "new_days": comparison["new_days"],
        "downloaded": downloaded,
        "swapped": swapped,
        "swap_skipped": swap_skipped,
        "verified": bool(verified.get("ok")),
        "dry_run": dry_run,
        "notes": notes,
    }


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 1：把全市场个股日线装进本地 Qlib 二进制数据集")
    ap.add_argument("--asof", required=True, help="信号日（YYYY-MM-DD），用于校验与披露")
    ap.add_argument("--provider-dir", default=str(DEFAULT_PROVIDER_DIR), help="现役 cn_data 目录")
    ap.add_argument("--dest-dir", default=str(DEFAULT_DEST_DIR), help="压缩包落盘目录")
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT), help="解压临时目录（不落 /tmp）")
    ap.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help="旧库备份目录")
    ap.add_argument("--ak-python", default=None, help="akshare 解释器（可选，仅做交易日交叉核对）")
    ap.add_argument("--no-fetch", action="store_true", help="不下载，复用 dest-dir 里已有的压缩包")
    ap.add_argument("--no-swap", action="store_true", help="只体检 + 比对，不换库")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落盘（默认行为）")
    ap.add_argument("--apply", action="store_true", help="真换库（与 --dry-run 互斥）")
    args = ap.parse_args(argv)

    if args.apply and args.dry_run:
        ap.error("--apply 与 --dry-run 互斥")
    dry_run = not args.apply

    result = run_stage1(
        asof=args.asof,
        provider_dir=args.provider_dir,
        work_root=args.work_root,
        backup_dir=args.backup_dir,
        dest_dir=args.dest_dir,
        do_fetch=not args.no_fetch,
        do_swap=not args.no_swap,
        dry_run=dry_run,
        ak_python=args.ak_python,
        progress=print,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
