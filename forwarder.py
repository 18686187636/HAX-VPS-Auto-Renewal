#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot 续期码提取器（仅写入文件，不转发）
"""
import os
import asyncio
import re
import sys
from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_STRING = os.environ.get('SESSION_STRING', '')
CODE_FILE = "renewal_code.txt"

if not all([API_ID, API_HASH, SESSION_STRING]):
    print("❌ 缺少必要的环境变量（API_ID, API_HASH, SESSION_STRING），退出。")
    sys.exit(1)

if len(SESSION_STRING) < 50:
    print(f"⚠️ SESSION_STRING 过短（{len(SESSION_STRING)} 字符），可能无效")
    sys.exit(1)

CODE_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def save_code_to_file(code):
    """将续期码写入文件（覆盖写入）"""
    try:
        with open(CODE_FILE, 'w', encoding='utf-8') as f:
            f.write(code)
        print('[Extractor] ✅ 续期码已写入文件')
    except Exception as e:
        print(f'[Extractor] ⚠️ 写入文件失败: {e}')


@client.on(events.NewMessage(from_users='@HaxTG_bot'))
async def handler(event):
    text = event.raw_text or ''
    print(f'[Extractor] 收到消息: {text[:80]}...')
    match = CODE_PATTERN.search(text)
    if match:
        code = match.group(0)
        print(f'[Extractor] ✅ 捕获到续期码: {code[:20]}...')
        save_code_to_file(code)
    else:
        print('[Extractor] ⚠️ 消息中未找到续期码（可能是其他消息）')


async def main():
    print('[Extractor] 启动，正在监听 @HaxTG_bot 的消息...')
    try:
        await client.start()
        print('[Extractor] ✅ 已登录 Telegram（使用 StringSession）')
        me = await client.get_me()
        print(f'[Extractor] 👤 登录用户: {me.first_name} (@{me.username or "无用户名"})')
        await client.run_until_disconnected()
    except Exception as e:
        print(f'[Extractor] ❌ 错误: {e}')
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
