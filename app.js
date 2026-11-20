const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
const $ = id => document.getElementById(id);
const el = UI.el;

let token = localStorage.getItem(STORE_KEY) || '';
let mode = 'email';
let view = null;
let productConfig = null;
let revealedKey = '';
let guideConfig = null;
let guideProgress = null;
let guideLocal = { filtered: false, favorites: new Set(), title: '', destination: '' };
let autoOpened = false;

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
  if (!resp.ok) throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  return data;
}

async function loadProductConfig() {
  try {
    productConfig = await fetch(API + '/api/config').then(resp => resp.json());
    for (const node of document.querySelectorAll('[data-product-name]')) node.textContent = productConfig.productName;
    document.title = productConfig.productName + ' · 创作工作台';
    if (productConfig.onboarding) guideConfig = productConfig.onboarding;
  } catch (error) { productConfig = { productName: '创作平台' }; }
}

function renderKey(relayKey) {
  if (!relayKey) { $('relayKey').textContent = '尚未发放'; return; }
  if (relayKey.plaintext) {
    revealedKey = relayKey.plaintext;
    $('relayKey').textContent = relayKey.plaintext;
    $('keyNote').textContent = '完整密钥只在本次显示，刷新页面后只能看到掩码。请立即复制保存。';
  } else {
    revealedKey = '';
    $('relayKey').textContent = relayKey.hint || '尚未发放';
    $('keyNote').textContent = relayKey.status === 'unavailable'
      ? '中转站暂时不可用，稍后点击“重新生成”重试。'
      : '完整密钥只在发放时显示一次，平台与学院看不到密钥明文。';
  }
}

function ledgerNode(rows) {
  const wrap = el('div', { className: 'ledger' });
  if (!rows || !rows.length) return el('p', { className: 'muted empty', text: '暂无流水记录' });
  for (const row of rows) {
    wrap.appendChild(el('div', { className: 'ledger-row' }, [
      el('div', { className: 'ledger-title' }, [
        el('span', { className: 'ledger-icon', text: row.type === 'grant' ? '＋' : '✦' }),
        el('div', {}, [el('b', { text: row.title }), el('small', { text: row.time })])
      ]),
      el('span', { className: 'amount ' + (row.amount > 0 ? 'plus' : 'minus'),
        text: (row.amount > 0 ? '+' : '') + fmt(row.amount) })
    ]));
  }
  return wrap;
}

function renderAccount(next) {
  view = next;
  $('loginView').classList.add('hidden');
  $('appView').classList.remove('hidden');
  $('userIdentity').textContent = next.identity;
  const stats = $('studentStats');
  stats.replaceChildren(
    UI.MetricCard({ label: '可用 Token', value: next.balance, tone: 'accent', hint: '本期可使用额度' }),
    UI.MetricCard({ label: '累计消耗', value: next.spent, hint: '从领取以来的使用量' }),
    UI.MetricCard({ label: '所属组织', value: (next.org && next.org.name) || '试点组织', hint: '组织是权限与可见范围' })
  );
  $('ledger').replaceChildren(...ledgerNode(next.ledger).childNodes);
  $('profileLedger').replaceChildren(...ledgerNode(next.ledger).childNodes);
  $('profileMetrics').replaceChildren(
    UI.MetricCard({ label: '可用 Token', value: next.balance, tone: 'accent' }),
    UI.MetricCard({ label: '累计消耗', value: next.spent })
  );
  const org = $('profileOrg');
  org.replaceChildren();
  if (next.org) {
    org.appendChild(el('h4', { text: next.org.name }));
    org.appendChild(UI.RoleBadge({ role: next.role }, next.labels));
    org.appendChild(el('p', { className: 'muted', text: '准入状态：' + (next.admissionStatus || '未知') }));
  }
  $('teacherLink').classList.toggle('hidden', next.role !== 'org_admin');
  $('adminEntry').href = next.role === 'org_admin' ? 'teacher.html' : 'admin.html';
  if (next.relayKey) renderKey(next.relayKey);
  const onboarding = next.onboarding || {};
  const showBanner = onboarding.status === 'in_progress' || onboarding.status === 'not_started';
  $('guideBanner').classList.toggle('hidden', !showBanner);
  if (onboarding.status === 'in_progress') $('guideBannerBtn').textContent = '继续指南';
  if (onboarding.autoOpenPending && !autoOpened) { autoOpened = true; openGuide(); }
}

function showLogin() {
  view = null;
  $('appView').classList.add('hidden');
  $('loginView').classList.remove('hidden');
  $('onboarding').classList.add('hidden');
  $('adminEntry').href = 'admin.html';
}

async function refreshMe() {
  const next = await api('/api/me', { method: 'GET' });
  renderAccount(next);
}

async function boot() {
  await loadProductConfig();
  if (!token) { showLogin(); return; }
  try { await refreshMe(); await loadOnboarding(); await loadWorks(); }
  catch (error) { token = ''; localStorage.removeItem(STORE_KEY); showLogin(); }
}

document.querySelectorAll('.tab[data-mode]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab[data-mode]').forEach(node => node.classList.remove('active'));
  button.classList.add('active');
  mode = button.dataset.mode;
  $('identityLabel').textContent = mode === 'email' ? '校园邮箱' : '学号';
  $('identity').placeholder = mode === 'email' ? '例如 2024001001@mail.hust.edu.cn' : '例如 2024001001';
  $('identityHint').textContent = mode === 'email' ? '仅限名册内的学生，邮箱前缀为学号' : '仅限名册内的学生，请输入学号';
}));

function readForm() {
  const identity = $('identity').value.trim();
  const password = $('password').value;
  if (!identity) { toast('请输入校园邮箱或学号'); return null; }
  if (!password || password.length < 8) { toast('密码至少 8 位'); return null; }
  return { mode, identity, password };
}

async function handleAuth(path, message) {
  const form = readForm();
  if (!form) return;
  try {
    const result = await api(path, { method: 'POST', body: JSON.stringify(form) });
    token = result.token;
    localStorage.setItem(STORE_KEY, token);
    renderAccount(result);
    toast(result.firstLogin ? message : '登录成功，已恢复账户数据');
    await Promise.all([loadWorks(), loadOnboarding()]);
  } catch (error) { toast(error.message || '操作失败'); }
}

$('loginBtn').onclick = () => handleAuth('/api/login', '登录成功');
$('registerBtn').onclick = () => handleAuth('/api/register', '注册成功，已领取 100,000 Token');
$('logoutBtn').onclick = async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  token = ''; revealedKey = ''; localStorage.removeItem(STORE_KEY); showLogin();
};
$('rotateKeyBtn').onclick = async () => {
  try { const relayKey = await api('/api/key/rotate', { method: 'POST', body: '{}' }); renderKey(relayKey); toast('已重新生成密钥，旧密钥已失效'); }
  catch (error) { toast(error.message || '重新生成失败'); }
};
$('copyKeyBtn').onclick = async () => {
  if (!revealedKey) { toast('当前只能看到掩码，点“重新生成”获取新的完整密钥'); return; }
  try { await navigator.clipboard.writeText(revealedKey); toast('密钥已复制'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
$('demoUseBtn').onclick = async () => {
  try {
    const next = await api('/api/consume', { method: 'POST', body: JSON.stringify({ amount: 1200, requestId: 'ui-' + Date.now() }) });
    renderAccount(next); toast('已模拟消耗 1,200 Token');
  } catch (error) { toast(error.status === 409 ? 'Token 余额不足' : (error.message || '操作失败')); }
};

function setPage(page) {
  document.querySelectorAll('.page').forEach(node => node.classList.toggle('active', node.id === 'page-' + page));
  document.querySelectorAll('.navbtn[data-page]').forEach(node => node.classList.toggle('active', node.dataset.page === page));
  if (page === 'gallery') loadWorks();
  if (page === 'profile' && view) renderAccount(view);
}
document.querySelectorAll('.navbtn[data-page]').forEach(button => button.addEventListener('click', () => setPage(button.dataset.page)));
$('galleryQuick').onclick = () => setPage('gallery');
$('createBtn').onclick = () => { setPage('gallery'); $('submitPanel').classList.remove('hidden'); };

function openModal(title, text) { $('modalTitle').textContent = title; $('modalText').textContent = text; $('modal').classList.remove('hidden'); }
$('rechargeBtn').onclick = () => openModal('充值额度', '充值功能将在正式版开放，原型阶段仅展示入口。');
$('closeModal').onclick = () => $('modal').classList.add('hidden');
$('modalAction').onclick = () => $('modal').classList.add('hidden');
$('resetFromLogin').onclick = $('resetBtn').onclick = async () => {
  try { await api('/api/reset', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  token = ''; revealedKey = ''; localStorage.removeItem(STORE_KEY); showLogin(); toast('演示数据已重置');
};

function workCards(items, options) {
  const grid = el('div', { className: 'work-grid' });
  if (!items || !items.length) {
    grid.appendChild(el('p', { className: 'muted empty', text: '这里还没有作品。你可以先看看演示作品，或提交自己的作品。' }));
    return grid;
  }
  for (const item of items) grid.appendChild(UI.WorkCard(item, options));
  return grid;
}

async function loadWorks() {
  if (!token) return;
  try {
    const [gallery, mine] = await Promise.all([api('/api/works'), api('/api/works/mine')]);
    $('galleryWorks').replaceChildren(...workCards(gallery.items, {
      onOpen: work => openModal(work.title, (work.summary || '') + '\n\n' + (work.sourceKind === 'seed_demo'
        ? '这是平台演示作品，用于展示作品卡和广场交互。' : '这是本组织作品，仅在本组织内可见。'))
    }).childNodes);
    $('myWorks').replaceChildren(...workCards(mine.items, {
      showStatus: true, onOpen: work => openModal(work.title, work.summary || ''),
      actions: [{ label: '撤回', tone: 'danger', onClick: withdrawWork }]
    }).childNodes);
  } catch (error) { if (error.status !== 401) toast(error.message || '作品列表暂时不可用'); }
}

async function withdrawWork(work) {
  if (!confirm('确认撤回《' + work.title + '》？撤回后将从广场移除。')) return;
  try { await api('/api/works/' + encodeURIComponent(work.workId) + '/withdraw', { method: 'POST', body: '{}' }); await loadWorks(); toast('作品已撤回'); }
  catch (error) { toast(error.message || '撤回失败'); }
}
$('submitWorkBtn').onclick = () => $('submitPanel').classList.toggle('hidden');
$('submitWorkCancel').onclick = () => $('submitPanel').classList.add('hidden');
$('submitWorkConfirm').onclick = async () => {
  const title = $('workTitle').value.trim();
  const summary = $('workSummary').value.trim();
  if (!title || !summary) { toast('请填写作品名称和简介'); return; }
  try {
    await api('/api/works', { method: 'POST', body: JSON.stringify({ title, summary, category: $('workCategory').value.trim() || '其他' }) });
    $('workTitle').value = ''; $('workSummary').value = ''; $('workCategory').value = '';
    $('submitPanel').classList.add('hidden'); await loadWorks(); toast('已提交，等待本组织教师审核');
  } catch (error) { toast(error.message || '提交失败'); }
};

async function loadOnboarding() {
  if (!token) return;
  try { const data = await api('/api/onboarding'); guideConfig = data.config; guideProgress = data.progress; }
  catch (error) { /* account remains usable */ }
}
function stepIndex(stepId) { return (guideConfig.steps || []).findIndex(step => step.step_id === stepId); }
function currentGuideStep() {
  if (!guideConfig || !guideProgress) return null;
  const steps = guideConfig.steps || [];
  return steps.find(step => step.step_id === guideProgress.currentStepId) ||
    steps.find(step => ['not_started', 'in_progress'].includes((guideProgress.steps[step.step_id] || {}).status)) || null;
}
function guidePost(payload) {
  return api('/api/onboarding', { method: 'POST', body: JSON.stringify(Object.assign({ expectedRevision: guideProgress.revision }, payload)) });
}

async function openGuide() {
  if (!token) { setPage('workspace'); toast('登录后可以开始指南'); return; }
  if (!guideConfig || !guideProgress) await loadOnboarding();
  if (!guideConfig || !guideProgress) { toast('指南暂时不可用'); return; }
  $('onboarding').classList.remove('hidden');
  const step = currentGuideStep();
  if (step && (guideProgress.steps[step.step_id] || {}).status === 'not_started') {
    try { guideProgress = (await guidePost({ action: 'start', stepId: step.step_id })).progress; } catch (error) { toast(error.message); }
  }
  renderGuide();
}

function progressTrack() {
  const list = el('ol', { className: 'step-track' });
  for (const step of guideConfig.steps) {
    const state = (guideProgress.steps[step.step_id] || {}).status || 'not_started';
    list.appendChild(el('li', { className: state + (guideProgress.currentStepId === step.step_id ? ' current' : ''),
      'aria-current': guideProgress.currentStepId === step.step_id ? 'step' : null }, [
      el('span', { className: 'step-dot', text: state === 'completed' ? '✓' : state === 'skipped' ? '–' : '' }),
      el('span', { text: step.title }), el('small', { text: state === 'completed' ? '已完成' : state === 'skipped' ? '已跳过' : step.estimated_minutes + ' 分钟' })
    ]));
  }
  return list;
}

function roleMapBlock() {
  return el('div', { className: 'role-map' }, [
    el('article', {}, [el('b', { text: '总协调' }), el('p', { text: '接收目标，确认范围与关键取舍。' }), el('small', { text: '你在这里做决定' })]),
    el('article', {}, [el('b', { text: '项目规划' }), el('p', { text: '把目标拆成任务，确定依赖并验收。' }), el('small', { text: '安排并行和交付' })]),
    el('article', {}, [el('b', { text: '任务执行' }), el('p', { text: '完成具体成果，交回证据和未完成项。' }), el('small', { text: '界面、数据、检查' })])
  ]);
}
const TEMPLATE = '任务名：校园活动清单\n目标：让同学从虚构活动中找到感兴趣的一项。\n验收条件：\n1. 展示 6 条虚构活动。\n2. 切换类别后只显示对应活动。\n3. 收藏可以取消。\n范围：列表、筛选、本次练习内收藏。\n不在范围：真实报名、支付和学校系统。';
function templateBlock() {
  return el('div', { className: 'template-block' }, [
    el('div', { className: 'block-head' }, [el('h4', { text: '可复制的任务书' }), el('button', { className: 'ghost', text: '复制', onclick: async () => {
      try { await navigator.clipboard.writeText(TEMPLATE); toast('任务书已复制'); } catch (error) { toast('复制失败，请手动选择文本'); }
    } })]), el('pre', { text: TEMPLATE })
  ]);
}
function productBlock() {
  const products = [
    ['teams', 'Huanxi Teams', '把一个小目标交给个人 AI 团队，在关键节点做选择。'],
    ['huaxios', '欢喜 OS', '由课程提供的个人 Agent 系统，让助手围绕任务协作。'],
    ['demo', '先用演示了解', '不连接外部产品，用内置小工具练习完整过程。']
  ];
  const wrap = el('div', { className: 'choice-grid', role: 'radiogroup', 'aria-label': '选择练习工具' });
  for (const [id, title, desc] of products) {
    const input = el('input', { type: 'radio', name: 'product', value: id, checked: guideLocal.productChoice === id,
      onchange: () => { guideLocal.productChoice = id; } });
    wrap.appendChild(el('label', { className: 'choice-card' }, [input, el('b', { text: title }), el('span', { text: desc })]));
  }
  return wrap;
}
const EVENTS = [
  { title: '社团招新摆摊', category: '社团', time: '周一 18:30' }, { title: '软件学院技术沙龙', category: '学习', time: '周二 19:00' },
  { title: '校园夜跑', category: '运动', time: '周三 20:00' }, { title: '开源项目分享会', category: '学习', time: '周四 16:00' },
  { title: '草地音乐节', category: '社团', time: '周五 19:30' }, { title: '周末羽毛球', category: '运动', time: '周六 15:00' }
];
function practiceBlock() {
  const wrap = el('div', { className: 'practice' }, [
    el('div', { className: 'demo-flag', text: '演示 · 不调用真实 AI，不扣减真实额度' }),
    el('label', { text: '作品标题' }),
    el('input', { id: 'practiceTitle', maxlength: '60', value: guideLocal.title || '本周校园活动',
      oninput: event => { guideLocal.title = event.target.value.trim(); } }),
    el('div', { className: 'filter-row', id: 'practiceFilters' }, ['全部', '学习', '社团', '运动'].map((category, index) =>
      el('button', { className: 'chip' + (index === 0 ? ' active' : ''), text: category, onclick: event => {
        guideLocal.filtered = true;
        for (const node of event.currentTarget.parentNode.children) node.classList.remove('active');
        event.currentTarget.classList.add('active'); renderPracticeList(wrap, category === '全部' ? null : category);
      } }))),
    el('div', { className: 'event-list', id: 'practiceEvents' })
  ]);
  guideLocal.title = guideLocal.title || '本周校园活动';
  renderPracticeList(wrap, null);
  if (guideConfig.videoUrl) wrap.appendChild(el('video', { className: 'guide-video', controls: true, preload: 'metadata', src: guideConfig.videoUrl }));
  return wrap;
}
function renderPracticeList(wrap, category) {
  const list = wrap.querySelector('#practiceEvents');
  if (!list) return;
  list.replaceChildren();
  for (const item of EVENTS.filter(event => !category || event.category === category)) {
    const picked = guideLocal.favorites.has(item.title);
    list.appendChild(el('div', { className: 'event-row' }, [
      el('div', {}, [el('b', { text: item.title }), el('small', { text: item.category + ' · ' + item.time })]),
      el('button', { className: picked ? 'primary' : 'outline', text: picked ? '已收藏' : '收藏', onclick: () => {
        if (picked) guideLocal.favorites.delete(item.title); else guideLocal.favorites.add(item.title);
        renderPracticeList(wrap, category);
      } })
    ]));
  }
}
function destinationBlock() {
  return el('div', { className: 'choice-grid' }, [
    ['gallery', '产品广场', '看演示作品，也提交自己的作品。'], ['profile', '个人信息', '查看额度、密钥、记录和组织身份。']
  ].map(([id, title, desc]) => el('label', { className: 'choice-card' }, [
    el('input', { type: 'radio', name: 'destination', value: id, checked: guideLocal.destination === id,
      onchange: () => { guideLocal.destination = id; } }), el('b', { text: title }), el('span', { text: desc })
  ])));
}
function renderBlock(block) {
  if (block.kind === 'role-map') return roleMapBlock();
  if (block.kind === 'task-template') return templateBlock();
  if (block.kind === 'product-choice') return productBlock();
  if (block.kind === 'practice') return practiceBlock();
  if (block.kind === 'destination-choice') return destinationBlock();
  if (block.kind === 'video') return null;
  return el('p', { className: 'muted', text: '这一步的内容暂不可用。' });
}

function renderGuide() {
  if (!guideProgress || !guideConfig) return;
  $('onboardingProgress').replaceChildren(progressTrack());
  const step = currentGuideStep();
  const body = $('onboardingBody');
  const actions = $('onboardingActions');
  body.replaceChildren(); actions.replaceChildren();
  if (!step) {
    body.appendChild(el('div', { className: 'guide-finish' }, [
      el('h3', { text: '这轮指南已经结束' }),
      el('p', { className: 'muted', text: '完成 ' + guideProgress.completedCount + ' 步，跳过 ' + guideProgress.skippedCount + ' 步。' }),
      el('p', { className: 'muted', text: '你可以从工作台的“开始指南”随时重进，也可以继续创作。' })
    ]));
    actions.appendChild(el('button', { className: 'primary', text: '回到工作台', onclick: () => closeGuide(false) }));
    return;
  }
  const status = (guideProgress.steps[step.step_id] || {}).status;
  $('onboardingTitle').textContent = '第 ' + (stepIndex(step.step_id) + 1) + ' 步，共 ' + guideConfig.steps.length + ' 步 · ' + step.title;
  body.appendChild(el('div', { className: 'guide-copy' }, [
    el('p', { className: 'eyebrow', text: '完成 ' + guideProgress.completedCount + ' 步 · 跳过 ' + guideProgress.skippedCount + ' 步' }),
    el('h3', { text: step.title }),
    el('p', { className: 'muted', text: step.step_id === 'team-basics' ? '先把目标说清楚，再让团队分工。这里的角色名称用于教学，对应关系见下方说明。' :
      step.step_id === 'reuse-teams' ? '两种产品都先讲定位和通用练习，不写具体按钮、截图或安装指引。' :
      step.step_id === 'first-work' ? '用预制小工具做一次完整练习。全过程标注“演示”，不会调用真实 AI。' :
      '选一个去向，结束这次指南。你也可以稍后继续。' })
  ]));
  for (const block of step.blocks || []) { const node = renderBlock(block); if (node) body.appendChild(node); }
  if (step.step_id === 'team-basics') body.appendChild(el('p', { className: 'hint', text: '对应说明：总协调对应内部的总控，项目规划对应 owner，任务执行对应执行者。' }));
  const previous = guideConfig.steps[stepIndex(step.step_id) - 1];
  if (previous) actions.appendChild(el('button', { className: 'ghost', text: '上一步', onclick: async () => {
    try { guideProgress = (await guidePost({ action: 'start', stepId: previous.step_id })).progress; renderGuide(); } catch (error) { toast(error.message); }
  } }));
  actions.appendChild(el('button', { className: 'outline', text: '暂时跳过', onclick: () => skipGuideStep(step) }));
  actions.appendChild(el('button', { className: 'ghost', text: '跳过全部', onclick: skipAllGuide }));
  actions.appendChild(el('button', { className: 'primary', text: step.step_id === 'explore' ? '完成并前往' : '继续', onclick: () => completeGuideStep(step) }));
  if (status === 'skipped') body.appendChild(el('p', { className: 'hint', text: '这一步之前被跳过，现在可以重新完成。' }));
}

async function completeGuideStep(step) {
  if (step.step_id === 'reuse-teams' && !guideLocal.productChoice) { toast('请选择一个工具，或选择“先用演示了解”'); return; }
  if (step.step_id === 'first-work' && (!guideLocal.title || !guideLocal.filtered || guideLocal.favorites.size === 0)) {
    toast('请修改标题、切换一次筛选并收藏一项活动'); return;
  }
  if (step.step_id === 'explore' && !guideLocal.destination) { toast('请选择一个去向'); return; }
  try {
    const data = await guidePost({ action: 'complete', stepId: step.step_id, completionKind: 'demo',
      draft: { productChoice: guideLocal.productChoice, title: guideLocal.title,
        checks: { filtered: guideLocal.filtered, favorites: guideLocal.favorites.size }, destination: guideLocal.destination } });
    guideProgress = data.progress;
    if (step.step_id === 'explore') {
      guideProgress = (await guidePost({ action: 'finish', destination: guideLocal.destination || 'workspace' })).progress;
      await closeGuide(false);
      setPage(guideLocal.destination === 'profile' ? 'profile' : 'gallery');
      toast('指南完成，已记录完成 ' + guideProgress.completedCount + ' 步');
      return;
    }
    const next = guideConfig.steps[stepIndex(step.step_id) + 1];
    if (next) guideProgress = (await guidePost({ action: 'start', stepId: next.step_id })).progress;
    await refreshMe(); renderGuide();
  } catch (error) { toast(error.message || '保存失败'); }
}
async function skipGuideStep(step) {
  try {
    guideProgress = (await guidePost({ action: 'skip', stepId: step.step_id })).progress;
    const next = guideConfig.steps[stepIndex(step.step_id) + 1];
    if (next) guideProgress = (await guidePost({ action: 'start', stepId: next.step_id })).progress;
    await refreshMe(); renderGuide();
  } catch (error) { toast(error.message || '保存失败'); }
}
async function skipAllGuide() {
  try {
    guideProgress = (await guidePost({ action: 'skip_all' })).progress;
    await closeGuide(false); toast('已跳过本轮指南，可在工作台重新进入');
  } catch (error) { toast(error.message || '保存失败'); }
}
async function closeGuide(recordExit = true) {
  if (recordExit && guideProgress && guideProgress.currentStepId) {
    try { guideProgress = (await guidePost({ action: 'exit', stepId: guideProgress.currentStepId })).progress; } catch (error) { /* keep current */ }
  }
  $('onboarding').classList.add('hidden');
  await loadOnboarding();
  if (view) await refreshMe();
}
$('guideNav').onclick = openGuide;
$('guideBannerBtn').onclick = openGuide;
$('onboardingClose').onclick = () => closeGuide(true);

boot();
