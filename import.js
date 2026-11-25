const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STUDENT_KEY = 'hust-campus-ai-demo-token-v2';
const ADMIN_KEY = 'hust-campus-ai-admin-token-v1';
const $ = id => document.getElementById(id);
const el = UI.el;
const fmt = n => Number(n || 0).toLocaleString('zh-CN');
const ADMISSION_LABELS = { active: '已准入', disabled: '已停用', pending_review: '待复核' };
let authToken = localStorage.getItem(STUDENT_KEY) || sessionStorage.getItem(ADMIN_KEY) || '';
let orgId = new URLSearchParams(location.search).get('org') || '';
let roleLabels = { admin: '教师', member: '学生' };

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

async function request(path, options) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (options && options.headers) || {});
  if (authToken) headers.Authorization = 'Bearer ' + authToken;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  let data = {};
  try { data = await resp.json(); } catch (error) { data = {}; }
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  return data;
}

function denied() {
  $('teacherNav').classList.add('hidden');
  $('teacherLogout').classList.add('hidden');
  $('importMain').classList.add('hidden');
  $('importDenied').classList.remove('hidden');
}

function showMain(orgName, isPlatformAdmin) {
  $('importDenied').classList.add('hidden');
  $('teacherNav').classList.remove('hidden');
  $('importMain').classList.remove('hidden');
  if (orgName) $('teacherOrg').textContent = orgName;
  if (isPlatformAdmin) {
    $('dashboardLink').textContent = '返回管理端';
    $('dashboardLink').href = 'admin.html';
    $('galleryLink').classList.add('hidden');
    $('teacherLogout').classList.add('hidden');
  } else {
    $('dashboardLink').textContent = '教学看板';
    $('dashboardLink').href = 'teacher.html';
    $('galleryLink').classList.remove('hidden');
    $('teacherLogout').classList.remove('hidden');
  }
}

async function boot() {
  fetch(API + '/api/config').then(resp => resp.json()).then(config => {
    for (const node of document.querySelectorAll('[data-product-name]')) node.textContent = config.productName;
    document.title = config.productName + ' · 成员管理';
  }).catch(() => {});
  if (!authToken) { denied(); return; }
  const userToken = localStorage.getItem(STUDENT_KEY) || '';
  let orgName = '';
  let isPlatformAdmin = false;
  if (userToken) {
    try {
      const me = await request('/api/me');
      if (me.role === 'org_member') { window.location.replace('index.html'); return; }
      if (me.role === 'org_admin') { window.location.replace('teacher.html'); return; }
      if (me.role !== 'platform_admin') { denied(); return; }
      orgId = orgId || (me.org && me.org.id);
      orgName = (me.org && me.org.name) || '';
      if (me.labels) roleLabels = me.labels;
    } catch (error) {
      authToken = sessionStorage.getItem(ADMIN_KEY) || '';
      if (!authToken) { denied(); return; }
      isPlatformAdmin = true;
    }
  } else if (sessionStorage.getItem(ADMIN_KEY)) {
    isPlatformAdmin = true;
  }
  if (!orgId) {
    try {
      const orgs = await request('/api/orgs');
      const first = orgs.organizations && orgs.organizations[0];
      orgId = first && first.id;
      if (first && !orgName) orgName = first.name;
      if (first) roleLabels = { admin: first.adminLabel || '教师', member: first.memberLabel || '学生' };
    } catch (inner) { denied(); return; }
  }
  if (!orgId) { denied(); return; }
  showMain(orgName, isPlatformAdmin);
  await loadMembers();
}

async function loadMembers(query) {
  const suffix = query ? '?q=' + encodeURIComponent(query) : '';
  try {
    const data = await request('/api/orgs/' + encodeURIComponent(orgId) + '/members' + suffix);
    renderMembers(data.members || []);
  } catch (error) { toast(error.message || '成员列表暂时不可用'); }
}

function renderMembers(rows) {
  const table = $('memberTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['姓名', '学号', '角色', '余额', '消耗', '密钥掩码', '准入'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  if (!rows.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '7', className: 'muted', text: '暂无匹配成员' })]));
  }
  for (const row of rows) {
    const tone = row.admissionStatus === 'active' ? 'active' : row.admissionStatus === 'pending_review' ? 'skipped' : 'none';
    body.appendChild(el('tr', {}, [
      el('td', { text: row.name || '—' }),
      el('td', { text: row.studentId || '—' }),
      el('td', {}, [UI.RoleBadge(row, roleLabels)]),
      el('td', { text: fmt(row.balance) }),
      el('td', { text: fmt(row.spent) }),
      el('td', {}, [el('code', { text: row.keyHint || '无有效密钥' })]),
      el('td', {}, [el('span', { className: 'badge ' + tone, text: ADMISSION_LABELS[row.admissionStatus] || row.admissionStatus || '未知' })])
    ]));
  }
  table.appendChild(body);
}

$('memberSearchBtn').onclick = () => loadMembers($('memberSearch').value.trim());
$('memberSearch').addEventListener('keydown', event => { if (event.key === 'Enter') loadMembers($('memberSearch').value.trim()); });
$('teacherLogout').onclick = async () => {
  try { await request('/api/logout', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  localStorage.removeItem(STUDENT_KEY);
  location.href = 'index.html';
};
$('importBackLogin').onclick = () => { localStorage.removeItem(STUDENT_KEY); };

boot();
