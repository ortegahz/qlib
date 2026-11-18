"""三道过滤网（doc2）+ 主题聚类（doc1 的「同质化套娃」防御）。

【过滤网 1 · 流动性】
日均成交额 < 5000 万 或 规模 < 2 亿 ⇒ 一票否决（人工买卖会被流动性吃掉）。

【过滤网 2 · 同质化去重】
跟踪同一指数的 ETF 有十几只；Top5 全是「芯片 ETF」等于满仓一条赛道。
实现方式：**主题聚类**（weight-overlap 系数 ∪ 日收益相关性 ∪ 名称族），
每簇只保留**成交额最大**的那只 ⇒ 数据驱动，不需要人工维护「同一指数」表。

🔴 v2 升级：**光靠持仓重叠拦不住同质化**（实测）：
    红利ETF华泰柏瑞(510880) vs 红利ETF易方达(515180)    重叠 0.601 / corr120 0.971
    红利低波易方达(563020)     vs 红利低波华泰柏瑞(512890) 重叠 —    / corr120 0.983
    红利ETF华泰柏瑞(510880) vs 红利低波易方达(563020)     重叠 0.325 / corr120 0.787
第三组**两种数据判据都抓不到**（重叠远低于 0.90、相关性远低于 0.80）
⇒ 只能再加**名称族**兜底。
⇒ 主题判据 = 持仓重叠 ∪ 日收益相关性 ∪ 名称族（任一为真 ⇒ 同簇）。

【过滤网 3 · 行业/风格多样性】
同一主题簇在最终组合里最多 MAX_PER_THEME 只。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402
from engine.features import compute_corr_matrix  # noqa: E402


def apply_liquidity_filter(candidates: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """过滤网 1：成交额 / 规模 / 历史长度。

    `candidates` 需含 code, name, avg_amount_20d, float_mcap；
    `feats` 来自 compute_etf_features（提供 n_days 与 amount_20d_mean）。
    """
    # 🔴 必须把**全部**特征列并进来：下游 add_relative_strength / momentum_score /
    #    portfolio 都要用 mom20/60/120、ma60、above_ma60、vol20；
    #    早先只并了 5 列 ⇒ 动量列在过滤后就丢了 ⇒ 下一步直接 KeyError。
    dup_cols = [c for c in feats.columns if c != "code" and c in candidates.columns]
    df = candidates.merge(feats.drop(columns=dup_cols), on="code", how="left")
    df["liq_amount"] = df["amount_20d_mean"].fillna(df["avg_amount_20d"])
    df["pass_liquidity"] = (
        (df["liq_amount"] >= C.MIN_AVG_AMOUNT_20D)
        & (df["float_mcap"] >= C.MIN_FLOAT_MCAP)
        & (df["n_days"] >= C.MIN_HISTORY_DAYS)
    )
    df["filter_reason"] = np.where(
        df["liq_amount"] < C.MIN_AVG_AMOUNT_20D,
        "成交额不足",
        np.where(
            df["float_mcap"] < C.MIN_FLOAT_MCAP,
            "规模不足",
            np.where(df["n_days"] < C.MIN_HISTORY_DAYS, "历史不足", ""),
        ),
    )
    return df


def _weight_overlap(a: dict[str, float], b: dict[str, float]) -> float:
    """权重重叠系数 = Σ min(w_a, w_b) / min(Σw_a, Σw_b) ∈ [0,1]。"""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    num = sum(min(a[s], b[s]) for s in common)
    den = min(sum(a.values()), sum(b.values()))
    return float(num / den) if den > 0 else 0.0


def _families_of(name: str | None, family_keywords: dict[str, tuple[str, ...]]) -> frozenset[str]:
    """ETF 名称命中的「族」集合（保留给诊断用）。

    🔴 **判定用 `_family_of`（单一主族），不用这个集合**：多族 ETF（港股创新药 同时命中
    「港股」「医药」）用集合交集判定会**跨族串联** ⇒ 实测候选池里出现 **207 只的巨型簇**
    （港股60+医药44+半导体34+通信33+红利28…）⇒ MAX_PER_THEME=1 时整个组合被压成 1 只。
    ⇒ 改为**硬分区**：一个 ETF 只归属**一个**族（按 config 里的族顺序取第一个命中的）。
    """
    if not name:
        return frozenset()
    text = str(name)
    return frozenset(
        family
        for family, keywords in family_keywords.items()
        if any(str(kw) in text for kw in keywords)
    )


def _family_of(name: str | None, family_keywords: dict[str, tuple[str, ...]]) -> str:
    """ETF 的**单一主族**（按族表顺序取第一个命中的）；没命中 ⇒ 空串。

    🔴 硬分区 = 一个 ETF 只属于一个族 ⇒ 族与族之间**不会**因为某只多族 ETF 而被并成一个巨型簇。
    """
    if not name:
        return ""
    text = str(name)
    for family, keywords in family_keywords.items():
        if any(str(kw) in text for kw in keywords):
            return family
    return ""


def _same_theme(
    a: str,
    b: str,
    weight_map: dict[str, dict[str, float]],
    family_map: dict[str, frozenset[str]],
    corr_map: pd.DataFrame | None,
    overlap_thresh: float,
    corr_thresh: float | None = None,
) -> bool:
    """两个 ETF 是否同主题（三个判据取并集）。

    1. 持仓权重重叠 ≥ overlap_thresh
    2. 日收益相关系数 ≥ corr_thresh（corr_map 为 None ⇒ 该判据不生效）
    3. 名称族集合有交集
    """
    if _weight_overlap(weight_map.get(a, {}), weight_map.get(b, {})) >= overlap_thresh:
        return True
    if family_map.get(a, frozenset()) & family_map.get(b, frozenset()):
        return True
    if corr_map is not None and a in corr_map.index and b in corr_map.columns:
        # 🔴 阈值在**调用时**读 config（不是默认参数）⇒ 运行期改 config 立即生效
        thresh = C.THEME_CORR_THRESHOLD if corr_thresh is None else float(corr_thresh)
        value = float(corr_map.at[a, b])
        if np.isfinite(value) and value >= thresh:
            return True
    return False


def build_theme_and_conflicts(
    holdings: pd.DataFrame,
    codes: list[str],
    overlap_thresh: float = C.DEDUP_OVERLAP_THRESHOLD,
    names: dict[str, str] | None = None,
    prices: pd.DataFrame | None = None,
    asof: pd.Timestamp | None = None,
) -> tuple[dict[str, str], set[frozenset[str]]]:
    """算「主题标签」与「直接冲突对」。

    返回
    ----
    theme_map : {code: theme_id}
        theme_id = ETF 的**主族标签**（如「红利」）；未命中任何族 ⇒ 就是它自己的代码
        （自成一簇）。**不做传递闭包** ⇒ 族与族之间不会被多族 ETF 串成巨型簇。
    conflicts : set[frozenset({a, b})]
        **直接**同质的一对（持仓重叠 ≥ 阈值 或 日收益相关性 ≥ 阈值）。
        这类关系**不合并簇**（合并会跨族串联），而是在选股时**逐对拦截**：
        已选中的标的与候选若构成冲突对 ⇒ 候选被拒。
    """
    codes = [str(c) for c in codes]
    h = holdings[holdings["etf_code"].isin(codes)].copy()
    h["weight_pct"] = h["weight_pct"].fillna(0.0)
    weight_map: dict[str, dict[str, float]] = {}
    for code, g in h.groupby("etf_code"):
        weight_map[str(code)] = dict(zip(g["instrument"].astype(str), g["weight_pct"].astype(float)))

    name_map = {str(k): v for k, v in (names or {}).items()}
    theme_map = {code: (_family_of(name_map.get(code), C.THEME_FAMILY_KEYWORDS) or code)
                 for code in codes}

    corr_map: pd.DataFrame | None = None
    if prices is not None and asof is not None:
        corr_map = compute_corr_matrix(prices, codes, asof, C.THEME_CORR_WINDOW)

    conflicts: set[frozenset[str]] = set()
    for i, code_a in enumerate(codes):
        for code_b in codes[i + 1:]:
            if code_a == code_b:
                continue
            hit = False
            if _weight_overlap(weight_map.get(code_a, {}), weight_map.get(code_b, {})) >= overlap_thresh:
                hit = True
            elif corr_map is not None and code_a in corr_map.index and code_b in corr_map.columns:
                value = float(corr_map.at[code_a, code_b])
                if np.isfinite(value) and value >= C.THEME_CORR_THRESHOLD:
                    hit = True
            if hit:
                conflicts.add(frozenset({code_a, code_b}))
    return theme_map, conflicts


def cluster_by_overlap(
    holdings: pd.DataFrame,
    codes: list[str],
    overlap_thresh: float = C.DEDUP_OVERLAP_THRESHOLD,
    names: dict[str, str] | None = None,
    prices: pd.DataFrame | None = None,
    asof: pd.Timestamp | None = None,
) -> dict[str, str]:
    """把 ETF 聚成「主题簇」：持仓重叠 ∪ 日收益相关性 ∪ 名称族。

    三个判据（任一为真 ⇒ 同主题）：
      1. **持仓重叠**：`_weight_overlap(a, b) >= overlap_thresh`（成分股重合 ⇒ 同主题）
      2. **日收益相关性**：近 `C.THEME_CORR_WINDOW` 个交易日的日收益 corr
         ≥ `C.THEME_CORR_THRESHOLD`（需同时给 `prices` 与 `asof`；只用 asof
         及之前的数据 ⇒ **无未来函数**；窗口不足 ⇒ 该对不合并）
      3. **名称族**：名称命中的族集合**有交集**（需给 `names`；族表见
         `C.THEME_FAMILY_KEYWORDS`；「红利低波」同时命中「红利」）

    返回 ``{etf_code: theme_id}``；theme_id = 簇内**在 codes 顺序中最靠前**的那只
    （= 旧贪心的「代表先入簇者」语义；调用方按流动性降序传 codes ⇒ 代表即流动性最大者）。

    🔴 合并用**并查集**（传递闭包）⇒ A~B、B~C ⇒ A、B、C 同簇。
    🔴 比较对象是**簇内所有成员**（而不是只比簇代表）：只比代表会漏网 ——
       若 A~B 因相关性成立、但 B 并进了别的簇，则 A~B 这条关系就断了链，
       后面 C 只跟「代表」比较时同样会漏掉 A~C。
    """
    codes = [str(c) for c in codes]
    h = holdings[holdings["etf_code"].isin(codes)].copy()
    h["weight_pct"] = h["weight_pct"].fillna(0.0)
    weight_map: dict[str, dict[str, float]] = {}
    for code, g in h.groupby("etf_code"):
        weight_map[str(code)] = dict(zip(g["instrument"].astype(str), g["weight_pct"].astype(float)))

    name_map = {str(k): v for k, v in (names or {}).items()}
    family_map: dict[str, frozenset[str]] = {
        code: _families_of(name_map.get(code), C.THEME_FAMILY_KEYWORDS) for code in codes
    }

    corr_map: pd.DataFrame | None = None
    if prices is not None and asof is not None:
        corr_map = compute_corr_matrix(prices, codes, asof, C.THEME_CORR_WINDOW)

    # ---- 并查集（union-find）：传递闭包，A~B、B~C ⇒ A、B、C 同簇 ----
    parent: dict[str, str] = {code: code for code in codes}
    order: dict[str, int] = {code: i for i, code in enumerate(codes)}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]      # 路径压缩
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        lroot, rroot = find(left), find(right)
        if lroot == rroot:
            return
        # 根固定为 codes 顺序更靠前者 ⇒ 结果与遍历顺序无关（确定性）
        if order[lroot] <= order[rroot]:
            parent[rroot] = lroot
        else:
            parent[lroot] = rroot

    for i, code_a in enumerate(codes):
        for code_b in codes[i + 1:]:
            if _same_theme(code_a, code_b, weight_map, family_map, corr_map, overlap_thresh):
                union(code_a, code_b)

    members: dict[str, list[str]] = {}
    for code in codes:
        members.setdefault(find(code), []).append(code)
    clusters: dict[str, str] = {}
    for group in members.values():
        rep = group[0]                              # codes 顺序最靠前 = 簇代表
        for code in group:
            clusters[code] = rep
    return clusters


def apply_theme_cap(
    scored: pd.DataFrame,
    theme_map: dict[str, str],
    max_per_theme: int = C.MAX_PER_THEME,
    n_target: int = C.TARGET_N_HOLDINGS,
    keep_codes: set[str] | None = None,
    hold_buffer: int = 0,
    conflicts: set[frozenset[str]] | None = None,
    prefer_held_in_theme: bool | None = None,
) -> pd.DataFrame:
    """过滤网 3：按分数降序贪心挑选，同主题簇最多 max_per_theme 只。

    🔴 `keep_codes` + `hold_buffer` = **迟滞（hysteresis）**：
    已持有且排名落在 `[n_target, n_target + hold_buffer)` 的标的，
    可以**顶掉**一个「非持有」里分数最低的名额 —— 持仓只数**不增加**（仍是 n_target），
    但换手大幅下降。这是「人工低频调仓」能落地的关键。

    输入 `scored` 需含 code, final_score（已降序或未降序均可）。
    输出：被选中的行 + `selected` 标记 + `theme_id`。

    `conflicts`：**直接**同质的冲突对（持仓重叠/日收益相关性判据，见
    `build_theme_and_conflicts`）。候选若与**已选中的**任一标的构成冲突对 ⇒ 被拒。
    🔴 为什么冲突对**不并簇**：并簇要走传递闭包 ⇒ 多族 ETF（港股创新药）会把
    「港股」「医药」「半导体」整片串成一个巨型簇（实测 207 只）⇒ 组合被压成 1 只。
    """
    df = scored.copy().sort_values("final_score", ascending=False, na_position="last").reset_index(drop=True)
    df["theme_id"] = df["code"].map(theme_map).fillna(df["code"])
    keep = {str(c) for c in keep_codes} if keep_codes else set()
    conflict_set = conflicts or set()

    def _conflicts_with_selected(code: str, positions: list[int]) -> bool:
        if not conflict_set:
            return False
        for other in positions:
            if frozenset({code, str(df.iloc[other]["code"])}) in conflict_set:
                return True
        return False

    # ---- 第一步：按分数贪心选满 n_target（主题上限 + 冲突对约束下）----
    selected_pos: list[int] = []
    theme_count: dict[str, int] = {}
    theme_holder: dict[str, str] = {}   # theme_id -> 占位的 code
    for pos in range(len(df)):
        if len(selected_pos) >= n_target:
            break
        row = df.iloc[pos]
        if not np.isfinite(row["final_score"]):
            continue
        tid = row["theme_id"]
        if theme_count.get(tid, 0) >= max_per_theme:
            continue
        if _conflicts_with_selected(str(row["code"]), selected_pos):
            continue
        selected_pos.append(pos)
        theme_count[tid] = theme_count.get(tid, 0) + 1
        theme_holder[tid] = str(row["code"])

    # ---- 第一步b：同族内「已持有品种优先」----
    #   同一族里的两只 ETF 暴露几乎相同（例：512800 银行ETF华宝 vs 515020 银行ETF华夏），
    #   同族内部换手**不改变组合暴露，只增加摩擦成本** ⇒ 已持有的占位者让位给已持有的同族品种。
    #   ⚠️ 只动同族内部的取舍：入选只数、族结构、其它族的取舍完全不变。
    if prefer_held_in_theme is None:
        prefer_held_in_theme = bool(getattr(C, "THEME_PREFER_HELD", False))
    if prefer_held_in_theme and keep:
        selected_codes = {str(df.iloc[p]["code"]) for p in selected_pos}
        for tid, occ_code in list(theme_holder.items()):
            if occ_code in keep:
                continue
            same_theme = df[(df["theme_id"] == tid) & (~df["code"].astype(str).isin(selected_codes))]
            held_same = same_theme[same_theme["code"].astype(str).isin(keep)]
            held_same = held_same[np.isfinite(held_same["final_score"])]
            if held_same.empty:
                continue
            new_pos = int(held_same["final_score"].idxmax())
            new_code = str(df.iloc[new_pos]["code"])
            occ_pos = next((p for p in selected_pos if str(df.iloc[p]["theme_id"]) == tid), None)
            if occ_pos is None:
                continue
            # 让位者必须与「其余已选」都不冲突
            if _conflicts_with_selected(new_code, [p for p in selected_pos if p != occ_pos]):
                continue
            selected_pos[selected_pos.index(occ_pos)] = new_pos
            theme_holder[tid] = new_code
            selected_codes = {str(df.iloc[p]["code"]) for p in selected_pos}

    # ---- 第二步：迟滞 —— 已持有的边缘标的顶掉「非持有」里分数最低的那个 ----
    if keep and hold_buffer > 0:
        for pos in range(n_target, min(n_target + hold_buffer, len(df))):
            row = df.iloc[pos]
            code = str(row["code"])
            if code not in keep or not np.isfinite(row["final_score"]):
                continue
            # 被挤掉的是「已选中的、且**不是当前持仓**的」里分数最低的那只
            # （防御者排名 5~7，其分数必然低于全部已选 ⇒ 不能用分数比较来挑 victim）
            victims = [p for p in selected_pos if str(df.iloc[p]["code"]) not in keep]
            if not victims:
                continue
            victim = min(victims, key=lambda p: df.iloc[p]["final_score"])
            tid_v, tid_n = df.iloc[victim]["theme_id"], row["theme_id"]
            if theme_count.get(tid_n, 0) >= max_per_theme:
                continue
            # 🔴 迟滞进来的标的也要过冲突对检查（否则能把「已选的同类」换进来）
            remaining = [p for p in selected_pos if p != victim]
            if _conflicts_with_selected(code, remaining):
                continue
            theme_count[tid_v] = max(0, theme_count.get(tid_v, 1) - 1)
            # 🔴 `victim` 是**位置值**（在 df 中的行号），不是 `selected_pos` 的下标。
            #    早先写成 `selected_pos[victim] = pos` ⇒ 当 victim >= len(selected_pos) 时
            #    抛 `IndexError: list assignment index out of range`
            #    ⇒ 迟滞分支**每次都崩** ⇒ 整期调仓被跳过（持仓/评估窗口全错）。
            #    正确写法：按**值**定位它在 selected_pos 里的下标再替换。
            selected_pos[selected_pos.index(victim)] = pos
            theme_count[tid_n] = theme_count.get(tid_n, 0) + 1

    df["selected"] = False
    if selected_pos:
        df.loc[df.index[selected_pos], "selected"] = True
    return df
