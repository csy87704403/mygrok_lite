"""Grok 账号管理平台 - SQLite 数据库模型"""
import sqlite3, json, time, os
from config import DB_PATH

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    # 账号表
    c.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT DEFAULT '',
        username TEXT DEFAULT '',
        access_token TEXT DEFAULT '',
        refresh_token TEXT DEFAULT '',
        id_token TEXT DEFAULT '',
        base_url TEXT DEFAULT 'https://cli-chat-proxy.grok.com/v1',
        status TEXT DEFAULT 'active',      -- active/cooling/expired/banned
        pool_status TEXT DEFAULT 'active', -- active/expired/banned
        expires_in INTEGER DEFAULT 21600,
        expired TEXT DEFAULT '',
        last_refresh TEXT DEFAULT '',
        quota TEXT DEFAULT '{}',          -- JSON {used, limit, etc}
        node_port TEXT DEFAULT '',
        fingerprint_seed TEXT DEFAULT '',
        timezone TEXT DEFAULT '',
        registered_at TEXT DEFAULT '',
        last_check TEXT DEFAULT '',
        note TEXT DEFAULT '',
        source TEXT DEFAULT 'cpa'
    )""")
    
    # API Key 表
    c.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT DEFAULT '',
        key TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'active',     -- active/disabled
        created_at TEXT DEFAULT '',
        note TEXT DEFAULT ''
    )""")
    
    # 用量统计表
    c.execute("""
    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key TEXT DEFAULT '',
        account_email TEXT DEFAULT '',
        model TEXT DEFAULT '',
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        request_count INTEGER DEFAULT 1,
        created_at TEXT DEFAULT ''
    )""")
    
    # 任务日志表
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT DEFAULT '',            -- register/refresh/import/testsso
        account_email TEXT DEFAULT '',
        status TEXT DEFAULT 'running',   -- running/done/failed
        detail TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        finished_at TEXT DEFAULT ''
    )""")
    
    # 节点池表 (自定义IP池)
    c.execute("""
    CREATE TABLE IF NOT EXISTS node_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        port TEXT UNIQUE NOT NULL,       -- Mihomo 监听端口 或 外部地址(ip:port)
        name TEXT DEFAULT '',
        ip TEXT DEFAULT '',
        country TEXT DEFAULT '',
        proxy_type TEXT DEFAULT 'http',  -- http/socks5
        status TEXT DEFAULT 'active',    -- active/disabled
        last_used TEXT DEFAULT ''
    )""")
    # 兼容旧表: 如果 proxy_type 列不存在则添加
    try:
        c.execute("ALTER TABLE node_pool ADD COLUMN proxy_type TEXT DEFAULT 'http'")
    except:
        pass
    
    # 临时邮箱域名配置表 (base_url + domain + 管理密码)
    c.execute("""
    CREATE TABLE IF NOT EXISTS mail_domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT UNIQUE NOT NULL,       -- 邮箱域名 (部署时配置)
        base_url TEXT NOT NULL,            -- 临时邮箱 worker 地址
        admin_password TEXT DEFAULT '',    -- 管理密码 (建址可能需要)
        status TEXT DEFAULT 'active',      -- active/disabled
        created_at TEXT DEFAULT ''
    )""")
    
    # 系统设置表 (key-value)
    c.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )""")
    
    conn.commit()
    conn.close()

# 默认临时邮箱配置 (seed) — 从环境变量读取, 无则跳过 (让外部部署自带, 不泄露私有邮箱)
# 格式: TEMP_MAIL_DOMAINS="domain1|base_url1|admin1,domain2|base_url2|admin2"
def _load_env_mail_domains():
    raw = os.environ.get('TEMP_MAIL_DOMAINS', '')
    if not raw:
        return []
    domains = []
    for chunk in raw.split(','):
        parts = chunk.split('|')
        if len(parts) >= 2:
            admin = parts[2] if len(parts) > 2 else ''
            domains.append((parts[0].strip(), parts[1].strip(), admin.strip()))
    return domains

def seed_default_mail_domains():
    domains = _load_env_mail_domains()
    conn = get_conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for domain, base, admin in domains:
        conn.execute(
            "INSERT OR IGNORE INTO mail_domains (domain, base_url, admin_password, status, created_at) VALUES (?,?,?, 'active', ?)",
            (domain, base, admin, now))
    conn.commit()
    conn.close()

# 默认节点池 (Docker Desktop/WSL2 适配: mihomo 容器同网络, 通过容器名访问)
DEFAULT_NODES = [
    "mihomo:8001", "mihomo:8002"
]

def seed_default_nodes():
    conn = get_conn()
    for p in DEFAULT_NODES:
        conn.execute("INSERT OR IGNORE INTO node_pool (port, status) VALUES (?, 'active')", (p,))
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    seed_default_nodes()
    print("数据库初始化完成:", DB_PATH)
