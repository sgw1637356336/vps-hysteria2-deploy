# 完整工作流

以下步骤 `hy2.py deploy` 已封装；这里写清楚每一步在做什么、判据是什么，便于排障和手改。

## 0. 前置

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
export VPS_SSH_PASSWORD='...'
```

## 1. plan（离线）

校验：域名格式、端口范围、`hop_range` 合法且不包含 `hy2_port`、面板端口不撞 80/2096/443。
失败返回 2，先把配置改对再连服务器。

## 2. doctor（只读）

远端采样：`id -u`、`/etc/os-release`、`uname -m`、内存/磁盘、`ip -4 addr`、
`ss -lntup`、`x-ui -v`、证书到期、`nft list chain ip nat PREROUTING`、`getent hosts <domain>`。

判据：

- 必须 root（否则后续 apt/systemd 全失败）；
- `getent hosts` 的第一个地址要等于本机公网 IPv4，否则 http-01 必然失败；
- 若 `udp/443` 已被非 xray 进程占用，停下报告，不要抢端口。

## 3. deploy

### 3.1 基础包

`apt-get update` + `curl socat openssl nftables cron ca-certificates jq sqlite3`；
时区设 `Asia/Shanghai`（仅影响日志，可跳过）。

### 3.2 安装 3X-UI（固定版本）

```bash
XUI_NONINTERACTIVE=1 bash <(curl -Ls .../install.sh) 3.10.2
cat /etc/x-ui/install-result.env
```

产出 `XUI_USERNAME / XUI_PASSWORD / XUI_PANEL_PORT / XUI_WEB_BASE_PATH / XUI_API_TOKEN`。
已安装则跳过，复用现有凭据。

### 3.3 证书

- 已有证书且 `-checkend 1209600` 通过 → 保留。
- 否则 `acme.sh --issue --standalone --keylength ec-256 --server letsencrypt`（临时占用 TCP/80）。
- 失败且 `tls.mode=auto` → 自签 P-256，SAN=域名，10 年；链接加 `insecure=1`。
- 安装到 `/etc/ssl/hy2/{fullchain,privkey}.pem`，`--reloadcmd "systemctl restart x-ui"`。

判据：`openssl x509 -noout -subject -enddate` 输出 CN 为域名；`openssl verify` 链 OK；公私钥匹配。

### 3.4 面板与订阅启用 TLS

```sql
insert or replace into settings(key,value) values('webCertFile','/etc/ssl/hy2/fullchain.pem');
insert or replace into settings(key,value) values('webKeyFile','/etc/ssl/hy2/privkey.pem');
insert or replace into settings(key,value) values('subCertFile','/etc/ssl/hy2/fullchain.pem');
insert or replace into settings(key,value) values('subKeyFile','/etc/ssl/hy2/privkey.pem');
```

`systemctl restart x-ui` 后 `curl -k https://127.0.0.1:<port>/<basePath>/` 必须 200。

### 3.5 建 Hysteria2 入站

`POST /panel/api/inbounds/add`，Bearer = `XUI_API_TOKEN`。
关键字段（完整 JSON 见 `scripts/hy2.py: inbound_payload`）：

```json
{
  "protocol": "hysteria",
  "port": 443,
  "streamSettings": {
    "network": "hysteria",
    "security": "tls",
    "tlsSettings": {"serverName": "<domain>", "minVersion": "1.3", "maxVersion": "1.3", "alpn": ["h3"]},
    "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60},
    "finalmask": {
      "udp": [{"type": "salamander", "settings": {"password": "<obfs>"}}],
      "quicParams": {"congestion": "bbr", "udpHop": {"ports": "20000-20100", "interval": "5-10"}}
    }
  }
}
```

`settings.clients[0].auth` 就是客户端密码。重复执行时按 `remark` 找已有入站，
只在 `udpHop.ports` 漂移或出现 `packetSize` 时更新，不重建。

### 3.6 端口跳跃持久化

```
/usr/local/bin/hy2-port-hop.sh                 # 幂等：flush 后 add，v4+v6 各一条
/etc/systemd/system/hy2-port-hop.service       # After=network-online.target, RemainAfterExit=yes
systemctl enable --now hy2-port-hop.service
```

### 3.7 等监听 + 自测

`ss -lunp | grep ':443 '` 出现后，下载官方客户端到 `/root/.hy2-selftest/`，
连 `127.0.0.1:443` + Salamander + SNI 域名，起 SOCKS5，取 `cdn-cgi/trace` 的 `ip=`。
拿到才继续；拿不到返回 4，不要生成"成功"结论。

### 3.8 导出

从面板订阅接口取权威链接（`--resolve` 固定 Host/SNI），base64 解码，
写 `customer/v2rayn.txt`，生成 `customer/qr.png`；有 zxing-cpp 就解码回验。

## 4. verify / export

- `verify`：`systemctl is-active x-ui`、`ss`、`nft`、证书到期、面板 200，再跑一次 3.7 的自测。
- `export`：只重新取链接 + 二维码，不改服务器；报告里注明"未新做网络测试"。

## 5. 交付检查表

- [ ] 面板 URL（https + 域名 + 随机路径）能打开
- [ ] 面板用户名/密码可用（浏览器登录，不用 curl 判）
- [ ] `hysteria2://` 链接含 `obfs=salamander`、`obfs-password`、`mport`、`sni`
- [ ] 二维码解码结果与链接完全一致
- [ ] 自测 PASS
- [ ] 重启后复验 PASS（至少做过一次）
- [ ] 云安全组放行 UDP 443 + 跳跃范围（告知用户）
- [ ] 未把 `state.json`、链接、二维码提交到公开仓库
