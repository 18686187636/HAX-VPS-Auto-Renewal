#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot 转发器
环境变量：
  API_ID          - my.telegram.org 获取的应用 ID
  API_HASH        - 应用 Hash
  SESSION_STRING  - 本地生成的会话字符串（对应个人账号）
  TARGET_BOT_TOKEN - 目标 Bot Token（续期脚本轮询的 Bot）
  TARGET_CHAT_ID   - 接收通知的 Chat ID（个人或群组）
"""
import os
import asyncio
import re
import sys
import time
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

if len(SESSION_STRING) < 50:
    print(f"⚠️ SESSION_STRING 过短（{len(SESSION_STRING)} 字符），可能无效")
    sys.exit(1)

CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

def forward_to_bot(text, max_retries=3):
    url = f'https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendMessage'
    data = {'chat_id': TARGET_CHAT_ID, 'text': text}
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=data, timeout=15)
            if resp.status_code == 200 and resp.json().get('ok'):
                print('[Forwarder] ✅ 续期码已发送到目标 Bot')
                return True
            else:
                print(f'[Forwarder] ⚠️ 发送尝试 {attempt+1} 失败: {resp.text}')
        except Exception as e:
            print(f'[Forwarder] ⚠️ 发送尝试 {attempt+1} 异常: {e}')
        time.sleep(2)
    print('[Forwarder] ❌ 发送失败（已达到最大重试次数）')
    return False

@client.on(events.NewMessage(from_users='@HaxTG_bot'))
async def handler(event):
    text = event.raw_text or ''
    print(f'[Forwarder] 收到来自 @HaxTG_bot 的消息: {text[:80]}...')
    match = CODE_PATTERN.search(text)
    if match:
        code = match.group(0)
        print(f'[Forwarder] ✅ 捕获到续期码: {code[:20]}...')
        forward_to_bot(code)
    else:
        print('[Forwarder] ⚠️ 消息中未找到续期码（可能是其他消息）')

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
