---
name: vps-hysteria2-deploy
description: 用 SSH 登录资料自动部署 3X-UI + Hysteria2（UDP/443、Salamander 混淆、端口跳跃），签发证书、生成 v2rayN 链接/订阅/二维码，并用真实 Hysteria2 客户端做端到端验收。适用于 Debian/Ubuntu VPS 新机部署，以及本 Skill 已管理实例的复验与重新导出。
---

# VPS Hysteria2 Deploy

把用户授权管理的 VPS 配置成可交付的 Hysteria2 节点。**不能**把"安装结束""端口在监听""二维码生成成功"当成代理可用；必须用真实客户端跑通一次代理请求。

## 运行准备

- 定位本文件所在目录为 `SKILL_ROOT`，不要硬编码作者机器路径。
- 本机需要 Python 3.11+、网络与 SSH 出站权限。建独立虚拟环境并安装 `SKILL_ROOT/requirements.txt`，之后一律用该环境的 Python。
- 远端第一版适配 Debian 11+ / Ubuntu 20.04+、systemd、amd64/arm64、公网 IPv4。其他系统先说明适配边界再动手。
- 先读 [操作与输入](references/operations.md)，踩坑与判据读 [坑位清单](references/pitfalls.md)，逐步细节读 [完整工作流](references/workflow.md)。
- 全部动作走 `scripts/hy2.py` 封装的 SSH/固定版本/回读/验收，不依赖浏览器面板坐标。

## 接收资料

需要：SSH 主机、端口、用户名、密码或私钥；域名（必须已解析到该机）；可选面板端口、跳跃范围、客户端名。

已有资料不重复询问。密码只通过当前进程环境变量提供（`password_env`），**不写进 JSON、命令行参数、公开文件或日志**。真实配置、状态、链接、二维码、日志全部放在仓库外私有 `runtime/`。

## 执行

1. `plan`：离线校验域名、端口、跳跃范围、面板端口冲突。
2. `doctor`：只读检查 root、发行版、内存/磁盘、DNS 是否指向本机、443/80 占用、已有安装。DNS 不指向本机就停下——证书签发必然失败。
3. `deploy`：固定版本安装 3X-UI → 签发证书（http-01，失败回退自签）→ 面板与订阅启用 TLS → 建 Hysteria2 入站（Salamander + 跳跃声明）→ 端口跳跃持久化 → 等 `udp/443` 监听 → **真实客户端自测** → 导出交付物。网络没通返回 4，可以出材料，但不许说全部成功。
4. `verify`：回读现状并重跑自测，用于交付后复验或排障。
5. `export`：从面板订阅接口重新导出链接与二维码，说明本次没新做网络测试。

调用形式（替换为实际路径）：

    PYTHON SKILL_ROOT/scripts/hy2.py plan   --config PRIVATE_INPUT
    PYTHON SKILL_ROOT/scripts/hy2.py doctor --config PRIVATE_INPUT
    PYTHON SKILL_ROOT/scripts/hy2.py deploy --config PRIVATE_INPUT
    PYTHON SKILL_ROOT/scripts/hy2.py verify --config PRIVATE_INPUT
    PYTHON SKILL_ROOT/scripts/hy2.py export --config PRIVATE_INPUT

PowerShell 用 `&` 调带空格的路径，Bash 正确加引号。示例 IP/域名必须替换，不能对示例直接执行。

## 硬性技术约束

- **Salamander 只能带 password，绝不能带 `packetSize`。** 3X-UI 表单默认填的 `packetSize: "100-200"` 是 Xray 的 Gecko 扩展，标准 Hysteria2 客户端握手时会被服务端静默丢弃，客户端只报 `timeout: no recent network activity`。这是本 Skill 最关键的一条，`hy2.py` 已在建站时强制剔除。
- 入站 `protocol` 字段是 `hysteria`（不是 `hysteria2`），`streamSettings.network` 也是 `hysteria`，`hysteriaSettings.version=2`。
- 端口跳跃是"客户端 mport + 服务端 UDP 重定向"两件事：服务端 `nft` 把 `hop_range` REDIRECT 到 `hy2_port`（v4/v6 都要），链接里带 `mport=hop_range`。两边范围必须完全一致。
- 服务端 nft 规则默认不持久，必须用 systemd oneshot 单元固化；否则重启后跳跃失效，而 443 直连仍然正常，很容易误判"没问题"。
- 面板 API 用 `/etc/x-ui/install-result.env` 里的 `XUI_API_TOKEN` 走 Bearer；**不要**用 curl 直接 POST `/login` 验证密码，v3 会对非浏览器请求返回 403（缺 `Sec-Fetch-*`），那不是密码错。
- 订阅接口按请求的 Host 生成链接：从公网取才是域名，本机 `127.0.0.1` 取会得到 `localhost`。用 `curl --resolve domain:2096:127.0.0.1` 固定 SNI/Host。

## 验收标准

自测必须由 `hy2.py` 里的官方 Hysteria2 客户端完成：本机下载客户端 → 连 `127.0.0.1:hy2_port` + Salamander → 起 SOCKS5 → `curl https://www.cloudflare.com/cdn-cgi/trace` 拿到 `ip=`。只有这一步通过，才能说节点可用。

额外抽查（有外网条件时）：直连某个跳跃端口（如 `20050`）也应握手成功，证明 nft 重定向与云安全组放行。

## 交付

`runtime/customer/` 下三个文件必须明确交给用户：

- `v2rayn.txt`：完整 `hysteria2://` 链接（优先用面板订阅接口的原始输出，参数最全）。
- `qr.png`：同一链接的二维码，能解码就用 zxing-cpp 回验，`qr_verify` 写明 match / not-checked。
- `README.txt`：导入说明与两行关键参数。

面板地址、用户名、密码、订阅地址一并给出；证书到期时间、跳跃范围、重启后是否复验要写清楚。

链接、二维码、订阅 token 都是凭据：不提交 GitHub、不上传公共二维码服务、不贴群。

## 恢复

- 部署中断：先 `doctor` 看远端实际状态，再决定续跑；`deploy` 本身幂等，可重复执行。
- 只管理"新装"或"同 remark 的已管理入站"。发现无状态面板、端口被别的服务占用、凭据漂移时停止并报告，不覆盖。
- 证书失败先查 80 端口与 DNS；不要用"重装"掩盖。
- 回滚只回滚明确快照（数据库备份 `/etc/x-ui/x-ui.db.bak.*`），不整机回滚，不撤销已加的防火墙与系统包。
