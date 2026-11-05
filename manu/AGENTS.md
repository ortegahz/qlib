# AGENTS.md — manu/（qlib 量化研究工程）

> 本文件是**本工程自研层 `manu/`** 的规约。
> 上游 `microsoft/qlib` 的官方文档与开发规约（`docs/developer/code_standard_and_dev_guide.rst`）
> 管上游代码风格；本文件管**本项目**的不可协商约束。
> **完整铁律正文：`manu/memory/rules.md`（1667 行，动手前必读）。**
> 其中 §十二 的 qlib 技术断言**全部经源码核验**（qlib v0.9.8），改动前请先读源码。

---

## 0. 不可协商约束（违反即事故）

| # | 铁律 | 落地动作 |
|:--|:--|:--|
| 〇 | **本地优先** | 一律先改本地 `/media/manu/1TB-Volume/workspace/qlib/`；**严禁直接改服务器代码**；同步判据**只能靠 md5 双侧比对** |
| 〇之二 | **基线冻结** | 数据集构建脚本 / 官方基准配置 / 基准产物**严禁修改**（只可调用/读取/派生到新目录） |
| 〇之三 | **任务边界** | 只做用户交代的事；**禁止自启动未授权的下载/训练/回测/重活**。发现缺口只能记录 + 上报 |
| 一 | **禁擅自启动重活** | 全量下载 / 训练 / 回测 / 特征重建 / >5 min 任务，必须用户当轮明确授权 |
| 二 | **缓存精简** | cache 落 `/mnt/data`（**不落 `/tmp`**）；单文件 >10 GB 先确认；pkl 级控制在 ~100 MB |
| 三 | **自动同步 + md5 比对** | `bash manu/tools/sync_to_server.sh`；**禁止**拿「命令返回 0」当同步成功 |
| 四 | **事实实时入档** | 实验事实与结论实时写进 `manu/memory/`；**事实进 memory，方法进 skill**。🔴 **「有必要的时候更新 memory」＝ 触发器清单**（`rules.md §十四.93` 第 1 条）：结论变 / 口径变 / 缺陷定性 / 交付件变 / 旧数字作废 / 要交接 / 工程结构变 ⇒ **当轮必须回写**，不许留到下一轮；反之**没变就别重写**（memory 是事实源，噪声稀释真值） |
| 五 | **受控 A/B** | 重大非继承性实验严禁越级对比；对比前先自证「同源可比」（同数据/同区间/同成本/同种子） |
| 六 | **上游源码冻结** | **禁止**改 `qlib/`、`examples/`、`tests/`；自研代码一律放 `manu/`；patch 写成独立 `.patch` |
| 九 | **可复现记录** | 每条结论必带**脚本相对路径 + 可复制完整命令 + 执行机 + 产物路径** |
| 十 | **算力归属** | 重活一律上服务器 `192.168.99.40`；host 只做编辑/抽检/文档；**禁止 `nohup &`** 挂常驻重活 |
| 十一 | **关机窗口** | 收尾链必须**服务器端自主执行**（`setsid nohup` + 墙钟硬截止 + 幂等封存 + 保险层先出） |
| 十二 | **量化研究纪律** | 回测口径六项 + region / 禁未来函数（`Ref` 方向已按源码核正）/ 种子只能挂模型构造参数（**`qlib.init(seed=)` 会被静默吞掉**）/ 数据质量断言先行（**特征列数 ≠ processors 声明**）/ 路径显式 / 统计带 n 与 CI / **评估区间不参与选参**（`fit_*` 必须落在训练段内）/ manifest 齐备 |
| 十三 | **交付命令口径** | 每个路径都要能被对方直接执行；**实测路径存在**；**GUI 成功 ≠ CLI 可复现**；每次重跑重发完整命令 |
| ★ | **PyCharm 协同边界（双向！）** | ① PyCharm 的 SFTP 自动上传只覆盖「**在 PyCharm 里改的**」文件 ⇒ **Agent 命令行改的文件 PyCharm 不感知，仍须跑同步脚本**；② 🔴 **反向也成立**：用户在 PyCharm GUI 里改的文件会**直达服务器而本地 checkout 不变** ⇒ **发现分叉必须先问用户**再定以哪侧为准，**严禁**未确认就用本地覆盖服务器 |
| 十四 | **版本库只放「源」** | 核心代码 / 文档 / memory / **输入真值**进版本库；日志 / 运行产物 / 缓存 / 一次性备份 **一律 `.gitignore`**（判定问句：「这文件丢了，明天能重跑出来吗？」）。🔴 上游 `.gitignore` 的全局 `*.csv` 会**静默误杀**输入真值 `manu/etf_rotation/data/account_snapshot.csv` ⇒ 已白名单放行；🔴 该 csv 带 **BOM+CRLF** ⇒ 必须用 `.gitattributes`（`-text`）钉住，防 checkout 行尾归一化改写文件造成 md5 假漂移。详见 `rules.md §十四.93` |

---

## 1. 事实层 vs 方法层（分工硬约束）

| 内容 | 去处 |
|:--|:--|
| 「做过什么 / 当前是什么」（状态、指标、结论） | `manu/memory/`（`memory.md` 主档 / `memory_compact.md` 5 分钟卡 / `rules.md` 铁律 / `archive/` 只增不改 / 专题档） |
| 「怎么做」（SOP、方法论、判别标准） | `manu/skills/`（方法论包 + `REGISTRY.md` 索引） |

**不要在 skill 里写实验历史，也不要在 memory 里写操作步骤。**

---

## 2. 动手前必读（顺序固定）

1. `manu/memory/memory_compact.md` — 5 分钟建立上下文
2. `manu/memory/rules.md` — 铁律正文（尤其 §〇~§〇之三、§一、§十、§十二）
3. `manu/memory/memory.md` — 当前状态快照 + 关键真值 + 命令速查 + 导航表

历史细节走 `manu/memory/archive/`（**按 §编号查导航表，不要线性读归档**）。

---

## 3. 目录结构约定

```
qlib/                          # 上游 microsoft/qlib —— 冻结，禁止修改
manu/                          # 本工程自研层 —— 全部自研代码在这里
├── AGENTS.md                  # 本文件
├── memory/                    # 事实层
│   ├── memory.md              # 主索引（当前状态 / 真值 / 命令 / 导航）
│   ├── memory_compact.md      # 5 分钟速览卡
│   ├── rules.md               # 铁律正文
│   └── archive/               # 历史全文，只增不改
├── skills/                    # 方法层（SOP）
├── tools/                     # 工具脚本（同步脚本、patch、辅助）
├── docs/                      # 设计文档 / 实验方案
├── data/                      # 数据集构建脚本（冻结区，见 rules §〇之二）
├── experiments/               # 单次实验的原始产物
└── evaluation/                # 汇总指标与报告
```

**新增目录前先问**：这个东西是「事实」还是「方法」？是「自研」还是「上游」？
自研 ⇒ 进 `manu/`；上游 ⇒ 冻结；事实 ⇒ `memory/`；方法 ⇒ `skills/`。

---

## 4. 环境

| 项 | 值 |
|:--|:--|
| 本地工程 checkout | `/media/manu/1TB-Volume/workspace/qlib/`（**唯一可写编辑区**） |
| 服务器 | `ssh -F /dev/null -p 32222 huangzhe@192.168.99.40` |
| 服务器工程目录 | `/tmp/pycharm_project_658d3fc4`（**只读 + 同步落地区**） |
| 本机 SSHFS 镜像 | `/home/manu/mnt/pycharm_project_658d3fc4`（**只读 ro**，写不进去，**不是推送通道**） |
| conda env | `ql`（Python 3.12.16）→ `source /home/huangzhe/anaconda3/etc/profile.d/conda.sh && conda activate ql` |
| qlib 安装 | ✅ `pyqlib 0.9.8` **editable** 装到工程目录；numpy 2.5.4 / pandas 2.3.3 / lightgbm 4.7.0 |
| 服务器持久盘 | `/mnt/data`（7.4 T）⇒ **重活产物放这里** |
| 同步脚本 | `bash manu/tools/sync_to_server.sh`（rsync over ssh + md5 双侧比对） |

### 4.1 四个实测坑（每次都会撞）

1. **`ssh` 必须加 `-F /dev/null`** —— `/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf` 权限不对。
2. **服务器 sshd 在 32222**，22 端口关闭。
3. **服务器工程目录无 `.git`** ⇒ 装/重装 qlib 需 `SETUPTOOLS_SCM_PRETEND_VERSION=0.9.8`。
4. **服务器未装 `torch`/`xgboost`** ⇒ `TorchModel`/`XGBModel` 静默跳过；
   **别装 `rl` extra**（会把 numpy 2.5.4 降到 1.x）。

### 4.2 PyCharm 协同（本工程特有）

- 远程解释器：`/home/huangzhe/anaconda3/envs/ql/bin/python`（SFTP 到 `192.168.99.40:32222`）
- 自动上传：`autoUpload=Always`，映射 `local=$PROJECT_DIR$` → `deploy=/tmp/pycharm_project_658d3fc4`
- **在 PyCharm GUI 里改文件** → 自动上传到服务器，**但本地 checkout 不变**。
- **Agent 在命令行改文件** → PyCharm **不感知** ⇒ 必须 `bash manu/tools/sync_to_server.sh`。
- 🔴 **两条方向都要记住**：
  - 正向（本地 → 服务器）：同步脚本负责，负责 Agent 改的文件；
  - **反向（PyCharm GUI → 服务器，用户改的文件本地没有）**：
    ⇒ **发现本地与服务器分叉时，先问用户「这批差异是不是我在 PyCharm 里改的」**，
    **严禁**未确认就用本地覆盖服务器（会静默抹掉用户改动）。

---

## 5. 算力归属 SOP（重活上服务器）

| 事实 | 值 |
|:--|:--|
| 服务器 ssh | **`ssh -F /dev/null -p 32222 huangzhe@192.168.99.40`** |
| 服务器工程目录 | `/tmp/pycharm_project_658d3fc4`（conda env `ql`） |
| 服务器数据 | `/mnt/data/...`（本机镜像 `/home/manu/mnt/data`，**只读**） |
| 常驻作业写法 | `setsid nohup <cmd> > /mnt/data/<name>.log 2>&1 < /dev/null &` |

**host 侧纪律**：
- 起活前查 `uptime` / `free -g`；**load > 6 或 available 内存 < 3 GB ⇒ 不得起新重活**。
- host 单重活 `OMP_NUM_THREADS ≤ 3`、`MKL_NUM_THREADS ≤ 3`；不得并行起 >1 个重活。
- **禁止 `nohup &`**（沙箱会回收，主机重启连带杀死）。

---

## 6. 交付与汇报

- 每个交付物必须能回答：**怎么复现**（命令）、**在哪跑**（机器）、**产物在哪**（路径）、**数字怎么来的**（口径）。
- 汇报里引用的每个回测数字，**必须同时给出** `rules.md §十二·12.1` 的六项口径。
- **不确定就说不确定**；不要用「应该」「大概」掩盖未验证的事实。
