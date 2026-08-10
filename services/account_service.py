"""Grok 账号管理平台 - 账号服务 (导入/状态/续期/额度)"""
import json, time, os, glob, subprocess, threading, sys, uuid, re, random
from curl_cffi import requests as cffi
from db import get_conn
import config
from services.registration_service import get_node_proxy

# 全局降级登录锁: 同一时间只允许一个降级登录(浏览器/xdotool/9222端口互斥)
DEGRADE_LOCK = threading.Lock()

# ============ 账号导入 ============

def import_cpa_file(filepath):
    """导入一个 CPA 文件到数据库"""
    try:
        with open(filepath) as f:
            doc = json.load(f)
        email = doc.get('email', '')
        if not email:
            return None, "CPA缺少email字段"
        
        ctx = doc.get('registration_context', {})
        conn = get_conn()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        
        # 密码回退: 顶层 password 缺失时读 registration_context.password (旧版脚本只存后者)
        password = doc.get('password', '') or ctx.get('password', '')
        if not password:
            # 统一默认密码 (注册脚本 grok_auto_v6 的固定密码)
            password = ''
        
        # quota 保留: CPA 文件通常没有 quota 数据, 不能覆盖数据库已有的额度
        # 先查已有记录, 保留旧 quota 除非 CPA 显式提供
        quota_json = json.dumps(doc.get('quota', {}))
        existing = conn.execute("SELECT quota FROM accounts WHERE email=?", (email,)).fetchone()
        if existing and existing[0] and (not doc.get('quota')):
            quota_json = existing[0]  # 保留数据库已有的额度
        
        conn.execute("""
        INSERT OR REPLACE INTO accounts
        (email, password, access_token, refresh_token, id_token, base_url,
         status, pool_status, expires_in, expired, last_refresh, quota,
         node_port, fingerprint_seed, timezone, registered_at, last_check, source)
        VALUES (?,?,?,?,?,?, 'active','active',?,?,?,?,?,?,?,?,?, 'cpa')
        """, (
            email, password,
            doc.get('access_token',''), doc.get('refresh_token',''), doc.get('id_token',''),
            doc.get('base_url', 'https://cli-chat-proxy.grok.com/v1'),
            doc.get('expires_in', 21600), doc.get('expired',''), doc.get('last_refresh',''),
            quota_json,
            str(ctx.get('node_port','')), str(ctx.get('fingerprint_seed','')), ctx.get('timezone',''),
            doc.get('registered_at', now), now
        ))
        conn.commit()
        conn.close()
        # 新导入账号: 立即后台刷新一次额度 (CPA文件无quota字段, 不刷新则前端显示"可用")
        try:
            from services.api_service import _refresh_single_quota
            threading.Thread(target=_refresh_single_quota, args=(email,), daemon=True).start()
        except Exception:
            pass
        return email, None
    except Exception as e:
        return None, str(e)

def import_all_cpa():
    """从 /root/grok_accounts/cpa 导入所有 CPA"""
    if not os.path.isdir(config.CPA_DIR):
        return 0, "CPA目录不存在"
    imported = 0
    errors = []
    for f in sorted(glob.glob(os.path.join(config.CPA_DIR, '*.json'))):
        email, err = import_cpa_file(f)
        if email:
            imported += 1
        elif err:
            errors.append(f"{os.path.basename(f)}: {err}")
    return imported, errors

# ============ 账号查询 ============

def list_accounts(status=None, pool_status=None):
    conn = get_conn()
    sql = "SELECT * FROM accounts"
    params = []
    conds = []
    if status:
        conds.append("status=?")
        params.append(status)
    if pool_status:
        conds.append("pool_status=?")
        params.append(pool_status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return rows

def get_account(email):
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
    conn.close()
    return dict(row) if row else None

# ============ 状态检查 / 续期 ============

def check_account_status(email):
    """检查账号 AT 是否有效, 返回状态"""
    acc = get_account(email)
    if not acc:
        return None
    
    at = acc.get('access_token', '')
    if not at:
        # 无 access_token 视为失效, 同步更新数据库状态 (否则前端列表与测活结果不一致)
        conn = get_conn()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute("UPDATE accounts SET status='expired', pool_status='expired', last_check=? WHERE email=?", (now, email))
        conn.commit()
        conn.close()
        # AT为空但RT还在: 先试 RT 续期; RT 失效则自动降级重登
        # 修复: 之前直接返回 expired, 账号不会自动恢复
        try:
            rt = acc.get('refresh_token', '')
            node = acc.get('node_port', '8078') or '8078'
            if rt:
                rr = refresh_with_rt(email, rt, node)
                if rr.get('ok'):
                    print(f"[check] {email} 无AT但RT续期成功", flush=True)
                    return {'status': 'active', 'reason': 'RT refreshed', 'code': 200}
                print(f"[check] {email} 无AT且RT续期失败 → 降级重登", flush=True)
            rd = refresh_degrade(email)
            if rd.get('ok'):
                return {'status': 'degrading', 'reason': '降级重登已启动', 'code': 0}
        except Exception as e:
            print(f"[check] {email} 无AT自动恢复异常: {e}", flush=True)
        return {'status': 'expired', 'reason': 'no access_token'}
    
    # 调 models 探测额度/有效性
    result = probe_models(email)
    return result

def probe_models(email):
    """探测账号的 /models 可用性, 更新状态和额度"""
    import curl_cffi.requests as cffi
    acc = get_account(email)
    if not acc:
        return {'status': 'error', 'reason': 'account not found'}
    
    at = acc.get('access_token', '')
    node = acc.get('node_port', '8078') or '8078'
    
    ports = [node] + ['8078','8081','8082','8083','8084','8085','8086','8087','8089','8090','8091','8092']
    for port in ports:
        try:
            s = cffi.Session(impersonate='chrome131')
            p_url, _ = get_node_proxy(str(port))
            s.proxies = {'http': p_url, 'https': p_url}
            r = s.get('https://cli-chat-proxy.grok.com/v1/models',
                     headers={'Authorization': f'Bearer {at}',
                              'x-xai-token-auth': 'xai-grok-cli'},
                     timeout=15)
            if r.status_code == 200:
                # 更新状态
                conn = get_conn()
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                conn.execute("UPDATE accounts SET status='active', pool_status='active', last_check=? WHERE email=?", (now, email))
                conn.commit()
                conn.close()
                models = [m.get('id') for m in (r.json().get('data') or [])]
                return {'status': 'active', 'models': models[:5], 'code': 200}
            elif r.status_code == 401:
                conn = get_conn()
                conn.execute("UPDATE accounts SET status='expired', pool_status='expired' WHERE email=?", (email,))
                conn.commit()
                conn.close()
                # AT失效: 尝试 RT 续期 (纯API秒级); RT也失效则自动降级重登 (浏览器)
                # 修复: 之前只标 expired 就完事, 账号会一直躺着直到手动重登
                try:
                    rt = acc.get('refresh_token', '')
                    if rt:
                        rr = refresh_with_rt(email, rt, node)
                        if rr.get('ok'):
                            print(f"[check] {email} 测活401后RT续期成功", flush=True)
                            return {'status': 'active', 'reason': 'RT refreshed', 'code': 200}
                        print(f"[check] {email} 测活401且RT续期失败 → 降级重登", flush=True)
                    rd = refresh_degrade(email)
                    if rd.get('ok'):
                        return {'status': 'degrading', 'reason': '降级重登已启动', 'code': 401}
                    print(f"[check] {email} 降级重登未启动: {str(rd.get('msg'))[:80]}", flush=True)
                except Exception as e:
                    print(f"[check] {email} 401自动恢复异常: {e}", flush=True)
                return {'status': 'expired', 'reason': 'unauthorized (AT invalid)', 'code': 401}
            else:
                # 429 或 403 尝试下个节点
                continue
        except Exception:
            continue
    
    conn = get_conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("UPDATE accounts SET last_check=? WHERE email=?", (now, email))
    conn.commit()
    conn.close()
    return {'status': 'error', 'reason': f'all {len(ports)} nodes failed'}

# ============ AT 续期 ============

def refresh_account(email):
    """用 refresh_token 续期 AT; 若 RT 也失效, 走降级登录"""
    acc = get_account(email)
    if not acc:
        return {'ok': False, 'msg': 'account not found'}
    
    rt = acc.get('refresh_token', '')
    at = acc.get('access_token', '')
    node = acc.get('node_port', '') or '8078'
    
    # 先尝试用 RT 直接换 AT
    if rt and at:
        result = refresh_with_rt(email, rt, node)
        if result.get('ok'):
            return result
        print(f"[refresh] {email} RT 换 AT 失败: {result.get('msg')}, 走降级登录")
    
    # RT 也失效 → 降级登录 (浏览器)
    return refresh_degrade(email)

def refresh_with_rt(email, rt, node):
    """用 refresh_token 换新 access_token，并同步 DB + CPA 文件
    RT 续期是纯 API 调用(无浏览器指纹), 节点不重要, 按延迟/可用性尝试
    """
    import curl_cffi.requests as cffi
    try:
        from services.api_service import _get_usable_nodes, _get_node_speed_score
        ports = sorted(_get_usable_nodes(), key=lambda p: _get_node_speed_score(p))
    except Exception:
        ports = [str(node)] + ['8078','8081','8082','8083','8084','8085','8086','8087','8089','8090','8091','8092']
    ports = list(dict.fromkeys([str(p) for p in ports if str(p)]))[:8]
    for port in ports:
        try:
            s = cffi.Session(impersonate='chrome131')
            p_url, _ = get_node_proxy(str(port))
            s.proxies = {'http': p_url, 'https': p_url}
            r = s.post('https://auth.x.ai/oauth2/token',
                      data={'grant_type': 'refresh_token',
                            'refresh_token': rt,
                            'client_id': 'b1a00492-073a-47ea-816f-4c329264a828'},
                      timeout=20)
            if r.status_code == 200:
                d = r.json()
                new_at = d.get('access_token', '')
                new_rt = d.get('refresh_token', rt)
                new_idt = d.get('id_token', '')
                exp = int(d.get('expires_in', 3600) or 3600)
                now_ts = int(time.time())
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
                expired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts + exp))

                # 同步 CPA 文件，防止后续导入旧 CPA 覆盖 DB
                cpa_file = find_cpa_file(email)
                if cpa_file:
                    try:
                        doc = json.load(open(cpa_file))
                        doc['access_token'] = new_at
                        doc['refresh_token'] = new_rt
                        if new_idt:
                            doc['id_token'] = new_idt
                        doc['expires_in'] = exp
                        doc['expired'] = expired
                        doc['last_refresh'] = now
                        ctx = doc.get('registration_context', {})
                        ctx['node_port'] = str(port)
                        doc['registration_context'] = ctx
                        with open(cpa_file, 'w') as f:
                            json.dump(doc, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"  [refresh] 写回CPA失败 {email}: {e}", flush=True)

                conn = get_conn()
                conn.execute("""
                UPDATE accounts SET access_token=?, refresh_token=?, id_token=?, status='active', pool_status='active',
                expires_in=?, expired=?, last_refresh=?, node_port=? WHERE email=?
                """, (new_at, new_rt, new_idt or None, exp, expired, now, str(port), email))
                conn.commit()
                conn.close()
                return {'ok': True, 'msg': 'RT refreshed', 'at_len': len(new_at), 'expires_in': exp, 'expired': expired, 'node_port': str(port)}
            else:
                print(f"  [refresh] port{port} {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"  [refresh] port{port} err: {e}")
    return {'ok': False, 'msg': 'all ports RT failed'}

def refresh_degrade(email):
    """降级登录: 调用 refresh_cpa.py"""
    acc = get_account(email)
    cpa_file = find_cpa_file(email)
    if not cpa_file:
        return {'ok': False, 'msg': 'no cpa file found for degrade login'}
    
    # 后台线程执行降级登录 (加全局锁DEGRADE_LOCK, 同一时间只放一个, 避免抢9222端口+xdotool)
    def run():
        # 等待浏览器锁 (注册可能占用, 等待至多10分钟)
        if not DEGRADE_LOCK.acquire(timeout=600):
            print(f"[degrade] {email} 等待浏览器资源超时, 跳过", flush=True)
            return
        try:
            print(f"[degrade] {email} 开始降级登录", flush=True)
            output = subprocess.run(
                ['/usr/bin/python3.11', config.REFRESH_SCRIPT, cpa_file],
                capture_output=True, text=True, timeout=420, env={**os.environ, 'DISPLAY': ':1', 'MANUAL_TURNSTILE': '1', 'MANUAL_TURNSTILE_TIMEOUT': '240'},
                cwd=config.CPA_DIR)
            # 打印结果
            print(f"[degrade] {email} 完成 rc={output.returncode}", flush=True)
            # 完成标志: refresh_cpa 成功会写 "=== 完成 ==="
            if '=== 完成 ===' in output.stdout or '✅ CPA 已更新' in output.stdout or '✅ SSO 获取成功' in output.stdout:
                # 重新导入该CPA刷新数据库
                import_cpa_file(cpa_file)
                # 从CPA文件读取最新 expired 时间
                try:
                    new_doc = json.load(open(cpa_file))
                    new_exp = new_doc.get('expired', '')
                    new_at = new_doc.get('access_token', '')
                    conn = get_conn()
                    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    conn.execute("UPDATE accounts SET status='active', pool_status='active', last_refresh=?, expired=?, access_token=? WHERE email=?",
                                (now, new_exp, new_at, email))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"[relogin] 更新expired失败: {e}", flush=True)
                # 重登成功: 后台刷新额度 (新AT 立即拉取 quota 数据)
                try:
                    from services.api_service import _refresh_single_quota
                    threading.Thread(target=_refresh_single_quota, args=(email,), daemon=True).start()
                except Exception as e:
                    print(f"[relogin] 刷新额度失败: {e}", flush=True)
                _relogin_locks[email] = {'status': 'success', 'progress': 100, 'log': ['降级登录成功'], 'email': email}
                print(f"[relogin] {email} 成功", flush=True)
            else:
                print(f"[degrade] {email} 降级登录失败:\n{output.stdout[-300:]}", flush=True)
        except Exception as e:
            print(f"[degrade] {email} 异常: {e}", flush=True)
        finally:
            try: DEGRADE_LOCK.release()
            except: pass
    
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return {'ok': True, 'msg': 'degrade login started (thread)'}

def find_cpa_file(email):
    if not os.path.isdir(config.CPA_DIR):
        return None
    for f in glob.glob(os.path.join(config.CPA_DIR, f'cpa_{email}*.json')):
        return f
    return None

# ============ 额度查询 ============

def get_quota(email):
    """查询账号额度 (真实探测)"""
    import curl_cffi.requests as cffi
    acc = get_account(email)
    if not acc:
        return {'error': 'account not found'}
    
    at = acc.get('access_token', '')
    node = acc.get('node_port', '') or '8078'
    
    # 尝试探测额度 API
    endpoints = [
        'https://api.x.ai/v1/user/quota',
        'https://cli-chat-proxy.grok.com/v1/quota',
    ]
    ports = [node] + ['8078','8081','8082','8083','8084','8085','8086','8087','8089','8090','8091','8092']
    for endpoint in endpoints:
        for port in ports:
            try:
                s = cffi.Session(impersonate='chrome131')
                p_url, _ = get_node_proxy(str(port))
                s.proxies = {'http': p_url, 'https': p_url}
                r = s.get(endpoint,
                         headers={'Authorization': f'Bearer {at}', 'x-xai-token-auth': 'xai-grok-cli'},
                         timeout=15)
                if r.status_code == 200:
                    quota = r.json()
                    conn = get_conn()
                    conn.execute("UPDATE accounts SET quota=? WHERE email=?", (json.dumps(quota), email))
                    conn.commit()
                    conn.close()
                    return {'email': email, 'quota': quota}
            except Exception:
                continue
    return {'email': email, 'quota': 'unknown', 'note': '免费账号常无额度API, 参考 /models 200 即可用'}


# ============ 新增: 续期(仅RT)/重登/测活/删除 ============

def refresh_with_rt_only(email):
    """只用 RT 刷新 AT (不降级登录), 失败返回明确错误"""
    acc = get_account(email)
    if not acc:
        return {'ok': False, 'msg': 'account not found'}
    rt = acc.get('refresh_token', '')
    if not rt:
        return {'ok': False, 'msg': 'no refresh_token'}
    node = acc.get('node_port', '') or '8078'
    return refresh_with_rt(email, rt, node)


def relogin_start(email):
    """启动降级登录 (重登), 返回 task_id"""
    acc = get_account(email)
    if not acc:
        return {'ok': False, 'msg': 'account not found'}
    task_id = f"relogin_{uuid.uuid4().hex[:8]}"
    task = {
        'task_id': task_id, 'email': email, 'status': 'running',
        'progress': 0, 'log': [], 'started': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _relogin_locks[email] = task
    threading.Thread(target=_run_relogin, args=(task_id, email), daemon=True).start()
    return {'ok': True, 'task_id': task_id}


_relogin_locks = {}  # email -> task dict (单个重登进度)
# ===== 重登队列 (批量模式) =====
_relogin_queue = []  # [(email, task_id), ...]
_queue_lock = threading.Lock()
_queue_worker_running = False
_queue_cancel = False
_queue_progress = {'total': 0, 'done': 0, 'failed': 0, 'current': '', 'status': 'idle'}


def relogin_queue_status():
    """返回队列整体进度 + 当前账号的逐步日志"""
    with _queue_lock:
        state = dict(_queue_progress)
    # 附加当前正在处理的账号的日志
    current = state.get('current', '')
    if current and current in _relogin_locks:
        state['current_log'] = _relogin_locks[current].get('log', [])
        state['current_progress'] = _relogin_locks[current].get('progress', 0)
    else:
        state['current_log'] = []
        state['current_progress'] = 0
    return state


def relogin_queue_cancel():
    """取消队列中剩余任务"""
    global _queue_cancel
    _queue_cancel = True
    with _queue_lock:
        _queue_progress['status'] = 'cancelling'
    return {'ok': True, 'msg': '已取消'}


def relogin_all():
    """一键重登: 将所有 expired/cooling 账号加入队列, 由工作线程顺序处理"""
    global _queue_cancel
    _queue_cancel = False
    conn = get_conn()
    rows = conn.execute(
        "SELECT email FROM accounts WHERE status IN ('expired','cooling','banned') ORDER BY id"
    ).fetchall()
    conn.close()
    if not rows:
        return {'ok': True, 'msg': '没有过期账号', 'count': 0}
    emails = [r['email'] for r in rows]
    tasks = []
    with _queue_lock:
        for email in emails:
            task_id = f"relogin_{uuid.uuid4().hex[:8]}"
            _relogin_locks[email] = {
                'task_id': task_id, 'email': email, 'status': 'queued',
                'progress': 0, 'log': ['排队中'],
            }
            _relogin_queue.append((email, task_id))
            tasks.append(task_id)
        _queue_progress.update({
            'total': len(emails), 'done': 0, 'failed': 0,
            'current': emails[0] if emails else '',
            'status': 'running',
        })
    # 启动工作线程 (如果没在运行)
    _start_queue_worker()
    return {'ok': True, 'msg': f'已加入队列 {len(emails)} 个', 'count': len(emails), 'task_ids': tasks}


def _start_queue_worker():
    global _queue_worker_running
    with _queue_lock:
        if _queue_worker_running:
            return
        _queue_worker_running = True
    threading.Thread(target=_queue_worker, daemon=True).start()


def _queue_worker():
    """队列工作线程: 顺序处理每个重登任务"""
    global _queue_worker_running, _queue_cancel
    try:
        while True:
            item = None
            with _queue_lock:
                if not _relogin_queue or _queue_cancel:
                    _queue_progress['status'] = 'idle' if not _queue_cancel else 'cancelled'
                    _queue_worker_running = False
                    return
                item = _relogin_queue.pop(0)
            if not item:
                break
            email, task_id = item
            with _queue_lock:
                _queue_progress['current'] = email
                _queue_progress['done_count'] = _queue_progress.get('done', 0) + _queue_progress.get('failed', 0)
            # 执行单个重登
            _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'status': 'running', 'log': ['开始降级登录'], 'progress': 10}
            try:
                _run_relogin(task_id, email)
            except Exception as e:
                _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'status': 'failed', 'log': [str(e)], 'progress': 100}
            # 更新计数
            with _queue_lock:
                status = _relogin_locks.get(email, {}).get('status', 'failed')
                if status == 'active' or '成功' in str(_relogin_locks.get(email, {}).get('log', [])):
                    _queue_progress['done'] = _queue_progress.get('done', 0) + 1
                else:
                    _queue_progress['failed'] = _queue_progress.get('failed', 0) + 1
    finally:
        with _queue_lock:
            _queue_worker_running = False
            if _queue_progress['status'] != 'cancelled':
                _queue_progress['status'] = 'idle'


def relogin_start(email):
    """启动单个降级登录 (重登), 返回 task_id"""
    acc = get_account(email)
    if not acc:
        return {'ok': False, 'msg': 'account not found'}
    task_id = f"relogin_{uuid.uuid4().hex[:8]}"
    task = {
        'task_id': task_id, 'email': email, 'status': 'running',
        'progress': 0, 'log': [], 'started': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _relogin_locks[email] = task
    threading.Thread(target=_run_relogin, args=(task_id, email), daemon=True).start()
    return {'ok': True, 'task_id': task_id}


def _run_relogin(task_id, email):
    """降级登录, 失败自动换IP再试一次"""
    acc = get_account(email)
    cpa_file = find_cpa_file(email)
    if not cpa_file:
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'status': 'failed', 'log': ['❌ CPA文件不存在'], 'progress': 100}
        return
    lock = DEGRADE_LOCK
    # 等待浏览器锁 (注册可能占用, 等待至多10分钟; 与注册互斥共用 9222+Xvfb)
    if not lock.acquire(timeout=600):
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'status': 'busy', 'log': ['⏳ 等待浏览器资源超时(10分钟), 重登放弃'], 'progress': 0}
        return
    try:
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 10, 'log': ['📋 读取CPA配置...']}
        # 从 CPA 文件读取 email/password/node_port/seed/timezone
        doc = json.load(open(cpa_file))
        email_addr = doc.get('email', email)
        pw = doc.get('password', '') or acc.get('password', '')
        node_port = doc.get('registration_context', {}).get('node_port', acc.get('node_port', '8078'))
        seed = random.randint(10000, 99999)
        tz = doc.get('registration_context', {}).get('timezone', 'America/New_York')
        sso_out = cpa_file.replace('.json', '_sso.txt')

        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 15, 'log': [
            '📋 CPA配置读取完成',
            f'  邮箱: {email_addr}',
            f'  节点: {node_port}',
            f'  指纹seed: {seed}',
        ]}
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 20, 'log': [
            '📋 CPA配置读取完成',
            f'  邮箱: {email_addr}',
            f'  节点: {node_port}',
            f'  指纹seed: {seed}',
            '🌐 启动Chrome浏览器 (Mac指纹)...',
        ]}

        # 调用降级登录脚本 (grok_login_sso.py)
        output = subprocess.run(
            ['/usr/bin/python3.11', config.DEGRADE_SCRIPT, email_addr, pw, sso_out, str(node_port), str(seed), tz],
            capture_output=True, text=True, timeout=420, env={**os.environ, 'DISPLAY': ':1', 'MANUAL_TURNSTILE': '1', 'MANUAL_TURNSTILE_TIMEOUT': '240'},
            cwd=config.CPA_DIR)
        # 解析脚本输出提取关键步骤
        stdout = output.stdout or ''
        steps = []
        for line in stdout.splitlines():
            line = line.strip()
            if line and len(line) > 3:
                steps.append(line)
        if steps:
            _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 70, 'log': steps[-8:]}  # 保留最后8步

        if os.path.exists(sso_out) and os.path.getsize(sso_out) > 0:
            try:
                with open(sso_out) as f:
                    sso = f.read().strip()
                if sso and len(sso) > 100:
                    _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 80, 'log': steps[-8:] + ['🔑 SSO获取成功, 转换OAuth...']}
                    _import_sso_to_cpa(cpa_file, sso)
                    _relogin_success(email, cpa_file)
                    _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 100, 'log': steps[-8:] + [
                        '🔑 SSO获取成功, 转换OAuth...',
                        '✅ 重登成功! CPA已更新',
                    ]}
                    return
            except Exception as e:
                print(f"[relogin] SSO 导入失败: {e}", flush=True)
        # 检查 stdout 中的成功标志
        if '✅ CPA 已更新' in stdout or '✅ SSO 获取成功' in stdout or '=== 完成 ===' in stdout:
            _relogin_success(email, cpa_file)
            _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 100, 'log': steps[-8:] + ['✅ 重登成功! CPA已更新']}
            return
        # 第1次失败, 换IP再试
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 30, 'log': steps[-8:] + ['⚠️ 第1次失败, 换IP重试 (第2次)...']}
        alt_node = _get_alternate_node(email)
        if alt_node:
            _switch_cpa_node(cpa_file, alt_node)
            output2 = subprocess.run(
                ['/usr/bin/python3.11', config.DEGRADE_SCRIPT, email_addr, pw, sso_out, str(alt_node), str(random.randint(10000, 99999)), tz],
                capture_output=True, text=True, timeout=420, env={**os.environ, 'DISPLAY': ':1', 'MANUAL_TURNSTILE': '1', 'MANUAL_TURNSTILE_TIMEOUT': '240'},
                cwd=config.CPA_DIR)
            stdout2 = output2.stdout or ''
            steps2 = [l.strip() for l in stdout2.splitlines() if l.strip() and len(l.strip()) > 3]
            if os.path.exists(sso_out) and os.path.getsize(sso_out) > 0:
                try:
                    with open(sso_out) as f:
                        sso = f.read().strip()
                    if sso and len(sso) > 100:
                        _import_sso_to_cpa(cpa_file, sso)
                        _relogin_success(email, cpa_file)
                        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 100, 'log': steps2[-8:] + ['✅ 重登成功! CPA已更新 (换IP后)']}
                        return
                except Exception as e:
                    print(f"[relogin] SSO 导入失败: {e}", flush=True)
            if '✅ CPA 已更新' in stdout2 or '✅ SSO 获取成功' in stdout2 or '=== 完成 ===' in stdout2:
                _relogin_success(email, cpa_file)
                _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'progress': 100, 'log': steps2[-8:] + ['✅ 重登成功! CPA已更新 (换IP后)']}
                return
        # 两次都失败
        _relogin_locks[email] = {'status': 'failed', 'progress': 100, 'log': (steps[-5:] if steps else ['❌ 两次降级登录均失败']), 'email': email}
        print(f"[relogin] {email} 两次均失败", flush=True)
    except Exception as e:
        _relogin_locks[email] = {**_relogin_locks.get(email, {}), 'status': 'failed', 'progress': 100, 'log': [f'❌ 异常: {e}'], 'email': email}
        print(f"[relogin] {email} 异常: {e}", flush=True)
    finally:
        lock.release()
        # 重登结束兜底清理: 确保残留 chromium 被清除 (脚本已自行清理, 这里双保险)
        try:
            subprocess.run(['pkill', '-f', 'remote-debugging-port=9222'],
                           capture_output=True, timeout=5)
        except Exception:
            pass


def _import_sso_to_cpa(cpa_file, sso):
    """从 SSO cookie 生成 CPA 文件"""
    try:
        doc = json.load(open(cpa_file))
        # 调用 OAuth 流程转换 SSO 为 CPA
        result = _sso_to_oauth(sso, doc)
        if result:
            doc.update(result)
            with open(cpa_file, 'w') as f:
                json.dump(doc, f, indent=2, ensure_ascii=False)
            print(f"[relogin] CPA 已更新: {cpa_file}", flush=True)
            return True
        return False
    except Exception as e:
        print(f"[relogin] SSO 导入失败: {e}", flush=True)
        return False


def _sso_to_oauth(sso, doc):
    """从 SSO 获取 OAuth token 并更新 CPA (复用已验证的 /tmp/sso2cpa.py 流程)"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('sso2cpa', '/tmp/sso2cpa.py')
        sso2cpa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sso2cpa)

        s = cffi.Session(impersonate='chrome131')
        p_url, _ = get_node_proxy('8078')
        s.proxies = {'http': p_url, 'https': p_url}
        s.headers.update({
            'user-agent': sso2cpa.UA,
            'accept': '*/*',
            'origin': 'https://accounts.x.ai',
            'referer': 'https://accounts.x.ai/',
        })

        dev_ep, tok_ep, auth_ep = sso2cpa.discover(s)
        flow, err = sso2cpa.start_device_flow(s, dev_ep, sso2cpa.Scope, sso2cpa.ClientID)
        if err:
            print(f"[relogin] device/code 失败: {err}", flush=True)
            return None

        err = sso2cpa.confirm_http(s, sso, flow, tok_ep)
        if err:
            print(f"[relogin] confirm_http 失败: {err}", flush=True)
            return None

        at, rt, idt, tt, exp, te = sso2cpa.poll_token(s, flow, tok_ep)
        if not at or not rt:
            print("[relogin] poll_token 失败", flush=True)
            return None

        now_ts = int(time.time())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
        expired = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts + int(exp or 3600)))
        print(f"[relogin] OAuth 成功: AT len={len(at)}", flush=True)
        return {
            'access_token': at,
            'refresh_token': rt,
            'id_token': idt,
            'token_type': tt or 'Bearer',
            'expires_in': int(exp or 3600),
            'expired': expired,
            'last_refresh': now,
            'base_url': 'https://cli-chat-proxy.grok.com/v1',
            'token_endpoint': te or tok_ep,
            'auth_kind': 'oauth',
            'headers': {
                'x-grok-client-version': '0.2.93',
                'x-xai-token-auth': 'xai-grok-cli',
                'X-XAI-Token-Auth': 'xai-grok-cli',
                'x-authenticateresponse': 'authenticate-response',
                'x-grok-client-identifier': 'grok-shell',
                'x-compaction-at': '400000',
                'User-Agent': 'grok-shell/0.2.93 (linux; x86_64)',
            }
        }
    except Exception as e:
        print(f"[relogin] OAuth 失败: {e}", flush=True)
        return None


def _get_alternate_node(email):
    """获取一个不同于当前节点的备用节点 (排除死节点, 按延迟优先)"""
    acc = get_account(email)
    current = str(acc.get('node_port', ''))
    try:
        from services.api_service import _get_usable_nodes, _get_node_speed_score
        candidates = sorted(_get_usable_nodes(), key=lambda p: _get_node_speed_score(p))
    except Exception:
        candidates = ['8078','8081','8082','8083','8084','8085','8086','8087','8089','8090','8091','8092']
    for n in candidates:
        if str(n) != current:
            return str(n)
    return candidates[0] if candidates else '8078'


def _switch_cpa_node(cpa_file, new_port):
    """临时切换 CPA 的节点端口"""
    try:
        doc = json.load(open(cpa_file))
        ctx = doc.get('registration_context', {})
        ctx['node_port'] = new_port
        doc['registration_context'] = ctx
        json.dump(doc, open(cpa_file, 'w'), indent=2, ensure_ascii=False)
        print(f"[relogin] 切换节点到 {new_port}", flush=True)
    except Exception as e:
        print(f"[relogin] 切换节点失败: {e}", flush=True)


def _relogin_success(email, cpa_file):
    """降级登录成功后的统一处理"""
    import_cpa_file(cpa_file)
    try:
        new_doc = json.load(open(cpa_file))
        new_exp = new_doc.get('expired', '')
        new_at = new_doc.get('access_token', '')
        conn = get_conn()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute("UPDATE accounts SET status='active', pool_status='active', last_refresh=?, expired=?, access_token=? WHERE email=?",
                    (now, new_exp, new_at, email))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[relogin] 更新expired失败: {e}", flush=True)
    _relogin_locks[email] = {'status': 'success', 'progress': 100, 'log': ['降级登录成功'], 'email': email}
    print(f"[relogin] {email} 成功", flush=True)


def relogin_status(email):
    return _relogin_locks.get(email, {'status': 'idle', 'progress': 0, 'log': []})


def check_all_sync():
    """异步测活全部账号, 返回任务ID (后台线程执行, 支持过程日志)"""
    results = []
    task_id = f"check_all_{int(time.time())}"
    def run():
        accs = list_accounts()
        log = []
        for idx, acc in enumerate(accs):
            email = acc['email']
            r = check_account_status(email)
            results.append({'email': email, 'result': r})
            st = (r or {}).get('status', 'error')
            mark = '✅' if st == 'active' else ('❌' if st in ('expired','banned') else '⚠️')
            reason = (r or {}).get('reason', '')
            log.append(f"{mark} [{idx+1}/{len(accs)}] {email}: {st}" + (f" ({reason[:60]})" if reason else ""))
            _check_all_results[task_id] = {'status':'running','results':results,'total':len(accs),'current_log':list(log)}
        _check_all_results[task_id] = {'status':'done','results':results,'total':len(accs),'current_log':log}
    _check_all_results[task_id] = {'status':'running','results':[],'total':0,'current_log':[]}
    threading.Thread(target=run, daemon=True).start()
    return {'task_id': task_id, 'status': 'running', 'total': len(list_accounts())}

_check_all_results = {}

def get_check_all_result(task_id):
    """查询测活任务进度"""
    return _check_all_results.get(task_id, {'status': 'unknown', 'results': [], 'total': 0, 'current_log': []})


def refresh_all_inactive():
    """批量续期所有失活账号 (仅RT刷新, 不降级), 后台线程执行, 支持过程日志"""
    results = []
    task_id = f"refresh_all_{int(time.time())}"
    def run():
        inactive = [a for a in list_accounts() if a['status'] in ('expired', 'cooling', 'banned')]
        log = []
        for idx, acc in enumerate(inactive):
            email = acc['email']
            r = refresh_with_rt_only(email)
            results.append({'email': email, 'result': r})
            mark = '✅' if r.get('ok') else '❌'
            msg = r.get('msg', '')[:80]
            log.append(f"{mark} [{idx+1}/{len(inactive)}] {email}: {msg}")
            _refresh_all_results[task_id] = {'status':'running','results':results,'total':len(inactive),'current_log':list(log)}
        _refresh_all_results[task_id] = {'status':'done','results':results,'total':len(inactive),'current_log':log}
    _refresh_all_results[task_id] = {'status':'running','results':[],'total':0,'current_log':[]}
    threading.Thread(target=run, daemon=True).start()
    return {'task_id': task_id, 'status': 'running', 'total': len([a for a in list_accounts() if a['status'] in ('expired','cooling','banned')])}

_refresh_all_results = {}

def get_refresh_all_result(task_id):
    return _refresh_all_results.get(task_id, {'status': 'unknown', 'results': [], 'total': 0})


def delete_account(email):
    """删除账号 (从数据库+CPA文件)"""
    acc = get_account(email)
    if not acc:
        return {'ok': False, 'msg': 'not found'}
    cpa_file = find_cpa_file(email)
    if cpa_file and os.path.exists(cpa_file):
        os.remove(cpa_file)
    conn = get_conn()
    conn.execute("DELETE FROM accounts WHERE email=?", (email,))
    conn.commit(); conn.close()
    return {'ok': True, 'msg': 'deleted'}
