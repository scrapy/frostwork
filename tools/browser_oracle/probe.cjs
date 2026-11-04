// Real Chrome and Firefox as the tree-construction oracle for browser mode (docs/BROWSER_MODE.md,
// section 5). `serve` is what tools/browser_oracle/client.py drives; the other modes are measurements.
//
//   node tools/browser_oracle/probe.cjs <chrome|firefox> <browser-binary> <mode> [arg]
//
//   fidelity        which entry points build the tree a page load builds
//   throughput      documents per second per entry point, one browser process
//   equiv [N]       N random token sequences (default 5000): a page load against DOMParser,
//                   parseHTMLUnsafe and srcdoc, compared node for node
//   claims <file>   each claim's expressions against the page load, DOMParser and parseHTMLUnsafe
//   serve [TABS]    the oracle as a service: newline-delimited JSON requests on stdin, one reply line
//                   each on stdout, page loads spread over TABS tabs (default 8). See `serve` below.
//
// The measurement modes print one JSON object on stdout, carrying the browser build. Needs Node 22 or
// later (global WebSocket). Each run starts the browser headless from a fresh temporary profile and
// deletes it on exit. Pages come from an in-process server with exact bytes and headers, served with
// `Content-Security-Policy: script-src 'none'`: that blocks page scripts and leaves the parser's
// scripting flag on, which CSP `sandbox` and CDP's script disabling both turn off.
'use strict';
const { spawn } = require('node:child_process');
const http = require('node:http');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [, , WHICH, BIN, MODE = 'fidelity', ARG] = process.argv;
if (!['chrome', 'firefox'].includes(WHICH) || !BIN) {
  console.error('usage: node probe.cjs <chrome|firefox> <browser-binary> <fidelity|throughput|equiv [N]|claims FILE|serve [TABS]>');
  process.exit(2);
}

// One page exercising every fidelity question at once.
const FIDELITY = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>t</title></head><body>
<noscript><p id="ns">x</p></noscript>
<template><p id="tp">y</p></template>
<script>document.documentElement.setAttribute('data-ran','1')</script>
<table><p id="fo">f</p><tr><td>c</td></tr></table>
<div class="a"/>sc
<select><div id="sd">x</div><option>o</option></select>
</body></html>`;

// The gate-seq / diff_lxml shape: small generated documents, each one different.
const small = (i) => `<!DOCTYPE html><html><head><meta charset="utf-8"><title>p${i}</title></head><body>` +
  `<div class="c${i % 7}"><p>a${i}<b>b<i>c</b>d</i></p><ul><li>x<li>y${i}</ul>` +
  `<table><tr><td>${i}<td>z</table><dl><dt>k<dd>v</dl></div></body></html>`;

// The crawl shape: a product listing of about 40 KB.
const BIG = (() => {
  let s = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>big</title>' +
    '<script>var x = "<div>not a tag</div>";</script></head><body><header><nav>';
  for (let i = 0; i < 40; i++) s += `<a href="/c/${i}" class="nav-link">Category ${i}</a>`;
  s += '</nav></header><main>';
  for (let i = 0; i < 200; i++) {
    s += `<div class="card" data-id="${i}"><h2 class="t">Product ${i}</h2><p class="d">Lorem ipsum dolor ` +
      `sit amet <b>bold</b> <i>it</i></p><span class="price">${i}.99 &euro;</span><ul><li>a<li>b</ul></div>`;
  }
  s += '<table class="spec">';
  for (let i = 0; i < 60; i++) s += `<tr><th>k${i}<td>v${i}`;
  return s + '</table></main><footer>&copy; 2026</footer></body></html>';
})();

// The equivalence alphabet. Its ORDER is part of the measurement: with the seed below it fixes the
// exact documents, so a re-run reproduces the published counts.
const TOKENS = ['<p>', '</p>', '<div>', '</div>', '<table>', '</table>', '<tr>', '<td>', '</td>', '<b>', '</b>',
  '<i>', '</i>', '<a href=x>', '</a>', '<li>', '<ul>', '<select>', '<option>', '</select>', '<template>',
  '</template>', 't', '<br>', '</br>', '<form>', '</form>', '<body>', '</body>', '<html>', '</html>', '<head>',
  '<title>T</title>', '<svg>', '</svg>', '<math>', '<!--c-->', '<button>', '<h1>', '<dd>', '<dt>', '<caption>',
  '<colgroup>', '<frameset>', '<pre>', '<textarea>', '<script>s</script>', '<style>s</style>', '<image>', '<nobr>',
  '<marquee>', '<object>', '<input>', '<hr>', '<![CDATA[x]]>', '<?pi?>', '<p/>', '<div/>', '<frame>', '<dl>',
  '<tbody>', '<th>', '<title>', '<foreignObject>', '<desc>', '<mi>', '<annotation-xml encoding="text/html">',
  '<template shadowrootmode=open>', '<template shadowrootmode=closed>', '<selectedcontent>'];
const SEED = 12345;

// Which documents DOMParser may stand in for a page load on: derived from the equiv measurement, not
// assumed. The scripting flag (noscript) and declarative shadow roots are the two known splits.
const FAST_PATH_ELIGIBLE = (doc) => !/noscript|shadowrootmode/i.test(doc);

// Page-side helpers. `__ser` serialises a node losslessly: HTML template content and open shadow roots
// included, namespaces kept. Only an HTML-namespace <template> has `.content`.
const PAGE_LIB = `
const XHTML = 'http://www.w3.org/1999/xhtml';
window.__probe = (d) => { const ns = d.getElementById('ns'), fo = d.getElementById('fo'),
  a = d.querySelector('div.a'), sd = d.getElementById('sd');
  return { scriptRan: d.documentElement.getAttribute('data-ran') === '1',
    noscriptRawtext: !ns, templateInert: d.querySelectorAll('#tp').length === 0,
    fosteredOutOfTable: !!fo && !fo.closest('table'),
    tbodyInserted: d.querySelectorAll('table > tbody > tr').length > 0,
    selfCloseIgnored: !!a && a.textContent.includes('sc'),
    divKeptInSelect: !!sd && !!sd.closest('select') }; };
window.__ser = (n) => {
  if (n.nodeType === 3) return n.data;
  if (n.nodeType === 8) return ['#c', n.data];
  if (n.nodeType === 10) return ['#dt', n.name, n.publicId, n.systemId];
  if (n.nodeType === 7) return ['#7', n.target, n.data];
  if (n.nodeType === 9 || n.nodeType === 11) return ['#d', [...n.childNodes].map(__ser)];
  if (!n.attributes) return ['#' + n.nodeType];
  const at = []; for (const x of n.attributes) at.push(x.name, x.value);
  const kids = [...n.childNodes].map(__ser);
  if (n.localName === 'template' && n.namespaceURI === XHTML) kids.push(__ser(n.content));
  if (n.shadowRoot) kids.push(['#shadow', n.shadowRoot.mode, [...n.shadowRoot.childNodes].map(__ser)]);
  return [n.localName, n.namespaceURI === XHTML ? 0 : n.namespaceURI, at, kids]; };
// Parsel's CSS over document d, from the plans tools/browser_oracle/css.py compiles. A plan is
// {members: [{css, anchored, value: text|attr|outer, attr, scope: self|subtree, contains: [[mark, s]]}]},
// or {container: plan, subs: [plan]} for values relative to each container match. One answer per plan:
// the values in document order, one per node (a comma group is a union), or {error} if the browser
// refuses the selector. Marker attributes exist only for the duration of one querySelectorAll.
window.__css = (d, plans) => {
  // Document order, as XPath has it: an element, then its attributes, then its children. Each node is
  // numbered BEFORE any marker is added, and an attribute by its owner's number plus its position, so
  // a result never depends on Attr object identity.
  const order = new Map(); let k = 0;
  const walk = (n) => { order.set(n, k); k += 1 + (n.attributes ? n.attributes.length : 0);
    for (const c of n.childNodes) walk(c); };
  walk(d);
  const run = (m, scope, acc) => {
    const marked = [];
    let els;
    try {
      for (const [name, needle] of m.contains) {
        for (const e of d.querySelectorAll('*')) if (e.textContent.includes(needle)) { e.setAttribute(name, ''); marked.push([e, name]); }
      }
      if (scope) { scope.setAttribute('data-fw-scope', ''); marked.push([scope, 'data-fw-scope']); }
      els = [...d.querySelectorAll(scope ? m.anchored : m.css)];
    } finally { for (const [e, name] of marked) e.removeAttribute(name); }
    for (const el of els) {
      if (m.value === 'outer') acc.set(order.get(el), el);
      else if (m.value === 'text') {
        if (m.scope === 'self') { for (const c of el.childNodes) if (c.nodeType === 3) acc.set(order.get(c), c.data); }
        else { const w = d.createTreeWalker(el, 4); let t; while ((t = w.nextNode())) acc.set(order.get(t), t.data); }
      } else {
        const take = (e) => { let i = 0; for (const a of e.attributes) { if (a.name === m.attr) acc.set(order.get(e) + 1 + i, a.value); i++; } };
        take(el); if (m.scope === 'subtree') for (const e of el.querySelectorAll('*')) take(e);
      }
    }
  };
  const answer = (p, scope) => { const acc = new Map(); for (const m of p.members) run(m, scope, acc);
    return [...acc.keys()].sort((a, b) => a - b).map((key) => acc.get(key)); };
  const value = (v) => (typeof v === 'string' ? v : v.outerHTML);
  return plans.map((p) => { try {
    if (p.subs) return answer(p.container, null).map((c) => p.subs.map((s) => answer(s, c).map(value)));
    return answer(p, null).map(value);
  } catch (e) { return { error: String((e && e.message) || e) }; } });
};
window.__q = (d, s) => [...d.querySelectorAll(s)]
  .map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.data).join('|'));
window.__x = (d, x) => { const r = d.evaluate(x, d, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null); const o = [];
  for (let i = 0; i < r.snapshotLength; i++) { const n = r.snapshotItem(i);
    o.push(n.nodeType === 2 ? n.value : n.nodeType === 3 ? n.data : n.localName); } return o; };
window.__codes = (t) => [...t].map(c => c.codePointAt(0).toString(16));
window.__srcdoc = (html, f2) => new Promise((res) => { const f = document.createElement('iframe');
  f.onload = () => { const r = f2(f.contentDocument); f.remove(); res(r); }; f.srcdoc = html; document.body.appendChild(f); });
`;

// ---------------------------------------------------------------- exact bytes and headers
const SEQ = [];
// `serve`'s documents, keyed by a counter and deleted once their page has loaded.
const SERVED = new Map();
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const h = { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' };
  const csp = u.searchParams.get('csp');
  if (csp === 'script') h['Content-Security-Policy'] = "script-src 'none'";
  if (csp === 'sandbox') h['Content-Security-Policy'] = 'sandbox';
  let body;
  if (u.pathname === '/fidelity') body = FIDELITY;
  else if (u.pathname.startsWith('/small/')) body = small(+u.pathname.slice(7));
  else if (u.pathname === '/big') body = BIG;
  else if (u.pathname.startsWith('/seq/')) body = SEQ[+u.pathname.slice(5)];
  else if (u.pathname.startsWith('/doc/')) {
    // a served document is a string (sent as UTF-8) or {body: Buffer, ctype}: exact bytes, and the
    // Content-Type header as given (omitted when null), so the browser resolves the encoding itself
    const s = SERVED.get(+u.pathname.slice(5));
    if (s !== undefined && typeof s !== 'string') {
      if (s.ctype === null) delete h['Content-Type']; else h['Content-Type'] = s.ctype;
      body = s.body;
    } else body = s;
  } else if (u.pathname === '/harness') body = '<!DOCTYPE html><meta charset="utf-8"><title>h</title><body>';
  if (body === undefined) { res.writeHead(404); return res.end(); }
  res.writeHead(200, h); res.end(body);
});

// ---------------------------------------------------------------- protocol clients
// Every browser this run starts, so a failure or a signal at any point still kills it and deletes its
// profile. Each runs as its own process group, because Firefox's content processes outlive a plain kill.
const browsers = new Set();
function killNow(b) {
  try { process.kill(-b.p.pid, 'SIGKILL'); } catch { /* already gone */ }
  fs.rmSync(b.profile, { recursive: true, force: true });
  browsers.delete(b);
}
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => { browsers.forEach(killNow); process.exit(130); });

function launch(args, endpoint, profile) {
  const p = spawn(BIN, args, { stdio: ['ignore', 'ignore', 'pipe'], detached: true });
  const b = { p, profile };
  browsers.add(b);
  let closing = null;
  b.close = () => closing || (closing = new Promise((done) => {
    let finished = false;
    const finish = () => { if (!finished) { finished = true; killNow(b); done(); } };
    if (p.exitCode !== null || p.signalCode !== null) return finish();
    p.once('exit', finish);
    try { process.kill(-p.pid, 'SIGTERM'); } catch { finish(); }
    setTimeout(finish, 5000).unref();
  }));
  return new Promise((ok, bad) => {
    let buf = ''; let found = false;
    const fail = (why) => { if (!found) { found = true; b.close().then(() => bad(new Error(`${why}\n${buf.slice(-2000)}`))); } };
    p.stderr.on('data', (d) => {
      buf += d; const m = buf.match(endpoint);
      if (m && !found) { found = true; ok({ url: m[1], close: b.close, pid: p.pid }); }
    });
    p.on('exit', (c) => fail(`browser exited ${c}`));
    setTimeout(() => fail('no endpoint'), 30000).unref();
  });
}

// Run a browser's setup, and close the browser if any step of it fails.
async function guarded(b, setup) {
  try { return { ...(await setup()), close: b.close }; } catch (e) { await b.close(); throw e; }
}

// JSON-RPC over the global WebSocket, shared by CDP and BiDi: both match replies to requests by `id`.
function rpc(url) {
  const ws = new WebSocket(url);
  let id = 0; const wait = new Map(); const listeners = [];
  ws.addEventListener('message', (ev) => {
    const j = JSON.parse(ev.data);
    if (j.id && wait.has(j.id)) {
      const [ok, bad] = wait.get(j.id); wait.delete(j.id);
      j.error ? bad(new Error(JSON.stringify(j.error))) : ok(j.result);
    } else listeners.forEach((f) => f(j));
  });
  const send = (method, params = {}, extra = {}) => new Promise((ok, bad) => {
    const i = ++id; wait.set(i, [ok, bad]); ws.send(JSON.stringify({ id: i, method, params, ...extra }));
    setTimeout(() => { if (wait.has(i)) { wait.delete(i); bad(new Error(`timeout: ${method}`)); } }, 20000).unref();
  });
  return new Promise((ok, bad) => {
    ws.addEventListener('open', () => ok({ send, on: (f) => listeners.push(f) }));
    ws.addEventListener('error', () => bad(new Error(`cannot connect to ${url}`)));
  });
}

// Both browsers as the same tab interface: navigate(url) and evaluate(expr) -> a JSON value.
async function chrome() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'frostwork-oracle-chrome-'));
  const b = await launch(['--headless', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--lang=en-GB', 'about:blank'],
  /DevTools listening on (ws:\/\/\S+)/, profile);
  return guarded(b, async () => {
    const c = await rpc(b.url);
    const version = (await c.send('Browser.getVersion')).product;
    const loads = new Map();
    c.on((j) => { if (j.method === 'Page.loadEventFired' && loads.has(j.sessionId)) loads.get(j.sessionId)(); });
    async function tab() {
      const { targetId } = await c.send('Target.createTarget', { url: 'about:blank' });
      const { sessionId } = await c.send('Target.attachToTarget', { targetId, flatten: true });
      const s = (m, params) => c.send(m, params, { sessionId });
      await s('Page.enable');
      return {
        async navigate(u) {
          const loaded = new Promise((r, x) => { loads.set(sessionId, r); setTimeout(() => x(new Error(`load timeout ${u}`)), 20000).unref(); });
          await s('Page.navigate', { url: u }); await loaded;
        },
        async evaluate(expr) {
          const r = await s('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
          if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value;
        },
        disableScripts: () => s('Emulation.setScriptExecutionDisabled', { value: true }),
      };
    }
    return { version, tab, pid: b.pid };
  });
}

async function firefox() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'frostwork-oracle-firefox-'));
  const prefs = {
    'browser.shell.checkDefaultBrowser': false, 'datareporting.policy.dataSubmissionEnabled': false,
    'toolkit.telemetry.reportingpolicy.firstRun': false, 'browser.startup.homepage_override.mstone': 'ignore',
    'intl.locale.requested': 'en-GB', 'app.update.disabledForTesting': true,
  };
  fs.writeFileSync(path.join(profile, 'user.js'), Object.entries(prefs)
    .map(([k, v]) => `user_pref(${JSON.stringify(k)}, ${JSON.stringify(v)});`).join('\n'));
  const b = await launch(['--headless', '--remote-debugging-port', '0', '--profile', profile, '--no-remote',
    'about:blank'], /WebDriver BiDi listening on (ws:\/\/\S+)/, profile);
  return guarded(b, async () => {
    const c = await rpc(b.url.replace(/\/?$/, '/session'));
    const sess = await c.send('session.new', { capabilities: {} });
    const version = `${sess.capabilities.browserName}/${sess.capabilities.browserVersion}`;
    async function tab() {
      const { context } = await c.send('browsingContext.create', { type: 'tab' });
      return {
        navigate: (u) => c.send('browsingContext.navigate', { context, url: u, wait: 'complete' }),
        async evaluate(expr) {
          const r = await c.send('script.evaluate', { expression: `(async () => JSON.stringify(await (${expr})))()`,
            target: { context }, awaitPromise: true, resultOwnership: 'none' });
          if (r.type === 'exception') throw new Error(JSON.stringify(r.exceptionDetails)); return JSON.parse(r.result.value);
        },
        disableScripts: null,
      };
    }
    return { version, tab, pid: b.pid };
  });
}

// ---------------------------------------------------------------- the modes
const now = () => Number(process.hrtime.bigint()) / 1e9;
const ser = (d) => `JSON.stringify(__ser(${d}))`;

async function fidelity(B, base, out) {
  const entry = async (name, f) => {
    try { out.fidelity[name] = await f(); } catch (e) { out.fidelity[name] = `ERR ${e.message.slice(0, 120)}`; }
  };
  const load = async (q, disableScripts) => {
    const tb = await B.tab(); if (disableScripts) await tb.disableScripts();
    await tb.navigate(`${base}/fidelity${q}`); await B.lib(tb); return tb.evaluate('__probe(document)');
  };
  out.fidelity = {};
  await entry('page load, scripts run', () => load('', false));
  await entry("page load, CSP script-src 'none'", () => load('?csp=script', false));
  await entry('page load, CSP sandbox', () => load('?csp=sandbox', false));
  if (WHICH === 'chrome') await entry('page load, CDP script execution disabled', () => load('', true));
  const h = await B.harness(); const F = JSON.stringify(FIDELITY);
  await entry('srcdoc iframe in a CSP page', () => h.evaluate(`__srcdoc(${F}, __probe)`));
  await entry('DOMParser', () => h.evaluate(`__probe(new DOMParser().parseFromString(${F}, 'text/html'))`));
  await entry('Document.parseHTMLUnsafe', () => h.evaluate(`Document.parseHTMLUnsafe ? __probe(Document.parseHTMLUnsafe(${F})) : null`));
}

async function throughput(B, base, out) {
  const TABS = 8; const rate = (n, t0) => Math.round(n / (now() - t0));
  const loads = async (paths, tabs) => Promise.all([...Array(tabs)].map(async (_, k) => {
    const tb = await B.tab();
    for (let i = k; i < paths.length; i += tabs) {
      await tb.navigate(`${base}${paths[i]}?csp=script`); await B.lib(tb); await tb.evaluate(`${ser('document')}.length`);
    }
  }));
  const h = await B.harness(); const kb = `${Math.round(BIG.length / 1024)} KB`;
  out.throughput = { smallBytes: [small(0).length, small(999).length], bigBytes: BIG.length };
  // The timed loops serialise the whole tree in the page and send back only its length.
  let t0 = now(); await loads([...Array(200)].map((_, i) => `/small/${i}`), 1);
  out.throughput['small, page load, 1 tab'] = rate(200, t0);
  t0 = now(); await loads([...Array(800)].map((_, i) => `/small/${i}`), TABS);
  out.throughput[`small, page load, ${TABS} tabs`] = rate(800, t0);
  t0 = now();
  for (let i = 0; i < 2000; i += 50) {
    const docs = JSON.stringify([...Array(50)].map((_, j) => small(i + j)));
    await h.evaluate(`Promise.all(${docs}.map(x => __srcdoc(x, d => ${ser('d')}.length))).then(r => r.length)`);
  }
  out.throughput['small, srcdoc, 50 at a time'] = rate(2000, t0);
  t0 = now();
  for (let i = 0; i < 5000; i += 500) {
    const docs = JSON.stringify([...Array(500)].map((_, j) => small(i + j)));
    await h.evaluate(`${docs}.map(x => ${ser("new DOMParser().parseFromString(x, 'text/html')")}.length).length`);
  }
  out.throughput['small, DOMParser'] = rate(5000, t0);
  t0 = now(); await loads(Array(100).fill('/big'), 1);
  out.throughput[`${kb}, page load, 1 tab`] = rate(100, t0);
  t0 = now(); await loads(Array(400).fill('/big'), TABS);
  out.throughput[`${kb}, page load, ${TABS} tabs`] = rate(400, t0);
}

// mulberry32: exact 32-bit arithmetic. A float LCG loses precision past 2^53 and silently emits a
// fraction of its alphabet, so coverage is asserted below rather than assumed.
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

async function equiv(B, base, out) {
  const n = ARG ? +ARG : 5000; const next = mulberry32(SEED); const rnd = (k) => Math.floor(next() * k);
  const used = new Map();
  const docs = [...Array(n)].map((_, i) => (i % 2 ? '<!DOCTYPE html>' : '') + [...Array(1 + rnd(7))].map(() => {
    const t = TOKENS[rnd(TOKENS.length)]; used.set(t, (used.get(t) || 0) + 1); return t;
  }).join(''));
  const missing = TOKENS.filter((t) => !used.has(t));
  if (missing.length) throw new Error(`generator never emitted ${JSON.stringify(missing)}; raise N`);
  docs.push('<p id=p><table id=tb></table>', '<!DOCTYPE html><p id=p><table id=tb></table>');
  out.coverage = { seed: SEED, tokens: TOKENS.length, minUses: Math.min(...used.values()), maxUses: Math.max(...used.values()) };
  const base0 = SEQ.length; SEQ.push(...docs);

  const loaded = Array(docs.length); const TABS = 8;
  await Promise.all([...Array(TABS)].map(async (_, k) => {
    const tb = await B.tab();
    for (let i = k; i < docs.length; i += TABS) {
      try { await tb.navigate(`${base}/seq/${base0 + i}?csp=script`); await B.lib(tb); loaded[i] = await tb.evaluate(ser('document')); }
      catch (e) { loaded[i] = null; console.error('page load failed', i, JSON.stringify(docs[i]), e.message.slice(0, 80)); }
    }
  }));
  const h = await B.harness(); const dom = [], unsafe = [], srcdoc = [];
  for (let i = 0; i < docs.length; i += 100) {
    const chunk = JSON.stringify(docs.slice(i, i + 100));
    dom.push(...await h.evaluate(`${chunk}.map(x => ${ser("new DOMParser().parseFromString(x, 'text/html')")})`));
    unsafe.push(...await h.evaluate(`${chunk}.map(x => Document.parseHTMLUnsafe ? ${ser('Document.parseHTMLUnsafe(x)')} : null)`));
    srcdoc.push(...await h.evaluate(`Promise.all(${chunk}.map(x => __srcdoc(x, d => ${ser('d')})))`));
  }
  const differing = (trees) => docs.filter((_, i) => loaded[i] !== null && trees[i] !== loaded[i]);
  const report = (trees) => { const d = differing(trees); const e = d.filter(FAST_PATH_ELIGIBLE);
    return { differ: d.length, differEligible: e.length, examples: d.slice(0, 4), eligibleExamples: e.slice(0, 4) }; };
  out.equiv = { docs: docs.length, eligible: docs.filter(FAST_PATH_ELIGIBLE).length,
    containsShadowrootmode: docs.filter((d) => /shadowrootmode/i.test(d)).length,
    pageLoadFailures: loaded.filter((x) => x === null).length,
    domparser: report(dom), parseHTMLUnsafe: report(unsafe), srcdoc: report(srcdoc) };
}

// A claim is a document plus JS expressions over `d`, evaluated on the page load and on the two
// parser APIs. Expressions are inlined as arrow functions, never eval'd: the page's CSP forbids eval.
async function claims(B, base, out) {
  if (!ARG) throw new Error('claims needs a fixture file');
  const list = JSON.parse(fs.readFileSync(ARG, 'utf8'));
  const base0 = SEQ.length; SEQ.push(...list.map((c) => c.doc));
  const tb = await B.tab(); out.claims = [];
  for (let i = 0; i < list.length; i++) {
    await tb.navigate(`${base}/seq/${base0 + i}?csp=script`); await B.lib(tb);
    const fns = `[${list[i].exprs.map((e) => `(d) => (${e})`).join(',')}]`; const src = JSON.stringify(list[i].doc);
    const r = await tb.evaluate(`(() => { const F = ${fns};
      const docs = { pageLoad: document, domparser: new DOMParser().parseFromString(${src}, 'text/html'),
                     parseHTMLUnsafe: Document.parseHTMLUnsafe ? Document.parseHTMLUnsafe(${src}) : null };
      const o = {}; for (const [k, d] of Object.entries(docs))
        o[k] = d ? F.map(f => { try { return f(d); } catch (e) { return 'ERR ' + e.message; } }) : null;
      return o; })()`);
    out.claims.push({ name: list[i].name, exprs: list[i].exprs, ...r });
  }
}

// The oracle as a service, for client.py. The first stdout line is {"ready": true, "browser": build,
// "tabs": n, "pid": the browser's process group}. Then each stdin line is a request
// {"id": n, "docs": [string, ...], "selfcheck": bool}, answered in order by one line
// {"id": n, "trees": [...], "paths": [...], "errors": [...]}:
//   trees[i]   __ser's lossless tree of docs[i], or null when its page load failed
//   paths[i]   "domparser" for a FAST_PATH_ELIGIBLE document, "page" for a CSP-served page load
//   errors     [{index, error}] for every null tree
// A request may also carry CSS: "plans" (a table of css.py plans) and "queries" (per document, indexes
// into it). The reply then has "css": per document, one __css answer per query, computed on the same
// document the tree was serialised from, after serialising it. Every reply has "charsets": the
// document.characterSet each document was decoded with (UTF-8 for a DOMParser string).
// "bodies" (base64, one per document, in place of "docs") and "ctypes" (a Content-Type value or null
// per document) serve exact bytes with that header, and "page": true sends every document down the
// page-load path; bodies always take it, since only a page load decodes bytes as a browser does.
// With "selfcheck", every eligible document is also page-loaded, and the reply carries
// "selfcheck": {"checked": n, "differ": [index, ...], "css_differ": [index, ...], "failed": [index, ...]}:
// the eligible documents whose two trees differ, those whose CSS answers differ, and those whose page
// load failed.
// A request that cannot be read is answered {"id": null, "error": ...}. The browser closes when stdin
// ends.
async function serve(B, base) {
  const TABS = ARG ? +ARG : 8; const CHUNK = 200;
  const h = await B.harness();
  const tabs = await Promise.all([...Array(TABS)].map(() => B.tab()));
  let served = 0;

  // Each path answers [tree JSON, CSS answers JSON or null] per document. `queriesOf(j)` is the j-th
  // document's list of indexes into `plans`; the DOMParser path sends the table once per chunk.
  const viaDomparser = async (docs, plans, queriesOf) => {
    const out = [];
    for (let i = 0; i < docs.length; i += CHUNK) {
      const qs = docs.slice(i, i + CHUNK).map((_, j) => queriesOf(i + j));
      out.push(...await h.evaluate(`(() => { const P = ${JSON.stringify(plans || [])}; const Q = ${JSON.stringify(qs)};
        return ${JSON.stringify(docs.slice(i, i + CHUNK))}.map((x, j) => { const d = new DOMParser().parseFromString(x, 'text/html');
          const t = JSON.stringify(__ser(d)); return [t, Q[j] ? JSON.stringify(__css(d, Q[j].map((p) => P[p]))) : null]; }); })()`));
    }
    return out;
  };
  // A shared queue over the tab pool. A tab whose load failed is replaced, so one wedged page cannot
  // fail every document after it.
  const viaPage = async (docs, plans, queriesOf) => {
    const out = Array(docs.length).fill(null); const errors = []; let next = 0;
    await Promise.all(tabs.map(async (_, k) => {
      while (next < docs.length) {
        const i = next++; const key = served++; SERVED.set(key, docs[i]);
        try {
          await tabs[k].navigate(`${base}/doc/${key}?csp=script`); await B.lib(tabs[k]);
          const qi = queriesOf(i); const q = qi ? qi.map((p) => plans[p]) : null;
          out[i] = await tabs[k].evaluate(`(() => { const t = JSON.stringify(__ser(document));
            return [t, ${q ? `JSON.stringify(__css(document, ${JSON.stringify(q)}))` : 'null'}, document.characterSet]; })()`);
        } catch (e) {
          errors.push({ index: i, error: e.message.slice(0, 200) });
          try { tabs[k] = await B.tab(); } catch { /* keep the old tab */ }
        } finally { SERVED.delete(key); }
      }
    }));
    return { out, errors };
  };

  async function answer(req) {
    const bodies = Array.isArray(req.bodies) ? req.bodies : null;
    const docs = bodies
      ? bodies.map((b, i) => ({ body: Buffer.from(b, 'base64'), ctype: req.ctypes ? req.ctypes[i] : 'text/html' }))
      : req.docs;
    const n = docs.length;
    const plans = req.plans || null;
    const queriesOf = (i) => (plans && req.queries && req.queries[i] ? req.queries[i] : null);
    const eligible = bodies || req.page ? docs.map(() => false) : docs.map(FAST_PATH_ELIGIBLE);
    const fastIdx = [...Array(n).keys()].filter((i) => eligible[i]);
    const slowIdx = req.selfcheck ? [...Array(n).keys()] : [...Array(n).keys()].filter((i) => !eligible[i]);
    const [fast, slow] = await Promise.all([
      viaDomparser(fastIdx.map((i) => docs[i]), plans, (j) => queriesOf(fastIdx[j])),
      viaPage(slowIdx.map((i) => docs[i]), plans, (j) => queriesOf(slowIdx[j]))]);
    const got = Array(n).fill(null); const paged = Array(n).fill(null);
    fastIdx.forEach((i, j) => { got[i] = fast[j]; });
    slowIdx.forEach((i, j) => { paged[i] = slow.out[j]; if (!eligible[i]) got[i] = slow.out[j]; });
    const errors = slow.errors.map((e) => ({ ...e, index: slowIdx[e.index] })).filter((e) => !eligible[e.index]);
    const parts = [`"id":${JSON.stringify(req.id)}`, `"trees":[${got.map((t) => (t ? t[0] : 'null')).join(',')}]`,
      `"paths":${JSON.stringify(eligible.map((e) => (e ? 'domparser' : 'page')))}`, `"errors":${JSON.stringify(errors)}`];
    if (plans) parts.push(`"css":[${got.map((t) => (t && t[1] ? t[1] : 'null')).join(',')}]`);
    // the encoding the browser decoded each document with; a DOMParser document is a string, so UTF-8
    parts.push(`"charsets":${JSON.stringify(got.map((t, i) => (t ? (eligible[i] ? 'UTF-8' : t[2]) : null)))}`);
    if (req.selfcheck) {
      const loaded = fastIdx.filter((i) => paged[i] !== null);
      parts.push(`"selfcheck":${JSON.stringify({ checked: loaded.length,
        differ: loaded.filter((i) => paged[i][0] !== got[i][0]),
        css_differ: loaded.filter((i) => paged[i][1] !== got[i][1]),
        failed: slow.errors.filter((e) => eligible[slowIdx[e.index]]).map((e) => slowIdx[e.index]) })}`);
    }
    return `{${parts.join(',')}}`;
  }

  process.stdout.write(`${JSON.stringify({ ready: true, browser: B.version, tabs: TABS, pid: B.pid })}\n`);
  const rl = require('node:readline').createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    let reply;
    try {
      const req = JSON.parse(line);
      const listed = Array.isArray(req.bodies) ? req.bodies : req.docs;
      if (!Array.isArray(listed) || !listed.every((d) => typeof d === 'string')) throw new Error('docs (or bodies) must be a list of strings');
      if (req.queries && (!Array.isArray(req.plans) || req.queries.length !== listed.length)) throw new Error('queries needs plans and one entry per document');
      reply = await answer(req);
    } catch (e) {
      let id = null; try { id = JSON.parse(line).id ?? null; } catch { /* unreadable */ }
      reply = JSON.stringify({ id, error: e.message.slice(0, 500) });
    }
    process.stdout.write(`${reply}\n`);
  }
}

(async () => {
  const MODES = { fidelity, throughput, equiv, claims, serve };
  if (!MODES[MODE]) throw new Error(`unknown mode ${MODE}`);
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  const B = WHICH === 'chrome' ? await chrome() : await firefox();
  B.lib = (tb) => tb.evaluate(`(() => { ${PAGE_LIB}; return 1; })()`);
  let harness = null;
  B.harness = async () => {
    if (!harness) { harness = await B.tab(); await harness.navigate(`${base}/harness?csp=script`); await B.lib(harness); }
    return harness;
  };
  const out = { browser: B.version, mode: MODE, date: new Date().toISOString().slice(0, 10) };
  try {
    await MODES[MODE](B, base, out);
    if (MODE !== 'serve') console.log(JSON.stringify(out, null, 1));
  } finally { await B.close(); server.close(); }
})().catch((e) => { console.error(e); process.exit(1); });
