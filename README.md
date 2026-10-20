# vps-hysteria2-deploy

一个可复用的 **Agent Skill / CLI**：把一台全新的 Debian/Ubuntu VPS 变成可交付的
**3X-UI + Hysteria2（UDP 443 + Salamander 混淆 + 端口跳跃）** 节点，并强制用真实客户端验收。

```
plan → doctor → deploy → verify → export
```

## 它解决什么

手工搭 Hysteria2 最容易翻车的不是装不上，而是"装上了但连不上"：

- 3X-UI 给 Salamander 预填的 `packetSize` 会让**所有标准客户端**握手超时（服务端静默丢包）；
- 端口跳跃要同时改 nft 重定向、入站 `udpHop`、客户端 `mport` 三处；
- 手工 nft 规则重启即失效，443 直连却照常，极易误判；
- 面板 API 用 curl 登录会 403（缺 `Sec-Fetch-*`），不是密码错。

这些坑都写进了 `references/pitfalls.md`，脚本里也做了硬约束。

## 目录

```
SKILL.md                     Agent 使用说明（skill 入口）
agents/openai.yaml           展示名与默认提示
scripts/hy2.py               全部实现（单文件，无隐藏依赖）
references/workflow.md       逐步命令与判据
references/pitfalls.md       踩坑清单
references/operations.md     输入、配置、凭据处理
references/config.example.json
requirements.txt
```

## 安装

```bash
git clone https://github.com/<you>/vps-hysteria2-deploy.git
cd vps-hysteria2-deploy
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
```

作为 Agent Skill 使用时，把仓库放到 skills 目录（例如 `~/.agents/skills/vps-hysteria2-deploy`），
`SKILL.md` 的 frontmatter 会被自动识别。

## 用法

```bash
cp references/config.example.json ~/.config/hy2-nodes/node1.json
$EDITOR ~/.config/hy2-nodes/node1.json           # 填 host / domain

export VPS_SSH_PASSWORD='...'                    # 密码只走环境变量

python scripts/hy2.py plan   --config ~/.config/hy2-nodes/node1.json
python scripts/hy2.py doctor --config ~/.config/hy2-nodes/node1.json
python scripts/hy2.py deploy --config ~/.config/hy2-nodes/node1.json
python scripts/hy2.py verify --config ~/.config/hy2-nodes/node1.json
python scripts/hy2.py export --config ~/.config/hy2-nodes/node1.json
```

退出码：`0` 成功；`2` 前置条件不满足；`4` 部署完成但网络验收未通过（材料仍会生成）。

## 配置文件

```json
{
  "ssh": { "host": "203.0.113.10", "port": 22, "username": "root", "password_env": "VPS_SSH_PASSWORD" },
  "domain": "node.example.com",
  "server": { "hy2_port": 443, "hop_range": "20000-20100", "panel_port": 0 },
  "client": { "name": "v2rayn" },
  "tls": { "mode": "auto", "email": "admin@example.com" },
  "self_test": true,
  "runtime": "./runtime"
}
```

- `panel_port: 0` 用安装器随机端口；
- `tls.mode`: `auto | acme | selfsigned`，`auto` 在 http-01 失败时回退自签；
- `runtime/` 保存 `state.json`（含凭据）与 `customer/` 交付物，**不要提交**。

## 产出

```
runtime/
├── state.json              面板端口、auth、obfs、cert_mode、链接（0600，勿公开）
└── customer/
    ├── v2rayn.txt          完整 hysteria2:// 链接
    ├── qr.png              同链接二维码
    └── README.txt          导入说明 + 二维码校验结果
```

## 远端会改什么

| 项 | 位置 |
|---|---|
| 3X-UI | `/usr/local/x-ui`，`x-ui.service`（开机自启） |
| 证书 | `/etc/ssl/hy2/{fullchain,privkey}.pem`，acme.sh 每 6 小时检查续期 |
| 面板/订阅 TLS | `x-ui.db` 的 `webCertFile`/`webKeyFile`/`subCertFile`/`subKeyFile` |
| 端口跳跃 | `/usr/local/bin/hy2-port-hop.sh` + `hy2-port-hop.service`（IPv4+IPv6，持久） |
| 数据库备份 | `/etc/x-ui/x-ui.db.bak.*` |

不改 SSH 配置、不改系统自带的 BBR/防火墙策略。

## 排障

1. 连不上先跑 `verify`；失败时看 `journalctl -u x-ui -n 50`。
2. 客户端报 `timeout: no recent network activity` → 99% 是 `packetSize` 又回来了，
   或客户端/服务端 obfs 密码不一致。
3. 客户端跳到某端口不通 → 检查 `nft list chain ip nat PREROUTING` 与云安全组 UDP 范围。
4. 面板打不开 → 只用 `https://域名:端口/路径/`，IP 打开必然证书错误。

## 免责声明

仅供个人合规使用与网络技术学习。请遵守所在地法律法规及云服务商条款，勿用于非法用途。

## License

MIT
