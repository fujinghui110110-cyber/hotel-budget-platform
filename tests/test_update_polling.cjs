const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = state => ({ok: true, status: 200, json: async () => state});
function setup(initial = {}) {
  const elements = new Map(), requests = [], timers = new Map(); let sequence = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {textContent: id === 'update-initial-state' ? JSON.stringify(initial) : '', dataset: {statusUrl: '/status', actionUrl: '/action'}, addEventListener(type, handler) { this[type] = handler; }, focus() { this.focused = true; }, disabled: false});
    return elements.get(id);
  };
  const context = {document: {getElementById: element}, AbortController,
    window: {setTimeout(fn, delay) { const id = ++sequence; timers.set(id, {fn, delay}); return id; }, clearTimeout(id) { timers.delete(id); }},
    fetch: (url, options) => new Promise((resolve, reject) => { requests.push({resolve, reject, url}); options.signal.addEventListener('abort', () => { const error = new Error('aborted'); error.name = 'AbortError'; reject(error); }); }),
    FormData: class {set() {}},
  };
  vm.runInNewContext(fs.readFileSync('static/budgeting/update.js', 'utf8'), context);
  const tick = delay => { const entry = [...timers].find(([, value]) => value.delay === delay); assert.ok(entry, `timer ${delay}`); timers.delete(entry[0]); entry[1].fn(); };
  const submit = action => element('update-form').submit({preventDefault() {}, target: {}, submitter: element(action === 'check' ? 'update-check' : 'update-install')});
  element('update-check').value = 'check'; element('update-install').value = 'install';
  return {element, requests, tick, submit};
}
test('unconfigured page gives actionable guidance instead of a disabled check button', async () => {
  const {element, requests, submit} = setup({configured: false, current_version: 'v1'});
  assert.equal(element('update-save').disabled, false);
  assert.equal(element('update-check').disabled, false);
  assert.equal(element('update-install').disabled, true);
  requests.shift().resolve(response({configured: false})); await flush();
  await submit('check');
  assert.equal(requests.length, 0);
  assert.equal(element('update-token').focused, true);
  assert.match(element('update-error').textContent, /保存凭据/);
});
test('status timeout unlocks recovery and credentials but never permits unknown install', async () => {
  const {element, requests, tick} = setup({configured: true, update_available: true});
  tick(12000); await flush();
  assert.match(element('update-error').textContent, /超时/);
  assert.equal(element('update-save').disabled, false);
  assert.equal(element('update-install').disabled, true);
  assert.equal(element('update-retry').hidden, false);
  element('update-retry').click(); requests.at(-1).resolve(response({configured: true})); await flush();
  assert.equal(element('update-retry').hidden, true);
});
test('action timeout allows retry and retains token input', async () => {
  const {element, requests, tick} = setup({configured: false});
  requests.shift().resolve(response({configured: false})); await flush();
  element('update-token').value = 'not-a-real-token';
  element('update-config-form').submit({preventDefault() {}, target: {}});
  assert.equal(element('update-save').disabled, true);
  tick(60000); await flush();
  assert.equal(element('update-save').disabled, false);
  assert.equal(element('update-token').value, 'not-a-real-token');
  assert.equal(element('update-controls').dataset.busy, 'false');
});
test('new action wins over stale polling; actual busy blocks mutations then recovers', async () => {
  const {element, requests, tick, submit} = setup();
  requests.shift().resolve(response({configured: true, update_available: true})); await flush();
  tick(3000);
  submit('install');
  requests[1].resolve(response({configured: true, busy: true, message: 'updating'})); await flush();
  requests[0].resolve(response({configured: true, busy: false, message: 'stale'})); await flush();
  assert.equal(element('update-status').textContent, 'updating');
  assert.equal(element('update-check').disabled, true);
  assert.equal(element('update-controls').dataset.busy, 'true');
  requests.length = 0; tick(1500); requests.shift().reject(new Error('offline')); await flush();
  assert.equal(element('update-badge').textContent, '重启中');
  assert.equal(element('update-retry').hidden, false);
  tick(3000); requests.shift().resolve(response({configured: true, current_version: 'v2', busy: false})); await flush();
  assert.equal(element('update-current').textContent, 'v2');
  assert.equal(element('update-check').disabled, false);
});
test('initial status error permits recovery without installing', () => {
  const {element} = setup({status: 'status_error', error: '读取失败', configured: true, update_available: true});
  assert.equal(element('update-retry').hidden, false);
  assert.equal(element('update-install').disabled, true);
  assert.equal(element('update-save').disabled, false);
});
