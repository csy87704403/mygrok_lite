"""Grok 账号管理平台 - API Key 服务"""
import secrets, time
from db import get_conn

def generate_key():
    return "sk-grok-" + secrets.token_hex(24)

def create_key(name='', note=''):
    key = generate_key()
    conn = get_conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("INSERT INTO api_keys (name, key, status, created_at, note) VALUES (?,?,?,?,?)",
                (name, key, 'active', now, note))
    conn.commit()
    conn.close()
    return key

def list_keys():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT id, name, key, status, created_at, note FROM api_keys ORDER BY id").fetchall()]
    conn.close()
    return rows

def valid_key(key):
    conn = get_conn()
    row = conn.execute("SELECT * FROM api_keys WHERE key=? AND status='active'", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_key(key_id):
    conn = get_conn()
    conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    conn.commit()
    conn.close()
    return True

def count_keys():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as c FROM api_keys").fetchone()
    conn.close()
    return row['c']
