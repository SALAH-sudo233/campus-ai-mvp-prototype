const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STUDENT_KEY = 'hust-campus-ai-demo-token-v2';
const ADMIN_KEY = 'hust-campus-ai-admin-token-v1';
const $ = id => document.getElementById(id);
const el = UI.el;
let authToken = localStorage.getItem(STUDENT_KEY) || sessionStorage.getItem(ADMIN_KEY) || '';
let orgId = new URLSearchParams(location.search).get('org') || '';

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

async function request(path, options, raw) {
  const headers = (options && options.headers) || {};
  if (!raw) headers['Content-Type'] = 'application/json';
  if (authToken) headers.Authorization = 'Bearer ' + authToken;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (!resp.ok) {
    let data = {};
    try { data = await resp.json(); } catch (error) { data = {}; }
    throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  }
  return raw ? resp : resp.json();
}

async function boot() {
  fetch(API + '/api/config').then(resp => resp.json()).then(config => {
    for (const node of document.querySelectorAll('[data-product-name]')) node.textContent = config.productName;
    document.title = config.productName + ' · 名单导入';
  }).catch(() => {});
  if (!authToken) { toast('请先登录'); return; }
  if (!orgId) {
    try {
      const me = await request('/api/me');
      orgId = me.org && me.org.id;
      $('backLink').href = me.role === 'org_admin' ? 'teacher.html' : 'index.html';
    } catch (error) {
      try { const orgs = await request('/api/orgs'); orgId = orgs.organizations[0] && orgs.organizations[0].id; }
      catch (inner) { toast('无法确定组织，请从教师端进入'); return; }
    }
  }
  if (!orgId) { toast('无法确定组织'); return; }
}

function status(text, tone) {
  const node = $('importStatus');
  node.textContent = text;
  node.className = 'notice ' + (tone || '');
}

async function preview() {
  const file = $('csvFile').files[0];
  if (!file) { toast('请先选择 CSV 文件'); return; }
  if (file.size > 10 * 1024 * 1024) { toast('文件超过 10 MB 上限'); return; }
  status('正在解析，不会写入成员表……');
  try {
    const data = await request('/api/orgs/' + encodeURIComponent(orgId) + '/imports/preview', {
      method: 'POST', body: file, headers: { 'Content-Type': 'text/csv; charset=utf-8' }
    }, true).then(resp => resp.json());
    $('previewArea').classList.remove('hidden');
    $('commitArea').classList.add('hidden');
    $('previewContent').replaceChildren(UI.ImportPreview(data, {
      onConfirm: commit,
      onDownload: () => downloadErrors(data.batchId),
      onCancel: () => { $('previewArea').classList.add('hidden'); status('已取消预览，没有写入任何成员。'); }
    }));
    status('预览完成：可写入 ' + data.counts.accepted + ' 行，拒绝 ' + data.counts.rejected + ' 行。确认前不会写成员表。');
  } catch (error) {
    status(error.message || '解析失败', 'error');
    toast(error.message || '解析失败');
  }
}

async function commit(data) {
  status('正在确认导入……');
  try {
    const result = await request('/api/orgs/' + encodeURIComponent(orgId) + '/imports/' + encodeURIComponent(data.batchId) + '/commit', {
      method: 'POST', body: JSON.stringify({ requestId: 'import-' + data.batchId })
    });
    $('commitArea').classList.remove('hidden');
    const c = result.counts || {};
    $('commitContent').replaceChildren(el('div', { className: 'import-summary' }, [
      el('p', { text: '写入 ' + (c.accepted || 0) + ' 行，跳过 ' + (c.skipped || 0) + ' 行，冲突 ' + (c.conflicts || 0) + ' 行，拒绝 ' + (c.rejected || 0) + ' 行。' }),
      (result.partialFailures && result.partialFailures.length) ? el('p', { className: 'hint', text: '部分行未写入：' + result.partialFailures.map(item => '第 ' + item.rowNo + ' 行 ' + item.reasonCode).join('；') }) : el('p', { className: 'hint', text: '重复确认同一批次会返回原结果，不会重复授予。' })
    ]));
    status('导入已确认。', 'success');
  } catch (error) { status(error.message || '确认失败', 'error'); toast(error.message || '确认失败'); }
}

async function download(path, filename) {
  try {
    const resp = await request(path, { method: 'GET' }, true);
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

function downloadErrors(batchId) {
  download('/api/orgs/' + encodeURIComponent(orgId) + '/imports/' + encodeURIComponent(batchId) + '/errors.csv', 'import-errors.csv');
}

$('previewBtn').onclick = preview;
$('downloadTemplate').onclick = () => download('/api/orgs/' + encodeURIComponent(orgId) + '/imports/template', 'campus-roster-template.csv');

boot();
