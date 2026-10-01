'use strict';
const assert = require('node:assert/strict');
const {createServer} = require('./server.cjs');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

(async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'doom-report-test-'));
  const {privateKey} = crypto.generateKeyPairSync('rsa', {modulusLength:2048});
  let posts = 0, created;
  const request = async (url, options) => {
    assert.ok(url.startsWith('https://api.github.com/'));
    if (url.includes('/access_tokens')) return {ok:true,json:async()=>({token:'fixture-server-secret'})};
    if (url.includes('/search/issues')) return {ok:true,json:async()=>({items:[created]})};
    assert.equal(url, 'https://api.github.com/repos/snowzzrra/DoomEternal-AP-Mod/issues');
    assert.equal(options.headers.Authorization, 'Bearer fixture-server-secret');
    posts++;
    created = {html_url:'https://github.com/snowzzrra/DoomEternal-AP-Mod/issues/42',body:JSON.parse(options.body).body};
    throw new Error('fixture timeout after issue creation');
  };
  let server = createServer({directory,appId:'1',installationId:'2',privateKey:privateKey.export({type:'pkcs8',format:'pem'}),request});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let endpoint = `http://127.0.0.1:${server.address().port}/v1/reports`;
  const report = {schema_version:1,idempotency_key:crypto.randomUUID(),title:'Fixture',description:'Fixture only',diagnostics:'No retail data'};
  const send = async payload => fetch(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  try {
    assert.equal((await send({...report,repository:'foreign/repository'})).status,400);
    assert.equal((await send(report)).status,503);
    // A restart must resolve an uncertain POST rather than publish again.
    await new Promise(resolve => server.close(resolve));
    server = createServer({directory,appId:'1',installationId:'2',privateKey:privateKey.export({type:'pkcs8',format:'pem'}),request});
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    endpoint = `http://127.0.0.1:${server.address().port}/v1/reports`;
    const result = await send(report);
    assert.equal(result.status,200); assert.equal((await result.json()).url,created.html_url);
    assert.equal((await send({...report,title:'Changed'})).status,409);
    assert.equal(posts,1);
    assert.equal((await send({...report,diagnostics:'x'.repeat(40000)})).status,413);
    console.log('PASS schema, repository isolation, bounded payload, durable idempotency and uncertain-response restart');
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory,{recursive:true,force:true});
  }
})().catch(error => {console.error(error);process.exitCode=1;});
