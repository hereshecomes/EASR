"""EASR: effect-aware selective repair (paper, Section 3), browser-only tier, HTML-form path.

Per covered commit the controller builds the operation contract C(a) = (I, D, W_hat, G, P) from permitted sources only:
  W_hat  what the submission will carry: the browser's own FormData for the agent's form (hidden inputs included), grouped into units
         (a field with its hidden companions: relate ids, presence flags, widget control inputs); unattributed hidden inputs are their
         own units
  I      units whose submitted value differs from the value the page loaded with
  D      the task contract's declared dependency facts (as Repair v1)
Predicates (value-aware, paper Section 3.3):
  DecisionInvalid    a dependency the agent read has a different decision-relevant value now
  WriteInterference  a unit in W_hat \\ I has a current value different from its load value (the stale copy would overwrite it), or a
                     unit in I was changed by someone else to a value different from the one the agent submits (equal = convergent)
Classes A (neither), B (decision only), C (interference only), D (both). Decisions:
  A CONTINUE (omission-safe applications still submit only I, as Repair v1 does, which also covers the check-to-commit window)
  C on untouched units REBASE: submit only I (omission-safe) or rewrite the interfered units to their current values (otherwise);
    no hold, no model call
  C on an intended unit, B, D  LOCAL_REPLAN: hold, name the changed facts, show the current page
  insufficient evidence (the form's structure changed, a rewrite was impossible)  SAFE_HOLD
  repair budget (3 holds per task) spent  CONTINUE with a declared note
After a passed commit VERIFY_OUTCOME re-reads the target form and the dependencies once and reports intended units that were not
applied (dropped or partial write), interfered units no longer at their new value (lost update) and dependencies that changed during
the save. Nothing is retried automatically.
Request path (SPA JSON writes, e.g. BreakingWeb): W_hat is the body of the agent's own outgoing write request, intercepted in its
own browser; load values are the JSON the page's GET returned; the decision is taken when the request is visible: a hold answers the
request with 409 and is reported to the agent after the click; REBASE forwards the request with only I (omission-safe) or with
interfered keys rewritten to their current values.
Variants used as baselines and ablations are switches of the same code (pair/controllers.py).
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from .guards import GuardV2, semantic

FORMDATA_JS = r"""(id) => {
  const el = document.querySelector(`[data-pair-id="${id}"]`);
  const form = el && (el.form || el.closest('form'));
  if (!form) return null;
  const sent = {};
  for (const [k, v] of new FormData(form).entries()) (sent[k] = sent[k] || []).push(typeof v === 'string' ? v : '[file]');
  const load = {}, kind = {};
  for (const e of form.querySelectorAll('input[name],select[name],textarea[name]')) {
    if (e.disabled || ['button', 'submit', 'image', 'reset', 'file'].includes(e.type)) continue;
    let v;
    if (e.tagName === 'SELECT') { const o = [...e.options].filter(o => o.defaultSelected); v = (o.length ? o : [...e.options].slice(0, 1)).map(o => o.value); }
    else if (e.type === 'checkbox' || e.type === 'radio') v = e.defaultChecked ? [e.value] : [];
    else v = [e.type === 'hidden' ? e.getAttribute('value') ?? '' : e.defaultValue];
    load[e.name] = (load[e.name] || []).concat(v);
    kind[e.name] = kind[e.name] === 'visible' ? 'visible' : (e.type === 'hidden' ? 'hidden' : 'visible');
  }
  return {sent, load, kind, action: form.getAttribute('action') || ''};
}"""

# The same view of a freshly loaded copy of the form (separate session): the form sharing most names with the agent's form.
FRESH_JS = r"""(names) => {
  const want = new Set(names);
  let best = null, score = -1;
  for (const f of document.querySelectorAll('form')) {
    const s = [...f.querySelectorAll('[name]')].filter(e => want.has(e.name)).length;
    if (s > score) { best = f; score = s; }
  }
  if (!best) return {};
  const out = {};
  for (const e of best.querySelectorAll('input[name],select[name],textarea[name]')) {
    if (e.disabled || ['button', 'submit', 'image', 'reset', 'file'].includes(e.type)) continue;
    let v;
    if (e.tagName === 'SELECT') { const o = [...e.options].filter(o => o.defaultSelected); v = (o.length ? o : [...e.options].slice(0, 1)).map(o => o.value); }
    else if (e.type === 'checkbox' || e.type === 'radio') v = e.defaultChecked ? [e.value] : [];
    else v = [e.type === 'hidden' ? e.getAttribute('value') ?? '' : e.defaultValue];
    out[e.name] = (out[e.name] || []).concat(v);
  }
  return out;
}"""

# Withhold (disable) the given inputs, and set given inputs to given values, in the agent's own form.
APPLY_JS = r"""({id, withhold, rewrite}) => {
  const el = document.querySelector(`[data-pair-id="${id}"]`);
  const form = el && (el.form || el.closest('form'));
  const out = {withheld: [], rewritten: [], failed: []};
  if (!form) return out;
  const w = new Set(withhold || []);
  for (const e of form.querySelectorAll('input[name],select[name],textarea[name]')) {
    if (w.has(e.name) && !['button', 'submit'].includes(e.type) && !e.disabled) {
      e.disabled = true; e.setAttribute('data-pair-withheld', '1'); out.withheld.push(e.name);
    }
  }
  for (const [name, vals] of Object.entries(rewrite || {})) {
    const els = [...form.querySelectorAll('[name]')].filter(e => e.name === name);
    let ok = els.length > 0;
    for (const e of els) {
      if (e.tagName === 'SELECT') { for (const o of e.options) o.selected = vals.includes(o.value); }
      else if (e.type === 'checkbox' || e.type === 'radio') e.checked = vals.includes(e.value);
      else if (els.length === 1) e.value = vals[0] ?? '';
      else ok = false;
    }
    (ok ? out.rewritten : out.failed).push(name);
  }
  return out;
}"""

CONTROL = re.compile(r"(token|csrf|^module$|^action$|^record$|^return_|^relate_|^offset$|^isDuplicate$|^bug_id$|^last_updated$|^bugnote_text$|"
                     r"^private$|^time_tracking$|^form-(TOTAL|INITIAL|MIN|MAX)_FORMS$|^form-\d+-id$|^session|^contact_role$|^inbound_email_id$|"
                     r"^email_id$|^pair_|^action_type$|^button$|^save$)", re.I)
WIDGET = re.compile(r"^[A-Za-z]+\d+emailAddress|^(useEmailWidget|emailAddressWidget|\w+_email_widget_id)$")
STRUCTURE = re.compile(r"^form-\d+-id$")      # formset line identity: a change means lines were added, removed or reordered


def units(names: list, kind: dict) -> dict:
    """Group submitted business names into units: unit -> member names (lead first). Hidden companions join their visible field."""
    out = {n: [n] for n in names if kind.get(n) == "visible" and not WIDGET.search(n)}
    email = [n for n in names if WIDGET.search(n)]
    if email:
        out["email_widget"] = sorted(email, key=lambda n: (kind.get(n) != "visible", n))
    for n in names:
        if kind.get(n) != "hidden" or n in email:
            continue
        base = re.sub(r"_presence$", "", n)
        partner = base if base in out else (base[:-3] + "_name" if base.endswith("_id") and base[:-3] + "_name" in out else None)
        if partner:
            out[partner].append(n)
        else:
            out["hidden:" + n] = [n]
    return out


def joined(vals) -> str:
    return "|".join(vals or [])


PHONE = re.compile(r"^\+?[\d\s().\-]{6,}$")


def same_val(field: str, a, b) -> bool:
    """Value-aware equality: the semantic normalizer, plus formatting-equivalent phone numbers (applications store E.164)."""
    a, b = "" if a is None else str(a), "" if b is None else str(b)
    if semantic(field, a) == semantic(field, b):
        return True
    if PHONE.match(a.strip()) and PHONE.match(b.strip()):
        return re.sub(r"[^\d+]", "", a) == re.sub(r"[^\d+]", "", b)
    return False


def rewrite_body(ctype: str, body: bytes, withhold: set, rewrite: dict):
    """Drop withheld keys from, and set rewritten keys in, the agent's own form submission (urlencoded or multipart).
    Returns (new body or None when the format is not understood, record of what the request carried and what changed)."""
    if "application/x-www-form-urlencoded" in ctype or not ctype:
        pairs = parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)
        keys = {k for k, _ in pairs}
        out = [(k, v) for k, v in pairs if k not in withhold and k not in rewrite]
        out += [(k, v) for k, vals in rewrite.items() if k in keys for v in vals]
        return urlencode(out).encode(), {"format": "urlencoded", "keys": sorted(keys), "removed": sorted(keys & set(withhold)),
                                         "rewritten": sorted(keys & set(rewrite))}
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if "multipart/form-data" in ctype and m:
        b = m.group(1).encode()
        parts = body.split(b"--" + b)
        kept, keys, removed, rewritten = [], set(), set(), set()
        for part in parts[1:-1]:
            head, _, _content = part.partition(b"\r\n\r\n")
            nm = re.search(rb'name="([^"]*)"', head)
            name = nm.group(1).decode() if nm else None
            keys.add(name)
            if name in withhold:
                removed.add(name)
                continue
            if name in rewrite and b"filename=" not in head:
                if name not in rewritten:
                    kept += [head + b"\r\n\r\n" + v.encode() + b"\r\n" for v in rewrite[name]]
                rewritten.add(name)
                continue
            kept.append(part)
        new = b"".join(b"--" + b + k for k in kept) + b"--" + b + b"--\r\n"
        return new, {"format": "multipart", "keys": sorted(k for k in keys if k), "removed": sorted(removed), "rewritten": sorted(rewritten)}
    return None, {"format": ctype or "unknown"}


def degrade(units_or_keys, intended, variant, omit_field, seed):
    """Remove part of the estimated write footprint (never the intended part): a random fraction, or the one field another party changes."""
    import random
    keys = list(units_or_keys)
    cand = [k for k in keys if k not in intended]
    drop = set()
    if omit_field:
        drop = {k for k in cand if k == omit_field or k.endswith(":" + omit_field)}
    frac = float(variant.get("omit_frac") or 0)
    if frac:
        ranked = sorted(cand)
        random.Random(str(seed)).shuffle(ranked)
        drop |= set(ranked[:int(round(frac * len(cand)))])
    return drop


class EASR(GuardV2):
    """EASR v2 and the browser-only baselines that share its machinery (pair/controllers.py sets the switches)."""

    def __init__(self, adapter, dependencies: list[str], budget: int = 3, verify: bool = True, variant: dict | None = None,
                 start_url: str | None = None, name: str = "easr_v2", task: dict | None = None):
        super().__init__(adapter, "dependency", dependencies, post_check=False, max_holds_per_object=budget)
        self.mode, self.name = "easr", name
        self.budget, self.verify, self.start_url = budget, verify, start_url
        self.v = variant or {}
        self.holds_total = 0
        self.pending_verify = None
        self.last_commit = None
        self.snap, self.routed, self.armed, self.json_result = {}, set(), None, None
        self.json_busy = False                           # a save request is inside _route_json (v2.1.1 waits for it)
        self.form_armed, self.form_result = None, None
        # contract-quality study (spec 9.3): degrade the effect contract the controller works with
        self.omit_field = ((task or {}).get("easr") or {}).get("untouched", [""])[0].rsplit(".", 1)[-1] if self.v.get("omit_targeted") else None
        self.omit_seed = str((task or {}).get("scenario", ""))

    def describe(self) -> dict:
        d = super().describe()
        d.update(guard=self.name, budget=self.budget, verify=self.verify, variant=self.v)
        return d

    def omission_safe(self) -> bool:
        return bool(getattr(self.adapter, "EASR_OMISSION_SAFE", False))

    async def _dependencies(self, obj, url):
        changed, unseen, now, cache, reads, hold_url = {}, [], {}, {}, 0, None
        for o, fld in self.deps:
            seen = self.last_seen.get(o, {}).get(fld)
            if seen is None:
                if o == obj:
                    changed[self._label(o, fld) + " (not yet viewed on its own page)"] = [None, None]
                    hold_url = hold_url or self.urls.get(o) or url
                else:
                    unseen.append(f"{o}.{fld}")
                continue
            u = self.urls.get(o)
            if u is None:
                unseen.append(f"{o}.{fld}")
                continue
            if u not in cache:
                reads += 1
                cache[u] = await self._fetch(u)
            cur = next((f["value"] for f in cache[u] if f["object"] == o and f["field"] == fld and f["source"] in ("detail", "record")), None)
            if cur is None:
                unseen.append(f"{o}.{fld}")
                continue
            now[(o, fld)] = cur
            if semantic(fld, seen[0]) != semantic(fld, cur):
                changed[self._label(o, fld)] = [seen[0], cur]
                hold_url = hold_url or u
        return changed, unseen, now, reads, hold_url

    async def _fetch_fresh(self, url, names):
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if getattr(self.adapter, "EASR_FORM_WAIT", None):
                await page.wait_for_selector(self.adapter.EASR_FORM_WAIT, state="attached", timeout=15000)
            return await page.evaluate(FRESH_JS, names) or {}
        finally:
            await page.close()

    # ---- request path (SPA JSON writes) -----------------------------------------------------------------------------------------
    async def attach(self, context) -> None:
        """Watch the agent's own browser: keep the last JSON each GET returned (the data the page was built from)."""
        async def on_response(resp):
            try:
                if resp.request.method == "GET" and "json" in (resp.headers.get("content-type") or ""):
                    self.snap[urlsplit(resp.url).path] = await resp.json()
            except Exception:
                pass
        context.on("response", on_response)

    @staticmethod
    def _unwrap(data, wrapper):
        return data.get(wrapper, data) if wrapper and isinstance(data, dict) else (data if isinstance(data, dict) else {})

    @staticmethod
    def _eq(k, a, b) -> bool:
        if json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True):
            return True
        return same_val(k, a, b)

    async def _get_json(self, path):
        resp = await self.context.request.get(self.adapter.inst.origin + path)
        return await resp.json()

    async def _check_json(self, act, obs, page, t, obj, reads):
        budget_left = self.holds_total < self.budget
        now = {}
        if not self.v.get("verify_only") and not self.v.get("no_dependencies"):
            changed_deps, unseen, now, r, dep_url = await self._dependencies(obj, t["url"])
            reads += r
            if changed_deps and budget_left:
                self.holds_total += 1
                if self.v.get("full_replan") or self.v.get("full_replan_holds"):
                    return self._hold(obj, self.start_url or t["url"], changed_deps, "Your action was not performed because the information this "
                                      "task works with changed. Start the task over from the beginning: re-check everything and redo every step "
                                      "that is still needed.", reads, easr="FULL_REPLAN", conflict_class="B")
                return self._hold(obj, dep_url or t["url"], changed_deps, "Your action was not performed because information it depends on "
                                  "changed since you last saw it: " + "; ".join(f"{k} {a} -> {b}" if a is not None else k for k, (a, b) in changed_deps.items())
                                  + ". Only this step needs to be reconsidered: the current page is now shown; decide this change again.",
                                  reads, easr="LOCAL_REPLAN", conflict_class="B")
        if self.v.get("dom_only"):                       # ablation: no view of the request (Repair v1's DOM-only view)
            return {"decision": "pass", "object": obj, "reads": reads, "easr": "CONTINUE", "note": "request not inspected (DOM-only)"}
        res = t["json"]
        self.armed = {"t": t, "obj": obj, "now": now, "reads": reads}
        self.json_result = None
        key = (id(page), res["path"])
        if key not in self.routed:
            await page.route(re.compile(re.escape(res["path"]) + r"(\?.*)?$"), self._route_json)
            self.routed.add(key)
        return {"decision": "pass", "object": obj, "reads": reads, "easr": "ARMED_REQUEST"}

    async def _route_json(self, route):
        mine = self.armed is not None and route.request.method in ("PUT", "POST", "PATCH")
        if mine:
            self.json_busy = True
        try:
            await self._route_json_request(route)
        finally:
            if mine:
                self.json_busy = False

    async def _route_json_request(self, route):
        req, a = route.request, self.armed
        if a is None or req.method not in ("PUT", "POST", "PATCH"):
            await route.fallback()
            return
        self.armed = None
        t, reads = a["t"], a["reads"]
        res = t["json"]
        ctrl = set(res.get("control_keys") or [])
        try:
            body = json.loads(req.post_data or "{}")
        except Exception:
            body = None
        if not isinstance(body, dict):
            self.json_result = {"decision": "pass", "easr": "CONTINUE", "note": "request body not JSON (footprint unknown)", "reads": reads}
            await route.fallback()
            return
        keys = [k for k in body if k not in ctrl]
        load = self._unwrap(self.snap.get(res["path"]) or {}, res.get("wrapper"))
        intended = [k for k in keys if k not in load or not self._eq(k, body[k], load[k])]
        units = intended if self.v.get("intended_only") else keys
        dropped = degrade(units, intended, self.v, self.omit_field, self.omit_seed) if (self.omit_field or self.v.get("omit_frac")) else set()
        units = [k for k in units if k not in dropped]
        untouched = [k for k in units if k not in intended]
        contract = {"W_hat": [k for k in keys if k not in dropped], "I": intended, "load_unknown": [k for k in keys if k not in load],
                    "omission_safe": self.omission_safe(), "omitted_by_contract_study": sorted(dropped)}
        interference, conflicts, others, fresh = {}, {}, {}, None
        if not self.v.get("verify_only") and not self.v.get("no_interference"):
            reads += 1
            fresh = self._unwrap(await self._get_json(res["get"]), res.get("wrapper"))
            for k in untouched:
                if k in fresh and k in load and not self._eq(k, fresh[k], load[k]):
                    interference[k] = [load[k], fresh[k]]
            for k in intended:
                if k in fresh and k in load and not self._eq(k, fresh[k], load[k]):
                    if self.v.get("syntactic") or self.v.get("entity_freshness") or not self._eq(k, fresh[k], body[k]):
                        conflicts[k] = [load[k], fresh[k]]
            others = {k: [load[k], v] for k, v in fresh.items() if k not in keys and k in load and k not in ctrl and not self._eq(k, v, load[k])}
        cls = "C" if (interference or conflicts) else "A"
        budget_left = self.holds_total < self.budget
        hold, easr, changed, msg = False, None, {}, None
        label = lambda k: self._label(a["obj"], k)
        if budget_left and self.v.get("entity_freshness") and (interference or conflicts or others):
            hold, easr, changed = True, "REJECT_STALE", {**interference, **conflicts, **others}
            msg = "Your change was not saved because these settings changed since you opened them: "
        elif budget_left and self.v.get("full_replan") and (interference or conflicts):
            hold, easr, changed = True, "FULL_REPLAN", {**interference, **conflicts}
            msg = ("Your change was not saved because the information this task works with changed. Start the task over from the beginning: "
                   "re-check everything and redo every step that is still needed. Changed: ")
        elif budget_left and (conflicts or (self.v.get("syntactic") and interference)) and self.v.get("full_replan_holds"):
            hold, easr, changed = True, "FULL_REPLAN", {**conflicts, **(interference if self.v.get("syntactic") else {})}
            msg = ("Your change was not saved because the information this task works with changed. Start the task over from the beginning: "
                   "re-check everything and redo every step that is still needed. Changed: ")
        elif budget_left and (conflicts or (self.v.get("syntactic") and interference)):
            hold, easr, changed = True, "LOCAL_REPLAN", {**conflicts, **(interference if self.v.get("syntactic") else {})}
            msg = "Your change was not saved because a setting you are changing was changed by someone else since you opened the page: "
        elif budget_left and self.v.get("no_rebase") and interference:
            hold, easr, changed = True, "SAFE_HOLD", dict(interference)
            msg = "Your change was not saved because these settings changed since you opened them: "
        if hold:
            self.holds_total += 1
            text = msg + "; ".join(f"{label(k)} {o!r} -> {n!r}" for k, (o, n) in changed.items()) + ". Reload the page and decide this change again."
            await route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": text}))
            self.json_result = {"decision": "hold", "easr": easr, "message": text, "changed": changed, "reads": reads, "conflict_class": cls,
                                "contract": contract, "url": t["url"]}
            return
        new, easr = dict(body), "CONTINUE"
        if fresh is not None and not self.v.get("no_rebase"):
            if self.omission_safe() and not self.v.get("rewrite_only"):
                new = {k: v for k, v in body.items() if k in ctrl or k in intended or k in dropped}
            else:
                for k, (_, cur) in interference.items():
                    new[k] = cur
            easr = "REBASE" if interference else "CONTINUE"
        if new != body:
            await route.continue_(post_data=json.dumps(new))
        else:
            await route.fallback()
        self.json_result = {"decision": "pass", "easr": easr, "conflict_class": cls, "contract": contract, "interference": interference,
                            "sent": body, "submitted": new, "reads": reads, "obj": a["obj"], "now": a["now"], "res": res}

    async def _post_json(self):
        # the click's request may still be in flight. v2.1 stopped waiting once the request reached the handler, before the handler
        # had recorded its result (it may still be reading the current state), and then skipped the check; v2.1.1 (variant
        # "await_route") also waits while the handler is running.
        wait_handler = bool(self.v.get("await_route"))
        for _ in range(50):
            if self.json_result is not None or (self.armed is None and not (wait_handler and self.json_busy)):
                break
            await asyncio.sleep(0.1)
        r, self.json_result, self.armed = self.json_result, None, None
        if r is None:
            return None
        if r["decision"] == "hold":
            return {"reads": r["reads"], "easr": r["easr"], "request_hold": True, "changed": r["changed"], "conflict_class": r["conflict_class"],
                    "notice": r["message"], "url": r["url"]}
        rec = {"reads": r["reads"], "easr": r["easr"], "conflict_class": r.get("conflict_class"), "interference": r.get("interference"),
               "request": True, "contract": r.get("contract")}
        if not self.verify or self.v.get("no_verify") or "res" not in r:
            return rec
        if self.v.get("verify_on_signal") and not r.get("interference"):
            return rec
        notes = []
        after = self._unwrap(await self._get_json(r["res"]["get"]), r["res"].get("wrapper"))
        rec["reads"] += 1
        for k in r["contract"]["I"]:
            if k in after and not self._eq(k, after[k], r["sent"][k]):
                notes.append(f"{self._label(r['obj'], k)} is {after[k]!r}, not the {r['sent'][k]!r} you submitted (your change may not have been saved)")
        for k, (_, new) in (r.get("interference") or {}).items():
            if k in after and not self._eq(k, after[k], new):
                notes.append(f"{self._label(r['obj'], k)} had been changed by someone else to {new!r} and is now {after[k]!r}")
        rec["findings"] = notes
        if notes:
            rec["notice"] = "After saving: " + "; ".join(notes) + ". Check whether your change is still correct and complete, and correct it if needed."
        return rec

    async def check(self, act: dict, obs: dict, page, context) -> dict:
        self.pending_verify = None
        self.form_armed, self.form_result = None, None
        t = await self.adapter.commit_target(page, act, obs)
        if t is None:
            return {"decision": "not_covered", "commit_like": self.adapter.commit_like(act, obs)}
        reads, obj, url = 0, t["object"], t["url"]
        try:
            if obj is None:
                reads += 1
                facts = await self._fetch(url)
                obj = next((f["object"] for f in facts if f["source"] == "detail"), None)
                if obj is None:
                    return {"decision": "check_failed", "url": url, "error": "target page shows no item", "reads": reads}
            self.urls.setdefault(obj, url)
            if t.get("kind") == "json":
                return await self._check_json(act, obs, page, t, obj, reads)
            budget_left = self.holds_total < self.budget
            # ---- contract: W_hat and I from the agent's own form ----
            fd, us, intended, contract = None, {}, [], None
            if t.get("form"):
                fd = await page.evaluate(FORMDATA_JS, str(act["id"]))
            if fd:
                sent, load, kind = fd["sent"], fd["load"], fd["kind"]
                names = sorted({n for n in list(sent) + list(load) if not CONTROL.search(n)})
                if self.v.get("dom_only"):                       # ablation: visible inputs only (Repair v1's view of the write)
                    names = [n for n in names if kind.get(n) == "visible"]
                us = units(names, kind)
                intended = [u for u, mem in us.items() if any(joined(sent.get(m)) != joined(load.get(m)) for m in mem)]
                if self.v.get("intended_only"):                  # ablation: assume the action writes only the intended fields
                    us = {u: us[u] for u in intended}
                dropped = degrade(us, intended, self.v, self.omit_field, self.omit_seed) if (self.omit_field or self.v.get("omit_frac")) else set()
                us = {u: m for u, m in us.items() if u not in dropped}
                contract = {"W_hat": sorted(us), "I": sorted(intended), "units": us, "omission_safe": self.omission_safe(),
                            "omitted_by_contract_study": sorted(dropped)}
            if self.v.get("verify_only"):                        # B4: postcondition verification only
                out = {"decision": "pass", "object": obj, "reads": reads, "easr": "CONTINUE", "contract": contract}
                return self._arm_verify(out, obj, t, {}, contract, {}, fd)
            # ---- D: decision dependencies ----
            changed_deps, unseen, now = {}, [], {}
            if not self.v.get("no_dependencies"):
                changed_deps, unseen, now, r, dep_url = await self._dependencies(obj, url)
                reads += r
            else:
                dep_url = None
            # ---- write interference on W_hat ----
            interference, conflicts, structure = {}, {}, None
            if fd and t.get("edit_url") and not self.v.get("no_interference"):
                reads += 1
                fresh = await self._fetch_fresh(t["edit_url"], list(fd["load"]))
                ids_load = {n: joined(v) for n, v in fd["load"].items() if STRUCTURE.search(n)}
                ids_now = {n: joined(v) for n, v in fresh.items() if STRUCTURE.search(n)}
                if ids_load != ids_now:
                    structure = {"load": ids_load, "now": ids_now}
                n_fresh = len({n for n in fresh if not CONTROL.search(n)})
                if n_fresh >= 8 and len(us) * 4 <= n_fresh and not self.v.get("dom_only"):
                    # an action form (e.g. a quick-assign box) carries a small part of the record's fields; its controls exist to perform
                    # the action, and their preset values are suggestions, not the stored values: every visible unit is intended, and a
                    # same-field change is judged against what the agent last saw of the record, not against the preset
                    intended = [u for u in us if not u.startswith("hidden:")]
                    contract.update(I=sorted(intended), action_form=True)
                    reads += 1
                    rec = [f for f in await self._fetch(url) if f["object"] == obj and f["source"] in ("detail", "record")]
                    fmap = getattr(self.adapter, "FACT_FIELD", {}) or {}
                    for u in intended:
                        fld = fmap.get(us[u][0], us[u][0])
                        seen = self.last_seen.get(obj, {}).get(fld)
                        cur = next((f["value"] for f in rec if f["field"] == fld), None)
                        if seen is not None and cur is not None and seen[2] in ("detail", "record") and not same_val(fld, seen[0], cur):
                            conflicts[self._label(obj, fld) + " (you are also changing it)"] = [seen[0], cur]
                for u, mem in us.items():
                    lead = mem[0]
                    if lead not in fresh or (contract.get("action_form") and u in intended):
                        continue
                    lv, cv = joined(fd["load"].get(lead)), joined(fresh[lead])
                    same_now = (lv == cv) if self.v.get("syntactic") else same_val(lead, lv, cv)
                    if same_now:
                        continue
                    if u in intended:
                        sv = joined(fd["sent"].get(lead))
                        if not self.v.get("syntactic") and not self.v.get("entity_freshness") and same_val(lead, sv, cv):
                            continue                         # convergent: the other party set what the agent submits
                        conflicts[self._label(obj, lead) + " (you are also changing it)"] = [lv, cv]
                    else:
                        interference[u] = [lv, cv]
            cls = ("D" if changed_deps and (interference or conflicts) else "B" if changed_deps else
                   "C" if (interference or conflicts) else "A")
            base = {"object": obj, "conflict_class": cls, "contract": contract, "unseen_dependencies": unseen,
                    "interference": interference, "structure_changed": structure}
            # ---- decisions ----
            signal = bool(changed_deps or conflicts or interference or structure)
            if self.v.get("full_replan") and signal and budget_left:            # B6: any signal restarts the whole task
                self.holds_total += 1
                return self._hold(obj, self.start_url or url, {**changed_deps, **conflicts, **interference},
                                  "Your action was not performed because the information this task works with changed. Start the task over "
                                  "from the beginning: re-check everything and redo every step that is still needed.", reads,
                                  easr="FULL_REPLAN", **base)
            if self.v.get("entity_freshness") and (interference or conflicts) and budget_left:   # global freshness: any change rejects
                self.holds_total += 1
                ch = {**conflicts, **{self._label(obj, us[u][0]): v for u, v in interference.items()}, **changed_deps}
                return self._hold(obj, url, ch, "Your action was not performed because this item changed since you opened it: "
                                  + "; ".join(f"{k} {a} -> {b}" for k, (a, b) in ch.items()) + ". The current page is now shown; decide again.",
                                  reads, easr="REJECT_STALE", **base)
            if (changed_deps or conflicts) and budget_left and self.v.get("full_replan_holds"):   # spec A8: full replanning instead of local
                self.holds_total += 1
                return self._hold(obj, self.start_url or url, {**changed_deps, **conflicts}, "Your action was not performed because the information this task works with changed. Start the task over from the beginning: re-check everything and redo every step that is still needed.", reads, easr="FULL_REPLAN", **base)
            if (changed_deps or conflicts) and budget_left:
                self.holds_total += 1
                ch = {**changed_deps, **conflicts}
                return self._hold(obj, dep_url or url, ch, "Your action was not performed because information it depends on, or a field you are "
                                  "changing, changed since you last saw it: " + "; ".join(f"{k} {a} -> {b}" if a is not None else k for k, (a, b) in ch.items())
                                  + ". Only this step needs to be reconsidered: the current page is now shown; decide this change again.",
                                  reads, easr="LOCAL_REPLAN", **base)
            if structure and budget_left:
                self.holds_total += 1
                return self._hold(obj, url, {}, "Your action was not performed because the items on this page changed (lines were added, removed "
                                  "or reordered) since you opened it. The current page is now shown; decide again.", reads, easr="SAFE_HOLD", **base)
            out = {"decision": "pass", "reads": reads, **base, "easr": "CONTINUE"}
            if not budget_left and signal:
                out["note"] = "repair budget exhausted (declared pass)"
            if fd and contract and not self.v.get("no_rebase"):
                untouched = [u for u in us if u not in intended]
                if self.omission_safe() and not self.v.get("rewrite_only"):
                    withhold = [m for u in untouched if not u.startswith("hidden:") for m in us[u]]
                    rewrite = {}
                    for u in interference:
                        if u.startswith("hidden:"):
                            rewrite[us[u][0]] = interference[u][1].split("|") if interference[u][1] else []
                else:
                    withhold = []
                    rewrite = {us[u][0]: (v[1].split("|") if v[1] else []) for u, v in interference.items()}
                if withhold or rewrite:
                    # REBASE edits the agent's own outgoing request (the page and its validation see the form unchanged)
                    self.form_armed = {"withhold": set(withhold), "rewrite": rewrite, "hint": set(fd["sent"])}
                    key = (id(page), "form")
                    if key not in self.routed:
                        await page.route("**/*", self._route_form)
                        self.routed.add(key)
                    out.update(request_rewrite={"withhold": sorted(withhold), "rewrite": rewrite})
                    if interference:
                        out["easr"] = "REBASE"
            elif interference and self.v.get("no_rebase") and budget_left:      # ablation: interference holds instead of rebasing
                self.holds_total += 1
                ch = {self._label(obj, us[u][0]): v for u, v in interference.items()}
                return self._hold(obj, url, ch, "Your action was not performed because this item changed since you opened it: "
                                  + "; ".join(f"{k} {a} -> {b}" for k, (a, b) in ch.items()) + ". The current page is now shown; decide again.",
                                  reads, easr="SAFE_HOLD", **base)
            return self._arm_verify(out, obj, t, now, contract, interference, fd)
        except Exception as exc:
            return {"decision": "check_failed", "object": obj, "url": url, "error": repr(exc)[:300], "reads": reads}

    async def _route_form(self, route):
        req, a = route.request, self.form_armed
        if a is None or req.method != "POST":
            await route.fallback()
            return
        body = req.post_data_buffer or b""
        new, info = rewrite_body(req.headers.get("content-type", ""), body, a["withhold"], a["rewrite"])
        if new is None or not (set(info.get("keys") or []) & a["hint"]):
            await route.fallback()                      # not the form submission (another POST of the page)
            return
        self.form_armed, self.form_result = None, {**info, "url": req.url}
        if new != body:
            await route.continue_(post_data=new)
        else:
            await route.fallback()

    def _arm_verify(self, out, obj, t, now, contract, interference, fd):
        if self.verify and not self.v.get("no_verify"):
            if self.v.get("verify_on_signal") and not (interference or out.get("conflict_class") in ("B", "C", "D")):
                return out
            self.pending_verify = {"obj": obj, "edit_url": t.get("edit_url"), "now": now, "contract": contract,
                                   "interference": interference, "sent": (fd or {}).get("sent"), "load": (fd or {}).get("load")}
        return out

    async def post_commit(self) -> dict | None:
        # v2.1 (frozen arms) looked at the SPA save path only while the request was still armed or its result recorded; while the
        # request handler was running it fell through to the form path and returned nothing. v2.1.1 ("await_route") waits for it.
        if self.armed is not None or self.json_result is not None or (self.v.get("await_route") and self.json_busy):
            return await self._post_json()
        form_request, self.form_result = self.form_result, None
        if self.form_armed is not None:
            form_request, self.form_armed = {"matched": False}, None
        p, self.pending_verify = self.pending_verify, None
        if not p:
            return {"form_request": form_request} if form_request else None
        notes, reads = [], 0
        c = p.get("contract")
        if p.get("edit_url") and c:
            reads += 1
            after = await self._fetch_fresh(p["edit_url"], list(p.get("load") or {}))
            sent, us = p.get("sent") or {}, c["units"]
            for u in c["I"]:
                lead = us[u][0]
                if lead in after and not same_val(lead, joined(after[lead]), joined(sent.get(lead))):
                    notes.append(f"{self._label(p['obj'], lead)} is {joined(after[lead])!r}, not the {joined(sent.get(lead))!r} you submitted "
                                 "(your change may not have been saved)")
            for u, (old, new) in (p.get("interference") or {}).items():
                lead = us[u][0]
                if lead in after and not same_val(lead, joined(after[lead]), new):
                    notes.append(f"{self._label(p['obj'], lead)} had been changed by someone else to {new!r} and is now {joined(after[lead])!r}")
        cache = {}
        for (o, fld), before in (p.get("now") or {}).items():
            u = self.urls.get(o)
            if u not in cache:
                reads += 1
                cache[u] = await self._fetch(u)
            cur = next((f["value"] for f in cache[u] if f["object"] == o and f["field"] == fld and f["source"] in ("detail", "record")), None)
            if cur is not None and semantic(fld, cur) != semantic(fld, before):
                notes.append(f"{self._label(o, fld)} changed from {before!r} to {cur!r} while your change was being saved")
        rec = {"reads": reads, "easr": "VERIFY_OUTCOME", "findings": notes, "form_request": form_request}
        if notes:
            rec["notice"] = "After saving: " + "; ".join(notes) + ". Check whether your change is still correct and complete, and correct it if needed."
        return rec
