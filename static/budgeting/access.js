(() => {
  const controls = document.getElementById('access-controls');
  if (!controls) return;
  const byId = (id) => document.getElementById(id);
  const restart = byId('access-restart');
  const stop = byId('access-stop');
  const error = byId('access-error');
  let posting = false;
  let timer;
  let requestVersion = 0;
  const labels = {restarting: '正在重启', stopping: '正在停止', connecting: '正在连接', started: '运行中', stopped: '未启动', failed: '启动失败'};
  function showError(message) {
    error.textContent = message;
    error.hidden = !message;
  }
  function render(state) {
    const busy = Boolean(state.busy);
    const running = Boolean(state.running) && state.status === 'started' && !busy;
    const validUrl = /^https:\/\/[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$/.test(state.url || '');
    byId('access-badge').textContent = labels[state.status] || '未启动';
    byId('access-status').textContent = busy ? '公网服务正在处理，请稍候。此页面会自动更新。' : running ? '链接已生成。请先打开公网登录页，确认当前网络可以访问后再发给项目。' : '公网服务尚未就绪。本机预算系统可继续使用。';
    byId('access-link-area').hidden = !(running && validUrl);
    byId('access-url').value = running && validUrl ? state.url : '';
    byId('access-open').href = running && validUrl ? state.url + '/login/' : '#';
    restart.disabled = busy || posting;
    restart.textContent = busy ? '正在处理…' : running ? '重新生成公网链接' : '生成公网链接';
    stop.disabled = busy || posting || !running;
    showError(state.error || '');
  }
  async function readResponse(response) {
    if (response.status === 403 || response.redirected) throw new Error('登录已失效或不在本机入口，请在服务器电脑上重新登录。');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作失败，请稍后重试。');
    return data;
  }
  async function refresh() {
    if (posting) return;
    const version = ++requestVersion;
    try {
      const state = await readResponse(await fetch(controls.dataset.statusUrl, {cache: 'no-store'}));
      if (version === requestVersion) render(state);
    } catch (exception) {
      if (version !== requestVersion) return;
      byId('access-badge').textContent = '状态读取失败';
      byId('access-link-area').hidden = true;
      restart.disabled = true;
      stop.disabled = true;
      showError(exception.message);
    } finally {
      if (version === requestVersion) timer = window.setTimeout(refresh, 3000);
    }
  }
  byId('access-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (posting || !event.submitter || event.submitter.disabled) return;
    const action = event.submitter.value;
    posting = true;
    requestVersion += 1;
    window.clearTimeout(timer);
    restart.disabled = stop.disabled = true;
    byId('access-link-area').hidden = true;
    byId('access-status').textContent = '正在提交操作…';
    showError('');
    try {
      const body = new FormData(event.target);
      body.set('action', action);
      const state = await readResponse(await fetch(controls.dataset.actionUrl, {method: 'POST', body}));
      posting = false;
      render(state);
    } catch (exception) {
      showError(exception.message);
    } finally {
      posting = false;
      timer = window.setTimeout(refresh, 1500);
    }
  });
  byId('access-copy').addEventListener('click', async () => {
    const input = byId('access-url');
    input.select();
    try {
      await navigator.clipboard.writeText(input.value);
      byId('access-status').textContent = '链接已复制，可以发给各项目。';
    } catch {
      byId('access-status').textContent = '请复制已选中的链接（Ctrl+C 或 ⌘C）。';
    }
  });
  refresh();
})();
