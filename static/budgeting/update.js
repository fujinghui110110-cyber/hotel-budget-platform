(() => {
  const controls = document.getElementById('update-controls');
  if (!controls) return;
  const byId = id => document.getElementById(id);
  let posting = false, timer, generation = 0, restarting = false, actionError = '';
  const buttons = ['update-check', 'update-install', 'update-save'];
  const showError = message => { byId('update-error').textContent = message; byId('update-error').hidden = !message; };
  function disable() { buttons.forEach(id => { byId(id).disabled = true; }); }
  function render(state) {
    restarting = Boolean(state.busy);
    byId('update-current').textContent = state.current_version || '未标记版本';
    byId('update-available').textContent = state.available_version || '尚未检查';
    byId('update-badge').textContent = state.busy ? '更新中' : state.error ? '需要处理' : state.update_available ? '有新版本' : '就绪';
    byId('update-status').textContent = state.message || (state.busy ? '正在更新，请勿关闭服务器。' : '点击检查更新，获取最新发布版本。');
    byId('update-configured').textContent = state.configured ? '已配置下载凭据，可按需更换。' : '尚未配置下载凭据。';
    byId('update-notes').textContent = state.release_notes || '';
    byId('update-notes-wrap').hidden = !state.release_notes;
    byId('update-check').disabled = posting || state.busy || !state.configured;
    byId('update-install').disabled = posting || state.busy || !state.update_available || !state.configured;
    byId('update-save').disabled = posting || state.busy;
    showError(state.error || actionError);
  }
  async function read(response) {
    if (response.status === 403 || response.redirected) throw new Error('登录已失效或不在本机入口，请在服务器电脑上重新登录。');
    const state = await response.json();
    if (!response.ok) throw new Error(state.error || '操作失败，请稍后重试。');
    return state;
  }
  async function refresh() {
    if (posting) return;
    const version = ++generation;
    try {
      const state = await read(await fetch(controls.dataset.statusUrl, {cache: 'no-store'}));
      if (version === generation) render(state);
    } catch (error) {
      if (version !== generation) return;
      disable();
      byId('update-badge').textContent = restarting ? '重启中' : '连接中断';
      byId('update-status').textContent = restarting ? '服务器正在重启，页面将自动重连，请稍候…' : '暂时无法读取更新状态，正在自动重试…';
      showError(error.message);
    } finally { if (version === generation) timer = window.setTimeout(refresh, 3000); }
  }
  async function submit(event, configure) {
    event.preventDefault();
    if (posting || (!configure && (!event.submitter || event.submitter.disabled))) return;
    posting = true; actionError = ''; generation += 1; window.clearTimeout(timer); disable(); showError('');
    const action = configure ? 'configure' : event.submitter.value;
    byId('update-status').textContent = action === 'check' ? '正在检查 GitHub 发布版本…' : action === 'install' ? '正在提交更新，随后将自动重启…' : '正在保存下载凭据…';
    try {
      const body = new FormData(event.target); body.set('action', action);
      if (configure) byId('update-token').value = '';
      const state = await read(await fetch(controls.dataset.actionUrl, {method: 'POST', body}));
      posting = false; render(state);
    } catch (error) {
      if (action === 'install') restarting = true;
      actionError = error.message;
      byId('update-status').textContent = '操作未完成，请按提示处理后重试。';
      showError(error.message);
    } finally { posting = false; timer = window.setTimeout(refresh, 1500); }
  }
  byId('update-form').addEventListener('submit', event => submit(event, false));
  byId('update-config-form').addEventListener('submit', event => submit(event, true));
  refresh();
})();
