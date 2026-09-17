// Tests for web/view.js — `node --test web/test/`.
//
// No dependencies, no DOM, no network. These pin the logic; the README in
// this directory says plainly what they cannot reach.

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  ageInDays, catalogUrl, day, durabilityTag, esc, humanSize, makeRunner,
  table,
} from '../view.js';

describe('escaping', () => {
  it('neutralises markup in values that come from the catalog', () => {
    // Paths are user data. A file called `<img onerror=...>` must render as
    // text, not as an element.
    const out = esc('<img src=x onerror="alert(1)">');
    assert.ok(!out.includes('<'), out);
    assert.ok(!out.includes('"'), out);
    assert.equal(esc("it's"), 'it&#39;s');
  });

  it('renders null and undefined as empty rather than as words', () => {
    assert.equal(esc(null), '');
    assert.equal(esc(undefined), '');
  });
});

describe('humanSize', () => {
  it('keeps bytes whole and scales with one decimal', () => {
    assert.equal(humanSize(0), '0B');
    assert.equal(humanSize(1023), '1023B');
    assert.equal(humanSize(1024), '1.0KB');
    assert.equal(humanSize(5 * 1024 ** 3), '5.0GB');
  });

  it('reads a missing number as zero, the way SUM() comes back', () => {
    assert.equal(humanSize(null), '0B');
    assert.equal(humanSize(undefined), '0B');
  });
});

describe('day', () => {
  it('says never rather than 1970 when nothing was recorded', () => {
    assert.equal(day(null), 'never');
    assert.equal(day(0), 'never');
  });

  it('formats a timestamp as a date', () => {
    assert.equal(day(Date.UTC(2026, 8, 17) / 1000), '2026-09-17');
  });
});

describe('durabilityTag', () => {
  it('marks as backup only what survives deleting the original', () => {
    assert.ok(durabilityTag({ durability: 'independent' }).includes('backup'));
    assert.ok(!durabilityTag({ durability: 'mirror' }).includes('backup'));
    assert.ok(!durabilityTag({ durability: 'working' }).includes('backup'));
  });

  it('shows a lapsed lease as danger, not as a backup', () => {
    const past = (Date.now() - 86400000) / 1000;
    const out = durabilityTag({ durability: 'leased', lease_expires: past });
    assert.ok(out.includes('danger'), out);
    assert.ok(out.includes('lapsed'), out);
  });

  it('warns while a lease is inside the margin', () => {
    const soon = (Date.now() + 5 * 86400000) / 1000;
    assert.ok(durabilityTag({ durability: 'leased', lease_expires: soon })
      .includes('danger'));
    const later = (Date.now() + 300 * 86400000) / 1000;
    assert.ok(durabilityTag({ durability: 'leased', lease_expires: later })
      .includes('tag backup'));
  });

  it('shows hosting nobody else joins as danger', () => {
    // A Radicle repo seeded only by your own node is your own node.
    assert.ok(durabilityTag({ durability: 'hosted', replicas: 0 })
      .includes('danger'));
    assert.ok(durabilityTag({ durability: 'hosted', replicas: 3 })
      .includes('tag backup'));
  });

  it('leaves a single-custodian hub alone when no count applies', () => {
    const out = durabilityTag({ durability: 'hosted' });
    assert.ok(out.includes('backup'), out);
    assert.ok(!out.includes('×'), out);
  });
});

describe('table', () => {
  it('says so plainly when there is nothing to show', () => {
    assert.match(table([{ label: 'A' }], []), /nothing to show/);
  });

  it('wraps in the scroller, so a wide table cannot clip', () => {
    const out = table([{ label: 'A' }], [[{ html: 'x' }]]);
    assert.ok(out.startsWith('<div class="scroll">'), out);
  });

  it('escapes headers but passes cell html through as built', () => {
    const out = table([{ label: '<b>' }], [[{ html: '<span>ok</span>' }]]);
    assert.ok(!out.includes('<b>'), 'header must be escaped');
    assert.ok(out.includes('<span>ok</span>'), 'cells are pre-built html');
  });

  it('marks numeric columns for right alignment', () => {
    const out = table([{ label: 'N', num: true }], [[{ html: '1' }]]);
    assert.match(out, /<th class="num">/);
    assert.match(out, /<td class="num">/);
    // and no class attribute at all when there is nothing to say
    const plain = table([{ label: 'A' }], [[{ html: 'x' }]]);
    assert.match(plain, /<td>x<\/td>/);
  });
});

describe('makeRunner', () => {
  it('never lets two queries overlap', async () => {
    // The bug this exists for: wa-sqlite steps statements on one connection
    // handle and does not serialise, so overlapping queries interleave and
    // the second sees a database with no tables in it.
    let active = 0;
    let maxActive = 0;
    const run = makeRunner(async () => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((r) => setTimeout(r, 5));
      active -= 1;
    });
    await Promise.all([run(), run(), run(), run()]);
    assert.equal(maxActive, 1, 'queries must be strictly serial');
  });

  it('runs them in the order they were asked for', async () => {
    const order = [];
    const run = makeRunner(async (n) => {
      await new Promise((r) => setTimeout(r, 10 - n));
      order.push(n);
    });
    await Promise.all([run(1), run(2), run(3)]);
    assert.deepEqual(order, [1, 2, 3]);
  });

  it('keeps going after one query fails', async () => {
    let calls = 0;
    const run = makeRunner(async (fail) => {
      calls += 1;
      if (fail) throw new Error('boom');
      return 'ok';
    });
    await assert.rejects(run(true));
    assert.equal(await run(false), 'ok', 'a failure must not stall the queue');
    assert.equal(calls, 2);
  });
});

describe('catalogUrl', () => {
  const loc = (search = '') => ({
    search,
    href: 'http://host/bzz/ROOT/index.html',
    origin: 'http://host',
  });

  it('defaults to the catalog beside the page', async () => {
    assert.equal(await catalogUrl({}, loc()),
      'http://host/bzz/ROOT/catalog.sqlite');
  });

  it('honours a configured name', async () => {
    assert.equal(await catalogUrl({ name: 'other.db' }, loc()),
      'http://host/bzz/ROOT/other.db');
  });

  it('lets ?db= override everything', async () => {
    assert.equal(
      await catalogUrl({ feed: 'owner/topic' }, loc('?db=http://x/y.db')),
      'http://x/y.db');
  });

  it('resolves a feed once, at load', async () => {
    let calls = 0;
    const resolve = async (api, owner, topic) => {
      calls += 1;
      assert.equal(owner, 'owner');
      assert.equal(topic, 'my/topic');
      return { reference: 'REF' };
    };
    const url = await catalogUrl({ feed: 'owner/my/topic' }, loc(), resolve);
    assert.equal(url, 'http://host/bzz/REF/catalog.sqlite');
    assert.equal(calls, 1, 'a feed can move mid-session; a database must not');
  });
});

describe('ageInDays', () => {
  it('is null when nothing was ever recorded', () => {
    assert.equal(ageInDays(null), null);
  });

  it('counts days back from now', () => {
    const now = Date.now();
    assert.equal(Math.round(ageInDays(now / 1000 - 200 * 86400, now)), 200);
  });
});
