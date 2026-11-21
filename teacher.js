const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
const token = localStorage.getItem(STORE_KEY) || '';
const $ = id => document.getElementById(id);
const el = UI.el;
let me = null;
let orgId = null;

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}
const fmt = n => Number(n || 0).toLocaleString('zh-CN');

async function api(path, options) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, (options && options.headers) || {});
  if (token) headers.Authorization = 'Bearer ' + token;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  let data = {};
  try { data = await resp.json(); } catch (error) { data = {}; }
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status });
  return data;
}

function denied() {
  $('teacherMain').classList.add('hidden');
  $('teacherDenied').classList.remove('hidden');
}

async function boot() {
  fetch(API + '/api/config').then(resp => resp.json()).then(config => {
    for (const node of document.querySelectorAll('[data-product-name]')) node.textContent = config.productName;
    document.title = config.productName + ' · 教师端';
  }).catch(() => {});
  if (!token) { denied(); return; }
  try {
    me = await api('/api/me');
    if (me.role !== 'org_admin' && me.role !== 'platform_admin') { denied(); return; }
    orgId = me.org && me.org.id;
    if (!orgId) { denied(); return; }
    $('teacherMain').classList.remove('hidden');
    $('teacherOrg').textContent = me.org.name;
    $('orgTitle').textContent = me.org.name;
    $('orgSubtitle').textContent = ((me.labels && me.labels.admin) || '教师') + '视角 · 本组织成员、用量与精选作品';
    $('importLink').href = 'import.html?org=' + encodeURIComponent(orgId);
    $('importBtn').href = $('importLink').href;
    await Promise.all([loadDashboard(), loadMembers(), loadWorks(), loadAudit()]);
  } catch (error) {
    if (error.status === 401 || error.status === 403) denied(); else toast(error.message);
  }
}

async function loadDashboard() {
  $('teacherError').classList.add('hidden');
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(orgId) + '/dashboard?days=90');
    const summary = data.summary;
    $('teacherStats').replaceChildren(
      UI.MetricCard({ label: '成员数', value: summary.memberCount, hint: '已准入成员' }),
      UI.MetricCard({ label: '活跃成员', value: summary.activeMemberCount, hint: '近 30 天有使用', tone: 'accent' }),
      UI.MetricCard({ label: '总消耗', value: summary.totalConsumed, unit: ' Token', hint: '现有流水累计' }),
      UI.MetricCard({ label: '剩余额度', value: summary.remainingQuota, unit: ' Token', hint: '本组织成员余额' })
    );
    $('trendChart').replaceChildren(UI.TrendChart(data.trend));
    $('heatmap').replaceChildren(UI.Heatmap(data.heatmap));
    renderActivity(data.activity);
    renderRanking(data.ranking);
    const featured = data.featuredWorks || [];
    $('featuredWorks').replaceChildren();
    if (!featured.length) $('featuredWorks').appendChild(el('p', { className: 'muted empty', text: '还没有精选作品。' }));
    for (const work of featured) $('featuredWorks').appendChild(UI.WorkCard(work, { density: 'compact', onOpen: openWork }));
  } catch (error) {
    $('teacherError').classList.remove('hidden');
    $('teacherError').textContent = '数据暂不可用（近 90 天）。' + (error.message || '请稍后重试。');
    $('teacherStats').replaceChildren();
    $('trendChart').replaceChildren();
    $('heatmap').replaceChildren();
  }
}

function renderActivity(activity) {
  const wrap = el('div', { className: 'activity-bars' });
  const max = Math.max(1, ...activity.buckets.map(item => item.count));
  for (const bucket of activity.buckets) {
    wrap.appendChild(el('div', { className: 'activity-row' }, [
      el('span', { text: bucket.label }),
      el('div', { className: 'bar-track' }, [el('i', { className: 'bar ' + bucket.tone, style: 'width:' + (bucket.count / max * 100) + '%' })]),
      el('b', { text: String(bucket.count) })
    ]));
  }
  $('activityChart').replaceChildren(wrap);
}

function renderRanking(rows) {
  const table = $('rankingTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['姓名', '学号', '消耗'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  if (!rows.length) body.appendChild(el('tr', {}, [el('td', { colspan: '3', className: 'muted', text: '暂无消耗记录' })]));
  rows.forEach((row, index) => body.appendChild(el('tr', {}, [
    el('td', {}, [el('span', { className: 'rank', text: String(index + 1) }), row.name]),
    el('td', { text: row.studentId || '—' }),
    el('td', { text: fmt(row.consumed) + ' Token' })
  ])));
  table.appendChild(body);
}

async function loadMembers(query) {
  const suffix = query ? '?q=' + encodeURIComponent(query) : '';
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(orgId) + '/members' + suffix);
    renderMembers(data.members);
  } catch (error) { toast(error.message || '成员列表暂时不可用'); }
}

function renderMembers(rows) {
  const table = $('memberTable');
  table.replaceChildren(el('thead', {}, [el('tr', {}, ['姓名', '学号', '角色', '余额', '消耗', '密钥掩码', '准入', '操作'].map(label => el('th', { text: label })))]));
  const body = el('tbody');
  for (const row of rows) {
    const select = el('select', { className: 'compact-select', 'aria-label': '调整 ' + row.name + ' 的准入状态', onchange: event => changeStatus(row, event.target.value) }, [
      ['active', '已准入'], ['disabled', '已停用'], ['pending_review', '待复核']
    ].map(([value, label]) => el('option', { value, selected: row.admissionStatus === value, text: label })));
    body.appendChild(el('tr', {}, [
      el('td', { text: row.name || '—' }),
      el('td', { text: row.studentId || '—' }),
      el('td', {}, [UI.RoleBadge(row, me.labels)]),
      el('td', { text: fmt(row.balance) }),
      el('td', { text: fmt(row.spent) }),
      el('td', {}, [el('code', { text: row.keyHint || '无有效密钥' })]),
      el('td', {}, [select]),
      el('td', {}, [el('button', { className: 'danger', text: '吊销密钥', onclick: () => revokeKey(row) })])
    ]));
  }
  table.appendChild(body);
}

async function changeStatus(row, status) {
  try {
    await api('/api/orgs/' + encodeURIComponent(orgId) + '/members/' + encodeURIComponent(row.userId), {
      method: 'PATCH', body: JSON.stringify({ admissionStatus: status })
    });
    toast('准入状态已更新');
  } catch (error) { toast(error.message || '更新失败'); await loadMembers(); }
}

async function revokeKey(row) {
  if (!confirm('确认吊销 ' + (row.name || row.studentId) + ' 的当前密钥？')) return;
  try {
    await api('/api/orgs/' + encodeURIComponent(orgId) + '/members/' + encodeURIComponent(row.userId) + '/revoke-key', { method: 'POST', body: '{}' });
    await loadMembers(); toast('密钥已吊销');
  } catch (error) { toast(error.message || '吊销失败'); }
}

async function loadWorks() {
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(orgId) + '/works');
    const wrap = $('pendingWorks');
    wrap.replaceChildren();
    const items = data.items || [];
    if (!items.length) wrap.appendChild(el('p', { className: 'muted empty', text: '暂无待审核或已发布作品。' }));
    for (const work of items) {
      const actions = [];
      if (work.status === 'pending_review') {
        actions.push({ label: '通过', tone: 'primary', onClick: () => reviewWork(work, 'approve') });
        actions.push({ label: '驳回', tone: 'danger', onClick: () => reviewWork(work, 'reject') });
      }
      if (work.status === 'published') {
        actions.push({ label: work.featured ? '取消精选' : '精选', tone: 'outline', onClick: () => featureWork(work, !work.featured) });
        actions.push({ label: '下架', tone: 'danger', onClick: () => unpublishWork(work) });
      }
      wrap.appendChild(UI.WorkCard(work, { density: 'compact', showStatus: true, onOpen: openWork, actions }));
    }
  } catch (error) { toast(error.message || '作品列表暂时不可用'); }
}

async function reviewWork(work, decision) {
  try {
    await api('/api/orgs/' + encodeURIComponent(orgId) + '/works/' + encodeURIComponent(work.workId) + '/review', {
      method: 'POST', body: JSON.stringify({ decision })
    });
    await Promise.all([loadWorks(), loadDashboard()]); toast(decision === 'approve' ? '作品已通过审核' : '作品已驳回');
  } catch (error) { toast(error.message || '审核失败'); }
}
async function featureWork(work, featured) {
  try {
    await api('/api/orgs/' + encodeURIComponent(orgId) + '/works/' + encodeURIComponent(work.workId) + '/feature', {
      method: 'POST', body: JSON.stringify({ featured })
    });
    await Promise.all([loadWorks(), loadDashboard()]); toast(featured ? '已加入精选' : '已取消精选');
  } catch (error) { toast(error.message || '操作失败'); }
}
async function unpublishWork(work) {
  if (!confirm('确认下架《' + work.title + '》？')) return;
  try {
    await api('/api/orgs/' + encodeURIComponent(orgId) + '/works/' + encodeURIComponent(work.workId) + '/unpublish', { method: 'POST', body: '{}' });
    await Promise.all([loadWorks(), loadDashboard()]); toast('作品已下架');
  } catch (error) { toast(error.message || '下架失败'); }
}

function openWork(work) {
  toast(work.title + '：这是作品卡预览，演示作品不会打开外部脚本。');
}

async function loadAudit() {
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(orgId) + '/audit');
    const table = $('auditTable');
    table.replaceChildren(el('thead', {}, [el('tr', {}, ['时间', '动作', '资源', '角色'].map(label => el('th', { text: label })))]));
    const body = el('tbody');
    for (const event of data.events) {
      body.appendChild(el('tr', {}, [
        el('td', { text: new Date(event.createdAt * 1000).toLocaleString('zh-CN') }),
        el('td', { text: event.action }), el('td', { text: event.resourceType + ' / ' + (event.resourceId || '—') }),
        el('td', { text: event.actorRole || '—' })
      ]));
    }
    table.appendChild(body);
  } catch (error) { /* audit is secondary */ }
}

$('memberSearchBtn').onclick = () => loadMembers($('memberSearch').value.trim());
$('memberSearch').addEventListener('keydown', event => { if (event.key === 'Enter') loadMembers($('memberSearch').value.trim()); });
$('refreshWorks').onclick = loadWorks;
$('refreshAudit').onclick = loadAudit;
$('teacherLogout').onclick = () => { localStorage.removeItem(STORE_KEY); location.href = 'index.html'; };

boot();
