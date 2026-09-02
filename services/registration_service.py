"""Grok 账号管理平台 - 注册服务 (自定义节点池+临时邮箱)"""
import subprocess, json, time, os, threading, random, sys, re, glob
from db import get_conn
import config

def list_nodes():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM node_pool ORDER BY id").fetchall()]
    conn.close()
    return rows

def get_active_nodes():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT port FROM node_pool WHERE status='active'").fetchall()]
    conn.close()
    return [r['port'] for r in rows]

def add_node(port, name='', ip='', country='', proxy_type='http'):
    conn = get_conn()
    conn.execute("""
    INSERT OR REPLACE INTO node_pool (port, name, ip, country, proxy_type, status) VALUES (?,?,?,?,?, 'active')
    """, (port, name, ip, country, proxy_type or 'http'))
    conn.commit()
    conn.close()
    return True

def add_nodes_batch(text, default_proxy_type='http'):
    """批量新增节点: 每行一个节点. 支持格式:
    端口 (如 8047) / ip:port (如 1.2.3.4:8080) / socks5://ip:port / http://ip:port
    """
    added, skipped, errors = 0, 0, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        proxy_type = default_proxy_type
        port = line
        # 解析协议前缀
        if '://' in line:
            scheme, rest = line.split('://', 1)
            if scheme.lower() in ('socks5', 'socks4'):
                proxy_type = 'socks5'
            elif scheme.lower() == 'http':
                proxy_type = 'http'
            port = rest
        # 去可能的用户名密码或路径
        if '@' in port:
            port = port.rsplit('@', 1)[1]
        port = port.strip('/').strip()
        if not port:
            continue
        try:
            add_node(port, proxy_type=proxy_type)
            added += 1
        except Exception as e:
            errors.append(f'{line}: {e}')
            skipped += 1
    return {'added': added, 'skipped': skipped, 'errors': errors}

def check_nodes(text, default_proxy_type='http', timeout=8):
    """检测文本中的节点到 Grok 的连通性. 返回每行的检测结果列表.
    ok=True 表示能通过该代理访问 cli-chat-proxy.grok.com (200/401/403 都算通, 连不上/超时算无效).
    """
    import curl_cffi.requests as cffi
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        proxy_type = default_proxy_type
        port = line
        if '://' in line:
            scheme, rest = line.split('://', 1)
            if scheme.lower() in ('socks5', 'socks4'):
                proxy_type = 'socks5'
            elif scheme.lower() == 'http':
                proxy_type = 'http'
            port = rest
        if '@' in port:
            port = port.rsplit('@', 1)[1]
        port = port.strip('/').strip()
        lines.append((line, port, proxy_type))

    results = []
    for raw, port, proxy_type in lines:
        p_url = proxy_url(port, proxy_type)
        try:
            s = cffi.Session(impersonate='chrome131')
            s.proxies = {'http': p_url, 'https': p_url}
            import time as _t
            t0 = _t.time()
            r = s.get('https://cli-chat-proxy.grok.com/v1/models', timeout=timeout,
                      headers={'Authorization': 'Bearer invalid-test', 'x-xai-token-auth': 'xai-grok-cli'})
            latency = int((_t.time() - t0) * 1000)
            ok = r.status_code in (200, 401, 403)
            results.append({
                'line': raw, 'port': port, 'proxy_type': proxy_type,
                'ok': ok, 'latency_ms': latency, 'code': r.status_code,
                'error': '' if ok else f'HTTP {r.status_code}',
            })
        except Exception as e:
            results.append({
                'line': raw, 'port': port, 'proxy_type': proxy_type,
                'ok': False, 'latency_ms': 0, 'code': 0,
                'error': str(e)[:80],
            })
    return results

def proxy_url(port, proxy_type='http'):
    """根据节点类型生成 curl_cffi 可用的代理 URL"""
    if port.startswith('http://') or port.startswith('socks'):
        return port
    scheme = 'socks5h' if proxy_type == 'socks5' else 'http'
    if ':' in port:
        # 外部地址 ip:port
        return f'{scheme}://{port}'
    # 本地 Mihomo 端口
    return f'http://127.0.0.1:{port}'

def get_node_proxy(node_port):
    """按端口查节点并返回 (proxy_url, proxy_type)"""
    conn = get_conn()
    row = conn.execute("SELECT port, proxy_type FROM node_pool WHERE port=?", (str(node_port),)).fetchone()
    conn.close()
    if not row:
        return proxy_url(str(node_port), 'http'), 'http'
    return proxy_url(row['port'], row['proxy_type'] or 'http'), row['proxy_type'] or 'http'

def delete_node(node_id):
    conn = get_conn()
    conn.execute("DELETE FROM node_pool WHERE id=?", (node_id,))
    conn.commit()
    conn.close()
    return True

def toggle_node(node_id, status):
    conn = get_conn()
    conn.execute("UPDATE node_pool SET status=? WHERE id=?", (status, node_id))
    conn.commit()
    conn.close()
    return True

# ============ 临时邮箱域名配置 (DB) ============

def list_mail_domains():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute("SELECT id, domain, base_url, admin_password, status, created_at FROM mail_domains ORDER BY domain").fetchall()]
    conn.close()
    return rows

def get_mail_domain(domain):
    conn = get_conn()
    row = conn.execute("SELECT * FROM mail_domains WHERE domain=?", (domain,)).fetchone()
    conn.close()
    return dict(row) if row else None

def add_mail_domain(domain, base_url, admin_password='', status='active'):
    """新增或更新临时邮箱配置"""
    conn = get_conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        conn.execute("""
        INSERT INTO mail_domains (domain, base_url, admin_password, status, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(domain) DO UPDATE SET
          base_url=excluded.base_url,
          admin_password=excluded.admin_password,
          status=excluded.status
        """, (domain, base_url, admin_password, status, now))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        conn.close()
        return str(e)

def delete_mail_domain(mid):
    conn = get_conn()
    conn.execute("DELETE FROM mail_domains WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    return True

def toggle_mail_domain(mid, status):
    conn = get_conn()
    conn.execute("UPDATE mail_domains SET status=? WHERE id=?", (status, mid))
    conn.commit()
    conn.close()
    return True

def active_mail_domains():
    """返回 active 域名的 (base_url, domain, admin) 列表, 供注册脚本 GROK_MAIL_CONFIG"""
    conn = get_conn()
    rows = conn.execute("SELECT base_url, domain, admin_password FROM mail_domains WHERE status='active'").fetchall()
    conn.close()
    return [{'base_url': r['base_url'], 'domain': r['domain'], 'admin_password': r['admin_password']} for r in rows]

class RegistrationManager:
    """注册任务管理器 (后台线程)"""
    def __init__(self):
        self.tasks = {}   # task_id -> {status, log, ...}
        self.stop_flags = {}  # task_id -> bool
        self.procs = {}   # task_id -> Popen (用于停止)
        self.lock = threading.Lock()

    def _set(self, task_id, **kw):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id].update(kw)

    def register(self, count=1, node_ports=None, domain=None, username_prefix=''):
        """启动注册任务"""
        task_id = f"reg_{int(time.time())}"
        with self.lock:
            self.tasks[task_id] = {
                'status': 'running',
                'type': 'register',
                'count': count,
                'registered': 0,
                'failed': 0,
                'domain': domain or '自动轮询',
                'log': [],
                'started': time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.stop_flags[task_id] = False

        # 后台执行
        def run():
            # 注册节点池: 优先验证成功的节点 (8078 H2专线20个, 8047/8040 sanyuan各2/1个, 8083/8086 Oracle各1个)
            # + H2专线(8079) + Oracle其余(8081-8092), 覆盖更多出口IP提高成功率
            # 注册节点池: 使用全部 active 节点 (坐标问题已修复, IP不是风控因素)
            active_ports = get_active_nodes() or ['8047','8063','8081','8040']
            self._log(task_id, f"启动注册任务: 目标{count}个, 节点池={node_ports or f'全部({len(active_ports)}个active)'}, 域名={domain or '自动轮询'}")
            ports = node_ports or active_ports
            random.shuffle(ports)
            task_started = time.time()   # 用于兜底筛选"本次新生成"的 CPA 文件

            done = 0
            while done < count:
                # 检查停止信号
                if self.stop_flags.get(task_id):
                    self._log(task_id, "🛑 收到停止信号, 正在停止...")
                    break
                if not ports:
                    self._log(task_id, "节点池为空, 无法注册")
                    break
                try:
                    # 注册前获取全局浏览器锁 (与重登互斥: 都占用 9222+Xvfb+xdotool)
                    # 获取失败说明重登/其他注册占用中, 等待
                    from services.account_service import DEGRADE_LOCK
                    got_lock = DEGRADE_LOCK.acquire(timeout=600)
                    if not got_lock:
                        self._log(task_id, "⏳ 浏览器资源被占用(重登/其他注册), 等待超时, 放弃本轮")
                        done += 1
                        continue
                    try:
                        # 传整个节点池给脚本: 脚本内部3次重试会随机换节点, 避免死磕风控节点
                        ok, cpa_path, detail = self._register_one(task_id, ports, domain)
                    finally:
                        try:
                            DEGRADE_LOCK.release()
                        except Exception:
                            pass
                    if ok:
                        self._log(task_id, f"✅ 注册成功 (node 池内)")
                        self._set(task_id, registered=self.tasks[task_id].get('registered', 0) + 1)
                        # 只导入本次产出的 CPA (禁止全目录重扫, 否则会复活用户已删除的账号)
                        self._import_new_cpa(task_id, cpa_path, task_started)
                    else:
                        # 被停止时 detail 无需再报错
                        if self.stop_flags.get(task_id):
                            break
                        self._log(task_id, f"❌ 注册失败: {detail[-200:]}")
                        self._set(task_id, failed=self.tasks[task_id].get('failed', 0) + 1)
                    done += 1
                except Exception as e:
                    if self.stop_flags.get(task_id):
                        break
                    self._log(task_id, f"⚠️ 注册异常: {e}")
                    done += 1

            with self.lock:
                self.tasks[task_id]['status'] = 'done' if not self.stop_flags.get(task_id) else 'stopped'
                self.tasks[task_id]['finished'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.tasks[task_id]['log'].append("任务完成" if not self.stop_flags.get(task_id) else "任务已停止")
                # 清理
                self.procs.pop(task_id, None)
                self.stop_flags.pop(task_id, None)
                # 任务保留 1 小时后自动移除 (防止内存无限增长)
                try:
                    import threading as _th
                    _th.Timer(3600, lambda tid=task_id: self.tasks.pop(tid, None)).start()
                except Exception:
                    pass

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return task_id

    def _kill_proc_group(self, proc, task_id, tag=''):
        """杀掉注册子进程所在的整个进程组。

        关键: Popen 用了 start_new_session=True, 子进程处于独立进程组。
        只 terminate()/kill() 主进程, 组内的 chromium 会变成孤儿残留,
        继续占用 9222 调试端口 -> 下一次注册启动浏览器直接失败。
        因此必须 killpg 杀整组。
        """
        import signal as _s
        if not proc:
            return False
        killed = False
        try:
            if proc.poll() is not None:
                return False  # 已退出
            try:
                pgid = os.getpgid(proc.pid)
            except Exception:
                pgid = None
            if pgid:
                os.killpg(pgid, _s.SIGKILL)   # 整组连浏览器一起杀
                killed = True
            else:
                proc.kill()
                killed = True
            try:
                proc.wait(timeout=5)          # 回收, 避免僵尸进程
            except Exception:
                pass
        except Exception as e:
            self._log(task_id, f"⚠️ 终止子进程异常{tag}: {e}")
        return killed

    def stop(self, task_id):
        """停止注册任务: 置停止标志 + 杀掉正在运行的子进程(整个进程组), 立即返回"""
        with self.lock:
            self.stop_flags[task_id] = True
            self.tasks.get(task_id, {}).setdefault('log', []).append("正在终止子进程...")
            proc = self.procs.get(task_id)
        killed = self._kill_proc_group(proc, task_id)
        # 兜底清理: 杀掉可能残留的 chromium (占用9222会导致下次注册起不来)
        try:
            subprocess.run(['pkill', '-f', 'remote-debugging-port=9222'],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        if killed:
            self._log(task_id, "🛑 子进程已终止 (含浏览器进程组)")
        else:
            self._log(task_id, "🛑 已置停止标志 (当前无运行中的子进程, 后续账号不再启动)")
        return {'ok': True, 'msg': 'stop signal sent', 'killed': killed}

    def stop_all(self):
        """停止全部注册任务 (含自动补号启动的、前端拿不到 task_id 的那些)。

        自动补号(auto_fill)会自行调用 register(), 其 task_id 只存在于后端内存,
        前端无从得知, 单任务 stop 接口停不掉它 —— 必须用这个全停。
        """
        stopped = []
        with self.lock:
            running_ids = [tid for tid, t in self.tasks.items()
                           if t.get('status') == 'running']
            procs = dict(self.procs)
        for tid in running_ids:
            try:
                self.stop(tid)
                stopped.append(tid)
            except Exception as e:
                print(f"[register] stop_all 停止 {tid} 异常: {e}", flush=True)
        # 同时停掉自动补号的在途标记, 防止刚停完又被立即拉起
        try:
            from services import api_service
            with api_service._fill_lock:
                api_service._fill_running = False
            api_service._fill_state['in_progress'] = False
        except Exception as e:
            print(f"[register] stop_all 清理 auto_fill 状态异常: {e}", flush=True)
        return {'ok': True, 'stopped': stopped, 'count': len(stopped)}

    def _register_one(self, task_id, ports, domain):
        """调用 grok_auto_v6.py 真实注册单个账号 (MCDP方案)
        ports = 候选节点列表. 通过 GROK_NODES 环境变量传给脚本, 脚本每次重试会换节点.
        domain = 指定域名(或空=自动轮询全部 active 域名).
        用 Popen 逐行读取 stdout, 实时写入任务日志 (逐步进度).
        注册期间检测 stop 标志: 触发则 kill 子进程.
        """
        script = config.REGISTER_SCRIPT  # /tmp/grok_auto_v6.py
        env = dict(os.environ)
        env['GROK_NODES'] = ','.join(str(p) for p in ports)   # 传整个节点池, 脚本内部重试换节点
        # 注意: 不注入 DISPLAY —— 容器内无 X Server, 注册脚本是 headless=True;
        # 注入 ':1' 会让部分图形库误判有显示环境, 历史上导致 headed browser 启动失败。
        # 从 DB 读取邮箱域名配置 (支持自动轮询/指定单域名)
        mails = active_mail_domains()
        if domain:
            mails = [m for m in mails if m['domain'] == domain]
        if not mails:
            self._log(task_id, f"⚠️ 没有可用的临时邮箱域名配置 (domain={domain or '自动'})，请先添加")
            return False, None, 'no mail domain configured'
        env['GROK_MAIL_CONFIG'] = json.dumps(mails, ensure_ascii=False)
        # Karing 出口开关: 勾选"通过 Karing 代理出口(127.0.0.1:3066)"时,
        # 注册浏览器/SSO会话也走 Karing 代理 (容器内用 host.docker.internal 穿透到宿主机).
        # 与平台 api_service.is_egress_karing() 同源, 通过环境变量传给注册脚本.
        try:
            from services.settings_service import get_setting
            _karing = get_setting('egress_karing', '0') == '1'
        except Exception:
            _karing = False
        env['EGRESS_KARING'] = '1' if _karing else '0'
        # 浏览器二进制首次下载(约216MB)由 cloakbrowser 内部 httpx 发起, 不走浏览器代理。
        # 直连下载源在国内极易中途断开(SSL EOF / peer closed), 故给 httpx 也挂上代理
        # (httpx 默认 trust_env=True, 会读 HTTPS_PROXY/HTTP_PROXY 环境变量)。
        # 二进制已缓存在持久化卷 /root/.cloakbrowser, 正常不会重复下载, 此为兜底。
        if _karing:
            env['HTTPS_PROXY'] = 'http://host.docker.internal:3066'
            env['HTTP_PROXY'] = 'http://host.docker.internal:3066'
            env['https_proxy'] = 'http://host.docker.internal:3066'
            env['http_proxy'] = 'http://host.docker.internal:3066'
            # 必须排除本机地址: 脚本通过 CDP(127.0.0.1:9222) 读取浏览器 SSO cookie,
            # 若不排除, urllib 会把该本机请求也送进 Karing -> HTTP 502 -> 读不到 SSO。
            # 下载源(cloakbrowser.dev / github.com)不在排除列表, 仍走代理。
            env['NO_PROXY'] = '127.0.0.1,localhost,::1,0.0.0.0'
            env['no_proxy'] = '127.0.0.1,localhost,::1,0.0.0.0'

        proc = subprocess.Popen(
            ['/usr/local/bin/python3.11', script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, cwd='/tmp',
            start_new_session=True,  # 独立进程组, kill时可连浏览器一起杀
        )
        with self.lock:
            self.procs[task_id] = proc

        full_out = []
        # 注册硬超时: 600秒 (脚本3次重试正常需要5-10分钟), 超时强制杀整个进程组
        REGISTER_TIMEOUT = 600
        import signal as _sig
        deadline = time.time() + REGISTER_TIMEOUT
        timed_out = False
        try:
            while True:
                # 检查超时
                if time.time() > deadline:
                    timed_out = True
                    self._log(task_id, f"⏰ 注册超时({REGISTER_TIMEOUT}s), 强制终止")
                    try:
                        os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                    except Exception:
                        proc.kill()
                    break
                # 非阻塞读一行 (0.5s轮询, 兼顾超时检查与停止信号)
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.rstrip('\n')
                if not line.strip():
                    continue
                full_out.append(line)
                # 逐步实时上报日志 (去重: 空行/纯进度)
                self._log(task_id, line)
                # 注册期间循环检查停止信号
                if self.stop_flags.get(task_id):
                    self._log(task_id, "🛑 检测到停止, 终止子进程")
                    try:
                        os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                    except Exception:
                        proc.kill()
                    break
        finally:
            # 无论何种退出路径, 确保进程组被清理 (含残留chromium)
            try:
                if proc.poll() is None:
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            # 清理可能残留的 chromium (remote-debugging-port=9222)
            try:
                subprocess.run(['pkill', '-f', 'remote-debugging-port=9222'],
                               capture_output=True, timeout=5)
            except Exception:
                pass

        out = '\n'.join(full_out)
        print(f"  [reg] nodes={','.join(map(str, ports))} rc={proc.returncode} timeout={timed_out}")
        if self.stop_flags.get(task_id):
            return False, None, 'stopped by user'
        if timed_out:
            return False, None, f'注册超时({REGISTER_TIMEOUT}s), 已终止'
        # 成功标志: DONE + 生成了CPA
        if 'DONE' in out and '✅ CPA' in out:
            # 解析本次产出的 CPA 文件路径 (只导入这一个, 禁止整目录重扫)
            m = re.search(r'CPA\s*已生成[:：]\s*(\S+)', out)
            return True, (m.group(1) if m else None), '注册成功'
        return False, None, out[-300:]

    def _import_new_cpa(self, task_id, cpa_path, since_ts):
        """只把【本次注册产出的】CPA 入库。

        严禁调用 import_all_cpa(): 它会把 /root/grok_accounts/cpa 下所有文件
        INSERT OR REPLACE 回 accounts 表, 导致用户在面板上删掉的账号被"复活"。
        用户在面板删号 = 明确意图, 任何自动流程都不得恢复。
        """
        from services import account_service
        targets = []
        if cpa_path and os.path.isfile(cpa_path):
            targets = [cpa_path]
        else:
            # 兜底: 解析不到路径时, 只取任务启动之后新生成的 CPA 文件
            for f in glob.glob(os.path.join(config.CPA_DIR, '*.json')):
                try:
                    if os.path.getmtime(f) >= since_ts - 5:
                        targets.append(f)
                except OSError:
                    pass
        n = 0
        for f in targets:
            email, err = account_service.import_cpa_file(f)
            if email:
                n += 1
                self._log(task_id, f"📥 已入库: {email}")
            elif err:
                self._log(task_id, f"⚠️ CPA入库失败 {os.path.basename(f)}: {err}")
        if not n:
            self._log(task_id, "⚠️ 未找到本次注册产出的 CPA, 跳过入库")
        return n

    def _log(self, task_id, msg):
        with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]['log'].append(msg)
                # 任务表只记录关键节点 (每5条写1次, 避免日志洪水)
                if len(self.tasks[task_id]['log']) % 5 == 1:
                    try:
                        conn = get_conn()
                        conn.execute("INSERT INTO tasks (type, status, detail, created_at) VALUES ('register', 'running', ?, ?)",
                                    (msg[:200], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)


registration_manager = RegistrationManager()
