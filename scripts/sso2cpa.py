"""SSO → OAuth → CPA (CBA) 格式转换
参考项目: grok-reg 的 internal/oauth + internal/cpa
用法: python3 sso2cpa.py <SSO> [email]
"""
import sys, json, time, re, base64, urllib.parse, struct
from curl_cffi import requests as cffi_requests

# ===================== 常量 =====================
ClientID  = "b1a00492-073a-47ea-816f-4c329264a828"
Scope     = "openid profile email offline_access grok-cli:access api:access"
UA        = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
ClientVersion = "0.2.93"
BASE_URL  = "https://cli-chat-proxy.grok.com/v1"

# ===================== 工具 =====================
def b64url_decode(s):
    s += '=' * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)

def jwt_payload(jwt):
    try:
        return json.loads(b64url_decode(jwt.split('.')[1]))
    except Exception as e:
        print(f"  decode jwt payload error: {e}")
        return {}

def jwt_claim(jwt, key):
    try:
        p = jwt_payload(jwt)
        if key in p:
            return p[key]
        for nest in ('user', 'account', 'identity', 'profile'):
            if isinstance(p.get(nest), dict):
                sub = p[nest]
                for k in ('sub', 'id', 'user_id', 'userId', 'uid'):
                    if k in sub and sub[k]:
                        return sub[k]
        return ''
    except Exception:
        return ''

def principal_from_sso(sso):
    """从 SSO JWT 提取 principal_id（对应 Go 里的 principalFromSSO）"""
    keys = ('sub', 'user_id', 'userId', 'uid', 'id', 'principal_id', 'principalId')
    for k in keys:
        v = jwt_claim(sso, k)
        if v:
            return v
    return ''

def merge_cookies(old, headers):
    """合并 Set-Cookie（简单版）"""
    out = old
    for k, v in headers.items():
        if k.lower() == 'set-cookie':
            # 只取 name=value 部分
            parts = v.split(';')
            cookie = parts[0].strip()
            if cookie and '=' in cookie:
                out += '; ' + cookie
    return out

def is_sign_in_redirect(loc):
    low = loc.lower()
    return '/sign-in' in low or '/login' in low or 'signin' in low or 'login_required' in low

def is_device_done(loc):
    if not loc:
        return False
    return '/device/done' in loc.lower() or '/oauth2/device/done' in loc.lower()

def abs_url(base_host, loc):
    if not loc:
        return ''
    # 已经绝对 URL
    if loc.startswith('http://') or loc.startswith('https://'):
        return loc
    # 相对 URL
    return f"{base_host.rstrip('/')}/{loc.lstrip('/')}"

# ===================== 核心流程 =====================
def discover(s):
    r = s.get('https://auth.x.ai/.well-known/openid-configuration', timeout=20)
    d = r.json()
    return d.get('device_authorization_endpoint',''), d.get('token_endpoint',''), d.get('authorization_endpoint','')

def start_device_flow(s, dev_ep, scope, client_id):
    """POST device/code, 返回 device_code/user_code/verification_uri_complete/expires_in/interval/token_endpoint"""
    r = s.post(dev_ep,
               data={'client_id': client_id, 'scope': scope},
               timeout=20, allow_redirects=False)
    if r.status_code != 200:
        return None, f"device/code HTTP {r.status_code}: {r.text[:200]}"
    d = r.json()
    return {
        'device_code': d['device_code'],
        'user_code': d['user_code'],
        'verification_uri': d.get('verification_uri', 'https://accounts.x.ai/oauth2/device'),
        'verification_uri_complete': d.get('verification_uri_complete', ''),
        'expires_in': d.get('expires_in', 1800),
        'interval': d.get('interval', 5),
        'token_endpoint': d.get('token_endpoint', ''),  # 可能为空, 用 discover 结果
    }, None

def confirm_http(s, sso, flow, tok_ep, debug=False):
    """用 sso cookie 完成 device verify + consent approve + 跟随 redirect
    返回: 错误字符串 (成功时返回 None 或 "")"""
    cookie = f"sso={sso}"
    dev_code = flow['device_code']
    user_code = flow['user_code']
    verify_url = 'https://auth.x.ai/oauth2/device/verify'
    approve_url = 'https://auth.x.ai/oauth2/device/approve'
    principal_id = principal_from_sso(sso)
    
    # 1. verify (POST /oauth2/device/verify 带 user_code + sso cookie)
    verify_data = urllib.parse.urlencode({'user_code': user_code})
    headers1 = {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie}
    r1 = s.post(verify_url, data=verify_data, headers=headers1, timeout=20, allow_redirects=False)
    cookie = merge_cookies(cookie, r1.headers)
    loc1 = r1.headers.get('Location', '')
    body1 = r1.text
    print(f"  verify: HTTP {r1.status_code}, loc={loc1[:80]}", flush=True)
    if r1.status_code == 403:
        return "challenge (403 on verify)"
    if is_sign_in_redirect(loc1):
        return f"sso_rejected verify→{loc1}"
    if is_device_done(loc1):
        return None  # 成功
    if r1.status_code not in (301,302,303,307,308):
        return f"verify unexpected status={r1.status_code}"
    
    # 2. 跟随 consent 页面 (可能是 GET)
    consent_url = abs_url('https://accounts.x.ai', loc1)
    if not consent_url:
        consent_url = f"https://accounts.x.ai/oauth2/device/consent?user_code={user_code}"
    print(f"  consent_url: {consent_url[:80]}", flush=True)
    
    # 3. approve (POST /oauth2/device/approve 带表单)
    aform = urllib.parse.urlencode({
        'user_code': user_code,
        'action': 'allow',
        'principal_type': 'User',
        'principal_id': principal_id,
    })
    headers2 = {'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie, 'Origin': 'https://accounts.x.ai'}
    r2 = s.post(approve_url, data=aform, headers=headers2, timeout=20, allow_redirects=False)
    cookie = merge_cookies(cookie, r2.headers)
    loc2 = r2.headers.get('Location', '')
    body2 = r2.text
    print(f"  approve: HTTP {r2.status_code}, loc={loc2[:80]}", flush=True)
    if r2.status_code == 403:
        return "challenge (403 on approve)"
    if is_sign_in_redirect(loc2):
        return f"sso_rejected approve→{loc2}"
    if is_device_done(loc2):
        return None  # 成功
    if r2.status_code not in (301,302,303,307,308):
        return f"approve unexpected status={r2.status_code}"
    
    # 4. 跟随最终 redirect（device/done 页面）
    final_url = abs_url('https://auth.x.ai', loc2)
    if not final_url:
        return "approve no redirect"
    print(f"  final url: {final_url[:80]}", flush=True)
    r3 = s.get(final_url, timeout=20)
    if is_device_done(r3.url):
        return None  # 成功
    print(f"  final: HTTP {r3.status_code} url={r3.url[:80]}", flush=True)
    return "device approve incomplete"

def poll_token(s, flow, tok_ep, debug=False):
    """轮询 token endpoint 直到拿到 access_token/refresh_token"""
    interval = max(float(flow.get('interval', 5)), 5)
    deadline = time.time() + int(flow.get('expires_in', 1800))
    token_ep = tok_ep or flow.get('token_endpoint', 'https://auth.x.ai/oauth2/token')
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        form = urllib.parse.urlencode({
            'client_id': ClientID,
            'device_code': flow['device_code'],
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
        })
        headers3 = {'Content-Type': 'application/x-www-form-urlencoded'}
        r = s.post(token_ep, data=form, headers=headers3, timeout=30)
        if r.status_code == 200:
            d = r.json()
            at = d.get('access_token', '')
            rt = d.get('refresh_token', '')
            idt = d.get('id_token', '')
            tt = d.get('token_type', 'Bearer')
            exp = int(d.get('expires_in', 3600))
            if at and rt:
                print(f"  poll_token ✅ 成功 (attempt={attempt})", flush=True)
                return at, rt, idt, tt, exp, token_ep
            else:
                print(f"  poll_token ⚠️ 响应缺 token: {list(d.keys())[:5]}", flush=True)
                return None, None, None, None, None, None
        err_code = d.get('error', '') if 'error' in r.text else ''
        if err_code == 'authorization_pending':
            wait = max(interval, 5)
            print(f"  poll_token [{attempt}] authorization_pending, wait {wait}s", flush=True)
            time.sleep(wait)
        elif err_code == 'slow_down':
            interval += 1
            print(f"  poll_token [{attempt}] slow_down", flush=True)
            time.sleep(max(interval, 5))
        elif err_code == 'access_denied':
            print(f"  poll_token ❌ access_denied", flush=True)
            return None, None, None, None, None, None
        elif err_code == 'expired_token':
            print(f"  poll_token ❌ expired", flush=True)
            return None, None, None, None, None, None
        else:
            print(f"  poll_token ❌ {r.status_code} {r.text[:100]}", flush=True)
            return None, None, None, None, None, None
    return None, None, None, None, None, None

def cpa_document(access_token, refresh_token, id_token, token_endpoint, subject, email):
    """转 cpa.Document 格式 (CBA 可导入)"""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc = {
        'type': 'xai',
        'access_token': access_token,
        'refresh_token': refresh_token,
        'id_token': id_token,
        'token_type': 'Bearer',
        'expires_in': 3600,
        'expired': now,
        'last_refresh': now,
        'sub': subject,
        'email': email,
        'base_url': BASE_URL,
        'token_endpoint': token_endpoint,
        'auth_kind': 'oauth',
        'headers': {
            'x-grok-client-version': ClientVersion,
            'x-xai-token-auth': 'xai-grok-cli',
            'X-XAI-Token-Auth': 'xai-grok-cli',
            'x-authenticateresponse': 'authenticate-response',
            'x-grok-client-identifier': 'grok-shell',
            'x-compaction-at': '400000',
            'User-Agent': f'grok-shell/{ClientVersion} (linux; x86_64)',
        }
    }
    return doc

def main():
    if len(sys.argv) < 2:
        print("用法: python3 sso2cpa.py <SSO> [email]")
        sys.exit(1)
    sso = sys.argv[1].strip()
    email = sys.argv[2] if len(sys.argv) > 2 else ''
    # 代理: 默认 mihomo:8001 (容器内 mihomo 容器名), 可传第3参覆盖
    _proxy = sys.argv[3] if len(sys.argv) > 3 else 'mihomo:8001'
    if '://' not in _proxy and ':' in _proxy:
        _proxy = f'http://{_proxy}'
    elif '://' not in _proxy:
        _proxy = f'http://127.0.0.1:{_proxy}'
    
    print(f"=== SSO → OAuth → CPA ===")
    print(f"SSO len: {len(sso)}, email: {email or '(from JWT)'}")
    
    # 从 SSO 提取 principal_id 和 email
    principal_id = principal_from_sso(sso)
    print(f"principal_id: {principal_id or '未提取'}", flush=True)
    
    # 初始化 session
    s = cffi_requests.Session(impersonate='chrome131')
    s.proxies = {'http': _proxy, 'https': _proxy}
    s.headers.update({
        'user-agent': UA,
        'accept': '*/*',
        'origin': 'https://accounts.x.ai',
        'referer': 'https://accounts.x.ai/',
    })
    
    # 1. discover
    print("\n[1] discover...", flush=True)
    dev_ep, tok_ep, auth_ep = discover(s)
    print(f"  device_ep: {dev_ep[:60]}")
    print(f"  token_ep:  {tok_ep[:60]}")
    
    # 2. start device flow
    print("\n[2] start_device_flow...", flush=True)
    flow, err = start_device_flow(s, dev_ep, Scope, ClientID)
    if err:
        print(f"❌ {err}"); sys.exit(1)
    print(f"  device_code: {flow['device_code'][:20]}...")
    print(f"  user_code: {flow['user_code']}")
    print(f"  verification_uri: {flow['verification_uri']}")
    print(f"  expires_in: {flow['expires_in']}s", flush=True)
    
    # 3. confirm_http (sso cookie + verify + approve)
    print("\n[3] confirm_http (sso→consent)...", flush=True)
    err = confirm_http(s, sso, flow, tok_ep)
    if err and err != "":
        print(f"❌ confirm_http: {err}")
        sys.exit(1)
    print(f"  ✅ consent 授权通过", flush=True)
    
    # 4. poll token
    print("\n[4] poll_token...", flush=True)
    at, rt, idt, tt, exp, te = poll_token(s, flow, tok_ep)
    if not at or not rt:
        print(f"❌ poll_token 失败"); sys.exit(1)
    print(f"  access_token: {at[:30]}...")
    print(f"  refresh_token: {rt[:30]}...", flush=True)
    
    # 5. 提取 subject 和 email
    subj = principal_id or jwt_claim(sso, 'sub') or jwt_claim(at, 'sub')
    sub_email = email or jwt_claim(sso, 'email') or jwt_claim(at, 'email') or jwt_claim(idt, 'email')
    print(f"\n[5] subject={subj[:30] if subj else ''}, email={sub_email[:30] if sub_email else ''}", flush=True)
    
    # 6. 生成 cpa document
    doc = cpa_document(at, rt, idt, te, subj, sub_email)
    # 文件名去重: 用完整 email 或 sso 前缀
    import hashlib
    name_hash = hashlib.md5(sub_email.encode()).hexdigest()[:8]
    doc_path = f"/tmp/cpa_{sub_email}_{name_hash}.json"
    with open(doc_path, 'w') as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"\n✅ CPA 格式已保存: {doc_path}")
    print(f"  type: {doc['type']}")
    print(f"  base_url: {doc['base_url']}")
    print(f"  token_endpoint: {doc['token_endpoint']}")
    print(f"  expires_in: {doc['expires_in']}s")
    print(f"  headers keys: {list(doc['headers'].keys())}")
    print(f"\n完整 JSON 已保存，可直接导入 CBA 或参考项目。")
    return doc

if __name__ == '__main__':
    main()
