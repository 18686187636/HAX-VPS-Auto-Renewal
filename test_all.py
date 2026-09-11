#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAX Cookie 完整测试（合并版）
依次执行 3 个测试：
  1. requests 验证 PHPSESSID（不启动浏览器，30 秒出结果）
  2. 浏览器启动 + UA + cookie 注入（不登录 HAX）
  3. 真实 HAX 登录验证（完整流程）

优先读 TEST_ACCOUNTS_JSON，读不到再回退 ACCOUNTS_JSON。

用法：
  TEST_ACCOUNTS_JSON='[...]' PROXY_SERVER=http://127.0.0.1:1081 python3 test_all.py

可选环境变量：
  SKIP_TEST=1,2,3    跳过指定测试（逗号分隔）
  HEADLESS=true      测试 2/3 是否无头
  BROWSER_UA=...     浏览器 UA
  TIMEOUT=30         requests 超时（秒）
"""
import os
import sys
import json
import time
import re
import traceback

# ========== 延迟导入，避免测试 1 因缺 ruyipage 而崩溃 ==========
try:
    import requests
except ImportError:
    print("❌ 缺少 requests，请 pip install requests")
    sys.exit(1)

ruyipage_ok = True
try:
    from ruyipage import launch
except ImportError:
    ruyipage_ok = False


# ================================================================
# 公共配置
# ================================================================
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/152.0.0.0 Safari/537.36")

UA = os.getenv("BROWSER_UA", DEFAULT_UA)
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER", "").strip()
TIMEOUT = int(os.getenv("TIMEOUT", "30"))

# 需要忽略的第三方 cookie 名字（Google Analytics、Ads、同意管理等）
IGNORE_COOKIE_NAMES = {
    "_ga", "_gid", "_gat_gtag_UA_179253361_1", "_ga_MK6PLQ755F",
    "__gads", "__gpi", "__eoi",
    "FCCDCF", "FCNEC", "FCOEC",
}

# 全局结果收集
RESULTS = {}   # {"test1": True/False/None, ...}
SKIPPED = set()

# 分隔符
LINE = "=" * 70
SUB = "-" * 70


# ================================================================
# 工具函数
# ================================================================
def banner(title, step=None):
    print(f"\n{LINE}")
    if step is not None:
        print(f"  测试 {step}：{title}")
    else:
        print(f"  {title}")
    print(LINE)


def sub_banner(text):
    print(f"\n{SUB}")
    print(f"  {text}")
    print(SUB)


def load_accounts():
    """优先读 TEST_ACCOUNTS_JSON，回退到 ACCOUNTS_JSON"""
    raw = os.getenv("TEST_ACCOUNTS_JSON", "").strip()
    source = "TEST_ACCOUNTS_JSON"
    if not raw:
        raw = os.getenv("ACCOUNTS_JSON", "[]").strip()
        source = "ACCOUNTS_JSON（回退）"

    print(f"📦 账号来源: {source}")

    if not raw:
        print("❌ 两个变量都为空，请配置 TEST_ACCOUNTS_JSON")
        sys.exit(2)

    try:
        accounts = json.loads(raw)
    except Exception as e:
        print(f"❌ {source} 解析失败: {e}")
        print(f"   原始内容前 200 字符: {raw[:200]}")
        sys.exit(2)
    if not accounts:
        print(f"❌ {source} 为空数组")
        sys.exit(2)
    return accounts


def normalize_cookies(cookies_data):
    """
    统一成 list[dict]。
    支持 str / list[dict]。
    自动过滤第三方 cookie（_ga、__gads 等）。
    """
    if isinstance(cookies_data, str):
        return [
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": "hax.co.id", "path": "/"},
            {"name": "PHPSESSID", "value": cookies_data,
             "domain": "hax.co.id", "path": "/vps-info"},
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
        domain = str(c.get("domain", "hax.co.id"))
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

    # 去重（name + domain + path）
    seen = set()
    uniq = []
    for c in result:
        key = (c["name"], c["domain"], c["path"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq


def extract_phpsessid(session_token):
    """从各种格式的 session_token 里提取 PHPSESSID 字符串"""
    if isinstance(session_token, str):
        return session_token.strip()
    if isinstance(session_token, list):
        # 优先选 path=/
        for c in session_token:
            if isinstance(c, dict) and c.get("name") == "PHPSESSID" \
                    and c.get("path", "/") == "/":
                return c.get("value", "").strip()
        for c in session_token:
            if isinstance(c, dict) and c.get("name") == "PHPSESSID":
                return c.get("value", "").strip()
    return None


def get_requests_proxies():
    if not PROXY_SERVER:
        return None
    return {"http": PROXY_SERVER, "https": PROXY_SERVER}


def http_headers(referer=None):
    h = {
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    if referer:
        h["Referer"] = referer
        h["Sec-Fetch-Site"] = "same-origin"
    return h


# ================================================================
# 测试 1：requests 验证 PHPSESSID
# ================================================================
def test_1_requests_cookie(accounts):
    banner("requests 验证 PHPSESSID（不启动浏览器）", step=1)

    proxies = get_requests_proxies()
    print(f"🔗 代理:        {PROXY_SERVER or '(直连)'}")
    print(f"🖥️  User-Agent: {UA[:80]}...")
    print(f"👥 账号数:      {len(accounts)}")

    all_ok = True
    summary = []

    for idx, acc in enumerate(accounts, 1):
        phone = acc.get("phone", f"account_{idx}")
        sub_banner(f"账号 {idx}/{len(accounts)}: {phone}")

        sess_id = extract_phpsessid(acc.get("session_token"))
        if not sess_id:
            print(f"  ❌ 未找到 PHPSESSID")
            all_ok = False
            summary.append((phone, "❌ 无 PHPSESSID"))
            continue

        print(f"  PHPSESSID: {sess_id[:8]}...{sess_id[-4:]} (长度 {len(sess_id)})")

        # 预访问 /login
        try:
            r0 = requests.get(
                "https://hax.co.id/login",
                headers=http_headers(),
                proxies=proxies,
                timeout=TIMEOUT,
            )
            print(f"  [预访问] /login → HTTP {r0.status_code}")
        except Exception as e:
            print(f"  ❌ 预访问异常: {e}")
            all_ok = False
            summary.append((phone, f"❌ 网络: {e}"))
            continue

        # 主请求 /vps-info
        try:
            r = requests.get(
                "https://hax.co.id/vps-info",
                headers=http_headers(referer="https://hax.co.id/login"),
                cookies={"PHPSESSID": sess_id},
                proxies=proxies,
                allow_redirects=True,
                timeout=TIMEOUT,
            )
        except Exception as e:
            print(f"  ❌ 主请求异常: {e}")
            all_ok = False
            summary.append((phone, f"❌ 请求: {e}"))
            continue

        print(f"  [主请求] /vps-info → HTTP {r.status_code}")
        print(f"  final URL: {r.url}")

        text = r.text
        is_cf = (
            "cf-challenge" in text.lower()
            or "just a moment" in text.lower()
            or "checking your browser" in text.lower()
            or "challenge-platform" in text.lower()
            or "cf-wrapper" in text.lower()
        )
        has_logout = ("Logout" in text) or ("Log out" in text) or ("logout" in text)
        has_login_btn = ('>Login<' in text) or ('>login<' in text)
        has_valid_until = "Valid until" in text
        has_vps_info = "VPS Information" in text or "vps-info" in r.url

        print(f"  {'─'*50}")
        print(f"  是 CF 挑战页:      {is_cf}")
        print(f"  含 'Logout':      {has_logout}")
        print(f"  含 'Login' 按钮:   {has_login_btn}")
        print(f"  含 'Valid until':  {has_valid_until}")
        print(f"  是 VPS 信息页:     {has_vps_info}")
        print(f"  {'─'*50}")

        if is_cf:
            print(f"  🔴 Cloudflare 拦截 → 需要 cf_clearance cookie")
            print(f"     请检查：代理 IP 是否和导出 cookie 时一致")
            all_ok = False
            summary.append((phone, "🔴 CF 拦截"))
        elif has_logout or has_valid_until or has_vps_info:
            print(f"  ✅ cookie 有效，登录成功！")
            summary.append((phone, "✅ cookie 有效"))
        elif has_login_btn:
            print(f"  🔴 cookie 已失效（服务器端 session 过期）")
            print(f"     请在本地重新登录 HAX，重新导出 PHPSESSID")
            all_ok = False
            summary.append((phone, "🔴 session 过期"))
        else:
            print(f"  ⚠️  状态不明")
            print(f"     页面片段：{text[:300].replace(chr(10), ' ')}")
            all_ok = False
            summary.append((phone, "⚠️ 状态不明"))

    sub_banner("测试 1 汇总")
    for phone, status in summary:
        print(f"  {status}  {phone}")

    return all_ok


# ================================================================
# 测试 2：浏览器环境 + UA + cookie 注入
# ================================================================
def test_2_browser():
    banner("浏览器启动 + UA + cookie 注入（不登录 HAX）", step=2)

    if not ruyipage_ok:
        print("❌ ruyipage 未安装，跳过")
        return False

    print(f"HEADLESS:     {HEADLESS}")
    print(f"PROXY_SERVER: {PROXY_SERVER or '(无)'}")
    print(f"BROWSER_UA:   {UA[:80]}...")

    all_ok = True

    # ---- 启动浏览器 ----
    print(f"\n[1/5] 启动 ruyipage...")
    launch_args = {"headless": HEADLESS, "window_size": (1366, 768)}
    try:
        launch_args["user_agent"] = UA
        page = launch(**launch_args)
        print("  ✅ 启动成功（含 user_agent 参数）")
        ua_via_arg = True
    except TypeError as e:
        print(f"  ⚠️  launch() 不支持 user_agent: {e}")
        launch_args.pop("user_agent", None)
        page = launch(**launch_args)
        print("  ✅ 启动成功（无 user_agent 参数）")
        ua_via_arg = False
    except Exception as e:
        print(f"  ❌ 浏览器启动失败: {e}")
        traceback.print_exc()
        return False

    try:
        # ---- 测试 UA ----
        print(f"\n[2/5] 验证 UA...")
        try:
            page.get("https://httpbin.org/user-agent")
            page.wait.doc_loaded(timeout=20)
            time.sleep(2)
            body = page.run_js("document.body.innerText") or ""
            print(f"  当前 UA: {body[:200]}")
            if ua_via_arg:
                if UA[:60] in body:
                    print(f"  ✅ UA 参数生效")
                else:
                    print(f"  ⚠️  UA 参数似乎未生效")
                    all_ok = False
            else:
                print(f"  ⚠️  user_agent 参数不支持，用浏览器默认 UA")
        except Exception as e:
            print(f"  ⚠️  访问 httpbin 失败: {e}（可能代理问题，继续）")

        # ---- cookie 注入 ----
        print(f"\n[3/5] 测试 cookie 注入...")
        try:
            page.get("https://hax.co.id/login")
            page.wait.doc_loaded(timeout=20)
            time.sleep(2)
            print(f"  ✅ 访问 hax.co.id/login 成功")
        except Exception as e:
            print(f"  ⚠️  访问 hax.co.id 失败: {e}")

        test_cookie = {
            "name": "TEST_COOKIE_12345",
            "value": "hello_world",
            "domain": "hax.co.id",
            "path": "/",
        }
        try:
            page.set_cookies([test_cookie])
            print(f"  ✅ set_cookies 调用成功")
        except Exception as e:
            print(f"  ❌ set_cookies 失败: {e}")
            all_ok = False

        time.sleep(1)
        try:
            all_cookies = page.get_cookies()
            names = [c.get("name") for c in all_cookies]
            print(f"  浏览器当前持有 {len(all_cookies)} 个 cookie: {names}")
            if "TEST_COOKIE_12345" in names:
                print(f"  ✅ 测试 cookie 注入成功，能被读回")
            else:
                print(f"  ❌ 测试 cookie 未被浏览器接受")
                print(f"     set_cookies 静默失败了，domain/path 可能有问题")
                all_ok = False
        except Exception as e:
            print(f"  ⚠️  get_cookies 失败: {e}")

        # ---- JS 执行 ----
        print(f"\n[4/5] 测试 JS 执行...")
        try:
            result = page.run_js("1 + 1")
            print(f"  1 + 1 = {result}")
            if str(result) == "2":
                print(f"  ✅ JS 执行正常")
            else:
                print(f"  ⚠️  JS 返回值异常")
        except Exception as e:
            print(f"  ❌ JS 失败: {e}")
            all_ok = False

        # ---- 截图 ----
        print(f"\n[5/5] 测试截图...")
        try:
            png_path = "test_browser_screenshot.png"
            driver = None
            for attr in ['driver', '_driver', 'page']:
                if hasattr(page, attr):
                    driver = getattr(page, attr)
                    break
            if driver and hasattr(driver, 'get_screenshot_as_file'):
                driver.get_screenshot_as_file(png_path)
            elif hasattr(page, 'screenshot'):
                page.screenshot(png_path)
            if os.path.exists(png_path):
                size = os.path.getsize(png_path)
                print(f"  ✅ 截图成功: {png_path} ({size} bytes)")
            else:
                print(f"  ❌ 截图文件未生成")
                all_ok = False
        except Exception as e:
            print(f"  ❌ 截图失败: {e}")
            all_ok = False

    finally:
        try:
            page.quit()
        except Exception:
            pass

    return all_ok


# ================================================================
# 测试 3：真实 HAX 登录
# ================================================================
def is_logged_in(page):
    """检测页面是否处于登录态，返回 (bool, reason)"""
    try:
        logout_btn = page.ele(
            "xpath://*[contains(text(), 'Logout') or contains(text(), 'Log out')]",
            timeout=3)
        if logout_btn and logout_btn.is_displayed:
            return True, "找到 Logout 按钮"
        login_btn = page.ele("xpath://*[contains(text(), 'Login')]", timeout=2)
        if login_btn and login_btn.is_displayed:
            return False, "找到 Login 按钮"
        if "hax.co.id/vps-info" in (page.url or ""):
            menu = page.ele("css:a.nav-link.dropdown-toggle", timeout=2)
            if menu and menu.is_displayed:
                return True, "找到用户下拉菜单"
        return False, "未找到登录态标志"
    except Exception as e:
        return False, f"检测异常: {e}"


def test_3_real_hax_login(accounts):
    banner("真实 HAX cookie 登录（完整流程）", step=3)

    if not ruyipage_ok:
        print("❌ ruyipage 未安装，跳过")
        return False

    print(f"HEADLESS:     {HEADLESS}")
    print(f"PROXY_SERVER: {PROXY_SERVER or '(无)'}")
    print(f"账号数:       {len(accounts)}")

    all_ok = True
    summary = []

    for idx, acc in enumerate(accounts, 1):
        phone = acc.get("phone", f"account_{idx}")
        sub_banner(f"账号 {idx}/{len(accounts)}: {phone}")

        session_token = acc.get("session_token")
        if not session_token:
            print(f"  ❌ 未配置 session_token")
            all_ok = False
            summary.append((phone, "❌ 无 session_token"))
            continue

        cookies_list = normalize_cookies(session_token)
        if not cookies_list:
            print(f"  ❌ cookie 数据为空")
            all_ok = False
            summary.append((phone, "❌ cookie 空"))
            continue

        names = [f"{c['name']}@{c['domain']}{c['path']}" for c in cookies_list]
        print(f"  规范化后的 cookie ({len(cookies_list)} 个):")
        for n in names:
            print(f"      - {n}")

        # ---- 启动浏览器 ----
        print(f"\n  [1/4] 启动浏览器...")
        launch_args = {"headless": HEADLESS, "window_size": (1366, 768)}
        try:
            launch_args["user_agent"] = UA
            page = launch(**launch_args)
        except TypeError:
            launch_args.pop("user_agent", None)
            page = launch(**launch_args)
        except Exception as e:
            print(f"        ❌ 启动失败: {e}")
            all_ok = False
            summary.append((phone, f"❌ 启动: {e}"))
            continue

        try:
            # ---- 访问登录页 ----
            print(f"  [2/4] 访问 hax.co.id/login...")
            page.get("https://hax.co.id/login")
            page.wait.doc_loaded(timeout=20)
            time.sleep(3)
            print(f"        当前 URL: {page.url}")

            # ---- 注入 cookie ----
            print(f"  [3/4] 注入 cookie...")
            try:
                page.set_cookies(cookies_list)
                print(f"        ✅ set_cookies 调用成功")
            except Exception as e:
                print(f"        ❌ set_cookies 失败: {e}")
                all_ok = False
                summary.append((phone, f"❌ 注入: {e}"))
                continue

            time.sleep(1)
            try:
                readback = page.get_cookies()
                hax_cookies = [c for c in readback
                               if "hax.co.id" in c.get("domain", "")]
                print(f"        浏览器实际持有 hax.co.id 下 {len(hax_cookies)} 个 cookie:")
                for c in hax_cookies:
                    print(f"          - {c.get('name')}@{c.get('domain')}{c.get('path')} = "
                          f"{str(c.get('value'))[:8]}...")

                has_sess = any(c.get("name") == "PHPSESSID" for c in hax_cookies)
                if not has_sess:
                    print(f"        ❌ PHPSESSID 未注入！")
                    all_ok = False
                    summary.append((phone, "❌ PHPSESSID 未注入"))
                    continue
            except Exception as e:
                print(f"        ⚠️  readback 失败: {e}")

            # ---- 访问 vps-info ----
            print(f"  [4/4] 访问 vps-info 验证登录态...")
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=20)
            time.sleep(2)
            page.get("https://hax.co.id/vps-info")
            page.wait.doc_loaded(timeout=15)
            time.sleep(2)

            print(f"        当前 URL: {page.url}")

            logged_in, reason = is_logged_in(page)
            print(f"        登录检测: {logged_in} ({reason})")

            if logged_in:
                print(f"        ✅ Cookie 登录成功！")
                try:
                    title = page.run_js("document.title") or ""
                    print(f"        页面标题: {title}")
                    body_text = page.run_js("document.body.innerText") or ""
                    m = re.search(r"Valid until\s*[:\s]*([^\n]+)", body_text)
                    if m:
                        print(f"        Valid until: {m.group(1).strip()}")
                except Exception:
                    pass
                summary.append((phone, "✅ 登录成功"))
            else:
                print(f"        ❌ Cookie 登录失败")
                try:
                    snippet = page.run_js(
                        "document.body.innerText.substring(0, 500)") or ""
                    print(f"        页面片段: {snippet[:300].replace(chr(10), ' ')}")
                except Exception:
                    pass
                all_ok = False
                summary.append((phone, "❌ 登录失败"))

        except Exception as e:
            print(f"  ❌ 异常: {e}")
            traceback.print_exc()
            all_ok = False
            summary.append((phone, f"❌ 异常: {e}"))
        finally:
            try:
                page.quit()
            except Exception:
                pass

    sub_banner("测试 3 汇总")
    for phone, status in summary:
        print(f"  {status}  {phone}")

    return all_ok


# ================================================================
# 主入口
# ================================================================
def main():
    print(LINE)
    print("  HAX Cookie 完整测试（合并版）")
    print(LINE)

    # 解析 SKIP_TEST
    skip_raw = os.getenv("SKIP_TEST", "").strip()
    if skip_raw:
        for s in skip_raw.split(","):
            s = s.strip()
            if s.isdigit():
                SKIPPED.add(int(s))

    print(f"环境：")
    print(f"  HEADLESS:     {HEADLESS}")
    print(f"  PROXY_SERVER: {PROXY_SERVER or '(无)'}")
    print(f"  BROWSER_UA:   {UA[:70]}...")
    print(f"  TIMEOUT:      {TIMEOUT}s")
    print(f"  ruyipage:     {'✅ 已安装' if ruyipage_ok else '❌ 未安装'}")
    if SKIPPED:
        print(f"  SKIP_TEST:    {sorted(SKIPPED)}")

    accounts = load_accounts()
    print(f"  ✅ 加载 {len(accounts)} 个账号")

    # ---- 测试 1 ----
    if 1 in SKIPPED:
        print(f"\n⏭️  跳过测试 1")
        RESULTS["test1"] = None
    else:
        try:
            RESULTS["test1"] = test_1_requests_cookie(accounts)
        except Exception as e:
            print(f"\n❌ 测试 1 崩溃: {e}")
            traceback.print_exc()
            RESULTS["test1"] = False

    # ---- 测试 2 ----
    if 2 in SKIPPED:
        print(f"\n⏭️  跳过测试 2")
        RESULTS["test2"] = None
    elif not ruyipage_ok:
        print(f"\n⏭️  跳过测试 2（ruyipage 未安装）")
        RESULTS["test2"] = None
    else:
        try:
            RESULTS["test2"] = test_2_browser()
        except Exception as e:
            print(f"\n❌ 测试 2 崩溃: {e}")
            traceback.print_exc()
            RESULTS["test2"] = False

    # ---- 测试 3 ----
    if 3 in SKIPPED:
        print(f"\n⏭️  跳过测试 3")
        RESULTS["test3"] = None
    elif not ruyipage_ok:
        print(f"\n⏭️  跳过测试 3（ruyipage 未安装）")
        RESULTS["test3"] = None
    else:
        try:
            RESULTS["test3"] = test_3_real_hax_login(accounts)
        except Exception as e:
            print(f"\n❌ 测试 3 崩溃: {e}")
            traceback.print_exc()
            RESULTS["test3"] = False

    # ---- 总汇总 ----
    print(f"\n{LINE}")
    print("  汇总")
    print(LINE)

    labels = {1: "requests 验证 cookie", 2: "浏览器环境", 3: "真实 HAX 登录"}
    for k in (1, 2, 3):
        v = RESULTS.get(f"test{k}")
        if v is True:
            icon = "✅ 通过"
        elif v is False:
            icon = "❌ 失败"
        else:
            icon = "⏭️  跳过"
        print(f"  测试 {k}（{labels[k]}）: {icon}")

    print(f"\n{LINE}")
    any_fail = any(v is False for v in RESULTS.values())
    if any_fail:
        print("❌ 有测试失败，请检查上面的日志")
        sys.exit(1)
    elif all(v is None for v in RESULTS.values()):
        print("⚠️  全部跳过")
        sys.exit(0)
    else:
        print("🎉 全部通过的测试都成功了")
        sys.exit(0)


if __name__ == "__main__":
    main()
