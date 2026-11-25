(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'className') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'dataset') Object.assign(node.dataset, value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
        else node.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of children || []) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      node.setAttribute(key === 'className' ? 'class' : key, String(value));
    }
    return node;
  }

  function reducedMotion() {
    return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function numberTicker(node, value, suffix) {
    const target = Number(value) || 0;
    if (reducedMotion()) { node.textContent = target.toLocaleString('zh-CN') + (suffix || ''); return; }
    const start = performance.now();
    const duration = 650;
    function frame(now) {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      node.textContent = Math.round(target * eased).toLocaleString('zh-CN') + (suffix || '');
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function MetricCard(data) {
    const value = el('strong', { className: 'metric-value' });
    if (typeof data.value === 'number' && Number.isFinite(data.value)) {
      numberTicker(value, data.value, data.unit || '');
    } else {
      value.textContent = data.value === null || data.value === undefined ? '—' : String(data.value);
    }
    return el('article', { className: 'stat-card ' + (data.tone || '') }, [
      el('span', { text: data.label }),
      value,
      el('small', { text: data.hint || '' })
    ]);
  }

  function RoleBadge(data, labels) {
    const map = {
      platform_admin: '平台管理员',
      org_admin: (labels && labels.admin) || '组织管理员',
      org_member: (labels && labels.member) || '组织用户'
    };
    const tone = data.role === 'platform_admin' ? 'platform' : data.role === 'org_admin' ? 'admin' : 'member';
    return el('span', { className: 'role-badge ' + tone, text: map[data.role] || data.role });
  }

  function WorkCard(work, options) {
    const opts = options || {};
    const badges = [];
    if (work.sourceKind === 'seed_demo') badges.push(el('span', { className: 'work-badge demo', text: '演示作品' }));
    if (work.featured) badges.push(el('span', { className: 'work-badge featured', text: '精选' }));
    if (work.status && work.status !== 'published' && opts.showStatus) {
      badges.push(el('span', { className: 'work-badge state', text: work.status }));
    }
    const actions = el('div', { className: 'work-actions' });
    actions.appendChild(el('button', {
      className: 'outline', text: '查看作品',
      onclick: () => opts.onOpen && opts.onOpen(work)
    }));
    for (const action of opts.actions || []) {
      actions.appendChild(el('button', {
        className: action.tone || 'ghost', text: action.label,
        onclick: () => action.onClick && action.onClick(work)
      }));
    }
    const coverImg = work.previewImages && work.previewImages.length ? work.previewImages[0] : '';
    return el('article', { className: 'work-card ' + (opts.density === 'compact' ? 'compact' : '') }, [
      el('div', { className: 'work-cover', 'aria-hidden': 'true' }, [
        coverImg ? el('img', { className: 'work-cover-img', src: coverImg, alt: '' }) : el('span', { text: (work.category || '作品').slice(0, 1) })
      ]),
      el('div', { className: 'work-body' }, [
        el('div', { className: 'work-badges' }, badges),
        el('h3', { text: work.title }),
        el('p', { className: 'muted', text: work.summary }),
        el('div', { className: 'work-meta' }, [
          el('span', { text: (opts.showAuthor === false ? '' : work.authorDisplayName || '') }),
          el('span', { text: work.visibilityLabel || '' })
        ]),
        actions
      ])
    ]);
  }

  function TrendChart(data) {
    const series = data.series || [];
    const width = 760, height = 240, pad = 34;
    const max = Math.max(1, ...series.map(item => Number(item.value) || 0));
    const total = series.reduce((sum, item) => sum + (Number(item.value) || 0), 0);
    const peak = series.reduce((best, item) => (Number(item.value) > Number(best.value) ? item : best), { date: '', value: 0 });
    const points = series.map((item, index) => {
      const x = pad + (series.length <= 1 ? 0 : index * (width - pad * 2) / (series.length - 1));
      const y = height - pad - (Number(item.value) || 0) / max * (height - pad * 2);
      return [x, y];
    });
    const line = points.map((point, index) => (index ? 'L' : 'M') + point[0].toFixed(1) + ',' + point[1].toFixed(1)).join(' ');
    const area = points.length ? line + ' L' + points[points.length - 1][0].toFixed(1) + ',' + (height - pad) +
      ' L' + points[0][0].toFixed(1) + ',' + (height - pad) + ' Z' : '';
    const chart = svg('svg', { viewBox: '0 0 ' + width + ' ' + height, role: 'img',
      'aria-label': '近 ' + (data.range ? data.range.days : series.length) + ' 天用量趋势，合计 ' + total + ' ' + (data.unit || '') });
    const defs = svg('defs', {});
    const gradient = svg('linearGradient', { id: 'trendFill', x1: '0', y1: '0', x2: '0', y2: '1' });
    gradient.appendChild(svg('stop', { offset: '0', className: 'trend-stop-top' }));
    gradient.appendChild(svg('stop', { offset: '1', className: 'trend-stop-bottom' }));
    defs.appendChild(gradient);
    chart.appendChild(defs);
    for (let i = 0; i <= 3; i += 1) {
      const y = pad + i * (height - pad * 2) / 3;
      chart.appendChild(svg('line', { x1: pad, y1: y, x2: width - pad, y2: y, className: 'trend-grid' }));
    }
    if (area) chart.appendChild(svg('path', { d: area, className: 'trend-area' }));
    if (line) chart.appendChild(svg('path', { d: line, className: 'trend-line' }));
    points.forEach((point, index) => {
      if (index % Math.max(1, Math.floor(points.length / 10)) !== 0 && index !== points.length - 1) return;
      const circle = svg('circle', { cx: point[0], cy: point[1], r: '3.2', className: 'trend-dot' });
      const title = svg('title', {});
      title.textContent = series[index].date + '：' + series[index].value + ' ' + (data.unit || '');
      circle.appendChild(title);
      chart.appendChild(circle);
    });
    const table = el('table', { className: 'chart-table' }, [
      el('thead', {}, [el('tr', {}, [el('th', { text: '日期' }), el('th', { text: data.unit || '数量' })])]),
      el('tbody', {}, series.map(item => el('tr', {}, [el('td', { text: item.date }), el('td', { text: String(item.value) })])))
    ]);
    return el('div', { className: 'trend-chart', tabindex: '0' }, [
      el('div', { className: 'chart-summary' }, [
        el('span', { text: '合计 ' + total.toLocaleString('zh-CN') + ' ' + (data.unit || '') }),
        el('span', { text: '峰值 ' + Number(peak.value).toLocaleString('zh-CN') + (peak.date ? '（' + peak.date + '）' : '') })
      ]),
      chart,
      data.truncated ? el('p', { className: 'hint', text: '请求范围超过 90 天，服务端已截断到近 90 天。' }) : null,
      el('details', { className: 'chart-details' }, [el('summary', { text: '查看数据表' }), table])
    ]);
  }

  function Heatmap(data) {
    const cells = data.cells || [];
    const max = Math.max(1, ...cells.map(item => Number(item.value) || 0));
    const grid = el('div', { className: 'heatmap', role: 'list', 'aria-label': '近 90 天活跃热力格' });
    for (const item of cells) {
      const level = item.value === 0 ? 0 : Math.min(4, Math.ceil(item.value / max * 4));
      const cell = el('span', { className: 'heat-cell level-' + level, role: 'listitem', tabindex: '0',
        title: item.date + '：' + item.value + ' ' + (data.unit || ''),
        'aria-label': item.date + '，' + item.value + ' ' + (data.unit || '') });
      grid.appendChild(cell);
    }
    return el('div', {}, [grid, el('div', { className: 'heat-legend' }, [
      el('span', { text: '低' }), el('i', { className: 'heat-cell level-0' }), el('i', { className: 'heat-cell level-1' }),
      el('i', { className: 'heat-cell level-2' }), el('i', { className: 'heat-cell level-3' }), el('i', { className: 'heat-cell level-4' }),
      el('span', { text: '高' })
    ])]);
  }

  function OrgTree(tree, options) {
    const opts = options || {};
    function memberRow(child, labels) {
      return el('li', { className: 'tree-member', tabindex: '0', onclick: () => opts.onSelect && opts.onSelect(child) }, [
        el('input', { type: 'checkbox', 'aria-label': '选择 ' + child.name, onclick: event => event.stopPropagation() }),
        el('span', { className: 'tree-name', text: child.name || '未命名成员' }),
        el('code', { text: child.studentId || '—' }),
        RoleBadge(child, labels),
        el('span', { className: 'muted', text: '余额 ' + Number(child.balance || 0).toLocaleString('zh-CN') }),
        el('span', { className: 'muted', text: child.keyHint || '无密钥' })
      ]);
    }
    function branch(node, depth, expanded) {
      const isSelectedOrg = node.kind === 'organization' && opts.selectedOrgId && node.id === opts.selectedOrgId;
      const details = el('details', { className: 'org-tree' + (depth ? ' nested' : '') + (isSelectedOrg ? ' is-selected' : '') });
      if (expanded || isSelectedOrg) details.setAttribute('open', '');
      const summary = el('summary', {
        onclick: node.kind === 'organization' && opts.onSelectOrg ? () => {
          if (node.id !== opts.selectedOrgId) opts.onSelectOrg(node.id);
        } : null
      }, [
        el('span', { className: 'tree-icon', text: '▾' }),
        el('strong', { text: node.name }),
        el('span', { className: 'muted', text: Number(node.count || 0) + ' 名成员' })
      ]);
      details.appendChild(summary);
      const labels = { admin: node.adminLabel || '组织管理员', member: node.memberLabel || '组织用户' };
      const list = el('ul', { className: 'tree-members' });
      for (const child of node.children || []) {
        if (child.children) {
          list.appendChild(el('li', { className: 'tree-branch' }, [branch(child, depth + 1, false)]));
        } else {
          list.appendChild(memberRow(child, labels));
        }
      }
      details.appendChild(list);
      return details;
    }
    return branch(tree, 0, true);
  }

  function ImportPreview(data, handlers) {
    const counts = data.counts || {};
    const wrap = el('div', { className: 'import-preview' }, [
      el('div', { className: 'import-counts' }, [
        MetricCard({ label: '可写入', value: counts.accepted || 0, tone: 'accent' }),
        MetricCard({ label: '跳过', value: counts.skipped || 0 }),
        MetricCard({ label: '冲突', value: counts.conflicts || 0 }),
        MetricCard({ label: '拒绝', value: counts.rejected || 0 })
      ])
    ]);
    const table = el('table', { className: 'data-table' }, [
      el('thead', {}, [el('tr', {}, ['行', '结果', '姓名', '学号', '邮箱', '角色', '原因'].map(label => el('th', { text: label })))]),
      el('tbody', {}, (data.rows || []).map(row => el('tr', {}, [
        el('td', { text: String(row.rowNo) }),
        el('td', {}, [el('span', { className: 'badge ' + row.result, text: row.result })]),
        el('td', { text: (row.fields && row.fields.name) || '' }),
        el('td', { text: (row.fields && row.fields.student_id) || '' }),
        el('td', { text: (row.fields && row.fields.email) || '' }),
        el('td', { text: (row.fields && row.fields.role) || '' }),
        el('td', { text: row.message || row.reasonCode || '' })
      ])))
    ]);
    wrap.appendChild(el('div', { className: 'table-scroll' }, [table]));
    const actions = el('div', { className: 'form-actions' });
    actions.appendChild(el('button', { className: 'primary', text: '确认导入', onclick: () => handlers.onConfirm(data) }));
    actions.appendChild(el('button', { className: 'outline', text: '下载错误报告', onclick: () => handlers.onDownload(data) }));
    actions.appendChild(el('button', { className: 'ghost', text: '取消', onclick: () => handlers.onCancel(data) }));
    wrap.appendChild(actions);
    return wrap;
  }


  function guard(handler) {
    let busy = false;
    return async function (event) {
      if (busy) {
        if (event && typeof event.preventDefault === 'function') event.preventDefault();
        return;
      }
      busy = true;
      const target = event && event.currentTarget;
      const canDisable = target && 'disabled' in target;
      if (canDisable) target.disabled = true;
      if (target && target.classList) target.classList.add('is-busy');
      try {
        return await handler.call(this, event);
      } finally {
        busy = false;
        if (canDisable) target.disabled = false;
        if (target && target.classList) target.classList.remove('is-busy');
      }
    };
  }

  window.UI = { el, svg, MetricCard, RoleBadge, WorkCard, TrendChart, Heatmap, OrgTree, ImportPreview, numberTicker, guard };
})();
