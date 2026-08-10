let ADMIN_TOKEN = localStorage.getItem('grok_admin_token') || '';

function toast(msg, type='') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast', 2500);
}

async function api(path, method='GET', body=null) {
  const opt = { method, headers: { 'Authorization': 'Bearer ' + ADMIN_TOKEN } };
  if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && ADMIN_TOKEN) {
    localStorage.removeItem('grok_admin_token');
    location.reload();
  }
  return { status: r.status, data };
}

function esc(s) { return (s||'').replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// ============ 登录 ============
document.getElementById('login-btn').addEventListener('click', async () => {
  const pw = document.getElementById('admin-pass').value;
  const r = await fetch('/api/accounts', { headers: { 'Authorization': 'Bearer ' + pw } });
  if (r.status === 200) {
    ADMIN_TOKEN = pw;
    localStorage.setItem('grok_admin_token', pw);
    document.getElementById('login-view').style.display = 'none';
    document.getElementById('app-view').style.display = 'block';
    init();
  } else {
    document.getElementById('login-err').textContent = '密码错误';
  }
});

document.querySelector('.logout').addEventListener('click', () => {
  localStorage.removeItem('grok_admin_token');
  location.reload();
});

// ============ Tab 切换 ============
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById('tab-' + btn.dataset.tab);
    if (panel) panel.classList.add('active');
    const fn = refresh[btn.dataset.tab];
    if (typeof fn === 'function') fn();
  });
});

// ============ 模块加载 + 初始渲染 ============
const refresh = {};

async function loadScript(name) {
  return new Promise((resolve, reject) => {
    const sc = document.createElement('script');
    sc.src = '/static/' + name + '.v3.js';
    sc.onload = resolve;
    sc.onerror = reject;
    document.head.appendChild(sc);
  });
}

async function init() {
  if (!ADMIN_TOKEN) { location.reload(); return; }
  document.getElementById('login-view').style.display = 'none';
  document.getElementById('app-view').style.display = 'block';

  // 加载全部模块脚本 (串行避免竞态)
  for (const name of ['accounts', 'register', 'keys', 'usage', 'nodes', 'settings']) {
    try { await loadScript(name); } catch(e) { console.warn('加载模块失败:', name, e); }
  }
  // 默认渲染账号 tab
  if (typeof refresh.accounts === 'function') refresh.accounts();
  // 账号列表每30秒自动刷新 (静默拉取, 不弹toast)
  if (window._grokAutoRefresh) clearInterval(window._grokAutoRefresh);
  window._grokAutoRefresh = setInterval(() => {
    // 重登进行中时跳过自动刷新, 避免销毁日志区
    if (window._reloginInProgress) return;
    if (typeof refresh.accounts === 'function') refresh.accounts();
  }, 30000);
}

// 页面加载完成后, 如有 token 直接进入
window.addEventListener('load', () => {
  if (ADMIN_TOKEN) init();
});
