#!/usr/bin/env python3
"""全自动Grok注册 v7 (修复: OneTrust时序 + Turnstile CDP动态定位+trusted点击)
改动:
  1. 所有按钮点击改为 轮询等待出现→点击→验证 (解决 OneTrust 弹窗晚渲染问题)
  2. Turnstile 用 CDP 获取 iframe 真实位置 + page.mouse.click (视口坐标, 不再依赖窗口偏移)
  3. 保留: 邮箱逻辑 / SSO→OAuth→CPA 转换 / finally 浏览器清理
"""
import os, sys, time, random, string, re, json, subprocess
os.environ.setdefault('DISPLAY', ':1')
from cloakbrowser import launch
from curl_cffi import requests as cffi

# ============ 临时邮箱域名配置 ============
_config_json = os.environ.get("GROK_MAIL_CONFIG", "").strip()
if _config_json:
    try:
        _cfg = json.loads(_config_json)
        MAIL_DOMAINS = [(m['base_url'], m['domain']) for m in _cfg]
    except Exception as e:
        print(f'⚠️ GROK_MAIL_CONFIG 解析失败({e}), 使用空邮箱池', flush=True)
        MAIL_DOMAINS = []
else:
    # 部署时必须通过 GROK_MAIL_CONFIG 传入临时邮箱配置, 不内置默认池
    MAIL_DOMAINS = []

NODES = [n for n in os.environ.get("GROK_NODES", "8078,8083,8086,8047").split(",") if n.strip()]

# CPA/账号产出目录 (部署时通过环境变量/挂载卷指定, 不硬编码)
GROK_ACCOUNTS_DIR = os.environ.get("GROK_ACCOUNTS_DIR", "/root/grok_accounts")
os.makedirs(GROK_ACCOUNTS_DIR, exist_ok=True)
os.makedirs(os.path.join(GROK_ACCOUNTS_DIR, "cpa"), exist_ok=True)

TZ_POOL = ["America/New_York", "Europe/London", "Asia/Tokyo", "America/Chicago", "Australia/Sydney", "Europe/Berlin"]

def random_name():
    firsts = ["James","Mary","John","Patricia","David","Jennifer","Robert","Linda","Michael","Elizabeth","William","Barbara","Richard","Susan","Joseph","Jessica","Thomas","Sarah","Daniel","Karen","Matthew","Nancy","Anthony","Lisa","Mark","Betty","Donald","Sandra","Steven","Ashley","Andrew","Emily","Joshua","Kimberly","Kevin","Donna","Brian","Carol","Kevin","Michelle","Charles","Amanda"]
    lasts = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez","Hernandez","Lopez","Gonzalez","Wilson","Anderson","Thomas","Taylor","Moore","Jackson","Martin","Lee","Perez","Thompson","White","Harris","Sanchez","Clark","Ramirez","Lewis","Robinson","Walker","Young","Allen","King","Wright","Scott","Torres","Nguyen","Hill","Flores","Green","Adams","Nelson","Baker","Hall","Rivera","Campbell"]
    return random.choice(firsts), random.choice(lasts)

def create_mail():
    """多域名池随机轮询建临时邮箱. 返回 (email, jwt, mail_base)"""
    _plen=random.randint(8,12)
    _pool=string.ascii_lowercase+string.digits
    name=''.join(random.choices(_pool, k=_plen))
    if not any(c.isdigit() for c in name): name=name[:-1]+str(random.randint(0,9))
    if not any(c.isalpha() for c in name): name=name[:-1]+random.choice(string.ascii_lowercase)
    candidates = list(MAIL_DOMAINS)
    random.shuffle(candidates)
    for base, domain in candidates:
        try:
            r=cffi.post(f'{base}/api/new_address', json={'name':name,'domain':domain},
                        impersonate='chrome131', timeout=20)
            d=r.json()
            addr=d.get('address',''); jwt=d.get('jwt','')
            if addr and jwt:
                return addr, jwt, base
            print(f'  {domain} 建址失败: {str(d)[:80]}', flush=True)
        except Exception as e:
            print(f'  {domain} 建址异常: {str(e)[:60]}', flush=True)
            continue
    return None, None, None

def pull_code(email,jwt,mail_base,timeout=120):
    name=email.split('@')[0]; deadline=time.time()+timeout
    while time.time()<deadline:
        time.sleep(3)
        try:
            r=cffi.get(f'{mail_base}/api/mails?name={name}&limit=5&offset=0',
                       headers={'Authorization':f'Bearer {jwt}'}, impersonate='chrome131', timeout=15)
            for m in r.json().get('results',[]):
                raw=m.get('raw','') or m.get('text','') or json.dumps(m)
                s=re.search(r'code:\s*([A-Z0-9]{3}-[A-Z0-9]{3})',raw)
                if s: return s.group(1).replace('-','')
        except Exception:
            pass
    return None

# ============ 页面交互辅助 (轮询等待+验证) ============
def human_move(x, y):
    """模拟人类鼠标移动: 先移动到附近, 再精确移动到目标, 带抖动"""
    cx, cy = random.randint(200, 600), random.randint(200, 500)
    subprocess.run(['xdotool','mousemove',str(cx),str(cy)], capture_output=True)
    time.sleep(random.uniform(0.3, 0.8))
    mx = (cx+x)//2 + random.randint(-30,30)
    my = (cy+y)//2 + random.randint(-20,20)
    subprocess.run(['xdotool','mousemove',str(mx),str(my)], capture_output=True)
    time.sleep(random.uniform(0.2, 0.5))
    subprocess.run(['xdotool','mousemove',str(x+random.randint(-3,3)),str(y+random.randint(-3,3))], capture_output=True)
    time.sleep(random.uniform(0.2, 0.4))

def click_xy(x, y):
    human_move(x, y)
    subprocess.run(['xdotool','click','1'], capture_output=True)
    time.sleep(random.uniform(0.3, 0.6))

def wait_js(page, js, timeout, desc, interval=1.0):
    """轮询等待 JS 表达式返回真值"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.evaluate(js):
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f'  ⚠️ 等待超时({timeout}s): {desc}', flush=True)
    return False

def click_btn(page, regex, timeout=12, desc='按钮', interval=0.8):
    """轮询等待按钮出现并点击. 返回 True/False"""
    js = f"""(function(){{var b=[...document.querySelectorAll('button')].find(x=>/{regex}/i.test(x.innerText||''));if(!b)return false;b.click();return true;}})()"""
    return wait_js(page, js, timeout, desc, interval)

def set_input(page, selector, value, desc='输入框'):
    """填输入框 (原生setter+事件触发)"""
    js = f"""(function(){{var i=document.querySelector('{selector}');if(!i)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,'{value}');i.dispatchEvent(new Event('input',{{bubbles:true}}));i.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()"""
    return wait_js(page, js, 5, desc, 0.5)

def main():
    max_tries = 3
    tried = set()
    for attempt in range(1, max_tries+1):
        candidates = [n for n in NODES if n not in tried] or NODES
        port = random.choice(candidates)
        tried.add(port)
        proxy=f"http://127.0.0.1:{port}"
        seed=random.randint(10000,99999)
        tz=random.choice(TZ_POOL)
        email,jwt,mail_base=create_mail()
        if not email:
            print('❌ 所有域名建临时邮箱失败, 本轮放弃', flush=True)
            time.sleep(3)
            continue
        password="Kx9!vR7mP2qL8sT4wZ6aB1nC5"
        given, family = random_name()
        print(f"1. 邮箱:{email} 节点:{port} seed:{seed} tz:{tz} 名字:{given} {family} (try {attempt}/{max_tries})", flush=True)

        browser=launch(headless=False, proxy={'server':proxy},
                       timezone=tz, locale='en-US',
                       args=['--fingerprint-platform=macos',f'--fingerprint={seed}',
                             '--remote-debugging-port=9222','--remote-allow-origins=*'])
        page=browser.new_page()
        try: page.bring_to_front()
        except: pass
        page.goto('https://accounts.x.ai/sign-up?redirect=grok-com&return_to=%2F', wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(4000)
        print('2. 页面加载', flush=True)

        # --- OneTrust cookie 弹窗: 轮询等 Reject All 出现再点 ---
        click_btn(page, 'reject all', timeout=12, desc='Reject All (cookie弹窗)')
        time.sleep(1)
        # 弹窗有时分层, 再尝试一次
        click_btn(page, 'reject all', timeout=3, desc='Reject All 二次')
        time.sleep(1)

        # --- 新版注册流程: 先点 "Sign up with email" (入口选择页) ---
        if not click_btn(page, 'sign up with email', timeout=12, desc='Sign up with email'):
            print('❌ 未找到 Sign up with email 按钮', flush=True)
            os.system('DISPLAY=:1 import -window root /tmp/g6_v7_nobtn.png')
            browser.close(); time.sleep(3); continue

        # --- 等邮箱输入框出现 (点击成功标志) ---
        if not wait_js(page, "!!document.querySelector('input[type=email]')", 10, '邮箱输入框'):
            print('❌ 点击后未出现邮箱输入框', flush=True)
            os.system('DISPLAY=:1 import -window root /tmp/g6_v7_noemail.png')
            browser.close(); time.sleep(3); continue

        # --- 填邮箱 → 点 Sign up ---
        set_input(page, 'input[type=email]', email, '邮箱')
        page.wait_for_timeout(1500)
        if not click_btn(page, r'^sign up$|sign up|continue|next', timeout=8, desc='Sign up(发码)按钮'):
            print('❌ 未找到发码按钮', flush=True)
            browser.close(); time.sleep(3); continue
        print('3. 发码', flush=True)

        # --- 等验证码输入框 ---
        if not wait_js(page, "!!document.querySelector('input[name=code]')", 50, '验证码输入框', interval=2):
            print('❌ 验证码输入框未出现', flush=True)
            os.system('DISPLAY=:1 import -window root /tmp/g6_v7_nocode.png')
            browser.close(); time.sleep(3); continue
        code=pull_code(email,jwt,mail_base,timeout=90)
        print('4. 验证码:',code, flush=True)
        if not code:
            print('❌ 验证码超时', flush=True)
            os.system('DISPLAY=:1 import -window root /tmp/g6_v7_notoken.png')
            browser.close(); time.sleep(3); continue

        # --- 填验证码 → confirm ---
        set_input(page, 'input[name=code]', code, '验证码')
        page.wait_for_timeout(1500)
        click_btn(page, 'confirm|verify', timeout=8, desc='Confirm验证码')
        print('5. 填码', flush=True)
        time.sleep(8)

        # --- 填表单 (givenName/familyName/password) ---
        set_input(page, 'input[name=givenName]', given, '名')
        set_input(page, 'input[name=familyName]', family, '姓')
        set_input(page, 'input[name=password]', password, '密码')
        page.wait_for_timeout(2000)
        print('6. 表单已填', flush=True)

        # --- 固定窗口 (仍然做, 保持一致性; 但坐标不再依赖它) ---
        try:
            subprocess.run(['xdotool','search','--class','chromium','windowmove','0','0'], capture_output=True, timeout=3)
            subprocess.run(['xdotool','search','--class','chromium','windowsize','1280','800'], capture_output=True, timeout=3)
            time.sleep(1)
        except Exception as e:
            print(f'   固定窗口失败: {e}', flush=True)

        # --- Turnstile: 等渲染 → 容器定位 (cf-turnstile-response的父容器, 不依赖iframe src) → page.mouse.click ---
        print('7. 定位Turnstile...', flush=True)
        wait_js(page, "!!document.querySelector('input[name=cf-turnstile-response]')", 15, 'Turnstile渲染')
        loc = None
        for i in range(10):
            loc = page.evaluate("""(function(){
                var inp = document.querySelector('input[name=cf-turnstile-response]');
                if (!inp) return null;
                // 向上找可见容器 (宽>100 高>20 的祖先)
                var el = inp;
                for (var k = 0; k < 6; k++) {
                    el = el.parentElement;
                    if (!el) break;
                    var r = el.getBoundingClientRect();
                    if (r.width > 100 && r.height > 20) {
                        return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
                    }
                }
                var r2 = inp.getBoundingClientRect();
                return {x: Math.round(r2.x), y: Math.round(r2.y), w: Math.round(r2.width), h: Math.round(r2.height)};
            })()""")
            if loc:
                print(f'   Turnstile 容器: {json.dumps(loc)}, 视口坐标', flush=True)
                break
            time.sleep(1)
        if loc:
            # checkbox 位于容器左缘约30px, 垂直居中
            cx = loc['x'] + 30
            cy = loc['y'] + loc['h']/2
            # 人类化移动 (CDP trusted 事件, 视口坐标系)
            page.mouse.move(random.randint(200,600), random.randint(200,400))
            time.sleep(random.uniform(0.3,0.6))
            page.mouse.move(cx+random.randint(-3,3), cy+random.randint(-3,3))
            time.sleep(random.uniform(0.2,0.4))
            page.mouse.click(cx, cy)
            print(f'8. 已点击 checkbox ({cx},{cy})', flush=True)
        else:
            print('   ⚠️ 未定位到 Turnstile 容器, 用旧标定坐标 xdotool 兜底', flush=True)
            winout=subprocess.run(['xdotool','search','--class','chromium','getwindowgeometry'],capture_output=True,text=True).stdout
            wx, wy = 0, 0
            for line in winout.split('\n'):
                if 'Position:' in line:
                    parts=line.split('Position:')[1].strip().split(',')
                    wx, wy = int(parts[0]), int(parts[1].split()[0])
            click_xy(wx+101, wy+627)

        # --- 等 token ---
        token_ok=False
        for i in range(30):
            time.sleep(2)
            try:
                v=page.evaluate("""(function(){var i=document.querySelector('input[name="cf-turnstile-response"]');return i?i.value:'';})()""")
                if v and len(v)>50:
                    token_ok=True; print(f'9. ✅ token len={len(v)}', flush=True); break
            except Exception:
                pass
        if not token_ok:
            print(f'❌ token 未出现 (节点{port}), 换节点重试', flush=True)
            os.system('DISPLAY=:1 import -window root /tmp/g6_tsfail_v7.png')
            browser.close()
            time.sleep(3)
            continue

        # --- 点 Complete sign up ---
        print('10. 点Complete sign up...', flush=True)
        click_btn(page, 'complete sign up', timeout=8, desc='Complete sign up')
        time.sleep(5)
        for i in range(20):
            time.sleep(3)
            if 'grok.com' in page.url:
                print('11. ✅ 跳转 grok.com', flush=True)
                break
        time.sleep(3)

        # --- 读 SSO ---
        sso=''
        try:
            import urllib.request
            pages=json.load(urllib.request.urlopen('http://127.0.0.1:9222/json'))
            pg=[p for p in pages if p.get('type')=='page' and 'stripe' not in p.get('url','') and p.get('url','').startswith('http')][0]
            import websocket
            ws=websocket.create_connection(pg['webSocketDebuggerUrl'],timeout=15)
            ws.send(json.dumps({"id":1,"method":"Network.getAllCookies","params":{}}))
            deadline=time.time()+10
            while time.time()<deadline:
                try: msg=json.loads(ws.recv())
                except: break
                if msg.get('id')==1:
                    for c in msg['result']['cookies']:
                        if c['name']=='sso': sso=c['value']
                    break
            ws.close()
        except Exception as e:
            print('读SSO err:',e, flush=True)

        print('\n=== 结果 ===', flush=True)
        if sso:
            print('✅ SSO:', sso[:40]+'...', flush=True)
            with open(f'{GROK_ACCOUNTS_DIR}/grok_account.txt','a') as f:
                f.write(f'{email}:{password}:{given}_{family}:{sso}\n')
            open('/tmp/last_sso.txt','w').write(sso)
            print(f'✅ 已存 {GROK_ACCOUNTS_DIR}/grok_account.txt', flush=True)
            # 立即转 CPA (SSO 最新鲜时)
            print('\n=== SSO→OAuth→CPA 转换 (注册时立即) ===', flush=True)
            try:
                sys.path.insert(0,'/tmp')
                import sso2cpa
                import urllib.parse, base64, hashlib
                from curl_cffi import requests as cffi2
                s = cffi2.Session(impersonate='chrome131')
                s.proxies = {'http': f'http://127.0.0.1:{port}', 'https': f'http://127.0.0.1:{port}'}
                s.headers.update({'user-agent': sso2cpa.UA, 'accept': '*/*',
                                  'origin': 'https://accounts.x.ai', 'referer': 'https://accounts.x.ai/'})
                dev_ep, tok_ep, auth_ep = sso2cpa.discover(s)
                flow, err = sso2cpa.start_device_flow(s, dev_ep, sso2cpa.Scope, sso2cpa.ClientID)
                if err:
                    print(f'❌ device flow: {err}', flush=True)
                else:
                    err = sso2cpa.confirm_http(s, sso, flow, tok_ep)
                    if err:
                        print(f'❌ confirm: {err}', flush=True)
                    else:
                        at, rt, idt, tt, exp, te = sso2cpa.poll_token(s, flow, tok_ep)
                        if at and rt:
                            doc = sso2cpa.cpa_document(at, rt, idt, te, '', email)
                            doc['registration_context'] = {
                                'node_port': port,
                                'node_ip': port,
                                'fingerprint_seed': seed,
                                'fingerprint_platform': 'macos',
                                'timezone': tz,
                                'password': password,
                            }
                            doc['password'] = password
                            doc_path = f"{GROK_ACCOUNTS_DIR}/cpa/cpa_{email}_{hashlib.md5(email.encode()).hexdigest()[:8]}.json"
                            with open(doc_path,'w') as f:
                                json.dump(doc, f, indent=2, ensure_ascii=False)
                            print(f'✅ CPA 已生成: {doc_path}', flush=True)
                            print(f'   AT len={len(at)}, RT len={len(rt)}, expires_in={exp}s', flush=True)
                        else:
                            print('❌ poll_token 失败', flush=True)
            except Exception as e:
                print(f'❌ CPA 转换异常: {str(e)[:150]}', flush=True)
            # 自动刷新 SUMMARY.md
            try:
                subprocess.run(['/usr/bin/python3.11','/tmp/gen_summary.py'], capture_output=True, timeout=30)
                print('✅ SUMMARY.md 已自动刷新', flush=True)
            except Exception as e:
                print(f'⚠️ SUMMARY 刷新失败: {str(e)[:80]}', flush=True)
            page.screenshot(path='/tmp/g6_final.png')
            browser.close()
            print('DONE', flush=True)
            return
        else:
            print('❌ 无SSO', flush=True)
            page.screenshot(path='/tmp/g6_nosso.png')
            browser.close()
            time.sleep(3)
            continue
    print('❌ 三次尝试均失败', flush=True)
    sys.exit(1)

if __name__=='__main__':
    try:
        main()
    finally:
        # 兜底清理: 无论成功/失败/异常, 确保浏览器进程关闭, 避免残留占用9222端口
        try:
            import subprocess as _sp
            _sp.run(['pkill', '-f', 'remote-debugging-port=9222'], capture_output=True)
            _sp.run(['pkill', '-f', '--fingerprint-platform=macos'], capture_output=True)
        except Exception:
            pass
