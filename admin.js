const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const ADMIN_STORE_KEY = 'hust-campus-ai-admin-token-v1';
const SELECTED_ORG_KEY = 'hust-campus-ai-admin-selected-org-v1';
let adminToken = sessionStorage.getItem(ADMIN_STORE_KEY) || '';
let organizations = [];
let selectedOrg = sessionStorage.getItem(SELECTED_ORG_KEY) || null;
let memberRows = [];
let orgLabels = { admin: '教师', member: '学生' };
let selectSeq = 0;

const $ = id => document.getElementById(id);
const el = UI.el;
const fmt = n => Number(n || 0).toLocaleString('zh-CN');

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

async function api(path, options, raw) {
  const headers = Object.assign({}, (options && options.headers) || {});
  if (!raw && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  if (adminToken) headers.Authorization = 'Bearer ' + adminToken;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (!resp.ok) {
    let data = {};
    try { data = await resp.json(); } catch (error) { data = {}; }
    throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  }
  return raw ? resp : resp.json();
}

function currentOrg() {
  return organizations.find(item => item.id === selectedOrg) || null;
}

function openModal(id) {
  $(id).classList.remove('hidden');
}
function closeModal(id) {
  $(id).classList.add('hidden');
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

async function quickLogin() {
  try {
    const data = await api('/api/demo/login', { method: 'POST', body: JSON.stringify({ role: 'admin' }) });
    adminToken = data.token;
    sessionStorage.setItem(ADMIN_STORE_KEY, adminToken);
    await showAdmin();
  } catch (error) { toast(error.message || '快捷登录失败'); }
}

function renderOrgControls() {
  const switcher = $('orgSwitcher');
  switcher.replaceChildren();
  for (const org of organizations) {
    switcher.appendChild(el('option', { value: org.id, selected: selectedOrg === org.id, text: org.name + '（' + org.memberCount + '人）' }));
  }
  if (selectedOrg) switcher.value = selectedOrg;
}

async function loadOrgs(preferredOrgId) {
  try {
    const data = await api('/api/orgs');
    organizations = data.organizations || [];
    const target = preferredOrgId || selectedOrg;
    const valid = target && organizations.some(item => item.id === target);
    selectedOrg = valid ? target : (organizations[0] ? organizations[0].id : null);
    if (selectedOrg) sessionStorage.setItem(SELECTED_ORG_KEY, selectedOrg);
    renderOrgControls();
    if (selectedOrg) {
      await selectOrg(selectedOrg);
    } else {
      await Promise.all([loadRelay(), loadAudit()]);
    }
  } catch (error) {
    if (error.status === 401) showLogin(); else toast(error.message);
  }
}

async function selectOrg(orgId) {
  if (!orgId) return;
  const seq = ++selectSeq;
  selectedOrg = orgId;
  sessionStorage.setItem(SELECTED_ORG_KEY, orgId);
  if ($('orgSwitcher').value !== orgId) $('orgSwitcher').value = orgId;

  const org = currentOrg();
  if (org) orgLabels = { admin: org.adminLabel || '教师', member: org.memberLabel || '学生' };
  const label = org ? (org.name + ' · ' + org.slug) : orgId;
  $('selectedOrgLabel').textContent = label;
  $('memberOrgHint').textContent = '当前组织：' + (org ? org.name : orgId) + ' · 支持录入学生/老师、CSV 批量导入与批量操作。';
  $('worksOrgHint').textContent = '当前组织：' + (org ? org.name : orgId) + ' · 平台管理员可全局下架。';

  const memberQuery = ($('adminMemberSearch').value || '').trim();
  const memberSuffix = memberQuery ? '?q=' + encodeURIComponent(memberQuery) : '';
  try {
    const [dashboard, members, works] = await Promise.all([
      api('/api/orgs/' + encodeURIComponent(orgId) + '/dashboard?days=90'),
      api('/api/orgs/' + encodeURIComponent(orgId) + '/members' + memberSuffix),
      api('/api/orgs/' + encodeURIComponent(orgId) + '/works')
    ]);
    if (seq !== selectSeq) return;
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
    await Promise.all([loadRelay(), loadAudit()]);
  } catch (error) {
    if (seq === selectSeq) toast(error.message || '组织数据暂时不可用');
  }
}

async function refreshOrgMembers() {
  if (!selectedOrg) return;
  const q = ($('adminMemberSearch').value || '').trim();
  const suffix = q ? '?q=' + encodeURIComponent(q) : '';
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members' + suffix);
    memberRows = data.members || [];
    renderMembers();
  } catch (error) { toast(error.message || '成员列表暂时不可用'); }
}

function renderMembers() {
  const table = $('adminMemberTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['', '姓名', '学号', '角色', '余额', '消耗', '密钥掩码', '准入', '操作'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  if (!memberRows.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '9', className: 'muted', text: '当前组织暂无成员，可点击右上角“录入成员”或“CSV 批量导入”。' })]));
  }
  for (const row of memberRows) {
    const checkbox = el('input', { type: 'checkbox', 'data-user': row.userId, 'aria-label': '选择 ' + row.name });
    const status = el('select', { className: 'compact-select', onchange: event => updateMember(row, { admissionStatus: event.target.value }) }, [
      ['active', '已准入'], ['disabled', '已停用'], ['pending_review', '待复核']
    ].map(([value, label]) => el('option', { value, selected: row.admissionStatus === value, text: label })));
    const isAdmin = row.role === 'org_admin';
    body.appendChild(el('tr', {}, [
      el('td', {}, [checkbox]), el('td', { text: row.name || '—' }), el('td', { text: row.studentId || '—' }),
      el('td', {}, [UI.RoleBadge(row, orgLabels)]), el('td', { text: fmt(row.balance) }),
      el('td', { text: fmt(row.spent) }), el('td', {}, [el('code', { text: row.keyHint || '无有效密钥' })]),
      el('td', {}, [status]), el('td', {}, [
        el('div', { className: 'inline-actions' }, [
          el('button', {
            className: 'ghost',
            text: isAdmin ? '设为学生' : '设为教师',
            onclick: () => updateMember(row, { role: isAdmin ? 'org_member' : 'org_admin' })
          }),
          el('button', { className: 'danger', text: '吊销', onclick: () => revokeOne(row) })
        ])
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
    await loadOrgs(selectedOrg);
    toast('成员信息已更新');
  } catch (error) { toast(error.message || '更新失败'); }
}

async function revokeOne(row) {
  if (!confirm('确认吊销 ' + (row.name || row.studentId) + ' 的密钥？')) return;
  try {
    await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members/' + encodeURIComponent(row.userId) + '/revoke-key', { method: 'POST', body: '{}' });
    await selectOrg(selectedOrg);
    toast('已吊销');
  } catch (error) { toast(error.message || '操作失败'); }
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
  if (!items.length) { wrap.appendChild(el('p', { className: 'muted empty', text: '该组织暂无作品。' })); return; }
  for (const work of items) {
    const actions = work.status === 'published' || work.status === 'pending_review'
      ? [{ label: '全局下架', tone: 'danger', onClick: () => globalUnpublish(work) }] : [];
    wrap.appendChild(UI.WorkCard(work, { density: 'compact', showStatus: true, actions,
      onOpen: item => toast(item.title + '：演示作品仅展示卡片与预览说明。') }));
  }
}

async function globalUnpublish(work) {
  if (!confirm('确认全局下架《' + work.title + '》？')) return;
  try {
    await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/works/' + encodeURIComponent(work.workId) + '/unpublish', { method: 'POST', body: '{}' });
    await selectOrg(selectedOrg);
    toast('已全局下架');
  } catch (error) { toast(error.message || '下架失败'); }
}

async function loadRelay() {
  const query = $('relaySearch').value.trim();
  const params = new URLSearchParams();
  if (query) params.set('q', query);
  if ($('relayOrgFilter').value === 'selected' && selectedOrg) params.set('orgId', selectedOrg);
  const qs = params.toString() ? '?' + params.toString() : '';
  try {
    const data = await api('/api/admin/accounts' + qs);
    const table = $('relayTable');
    table.replaceChildren(el('thead', {}, [el('tr', {}, ['学生编号', '身份', '姓名', '组织', '密钥掩码', '状态', '操作'].map(label => el('th', { text: label })))]));
    const body = el('tbody');
    if (!data.accounts.length) {
      body.appendChild(el('tr', {}, [el('td', { colspan: '7', className: 'muted', text: '当前条件下暂无台账记录' })]));
    }
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
  try {
    await api('/api/admin/revoke', { method: 'POST', body: JSON.stringify({ owner_ref: account.owner_ref }) });
    await loadRelay();
    toast('已吊销');
  } catch (error) { toast(error.message || '吊销失败'); }
}

async function loadAudit() {
  const qs = $('auditOrgFilter').value === 'selected' && selectedOrg ? '?orgId=' + encodeURIComponent(selectedOrg) : '';
  try {
    const data = await api('/api/audit-events' + qs);
    const table = $('adminAuditTable');
    table.replaceChildren(el('thead', {}, [el('tr', {}, ['时间', '组织', '角色', '动作', '资源', '请求编号'].map(label => el('th', { text: label })))]));
    const body = el('tbody');
    if (!data.events.length) {
      body.appendChild(el('tr', {}, [el('td', { colspan: '6', className: 'muted', text: '当前条件下暂无审计记录' })]));
    }
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

// Create organization modal
async function createOrg() {
  const name = $('newOrgName').value.trim();
  const slug = $('newOrgSlug').value.trim().toLowerCase();
  if (!name || !slug) { toast('请填写组织名称和 slug'); return; }
  try {
    const org = await api('/api/orgs', { method: 'POST', body: JSON.stringify({ name, slug }) });
    $('newOrgName').value = '';
    $('newOrgSlug').value = '';
    closeModal('createOrgModal');
    await loadOrgs(org.id);
    toast('组织「' + org.name + '」已创建并切换');
  } catch (error) { toast(error.message || '创建失败'); }
}

// Add single student / teacher modal
async function addMember() {
  if (!selectedOrg) { toast('请先选择组织'); return; }
  const name = $('memberName').value.trim();
  const studentId = $('memberStudentId').value.trim();
  const email = $('memberEmail').value.trim();
  const role = $('memberRole').value;
  const admissionStatus = $('memberAdmission').value;
  if (!name) { toast('请填写姓名'); return; }
  if (!studentId && !email) { toast('学号/工号与校园邮箱至少填写一项'); return; }
  try {
    await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members', {
      method: 'POST',
      body: JSON.stringify({ name, studentId, email, role, admissionStatus })
    });
    $('memberName').value = '';
    $('memberStudentId').value = '';
    $('memberEmail').value = '';
    closeModal('addMemberModal');
    await loadOrgs(selectedOrg);
    toast((role === 'org_admin' ? '教师' : '学生') + '「' + name + '」已录入');
  } catch (error) { toast(error.message || '录入失败'); }
}

// CSV batch import modal
function setImportStatus(text, tone) {
  const node = $('adminImportStatus');
  node.textContent = text;
  node.className = 'import-status ' + (tone || '');
}

async function downloadFile(path, filename) {
  try {
    const resp = await api(path, { method: 'GET' }, true);
    const blob = await resp.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  } catch (error) { toast(error.message || '下载失败'); }
}

async function previewCsv() {
  if (!selectedOrg) { toast('请先选择组织'); return; }
  const file = $('adminCsvFile').files[0];
  if (!file) { toast('请先选择 CSV 文件'); return; }
  if (file.size > 10 * 1024 * 1024) { toast('文件超过 10 MB 上限'); return; }
  setImportStatus('正在解析……');
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/imports/preview', {
      method: 'POST', body: file, headers: { 'Content-Type': 'text/csv; charset=utf-8' }
    }, true).then(resp => resp.json());
    $('adminPreviewArea').classList.remove('hidden');
    $('adminCommitArea').classList.add('hidden');
    $('adminPreviewContent').replaceChildren(UI.ImportPreview(data, {
      onConfirm: commitCsv,
      onDownload: () => downloadFile('/api/orgs/' + encodeURIComponent(selectedOrg) + '/imports/' + encodeURIComponent(data.batchId) + '/errors.csv', 'import-errors.csv'),
      onCancel: () => { $('adminPreviewArea').classList.add('hidden'); setImportStatus('已取消预览'); }
    }));
    setImportStatus('可写入 ' + data.counts.accepted + ' 行，拒绝 ' + data.counts.rejected + ' 行');
  } catch (error) {
    setImportStatus(error.message || '解析失败', 'error');
    toast(error.message || '解析失败');
  }
}

async function commitCsv(data) {
  setImportStatus('正在确认导入……');
  try {
    const result = await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/imports/' + encodeURIComponent(data.batchId) + '/commit', {
      method: 'POST', body: JSON.stringify({ requestId: 'admin-import-' + data.batchId })
    });
    $('adminCommitArea').classList.remove('hidden');
    const c = result.counts || {};
    $('adminCommitContent').replaceChildren(el('p', {
      text: '写入 ' + (c.accepted || 0) + ' 行，跳过 ' + (c.skipped || 0) + ' 行，冲突 ' + (c.conflicts || 0) + ' 行，拒绝 ' + (c.rejected || 0) + ' 行。'
    }));
    setImportStatus('导入已确认', 'success');
    await loadOrgs(selectedOrg);
    toast('名单导入已完成');
  } catch (error) {
    setImportStatus(error.message || '确认失败', 'error');
    toast(error.message || '确认失败');
  }
}

// Event bindings
$('adminLoginBtn').addEventListener('click', login);
$('adminQuickLoginBtn').addEventListener('click', quickLogin);
$('adminPassword').addEventListener('keydown', event => { if (event.key === 'Enter') login(); });
$('adminLogoutBtn').addEventListener('click', showLogin);
$('orgSwitcher').addEventListener('change', event => selectOrg(event.target.value));
document.querySelectorAll('[data-admin-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-admin-tab]').forEach(node => node.classList.toggle('active', node === button));
  document.querySelectorAll('.admin-tab').forEach(node => node.classList.toggle('active', node.id === 'tab-' + button.dataset.adminTab));
  if (button.dataset.adminTab === 'audit') loadAudit();
  if (button.dataset.adminTab === 'relay') loadRelay();
}));

$('openCreateOrgBtn').onclick = () => { openModal('createOrgModal'); $('newOrgName').focus(); };
$('createOrgBtn').onclick = createOrg;
$('newOrgSlug').addEventListener('keydown', event => { if (event.key === 'Enter') createOrg(); });

$('openAddMemberBtn').onclick = () => {
  if (!selectedOrg) { toast('请先选择组织'); return; }
  const org = currentOrg();
  $('addMemberOrgLabel').textContent = '所属组织：' + (org ? org.name : selectedOrg);
  openModal('addMemberModal');
  $('memberName').focus();
};
$('addMemberConfirmBtn').onclick = addMember;
$('memberEmail').addEventListener('keydown', event => { if (event.key === 'Enter') addMember(); });

$('openImportModalBtn').onclick = () => {
  if (!selectedOrg) { toast('请先选择组织'); return; }
  const org = currentOrg();
  $('importModalOrgLabel').textContent = '所属组织：' + (org ? org.name : selectedOrg);
  $('adminCsvFile').value = '';
  $('adminPreviewArea').classList.add('hidden');
  $('adminCommitArea').classList.add('hidden');
  setImportStatus('未选择文件');
  openModal('importMembersModal');
};
$('adminPreviewBtn').onclick = previewCsv;
$('adminDownloadTemplate').onclick = () => selectedOrg && downloadFile('/api/orgs/' + encodeURIComponent(selectedOrg) + '/imports/template', 'campus-roster-template.csv');

document.querySelectorAll('[data-close-modal]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.closeModal)));
document.querySelectorAll('.modal').forEach(modal => modal.addEventListener('click', event => {
  if (event.target === modal) modal.classList.add('hidden');
}));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('.modal:not(.hidden)').forEach(m => m.classList.add('hidden'));
});

$('adminMemberSearchBtn').onclick = refreshOrgMembers;
$('adminMemberSearch').addEventListener('keydown', event => { if (event.key === 'Enter') refreshOrgMembers(); });
$('bulkGrantBtn').onclick = () => bulk('grant');
$('bulkRevokeBtn').onclick = () => bulk('revoke');
$('refreshOrgWorks').onclick = () => selectedOrg && selectOrg(selectedOrg);
$('relayOrgFilter').onchange = loadRelay;
$('relaySearchBtn').onclick = loadRelay;
$('relayRefreshBtn').onclick = loadRelay;
$('relaySearch').addEventListener('keydown', event => { if (event.key === 'Enter') loadRelay(); });
$('auditOrgFilter').onchange = loadAudit;
$('refreshAdminAudit').onclick = loadAudit;

if (adminToken) showAdmin(); else showLogin();
