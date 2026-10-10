'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/platform.js','utf8')
  .replace(/^import .*;$/gm,'').replace(/export /g,'');
function scenario(env, responder) {
  const requests = [];
  const context = {__ENV:env, Rate:class {add() {}}, check:() => {}, fail:message => {throw new Error(message);},
    http:{get(url, options) {
      requests.push({url, options});
      const body = responder(url, options);
      return {status:200, body:typeof body === 'string' ? body : JSON.stringify(body), json:() =>
        typeof body === 'string' ? JSON.parse(body) : body};
    }}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return {context, requests};
}
for (const authMode of ['local','hosted']) {
  for (const paginated of [false,true]) {
    test(authMode + ' preflight accepts ' + (paginated ? 'paged' : 'legacy') + ' jobs', () => {
      const token = 'a'.repeat(43);
      const {context, requests} = scenario({AUTH_MODE:authMode, SKY_COOKIE:'session=test', JOB_CURSOR:'opaque+/='},
        url => url.endsWith('/health') ? {status:'ok'} :
          url.endsWith('/api/config') ? {ai_available:false,targets:[]} :
          url.includes('/api/jobs') ? (paginated ? {items:[],next_cursor:'next'} : []) :
          "const token='" + (authMode === 'local' ? token : '') + "';");
      const data = context.setup();
      assert.equal(data.token, authMode === 'local' ? token : '');
      assert.ok(requests.some(req => req.url.endsWith('/api/jobs?cursor=opaque%2B%2F%3D')));
      if (authMode === 'hosted') assert.ok(requests.every(req => !('X-Sky-Token' in req.options.headers)));
      const before = requests.length;
      context.readTraffic(data);
      assert.equal(requests.length, before + 1);
      assert.ok(requests.every(req => !req.options.tags.name.includes('?')));
    });
  }
}
test('hosted preflight rejects a login page or malformed pagination', () => {
  for (const bad of ['login','cursor']) {
    const {context} = scenario({AUTH_MODE:'hosted',SKY_COOKIE:'session=test'}, url =>
      url.endsWith('/health') ? {status:'ok'} :
      url.endsWith('/api/config') ? {ai_available:false,targets:[]} :
      url.endsWith('/api/jobs') ? {items:[],next_cursor:42} :
      bad === 'login' ? '<html>Login</html>' : "const token='';");
    assert.throws(() => context.setup(), /preflight failed/);
  }
});
test('hosted mode rejects local token and missing cookie', () => {
  for (const env of [{AUTH_MODE:'hosted'}, {AUTH_MODE:'hosted',SKY_COOKIE:'test',SKY_API_TOKEN:'secret'},
    {AUTH_MODE:'invalid'}]) assert.throws(() => scenario(env, () => ({})));
});
