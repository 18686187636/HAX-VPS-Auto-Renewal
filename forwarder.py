#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot 转发器：监听 @HaxTG_bot 消息，提取续期码并转发到目标 Bot
使用 StringSession（支持 Base64 编码的 Session 字符串）
"""
import os
import asyncio
import re
import sys
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
TARGET_BOT_TOKEN = os.environ.get('TARGET_BOT_TOKEN', '')
TARGET_CHAT_ID = os.environ.get('TARGET_CHAT_ID', '')

if not all([API_ID, API_HASH, SESSION_STRING, TARGET_BOT_TOKEN, TARGET_CHAT_ID]):
    print("❌ 缺少必要的环境变量，退出。")
    sys.exit(1)

# 验证 SESSION_STRING 长度（Base64 编码的 session 文件通常很长）
if len(SESSION_STRING) < 100:
    print(f"⚠️ SESSION_STRING 过短（{len(SESSION_STRING)} 字符），可能无效")
    # 不强制退出，让 telethon 自己处理

CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')

# 使用 StringSession
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@client.on(events.NewMessage(from_users='@HaxTG_bot'))
async def handler(event):
    text = event.raw_text or ''
    print(f'[Forwarder] 收到消息: {text[:50]}...')
    match = CODE_PATTERN.search(text)
    if match:
        code = match.group(0)
        print(f'[Forwarder] ✅ 捕获到续期码: {code[:20]}...')
        url = f'https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendMessage'
        data = {'chat_id': TARGET_CHAT_ID, 'text': code}
        try:
            resp = requests.post(url, json=data, timeout=10)
            if resp.status_code == 200 and resp.json().get('ok'):
                print('[Forwarder] ✅ 续期码已转发到目标 Bot')
            else:
                print(f'[Forwarder] ❌ 转发失败: {resp.text}')
        except Exception as e:
            print(f'[Forwarder] ❌ 网络异常: {e}')
    else:
        print('[Forwarder] ⚠️ 消息中未找到续期码')

async def main():
    print('[Forwarder] 启动，正在监听 @HaxTG_bot 的消息...')
    try:
        await client.start()
        print('[Forwarder] ✅ 已登录 Telegram（使用 StringSession）')
        me = await client.get_me()
        print(f'[Forwarder] 👤 登录用户: {me.first_name} (@{me.username or "无用户名"})')
        await client.run_until_disconnected()
    except Exception as e:
        print(f'[Forwarder] ❌ 错误: {e}')
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
