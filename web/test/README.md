# Viewer tests

`node --test web/test/` — no dependencies, no browser, no network.

What is covered: everything in `web/view.js`. Escaping, formatting, the
durability tag, table building, the query serialiser, and catalog URL
resolution.

## What is not covered, and cannot be

**Layout.** The media table clipping at phone width was a real bug found by
driving a browser and measuring; nothing here computes layout, so nothing
here would have caught it.

**The wasm reader.** Concurrency against a real `wa-sqlite` connection is
what produced "no such table: media"; these tests pin the *rule* that came
out of it — one query at a time — but not the engine behaviour underneath.

So a change to `index.html`'s CSS, or to how `app.js` wires the DOM, still
wants a look in a real browser before publishing. Serving it locally is
enough: `python web/serve.py --db <catalog>`.
