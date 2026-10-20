# arXiv 每日论文日报

每天早上 **9:00** 自动抓取 arXiv 指定分类（默认 `cs.CL`）最新提交的 **50 篇**论文，
把标题、作者、分类和完整摘要排版成邮件，发送到你指定的邮箱。

- 数据源：arXiv 官方 API，按提交时间倒序，包含完整摘要
- 自动去重：`state/seen.json` 记录已推送论文，跨天不会重复
- 周末/无更新自动跳过（可配置为重发）
- 邮件同时包含纯文本版，手机端阅读友好

## 目录结构

```
arxiv-daily/
├─ arxiv_daily.py      主程序
├─ config.ini          邮箱与抓取配置（需要你填写）
├─ run_daily.bat       计划任务调用的启动脚本
├─ install_task.bat    一键创建 Windows 计划任务
├─ install_task.ps1
├─ state/seen.json     已推送论文记录（自动生成）
├─ logs/               运行日志（自动生成）
└─ out/                --dry-run 生成的邮件预览（自动生成）
```

## 一、获取 126 邮箱授权码

1. 浏览器登录 <https://mail.126.com>
2. 顶部 **设置** → **POP3/SMTP/IMAP**
3. 开启 **SMTP 服务**（会要求手机验证）
4. 点击 **新增授权密码**，得到一串 16 位左右的授权码（只显示一次，记下来）

> 授权码不是邮箱登录密码。填错会报 `535 Authentication failed`。

## 二、填写 config.ini

```ini
[smtp]
host = smtp.126.com
port = 465
user = 你的邮箱@126.com
auth_code = 刚才拿到的授权码

[mail]
to = 收件邮箱（可以就是你自己）
```

如果要看多个方向，改这里（逗号分隔）：

```ini
[arxiv]
categories = cs.CL, cs.AI, cs.LG
```

## 三、测试

先预览邮件样式（不发送、不记录已读）：

```bash
cd "D:\北科 研0\arxiv-daily"
python arxiv_daily.py --dry-run
```

会生成 `out/preview_日期.html`，用浏览器打开即可查看效果。

再发一封测试邮件，验证授权码：

```bash
python arxiv_daily.py --test-email
```

收到测试邮件后，手动完整跑一次：

```bash
python arxiv_daily.py
```

## 四、安装定时任务

双击 **`install_task.bat`** 即可（无需管理员权限）。

它会创建名为 **arXiv每日论文** 的计划任务，每天 09:00 运行。

- 查看/修改：开始菜单搜索「任务计划程序」→ 任务计划程序库
- 删除：`schtasks /Delete /TN "arXiv每日论文" /F`
- 立即测试任务：`schtasks /Run /TN "arXiv每日论文"`

> 默认任务只在**用户已登录**且电脑开机时运行。笔记本合盖/关机当天不会发送，
> 下次开机后若勾选了「错过计划后尽快启动」（脚本已默认开启）会补发一次。
> 若需要关机也运行，需在任务属性里勾选「不管用户是否登录都要运行」并保存密码。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `python arxiv_daily.py` | 正常抓取并发送 |
| `python arxiv_daily.py --dry-run` | 只生成预览，不发邮件 |
| `python arxiv_daily.py --test-email` | 发送测试邮件 |
| `python arxiv_daily.py --limit 20` | 本次只发 20 篇 |
| `python arxiv_daily.py --category cs.AI cs.LG` | 临时换分类 |
| `python arxiv_daily.py --force` | 没有新论文时也重发最新 N 篇 |

## 五、云端运行（GitHub Actions，不需要开电脑）

本地计划任务依赖「电脑开机 + 用户登录」。如果希望彻底不依赖电脑，把项目放到
GitHub 私有仓库，用 Actions 的定时任务每天在云端运行。

### 1. 创建私有仓库并推送

```bash
git init
git add .
git commit -m "init arxiv daily digest"
git branch -M main
git remote add origin https://github.com/<你的用户名>/arxiv-daily-digest.git
git push -u origin main
```

`config.ini` 已被 `.gitignore` 排除，授权码不会进入仓库。云端凭证通过 Secrets 注入。

### 2. 配置 Secrets

仓库页面 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，
添加三项：

| Name | Value |
| --- | --- |
| `ARXIV_SMTP_USER` | `xyfhw2017@126.com` |
| `ARXIV_SMTP_AUTH_CODE` | 126 邮箱授权码 |
| `ARXIV_MAIL_TO` | 收件邮箱 |

### 3. 时间与手动触发

工作流文件：`.github/workflows/daily.yml`

- 定时：北京时间 **09:05 / 09:45 / 10:25** 三次兜底（GitHub 偶尔会漏触发 cron）
  脚本内置幂等保护：同一天只有第一次成功运行会发信，后续运行自动跳过
- 手动：仓库 → **Actions** → **arXiv Daily Digest** → **Run workflow**
  - `send`：正常抓取并发送
  - `test-email`：只发测试邮件
  - `dry-run`：只抓取，不发信

> 云端使用 `ARXIV_SOURCE=html`：先读 arXiv 列表页拿到当天论文 ID（窗口 250 条），
> 再逐篇抓摘要页。原因是 arXiv API 会对 GitHub Actions 的共享出口 IP 返回 429 限流，
> 而主站网页不受影响。

### 4. 已读记录

GitHub 的 runner 每次都是全新环境，所以工作流会在发送成功后将
`state/seen.json` 自动 commit 回仓库，保证跨天去重。

> 云端启用后，本机计划任务已被停用（避免同一天收到两封）。
> 如需恢复：`schtasks /Change /TN "arXiv每日论文" /ENABLE`

## 常见问题

**1. 报 `535 Authentication failed`**
授权码错误或 SMTP 服务未开启。重新在 126 设置里生成授权密码，注意不要填登录密码。

**2. 邮件进了垃圾箱**
把发件地址加入通讯录/白名单。126 自建邮件发给自己通常没问题。

**3. 某天没收到**
看 `logs/arxiv_daily.log` 和 `logs/bat.log`。周末、arXiv 服务器维护时可能没有新论文，
`on_no_new = skip` 时不会发送。想每天都收到就改成 `resend`。

**4. 想换发送时间**
重新运行 `install_task.bat` 前，先编辑 `install_task.ps1` 里的 `$time = "09:00"`。

**5. 摘要太长/篇数太多**
改 `config.ini` 里 `max_papers`。邮件体积约 50 篇 100 KB 左右，主流邮箱都能接收。
