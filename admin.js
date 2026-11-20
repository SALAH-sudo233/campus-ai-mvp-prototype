const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const ADMIN_STORE_KEY = 'hust-campus-ai-admin-token-v1';
let adminToken = sessionStorage.getItem(ADMIN_STORE_KEY) || '';
let organizations = [];
let selectedOrg = null;
let memberRows = [];
let orgLabels = { admin: '教师', member: '学生' };

const $ = id => document.getElementById(id);
const el = UI.el;
const fmt = n => Number(n || 0).toLocaleString('zh-CN');

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

async function api(path, options) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (options && options.headers) || {});
  if (adminToken) headers.Authorization = 'Bearer ' + adminToken;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  let data = {};
  try { data = await resp.json(); } catch (error) { data = {}; }
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  return data;
}

function showLogin() {
  adminToken = '';
  sessionStorage.removeItem(ADMIN_STORE_KEY);
  $('adminView').classList.add('hidden');
  $('adminLogin').classList.remove('hidden');
}

async function showAdmin() {
  $('adminLogin').classList.add('hidden');
  $('adminView').classList.remove('hidden');
  await loadOrgs();
  await Promise.all([loadRelay(), loadAudit()]);
}

async function login() {
  const username = $('adminUsername').value.trim();
  const password = $('adminPassword').value;
  if (!username || !password) { toast('请输入管理员账号和密码'); return; }
  try {
    const data = await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    adminToken = data.token;
    sessionStorage.setItem(ADMIN_STORE_KEY, adminToken);
    $('adminPassword').value = '';
    await showAdmin();
  } catch (error) { toast(error.message || '登录失败'); }
}

async function loadOrgs() {
  try {
    const data = await api('/api/orgs');
    organizations = data.organizations || [];
    const list = $('orgList');
    list.replaceChildren();
    for (const org of organizations) {
      list.appendChild(el('button', { className: 'org-item' + (selectedOrg === org.id ? ' active' : ''), onclick: () => selectOrg(org.id) }, [
        el('b', { text: org.name }), el('small', { text: org.memberCount + ' 名成员 · ' + org.slug })
      ]));
    }
    if (!selectedOrg && organizations.length) await selectOrg(organizations[0].id);
    else await loadPlatformTree();
  } catch (error) {
    if (error.status === 401) showLogin(); else toast(error.message);
  }
}

async function loadPlatformTree() {
  try {
    const trees = await Promise.all(organizations.map(org =>
      api('/api/orgs/' + encodeURIComponent(org.id) + '/tree')));
    const root = {
      id: 'platform', name: '平台', count: trees.reduce((sum, tree) => sum + Number(tree.count || 0), 0),
      children: trees
    };
    $('adminOrgTree').replaceChildren(UI.OrgTree(root, {
      onSelect: item => toast((item.name || '成员') + ' · ' + (item.studentId || '无学号'))
    }));
  } catch (error) { toast(error.message || '组织树暂时不可用'); }
}

async function selectOrg(orgId) {
  selectedOrg = orgId;
  document.querySelectorAll('.org-item').forEach((node, index) => node.classList.toggle('active', organizations[index] && organizations[index].id === orgId));
  const org = organizations.find(item => item.id === orgId);
  if (org) orgLabels = { admin: org.adminLabel || '教师', member: org.memberLabel || '学生' };
  $('selectedOrgLabel').textContent = org ? org.name : orgId;
  try {
    const [dashboard, members, works] = await Promise.all([
      api('/api/orgs/' + encodeURIComponent(orgId) + '/dashboard?days=90'),
      api('/api/orgs/' + encodeURIComponent(orgId) + '/members'),
      api('/api/orgs/' + encodeURIComponent(orgId) + '/works')
    ]);
    const s = dashboard.summary;
    $('adminOrgStats').replaceChildren(
      UI.MetricCard({ label: '成员', value: s.memberCount }),
      UI.MetricCard({ label: '活跃', value: s.activeMemberCount, tone: 'accent' }),
      UI.MetricCard({ label: '总消耗', value: s.totalConsumed }),
      UI.MetricCard({ label: '剩余额度', value: s.remainingQuota })
    );
    memberRows = members.members || [];
    renderMembers();
    renderWorks(works.items || []);
    await loadPlatformTree();
  } catch (error) { toast(error.message || '组织数据暂时不可用'); }
}

function renderMembers() {
  const table = $('adminMemberTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['', '姓名', '学号', '角色', '余额', '消耗', '密钥掩码', '准入', '操作'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  for (const row of memberRows) {
    const checkbox = el('input', { type: 'checkbox', 'data-user': row.userId, 'aria-label': '选择 ' + row.name });
    const status = el('select', { className: 'compact-select', onchange: event => updateMember(row, { admissionStatus: event.target.value }) }, [
      ['active', '已准入'], ['disabled', '已停用'], ['pending_review', '待复核']
    ].map(([value, label]) => el('option', { value, selected: row.admissionStatus === value, text: label })));
    body.appendChild(el('tr', {}, [
      el('td', {}, [checkbox]), el('td', { text: row.name || '—' }), el('td', { text: row.studentId || '—' }),
      el('td', {}, [UI.RoleBadge(row, orgLabels)]), el('td', { text: fmt(row.balance) }),
      el('td', { text: fmt(row.spent) }), el('td', {}, [el('code', { text: row.keyHint || '无有效密钥' })]),
      el('td', {}, [status]), el('td', {}, [
        el('button', { className: 'ghost', text: '设为教师', onclick: () => updateMember(row, { role: 'org_admin' }) }),
        el('button', { className: 'danger', text: '吊销', onclick: () => revokeOne(row) })
      ])
    ]));
  }
  table.appendChild(body);
}

async function updateMember(row, changes) {
  try {
    await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members/' + encodeURIComponent(row.userId), {
      method: 'PATCH', body: JSON.stringify(changes)
    });
    await selectOrg(selectedOrg);
    toast('成员信息已更新');
  } catch (error) { toast(error.message || '更新失败'); }
}

async function revokeOne(row) {
  if (!confirm('确认吊销 ' + (row.name || row.studentId) + ' 的密钥？')) return;
  try { await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members/' + encodeURIComponent(row.userId) + '/revoke-key', { method: 'POST', body: '{}' }); await selectOrg(selectedOrg); toast('已吊销'); }
  catch (error) { toast(error.message || '操作失败'); }
}

function selectedUsers() {
  return Array.from(document.querySelectorAll('#adminMemberTable input[type=checkbox]:checked')).map(node => node.dataset.user);
}

async function bulk(action) {
  const userIds = selectedUsers();
  if (!userIds.length) { toast('请先勾选成员'); return; }
  const payload = { action, userIds, requestId: 'admin-ui-' + action + '-' + Date.now() };
  if (action === 'grant') {
    payload.amount = Number($('bulkAmount').value);
    if (!payload.amount || payload.amount < 1) { toast('请输入赠送额度'); return; }
  }
  if (action === 'revoke' && !confirm('确认吊销选中成员的当前密钥？')) return;
  try {
    await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members/bulk', { method: 'POST', body: JSON.stringify(payload) });
    await selectOrg(selectedOrg);
    toast(action === 'grant' ? '批量赠送完成' : '批量吊销完成');
  } catch (error) { toast(error.message || '批量操作失败'); }
}

function renderWorks(items) {
  const wrap = $('adminWorks');
  wrap.replaceChildren();
  if (!items.length) { wrap.appendChild(el('p', { className: 'muted empty', text: '暂无作品。' })); return; }
  for (const work of items) {
    const actions = work.status === 'published' || work.status === 'pending_review'
      ? [{ label: '全局下架', tone: 'danger', onClick: () => globalUnpublish(work) }] : [];
    wrap.appendChild(UI.WorkCard(work, { density: 'compact', showStatus: true, actions,
      onOpen: item => toast(item.title + '：演示作品仅展示卡片与预览说明。') }));
  }
}

async function globalUnpublish(work) {
  if (!confirm('确认全局下架《' + work.title + '》？')) return;
  try { await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/works/' + encodeURIComponent(work.workId) + '/unpublish', { method: 'POST', body: '{}' }); await selectOrg(selectedOrg); toast('已全局下架'); }
  catch (error) { toast(error.message || '下架失败'); }
}

async function loadRelay() {
  const query = $('relaySearch').value.trim();
  try {
    const data = await api('/api/admin/accounts' + (query ? '?q=' + encodeURIComponent(query) : ''));
    const table = $('relayTable');
    table.replaceChildren(el('thead', {}, [el('tr', {}, ['学生编号', '身份', '姓名', '组织', '密钥掩码', '状态', '操作'].map(label => el('th', { text: label })))]));
    const body = el('tbody');
    for (const account of data.accounts) {
      body.appendChild(el('tr', {}, [
        el('td', {}, [el('code', { text: account.owner_ref })]), el('td', { text: account.identity }),
        el('td', { text: account.name || '（名册外）' }), el('td', { text: account.org_id || '—' }),
        el('td', {}, [el('code', { text: account.key_hint || '—' })]),
        el('td', {}, [el('span', { className: 'badge ' + (account.key_status === 'active' ? 'active' : 'none'), text: account.key_status })]),
        el('td', {}, [account.key_status === 'active' ? el('button', { className: 'danger', text: '吊销', onclick: () => revokeRelay(account) }) : el('span', { text: '—' })])
      ]));
    }
    table.appendChild(body);
  } catch (error) { toast(error.message || '台账暂时不可用'); }
}

async function revokeRelay(account) {
  if (!confirm('确认吊销该账户当前密钥？')) return;
  try { await api('/api/admin/revoke', { method: 'POST', body: JSON.stringify({ owner_ref: account.owner_ref }) }); await loadRelay(); toast('已吊销'); }
  catch (error) { toast(error.message || '吊销失败'); }
}

async function loadAudit() {
  try {
    const data = await api('/api/audit-events');
    const table = $('adminAuditTable');
    table.replaceChildren(el('thead', {}, [el('tr', {}, ['时间', '组织', '角色', '动作', '资源', '请求编号'].map(label => el('th', { text: label })))]));
    const body = el('tbody');
    for (const event of data.events) {
      body.appendChild(el('tr', {}, [
        el('td', { text: new Date(event.createdAt * 1000).toLocaleString('zh-CN') }), el('td', { text: event.orgId || '—' }),
        el('td', { text: event.actorRole || '—' }), el('td', { text: event.action }),
        el('td', { text: event.resourceType + ' / ' + (event.resourceId || '—') }), el('td', { text: event.requestId || '—' })
      ]));
    }
    table.appendChild(body);
  } catch (error) { /* audit is secondary */ }
}

$('adminLoginBtn').addEventListener('click', login);
$('adminPassword').addEventListener('keydown', event => { if (event.key === 'Enter') login(); });
$('adminLogoutBtn').addEventListener('click', showLogin);
document.querySelectorAll('[data-admin-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-admin-tab]').forEach(node => node.classList.toggle('active', node === button));
  document.querySelectorAll('.admin-tab').forEach(node => node.classList.toggle('active', node.id === 'tab-' + button.dataset.adminTab));
  if (button.dataset.adminTab === 'audit') loadAudit();
  if (button.dataset.adminTab === 'relay') loadRelay();
}));
$('bulkGrantBtn').onclick = () => bulk('grant');
$('bulkRevokeBtn').onclick = () => bulk('revoke');
$('refreshOrgWorks').onclick = () => selectedOrg && selectOrg(selectedOrg);
$('relaySearchBtn').onclick = loadRelay;
$('relayRefreshBtn').onclick = loadRelay;
$('relaySearch').addEventListener('keydown', event => { if (event.key === 'Enter') loadRelay(); });
$('refreshAdminAudit').onclick = loadAudit;
$('createOrgBtn').onclick = async () => {
  const name = $('newOrgName').value.trim();
  const slug = $('newOrgSlug').value.trim();
  if (!name || !slug) { toast('请填写组织名称和 slug'); return; }
  try {
    const org = await api('/api/orgs', { method: 'POST', body: JSON.stringify({ name, slug }) });
    $('newOrgName').value = ''; $('newOrgSlug').value = '';
    await loadOrgs(); await selectOrg(org.id); toast('组织已创建');
  } catch (error) { toast(error.message || '创建失败'); }
};

if (adminToken) showAdmin(); else showLogin();
