def set_session_cookie(page, cookies_data):
    """
    多路径注入 + 完整调试信息。
    打印 page.set/page.browser 方法列表、CookieInfo 字段名、XHR 探测结果。
    """
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

    # ---- 0. requests 探测（仅作参考）----
    ok, info = probe_cookie_with_requests(sess_value)
    if ok:
        print(f"  [COOKIE] ✅ requests 探测：{info}", flush=True)
    else:
        print(f"  [COOKIE] ⚠️ requests 探测：{info}（继续尝试浏览器注入）", flush=True)

    # ---- 1. 访问 /login ----
    try:
        page.get("https://hax.co.id/login")
        page.wait.doc_loaded(timeout=20)
        time.sleep(2)
        print(f"  [COOKIE] 当前页面: {page.url}", flush=True)
    except Exception as e:
        debug_print(f"预访问 /login 失败: {e}")

    # ---- 2. 打印 page.set 和 page.browser 的方法列表（只打一次）----
    if not getattr(set_session_cookie, '_debug_done', False):
        try:
            page_set_methods = [m for m in dir(page.set) if not m.startswith('_')]
            print(f"  [DEBUG] page.set 方法: {page_set_methods}", flush=True)
        except Exception as e:
            print(f"  [DEBUG] 列 page.set 失败: {e}", flush=True)
        for battr in ['browser', '_browser']:
            obj = getattr(page, battr, None)
            if obj is None:
                continue
            try:
                methods = [m for m in dir(obj) if not m.startswith('_')]
                print(f"  [DEBUG] page.{battr} 方法: {methods}", flush=True)
            except Exception as e:
                print(f"  [DEBUG] 列 page.{battr} 失败: {e}", flush=True)
        set_session_cookie._debug_done = True

    # ---- 3. 依次尝试多种 set.cookies 调用 ----
    tried = []

    def try_call(name, fn):
        try:
            fn()
            tried.append(f"{name} ✅")
            return True
        except Exception as e:
            tried.append(f"{name} ❌ ({str(e)[:100]})")
            return False

    # 3a. 标准调用
    try_call("set.cookies(list)", lambda: page.set.cookies(cookies_list))
    # 3b. target='browser'
    try_call("set.cookies(list, target='browser')",
             lambda: page.set.cookies(cookies_list, target='browser'))
    # 3c. target=None
    try_call("set.cookies(list, target=None)",
             lambda: page.set.cookies(cookies_list, target=None))
    # 3d. page.set_cookies
    try_call("page.set_cookies(list)", lambda: page.set_cookies(cookies_list))
    # 3e. 逐个 set
    for c in cookies_list:
        try_call(f"set.cookies({c['name']})", lambda c=c: page.set.cookies(c))

    print(f"  [COOKIE] 尝试结果: {tried}", flush=True)

    # ---- 4. JS document.cookie 兜底 ----
    try:
        page.run_js(f"document.cookie = 'PHPSESSID={sess_value}; path=/; SameSite=Lax';")
        print(f"  [COOKIE] JS 注入完成", flush=True)
    except Exception as e:
        print(f"  [COOKIE] JS 注入失败: {e}", flush=True)

    time.sleep(1)

    # ---- 5. 强制 reload，让 cookie 在下次请求时生效 ----
    try:
        page.refresh()
        page.wait.doc_loaded(timeout=15)
        time.sleep(2)
        print(f"  [COOKIE] reload 后 URL: {page.url}", flush=True)
    except Exception as e:
        print(f"  [COOKIE] reload 失败: {e}", flush=True)

    # ---- 6. 打印 CookieInfo 字段名 + 前 2 个 cookie 内容 ----
    for battr in ['browser', '_browser']:
        obj = getattr(page, battr, None)
        if obj is None:
            continue
        g = getattr(obj, 'cookies', None)
        if g is None:
            continue
        try:
            val = g() if callable(g) else g
            if isinstance(val, list) and val:
                sample = val[0]
                if hasattr(sample, '_fields'):
                    print(f"  [DEBUG] CookieInfo 字段名: {sample._fields}", flush=True)
                elif hasattr(sample, '__dict__'):
                    print(f"  [DEBUG] Cookie 属性: {list(sample.__dict__.keys())}", flush=True)
                for i, c in enumerate(val[:2]):
                    print(f"  [DEBUG] cookie[{i}]: {c}", flush=True)

                # 用 _asdict / __dict__ 遍历
                hax = []
                for c in val:
                    if hasattr(c, '_asdict'):
                        d = c._asdict()
                    elif hasattr(c, '__dict__'):
                        d = c.__dict__
                    else:
                        d = {}
                    dom = str(d.get('domain') or d.get('host') or d.get('Domain') or '')
                    nm = str(d.get('name') or d.get('Name') or '')
                    pth = str(d.get('path') or d.get('Path') or '/')
                    if 'hax.co.id' in dom:
                        hax.append(f"{nm}@{dom}{pth}")
                print(f"  [COOKIE] page.{battr}.cookies() 总计 {len(val)} 个, hax.co.id 下 {len(hax)} 个: {hax}", flush=True)
                break
        except Exception as e:
            print(f"  [COOKIE] 读 page.{battr}.cookies() 失败: {e}", flush=True)

    # ---- 7. ★ XHR 探测：看浏览器实际发出的请求里有没有 cookie ----
    try:
        xhr_result = page.run_js("""
        (function() {
            try {
                var xhr = new XMLHttpRequest();
                xhr.open('GET', 'https://hax.co.id/vps-info', false);
                xhr.send();
                var t = xhr.responseText || '';
                return JSON.stringify({
                    status: xhr.status,
                    len: t.length,
                    hasLogout: t.indexOf('Logout') >= 0,
                    hasLogin: t.indexOf('>Login<') >= 0,
                    hasValid: t.indexOf('Valid until') >= 0,
                    hasVpsInfo: t.indexOf('VPS Information') >= 0,
                    finalUrl: xhr.responseURL || '',
                    docCookieHasSess: document.cookie.indexOf('PHPSESSID=' + arguments[0]) >= 0
                });
            } catch(e) {
                return JSON.stringify({error: String(e)});
            }
        })()
        """.replace("arguments[0]", "'" + sess_value + "'"))
        print(f"  [COOKIE] XHR 探测 /vps-info: {xhr_result}", flush=True)
    except Exception as e:
        print(f"  [COOKIE] XHR 探测失败: {e}", flush=True)

    return True
