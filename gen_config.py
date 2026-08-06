#!/usr/bin/env python3
import os
import json
import base64
import urllib.parse

vless_link = os.getenv("NODE_LINK", "")
if not vless_link:
    print("NODE_LINK 未设置，退出")
    exit(0)

# 解析 vless://... 链接
if not vless_link.startswith("vless://"):
    print("不支持的链接类型")
    exit(1)

# 去掉协议头
link = vless_link[8:]
# 分离用户信息@地址和参数
if "@" in link:
    user_info, rest = link.split("@", 1)
else:
    user_info = ""
    rest = link

if "?" in rest:
    server_part, query = rest.split("?", 1)
else:
    server_part = rest
    query = ""

# 解析 server 和 port
if ":" in server_part:
    server, port_str = server_part.rsplit(":", 1)
    port = int(port_str)
else:
    server = server_part
    port = 443

# 解析参数
params = urllib.parse.parse_qs(query)
uuid = user_info  # 在 vless 链接中，@ 前是 uuid
if not uuid:
    uuid = params.get("id", [""])[0]

# 取各参数（默认值）
security = params.get("security", ["tls"])[0]
flow = params.get("flow", ["xtls-rprx-vision"])[0]
network = params.get("type", ["ws"])[0]
path = params.get("path", ["/"])[0]
host = params.get("host", [server])[0]  # 通常用于 SNI
encryption = params.get("encryption", ["none"])[0]

# 构造 sing-box 配置
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
    "outbounds": [
        {
            "type": "vless",
            "tag": "proxy",
            "server": server,
            "server_port": port,
            "uuid": uuid,
            "security": security,
            "flow": flow,
            "network": network,
            "ws_settings": {
                "path": path,
                "headers": {"Host": host}
            } if network == "ws" else {},
            "tls": {
                "enabled": security == "tls",
                "server_name": host
            } if security == "tls" else {}
        }
    ],
    "route": {
        "rules": [
            {"outbound": "proxy"}
        ]
    }
}

with open("config.json", "w") as f:
    json.dump(config, f, indent=2)
print("✅ config.json 已生成")
