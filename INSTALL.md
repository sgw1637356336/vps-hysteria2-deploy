# 安装指南（给别人用的版本）

这个仓库有两种用法：**当 Agent 技能**（推荐）或**当命令行工具**。两种都只需要
Python 3.11+ 和一个能 SSH 出站的网络。

---

## 一、当 Agent 技能用（推荐）

技能的本质就是一个带 `SKILL.md` 的文件夹。把它放进你所用的 Agent 的 skills 目录，
重启会话后 Agent 就能自己读到。

### 1. 下载

```bash
git clone https://github.com/sgw1637356336/vps-hysteria2-deploy.git
```

没有 git 就下 ZIP 解压：

- 浏览器打开仓库 → 绿色 `Code` → `Download ZIP`
- 或直接下载：<https://codeload.github.com/sgw1637356336/vps-hysteria2-deploy/zip/refs/heads/main>

### 2. 放进 skills 目录

| Agent | 个人级目录 | 项目级目录 |
|---|---|---|
| DeepSeek Harness / 通用 `.agents` 约定 | `~/.agents/skills/vps-hysteria2-deploy` | 项目下 `.agents/skills/…` |
| Claude Code | `~/.claude/skills/vps-hysteria2-deploy` | 项目下 `.claude/skills/…` |

Windows 上 `~` 就是 `C:\Users\<你的用户名>`，例如：

```
C:\Users\Alice\.agents\skills\vps-hysteria2-deploy\SKILL.md
```

**macOS / Linux：**

```bash
mkdir -p ~/.agents/skills
cp -r vps-hysteria2-deploy ~/.agents/skills/vps-hysteria2-deploy
```

**Windows PowerShell：**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.agents\skills" | Out-Null
Copy-Item -Recurse .\vps-hysteria2-deploy "$env:USERPROFILE\.agents\skills\vps-hysteria2-deploy"
```

### 3. 装依赖（可选但建议）

技能里会让 Agent 自己建虚拟环境；你也可以先装好，省得每次等：

```bash
cd ~/.agents/skills/vps-hysteria2-deploy
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
```

### 4. 验证

```bash
python scripts/hy2.py plan --config references/config.example.json
# 期望输出：plan OK
```

然后重启 Agent 会话，问一句"你有哪些技能"，应该能看到 `vps-hysteria2-deploy`。

### 5. 用起来

直接对 Agent 说：

> 用 vps-hysteria2-deploy 技能，VPS 是 `1.2.3.4`，root 密码 `xxx`，域名 `node.example.com`，
> 要 Salamander 混淆 + 端口跳跃，客户端是 v2rayN

它会走 `plan → doctor → deploy`，最后给你面板地址和 `hysteria2://` 链接 + 二维码。

---

## 二、当命令行工具用（不需要 Agent）

```bash
git clone https://github.com/sgw1637356336/vps-hysteria2-deploy.git
cd vps-hysteria2-deploy
python -m venv .venv && .venv/bin/pip install -r requirements.txt

cp references/config.example.json ~/node1.json
# 编辑 node1.json：ssh.host / domain / hop_range
export VPS_SSH_PASSWORD='你的root密码'

.venv/bin/python scripts/hy2.py plan   --config ~/node1.json
.venv/bin/python scripts/hy2.py doctor --config ~/node1.json
.venv/bin/python scripts/hy2.py deploy --config ~/node1.json
```

Windows 把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`，环境变量用
`$env:VPS_SSH_PASSWORD='...'`。

产物在 `runtime/customer/`：`v2rayn.txt`、`qr.png`、`README.txt`。

---

## 三、需要准备什么

| 项 | 说明 |
|---|---|
| VPS | Debian 11+ / Ubuntu 20.04+，amd64 或 arm64，有公网 IPv4 |
| SSH | 主机、端口、root 密码或私钥 |
| 域名 | 必须已解析到该 VPS 的公网 IP（`doctor` 会校验） |
| 云安全组 | 放行 TCP 22、TCP 80（签证书用）、TCP 2096、面板端口，**UDP 443 + 跳跃范围** |

没有域名也能用，但证书只能自签，客户端要勾"跳过证书验证"；`tls.mode` 设 `selfsigned` 即可。

---

## 四、常见问题

**`python` 不是 3.11+**
先 `python --version`；macOS/Linux 用 `python3`，Windows 去 python.org 装并勾选 Add to PATH。

**pip 装得慢**
换镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

**PowerShell 提示"禁止运行脚本"（激活 venv 时）**
不用激活，直接调用 `.venv\Scripts\python.exe scripts\hy2.py ...` 就行。

**`doctor` 报域名没指向本机**
去 DNS 改 A 记录，等生效；证书签发依赖这一步，不要跳过。

**客户端连上但打不开网页**
先看 `verify` 的自测是否 PASS；再看服务端 `journalctl -u x-ui -n 50`。
最常见原因是混淆密码不一致，或跳跃范围只改了服务端没改链接。

**会不会把我的密码传到仓库**
不会。密码只从环境变量读，链接和密码存在本机 `runtime/state.json`（已在 `.gitignore` 里）。
但你自己别把 `config.json`、`runtime/` 提交到公开仓库。

---

## 五、卸载

删掉 skills 目录里那个文件夹即可，VPS 上的 3X-UI/证书/防火墙规则不会自动清理
（需要的话 SSH 上去 `x-ui uninstall`，再删 `/etc/systemd/system/hy2-port-hop.service`）。

---

License: MIT
