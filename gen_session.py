#!/usr/bin/env python3
"""
在本地运行一次，生成 Telegram 会话字符串，存入 GitHub Secrets
"""
from telethon import TelegramClient
import sys

print("=" * 60)
print("Telegram Session 生成器")
print("=" * 60)
print("\n请先访问 https://my.telegram.org/apps")
print("登录后点击 'Create new application'，获取 API ID 和 API Hash\n")

try:
    api_id = int(input("请输入 API ID（整数）: "))
    api_hash = input("请输入 API Hash（字符串）: ").strip()
except ValueError:
    print("❌ API ID 必须为整数，请重新运行。")
    sys.exit(1)

if not api_hash:
    print("❌ API Hash 不能为空")
    sys.exit(1)

client = TelegramClient('session', api_id, api_hash)

async def main():
    print("\n📱 正在连接 Telegram...")
    await client.start()
    print("✅ 登录成功！")
    me = await client.get_me()
    print(f"\n👤 用户: {me.first_name} (@{me.username or '无用户名'})")
    session_str = client.session.save()
    with open('session_string.txt', 'w') as f:
        f.write(session_str)
    print("\n" + "=" * 60)
    print("✅ 生成的 Session 字符串（请保存到 GitHub Secrets）")
    print("=" * 60)
    print(session_str)
    print("=" * 60)
    print("\n已同时保存到文件: session_string.txt")

with client:
    client.loop.run_until_complete(main())
