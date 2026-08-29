const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

function makeToken(exp) {
  const payload = Buffer.from(JSON.stringify({ exp }), 'utf8').toString('base64url');
  return `header.${payload}.signature`;
}

function loadClient(fetchImpl, initialSession) {
  const storage = new Map(initialSession ? [['rec_session_operator_tenant-default', JSON.stringify(initialSession)]] : []);
  const context = {
    window: { addEventListener() {} },
    document: { addEventListener() {}, getElementById() { return null; }, querySelectorAll() { return []; } },
    sessionStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
      clear: () => storage.clear(),
    },
    fetch: fetchImpl,
    Headers,
    atob: (value) => Buffer.from(value, 'base64').toString('binary'),
    alert() {},
    console,
    setTimeout,
    clearTimeout,
    Date,
  };
  vm.runInNewContext(fs.readFileSync('backend/api/static/app.js', 'utf8'), context);
  return context.window.__recoveryosAuth;
}

test('valid token makes a normal request without refresh', async () => {
  let refreshes = 0;
  const auth = loadClient(async (url) => {
    if (url === '/api/auth/refresh') refreshes++;
    return { ok: true, status: 200, json: async () => ({ value: true }) };
  }, { access_token: makeToken(Math.floor(Date.now() / 1000) + 300), refresh_token: 'refresh' });
  const response = await auth.apiFetch('/api/workflows');
  assert.equal(response.value, true);
  assert.equal(refreshes, 0);
});

test('expired token refreshes once and retries the original request', async () => {
  const calls = [];
  const auth = loadClient(async (url) => {
    calls.push(url);
    if (url === '/api/auth/refresh') {
      return { ok: true, status: 200, json: async () => ({ access_token: makeToken(Date.now() / 1000 + 300), refresh_token: 'refresh' }) };
    }
    return { ok: true, status: 200, json: async () => ({ launched: true }) };
  }, { access_token: makeToken(1), refresh_token: 'refresh' });
  const response = await auth.apiFetch('/api/scenarios/billing_unavailable', { method: 'POST', body: '{}' });
  assert.equal(response.launched, true);
  assert.deepEqual(calls, ['/api/auth/refresh', '/api/scenarios/billing_unavailable']);
});

test('401 refreshes once and retries the request exactly once', async () => {
  let apiRequests = 0;
  let refreshes = 0;
  const auth = loadClient(async (url) => {
    if (url === '/api/auth/refresh') {
      refreshes++;
      return { ok: true, status: 200, json: async () => ({ access_token: makeToken(Date.now() / 1000 + 300), refresh_token: 'refresh' }) };
    }
    apiRequests++;
    if (apiRequests === 1) {
      return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ detail: 'Expired' }) };
    }
    return { ok: true, status: 200, json: async () => ({ retried: true }) };
  }, { access_token: makeToken(Date.now() / 1000 + 300), refresh_token: 'refresh' });
  assert.equal((await auth.apiFetch('/api/workflows')).retried, true);
  assert.equal(apiRequests, 2);
  assert.equal(refreshes, 1);
});

test('concurrent expired requests share one refresh flight', async () => {
  let refreshes = 0;
  const auth = loadClient(async (url) => {
    if (url === '/api/auth/refresh') {
      refreshes++;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { ok: true, status: 200, json: async () => ({ access_token: makeToken(Date.now() / 1000 + 300), refresh_token: 'refresh' }) };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }, { access_token: makeToken(1), refresh_token: 'refresh' });
  await Promise.all([auth.apiFetch('/api/a'), auth.apiFetch('/api/b'), auth.apiFetch('/api/c')]);
  assert.equal(refreshes, 1);
});

test('a repeated 401 does not cause an infinite retry loop', async () => {
  let apiRequests = 0;
  const auth = loadClient(async (url) => {
    if (url === '/api/auth/refresh') {
      return { ok: true, status: 200, json: async () => ({ access_token: makeToken(Date.now() / 1000 + 300), refresh_token: 'refresh' }) };
    }
    apiRequests++;
    return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ detail: 'Unauthorized' }) };
  }, { access_token: makeToken(1), refresh_token: 'refresh' });
  await assert.rejects(() => auth.apiFetch('/api/workflows'));
  assert.equal(apiRequests, 2);
});

test('refresh failure clears auth state and surfaces an expiry message', async () => {
  const auth = loadClient(async (url) => {
    if (url === '/api/auth/refresh') return { ok: false, status: 401, json: async () => ({ detail: 'Expired refresh' }) };
    throw new Error('unexpected API request');
  }, { access_token: makeToken(1), refresh_token: 'refresh' });
  await assert.rejects(() => auth.apiFetch('/api/workflows'));
  assert.equal(auth.loadAuthSession(), null);
});
