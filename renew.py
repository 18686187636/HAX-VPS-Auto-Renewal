#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX VPS Auto-Renewal (最终修正版 - 续期码文件读写)
- 每个账号完成后 TG 通知剩余未完成列表
- 全部完成后 TG 通知「今日 hax 续期全部完成」
- 登录后检测到期时间，剩余 > 96h 则跳过
- 失败账号自动重试，最多 5 轮
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
from datetime import datetime, timezone, timedelta

import requests as req_lib
from ruyipage import launch, Keys

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
# 进度通知开关（每个账号完成后推送）
NOTIFY_PROGRESS = os.getenv("NOTIFY_PROGRESS", "true").lower() == "true"
# 剩余时间超过该阈值（小时）则视为"已续期"，直接跳过
SKIP_THRESHOLD_HOURS = float(os.getenv("SKIP_THRESHOLD_HOURS", "96"))
# 失败账号最大重试轮数
MAX_RENEW_ROUNDS = int(os.getenv("MAX_RENEW_ROUNDS", "5"))

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

def notify_skipped(phone, valid_until, remaining_hours, bot_token, chat_id):
    rh_str = f"{remaining_hours:.1f} 小时" if isinstance(remaining_hours, (int, float)) else "未知"
    msg = (f"⏭️ <b>VPS 已续期，跳过</b>\n\nHAX\n📱 {phone}\n"
           f"📅 到期: {valid_until or '未知'}\n"
           f"⏰ 剩余: {rh_str}\n"
           f"🕒 {get_beijing_time()}")
    send_telegram_message(msg, bot_token, chat_id)

def notify_progress(round_no, max_rounds, current_idx, total, phone,
                    status_emoji, status_text, pending_list, bot_token, chat_id):
    """每个账号处理完后，推送当前进度 + 未完成账号列表"""
    if not bot_token or not chat_id:
        return
    lines = [f"{status_emoji} <b>HAX 进度 [第{round_no}/{max_rounds}轮] {current_idx}/{total}</b>",
             "",
             f"📱 刚完成: <code>{phone}</code>",
             f"📌 结果: {status_text}",
             ""]
    if pending_list:
        lines.append(f"⏳ <b>本轮未完成 ({len(pending_list)})：</b>")
        for i, p in enumerate(pending_list, 1):
            lines.append(f"  {i}. <code>{p}</code>")
    else:
        lines.append("🎉 <b>本轮所有账号已处理完毕</b>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)

def notify_round_start(round_no, max_rounds, pending_accounts, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"🔄 <b>HAX 第 {round_no}/{max_rounds} 轮开始</b>", ""]
    lines.append(f"⏳ <b>待处理 ({len(pending_accounts)})：</b>")
    for i, a in enumerate(pending_accounts, 1):
        lines.append(f"  {i}. <code>{a.get('phone', '?')}</code>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)

def notify_round_end(round_no, will_retry, pending_accounts, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"📋 <b>HAX 第 {round_no} 轮结束</b>", ""]
    if will_retry:
        lines.append(f"⏳ 仍有 {len(pending_accounts)} 个账号未完成，将进入下一轮重试：")
        for i, a in enumerate(pending_accounts, 1):
            lines.append(f"  {i}. <code>{a.get('phone', '?')}</code>")
    else:
        lines.append("🎉 本轮全部处理完毕")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)

def notify_all_done(total, success, failed, skipped, failed_list, bot_token, chat_id):
    """全部处理完后，推送总结"""
    if not bot_token or not chat_id:
        return
    lines = [
        "🎊 <b>今日 hax 续期全部完成</b>",
        "",
        f"📊 总数: {total}",
        f"✅ 成功: {success}",
        f"⏭️ 跳过: {skipped}",
        f"❌ 失败: {failed}",
    ]
    if failed_list:
        lines.append("")
        lines.append("⚠️ <b>失败账号：</b>")
        for i, p in enumerate(failed_list, 1):
            lines.append(f"  {i}. <code>{p}</code>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)

# ===================== 到期时间检测 =====================
def get_page_field_value(page, label_text):
    """从页面 label.col-form-label + 相邻值 div 中提取字段值"""
    try:
        js = """
        (function(lbl) {
            var labels = document.querySelectorAll('label.col-form-label, label');
            for (var i = 0; i < labels.length; i++) {
                var t = (labels[i].textContent || '').trim();
                if (t.toLowerCase() === lbl.toLowerCase()) {
                    var parent = labels[i].closest('.row') || labels[i].parentElement;
                    if (parent) {
                        var valDiv = parent.querySelector('.col-sm-7, .col-sm-6, .col-md-7, div');
                        if (valDiv && valDiv !== labels[i]) {
                            return (valDiv.textContent || '').trim();
                        }
                    }
                }
            }
            return '';
        })('%s');
        """ % label_text.replace("'", "\\'")
        return page.run_js(js) or ""
    except Exception as e:
        debug_print(f"get_page_field_value({label_text}) 异常: {e}")
        return ""

def parse_dt(s):
    """
    支持以下所有格式（多余空白/括号注释会被忽略）：
      1) "HH:MM:SS - Month DD, YYYY"   → 时间在前、日期在后（HAX 的 Current time）
      2) "HH:MM - Month DD, YYYY"
      3) "Month DD, YYYY HH:MM:SS"      → 日期在前、时间在后
      4) "Month DD, YYYY"               → 只有日期，按当天 00:00:00 处理（HAX 的 Valid until）
      5) "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD" / "YYYY/MM/DD ..."
      6) "DD Month YYYY" 等常见变体
    解析不到时间部分时，按当天 00:00:00 处理。
    """
    if not s:
        return None

    # 去掉括号注释 (UTC+8) 之类，压缩空白
    s = re.sub(r'\(.*?\)', '', s).strip()
    s = re.sub(r'\s+', ' ', s)

    MONTHS = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12,
    }

    def month_num(name):
        if not name:
            return None
        return MONTHS.get(name.strip().lower().rstrip('.,'))

    # ---- 1) "HH:MM[:SS] - Month DD, YYYY"  （时间 - 日期）----
    m = re.match(
        r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*'
        r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$',
        s
    )
    if m:
        hh = int(m.group(1)); mm = int(m.group(2))
        ss = int(m.group(3)) if m.group(3) else 0
        mon = month_num(m.group(4))
        day = int(m.group(5)); year = int(m.group(6))
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    # ---- 2) "Month DD, YYYY HH:MM[:SS]"  （日期 时间）----
    m = re.match(
        r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})'
        r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$',
        s
    )
    if m:
        mon = month_num(m.group(1))
        day = int(m.group(2)); year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    # ---- 3) "DD Month YYYY [HH:MM[:SS]]"  （日 月 年）----
    m = re.match(
        r'^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})'
        r'(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$',
        s
    )
    if m:
        day = int(m.group(1))
        mon = month_num(m.group(2))
        year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    # ---- 4) 数字日期格式，用 strptime 快速处理 ----
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue

    return None

def check_should_renew(page):
    """
    检查 VPS 是否需要续期。
    返回 (should_renew: bool, valid_until_str, remaining_hours)
    """
    valid_str = get_page_field_value(page, "Valid until")
    current_str = get_page_field_value(page, "Current time")

    if not valid_str:
        debug_print("未找到 'Valid until' 字段，继续续期流程")
        return True, None, None

    valid_dt = parse_dt(valid_str)
    if not valid_dt:
        print(f"  [CHECK] ⚠️ 无法解析到期时间: {valid_str!r}，继续续期")
        return True, valid_str, None

    now_dt = parse_dt(current_str) or datetime.now()
    remaining_hours = (valid_dt - now_dt).total_seconds() / 3600.0

    print(f"  [CHECK] Valid until : {valid_str}", flush=True)
    print(f"  [CHECK] Current time: {now_dt.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  [CHECK] 剩余: {remaining_hours:.2f} 小时 (阈值 {SKIP_THRESHOLD_HOURS} 小时)", flush=True)

    if remaining_hours > SKIP_THRESHOLD_HOURS:
        return False, valid_str, remaining_hours
    return True, valid_str, remaining_hours

# ===================== 续期码文件读写 =====================
def read_code_from_file():
    """从文件读取续期码，若存在且有效则返回，否则返回 None"""
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
    """将续期码写入文件"""
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

# ===================== Telegram OAuth 登录 =====================
def login_with_telegram_original(page, phone):
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
            continue_btn = oauth_page.ele("text:继续") or oauth_page.ele("text:Next") or oauth_page.ele("css:button[type=submit]") or oauth_page.ele("css:button")
            if continue_btn:
                continue_btn.click_self()
                print("  [LOGIN] 点击继续", flush=True)
            else:
                oauth_page.run_js("document.querySelector('form')?.submit();")
                print("  [LOGIN] 使用 JS 提交", flush=True)
            try:
                page.to_tab(page.tab_id)
            except:
                pass
            for _ in range(60):
                time.sleep(2)
                if "hax.co.id/vps-info" in (page.url or ""):
                    print("  [LOGIN] 已跳转到 VPS 信息页", flush=True)
                    return True
            raise RuntimeError("登录超时，未跳转到 VPS 信息页")
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

# ===================== reCAPTCHA 音频求解（完整） =====================
def find_frame(page, keyword):
    try:
        frames = page.get_frames()
        for frame in frames:
            frame_url = (frame.url or "").lower()
            if "recaptcha" in frame_url and keyword in frame_url:
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            token = frame.run_js(
                "(() => { try { const el = document.querySelector('textarea[name=g-recaptcha-response]'); return el ? el.value : ''; } catch(e) { return ''; } })()"
            )
            if token and len(token) > 30:
                return True
    except Exception:
        pass
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "(() => { try { const el = document.querySelector('#recaptcha-anchor'); return el ? (el.getAttribute('aria-checked') === 'true') : false; } catch(e) { return false; } })()"
            )
            if checked:
                return True
        except Exception:
            pass
    return False

def click_recaptcha_checkbox(page):
    anchor = find_frame(page, "anchor")
    if not anchor:
        for _ in range(120):
            anchor = find_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
        if not anchor:
            raise RuntimeError("reCAPTCHA anchor iframe not found")
    checkbox = anchor.ele("#recaptcha-anchor", timeout=3)
    if not checkbox:
        raise RuntimeError("reCAPTCHA checkbox not found")
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
    time.sleep(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except Exception:
        checkbox.click(by_js=True)
    time.sleep(3)

def switch_to_audio(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    for _ in range(3):
        try:
            audio_btn = bframe.ele("#recaptcha-audio-button", timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                input_box = bframe.ele("#audio-response", timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except Exception:
            pass
    try:
        bframe.run_js(
            "(() => { const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click(); })()"
        )
        time.sleep(3)
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    return False

def get_audio_url(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele(".rc-audiochallenge-tdownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return html.unescape(href)
            link = bframe.ele(".rc-audiochallenge-ndownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = bframe.ele("#audio-source", timeout=1)
            if audio:
                src = audio.attr("src")
                if src and len(src) > 10:
                    return html.unescape(src)
        except Exception:
            pass
        time.sleep(1)
    return None

def download_audio(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/",
    }
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            r = req_lib.get(audio_url, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception:
            pass
    return None

def recognize_audio(mp3_path):
    if sr and AudioSegment:
        try:
            wav_path = mp3_path.replace(".mp3", ".wav")
            AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio_data = recognizer.record(source)
                text = recognizer.recognize_google(audio_data)
            try:
                os.remove(wav_path)
            except Exception:
                pass
            if text:
                print(f"  [STT] Google 识别: {text}", flush=True)
                return text
        except Exception as e:
            print(f"  [STT] Google 失败: {e}", flush=True)
    audio_api_url = os.getenv("AUDIO_API_URL")
    if audio_api_url:
        try:
            with open(mp3_path, "rb") as f:
                files = {"audio": f}
                resp = req_lib.post(audio_api_url, files=files, timeout=30)
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text") or result.get("result") or result.get("data")
                if text:
                    print(f"  [API] 备用识别: {text}", flush=True)
                    return text
        except Exception as e:
            print(f"  [API] 备用识别失败: {e}", flush=True)
    return None

def fill_and_verify(page, text):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=2)
        if not input_box:
            return False
        input_box.click()
        input_box.clear()
        input_box.input(text)
    except Exception:
        return False
    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele("#recaptcha-verify-button", timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(by_js=True)
    except Exception:
        pass
    return True

def solve_recaptcha(page, timeout=60):
    debug_print("开始 solve_recaptcha")
    start_time = time.time()
    for _ in range(int(timeout / 2)):
        if find_frame(page, "anchor"):
            break
        time.sleep(2)
    while time.time() - start_time < timeout:
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 已通过！", flush=True)
            return True
        try:
            click_recaptcha_checkbox(page)
        except Exception as e:
            print(f"  [reCAPTCHA] 点击复选框失败: {e}", flush=True)
            time.sleep(2)
            continue
        time.sleep(2)
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 点击后直接通过！", flush=True)
            return True
        if not switch_to_audio(page):
            time.sleep(2)
            if not switch_to_audio(page):
                print("  [reCAPTCHA] 无法切换到音频模式", flush=True)
                time.sleep(random.uniform(2, 4))
                continue
        time.sleep(random.uniform(2, 4))
        audio_url = get_audio_url(page)
        if not audio_url:
            print("  [reCAPTCHA] 未找到音频 URL，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 音频 URL: {audio_url[:80]}...", flush=True)
        mp3_path = download_audio(audio_url)
        if not mp3_path:
            print("  [reCAPTCHA] 音频下载失败，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 音频已下载: {os.path.basename(mp3_path)}", flush=True)
        text = recognize_audio(mp3_path)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        if not text:
            print("  [reCAPTCHA] 无法识别语音，重试...", flush=True)
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 识别结果: [{text}]", flush=True)
        fill_and_verify(page, text)
        time.sleep(5)
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] 语音验证通过！", flush=True)
            return True
        else:
            print("  [reCAPTCHA] 验证未通过，重新获取音频...", flush=True)
            time.sleep(random.uniform(2, 4))
    print(f"  [reCAPTCHA] {timeout} 秒超时", flush=True)
    return False

# ===================== 算术验证码 =====================
def solve_arithmetic_captcha(page):
    debug_print("开始 solve_arithmetic_captcha")
    print("  [CAPTCHA] 识别算式验证码...", flush=True)
    page.wait(3)
    img_urls_str = page.run_js("""(() => {
        const all = document.querySelectorAll("img");
        const urls = [];
        for (let i = 0; i < all.length; i++) {
            const s = all[i].src || '';
            if (s && !s.startsWith('data:')) {
                urls.push({src: s, w: all[i].naturalWidth, h: all[i].naturalHeight});
            }
        }
        return JSON.stringify(urls);
    })()""")
    try:
        all_imgs = json.loads(img_urls_str)
    except Exception:
        all_imgs = []
    print(f"  [CAPTCHA] 页面共 {len(all_imgs)} 张图片", flush=True)
    captcha_urls = []
    for img in all_imgs:
        s = img.get('src', '')
        w, h = img.get('w', 0), img.get('h', 0)
        if 'hax.co.id/img/temp/' in s and 15 <= w <= 50 and 15 <= h <= 50:
            captcha_urls.append(s)
    print(f"  [CAPTCHA] 找到 {len(captcha_urls)} 张验证码图片", flush=True)
    if len(captcha_urls) < 2:
        for img in all_imgs:
            s = img.get('src', '')
            w, h = img.get('w', 0), img.get('h', 0)
            if s and not s.startswith('data:') and 'hax.co.id' in s:
                if w <= 50 and h <= 50:
                    captcha_urls.append(s)
    if len(captcha_urls) < 2:
        print(f"  [CAPTCHA] 图片不足，使用所有非logo图片", flush=True)
        for img in all_imgs:
            s = img.get('src', '')
            if s and not s.startswith('data:') and 'logo' not in s.lower():
                captcha_urls.append(s)
        captcha_urls = captcha_urls[:2]
    print(f"  [CAPTCHA] URL: {[u.split('/')[-1][:30] for u in captcha_urls]}", flush=True)
    digits = []
    for url in captcha_urls[:2]:
        after_dash = url.rsplit('-', 1)[-1] if '-' in url else ''
        first_char = after_dash[0] if after_dash else ''
        if first_char.isdigit():
            digit = int(first_char)
            digits.append(digit)
            print(f"  [CAPTCHA] URL提取数字: {digit}", flush=True)
        else:
            print(f"  [CAPTCHA] URL提取失败，使用默认值0", flush=True)
            digits.append(0)
    if len(digits) < 2:
        print(f"  [CAPTCHA] 识别失败: {digits}", flush=True)
        return None
    op_text = page.run_js("""(() => {
        const groups = document.querySelectorAll('.form-group.row');
        for (let g = 0; g < groups.length; g++) {
            const imgs = groups[g].querySelectorAll('img');
            if (imgs.length >= 2) {
                const walker = document.createTreeWalker(groups[g], NodeFilter.SHOW_TEXT, null, false);
                while (walker.nextNode()) {
                    const txt = walker.currentNode.textContent.trim();
                    if (txt.length <= 3 && /[+\\-×÷*/xX]/.test(txt)) return txt;
                }
                const els = groups[g].querySelectorAll('*');
                for (let e = 0; e < els.length; e++) {
                    const txt = els[e].textContent.trim();
                    if (txt.length <= 3 && /[+\\-×÷*/xX]/.test(txt) && els[e].querySelectorAll('img').length === 0) return txt;
                }
                return '';
            }
        }
        return '';
    })()""")
    print(f"  [CAPTCHA] 运算符: [{op_text}]", flush=True)
    op = "+"
    if "×" in op_text or "*" in op_text or "x" in op_text or "X" in op_text:
        op = "*"
    elif "-" in op_text or "−" in op_text or "－" in op_text:
        op = "-"
    result = eval(f"{digits[0]} {op} {digits[1]}")
    op_symbol = "×" if op == "*" else ("−" if op == "-" else "+")
    print(f"  [CAPTCHA] 算式: {digits[0]} {op_symbol} {digits[1]} = {result}", flush=True)
    return result

# ===================== 续期码获取（含文件轮询） =====================
def get_renewal_code_from_telegram(bot_tokens, page, phone, bot_token, chat_id, timeout=1800, poll_interval=10):
    """
    轮询 Telegram 获取续期码，同时每轮检查文件。
    若从文件读取到有效码，则立即返回。
    """
    debug_print(f"进入 get_renewal_code_from_telegram，超时 {timeout}s")
    offsets = {}
    for bt in bot_tokens:
        try:
            proxies = get_proxies()
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            resp = req_lib.get(url, timeout=10, proxies=proxies) if proxies else req_lib.get(url, timeout=10)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception:
            offsets[bt['token']] = 0
    elapsed = 0
    code = ""
    last_screenshot_minute = -1
    while elapsed < timeout:
        # 每轮先检查文件
        file_code = read_code_from_file()
        if file_code:
            print(f"  [CODE] 从文件读取到续期码: {file_code[:20]}***，直接使用", flush=True)
            return file_code, "file"

        current_minute = elapsed // 60
        if current_minute > last_screenshot_minute and current_minute > 0:
            last_screenshot_minute = current_minute
            try:
                png_path = f"waiting_{phone}_{current_minute}m.png"
                take_screenshot(page, png_path, bot_token, chat_id,
                                f"⏳ 等待续期码 (已等待 {current_minute} 分钟) - {phone}")
            except Exception as e:
                print(f"  [截图] 等待截图失败: {e}", flush=True)

        # 轮询 Telegram
        for bt in bot_tokens:
            offset = offsets.get(bt['token'], 0)
            try:
                proxies = get_proxies()
                url = f"https://api.telegram.org/bot{bt['token']}/getUpdates?offset={offset}&timeout=5"
                resp = (req_lib.get(url, timeout=10, proxies=proxies) if proxies else req_lib.get(url, timeout=10))
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offsets[bt['token']] = update["update_id"] + 1
                        msg = update.get("message", {})
                        text = msg.get("text", "") or msg.get("caption", "")
                        if text:
                            match = TG_RENEWAL_PATTERN.search(text)
                            if match:
                                code = match.group(0)
                                # 写入文件
                                write_code_to_file(code)
                                return code, bt.get("label", bt['token'][-6:])
            except Exception:
                pass
        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)", flush=True)
    return "", None

# ===================== 增强版广告关闭（含 JS 移除） =====================
def close_ads(page):
    """按照本地脚本方式关闭广告，并额外用 JS 移除遮挡元素"""
    print("  [AD] 等待并关闭广告...")
    page.wait(3)
    try:
        page.actions.press(Keys.ESCAPE).perform()
        page.wait(1)
    except Exception:
        pass
    for keyword in ["Close", "close", "×", "关闭"]:
        try:
            el = page.ele(f'xpath://*[contains(text(), "{keyword}")]')
            if el and el.is_displayed:
                el.click_self()
                page.wait(1)
                break
        except Exception:
            pass
    page.wait(3)

    # 额外通过 JS 移除常见弹窗元素
    js_remove = """
    (function() {
        var selectors = [
            '.overlay', '.modal-backdrop', '.popup-overlay', 
            '[class*="overlay"]', '[class*="modal"]', '[class*="popup"]',
            '.ad-container', '.ad-wrapper', '.banner-ad'
        ];
        selectors.forEach(function(sel) {
            document.querySelectorAll(sel).forEach(function(el) { el.remove(); });
        });
        var all = document.querySelectorAll('*');
        all.forEach(function(el) {
            var style = getComputedStyle(el);
            if (style.position === 'fixed' && parseInt(style.zIndex) > 999) {
                el.remove();
            }
        });
    })();
    """
    try:
        page.run_js(js_remove)
        time.sleep(1)
    except Exception as e:
        debug_print(f"JS移除弹窗失败: {e}")

# ===================== 处理 Consent 弹窗 =====================
def handle_consent(page):
    try:
        consent_btn = None
        keywords = ["Consent", "Accept", "Agree", "Got it", "OK", "Allow"]
        for kw in keywords:
            try:
                btn = page.ele(f"xpath://*[contains(text(), '{kw}')]", timeout=2)
                if btn and btn.is_displayed:
                    consent_btn = btn
                    break
            except:
                continue
        if consent_btn:
            consent_btn.click_self()
            print("  [Consent] 点击 Consent 按钮", flush=True)
            page.wait(2)
            return True
        return False
    except Exception as e:
        debug_print(f"处理 Consent 失败: {e}")
        return False

# ===================== 单账号续期主流程 =====================
def renew_account(account):
    """
    返回值：
      ("success", {"expiry": ...})  — 续期成功
      ("skipped", {"valid_until": ..., "remaining_hours": ...}) — 已续期跳过
      ("failed",  {"step": ..., "error": ...}) — 失败
    """
    phone = account.get("phone")
    session_token = account.get("session_token")
    bot_token = account.get("bot_token")
    chat_id = account.get("chat_id")

    if not phone:
        print("  ⚠️ 账号缺少手机号，跳过", flush=True)
        return "failed", {"step": "参数校验", "error": "缺少手机号"}

    print(f"\n{'='*60}\n  续期: {phone}\n{'='*60}", flush=True)

    proxies = get_proxies()
    if proxies:
        print(f"🔗 代理地址: {PROXY_ADDR}", flush=True)
        ok, ip = check_proxy_ip(proxies)
        if ok:
            print(f"📍 代理出口 IP: {ip}", flush=True)
        else:
            print("⚠️ 代理出口 IP 获取失败，将使用直连", flush=True)
            proxies = None
    else:
        print("🔗 代理不可用，使用直连", flush=True)

    page = None
    try:
        debug_print("准备启动浏览器...")
        launch_args = {"headless": HEADLESS, "window_size": (1366, 768)}
        if proxies is not None:
            launch_args["proxy"] = PROXY_ADDR
        print("  [BROWSER] 正在启动浏览器（此步骤可能较慢）...", flush=True)
        page = launch(**launch_args)
        debug_print("浏览器启动成功，开始访问登录页")
        page.get("https://hax.co.id/login")
        page.wait.doc_loaded(timeout=20)
        page.wait(5)

        # ---------- 尝试 Cookie 登录 ----------
        login_success = False
        if session_token:
            debug_print("尝试 Cookie 登录")
            print("  [LOGIN] 尝试使用 session_token 快速登录...", flush=True)
            page.get("https://hax.co.id/login")
            set_session_cookie(page, session_token)
            debug_print("Cookie 设置完成，跳转 vps-info")
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            page.get("https://hax.co.id/vps-info")  # 强制刷新
            page.wait.doc_loaded(timeout=10)
            if is_logged_in(page):
                print("  ✅ Cookie 登录成功", flush=True)
                login_success = True
            else:
                print("  ⚠️ Cookie 未生效，将执行 OAuth", flush=True)

        if not login_success:
            debug_print("Cookie 登录失败，执行 OAuth")
            print("  [LOGIN] 执行 Telegram OAuth 登录...", flush=True)
            login_success = login_with_telegram_original(page, phone)
            if not login_success:
                raise RuntimeError("Telegram 登录失败")

        # 再次确认登录
        if not is_logged_in(page):
            print("  ⚠️ 登录后未检测到登录状态，重新加载...", flush=True)
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            if is_logged_in(page):
                print("  ✅ 重新加载后确认登录", flush=True)
            else:
                raise RuntimeError("无法确认登录状态")

        print("  ✅ 登录成功，开始续期流程", flush=True)

        # ----- 1. 处理 Consent 和广告 -----
        handle_consent(page)
        close_ads(page)

        # 登录成功截图
        try:
            take_screenshot(page, f"login_success_{phone}.png", bot_token, chat_id, f"✅ 登录成功 - {phone}")
        except Exception as e:
            print(f"  [截图] 登录截图失败: {e}", flush=True)

        # ---------- 检测是否需要续期 ----------
        # 确保在 vps-info 页面
        if "hax.co.id/vps-info" not in (page.url or ""):
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=15)
        page.wait(2)
        try:
            should_renew, valid_until, remaining_hours = check_should_renew(page)
        except Exception as e:
            print(f"  [CHECK] 检查异常: {e}，继续续期", flush=True)
            should_renew, valid_until, remaining_hours = True, None, None

        if not should_renew:
            print(f"  ⏭️ 已续期（剩余 {remaining_hours:.1f} 小时），跳过", flush=True)
            notify_skipped(phone, valid_until, remaining_hours, bot_token, chat_id)
            return "skipped", {"valid_until": valid_until, "remaining_hours": remaining_hours}

        # ---------- 导航到续期 ----------
        debug_print("导航到 VPS 续期")
        vps_menu = None
        for _ in range(5):
            try:
                vps_menu = page.ele("css:a.nav-link.dropdown-toggle")
                if vps_menu and vps_menu.is_displayed:
                    break
            except Exception:
                pass
            page.wait(2)
        if not vps_menu:
            raise RuntimeError("未找到 VPS 下拉菜单")
        vps_menu.click_self()
        page.wait(2)

        renew_btn = page.ele('css:a.dropdown-item[href="/vps-renew/"]')
        if not renew_btn:
            raise RuntimeError("未找到 VPS Renew 按钮")
        renew_btn.click_self(by_js=True)
        print("  [NAV] 点击 VPS Renew", flush=True)
        page.wait(5)

        # ---------- 填写表单 ----------
        debug_print("填写续期表单")
        web_input = page.ele("css:#web_address")
        if web_input:
            web_input.input("hax.co.id", clear=True)
            print("  [FORM] 输入域名", flush=True)
        agreement = page.ele('css:input[name="agreement"][value="yes"]')
        if agreement and not agreement.is_checked:
            agreement.click_self(by_js=True)
            print("  [FORM] 勾选协议", flush=True)

        print("  [CF] 等待 CloudFlare 验证 (60s)...", flush=True)
        page.wait(60)

        renew_vps_btn = page.ele("css:button[name=submit_button][type=button].btn-primary")
        if not renew_vps_btn:
            raise RuntimeError("未找到 Renew VPS 按钮")
        renew_vps_btn.click_self(by_js=True)
        print("  [FORM] 点击 Renew VPS", flush=True)
        page.wait(5)

        # ----- 2. 点击 Renew VPS 后关闭广告 -----
        close_ads(page)

        try:
            take_screenshot(page, f"renew_vps_{phone}.png", bot_token, chat_id, f"🔄 已点击 Renew VPS - {phone}")
        except Exception as e:
            print(f"  [截图] Renew VPS 截图失败: {e}", flush=True)

        # ---------- 获取续期码（先读文件，若无则轮询） ----------
        debug_print("开始获取续期码")
        # 1. 优先从文件读取
        code = read_code_from_file()
        source = "file"
        if code:
            print(f"  [CODE] 直接从文件使用续期码: {code[:20]}***", flush=True)
        else:
            # 2. 文件没有，则轮询 Telegram
            print("  [CODE] 文件无续期码，开始等待 @HaxTG_bot 发送...", flush=True)
            all_bots = []
            seen = set()
            for acc in ACCOUNTS:
                t = acc.get("bot_token")
                if t and t not in seen:
                    seen.add(t)
                    all_bots.append({"token": t, "label": f"...{t[-6:]}"})
            if bot_token and bot_token not in seen:
                all_bots.insert(0, {"token": bot_token, "label": f"...{bot_token[-6:]}"})

            code, source = get_renewal_code_from_telegram(
                all_bots, page, phone, bot_token, chat_id,
                timeout=1800, poll_interval=10
            )
            if not code:
                try:
                    take_screenshot(page, f"timeout_{phone}.png", bot_token, chat_id,
                                    f"⏰ 续期码超时 - {phone}\n请检查 HaxTG_bot")
                except:
                    pass
                raise RuntimeError("未获取到续期码")

        # 3. 使用原始 Base64（不解码）
        print(f"  [CODE] 使用原始码: {code[:20]}***", flush=True)
        renewal_code_to_input = code

        # ---------- 进入续期码输入页 ----------
        debug_print("进入续期码输入页")
        renew_code_link = None
        for selector in [
            'css:a.btn[href="/vps-renew-code"]',
            "text:INPUT RENEW CODE",
            "text:Input Renew Code",
        ]:
            try:
                renew_code_link = page.ele(selector)
                if renew_code_link:
                    break
            except Exception:
                pass
        if not renew_code_link:
            raise RuntimeError("未找到 INPUT RENEW CODE 按钮")
        renew_code_link.click_self(by_js=True)
        print("  [NAV] 点击 INPUT RENEW CODE", flush=True)
        page.wait.doc_loaded(timeout=15)
        page.wait(3)

        # ----- 3. 进入续期码页后关闭广告 -----
        close_ads(page)

        # ---------- 算术验证码 ----------
        captcha_result = solve_arithmetic_captcha(page)
        if captcha_result is not None:
            code_input = page.ele("css:#captcha")
            if code_input:
                code_input.input(str(captcha_result), clear=True)
                print(f"  [CAPTCHA] 输入结果: {captcha_result}", flush=True)

        # 填入续期码（直接使用原始 Base64）
        debug_print("填入续期码")
        vcode_input = None
        for selector in ["css:input.form-control:not(#captcha)", "css:input[name=code]", "css:input#code"]:
            try:
                vcode_input = page.ele(selector)
                if vcode_input:
                    break
            except Exception:
                pass
        if vcode_input:
            try:
                page.run_js(
                    "(() => { const el = document.querySelector('input.form-control:not(#captcha)'); if (el) el.scrollIntoView({behavior: 'instant', block: 'center'}); })()"
                )
                page.wait(1)
            except Exception:
                pass
            vcode_input.input(renewal_code_to_input, clear=True)
            print(f"  [CODE] 输入 renewal code (原始Base64): {renewal_code_to_input[:20]}***", flush=True)

        # ---------- reCAPTCHA ----------
        debug_print("开始 reCAPTCHA")
        print("  [reCAPTCHA] 处理音频验证...", flush=True)
        recaptcha_solved = solve_recaptcha(page, timeout=90)
        if not recaptcha_solved:
            print("  [reCAPTCHA] 自动解决失败，等待用户手动处理...", flush=True)
            print("  [reCAPTCHA] 请在浏览器中手动完成验证（60秒）", flush=True)
            page.wait(60)
            recaptcha_solved = is_recaptcha_solved(page)

        # ---------- 提交续期 ----------
        debug_print("提交续期")
        print("  [SUBMIT] 提交续期...", flush=True)
        close_ads(page)  # 先关闭可能遮挡的广告

        submit_btn = None
        for selector in [
            "css:button[name=submit_button]",
            "css:button.btn-primary",
            "text:Submit",
            "text:Submit Renew",
        ]:
            try:
                submit_btn = page.ele(selector)
                if submit_btn:
                    break
            except Exception:
                pass
        if not submit_btn:
            print("  [SUBMIT] 未找到提交按钮，列出所有 button...", flush=True)
            all_btns = page.eles("css:button")
            for btn in all_btns:
                print(f"    button: name={btn.attr('name')} class={btn.attr('class')} text={btn.text.strip()[:50]}", flush=True)
            raise RuntimeError("未找到提交按钮")
        try:
            submit_btn.click_self(by_js=True)
        except Exception:
            submit_btn.click_self()
        print("  [SUBMIT] 已点击提交，等待结果...", flush=True)
        time.sleep(60)

        # ---------- 检查结果（增强弹窗清理） ----------
        debug_print("检查续期结果")
        for _ in range(3):
            close_ads(page)
            time.sleep(1)
        page.run_js("""
            document.querySelectorAll('.overlay, .modal, .popup, [class*="overlay"], [class*="modal"], [class*="popup"]')
                .forEach(el => el.remove());
        """)
        time.sleep(2)
        page.wait.doc_loaded(timeout=15)
        page.wait(3)
        close_ads(page)
        result_text = page.run_js("document.body.innerText") or ""
        result_lower = result_text.lower()
        is_success = False
        expiry_date = None
        success_keywords = [
            "renewed successfully",
            "renewal successful",
            "subscription renewed",
            "subscription successfully",
            "续期成功",
            "renewed",
        ]
        if any(kw in result_lower for kw in success_keywords):
            is_success = True
            print("  [RESULT] 检测到续期成功！", flush=True)
            for pat in [
                r"until\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})",
                r"[Ee]xpir(?:e|y)[:\s]*(\d{4}-\d{2}-\d{2})",
                r"[Vv]alid.*[Uu]ntil[:\s]*(\d{4}-\d{2}-\d{2})",
                r"[到期期][：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})",
            ]:
                m = re.search(pat, result_text)
                if m:
                    expiry_date = m.group(1)
                    print(f"  [RESULT] 到期日: {expiry_date}", flush=True)
                    break

        try:
            result_png = f"result_{phone}.png"
            status = "成功" if is_success else "失败"
            caption = f"{'✅' if is_success else '❌'} {status} - {phone}\n到期日: {expiry_date or '未知'}"
            take_screenshot(page, result_png, bot_token, chat_id, caption)
        except Exception as e:
            print(f"  [截图] 结果截图失败: {e}", flush=True)

        if is_success:
            notify_success(phone, expiry_date or "未知日期", bot_token, chat_id)
            return "success", {"expiry": expiry_date}
        else:
            error_msg = "Captcha 验证失败" if "captcha" in result_lower else "页面未显示明确结果"
            notify_failed(phone, "结果页", error_msg, bot_token, chat_id)
            print(f"  [RESULT] 失败: {error_msg}", flush=True)
            return "failed", {"step": "结果页", "error": error_msg}

    except Exception as e:
        print(f"  ❌ 异常: {e}", flush=True)
        traceback.print_exc()
        if page:
            try:
                take_screenshot(page, f"error_{phone}.png", bot_token, chat_id, f"⚠️ 异常 - {phone}\n{e}")
            except:
                pass
        notify_failed(phone, "执行异常", str(e), bot_token, chat_id)
        return "failed", {"step": "执行异常", "error": str(e)}
    finally:
        if page:
            try:
                page.quit()
            except:
                pass

# ===================== 主入口 =====================
if __name__ == "__main__":
    print("#########################", flush=True)
    print("   HAX 自动续期 (最终修正版 - 文件读写)", flush=True)
    print("#########################", flush=True)
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON", flush=True)
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号", flush=True)
    print(f"✅ 跳过阈值: {SKIP_THRESHOLD_HOURS} 小时", flush=True)
    print(f"✅ 最大轮数: {MAX_RENEW_ROUNDS}", flush=True)
    print(f"✅ 进度通知: {'开启' if NOTIFY_PROGRESS else '关闭'}", flush=True)

    total = len(ACCOUNTS)

    # 通知通道：优先用第一个 bot_token + chat_id 都齐全的账号
    summary_bot = ""
    summary_chat = ""
    for acc in ACCOUNTS:
        if acc.get("bot_token") and acc.get("chat_id"):
            summary_bot = acc["bot_token"]
            summary_chat = acc["chat_id"]
            break
    if not summary_bot:
        for acc in ACCOUNTS:
            if acc.get("bot_token"):
                summary_bot = acc["bot_token"]
                summary_chat = acc.get("chat_id", "")
                break

    # 结果统计（用原始索引，确保 SESSION_STRING 匹配不因重试而错位）
    success_set = set()
    skipped_set = set()

    # pending 是 (original_index, account) 列表
    pending = [(i, acc) for i, acc in enumerate(ACCOUNTS, 1)]
    round_no = 0

    # ========== 外层轮次循环 ==========
    while pending and round_no < MAX_RENEW_ROUNDS:
        round_no += 1
        print(f"\n{'#'*60}", flush=True)
        print(f"  第 {round_no}/{MAX_RENEW_ROUNDS} 轮，待处理 {len(pending)} 个账号", flush=True)
        print(f"{'#'*60}", flush=True)

        # 第 2 轮开始前通知
        if round_no > 1 and NOTIFY_PROGRESS:
            try:
                notify_round_start(round_no, MAX_RENEW_ROUNDS,
                                   [a for _, a in pending],
                                   summary_bot, summary_chat)
            except Exception as e:
                print(f"  [NOTIFY] 轮次开始通知失败: {e}", flush=True)

        failed_in_round = []  # 本轮失败的 (orig_idx, acc)
        accounts_this_round = pending  # 本轮要处理的账号快照

        for i_in_round, (orig_idx, acc) in enumerate(accounts_this_round, 1):
            phone = acc.get("phone", f"account_{orig_idx}")
            print(f"\n===== [第{round_no}轮] 处理 {i_in_round}/{len(accounts_this_round)}: {phone} (原索引 {orig_idx}) =====", flush=True)

            status = "failed"
            info = {"step": "未知", "error": "未知"}
            try:
                status, info = renew_account(acc)
            except Exception as e:
                print(f"  ⚠️ 账号处理异常: {e}", flush=True)
                traceback.print_exc()
                status = "failed"
                info = {"step": "主循环异常", "error": str(e)}

            # 分类
            if status == "success":
                success_set.add(orig_idx)
                emoji = "✅"
                status_text = f"续期成功（到期 {info.get('expiry') or '未知'}）"
            elif status == "skipped":
                skipped_set.add(orig_idx)
                emoji = "⏭️"
                rh = info.get("remaining_hours")
                status_text = f"已续期跳过（剩余 {rh:.1f}h）" if isinstance(rh, (int, float)) else "已续期跳过"
            else:
                failed_in_round.append((orig_idx, acc))
                emoji = "❌"
                status_text = f"失败（{info.get('step', '')}: {info.get('error', '')}）"

            # 计算"本轮未完成" = 本轮剩下的 + 本轮已失败的
            remaining_in_round = accounts_this_round[i_in_round:]
            pending_phones = (
                [a.get("phone", "?") for _, a in remaining_in_round] +
                [a.get("phone", "?") for _, a in failed_in_round]
            )

            if NOTIFY_PROGRESS:
                try:
                    notify_progress(
                        round_no=round_no,
                        max_rounds=MAX_RENEW_ROUNDS,
                        current_idx=i_in_round,
                        total=len(accounts_this_round),
                        phone=phone,
                        status_emoji=emoji,
                        status_text=status_text,
                        pending_list=pending_phones,
                        bot_token=acc.get("bot_token", "") or summary_bot,
                        chat_id=acc.get("chat_id", "") or summary_chat,
                    )
                except Exception as e:
                    print(f"  [NOTIFY] 进度通知失败: {e}", flush=True)

            # 同一轮账号之间间隔
            if i_in_round < len(accounts_this_round):
                time.sleep(random.randint(10, 30))

        # 本轮结束：把失败的作为下一轮的 pending
        pending = failed_in_round

        if pending:
            print(f"\n[ROUND {round_no}] 本轮结束，仍有 {len(pending)} 个账号未完成", flush=True)
            will_retry = round_no < MAX_RENEW_ROUNDS
            if NOTIFY_PROGRESS:
                try:
                    notify_round_end(round_no, will_retry,
                                     [a for _, a in pending],
                                     summary_bot, summary_chat)
                except Exception as e:
                    print(f"  [NOTIFY] 轮次结束通知失败: {e}", flush=True)

            # 若还有下一轮，等一下再开始，避免被风控
            if will_retry:
                delay = random.randint(60, 120)
                print(f"  轮次间隔等待 {delay} 秒...", flush=True)
                time.sleep(delay)
        else:
            break

    # ========== 全部完成后：总结通知 ==========
    final_failed = [a.get("phone", "?") for _, a in pending]
    final_failed_detail = [(idx, a.get("phone", "?")) for idx, a in pending]

    try:
        notify_all_done(
            total=total,
            success=len(success_set),
            failed=len(final_failed),
            skipped=len(skipped_set),
            failed_list=final_failed,
            bot_token=summary_bot,
            chat_id=summary_chat,
        )
    except Exception as e:
        print(f"  [NOTIFY] 总结通知失败: {e}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"最终结果: 成功 {len(success_set)} / 跳过 {len(skipped_set)} / 失败 {len(final_failed)} / 共 {total} 个账号", flush=True)
    print(f"总轮数: {round_no}", flush=True)
    if final_failed_detail:
        print("仍失败的账号：", flush=True)
        for idx, phone in final_failed_detail:
            print(f"  - 索引 {idx}: {phone}", flush=True)
    print(f"{'='*60}", flush=True)
