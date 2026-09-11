const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

test('update polling recovers after restart and does not overwrite a newer action', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {dataset: {statusUrl: '/status', actionUrl: '/action'}, addEventListener(type, handler) { this[type] = handler; }, disabled: false});
    return elements.get(id);
  };
  const requests = [], timers = [];
  const response = state => ({ok: true, status: 200, json: async () => state});
  const context = {
    document: {getElementById: element},
    window: {setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {}},
    fetch: () => new Promise((resolve, reject) => requests.push({resolve, reject})),
    FormData: class {set() {}},
  };
  vm.runInNewContext(fs.readFileSync('static/budgeting/update.js', 'utf8'), context);
  const flush = () => new Promise(resolve => setImmediate(resolve));
  requests.shift().resolve(response({configured: true, update_available: true})); await flush();
  assert.equal(element('update-install').disabled, false);
  timers.pop()(); // A status response is now outstanding.
  element('update-form').submit({preventDefault() {}, target: {}, submitter: {disabled: false, value: 'install'}});
  requests[1].resolve(response({configured: true, busy: true, message: 'updating'})); await flush();
  requests[0].resolve(response({configured: true, busy: false, message: 'stale'})); await flush();
  assert.equal(element('update-status').textContent, 'updating');
  assert.equal(element('update-install').disabled, true);
  requests.length = 0;
  timers.pop()(); requests.shift().reject(new Error('offline')); await flush();
  assert.equal(element('update-badge').textContent, '重启中');
  timers.pop()(); requests.shift().resolve(response({configured: true, current_version: 'v2', busy: false})); await flush();
  assert.equal(element('update-current').textContent, 'v2');
  assert.equal(element('update-check').disabled, false);
});
