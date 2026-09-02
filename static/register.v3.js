refresh.register = async function() {
  const el = document.getElementById('tab-register');
  // 并行加载节点 + 邮箱域名配置
  const [nr, mr] = await Promise.all([
    api('/api/nodes'),
    api('/api/mail-domains').catch(() => ({data: []})),
  ]);
  const nodes = nr.data || [];
  const mails = mr.data || [];
  const activeNodes = nodes.filter(n => n.status === 'active');
  const activeMails = mails.filter(m => m.status === 'active');

  // 邮箱下拉选项: 有配置则显示配置的域名, 无则提示先添加
  let mailOpts;
  if (mails.length) {
    mailOpts = '<option value="">自动轮询全部 (' + activeMails.length + '个)</option>' +
      mails.map(m => `<option value="${esc(m.domain)}" ${m.status!=='active'?'disabled':''}>${esc(m.domain)}${m.status!=='active'?' (停用)':''}</option>`).join('');
  } else {
    mailOpts = '<option value="">(暂无邮箱配置)</option>';
  }

  // 已配置邮箱列表
  const mailRows = mails.map(m => `
    <tr>
      <td>${esc(m.domain)}</td>
      <td style="font-size:11px">${esc(m.base_url)}</td>
      <td>${m.status === 'active' ? '<span style="color:#16a34a">启用</span>' : '<span style="color:#94a3b8">停用</span>'}</td>
      <td>
        <button class="ghost" onclick="mailToggle(${m.id}, '${m.status === 'active' ? 'disabled' : 'active'}')">${m.status === 'active' ? '停用' : '启用'}</button>
        <button class="ghost" style="color:#dc2626" onclick="mailDelete(${m.id}, '${esc(m.domain)}')">删除</button>
      </td>
    </tr>`).join('') || '<tr><td colspan="4" style="color:#64748b">暂无配置，请在下方添加</td></tr>';

  el.innerHTML = `
  <!-- 注册日志面板 (顶部, 持久化: 刷新后从 _regLogHTML 恢复) -->
  <div id="reg-log-panel">${window._regLogHTML || ''}</div>
  <div class="card">
    <h3>账号注册</h3>
    <div class="row">
      <div><label>注册数量</label><input type="number" id="reg-count" value="1" min="1"></div>
      <div><label>临时邮箱域名</label>
        <select id="reg-domain">${mailOpts}</select>
        <div style="font-size:11px;color:#64748b">从下方「临时邮箱配置」添加/管理，停用域名不可选</div>
      </div>
      <div style="flex:1"><label>可用节点池 (${activeNodes.length}个)</label>
        <input type="text" id="reg-ports" placeholder="留空用全部, 或用逗号指定多个: 8047,8063,8081">
      </div>
      <button class="primary" onclick="startRegister()">开始注册</button>
      <button class="danger" id="btn-stop-reg-top" style="background:#dc2626;color:#fff;border:0;display:none" onclick="stopRegister()">🛑 停止注册</button>
    </div>
    <div style="margin-top:6px">
      <span class="badge">提示: 注册使用 MCDP 方案 (Mac指纹 + 物理点击 Turnstile)</span>
      <span class="badge">自定义节点: 在「节点池」tab 管理</span>
      <span class="badge">自定义邮箱: 下方配置 base_url + 域名 + 管理密码</span>
      <span class="badge" style="color:#d97706">域名为空时自动随机轮询全部已启用邮箱</span>
    </div>
  </div>
  <div class="card">
    <h3>临时邮箱配置</h3>
    <div class="row" style="align-items:end">
      <div><label>域名</label><input type="text" id="mail-domain" placeholder="如 mydomain.com" style="width:140px"></div>
      <div style="flex:1"><label>Base URL</label><input type="text" id="mail-base" placeholder="如 https://temp-email-api.example.com"></div>
      <div><label>管理密码</label><input type="password" id="mail-admin" placeholder="建址用的管理密码" style="width:160px"></div>
      <button class="primary" onclick="mailAdd()">添加邮箱配置</button>
    </div>
    <div class="table-wrap"><table style="margin-top:8px">
      <tr><th>域名</th><th>Base URL</th><th>状态</th><th>操作</th></tr>
      ${mailRows}
    </table>
  </div>
  <div class="card">
    <h3>注册任务</h3>
    <div class="row">
      <input type="text" id="reg-task-id" placeholder="任务ID (queue_xxx) 或留空看最近">
      <button class="ghost" onclick="queryTask()">查询</button>
      <button class="ghost" onclick="listTasks()">最近任务</button>
    </div>
    <div id="reg-poll-status" style="font-size:11px;color:#64748b;margin-top:4px"></div>
  </div>`;

  // 切回本tab时自动接管仍在运行的注册任务: 否则停止按钮随页面重建而消失,
  // 任务在后台跑却无从停止 (正是"注册一开始就无法停止"的体感来源)。
  try {
    const rr = await api('/api/register/running');
    const running = (rr && rr.data && rr.data.running) || [];
    if (running.length) {
      const t = running[0];
      const tidInput = document.getElementById('reg-task-id');
      if (tidInput) tidInput.value = t.task_id;
      currentRegTask = t.task_id;
      regStopVisible(true);
      if (!registerPoll) {
        registerPoll = true;
        pollRegisterTask(t.task_id);
      }
    }
  } catch (e) {}
};

// ===== 临时邮箱配置 CRUD =====
async function mailAdd() {
  const domain = document.getElementById('mail-domain').value.trim();
  const base = document.getElementById('mail-base').value.trim();
  const admin = document.getElementById('mail-admin').value.trim();
  if (!domain || !base) { toast('请填写域名和 Base URL', 'err'); return; }
  const r = await api('/api/mail-domains', 'POST', { domain, base_url: base, admin_password: admin, status: 'active' });
  toast(r.data.msg || (r.data.ok ? '已保存' : JSON.stringify(r.data)), r.data.ok ? 'ok' : 'err');
  refresh.register();
}
async function mailDelete(id, domain) {
  if (!confirm('确定删除邮箱配置 ' + domain + '？')) return;
  const r = await api('/api/mail-domains/' + id, 'DELETE');
  toast(r.data.ok ? '已删除' : '失败', r.data.ok ? 'ok' : 'err');
  refresh.register();
}
async function mailToggle(id, status) {
  const r = await api('/api/mail-domains/' + id + '/toggle', 'POST', { status });
  toast(r.data.ok ? '已更新' : '失败', r.data.ok ? 'ok' : 'err');
  refresh.register();
}

async function startRegister() {
  const count = parseInt(document.getElementById('reg-count').value || '1');
  const domain = document.getElementById('reg-domain').value;
  const portsRaw = document.getElementById('reg-ports').value;
  const node_ports = portsRaw ? portsRaw.split(',').map(s => s.trim()).filter(Boolean) : undefined;
  const r = await api('/api/register', 'POST', { count, domain, node_ports });
  if (r.data.task_id) {
    toast('注册任务已启动: ' + r.data.task_id, 'ok');
    document.getElementById('reg-task-id').value = r.data.task_id;
    registerPoll = true;
    currentRegTask = r.data.task_id;
    regStopVisible(true);          // 启动即显示停止按钮
    pollRegisterTask(r.data.task_id);
  } else {
    toast('启动失败: ' + JSON.stringify(r.data), 'err');
  }
}
let registerPoll = false;
let currentRegTask = '';

// 统一控制所有停止按钮的显隐。
// 注意: 停止按钮分散在顶部按钮区 + 日志面板两处, 且日志面板会被 regLogShow() 重建,
// 所以必须每次实时查询 DOM, 不能缓存元素引用 (缓存会因面板重建变成失效的游离节点)。
function regStopVisible(show) {
  ['btn-stop-reg-top', 'btn-stop-reg'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.style.display = show ? 'inline-block' : 'none';
  });
}

// ============ 注册日志面板 (顶部, 持久化) ============
window._regLogHTML = '';

function regLogShow(statusText) {
  window._regLogHTML = `
    <div class="card" style="margin:0 0 12px">
      <h3>📋 注册日志</h3>
      <div id="reg-log-status" style="font-size:12px;color:#94a3b8;margin-bottom:4px">${statusText || '准备中...'}</div>
      <div id="reg-log" class="log-box" style="max-height:300px;font-size:11px">启动注册后在此查看进度...</div>
      <div style="margin-top:8px">
        <button class="danger" id="btn-stop-reg" style="background:#dc2626;color:#fff;display:none" onclick="stopRegister()">🛑 停止注册</button>
      </div>
    </div>
  `;
  const panel = document.getElementById('reg-log-panel');
  if (panel) panel.innerHTML = window._regLogHTML;
}

function regLogUpdate(log, statusText) {
  const logEl = document.getElementById('reg-log');
  if (logEl && log && log.length > 0) {
    logEl.innerHTML = log.map(l => `<div style="color:${l.startsWith('✅')?'#16a34a':l.startsWith('❌')?'#dc2626':l.startsWith('⚠️')?'#d97706':'#94a3b8'}">${esc(l)}</div>`).join('');
    setTimeout(() => { logEl.scrollTop = logEl.scrollHeight; }, 50);
  }
  const statusEl = document.getElementById('reg-log-status');
  if (statusEl && statusText) statusEl.textContent = statusText;
  // 持久化
  const panel = document.getElementById('reg-log-panel');
  if (panel) window._regLogHTML = panel.innerHTML;
}

function regLogHide() {
  // 常驻显示: 不自动收起, 保留日志供查看
  // (如需清除, 刷新页面或切换tab后再回来即重置)
}

// 持续轮询注册任务 (每3秒), 实时显示逐步日志; 结束后刷新账号列表
async function pollRegisterTask(taskId) {
  const pollStatus = document.getElementById('reg-poll-status');
  regLogShow('注册任务运行中...');
  regStopVisible(true);   // 必须在 regLogShow 之后: 面板此刻才插入 DOM
  while (registerPoll) {
    try {
      const r = await api('/api/register/task/' + taskId);
      const t = r.data;
      if (!t) { regLogUpdate([], '任务不存在'); break; }
      const statusText = `状态: ${t.status} · 成功${t.registered||0} · 失败${t.failed||0}`;
      regLogUpdate(t.log || [], statusText);
      if (pollStatus) pollStatus.textContent = statusText;
      regStopVisible(t.status === 'running');   // 实时查询 DOM, 不缓存引用

      if (t.status !== 'running') {
        registerPoll = false;
        currentRegTask = '';
        regStopVisible(false);
        try { if (typeof refresh.accounts === 'function') refresh.accounts(); } catch(e) {}
        if (t.status === 'done') {
          const finalMsg = `注册完成: 成功${t.registered||0}, 失败${t.failed||0}`;
          regLogUpdate(t.log || [], finalMsg);
          toast(finalMsg, (t.registered||0) > 0 ? 'ok' : 'err');
          regLogHide();
        } else {
          toast('注册任务已停止', 'err');
          regLogHide();
        }
        break;
      }
    } catch(e) {}
    await new Promise(res => setTimeout(res, 3000));
  }
}

async function queryTask(id) {
  registerPoll = false;
  const taskId = id || document.getElementById('reg-task-id').value;
  if (!taskId) { toast('请输入任务ID', 'err'); return; }
  const r = await api('/api/register/task/' + taskId);
  const t = r.data;
  const statusText = `状态: ${t.status} · 成功${t.registered||0} · 失败${t.failed||0}`;
  regLogShow(statusText);
  regLogUpdate((t.log || []).concat([`--- 状态: ${t.status} ---`]), statusText);
  currentRegTask = taskId;
  regStopVisible(t.status === 'running');   // 同样在 regLogShow 之后实时查询
}

async function stopRegister() {
  const taskId = currentRegTask || document.getElementById('reg-task-id').value;
  // 不弹 confirm: 停止要快, 且用户点按钮时意图已明确
  regStopVisible(true);
  ['btn-stop-reg-top', 'btn-stop-reg'].forEach(id => {
    const b = document.getElementById(id);
    if (b) { b.disabled = true; b.textContent = '⏹ 停止中...'; }
  });
  try {
    // 用 stop-all: 自动补号(auto_fill)自行启动的任务前端拿不到 task_id,
    // 单任务 stop 停不掉它, 必须全停。同时会清掉补号在途标记防止被立即拉起。
    const r = await api('/api/register/stop-all', 'POST', {});
    const d = r.data || {};
    const n = d.count || 0;
    toast(n > 0 ? `已停止 ${n} 个注册任务` : '已发送停止信号', 'ok');
    if (taskId) {
      // 继续轮询到终态, 让用户看到"已停止"而不是卡在"运行中"
      registerPoll = true;
      pollRegisterTask(taskId);
    }
  } catch (e) {
    toast('停止失败: ' + e, 'err');
  } finally {
    ['btn-stop-reg-top', 'btn-stop-reg'].forEach(id => {
      const b = document.getElementById(id);
      if (b) { b.disabled = false; b.textContent = '🛑 停止注册'; }
    });
    setTimeout(() => { try { if (typeof refresh.accounts === 'function') refresh.accounts(); } catch(e){} }, 1500);
  }
}

async function listTasks() {
  const r = await api('/api/tasks?limit=20');
  const rows = (r.data || []).map(t => `${t.created_at.replace('T',' ').slice(0,19)} [${t.type}/${t.status}] ${esc(t.account_email||'')} ${esc(t.detail||'').slice(0,80)}`);
  regLogShow('最近 20 条任务记录');
  regLogUpdate(rows, '最近任务');
}
