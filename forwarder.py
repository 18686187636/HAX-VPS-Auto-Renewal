#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot 转发器：监听 @HaxTG_bot 消息，提取续期码并转发到目标 Bot
环境变量：
  API_ID          - my.telegram.org 获取的应用 ID
  API_HASH        - 应用 Hash
  SESSION_STRING  - 本地生成的会话字符串
  TARGET_BOT_TOKEN - 目标 Bot Token（续期脚本轮询的 Bot）
  TARGET_CHAT_ID   - 目标 Chat ID
"""
import os
import asyncio
import re
import sys
from telethon import TelegramClient, events
import requests

# 环境变量
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
TARGET_BOT_TOKEN = os.environ.get('TARGET_BOT_TOKEN', '')
TARGET_CHAT_ID = os.environ.get('TARGET_CHAT_ID', '')

if not all([API_ID, API_HASH, SESSION_STRING, TARGET_BOT_TOKEN, TARGET_CHAT_ID]):
    print("❌ 缺少必要的环境变量，退出。")
    sys.exit(1)

# 续期码正则（匹配 Base64 字符串，长度 >= 32）
CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')

client = TelegramClient(SESSION_STRING, API_ID, API_HASH)


@client.on(events.NewMessage(from_users='@HaxTG_bot'))
async def handler(event):
    """当收到 @HaxTG_bot 的消息时触发"""
    text = event.raw_text or ''
    print(f'[Forwarder] 收到消息: {text[:50]}...')

    # 提取续期码
    match = CODE_PATTERN.search(text)
    if match:
        code = match.group(0)
        print(f'[Forwarder] ✅ 捕获到续期码: {code[:20]}...')

        # 发送到目标 Bot
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
        print('[Forwarder] ⚠️ 消息中未找到续期码（可能是其他消息）')


async def main():
    print('[Forwarder] 启动，正在监听 @HaxTG_bot 的消息...')
    try:
        await client.start()
        print('[Forwarder] ✅ 已登录 Telegram（使用 Session）')
        print('[Forwarder] 等待消息中...')
        await client.run_until_disconnected()
    except Exception as e:
        print(f'[Forwarder] ❌ 错误: {e}')
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
