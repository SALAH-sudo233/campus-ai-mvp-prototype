const ADMIN_STORE_KEY = 'hust-campus-ai-admin-token-v1';
const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
let adminToken = sessionStorage.getItem(ADMIN_STORE_KEY) || '';

const $ = id => document.getElementById(id);
function toast(msg){const el=$('toast');el.textContent=msg;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),2400)}

async function adminApi(path, options){
  const headers = {'Content-Type':'application/json'};
  if (adminToken) headers['Authorization'] = 'Bearer ' + adminToken;
  const resp = await fetch(API + path, Object.assign({}, options, {headers}));
  let data = {};
  try { data = await resp.json(); } catch (e) { data = {}; }
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), {status: resp.status});
  return data;
}

function showLogin(){
  adminToken = '';
  sessionStorage.removeItem(ADMIN_STORE_KEY);
  $('adminView').classList.add('hidden');
  $('adminLogin').classList.remove('hidden');
}

function showAdmin(){
  $('adminLogin').classList.add('hidden');
  $('adminView').classList.remove('hidden');
}

function cell(text, className){
  const td = document.createElement('td');
  if (className) {
    const span = document.createElement('span');
    span.className = className;
    span.textContent = text;
    td.appendChild(span);
  } else {
    td.textContent = text;
  }
  return td;
}

const KEY_STATUS_TEXT = {active: '有效', revoked: '已吊销', none: '无有效密钥'};
const MODE_TEXT = {email: '邮箱', student: '学号'};

function renderRows(accounts){
  const body = $('accountRows');
  body.replaceChildren();
  $('emptyHint').classList.toggle('hidden', accounts.length > 0);
  for (const account of accounts) {
    const row = document.createElement('tr');
    row.appendChild(cell(account.owner_ref));
    row.appendChild(cell(MODE_TEXT[account.mode] || account.mode));
    row.appendChild(cell(account.identity));
    row.appendChild(cell(account.name || '（名册外）'));
    row.appendChild(cell(account.key_hint || '—'));
    row.appendChild(cell(KEY_STATUS_TEXT[account.key_status] || account.key_status, 'badge ' + (account.key_status === 'active' ? 'active' : 'none')));
    const action = document.createElement('td');
    if (account.key_status === 'active') {
      const button = document.createElement('button');
      button.className = 'danger';
      button.textContent = '吊销';
      button.addEventListener('click', () => revoke(account));
      action.appendChild(button);
    } else {
      action.textContent = '—';
    }
    row.appendChild(action);
    body.appendChild(row);
  }
}

async function loadAccounts(){
  const query = $('searchInput').value.trim();
  try {
    const data = await adminApi('/api/admin/accounts' + (query ? '?q=' + encodeURIComponent(query) : ''));
    renderRows(data.accounts);
  } catch (err) {
    if (err.status === 401) { showLogin(); toast('登录已过期，请重新登录'); return; }
    toast(err.message);
  }
}

async function revoke(account){
  const label = account.name ? account.name + '（' + account.identity + '）' : account.identity;
  if (!confirm('确认吊销 ' + label + ' 的当前密钥？吊销后该密钥立即失效，学生下次登录会重新发放。')) return;
  try {
    await adminApi('/api/admin/revoke', {method: 'POST', body: JSON.stringify({owner_ref: account.owner_ref})});
    toast('已吊销');
    await loadAccounts();
  } catch (err) {
    if (err.status === 401) { showLogin(); toast('登录已过期，请重新登录'); return; }
    toast(err.message);
  }
}

async function login(){
  const username = $('adminUsername').value.trim();
  const password = $('adminPassword').value;
  if (!username || !password) { toast('请输入管理员账号和密码'); return; }
  try {
    const data = await adminApi('/api/admin/login', {method: 'POST', body: JSON.stringify({username, password})});
    adminToken = data.token;
    sessionStorage.setItem(ADMIN_STORE_KEY, adminToken);
    $('adminPassword').value = '';
    showAdmin();
    await loadAccounts();
  } catch (err) {
    toast(err.message);
  }
}

$('adminLoginBtn').addEventListener('click', login);
$('adminPassword').addEventListener('keydown', event => { if (event.key === 'Enter') login(); });
$('searchBtn').addEventListener('click', loadAccounts);
$('searchInput').addEventListener('keydown', event => { if (event.key === 'Enter') loadAccounts(); });
$('refreshBtn').addEventListener('click', loadAccounts);
$('adminLogoutBtn').addEventListener('click', showLogin);

if (adminToken) { showAdmin(); loadAccounts(); }
