// holdings — read-only viewer for a published catalog.
//
// The catalog is fetched a page at a time over HTTP Range requests, so
// opening this page does not download it: a lookup costs tens of kilobytes
// of a catalog that may be hundreds of megabytes. Nothing is uploaded and
// there is no write path — `add-medium`, `scan` and `import-restic` do not
// exist here, by construction rather than by omission.
//
// The SQL is not written here. `queries.json` is generated from
// holdings.QUERIES and a test in the Python suite fails if the two
// disagree, so this viewer cannot quietly answer a different question from
// the CLI.

import { open, resolveFeed } from './swarmlite/index.js';

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

const DAY = 86400;
const STALE_DAYS = 30;

let db;
let Q;
let seen = null;          // page counters after the previous query

// ---------------------------------------------------------------- helpers

function humanSize(n) {
  n = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  for (const u of units) {
    if (n < 1024 || u === 'TB') {
      return u === 'B' ? `${n.toFixed(0)}B` : `${n.toFixed(1)}${u}`;
    }
    n /= 1024;
  }
}

const day = (ts) =>
  ts ? new Date(ts * 1000).toISOString().slice(0, 10) : 'never';

// What a medium's copies survive. Green where the copy outlives deletion of
// the original; plain where it does not (a sync mirror is not a backup);
// red where a lease has lapsed, which is a backup you stopped paying for.
function durabilityTag(r) {
  if (r.durability === 'leased' && r.lease_expires) {
    const days = (r.lease_expires * 1000 - Date.now()) / 86400000;
    if (days <= 0) return '<span class="tag danger">lease lapsed</span>';
    const cls = days < 14 ? 'tag danger' : 'tag backup';
    return `<span class="${cls}">leased · ${days.toFixed(0)}d</span>`;
  }
  const backs = ['independent', 'leased', 'hosted'].includes(r.durability);
  return `<span class="tag${backs ? ' backup' : ''}">${esc(r.durability)}</span>`;
}

function economy(label, t0) {
  const s = db.stats();
  const pages = s.pagesFetched - (seen?.pagesFetched ?? 0);
  const bytes = s.bytesFetched - (seen?.bytesFetched ?? 0);
  seen = s;
  const pct = (100 * bytes) / s.fileSize;
  $('economy').textContent =
    `${label}: ${pages} pages (${humanSize(bytes)}, ${pct.toFixed(3)}% of ` +
    `${humanSize(s.fileSize)}) in ${(performance.now() - t0).toFixed(0)} ms`;
}

// One query at a time, always. wa-sqlite's async build steps statements on
// a single connection handle and does not serialise: two overlapping
// queries interleave and the second sees a database with no tables in it.
// Enforced here rather than at the call sites so views can still use
// Promise.all — which is how this was found, by the page failing to open a
// catalog that the same queries read perfectly when run one after another.
let queue = Promise.resolve();

function run(name, params = []) {
  const result = queue.then(() => db.query(Q[name], params));
  queue = result.catch(() => {});      // a failure must not stall the queue
  return result;
}

function table(head, rows) {
  if (!rows.length) return '<p class="note">nothing to show.</p>';
  const ths = head.map((h) =>
    `<th class="${h.num ? 'num' : ''}">${esc(h.label)}</th>`).join('');
  const trs = rows.map((cells) =>
    '<tr>' + cells.map((c, i) =>
      `<td class="${head[i].num ? 'num' : ''} ${c.cls ?? ''}">${c.html}</td>`
    ).join('') + '</tr>').join('');
  return `<div class="scroll"><table><thead><tr>${ths}</tr></thead>`
       + `<tbody>${trs}</tbody></table></div>`;
}

// ------------------------------------------------------------------ views

async function showMedia() {
  const t0 = performance.now();
  const rows = await run('media');
  $('media-out').innerHTML = table(
    [{ label: 'Medium' }, { label: 'Kind' }, { label: 'Files', num: true },
     { label: 'Size', num: true }, { label: 'Only here', num: true },
     { label: 'Last scan' }, { label: 'Location' }],
    rows.map((r) => [
      { html: `${esc(r.medium_id)} ${durabilityTag(r)}` },
      { html: esc(r.kind) },
      { html: r.file_count.toLocaleString() },
      { html: humanSize(r.byte_count) },
      { html: r.only_here_count
          ? `<span class="tag danger">${r.only_here_count.toLocaleString()}</span>`
          : '0' },
      { html: day(r.last_scanned) },
      { html: esc(r.location_hint ?? '') },
    ]));
  economy('media', t0);

  const sel = $('onlyon-medium');
  sel.innerHTML = rows.map((r) =>
    `<option value="${esc(r.medium_id)}">${esc(r.medium_id)}</option>`).join('');
}

async function showWhereis(target) {
  const out = $('whereis-out');
  if (!target) { out.innerHTML = ''; return; }
  const t0 = performance.now();

  let hash = null;
  if (/^(sha256|unverified):/.test(target)) {
    hash = target;
  } else {
    // Exact path first, then bare filename — the CLI's resolution order.
    const byPath = await run('resolve_by_path', [target.replace(/^\/+/, '')]);
    if (byPath.length) {
      hash = byPath[0].hash;
    } else {
      const byName = await run('resolve_by_name', [target]);
      if (byName.length > 1) {
        out.innerHTML = `<p class="note danger-note">“${esc(target)}” is
          ambiguous — several distinct files share that name. Give a full
          path or a hash.</p>`;
        economy('whereis (ambiguous)', t0);
        return;
      }
      if (byName.length) hash = byName[0].hash;
    }
  }

  if (!hash) {
    out.innerHTML = `<p class="note">“${esc(target)}” is not in this
      catalog.</p>`;
    economy('whereis (miss)', t0);
    return;
  }

  const [places, size] = await Promise.all([
    run('placements', [hash]),
    run('content_size', [hash]),
  ]);
  const media = new Set(places.map((p) => p.medium_id));
  const backups = new Set(places.filter((p) => p.is_backup)
    .map((p) => p.medium_id));

  out.innerHTML = `
    <p class="hash">${esc(hash)} <span class="note">(${
      humanSize(size[0]?.size)})</span></p>
    <p class="note ${backups.size ? '' : 'danger-note'}">on ${media.size}
      ${media.size === 1 ? 'medium' : 'media'}, ${backups.size} of them
      ${backups.size === 1 ? 'a backup' : 'backups'}${
        backups.size ? '' : ' — no backup copy'}</p>` +
    table([{ label: 'Medium' }, { label: 'Path' }, { label: 'Seen' }],
      places.map((p) => [
        { html: `${esc(p.medium_id)} ${p.is_backup
            ? '<span class="tag backup">backup</span>'
            : `<span class="tag">${esc(p.kind)}</span>`}` },
        { html: esc(p.path), cls: 'path' },
        { html: day(p.seen_at) },
      ]));
  economy('whereis', t0);
}

async function showRedundancy(minCopies) {
  const t0 = performance.now();
  const [rows, total] = await Promise.all([
    run('redundancy_rows', [minCopies, 40]),
    run('redundancy_total', [minCopies]),
  ]);
  const n = total[0]?.n ?? 0;
  const head = n
    ? `<p class="note danger-note">${n.toLocaleString()} content objects have
       fewer than ${minCopies} backup ${minCopies === 1 ? 'copy' : 'copies'}
       ${rows.length < n ? `(largest ${rows.length} shown)` : ''}</p>`
    : `<p class="note">Everything has at least ${minCopies} backup
       ${minCopies === 1 ? 'copy' : 'copies'}.</p>`;
  $('redundancy-out').innerHTML = head + (n ? table(
    [{ label: 'Backups', num: true }, { label: 'Copies', num: true },
     { label: 'Size', num: true }, { label: 'Example path' }],
    rows.map((r) => [
      { html: String(r.backup_copies) },
      { html: String(r.copies) },
      { html: humanSize(r.size) },
      { html: esc(r.example_path ?? ''), cls: 'path' },
    ])) : '');
  economy('redundancy', t0);
}

async function showOnlyOn(medium) {
  if (!medium) return;
  const t0 = performance.now();
  const [rows, totals] = await Promise.all([
    run('only_on_rows', [medium, 40]),
    run('only_on_totals', [medium]),
  ]);
  const t = totals[0] ?? { only_here_count: 0, only_here_bytes: 0 };
  $('onlyon-out').innerHTML =
    `<p class="note ${t.only_here_count ? 'danger-note' : ''}">${
      t.only_here_count.toLocaleString()} files (${
      humanSize(t.only_here_bytes)}) exist only on ${esc(medium)}${
      rows.length < t.only_here_count
        ? ` — largest ${rows.length} shown` : ''}</p>` +
    (t.only_here_count ? table(
      [{ label: 'Size', num: true }, { label: 'Path' }],
      rows.map((r) => [
        { html: humanSize(r.size) },
        { html: esc(r.path), cls: 'path' },
      ])) : '');
  economy('only-on', t0);
}

// ------------------------------------------------------------------- boot

function tabs() {
  $('tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-panel]');
    if (!btn) return;
    for (const b of $('tabs').querySelectorAll('button')) {
      b.setAttribute('aria-selected', String(b === btn));
    }
    for (const p of document.querySelectorAll('.panel')) p.classList.remove('on');
    $(`panel-${btn.dataset.panel}`).classList.add('on');
  });

  $('whereis-form').addEventListener('submit', (e) => {
    e.preventDefault();
    showWhereis($('whereis-q').value.trim()).catch(fail);
  });
  $('redundancy-form').addEventListener('submit', (e) => {
    e.preventDefault();
    showRedundancy(Number($('min-copies').value) || 2).catch(fail);
  });
  $('onlyon-form').addEventListener('submit', (e) => {
    e.preventDefault();
    showOnlyOn($('onlyon-medium').value).catch(fail);
  });
}

function fail(err) {
  $('source').textContent = 'error';
  $('economy').textContent = String(err?.message ?? err);
  console.error(err);
}

/** Where the catalog lives: ?db= wins, then ?feed=owner/topic, then config. */
async function catalogUrl(config) {
  const params = new URLSearchParams(location.search);
  const direct = params.get('db') ?? config.db;
  if (direct) return new URL(direct, location.href).href;

  const feed = params.get('feed') ?? config.feed;
  const api = params.get('api') ?? config.api ?? location.origin;
  if (!feed) return new URL(config.name ?? 'catalog.sqlite', location.href).href;

  // Resolved once, at load. A feed can move mid-session; a database must not.
  const [owner, ...rest] = feed.split('/');
  const { reference } = await resolveFeed(api, owner, rest.join('/'));
  return `${api}/bzz/${reference}/${config.name ?? 'catalog.sqlite'}`;
}

async function boot() {
  tabs();
  try {
    const config = await fetch('./config.json')
      .then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
    Q = await fetch('./queries.json').then((r) => r.json());

    const url = await catalogUrl(config);
    db = await open(url, { verify: config.verify ?? false });
    $('source').textContent = config.label ?? 'published catalog';

    const [summary, mediaCount, newest] = await Promise.all([
      run('summary'), run('media_count'), run('newest_scan'),
    ]);
    const s = summary[0];
    const nMedia = mediaCount[0]?.n ?? 0;
    if (s) {
      $('totals').textContent =
        `${nMedia} media · ${s.content_count.toLocaleString()} unique ` +
        `content objects (${humanSize(s.content_bytes)}) · ` +
        `${s.instance_count.toLocaleString()} placements`;
    }

    // Same warning the CLI prints: a published catalog is a snapshot and
    // does not refresh itself, so old facts can look current.
    const last = newest[0]?.newest;
    if (last) {
      const age = (Date.now() / 1000 - last) / DAY;
      if (age >= (config.maxScanAgeDays ?? STALE_DAYS)) {
        const el = $('stale');
        el.hidden = false;
        el.textContent =
          `Nothing here has been scanned since ${day(last)} (${
            age.toFixed(0)} days ago). A published catalog is a snapshot: it
           does not refresh itself.`;
      }
    }

    await showMedia();
  } catch (err) {
    fail(err);
  }
}

boot();
