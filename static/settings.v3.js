// ============ 设置页 ============
refresh.settings = async function() {
  const el = document.getElementById('tab-settings');
  // 加载当前 CPA 设置
  let cpa = { mode: 'local', local_path: '', remote_url: '', remote_password: '' };
  try {
    const s = await api('/api/settings/cpa');
    cpa = s.data || cpa;
  } catch(e) {}
  // 加载自动补号状态
  let af = { enabled: false, trigger_threshold: 5, target_dispatchable: 30, last_result: null, in_progress: false };
  try {
    const s = await api('/api/auto-fill/status');
    af = s.data || af;
  } catch(e) {}

  el.innerHTML = `
  <div class="card">
    <h3>🔒 登录密码修改</h3>
    <div class="row">
      <div><label>旧密码</label><input type="password" id="set-old-pw" style="width:160px"></div>
      <div><label>新密码</label><input type="password" id="set-new-pw" style="width:160px"></div>
      <div><label>确认新密码</label><input type="password" id="set-confirm-pw" style="width:160px"></div>
      <button class="primary" onclick="changePassword()">修改密码</button>
    </div>
    <div id="pw-result" style="font-size:12px;color:#94a3b8;margin-top:6px"></div>
  </div>

  <div class="card">
    <h3>📦 CPA 设置</h3>
    <div style="margin-bottom:12px">
      <label style="margin-right:20px;cursor:pointer">
        <input type="radio" name="cpa-mode" value="local" ${cpa.mode==='local'?'checked':''} onchange="cpaModeChange()"> 本地 CPA
      </label>
      <label style="cursor:pointer">
        <input type="radio" name="cpa-mode" value="remote" ${cpa.mode==='remote'?'checked':''} onchange="cpaModeChange()"> 远程 CPA
      </label>
    </div>

    <!-- 本地 CPA -->
    <div id="cpa-local-box" class="row" ${cpa.mode==='local'?'':'style="display:none"'}>
      <div style="flex:1"><label>本地 CPA JSON 存放路径</label>
        <input type="text" id="cpa-local-path" value="${esc(cpa.local_path || '')}" placeholder="CPA输出目录">
      </div>
    </div>

    <!-- 远程 CPA -->
    <div id="cpa-remote-box" class="row" ${cpa.mode==='remote'?'':'style="display:none"'}>
      <div style="flex:1"><label>远程 CPA 登录地址 (WebDAV/HTTP 目录)</label>
        <input type="text" id="cpa-remote-url" value="${esc(cpa.remote_url || '')}" placeholder="https://example.com/cpa">
      </div>
      <div><label>登录密码</label>
        <input type="password" id="cpa-remote-pw" value="${esc(cpa.remote_password || '')}" style="width:160px">
      </div>
    </div>

    <div class="flex" style="margin-top:4px">
      <button class="primary" onclick="saveCpaSettings()">💾 保存设置</button>
      <button class="primary" onclick="importCpa()">📥 导入 CPA</button>
    </div>
    <div id="cpa-result" style="font-size:12px;color:#94a3b8;margin-top:8px"></div>
  </div>

  <div class="card">
    <h3>🔁 自动补号</h3>
    <div style="display:flex;align-items:center;gap:10px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px">
        <input type="checkbox" id="auto-fill-enabled" ${af.enabled?'checked':''} onchange="autoFillToggle()">
        启用自动补号（可调度号 ≤ <span id="af-threshold">${af.trigger_threshold||5}</span> 时自动注册补至 <span id="af-target">${af.target_dispatchable||30}</span> 个）
      </label>
      <span id="af-status" style="font-size:12px;${af.enabled?'color:#16a34a':'color:#94a3b8'}">${af.in_progress?'⏳ 补号进行中':(af.enabled?'✅ 已启用':'未启用')}</span>
    </div>
    <div id="af-result" style="font-size:12px;color:#94a3b8;margin-top:8px">${af.last_result?('上次补号: '+esc(af.last_result.summary||'')):''}</div>
  </div>

  <div class="card">
    <h3>ℹ️ 说明</h3>
    <div style="font-size:12px;color:#94a3b8;line-height:1.8">
      • <b>本地 CPA</b>: 从指定路径复制 cpa_*.json 文件到平台 CPA 目录并导入账号库<br>
      • <b>远程 CPA</b>: 从 WebDAV/HTTP 目录拉取 cpa_*.json (需支持 Basic Auth 或匿名访问)<br>
      • <b>导入 CPA</b>: 按当前勾选的模式, 将有效的 CPA 文件导入平台 (去重, 已存在账号自动更新)<br>
      • <b>自动补号</b>: 仅当勾选"启用自动补号"后才生效, 可调度号不足时自动批量注册补号<br>
      • 修改密码后, 下次登录使用新密码 (旧密码立即失效)
    </div>
  </div>`;
};

async function autoFillToggle() {
  const cb = document.getElementById('auto-fill-enabled');
  const res = document.getElementById('af-result');
  const status = document.getElementById('af-status');
  try {
    const r = await api('/api/auto-fill/set-enabled', 'POST', { enabled: cb.checked });
    const d = r.data || {};
    const on = d.enabled === true;
    status.textContent = on ? '✅ 已启用' : '未启用';
    status.style.color = on ? '#16a34a' : '#94a3b8';
    res.textContent = on ? '已启用, 可调度号不足时将自动补号' : '已关闭, 不再自动补号';
    res.style.color = '#16a34a';
  } catch(e) {
    cb.checked = !cb.checked;
    res.textContent = '设置失败: ' + (e.message || e);
    res.style.color = '#dc2626';
  }
}

function cpaModeChange() {
  const mode = document.querySelector('input[name="cpa-mode"]:checked').value;
  document.getElementById('cpa-local-box').style.display = mode === 'local' ? 'flex' : 'none';
  document.getElementById('cpa-remote-box').style.display = mode === 'remote' ? 'flex' : 'none';
}

async function changePassword() {
  const oldPw = document.getElementById('set-old-pw').value;
  const newPw = document.getElementById('set-new-pw').value;
  const confirmPw = document.getElementById('set-confirm-pw').value;
  const res = document.getElementById('pw-result');
  if (!oldPw || !newPw) { res.textContent = '请填写完整'; res.style.color = '#dc2626'; return; }
  if (newPw !== confirmPw) { res.textContent = '两次新密码不一致'; res.style.color = '#dc2626'; return; }
  const r = await api('/api/settings/password', 'POST', { old_password: oldPw, new_password: newPw });
  res.textContent = r.data.msg || JSON.stringify(r.data);
  res.style.color = r.data.ok ? '#16a34a' : '#dc2626';
  if (r.data.ok) {
    document.getElementById('set-old-pw').value = '';
    document.getElementById('set-new-pw').value = '';
    document.getElementById('set-confirm-pw').value = '';
  }
}

async function saveCpaSettings() {
  const mode = document.querySelector('input[name="cpa-mode"]:checked').value;
  const body = {
    mode,
    local_path: document.getElementById('cpa-local-path').value.trim(),
    remote_url: document.getElementById('cpa-remote-url').value.trim(),
    remote_password: document.getElementById('cpa-remote-pw').value.trim(),
  };
  const r = await api('/api/settings/cpa', 'POST', body);
  const res = document.getElementById('cpa-result');
  res.textContent = r.data.msg || JSON.stringify(r.data);
  res.style.color = r.data.ok ? '#16a34a' : '#dc2626';
}

async function importCpa() {
  const btn = event.target;
  btn.disabled = true;
  const res = document.getElementById('cpa-result');
  res.textContent = '导入中...';
  res.style.color = '#d97706';
  const r = await api('/api/settings/cpa/import', 'POST');
  res.textContent = r.data.msg || JSON.stringify(r.data);
  res.style.color = r.data.ok ? '#16a34a' : '#dc2626';
  btn.disabled = false;
  if (r.data.ok) {
    try { if (typeof refresh.accounts === 'function') refresh.accounts(); } catch(e) {}
  }
}
