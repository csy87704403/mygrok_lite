#!/usr/bin/env python3
"""Grok 平台 - 自动续期守护脚本
用法: python3 auto_refresh.py [--force-all] [--min-expire-hours 1]
逻辑:
  1. 扫描所有账号
  2. AT 剩余有效期 < min_expire_hours 或已过期 → 尝试 RT 续期
  3. RT 续期失败 → 降级登录 (浏览器)
输出: 简要报告 (供 cron 日志)
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 脚本自身目录

from services import account_service
from db import get_conn

def refresh_loop(min_hours=1.0, force_all=False):
    accounts = account_service.list_accounts()
    now = time.time()
    refreshed = 0
    failed = []
    skipped = 0
    
    for acc in accounts:
        email = acc['email']
        at = acc.get('access_token', '')
        expired_str = acc.get('expired', '')
        
        # 计算剩余时间
        remaining = 0
        if expired_str:
            try:
                exp_ts = time.mktime(time.strptime(expired_str.replace('Z',''), "%Y-%m-%dT%H:%M:%S"))
                remaining = exp_ts - now
            except:
                remaining = -1  # 解析失败视为过期
        
        need = force_all or remaining < min_hours * 3600
        
        if not need:
            skipped += 1
            continue
        
        print(f"[{time.strftime('%H:%M:%S')}] {email} 剩余 {remaining/3600:.1f}h → 续期", flush=True)
        result = account_service.refresh_account(email)
        if result.get('ok'):
            refreshed += 1
            print(f"  ✅ {result.get('msg')}", flush=True)
        else:
            failed.append(email)
            print(f"  ❌ {result.get('msg')}", flush=True)
    
    print(f"\n=== 续期完成: 成功{refreshed}, 失败{len(failed)}, 跳过{skipped} ===", flush=True)
    if failed:
        print(f"失败账号: {', '.join(failed)}", flush=True)
    return refreshed, failed

if __name__ == '__main__':
    min_hours = 1.0
    force_all = False
    if '--force-all' in sys.argv:
        force_all = True
    for i, a in enumerate(sys.argv):
        if a == '--min-expire-hours' and i+1 < len(sys.argv):
            min_hours = float(sys.argv[i+1])
    refresh_loop(min_hours, force_all)