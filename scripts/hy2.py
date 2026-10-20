#!/usr/bin/env python3
"""hy2.py — 3X-UI + Hysteria2 (UDP/443 + Salamander + port hopping) deployer.

Subcommands:
    plan    offline validation of the config and the plan
    doctor  read-only remote inspection (OS, ports, existing install, DNS)
    deploy  idempotent deployment + server-side self test
    verify  read back the current state and re-run the self test
    export  regenerate the share link / QR from the live server

The single non-obvious rule enforced everywhere: the Salamander obfs mask must
carry ONLY a password.  Xray's "packetSize" extension breaks every standard
Hysteria2 client at the QUIC handshake (silent drop, client reports
"timeout: no recent network activity").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.stderr.write("paramiko is required: pip install -r requirements.txt\n")
    raise

PANEL_VERSION = "3.10.2"
INSTALL_URL = "https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh"
CERT_DIR = "/etc/ssl/hy2"
CERT_CRT = f"{CERT_DIR}/fullchain.pem"
CERT_KEY = f"{CERT_DIR}/privkey.pem"
HOP_HELPER = "/usr/local/bin/hy2-port-hop.sh"
HOP_UNIT_FILE = "/etc/systemd/system/hy2-port-hop.service"
HOP_UNIT = "hy2-port-hop.service"
SELFTEST_BIN = "/root/.hy2-selftest/hysteria"
DEFAULT_REMARK_FMT = "hy2-{port}-salamander"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
@dataclass
class Cfg:
    host: str
    domain: str
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None
    password_env: str | None = None
    ssh_key: str | None = None
    hy2_port: int = 443
    hop_range: str = "20000-20100"
    panel_port: int = 0  # 0 = keep whatever the installer generated
    panel_user: str | None = None
    panel_pass: str | None = None
    client_name: str = "v2rayn"
    auth: str | None = None
    obfs_password: str | None = None
    tls_mode: str = "auto"  # auto | acme | selfsigned
    email: str = ""
    runtime: Path = Path("runtime")
    self_test: bool = True
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def remark(self) -> str:
        return DEFAULT_REMARK_FMT.format(port=self.hy2_port)

    @property
    def state_file(self) -> Path:
        return self.runtime / "state.json"


def load_config(path: str) -> Cfg:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config is not valid JSON ({path}): {exc}")
    ssh = raw.get("ssh", {})
    srv = raw.get("server", {})
    cli = raw.get("client", {})
    tls = raw.get("tls", {})

    password_env = ssh.get("password_env") or None
    password = os.environ.get(password_env) if password_env else None
    if not password:
        password = ssh.get("password") or None

    config_dir = Path(path).resolve().parent
    runtime = Path(raw.get("runtime") or "runtime")
    if not runtime.is_absolute():
        runtime = (config_dir / runtime).resolve()
    cfg = Cfg(
        host=ssh.get("host", ""),
        domain=raw.get("domain", ""),
        ssh_port=int(ssh.get("port", 22)),
        ssh_user=ssh.get("username", "root"),
        ssh_password=password,
        password_env=password_env,
        ssh_key=ssh.get("key_file"),
        hy2_port=int(srv.get("hy2_port", 443)),
        hop_range=str(srv.get("hop_range", "20000-20100")),
        panel_port=int(srv.get("panel_port", 0)),
        panel_user=raw.get("panel", {}).get("username"),
        panel_pass=raw.get("panel", {}).get("password"),
        client_name=cli.get("name", "v2rayn"),
        auth=cli.get("auth"),
        obfs_password=cli.get("obfs_password"),
        tls_mode=tls.get("mode", "auto"),
        email=tls.get("email", ""),
        runtime=runtime,
        self_test=bool(raw.get("self_test", True)),
    )
    if not cfg.host or not cfg.domain:
        raise SystemExit("config needs ssh.host and domain")
    cfg.runtime.mkdir(parents=True, exist_ok=True)
    if cfg.state_file.exists():
        cfg.state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
    return cfg


def save_state(cfg: Cfg) -> None:
    cfg.state_file.write_text(
        json.dumps(cfg.state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def gen_token(nbytes: int = 16) -> str:
    return secrets.token_hex(nbytes)


# --------------------------------------------------------------------------- #
# remote
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def tail(self, n: int = 12) -> str:
        lines = (self.out + "\n" + self.err).strip().splitlines()
        return "\n".join(lines[-n:])


class Remote:
    def __init__(self, cfg: Cfg, timeout: int = 900):
        self.cfg = cfg
        self.timeout = timeout
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> "Remote":
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = dict(
            hostname=self.cfg.host,
            port=self.cfg.ssh_port,
            username=self.cfg.ssh_user,
            timeout=25,
            banner_timeout=40,
            auth_timeout=40,
            allow_agent=False,
            look_for_keys=False,
        )
        if not self.cfg.ssh_password and not self.cfg.ssh_key:
            raise SystemExit(
                f"no SSH credential: set environment variable {self.cfg.password_env or 'VPS_SSH_PASSWORD'}"
            )
        if self.cfg.ssh_password:
            kwargs["password"] = self.cfg.ssh_password
        elif self.cfg.ssh_key:
            kwargs["key_filename"] = self.cfg.ssh_key
        client.connect(**kwargs)
        self.client = client
        self.sftp = client.open_sftp()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.sftp:
            self.sftp.close()
        if self.client:
            self.client.close()

    def run(self, script: str, timeout: int | None = None, check: bool = False) -> Result:
        assert self.client is not None
        script = "set -o pipefail\n" + script
        stdin, stdout, stderr = self.client.exec_command(
            "bash -s", timeout=timeout or self.timeout
        )
        stdin.write(script if script.endswith("\n") else script + "\n")
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        res = Result(rc, out, err)
        if check and not res.ok:
            raise SystemExit(f"remote command failed (rc={rc}):\n{res.tail(30)}")
        return res

    def put(self, text: str, remote_path: str, mode: int = 0o600) -> None:
        assert self.sftp is not None
        tmp = f"{remote_path}.tmp{secrets.token_hex(3)}"
        with self.sftp.file(tmp, "w") as fh:
            fh.write(text)
        self.run(f"install -m {mode:o} {shlex.quote(tmp)} {shlex.quote(remote_path)}; rm -f {shlex.quote(tmp)}", check=True)

    def write_script(self, text: str, remote_path: str) -> None:
        self.put(text, remote_path, mode=0o755)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_install_env(remote: Remote) -> dict[str, str]:
    res = remote.run("cat /etc/x-ui/install-result.env 2>/dev/null || true")
    env: dict[str, str] = {}
    for line in res.out.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def panel_base(env: dict[str, str], cfg: Cfg) -> str:
    port = cfg.panel_port or int(env.get("XUI_PANEL_PORT") or 0)
    path = env.get("XUI_WEB_BASE_PATH") or ""
    base = path.strip("/")
    return f"https://127.0.0.1:{port}/{base}".rstrip("/") + "/"


def api(remote: Remote, base: str, token: str, method: str, path: str,
        body: dict | None = None) -> tuple[int, Any]:
    """Call the panel API from the server itself; returns (http_code, parsed)."""
    url = base + path.lstrip("/")
    if body is None:
        script = f"curl -sk -m 30 -o /tmp/hy2_api.out -w '%{{http_code}}\\n' -H 'Authorization: Bearer {token}' {shlex.quote(url)}"
    else:
        remote.put(json.dumps(body, ensure_ascii=False), "/tmp/hy2_api.json", mode=0o600)
        script = (
            f"curl -sk -m 30 -o /tmp/hy2_api.out -w '%{{http_code}}\\n' -X {method} "
            f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
            f"--data-binary @/tmp/hy2_api.json {shlex.quote(url)}"
        )
    res = remote.run(script + "\ncat /tmp/hy2_api.out")
    lines = res.out.splitlines()
    code = 0
    payload = ""
    if lines:
        try:
            code = int(lines[0].strip())
        except ValueError:
            code = 0
        payload = "\n".join(lines[1:]).strip()
    try:
        return code, json.loads(payload) if payload else {}
    except json.JSONDecodeError:
        return code, {"raw": payload}


def extract_link(raw: str) -> str:
    """Subscription bodies are base64 blobs; a single link is returned as is."""
    raw = raw.strip()
    if raw.startswith("hysteria2://"):
        return raw
    try:
        import base64

        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
        for line in decoded.splitlines():
            if line.strip().startswith("hysteria2://"):
                return line.strip()
    except Exception:
        pass
    return raw


def build_link(cfg: Cfg, auth: str, obfs: str, insecure: bool = False) -> str:
    query = [
        "alpn=h3",
        "fp=chrome",
        f"mport={cfg.hop_range}",
        "obfs=salamander",
        f"obfs-password={obfs}",
        "security=tls",
        f"sni={cfg.domain}",
    ]
    if insecure:
        query.insert(0, "insecure=1")
    return (
        f"hysteria2://{auth}@{cfg.domain}:{cfg.hy2_port}/?"
        + "&".join(query)
        + f"#{cfg.remark}-{cfg.client_name}"
    )


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
PORT_RE = re.compile(r"^(\d{1,5})-(\d{1,5})$")


def cmd_plan(cfg: Cfg) -> int:
    problems: list[str] = []
    if not re.match(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", cfg.domain):
        problems.append(f"domain looks invalid: {cfg.domain}")
    if not 1 <= cfg.hy2_port <= 65535:
        problems.append("hy2_port out of range")
    m = PORT_RE.match(cfg.hop_range)
    if not m:
        problems.append("hop_range must look like 20000-20100")
    else:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo >= hi:
            problems.append("hop_range start must be < end")
        if hi > 65535:
            problems.append("hop_range end > 65535")
        if lo <= cfg.hy2_port <= hi:
            problems.append("hop_range must not contain hy2_port")
    if cfg.panel_port in (cfg.hy2_port, 80, 2096):
        problems.append("panel_port collides with a reserved port")

    print(f"target        : {cfg.ssh_user}@{cfg.host}:{cfg.ssh_port}")
    print(f"domain        : {cfg.domain}")
    print(f"hysteria2     : udp/{cfg.hy2_port}  obfs=salamander  mport={cfg.hop_range}")
    print(f"panel         : {'auto' if not cfg.panel_port else cfg.panel_port} (https, random base path)")
    print(f"tls           : {cfg.tls_mode} (acme http-01 needs tcp/80 reachable)")
    print(f"runtime       : {cfg.runtime.resolve()}")
    print(f"self test     : {cfg.self_test}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 2
    print("\nplan OK")
    return 0


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
DOCTOR_PROBE = r"""
echo "ID=$(id -u)"
. /etc/os-release 2>/dev/null && echo "OS=$ID $VERSION_ID"
echo "ARCH=$(uname -m)"
echo "MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)"
echo "DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)"
echo "IP4=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)"
if [ -x /usr/local/x-ui/x-ui ]; then
  echo "XUI=$(/usr/local/x-ui/x-ui -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
else
  echo "XUI=none"
fi
[ -s /etc/ssl/hy2/fullchain.pem ] && echo "CERT_END=$(openssl x509 -in /etc/ssl/hy2/fullchain.pem -noout -enddate 2>/dev/null | cut -d= -f2)" || echo "CERT_END=none"
echo "UDP_PORT=$(ss -lunp 2>/dev/null | grep -c ':'__PORT__' ')"
echo "TCP80=$(ss -lntp 2>/dev/null | grep -c ':80 ')"
echo "DNS=$(getent hosts __DOMAIN__ | awk '{print $1}' | head -1)"
echo "LISTEN<<"
ss -lntup 2>/dev/null | tail -n +2 | head -20
echo ">>"
echo "NFT<<"
nft list chain ip nat PREROUTING 2>/dev/null
echo ">>"
"""


def cmd_doctor(cfg: Cfg) -> int:
    probe = (
        DOCTOR_PROBE.replace("__DOMAIN__", cfg.domain)
        .replace("__PORT__", str(cfg.hy2_port))
    )
    with Remote(cfg) as remote:
        res = remote.run(probe)
        print(res.out.strip())
        print()

        out = res.out
        m = re.search(r"^IP4=(\S+)", out, re.M)
        server_ip = m.group(1) if m else ""
        d = re.search(r"^DNS=(\S+)", out, re.M)
        dns_ip = d.group(1) if d else ""
        udp = re.search(r"^UDP_PORT=(\d+)", out, re.M)
        tcp80 = re.search(r"^TCP80=(\d+)", out, re.M)
        xui = re.search(r"^XUI=(\S+)", out, re.M)

        checks: list[tuple[str, bool, str]] = [
            ("root over SSH", "ID=0" in out, "deploy needs root"),
            ("Debian/Ubuntu + systemd",
             any(k in out for k in ("OS=ubuntu", "OS=debian")), "unsupported distro"),
            ("domain points at this host",
             bool(server_ip) and dns_ip == server_ip,
             f"DNS={dns_ip or 'none'} server={server_ip or 'none'}"),
            ("udp port not taken by something else",
             not (udp and int(udp.group(1)) > 0 and not xui),
             "another process already listens on the hy2 port"),
        ]
        if tcp80 and int(tcp80.group(1)) == 0:
            print("[INFO] tcp/80 is free — Let's Encrypt http-01 can be used")
        for name, ok, hint in checks:
            print(f"[{'OK  ' if ok else 'WARN'}] {name}" + ("" if ok else f"   ({hint})"))
        return 0 if all(c[1] for c in checks) else 2


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #
def ensure_base(remote: Remote) -> None:
    print("== base packages")
    remote.run(
        """
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl socat openssl nftables cron ca-certificates jq sqlite3 >/dev/null 2>&1
timedatectl set-timezone Asia/Shanghai >/dev/null 2>&1 || true
command -v curl && command -v jq && command -v sqlite3
""",
        check=True,
    )


def ensure_panel(remote: Remote) -> dict[str, str]:
    env = load_install_env(remote)
    if env.get("XUI_USERNAME"):
        print(f"== panel already installed (user={env['XUI_USERNAME']}) — skipping installer")
        return env
    print(f"== install 3X-UI {PANEL_VERSION} (non-interactive)")
    res = remote.run(
        f"XUI_NONINTERACTIVE=1 bash <(curl -Ls {INSTALL_URL}) {PANEL_VERSION} 2>&1 | tail -n 15",
        timeout=900,
    )
    if not res.ok:
        raise SystemExit(f"installer failed:\n{res.tail(20)}")
    env = load_install_env(remote)
    if not env.get("XUI_USERNAME"):
        raise SystemExit("installer finished but /etc/x-ui/install-result.env is missing")
    print(f"   panel user={env['XUI_USERNAME']} port={env['XUI_PANEL_PORT']} path=/{env['XUI_WEB_BASE_PATH']}/")
    return env


def ensure_cert(remote: Remote, cfg: Cfg) -> str:
    """Returns the effective tls mode: acme | selfsigned."""
    print("== certificate")
    check = remote.run(
        f"if [ -s {CERT_CRT} ] && [ -s {CERT_KEY} ]; then "
        f"openssl x509 -in {CERT_CRT} -noout -checkend 1209600 >/dev/null 2>&1 && echo VALID || echo EXPIRING; "
        f"else echo MISSING; fi"
    )
    state = check.out.strip().splitlines()[-1] if check.out.strip() else "MISSING"
    if state == "VALID":
        print("   existing certificate is valid for >14 days — keeping it")
        return cfg.state.get("cert_mode", "acme")

    if cfg.tls_mode in ("auto", "acme"):
        print("   issuing Let's Encrypt certificate (http-01 on tcp/80)")
        res = remote.run(
            f"""
set -e
if [ ! -f /root/.acme.sh/acme.sh ]; then
  curl -s https://get.acme.sh | sh -s email={shlex.quote(cfg.email or 'admin@' + cfg.domain)} >/dev/null 2>&1
fi
pkill -f "http.server 80" 2>/dev/null || true
/root/.acme.sh/acme.sh --issue -d {shlex.quote(cfg.domain)} --standalone --keylength ec-256 \
  --server letsencrypt --force >/tmp/acme.log 2>&1 || {{ tail -n 15 /tmp/acme.log; exit 1; }}
mkdir -p {CERT_DIR}
/root/.acme.sh/acme.sh --install-cert -d {shlex.quote(cfg.domain)} --ecc \
  --key-file {CERT_KEY} --fullchain-file {CERT_CRT} \
  --reloadcmd "systemctl restart x-ui" >>/tmp/acme.log 2>&1 || {{ tail -n 15 /tmp/acme.log; exit 1; }}
openssl x509 -in {CERT_CRT} -noout -subject -enddate
""",
            timeout=300,
        )
        if res.ok:
            print("   " + res.out.strip().replace("\n", "\n   "))
            cfg.state["cert_mode"] = "acme"
            return "acme"
        print("   acme failed (port 80 blocked?) — falling back to self-signed")
        if cfg.tls_mode == "acme":
            raise SystemExit("tls.mode=acme but http-01 failed:\n" + res.tail(15))

    print("   generating self-signed certificate")
    remote.run(
        f"""
set -e
mkdir -p {CERT_DIR}
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
  -keyout {CERT_KEY} -out {CERT_CRT} -subj "/CN={cfg.domain}" \
  -addext "subjectAltName=DNS:{cfg.domain}" >/dev/null 2>&1
chmod 600 {CERT_KEY}
""",
        check=True,
    )
    cfg.state["cert_mode"] = "selfsigned"
    return "selfsigned"


def configure_panel_tls(remote: Remote, cfg: Cfg) -> None:
    print("== panel + subscription TLS")
    remote.run(
        f"""
sqlite3 /etc/x-ui/x-ui.db "insert or replace into settings(key,value) values('webCertFile','{CERT_CRT}');"
sqlite3 /etc/x-ui/x-ui.db "insert or replace into settings(key,value) values('webKeyFile','{CERT_KEY}');"
sqlite3 /etc/x-ui/x-ui.db "insert or replace into settings(key,value) values('subCertFile','{CERT_CRT}');"
sqlite3 /etc/x-ui/x-ui.db "insert or replace into settings(key,value) values('subKeyFile','{CERT_KEY}');"
systemctl restart x-ui
""",
        check=True,
    )
    time.sleep(3)


def ensure_hop(remote: Remote, cfg: Cfg) -> None:
    print(f"== port hopping {cfg.hop_range} -> {cfg.hy2_port}")
    helper = f"""#!/usr/bin/env bash
# Redirect UDP {cfg.hop_range} to the Hysteria2 listener on {cfg.hy2_port}.
# Idempotent: flushes the nat PREROUTING chain, then installs one rule per family.
set -u
PORTS="{cfg.hop_range}"
TARGET={cfg.hy2_port}
IFACE="${{HY2_IFACE:-eth0}}"

ensure() {{
  local fam="$1"
  nft list table "$fam" nat >/dev/null 2>&1 || nft add table "$fam" nat
  nft list chain "$fam" nat PREROUTING >/dev/null 2>&1 || \\
    nft add chain "$fam" nat PREROUTING '{{ type nat hook prerouting priority dstnat ; policy accept ; }}'
  nft flush chain "$fam" nat PREROUTING
  nft add rule "$fam" nat PREROUTING iifname "$IFACE" meta l4proto udp udp dport "$PORTS" counter redirect to :"$TARGET"
}}

ensure ip
ensure ip6
"""
    unit = f"""[Unit]
Description=Hysteria2 UDP port hopping redirect ({cfg.hop_range} -> {cfg.hy2_port})
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={HOP_HELPER}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
    remote.write_script(helper, HOP_HELPER)
    remote.put(unit, HOP_UNIT_FILE, mode=0o644)
    remote.run(
        f"systemctl daemon-reload && systemctl enable --now {HOP_UNIT} && systemctl restart {HOP_UNIT} && "
        f"nft list chain ip nat PREROUTING && nft list chain ip6 nat PREROUTING",
        check=True,
    )


def inbound_payload(cfg: Cfg, auth: str, obfs: str) -> dict[str, Any]:
    return {
        "up": 0, "down": 0, "total": 0,
        "remark": cfg.remark,
        "enable": True, "expiryTime": 0,
        "listen": "", "port": cfg.hy2_port,
        "protocol": "hysteria",
        "settings": json.dumps({
            "version": 2,
            "clients": [{
                "auth": auth, "email": f"{cfg.client_name}-hy2", "enable": True,
                "limitIp": 0, "totalGB": 0, "expiryTime": 0, "tgId": 0,
                "subId": f"hy2{cfg.client_name}", "comment": cfg.client_name, "reset": 0,
            }],
        }, ensure_ascii=False),
        "streamSettings": json.dumps({
            "network": "hysteria",
            "security": "tls",
            "tlsSettings": {
                "serverName": cfg.domain,
                "minVersion": "1.3", "maxVersion": "1.3",
                "alpn": ["h3"],
                "certificates": [{
                    "certificateFile": CERT_CRT, "keyFile": CERT_KEY,
                    "oneTimeLoading": False, "usage": "encipherment", "buildChain": False,
                }],
                "settings": {"fingerprint": "chrome"},
            },
            "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60},
            "finalmask": {
                # NOTE: no packetSize — that Xray "Gecko" knob breaks standard
                # Hysteria2 clients (silent QUIC drop). Password only.
                "udp": [{"type": "salamander", "settings": {"password": obfs}}],
                "quicParams": {
                    "congestion": "bbr",
                    "udpHop": {"ports": cfg.hop_range, "interval": "5-10"},
                },
            },
        }, ensure_ascii=False),
        "sniffing": json.dumps({
            "enabled": False, "destOverride": ["http", "tls", "quic", "fakedns"],
            "metadataOnly": False, "routeOnly": False,
        }),
        "allocate": json.dumps({"strategy": "always", "refresh": 5, "concurrency": 3}),
    }


def ensure_inbound(remote: Remote, cfg: Cfg, env: dict[str, str]) -> dict[str, str]:
    print("== hysteria2 inbound")
    base = panel_base(env, cfg)
    token = env.get("XUI_API_TOKEN", "")
    if not token:
        raise SystemExit("XUI_API_TOKEN missing from /etc/x-ui/install-result.env")

    code, data = api(remote, base, token, "GET", "panel/api/inbounds/list")
    inbounds = (data or {}).get("obj") or []
    found = next((i for i in inbounds if i.get("remark") == cfg.remark), None)

    if found:
        settings = found.get("settings") or {}
        stream = found.get("streamSettings") or {}
        client = (settings.get("clients") or [{}])[0]
        auth = client.get("auth") or cfg.auth or gen_token()
        mask = ((stream.get("finalmask") or {}).get("udp") or [{}])[0]
        obfs = (mask.get("settings") or {}).get("password") or cfg.obfs_password or gen_token()
        hop = ((stream.get("finalmask") or {}).get("quicParams") or {}).get("udpHop") or {}
        need_update = (
            hop.get("ports") != cfg.hop_range
            or (mask.get("settings") or {}).get("packetSize") is not None
        )
        if need_update:
            print("   existing inbound drifted from the plan — updating")
            payload = inbound_payload(cfg, auth, obfs)
            payload["id"] = found["id"]
            code, resp = api(remote, base, token, "POST",
                             f"panel/api/inbounds/update/{found['id']}", payload)
            if not str(resp.get("success", "")).lower() == "true":
                raise SystemExit(f"update failed: {resp}")
        else:
            print(f"   inbound id={found['id']} already matches the plan")
        time.sleep(2)
        remote.run("systemctl restart x-ui", check=True)
        time.sleep(3)
        return {"id": str(found["id"]), "auth": auth, "obfs": obfs}

    auth = cfg.auth or gen_token()
    obfs = cfg.obfs_password or gen_token()
    code, resp = api(remote, base, token, "POST", "panel/api/inbounds/add",
                     inbound_payload(cfg, auth, obfs))
    if str(resp.get("success", "")).lower() != "true":
        raise SystemExit(f"inbound creation failed (http {code}): {resp}")
    inbound_id = str(((resp.get("obj") or {}).get("id")) or "")
    print(f"   created inbound id={inbound_id}")
    time.sleep(3)
    return {"id": inbound_id, "auth": auth, "obfs": obfs}


def wait_listening(remote: Remote, cfg: Cfg, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = remote.run(f"ss -lunp 2>/dev/null | grep -c ':{cfg.hy2_port} ' || true")
        if res.out.strip().isdigit() and int(res.out.strip()) > 0:
            return True
        time.sleep(2)
    return False


def selftest(remote: Remote, cfg: Cfg) -> tuple[bool, str]:
    """Download the official hysteria client and actually proxy through the node."""
    print("== self test (official hysteria2 client via loopback)")
    bin_dir = "/root/.hy2-selftest"
    fetch = remote.run(
        f"""
mkdir -p {bin_dir}
if [ ! -x {SELFTEST_BIN} ]; then
  rm -f {SELFTEST_BIN}
  curl -fsSL --max-time 180 -o {SELFTEST_BIN} \
    https://github.com/apernet/hysteria/releases/latest/download/hysteria-linux-amd64 || exit 1
  chmod 755 {SELFTEST_BIN}
fi
{SELFTEST_BIN} version 2>&1 | head -2
""",
        timeout=300,
    )
    if not fetch.ok:
        print("   cannot fetch the hysteria client: " + fetch.tail(6))
        return False, fetch.tail(12)
    cfg_text = f"""server: 127.0.0.1:{cfg.hy2_port}
auth: {cfg.state['auth']}
obfs:
  type: salamander
  salamander:
    password: {cfg.state['obfs']}
tls:
  sni: {cfg.domain}
socks5:
  listen: 127.0.0.1:11080
log:
  level: info
"""
    remote.put(cfg_text, "/tmp/hy2-client.yaml", mode=0o600)
    res = remote.run(
        f"""
pkill -f "hysteria client" 2>/dev/null || true
sleep 0.5
nohup {SELFTEST_BIN} client -c /tmp/hy2-client.yaml >/tmp/hy2-client.log 2>&1 &
sleep 4
out=$(curl -s -m 15 --socks5-hostname 127.0.0.1:11080 https://www.cloudflare.com/cdn-cgi/trace | grep '^ip=' || true)
pkill -f "hysteria client" 2>/dev/null || true
rm -f /tmp/hy2-client.yaml
echo "RESULT=$out"
tail -n 5 /tmp/hy2-client.log
""",
        timeout=120,
    )
    ok = "RESULT=ip=" in res.out
    detail = res.out.strip()
    print("   " + ("PASS" if ok else "FAIL"))
    for line in detail.splitlines()[:6]:
        print("   " + line)
    return ok, detail


def read_credentials(remote: Remote, cfg: Cfg, env: dict[str, str]) -> tuple[str, str] | None:
    """Pull auth + obfs password straight out of the managed inbound."""
    base = panel_base(env, cfg)
    token = env.get("XUI_API_TOKEN", "")
    if not token:
        return None
    _, data = api(remote, base, token, "GET", "panel/api/inbounds/list")
    for ib in (data or {}).get("obj") or []:
        if ib.get("remark") != cfg.remark:
            continue
        client = ((ib.get("settings") or {}).get("clients") or [{}])[0]
        masks = ((ib.get("streamSettings") or {}).get("finalmask") or {}).get("udp") or [{}]
        return client.get("auth", ""), (masks[0].get("settings") or {}).get("password", "")
    return None


def fetch_share(remote: Remote, cfg: Cfg, env: dict[str, str]) -> str:
    """Ask the panel's own subscription endpoint for the canonical link."""
    db = "/etc/x-ui/x-ui.db"
    sub_path = remote.run(
        f"sqlite3 {db} \"select value from settings where key='subPath';\""
    ).out.strip() or "/"
    sub_id = f"hy2{cfg.client_name}"
    url = f"https://{cfg.domain}:2096/{sub_path.strip('/')}/{sub_id}"
    res = remote.run(
        f"curl -sk -m 20 --resolve {cfg.domain}:2096:127.0.0.1 -A 'v2rayN/6.45' {shlex.quote(url)}"
    )
    link = extract_link(res.out)
    if link.startswith("hysteria2://") and "@" in link:
        return link
    return ""


def cmd_deploy(cfg: Cfg) -> int:
    with Remote(cfg) as remote:
        ident = remote.run("id -u")
        if ident.out.strip() != "0":
            raise SystemExit("deploy needs root over SSH")
        ensure_base(remote)
        env = ensure_panel(remote)
        cfg.state["panel_port"] = env.get("XUI_PANEL_PORT")
        cert_mode = ensure_cert(remote, cfg)
        save_state(cfg)
        configure_panel_tls(remote, cfg)
        inbound = ensure_inbound(remote, cfg, env)
        cfg.state.update(inbound)
        cfg.state["cert_mode"] = cert_mode
        cfg.state["hop_range"] = cfg.hop_range
        save_state(cfg)

        if not wait_listening(remote, cfg):
            print("!! nothing is listening on udp/" + str(cfg.hy2_port))
            return 4

        ensure_hop(remote, cfg)

        ok = True
        if cfg.self_test:
            ok, _ = selftest(remote, cfg)

        link = fetch_share(remote, cfg, env) or build_link(
            cfg, cfg.state["auth"], cfg.state["obfs"], insecure=(cert_mode == "selfsigned")
        )
        cfg.state["link"] = link
        save_state(cfg)
        write_customer(cfg, link)

        print("\n== result")
        print(f"   panel : https://{cfg.domain}:{cfg.state['panel_port']}/{env.get('XUI_WEB_BASE_PATH','')}/")
        print(f"   user  : {env.get('XUI_USERNAME')} / {env.get('XUI_PASSWORD')}")
        print(f"   link  : {link}")
        return 0 if ok else 4


# --------------------------------------------------------------------------- #
# verify / export
# --------------------------------------------------------------------------- #
VERIFY_PROBE = r"""
echo "SVC=$(systemctl is-active x-ui 2>/dev/null)"
echo "HOP=$(systemctl is-active hy2-port-hop 2>/dev/null)"
echo "UDP=$(ss -lunp 2>/dev/null | grep -c ':'__PORT__' ' || true)"
echo "HOPRULE=$(nft list chain ip nat PREROUTING 2>/dev/null | grep -c 'udp dport' || true)"
echo "CERT_END=$(openssl x509 -in /etc/ssl/hy2/fullchain.pem -noout -enddate 2>/dev/null | cut -d= -f2)"
echo "PANEL=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' __BASE__ || true)"
echo "---- recent x-ui errors ----"
journalctl -u x-ui --since '-10 min' --no-pager 2>/dev/null | grep -iE 'error|panic' | tail -3 || true
"""


def cmd_verify(cfg: Cfg) -> int:
    with Remote(cfg) as remote:
        env = load_install_env(remote)
        base = panel_base(env, cfg)
        probe = VERIFY_PROBE.replace("__PORT__", str(cfg.hy2_port)).replace("__BASE__", base)
        res = remote.run(probe)
        print(res.out.strip())

        if not cfg.state.get("auth"):
            creds = read_credentials(remote, cfg, env)
            if creds:
                cfg.state["auth"], cfg.state["obfs"] = creds
                save_state(cfg)

        def num(key: str) -> int:
            m = re.search(rf"^{key}=(\d+)", res.out, re.M)
            return int(m.group(1)) if m else 0

        checks = [
            ("x-ui service active", "SVC=active" in res.out),
            ("port-hop service active", "HOP=active" in res.out),
            (f"udp/{cfg.hy2_port} listening", num("UDP") > 0),
            (f"nft redirect for {cfg.hop_range}", num("HOPRULE") > 0),
            ("panel https 200", "PANEL=200" in res.out),
        ]
        for name, ok in checks:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}")

        ok = all(c[1] for c in checks)
        if cfg.self_test:
            if not cfg.state.get("auth"):
                print("!! no credentials available — run deploy first")
                return 2
            passed, _ = selftest(remote, cfg)
            ok = ok and passed
        print("\nverify:", "PASS" if ok else "FAIL")
        return 0 if ok else 4


def write_customer(cfg: Cfg, link: str) -> None:
    out = cfg.runtime / "customer"
    out.mkdir(parents=True, exist_ok=True)
    (out / "v2rayn.txt").write_text(link + "\n", encoding="utf-8")

    import qrcode

    qr_path = out / "qr.png"
    qrcode.make(link).save(qr_path)

    verified = "not-checked"
    try:  # optional decoder
        import zxingcpp  # type: ignore
        from PIL import Image

        result = zxingcpp.read_barcodes(Image.open(qr_path))
        decoded = result[0].text if result else ""
        verified = "match" if decoded.strip() == link.strip() else "MISMATCH"
    except Exception:
        pass
    (out / "README.txt").write_text(
        "v2rayN.txt  完整 hysteria2:// 链接\n"
        "qr.png      同一链接的二维码\n"
        f"qr_verify   {verified}\n",
        encoding="utf-8",
    )
    print(f"   customer files -> {out}")


def cmd_export(cfg: Cfg) -> int:
    with Remote(cfg) as remote:
        env = load_install_env(remote)
        creds = read_credentials(remote, cfg, env)
        if creds:
            cfg.state["auth"], cfg.state["obfs"] = creds
        link = fetch_share(remote, cfg, env)
        if not link:
            if not cfg.state.get("auth"):
                raise SystemExit("cannot build link: no credentials in state.json")
            link = build_link(cfg, cfg.state["auth"], cfg.state["obfs"],
                              insecure=cfg.state.get("cert_mode") == "selfsigned")
            print("(panel subscription unavailable — built offline)")
        cfg.state["link"] = link
        save_state(cfg)
        write_customer(cfg, link)
        print(link)
        return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hy2.py", description=__doc__)
    parser.add_argument("command", choices=["plan", "doctor", "deploy", "verify", "export"])
    parser.add_argument("--config", required=True, help="path to config.json")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    handler = {
        "plan": cmd_plan,
        "doctor": cmd_doctor,
        "deploy": cmd_deploy,
        "verify": cmd_verify,
        "export": cmd_export,
    }[args.command]
    return handler(cfg)


if __name__ == "__main__":
    sys.exit(main())
