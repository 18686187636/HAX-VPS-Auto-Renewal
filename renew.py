#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX VPS Auto-Renewal Script (Cookie Login + Proxy + GitHub Actions)
Features:
- Multi-account via ACCOUNTS_JSON env
- Cookie login (session_token)
- Socks5 proxy support
- Arithmetic CAPTCHA solver
- Audio reCAPTCHA solver (Google STT + backup API)
- Telegram notifications
- Headless browser mode
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
import subprocess
from datetime import datetime, timezone, timedelta

import requests
from DrissionPage import ChromiumPage, ChromiumOptions
from PIL import Image

# 可选语音识别
try:
    import speech_recognition as sr
    from pydub import AudioSegment
except ImportError:
    sr = None
    AudioSegment = None

# ===================== 环境变量读取 =====================
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON", "[]")
try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
except Exception:
    ACCOUNTS = []
    print("❌ 无法解析 ACCOUNTS_JSON，请检查格式")

PROXY_SERVER = os.getenv("PROXY_SERVER", "")          # socks5://127.0.0.1:1080
IS_PROXY = os.getenv("IS_PROXY", "false").lower() == "true"
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# 续期码文件（可缓存）
CODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renewal_code.txt")
TG_RENEWAL_PATTERN = re.compile(r'[A-Za-z0-9+/=]{32,}')

# ===================== 工具函数 =====================
def get_beijing_time():
    bj_tz = timezone(timedelta(hours=8))
    return datetime.now(bj_tz).strftime("%Y-%m-%d %H:%M:%S")

def send_telegram_message(text, bot_token, chat_id):
    if not bot_token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if PROXY_SERVER else None
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10, proxies=proxies)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"  [TG] 发送失败: {e}")
        return False

def notify_success(phone, expiry_date, bot_token, chat_id):
    msg = f"✅ <b>VPS 续期成功</b>\n\nHAX\n📱 {phone}\n📅 {expiry_date or '未知'}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def notify_failed(phone, step, error, bot_token, chat_id):
    msg = f"❌ <b>VPS 续期失败</b>\n\nHAX\n📱 {phone}\n📍 {step}\n⚠️ {error}\n⏰ {get_beijing_time()}"
    send_telegram_message(msg, bot_token, chat_id)

def get_renewal_code_from_telegram(bot_tokens, timeout=1800, poll_interval=10):
    """
    从多个 Bot 中轮询获取续期码（Base64 格式）
    返回 (code, source_bot_label)
    """
    code = ""
    offsets = {}
    for bt in bot_tokens:
        try:
            url = f"https://api.telegram.org/bot{bt['token']}/getUpdates"
            proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if PROXY_SERVER else None
            resp = requests.get(url, timeout=10, proxies=proxies)
            data = resp.json()
            if data.get("ok") and data.get("result"):
                offsets[bt['token']] = max(u["update_id"] for u in data["result"]) + 1
            else:
                offsets[bt['token']] = 0
        except Exception:
            offsets[bt['token']] = 0

    elapsed = 0
    while elapsed < timeout:
        for bt in bot_tokens:
            offset = offsets.get(bt['token'], 0)
            try:
                url = f"https://api.telegram.org/bot{bt['token']}/getUpdates?offset={offset}&timeout=5"
                proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if PROXY_SERVER else None
                resp = requests.get(url, timeout=10, proxies=proxies)
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

    return code, None

# ===================== reCAPTCHA 音频求解（无头适配） =====================
def find_frame(page, keyword):
    try:
        for frame in page.get_frames():
            if "recaptcha" in (frame.url or "").lower() and keyword in (frame.url or "").lower():
                return frame
    except Exception:
        pass
    return None

def is_recaptcha_solved(page):
    # 检查 g-recaptcha-response
    try:
        for frame in page.get_frames():
            token = frame.run_js("(() => { const el = document.querySelector('textarea[name=g-recaptcha-response]'); return el ? el.value : ''; })()")
            if token and len(token) > 30:
                return True
    except:
        pass
    # 检查 checkbox aria-checked
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
    # 已经音频模式？
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
    # JS fallback
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if PROXY_SERVER else None
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))
    for audio_url in urls:
        try:
            r = requests.get(audio_url, headers=headers, timeout=30, proxies=proxies)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except:
            pass
    return None

def recognize_audio(mp3_path):
    # Google STT
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
    # 备用 API（从环境变量读取）
    api_url = os.getenv("AUDIO_API_URL")
    if api_url:
        try:
            with open(mp3_path, "rb") as f:
                files = {"audio": f}
                proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if PROXY_SERVER else None
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
    # 提取图片 URL
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
    # 提取数字（URL 中最后一个 '-' 后的第一个字符）
    digits = []
    for url in captcha_urls[:2]:
        after = url.rsplit('-', 1)[-1] if '-' in url else ''
        first = after[0] if after and after[0].isdigit() else '0'
        digits.append(int(first))
    # 提取运算符
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

# ===================== 单账号续期主流程（Cookie 登录） =====================
def renew_account(account):
    phone = account.get("phone")
    session_token = account.get("session_token")   # 从 ACCOUNTS_JSON 中传入
    bot_token = account.get("bot_token")
    chat_id = account.get("chat_id")

    if not session_token:
        print(f"  ⚠️ 账号 {phone} 缺少 session_token，跳过")
        return False

    print(f"\n{'='*60}\n  续期: {phone}\n{'='*60}")

    page = None
    try:
        # ---------- 浏览器配置 ----------
        co = ChromiumOptions()
        if HEADLESS:
            co.headless(True)
        if PROXY_SERVER:
            co.set_proxy(PROXY_SERVER)   # DrissionPage 支持 socks5
        co.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = ChromiumPage(co)

        # ---------- 注入 Cookie ----------
        page.get("https://hax.co.id/login")   # 先访问域名建立会话
        page.set_cookies([{"name": "session", "value": session_token, "domain": "hax.co.id"}])
        page.get("https://hax.co.id/vps-info")   # 直接进入信息页
        page.wait.doc_loaded(timeout=15)

        # 检查是否登录成功
        if "login" in page.url.lower():
            raise RuntimeError("Cookie 无效，未登录")

        print("  ✅ Cookie 登录成功")

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
        # 构造所有 Bot 列表（包括当前账号和其他账号）
        all_bots = []
        for acc in ACCOUNTS:
            t = acc.get("bot_token")
            if t:
                all_bots.append({"token": t, "label": f"...{t[-6:]}"})
        # 去重
        seen = set()
        unique_bots = []
        for b in all_bots:
            if b["token"] not in seen:
                seen.add(b["token"])
                unique_bots.append(b)

        code, source = get_renewal_code_from_telegram(unique_bots, timeout=1800, poll_interval=10)
        if not code:
            raise RuntimeError("未获取到续期码")

        # Base64 解码（HAX 发的通常为 Base64）
        try:
            decoded = base64.b64decode(code).decode('utf-8')
            print(f"  [CODE] 解码后: {decoded[:20]}***")
        except:
            decoded = code   # 如果已经是明文
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
            print("  [reCAPTCHA] 自动失败，等待手动（无头不可手动）")
            # 这里可考虑重试或放弃
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
    print("   HAX 自动续期 (Cookie + 代理)")
    print("#########################")

    if IS_PROXY and PROXY_SERVER:
        print(f"🔗 使用代理: {PROXY_SERVER}")
    else:
        print("🔗 无代理")

    if not ACCOUNTS:
        print("❌ 未加载账号，请设置 ACCOUNTS_JSON 环境变量")
        sys.exit(1)

    print(f"✅ 加载了 {len(ACCOUNTS)} 个账号\n")

    success = 0
    for idx, acc in enumerate(ACCOUNTS, 1):
        print(f"\n============================== 处理第 {idx}/{len(ACCOUNTS)} 个账号 ==============================")
        if renew_account(acc):
            success += 1
        time.sleep(random.randint(10, 30))  # 避免过快

    print(f"\n{'='*60}\n完成: {success}/{len(ACCOUNTS)} 个账号续期成功\n{'='*60}")
