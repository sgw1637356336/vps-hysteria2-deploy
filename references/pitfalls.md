# 坑位清单

按踩到的顺序记录，每条都给出判据，避免"看起来像成功"。

## 1. Salamander 带 packetSize → 全网客户端握手失败

3X-UI 入站表单给 Salamander 预填 `packetSize: "100-200"`。这是 Xray 侧 Gecko 扩展，
标准 Hysteria2（sing-box / mihomo / 官方客户端 / v2rayN）不实现它。

**现象**：`udp/443` 在监听，`tcpdump` 能看到客户端发出的 1258 字节 QUIC 包，服务端**一个字节都不回**；
客户端 5 秒后报 `connect error: timeout: no recent network activity`。

**判据**：入站 `finalmask.udp[0].settings` 里只有 `password`，没有 `packetSize`。
去掉后官方客户端立即能连上。

## 2. 协议名是 `hysteria`，不是 `hysteria2`

入站 `protocol` 与 `streamSettings.network` 都写 `hysteria`；
`hysteriaSettings.version` 固定 2。面板链接仍然输出 `hysteria2://`。

## 3. 跳跃范围必须三处一致

1. 服务端 nft：`udp dport <range> redirect to :<hy2_port>`（IPv4 + IPv6）；
2. 入站 `finalmask.quicParams.udpHop.ports`（决定链接里的 `mport`）；
3. 客户端链接里的 `mport`。

只改一处 → 客户端跳到没被重定向的端口，包被丢弃；443 直连仍然正常，极易误判。

## 4. nft 规则默认不持久

手工 `nft add rule` 重启即失效。必须落成 `/usr/local/bin/hy2-port-hop.sh` + systemd oneshot
（`After=network-online.target`），并在脚本里做成幂等（先 `flush chain` 再 `add`），
否则重复执行会叠加出多条重复规则。

**判据**：`nft list chain ip nat PREROUTING` 里每个协议族只有一条 `udp dport` 规则。

## 5. 面板 API 不能用 curl 登录

v3 对缺少 `Sec-Fetch-*` 的 `POST /login` 返回 **403**，这不代表密码错误。
正确做法：用 `/etc/x-ui/install-result.env` 里的 `XUI_API_TOKEN` 发
`Authorization: Bearer ...`，或直接从面板 UI 登录。

**取 403 时别改密码**，否则会真的把用户锁在外面。

## 6. 订阅链接里的主机名取决于请求 Host

从 `127.0.0.1` 取订阅会得到 `@localhost:443`；必须带正确 Host/SNI 才输出域名。
服务端自检用：

```bash
curl -sk --resolve node.example.com:2096:127.0.0.1 -A 'v2rayN/6.45' \
  https://node.example.com:2096/<subPath>/<subId>
```

返回体是 base64，decode 后才是 `hysteria2://...`。

## 7. 面板启用 HTTPS 后只能用域名访问

证书只签了域名，用 IP 打开必然握手失败。启用方式：`settings` 表写入
`webCertFile` / `webKeyFile`（订阅另加 `subCertFile` / `subKeyFile`），然后重启 `x-ui`。

改完先在本机 `curl -k https://127.0.0.1:<port>/<basePath>/` 确认 200，再告诉用户。

## 8. 证书续期会重启面板

`acme.sh --install-cert --reloadcmd "systemctl restart x-ui"` 会在续期时短暂断流。
可接受，但要写进交付说明，别让用户以为节点挂了。

## 9. 自签证书必须让客户端跳过校验

自签模式下链接要带 `insecure=1`，否则 v2rayN 报证书错误。`hy2.py export` 会按
`state.json` 里的 `cert_mode` 自动加。

## 10. 验收不能只看端口

`ss -lunp` 有 `:443`、`systemctl is-active x-ui` 是 active，都不能证明协议可用
（第 1 条就是典型反例）。唯一可信判据：官方 Hysteria2 客户端经 SOCKS5 取到
`https://www.cloudflare.com/cdn-cgi/trace` 的 `ip=`。
