const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
const $ = id => document.getElementById(id);
const el = UI.el;

let token = localStorage.getItem(STORE_KEY) || '';
let mode = 'student';
let view = null;
let productConfig = null;
let revealedKey = '';
let pendingReview = false;
let activePromptType = 'web';

const PENDING_HINT = '账户待复核，复核通过后开放';

const PUBLISH_PROMPTS = {
  web: {
    hint: 'Web 应用支持直接上传单文件 index.html 或静态站点 dist.zip，平台自动托管成可访问网站。',
    text: `请帮我准备这个 Web 应用的发布产物，目标上传到「校园 AI 创作平台」托管成在线网站：
1. 将当前项目构建/整理为纯静态网站：要么是单文件 index.html，要么是根目录直接包含 index.html 的 dist.zip（所有 JS/CSS/图片使用相对路径，不依赖本地后端）。
2. 本地验证页面打开无控制台报错、无 404 资源、首屏加载清晰。
3. 整理一段发布信息发给我，包含：
   - 作品名称（≤ 20 字）
   - 一句话卖点（≤ 60 字，说明帮谁解决什么问题）
   - 版本号（如 v1.0.0）与使用的 AI 工具
   - 核心亮点与使用说明`
  },
  app: {
    hint: 'App 支持上传 .apk / .ipa / .hap / .zip 安装包，或填写外部下载/TestFlight 链接。',
    text: `请帮我准备这个 App 的测试发布安装包，目标上传到「校园 AI 创作平台」供同学下载体验：
1. 检查应用名称、版本号与权限配置，构建可安装的测试包（Android .apk / iOS .ipa / 鸿蒙 .hap，或包含安装说明的 .zip，单个包 ≤ 50 MB）。
2. 确认目标安装平台（如 Android / iOS / 鸿蒙）与最低系统要求。
3. 整理一段发布信息发给我，包含：
   - 作品名称（≤ 20 字）
   - 一句话卖点（≤ 60 字）
   - 适用平台与版本号（如 Android · v1.0.0）
   - 安装步骤与主要功能说明`
  },
  pc: {
    hint: 'PC 软件支持上传 .dmg / .exe / .msi / .zip / .tar.gz 安装包，或填写外部 Release 链接。',
    text: `请帮我准备这个 PC 软件的发布包，目标上传到「校园 AI 创作平台」供同学下载使用：
1. 将当前桌面应用打包为可直接运行或安装的发布包（macOS .dmg/.zip、Windows .exe/.msi/.zip 或 Linux 包，≤ 50 MB），附带简明 README。
2. 核对运行依赖与首次启动说明（如无需额外配置环境）。
3. 整理一段发布信息发给我，包含：
   - 作品名称（≤ 20 字）
   - 一句话卖点（≤ 60 字）
   - 适用系统与版本号（如 macOS / Windows · v1.0.0）
   - 安装与运行说明`
  }
};

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
    document.title = productConfig.productName + ' · 我的作品';
  } catch (error) { productConfig = { productName: '创作平台' }; }
}

function renderPublishPrompt() {
  const item = PUBLISH_PROMPTS[activePromptType] || PUBLISH_PROMPTS.web;
  $('publishPromptBox').textContent = item.text;
  $('publishPromptHint').textContent = item.hint;
  document.querySelectorAll('[data-prompt-type]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.promptType === activePromptType);
  });
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
      ? '中转站暂时不可用，稍后点击“重新生成密钥”重试。'
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
  pendingReview = next.admissionStatus === 'pending_review';
  $('loginView').classList.add('hidden');
  $('appView').classList.remove('hidden');
  $('userIdentity').textContent = next.identity;
  $('studentStats').replaceChildren(
    UI.MetricCard({ label: '可用 Token', value: next.balance, tone: 'accent', hint: '本期可使用额度' }),
    UI.MetricCard({ label: '累计消耗', value: next.spent, hint: '从领取以来的使用量' }),
    UI.MetricCard({ label: '所属组织', value: (next.org && next.org.name) || '试点组织', hint: '组织是权限与可见范围' })
  );
  const rows = next.ledger || [];
  $('ledgerCount').textContent = '共 ' + rows.length + ' 条记录';
  $('ledger').replaceChildren(...ledgerNode(rows).childNodes);
  $('apiBaseUrl').textContent = location.origin + '/v1';
  renderPublishPrompt();
  if (next.relayKey) renderKey(next.relayKey);
  applyAdmission();
}

function applyAdmission() {
  $('pendingBanner').classList.toggle('hidden', !pendingReview);
  const blocked = [$('demoUseBtn'), $('rotateKeyBtn'), $('createBtn'), $('goPublishBtn'), $('galleryQuick'), $('galleryNav')];
  for (const node of blocked) {
    if (!node) continue;
    if ('disabled' in node) node.disabled = pendingReview;
    node.classList.toggle('is-disabled', pendingReview);
    node.title = pendingReview ? PENDING_HINT : '';
  }
}

function redirectIfTeacher(account) {
  if (account && account.role === 'org_admin') {
    window.location.replace('teacher.html');
    return true;
  }
  return false;
}

function showLogin() {
  view = null;
  pendingReview = false;
  $('appView').classList.add('hidden');
  $('loginView').classList.remove('hidden');
}

async function refreshMe() {
  const next = await api('/api/me', { method: 'GET' });
  if (redirectIfTeacher(next)) return false;
  renderAccount(next);
  return true;
}

async function boot() {
  await loadProductConfig();
  if (!token) { showLogin(); return; }
  try {
    await refreshMe();
  } catch (error) {
    token = '';
    localStorage.removeItem(STORE_KEY);
    showLogin();
  }
}

document.querySelectorAll('.tab[data-mode]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab[data-mode]').forEach(node => node.classList.remove('active'));
  button.classList.add('active');
  mode = button.dataset.mode;
  const student = mode === 'student';
  $('identityLabel').textContent = '校园邮箱';
  $('identityLabel').classList.toggle('hidden', !student);
  $('identity').placeholder = '例如 2024001001@mail.hust.edu.cn';
  $('identity').disabled = !student;
  $('identity').classList.toggle('hidden', !student);
  $('identityHint').textContent = '学生端只使用校园邮箱登录，邮箱前缀为学号';
  $('identityHint').classList.toggle('hidden', !student);
  $('passwordLabel').classList.toggle('hidden', !student);
  $('password').classList.toggle('hidden', !student);
  $('passwordHint').classList.toggle('hidden', !student);
  $('loginBtn').classList.toggle('hidden', !student);
  $('registerBtn').classList.toggle('hidden', !student);
  $('quickLoginBtn').textContent = student ? '管理员快捷登录学生端' : '管理员快捷登录老师端';
}));

function readForm() {
  const identity = $('identity').value.trim();
  const password = $('password').value;
  if (!identity) { toast('请输入校园邮箱'); return null; }
  if (!password || password.length < 8) { toast('密码至少 8 位'); return null; }
  return { mode: 'email', identity, password };
}

async function handleAuth(path, message) {
  const form = readForm();
  if (!form) return;
  try {
    const result = await api(path, { method: 'POST', body: JSON.stringify(form) });
    token = result.token;
    localStorage.setItem(STORE_KEY, token);
    if (redirectIfTeacher(result)) return;
    renderAccount(result);
    toast(result.firstLogin ? message : '登录成功，已恢复账户数据');
  } catch (error) { toast(error.message || '操作失败'); }
}

async function demoLogin(role) {
  try {
    const resp = await fetch(API + '/api/demo/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role })
    });
    const result = await resp.json();
    if (!resp.ok) throw Object.assign(new Error(result.error || '快捷登录失败'), { status: resp.status });
    token = result.token;
    localStorage.setItem(STORE_KEY, token);
    if (role === 'teacher' || redirectIfTeacher(result)) {
      window.location.replace('teacher.html');
      return;
    }
    renderAccount(result);
    toast('已进入学生端演示账号');
  } catch (error) { toast(error.message || '快捷登录失败'); }
}

$('loginBtn').onclick = () => handleAuth('/api/login', '登录成功');
$('registerBtn').onclick = () => handleAuth('/api/register', '注册成功，已领取 100,000 Token');
$('quickLoginBtn').onclick = () => demoLogin(mode === 'teacher' ? 'teacher' : 'student');
$('logoutBtn').onclick = async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  token = ''; revealedKey = ''; localStorage.removeItem(STORE_KEY); showLogin();
};
$('rotateKeyBtn').onclick = async () => {
  try { const relayKey = await api('/api/key/rotate', { method: 'POST', body: '{}' }); renderKey(relayKey); toast('已重新生成密钥，旧密钥已失效'); }
  catch (error) { toast(error.message || '重新生成失败'); }
};
$('copyBaseBtn').onclick = async () => {
  try { await navigator.clipboard.writeText($('apiBaseUrl').textContent); toast('Base URL 已复制'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
$('copyKeyBtn').onclick = async () => {
  if (!revealedKey) { toast('当前只能看到掩码，点“重新生成密钥”获取新的完整密钥'); return; }
  try { await navigator.clipboard.writeText(revealedKey); toast('密钥已复制'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
$('copyEnvBtn').onclick = async () => {
  const keyText = revealedKey || '<点击重新生成密钥获取完整密钥>';
  const snippet = 'export OPENAI_BASE_URL="' + $('apiBaseUrl').textContent + '"\nexport OPENAI_API_KEY="' + keyText + '"';
  try { await navigator.clipboard.writeText(snippet); toast(revealedKey ? '环境变量配置已复制' : '配置模板已复制（请补填完整密钥）'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
document.querySelectorAll('[data-prompt-type]').forEach(btn => btn.addEventListener('click', () => {
  activePromptType = btn.dataset.promptType;
  renderPublishPrompt();
}));
$('copyPromptBtn').onclick = async () => {
  const item = PUBLISH_PROMPTS[activePromptType] || PUBLISH_PROMPTS.web;
  try { await navigator.clipboard.writeText(item.text); toast('发布 Prompt 已复制，发给你的 AI 即可'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
$('goPublishBtn').onclick = () => {
  if (pendingReview) { toast(PENDING_HINT); return; }
  window.location.href = 'gallery.html?submit=1&type=' + encodeURIComponent(activePromptType);
};
$('demoUseBtn').onclick = async () => {
  try {
    const next = await api('/api/consume', { method: 'POST', body: JSON.stringify({ amount: 1200, requestId: 'ui-' + Date.now() }) });
    renderAccount(next);
    $('ledgerFold').open = true;
    toast('已模拟消耗 1,200 Token');
  } catch (error) { toast(error.status === 409 ? 'Token 余额不足' : (error.message || '操作失败')); }
};
$('galleryNav').addEventListener('click', event => {
  if (pendingReview) { event.preventDefault(); toast(PENDING_HINT); }
});
$('galleryQuick').onclick = () => {
  if (pendingReview) { toast(PENDING_HINT); return; }
  window.location.href = 'gallery.html';
};
$('createBtn').onclick = () => {
  if (pendingReview) { toast(PENDING_HINT); return; }
  window.location.href = 'gallery.html?submit=1';
};

function openModal(title, text) { $('modalTitle').textContent = title; $('modalText').textContent = text; $('modal').classList.remove('hidden'); }
$('rechargeBtn').onclick = () => openModal('充值额度', '充值功能将在正式版开放，原型阶段仅展示入口。');
$('closeModal').onclick = () => $('modal').classList.add('hidden');
$('modalAction').onclick = () => $('modal').classList.add('hidden');
$('resetFromLogin').onclick = $('resetBtn').onclick = async () => {
  try { await api('/api/reset', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  token = ''; revealedKey = ''; localStorage.removeItem(STORE_KEY); showLogin(); toast('演示数据已重置');
};

boot();
