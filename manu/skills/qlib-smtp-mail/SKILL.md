---
name: qlib-smtp-mail
description: |
  用 SMTP 发邮件的完整操作规程（126 / 网易邮箱授权码、凭据落盘、连通性自检、按需发信）。
  Load this skill whenever the user says "发封邮件给我看看", "邮件发不出去 / 收不到邮件", "535 认证失败",
  "126 授权码怎么弄", "把巡检邮件发出去", "顺便把邮件也发一份", or asks to wire an email notifier into any job in this
  repo. It covers the client-authorization-code (授权码) vs web-login-password distinction, the 0600 secrets
  file contract, the standalone `manu/tools/send_test_mail.py` probe, the four-template renderer in
  `manu/etf_rotation/daily/notify_email.py`, the daily pipeline's notify stage, and the on-demand
  (按需) report path that replaced the retired cron/定时推送 form.
  Do NOT use it to design mail content strategy, to mass-mail third parties, or to store credentials in git.
---

# qlib-smtp-mail —— SMTP 邮件推送

> **定位**：本 skill 讲「**怎么把信发出去**」。
> 「项目现在处于什么状态 / 上次发信的结果如何」属**事实层** ⇒ 写 `manu/memory/memory.md`，**不写在这里**。
> 不可协商约束见 `manu/memory/rules.md`（本文件只引用条目号，不复制铁律原文）。

---

## 1. 适用场景

- 需要**立刻**发一封邮件验证通道 / 验证收件箱（用户验收口径常是「我有没有收到」）；
- 把邮件接进某个**按需分析任务**（每日巡检、调仓决策卡、失败告警）—— 🔴 **邮件是 opt-in，不是默认动作**（见 §3.4 / §3.5）；
- 排查「**发不出去** / **收不到**」的 SMTP 问题；
- 需要一封**结构化**的告警信（带表格；字段缺失要显式渲染成「—」）。

**不适用**：邮件内容策略设计、群发第三方、任何需要把凭据入库的场景。

---

## 2. 前置条件

| 项 | 要求 | 怎么确认 |
|:--|:--|:--|
| Python | 标准库即可（`smtplib` + `email.mime`），**不需要**额外包 | `python -c "import smtplib"` |
| 网络 | 能连到目标 SMTP 主机 | `getent hosts smtp.126.com` |
| 端口 | 465（隐式 SSL）或 25（STARTTLS）二选一 | `timeout 5 bash -c '</dev/tcp/smtp.126.com/465' && echo open` |
| 凭据 | **客户端授权码**（16 位），**不是**网页登录密码 | 见 §3.1 |
| 落盘位置 | 一个**只有自己能读**的 env 文件（0600） | 见 §3.2 |

🔴 **铁律〇（本地优先）**：代码只改 host（`/media/manu/1TB-Volume/workspace/qlib/`），
再 `bash manu/tools/sync_to_server.sh <paths>` 同步到服务器，**判据只能 md5 双侧比对**；
**严禁直接改服务器代码**。唯一例外是 §3.2 的**凭据文件**——那是凭据，不是代码。

🔴 **铁律十四（版本库只放「源」）**：授权码**绝不**进版本库、不进日志、不进邮件正文。

---

## 3. 步骤

### 3.1 取凭据：授权码 ≠ 登录密码

网易系邮箱（126 / 163 / yeah.net）的 SMTP **只认「客户端授权码」**，网页登录密码一律被拒。
拿授权码的路径：

1. 浏览器登录邮箱 → **设置** → **POP3/SMTP/IMAP**；
2. 确认 **IMAP/SMTP服务 = 已开启**（发信必须开）；
3. **授权密码管理** → **新增授权码**；
4. 绑定的手机号收**短信验证码** → 填入；
5. 页面显示 16 位授权码 —— **这个码才是 SMTP 密码**，**只在这一次显示**；
6. 授权码有独立有效期，过期或改密后要重新走上面 5 步。

> 判定口诀：**登录密码 = 给人用的；授权码 = 给机器用的。** 两者混用 ⇒ `535 authentication failed`。
> 拿到授权码后立刻按 §3.2 落盘，不要留在聊天记录里过夜。

### 3.2 写凭据文件（0600，格式固定）

服务器侧固定落点：`/mnt/data/siping/secrets/smtp.env`（目录也在服务器持久盘 `/mnt/data` 上）。

```text
SMTP_HOST=smtp.126.com
SMTP_PORT=465
SMTP_USER=<你的邮箱地址>
SMTP_PASSWORD=<16 位客户端授权码>
SMTP_SENDER=<你的邮箱地址>
```

硬要求：

- 文件权限**必须 0600**（`chmod 600`）；本工程的读取器会**主动拒绝**权限过宽的文件并给警告；
- 键名只认上面这 5 个；多余的键会被忽略并点名告警（防注入任意环境变量）；
- **不进版本库**（`.gitignore` 已忽略 `*.env` / `secrets/`）；
- 写的时候**用 stdin 管道**传内容，不要把授权码放进命令行参数（会进 `ps` 与 shell history）。

```bash
# 模板（把 <...> 换成真值；用管道传，别进 argv）
cat > /tmp/_smtp.env <<'EOF'
SMTP_HOST=smtp.126.com
SMTP_PORT=465
SMTP_USER=<你的邮箱地址>
SMTP_PASSWORD=<16 位客户端授权码>
SMTP_SENDER=<你的邮箱地址>
EOF
ssh -p <端口> <用户>@<服务器> \
  'cat > /mnt/data/siping/secrets/smtp.env && chmod 600 /mnt/data/siping/secrets/smtp.env' < /tmp/_smtp.env
```

### 3.3 连通性自检（先自检，再跑整条链）

本工程有**独立**探针脚本 `manu/tools/send_test_mail.py`（纯标准库，不依赖项目模块）：

```bash
# 服务器侧：发一封真实测试信（加 --dry-run 则只落 .eml 不发）
python manu/tools/send_test_mail.py \
  --secrets-file /mnt/data/siping/secrets/smtp.env \
  --to <收件人> \
  --subject "[ETF量化巡检] SMTP 连通性测试" \
  --text "授权码已生效，邮件管道打通。"
```

脚本输出 JSON：`{"sent": true, ...}` ⇒ **通道已通**；`sent: false` 且 `dry_run: true` ⇒ 只落了 `.eml`，**没发**。

🔴 **验收口径是「用户收件箱里真的看到信」**，不是「命令返回 0」。
`sent: true` 只代表**服务器已接受投递**，最终仍需用户确认收件箱。

### 3.4 走流水线发信（本工程已有实现；**默认不发**）

每日 ETF 轮动流水线**已经内置**发信能力，不要另起炉灶：

| 能力 | 位置 |
|:--|:--|
| 四套模板渲染（日常正常 / 紧急破位 / 月度调仓卡 / 流水线失败告警） | `manu/etf_rotation/daily/notify_email.py` |
| 模板选择与编排 | `manu/etf_rotation/daily/main.py`（阶段 5「通知」） |
| 邮件正文里的账户卡片 | `manu/etf_rotation/daily/account_state.py` |

```bash
# 服务器侧手动跑一次（🔴 不加 --send-email ⇒ 只渲染报告，**不发信**）
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --skip-fetch

# 只渲染不发（--dry-run：不联网取数，.eml 落盘，便于肉眼验收排版）
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --dry-run

# 🔴 显式发信（按需 opt-in：默认行为是**不发邮件**）
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-email
```

发信配置读取顺序：**环境变量（`ETF_ROTATION_SMTP_*` / `SMTP_*`）→ 0600 secrets 文件 → 模块默认**。

### 3.5 🔴 按需报告路径（**当前唯一交付形态**：不发邮件，报告回窗口）

```bash
# 默认：渲染 Markdown 报告 → 落盘 manu/report/report_<YYYYMMDD>_mode<A|B>.md → 同时打印到 stdout
python manu/etf_rotation/daily/main.py --asof 2026-11-05

# 指定报告落盘路径
python manu/etf_rotation/daily/main.py --asof 2026-11-05 \
    --report-md /mnt/data/siping/runs/etf_rotation/daily/report_20261105_modeB.md
```

- **触发方式**：用户在窗口说「分析」⇒ 主 agent 在服务器跑流程 ⇒ **报告直接回窗口**（stdout 即交付件）；
- 🔴 **不再有定时触发**：2026-11-05 已取消服务器 cron 定时（用户 crontab 里的 5 条已全部卸载）；
- 🔴 **邮件默认不发**：要发必须显式 `--send-email`（**按需 opt-in**）；
- ❌ **不要再往 crontab / systemd timer 装本工程条目**：`install_schedule.sh` 已随定时取消一并删除。
- **退出码**：`0` 成功（含非交易日静默退出）· `1` 失败 · `2` 用法错误 / 锁冲突。

---

## 4. 产出物

| 产出物 | 落点 | 说明 |
|:--|:--|:--|
| 凭据文件 | `/mnt/data/siping/secrets/smtp.env` | **0600**，不进版本库 |
| 单封测试信 | 无（直接投递） | `--dry-run` 时落 `<out>/<时间戳>_<主题>.eml` |
| 巡检邮件 | 收件箱 | 主题模板见 `notify_email.py` 的四个 `render_*` |
| **Markdown 报告（主交付件）** | `manu/report/report_<YYYYMMDD>_mode<A|B>.md`（env `ETF_ROTATION_REPORT_DIR` / `--report-md` 可改路径） | **不发邮件时的默认交付物**，同时打印到 stdout |
| 流水线产物 | `/mnt/data/siping/runs/etf_rotation/daily/<YYYYMMDD>/` | 含 `execution_card_*.json` / `heartbeat_*.json` / `email_draft_*.json` / `.eml` |
| 结构化日志 | `/mnt/data/siping/runs/etf_rotation/daily/logs/daily_run_YYYYMMDD.log` | 格式 `时间 \| 级别 \| 阶段 \| 消息` |

---

## 5. 失败模式

| 现象 | 真因 | 处置 |
|:--|:--|:--|
| `535 authentication failed` | 用了**网页登录密码**而不是授权码；或授权码已过期/被删 | 重走 §3.1 重新生成授权码 |
| `553 authentication is required` | 直投 MX 被拒（126 不允许绕过认证投递） | 必须走 SMTP + 授权码 |
| `553 Local user only` | 换成了非本账号域名的发件人 | `SMTP_SENDER` 必须是同一账号 |
| `Connection refused` / 超时 | 端口被墙，或 465/25 选错 | `getent hosts` + `/dev/tcp` 探测；465 走 SSL、25 走 STARTTLS |
| 读取器**忽略**了 secrets 文件 | 权限过宽（`mode & 0o077 != 0`） | `chmod 600` 后重试 |
| 脚本报 `missing_fields` | env 文件缺键 / 键名拼错 | 只认 `SMTP_HOST/PORT/USER/PASSWORD/SENDER` 五个 |
| **命令返回 0 但用户说没收到** | 落在「已发送」而非「已投递」；或被收件箱规则拦了 | 看日志 `sent=True` 后**让用户查垃圾箱 / 订阅文件夹**，再试一封带明显标题的测试信 |
| 邮件正文数字对不上 | 渲染时字段缺失被硬编 | 缺字段**必须渲染成「—」**，不许反推、不许填旧值 |
| 报告没回窗口 | 跑的是旧命令（没带 `--asof`）或 stdout 被吞 | 看 `manu/report/report_<YYYYMMDD>_mode<A|B>.md` 是否生成；退出码非 0 ⇒ 异常已落盘 |

---

## 6. 边界（明确不做）

- **不改**上游 `qlib/` `examples/` `tests/`（铁律六）；自研代码只进 `manu/`；
- **不**把授权码写进任何代码、日志、memory、版本库；
- **不**主动发信：邮件是 **opt-in**（`--send-email`），默认交付物是**回窗口的 Markdown 报告**；
- **不**为了「顺手」就跑重活（打分 / 回测 / LLM > 5 min）—— 需用户当轮授权（铁律一）；
- **不**在没有用户确认的情况下反复重发测试信（收件箱会被刷屏）；
- **不**用 try/except 静默吞掉发信异常：发不出去必须**显式失败**并触发失败告警路径。
