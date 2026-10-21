const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { createHash, createHmac } = require('node:crypto');
const ts = require('typescript');

// Execute the real server modules; only the external Supabase service is replaced.
function load(relative, overrides = {}) {
  const filename = path.resolve(__dirname, '..', relative);
  const code = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const module = { exports: {} };
  const dependency = (name) => {
    if (name in overrides) return overrides[name];
    if (name === './request-origin') return load('lib/request-origin.ts');
    return require(name);
  };
  new Function('require', 'module', 'exports', code)(dependency, module, module.exports);
  return module.exports;
}

process.env.ZCONNECT_BACKEND_SECRET = 'isolated-auth-test-secret-0123456789abcdef';
const auth = load('lib/backend-auth.ts', {
  './supabase': {
    fetchCurrentUser: async (token) => token === 'valid' ? {
      id: 'U1', role: 'admin', app_metadata: { company_id: 'acme' },
    } : null,
    refreshSession: async () => { throw new Error('Refresh denied in test'); },
  },
});

test('company and platform privilege use trusted metadata only', () => {
  assert.deepEqual(auth.backendIdentity({ id: 'U', role: 'user', app_metadata: { company_id: 'acme', platform_admin: true } }),
    { id: 'U', role: 'user', companyId: 'acme' });
  assert.equal(auth.backendIdentity({ id: 'U', role: 'admin', app_metadata: { company_id: 'acme', platform_admin: true } }).role, 'platform_admin');
  assert.equal(auth.backendIdentity({ id: 'U', app_metadata: { company_id: '../other' } }), null);
  assert.equal(auth.backendIdentity({ id: 'U', app_metadata: { company_id: 123 } }), null);
});

test('HMAC v3 binds company, body, method, path and query; client headers removed', () => {
  const body = new TextEncoder().encode('test body');
  const headers = new Headers({ authorization: 'forged', cookie: 'forged', 'x-zconnect-company': 'other' });
  auth.applySignedIdentity(headers, 'POST', '/api/analytics/summary', '?x=1', { id: 'U', role: 'user', companyId: 'acme' }, body);
  const digest = createHash('sha256').update(body).digest('hex');
  const message = ['v3', 'POST', '/api/analytics/summary', '?x=1', headers.get('x-zconnect-timestamp'), 'U', 'user', 'acme', digest].join('\n');
  assert.equal(headers.get('x-zconnect-signature'), createHmac('sha256', process.env.ZCONNECT_BACKEND_SECRET).update(message).digest('hex'));
  assert.equal(headers.get('x-zconnect-company'), 'acme');
  assert.equal(headers.has('authorization'), false);
  assert.equal(headers.has('cookie'), false);
});

test('server validates the session before returning signed tenant identity', async () => {
  const cookie = 'zconnect_session=' + encodeURIComponent(JSON.stringify({ access_token: 'valid' }));
  const result = await auth.authenticateRequest(new Request('http://localhost/api/analytics/summary', { headers: { cookie } }));
  assert.equal(result.ok, true);
  assert.deepEqual(result.user, { id: 'U1', role: 'admin', companyId: 'acme' });
  const denied = await auth.authenticateRequest(new Request('http://localhost/api/analytics/summary'));
  assert.equal(denied.response.status, 401);
});

test('cross-site state-changing requests fail before authentication', async () => {
  const result = await auth.authenticateRequest(new Request('http://localhost/api/export/run', {
    method: 'POST', headers: { origin: 'https://attacker.invalid', 'sec-fetch-site': 'cross-site' },
  }));
  assert.equal(result.response.status, 403);
});
