# 输入与操作

## 需要向用户收集

| 项 | 必填 | 说明 |
|---|---|---|
| SSH 主机 / 端口 / 用户 | 是 | 默认 22 / root |
| SSH 密码或私钥 | 是 | 密码走环境变量，私钥给路径 |
| 域名 | 是 | 必须已解析到该机公网 IP，否则证书签发必失败 |
| 面板端口 | 否 | 0 表示用安装器随机生成的端口 |
| 跳跃范围 | 否 | 默认 `20000-20100`；云安全组要同时放行 |
| 客户端名 | 否 | 默认 `v2rayn`，只影响备注与 subId |

## 配置文件

复制 `references/config.example.json` 到仓库外私有目录，例如 `~/.config/hy2-node/config.json`，然后：

```bash
export VPS_SSH_PASSWORD='...'
python SKILL_ROOT/scripts/hy2.py plan --config ~/.config/hy2-node/config.json
```

`runtime/` 目录（默认与 config 同级）保存：

- `state.json`：面板端口、auth、obfs 密码、证书模式、最近一次链接。**含凭据，勿提交**。
- `customer/v2rayn.txt`、`customer/qr.png`、`customer/README.txt`：交付物。

## 凭据处理

- 密码只从 `password_env` 指定的环境变量读取；`hy2.py` 不会把密码写进任何文件。
- 生成的 auth / obfs / 面板密码保存在 `runtime/state.json`（0600），用于幂等重跑与离线导出。
- 交付时只把 `customer/` 下的内容发给用户；`state.json` 留在本机。

## 常见环境前提

- 云安全组放行：TCP 22、TCP 80（签发证书时临时用）、TCP 面板端口、TCP 2096（订阅）、**UDP 443 与 UDP 跳跃范围**。
- 80 端口被安全组挡死时，`tls.mode=auto` 会回退自签证书，链接自动加 `insecure=1`。
- 1 核 512MB 也能跑，但首次 `apt-get` 与安装 3X-UI 会慢一些。
