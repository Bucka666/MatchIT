# Diagnostics

Reusable debugging tools. Nothing here is shipped — these are injected
temporarily, read, then removed.

## Diagnostic pill

| File | What it is |
|---|---|
| `pill_block.html` | The injectable block. Paste at the top of a page's `{% block body %}`. |
| `sw_message_handler.js` | The `message` handler. Paste into `static/sw.js` (anywhere at top level). |
| `contact_with_pill.html` | Reference: `/contact` with the pill in place. |
| `terms_with_pill.html` | Reference: `/terms` with the pill in place. |
| `privacy_with_pill.html` | Reference: `/privacy` with the pill in place. |

The three `*_with_pill.html` files are frozen snapshots showing where the
block sits relative to real page content. They are NOT live templates and
must never be copied back over `templates/` wholesale — they are older
versions of those pages.

### What it renders

A fixed black/green banner at the top of the page:

```
SW:grailsweep-v126 | ctrl:yes | new-markup:13 (w7 i4 lw0 li0 cw1 ci1) | iOS:true | waiting:no
```

| Field | Meaning |
|---|---|
| `SW:` | `CACHE_NAME` of the SW **controlling this document**, via `MessageChannel` with a 2s timeout. `OLD(pre-vNNN)` = no reply, so an older SW is still in control. |
| `ctrl:` | Whether the document is SW-controlled at all. `NONE` = served outside the SW. |
| `new-markup:` | Total count of platform-conditional classes present, with a per-family breakdown. `0` means the device is running stale HTML. |
| `iOS:` | `gsIsRunningInIOSApp()` on this page load. `fn-undefined` = base.html JS never ran. |
| `waiting:` | `YES-STUCK` = a newer SW installed but never activated. |

### Why the counter sums six classes

`w` `gs-web-only` · `i` `gs-ios-only` · `lw` `gs-legal-codes-web` ·
`li` `gs-legal-codes-ios` · `cw` `gs-cancel-web` · `ci` `gs-cancel-ios`

Each page carries a different subset. `/terms` and `/privacy` legitimately
have **zero** `gs-web-only`/`gs-ios-only` and use `gs-legal-codes-*`
instead — a counter watching only the first two would report a correct
page as stale. That false negative was caught in review before it could
send anyone chasing a caching layer that didn't exist. Keep the sum.

### Known-good readings (as of Aug 2026)

```
/contact   new-markup:13  (w7 i4 lw0 li0 cw1 ci1)
/terms     new-markup:6   (w0 i0 lw1 li1 cw2 ci2)
/privacy   new-markup:5   (w0 i0 lw2 li1 cw1 ci1)
```

Re-derive these before trusting them — they change whenever the
platform-conditional markup changes.

### Procedure

1. Paste `pill_block.html` into the target page's `{% block body %}`
2. Paste `sw_message_handler.js` into `static/sw.js`
3. Bump `CACHE_NAME` — without this the pill never reaches the device
4. Deploy, purge Cloudflare, force-quit the app, relaunch
5. Read the banner
6. Remove both blocks, bump `CACHE_NAME` again

Both blocks are marked `TEMPORARY DIAGNOSTIC` so they are greppable.

### Gotcha when removing the SW handler

`event.ports[0].postMessage({ cache: CACHE_NAME });` **ends with `});`**,
so a naive `s.find('});')` cuts at the inner brace and leaves an orphan,
breaking `sw.js`. Match the whole block verbatim, and always
`node --check static/sw.js` afterwards. This has bitten twice.
