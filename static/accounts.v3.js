refresh.accounts = async function() {
  const el = document.getElementById('tab-accounts');
  // 并行加载账号列表 + 可调度数 + 自动续期状态
  const [r, dr] = await Promise.all([
    api('/api/accounts'),
    api('/api/accounts/dispatchable').catch(() => ({data:{count:0}})),
  ]);
  const allAccs = r.data || [];
  const accs = allAccs;
  const active = allAccs.filter(a => a.status === 'active');
  const expired = allAccs.filter(a => a.status === 'expired');
  const others = allAccs.filter(a => a.status !== 'active' && a.status !== 'expired');
  const dispatchable = dr.data?.count ?? 0;
  const dpColor = dispatchable > 0 ? '#16a34a' : '#dc2626';

  // 总额度 = 各可用账号剩余额度总和 (仅统计 active + 有额度数据)
  let totalRemaining = 0, quotaCount = 0, exhausted = 0;
  active.forEach(a => {
    try {
      const q = typeof a.quota === 'string' ? JSON.parse(a.quota || '{}') : (a.quota || {});
      const r = q.remaining_tokens;
      if (typeof r === 'number' && r > 0) { totalRemaining += r; quotaCount++; }
      else if (typeof r === 'number' && r <= 0) { exhausted++; }  // 额度耗尽
    } catch(e) {}
  });
  const fmtQuota = totalRemaining >= 1000000
    ? (totalRemaining/1000000).toFixed(1) + 'M'
    : totalRemaining.toLocaleString();
  const exColor = exhausted > 0 ? '#dc2626' : '#16a34a';

  let html = `
  <div class="card">
    <h3>账号概览
    </h3>
    <div>
      <span class="metric"><span class="num">${accs.length}</span><div class="lbl">总账号</div></span>
      <span class="metric"><span class="num" style="color:#16a34a">${active.length}</span><div class="lbl">可用</div></span>
      <span class="metric"><span class="num" style="color:#dc2626">${expired.length}</span><div class="lbl">过期</div></span>
      <span class="metric"><span class="num" style="color:#d97706">${others.length}</span><div class="lbl">其他</div></span>
      <span class="metric"><span class="num" style="color:${dpColor}">${dispatchable}</span><div class="lbl">可调度</div></span>
      <span class="metric"><span class="num" style="color:#3b82f6">${fmtQuota}</span><div class="lbl">总额度</div></span>
      <span class="metric"><span class="num" style="color:${exColor}">${exhausted}</span><div class="lbl">额度耗尽</div></span>
    </div>
    <div style="margin-top:8px;font-size:13px;color:#94a3b8">
      当前有 <b style="color:${dpColor}">${dispatchable}</b> 个账号参与均衡调度
      <span style="margin-left:12px;font-size:11px;color:#64748b">（轮询 + 最少使用优先，不会重复调度同一账号直到用完额度）</span>
    </div>
  </div>
  <div class="card">
    <h3>自动续期 · 检测频率</h3>
    <div id="auto-refresh-status" style="font-size:12px;color:#94a3b8">加载中...</div>
  </div>
  <div class="card">
    <h3>🤖 自动补号</h3>
    <div id="auto-fill-status" style="font-size:12px;color:#94a3b8">加载中...</div>
  </div>
  <!-- 批量任务日志面板 (持久化: 刷新表格后从 window._logPanelHTML 恢复) -->
  <div id="relogin-panel">${window._logPanelHTML || ''}</div>
  <div class="card">
    <h3>操作</h3>
    <button class="primary" onclick="checkAll()">🔍 一键测活</button>
    <button class="primary" onclick="refreshAll()">🔄 一键续期 (仅失活, RT刷新)</button>
    <button class="primary" onclick="reloginAll()" id="btn-relogin-all">🔑 一键重登 (降级登录, 失活账号)</button>
    <span id="batch-result" style="margin-left:12px;color:#94a3b8"></span>
  </div>
  <div class="card">
    <h3>账号列表 (${allAccs.length})</h3>
    <div class="table-wrap">
    <table>
      <tr><th>邮箱</th><th>密码</th><th>状态</th><th>AT到期</th><th>额度</th><th>节点</th><th>重登进度</th><th>操作</th></tr>`;

  // 先取所有重登状态 (并行, 避免串行卡死)
  const reloginStates = {};
  const reloginResults = await Promise.all(accs.map(async a => {
    try {
      const s = await api('/api/accounts/relogin/' + a.email);
      return {email: a.email, data: s.data || {status:'idle', progress:0, log:[]}};
    } catch(e) {
      return {email: a.email, data: {status:'idle', progress:0, log:[]}};
    }
  }));
  reloginResults.forEach(x => { reloginStates[x.email] = x.data; });

  accs.forEach(a => {
    const st = a.status;
    const exp = a.expired ? a.expired.replace('T', ' ').slice(0, 16) : '-';
    let quotaTxt = '-';
    let quotaColor = '#94a3b8';
    let q = null;
    try {
      q = typeof a.quota === 'string' ? JSON.parse(a.quota) : a.quota;
      if (q && q !== {} && q.remaining_tokens !== undefined) {
        const remaining = Number(q.remaining_tokens) || 0;
        const limit = Number(q.limit_tokens) || 0;
        if (limit > 0) {
          const pct = Math.round(remaining / limit * 100);
          quotaTxt = `${Math.floor(remaining/1000)}k/${Math.floor(limit/1000)}k (${pct}%)`;
          if (pct > 50) quotaColor = '#16a34a';
          else if (pct > 20) quotaColor = '#d97706';
          else quotaColor = '#dc2626';
        } else {
          quotaTxt = `${remaining} tokens`;
          quotaColor = remaining > 0 ? '#16a34a' : '#dc2626';
        }
      } else {
        quotaTxt = JSON.stringify(q).slice(0, 40);
      }
    } catch(e) {}
    // 429 临时限流标注 (上游限流, 非额度耗尽, 几秒后自动恢复)
    if (a.rate_limited) {
      quotaTxt = '⏳限流中';
      quotaColor = '#d97706';
    }
    // 免费账号无真实 quota API，用 status 作为可用性指示
    if (quotaTxt === '-' || quotaTxt === '{}' || quotaTxt === '') {
      if (st === 'active') { quotaTxt = '可用'; quotaColor = '#16a34a'; }
      else if (st === 'expired') { quotaTxt = '不可用'; quotaColor = '#dc2626'; }
      else if (st === 'cooling') { quotaTxt = '冷却中'; quotaColor = '#d97706'; }
      else if (st === 'banned') { quotaTxt = '封禁'; quotaColor = '#7f1d1d'; }
    }
    // 如果 quota=0，显示红色 '额度耗尽'
    if (q && q.remaining_tokens == 0 && q.limit_tokens > 0) {
      quotaTxt = '额度耗尽';
      quotaColor = '#dc2626';
    }

    const isExpired = st === 'expired';
    const cannotRefresh = isExpired || st === 'cooling' || st === 'banned';
    const refreshLabel = cannotRefresh ? '<span style="color:#dc2626;font-size:11px;margin-left:4px">无法续期</span>' : '';
    const relogin = reloginStates[a.email] || {status:'idle', progress:0, log:[]};
    const isReloginRunning = relogin.status === 'running';
    const isReloginSuccess = relogin.status === 'success';
    const isReloginFailed = relogin.status === 'failed';
    const reloginDisabled = isReloginRunning ? 'disabled' : '';

    let progressBar = '';
    if (isReloginRunning) {
      progressBar = `<div style="width:100%;height:18px;background:#1e293b;border-radius:4px;overflow:hidden"><div style="width:${relogin.progress}%;height:100%;background:#3b82f6;transition:width .3s"></div></div><div style="font-size:11px;color:#3b82f6">重登中... ${relogin.progress}%</div>`;
    } else if (isReloginSuccess) {
      progressBar = `<div style="color:#16a34a;font-size:12px">✅ 重登成功</div>`;
    } else if (isReloginFailed) {
      progressBar = `<div style="color:#dc2626;font-size:12px">❌ 重登失败</div>`;
    }

    let actions = '';
    if (isExpired || st === 'cooling' || st === 'banned') {
      actions += `<button class="ghost" onclick="refreshAcc('${esc(a.email)}')" ${reloginDisabled}>续期 (RT)</button> `;
    }
    if (isExpired) {
      actions += `<button class="ghost" onclick="reloginAcc('${esc(a.email)}', this)" ${reloginDisabled}>重登</button> `;
    }
    // 删除按钮常驻: 所有状态都可删除账号
    actions += `<button class="ghost" style="color:#dc2626" onclick="deleteAcc('${esc(a.email)}')">删除</button> `;
    actions += `<button class="ghost" onclick="checkAcc('${esc(a.email)}')">测活</button>`;
    actions += ` <button class="ghost" onclick="quotaAcc('${esc(a.email)}')">额度</button>`;

    html += `<tr>
      <td title="${esc(a.password)}">${esc(a.email)}</td>
      <td title="${esc(a.password)}">${esc(a.password)}</td>
      <td><span class="status ${st}">${st}</span>${a.rate_limited ? '<span style="color:#d97706;font-size:11px;margin-left:4px">限流中</span>' : ''}${refreshLabel}</td>
      <td>${exp}</td>
      <td style="color:${quotaColor}">${quotaTxt}</td>
      <td>${esc(a.node_port || '-')}</td>
      <td style="min-width:140px">${progressBar}</td>
      <td>${actions}</td>
    </tr>`;
  });
  html += `</table></div>`;
  html += `</div>`;
  el.innerHTML = html;

  // 加载自动续期状态 (展示检测频率/下次运行)
  (async () => {
    try {
      const s = await api('/api/auto-refresh/status');
      const d = s.data || {};
      const seg = document.getElementById('auto-refresh-status');
      if (!seg) return;
      let txt = `${d.mode || '自动续期'}（扫描间隔 ${d.scan_interval_sec || 60}s）`;
      if (d.last_run_at) txt += `<br>上次兜底: ${d.last_run_at}`;
      if (d.last_output_tail) txt += `<br><span style="color:#64748b">${d.last_output_tail}</span>`;
      seg.innerHTML = txt;
    } catch(e) {
      const seg = document.getElementById('auto-refresh-status');
      if (seg) seg.textContent = '状态读取失败';
    }
  })();
  // 加载自动补号状态 (含最近一次补号结果)
  (async () => {
    try {
      const s = await api('/api/auto-fill/status');
      const d = s.data || {};
      const seg = document.getElementById('auto-fill-status');
      if (!seg) return;
      let txt = `可调度号 ≤ ${d.trigger_threshold} 自动补号至 ${d.target_dispatchable} 个 · 连续失败 ${d.max_consecutive_fails} 个自动停止`;
      txt += `<br>当前: 可调度 <b style="color:${d.dispatchable > d.trigger_threshold ? '#16a34a' : '#dc2626'}">${d.dispatchable}</b> 个`;
      if (d.in_progress) txt += `<br><span style="color:#d97706">🔄 补号进行中...</span>`;
      const r = d.last_result;
      if (r && r.summary) {
        const color = (r.failed || 0) === 0 ? '#16a34a' : ((r.registered || 0) > 0 ? '#d97706' : '#dc2626');
        txt += `<br><span style="color:${color}">📋 上次补号: ${r.summary}</span>`;
        if (r.finished_at) txt += ` <span style="color:#64748b">(${r.finished_at})</span>`;
      } else {
        txt += `<br><span style="color:#64748b">尚未触发过补号</span>`;
      }
      seg.innerHTML = txt;
    } catch(e) {
      const seg = document.getElementById('auto-fill-status');
      if (seg) seg.textContent = '状态读取失败';
    }
  })();
};

// ============ 批量任务日志面板 (统一管理, 持久化到 _logPanelHTML) ============
window._logPanelHTML = '';

function logPanelShow(title, progressId, logId, count) {
  // 渲染日志面板 (在自动续期卡片下方), 并持久化
  // 重登面板带取消按钮
  const cancelBtn = title.includes('重登') ? ` <button class="ghost" style="margin-left:8px;font-size:12px" onclick="cancelRelogin()">取消</button>` : '';
  window._logPanelHTML = `
    <div class="card" style="margin:12px 0">
      <h3>${title} (共${count}个)${cancelBtn}</h3>
      <div id="${progressId}" style="font-size:12px;color:#94a3b8;margin-bottom:4px">准备中...</div>
      <div id="${logId}" class="log-box" style="max-height:300px;font-size:11px"></div>
    </div>
  `;
  const panel = document.getElementById('relogin-panel');
  if (panel) panel.innerHTML = window._logPanelHTML;
}

function logPanelUpdate(progressId, logId, log, progressText) {
  // 更新日志内容 + 持久化 (刷新表格后能恢复)
  const logEl = document.getElementById(logId);
  const progEl = document.getElementById(progressId);
  if (logEl && log.length > 0) {
    const html = log.map(l => `<div style="color:${l.startsWith('✅')?'#16a34a':l.startsWith('❌')?'#dc2626':l.startsWith('⚠️')?'#d97706':'#94a3b8'}">${esc(l)}</div>`).join('');
    logEl.innerHTML = html;
    setTimeout(() => { logEl.scrollTop = logEl.scrollHeight; }, 50);
    // 持久化: 用占位符保存当前状态 (下次 refresh.accounts 恢复)
    window._logPanelHTML = document.getElementById('relogin-panel').innerHTML;
  }
  if (progEl && progressText) progEl.textContent = progressText;
}

function logPanelHide() {
  // 完成后8秒收起日志面板
  setTimeout(() => {
    window._logPanelHTML = '';
    const panel = document.getElementById('relogin-panel');
    if (panel) panel.innerHTML = '';
  }, 8000);
}

async function checkAll() {
  toast('一键测活开始...', 'ok');
  const r = await api('/api/accounts/check-all', 'POST');
  const taskId = r.data.task_id;
  if (!taskId) { toast('启动失败: ' + JSON.stringify(r.data), 'err'); return; }
  const count = r.data.total || 0;
  logPanelShow('🔍 一键测活', 'check-progress-text', 'check-log-box', count);
  const poll = setInterval(async () => {
    try {
      const s = await api('/api/accounts/check-all/' + taskId);
      const d = s.data || {};
      if (d.status === 'done') {
        clearInterval(poll);
        let ok = 0, fail = 0;
        (d.results || []).forEach(x => { if (x.result && x.result.status === 'active') ok++; else fail++; });
        logPanelUpdate('check-progress-text', 'check-log-box', d.current_log || [], `✅ 测活完成: ${ok} 可用, ${fail} 不可用 (共${d.total})`);
        toast(`测活完成: ${ok} 可用, ${fail} 不可用`, ok > 0 ? 'ok' : 'err');
        refresh.accounts();
        logPanelHide();
      } else {
        logPanelUpdate('check-progress-text', 'check-log-box', d.current_log || [], `测活中... (${(d.current_log||[]).length}/${d.total})`);
      }
    } catch(e) { clearInterval(poll); }
  }, 2000);
}

async function refreshAll() {
  if (!confirm('确定批量续期所有失活账号？仅用 RT 刷新 AT，不降级登录。')) return;
  const btn = event.target;
  btn.disabled = true;
  toast('一键续期开始...', 'ok');
  const r = await api('/api/accounts/refresh-all', 'POST');
  const taskId = r.data.task_id;
  const count = r.data.total || 0;
  if (!taskId) { toast('启动失败: ' + JSON.stringify(r.data), 'err'); btn.disabled = false; return; }
  logPanelShow('🔄 一键续期', 'refresh-progress-text', 'refresh-log-box', count);
  // 轮询结果
  const poll = setInterval(async () => {
    try {
      const s = await api('/api/accounts/refresh-all/' + taskId);
      const d = s.data || {};
      if (d.status === 'done') {
        clearInterval(poll);
        btn.disabled = false;
        let ok = 0, fail = 0;
        (d.results || []).forEach(x => { if (x.result && x.result.ok) ok++; else fail++; });
        logPanelUpdate('refresh-progress-text', 'refresh-log-box', d.current_log || [], `✅ 续期完成: ${ok} 成功, ${fail} 失败 (共${d.total})`);
        toast(`一键续期: ${ok} 成功, ${fail} 失败`, ok > 0 ? 'ok' : 'err');
        refresh.accounts();
        logPanelHide();
      } else {
        logPanelUpdate('refresh-progress-text', 'refresh-log-box', d.current_log || [], `续期中... (${(d.current_log||[]).length}/${d.total})`);
      }
    } catch(e) { clearInterval(poll); btn.disabled = false; }
  }, 2000);
}

async function reloginAll() {
  if (!confirm('确定对所有过期账号执行一键重登？这将逐个启动浏览器降级登录，耗时较长。')) return;
  // 标记重登进行中, 暂停自动刷新
  window._reloginInProgress = true;
  // 禁用所有操作按钮防重复点击
  document.querySelectorAll('.card button.primary').forEach(b => b.disabled = true);
  const btn = document.getElementById('btn-relogin-all');
  if (btn) btn.textContent = '⏳ 重登中...';
  // 加入队列
  const r = await api('/api/accounts/relogin-all', 'POST');
  if (!r.data.ok) {
    toast(r.data.msg || '启动失败', 'err');
    window._reloginInProgress = false;
    document.querySelectorAll('.card button.primary').forEach(b => b.disabled = false);
    if (btn) btn.textContent = '🔑 一键重登 (降级登录, 失活账号)';
    return;
  }
  const count = r.data.count || 0;
  toast(`已加入队列 ${count} 个账号`, 'ok');
  // 创建日志显示区 (自动续期卡片下方, 持久化不随刷新消失)
  logPanelShow('🔑 一键重登', 'relogin-progress-text', 'relogin-log-box', count);
  // 轮询队列进度 + 日志
  const poll = setInterval(async () => {
    try {
      const qr = await api('/api/accounts/relogin-queue');
      const q = qr.data || {};
      const done = (q.done || 0) + (q.failed || 0);
      const total = q.total || 0;
      const status = q.status || 'idle';
      const current = q.current || '';
      const log = q.current_log || [];
      const progress = q.current_progress || 0;
      if (status === 'idle' || status === 'cancelled') {
        clearInterval(poll);
        const msg = status === 'cancelled'
          ? `重登已取消 (完成${q.done||0}个, 失败${q.failed||0}个)`
          : `✅ 重登完成: ${q.done||0} 成功, ${q.failed||0} 失败 (共${total})`;
        logPanelUpdate('relogin-progress-text', 'relogin-log-box', log, msg);
        toast(msg, 'ok');
        // 恢复按钮 + 清除重登标记
        window._reloginInProgress = false;
        document.querySelectorAll('.card button.primary').forEach(b => b.disabled = false);
        if (btn) btn.textContent = '🔑 一键重登 (降级登录, 失活账号)';
        refresh.accounts();
        logPanelHide();
        return;
      }
      // 显示实时进度
      logPanelUpdate('relogin-progress-text', 'relogin-log-box', log, `重登中 (${done}/${total}) 当前: ${current} (${progress}%)`);
    } catch(e) {}
  }, 2000);
}

async function cancelRelogin() {
  await api('/api/accounts/relogin-queue/cancel', 'POST');
  toast('已发送取消信号', 'ok');
  // 取消后也恢复自动刷新
  window._reloginInProgress = false;
}

async function reloginAcc(email, btn) {
  toast(email + ' 重登开始...', 'ok');
  const r = await api('/api/accounts/' + encodeURIComponent(email) + '/relogin', 'POST');
  if (r.data.ok) {
    toast(email + ' 重登已启动 (task: ' + r.data.task_id + ')', 'ok');
    // 轮询进度
    const taskId = r.data.task_id;
    const poll = setInterval(async () => {
      try {
        const s = await api('/api/accounts/relogin/' + encodeURIComponent(email));
        const d = s.data || {};
        if (d.status === 'running') {
          refresh.accounts();
        } else if (d.status === 'success') {
          clearInterval(poll);
          toast(email + ' ✅ 重登成功', 'ok');
          refresh.accounts();
        } else if (d.status === 'failed' || d.status === 'busy') {
          clearInterval(poll);
          toast(email + ' ❌ 重登失败: ' + (d.log && d.log[0] ? d.log[0].slice(0,80) : 'unknown'), 'err');
          refresh.accounts();
        } else if (d.status === 'idle') {
          // 内存无状态，去数据库读真实状态
          refresh.accounts();
        }
      } catch(e) { clearInterval(poll); }
    }, 3000);
  } else {
    toast(email + ' 重登失败: ' + r.data.msg, 'err');
    refresh.accounts();
  }
}

async function importAccounts() {
  const r = await api('/api/accounts/import', 'POST');
  toast(`导入成功: ${r.data.imported} 个`, 'ok');
  refresh.accounts();
}

async function checkAcc(email) {
  toast('测活中...');
  const r = await api('/api/accounts/' + encodeURIComponent(email) + '/check', 'POST');
  if (r.data.status === 'active') toast(email + ' ✅ 可用 (models ' + r.data.code + ')', 'ok');
  else toast(email + ' ❌ ' + r.data.reason, 'err');
  refresh.accounts();
}

async function refreshAcc(email) {
  toast('续期中...');
  const r = await api('/api/accounts/' + encodeURIComponent(email) + '/refresh', 'POST');
  if (r.data.ok) toast(email + ' ' + r.data.msg, 'ok');
  else toast(email + ' 续期失败: ' + r.data.msg, 'err');
  refresh.accounts();
}

async function quotaAcc(email) {
  const r = await api('/api/accounts/' + encodeURIComponent(email) + '/quota', 'POST');
  toast(JSON.stringify(r.data.quota || r.data).slice(0, 200));
}

async function deleteAcc(email) {
  if (!confirm('确定删除账号 ' + email + '？此操作不可恢复。')) return;
  const r = await api('/api/accounts/' + encodeURIComponent(email), 'DELETE');
  if (r.data.ok) { toast(email + ' 已删除', 'ok'); refresh.accounts(); }
  else toast('删除失败: ' + r.data.msg, 'err');
}
