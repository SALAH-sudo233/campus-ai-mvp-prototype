(() => {
  const root = new URL('.', document.currentScript.src);
  const sessionKey = 'hust-campus-ai-demo-token-v2';
  const createNode = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const dialog = createNode('dialog');
  dialog.className = 'modal-box';
  dialog.style.maxHeight = '85vh';
  dialog.style.overflow = 'auto';
  dialog.setAttribute('aria-label', '我的 agent 工具包');
  const close = createNode('button', '关闭');
  close.className = 'ghost';
  close.onclick = () => dialog.close();
  const title = createNode('h3', '获取我的 agent 工具包');
  const notice = createNode('p', 'PAT 拥有你当前账户的完整权限。明文只显示一次，不会存入浏览器。');
  const status = createNode('p');
  status.setAttribute('role', 'status');
  const label = createNode('input');
  label.value = '我的 agent';
  label.maxLength = 80;
  label.setAttribute('aria-label', '令牌名称');
  const days = createNode('input');
  days.type = 'number';
  days.min = '1';
  days.max = '90';
  days.value = '30';
  days.setAttribute('aria-label', '有效期（天）');
  const mint = createNode('button', '签发新工具包');
  mint.className = 'primary';
  const secretArea = createNode('div');
  const tokenList = createNode('div');
  const base = createNode('p', 'CAMPUS_BASE_URL：' + root.href.replace(/\/$/, ''));
  const cli = createNode('a', '下载 campus-cli（Python 3 标准库，单文件）');
  cli.href = new URL('cli/campus-cli', root).href;
  cli.download = 'campus-cli';
  const skillLink = createNode('a', '下载 SKILL.md');
  skillLink.href = new URL('skills/campus-platform/SKILL.md', root).href;
  skillLink.download = 'SKILL.md';
  const skillText = createNode('pre');
  skillText.style.whiteSpace = 'pre-wrap';
  dialog.append(close, title, notice, label, createNode('span', ' 有效期（天）：'), days, mint,
    status, secretArea, base, cli, createNode('p'), skillLink, skillText,
    createNode('h4', '已有 PAT（只显示掩码）'), tokenList);
  document.body.append(dialog);
  let requestId = null;
  let generation = 0;

  async function request(path, options = {}) {
    const session = localStorage.getItem(sessionKey) || '';
    const response = await fetch(new URL(path, root), {
      ...options,
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + session }
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
  }

  async function loadTokens() {
    const data = await request('api/me/tokens');
    tokenList.replaceChildren();
    for (const item of data.items) {
      const row = createNode('p', item.label + ' · ' + item.hint + ' · ' + item.status +
        ' · 到期：' + new Date(item.expiresAt * 1000).toLocaleString());
      if (item.status === 'active') {
        const revoke = createNode('button', '撤销');
        revoke.className = 'ghost';
        revoke.onclick = async () => {
          if (!window.confirm('撤销后，使用此 PAT 的 agent 将立即失去访问权限。确定撤销？')) return;
          revoke.disabled = true;
          try {
            await request('api/me/tokens/' + encodeURIComponent(item.id), { method: 'DELETE' });
            secretArea.replaceChildren();
            await loadTokens();
          } catch (error) { status.textContent = error.message; }
          finally { revoke.disabled = false; }
        };
        row.append(revoke);
      }
      tokenList.append(row);
    }
    return data.items;
  }

  async function mintToken() {
    const current = generation;
    mint.disabled = true;
    status.textContent = '正在签发……';
    secretArea.replaceChildren();
    requestId = requestId || Array.from(crypto.getRandomValues(new Uint8Array(16)),
      value => value.toString(16).padStart(2, '0')).join('');
    try {
      const data = await request('api/me/tokens', {
        method: 'POST', body: JSON.stringify({ requestId, label: label.value, expiresInDays: Number(days.value) })
      });
      requestId = null;
      if (!dialog.open || current !== generation) return;
      if (data.token) {
        const plaintext = createNode('input');
        plaintext.value = data.token;
        plaintext.readOnly = true;
        plaintext.setAttribute('aria-label', 'PAT 明文（仅本次显示）');
        const copy = createNode('button', '复制 PAT');
        copy.onclick = async () => {
          try { await navigator.clipboard.writeText(plaintext.value); status.textContent = '已复制 PAT，请私下交给 agent。'; }
          catch (error) { status.textContent = '复制失败，请手动复制明文。'; }
        };
        secretArea.append(createNode('p', '只显示这一次，关闭窗口后无法找回：'), plaintext, copy);
        status.textContent = '签发成功。凭据只通过 CAMPUS_PAT 环境变量传给 CLI，不要放进参数或日志。';
      } else {
        status.textContent = '这是已完成请求的重放，不能再次返回明文。请先撤销对应 PAT，再签发新的工具包。';
      }
      await loadTokens();
    } catch (error) { status.textContent = error.message + '；可重试同一请求，不会重复签发。'; }
    finally { mint.disabled = false; }
  }

  mint.onclick = mintToken;
  dialog.addEventListener('close', () => { generation += 1; secretArea.replaceChildren(); });
  window.addEventListener('pagehide', () => { secretArea.replaceChildren(); });
  document.addEventListener('click', event => {
    if (event.target.closest('#logoutBtn, #teacherLogout') && dialog.open) dialog.close();
  }, true);
  for (const button of document.querySelectorAll('[data-agent-tools]')) {
    button.onclick = async () => {
      if (dialog.open) return;
      generation += 1;
      secretArea.replaceChildren();
      status.textContent = '正在读取……';
      dialog.showModal();
      try {
        const items = await loadTokens();
        const response = await fetch(new URL('skills/campus-platform/SKILL.md', root));
        if (!response.ok) throw new Error('SKILL 内容暂时无法读取');
        skillText.textContent = await response.text();
        status.textContent = '已有 PAT 只显示掩码；如需新的明文，请签发新工具包。';
        if (!items.length && dialog.open) await mintToken();
      } catch (error) { status.textContent = error.message; }
    };
  }
})();
