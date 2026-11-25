const API = new URL('.', document.currentScript.src).pathname.replace(/\/$/, '');
const STORE_KEY = 'hust-campus-ai-demo-token-v2';
const $ = id => document.getElementById(id);
const el = UI.el;

let token = localStorage.getItem(STORE_KEY) || '';
let me = null;
let pendingReview = false;
let allWorks = [];
let myWorks = [];
let activeCategory = '全部';
let activeSort = 'hot';
let searchQuery = '';
let activeDetailWorkId = null;

let editingWorkId = null;
let formSwType = 'web';
let formPreviewImages = [];
let selectedAiTools = new Set();

const STATUS_LABELS = {
  pending_review: '待审核',
  published: '已发布',
  rejected: '已驳回',
  withdrawn: '已撤回',
  removed: '已下架'
};

const SW_META = {
  web: {
    label: 'Web 应用',
    badge: 'WEB',
    theme: 'cat-web',
    defaultPlatform: '浏览器',
    urlLabel: '网站访问地址（选填）',
    urlPlaceholder: 'https://example.com',
    prompt: `请帮我准备这个 Web 应用的发布产物，目标上传到「校园 AI 创作平台」托管成在线网站：
1. 将项目整理为纯静态网站（单文件 index.html 或根目录含 index.html 的 dist.zip，所有静态资源使用相对路径）。
2. 自检本地打开无报错、无 404 资源。
3. 给出：软件名称、一句话卖点（≤60字）、版本号与使用说明。`
  },
  app: {
    label: 'App',
    badge: 'APP',
    theme: 'cat-app',
    defaultPlatform: 'Android',
    urlLabel: 'App 下载 / TestFlight 链接（选填）',
    urlPlaceholder: 'https://testflight.apple.com/...',
    prompt: `请帮我准备这个 App 的测试发布包，目标上传到「校园 AI 创作平台」：
1. 检查应用名称与版本号，构建可安装测试包（.apk / .ipa / .hap 或打包 .zip，≤ 50 MB）。
2. 确认适用平台（Android / iOS / 鸿蒙）与安装步骤。
3. 给出：软件名称、一句话卖点（≤60字）、适用平台、版本号与使用说明。`
  },
  pc: {
    label: 'PC 软件',
    badge: 'PC',
    theme: 'cat-pc',
    defaultPlatform: 'macOS / Windows',
    urlLabel: 'PC 安装包 / Release 链接（选填）',
    urlPlaceholder: 'https://github.com/.../releases',
    prompt: `请帮我准备这个 PC 软件的发布安装包，目标上传到「校园 AI 创作平台」：
1. 打包出可直接运行或安装的桌面包（macOS .dmg/.zip、Windows .exe/.msi/.zip 或 Linux 包，≤ 50 MB），附简明 README。
2. 核对系统要求与首次运行步骤。
3. 给出：软件名称、一句话卖点（≤60字）、适用系统、版本号与运行说明。`
  }
};

function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.classList.add('show');
  setTimeout(() => node.classList.remove('show'), 2400);
}

function fmtSize(bytes) {
  const n = Number(bytes || 0);
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

async function api(path, options, raw) {
  const headers = Object.assign({}, (options && options.headers) || {});
  if (!raw && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  if (token) headers.Authorization = 'Bearer ' + token;
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (!resp.ok) {
    let data = {};
    try { data = await resp.json(); } catch (error) { data = {}; }
    throw Object.assign(new Error(data.error || '请求失败'), { status: resp.status, data });
  }
  return raw ? resp : resp.json();
}

function relativeTime(ts) {
  if (!ts) return '刚刚';
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)));
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  const days = Math.floor(diff / 86400);
  if (days < 30) return days + ' 天前';
  return new Date(Number(ts) * 1000).toLocaleDateString('zh-CN');
}

function swSpec(type) {
  return SW_META[type] || SW_META.web;
}

function deliveryBadgeText(work) {
  const sw = work.softwareType || 'web';
  if (sw === 'web') {
    if (work.hostedUrl) return '🌐 平台托管网站';
    if (work.externalUrl) return '🌐 在线网站';
    return '🌐 Web 应用';
  }
  const size = fmtSize(work.fileSize);
  const parts = ['📦 ' + (work.platform || swSpec(sw).defaultPlatform)];
  if (work.downloadUrl) parts.push('可下载安装包' + (size ? ' · ' + size : ''));
  else if (work.externalUrl) parts.push('外部下载');
  return parts.join(' · ');
}

function openModal(id) { $(id).classList.remove('hidden'); }
function closeModal(id) {
  $(id).classList.add('hidden');
  if (id === 'workDetailModal') activeDetailWorkId = null;
}

function denied() {
  $('studentNav').classList.add('hidden');
  $('teacherNav').classList.add('hidden');
  $('galleryLogout').classList.add('hidden');
  $('galleryMain').classList.add('hidden');
  $('galleryDenied').classList.remove('hidden');
}

function filteredWorks() {
  let list = allWorks.slice();
  if (activeCategory !== '全部') list = list.filter(item => item.category === activeCategory);
  if (activeSort === 'featured') list = list.filter(item => item.featured);
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    list = list.filter(item =>
      (item.title || '').toLowerCase().includes(q) ||
      (item.summary || '').toLowerCase().includes(q) ||
      (item.authorDisplayName || '').toLowerCase().includes(q) ||
      (item.category || '').toLowerCase().includes(q) ||
      (item.platform || '').toLowerCase().includes(q)
    );
  }
  list.sort((a, b) => {
    if (activeSort === 'newest') return Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    if (activeSort === 'featured') return (b.upvoteCount || 0) - (a.upvoteCount || 0) || Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
    return (b.upvoteCount || 0) - (a.upvoteCount || 0) || (b.featured ? 1 : 0) - (a.featured ? 1 : 0) || Number(b.updatedAt || 0) - Number(a.updatedAt || 0);
  });
  return list;
}

function renderCategories() {
  const wrap = $('phCategories');
  const cats = ['全部', 'Web 应用', 'App', 'PC 软件'];
  wrap.replaceChildren(...cats.map(cat => el('button', {
    className: 'ph-cat' + (activeCategory === cat ? ' active' : ''),
    text: cat,
    onclick: () => { activeCategory = cat; renderCategories(); renderFeed(); }
  })));
}

function renderFeed() {
  const list = filteredWorks();
  const headingMap = { hot: '热门作品榜', featured: '教师精选榜', newest: '最新发布榜' };
  const metaMap = { hot: '按点赞数实时排序', featured: '仅展示本组织教师精选软件', newest: '按发布时间倒序排列' };
  $('phFeedHeading').textContent = headingMap[activeSort] || '作品榜';
  $('phFeedMeta').textContent = metaMap[activeSort] || '';
  $('phFeedCount').textContent = '共 ' + list.length + ' 款软件';

  const wrap = $('phFeedList');
  wrap.replaceChildren();
  if (!list.length) {
    const msg = activeSort === 'featured'
      ? '当前还没有被设为精选的软件，可切回“热门”浏览全部软件。'
      : '没有找到匹配的软件，换个分类或关键词试试。';
    wrap.appendChild(el('div', { className: 'ph-empty' }, [el('p', { className: 'muted', text: msg })]));
    return;
  }

  list.forEach((work, index) => {
    const rank = index + 1;
    const rankClass = rank <= 3 ? 'ph-rank top-' + rank : 'ph-rank';
    const spec = swSpec(work.softwareType);
    const badges = [el('span', { className: 'work-badge sw-badge ' + spec.theme, text: spec.label })];
    if (work.version) badges.push(el('span', { className: 'work-badge state', text: work.version }));
    if (work.featured) badges.push(el('span', { className: 'work-badge featured', text: '★ 精选' }));
    if (work.sourceKind === 'seed_demo') badges.push(el('span', { className: 'work-badge demo', text: '示例' }));

    const upvoteBtn = el('button', {
      className: 'ph-upvote' + (work.upvoted ? ' is-upvoted' : ''),
      'aria-label': (work.upvoted ? '取消点赞 ' : '点赞 ') + work.title,
      'aria-pressed': work.upvoted ? 'true' : 'false',
      onclick: event => { event.stopPropagation(); toggleUpvote(work.workId); }
    }, [
      el('span', { className: 'ph-arrow', text: '▲' }),
      el('strong', { text: String(work.upvoteCount || 0) })
    ]);

    const row = el('article', {
      className: 'ph-item',
      tabindex: '0',
      onclick: () => openDetail(work.workId),
      onkeydown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openDetail(work.workId); } }
    }, [
      el('span', { className: rankClass, text: String(rank) }),
      el('div', { className: 'ph-icon ' + spec.theme, 'aria-hidden': 'true' }, [
        (work.previewImages && work.previewImages.length)
          ? el('img', { className: 'ph-icon-img', src: API + '/' + work.previewImages[0], alt: '' })
          : el('span', { text: spec.badge })
      ]),
      el('div', { className: 'ph-info' }, [
        el('div', { className: 'ph-title-line' }, [
          el('h4', { text: work.title }),
          el('div', { className: 'work-badges' }, badges)
        ]),
        el('p', { className: 'ph-tagline', text: work.summary || '' }),
        el('div', { className: 'ph-meta' }, [
          el('span', { className: 'ph-tag', text: deliveryBadgeText(work) }),
          el('span', { text: 'by ' + (work.authorDisplayName || '匿名') }),
          el('span', { text: '💬 ' + (work.commentCount || 0) + ' 条讨论' }),
          el('span', { text: relativeTime(work.updatedAt) })
        ])
      ]),
      upvoteBtn
    ]);
    wrap.appendChild(row);
  });
}

function renderSidebar() {
  const top3 = allWorks.slice().sort((a, b) => (b.upvoteCount || 0) - (a.upvoteCount || 0)).slice(0, 3);
  const spot = $('phTopSpotlight');
  spot.replaceChildren();
  if (!top3.length) {
    spot.appendChild(el('p', { className: 'muted', text: '暂无上榜软件' }));
  } else {
    top3.forEach((work, i) => {
      spot.appendChild(el('button', {
        className: 'ph-spot-item',
        onclick: () => openDetail(work.workId)
      }, [
        el('span', { className: 'ph-spot-rank top-' + (i + 1), text: String(i + 1) }),
        el('div', { className: 'ph-spot-body' }, [
          el('b', { text: work.title }),
          el('small', { text: work.category + ' · 💬 ' + (work.commentCount || 0) })
        ]),
        el('span', { className: 'ph-spot-votes' + (work.upvoted ? ' is-upvoted' : ''), text: '▲ ' + (work.upvoteCount || 0) })
      ]));
    });
  }

  if (me && (me.role === 'org_admin' || me.role === 'platform_admin')) {
    const totalUpvotes = allWorks.reduce((s, w) => s + Number(w.upvoteCount || 0), 0);
    const totalComments = allWorks.reduce((s, w) => s + Number(w.commentCount || 0), 0);
    const featuredCount = allWorks.filter(w => w.featured).length;
    $('teacherGalleryStats').replaceChildren(
      el('div', { className: 'ph-mini-stat' }, [el('strong', { text: String(allWorks.length) }), el('span', { text: '已发布软件' })]),
      el('div', { className: 'ph-mini-stat' }, [el('strong', { text: String(featuredCount) }), el('span', { text: '教师精选' })]),
      el('div', { className: 'ph-mini-stat' }, [el('strong', { text: String(totalUpvotes) }), el('span', { text: '累计点赞' })]),
      el('div', { className: 'ph-mini-stat' }, [el('strong', { text: String(totalComments) }), el('span', { text: '累计讨论' })])
    );
  }

  if (me && me.role === 'org_member') {
    const wrap = $('myWorks');
    wrap.replaceChildren();
    if (!myWorks.length) {
      wrap.appendChild(el('p', { className: 'muted ph-my-empty', text: '你还没有发布软件。用 Token 在本地开发完 Web / App / PC 作品后，点右上角即可上传发布。' }));
    } else {
      for (const work of myWorks) {
        const tone = work.status === 'published' ? 'active' : work.status === 'pending_review' ? 'skipped' : 'none';
        const actions = [];
        if (work.status !== 'removed') {
          actions.push(el('button', { className: 'ghost compact-btn', text: '编辑', onclick: () => openEditModal(work) }));
        }
        if (['pending_review', 'published', 'rejected'].includes(work.status)) {
          actions.push(el('button', { className: 'danger compact-btn', text: '撤回', onclick: () => withdrawWork(work) }));
        }
        wrap.appendChild(el('div', { className: 'ph-my-row' }, [
          el('div', { className: 'ph-my-info' }, [
            el('b', { text: work.title }),
            el('div', { className: 'ph-my-meta' }, [
              el('span', { className: 'badge ' + tone, text: STATUS_LABELS[work.status] || work.status }),
              el('small', { text: work.category + ' · ' + (work.version || 'v1.0.0') })
            ])
          ]),
          el('div', { className: 'ph-my-actions inline-actions' }, actions)
        ]));
      }
    }
  }
}

async function loadWorks() {
  if (!token || pendingReview) return;
  const isStudent = me && me.role === 'org_member';
  try {
    const tasks = [api('/api/works')];
    if (isStudent) tasks.push(api('/api/works/mine'));
    const [gallery, mine] = await Promise.all(tasks);
    allWorks = gallery.items || [];
    myWorks = (mine && mine.items) || [];
    renderCategories();
    renderFeed();
    renderSidebar();
  } catch (error) {
    if (error.status !== 401) toast(error.message || '作品列表暂时不可用');
  }
}

async function toggleUpvote(workId) {
  if (pendingReview) { toast('账户待复核，暂不可点赞'); return; }
  const target = allWorks.find(w => w.workId === workId);
  if (!target) return;
  try {
    const res = await api('/api/works/' + encodeURIComponent(workId) + '/upvote', { method: 'POST', body: '{}' });
    target.upvoted = !!res.upvoted;
    target.upvoteCount = Number(res.upvoteCount || 0);
    renderFeed();
    renderSidebar();
    if (activeDetailWorkId === workId) syncDetailUpvote(target);
  } catch (error) {
    toast(error.message || '点赞操作失败');
  }
}

function syncDetailUpvote(work) {
  const btn = $('detailUpvoteBtn');
  btn.classList.toggle('is-upvoted', !!work.upvoted);
  $('detailUpvoteLabel').textContent = work.upvoted ? '已点赞' : '点赞';
  $('detailUpvoteCount').textContent = String(work.upvoteCount || 0);
}

async function downloadPackage(work) {
  try {
    const resp = await api('/api/works/' + encodeURIComponent(work.workId) + '/download', { method: 'GET' }, true);
    const blob = await resp.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = work.fileName || (work.title + '.zip');
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1500);
    toast('开始下载 ' + (work.fileName || '安装包'));
  } catch (error) { toast(error.message || '下载失败'); }
}

async function openDetail(workId) {
  const work = allWorks.find(w => w.workId === workId);
  if (!work) return;
  activeDetailWorkId = workId;
  const spec = swSpec(work.softwareType);
  $('detailIcon').className = 'ph-icon ph-icon-lg ' + spec.theme;
  if (work.previewImages && work.previewImages.length) {
    $('detailIcon').replaceChildren(el('img', { className: 'ph-icon-img', src: API + '/' + work.previewImages[0], alt: work.title }));
  } else {
    $('detailIcon').textContent = spec.badge;
  }
  $('detailTitle').textContent = work.title;
  $('detailSummary').textContent = work.summary || '';
  const badges = [el('span', { className: 'work-badge sw-badge ' + spec.theme, text: spec.label })];
  if (work.version) badges.push(el('span', { className: 'work-badge state', text: work.version }));
  if (work.featured) badges.push(el('span', { className: 'work-badge featured', text: '★ 精选' }));
  if (work.sourceKind === 'seed_demo') badges.push(el('span', { className: 'work-badge demo', text: '平台示例' }));
  else badges.push(el('span', { className: 'work-badge', text: '本组织作品' }));
  $('detailBadges').replaceChildren(...badges);

  const metaNodes = [
    el('span', { className: 'ph-tag', text: '平台：' + (work.platform || spec.defaultPlatform) }),
    el('span', { text: '作者：' + (work.authorDisplayName || '匿名') }),
    el('span', { text: '更新于 ' + relativeTime(work.updatedAt) })
  ];
  if (work.aiTools && work.aiTools.length) {
    metaNodes.push(el('span', { text: 'AI 工具：' + work.aiTools.join(' / ') }));
  }
  $('detailMeta').replaceChildren(...metaNodes);

  const launchBtn = $('detailLaunchBtn');
  if (work.softwareType === 'web') {
    const webUrl = work.hostedUrl ? (API + '/' + work.hostedUrl) : work.externalUrl;
    if (webUrl) {
      launchBtn.textContent = '🌐 访问网站 ↗';
      launchBtn.disabled = false;
      launchBtn.onclick = () => window.open(webUrl, '_blank', 'noopener,noreferrer');
    } else {
      launchBtn.textContent = '暂无网址';
      launchBtn.disabled = true;
      launchBtn.onclick = null;
    }
  } else {
    if (work.downloadUrl) {
      const sz = fmtSize(work.fileSize);
      launchBtn.textContent = '📦 下载安装包' + (sz ? ' (' + sz + ')' : '');
      launchBtn.disabled = false;
      launchBtn.onclick = () => downloadPackage(work);
    } else if (work.externalUrl) {
      launchBtn.textContent = '🔗 打开下载页 ↗';
      launchBtn.disabled = false;
      launchBtn.onclick = () => window.open(work.externalUrl, '_blank', 'noopener,noreferrer');
    } else {
      launchBtn.textContent = '暂无安装包';
      launchBtn.disabled = true;
      launchBtn.onclick = null;
    }
  }

  const aboutParts = [];
  if (work.softwareType === 'web' && work.hostedUrl) {
    aboutParts.push('交付形式：已通过平台静态托管为可访问网站（沙箱隔离运行）。');
  } else if (work.downloadUrl) {
    aboutParts.push('交付安装包：' + (work.fileName || '安装包') + (work.fileSize ? '（' + fmtSize(work.fileSize) + '）' : '') + '，适用平台：' + (work.platform || spec.defaultPlatform) + '。');
  } else if (work.externalUrl) {
    aboutParts.push('外部访问地址：' + work.externalUrl);
  }
  aboutParts.push(work.sourceKind === 'seed_demo'
    ? '这是平台内置软件示例，用来演示 Web 托管网站与 App / PC 安装包的发布体验。'
    : '该软件由本组织同学自主使用 Token 开发并发布，仅在本组织作品广场内展示交流。');
  const shotsWrap = $('detailShots');
  if (work.previewImages && work.previewImages.length) {
    shotsWrap.classList.remove('hidden');
    shotsWrap.replaceChildren(...work.previewImages.map((src, idx) =>
      el('a', { className: 'ph-detail-shot', href: API + '/' + src, target: '_blank', rel: 'noopener noreferrer' }, [
        el('img', { src: API + '/' + src, alt: work.title + ' 预览图 ' + (idx + 1) })
      ])
    ));
  } else {
    shotsWrap.classList.add('hidden');
    shotsWrap.replaceChildren();
  }
  $('detailAbout').textContent = aboutParts.join(' ');

  syncDetailUpvote(work);
  $('detailUpvoteBtn').onclick = () => toggleUpvote(work.workId);
  $('commentInput').value = '';
  $('detailCommentCount').textContent = '(' + (work.commentCount || 0) + ')';
  $('detailCommentsList').replaceChildren(el('p', { className: 'muted', text: '正在加载讨论...' }));
  openModal('workDetailModal');
  await loadComments(workId);
}

async function loadComments(workId) {
  try {
    const data = await api('/api/works/' + encodeURIComponent(workId) + '/comments');
    renderComments(data.items || []);
  } catch (error) {
    $('detailCommentsList').replaceChildren(el('p', { className: 'muted', text: error.message || '评论加载失败' }));
  }
}

function renderComments(items) {
  $('detailCommentCount').textContent = '(' + items.length + ')';
  const wrap = $('detailCommentsList');
  wrap.replaceChildren();
  if (!items.length) {
    wrap.appendChild(el('p', { className: 'muted ph-comment-empty', text: '还没有讨论，来发表第一条评论吧。' }));
    return;
  }
  for (const item of items) {
    const roleLabel = item.authorRole === 'org_admin' ? '教师' : item.authorRole === 'platform_admin' ? '管理员' : '学生';
    const roleTone = item.authorRole === 'org_admin' ? 'admin' : item.authorRole === 'platform_admin' ? 'platform' : 'member';
    wrap.appendChild(el('div', { className: 'ph-comment' }, [
      el('div', { className: 'ph-comment-avatar', text: (item.authorName || '评').slice(0, 1) }),
      el('div', { className: 'ph-comment-body' }, [
        el('div', { className: 'ph-comment-head' }, [
          el('b', { text: item.authorName || '成员' }),
          el('span', { className: 'role-badge ' + roleTone, text: roleLabel }),
          el('small', { className: 'muted', text: relativeTime(item.createdAt) })
        ]),
        el('p', { text: item.body })
      ])
    ]));
  }
}

async function submitComment() {
  if (!activeDetailWorkId) return;
  const body = $('commentInput').value.trim();
  if (!body) { toast('请输入评论内容'); return; }
  try {
    const res = await api('/api/works/' + encodeURIComponent(activeDetailWorkId) + '/comments', {
      method: 'POST', body: JSON.stringify({ body })
    });
    $('commentInput').value = '';
    const work = allWorks.find(w => w.workId === activeDetailWorkId);
    if (work) work.commentCount = Number(res.commentCount || (work.commentCount || 0) + 1);
    await loadComments(activeDetailWorkId);
    renderFeed();
    renderSidebar();
    toast('评论已发表');
  } catch (error) { toast(error.message || '发表失败'); }
}

async function withdrawWork(work) {
  if (!confirm('确认撤回《' + work.title + '》？撤回后将从广场移除，后续仍可重新编辑发布。')) return;
  try {
    await api('/api/works/' + encodeURIComponent(work.workId) + '/withdraw', { method: 'POST', body: '{}' });
    await loadWorks();
    toast('作品已撤回');
  } catch (error) { toast(error.message || '撤回失败'); }
}

function renderPreviewImages() {
  const grid = $('previewImageGrid');
  grid.replaceChildren();
  formPreviewImages.forEach((url, idx) => {
    grid.appendChild(el('div', { className: 'shot-tile' }, [
      el('img', { src: API + '/' + url, alt: '预览图 ' + (idx + 1) }),
      idx === 0 ? el('span', { className: 'shot-cover-tag', text: '封面' }) : null,
      el('button', {
        type: 'button',
        className: 'shot-remove',
        'aria-label': '移除第 ' + (idx + 1) + ' 张预览图',
        text: '×',
        onclick: () => { formPreviewImages.splice(idx, 1); renderPreviewImages(); renderLivePreview(); }
      })
    ]));
  });
  if (formPreviewImages.length < 4) {
    grid.appendChild(el('button', {
      type: 'button',
      className: 'shot-add',
      id: 'addPreviewImageBtn',
      onclick: () => $('previewImageInput').click()
    }, [
      el('span', { className: 'shot-add-plus', text: '＋' }),
      el('small', { text: formPreviewImages.length ? '继续添加 (' + formPreviewImages.length + '/4)' : '添加预览图片' })
    ]));
  }
}

async function handlePreviewImageFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const remain = 4 - formPreviewImages.length;
  if (remain <= 0) { toast('最多添加 4 张预览图片'); return; }
  for (const file of files.slice(0, remain)) {
    if (file.size > 8 * 1024 * 1024) { toast(file.name + ' 超过 8 MB 限制'); continue; }
    try {
      const data = await api('/api/works/preview-image', {
        method: 'POST',
        body: file,
        headers: { 'Content-Type': file.type || 'application/octet-stream' }
      }, true).then(r => r.json());
      if (data.url && !formPreviewImages.includes(data.url)) formPreviewImages.push(data.url);
    } catch (error) {
      toast(error.message || '图片上传失败');
    }
  }
  $('previewImageInput').value = '';
  renderPreviewImages();
  renderLivePreview();
}

function setFormSwType(type, keepPlatform) {
  formSwType = SW_META[type] ? type : 'web';
  const spec = swSpec(formSwType);
  document.querySelectorAll('[data-sw-type]').forEach(btn => btn.classList.toggle('active', btn.dataset.swType === formSwType));
  $('modalPromptTypeLabel').textContent = spec.label;
  $('modalPromptPre').textContent = spec.prompt;
  $('externalUrlLabel').textContent = spec.urlLabel;
  $('workExternalUrl').placeholder = spec.urlPlaceholder;
  if (!keepPlatform) $('workPlatform').value = spec.defaultPlatform;
  renderLivePreview();
}

function renderLivePreview() {
  const spec = swSpec(formSwType);
  const title = $('workTitle').value.trim() || '你的软件名称';
  const summary = $('workSummary').value.trim() || '这里会显示你填写的一句话卖点，让同学一眼看懂它解决什么问题。';
  const version = $('workVersion').value.trim() || 'v1.0.0';
  const platform = $('workPlatform').value.trim() || spec.defaultPlatform;
  const mockWork = {
    softwareType: formSwType,
    platform,
    externalUrl: $('workExternalUrl').value.trim()
  };
  const iconChild = formPreviewImages.length
    ? el('img', { className: 'ph-icon-img', src: API + '/' + formPreviewImages[0], alt: '' })
    : el('span', { text: spec.badge });
  $('liveCardPreview').replaceChildren(el('div', { className: 'ph-item preview-item' }, [
    el('span', { className: 'ph-rank', text: 'NEW' }),
    el('div', { className: 'ph-icon ' + spec.theme }, [iconChild]),
    el('div', { className: 'ph-info' }, [
      el('div', { className: 'ph-title-line' }, [
        el('h4', { text: title }),
        el('div', { className: 'work-badges' }, [
          el('span', { className: 'work-badge sw-badge ' + spec.theme, text: spec.label }),
          el('span', { className: 'work-badge state', text: version })
        ])
      ]),
      el('p', { className: 'ph-tagline', text: summary }),
      el('div', { className: 'ph-meta' }, [
        el('span', { className: 'ph-tag', text: deliveryBadgeText(mockWork) }),
        el('span', { text: 'by ' + ((me && me.identity) || '我') })
      ])
    ]),
    el('div', { className: 'ph-upvote' }, [el('span', { className: 'ph-arrow', text: '▲' }), el('strong', { text: '1' })])
  ]));
}

function openCreateModal(initialType) {
  if (pendingReview) return;
  editingWorkId = null;
  formPreviewImages = [];
  selectedAiTools = new Set();
  $('submitModalTitle').textContent = '发布软件作品';
  $('submitWorkConfirm').textContent = '立即发布上榜';
  $('workTitle').value = '';
  $('workSummary').value = '';
  $('workVersion').value = 'v1.0.0';
  $('workExternalUrl').value = '';
  $('previewImageInput').value = '';
  document.querySelectorAll('#aiToolChips .chip').forEach(c => c.classList.remove('active'));
  renderPreviewImages();
  setFormSwType(initialType || 'web', false);
  openModal('submitWorkModal');
  $('workTitle').focus();
}

function openEditModal(work) {
  editingWorkId = work.workId;
  formPreviewImages = Array.isArray(work.previewImages) ? work.previewImages.slice(0, 4) : [];
  selectedAiTools = new Set(work.aiTools || []);
  $('submitModalTitle').textContent = '编辑软件作品';
  $('submitWorkConfirm').textContent = '保存并发布';
  $('workTitle').value = work.title || '';
  $('workSummary').value = work.summary || '';
  $('workVersion').value = work.version || 'v1.0.0';
  $('workPlatform').value = work.platform || swSpec(work.softwareType).defaultPlatform;
  $('workExternalUrl').value = work.externalUrl || '';
  $('previewImageInput').value = '';
  document.querySelectorAll('#aiToolChips .chip').forEach(c => c.classList.toggle('active', selectedAiTools.has(c.dataset.tool)));
  renderPreviewImages();
  setFormSwType(work.softwareType || 'web', true);
  renderLivePreview();
  openModal('submitWorkModal');
}

async function submitWorkForm() {
  const title = $('workTitle').value.trim();
  const summary = $('workSummary').value.trim();
  const version = $('workVersion').value.trim() || 'v1.0.0';
  const platform = $('workPlatform').value.trim() || swSpec(formSwType).defaultPlatform;
  const externalUrl = $('workExternalUrl').value.trim();
  if (!title || !summary) { toast('请填写软件名称和一句话卖点'); return; }
  const payload = {
    title, summary, softwareType: formSwType, version, platform,
    aiTools: Array.from(selectedAiTools),
    externalUrl,
    previewImages: formPreviewImages
  };
  try {
    if (editingWorkId) {
      await api('/api/works/' + encodeURIComponent(editingWorkId), { method: 'PATCH', body: JSON.stringify(payload) });
      toast('作品已更新');
    } else {
      await api('/api/works', { method: 'POST', body: JSON.stringify(payload) });
      toast('发布成功，已登上作品广场！');
    }
    closeModal('submitWorkModal');
    await loadWorks();
  } catch (error) { toast(error.message || '提交失败'); }
}

async function boot() {
  fetch(API + '/api/config').then(resp => resp.json()).then(config => {
    for (const node of document.querySelectorAll('[data-product-name]')) node.textContent = config.productName;
    document.title = config.productName + ' · 作品广场';
  }).catch(() => {});

  if (!token) { denied(); return; }
  try {
    me = await api('/api/me');
  } catch (error) {
    token = '';
    localStorage.removeItem(STORE_KEY);
    denied();
    return;
  }

  const isTeacher = me.role === 'org_admin' || me.role === 'platform_admin';
  const isStudent = me.role === 'org_member';
  pendingReview = me.admissionStatus === 'pending_review';

  $('galleryDenied').classList.add('hidden');
  $('galleryMain').classList.remove('hidden');
  $('galleryLogout').classList.remove('hidden');

  if (isTeacher) {
    $('galleryRoleTag').classList.remove('hidden');
    $('teacherNav').classList.remove('hidden');
    $('studentNav').classList.add('hidden');
    $('galleryUser').textContent = (me.org && me.org.name) || me.identity || '';
    $('galleryLogout').textContent = '退出';
    if (me.org && me.org.id) $('teacherMembersLink').href = 'import.html?org=' + encodeURIComponent(me.org.id);
    $('submitWorkBtn').classList.add('hidden');
    $('myWorksSection').classList.add('hidden');
    $('teacherSideCard').classList.remove('hidden');
  } else if (isStudent) {
    $('galleryRoleTag').classList.add('hidden');
    $('studentNav').classList.remove('hidden');
    $('teacherNav').classList.add('hidden');
    $('galleryUser').textContent = me.identity || '';
    $('galleryLogout').textContent = '退出登录';
    $('submitWorkBtn').classList.remove('hidden');
    $('myWorksSection').classList.remove('hidden');
    $('teacherSideCard').classList.add('hidden');
    if (pendingReview) {
      $('pendingBanner').classList.remove('hidden');
      $('submitWorkBtn').disabled = true;
      $('submitWorkBtn').classList.add('is-disabled');
      $('sideSubmitBtn').disabled = true;
    } else {
      const params = new URLSearchParams(location.search);
      if (params.get('submit') === '1') openCreateModal(params.get('type') || 'web');
    }
  } else {
    denied();
    return;
  }

  await loadWorks();
}

document.querySelectorAll('.ph-sort').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.ph-sort').forEach(n => n.classList.toggle('active', n === btn));
  activeSort = btn.dataset.sort;
  renderFeed();
}));
$('phSearch').addEventListener('input', event => { searchQuery = event.target.value.trim(); renderFeed(); });

$('submitWorkBtn').onclick = () => openCreateModal('web');
$('sideSubmitBtn').onclick = () => openCreateModal('web');
document.querySelectorAll('[data-sw-type]').forEach(btn => btn.addEventListener('click', () => setFormSwType(btn.dataset.swType, false)));
document.querySelectorAll('#aiToolChips .chip').forEach(chip => chip.addEventListener('click', () => {
  const t = chip.dataset.tool;
  if (selectedAiTools.has(t)) selectedAiTools.delete(t); else selectedAiTools.add(t);
  chip.classList.toggle('active', selectedAiTools.has(t));
}));
$('modalCopyPromptBtn').onclick = async () => {
  try { await navigator.clipboard.writeText($('modalPromptPre').textContent); toast('发布 Prompt 已复制'); }
  catch (error) { toast('复制失败，请手动选择文本'); }
};
$('previewImageInput').addEventListener('change', event => handlePreviewImageFiles(event.target.files));
['workTitle', 'workSummary', 'workVersion', 'workPlatform', 'workExternalUrl'].forEach(id => {
  $(id).addEventListener('input', renderLivePreview);
});
$('submitWorkConfirm').onclick = UI.guard(submitWorkForm);

$('commentSubmitBtn').onclick = UI.guard(submitComment);
$('commentInput').addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') submitComment();
});

document.querySelectorAll('[data-close-modal]').forEach(btn => btn.addEventListener('click', () => closeModal(btn.dataset.closeModal)));
document.querySelectorAll('.modal').forEach(modal => modal.addEventListener('click', event => {
  if (event.target === modal) closeModal(modal.id);
}));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('.modal:not(.hidden)').forEach(m => closeModal(m.id));
});

$('galleryLogout').onclick = async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch (error) { /* ignore */ }
  token = '';
  localStorage.removeItem(STORE_KEY);
  window.location.href = 'index.html';
};
$('galleryBackLogin').onclick = () => { localStorage.removeItem(STORE_KEY); };

boot();
