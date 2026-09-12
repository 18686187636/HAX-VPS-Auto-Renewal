#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX VPS Auto-Renewal (CF 挑战感知版 - 等待 105s)
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
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
SEND_SCREENSHOTS = os.getenv("SEND_SCREENSHOTS", "true").lower() == "true"
NOTIFY_PROGRESS = os.getenv("NOTIFY_PROGRESS", "true").lower() == "true"
SKIP_THRESHOLD_HOURS = float(os.getenv("SKIP_THRESHOLD_HOURS", "96"))
MAX_RENEW_ROUNDS = int(os.getenv("MAX_RENEW_ROUNDS", "5"))

NAV_RETRY = int(os.getenv("NAV_RETRY", "3"))
NAV_RETRY_DELAY = int(os.getenv("NAV_RETRY_DELAY", "10"))
REQ_RETRY = int(os.getenv("REQ_RETRY", "5"))
REQ_RETRY_DELAY = int(os.getenv("REQ_RETRY_DELAY", "20"))
CF_CHALLENGE_DELAY = int(os.getenv("CF_CHALLENGE_DELAY", "105"))   # ★ 45 → 105

IGNORE_COOKIE_NAMES = {
    "_ga", "_gid", "_gat_gtag_UA_179253361_1", "_ga_MK6PLQ755F",
    "__gads", "__gpi", "__eoi",
    "FCCDCF", "FCNEC", "FCOEC",
}


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
    except Exception:
        return False


def get_proxies():
    if not PROXY_ADDR:
        return None
    try:
        m = re.search(r':(\d+)/?$', PROXY_ADDR)
        if m:
            port = int(m.group(1))
            if is_port_open('127.0.0.1', port):
                return {"http": PROXY_ADDR, "https": PROXY_ADDR}
            return None
    except Exception:
        pass
    if is_port_open('127.0.0.1', 1080) or is_port_open('127.0.0.1', 1081):
        return {"http": PROXY_ADDR, "https": PROXY_ADDR}
    return None


def check_proxy_ip(proxies, retry=3):
    if not proxies:
        return False, None
    services = [
        'https://api.ipify.org?format=json',
        'https://ip.sb/json',
        'https://httpbin.org/ip'
    ]
    for attempt in range(retry):
        for url in services:
            try:
                resp = req_lib.get(url, proxies=proxies, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    ip = data.get('ip') or data.get('origin')
                    if ip:
                        return True, ip
            except Exception:
                continue
        if attempt < retry - 1:
            time.sleep(5)
    return False, None


# ===================== 浏览器安全导航 =====================
def safe_get(page, url, retry=NAV_RETRY, delay=NAV_RETRY_DELAY, timeout=20):
    for attempt in range(retry):
        try:
            page.get(url)
            page.wait.doc_loaded(timeout=timeout)
            return True
        except Exception as e:
            err = str(e)
            is_timeout = ("timeout" in err.lower() or "超时" in err)
            print(f"  [NAV] page.get({url}) 第{attempt+1}/{retry}次失败: "
                  f"{'超时' if is_timeout else err[:100]}", flush=True)
            if attempt < retry - 1:
                time.sleep(delay)
    return False


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
            resp = req_lib.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                                timeout=10)
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
        for attr in ['driver', '_driver', 'page']:
            if hasattr(page, attr):
                driver = getattr(page, attr)
                break
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


def _esc(v):
    return html.escape(str(v if v is not None else ""))


def notify_success(phone, expiry, bot_token, chat_id):
    msg = (f"✅ <b>VPS 续期成功</b>\n\nHAX\n"
           f"📱 {_esc(phone)}\n📅 {_esc(expiry or '未知')}\n⏰ {get_beijing_time()}")
    send_telegram_message(msg, bot_token, chat_id)


def notify_failed(phone, step, error, bot_token, chat_id):
    msg = (f"❌ <b>VPS 续期失败</b>\n\nHAX\n📱 {_esc(phone)}\n"
           f"📍 {_esc(step)}\n⚠️ {_esc(error)}\n⏰ {get_beijing_time()}")
    send_telegram_message(msg, bot_token, chat_id)


def notify_skipped(phone, valid_until, remaining_hours, bot_token, chat_id):
    rh_str = f"{remaining_hours:.1f} 小时" if isinstance(remaining_hours, (int, float)) else "未知"
    msg = (f"⏭️ <b>VPS 已续期，跳过</b>\n\nHAX\n📱 {_esc(phone)}\n"
           f"📅 到期: {_esc(valid_until or '未知')}\n⏰ 剩余: {rh_str}\n"
           f"🕒 {get_beijing_time()}")
    send_telegram_message(msg, bot_token, chat_id)


def notify_progress(round_no, max_rounds, current_idx, total, phone,
                    status_emoji, status_text, pending_list, bot_token, chat_id):
    if not bot_token or not chat_id:
        return
    lines = [f"{status_emoji} <b>HAX 进度 [第{round_no}/{max_rounds}轮] {current_idx}/{total}</b>",
             "",
             f"📱 刚完成: <code>{_esc(phone)}</code>",
             f"📌 结果: {_esc(status_text)}",
             ""]
    if pending_list:
        lines.append(f"⏳ <b>本轮未完成 ({len(pending_list)})：</b>")
        for i, p in enumerate(pending_list, 1):
            lines.append(f"  {i}. <code>{_esc(p)}</code>")
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
        lines.append(f"  {i}. <code>{_esc(a.get('phone', '?'))}</code>")
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
            lines.append(f"  {i}. <code>{_esc(a.get('phone', '?'))}</code>")
    else:
        lines.append("🎉 本轮全部处理完毕")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


def notify_all_done(total, success, failed, skipped, failed_list, bot_token, chat_id):
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
            lines.append(f"  {i}. <code>{_esc(p)}</code>")
    lines.append("")
    lines.append(f"🕒 {get_beijing_time()}")
    send_telegram_message("\n".join(lines), bot_token, chat_id)


# ===================== 到期时间检测 =====================
def get_page_field_value(page, label_text):
    try:
        js = """
        (function(lbl) {
            var labels = document.querySelectorAll('label.col-form-label, label');
            for (var i = 0; i < labels.length; i++) {
                var t = (labels[i].textContent || '').trim();
                if (t.toLowerCase() === lbl.toLowerCase()) {
                    var parent = labels[i].closest('.row') || labels[i].parentElement;
                    if (parent) {
                        var valDiv = parent.querySelector('.col-sm-7, .col-sm-6, .col-md-7');
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
    if not s:
        return None
    s = re.sub(r'\(.*?\)', '', s).strip()
    s = re.sub(r'\s+', ' ', s)

    MONTHS = {
        'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
        'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
        'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10, 'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    }

    def month_num(name):
        if not name:
            return None
        return MONTHS.get(name.strip().lower().rstrip('.,'))

    m = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*-\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        ss = int(m.group(3)) if m.group(3) else 0
        mon = month_num(m.group(4))
        day = int(m.group(5))
        year = int(m.group(6))
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', s)
    if m:
        mon = month_num(m.group(1))
        day = int(m.group(2))
        year = int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        if mon:
            try:
                return datetime(year, mon, day, hh, mm, ss)
            except Exception:
                pass

    m = re.match(r'^(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$', s)
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

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def check_should_renew(page):
    valid_str = get_page_field_value(page, "Valid until")
    current_str = get_page_field_value(page, "Current time")

    if not valid_str:
        debug_print("未找到 'Valid until' 字段，继续续期流程")
        return True, None, None

    valid_dt = parse_dt(valid_str)
    if not valid_dt:
        print(f"  [CHECK] ⚠️ 无法解析到期时间: {valid_str!r}，继续续期")
        return True, valid_str, None

    now_dt = parse_dt(current_str)
    if not now_dt:
        now_dt = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    remaining_hours = (valid_dt - now_dt).total_seconds() / 3600.0

    print(f"  [CHECK] Valid until : {valid_str}", flush=True)
    print(f"  [CHECK] Current time: {now_dt.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  [CHECK] 剩余: {remaining_hours:.2f} 小时 (阈值 {SKIP_THRESHOLD_HOURS} 小时)", flush=True)

    if remaining_hours > SKIP_THRESHOLD_HOURS:
        return False, valid_str, remaining_hours
    return True, valid_str, remaining_hours


# ===================== 续期码文件读写 =====================
def read_code_from_file():
    try:
        if os.path.exists(CODE_FILE):
            with open(CODE_FILE, 'r', encoding='utf-8') as f:
                code = f.read().strip()
            if code and TG_RENEWAL_PATTERN.search(code):
                print(f"  [文件] ✅ 从文件读取到续期码（长度 {len(code)}）")
                return code
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
        print(f"  [文件] ✅ 续期码已写入文件（长度 {len(code)}）")
        return True
    except Exception as e:
        print(f"  [文件] 写入失败: {e}", flush=True)
        return False


# ===================== 登录检测 =====================
def is_logged_in(page):
    try:
        logout_btn = page.ele(
            "xpath://*[contains(text(), 'Logout') or contains(text(), 'Log out')]",
            timeout=2)
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
    except Exception:
        return False


# ===================== Cookie 规范化 =====================
def normalize_cookies(cookies_data):
    if isinstance(cookies_data, str):
        return [
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": ".hax.co.id", "path": "/"},
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": ".hax.co.id", "path": "/vps-info"},
        ]

    if not isinstance(cookies_data, list):
        return []

    result = []
    for c in cookies_data:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = str(c.get("domain", ".hax.co.id"))
        if "hax.co.id" not in domain:
            continue
        if name in IGNORE_COOKIE_NAMES:
            continue

        nc = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": str(c.get("path", "/")),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                nc["expires"] = int(float(exp))
            except Exception:
                pass
        result.append(nc)

    seen = set()
    uniq = []
    for c in result:
        key = (c["name"], c["domain"], c["path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def _cookie_attr(c, attr, default=""):
    if isinstance(c, dict):
        return c.get(attr, default)
    return getattr(c, attr, default)


# ===================== requests 探测（CF 挑战感知 + 重试）=====================
def probe_cookie_with_requests(sess_value, retry=REQ_RETRY):
    proxies = get_proxies()
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://hax.co.id/login",
    }

    last_err = None
    for attempt in range(retry):
        try:
            r = req_lib.get(
                "https://hax.co.id/vps-info",
                cookies={"PHPSESSID": sess_value},
                headers=headers,
                proxies=proxies,
                allow_redirects=True,
                timeout=30,
            )
            text = r.text
            text_lower = text.lower()

            print(f"    [探测] 第{attempt+1}/{retry}次: HTTP {r.status_code}, "
                  f"final={r.url}, len={len(text)}", flush=True)

            is_cf_challenge = (
                "__cf$cv$params" in text_lower
                or "challenge-platform/scripts/jsd" in text_lower
                or ("cloudflare" in text_lower and "challenge" in text_lower
                    and len(text) < 3000)
            )

            has_valid_until = "Valid until" in text
            has_vps_title = "VPS Information" in text
            has_logout = "Logout" in text or "Log out" in text

            if has_valid_until or has_vps_title or has_logout:
                return "ok", f"服务端有效 (HTTP {r.status_code}, len={len(text)})"

            if is_cf_challenge:
                if attempt < retry - 1:
                    print(f"    [探测] 检测到 CF 挑战，等待 {CF_CHALLENGE_DELAY}s 后重试", flush=True)
                    time.sleep(CF_CHALLENGE_DELAY)
                    continue
                else:
                    return "cf", f"Cloudflare 挑战（重试 {retry} 次仍未通过）"

            has_redirect_to_login = ('http-equiv="refresh"' in text_lower
                                     and '/login' in text_lower)
            if has_redirect_to_login and not is_cf_challenge:
                return "expired", "session 过期（重定向到 /login）"

            if len(text) < 2000:
                if attempt < retry - 1:
                    print(f"    [探测] 短页面（{len(text)}B），等待 {CF_CHALLENGE_DELAY}s 后重试", flush=True)
                    time.sleep(CF_CHALLENGE_DELAY)
                    continue
                return "error", f"状态不明 (HTTP {r.status_code}, len={len(text)})"

            return "ok", f"服务端有效 (HTTP {r.status_code}, len={len(text)})"

        except Exception as e:
            last_err = str(e)
            if attempt < retry - 1:
                print(f"    [探测] 第{attempt+1}/{retry}次请求失败: {last_err[:80]}，"
                      f"{REQ_RETRY_DELAY}s 后重试", flush=True)
                time.sleep(REQ_RETRY_DELAY)

    return "error", f"requests 异常: {last_err[:100]}"


# ===================== Cookie 注入 =====================
def set_session_cookie(page, cookies_data):
    cookies_list = normalize_cookies(cookies_data)
    if not cookies_list:
        print("  [COOKIE] ⚠️ cookie 数据为空或格式不支持", flush=True)
        return False

    sess_value = None
    for c in cookies_list:
        if c["name"] == "PHPSESSID":
            sess_value = c["value"]
            break

    if not sess_value:
        print("  [COOKIE] ⚠️ 没有 PHPSESSID，无法注入", flush=True)
        return False

    print(f"  [COOKIE] 目标 PHPSESSID: {sess_value[:8]}...{sess_value[-4:]}", flush=True)

    status, info = probe_cookie_with_requests(sess_value)
    if status == "ok":
        print(f"  [COOKIE] ✅ requests 探测：{info}", flush=True)
    elif status == "cf":
        print(f"  [COOKIE] ⚠️ CF 挑战持续：{info}（仍尝试浏览器注入）", flush=True)
    elif status == "expired":
        print(f"  [COOKIE] ❌ requests 探测：{info}", flush=True)
        print(f"  [COOKIE] → 该 PHPSESSID 确实已过期，跳过该账号", flush=True)
        return False
    else:
        print(f"  [COOKIE] ❌ requests 探测：{info}", flush=True)
        return False

    if not safe_get(page, "https://hax.co.id/login", timeout=20):
        print(f"  [COOKIE] ❌ 访问 /login 三次都失败", flush=True)
        return False
    time.sleep(2)
    print(f"  [COOKIE] 当前页面: {page.url}", flush=True)

    try:
        page.set_cookies(cookies_list)
        print(f"  [COOKIE] ✅ page.set_cookies 调用成功", flush=True)
    except Exception as e:
        print(f"  [COOKIE] ❌ page.set_cookies 失败: {e}", flush=True)
        return False

    try:
        page.run_js(f"document.cookie = 'PHPSESSID={sess_value}; path=/; SameSite=Lax';")
        print(f"  [COOKIE] ✅ JS 注入完成", flush=True)
    except Exception as e:
        print(f"  [COOKIE] ⚠️ JS 注入失败: {e}", flush=True)

    return True


# ===================== reCAPTCHA 音频求解 =====================
def find_frame(page, keyword):
    try:
        for frame in page.get_frames():
            u = (frame.url or "").lower()
            if "recaptcha" in u and keyword in u:
                return frame
    except Exception:
        pass
    return None


def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            token = frame.run_js(
                "(() => { try { const els = document.querySelectorAll('textarea[name^=g-recaptcha-response]');"
                " for (const el of els) { if (el && el.value && el.value.length > 30) return el.value; }"
                " return ''; } catch(e) { return ''; } })()")
            if token and len(token) > 30:
                return True
    except Exception:
        pass
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "(() => { try { const el = document.querySelector('#recaptcha-anchor');"
                " return el ? (el.getAttribute('aria-checked') === 'true') : false; } catch(e) { return false; } })()")
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
        bframe.run_js("(() => { const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click(); })()")
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
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
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


def solve_recaptcha(page, timeout=90):
    debug_print("开始 solve_recaptcha")
    start_time = time.time()
    wait_anchor_deadline = time.time() + 30
    while time.time() < wait_anchor_deadline:
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
    if len(captcha_urls) < 2:
        for img in all_imgs:
            s = img.get('src', '')
            w, h = img.get('w', 0), img.get('h', 0)
            if s and not s.startswith('data:') and 'hax.co.id' in s:
                if w <= 50 and h <= 50:
                    captcha_urls.append(s)
    if len(captcha_urls) < 2:
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
            digits.append(int(first_char))
            print(f"  [CAPTCHA] URL提取数字: {first_char}", flush=True)
        else:
            print(f"  [CAPTCHA] URL提取失败，放弃本次提交", flush=True)
            return None
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
                    if (txt.length <= 3 && /[+\\-×÷*/xX]/.test(txt)
                        && els[e].querySelectorAll('img').length === 0) return txt;
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
    if op == "*":
        result = digits[0] * digits[1]
    elif op == "-":
        result = digits[0] - digits[1]
    else:
        result = digits[0] + digits[1]
    op_symbol = "×" if op == "*" else ("−" if op == "-" else "+")
    print(f"  [CAPTCHA] 算式: {digits[0]} {op_symbol} {digits[1]} = {result}", flush=True)
    return result


# ===================== 续期码获取 =====================
def get_renewal_code_from_telegram(bot_tokens, page, phone, bot_token, chat_id,
                                    timeout=1800, poll_interval=10):
    debug_print(f"进入 get_renewal_code_from_telegram，超时 {timeout}s")
    offsets = {}
    for bt in bot_tokens:
        try:
            proxies = get_proxies()
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            resp = (req_lib.get(url, timeout=10, proxies=proxies)
                    if proxies else req_lib.get(url, timeout=10))
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
        file_code = read_code_from_file()
        if file_code:
            print(f"  [CODE] 从文件读取到续期码（长度 {len(file_code)}），直接使用", flush=True)
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

        for bt in bot_tokens:
            offset = offsets.get(bt['token'], 0)
            try:
                proxies = get_proxies()
                url = (f"https://api.telegram.org/bot{bt['token']}/getUpdates"
                       f"?offset={offset}&timeout=5")
                resp = (req_lib.get(url, timeout=10, proxies=proxies)
                        if proxies else req_lib.get(url, timeout=10))
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
                                write_code_to_file(code)
                                return code, bt.get("label", bt['token'][-6:])
            except Exception:
                pass
        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)", flush=True)
    return "", None


# ===================== 广告关闭 =====================
def close_ads(page):
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
        var keepSelectors = ['.navbar', '.nav-', '[role="navigation"]', '.toast'];
        document.querySelectorAll('*').forEach(function(el) {
            var style = getComputedStyle(el);
            if (style.position === 'fixed' && parseInt(style.zIndex) > 999) {
                var keep = keepSelectors.some(function(sel) {
                    return el.matches(sel) || (el.closest && el.closest(sel));
                });
                if (!keep) el.remove();
            }
        });
    })();
    """
    try:
        page.run_js(js_remove)
        time.sleep(1)
    except Exception as e:
        debug_print(f"JS移除弹窗失败: {e}")


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
            except Exception:
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
            print("⚠️ 代理出口 IP 获取失败，仍尝试使用代理", flush=True)
    else:
        print("🔗 代理不可用，使用直连", flush=True)

    page = None
    try:
        debug_print("准备启动浏览器...")
        launch_args = {
            "headless": HEADLESS,
            "window_size": (1366, 768),
        }
        if proxies is not None:
            launch_args["proxy"] = PROXY_ADDR

        print(f"  [BROWSER] 正在启动浏览器...", flush=True)
        page = launch(**launch_args)

        debug_print("浏览器启动成功")

        if not session_token:
            print("  ⚠️ 无 session_token，跳过", flush=True)
            return "failed", {"step": "参数校验", "error": "缺少 session_token"}

        print("  [LOGIN] 尝试使用 session_token 快速登录...", flush=True)
        cookie_ok = set_session_cookie(page, session_token)
        if not cookie_ok:
            print("  ❌ Cookie 注入失败，跳过该账号", flush=True)
            notify_failed(phone, "Cookie 注入", "cookie 无效或 CF 挑战", bot_token, chat_id)
            return "failed", {"step": "Cookie 注入", "error": "cookie 无效或 CF 挑战"}

        debug_print("Cookie 注入完成，验证登录态")
        time.sleep(1)
        if not safe_get(page, "https://hax.co.id/vps-info", timeout=20):
            print("  ❌ 访问 vps-info 失败", flush=True)
            notify_failed(phone, "访问 vps-info", "网络故障", bot_token, chat_id)
            return "failed", {"step": "访问 vps-info", "error": "网络故障"}
        page.wait(2)

        if not safe_get(page, "https://hax.co.id/vps-info", timeout=20):
            print("  ❌ 再次访问 vps-info 失败", flush=True)
            notify_failed(phone, "访问 vps-info", "网络故障", bot_token, chat_id)
            return "failed", {"step": "访问 vps-info", "error": "网络故障"}
        page.wait(2)

        if not is_logged_in(page):
            print("  ❌ Cookie 未生效，跳过该账号", flush=True)
            try:
                snippet = page.run_js("document.body.innerText.substring(0, 300)") or ""
                print(f"  [DEBUG] 页面片段: {snippet[:200]}", flush=True)
            except Exception:
                pass
            notify_failed(phone, "登录验证", "Cookie 无效", bot_token, chat_id)
            return "failed", {"step": "登录验证", "error": "cookie 无效"}

        print("  ✅ Cookie 登录成功", flush=True)
        print("  ✅ 登录成功，开始续期流程", flush=True)

        handle_consent(page)
        close_ads(page)

        try:
            take_screenshot(page, f"login_success_{phone}.png", bot_token, chat_id,
                            f"✅ 登录成功 - {phone}")
        except Exception as e:
            print(f"  [截图] 登录截图失败: {e}", flush=True)

        if "hax.co.id/vps-info" not in (page.url or ""):
            safe_get(page, "https://hax.co.id/vps-info", timeout=15)
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

        close_ads(page)

        try:
            take_screenshot(page, f"renew_vps_{phone}.png", bot_token, chat_id,
                            f"🔄 已点击 Renew VPS - {phone}")
        except Exception as e:
            print(f"  [截图] Renew VPS 截图失败: {e}", flush=True)

        debug_print("开始获取续期码")
        code = read_code_from_file()
        source = "file"
        if code:
            print(f"  [CODE] 直接从文件使用续期码（长度 {len(code)}）", flush=True)
        else:
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
                timeout=1800, poll_interval=10)
            if not code:
                try:
                    take_screenshot(page, f"timeout_{phone}.png", bot_token, chat_id,
                                    f"⏰ 续期码超时 - {phone}")
                except Exception:
                    pass
                raise RuntimeError("未获取到续期码")

        print(f"  [CODE] 使用原始码（长度 {len(code)}）", flush=True)

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

        close_ads(page)

        captcha_result = solve_arithmetic_captcha(page)
        if captcha_result is not None:
            code_input = page.ele("css:#captcha")
            if code_input:
                code_input.input(str(captcha_result), clear=True)
                print(f"  [CAPTCHA] 输入结果: {captcha_result}", flush=True)

        debug_print("填入续期码")
        vcode_input = None
        for selector in ["css:input.form-control:not(#captcha)",
                         "css:input[name=code]", "css:input#code"]:
            try:
                vcode_input = page.ele(selector)
                if vcode_input:
                    break
            except Exception:
                pass
        if vcode_input:
            try:
                page.run_js(
                    "(() => { const el = document.querySelector('input.form-control:not(#captcha)');"
                    " if (el) el.scrollIntoView({behavior: 'instant', block: 'center'}); })()")
                page.wait(1)
            except Exception:
                pass
            vcode_input.input(code, clear=True)
            print(f"  [CODE] 输入 renewal code（长度 {len(code)}）", flush=True)

        debug_print("开始 reCAPTCHA")
        print("  [reCAPTCHA] 处理音频验证...", flush=True)
        recaptcha_solved = solve_recaptcha(page, timeout=90)
        if not recaptcha_solved:
            print("  [reCAPTCHA] 自动解决失败，等待用户手动处理...", flush=True)
            page.wait(60)
            recaptcha_solved = is_recaptcha_solved(page)

        debug_print("提交续期")
        print("  [SUBMIT] 提交续期...", flush=True)
        close_ads(page)

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
            raise RuntimeError("未找到提交按钮")
        try:
            submit_btn.click_self(by_js=True)
        except Exception:
            submit_btn.click_self()
        print("  [SUBMIT] 已点击提交，等待结果...", flush=True)
        time.sleep(60)

        debug_print("检查续期结果")
        for _ in range(3):
            close_ads(page)
            time.sleep(1)
        page.run_js("""
            document.querySelectorAll('.overlay, .modal, .popup, [class*="overlay"],
                                       [class*="modal"], [class*="popup"]')
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
            "renewed successfully", "renewal successful",
            "subscription renewed", "subscription successfully",
            "续期成功", "renewed",
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
                take_screenshot(page, f"error_{phone}.png", bot_token, chat_id,
                                f"⚠️ 异常 - {phone}\n{e}")
            except Exception:
                pass
        notify_failed(phone, "执行异常", str(e), bot_token, chat_id)
        return "failed", {"step": "执行异常", "error": str(e)}
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass


# ===================== 主入口 =====================
if __name__ == "__main__":
    print("#########################", flush=True)
    print("   HAX 自动续期 (CF 挑战感知版 - 等待 105s)", flush=True)
    print("#########################", flush=True)
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON", flush=True)
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号", flush=True)
    print(f"✅ 跳过阈值: {SKIP_THRESHOLD_HOURS} 小时", flush=True)
    print(f"✅ 最大轮数: {MAX_RENEW_ROUNDS}", flush=True)
    print(f"✅ 进度通知: {'开启' if NOTIFY_PROGRESS else '关闭'}", flush=True)
    print(f"✅ 探测重试: {REQ_RETRY} 次 / CF 挑战延迟: {CF_CHALLENGE_DELAY}s", flush=True)

    total = len(ACCOUNTS)

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

    success_set = set()
    skipped_set = set()
    pending = [(i, acc) for i, acc in enumerate(ACCOUNTS, 1)]
    round_no = 0

    while pending and round_no < MAX_RENEW_ROUNDS:
        round_no += 1
        print(f"\n{'#'*60}", flush=True)
        print(f"  第 {round_no}/{MAX_RENEW_ROUNDS} 轮，待处理 {len(pending)} 个账号", flush=True)
        print(f"{'#'*60}", flush=True)

        if round_no > 1 and NOTIFY_PROGRESS:
            try:
                notify_round_start(round_no, MAX_RENEW_ROUNDS,
                                   [a for _, a in pending],
                                   summary_bot, summary_chat)
            except Exception as e:
                print(f"  [NOTIFY] 轮次开始通知失败: {e}", flush=True)

        failed_in_round = []
        accounts_this_round = pending

        for i_in_round, (orig_idx, acc) in enumerate(accounts_this_round, 1):
            phone = acc.get("phone", f"account_{orig_idx}")
            print(f"\n===== [第{round_no}轮] 处理 {i_in_round}/{len(accounts_this_round)}: {phone} =====", flush=True)

            status = "failed"
            info = {"step": "未知", "error": "未知"}
            try:
                status, info = renew_account(acc)
            except Exception as e:
                print(f"  ⚠️ 账号处理异常: {e}", flush=True)
                traceback.print_exc()
                status = "failed"
                info = {"step": "主循环异常", "error": str(e)}

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

            remaining_in_round = accounts_this_round[i_in_round:]
            pending_phones = (
                [a.get("phone", "?") for _, a in remaining_in_round] +
                [a.get("phone", "?") for _, a in failed_in_round]
            )

            if NOTIFY_PROGRESS:
                try:
                    notify_progress(
                        round_no=round_no, max_rounds=MAX_RENEW_ROUNDS,
                        current_idx=i_in_round, total=len(accounts_this_round),
                        phone=phone, status_emoji=emoji, status_text=status_text,
                        pending_list=pending_phones,
                        bot_token=acc.get("bot_token", "") or summary_bot,
                        chat_id=acc.get("chat_id", "") or summary_chat)
                except Exception as e:
                    print(f"  [NOTIFY] 进度通知失败: {e}", flush=True)

            if i_in_round < len(accounts_this_round):
                delay = random.randint(60, 120)
                print(f"  等待 {delay} 秒后处理下一个账号（避免 CF 挑战）...", flush=True)
                time.sleep(delay)

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

            if will_retry:
                delay = random.randint(90, 180)
                print(f"  轮次间隔等待 {delay} 秒（给 CF 冷却）...", flush=True)
                time.sleep(delay)
        else:
            break

    final_failed = [a.get("phone", "?") for _, a in pending]

    try:
        notify_all_done(total=total, success=len(success_set),
                        failed=len(final_failed), skipped=len(skipped_set),
                        failed_list=final_failed,
                        bot_token=summary_bot, chat_id=summary_chat)
    except Exception as e:
        print(f"  [NOTIFY] 总结通知失败: {e}", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"最终结果: 成功 {len(success_set)} / 跳过 {len(skipped_set)} / 失败 {len(final_failed)} / 共 {total} 个账号", flush=True)
    print(f"总轮数: {round_no}", flush=True)
    print(f"{'='*60}", flush=True)
