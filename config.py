"""Grok 账号管理平台 - 核心配置 (全部从环境变量读取, 无硬编码敏感信息)"""
import os

# 平台根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# 数据库
DB_PATH = os.path.join(DATA_DIR, 'grok.db')

# 账号产出物目录 (已有账号/CPA 从这里导入) — 部署时通过挂载卷指定
GROK_ACCOUNTS_DIR = os.environ.get('GROK_ACCOUNTS_DIR', '/root/grok_accounts')
CPA_DIR = os.path.join(GROK_ACCOUNTS_DIR, 'cpa')

# 脚本路径 (Docker 内固定, 或通过环境变量覆盖)
REGISTER_SCRIPT = os.environ.get('GROK_REGISTER_SCRIPT', '/app/scripts/grok_auto_v7.py')
REFRESH_SCRIPT = os.environ.get('GROK_REFRESH_SCRIPT', '/app/scripts/refresh_cpa.py')
DEGRADE_SCRIPT = os.environ.get('GROK_DEGRADE_SCRIPT', '/app/scripts/grok_login_sso.py')

# 远程服务 (临时邮箱 API / 管理, 部署时配置, 无默认值强制提供)
TEMP_MAIL_API = os.environ.get('TEMP_MAIL_API', '')
TEMP_MAIL_ADMIN = os.environ.get('TEMP_MAIL_ADMIN', '')

# API 端口
API_PORT = int(os.environ.get('GROK_PLATFORM_PORT', 18080))

# 平台管理密码 (部署时设置, 无默认值强制提供)
ADMIN_PASSWORD = os.environ.get('GROK_PLATFORM_ADMIN_PASSWORD', '')