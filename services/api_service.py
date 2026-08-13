"""Grok 账号管理平台 - API 转发服务 (OpenAI 兼容)"""
import json, time, random, threading, datetime
from db import get_conn
from services import account_service

# ============ 全局 Token Bucket (限流) ============
_token_bucket_lock = threading.Lock()
_token_bucket_count = 0  # 当前并发请求数
_MAX_CONCURRENT = 10  # 最大并发请求数

def check_token_bucket():
    """检查是否还能处理请求，返回 True 表示可以处理"""
    global _token_bucket_count
    with _token_bucket_lock:
        if _token_bucket_count >= _MAX_CONCURRENT:
            return False
        _token_bucket_count += 1
        return True

def release_token_bucket():
    """释放 token"""
    global _token_bucket_count
    with _token_bucket_lock:
        if _token_bucket_count > 0:
            _token_bucket_count -= 1


# ============ 连接池 (复用 Session) ============
_session_pool = {}  # port -> (session, last_used_ts)
_pool_lock = threading.Lock()
_POOL_TTL = 300  # 5分钟过期

def _get_session(port):
    """获取或创建复用的 curl_cffi Session"""
    now = time.time()
    with _pool_lock:
        if port in _session_pool:
            s, ts = _session_pool[port]
            if now - ts < _POOL_TTL:
                _session_pool[port] = (s, now)
                return s
        from curl_cffi import requests as cffi
        s = cffi.Session(impersonate='chrome131')
        _session_pool[port] = (s, now)
        # 清理过期 session
        expired = [k for k, (_, t) in _session_pool.items() if now - t > _POOL_TTL]
        for k in expired:
            del _session_pool[k]
    return s

# ============ 节点延迟缓存 ============
_node_latency = {}  # port -> avg_latency_ms
_latency_lock = threading.Lock()

def _update_latency(port, latency_ms):
    with _latency_lock:
        old = _node_latency.get(port, [])
        old.append(latency_ms)
        _node_latency[port] = old[-10:]  # 保留最近10次

def _get_node_speed_score(port):
    """返回节点速度分 (越小越快), 无记录返回大数(排最后)"""
    with _latency_lock:
        latencies = _node_latency.get(port, [])
    if not latencies:
        return 999999  # 未探测节点排最后
    return sum(latencies) / len(latencies)

# ============ 账号选择 ============
_plock = threading.Lock()
_last_picked_idx = 0

# 缓存: usage 统计每60秒刷新一次
_usage_cache = {'counts': {}, 'ts': 0}
_usage_lock = threading.Lock()

def _get_usage_counts():
    """获取 usage 统计 (带缓存)"""
    now = time.time()
    with _usage_lock:
        if now - _usage_cache['ts'] < 60 and _usage_cache['counts']:
            return _usage_cache['counts']
    # 重新统计
    conn = get_conn()
    counts = {}
    for r in conn.execute("SELECT account_email, COUNT(*) as c FROM usage GROUP BY account_email").fetchall():
        counts[r['account_email']] = r['c']
    conn.close()
    with _usage_lock:
        _usage_cache['counts'] = counts
        _usage_cache['ts'] = now
    return counts

def _pick_round_robin(conn):
    global _last_picked_idx
    rows = conn.execute("""
        SELECT email, id FROM accounts
        WHERE status='active' AND pool_status='active' AND access_token != ''
        ORDER BY id
    """).fetchall()
    if not rows:
        return None
    with _plock:
        _last_picked_idx = (_last_picked_idx + 1) % len(rows)
        pick = rows[_last_picked_idx]
    row = conn.execute("SELECT * FROM accounts WHERE email=?", (pick['email'],)).fetchone()
    return dict(row) if row else None

# 账号 429 冷却: email -> cooldown_until_ts (内存级, 429后30分钟不参与调度)
_quota_429_cooldown = {}
_quota_429_lock = threading.Lock()

# 账号 busy 锁: email -> bool (一个账号同时只处理一个请求, 防止并发重复选中同一账号)
_account_busy = set()
_account_busy_lock = threading.Lock()

def _account_busy_mark(email):
    """尝试占用账号, 成功返回True (已被其他请求占用返回False)"""
    with _account_busy_lock:
        if email in _account_busy:
            return False
        _account_busy.add(email)
        return True

def _account_busy_release(email):
    with _account_busy_lock:
        _account_busy.discard(email)

def _mark_429_cooldown(email, seconds=1800):
    """标记账号 429 冷却 (实时生效, 不依赖数据库quota值)"""
    with _quota_429_lock:
        _quota_429_cooldown[email] = time.time() + seconds

def _is_429_cooled(email):
    """检查账号是否在 429 冷却期"""
    with _quota_429_lock:
        until = _quota_429_cooldown.get(email, 0)
        if until > time.time():
            return True
        if until and until <= time.time():
            _quota_429_cooldown.pop(email, None)
        return False

def _pick_least_used(conn):
    """智能调度: 优先选剩余额度高的账号 (结合 usage + quota)"""
    rows = conn.execute("""
        SELECT * FROM accounts
        WHERE status='active' AND pool_status='active' AND access_token != ''
        ORDER BY id
    """).fetchall()
    if not rows:
        return None
    counts = _get_usage_counts()
    # 获取所有账号的 quota 数据 (使用 row[0] 和 row[1] 因为 fetchall 返回 tuple)
    quotas = {}
    quota_rows = conn.execute("SELECT email, quota FROM accounts WHERE status='active' AND pool_status='active'").fetchall()
    for r in quota_rows:
        try:
            q = json.loads(r[1]) if r[1] else {}  # r[1] 是 quota 列
            quotas[r[0]] = q  # r[0] 是 email 列
        except:
            quotas[r[0]] = {}
    # 计算每个账号的得分: 请求次数 + 额度消耗比例
    scores = []
    for row in rows:
        email = row['email']
        # 429 冷却期内跳过 (实时生效)
        if _is_429_cooled(email):
            continue
        # busy 锁: 已被其他请求占用的账号跳过 (避免并发重复选中)
        if email in _account_busy:
            continue
        usage_count = counts.get(email, 0)
        quota = quotas.get(email, {})
        remaining = quota.get('remaining_tokens', None)
        limit = quota.get('limit_tokens', 0)
        # 额度明确为0 (429耗尽) → 跳过
        if remaining is not None and remaining == 0:
            continue
        # 额度未知 (quota为空) → 视为可用, 得分中额度部分给中性值
        if remaining is None or limit == 0:
            score = usage_count * 1000 + 50  # 未知额度给中性分
            scores.append((score, dict(row)))
            continue
        # 得分 = 请求次数 * 1000 + (1 - 剩余比例) * 100
        # 优先选请求次数少且剩余额度高的账号
        usage_score = usage_count * 1000
        pct = remaining / limit if limit > 0 else 0
        quota_score = int((1 - pct) * 100)
        score = usage_score + quota_score
        scores.append((score, dict(row)))
    # 如果没有有效额度的账号，回退到原始逻辑
    if not scores:
        scores = [(counts.get(row['email'], 0) * 1000, dict(row)) for row in rows]
    # 按得分排序，选最低的
    scores.sort(key=lambda x: x[0])
    return scores[0][1] if scores else None

def pick_account(strategy='round_robin'):
    conn = get_conn()
    try:
        if strategy == 'least_used':
            return _pick_least_used(conn)
        elif strategy == 'random':
            rows = conn.execute("""
                SELECT * FROM accounts
                WHERE status='active' AND pool_status='active' AND access_token != ''
                ORDER BY id
            """).fetchall()
            return dict(random.choice(rows)) if rows else None
        else:
            return _pick_round_robin(conn)
    finally:
        conn.close()

def get_dispatchable_count():
    """可调度账号数: active + pool active + 有AT + 额度未耗尽(remaining_tokens>0 或 未知额度)"""
    conn = get_conn()
    r = conn.execute("""
        SELECT COUNT(*) as c FROM accounts
        WHERE status='active' AND pool_status='active' AND access_token != ''
        AND (
            quota IS NULL OR quota = '' OR quota = '{}'
            OR json_extract(quota, '$.remaining_tokens') IS NULL
            OR CAST(json_extract(quota, '$.remaining_tokens') AS INTEGER) > 0
        )
    """).fetchone()
    conn.close()
    return r['c'] if r else 0

# ============ 死节点管理 ============
_dead_nodes = {}  # port -> dead_since_ts
_dead_lock = threading.Lock()
_DEAD_TTL = 300  # 死节点5分钟后复活重试

def _mark_dead(port):
    """标记节点死亡 (连不上/超时)"""
    if port:
        with _dead_lock:
            _dead_nodes[str(port)] = time.time()

def _mark_alive(port):
    """节点恢复"""
    with _dead_lock:
        _dead_nodes.pop(str(port), None)

def _is_dead(port):
    """判断节点是否在死亡名单中 (5分钟后自动复活)"""
    p = str(port)
    now = time.time()
    with _dead_lock:
        ts = _dead_nodes.get(p)
        if ts is None:
            return False
        if now - ts > _DEAD_TTL:
            _dead_nodes.pop(p, None)  # 自动复活
            return False
        return True

def _get_usable_nodes():
    """获取可用节点列表: 排除死节点"""
    try:
        from services.registration_service import get_active_nodes
        nodes = get_active_nodes()
        node_list = [str(n) for n in nodes]
    except Exception:
        node_list = ['8078', '8081', '8082', '8083', '8085', '8086', '8087', '8089', '8091', '8092']
    alive = [p for p in node_list if not _is_dead(p)]
    # 如果全被标记死 (异常情况), 重置全部复活
    if not alive:
        with _dead_lock:
            _dead_nodes.clear()
        alive = node_list
    return alive

def chat_completion(body, api_key=''):
    """转发聊天请求到 Grok. 账号级重试 + 延迟感知节点 + 死节点剔除 + 401自动续期"""
    # 限流检查
    if not check_token_bucket():
        return None, {'error': {'message': '服务繁忙，请稍后重试', 'type': 'rate_limited'}, 'code': 503}
    try:
        # 无效模型自动回退 grok-4.5 (实测免费账号只有 grok-4.5 有额度)
        model = _resolve_model(body.get('model'))
        upstream_url = 'https://cli-chat-proxy.grok.com/v1/chat/completions'
        stream = body.get('stream', False)

        # 节点选择: 纯按延迟排序 (排除死节点 + 未探测排最后)
        node_list = _get_usable_nodes()
        ports = sorted(node_list, key=lambda p: _get_node_speed_score(p))
        ports = ports[:5]  # 每个账号最多试5个节点

        # 账号级重试: 最多尝试3个不同账号 (401/429/节点失败换账号)
        for _attempt in range(3):
            account = pick_account('least_used')
            if not account:
                return None, {'error': {'message': '无可用账号(全部冷却/过期)', 'type': 'no_account'}, 'code': 503}
            # 占用账号 busy 锁 (防止其他并发请求重复选中同一账号)
            if not _account_busy_mark(account['email']):
                continue  # 竞争失败, 换账号
            try:
                at = account['access_token']
                result = _try_account(account, at, ports, model, upstream_url, stream, body, api_key)
                if result is not None:
                    return result
            finally:
                _account_busy_release(account['email'])
        return None, {'error': {'message': '所有账号/节点请求失败', 'type': 'node_exhausted'}, 'code': 502}
    finally:
        release_token_bucket()


def _try_account(account, at, ports, model, upstream_url, stream, body, api_key):
    """用单个账号尝试所有节点, 返回 (data, None) 或 None(换账号)"""
    import time as _t
    account_failed = False
    for port in ports:
        if account_failed:
            break
        try:
            s = _get_session(port)
            from services.registration_service import get_node_proxy
            p_url, _ = get_node_proxy(str(port))
            s.proxies = {'http': p_url, 'https': p_url}
            headers = {
                'Authorization': f'Bearer {at}',
                'X-XAI-Token-Auth': 'xai-grok-cli',
                'x-grok-client-version': '0.2.93',
                'x-grok-client-identifier': 'grok-shell',
                'User-Agent': 'grok-cli/0.2.93',
                'Content-Type': 'application/json',
            }
            payload = dict(body)
            payload['model'] = model
            if stream:
                payload['stream'] = True

            t0 = _t.time()
            r = s.post(upstream_url, json=payload, headers=headers, timeout=30, stream=stream)
            latency = int((_t.time() - t0) * 1000)
            _update_latency(port, latency)

            if r.status_code in (200, 201):
                _mark_alive(port)
                _record_quota(account['email'], r.headers)
                if stream:
                    # 预读第一个 chunk 判断是否合法 SSE (上游偶发 200+纯文本错误 "stream mode is not enabled")
                    it = r.iter_content(chunk_size=1024)
                    try:
                        _first = next(it)
                    except StopIteration:
                        _first = b''
                    if _first and _first.lstrip().startswith(b'data:'):
                        # 合法 SSE 流: 原样透传 (把预读的 chunk 一起带上)
                        def _sse_pass():
                            if _first:
                                yield _first
                            for c in it:
                                yield c
                        return None, {'stream': _sse_pass(), 'status': r.status_code,
                                      'headers': dict(r.headers), 'account': account['email'], 'node': port,
                                      'api_key': api_key, 'model': model}
                    # 非法 SSE: 上游不支持流式, 降级为非流式重发并包装成 SSE 返回
                    try:
                        _rest = (_first + b''.join(it)) if _first else b''.join(it)
                    except Exception:
                        _rest = _first or b''
                    print(f"[api] {account['email']} 流式响应非SSE({_rest[:60]!r}), 降级非流式重发", flush=True)
                    payload_ns = dict(body)
                    payload_ns['model'] = model
                    payload_ns['stream'] = False
                    r2 = s.post(upstream_url, json=payload_ns, headers=headers, timeout=30)
                    if r2.status_code in (200, 201):
                        data2 = r2.json()
                        # 包装成 OpenAI SSE chunk
                        _content = ((data2.get('choices') or [{}])[0].get('message', {}) or {}).get('content', '')
                        _chunk = {
                            'id': data2.get('id', 'chatcmpl-degrade'),
                            'object': 'chat.completion.chunk',
                            'created': int(_t.time()),
                            'model': model,
                            'choices': [{'index': 0, 'delta': {'content': _content}, 'finish_reason': 'stop'}],
                        }
                        _sse_bytes = f'data: {json.dumps(_chunk, ensure_ascii=False)}\n\n'.encode('utf-8')
                        def _sse_wrapped():
                            yield _sse_bytes
                            yield b'data: [DONE]\n\n'
                        return None, {'stream': _sse_wrapped(), 'status': r2.status_code,
                                      'headers': dict(r2.headers), 'account': account['email'], 'node': port,
                                      'api_key': api_key, 'model': model}
                    # 降级也失败 → 换账号重试
                    return None
                else:
                    data = r.json()
                    try:
                        conn = get_conn()
                        import datetime
                        u = data.get('usage') or {}
                        conn.execute("INSERT INTO usage (api_key, account_email, model, prompt_tokens, completion_tokens, total_tokens, request_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (api_key, account['email'], model,
                                      u.get('prompt_tokens', 0), u.get('completion_tokens', 0),
                                      u.get('total_tokens', 0), 1,
                                      datetime.datetime.utcnow().isoformat() + 'Z'))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
                    # 请求完成: 立即失效 usage 缓存 (下次pick能看到最新计数)
                    with _usage_lock:
                        _usage_cache['ts'] = 0
                    data['_account'] = account['email']
                    data['_node'] = port
                    return data, None
            elif r.status_code in (401, 403):
                # token 失效: 立即用 RT 尝试续期 (AT失效但RT通常有效)
                try:
                    from services.account_service import refresh_with_rt
                    rr = refresh_with_rt(account['email'], account.get('refresh_token', ''), port)
                    if rr.get('ok'):
                        print(f"[api] {account['email']} 401后RT续期成功", flush=True)
                        _update_quota_cache(account['email'])
                        try:
                            from services.account_service import get_account as _ga
                            fresh = _ga(account['email'])
                            if fresh and fresh.get('access_token'):
                                at = fresh['access_token']
                                continue  # 新AT试下一个节点
                        except Exception:
                            pass
                        return None  # 无法续期AT, 换账号
                    else:
                        # RT 也失效 → 立即触发降级重登 (浏览器登录拿新RT, 与注册互斥)
                        # RT被服务端吊销是最隐蔽的故障: AT已401, 调度器(只扫active+有AT)不会碰它
                        # 必须在这里主动拉起重登, 否则账号会一直cooling直到手动恢复
                        print(f"[api] {account['email']} 401且RT失效 → 触发降级重登", flush=True)
                        conn = get_conn()
                        conn.execute("UPDATE accounts SET status='cooling', pool_status='cooling', quota=? WHERE email=?",
                                     (json.dumps({'remaining_tokens': 0, 'limit_tokens': 1000000,
                                                  'remaining_requests': 0, 'limit_requests': 21}), account['email']))
                        conn.commit()
                        conn.close()
                        _update_quota_cache(account['email'])
                        try:
                            from services.account_service import refresh_degrade
                            rd = refresh_degrade(account['email'])
                            if rd.get('ok'):
                                print(f"[api] {account['email']} 降级重登已启动: {rd.get('task_id','')}", flush=True)
                            else:
                                print(f"[api] {account['email']} 降级重登未启动: {str(rd.get('msg'))[:80]}", flush=True)
                        except Exception as e:
                            print(f"[api] {account['email']} 降级重登异常: {e}", flush=True)
                        return None
                except Exception as e:
                    print(f"[api] {account['email']} 401处理异常: {e}", flush=True)
                    return None
            elif r.status_code == 429:
                # 额度耗尽: 记录 quota=0 + 30分钟冷却, 换账号
                _record_quota(account['email'], r.headers)
                _mark_429_cooldown(account['email'])
                print(f"[api] {account['email']} 429额度耗尽, 冷却30min, 换账号", flush=True)
                return None
            else:
                # 其他错误 (5xx等): 试下一个节点
                print(f"[api] {account['email']} 节点{port} {r.status_code}, 试下一节点", flush=True)
                continue
        except Exception as e:
            _mark_dead(port)  # 连不上/超时 → 标记死节点
            continue
    return None  # 该账号所有节点失败, 换账号


_models_cache = {'ts': 0, 'ids': []}

def list_models():
    """返回上游 Grok 当前可用模型 (动态探测, 5分钟缓存, 失败回退内置列表)"""
    now = time.time()
    if _models_cache['ids'] and now - _models_cache['ts'] < 300:
        return _models_cache['ids']
    ids = _probe_upstream_models()
    if ids:
        _models_cache['ts'] = now
        _models_cache['ids'] = ids
        return ids
    # 回退: 内置默认 (上游探测失败时)
    return _BUILTIN_MODELS

_BUILTIN_MODELS = [
    {
        "id": "grok-4.6",
        "object": "model",
        "owned_by": "xai",
        "active": True,
        "created": 1740000000,
        "modalities": {
            "input": ["text", "image", "pdf"],
            "output": ["text"]
        },
        "attachment": True,
        "reasoning": True,
        "temperature": True,
        "tool_call": True,
        "limit": {
            "context": 1000000,
            "output": 32768
        },
    },
    {
        "id": "grok-4.5",
        "object": "model",
        "owned_by": "xai",
        "active": True,
        "created": 1740000000,
        "modalities": {
            "input": ["text", "image", "pdf"],
            "output": ["text"]
        },
        "attachment": True,
        "reasoning": True,
        "temperature": True,
        "tool_call": True,
        "limit": {
            "context": 1000000,
            "output": 32768
        },
    },
]

def _probe_upstream_models():
    """真实探测上游 /v1/models, 返回模型描述列表 (失败返回 [])"""
    try:
        from services.account_service import get_account
        from services.registration_service import get_node_proxy
        from curl_cffi import requests as cffi
        # 取一个 active 账号探测 (复用调度逻辑取最近使用账号)
        try:
            acc = pick_account('least_used')
        except Exception:
            acc = None
        if not acc:
            return []
        at = acc.get('access_token', '')
        if not at:
            return []
        node = acc.get('node_port', 'mihomo:8001') or 'mihomo:8001'
        s = cffi.Session(impersonate='chrome131')
        p_url, _ = get_node_proxy(str(node))
        s.proxies = {'http': p_url, 'https': p_url}
        r = s.get('https://cli-chat-proxy.grok.com/v1/models',
                  headers={'Authorization': f'Bearer {at}', 'X-XAI-Token-Auth': 'xai-grok-cli'},
                  timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        return [
            {
                "id": m.get('id', ''),
                "object": "model",
                "owned_by": m.get('owned_by', 'xai'),
                "active": True,
                "created": m.get('created', 1740000000),
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "attachment": True,
                "reasoning": True,
                "temperature": True,
                "tool_call": True,
                "limit": {"context": 1000000, "output": 32768},
            }
            for m in data.get('data', []) if m.get('id')
        ]
    except Exception as e:
        print(f"[models] 上游模型探测失败: {e}", flush=True)
        return []

# 模型名 → 实际可用模型 映射 (无效模型自动回退 grok-4.6)
_MODEL_ALIASES = {
    'grok-2': 'grok-4.6',
    'grok-2-latest': 'grok-4.6',
    'grok-3': 'grok-4.6',
    'grok-3-mini': 'grok-4.6',
    'grok-3-fast': 'grok-4.6',
    'grok-4': 'grok-4.6',
    'grok-4-fast': 'grok-4.6',
    'grok-4.5': 'grok-4.6',
    'grok-4.6': 'grok-4.6',
}

def _resolve_model(model):
    """把无效模型名映射到 grok-4.6"""
    if not model:
        return 'grok-4.6'
    return _MODEL_ALIASES.get(model, 'grok-4.6')

# ============ 用量统计 ============

def usage_summary():
    """用量统计汇总: 总量/今日/按模型/按账号/按Key (匹配 usage 表真实结构)"""
    conn = get_conn()
    try:
        import datetime
        today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat() + 'Z'

        total_req = conn.execute("SELECT COUNT(*) as c FROM usage").fetchone()['c'] or 0
        total_t = conn.execute("SELECT COALESCE(SUM(total_tokens),0) as s FROM usage").fetchone()['s'] or 0
        total_in = conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) as s FROM usage").fetchone()['s'] or 0
        total_out = conn.execute("SELECT COALESCE(SUM(completion_tokens),0) as s FROM usage").fetchone()['s'] or 0
        today_req = conn.execute("SELECT COUNT(*) as c FROM usage WHERE created_at >= ?", (today,)).fetchone()['c'] or 0
        today_t = conn.execute("SELECT COALESCE(SUM(total_tokens),0) as s FROM usage WHERE created_at >= ?", (today,)).fetchone()['s'] or 0
        today_in = conn.execute("SELECT COALESCE(SUM(prompt_tokens),0) as s FROM usage WHERE created_at >= ?", (today,)).fetchone()['s'] or 0
        today_out = conn.execute("SELECT COALESCE(SUM(completion_tokens),0) as s FROM usage WHERE created_at >= ?", (today,)).fetchone()['s'] or 0

        by_model = [dict(r) for r in conn.execute(
            "SELECT model, SUM(prompt_tokens) as p, SUM(completion_tokens) as c, SUM(total_tokens) as t, COUNT(*) as req FROM usage GROUP BY model ORDER BY req DESC").fetchall()]
        by_account = [dict(r) for r in conn.execute(
            "SELECT account_email, SUM(total_tokens) as t, COUNT(*) as req FROM usage GROUP BY account_email ORDER BY req DESC").fetchall()]
        by_key = [dict(r) for r in conn.execute(
            "SELECT api_key, SUM(total_tokens) as t, COUNT(*) as req FROM usage GROUP BY api_key ORDER BY req DESC").fetchall()]
        return {
            'total': {'req': total_req, 't': total_t, 'in': total_in, 'out': total_out},
            'today': {'req': today_req, 't': today_t, 'in': today_in, 'out': today_out},
            'by_model': by_model,
            'by_account': by_account,
            'by_key': by_key,
        }
    finally:
        conn.close()

# ============ 节点主动检测 (后台线程) ============
_node_check_state = {
    'last_run': 0,
    'running': False,
    'results': {},  # port -> {latency_ms, status, last_check}
    'dead_ports': [],  # 最近一次检测到的死节点
}
_node_check_lock = threading.Lock()
_NODE_CHECK_INTERVAL = 600  # 10分钟一轮
_NODE_CHECK_TIMEOUT = 8  # 每节点8秒超时

def _probe_node(port):
    """单节点探测: 无认证请求 /v1/models, 401 表示通"""
    from curl_cffi import requests as cffi
    from services.registration_service import get_node_proxy
    try:
        s = cffi.Session(impersonate='chrome131')
        p_url, _ = get_node_proxy(str(port))
        s.proxies = {'http': p_url, 'https': p_url}
        t0 = time.time()
        r = s.get('https://cli-chat-proxy.grok.com/v1/models',
                  timeout=_NODE_CHECK_TIMEOUT, allow_redirects=False)
        latency = int((time.time() - t0) * 1000)
        if r.status_code in (401, 403):
            return {'port': str(port), 'ok': True, 'latency': latency, 'code': r.status_code}
        return {'port': str(port), 'ok': False, 'latency': latency, 'code': r.status_code}
    except Exception as e:
        return {'port': str(port), 'ok': False, 'latency': None, 'code': str(e)[:60]}

def _node_check_loop():
    """后台检测循环: 每10分钟并发探测所有节点"""
    while True:
        try:
            _run_node_check()
        except Exception as e:
            print(f"[node-check] 检测异常: {e}", flush=True)
        time.sleep(_NODE_CHECK_INTERVAL)

def _run_node_check():
    """执行一轮检测: 并发探测所有节点, 更新延迟 + 死节点"""
    with _node_check_lock:
        if _node_check_state['running']:
            return
        _node_check_state['running'] = True
    try:
        nodes = _get_usable_nodes()
        if not nodes:
            return
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_probe_node, nodes))

        now = time.time()
        with _node_check_lock:
            _node_check_state['last_run'] = now
            _node_check_state['results'] = {}
            _node_check_state['dead_ports'] = []
        for res in results:
            port = res['port']
            if res['ok']:
                _mark_alive(port)
                if res['latency'] is not None:
                    _update_latency(port, res['latency'])
                with _node_check_lock:
                    _node_check_state['results'][port] = {
                        'latency_ms': res['latency'],
                        'status': 'ok',
                        'code': res['code'],
                        'last_check': now,
                    }
            else:
                _mark_dead(port)
                with _node_check_lock:
                    _node_check_state['results'][port] = {
                        'latency_ms': None,
                        'status': 'dead',
                        'code': res['code'],
                        'last_check': now,
                    }
                    _node_check_state['dead_ports'].append(port)
        ok = sum(1 for r in _node_check_state['results'].values() if r['status'] == 'ok')
        print(f"[node-check] 检测完成: {ok}/{len(results)} 节点可用, 死节点: {_node_check_state['dead_ports']}", flush=True)
    finally:
        with _node_check_lock:
            _node_check_state['running'] = False

def start_node_checker():
    """启动后台节点检测线程 (幂等)"""
    if getattr(start_node_checker, '_started', False):
        return
    start_node_checker._started = True
    t = threading.Thread(target=_node_check_loop, daemon=True)
    t.start()
    print("[node-check] 后台检测已启动 (每10分钟一轮)", flush=True)

# ============ 后台额度刷新 (每分钟刷新所有账号的 quota) ============
_quota_refresh_lock = threading.Lock()
_quota_refresh_running = False

def _refresh_quota_loop():
    """后台任务: 按需刷新账号 quota (只刷新超过 5 分钟未更新的账号)"""
    global _quota_refresh_running
    while True:
        try:
            with _quota_refresh_lock:
                if _quota_refresh_running:
                    time.sleep(600)
                    continue
                _quota_refresh_running = True
            # 只刷新最近 1 小时内有请求的账号 (未被调用的账号额度必然 100%, 无需刷新)
            conn = get_conn()
            rows = conn.execute("SELECT email, access_token, node_port FROM accounts WHERE status='active' AND pool_status='active' AND access_token != ''").fetchall()
            # 最近 1 小时内有请求记录的账号集合
            hour_ago = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).isoformat() + 'Z'
            used = set(x[0] for x in conn.execute(
                "SELECT DISTINCT account_email FROM usage WHERE created_at >= ?", (hour_ago,)).fetchall())
            conn.close()
            need_refresh = [r for r in rows if r['email'] in used and _quota_needs_refresh(r['email'], max_age_seconds=900)]
            if rows and not need_refresh:
                print(f"[quota-refresh] 本轮无需刷新 (最近1h无请求账号额度默认100%)", flush=True)
            if need_refresh:
                from services.registration_service import get_node_proxy
                from curl_cffi import requests as cffi
                for row in need_refresh:
                    try:
                        email = row['email']
                        at = row['access_token']
                        node_port = row.get('node_port') or '8078'
                        p_url, _ = get_node_proxy(str(node_port))
                        s = cffi.Session(impersonate='chrome131')
                        s.proxies = {'http': p_url, 'https': p_url}
                        # 使用 chat/completions 获取 quota headers
                        r = s.post('https://cli-chat-proxy.grok.com/v1/chat/completions',
                                 json={'model': 'grok-4.5', 'messages': [{'role': 'user', 'content': 'ping'}]},
                                 headers={'Authorization': f'Bearer {at}', 'x-grok-client-version': '0.2.93',
                                          'x-grok-client-identifier': 'grok-shell', 'User-Agent': 'grok-cli/0.2.93'},
                                 timeout=10)
                        # 即使 429 也要记录 quota (剩余额度为 0)
                        _record_quota(email, r.headers)
                    except Exception:
                        pass
                print(f"[quota-refresh] 本轮刷新 {len(need_refresh)} 个账号 (按需)", flush=True)
            with _quota_refresh_lock:
                _quota_refresh_running = False
        except Exception as e:
            print(f"[quota-refresh] 异常: {e}", flush=True)
            with _quota_refresh_lock:
                _quota_refresh_running = False
        time.sleep(600)

def start_quota_refresher():
    """启动 quota 刷新后台线程 (幂等)"""
    if getattr(start_quota_refresher, '_started', False):
        return
    start_quota_refresher._started = True
    t = threading.Thread(target=_refresh_quota_loop, daemon=True)
    t.start()
    print("[quota-refresh] 后台额度刷新已启动 (每 10 分钟一轮 (按需刷新, 15分钟未更新才刷新))", flush=True)

def get_node_check_status():
    """返回检测状态 (供管理界面显示)"""
    with _node_check_lock:
        return {
            'last_run': _node_check_state['last_run'],
            'running': _node_check_state['running'],
            'interval_seconds': _NODE_CHECK_INTERVAL,
            'dead_ports': list(_node_check_state['dead_ports']),
            'results': {k: dict(v) for k, v in _node_check_state['results'].items()},
        }


# quota 最后更新时间缓存 (email -> timestamp)
_quota_last_updated = {}
_quota_cache_lock = threading.Lock()

def _update_quota_cache(email):
    """记录该账号 quota 最近更新时间"""
    with _quota_cache_lock:
        _quota_last_updated[email] = time.time()

_QUOTA_SEM = threading.Semaphore(2)  # 额度刷新并发限制: 最多2个同时 (防补号时雪崩拖垮API)

def _refresh_single_quota(email, node_port=None):
    """刷新单个账号额度 (重登/续期成功后调用)"""
    if not _QUOTA_SEM.acquire(timeout=30):
        print(f"[quota] {email} 刷新额度: 并发满, 跳过", flush=True)
        return
    try:
        from services.account_service import get_account
        from services.registration_service import get_node_proxy
        from curl_cffi import requests as cffi
        acc = get_account(email)
        if not acc or not acc.get('access_token'):
            return
        at = acc['access_token']
        port = node_port or acc.get('node_port') or '8078'
        p_url, _ = get_node_proxy(str(port))
        s = cffi.Session(impersonate='chrome131')
        s.proxies = {'http': p_url, 'https': p_url}
        r = s.post('https://cli-chat-proxy.grok.com/v1/chat/completions',
                   json={'model': 'grok-4.5', 'messages': [{'role': 'user', 'content': 'ping'}]},
                   headers={'Authorization': f'Bearer {at}', 'X-XAI-Token-Auth': 'xai-grok-cli',
                            'x-grok-client-version': '0.2.93', 'x-grok-client-identifier': 'grok-shell',
                            'User-Agent': 'grok-cli/0.2.93', 'Content-Type': 'application/json'},
                   timeout=15)
        # 403/429 等非 200 响应: 可能是节点风控/限流, 不覆盖额度 (保留旧值)
        if r.status_code == 200:
            _record_quota(email, r.headers)
        else:
            print(f"[quota] {email} 刷新额度: HTTP {r.status_code} (非200, 保留旧额度)", flush=True)
        print(f"[quota] {email} 刷新额度: HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"[quota] {email} 刷新额度异常: {e}", flush=True)
    finally:
        _QUOTA_SEM.release()

def _quota_needs_refresh(email, max_age_seconds=900):
    """检查账号 quota 是否需要刷新 (超过 max_age_seconds 未更新)"""
    with _quota_cache_lock:
        last = _quota_last_updated.get(email, 0)
    return (time.time() - last) > max_age_seconds

def _record_quota(email, headers):
    """从 response headers 记录额度信息
    如果没有 quota headers（如 429 响应），保留数据库旧值 (不误报为额度耗尽)
    """
    try:
        conn = get_conn()
        remaining_tokens = headers.get('x-ratelimit-remaining-tokens', '')
        limit_tokens = headers.get('x-ratelimit-limit-tokens', '')
        remaining_req = headers.get('x-ratelimit-remaining-requests', '')
        limit_req = headers.get('x-ratelimit-limit-requests', '')

        # 如果 headers 中没有 quota 信息（如 429 响应），保留旧值, 不覆盖为 0
        if not remaining_tokens or not limit_tokens:
            old = conn.execute("SELECT quota FROM accounts WHERE email=?", (email,)).fetchone()
            old_quota = json.loads(old[0]) if old and old[0] else {}
            conn.close()
            if old_quota:
                _update_quota_cache(email)
                return
            # 无旧值时才用默认 (新账号无数据)
            quota_data = {
                'remaining_tokens': 0,
                'limit_tokens': 1000000,
                'remaining_requests': 0,
                'limit_requests': 21,
            }
        else:
            quota_data = {
                'remaining_tokens': int(remaining_tokens),
                'limit_tokens': int(limit_tokens),
                'remaining_requests': int(remaining_req) if remaining_req else None,
                'limit_requests': int(limit_req) if limit_req else None,
            }
        conn2 = get_conn()
        conn2.execute("""
            UPDATE accounts
            SET quota=?
            WHERE email=?
        """, (json.dumps(quota_data), email))
        conn2.commit()
        conn2.close()
        _update_quota_cache(email)
    except Exception:
        pass

# ============ 日志自动清理 (tasks表 + 日志文件轮转) ============
_cleanup_lock = threading.Lock()
_cleanup_running = False
TASKS_MAX_KEEP = 500       # tasks 表最多保留条数
LOG_FILE_MAX_MB = 10       # 日志文件超过 10MB 轮转
LOG_FILE = '/tmp/grok_platform.log'

def _cleanup_loop():
    """后台清理线程: 每 30 分钟检查一次, 清理过期日志"""
    global _cleanup_running
    while True:
        try:
            with _cleanup_lock:
                if _cleanup_running:
                    time.sleep(1800)
                    continue
                _cleanup_running = True
            # 1. 清理 tasks 表 (保留最近 TASKS_MAX_KEEP 条)
            try:
                conn = get_conn()
                total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                if total > TASKS_MAX_KEEP:
                    conn.execute("DELETE FROM tasks WHERE id NOT IN (SELECT id FROM tasks ORDER BY id DESC LIMIT ?)", (TASKS_MAX_KEEP,))
                    conn.commit()
                    print(f"[cleanup] tasks 表清理: {total} → {TASKS_MAX_KEEP} 条", flush=True)
                conn.close()
            except Exception as e:
                print(f"[cleanup] tasks清理异常: {e}", flush=True)
            # 2. 日志文件轮转 (超过大小则截断, 保留最近部分)
            try:
                import os as _os
                if _os.path.exists(LOG_FILE):
                    size_mb = _os.path.getsize(LOG_FILE) / 1024 / 1024
                    if size_mb > LOG_FILE_MAX_MB:
                        # 保留最后 500KB
                        with open(LOG_FILE, 'rb') as f:
                            f.seek(max(0, _os.path.getsize(LOG_FILE) - 512*1024))
                            tail = f.read()
                        with open(LOG_FILE, 'wb') as f:
                            f.write(tail)
                        print(f"[cleanup] 日志轮转: {size_mb:.1f}MB → 保留尾部500KB", flush=True)
            except Exception as e:
                print(f"[cleanup] 日志轮转异常: {e}", flush=True)
            with _cleanup_lock:
                _cleanup_running = False
        except Exception as e:
            print(f"[cleanup] 线程异常: {e}", flush=True)
            with _cleanup_lock:
                _cleanup_running = False
        time.sleep(1800)

def start_cleanup():
    """启动日志清理线程 (幂等)"""
    if getattr(start_cleanup, '_started', False):
        return
    start_cleanup._started = True
    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()
    print("[cleanup] 日志自动清理已启动 (tasks保留500条, 日志>10MB轮转)", flush=True)

# ============ 平台自持自动续期 (不依赖 Hermes cron) ============
_refresh_thread_lock = threading.Lock()
_refresh_thread_running = False

# ============ 精确调度自动续期 (按AT过期时间, 替代每60分钟轮询) ============
REFRESH_ADVANCE = 1800          # 固定: 失效前 30 分钟续期 (不管有效期多长)
REFRESH_SCAN_INTERVAL = 60      # 主循环扫描间隔: 60s (毫秒级SQL, 感知新账号/状态变化)
REFRESH_FAIL_RETRY = 1800       # 刷新失败(含降级重登失败)后 30 分钟再试
_refresh_fail_retry = {}        # email -> retry_ts (失败重试冷却, 防高频重试)
_refresh_fail_retry_lock = threading.Lock()

def _parse_expired(expired_str):
    """解析 expired 字段 (ISO8601 UTC) → epoch 秒; 失败返回 None"""
    try:
        return time.mktime(time.strptime(str(expired_str).replace('Z', ''), "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None

def _scheduled_refresh_one(email):
    """刷新单个账号: RT 优先(纯API秒级), RT失败自动降级重登(浏览器, 异步内部处理)"""
    try:
        from services import account_service
        result = account_service.refresh_with_rt_only(email)
        if result.get('ok'):
            print(f"[scheduler] {email} RT续期成功", flush=True)
            return
        # RT 失败 → 降级重登 (refresh_degrade 内部异步执行, 占用 DEGRADE_LOCK 与注册互斥)
        print(f"[scheduler] {email} RT续期失败({str(result.get('msg'))[:60]}), 降级重登", flush=True)
        result2 = account_service.refresh_degrade(email)
        if not result2.get('ok'):
            # 降级重登未启动成功 (如锁超时/无CPA): 冷却避免高频重试
            with _refresh_fail_retry_lock:
                _refresh_fail_retry[email] = time.time() + REFRESH_FAIL_RETRY
            print(f"[scheduler] {email} 降级重登未启动({str(result2.get('msg'))[:60]}), {REFRESH_FAIL_RETRY}s后重试", flush=True)
        # 重登已异步启动: 成功会更新DB(expired顺延), 失败会标cooling → 下一轮扫描自动跳过
    except Exception as e:
        print(f"[scheduler] {email} 异常: {e}", flush=True)
        with _refresh_fail_retry_lock:
            _refresh_fail_retry[email] = time.time() + REFRESH_FAIL_RETRY

def _scheduler_loop():
    """精确调度自动续期: 每个账号按自己的 AT 过期时间, 到点前30分钟用RT刷新
    比固定60分钟轮询更精准(不会等到过期才发现), 且只刷到期账号(减少无效调用)
    RT过期自动降级重登; 每24小时全量兜底一次(防 expired 解析失败等漏网)"""
    global _refresh_thread_running
    last_full_check = 0
    while True:
        try:
            with _refresh_thread_lock:
                if _refresh_thread_running:
                    time.sleep(REFRESH_SCAN_INTERVAL)
                    continue
                _refresh_thread_running = True
            try:
                from services import account_service
                accounts = account_service.list_accounts()
                now = time.time()
                due = []
                for acc in accounts:
                    email = acc['email']
                    at = acc.get('access_token', '')
                    status = acc.get('status', '')
                    # 只调度 active 且有 AT 的账号 (cooling/expired 由重登队列/测活处理)
                    if not at or status != 'active':
                        continue
                    exp_ts = _parse_expired(acc.get('expired', ''))
                    if exp_ts is None:
                        continue  # 解析失败, 交给24h全量兜底
                    # 失败重试冷却中
                    with _refresh_fail_retry_lock:
                        if _refresh_fail_retry.get(email, 0) > now:
                            continue
                    # 固定提前 30 分钟续期
                    if exp_ts - REFRESH_ADVANCE <= now:
                        due.append((exp_ts - REFRESH_ADVANCE, email))
                if due:
                    due.sort()
                    # 串行处理所有到期账号 (RT秒级; 降级重登内部异步不阻塞)
                    for _, email in due:
                        _scheduled_refresh_one(email)
                    # 处理完立即重新扫描 (可能有新到期/重登更新了expired)
                    with _refresh_thread_lock:
                        _refresh_thread_running = False
                    continue
                # 24h 全量兜底 (force_all: 覆盖 expired 解析失败/状态异常漏网)
                if now - last_full_check > 86400:
                    try:
                        from auto_refresh import refresh_loop
                        refreshed, failed = refresh_loop(min_hours=1.0, force_all=True)
                        print(f"[scheduler] 24h全量兜底: 成功{refreshed}, 失败{len(failed)}", flush=True)
                    except Exception as e:
                        print(f"[scheduler] 全量兜底异常: {e}", flush=True)
                    last_full_check = now
            except Exception as e:
                print(f"[scheduler] 扫描异常: {e}", flush=True)
            with _refresh_thread_lock:
                _refresh_thread_running = False
        except Exception as e:
            print(f"[scheduler] 线程异常: {e}", flush=True)
            with _refresh_thread_lock:
                _refresh_thread_running = False
        time.sleep(REFRESH_SCAN_INTERVAL)

def start_platform_refresh():
    """启动平台自持精确调度自动续期线程 (幂等)"""
    if getattr(start_platform_refresh, '_started', False):
        return
    start_platform_refresh._started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print("[auto-refresh] 精确调度续期已启动 (各账号失效前30分钟RT续期, RT失败自动降级重登, 24h全量兜底)", flush=True)

# ============ 自动补号监控 (可调度号≤5时自动注册至可调度≥30) ============
_fill_lock = threading.Lock()
_fill_running = False
_fill_state = {'last_trigger': None, 'last_count': 0, 'in_progress': False}

FILL_TRIGGER_THRESHOLD = 5    # 可调度号 ≤ 5 时触发补号
FILL_TARGET_DISPATCHABLE = 30 # 补号目标: 可调度号达到 30 停止
FILL_CHECK_INTERVAL = 300     # 监控间隔: 每 5 分钟检查一次

def get_usable_account_count():
    """当前可用账号数 (active + pool active + 有AT)"""
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM accounts WHERE status='active' AND pool_status='active' AND access_token != ''").fetchone()[0]
    conn.close()
    return n

def get_total_account_count():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()
    return n

def _auto_fill_loop():
    """后台监控: 可调度号≤5 时自动补号至可调度≥30 (需在设置页勾选启用)
    连续失败 5 个账号自动停止; 结果落盘 data/auto_fill_result.json 供前端展示
    """
    global _fill_running
    while True:
        try:
            # 总开关: 未勾选启用则跳过 (持久化在 settings 表, 重启不丢)
            if not auto_fill_enabled():
                with _fill_lock:
                    _fill_running = False
                time.sleep(FILL_CHECK_INTERVAL)
                continue
            with _fill_lock:
                if _fill_running:
                    time.sleep(FILL_CHECK_INTERVAL)
                    continue
                _fill_running = True
            dispatchable = get_dispatchable_count()
            print(f"[auto-fill] 检查: 可调度{dispatchable} (阈值≤{FILL_TRIGGER_THRESHOLD}, 目标{FILL_TARGET_DISPATCHABLE})", flush=True)
            if dispatchable <= FILL_TRIGGER_THRESHOLD:
                # 保护: 未配置临时邮箱时不触发补号 (否则必然失败空转)
                from services.registration_service import active_mail_domains
                if not active_mail_domains():
                    print("[auto-fill] 未配置临时邮箱域, 跳过补号 (部署后配置 TEMP_MAIL_CONFIG 即可)", flush=True)
                    with _fill_lock:
                        _fill_running = False
                    time.sleep(FILL_CHECK_INTERVAL)
                    continue
                need = FILL_TARGET_DISPATCHABLE - dispatchable
                if need > 0:
                    print(f"[auto-fill] 🚨 可调度号{dispatchable}≤{FILL_TRIGGER_THRESHOLD}, 自动补号 {need} 个 → 可调度{FILL_TARGET_DISPATCHABLE}", flush=True)
                    _fill_state['in_progress'] = True
                    _fill_state['last_trigger'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _fill_state['last_count'] = need
                    try:
                        from services.registration_service import registration_manager
                        # 用已验证节点池 (H2专线+Oracle+成功过的sanyuan)
                        from services.registration_service import get_active_nodes
                        ports = get_active_nodes() or ['8078', '8047', '8040', '8079', '8081', '8082', '8083', '8084', '8085', '8086', '8087', '8089', '8090', '8091', '8092']
                        task = registration_manager.register(count=need, node_ports=ports, domain=None)
                        task_id = task if isinstance(task, str) else task.get('task_id', '')
                        print(f"[auto-fill] 补号任务已启动: {task_id}", flush=True)
                        # 监控补号任务: 连续失败5个自动停止
                        _watch_fill_task(task_id, need)
                    except Exception as e:
                        print(f"[auto-fill] 补号启动异常: {e}", flush=True)
                        _fill_state['in_progress'] = False
                    _fill_state['in_progress'] = False
            with _fill_lock:
                _fill_running = False
        except Exception as e:
            print(f"[auto-fill] 异常: {e}", flush=True)
            with _fill_lock:
                _fill_running = False
        time.sleep(FILL_CHECK_INTERVAL)

FILL_MAX_CONSECUTIVE_FAILS = 5   # 连续失败阈值: 达到即停止补号

def _watch_fill_task(task_id, need):
    """监控补号任务进度: 连续失败>=5 自动停止; 结束写结果文件"""
    from services.registration_service import registration_manager
    import time as _t
    consecutive_fails = 0
    stop_reason = None
    while True:
        _t.sleep(10)
        try:
            task = registration_manager.get(task_id)
            if not task:
                break
            log = task.get('log', [])
            # 统计最近结果: 从 log 里数 成功/失败
            ok_count = 0
            fail_count = 0
            for line in log:
                if '✅ 注册成功' in line:
                    ok_count += 1
                    consecutive_fails = 0
                elif '❌ 注册失败' in line or '注册失败' in line:
                    fail_count += 1
                    consecutive_fails += 1
            # 连续失败达到阈值 → 停止
            if consecutive_fails >= FILL_MAX_CONSECUTIVE_FAILS:
                stop_reason = f"连续失败{FILL_MAX_CONSECUTIVE_FAILS}个, 自动停止"
                print(f"[auto-fill] ⛔ {stop_reason}", flush=True)
                try:
                    registration_manager.stop(task_id)
                except Exception:
                    pass
                break
            if task.get('status') != 'running':
                stop_reason = '任务完成' if task.get('status') == 'done' else f"任务{task.get('status')}"
                break
        except Exception as e:
            print(f"[auto-fill] 监控异常: {e}", flush=True)
            break
    # 写结果文件 (用户登录可见)
    try:
        result = {
            'triggered_at': _fill_state.get('last_trigger'),
            'task_id': task_id,
            'target_count': need,
            'registered': ok_count if 'ok_count' in dir() else 0,
            'failed': fail_count if 'fail_count' in dir() else 0,
            'stop_reason': stop_reason,
            'finished_at': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            'summary': f"补号结束: 成功{ok_count if 'ok_count' in dir() else 0}, 失败{fail_count if 'fail_count' in dir() else 0}" + (f" ({stop_reason})" if stop_reason else ''),
        }
        import os as _os
        _os.makedirs('data', exist_ok=True)
        with open('data/auto_fill_result.json', 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[auto-fill] 结果已保存: {result['summary']}", flush=True)
        _fill_state['last_result'] = result
    except Exception as e:
        print(f"[auto-fill] 结果保存失败: {e}", flush=True)

def start_auto_fill():
    """启动自动补号监控线程 (幂等)"""
    if getattr(start_auto_fill, '_started', False):
        return
    start_auto_fill._started = True
    t = threading.Thread(target=_auto_fill_loop, daemon=True)
    t.start()
    print("[auto-fill] 自动补号监控已启动 (可用号≤5时自动注册至30个)", flush=True)

def auto_fill_enabled():
    """自动补号总开关 (设置页勾选才启用, 持久化到 settings 表)"""
    from services.settings_service import get_setting
    return get_setting('auto_fill_enabled', '0') == '1'

def auto_fill_set_enabled(enabled: bool):
    """设置自动补号总开关"""
    from services.settings_service import set_setting
    set_setting('auto_fill_enabled', '1' if enabled else '0')
    print(f"[auto-fill] 总开关 {'启用' if enabled else '关闭'}", flush=True)
    return {'enabled': enabled}

def auto_fill_status():
    """查询自动补号状态 (含最近一次补号结果)"""
    result = None
    try:
        import os as _os
        if _os.path.exists('data/auto_fill_result.json'):
            with open('data/auto_fill_result.json') as f:
                result = json.load(f)
    except Exception:
        pass
    return {
        'enabled': auto_fill_enabled(),
        'trigger_threshold': FILL_TRIGGER_THRESHOLD,
        'target_dispatchable': FILL_TARGET_DISPATCHABLE,
        'check_interval': FILL_CHECK_INTERVAL,
        'max_consecutive_fails': FILL_MAX_CONSECUTIVE_FAILS,
        'dispatchable': get_dispatchable_count(),
        'last_trigger': _fill_state.get('last_trigger'),
        'last_count': _fill_state.get('last_count'),
        'in_progress': _fill_state.get('in_progress'),
        'last_result': result,
    }
