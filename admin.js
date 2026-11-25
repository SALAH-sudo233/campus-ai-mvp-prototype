const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const ADMIN_STORE_KEY = 'hust-campus-ai-admin-token-v1';
const SELECTED_ORG_KEY = 'hust-campus-ai-admin-selected-org-v1';
let adminToken = sessionStorage.getItem(ADMIN_STORE_KEY) || '';
let organizations = [];
let selectedOrg = sessionStorage.getItem(SELECTED_ORG_KEY) || null;
let memberRows = [];
let orgLabels = { admin: '教师', member: '学生' };
let selectSeq = 0;
let activeAdminTab = 'tokens';
let rewardPreview = null;
let rewardSummary = null;
let rewardExcluded = new Set();
let rewardPeriodDirty = false;

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

async function loadDemoGate() {
  try {
    const config = await fetch(API + '/api/config').then(resp => resp.json());
    $('adminQuickLoginBtn').classList.toggle('hidden', !config.demoLoginEnabled);
  } catch (error) {
    $('adminQuickLoginBtn').classList.add('hidden');
  }
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
  rewardExcluded = new Set();
  rewardPeriodDirty = false;
  const quotaText = org && org.quotaLimit ? (' · 总额度上限 ' + fmt(org.quotaLimit) + ' Token') : ' · 总额度不限';
  const label = org ? (org.name + ' · ' + org.slug + quotaText) : orgId;
  $('selectedOrgLabel').textContent = label;
  $('memberOrgHint').textContent = '当前组织：' + (org ? org.name : orgId) + ' · 支持录入学生/老师、CSV 批量导入与批量操作。';
  $('worksOrgHint').textContent = '当前组织：' + (org ? org.name : orgId) + ' · 平台管理员可全局下架。';
  $('rewardsOrgHint').textContent = '当前组织：' + (org ? org.name : orgId) + ' · 每两周评审一次 · 平台额外激励（不扣减组织原定额度）· 纯按有效点赞排序，单件作品不重复获奖。';

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
    const rewardSuffix = s.rewardTotal > 0 ? (' · 另含作品额外激励 +' + fmt(s.rewardTotal)) : '';
    const quotaHint = (s.totalQuota ? ('组织上限 ' + fmt(s.totalQuota) + ' · 人均 ' + fmt(s.memberQuota) + ' Token') : '未设总额度上限') + rewardSuffix;
    $('adminOrgStats').replaceChildren(
      UI.MetricCard({ label: '成员', value: s.memberCount }),
      UI.MetricCard({ label: '活跃', value: s.activeMemberCount, tone: 'accent' }),
      UI.MetricCard({ label: '总消耗', value: s.totalConsumed }),
      UI.MetricCard({ label: '剩余额度', value: s.remainingQuota, hint: quotaHint })
    );
    memberRows = members.members || [];
    renderMembers();
    renderWorks(works.items || []);
    if (activeAdminTab === 'tokens') await loadRewards();
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
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['', '姓名', '学号', '角色', '剩余额度', '已用额度', '密钥掩码', '准入', '操作'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  if (!memberRows.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '9', className: 'muted', text: '当前组织暂无成员，可点击右上角“录入成员”或“CSV 批量导入”。' })]));
  }
  for (const row of memberRows) {
    const checkbox = el('input', { type: 'checkbox', 'data-user': row.userId, 'aria-label': '选择 ' + row.name });
    const status = el('select', { className: 'compact-select', onchange: event => updateMember(row, { admissionStatus: event.target.value }) }, [
      ['active', '已准入'], ['disabled', '已停用'], ['pending_review', '待复核']
    ].map(([value, label]) => el('option', { value, selected: row.admissionStatus === value, text: label })));
    const quota = row.quota || {};
    const canReissue = ['pending', 'failed', 'revoking', 'none'].includes(row.keyStatus);
    const actions = [
      el('button', { className: 'danger', text: '吊销', onclick: () => revokeOne(row) })
    ];
    if (canReissue) actions.unshift(el('button', { className: 'outline', text: '重发密钥', onclick: () => reissueOne(row) }));
    body.appendChild(el('tr', {}, [
      el('td', {}, [checkbox]), el('td', { text: row.name || '—' }), el('td', { text: row.studentId || '—' }),
      el('td', {}, [UI.RoleBadge(row, orgLabels)]), el('td', { text: quota.remainingQuota == null ? '—' : fmt(quota.remainingQuota) }),
      el('td', { text: quota.usedQuota == null ? '—' : fmt(quota.usedQuota) }), el('td', {}, [el('code', { text: row.keyHint || '无有效密钥' })]),
      el('td', {}, [status]), el('td', {}, [
        el('div', { className: 'inline-actions' }, actions)
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

async function reissueOne(row) {
  if (!confirm('确认让服务端立即重发 ' + (row.name || row.studentId) + ' 的密钥？')) return;
  try {
    const result = await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/members/' + encodeURIComponent(row.userId) + '/reissue-key', { method: 'POST', body: '{}' });
    await selectOrg(selectedOrg);
    toast(result.statuses && result.statuses[0] === 'active' ? '密钥已补发' : '已触发重发，状态：' + (result.statuses || []).join(', '));
  } catch (error) { toast(error.message || '重发失败'); }
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

function openAdminWork(item) {
  if (item.hostedUrl) { window.open(API + '/' + item.hostedUrl, '_blank', 'noopener,noreferrer'); return; }
  if (item.externalUrl) { window.open(item.externalUrl, '_blank', 'noopener,noreferrer'); return; }
  toast(item.title + '：可前往作品广场查看详情或下载安装包。');
}

function renderWorks(items) {
  const wrap = $('adminWorks');
  wrap.replaceChildren();
  if (!items.length) { wrap.appendChild(el('p', { className: 'muted empty', text: '该组织暂无作品。' })); return; }
  for (const work of items) {
    const actions = work.status === 'published' || work.status === 'pending_review'
      ? [{ label: '全局下架', tone: 'danger', onClick: () => globalUnpublish(work) }] : [];
    wrap.appendChild(UI.WorkCard(work, { density: 'compact', showStatus: true, actions, onOpen: openAdminWork }));
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

// Create organization modal
async function createOrg() {
  const name = $('newOrgName').value.trim();
  const slug = $('newOrgSlug').value.trim().toLowerCase();
  const quotaText = $('newOrgQuotaLimit').value.trim();
  if (!name || !slug) { toast('请填写组织名称和 slug'); return; }
  const payload = { name, slug };
  if (quotaText !== '') {
    const num = Number(quotaText);
    if (!Number.isInteger(num) || num <= 0) { toast('总额度上限必须是正整数'); return; }
    payload.quotaLimit = num;
  }
  try {
    const org = await api('/api/orgs', { method: 'POST', body: JSON.stringify(payload) });
    $('newOrgName').value = '';
    $('newOrgSlug').value = '';
    $('newOrgQuotaLimit').value = '1000000';
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

// Work rewards settlement tab
function switchAdminTab(tab) {
  activeAdminTab = tab;
  document.querySelectorAll('[data-admin-tab]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.adminTab === tab);
  });
  document.querySelectorAll('.admin-tab').forEach(section => {
    section.classList.toggle('active', section.id === 'tab-' + tab);
  });
  if (tab === 'tokens' && selectedOrg) loadRewards();
}

function readRewardSlots() {
  return {
    tier_10x: Math.max(0, Number($('tierSlot10x').value || 0)),
    tier_5x: Math.max(0, Number($('tierSlot5x').value || 0)),
    tier_1x: Math.max(0, Number($('tierSlot1x').value || 0))
  };
}

function buildRewardPreviewQuery() {
  const slots = readRewardSlots();
  const params = new URLSearchParams();
  params.set('tier_10x', String(slots.tier_10x));
  params.set('tier_5x', String(slots.tier_5x));
  params.set('tier_1x', String(slots.tier_1x));
  for (const wid of rewardExcluded) params.append('exclude', wid);
  return '?' + params.toString();
}

async function loadRewards() {
  if (!selectedOrg) return;
  try {
    const [summary, preview] = await Promise.all([
      api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/rewards'),
      api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/rewards/preview' + buildRewardPreviewQuery())
    ]);
    rewardSummary = summary;
    rewardPreview = preview;
    renderRewards();
  } catch (error) {
    toast(error.message || '作品奖励数据加载失败');
  }
}

function renderRewards() {
  const summary = rewardSummary;
  const preview = rewardPreview;
  if (!summary || !preview) return;

  $('rewardOverviewStats').replaceChildren(
    UI.MetricCard({ label: '累计发放激励', value: summary.totalAwarded, unit: ' Token', tone: 'reward', hint: '平台额外激励，不占组织额度' }),
    UI.MetricCard({ label: '已获奖作品', value: summary.rewardedWorkCount, hint: '单件作品终身不重复获奖' }),
    UI.MetricCard({ label: '已完成期数', value: summary.settlementCount, hint: '按组织统计的历史结算批次' }),
    UI.MetricCard({ label: '本期有效候选', value: preview.candidates.length, tone: 'accent', hint: '已发布学生作品且有效赞 ≥ 1' })
  );

  $('rewardBaseHint').textContent = '基准初始额度 ' + fmt(preview.baseQuota) + ' Token · 按 10× / 5× / 1× 三档发放（默认 1 / 5 / 10 名），平台额外出资不扣组织额度。';
  if (!rewardPeriodDirty || !$('rewardPeriodInput').value.trim()) {
    $('rewardPeriodInput').value = preview.suggestedPeriod || '2026 第 1 期';
  }
  const tierMap = {};
  for (const t of preview.tiers || []) tierMap[t.code] = t;
  $('tierAmount10x').textContent = '单人 +' + fmt(tierMap.tier_10x && tierMap.tier_10x.amount) + ' Token';
  $('tierAmount5x').textContent = '单人 +' + fmt(tierMap.tier_5x && tierMap.tier_5x.amount) + ' Token';
  $('tierAmount1x').textContent = '单人 +' + fmt(tierMap.tier_1x && tierMap.tier_1x.amount) + ' Token';

  const settleBtn = $('settleRewardsBtn');
  settleBtn.disabled = preview.winnerCount <= 0;
  $('rewardPreviewSummary').replaceChildren(
    el('span', { text: '本期可发奖 ' }),
    el('strong', { text: String(preview.winnerCount) + ' 件作品' }),
    el('span', { text: '（总名额 ' + preview.totalSlots + '），预计发放平台额外激励合计 ' }),
    el('strong', { className: 'reward-highlight-num', text: '+' + fmt(preview.estimatedTotal) + ' Token' }),
    el('span', { className: 'muted', text: ' · 排除自赞与演示示例作品' })
  );

  const table = $('rewardCandidatesTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['纳入', '拟定奖项', '作品', '作者 / 学号', '有效点赞（总赞）', '拟发 Token'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  if (!preview.candidates.length) {
    body.appendChild(el('tr', {}, [el('td', { colspan: '6', className: 'muted', text: '当前暂无符合条件的新候选作品（需为已发布学生作品、未曾获奖且有非自赞有效点赞）。' })]));
  } else {
    for (const item of preview.candidates) {
      const checked = !item.excluded;
      const checkbox = el('input', {
        type: 'checkbox',
        checked,
        'aria-label': '纳入作品 ' + item.title,
        onchange: () => {
          if (checkbox.checked) rewardExcluded.delete(item.workId);
          else rewardExcluded.add(item.workId);
          loadRewards();
        }
      });
      let awardBadge;
      if (item.excluded) {
        awardBadge = el('span', { className: 'badge none', text: '已手动排除' });
      } else if (item.tierCode) {
        awardBadge = el('span', { className: 'work-badge reward', text: '#' + item.rank + ' ' + item.tierLabel + '（' + item.multiplier + '×）' });
      } else {
        awardBadge = el('span', { className: 'muted', text: '未进入本期名额' });
      }
      body.appendChild(el('tr', { className: item.excluded ? 'row-muted' : '' }, [
        el('td', {}, [checkbox]),
        el('td', {}, [awardBadge]),
        el('td', {}, [el('strong', { text: item.title }), el('div', { className: 'muted', text: item.category })]),
        el('td', { text: item.authorName + (item.studentId ? (' · ' + item.studentId) : '') }),
        el('td', { text: fmt(item.validUpvotes) + '（总 ' + fmt(item.totalUpvotes) + '）' }),
        el('td', { className: item.amount > 0 ? 'amount plus' : 'muted', text: item.amount > 0 ? ('+' + fmt(item.amount) + ' Token') : '—' })
      ]));
    }
  }
  table.appendChild(body);

  const historyWrap = $('rewardHistoryList');
  historyWrap.replaceChildren();
  const settlements = summary.settlements || [];
  if (!settlements.length) {
    historyWrap.appendChild(el('p', { className: 'muted empty', text: '该组织尚未完成过作品奖励结算。' }));
    return;
  }
  for (const s of settlements) {
    const winnersTable = el('table', { className: 'data-table compact-table' }, [
      el('thead', {}, [el('tr', {}, ['名次', '奖项', '作品', '作者 / 学号', '有效点赞', '发放额度'].map(l => el('th', { text: l }))) ]),
      el('tbody', {}, (s.winners || []).map(w => el('tr', {}, [
        el('td', { text: '#' + w.rank }),
        el('td', {}, [el('span', { className: 'work-badge reward', text: w.tierLabel + '（' + w.multiplier + '×）' })]),
        el('td', { text: w.title }),
        el('td', { text: w.authorName + (w.studentId ? (' · ' + w.studentId) : '') }),
        el('td', { text: fmt(w.validUpvotes) }),
        el('td', { className: 'amount plus', text: '+' + fmt(w.amount) + ' Token' })
      ])))
    ]);
    historyWrap.appendChild(el('article', { className: 'reward-history-card' }, [
      el('div', { className: 'reward-history-head' }, [
        el('div', {}, [
          el('strong', { text: s.periodLabel }),
          el('span', { className: 'muted', text: ' · ' + new Date(s.createdAt * 1000).toLocaleString('zh-CN') + ' · 基准 ' + fmt(s.baseQuota) + ' Token' })
        ]),
        el('div', { className: 'reward-history-total' }, [
          el('span', { className: 'badge active', text: s.winnerCount + ' 件获奖' }),
          el('strong', { className: 'amount plus', text: '+' + fmt(s.totalAwarded) + ' Token' })
        ])
      ]),
      el('div', { className: 'table-scroll' }, [winnersTable])
    ]));
  }
}

async function settleRewards() {
  if (!selectedOrg || !rewardPreview) return;
  const periodLabel = ($('rewardPeriodInput').value || '').trim();
  if (!periodLabel) { toast('请填写结算期次名称'); return; }
  if (!rewardPreview.winnerCount) { toast('当前没有可发奖的候选作品'); return; }
  const msg = '确认结算「' + periodLabel + '」？\n将为 ' + rewardPreview.winnerCount + ' 件作品发放平台额外激励共 ' +
    fmt(rewardPreview.estimatedTotal) + ' Token（入账到作者账本且作品终身不可重复获奖）。';
  if (!confirm(msg)) return;
  try {
    const result = await api('/api/orgs/' + encodeURIComponent(selectedOrg) + '/rewards/settle', {
      method: 'POST',
      body: JSON.stringify({
        periodLabel,
        slots: readRewardSlots(),
        excludedWorkIds: Array.from(rewardExcluded),
        requestId: 'admin-settle-' + selectedOrg + '-' + Date.now()
      })
    });
    rewardExcluded = new Set();
    rewardPeriodDirty = false;
    await selectOrg(selectedOrg);
    await loadRewards();
    toast('已结算「' + result.periodLabel + '」：' + result.winnerCount + ' 件获奖，共发放 +' + fmt(result.totalAwarded) + ' Token');
  } catch (error) {
    toast(error.message || '结算失败');
  }
}

// Event bindings
document.querySelectorAll('[data-admin-tab]').forEach(btn => btn.addEventListener('click', () => switchAdminTab(btn.dataset.adminTab)));
$('refreshRewardPreview').onclick = () => selectedOrg && selectOrg(selectedOrg);
$('settleRewardsBtn').onclick = UI.guard(settleRewards);
$('rewardPeriodInput').addEventListener('input', () => { rewardPeriodDirty = true; });
['tierSlot10x', 'tierSlot5x', 'tierSlot1x'].forEach(id => $(id).addEventListener('change', loadRewards));
$('adminLoginBtn').addEventListener('click', login);
$('adminQuickLoginBtn').addEventListener('click', quickLogin);
$('adminPassword').addEventListener('keydown', event => { if (event.key === 'Enter') login(); });
$('adminLogoutBtn').addEventListener('click', showLogin);
$('orgSwitcher').addEventListener('change', event => selectOrg(event.target.value));
$('openCreateOrgBtn').onclick = () => { openModal('createOrgModal'); $('newOrgName').focus(); };
$('createOrgBtn').onclick = createOrg;
$('newOrgSlug').addEventListener('keydown', event => { if (event.key === 'Enter') createOrg(); });
$('newOrgQuotaLimit').addEventListener('keydown', event => { if (event.key === 'Enter') createOrg(); });

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

loadDemoGate();
if (adminToken) showAdmin(); else showLogin();
