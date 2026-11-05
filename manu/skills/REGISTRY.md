# Skill Registry · qlib 量化研究工程方法论

> **定位**：本目录是**「怎么做」（SOP）**的唯一真值；`manu/memory/` 是**「做过什么 / 当前是什么」（事实）**。
> 二者严格分工：事实进 memory，方法进 skill。**不要在 skill 里写实验历史，也不要在 memory 里写操作步骤。**
> 单一真值：每个 Skill 的正文都在 `manu/skills/<name>/SKILL.md`，本文件只做**索引与触发入口**，不复制正文。

---

## 1. 当前 Skill 速查

| # | Skill | 一句话能力 | 典型触发语 |
|:--|:--|:--|:--|
| — | （暂无） | — | — |

> **后续新增 Skill 时**，在本表登记：名称 / 一句话能力 / 典型触发语 / 正文路径。

---

## 2. 规划中的 Skill（待开题后落地）

| 候选 | 能力 | 依赖的研究方向 |
|:--|:--|:--|
| `qlib-data-pipeline` | 数据集构建 / point-in-time 成分股 / 特征对齐 / 数据质量断言 | 数据源确定后 |
| `qlib-backtest-protocol` | 回测口径六项声明表 / 成本模型 / 组合约束 / 基准对齐 / manifest 产出 | 主线开题后 |
| `qlib-alpha-audit` | 未来函数与幸存者偏差审计（Ref 方向核查、特征平移 sanity check） | 做因子前 |
| `qlib-eval-discipline` | 样本量与置信区间 / 配对检验 / 评估区间不参与选参 | 出结论前 |

---

## 3. 路由指南（典型任务 → 该调哪个）

| 任务形态 | 调用链 |
|:--|:--|
| "先把这个数据集准备好" | `qlib-data-pipeline` → `qlib-alpha-audit` |
| "跑个策略回测看看" | `qlib-backtest-protocol`（**先确认口径表填满**） |
| "这个因子是不是有未来函数" | `qlib-alpha-audit` |
| "这两个策略谁更好" | `qlib-eval-discipline`（配对检验，不看置信区间重叠） |
| "这个数字是哪来的 / 之前做过什么" | **不调 skill** → 读 `manu/memory/memory.md` + `manu/memory/archive/` |

⚠️ **边界**：数据 skill 不负责回测口径，回测 skill 不负责因子审计，审计 skill 不负责显著性判定。
**门禁永远是最后一道**。

---

## 4. 写 Skill 的规约

- **一个 Skill 一个目录**，内含 `SKILL.md`；正文长于 200 行时拆子文件。
- 每份 `SKILL.md` 必须含：**适用场景 / 前置条件 / 步骤 / 产出物 / 失败模式**。
- **禁止**在 Skill 正文写具体实验历史、具体指标数字、具体某次 trial 的结论 ⇒ 那些属于 memory。
- **禁止**在 Skill 正文写「铁律」原文 ⇒ 铁律属于 `manu/memory/rules.md`，Skill 引用其编号即可。
- 新增后**必须**在本文件 §1 登记，并在 `manu/memory/memory.md` §4.1 专题档索引里留一行。
