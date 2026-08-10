#!/usr/bin/env python3
"""Grok/xAI 登录拿 SSO — 加固版 (xdotool物理点击Turnstile, 稳定性实测)
用法: python3 grok_login_sso.py <EMAIL> <PASSWORD> [sso输出文件] [节点端口] [seed] [timezone]
前置: DISPLAY=:1, x11vnc运行中, 节点已配置到Mihomo
"""
import sys, time, subprocess, random, os
from collections import deque
from cloakbrowser import launch

EMAIL = sys.argv[1]
PW = sys.argv[2]
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/grok_sso.txt"
PORT = sys.argv[4] if len(sys.argv) > 4 else "8078"
SEED = int(sys.argv[5]) if len(sys.argv) > 5 else 12345
TZ = sys.argv[6] if len(sys.argv) > 6 else "America/New_York"

browser = launch(headless=False, proxy={"server": f"http://127.0.0.1:{PORT}"},
                 timezone=TZ, locale="en-US",
                 args=[
                     '--fingerprint-platform=macos',
                     f'--fingerprint={SEED}',
                     '--start-fullscreen',
                     '--window-size=1280,800',
                     '--remote-debugging-port=9222',
                     '--remote-allow-origins=*'
                 ])
page = browser.new_page()
# 固定窗口，避免 Turnstile 屏幕坐标漂移
try:
    subprocess.run(['xdotool','search','--class','chromium','windowactivate'], capture_output=True, timeout=3)
    subprocess.run(['xdotool','search','--class','chromium','windowraise'], capture_output=True, timeout=3)
    subprocess.run(['xdotool','search','--class','chromium','windowmove','0','0'], capture_output=True, timeout=3)
    subprocess.run(['xdotool','search','--class','chromium','windowsize','1280','800'], capture_output=True, timeout=3)
except Exception as e:
    print(f"[sso] 固定窗口失败: {e}", flush=True)
page.goto("https://accounts.x.ai/sign-in?redirect=account", wait_until="domcontentloaded", timeout=60000)
print("[sso] 已打开登录页", flush=True)
time.sleep(3)

def click_btn(text, timeout=12, interval=0.8):
    """轮询等待按钮出现并点击 (修复 OneTrust/按钮晚渲染问题). 返回 True/False"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = page.evaluate(f"""() => {{
                const b = [...document.querySelectorAll('button')].find(btn => /{text}/i.test(btn.innerText||''));
                if(!b) return false;
                b.click(); return true;
            }}""")
            if ok:
                return True
        except Exception:
            pass
        time.sleep(interval)
    print(f"[sso] ⚠️ 等待超时({timeout}s): {text}", flush=True)
    return False

def native_token():
    return page.evaluate('() => (document.querySelector(\'input[name="cf-turnstile-response"]\')||{}).value||""')

def find_checkbox_viewport():
    """CDP 定位 Turnstile checkbox, 返回视口坐标 (不依赖窗口位置, 与注册v7同款)
    用 cf-turnstile-response 的父容器 getBoundingClientRect (视口坐标系)"""
    return page.evaluate("""() => {
        const resp = document.querySelector('input[name="cf-turnstile-response"]');
        if(!resp) return null;
        let el = resp.parentElement;
        for(let k=0;k<6&&el;k++){
            const r=el.getBoundingClientRect();
            if(r.width>100&&r.height>20){
                // 容器左缘约30px为checkbox, 垂直居中 (与注册v7一致)
                return {x: Math.round(r.left+30), y: Math.round(r.top+r.height/2), type:'viewport'};
            }
            el=el.parentElement;
        }
        return null;
    }""")

def find_checkbox_rel():
    """精确定位复选框: 优先容器内真可点元素, 退化容器左缘+22px
    返回 {x, y} 屏幕绝对坐标"""
    return page.evaluate("""() => {
        const resp = document.querySelector('input[name="cf-turnstile-response"]');
        if(!resp) return null;
        let el = resp.parentElement;
        for(let k=0;k<6&&el;k++){
            const r=el.getBoundingClientRect();
            if(r.width>50&&r.height>30){
                const cands = [...el.querySelectorAll('a,button,[role="checkbox"],[class*="ctp-checkbox"],label,div[class*="checkbox"]')]
                    .filter(c => { const cr=c.getBoundingClientRect(); return cr.width>10&&cr.height>10; });
                if (cands.length) {
                    const c = cands[0].getBoundingClientRect();
                    return {x: Math.round(c.left + c.width/2 + window.screenX), y: Math.round(c.top + c.height/2 + window.screenY), type: 'found'};
                }
                return {x: Math.round(r.left + 22 + window.screenX), y: Math.round(r.top + r.height/2 + window.screenY), type: 'fallback'};
            }
            el=el.parentElement;
        }
        return null;
    }""")

def _xdotool_click(x, y):
    """xdotool物理点击屏幕坐标（绕过CF检测playwright合成事件）"""
    import subprocess
    # 不用 --sync，之前在 Xvfb 中会偶发卡死 timeout
    subprocess.run(['xdotool','mousemove',str(int(x)),str(int(y))],
                   capture_output=True, timeout=5)
    time.sleep(random.uniform(0.08,0.18))
    subprocess.run(['xdotool','click','1'], capture_output=True, timeout=5)
    time.sleep(random.uniform(0.35,0.55))

def detect_checkbox_by_screenshot():
    """从 X11 root 截图里识别 Turnstile checkbox，返回屏幕绝对坐标。"""
    try:
        from PIL import Image
    except Exception as e:
        print(f"[sso] PIL不可用，跳过视觉定位: {e}", flush=True)
        return None
    shot = '/tmp/grok_login_detect_root.png'
    subprocess.run(['bash','-lc',f'DISPLAY=:1 import -window root {shot}'], capture_output=True, timeout=5)
    if not os.path.exists(shot):
        print('[sso] root截图失败，跳过视觉定位', flush=True)
        return None
    im = Image.open(shot).convert('RGB')
    w,h = im.size
    pix = im.load()
    # 登录表单在左半边；Turnstile 位于中下区域。root截图坐标本身就是 X11 屏幕坐标。
    x0,x1 = 0, min(w, 680)
    y0,y1 = int(h*0.32), min(h, int(h*0.78))
    visited = set()
    comps = []
    def dark(x,y):
        r,g,b = pix[x,y]
        return r < 125 and g < 125 and b < 125
    for y in range(y0,y1):
        for x in range(x0,x1):
            if (x,y) in visited or not dark(x,y):
                continue
            q = deque([(x,y)]); visited.add((x,y))
            xs=[]; ys=[]; cnt=0
            while q:
                a,b = q.popleft(); xs.append(a); ys.append(b); cnt += 1
                for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                    nx,ny = a+dx, b+dy
                    if nx<x0 or nx>=x1 or ny<y0 or ny>=y1 or (nx,ny) in visited:
                        continue
                    if dark(nx,ny):
                        visited.add((nx,ny)); q.append((nx,ny))
            if cnt < 20:
                continue
            bx = (min(xs), min(ys), max(xs)+1, max(ys)+1)
            bw, bh = bx[2]-bx[0], bx[3]-bx[1]
            # checkbox 外框一般 20-30px，允许轻微缩放/抗锯齿
            if 14 <= bw <= 38 and 14 <= bh <= 38 and abs(bw-bh) <= 10:
                # 排除顶部 logo/眼睛图标等：Turnstile checkbox 应在左表单中下方
                cx, cy = (bx[0]+bx[2])//2, (bx[1]+bx[3])//2
                if 40 <= cx <= 330 and int(h*0.38) <= cy <= int(h*0.72):
                    comps.append((cnt,bx,bw,bh,cx,cy))
    if not comps:
        print('[sso] 视觉定位未找到checkbox候选', flush=True)
        return None
    # 评分：偏好 24x24 方框、位于登录页 Turnstile 预期区域
    def score(c):
        cnt,bx,bw,bh,cx,cy = c
        return abs(bw-24)+abs(bh-24)+abs(cx-200)/30+abs(cy-400)/60
    best = min(comps, key=score)
    cnt,bx,bw,bh,cx,cy = best
    print(f"[sso] 视觉定位checkbox center=({cx},{cy}) bbox={bx} size={bw}x{bh} candidates={len(comps)}", flush=True)
    return {'x': cx, 'y': cy, 'type': 'screenshot', 'bbox': bx}

def _click_turnstile_range(cx, cy):
    """Turnstile checkbox 会漂移 2-3px；围绕中心点做小范围点击。"""
    offsets = [(0,0), (-3,0), (3,0), (0,-3), (0,3), (-3,-3), (3,-3), (-3,3), (3,3)]
    for dx, dy in offsets:
        x, y = cx + dx, cy + dy
        print(f"[sso] range-click ({x},{y}) offset=({dx},{dy})", flush=True)
        _xdotool_click(x, y)
        page.wait_for_timeout(700)
        t = native_token()
        if len(t) > 10:
            print(f"[sso] range-click 命中 token len={len(t)}", flush=True)
            return t
    return native_token()

def _mouse_click_viewport(cx, cy):
    """CDP trusted 鼠标点击 (视口坐标, 与注册v7同款, 不依赖窗口位置)"""
    page.mouse.move(random.randint(200,600), random.randint(200,400))
    time.sleep(random.uniform(0.3,0.6))
    page.mouse.move(cx+random.randint(-3,3), cy+random.randint(-3,3))
    time.sleep(random.uniform(0.2,0.4))
    page.mouse.click(cx, cy)
    time.sleep(random.uniform(0.3,0.6))

def click_native_widget(rounds=6):
    """主路径: CDP 视口坐标点击 (trusted事件). 兜底: 截图定位+xdotool物理点击"""
    import subprocess
    page.evaluate("""() => {
        const resp = document.querySelector('input[name="cf-turnstile-response"]');
        resp && resp.scrollIntoView({block:'center',behavior:'instant'});
    }""")
    page.wait_for_timeout(1500)
    for i in range(rounds):
        t = native_token()
        if len(t) > 10:
            print(f"[sso] 第{i+1}轮找到token, len={len(t)}", flush=True)
            return t, i+1
        # 主路径: 视口坐标 CDP 点击 (与注册v7一致, 实测零漂移)
        box = find_checkbox_viewport()
        if box:
            print(f"[sso] 第{i+1}轮 视口坐标点击 ({box['x']},{box['y']}) type=viewport", flush=True)
            _mouse_click_viewport(box['x'], box['y'])
            page.wait_for_timeout(2500)
            t2 = native_token()
            if len(t2) > 10:
                print(f"[sso] ✅ 视口点击命中 token len={len(t2)}", flush=True)
                return t2, i+1
        # 兜底: 截图视觉定位 + xdotool 物理点击 (视口点击失败时才走)
        box = find_checkbox_rel()
        if not box:
            print(f"[sso] 第{i+1}轮未找到checkbox, box={box}", flush=True)
            break
        try:
            shot_box = detect_checkbox_by_screenshot()
            if shot_box:
                x, y = shot_box['x'], shot_box['y']
                print(f"[sso] 兜底: 点击checkbox范围 center=({x}, {y}) type=screenshot-detect", flush=True)
            else:
                # 最后兜底：旧固定坐标。正常不应走到这里。
                x, y = 153, 548
                print(f"[sso] 截图定位失败，兜底点击范围 center=({x}, {y})", flush=True)
            t2 = _click_turnstile_range(x, y)
            if len(t2) > 10:
                return t2, i+1
        except Exception as e:
            print(f"[sso] 点击失败: {e}", flush=True)
        page.wait_for_timeout(2500)
    return native_token(), rounds

# 填表
click_btn("email")
page.wait_for_timeout(4000)
page.locator('input[type="email"]').first.fill(EMAIL)
page.wait_for_timeout(800)
click_btn("Next")
page.wait_for_timeout(4000)
page.locator('input[type="password"]').first.fill(PW)
page.wait_for_timeout(800)
click_btn("Reject All")   # 关 OneTrust Cookie 弹窗
page.wait_for_timeout(3500)  # 等 Turnstile 渲染 (CF 人机交互在输入密码后才出现, 需要3秒+)
print("[sso] 等待Turnstile渲染完成...", flush=True)

# Turnstile (原生空壳, 加固)
token, rounds = click_native_widget(rounds=6)
print(f"Turnstile token len={len(token)} (点击{rounds}轮)", flush=True)

# 自动点击失败时，进入 noVNC 手动点击等待模式
if len(token) <= 50 and os.environ.get('MANUAL_TURNSTILE', '0') == '1':
    wait_sec = int(os.environ.get('MANUAL_TURNSTILE_TIMEOUT', '180'))
    print(f"[sso] 自动点击未拿到 token，进入手动模式：请在 noVNC 点击 Turnstile，最多等待 {wait_sec}s", flush=True)
    deadline = time.time() + wait_sec
    last_log = 0
    while time.time() < deadline:
        token = native_token()
        if len(token) > 50:
            print(f"[sso] ✅ 手动点击后检测到 token len={len(token)}", flush=True)
            break
        if time.time() - last_log > 10:
            print(f"[sso] 等待手动 Turnstile... token_len={len(token)}", flush=True)
            last_log = time.time()
        time.sleep(1)
    print(f"Turnstile token len={len(token)} (manual wait done)", flush=True)

# 点 Login
click_btn("^Login$") or click_btn("Login")
page.wait_for_timeout(12000)

# 抓 sso
sso = ""
try:
    for c in page.context.cookies():
        if c['name'] == 'sso':
            sso = c['value']; break
except: pass

if sso:
    open(OUT, "w").write(sso)
    print(f"[sso] ✅ 写入 {OUT}, sso_len={len(sso)}", flush=True)
else:
    print(f"[sso] ❌ 无 sso (token len={len(token)})", flush=True)
    page.screenshot(path="/tmp/grok_login_fail.png")

# 清理浏览器: 默认自动关闭, 除非 KEEP_BROWSER=1 (手动调试保留)
# (重登队列串行, 每次处理完必须清理, 避免残留占9222端口)
if os.environ.get('KEEP_BROWSER', '0') != '1':
    try:
        browser.close()
        print("[sso] ✅ 浏览器已关闭", flush=True)
    except Exception as e:
        print(f"[sso] 关闭浏览器异常: {e}", flush=True)
    # 兜底 pkill 残留 chromium (9222)
    try:
        import subprocess as _sp
        _sp.run(['pkill', '-f', 'remote-debugging-port=9222'], capture_output=True)
        _sp.run(['pkill', '-f', '--fingerprint-platform=macos'], capture_output=True)
    except Exception:
        pass
if not sso:
    sys.exit(1)