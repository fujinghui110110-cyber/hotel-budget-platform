const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

test('late polling response cannot restore an old URL after restart', async () => {
  const elements = new Map();
  const handlers = new Map();
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      dataset: {statusUrl: '/status', actionUrl: '/action'}, disabled: false,
      addEventListener(name, fn) { handlers.set(id + ':' + name, fn); },
    });
    return elements.get(id);
  }
  vm.runInNewContext(fs.readFileSync('static/budgeting/access.js', 'utf8'), {
    document: {getElementById: element},
    window: {setTimeout(fn) {timers.set(++timerId, fn); return timerId;}, clearTimeout(id) {timers.delete(id);}},
    fetch(url) {return new Promise(resolve => requests.push({url, resolve}));},
    FormData: class {set() {}},
  });
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const resolve = (index, data) => requests[index].resolve({status: 200, ok: true, json: async () => data});
  resolve(0, {status: 'started', running: true, url: 'https://old-link.trycloudflare.com'});
  await flush();
  const [id, poll] = timers.entries().next().value;
  timers.delete(id);
  poll();
  const posting = handlers.get('access-form:submit')({preventDefault() {}, submitter: {value: 'restart'}, target: {}});
  resolve(2, {status: 'restarting', busy: true});
  await posting;
  resolve(1, {status: 'started', running: true, url: 'https://old-link.trycloudflare.com'});
  await flush();
  assert.equal(element('access-link-area').hidden, true);
  assert.equal(element('access-restart').disabled, true);
  assert.equal(element('access-badge').textContent, '正在重启');
  assert.equal(timers.size, 1);
});
