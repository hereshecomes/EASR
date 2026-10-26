"""Helpers shared by the application adapters: table diffs and network boundary."""
from __future__ import annotations

from urllib.parse import urlsplit


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def make_route_handler(allowed_origins: set[str], blocked_log: list, blocked_patterns: tuple = ()):
    """Abort requests outside the registered origins and declared background endpoints."""
    async def handler(route):
        url = route.request.url
        if any(p in url for p in blocked_patterns):
            blocked_log.append({"url": url[:300], "resource_type": route.request.resource_type, "reason": "declared_background_endpoint"})
            await route.abort("blockedbyclient")
        elif urlsplit(url).scheme in ("http", "https") and origin(url) not in allowed_origins:
            blocked_log.append({"url": url[:300], "resource_type": route.request.resource_type})
            await route.abort("blockedbyclient")
        else:
            await route.continue_()
    return handler


def diff_tables(before: dict, after: dict, classify) -> list[dict]:
    """Row-level differences between two {table: {key: row_dict}} snapshots.

    classify(table, column) -> 'business' | 'background' | 'unlisted'. A changed row yields one
    record per changed column so background columns (e.g. last_login) separate from business ones.
    """
    out = []
    for table in sorted(set(before) | set(after)):
        b, a = before.get(table, {}), after.get(table, {})
        for key in sorted(set(b) | set(a), key=str):
            rb, ra = b.get(key), a.get(key)
            if rb == ra:
                continue
            if rb is None or ra is None:
                op = "insert" if rb is None else "delete"
                row = ra if rb is None else rb
                out.append({"table": table, "key": key, "op": op, "row": row, "class": classify(table, None)})
                continue
            for col in sorted(set(rb) | set(ra)):
                # NULL and empty string are the same field value for these applications' forms.
                if rb.get(col) != ra.get(col) and not (rb.get(col) in (None, "") and ra.get(col) in (None, "")):
                    out.append({"table": table, "key": key, "op": "update", "column": col,
                                "before": rb.get(col), "after": ra.get(col), "class": classify(table, col)})
    return out


def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # ignore TIME_WAIT; a live listener still blocks
        try:
            sk.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def kill_group(proc) -> None:
    """Terminate a server started with start_new_session=True, including any forked workers."""
    import os
    import signal
    import subprocess
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(5)


# ---- page-side helpers for the v2 validation family (pair/guards.py) ---------------------------------
# Effective write footprint of the form that holds control `id`: fields whose current value differs from
# the value the page loaded with (whether the agent or page script changed it).
FOOTPRINT_JS = r"""(id) => {
  const el = document.querySelector(`[data-pair-id="${id}"]`);
  const form = el && (el.form || el.closest('form'));
  if (!form) return null;
  const changed = [], unchanged = [], load = {};
  for (const e of form.querySelectorAll('input[name],textarea[name],select[name]')) {
    if (['button', 'submit', 'image', 'reset', 'hidden', 'file'].includes(e.type) || e.disabled) continue;
    let diff, initial;
    if (e.tagName === 'SELECT') { diff = [...e.options].some(o => o.selected !== o.defaultSelected);
      const o = [...e.options].find(o => o.defaultSelected); initial = o ? o.value : (e.options[0] ? e.options[0].value : ''); }
    else if (e.type === 'checkbox' || e.type === 'radio') { diff = e.checked !== e.defaultChecked; initial = e.defaultChecked ? '1' : '0'; }
    else { diff = e.value !== e.defaultValue; initial = e.defaultValue; }
    if (!(e.name in load)) load[e.name] = initial;
    (diff ? changed : unchanged).push(e.name);
  }
  return {changed: [...new Set(changed)], unchanged: [...new Set(unchanged)].filter(n => !changed.includes(n)), load};
}"""

# Withhold untouched fields from the submission (disabled inputs are not posted), together with the hidden
# id input of an untouched relate field (account_name -> account_id). A multi-input widget is one unit: SuiteCRM's
# email widget is withheld entirely, hidden control inputs included, when none of its inputs changed (then the
# server does not touch the addresses), and submitted entirely when any of them changed.
STRIP_JS = r"""({id, names, changed}) => {
  const el = document.querySelector(`[data-pair-id="${id}"]`);
  const form = el && (el.form || el.closest('form'));
  if (!form) return [];
  const widget = n => /emailAddress|email_widget|EmailWidget/i.test(n || '');
  const widgetChanged = (changed || []).some(widget);
  const keep = new Set((names || []).filter(n => !widget(n))), off = [];
  if (!widgetChanged) {
    for (const e of form.querySelectorAll('input[name],select[name],textarea[name]')) {
      if (widget(e.name) && !['button', 'submit'].includes(e.type)) { e.disabled = true; e.setAttribute('data-pair-withheld', '1'); off.push(e.name); }
    }
  }
  for (const e of form.querySelectorAll('input[name],textarea[name],select[name]')) {
    if (keep.has(e.name) && !['hidden', 'button', 'submit'].includes(e.type)) { e.disabled = true; e.setAttribute('data-pair-withheld', '1'); off.push(e.name); }
  }
  for (const n of names) {                 // a hidden presence flag travels with its field (MantisBT custom fields)
    const pres = form.querySelector(`input[type=hidden][name="${n}_presence"]`);
    if (pres && keep.has(n)) { pres.disabled = true; pres.setAttribute('data-pair-withheld', '1'); off.push(pres.name); }
  }
  for (const n of names) {
    if (!n.endsWith('_name')) continue;
    const h = form.querySelector(`input[type=hidden][name="${n.slice(0, -5)}_id"]`);
    if (h) { h.disabled = true; h.setAttribute('data-pair-withheld', '1'); off.push(h.name); }
  }
  return [...new Set(off)];
}"""

# App tier: attach the condition to the agent's own submission.
ATTACH_JS = r"""({id, value}) => {
  const el = document.querySelector(`[data-pair-id="${id}"]`);
  const form = el && (el.form || el.closest('form'));
  if (!form) return false;
  let h = form.querySelector('input[name=pair_condition]');
  if (!h) { h = document.createElement('input'); h.type = 'hidden'; h.name = 'pair_condition'; form.appendChild(h); }
  h.value = value;
  return true;
}"""

# Load values of the main edit form on a freshly fetched page.
FORM_VALUES_JS = r"""() => {
  const form = document.querySelector('form#EditView') || document.querySelector('form');
  const out = {};
  if (!form) return out;
  for (const e of form.querySelectorAll('input[name],textarea[name],select[name]')) {
    if (['button', 'submit', 'image', 'reset', 'hidden', 'file'].includes(e.type)) continue;
    if (e.tagName === 'SELECT') { const o = [...e.options].find(o => o.defaultSelected); out[e.name] = o ? o.value : (e.options[0] ? e.options[0].value : ''); }
    else if (e.type === 'checkbox' || e.type === 'radio') out[e.name] = e.defaultChecked ? '1' : '0';
    else out[e.name] = e.defaultValue;
  }
  return out;
}"""

# If a validated click did not submit (for example the page's own form validation stopped it), give the agent
# its form back as it was: re-enable withheld inputs and drop an unsent condition.
RESTORE_JS = r"""() => {
  let n = 0;
  document.querySelectorAll('[data-pair-withheld]').forEach(e => { e.disabled = false; e.removeAttribute('data-pair-withheld'); n++; });
  document.querySelectorAll('input[name=pair_condition]').forEach(e => { e.remove(); n++; });
  return n;
}"""


def enter_as_click(act: dict, obs: dict) -> dict:
    """Enter pressed on a button activates it exactly as a click does (browser behaviour), so a save submitted that way is the same commit.
    Added 2026-10-22: Phi-4 and Mistral-Nemo often save this way, and commit detection used to recognise clicks only."""
    if act.get("type") == "press" and act.get("key") == "Enter":
        c = next((c for c in (obs or {}).get("controls", []) if c.get("id") == str(act.get("id"))), None)
        if c and (c.get("tag") == "button" or c.get("type") in ("submit", "button", "image")):
            return dict(act, type="click")
    return act
