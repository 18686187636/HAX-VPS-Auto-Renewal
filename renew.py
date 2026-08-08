#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX VPS Auto-Renewal (多账号 + Telethon 自动确认 - 增强 token 提取)
"""
import os
import sys
import time
import re
import json
import base64
import html
import tempfile
import random
import socket
import traceback
import asyncio
from datetime import datetime, timezone, timedelta

import requests as req_lib
from ruyipage import launch, Keys

# Telethon 相关
try:
    from telethon import TelegramClient, functions
    from telethon.sessions import StringSession
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    sr = None
    AudioSegment = None

# ===================== 环境变量 =====================
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON", "[]")
ACCOUNTS = json.loads(ACCOUNTS_JSON)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_ADDR = os.getenv("PROXY_SERVER", "socks5://127.0.0.1:1080")
CODE_FILE = "renewal_code.txt"
TG_RENEWAL_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')
DEBUG = os.getenv("DEBUG", "true").lower() == "true"
SEND_SCREENSHOTS = os.getenv("SEND_SCREENSHOTS", "true").lower() == "true"

SESSION_STRINGS = [
    os.getenv("SESSION_STRING_1", ""),
    os.getenv("SESSION_STRING_2", ""),
    os.getenv("SESSION_STRING_3", ""),
]

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")

def debug_print(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, **kwargs, flush=True)

# ===================== 代理检测 =====================
def is_port_open(host, port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def get_proxies():
    if not PROXY_ADDR:
        return None
    if is_port_open('127.0.0.1', 1080) or is_port_open('127.0.0.1', 1081):
        return {"http": PROXY_ADDR, "https": PROXY_ADDR}
    return None

def check_proxy_ip(proxies):
    if not proxies:
        return False, None
    services = [
        'https://api.ipify.org?format=json',
        'https://ip.sb/json',
        'https://httpbin.org/ip'
    ]
    for url in services:
        try:
            resp = req_lib.get(url, proxies=proxies, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                ip = data.get('ip') or data.get('origin')
                if ip:
                    return True, ip
        except Exception:
            continue
    return False, None

# ===================== 工具函数 =====================
def get_beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def send_telegram_message(text, bot_token, chat_id):
    if not bot_token or not chat_id:
        return False
    proxies = get_proxies()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                            timeout=10, proxies=proxies)
        return resp.json().get("ok", False)
    except Exception:
        try:
            resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
            return resp.json().get("ok", False)
        except Exception:
            return False

def send_telegram_photo(photo_path, caption, bot_token, chat_id):
    if not bot_token or not chat_id or not SEND_SCREENSHOTS:
        return False
    if not os.path.exists(photo_path):
        return False
    proxies = get_proxies()
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': chat_id, 'caption': caption}
            resp = req_lib.post(url, data=data, files=files, timeout=30, proxies=proxies)
            return resp.json().get("ok", False)
    except Exception as e:
        print(f"  [TG] 发送图片失败: {e}", flush=True)
        return False

def take_screenshot(page, path, bot_token, chat_id, caption):
    try:
        debug_print(f"尝试截图: {path}")
        driver = None
        if hasattr(page, 'driver'):
            driver = page.driver
        elif hasattr(page, '_driver'):
            driver = page._driver
        elif hasattr(page, 'page'):
            driver = page.page
        else:
            for attr in ['driver', '_driver', 'page']:
                try:
                    if hasattr(page, attr):
                        driver = getattr(page, attr)
                        break
                except:
                    pass
        if driver and hasattr(driver, 'get_screenshot_as_file'):
            driver.get_screenshot_as_file(path)
        else:
            try:
                if hasattr(page, 'screenshot'):
                    page.screenshot(path)
                elif hasattr(page, 'get_screenshot'):
                    page.get_screenshot(path)
                else:
                    raise Exception("无可用截图方法")
            except Exception as e:
                debug_print(f"截图方法失败: {e}")
                if driver and hasattr(driver, 'save_screenshot'):
                    driver.save_screenshot(path)
                else:
                    raise
        if os.path.exists(path):
            send_telegram_photo(path, caption, bot_token, chat_id)
    except Exception as e:
        print(f"  [截图] 失败: {e}", flush=True)

def notify_success(phone, expiry, bot_token, chat_id):
    msg = f"✅ <b>VPS 续期成功</b>\n\nHAX\n📱 {phone}\n📅 {expiry or '未知'}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def notify_failed(phone, step, error, bot_token, chat_id):
    msg = f"❌ <b>VPS 续期失败</b>\n\nHAX\n📱 {phone}\n📍 {step}\n⚠️ {error}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

# ===================== 续期码文件读写 =====================
def read_code_from_file():
    try:
        if os.path.exists(CODE_FILE):
            with open(CODE_FILE, 'r', encoding='utf-8') as f:
                code = f.read().strip()
            if code and TG_RENEWAL_PATTERN.search(code):
                print(f"  [文件] ✅ 从文件读取到续期码: {code[:20]}...")
                return code
            else:
                print(f"  [文件] 文件内容无效或为空", flush=True)
        else:
            print(f"  [文件] 文件不存在", flush=True)
    except Exception as e:
        print(f"  [文件] 读取异常: {e}", flush=True)
    return None

def write_code_to_file(code):
    try:
        with open(CODE_FILE, 'w', encoding='utf-8') as f:
            f.write(code)
        print(f"  [文件] ✅ 续期码已写入文件: {code[:20]}...")
        return True
    except Exception as e:
        print(f"  [文件] 写入失败: {e}", flush=True)
        return False

# ===================== 登录检测 =====================
def is_logged_in(page):
    try:
        logout_btn = page.ele("xpath://*[contains(text(), 'Logout') or contains(text(), 'Log out')]", timeout=2)
        if logout_btn and logout_btn.is_displayed:
            return True
        login_btn = page.ele("xpath://*[contains(text(), 'Login')]", timeout=2)
        if login_btn and login_btn.is_displayed:
            return False
        if "hax.co.id/vps-info" in page.url:
            menu = page.ele("css:a.nav-link.dropdown-toggle", timeout=2)
            if menu and menu.is_displayed:
                return True
        return False
    except:
        return False

# ===================== Telethon 自动确认 =====================
async def accept_login_token_async(token, session_string):
    if not TELEGRAM_AVAILABLE or not session_string or not API_ID or not API_HASH:
        debug_print("Telethon 未配置或不可用")
        return False
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.start()
        result = await client(functions.auth.AcceptLoginTokenRequest(token=token))
        await client.disconnect()
        debug_print(f"Telethon 自动确认成功: {result}")
        return True
    except Exception as e:
        print(f"  [Telethon] 自动确认失败: {e}", flush=True)
        return False

def accept_login_token_sync(token, session_string):
    if not token or not session_string:
        return False
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(accept_login_token_async(token, session_string))
        loop.close()
        return result
    except Exception as e:
        print(f"  [Telethon] 执行异常: {e}", flush=True)
        return False

# ===================== 增强的 Login Token 提取 =====================
def extract_login_token(oauth_page):
    """
    从 OAuth 页面中提取 login token，尝试多种方法。
    """
    debug_print("尝试提取 login token...")
    
    # 方法1: 从 window 对象
    token = oauth_page.run_js("return window.tgLogin?.token || window._tgLoginToken || '';")
    if token:
        debug_print(f"从 window 提取到 token: {token[:10]}...")
        return token
    
    # 方法2: 从 iframe 的 src 参数（如果有嵌套 iframe）
    try:
        src = oauth_page.run_js("return document.querySelector('iframe')?.src || '';")
        if src:
            match = re.search(r'[?&]token=([^&]+)', src)
            if match:
                token = match.group(1)
                debug_print(f"从 iframe src 提取到 token: {token[:10]}...")
                return token
    except Exception:
        pass
    
    # 方法3: 从页面 HTML 中搜索 token 字符串
    try:
        html_content = oauth_page.run_js("return document.documentElement.outerHTML;")
        if html_content:
            # 常见的 token 格式: "token":"xxx" 或 'token':'xxx' 或 token=xxx
            patterns = [
                r'"token"\s*:\s*"([^"]+)"',
                r"'token'\s*:\s*'([^']+)'",
                r'token\s*=\s*"([^"]+)"',
                r'token\s*=\s*\'([^\']+)\'',
                r'token\s*=\s*([^\s&]+)',
            ]
            for pat in patterns:
                match = re.search(pat, html_content)
                if match:
                    token = match.group(1)
                    debug_print(f"从 HTML 提取到 token: {token[:10]}...")
                    return token
    except Exception:
        pass
    
    # 方法4: 尝试从页面中的 script 标签中提取 (更通用)
    try:
        scripts = oauth_page.run_js("return Array.from(document.querySelectorAll('script')).map(s => s.innerText).join('\\n');")
        if scripts:
            # 搜索 tgLogin 或类似变量
            match = re.search(r'tgLogin\s*=\s*\{[^}]*token\s*:\s*["\']([^"\']+)', scripts)
            if match:
                token = match.group(1)
                debug_print(f"从 script 提取到 token: {token[:10]}...")
                return token
    except Exception:
        pass
    
    debug_print("所有 token 提取方法均失败")
    return None

# ===================== Telegram OAuth 登录（增强版） =====================
def login_with_telegram_original(page, phone, bot_token=None, chat_id=None, session_string=None):
    debug_print("进入 login_with_telegram_original")
    print(f"  [LOGIN] 尝试 Telegram OAuth 登录: {phone}", flush=True)
    try:
        iframe_xpath = "xpath://iframe[contains(@src, 'oauth.telegram.org')]"
        for _ in range(10):
            if page.ele(iframe_xpath, timeout=2):
                break
            time.sleep(1)
        else:
            raise RuntimeError("未找到 Telegram OAuth iframe")
        with page.with_frame(iframe_xpath) as frame_page:
            btn = frame_page.ele("css:button.tgme_widget_login_button", timeout=5)
            if not btn:
                raise RuntimeError("未找到 Telegram 登录按钮")
            btn.click_self()
            print("  [LOGIN] 点击 Telegram 登录按钮", flush=True)
            page.wait(3)

            # 查找 OAuth 标签页
            oauth_tab_id = None
            for _ in range(10):
                for tab_id in page.tab_ids:
                    tab = page.get_tab(tab_id)
                    if "oauth.telegram.org" in (tab.url or ""):
                        oauth_tab_id = tab_id
                        break
                if oauth_tab_id:
                    break
                time.sleep(1)
            if not oauth_tab_id:
                raise RuntimeError("未找到 OAuth tab")

            oauth_page = page.get_tab(oauth_tab_id)
            oauth_page.activate()
            oauth_page.wait.doc_loaded(timeout=30)

            # 输入手机号
            phone_input = None
            for _ in range(10):
                phone_input = oauth_page.ele("css:#login-phone-code", timeout=2)
                if phone_input:
                    break
                time.sleep(1)
            if not phone_input:
                raise RuntimeError("未找到手机号输入框")
            phone_input.input(phone, clear=True)
            print(f"  [LOGIN] 输入手机号: {phone}", flush=True)
            page.wait(2)

            # 点击继续
            continue_btn = oauth_page.ele("text:继续") or oauth_page.ele("text:Next") or oauth_page.ele("css:button[type=submit]") or oauth_page.ele("css:button")
            if continue_btn:
                continue_btn.click_self()
                print("  [LOGIN] 点击继续", flush=True)
            else:
                oauth_page.run_js("document.querySelector('form')?.submit();")
                print("  [LOGIN] 使用 JS 提交", flush=True)

            time.sleep(3)

            # ----- 增强的 token 提取 -----
            login_token = extract_login_token(oauth_page)
            if login_token and session_string:
                print(f"  [LOGIN] 提取到 login token: {login_token[:10]}...")
                if TELEGRAM_AVAILABLE and API_ID and API_HASH:
                    print("  [LOGIN] 正在使用 Telethon 自动确认...")
                    if accept_login_token_sync(login_token, session_string):
                        print("  [LOGIN] ✅ 自动确认成功，等待跳转...")
                        for _ in range(30):
                            time.sleep(1)
                            if "hax.co.id/vps-info" in (page.url or ""):
                                print("  [LOGIN] 已跳转到 VPS 信息页")
                                return True
                        # 若未跳转，继续下面的逻辑
                    else:
                        print("  [LOGIN] ⚠️ 自动确认失败，将进入手动确认模式")
                else:
                    print("  [LOGIN] ⚠️ Telethon 未配置，将进入手动确认模式")
            else:
                if not login_token:
                    print("  [LOGIN] 未找到 login token")
                if not session_string:
                    print("  [LOGIN] 未提供 session_string")
                print("  [LOGIN] 将进入手动确认模式")

            # ----- 手动确认模式（保留原逻辑） -----
            try:
                page.to_tab(page.tab_id)
            except:
                pass

            print("  [LOGIN] 📱 请在您的 Telegram 应用中点击“确认登录”", flush=True)
            print("  [LOGIN] ⏳ 脚本将等待最多 3 分钟...", flush=True)

            for i in range(180):
                time.sleep(1)
                if i % 30 == 0 and i > 0:
                    try:
                        take_screenshot(page, f"waiting_confirm_{phone}.png",
                                        bot_token, chat_id,
                                        f"⏳ 等待确认登录 - {phone}\n已等待 {i} 秒")
                    except:
                        pass
                    print(f"  [LOGIN] ⏳ 等待确认中... ({i}s)", flush=True)
                if "hax.co.id/vps-info" in (page.url or ""):
                    print("  [LOGIN] ✅ 已跳转到 VPS 信息页，登录成功！", flush=True)
                    return True

            raise RuntimeError("登录超时：未在 3 分钟内收到确认")

    except Exception as e:
        print(f"  [LOGIN] 失败: {e}", flush=True)
        traceback.print_exc()
        return False

# ===================== 设置 Cookie =====================
def set_session_cookie(page, session_token):
    try:
        page.set_cookies([{"name": "PHPSESSID", "value": session_token, "domain": ".hax.co.id", "path": "/"}])
        print("  [COOKIE] 通过 set_cookies 成功", flush=True)
        return True
    except Exception as e:
        debug_print(f"set_cookies 失败: {e}")
    try:
        page.run_js(f"document.cookie = 'PHPSESSID={session_token}; path=/; domain=.hax.co.id; SameSite=Lax';")
        print("  [COOKIE] 通过 JS 注入成功", flush=True)
        return True
    except Exception as e:
        debug_print(f"JS 注入失败: {e}")
    return False

# ===================== reCAPTCHA 音频求解（完整，省略以节省篇幅，但实际应完整包含） =====================
# ...（此处应包含 solve_recaptcha、算术验证码等函数，与之前相同，此处省略以聚焦于问题，实际使用需补全）...

# 为了完整性，快速列出剩余函数（实际代码中需完整包含）：
def find_frame(page, keyword): pass
def is_recaptcha_solved(page): pass
def click_recaptcha_checkbox(page): pass
def switch_to_audio(page): pass
def get_audio_url(page): pass
def download_audio(url): pass
def recognize_audio(mp3_path): pass
def fill_and_verify(page, text): pass
def solve_recaptcha(page, timeout=60): pass
def solve_arithmetic_captcha(page): pass
def get_renewal_code_from_telegram(bot_tokens, page, phone, bot_token, chat_id, timeout=1800, poll_interval=10): pass
def close_ads(page): pass
def handle_consent(page): pass

# ===================== 单账号续期主流程 =====================
def renew_account(account, session_string=None):
    # ... 与之前相同，只需确保调用 login_with_telegram_original 时传入 session_string ...
    # 此处省略重复代码，实际使用时请合并完整版本
    pass

# ===================== 主入口 =====================
if __name__ == "__main__":
    print("#########################", flush=True)
    print("   HAX 自动续期 (多账号 + Telethon 自动确认 - 增强 token 提取)", flush=True)
    print("#########################", flush=True)
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON", flush=True)
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号", flush=True)
    success = 0
    for idx, acc in enumerate(ACCOUNTS):
        print(f"\n============================== 处理第 {idx+1}/{len(ACCOUNTS)} 个账号 ==============================", flush=True)
        session_string = SESSION_STRINGS[idx] if idx < len(SESSION_STRINGS) else ""
        try:
            if renew_account(acc, session_string=session_string):
                success += 1
        except Exception as e:
            print(f"  ⚠️ 账号处理异常: {e}", flush=True)
            traceback.print_exc()
        time.sleep(random.randint(10, 30))
    print(f"\n{'='*60}\n完成: {success}/{len(ACCOUNTS)} 个账号续期成功\n{'='*60}", flush=True)
