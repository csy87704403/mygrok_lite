refresh.nodes = async function() {
  const el = document.getElementById('tab-nodes');
  const [r, rcs] = await Promise.all([
    api('/api/nodes'),
    api('/api/nodes/check-status')
  ]);
  const nodes = r.data || [];
  const active = nodes.filter(n => n.status === 'active').length;
  const cs = rcs.data || {};
  // 节点检测状态卡片
  let checkHtml = '';
  if (cs.last_run) {
    const d = new Date(cs.last_run * 1000);
    const results = cs.results || {};
    const okCount = Object.values(results).filter(v => v.status === 'ok').length;
    const dead = (cs.dead_ports || []).map(p => { const v = results[p]; return v ? `${p}(${v.code})` : p; });
    // 按延迟排序展示最快节点
    const fast = Object.entries(results).filter(([,v]) => v.status==='ok').sort((a,b)=>a[1].latency_ms-b[1].latency_ms).slice(0,5);
    checkHtml = `
    <div class="card">
      <h3>🔍 节点自动检测 <span style="font-size:12px;color:#94a3b8;font-weight:normal">每 ${((cs.interval_seconds||600)/60)} 分钟一轮</span></h3>
      <div class="row">
        <span class="metric"><span class="num">${okCount}</span><div class="lbl">可用节点</div></span>
        <span class="metric"><span class="num" style="color:${dead.length?'#dc2626':'#16a34a'}">${dead.length}</span><div class="lbl">死节点</div></span>
        <span class="metric"><span class="num">${d.toLocaleTimeString('zh-CN',{hour12:false})}</span><div class="lbl">上次检测</div></span>
      </div>
      ${dead.length ? `<div style="margin-top:6px;font-size:12px;color:#dc2626">❌ 死节点: ${dead.join(', ')} <span style="opacity:.7">(5分钟后自动复活重试)</span></div>` : ''}
      <div style="margin-top:6px;font-size:12px;color:#94a3b8">⚡ 最快节点: ${fast.map(([p,v])=>`${p}(${v.latency_ms}ms)`).join(' · ') || '-'}</div>
    </div>`;
  }
  let html = `
  <div class="card">
    <h3>节点池 (IP 代理池)</h3>
    <p style="margin-bottom:12px;color:#94a3b8">支持本地 Mihomo 端口 (如 8047) 和外部代理地址 (如 1.2.3.4:8080 或 socks5://ip:port)。注册/续期/API 调用会随机从活跃节点池选择出口 IP。当前 <b style="color:#16a34a">${active} 活跃</b> / ${nodes.length} 总数</p>
    <div class="row">
      <div style="flex:1"><label>批量粘贴节点 (每行一个)</label>
        <textarea id="node-batch" rows="6" style="width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:6px;padding:8px;font-family:monospace;font-size:12px" placeholder="8047&#10;8081&#10;1.2.3.4:8080&#10;socks5://5.6.7.8:1080&#10;http://user:pass@host:port"></textarea>
      </div>
      <div><label>默认代理类型</label>
        <select id="node-proxy-type">
          <option value="http">HTTP</option>
          <option value="socks5">SOCKS5</option>
        </select>
      </div>
      <div style="align-self:flex-end">
        <button class="ghost" onclick="checkNodesBatch()">🔍 检测有效</button>
        <button class="primary" onclick="addNodesBatch()">批量添加</button>
      </div>
    </div>
    <div id="node-check-result" style="margin-top:6px;display:none"></div>
    <div style="margin-top:6px">
      <span class="badge">每行一个节点</span>
      <span class="badge">支持: 8047 / 1.2.3.4:8080 / socks5://ip:port / http://user:pass@host:port</span>
      <span class="badge" style="color:#d97706">socks:// 前缀会自动识别为 SOCKS5</span>
      <span class="badge" style="color:#16a34a">先「检测有效」再批量添加, 无效节点标红</span>
    </div>
  </div>
  ${checkHtml}
  <div class="card">
    <h3>节点列表 (${nodes.length})</h3>
    <div class="table-wrap">
    <table>
      <tr><th>ID</th><th>地址</th><th>类型</th><th>名称</th><th>IP</th><th>状态</th><th>操作</th></tr>`;
  nodes.forEach(n => {
    const addr = (n.port || '').includes(':') && !/^\d+$/.test(n.port) ? n.port : (n.proxy_type === 'socks5' ? 'socks5://' : 'http://127.0.0.1:') + n.port;
    html += `<tr>
      <td>${n.id}</td>
      <td>${esc(addr)}</td>
      <td><span class="badge" style="background:${n.proxy_type==='socks5'?'#7c3aed':'#334155'}">${esc(n.proxy_type||'http')}</span></td>
      <td>${esc(n.name||'-')}</td>
      <td>${esc(n.ip||'-')}</td>
      <td><span class="status ${n.status}">${n.status}</span></td>
      <td>
        <button class="ghost" onclick="toggleNode(${n.id}, '${n.status==='active'?'disabled':'active'}')\">${n.status==='active'?'停用':'启用'}</button>
        <button class="danger" onclick="delNode(${n.id})">删除</button>
      </td>
    </tr>`;
  });
  html += `</table></div></div>`;
  el.innerHTML = html;
};

async function addNodesBatch() {
  const text = document.getElementById('node-batch').value.trim();
  if (!text) { toast('请粘贴节点列表', 'err'); return; }
  const default_proxy_type = document.getElementById('node-proxy-type').value;
  const r = await api('/api/nodes/batch', 'POST', { text, default_proxy_type });
  const d = r.data || {};
  toast(`批量添加: 成功${d.added}个, 跳过${d.skipped||0}个`, d.added > 0 ? 'ok' : 'err');
  if (d.errors && d.errors.length) console.warn('节点错误:', d.errors);
  document.getElementById('node-batch').value = '';
  document.getElementById('node-check-result').style.display = 'none';
  refresh.nodes();
}

// 检测输入框中的节点到 Grok 的连通性: 无效行标红 + 底部统计
async function checkNodesBatch() {
  const text = document.getElementById('node-batch').value.trim();
  if (!text) { toast('请粘贴节点列表', 'err'); return; }
  const default_proxy_type = document.getElementById('node-proxy-type').value;
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '检测中...';
  const r = await api('/api/nodes/check', 'POST', { text, default_proxy_type });
  btn.disabled = false;
  btn.textContent = '🔍 检测有效';
  const d = r.data || {};
  const box = document.getElementById('node-check-result');
  if (!box) return;
  box.style.display = 'block';
  if (!d.results || !d.results.length) { box.innerHTML = '<span style="color:#94a3b8">没有可检测的节点</span>'; return; }
  const rows = d.results.map(x => {
    const color = x.ok ? '#16a34a' : '#dc2626';
    const icon = x.ok ? '✅' : '❌';
    const info = x.ok ? `${x.latency_ms}ms` : (x.error || '无效');
    return `<div style="color:${color};font-family:monospace;font-size:12px;line-height:1.7">${icon} ${esc(x.line)} <span style="opacity:.8">(${info})</span></div>`;
  }).join('');
  // 统计: 有效 / 无效
  const valid = d.valid || 0, invalid = d.invalid || 0;
  const statColor = invalid > 0 ? '#dc2626' : '#16a34a';
  box.innerHTML = `
    <div style="margin-bottom:6px;font-size:12px;color:#94a3b8">检测结果: <span style="color:#16a34a">${valid} 有效</span> · <span style="color:${statColor};font-weight:bold">${invalid} 个无效</span></div>
    <div style="max-height:200px;overflow-y:auto;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px">${rows}</div>`;
  toast(`检测完成: ${valid} 有效, ${invalid} 无效`, invalid > 0 ? 'err' : 'ok');
}

async function toggleNode(id, status) {
  await api('/api/nodes/' + id + '/toggle', 'POST', { status });
  refresh.nodes();
}

async function delNode(id) {
  if (!confirm('确认删除该节点?')) return;
  await api('/api/nodes/' + id, 'DELETE');
  refresh.nodes();
}
