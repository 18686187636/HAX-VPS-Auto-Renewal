#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX VPS Auto-Renewal
- Cookie 快速登录（通过 JS 注入 PHPSESSID）
- 若 Cookie 失效，自动回退 Telegram OAuth 登录（使用 find_frame + set_active）
- 代理自动检测 + 出口 IP 验证
- 算术验证码 + 音频 reCAPTCHA 识别
- 多 Bot 轮询获取续期码
- Telegram 通知
- 支持 GitHub Actions 无头运行
"""
import os
import sys
import json
import time
import re
import base64
import html
import tempfile
import random
import socket
from datetime import datetime, timezone, timedelta

import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# ---------- 可选语音识别库 ----------
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
            resp = requests.get(url, proxies=proxies, timeout=10)
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
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                             timeout=10, proxies=proxies)
        return resp.json().get("ok", False)
    except Exception:
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
            return resp.json().get("ok", False)
        except Exception:
            return False

def notify_success(phone, expiry, bot_token, chat_id):
    msg = f"✅ <b>VPS 续期成功</b>\n\nHAX\n📱 {phone}\n📅 {expiry or '未知'}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def notify_failed(phone, step, error, bot_token, chat_id):
    msg = f"❌ <b>VPS 续期失败</b>\n\nHAX\n📱 {phone}\n📍 {step}\n⚠️ {error}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

# ===================== 查找 frame（通用） =====================
def find_frame_by_keyword(page, keyword):
    """查找包含特定关键词的 frame"""
    try:
        for frame in page.get_frames():
            if keyword in (frame.url or "").lower():
                return frame
    except Exception:
        pass
    return None

# ===================== Telegram OAuth 登录（使用 set_active） =====================
def login_with_telegram(page, phone):
    """使用 Telegram OAuth 登录 HAX，通过 find_frame_by_keyword + set_active 切换标签页"""
    print(f"  [LOGIN] 尝试 Telegram OAuth 登录: {phone}")
    try:
        page.get("https://hax.co.id/login")
        page.wait.doc_loaded(timeout=20)
        page.wait(5)

        # 获取 Telegram OAuth iframe
        frame = find_frame_by_keyword(page, "oauth.telegram.org")
        if not frame:
            raise RuntimeError("未找到 Telegram OAuth iframe")

        # 在 frame 内点击登录按钮
        btn = frame.ele("css:button.tgme_widget_login_button", timeout=5)
        if not btn:
            raise RuntimeError("未找到 Telegram 登录按钮")
        btn.click()
        print("  [LOGIN] 点击 Telegram 登录按钮")
        page.wait(3)

        # 查找新打开的 OAuth 标签页
        original_tab = page.tab
        oauth_tab_id = None
        for tab_id in page.tab_ids:
            if tab_id == original_tab.id:
                continue
            tab = page.get_tab(tab_id)
            if "oauth.telegram.org" in (tab.url or ""):
                oauth_tab_id = tab_id
                break
        if not oauth_tab_id:
            raise RuntimeError("未找到 OAuth tab")

        # 切换到 OAuth 标签页（使用 set_active）
        oauth_tab = page.get_tab(oauth_tab_id)
        oauth_tab.set_active()
        oauth_tab.wait.doc_loaded(timeout=20)

        # 输入手机号
        phone_input = oauth_tab.ele("css:#login-phone-code", timeout=5)
        if not phone_input:
            raise RuntimeError("未找到手机号输入框")
        phone_input.input(phone, clear=True)
        print(f"  [LOGIN] 输入手机号: {phone}")
        time.sleep(2)

        # 点击继续
        continue_btn = oauth_tab.ele("text:继续") or oauth_tab.ele("css:button[type=submit]") or oauth_tab.ele("css:button")
        if continue_btn:
            continue_btn.click()
            print("  [LOGIN] 点击继续")

        # 切回主标签页
        original_tab.set_active()

        # 等待主标签页跳转回 vps-info
        for _ in range(60):
            time.sleep(2)
            if "hax.co.id/vps-info" in (page.url or ""):
                print("  [LOGIN] 已跳转到 VPS 信息页")
                return True
        raise RuntimeError("登录超时，未跳转到 VPS 信息页")

    except Exception as e:
        print(f"  [LOGIN] 失败: {e}")
        return False

# ===================== 续期码获取（多 Bot 轮询） =====================
def get_renewal_code_from_telegram(bot_tokens, timeout=1800, poll_interval=10):
    offsets = {}
    for bt in bot_tokens:
        try:
            proxies = get_proxies()
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            resp = requests.get(url, timeout=10, proxies=proxies) if proxies else requests.get(url, timeout=10)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception:
            offsets[bt['token']] = 0
    elapsed = 0
    code = ""
    while elapsed < timeout:
        for bt in bot_tokens:
            offset = offsets.get(bt['token'], 0)
            try:
                proxies = get_proxies()
                url = f"https://api.telegram.org/bot{bt['token']}/getUpdates?offset={offset}&timeout=5"
                resp = (requests.get(url, timeout=10, proxies=proxies) if proxies else requests.get(url, timeout=10))
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
                                with open(CODE_FILE, "w") as f:
                                    f.write(code)
                                return code, bt.get("label", bt['token'][-6:])
            except Exception:
                pass
        if code:
            break
        time.sleep(poll_interval)
        elapsed += poll_interval
        if elapsed % 60 < poll_interval:
            print(f"  [CODE] 等待中... ({elapsed//60} 分钟)")
    return "", None

# ===================== reCAPTCHA 音频求解 =====================
def find_frame(page, keyword):
    try:
        for frame in page.get_frames():
            if "recaptcha" in (frame.url or "").lower() and keyword in (frame.url or "").lower():
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    try:
        for frame in page.get_frames():
            token = frame.run_js("(() => { const el = document.querySelector('textarea[name=g-recaptcha-response]'); return el ? el.value : ''; })()")
            if token and len(token) > 30:
                return True
    except:
        pass
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js("(() => { const el = document.querySelector('#recaptcha-anchor'); return el ? (el.getAttribute('aria-checked') === 'true') : false; })()")
            if checked:
                return True
        except:
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
    page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0)).wait(random.uniform(0.2, 0.5))
    try:
        checkbox.click()
    except:
        checkbox.click(by_js=True)
    time.sleep(3)

def switch_to_audio(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        inp = bframe.ele("#audio-response", timeout=1)
        if inp and inp.states.is_displayed:
            return True
    except:
        pass
    for _ in range(3):
        try:
            btn = bframe.ele("#recaptcha-audio-button", timeout=3)
            if btn:
                btn.click()
                time.sleep(3)
                inp = bframe.ele("#audio-response", timeout=1)
                if inp and inp.states.is_displayed:
                    return True
        except:
            pass
    try:
        bframe.run_js("document.querySelector('#recaptcha-audio-button')?.click()")
        time.sleep(3)
        inp = bframe.ele("#audio-response", timeout=1)
        if inp and inp.states.is_displayed:
            return True
    except:
        pass
    return False

def get_audio_url(page):
    bframe = find_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele(".rc-audiochallenge-tdownload-link", timeout=1) or bframe.ele(".rc-audiochallenge-ndownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = bframe.ele("#audio-source", timeout=1)
            if audio:
                src = audio.attr("src")
                if src and len(src) > 10:
                    return html.unescape(src)
        except:
            pass
        time.sleep(1)
    return None

def download_audio(url):
    proxies = get_proxies()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            resp = requests.get(audio_url, headers=headers, timeout=30, proxies=proxies)
            resp.raise_for_status()
            if len(resp.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(resp.content)
            return path
        except:
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
            os.remove(wav_path)
            if text:
                return text
        except Exception as e:
            print(f"  [STT] Google 失败: {e}")
    api_url = os.getenv("AUDIO_API_URL")
    if api_url:
        try:
            proxies = get_proxies()
            with open(mp3_path, "rb") as f:
                files = {"audio": f}
                resp = requests.post(api_url, files=files, timeout=30, proxies=proxies)
                data = resp.json()
                text = data.get("text") or data.get("result") or data.get("data")
                if text:
                    return text
        except:
            pass
    return None

def solve_recaptcha(page, timeout=90):
    start = time.time()
    while time.time() - start < timeout:
        if is_recaptcha_solved(page):
            return True
        try:
            click_recaptcha_checkbox(page)
        except Exception as e:
            print(f"  [reCAPTCHA] 点击复选框失败: {e}")
            time.sleep(2)
            continue
        time.sleep(2)
        if is_recaptcha_solved(page):
            return True
        if not switch_to_audio(page):
            time.sleep(2)
            continue
        time.sleep(random.uniform(2, 4))
        audio_url = get_audio_url(page)
        if not audio_url:
            time.sleep(random.uniform(3, 6))
            continue
        mp3_path = download_audio(audio_url)
        if not mp3_path:
            time.sleep(random.uniform(3, 6))
            continue
        text = recognize_audio(mp3_path)
        try:
            os.remove(mp3_path)
        except:
            pass
        if not text:
            time.sleep(random.uniform(3, 6))
            continue
        print(f"  [reCAPTCHA] 识别结果: {text}")
        bframe = find_frame(page, "bframe")
        if bframe:
            inp = bframe.ele("#audio-response", timeout=2)
            if inp:
                inp.click()
                inp.clear()
                inp.input(text)
                time.sleep(random.uniform(0.5, 1.5))
                verify = bframe.ele("#recaptcha-verify-button", timeout=2)
                if verify:
                    verify.click()
                    time.sleep(5)
        if is_recaptcha_solved(page):
            return True
        else:
            print("  [reCAPTCHA] 验证未通过，重试...")
    return False

# ===================== 算术验证码 =====================
def solve_arithmetic_captcha(page):
    print("  [CAPTCHA] 识别算术验证码...")
    page.wait(3)
    img_data = page.run_js("""(() => {
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
        imgs = json.loads(img_data)
    except:
        imgs = []
    captcha_urls = []
    for img in imgs:
        s = img.get('src', '')
        w, h = img.get('w', 0), img.get('h', 0)
        if 'hax.co.id/img/temp/' in s and 15 <= w <= 50 and 15 <= h <= 50:
            captcha_urls.append(s)
    if len(captcha_urls) < 2:
        for img in imgs:
            s = img.get('src', '')
            w, h = img.get('w', 0), img.get('h', 0)
            if s and 'hax.co.id' in s and not s.startswith('data:') and w <= 50 and h <= 50:
                captcha_urls.append(s)
    if len(captcha_urls) < 2:
        print("  [CAPTCHA] 未找到足够图片，使用默认值 0")
        return 0
    digits = []
    for url in captcha_urls[:2]:
        after = url.rsplit('-', 1)[-1] if '-' in url else ''
        first = after[0] if after and after[0].isdigit() else '0'
        digits.append(int(first))
    op_text = page.run_js("""(() => {
        const groups = document.querySelectorAll('.form-group.row');
        for (let g of groups) {
            const imgs = g.querySelectorAll('img');
            if (imgs.length >= 2) {
                const walker = document.createTreeWalker(g, NodeFilter.SHOW_TEXT, null, false);
                while (walker.nextNode()) {
                    const txt = walker.currentNode.textContent.trim();
                    if (txt.length <= 3 && /[+\\-×÷*/xX]/.test(txt)) return txt;
                }
            }
        }
        return '';
    })()""")
    op = "+"
    if any(c in op_text for c in "×*xX"):
        op = "*"
    elif any(c in op_text for c in "-−－"):
        op = "-"
    result = eval(f"{digits[0]} {op} {digits[1]}")
    print(f"  [CAPTCHA] 算式: {digits[0]} {op} {digits[1]} = {result}")
    return result

# ===================== 设置 Cookie =====================
def set_session_cookie(page, session_token):
    try:
        page.run_js(f"document.cookie = 'PHPSESSID={session_token}; path=/; domain=.hax.co.id';")
        print("  [COOKIE] 通过 JS 注入成功")
        return True
    except Exception as e:
        print(f"  [COOKIE] JS 注入失败: {e}")
    try:
        page.set_cookies([{"name": "PHPSESSID", "value": session_token, "domain": ".hax.co.id", "path": "/"}])
        print("  [COOKIE] 通过 set_cookies 成功")
        return True
    except Exception:
        pass
    try:
        page.cookies.set("PHPSESSID", session_token, domain=".hax.co.id", path="/")
        print("  [COOKIE] 通过 cookies.set 成功")
        return True
    except Exception:
        pass
    try:
        page.set_cookie({"name": "PHPSESSID", "value": session_token, "domain": ".hax.co.id", "path": "/"})
        print("  [COOKIE] 通过 set_cookie 成功")
        return True
    except Exception:
        pass
    print("  [COOKIE] 所有方式均失败")
    return False

# ===================== 单账号续期主流程 =====================
def renew_account(account):
    phone = account.get("phone")
    session_token = account.get("session_token")
    bot_token = account.get("bot_token")
    chat_id = account.get("chat_id")

    if not phone:
        print("  ⚠️ 账号缺少手机号，跳过")
        return False

    print(f"\n{'='*60}\n  续期: {phone}\n{'='*60}")

    proxies = get_proxies()
    if proxies:
        print(f"🔗 代理地址: {PROXY_ADDR}")
        ok, ip = check_proxy_ip(proxies)
        if ok:
            print(f"📍 代理出口 IP: {ip}")
        else:
            print("⚠️ 代理出口 IP 获取失败，将使用直连")
            proxies = None
    else:
        print("🔗 代理不可用，使用直连")

    page = None
    try:
        co = ChromiumOptions()
        if HEADLESS:
            co.headless(True)
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--disable-gpu')
            co.set_argument('--headless=new')
        if proxies is not None:
            co.set_proxy(PROXY_ADDR)
        co.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = ChromiumPage(co)

        # ---------- 尝试 Cookie 登录 ----------
        login_success = False
        if session_token:
            print("  [LOGIN] 尝试使用 session_token 快速登录...")
            page.get("https://hax.co.id/login")
            set_session_cookie(page, session_token)
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            if "login" not in page.url.lower():
                print("  ✅ Cookie 登录成功")
                login_success = True
            else:
                print("  ⚠️ Cookie 无效或已过期")

        if not login_success:
            print("  [LOGIN] 执行 Telegram OAuth 登录...")
            login_success = login_with_telegram(page, phone)
            if not login_success:
                raise RuntimeError("Telegram 登录失败")

        print("  ✅ 登录成功，开始续期流程")

        # ---------- 处理广告 ----------
        time.sleep(3)
        page.actions.press("Escape").perform()
        for kw in ["Close", "close", "×"]:
            el = page.ele(f'xpath://*[contains(text(), "{kw}")]')
            if el and el.is_displayed:
                el.click()
                break
        time.sleep(2)

        # ---------- 导航到续期 ----------
        vps_menu = page.ele("css:a.nav-link.dropdown-toggle", timeout=5)
        if not vps_menu:
            raise RuntimeError("未找到 VPS 下拉菜单")
        vps_menu.click()
        time.sleep(2)
        renew_btn = page.ele('css:a.dropdown-item[href="/vps-renew/"]', timeout=5)
        if not renew_btn:
            raise RuntimeError("未找到 VPS Renew 按钮")
        renew_btn.click(by_js=True)
        time.sleep(5)

        # ---------- 填写表单 ----------
        web_input = page.ele("css:#web_address")
        if web_input:
            web_input.input("hax.co.id", clear=True)
        agreement = page.ele('css:input[name="agreement"][value="yes"]')
        if agreement and not agreement.is_checked:
            agreement.click(by_js=True)

        print("  [CF] 等待 CloudFlare 验证 (60s)...")
        time.sleep(60)

        renew_vps = page.ele("css:button[name=submit_button][type=button].btn-primary", timeout=5)
        if not renew_vps:
            raise RuntimeError("未找到 Renew VPS 按钮")
        renew_vps.click(by_js=True)
        time.sleep(5)

        # ---------- 获取续期码 ----------
        print("  [CODE] 等待 @HaxTG_bot 发送续期码...")
        all_bots = []
        seen = set()
        for acc in ACCOUNTS:
            t = acc.get("bot_token")
            if t and t not in seen:
                seen.add(t)
                all_bots.append({"token": t, "label": f"...{t[-6:]}"})
        if bot_token and bot_token not in seen:
            all_bots.insert(0, {"token": bot_token, "label": f"...{bot_token[-6:]}"})

        code, source = get_renewal_code_from_telegram(all_bots, timeout=1800, poll_interval=10)
        if not code:
            raise RuntimeError("未获取到续期码")

        try:
            decoded = base64.b64decode(code).decode('utf-8')
            print(f"  [CODE] 解码后: {decoded[:20]}***")
        except:
            decoded = code
            print(f"  [CODE] 非 Base64，直接使用: {decoded[:20]}***")

        # ---------- 进入续期码输入页 ----------
        code_link = page.ele('css:a.btn[href="/vps-renew-code"]', timeout=5) or page.ele("text:INPUT RENEW CODE")
        if not code_link:
            raise RuntimeError("未找到 INPUT RENEW CODE 按钮")
        code_link.click(by_js=True)
        page.wait.doc_loaded(timeout=15)
        time.sleep(3)

        # ---------- 算术验证码 ----------
        captcha_result = solve_arithmetic_captcha(page)
        captcha_input = page.ele("css:#captcha")
        if captcha_input:
            captcha_input.input(str(captcha_result), clear=True)

        # ---------- 填入续期码 ----------
        vcode_input = page.ele("css:input.form-control:not(#captcha)", timeout=5) or page.ele("css:input[name=code]")
        if vcode_input:
            vcode_input.input(decoded, clear=True)
            print(f"  [CODE] 填入续期码")

        # ---------- reCAPTCHA ----------
        print("  [reCAPTCHA] 开始音频验证...")
        if not solve_recaptcha(page, timeout=90):
            print("  [reCAPTCHA] 自动失败，尝试重试一次...")
            if not solve_recaptcha(page, timeout=60):
                raise RuntimeError("reCAPTCHA 未通过")

        # ---------- 提交 ----------
        submit = page.ele("css:button[name=submit_button]", timeout=5) or page.ele("css:button.btn-primary")
        if not submit:
            raise RuntimeError("未找到提交按钮")
        submit.click(by_js=True)
        time.sleep(60)

        # ---------- 检查结果 ----------
        page.wait.doc_loaded(timeout=15)
        body = page.run_js("document.body.innerText") or ""
        if any(kw in body.lower() for kw in ["renewed successfully", "续期成功", "subscription renewed"]):
            expiry = re.search(r"until\s+([A-Za-z]+\s+\d{1,2},?\s*\d{4})", body)
            expiry_date = expiry.group(1) if expiry else "未知"
            notify_success(phone, expiry_date, bot_token, chat_id)
            return True
        else:
            error = "页面未显示成功"
            if "captcha" in body.lower():
                error = "Captcha 验证失败"
            notify_failed(phone, "结果页", error, bot_token, chat_id)
            return False

    except Exception as e:
        print(f"  ❌ 异常: {e}")
        notify_failed(phone, "执行异常", str(e), bot_token, chat_id)
        return False
    finally:
        if page:
            try:
                page.quit()
            except:
                pass

# ===================== 主入口 =====================
if __name__ == "__main__":
    print("#########################")
    print("   HAX 自动续期 (Cookie + 回退登录)")
    print("#########################")
    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON")
        sys.exit(1)
    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号")
    success = 0
    for idx, acc in enumerate(ACCOUNTS, 1):
        print(f"\n============================== 处理第 {idx}/{len(ACCOUNTS)} 个账号 ==============================")
        try:
            if renew_account(acc):
                success += 1
        except Exception as e:
            print(f"  ⚠️ 账号处理异常: {e}")
        time.sleep(random.randint(10, 30))
    print(f"\n{'='*60}\n完成: {success}/{len(ACCOUNTS)} 个账号续期成功\n{'='*60}")
