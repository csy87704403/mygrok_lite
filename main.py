"""Grok 账号管理平台 - 主应用 (FastAPI)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json, time, datetime

import config
from db import init_db, get_conn, seed_default_nodes, seed_default_mail_domains
from services import account_service, api_service, key_service, registration_service, settings_service

# 初始化数据库
init_db()
seed_default_nodes()
seed_default_mail_domains()

app = FastAPI(title="Grok 账号管理平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ 认证 ============
security = HTTPBearer()

def verify_admin(request: Request, token: str):
    if token != config.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="管理密码错误")

@app.middleware("http")
async def admin_auth(request: Request, call_next):
    # 管理端 API (除 /apis/ 和 /v1/ 和静态资源外) 需要 admin token
    path = request.url.path
    if (path.startswith('/api/') or path.startswith('/admin')) and not path.startswith('/api/v1/') \
       and not path.startswith('/static') and path not in ['/', '/admin/login']:
        auth = request.headers.get('Authorization', '')
        if auth != f'Bearer {config.ADMIN_PASSWORD}':
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
    return await call_next(request)

# API Key 认证 (用于 /v1/ 接口)
def verify_api_key(auth: HTTPAuthorizationCredentials = Depends(security)):
    key = auth.credentials
    k = key_service.valid_key(key)
    if not k:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key

# ============ 首页 + 静态面板 ============

@app.get("/", response_class=HTMLResponse)
async def index():
    # 返回 v3 时间戳版首页, 加 no-cache 让 CF 当新资源
    resp = FileResponse(os.path.join(config.STATIC_DIR, 'index.v3.html'))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.get("/static/{path:path}")
async def static_files(path: str):
    # 时间戳版本 (202608081420) 强制 Cloudflare 当新资源
    ts_path = path.replace('.js', '.202608081420.js').replace('.css', '.202608081420.css')
    f = os.path.join(config.STATIC_DIR, ts_path)
    if os.path.exists(f):
        resp = FileResponse(f)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    # fallback (无时间戳文件时): 返回原文件, 但加 no-cache 防浏览器缓存旧版
    f = os.path.join(config.STATIC_DIR, path)
    if os.path.exists(f):
        resp = FileResponse(f)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    raise HTTPException(status_code=404)

# ============ 账号管理 API ============

@app.get("/api/accounts")
async def list_accounts(status: Optional[str] = None, pool_status: Optional[str] = None):
    return account_service.list_accounts(status, pool_status)

@app.get("/api/accounts/dispatchable")
async def dispatchable_count():
    """返回当前可调度的活跃账号数"""
    count = api_service.get_dispatchable_count()
    return {'count': count}

@app.post("/api/accounts/relogin-all")
async def relogin_all():
    """一键重登: 将所有 expired 账号加入队列, 顺序处理"""
    return account_service.relogin_all()

@app.get("/api/accounts/relogin-queue")
async def relogin_queue():
    """查询重登队列进度"""
    return account_service.relogin_queue_status()

@app.post("/api/accounts/relogin-queue/cancel")
async def relogin_queue_cancel():
    """取消重登队列"""
    return account_service.relogin_queue_cancel()

@app.get("/api/accounts/{email}")
async def get_account(email: str):
    acc = account_service.get_account(email)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return acc

@app.post("/api/accounts/import")
def import_accounts():
    imported, errors = account_service.import_all_cpa()
    return {'imported': imported, 'errors': errors}

@app.post("/api/accounts/{email}/check")
def check_account(email: str):
    return account_service.check_account_status(email)

@app.post("/api/accounts/{email}/refresh")
def refresh_account(email: str):
    """续期: 只用 RT 刷新 AT (不降级登录)"""
    return account_service.refresh_with_rt_only(email)

@app.post("/api/accounts/{email}/relogin")
async def relogin_account(email: str):
    """单个重登: 降级登录 (浏览器) 重新拿SSO"""
    return account_service.relogin_start(email)

@app.get("/api/accounts/relogin/{task_id}")
async def relogin_status(task_id: str):
    return account_service.relogin_status(task_id)

@app.post("/api/accounts/check-all")
def check_all():
    return account_service.check_all_sync()

@app.get("/api/accounts/check-all/{task_id}")
async def check_all_result(task_id: str):
    return account_service.get_check_all_result(task_id)

@app.post("/api/accounts/refresh-all")
def refresh_all():
    """一键批量续期所有失活账号 (仅RT刷新)"""
    return account_service.refresh_all_inactive()

@app.get("/api/accounts/refresh-all/{task_id}")
async def refresh_all_result(task_id: str):
    return account_service.get_refresh_all_result(task_id)

@app.delete("/api/accounts/{email}")
async def delete_account(email: str):
    return account_service.delete_account(email)

@app.post("/api/accounts/{email}/quota")
def get_quota(email: str):
    # 同步 def: get_quota 是阻塞探测, 放线程池执行避免卡死事件循环
    return account_service.get_quota(email)

# ============ 注册 API ============

@app.get("/api/nodes")
async def list_nodes():
    return registration_service.list_nodes()

@app.post("/api/nodes")
async def add_node(node: dict):
    return registration_service.add_node(**node)

@app.post("/api/nodes/batch")
async def add_nodes_batch(body: dict):
    """批量新增节点: {text: 多行文本, default_proxy_type: http/socks5}"""
    text = body.get('text', '')
    default_proxy_type = body.get('default_proxy_type', 'http')
    return registration_service.add_nodes_batch(text, default_proxy_type)

@app.post("/api/nodes/check")
def check_nodes(body: dict):
    """检测输入框中的节点到 Grok 的连通性: {text, default_proxy_type}"""
    text = body.get('text', '')
    default_proxy_type = body.get('default_proxy_type', 'http')
    results = registration_service.check_nodes(text, default_proxy_type)
    invalid = [r for r in results if not r['ok']]
    return {'total': len(results), 'valid': len(results) - len(invalid), 'invalid': len(invalid), 'results': results}

@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: int):
    return registration_service.delete_node(node_id)

@app.post("/api/nodes/{node_id}/toggle")
async def toggle_node(node_id: int, body: dict):
    return registration_service.toggle_node(node_id, body.get('status', 'disabled'))

@app.post("/api/register")
async def register(body: dict):
    """自定义注册: {count, node_ports: [], domain: '', username_prefix: ''}"""
    task_id = registration_service.registration_manager.register(
        count=body.get('count', 1),
        node_ports=body.get('node_ports'),
        domain=body.get('domain'),
        username_prefix=body.get('username_prefix', ''),
    )
    return {'task_id': task_id}

@app.get("/api/register/task/{task_id}")
async def get_register_task(task_id: str):
    task = registration_service.registration_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

@app.post("/api/register/task/{task_id}/stop")
async def stop_register_task(task_id: str):
    """停止注册任务: 终止当前正在运行的子进程 + 不再启动下一个"""
    return registration_service.registration_manager.stop(task_id)

# ============ 临时邮箱域名配置 ============

@app.get("/api/mail-domains")
async def list_mail_domains():
    """列出所有临时邮箱域名配置"""
    return registration_service.list_mail_domains()

@app.post("/api/mail-domains")
async def add_mail_domain(body: dict):
    """新增/更新临时邮箱配置: {domain, base_url, admin_password, status}"""
    domain = (body.get('domain') or '').strip()
    base_url = (body.get('base_url') or '').strip()
    if not domain or not base_url:
        return {'ok': False, 'msg': 'domain 和 base_url 必填'}
    r = registration_service.add_mail_domain(domain, base_url,
                                             body.get('admin_password', ''),
                                             body.get('status', 'active'))
    if r is True:
        return {'ok': True, 'msg': '已保存'}
    return {'ok': False, 'msg': str(r)}

@app.delete("/api/mail-domains/{mid}")
async def delete_mail_domain(mid: int):
    registration_service.delete_mail_domain(mid)
    return {'ok': True}

@app.post("/api/mail-domains/{mid}/toggle")
async def toggle_mail_domain(mid: int, body: dict):
    registration_service.toggle_mail_domain(mid, body.get('status', 'active'))
    return {'ok': True}

# ============ API Key ============

@app.get("/api/keys")
async def list_api_keys():
    return key_service.list_keys()

@app.post("/api/keys")
async def create_api_key(body: dict):
    key = key_service.create_key(body.get('name', ''), body.get('note', ''))
    return {'key': key}

@app.delete("/api/keys/{key_id}")
async def delete_api_key(key_id: int):
    return key_service.delete_key(key_id)

# ============ 用量 ============

@app.get("/api/usage")
async def get_usage():
    return api_service.usage_summary()

# ============ 设置 ============

@app.post("/api/settings/password")
async def change_password(body: dict):
    """修改管理密码: {old_password, new_password}"""
    return settings_service.change_password(body.get('old_password', ''), body.get('new_password', ''))

@app.get("/api/settings/cpa")
async def get_cpa_settings():
    """获取 CPA 设置"""
    return settings_service.get_cpa_settings()

@app.post("/api/settings/cpa")
async def save_cpa_settings(body: dict):
    """保存 CPA 设置: {mode, local_path, remote_url, remote_password}"""
    return settings_service.save_cpa_settings(body)

@app.post("/api/settings/cpa/import")
async def import_cpa():
    """按当前设置导入 CPA 文件"""
    return settings_service.import_cpa()

@app.get("/api/auto-fill/status")
async def auto_fill_status():
    """自动补号监控状态 (可用号≤5时自动注册至30个)"""
    return api_service.auto_fill_status()

@app.post("/api/auto-fill/set-enabled")
async def auto_fill_set_enabled(body: dict):
    """设置自动补号总开关: {enabled: true/false}"""
    return api_service.auto_fill_set_enabled(bool(body.get('enabled', False)))

@app.get("/api/auto-refresh/status")
async def auto_refresh_status():
    # 真实调度参数: 从 api_service 读 (精确调度: 各账号失效前30分钟RT续期)
    from services import api_service
    advance_min = api_service.REFRESH_ADVANCE // 60
    scan_sec = api_service.REFRESH_SCAN_INTERVAL
    fail_retry_min = api_service.REFRESH_FAIL_RETRY // 60
    data = {
        'enabled': True,
        'mode': f'精确调度: 各账号失效前{advance_min}分钟自动RT续期；RT失效自动降级重登；24h全量兜底',
        'advance_minutes': advance_min,
        'scan_interval_sec': scan_sec,
        'fail_retry_minutes': fail_retry_min,
        'schedule': f'失效前{advance_min}分钟 (扫描间隔{scan_sec}s)',
        'last_run_at': None,
        'next_run_at': None,
    }
    try:
        status_file = os.path.join(os.path.dirname(__file__), 'data', 'auto_refresh_status.json')
        if os.path.exists(status_file):
            old = json.load(open(status_file))
            data['last_run_at'] = old.get('last_run_at')
            data['last_output_tail'] = old.get('last_output_tail')
    except Exception as e:
        data['error'] = str(e)
    return data

@app.get("/api/models")
async def platform_models():
    return {'object': 'list', 'data': api_service.list_models()}

# 公网 IP 缓存 (探测一次后缓存5分钟, 避免频繁请求外部服务)
_public_ip_cache = {'ip': '', 'ts': 0}

def get_public_ip():
    global _public_ip_cache
    now = time.time()
    if _public_ip_cache['ip'] and now - _public_ip_cache['ts'] < 300:
        return _public_ip_cache['ip']
    import subprocess as _sp
    for cmd in (
        ['curl', '-s', '--noproxy', '*', '-m', '8', 'https://api.ipify.org'],
        ['curl', '-s', '--noproxy', '*', '-m', '8', 'ifconfig.me'],
    ):
        try:
            out = _sp.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
            if out and out.count('.') >= 3:
                _public_ip_cache['ip'] = out
                _public_ip_cache['ts'] = now
                return out
        except Exception:
            continue
    return ''

# 平台对外入口配置:
# - 部署了 Cloudflare 隧道(公网IP+隧道) → 用隧道域名作为主入口
# - 只有公网 IP → 用 http://公网IP:端口
# 通过环境变量 GROK_PUBLIC_BASE_URL 可强制指定; 默认自动判断
def _resolve_base_url():
    import os as _os
    forced = _os.environ.get('GROK_PUBLIC_BASE_URL', '').strip()
    if forced:
        return forced, 'forced'
    # 不内置私有隧道域名; 无公网URL则返回直连IP
    ip = get_public_ip()
    return f'http://{ip}:{config.API_PORT}/v1' if ip else '', 'direct'

@app.get("/api/base-url")
async def base_url_info(request: Request):
    """返回平台 API 的完整 Base URL.
    部署了隧道(公网IP+隧道) → 显示隧道域名; 只有公网IP → 显示公网IP.
    本机/内网访问(127.x / 192.168.x / 10.x / 172.16-31.x) → 锁定 127.0.0.1 (避免显示代理出口 IP)
    """
    # 根据访问来源判断 (容器内 mihomo 代理会让 get_public_ip() 返回代理出口 IP, 误显示)
    client_ip = (request.client.host if request.client else '') or ''
    is_local = (
        client_ip in ('127.0.0.1', '::1', 'localhost', '')
        or client_ip.startswith('192.168.')
        or client_ip.startswith('10.')
        or (client_ip.startswith('172.') and 16 <= int(client_ip.split('.')[1] if client_ip.count('.') >= 2 else 0) <= 31)
    )
    ip = get_public_ip()
    tunnel_url = os.environ.get('GROK_PUBLIC_BASE_URL', '').strip()
    if tunnel_url:
        base_url, mode = tunnel_url, 'forced'
    elif is_local:
        # 本机/内网访问: 锁定 localhost, 不查公网 IP (防代理出口 IP 误显示)
        base_url, mode = f'http://127.0.0.1:{config.API_PORT}/v1', 'local'
    else:
        base_url = f'http://{ip}:{config.API_PORT}/v1' if ip else f'http://127.0.0.1:{config.API_PORT}/v1'
        mode = 'direct' if ip else 'local'
    direct_url = f'http://{ip}:{config.API_PORT}/v1' if ip else ''
    return {
        'public_ip': ip,
        'base_url': base_url,
        'urls': list(dict.fromkeys([base_url, tunnel_url, direct_url])),
        'port': config.API_PORT,
        'access_mode': mode,
        'note': '外网走自定义域名(GROK_PUBLIC_BASE_URL)' if tunnel_url
                else ('本机/内网访问, Base URL 锁定 localhost' if mode == 'local' else '使用公网IP直连'),
    }

# ============ OpenAI 兼容 API ============

@app.get("/v1/models")
async def v1_models(api_key: str = Depends(verify_api_key)):
    return {'object': 'list', 'data': api_service.list_models()}

@app.post("/v1/chat/completions")
def v1_chat(body: dict, request: Request, auth: HTTPAuthorizationCredentials = Depends(security)):
    # 注意: 必须用普通 def (非async) — chat_completion 是同步阻塞的(上游推理可达60s+),
    # async 会阻塞 uvicorn 事件循环, 导致并发请求全部串行排队
    api_key = auth.credentials
    if not key_service.valid_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # 支持流式
    stream = body.get('stream', False)
    result, err = api_service.chat_completion(body, api_key='***')

    # 流式模式: chat_completion 返回 (None, {stream:..., headers:...})
    if stream and err and 'stream' in err:
        stream_data = err['stream']
        headers = err.get('headers', {})
        # 过滤 hop-by-hop 头
        resp_headers = {k: v for k, v in headers.items()
                       if k.lower() not in ('transfer-encoding', 'connection', 'content-length')}
        # 包装流: 迭代结束时记录用量 (流式响应无 usage 字段, 统计请求次数即可)
        account_email = err.get('account', '')
        model = err.get('model', body.get('model', ''))
        key = err.get('api_key', api_key)
        def stream_wrapper():
            yielded_any = False
            try:
                for chunk in stream_data:
                    yielded_any = True
                    yield chunk
            except Exception as e:
                # 流式中途断连 (上游连接关闭等)
                print(f"[stream] {account_email} 流中断连: {e}", flush=True)
                if not yielded_any:
                    # 开局即断 (客户端未收到任何内容): 自动换账号重试一次
                    try:
                        r2, e2 = api_service.chat_completion(body, api_key='***')
                        if e2 and 'stream' in e2:
                            print(f"[stream] {account_email} 断连后换账号重试成功", flush=True)
                            for chunk2 in e2['stream']:
                                yield chunk2
                        elif e2:
                            print(f"[stream] 重试失败: {e2.get('error', {}).get('message', '')[:80]}", flush=True)
                    except Exception as e2:
                        print(f"[stream] 重试异常: {e2}", flush=True)
                # 优雅终止: 补发 [DONE] 防止客户端一直等待
                try:
                    yield b'data: [DONE]\n\n'
                except Exception:
                    pass
            finally:
                # 流式完成后记录一次请求 (无 token 明细)
                try:
                    from db import get_conn
                    import datetime
                    conn = get_conn()
                    conn.execute("INSERT INTO usage (api_key, account_email, model, prompt_tokens, completion_tokens, total_tokens, request_count, created_at) VALUES (?,?,?,0,0,0,1,?)",
                                 (key, account_email, model, datetime.datetime.utcnow().isoformat() + 'Z'))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
        return StreamingResponse(stream_wrapper(), media_type="text/event-stream", headers=resp_headers)

    if err:
        return JSONResponse(err, status_code=err.get('code', 500))

    # 非流式: result 是 dict (包含 _account, _node)
    resp = {k: v for k, v in result.items() if not k.startswith('_')}
    return JSONResponse(resp)

# ============ 任务日志 ============

@app.get("/api/tasks")
async def list_tasks(limit: int = 50):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    conn.close()
    return rows

@app.get("/api/nodes/check-status")
async def node_check_status():
    """节点主动检测状态 (管理界面显示)"""
    return api_service.get_node_check_status()

@app.get("/api/diagnostics/pool")
async def diagnostics_pool():
    """诊断: 连接池 + 节点延迟缓存 + 死节点 + busy锁 (进程内真实状态)"""
    from services import api_service as A
    pool = {}
    with A._pool_lock:
        for p, (s, ts) in A._session_pool.items():
            pool[str(p)] = {'age_s': round(time.time() - ts, 1), 'alive': getattr(s, '_is_closed', False)}
    lat = {}
    with A._latency_lock:
        for p, l in A._node_latency.items():
            lat[str(p)] = {'avg_ms': round(sum(l)/len(l), 1), 'n': len(l)}
    dead = {}
    with A._dead_lock:
        for p, ts in A._dead_nodes.items():
            dead[str(p)] = round(time.time() - ts, 1)
    busy = list(A._account_busy) if hasattr(A, '_account_busy') else []
    return {
        'session_pool': pool,
        'node_latency': lat,
        'dead_nodes': dead,
        'busy_accounts': busy,
        'concurrent': A._token_bucket_count if hasattr(A, '_token_bucket_count') else -1,
        'total_accounts': account_service.list_accounts().__len__(),
    }

@app.get("/health")
async def health():
    return {'status': 'ok'}

if __name__ == '__main__':
    import uvicorn
    # 加载持久化管理密码 (若有)
    settings_service.load_admin_password()
    # 启动后台节点检测线程 (每10分钟主动探测所有节点延迟/死活)
    api_service.start_node_checker()
    api_service.start_quota_refresher()
    api_service.start_auto_fill()
    # 平台自持自动续期 (每60分钟, 不依赖 Hermes cron)
    api_service.start_platform_refresh()
    # 日志自动清理 (tasks表 + 日志文件轮转)
    api_service.start_cleanup()
    uvicorn.run(app, host='0.0.0.0', port=config.API_PORT)
