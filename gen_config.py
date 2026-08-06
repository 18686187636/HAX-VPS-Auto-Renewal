#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态生成 sing-box 配置（从 VLESS 链接）
兼容 sing-box 1.13+
"""
import os
import json
import urllib.parse

vless_link = os.getenv("NODE_LINK", "")
if not vless_link:
    print("NODE_LINK 未设置，跳过")
    exit(0)

if not vless_link.startswith("vless://"):
    print("不支持的链接类型，仅支持 vless://")
    exit(1)

# 解析 vless:// 链接
link = vless_link[8:]
user_info, rest = link.split("@", 1) if "@" in link else ("", link)
server_part, query = rest.split("?", 1) if "?" in rest else (rest, "")
server, port_str = server_part.rsplit(":", 1) if ":" in server_part else (server_part, "443")
port = int(port_str)
params = urllib.parse.parse_qs(query)

uuid = user_info if user_info else params.get("id", [""])[0]
flow = params.get("flow", ["xtls-rprx-vision"])[0]
network = params.get("type", ["ws"])[0]
path = params.get("path", ["/"])[0]
host = params.get("host", [server])[0]
# 注意：有些 VLESS 链接用 security 字段，有些用 encryption
security = params.get("security", params.get("encryption", ["tls"]))[0]

# 构造出站
outbound = {
    "type": "vless",
    "tag": "proxy",
    "server": server,
    "server_port": port,
    "uuid": uuid,
    "flow": flow,
    "packet_encoding": "xudp",
}

if network == "ws":
    outbound["network"] = "ws"
    outbound["ws_settings"] = {
        "path": path,
        "headers": {"Host": host}
    }
elif network == "tcp":
    outbound["network"] = "tcp"

# TLS 配置（若 security 不是 'none'）
if security.lower() != "none":
    outbound["tls"] = {
        "enabled": True,
        "server_name": host,
        "utls": {
            "enabled": True,
            "fingerprint": "random"
        }
    }
    # 如有 sni 参数覆盖 server_name
    if "sni" in params:
        outbound["tls"]["server_name"] = params["sni"][0]

# 完整配置
config = {
    "log": {"level": "info"},
    "inbounds": [
        {
            "type": "socks",
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "listen_port": 1080
        }
    ],
    "outbounds": [outbound],
    "route": {
        "rules": [
            {"outbound": "proxy"}
        ]
    }
}

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)
print("✅ config.json 已生成")
