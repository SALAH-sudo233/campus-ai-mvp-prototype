const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
const token = localStorage.getItem(STORE_KEY) || '';
const $ = id => document.getElementById(id);
const el = UI.el;
let me = null;
let orgId = null;
let allOrgWorks = [];
let workStatusTab = 'all';
let workTypeTab = 'all';

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
  $('teacherAgentTools').classList.add('hidden');
  $('teacherLogout').classList.add('hidden');
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
    if (me.role === 'org_member') { window.location.replace('index.html'); return; }
    if (!['org_admin', 'org_operator', 'platform_admin'].includes(me.role)) { denied(); return; }
    orgId = me.org && me.org.id;
    if (!orgId) { denied(); return; }
    $('teacherAgentTools').classList.remove('hidden');
    $('teacherLogout').classList.remove('hidden');
    $('teacherMain').classList.remove('hidden');
    $('teacherOrg').textContent = me.org.name;
    $('orgTitle').textContent = me.org.name;
    $('orgSubtitle').textContent = ((me.labels && me.labels.admin) || '教师') + '视角 · 本组织用量、活跃度与精选作品';
    await Promise.all([loadDashboard(), loadWorks()]);
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
      UI.MetricCard({ label: '总消耗', value: summary.totalConsumed, unit: ' Token', hint: '平台记账消耗' }),
      UI.MetricCard({ label: '剩余额度', value: summary.remainingQuota, unit: ' Token',
        hint: '平台账面剩余 · 人均 ' + Number(summary.memberQuota || 0).toLocaleString('zh-CN') +
          (summary.rewardTotal > 0 ? (' · 另含作品奖励 +' + fmt(summary.rewardTotal)) : '') })
    );
    $('trendChart').replaceChildren(UI.TrendChart(data.trend));
    $('heatmap').replaceChildren(UI.Heatmap(data.heatmap));
    renderActivity(data.activity);
    renderRanking(data.ranking);
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

function matchesWorkStatus(work, tab) {
  if (work.status === 'pending_review') return false;
  if (tab === 'featured') return work.status === 'published' && !!work.featured;
  if (tab === 'published') return work.status === 'published';
  if (tab === 'inactive') return ['removed', 'withdrawn', 'rejected'].includes(work.status);
  return true;
}

function matchesWorkType(work, typeKey) {
  if (typeKey === 'all') return true;
  return (work.softwareType || 'web') === typeKey;
}

function renderWorkFilters() {
  const statusTabs = [
    ['all', '全部'],
    ['featured', '已精选'],
    ['published', '已发布'],
    ['inactive', '已下架']
  ];
  $('teacherWorkTabs').replaceChildren(...statusTabs.map(([key, label]) => {
    const count = allOrgWorks.filter(w => matchesWorkStatus(w, key) && matchesWorkType(w, workTypeTab)).length;
    return el('button', {
      className: 'ph-sort' + (workStatusTab === key ? ' active' : ''),
      text: label + ' (' + count + ')',
      onclick: () => { workStatusTab = key; renderWorkFilters(); renderWorkCards(); }
    });
  }));
  const typePills = [
    ['all', '全部类型'],
    ['web', 'Web 应用'],
    ['app', 'App'],
    ['pc', 'PC 软件']
  ];
  $('teacherWorkTypes').replaceChildren(...typePills.map(([key, label]) => el('button', {
    className: 'ph-cat' + (workTypeTab === key ? ' active' : ''),
    text: label,
    onclick: () => { workTypeTab = key; renderWorkFilters(); renderWorkCards(); }
  })));
}

function renderWorkCards() {
  const wrap = $('pendingWorks');
  wrap.replaceChildren();
  const items = allOrgWorks.filter(w => matchesWorkStatus(w, workStatusTab) && matchesWorkType(w, workTypeTab));
  if (!items.length) {
    wrap.appendChild(el('p', { className: 'muted empty', text: '当前筛选条件下暂无作品。' }));
    return;
  }
  for (const work of items) {
    const actions = [];
    if (work.status === 'published') {
      actions.push({ label: work.featured ? '取消精选' : '精选', tone: 'outline', onClick: () => featureWork(work, !work.featured) });
      actions.push({ label: '下架', tone: 'danger', onClick: () => unpublishWork(work) });
    }
    wrap.appendChild(UI.WorkCard(work, { density: 'compact', showStatus: true, onOpen: openWork, actions }));
  }
}

async function loadWorks() {
  try {
    const data = await api('/api/orgs/' + encodeURIComponent(orgId) + '/works');
    allOrgWorks = (data.items || []).filter(w => w.status !== 'pending_review');
    renderWorkFilters();
    renderWorkCards();
  } catch (error) { toast(error.message || '作品列表暂时不可用'); }
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
  if (work.hostedUrl) { window.open(API + '/' + work.hostedUrl, '_blank', 'noopener,noreferrer'); return; }
  if (work.externalUrl) { window.open(work.externalUrl, '_blank', 'noopener,noreferrer'); return; }
  toast(work.title + '：可前往“作品广场”查看详情或下载安装包。');
}

$('refreshWorks').onclick = loadWorks;
$('teacherLogout').onclick = async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  localStorage.removeItem(STORE_KEY);
  location.href = 'index.html';
};
$('teacherBackLogin').onclick = () => { localStorage.removeItem(STORE_KEY); };

boot();
