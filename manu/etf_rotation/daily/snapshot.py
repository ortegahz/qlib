"""snapshot.py —— **快照层**：每次运行落一个**不可变**的独立目录。

【为什么存在（2026-11-09 架构瘦身）】
旧形态：每日链把产物写进**根目录全局共享文件**（canonical ``stock_scores.csv`` /
``tradingagents_decision.md`` / ``rebalance_recommendation.json``）⇒
① 同一根目录被多条链共享 ⇒ **跨链路污染**（旧影子链曾覆盖生产金标）；
② 两份跑的产物混在一处 ⇒ **无法 diff**，回答不了「11:30 那次和 15:05 那次差在哪」；
③ 「不写全局文件」只能靠 if 分支守 ⇒ 改一处漏一处就污染。

新形态：**每次运行 = 一个快照目录** ⇒
* 自包含：交付件 + 上下文三件套 + manifest 都在同一个目录里；
* 物理隔离：跑完即冻结，**旧快照逐字节不变** ⇒ 可以随时 diff 两个快照；
* 无状态引擎：输入 = 当场拉到的数据；输出 = 当场落下的快照目录。

【不变式（硬）】
* I1 快照目录**只增不改**：``finalize()`` 之后目录内文件不再被写；
* I2 **每日链不写根目录全局文件**（canonical 分数 / 金标决策件 / 规则层推荐件）；
* I3 **副作用由 ``live_trade`` 开关决定**：缺省 = 观察态（执行卡挂单禁用、不发信）；
* I4 manifest 是快照的**唯一权威索引**：阶段状态 / 跳过项 / 实盘开关 / 每件 sha1。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "MANIFEST_NAME",
    "RUN_ID_FORMAT",
    "Snapshot",
    "create_snapshot",
    "latest_snapshot",
    "resolve_scores_base",
    "root_global_fingerprint",
    "sha1_of",
    "md5_of",
    "file_digest",
]

#: manifest 文件名（快照目录里**唯一**由本模块自己写的文件）
MANIFEST_NAME = "snapshot_manifest.json"

#: 快照目录名格式（``YYYYMMDD_HHMMSS`` ⇒ 字典序 == 时间序）
RUN_ID_FORMAT = "%Y%m%d_%H%M%S"

#: 🆕 **观察态水印**（缺省 ``live_trade=False`` ⇒ 挂单禁用、非实盘信号）
#:    报告标题 / 执行卡 / 终端提示**共用同一句** ⇒ 不许各写各的
OBSERVATION_WATERMARK = "【观察态 · 挂单禁用 · 非实盘信号】"
OBSERVATION_DISCLAIMER = (
    "本轮**未开 `--live-trade`** ⇒ 执行卡**挂单禁用**、**未发信**；"
    "分数与交付件只落**本快照目录**，**未写入任何根目录全局文件**。"
)


def _now() -> datetime:
    return datetime.now().astimezone()


def file_digest(path: str | Path, algo: str = "sha1") -> str | None:
    """算文件摘要；文件不存在 ⇒ 返回 ``None``（**不抛**）。

    🔴 分块读 ⇒ 大文件（``stock_scores.csv`` 百万行级）不吃内存。
    """
    target = Path(path)
    if not target.is_file():
        return None
    digest = hashlib.new(algo)
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha1_of(path: str | Path) -> str | None:
    """文件 sha1（不存在 ⇒ ``None``）。"""
    return file_digest(path, "sha1")


def md5_of(path: str | Path) -> str | None:
    """文件 md5（不存在 ⇒ ``None``）。"""
    return file_digest(path, "md5")


def _unique_run_id(root: Path, run_id: str) -> str:
    """同一秒内连跑两次 ⇒ 目录名加后缀，**绝不覆盖**已有快照。"""
    candidate = run_id
    suffix = 1
    while (root / candidate).exists():
        suffix += 1
        candidate = f"{run_id}_{suffix:02d}"
    return candidate


class Snapshot:
    """一个不可变快照目录（**唯一 owner** = 本类）。

    用法::

        snap = create_snapshot(root, asof="2026-11-09", live_trade=False)
        snap.write_json("execution_card_20261109.json", card)
        snap.set_stage("analyze", "ok", seconds=12.3)
        snap.finalize()
    """

    def __init__(self, root: str | Path, *, asof: str, run_id: str,
                 live_trade: bool = False, created_at: datetime | None = None) -> None:
        self.root = Path(root)
        self.asof = str(asof)
        self.run_id = str(run_id)
        self.live_trade = bool(live_trade)
        self.created_at = created_at or _now()
        self.path = self.root / self.run_id
        self._stages: list[dict] = []
        self._skipped: list[dict] = []
        self._notes: list[str] = []
        self._finalized = False

    # ---- 路径 ---------------------------------------------------------------
    def file(self, name: str | Path) -> Path:
        """快照内某件的绝对路径（**不创建**）。"""
        return self.path / str(name)

    def ensure_dir(self, name: str | Path = ".") -> Path:
        """快照内子目录（自动创建）。"""
        target = self.path if str(name) == "." else self.path / str(name)
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ---- 写入 ---------------------------------------------------------------
    def write_text(self, name: str | Path, text: str) -> Path:
        """写文本件（父目录自动创建）。"""
        target = self.file(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_json(self, name: str | Path, payload: Any) -> Path:
        """写 JSON 件（``ensure_ascii=False`` ⇒ 中文可读）。"""
        target = self.file(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return target

    def copy_from(self, src: str | Path, name: str | Path | None = None) -> Path | None:
        """把外部件**复制**进快照（源不存在 ⇒ 返回 ``None``，不抛）。

        🔴 用复制而不是软链：快照必须**自包含且逐字节可复现**。
        """
        source = Path(src)
        if not source.is_file():
            return None
        target = self.file(name if name is not None else source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    # ---- manifest 累积 ------------------------------------------------------
    def set_stage(self, name: str, status: str = "ok", **detail: Any) -> dict:
        """登记一个阶段的执行状态（``ok`` / ``failed`` / ``skipped``）。"""
        entry = {"name": str(name), "status": str(status)}
        entry.update(detail)
        self._stages.append(entry)
        return entry

    def set_skipped(self, name: str, reason: str) -> dict:
        """登记一个**被跳过**的环节（🔴 绝不静默降级 ⇒ 必须显式登记）。"""
        entry = {"name": str(name), "reason": str(reason)}
        self._skipped.append(entry)
        return entry

    def add_note(self, note: str) -> None:
        """追加一条披露性说明。"""
        self._notes.append(str(note))

    # ---- 落盘 ---------------------------------------------------------------
    def artifacts(self) -> list[dict]:
        """快照内所有件（**排除 manifest 自身**）的大小 + sha1。"""
        items: list[dict] = []
        if not self.path.is_dir():
            return items
        for item in sorted(self.path.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(self.path).as_posix()
            if rel == MANIFEST_NAME:
                continue
            items.append({
                "name": rel,
                "bytes": item.stat().st_size,
                "sha1": sha1_of(item),
            })
        return items

    def manifest_payload(self) -> dict:
        """manifest 的内容（**纯函数** ⇒ 可单测）。"""
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "asof": self.asof,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "finalized_at": _now().isoformat(timespec="seconds"),
            "snapshot_root": str(self.root),
            "snapshot_path": str(self.path),
            "live_trade": self.live_trade,
            "stages": list(self._stages),
            "skipped": list(self._skipped),
            "notes": list(self._notes),
            "artifacts": self.artifacts(),
        }

    def finalize(self) -> Path:
        """写 manifest ⇒ 快照**冻结**。返回 manifest 路径。

        🔴 幂等：重复调用只是重写 manifest（内容按当前目录重算）。
        """
        self.path.mkdir(parents=True, exist_ok=True)
        target = self.file(MANIFEST_NAME)
        target.write_text(
            json.dumps(self.manifest_payload(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._finalized = True
        return target

    @property
    def finalized(self) -> bool:
        """是否已经 ``finalize()`` 过。"""
        return self._finalized

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Snapshot {self.run_id} live_trade={self.live_trade} path={self.path}>"


def create_snapshot(root: str | Path, *, asof: str, live_trade: bool = False,
                    run_id: str | None = None, now: datetime | None = None) -> Snapshot:
    """建一个新快照目录（**不写任何文件**，只 mkdir）。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = now or _now()
    wanted = str(run_id) if run_id else stamp.strftime(RUN_ID_FORMAT)
    unique = _unique_run_id(base, wanted)
    snap = Snapshot(base, asof=asof, run_id=unique, live_trade=live_trade, created_at=stamp)
    snap.path.mkdir(parents=True, exist_ok=True)
    return snap


def manifest_asof(path: str | Path) -> str | None:
    """读某份快照 manifest 里的 ``asof``（读不到 / 坏文件 ⇒ None，**不许炸**）。"""
    manifest = Path(path) / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("asof")
    return str(value) if value else None


def latest_snapshot(root: str | Path, *, exclude: str | Path | None = None,
                    asof: str | None = None) -> Path | None:
    """快照根下**最新**的一份快照目录（按目录名排序 ⇒ 字典序 == 时间序）。

    🔴 ``exclude``：正在写的那一份（避免把「本轮自己」当成底座）。

    🔴 2026-11-10 新增 ``asof``：**优先**挑 manifest 里 ``asof`` **相同**的那一份。
    否则补跑（asof = 过去的信号日）会拿**别的信号日**的快照当底座
    ⇒ ① 底座分数可能含 **asof 之后**的行 ⇒ 快照藏未来行 ⇒ A9 门禁误红；
    ⇒ ② `--skip-llm` 复用别个信号日的 LLM 决策件 ⇒ 交付件嵌的是**另一个信号日**的意见。
    没有任何 asof 匹配时 ⇒ 退回「按目录名取最新」（由调用方自行判断可用性）。
    """
    base = Path(root)
    if not base.is_dir():
        return None
    excluded = Path(exclude).name if exclude is not None else None
    candidates = [
        item for item in base.iterdir()
        if item.is_dir() and item.name != excluded and not item.name.startswith(".")
    ]
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda p: p.name)
    if asof:
        matched = [item for item in ordered if manifest_asof(item) == str(asof)]
        if matched:
            return matched[-1]
    return ordered[-1]


def snapshots_with_file(root: str | Path, filename: str, *,
                        exclude: str | Path | None = None,
                        asof: str | None = None) -> list[Path]:
    """快照根下**确实含有 ``filename``** 的快照列表（**从新到旧**）。

    🔴 2026-11-11（第九轮续）：`latest_snapshot_with_file` 只给「最新一份」，
       而复用决策件时**最新那份未必可用**（实测 `20261111_161126`：它复用了别的
       快照的决策件、却没把**那份上下文**一起带过来 ⇒ 决策件的 `context_sha1`
       与它自己的 context md 对不上 ⇒ sha1 链断裂）。
       ⇒ 调用方要能**按顺序试**多个候选 ⇒ 这里给出**列表**版本。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    excluded = Path(exclude).name if exclude is not None else None
    candidates = [
        item for item in base.iterdir()
        if item.is_dir() and item.name != excluded and not item.name.startswith(".")
    ]
    ordered = sorted(candidates, key=lambda p: p.name, reverse=True)
    if asof:
        ordered = [item for item in ordered if manifest_asof(item) == str(asof)]
    return [item for item in ordered if (item / filename).is_file()]


def latest_snapshot_with_file(root: str | Path, filename: str, *,
                             exclude: str | Path | None = None,
                             asof: str | None = None) -> Path | None:
    """快照根下**最新且确实含有 ``filename``** 的那份快照（找不到 ⇒ ``None``）。

    🔴 2026-11-11（真机跑批事故）：`--skip-llm` 复用底座时，旧口径只取
       「**最新**一份快照」⇒ 若那份是**被中断的不完整快照**（没有决策件），
       就**复制不到任何决策件**⇒ 本轮**静默变成「无 TA 意见」**
       ⇒ 交付件里写「本轮无 TA 否决」、🛡 TA 合规筛选整节失效（且**不报错**）。
    ⇒ 现在：按目录名**从新到旧**找**第一个真的有该文件**的快照。

    🔴 **asof 语义不变**：只考虑 manifest ``asof`` **相同**的快照
       （复用别个信号日的决策件 ⇒ 交付件嵌的是另一个信号日的意见 ⇒ 口径错误）。
    """
    found = snapshots_with_file(root, filename, exclude=exclude, asof=asof)
    return found[0] if found else None


def resolve_scores_base(snapshot_root: str | Path, canonical_csv: str | Path,
                         *, exclude: str | Path | None = None,
                         asof: str | None = None) -> tuple[Path | None, str]:
    """给 S 层挑一个**只读底座**（历史分数）。

    口径（**链式**）：
    1. **优先**上一份快照的 ``stock_scores.csv`` ⇒ 每日链自成一链，
       **永远不碰** canonical；
    2. 没有任何快照时 ⇒ 用 canonical 作**一次性只读种子**（复制，不写回）；
    3. 两者都没有 ⇒ ``None`` ⇒ S 层全量重算。

    返回 ``(底座路径, 说明)``；底座路径为 ``None`` 时说明里写明「无底座」。
    """
    prev = latest_snapshot(snapshot_root, exclude=exclude, asof=asof)
    if prev is not None:
        candidate = prev / "stock_scores.csv"
        if candidate.is_file():
            base_asof = manifest_asof(prev)
            same = (base_asof == str(asof)) if asof else True
            return candidate, (
                f"底座 = **上一份快照** {candidate}"
                "（链式口径 ⇒ 每日链不读写根目录 canonical）"
                + (
                    f"；底座 asof = {base_asof}（**与本轮一致**）"
                    if same
                    else f"；🔴 底座 asof = {base_asof}，**与本轮 {asof} 不一致**"
                    " ⇒ 分数可能含 asof 之后的行（补跑场景的历史包袱）"
                )
            )
    canonical = Path(canonical_csv)
    if canonical.is_file():
        return canonical, (
            f"底座 = canonical {canonical}（**一次性只读种子**；仅当还没有任何快照时使用）"
        )
    return None, "底座 = **无** ⇒ S 层全量重算（打分窗口可能比生产短）"


def root_global_fingerprint(paths: Iterable[str | Path]) -> dict[str, dict]:
    """给一批**根目录全局文件**取指纹（size + sha1 + mtime）⇒ 用来证明「没被写」。

    🔴 只读：这是**审计工具**，不是写入路径。
    """
    out: dict[str, dict] = {}
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            out[str(path)] = {"exists": False}
            continue
        stat = path.stat()
        out[str(path)] = {
            "exists": True,
            "bytes": stat.st_size,
            "sha1": sha1_of(path),
            "mtime": stat.st_mtime,
        }
    return out
