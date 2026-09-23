(() => {
  const controls = document.getElementById('update-controls');
  if (!controls) return;
  const byId = id => document.getElementById(id);
  let posting = false, timer, generation = 0, restarting = false, actionError = '', known = false, lastState = {};
  const showError = message => { byId('update-error').textContent = message; byId('update-error').hidden = !message; };
  function render(state) {
    lastState = state;
    restarting = Boolean(state.busy);
    const busy = posting || restarting;
    controls.dataset.busy = String(busy);
    byId('update-current').textContent = state.current_version || '未标记版本';
    byId('update-available').textContent = state.available_version || '尚未检查';
    byId('update-badge').textContent = busy ? '处理中' : !known ? '状态待确认' : !state.configured ? '待配置凭据' : state.error ? '需要处理' : state.update_available ? '有新版本' : state.available_version ? '已是最新版本' : '待检查';
    byId('update-status').textContent = busy ? (state.message || '正在处理，请勿关闭服务器。') : !known ? '暂时无法确认更新状态，请点击“重新读取状态”。' : !state.configured ? '请先在下方保存 GitHub 下载凭据，然后检查更新。' : (state.message || (state.update_available ? '已有新版本，可以更新并重启。' : state.available_version ? '当前没有可安装的新版本。' : '请先点击“检查更新”，获取最新发布版本。'));
    byId('update-configured').textContent = state.configured ? '已配置下载凭据，可按需更换。' : '尚未配置下载凭据。';
    byId('update-notes').textContent = state.release_notes || '';
    byId('update-notes-wrap').hidden = !state.release_notes;
    byId('update-check').disabled = busy;
    byId('update-install').disabled = busy || !known || !state.update_available || !state.configured;
    byId('update-install').title = busy ? '正在处理，请稍候' : !known ? '请先重新读取状态' : !state.configured ? '请先保存下载凭据' : !state.update_available ? '检查到新版本后可用' : '';
    byId('update-save').disabled = busy;
    byId('update-retry').hidden = known;
    byId('update-retry').disabled = posting;
    if (byId('update-backup')) byId('update-backup').textContent = state.backup_status || '尚无新版升级备份';
    showError(actionError || state.error || '');
  }
  async function request(url, options = {}, timeout = 12000) {
    const controller = new AbortController();
    const deadline = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      if (response.status === 403 || response.redirected) throw new Error('登录已失效或不在本机入口，请在服务器电脑上重新登录。');
      const state = await response.json();
      if (!response.ok) throw new Error(state.error || '操作失败，请稍后重试。');
      return state;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，请检查网络并重新读取状态；若已提交更新，请先确认结果，不要重复提交。');
      throw error;
    } finally { window.clearTimeout(deadline); }
  }
  async function refresh() {
    if (posting) return;
    window.clearTimeout(timer);
    const version = ++generation;
    try {
      const state = await request(controls.dataset.statusUrl, {cache: 'no-store'});
      if (version === generation) { known = state.status !== 'status_error'; actionError = ''; render(state); }
    } catch (error) {
      if (version !== generation) return;
      known = false; actionError = error.message;
      const wasRestarting = restarting;
      render({...lastState, busy: false});
      if (wasRestarting) { byId('update-badge').textContent = '重启中'; byId('update-status').textContent = '服务器可能正在重启，将自动重连；也可点击“重新读取状态”。'; }
    } finally { if (version === generation) timer = window.setTimeout(refresh, 3000); }
  }
  async function submit(event, configure) {
    event.preventDefault();
    if (posting || restarting || (!configure && (!event.submitter || event.submitter.disabled))) return;
    const action = configure ? 'configure' : event.submitter.value;
    if (action === 'check' && known && !lastState.configured) {
      showError('请先在下方粘贴 GitHub 下载凭据并点击“保存凭据”，再检查更新。');
      byId('update-token').focus();
      return;
    }
    if (action === 'check' && !known) { await refresh(); return; }
    posting = true; actionError = ''; generation += 1; window.clearTimeout(timer); render(lastState); showError('');
    byId('update-status').textContent = action === 'check' ? '正在检查 GitHub 发布版本…' : action === 'install' ? '正在提交更新，随后将自动重启…' : '正在保存下载凭据…';
    try {
      const body = new FormData(event.target); body.set('action', action);
      const state = await request(controls.dataset.actionUrl, {method: 'POST', body}, 60000);
      if (configure) byId('update-token').value = '';
      posting = false; known = state.status !== 'status_error'; render(state);
    } catch (error) {
      posting = false; known = false; actionError = error.message;
      render({...lastState, busy: false});
    } finally { posting = false; timer = window.setTimeout(refresh, 1500); }
  }
  byId('update-rollback-form')?.addEventListener('submit', event => submit(event, false));
  byId('update-form').addEventListener('submit', event => submit(event, false));
  byId('update-config-form').addEventListener('submit', event => submit(event, true));
  byId('update-retry').addEventListener('click', refresh);
  const initial = byId('update-initial-state');
  if (initial) { try { lastState = JSON.parse(initial.textContent); known = lastState.status !== 'status_error'; } catch (_) { known = false; } }
  render(lastState);
  refresh();
})();
