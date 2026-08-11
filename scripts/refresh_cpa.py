#!/usr/bin/env python3
"""CPA 降级恢复: RT过期时, 用注册时环境重新登录拿新SSO -> 新CPA
用法: python3 refresh_cpa.py <cpa.json> [节点端口]
关键: Turnstile 登录页通常是自动验证的, 不需要手动点击 checkbox
"""
import sys, json, time, os, subprocess, random
sys.path.insert(0, '/tmp')
from cloakbrowser import launch
import curl_cffi.requests as cffi
import importlib.util
import os
_script_dir = os.path.dirname(os.path.abspath(__file__))
# 加载 sso2cpa (与脚本同目录)
spec = importlib.util.spec_from_file_location("sso2cpa", os.path.join(_script_dir, "sso2cpa.py"))
sso2cpa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sso2cpa)

import urllib.request, websocket

def extract_sso(page):
    """通过 CDP 提取 sso cookie"""
    try:
        pages = json.load(urllib.request.urlopen('http://127.0.0.1:9222/json'))
        pg = [p for p in pages if p.get('type')=='page' and p.get('url','').startswith('http')][0]
        ws = websocket.create_connection(pg['webSocketDebuggerUrl'], timeout=15)
        ws.send(json.dumps({"id":1,"method":"Network.getAllCookies","params":{}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get('id') == 1:
                for c in msg['result']['cookies']:
                    if c['name'] == 'sso':
                        ws.close()
                        return c['value']
            time.sleep(0.1)
        ws.close()
    except Exception as e:
        print(f"  CDP读SSO异常: {e}", flush=True)
    return ''

def _proxy_url(port):
    """兼容纯端口 (8078 -> http://127.0.0.1:8078) 和 host:port (mihomo:8108 -> http://mihomo:8108)"""
    if '://' in str(port):
        return port
    if ':' in str(port):
        return f'http://{port}'
    return f'http://127.0.0.1:{port}'

def run_sso2cpa(sso, email, port):
    """调用 sso2cpa 模块进行 SSO->CPA 转换"""
    s = cffi.Session(impersonate='chrome131')
    p_url = _proxy_url(port)
    s.proxies = {'http': p_url, 'https': p_url}
    s.headers.update({'user-agent': sso2cpa.UA, 'accept': '*/*',
                      'origin': 'https://accounts.x.ai', 'referer': 'https://accounts.x.ai/'})
    dev_ep, tok_ep, auth_ep = sso2cpa.discover(s)
    flow, err = sso2cpa.start_device_flow(s, dev_ep, sso2cpa.Scope, sso2cpa.ClientID)
    if err:
        return None, f"device flow: {err}"
    err = sso2cpa.confirm_http(s, sso, flow, tok_ep)
    if err:
        return None, f"confirm: {err}"
    at, rt, idt, tt, exp, te = sso2cpa.poll_token(s, flow, tok_ep)
    if not at or not rt:
        return None, "poll_token失败"
    return at, rt, exp

def main():
    if len(sys.argv) < 2:
        print("用法: python3 refresh_cpa.py <cpa.json> [节点端口]")
        sys.exit(1)

    path = sys.argv[1]
    default_port = sys.argv[2] if len(sys.argv) > 2 else 'mihomo:8001'

    with open(path) as f:
        doc = json.load(f)

    email = doc.get('email', '')
    password = doc.get('password', 'Kx9!vR7mP2qL8sT4wZ6aB1nC5')
    ctx = doc.get('registration_context', {})
    seed = ctx.get('fingerprint_seed', random.randint(10000, 99999))
    tz = ctx.get('timezone', 'America/New_York')
    platform = ctx.get('fingerprint_platform', 'macos')

    print(f"降级恢复: {email}", flush=True)
    print(f"  原始节点: {ctx.get('node_port','?')}  seed: {seed}  tz: {tz}", flush=True)

    # 尝试多个节点
    ports_to_try = [default_port]
    extra_ports = ["mihomo:8002"]
    for p in extra_ports:
        if p not in ports_to_try:
            ports_to_try.append(p)
    random.shuffle(ports_to_try)

    for attempt, port in enumerate(ports_to_try):
        print(f"\n--- 尝试节点 {port} (第{attempt+1}次) ---", flush=True)

        browser = launch(headless=False,
                        proxy={'server': _proxy_url(port)},
                        timezone=tz, locale='en-US',
                        args=[f'--fingerprint-platform={platform}',
                              f'--fingerprint={seed}',
                              '--remote-debugging-port=9222',
                              '--remote-allow-origins=*'])
        page = browser.new_page()
        try: page.bring_to_front()
        except: pass

        # 打开登录页
        page.goto('https://accounts.x.ai/sign-in?redirect=grok-com',
                  wait_until='domcontentloaded', timeout=60000)
        page.set_viewport_size({'width': 1280, 'height': 800})
        page.wait_for_timeout(5000)
        print(f"  页面加载完成", flush=True)

        # 关 Cookie 弹窗
        page.evaluate("""(function(){var b=[...document.querySelectorAll('button')].find(x=>/reject all/i.test(x.innerText||''));if(b)b.click();})()""")
        page.wait_for_timeout(1000)

        # 点 "Login with email" (轮询等待)
        deadline = time.time() + 15
        while time.time() < deadline:
            clicked = page.evaluate("""(function(){var b=[...document.querySelectorAll('button')].find(x=>/login with email/i.test(x.innerText||''));if(b){b.click();return true;}return false;})()""")
            if clicked:
                break
            time.sleep(0.5)
        page.wait_for_timeout(4000)

        # 填邮箱
        page.evaluate(f"""(function(){{
            var e=document.querySelector('input[type=email]');if(e){{
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'{email}');
                e.dispatchEvent(new Event('input',{{'bubbles':true}}));
                e.dispatchEvent(new Event('change',{{'bubbles':true}}));
            }}
        }})()""")
        page.wait_for_timeout(1500)

        # 点 Next
        page.evaluate("""(function(){var b=[...document.querySelectorAll('button')].find(x=>/^Next$/i.test(x.innerText));if(b)b.click();})()""")
        page.wait_for_timeout(3000)

        # 填密码
        page.evaluate(f"""(function(){{
            var p=document.querySelector('input[type=password]');if(p){{
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(p,'{password}');
                p.dispatchEvent(new Event('input',{{'bubbles':true}}));
                p.dispatchEvent(new Event('change',{{'bubbles':true}}));
            }}
        }})()""")
        page.wait_for_timeout(1500)
        print(f"  已填邮箱密码", flush=True)

        # 点 Login 触发 Turnstile
        page.evaluate("""(function(){var b=[...document.querySelectorAll('button')].find(x=>/^Login$/i.test(x.innerText));if(b)b.click();})()""")
        print(f"  已点击 Login, 等待 Turnstile...", flush=True)
        
        # 等待 Turnstile 容器出现 (cf-turnstile-response 隐藏input的父容器, 不依赖iframe src)
        container = None
        for i in range(15):
            time.sleep(1)
            container = page.evaluate("""() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                if (!el) return null;
                const w = el.closest('div');
                if (!w) return null;
                const r = w.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""")
            if container and container.get('w', 0) > 0:
                print(f"  Turnstile 容器: {container} (第{i+1}秒)", flush=True)
                break
        time.sleep(1)  # 等 checkbox 渲染
        
        # 用 CDP 视口坐标点击 (trusted事件, 与注册v7同款, 不依赖窗口位置)
        if container and container.get('w', 0) > 0:
            cx = container['x'] + 30
            cy = container['y'] + container['h'] / 2
            page.mouse.move(random.randint(300, 600), random.randint(200, 400))
            time.sleep(random.uniform(0.3, 0.6))
            page.mouse.click(cx, cy)
            print(f"  ✅ 已点击 checkbox (视口坐标 {cx:.0f},{cy:.0f})", flush=True)
        else:
            print(f"  ⚠️ Turnstile 容器未定位到, 跳过点击 (等待自动验证)", flush=True)
        
        # 等 token
        token = ''
        for i in range(20):
            time.sleep(2)
            try:
                token = page.evaluate("""() => {
                    const i = document.querySelector('input[name="cf-turnstile-response"]');
                    return i ? i.value : '';
                }""")
                if token and len(token) > 50:
                    print(f"  ✅ Turnstile 通过, token len={len(token)}", flush=True)
                    break
            except:
                pass
            if (i + 1) % 5 == 0:
                page.screenshot(path=f'/tmp/refresh_wait_{i+1}.png')
                print(f"  等待 token... ({i+1}/20)", flush=True)
        
        if not (token and len(token) > 50):
            print(f"  ❌ Turnstile 未通过 (节点{port})", flush=True)
            page.screenshot(path='/tmp/refresh_fail.png')
            browser.close()
            time.sleep(3)
            continue
        
        # ✅ Token 已获取, 现在点 Login 按钮提交登录表单 (轮询等待)
        print(f"  Turnstile 通过, 点击 Login 按钮提交...", flush=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            clicked = page.evaluate("""(function(){var b=[...document.querySelectorAll('button')].find(x=>/^Login$/i.test(x.innerText));if(b){b.click();return true;}return false;})()""")
            if clicked:
                break
            time.sleep(0.5)
        time.sleep(3)
        
        # 等待登录跳转 (页面 URL 变化)
        print(f"  等待登录跳转...", flush=True)
        for i in range(10):
            time.sleep(2)
            if 'grok.com' in page.url or 'accounts.x.ai' not in page.url:
                print(f"  ✅ 页面已跳转", flush=True)
                break
        
        # 提取 SSO
        sso = extract_sso(page)
        if not sso:
            print(f"  ❌ 未获取到SSO", flush=True)
            page.screenshot(path='/tmp/refresh_nosso.png')
            browser.close()
            time.sleep(3)
            continue

        print(f"  ✅ SSO 获取成功, len={len(sso)}", flush=True)

        # SSO -> CPA
        print(f"  SSO->OAuth->CPA 转换...", flush=True)
        at, rt, exp = run_sso2cpa(sso, email, port)
        if not at:
            print(f"  ❌ CPA转换失败: {rt}", flush=True)
            browser.close()
            time.sleep(3)
            continue

        # 更新 CPA 文件
        doc['access_token'] = at
        doc['refresh_token'] = rt
        doc['id_token'] = ''
        doc['expires_in'] = exp
        doc['expired'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + exp))
        doc['last_refresh'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
        with open(path, 'w') as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)

        print(f"  ✅ CPA 已更新: AT len={len(at)}, RT len={len(rt)}, 有效期={exp}s", flush=True)
        browser.close()
        print("\n=== 完成 ===", flush=True)
        return

    print("\n❌ 所有节点尝试失败", flush=True)
    sys.exit(1)

if __name__ == '__main__':
    try:
        main()
    finally:
        # 兜底清理: 无论成功/失败/异常, 确保浏览器进程关闭, 避免残留占9222
        try:
            import subprocess as _sp
            _sp.run(['pkill', '-f', 'remote-debugging-port=9222'], capture_output=True)
            _sp.run(['pkill', '-f', '--fingerprint-platform'], capture_output=True)
        except Exception:
            pass
