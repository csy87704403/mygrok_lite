refresh.usage = async function() {
  const el = document.getElementById('tab-usage');
  const r = await api('/api/usage');
  const u = r.data || { total:{}, today:{}, by_model:[], by_account:[], by_key:[] };

  let html = `
  <div class="card">
    <h3>用量总览</h3>
    <span class="metric"><span class="num">${u.total.req||0}</span><div class="lbl">总请求</div></span>
    <span class="metric"><span class="num">${f(u.total.t)}</span><div class="lbl">总Tokens</div></span>
    <span class="metric"><span class="num">${u.today.req||0}</span><div class="lbl">今日请求</div></span>
    <span class="metric"><span class="num">${f(u.today.t)}</span><div class="lbl">今日Tokens</div></span>
  </div>
  <div class="card">
    <h3>按模型</h3>
    <div class="table-wrap"><table><tr><th>模型</th><th>Prompt</th><th>Completion</th><th>总Tokens</th><th>请求</th></tr>`;
  (u.by_model||[]).forEach(m => {
    html += `<tr><td>${esc(m.model)}</td><td>${m.p}</td><td>${m.c}</td><td>${m.t}</td><td>${m.req}</td></tr>`;
  });
  html += `</table></div>
  <div class="card">
    <h3>按账号</h3>
    <div class="table-wrap"><table><tr><th>账号</th><th>总Tokens</th><th>请求</th></tr>`;
  (u.by_account||[]).forEach(a => {
    html += `<tr><td>${esc(a.account_email)}</td><td>${a.t}</td><td>${a.req}</td></tr>`;
  });
  html += `</table></div>
  <div class="card">
    <h3>按 API Key</h3>
    <div class="table-wrap"><table><tr><th>Key</th><th>总Tokens</th><th>请求</th></tr>`;
  (u.by_key||[]).forEach(k => {
    html += `<tr><td><code>${esc((k.api_key||'').slice(0,20))}</code></td><td>${k.t}</td><td>${k.req}</td></tr>`;
  });
  html += `</table></div>`;
  el.innerHTML = html;
};

function f(n) { return (n||0).toLocaleString(); }