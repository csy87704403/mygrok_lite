refresh.keys = async function() {
  const el = document.getElementById('tab-keys');
  const r = await api('/api/keys');
  const keys = r.data || [];
  // 获取 Base URL (自动判断: 隧道可达用隧道, 否则公网IP)
  let baseUrl = '';
  let accessNote = '';
  try {
    const br = await api('/api/base-url');
    if (br.data && br.data.base_url) baseUrl = br.data.base_url;
    if (br.data && br.data.note) accessNote = br.data.note;
  } catch(e) {}
  let html = `
  <div class="card">
    <h3>创建 API Key</h3>
    <div class="row">
      <div><label>名称</label><input type="text" id="key-name" placeholder="如: 聊天机器人"></div>
      <div><label>备注</label><input type="text" id="key-note" placeholder="备注"></div>
      <button class="primary" onclick="createKey()">创建</button>
    </div>
    <div class="row" style="align-items:center">
      <label style="width:auto;margin-right:8px">Base URL:</label>
      <code id="base-url-text" style="background:#0f172a;padding:6px 10px;border-radius:6px;border:1px solid #334155;flex:1;word-break:break-all">${esc(baseUrl)}</code>
      <button class="ghost" onclick="copyText(document.getElementById('base-url-text').textContent.trim())">📋 复制</button>
    </div>
    <div style="margin-top:4px;font-size:11px;color:#94a3b8">${esc(accessNote || '自动判断可用入口: 隧道可达优先隧道, 否则公网IP')}</div>
  </div>
  <div class="card">
    <h3>API Keys (${keys.length})</h3>
    <div class="table-wrap">
    <table>
      <tr><th>名称</th><th>Key</th><th>状态</th><th>创建时间</th><th>操作</th></tr>`;
  keys.forEach(k => {
    html += `<tr>
      <td>${esc(k.name)}</td>
      <td><code class="copyable" onclick="copyText('${k.key}')" title="点击复制">${k.key.slice(0,20)}...</code></td>
      <td><span class="status ${k.status}">${k.status}</span></td>
      <td>${(k.created_at||'').replace('T',' ').slice(0,16)}</td>
      <td><button class="danger" onclick="delKey(${k.id})">删除</button></td>
    </tr>`;
  });
  html += `</table></div></div>`;
  el.innerHTML = html;
};

async function createKey() {
  const name = document.getElementById('key-name').value;
  const note = document.getElementById('key-note').value;
  const r = await api('/api/keys', 'POST', { name, note });
  toast('新 Key: ' + r.data.key, 'ok');
  /* copy */
  copyText(r.data.key);
  refresh.keys();
}

async function delKey(id) {
  if (!confirm('确认删除该 Key?')) return;
  await api('/api/keys/' + id, 'DELETE');
  toast('已删除', 'ok');
  refresh.keys();
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => toast('已复制到剪贴板', 'ok')).catch(() => toast('复制失败', 'err'));
}
