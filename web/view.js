// Pure logic behind the viewer: escaping, formatting, table building, query
// serialisation and URL resolution. Kept apart from app.js so it can be
// exercised under `node --test` with no DOM, no database and no network.
//
// What this deliberately does not cover is layout. The media table clipping
// at phone width was a real bug, and no stub of a DOM would have found it —
// only a browser computes layout. See web/test/README.md.

export const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export function humanSize(n) {
  n = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  for (const u of units) {
    if (n < 1024 || u === 'TB') {
      return u === 'B' ? `${n.toFixed(0)}B` : `${n.toFixed(1)}${u}`;
    }
    n /= 1024;
  }
}

export const day = (ts) =>
  ts ? new Date(ts * 1000).toISOString().slice(0, 10) : 'never';

// What a medium's copies survive. Green where the copy outlives deletion of
// the original; plain where it does not (a sync mirror is not a backup);
// red where a lease has lapsed, which is a backup you stopped paying for,
// or where hosting nobody else joins in makes it your own machine again.
export function durabilityTag(r) {
  if (r.durability === 'leased' && r.lease_expires) {
    const days = (r.lease_expires * 1000 - Date.now()) / 86400000;
    if (days <= 0) return '<span class="tag danger">lease lapsed</span>';
    const cls = days < 14 ? 'tag danger' : 'tag backup';
    return `<span class="${cls}">leased · ${days.toFixed(0)}d</span>`;
  }
  if (r.durability === 'hosted' && r.replicas != null) {
    const cls = r.replicas < 1 ? 'tag danger' : 'tag backup';
    return `<span class="${cls}">hosted×${r.replicas}</span>`;
  }
  const backs = ['independent', 'leased', 'hosted'].includes(r.durability);
  return `<span class="tag${backs ? ' backup' : ''}">${esc(r.durability)}</span>`;
}

// One query at a time, always. wa-sqlite's async build steps statements on a
// single connection handle and does not serialise: two overlapping queries
// interleave and the second sees a database with no tables in it. Enforced
// here rather than at the call sites so views can still use Promise.all —
// which is how the bug was found, by the page failing to open a catalog that
// the same queries read perfectly when run one after another.
export function makeRunner(query) {
  let queue = Promise.resolve();
  return function run(...args) {
    const result = queue.then(() => query(...args));
    queue = result.catch(() => {});      // a failure must not stall the queue
    return result;
  };
}

export function table(head, rows) {
  if (!rows.length) return '<p class="note">nothing to show.</p>';
  const ths = head.map((h) =>
    `<th class="${h.num ? 'num' : ''}">${esc(h.label)}</th>`).join('');
  const trs = rows.map((cells) =>
    '<tr>' + cells.map((c, i) => {
      const cls = [head[i].num ? 'num' : '', c.cls ?? ''].filter(Boolean)
        .join(' ');
      return `<td${cls ? ` class="${cls}"` : ''}>${c.html}</td>`;
    }).join('') + '</tr>').join('');
  // Tables scroll sideways rather than clipping: the media table needs more
  // width than a phone gives it.
  return `<div class="scroll"><table><thead><tr>${ths}</tr></thead>`
       + `<tbody>${trs}</tbody></table></div>`;
}

/** Where the catalog lives: ?db= wins, then ?feed=owner/topic, then config. */
export async function catalogUrl(config, loc, resolveFeed) {
  const params = new URLSearchParams(loc.search);
  const direct = params.get('db') ?? config.db;
  if (direct) return new URL(direct, loc.href).href;

  const feed = params.get('feed') ?? config.feed;
  const api = params.get('api') ?? config.api ?? loc.origin;
  const name = config.name ?? 'catalog.sqlite';
  if (!feed) return new URL(name, loc.href).href;

  // Resolved once, at load. A feed can move mid-session; a database must not.
  const [owner, ...rest] = feed.split('/');
  const { reference } = await resolveFeed(api, owner, rest.join('/'));
  return `${api}/bzz/${reference}/${name}`;
}

/** Days since `ts`, or null when nothing has ever been recorded. */
export function ageInDays(ts, now = Date.now()) {
  return ts ? (now / 1000 - ts) / 86400 : null;
}
