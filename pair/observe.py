"""Compact public page observation shared by every actor policy.

The snapshot numbers visible interactive controls (data-pair-id) and returns the page's visible
text. `render` turns it into the text the model sees; the same rendering is stored verbatim in
the episode record, so the ledger knows exactly what was delivered.
"""
from __future__ import annotations

from urllib.parse import urlsplit

SNAPSHOT_JS = r"""caps => {
 document.querySelectorAll('[data-pair-id]').forEach(e => e.removeAttribute('data-pair-id'));
 const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
 const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button]';
 const all = [...document.querySelectorAll(sel)].filter(visible);
 const controls = all.slice(0, caps.controls).map((e, i) => {
  const id = String(i + 1); e.setAttribute('data-pair-id', id);
  const tag = e.tagName.toLowerCase(); const type = (e.getAttribute('type') || '').toLowerCase();
  let label = (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('placeholder') || '').trim();
  if (!label && tag === 'input' && ['submit','button'].includes(type)) label = e.value || '';
  if (!label) { const img = e.querySelector('img[alt]'); if (img) label = img.alt; }
  if (!label) { const icon = e.matches('[class*="suitepicon-"],[class*="glyphicon-"],[class*="fa-"]') ? e : e.querySelector('[class*="suitepicon-"],[class*="glyphicon-"],[class*="fa-"]');
    const m = icon && String(icon.className).match(/(?:suitepicon|glyphicon|fa)-([a-z0-9-]+)/); if (m) label = '[icon: ' + m[1] + ']'; }
  if (!label && e.id) label = '[id: ' + e.id + ']';
  // Interface v2: expose the open/closed state of controls that toggle a hidden region.
  const ae = e.getAttribute('aria-expanded');
  if (ae === 'false' || (e.getAttribute('data-toggle') === 'collapse' && e.classList.contains('collapsed'))) label += ' [collapsed]';
  else if (ae === 'true') label += ' [expanded]';
  const form = e.form || e.closest('form');
  return {id, tag, type, label: label.replace(/\s+/g, ' ').slice(0, caps.label),
   name: e.getAttribute('name') || '', value: type === 'password' ? '[hidden]' : (['input','textarea','select'].includes(tag) ? (e.value || '') : ''),
   checked: ['checkbox','radio'].includes(type) ? e.checked : undefined,
   href: tag === 'a' ? e.href : undefined, disabled: !!e.disabled,
   form_action: form ? form.action : undefined, form_id: form ? (form.id || form.getAttribute('name') || '') : undefined,
   options: tag === 'select' ? [...e.options].slice(0, 60).map(o => ({value: o.value, text: o.text.trim()})) : undefined};
 });
 const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n');
 return {url: location.href, title: document.title, text: text.slice(0, caps.text), controls,
  limits: {text_chars: text.length, text_sent: Math.min(text.length, caps.text), text_truncated: text.length > caps.text,
           controls_total: all.length, controls_sent: controls.length, controls_truncated: all.length > caps.controls}};
}"""


def _short(href: str, page_url: str) -> str:
    a, b = urlsplit(href), urlsplit(page_url)
    if (a.scheme, a.netloc) == (b.scheme, b.netloc):
        return a.path + (("?" + a.query) if a.query else "")
    return href


def render(obs: dict) -> str:
    """Model-facing page text. Deterministic; truncation is stated, never silent."""
    if obs.get("dialog"):
        d = obs["dialog"]
        return (f"PAGE {obs['url']}\nA browser {d['type']} dialog is open: {d['message']!r}\n"
                "Reply with accept_dialog or dismiss_dialog.")
    lim = obs["limits"]
    lines = [f"PAGE {obs['url']}", f"TITLE {obs['title']}",
             f"TEXT ({lim['text_sent']} of {lim['text_chars']} chars{', truncated' if lim['text_truncated'] else ''}):",
             obs["text"].strip(),
             f"CONTROLS ({lim['controls_sent']} of {lim['controls_total']}{', truncated' if lim['controls_truncated'] else ''}):"]
    for c in obs["controls"]:
        parts = [f"[{c['id']}] {c['tag']}" + (f"/{c['type']}" if c['type'] and c['tag'] == 'input' else "")]
        if c["label"]:
            parts.append(repr(c["label"]))
        if c["name"]:
            parts.append(f"name={c['name']}")
        if c["value"] and c["tag"] != "select":
            parts.append(f"value={c['value']!r}")
        if c.get("checked") is not None:
            parts.append("checked" if c["checked"] else "unchecked")
        if c.get("href"):
            parts.append("-> " + _short(c["href"], obs["url"]))
        if c["disabled"]:
            parts.append("disabled")
        if c.get("options"):
            chosen = c["value"]
            parts.append("options: " + " | ".join(f"{o['value']}={o['text']}" + (" (selected)" if o["value"] == chosen else "")
                                                  for o in c["options"]))
        lines.append(" ".join(parts))
    return "\n".join(lines)


def stub(step: int, obs: dict) -> str:
    return f"[observation {step}: {obs['url']} - {obs.get('title', '')} - removed from context; rely on your notes]"
