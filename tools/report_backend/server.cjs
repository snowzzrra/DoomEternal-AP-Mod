'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');

const REPOSITORY = 'snowzzrra/DoomEternal-AP-Mod';
const MAX_BYTES = 32768;
const fields = ['schema_version', 'idempotency_key', 'title', 'description', 'diagnostics'];

function validate(value) {
  if (!value || Array.isArray(value) || typeof value !== 'object' ||
      Object.keys(value).length !== fields.length || fields.some(key => !(key in value)) ||
      value.schema_version !== 1 || typeof value.idempotency_key !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value.idempotency_key) ||
      typeof value.title !== 'string' || !value.title.trim() || value.title.length > 120 || /[\r\n\0]/.test(value.title) ||
      typeof value.description !== 'string' || !value.description.trim() || value.description.length > 8000 ||
      typeof value.diagnostics !== 'string' || value.diagnostics.length > 16000 ||
      /\0/.test(value.description + value.diagnostics)) throw new Error('invalid_report');
  return Object.fromEntries(fields.map(key => [key, value[key]]));
}

function createServer({directory, appId, installationId, privateKey, trustedProxyHops = 0, request = fetch, now = Date.now}) {
  if (!/^\d+$/.test(String(appId)) || !/^\d+$/.test(String(installationId))) throw new Error('invalid_app_configuration');
  const signingKey = crypto.createPrivateKey(privateKey);
  if (signingKey.asymmetricKeyType !== 'rsa') throw new Error('RSA_app_key_required');
  if (!Number.isInteger(trustedProxyHops) || trustedProxyHops < 0 || trustedProxyHops > 3) throw new Error('invalid_proxy_configuration');
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const rates = new Map();
  let token = '', tokenUntil = 0;
  let activeRequests = 0;

  async function github(route, body, authorization) {
    const response = await request(`https://api.github.com${route}`, {
      method: body === undefined ? 'GET' : 'POST', signal: AbortSignal.timeout(12000),
      headers: {'Authorization': `Bearer ${authorization}`, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'DoomEternal-AP-report-service', 'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    if (!response.ok) throw new Error('github_unavailable');
    return response.json();
  }
  async function credentials() {
    if (token && now() < tokenUntil) return token;
    const seconds = Math.floor(now() / 1000);
    const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
    const unsigned = `${encode({alg:'RS256',typ:'JWT'})}.${encode({iat:seconds-30,exp:seconds+300,iss:String(appId)})}`;
    const jwt = `${unsigned}.${crypto.sign('RSA-SHA256', Buffer.from(unsigned), signingKey).toString('base64url')}`;
    const result = await github(`/app/installations/${installationId}/access_tokens`,
      {repositories:['DoomEternal-AP-Mod'], permissions:{issues:'write'}}, jwt);
    if (typeof result.token !== 'string' || !result.token) throw new Error('github_unavailable');
    token = result.token; tokenUntil = now() + 240000;
    return token;
  }
  function save(filename, value) {
    const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    fs.renameSync(temporary, filename);
  }
  const server = http.createServer(async (req, res) => {
    const reply = (status, body) => {
      if (!res.destroyed) res.writeHead(status, {'Content-Type':'application/json', 'Cache-Control':'no-store'}).end(JSON.stringify(body));
    };
    if (req.method === 'GET' && req.url === '/health') return reply(200, {status:'ready'});
    if (req.method !== 'POST' || req.url !== '/v1/reports') return reply(404, {error:'not_found'});
    if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(req.headers['content-type'] || '')) return reply(415, {error:'json_required'});
    if (Number(req.headers['content-length']) > MAX_BYTES) return reply(413, {error:'report_too_large'});
    let chunks = [], size = 0;
    if (activeRequests >= 8) return reply(503, {error:'service_busy'});
    activeRequests++;
    try {
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_BYTES) return reply(413, {error:'report_too_large'});
        chunks.push(chunk);
      }
      const report = validate(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      const digest = crypto.createHash('sha256').update(JSON.stringify(report)).digest('hex');
      const filename = path.join(directory, report.idempotency_key + '.json');
      const marker = `doom-report:${report.idempotency_key}`;
      if (fs.existsSync(filename)) {
        const saved = JSON.parse(fs.readFileSync(filename, 'utf8'));
        if (saved.digest !== digest) return reply(409, {error:'idempotency_conflict'});
        if (saved.url) return reply(200, {url:saved.url});
        const auth = await credentials();
        const found = await github('/search/issues?q=' + encodeURIComponent(`repo:${REPOSITORY} is:issue "${marker}"`), undefined, auth);
        const issue = (found.items || []).find(item => typeof item.body === 'string' && item.body.includes(`<!-- ${marker} -->`));
        if (!issue) return reply(503, {error:'submission_unconfirmed'});
        if (!new RegExp(`^https://github\\.com/${REPOSITORY}/issues/\\d+$`).test(issue.html_url)) throw new Error('github_unavailable');
        save(filename, {digest,url:issue.html_url});
        return reply(200, {url:issue.html_url});
      }
      for (const [key, value] of rates) if (value.until <= now()) rates.delete(key);
      let address = req.socket.remoteAddress;
      if (trustedProxyHops) {
        const forwarded = req.headers['x-forwarded-for'];
        if (typeof forwarded !== 'string' || forwarded.length > 1024) return reply(400, {error:'invalid_client_address'});
        const chain = forwarded.split(',').map(value => value.trim()).concat(address);
        address = chain[chain.length - trustedProxyHops - 1];
        if (!net.isIP(address || '')) return reply(400, {error:'invalid_client_address'});
      }
      if (!rates.has(address) && rates.size >= 4096) return reply(429, {error:'rate_limit'});
      const rate = rates.get(address) || {count:0,until:now()+3600000};
      if (++rate.count > 10) return reply(429, {error:'rate_limit'});
      rates.set(address, rate);
      // Token acquisition precedes reservation; an uncertain issue POST is never repeated.
      const auth = await credentials();
      const pending = fs.openSync(filename, 'wx', 0o600);
      try { fs.writeFileSync(pending, JSON.stringify({digest})); fs.fsyncSync(pending); }
      finally { fs.closeSync(pending); }
      const diagnostic = report.diagnostics.replace(/```/g, '` ` `');
      const issue = await github(`/repos/${REPOSITORY}/issues`, {
        title: report.title, body: `${report.description}\n\nDiagnostics\n\n\`\`\`text\n${diagnostic}\n\`\`\`\n\n<!-- ${marker} -->`
      }, auth);
      if (!new RegExp(`^https://github\\.com/${REPOSITORY}/issues/\\d+$`).test(issue.html_url)) throw new Error('github_unavailable');
      save(filename, {digest,url:issue.html_url});
      reply(201, {url:issue.html_url});
    } catch (error) {
      reply(error.message === 'invalid_report' || error instanceof SyntaxError ? 400 : 503,
        {error:error.message === 'invalid_report' || error instanceof SyntaxError ? 'invalid_report' : 'submission_unconfirmed'});
    } finally {
      activeRequests--;
    }
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  server.maxRequestsPerSocket = 20;
  return server;
}

if (require.main === module) {
  // The ingress supplies HTTPS; this listener stays on the private container interface.
  const server = createServer({directory:process.env.REPORT_DATA_DIR || '/data', appId:process.env.GITHUB_APP_ID,
    installationId:process.env.GITHUB_INSTALLATION_ID, privateKey:fs.readFileSync(process.env.GITHUB_APP_KEY_FILE),
    trustedProxyHops:Number(process.env.REPORT_TRUSTED_PROXY_HOPS || 0)});
  server.listen(Number(process.env.PORT || 8080), '0.0.0.0');
}
module.exports = {validate, createServer};
