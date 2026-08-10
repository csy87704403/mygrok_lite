"""Grok 账号管理平台 - 设置服务 (密码修改 + CPA 本地/远程配置)"""
import json, os, time, shutil, glob, hashlib
from db import get_conn
import config

# ============ 设置读写 ============

def get_setting(key, default=''):
    conn = get_conn()
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return r['value'] if r else default

def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

# ============ 登录密码 ============

def change_password(old_password, new_password):
    """修改平台管理密码"""
    if old_password != config.ADMIN_PASSWORD:
        return {'ok': False, 'msg': '旧密码错误'}
    if not new_password or len(new_password) < 6:
        return {'ok': False, 'msg': '新密码至少6位'}
    # 持久化到 settings 表 + 环境文件
    set_setting('admin_password', new_password)
    # 写入持久化文件 (启动时读取覆盖环境变量)
    pw_file = os.path.join(config.DATA_DIR, 'admin_password.txt')
    with open(pw_file, 'w') as f:
        f.write(new_password)
    config.ADMIN_PASSWORD = new_password  # 运行时生效
    return {'ok': True, 'msg': '密码已修改'}

def load_admin_password():
    """启动时读取持久化密码 (若存在则覆盖环境变量默认值)"""
    pw_file = os.path.join(config.DATA_DIR, 'admin_password.txt')
    try:
        if os.path.exists(pw_file):
            pw = open(pw_file).read().strip()
            if pw:
                config.ADMIN_PASSWORD = pw
                return pw
    except Exception:
        pass
    return config.ADMIN_PASSWORD

# ============ CPA 设置 ============

DEFAULT_CPA_MODE = 'local'
DEFAULT_LOCAL_CPA_PATH = os.environ.get('GROK_ACCOUNTS_DIR', '/root/grok_accounts') + '/cpa'

def get_cpa_settings():
    """获取 CPA 设置"""
    mode = get_setting('cpa_mode', DEFAULT_CPA_MODE)
    local_path = get_setting('cpa_local_path', DEFAULT_LOCAL_CPA_PATH)
    remote_url = get_setting('cpa_remote_url', '')
    remote_password = get_setting('cpa_remote_password', '')
    return {
        'mode': mode,
        'local_path': local_path,
        'remote_url': remote_url,
        'remote_password': remote_password,
    }

def save_cpa_settings(body):
    """保存 CPA 设置: {mode: 'local'/'remote', local_path, remote_url, remote_password}"""
    mode = body.get('mode', 'local')
    if mode not in ('local', 'remote'):
        return {'ok': False, 'msg': 'mode 只能是 local 或 remote'}
    set_setting('cpa_mode', mode)
    set_setting('cpa_local_path', body.get('local_path', '').strip() or DEFAULT_LOCAL_CPA_PATH)
    set_setting('cpa_remote_url', body.get('remote_url', '').strip())
    set_setting('cpa_remote_password', body.get('remote_password', '').strip())
    return {'ok': True, 'msg': 'CPA 设置已保存'}

def import_cpa():
    """根据当前设置导入 CPA 文件到平台 CPA 目录并入库
    
    本地模式: 从本地路径复制 *.json 到平台 CPA_DIR
    远程模式: 从远程地址 (HTTP/WebDAV) 拉取 CPA 文件
    返回: 导入的文件名列表
    """
    settings = get_cpa_settings()
    mode = settings['mode']
    os.makedirs(config.CPA_DIR, exist_ok=True)
    imported_files = []

    if mode == 'local':
        local_path = settings['local_path']
        if not os.path.isdir(local_path):
            return {'ok': False, 'msg': f'本地路径不存在: {local_path}'}
        # 扫描本地目录的 CPA JSON 文件
        # 若本地路径 == 平台 CPA_DIR, 直接扫描入库 (无需复制)
        same_dir = os.path.abspath(local_path) == os.path.abspath(config.CPA_DIR)
        for f in sorted(glob.glob(os.path.join(local_path, 'cpa_*.json'))):
            try:
                if same_dir:
                    imported_files.append(os.path.basename(f))
                else:
                    dest = os.path.join(config.CPA_DIR, os.path.basename(f))
                    shutil.copy2(f, dest)
                    imported_files.append(os.path.basename(f))
            except Exception as e:
                print(f"[cpa-import] 处理失败 {f}: {e}", flush=True)
        # 也支持纯 *.json (如果目录里没有 cpa_ 前缀)
        if not imported_files:
            for f in sorted(glob.glob(os.path.join(local_path, '*.json'))):
                try:
                    dest = os.path.join(config.CPA_DIR, os.path.basename(f))
                    if os.path.abspath(f) == os.path.abspath(dest):
                        imported_files.append(os.path.basename(f))
                    else:
                        shutil.copy2(f, dest)
                        imported_files.append(os.path.basename(f))
                except Exception:
                    pass
    elif mode == 'remote':
        remote_url = settings['remote_url']
        remote_password = settings['remote_password']
        if not remote_url:
            return {'ok': False, 'msg': '远程地址未配置'}
        try:
            from curl_cffi import requests as cffi
            base = remote_url.rstrip('/')
            auth = None
            if remote_password:
                import base64 as _b64
                auth = 'Basic ' + _b64.b64encode(f'admin:{remote_password}'.encode()).decode()
            # 获取远程文件列表: 尝试 WebDAV PROPFIND 或目录列表
            headers = {}
            if auth:
                headers['Authorization'] = auth
            # 尝试 PROPFIND (WebDAV)
            r = cffi.request('PROPFIND', base + '/', headers={**headers, 'Depth': '1'}, timeout=20)
            if r.status_code in (200, 207):
                import re
                hrefs = re.findall(r'<D:href>([^<]+\.json)</D:href>', r.text)
                files = list(set(hrefs))
            else:
                # 尝试目录列表 HTML
                r2 = cffi.get(base + '/', headers=headers, timeout=20)
                files = re.findall(r'href="([^"]*cpa_[^"]*\.json)"', r2.text)
            if not files:
                return {'ok': False, 'msg': f'远程未找到 CPA 文件 (HTTP {r.status_code})'}
            for rel in files:
                name = rel.split('/')[-1]
                if not name.endswith('.json'):
                    continue
                fr = cffi.get(base + '/' + name, headers=headers, timeout=30)
                if fr.status_code == 200:
                    with open(os.path.join(config.CPA_DIR, name), 'w') as f:
                        f.write(fr.text)
                    imported_files.append(name)
        except Exception as e:
            return {'ok': False, 'msg': f'远程拉取失败: {str(e)[:120]}'}

    if not imported_files:
        return {'ok': False, 'msg': '未找到可导入的 CPA 文件'}

    # 入库
    from services import account_service
    count, errors = account_service.import_all_cpa()
    return {
        'ok': True,
        'msg': f'已导入 {len(imported_files)} 个文件, 入库 {count} 个账号',
        'files': imported_files,
        'imported_accounts': count,
        'errors': errors[:5],
    }
