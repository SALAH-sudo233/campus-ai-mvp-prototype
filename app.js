const API = '';
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
let token = localStorage.getItem(STORE_KEY) || '';
let backendOk = null;
let mode = 'email';
let revealedKey = '';

const $ = id => document.getElementById(id);
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2400)}
const fmt = n => Number(n).toLocaleString('zh-CN');

async function api(path, options){
  const headers = Object.assign({'Content-Type':'application/json'}, options && options.headers || {});
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(API + path, Object.assign({}, options, {headers}));
  let data = {};
  try { data = await resp.json(); } catch (e) { data = {}; }
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), {status: resp.status, data});
  return data;
}

async function checkBackend(){
  if (backendOk !== null) return backendOk;
  try {
    const resp = await fetch(API + '/api/health', {method:'GET'});
    backendOk = resp.ok;
  } catch (e) {
    backendOk = false;
  }
  return backendOk;
}

function maskKey(hint){
  return hint || '尚未发放';
}

function renderKey(relayKey){
  if (!relayKey) { $('relayKey').textContent = '尚未发放'; return; }
  if (relayKey.plaintext) {
    revealedKey = relayKey.plaintext;
    $('relayKey').textContent = relayKey.plaintext;
    $('keyNote').textContent = '完整密钥只在本次显示，刷新页面后只能看到掩码。请立即复制保存。';
  } else {
    revealedKey = '';
    $('relayKey').textContent = maskKey(relayKey.hint);
    $('keyNote').textContent = relayKey.status === 'unavailable'
      ? '中转站暂时不可用，稍后点击“重新生成”重试。'
      : '完整密钥只在发放时显示一次，平台与学院看不到密钥明文。';
  }
}

function renderAccount(view){
  $('loginView').classList.add('hidden');
  $('appView').classList.remove('hidden');
  $('userIdentity').textContent = view.identity;
  $('balance').textContent = fmt(view.balance);
  $('spent').textContent = fmt(view.spent);
  $('ledger').innerHTML = view.ledger && view.ledger.length
    ? view.ledger.map(x => `<div class="ledger-row"><div class="ledger-title"><span class="ledger-icon">${x.type === 'grant' ? '＋' : '✦'}</span><div><b>${x.title}</b><small>${x.time}</small></div></div><span class="amount ${x.amount > 0 ? 'plus' : 'minus'}">${x.amount > 0 ? '+' : ''}${fmt(x.amount)}</span></div>`).join('')
    : '<p class="muted">暂无流水记录</p>';
  if (view.relayKey) renderKey(view.relayKey);
}

function showLogin(){
  $('appView').classList.add('hidden');
  $('loginView').classList.remove('hidden');
}

async function refreshMe(){
  const view = await api('/api/me', {method:'GET'});
  renderAccount(view);
}

async function boot(){
  if (!token) { showLogin(); return; }
  try {
    await refreshMe();
  } catch (e) {
    token = '';
    localStorage.removeItem(STORE_KEY);
    showLogin();
  }
}

document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  mode = btn.dataset.mode;
  $('identityLabel').textContent = mode === 'email' ? '校园邮箱' : '学号';
  $('identity').placeholder = mode === 'email' ? '例如 2024001001@mail.hust.edu.cn' : '例如 2024001001';
  $('identityHint').textContent = mode === 'email' ? '原型演示可输入任意校园邮箱' : '原型演示可输入任意学号';
}));

function readForm(){
  const identity = $('identity').value.trim();
  const password = $('password').value;
  if (!identity) { toast('请输入校园邮箱或学号'); return null; }
  if (!password || password.length < 8) { toast('密码至少 8 位'); return null; }
  return {mode, identity, password};
}

async function handleAuth(path, successMsg){
  const form = readForm();
  if (!form) return;
  const ok = await checkBackend();
  if (!ok) { toast('演示后端未启动，请先运行 python3 server.py'); return; }
  try {
    const view = await api(path, {method: 'POST', body: JSON.stringify(form)});
    token = view.token;
    localStorage.setItem(STORE_KEY, token);
    renderAccount(view);
    toast(view.firstLogin ? successMsg : '登录成功，已恢复账户数据');
  } catch (e) {
    toast(e.message || '操作失败');
  }
}

$('loginBtn').onclick = () => handleAuth('/api/login', '登录成功');
$('registerBtn').onclick = () => handleAuth('/api/register', '注册成功，已领取 100,000 Token');

$('logoutBtn').onclick = async () => {
  try { await api('/api/logout', {method: 'POST', body: JSON.stringify({})}); } catch (e) {}
  token = '';
  revealedKey = '';
  localStorage.removeItem(STORE_KEY);
  showLogin();
};

$('rotateKeyBtn').onclick = async () => {
  try {
    const relayKey = await api('/api/key/rotate', {method: 'POST', body: JSON.stringify({})});
    renderKey(relayKey);
    toast('已重新生成密钥，旧密钥已失效');
  } catch (e) {
    toast(e.message || '重新生成失败');
  }
};

$('copyKeyBtn').onclick = async () => {
  if (!revealedKey) { toast('当前只能看到掩码，点“重新生成”获取新的完整密钥'); return; }
  try {
    await navigator.clipboard.writeText(revealedKey);
    toast('密钥已复制');
  } catch (e) {
    toast('复制失败，请手动选择文本');
  }
};

$('demoUseBtn').onclick = async () => {
  try {
    const view = await api('/api/consume', {method: 'POST', body: JSON.stringify({amount: 1200, requestId: 'ui-' + Date.now()})});
    renderAccount(view);
    toast('已模拟消耗 1,200 Token');
  } catch (e) {
    toast(e.status === 409 ? 'Token 余额不足' : (e.message || '操作失败'));
  }
};
$('createBtn').onclick = () => $('demoUseBtn').click();

function openModal(title, text){ $('modalTitle').textContent = title; $('modalText').textContent = text; $('modal').classList.remove('hidden'); }
$('rechargeBtn').onclick = () => openModal('充值额度', '充值功能将在正式版开放，原型阶段仅展示入口。');
$('closeModal').onclick = () => $('modal').classList.add('hidden');
$('modalAction').onclick = () => $('modal').classList.add('hidden');

$('resetFromLogin').onclick = $('resetBtn').onclick = async () => {
  const ok = await checkBackend();
  if (ok) {
    try { await api('/api/reset', {method: 'POST', body: JSON.stringify({})}); } catch (e) {}
  }
  token = '';
  revealedKey = '';
  localStorage.removeItem(STORE_KEY);
  showLogin();
  toast('演示数据已重置');
};

boot();
