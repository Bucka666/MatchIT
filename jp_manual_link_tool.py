"""
jp_manual_link_tool.py — localhost UI for manually linking the JP cards OCR
couldn't read a collector number for.

Walks the queue built by jp_manual_link_queue.py (the "failed" cardIDs across
jp_cardid_maps/*_map.json) one card at a time. Typing/clicking a number calls
jp_manual_link_queue.submit_number(), which reuses the exact same functions
the automated pipeline uses (link_jp_skus.sku_exists / insert_jp_sku_links.
link_one) — this tool only ever supplies the one input OCR couldn't produce.

Run:
    python jp_manual_link_tool.py
Then open http://127.0.0.1:5057/

Embedding (Phase 3) is triggered on demand from the page, not automatically —
click "Run embed batch" once you're done for the session (or periodically).
It runs locally on CPU/GPU (whatever this machine has), no Modal needed. The
final `modal run smart_upload.py --db-and-cache --set-metadata
--identifier-lookup` push is intentionally NOT wired into this tool — that
touches the production Modal volume and should stay an explicit, separate
command you run yourself.
"""
import os
import re

from flask import Flask, jsonify, request, send_file, abort

import jp_manual_link_queue as Q

PORT = 5057
SETCODE_RE = re.compile(r"^[a-z0-9]+$")
CARDID_RE = re.compile(r"^\d+$")

app = Flask(__name__)

_queue = []
_pos = 0


def _load_queue():
    global _queue, _pos
    _queue = Q.get_pending_queue()
    _pos = 0


def _current():
    if _pos >= len(_queue):
        return None
    return _queue[_pos]


def _advance():
    global _pos
    _pos += 1


PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>JP Manual Link Tool</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; max-width: 640px; margin: 30px auto; color: #222; }
  #progress { color: #666; margin-bottom: 8px; }
  #setinfo { color: #444; margin-bottom: 16px; font-size: 14px; }
  img#card { max-width: 320px; display: block; margin: 0 auto 16px; border: 1px solid #ccc; }
  .shortlist { margin-bottom: 16px; }
  .shortlist button { margin: 2px; padding: 4px 8px; font-size: 13px; cursor: pointer; }
  .shortlist button.imaged { background: #eee; color: #999; }
  #numberform { display: flex; gap: 8px; margin-bottom: 12px; }
  #number { font-size: 18px; padding: 6px; width: 100px; }
  button#submitBtn, button#skipBtn { font-size: 16px; padding: 6px 14px; cursor: pointer; }
  #status { margin-top: 16px; padding: 10px; border-radius: 4px; white-space: pre-wrap; }
  .ok { background: #e6f6e6; }
  .err { background: #fbe6e6; }
  #embedArea { margin-top: 30px; border-top: 1px solid #ddd; padding-top: 16px; }
  #done { font-size: 18px; }
</style>
</head>
<body>
<h2>JP Manual Link Tool</h2>
<div id="app">Loading...</div>

<div id="embedArea">
  <button id="embedBtn">Run embed batch (CPU/GPU, local)</button>
  <div id="embedStatus" style="margin-top:8px; white-space: pre-wrap;"></div>
</div>

<script>
async function loadState() {
  const r = await fetch('/api/state');
  const s = await r.json();
  render(s);
}

function render(s) {
  const app = document.getElementById('app');
  if (!s.card) {
    app.innerHTML = '<div id="done">Queue complete for this session — ' + s.total + ' total, 0 remaining.<br>'
      + 'Run the embed batch below, then push with:<br>'
      + '<code>modal run smart_upload.py --db-and-cache --set-metadata --identifier-lookup</code></div>';
    return;
  }
  const shortlist = s.unlinked_numbers.map(n =>
    `<button onclick="submitNumber('${n.toString().padStart(3,'0')}')">${n.toString().padStart(3,'0')}</button>`
  ).join(' ');
  const imagedCount = s.imaged_numbers.length;

  app.innerHTML = `
    <div id="progress">${s.pos + 1} / ${s.total} in this session (set ${s.set_index}/${s.set_count})</div>
    <div id="setinfo">Set <b>jpn-${s.setcode}</b> — known numbers ${s.known_range}
      (printed_total: ${s.printed_total === null ? 'unknown' : s.printed_total}),
      ${imagedCount} already imaged in this set</div>
    <img id="card" src="/image/${s.setcode}/${s.card_id}">
    <div class="shortlist"><b>Unlinked existing SKUs — click to link:</b><br>${shortlist || '(none unlinked — type a number)'}</div>
    <form id="numberform" onsubmit="return false;">
      <input id="number" type="text" placeholder="e.g. 071" autofocus>
      <button id="submitBtn" onclick="submitTyped()">Link</button>
      <button id="skipBtn" onclick="skipCard()">Skip for now</button>
    </form>
    <div id="status"></div>
  `;
  document.getElementById('number').addEventListener('keydown', e => {
    if (e.key === 'Enter') submitTyped();
  });
}

function submitTyped() {
  const n = document.getElementById('number').value.trim();
  if (!n) return;
  submitNumber(n);
}

async function submitNumber(number, confirmNew) {
  const body = { number: number };
  if (confirmNew) body.confirm_new = true;
  const r = await fetch('/api/submit', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const s = await r.json();
  const statusEl = document.getElementById('status');

  if (s.outcome === 'needs_confirm') {
    const ok = confirm(`${s.setcode} currently covers ${s.known_range}. Create ${s.sku} as a new secret rare?`);
    if (ok) return submitNumber(number, true);
    return;
  }
  if (s.outcome === 'error') {
    if (statusEl) { statusEl.className = 'err'; statusEl.textContent = 'ERROR: ' + s.error; }
    return;
  }
  // linked / already_covered — advance
  await loadState();
}

async function skipCard() {
  await fetch('/api/skip', { method: 'POST' });
  await loadState();
}

document.getElementById('embedBtn').onclick = async () => {
  const el = document.getElementById('embedStatus');
  el.textContent = 'Running (this can take a while for a large batch)...';
  const r = await fetch('/api/embed', { method: 'POST' });
  const s = await r.json();
  el.textContent = JSON.stringify(s, null, 2);
};

loadState();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return PAGE


@app.route("/api/state")
def api_state():
    item = _current()
    if item is None:
        return jsonify({"card": None, "total": len(_queue)})

    store = Q.get_store()
    setcode = item["setcode"]
    all_nums = store.set_numbers(setcode)
    imaged_skus = Q.already_imaged_skus(setcode)
    imaged_numbers = sorted(
        int(sku.rsplit("-", 1)[1]) for sku in imaged_skus if sku.rsplit("-", 1)[1].isdigit()
    )
    unlinked = [n for n in all_nums if n not in set(imaged_numbers)]

    set_codes_in_queue = sorted({q["setcode"] for q in _queue})
    set_index = set_codes_in_queue.index(setcode) + 1 if setcode in set_codes_in_queue else 0

    return jsonify({
        "card": True,
        "pos": _pos,
        "total": len(_queue),
        "setcode": setcode,
        "card_id": item["card_id"],
        "known_range": f"{all_nums[0]:03d}-{all_nums[-1]:03d}" if all_nums else "none yet",
        "printed_total": store.printed_total(setcode),
        "unlinked_numbers": unlinked,
        "imaged_numbers": imaged_numbers,
        "set_index": set_index,
        "set_count": len(set_codes_in_queue),
    })


@app.route("/api/submit", methods=["POST"])
def api_submit():
    item = _current()
    if item is None:
        return jsonify({"outcome": "error", "error": "no current card"})

    payload = request.get_json(force=True) or {}
    number = payload.get("number")
    confirm_new = bool(payload.get("confirm_new"))

    result = Q.submit_number(item["setcode"], item["card_id"], item["local_path"], number, confirm_new)
    if result["outcome"] in ("linked", "already_covered"):
        _advance()
    return jsonify(result)


@app.route("/api/skip", methods=["POST"])
def api_skip():
    item = _current()
    if item is not None:
        Q._append_log({
            "setcode": item["setcode"], "card_id": item["card_id"],
            "number": None, "sku": None, "class": None,
            "action": "skipped_by_operator",
        })
        _advance()
    return jsonify({"ok": True})


@app.route("/api/embed", methods=["POST"])
def api_embed():
    from incremental_embed import run_incremental_embed
    summary = run_incremental_embed(category_filter="POKEMON")
    return jsonify(summary)


@app.route("/image/<setcode>/<card_id>")
def image(setcode, card_id):
    if not SETCODE_RE.match(setcode) or not CARDID_RE.match(card_id):
        abort(400)
    path = os.path.join(Q.CARDSDB_DIR, f"jpn-{setcode}", f"{card_id}.jpg")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


if __name__ == "__main__":
    _load_queue()
    print(f"[JP-LINK-TOOL] {len(_queue)} cards pending (resume-skipped anything already in images.db)")
    print(f"[JP-LINK-TOOL] session log: {Q.SESSION_LOG_PATH}")
    print(f"[JP-LINK-TOOL] open http://127.0.0.1:{PORT}/")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
