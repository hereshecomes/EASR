"""Pre-commit validation family for the repair study (v2).

Browser tier (public pages only, through the validator's own browser context and login; no database):
  refresh     hold every covered commit once, show the target's current page, ask for a new decision
  record      re-read the target's public page; hold if any field the agent saw there has changed
  dependency  re-read the pages of the task's declared dependencies (contract facts, possibly on other
              objects); hold only if one the agent saw has changed. Changes to other fields pass.
  footprint   dependency + effective write: on an edit form, only the fields the agent's form actually
              changed are submitted (untouched inputs are disabled before the click, so the application
              keeps their current stored values); a concurrent change to a field the agent changed is held.
  post_check  (recovery, optional) after a passed commit, re-read the dependencies once more and tell the
              agent if any changed between the check and the write (the residual race window).
App tier (application-integrated conditional commit; see apps/*/pair hooks):
  conditional the browser component sends, with the agent's own submission, the dependency values the agent
              saw and the load values of the fields it changed; the application checks them in the same
              request that writes and rejects the commit if any differs. No pre-commit page reads.
None of the browser modes is atomic: a change after the check and before the write remains possible.
Every mode receives the same inputs: the delivered pages, the task's declared dependencies, the agent's
action, and a separate public browser session.
"""
from __future__ import annotations

import json
import re

AVAIL = re.compile(r"(out of stock|unavailable|in stock|available)", re.I)
NUMERIC = ("price", "amount", "probability", "line_total", "quantity")


def semantic(field: str, value) -> str:
    """The part of a displayed value a decision can depend on."""
    s = " ".join(str(value if value is not None else "").split()).lower()
    if field == "availability":
        m = AVAIL.search(s)
        return {"available": "in stock"}.get(m.group(1), m.group(1)) if m else s
    if field in NUMERIC:
        try:
            return f"{float(s.replace('$', '').replace(',', '')):.2f}"
        except ValueError:
            return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s


def literal(field: str, value) -> str:
    """Displayed value up to formatting only (whitespace, currency/thousands formatting, date format)."""
    s = " ".join(str(value if value is not None else "").split()).lower()
    if field in NUMERIC:
        try:
            return f"{float(s.replace('$', '').replace(',', '')):.2f}"
        except ValueError:
            return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s


class GuardV2:
    MODES = ("refresh", "record", "dependency", "footprint", "conditional")

    def __init__(self, adapter, mode: str, dependencies: list[str], post_check: bool = False, max_holds_per_object: int = 3):
        assert mode in self.MODES, mode
        self.adapter, self.mode, self.post_check = adapter, mode, post_check
        self.deps = [tuple(d.rsplit(".", 1)) for d in dependencies]      # [(object, field)]
        self.max_holds = max_holds_per_object
        self.last_seen = {}       # object -> {field: (value, step, source)}
        self.urls = {}            # object -> its own public page
        self.tickets = set()      # refresh mode: targets whose reloaded page has been shown once
        self.holds = {}
        self.pending = None       # post-commit check: values read at the pre-commit check
        self.context = None

    def describe(self) -> dict:
        return {"guard": "pre_commit_validation_v2", "mode": self.mode, "post_check": self.post_check,
                "dependencies": [".".join(d) for d in self.deps], "max_holds_per_object": self.max_holds,
                "information": "delivered pages, declared dependencies, public pages via a separate session",
                "coverage": self.adapter.GUARD_COVERAGE_V2}

    async def setup(self, browser) -> None:
        self.context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        await self.context.route("**/*", self.adapter.route_handler)
        await self.adapter.prepare_context(self.context, record_initial=False)

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()

    # ---- bookkeeping from delivered observations ------------------------------------------------
    def observe(self, step: int, facts: list, obs: dict | None = None) -> None:
        for f in facts:
            if f["object"] in ("page", "basket") or f["source"] in ("list", "banner", "conflict"):
                continue
            self.last_seen.setdefault(f["object"], {})[f["field"]] = (f["value"], step, f["source"])
            if obs and f["source"] in ("detail", "record"):
                self.urls[f["object"]] = re.sub(r"[?&]pair_rejected=1(&pair_msg=[^&]*)?", "", obs["url"])
        if obs:
            for obj, url in self.adapter.link_targets(obs).items():
                self.urls.setdefault(obj, url)

    async def _fetch(self, url: str) -> list:
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return await self.adapter.public_facts(page, None)
        finally:
            await page.close()

    async def _fetch_form(self, url: str) -> dict:
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return await page.evaluate(self.adapter.FORM_VALUES_JS) or {}
        finally:
            await page.close()

    def _label(self, obj: str, field: str) -> str:
        return f"{self.adapter.object_label(obj)} {field}"

    def _hold(self, obj, url, changed, message, reads, **extra):
        self.holds[obj] = self.holds.get(obj, 0) + 1
        return {"decision": "hold", "object": obj, "url": url, "changed": changed, "message": message, "reads": reads, **extra}

    # ---- decision at a covered commit -------------------------------------------------------------
    async def check(self, act: dict, obs: dict, page, context) -> dict:
        self.pending = None
        t = await self.adapter.commit_target(page, act, obs)
        if t is None:
            return {"decision": "not_covered", "commit_like": self.adapter.commit_like(act, obs)}
        reads = 0
        try:
            obj, url = t["object"], t["url"]
            if obj is None:                      # resolve the product behind an add form from its public page
                reads += 1
                facts = await self._fetch(url)
                obj = next((f["object"] for f in facts if f["source"] == "detail"), None)
                if obj is None:
                    return {"decision": "check_failed", "url": url, "error": "target page shows no item", "reads": reads}
            self.urls.setdefault(obj, url)
            if self.holds.get(obj, 0) >= self.max_holds:
                return {"decision": "pass", "object": obj, "reads": reads, "note": "hold limit reached for this object"}
            if self.mode == "refresh":
                if obj in self.tickets:
                    self.tickets.discard(obj)
                    return {"decision": "pass", "object": obj, "reads": reads}
                self.tickets.add(obj)
                return self._hold(obj, url, {}, "Your action was not performed yet: the current page has been reloaded so you can review it before the change is saved. Decide again.", reads)
            if self.mode == "conditional":
                return await self._conditional(page, act, obs, t, obj, reads)
            if self.mode == "record":
                reads += 1
                fresh = [f for f in await self._fetch(url) if f["object"] == obj or (t["kind"] == "basket" and f["source"] == "basket")]
                objs = {f["object"] for f in fresh} if t["kind"] == "basket" else {obj}
                if not any(o in self.last_seen for o in objs):
                    return self._hold(obj, url, {}, "Your action was not performed: you have not viewed this item's current details yet. "
                                      "Its current page is now shown; decide again.", reads)
                changed = {}
                for f in fresh:
                    if f["field"] in self.adapter.METADATA_FIELDS:
                        continue
                    seen = self.last_seen.get(f["object"], {}).get(f["field"])
                    if seen is not None and literal(f["field"], seen[0]) != literal(f["field"], f["value"]):
                        changed[self._label(f["object"], f["field"])] = [seen[0], f["value"]]
                if changed:
                    return self._hold(obj, url, changed, "Your action was not performed because this item changed since you last viewed it: "
                                      + "; ".join(f"{k} {a} -> {b}" for k, (a, b) in changed.items()) + ". Its current page is now shown; decide again.", reads)
                return {"decision": "pass", "object": obj, "reads": reads}
            # dependency / footprint
            changed, unseen, now, cache = {}, [], {}, {}
            hold_url = None
            for o, fld in self.deps:
                seen = self.last_seen.get(o, {}).get(fld)
                if seen is None:
                    if o == obj:      # the contract requires the target's own current page before changing it
                        hold_url = hold_url or self.urls.get(o) or url
                        changed[self._label(o, fld) + " (not yet viewed on its own page)"] = [None, None]
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
            footprint = None
            if self.mode == "footprint" and t.get("form"):
                footprint = await page.evaluate(self.adapter.FOOTPRINT_JS, str(act["id"]))
                if footprint and footprint["changed"]:
                    reads += 1
                    fresh = await self._fetch_form(t["edit_url"])
                    for name in footprint["changed"]:
                        if name in fresh and semantic(name, fresh[name]) != semantic(name, footprint["load"].get(name)):
                            changed[self._label(obj, name) + " (you are also changing it)"] = [footprint["load"].get(name), fresh[name]]
                            hold_url = hold_url or url
            if changed:
                return self._hold(obj, hold_url or url, changed, "Your action was not performed because information it depends on changed "
                                  "since you last saw it: " + "; ".join(f"{k} {a} -> {b}" if a is not None else k for k, (a, b) in changed.items())
                                  + ". The current page is now shown; decide again.", reads, unseen_dependencies=unseen)
            out = {"decision": "pass", "object": obj, "reads": reads, "unseen_dependencies": unseen}
            if footprint:
                out["submitted_fields"] = footprint["changed"]
                out["withheld_fields"] = await page.evaluate(self.adapter.STRIP_JS, {"id": str(act["id"]), "names": footprint["unchanged"],
                                                                                        "changed": footprint["changed"]})
            if self.post_check:
                self.pending = {"now": now, "obj": obj}
            return out
        except Exception as exc:
            return {"decision": "check_failed", "object": t.get("object"), "url": t.get("url"), "error": repr(exc)[:300], "reads": reads}

    async def _conditional(self, page, act, obs, t, obj, reads) -> dict:
        """App tier: attach the seen dependency values and the changed fields' load values; strip untouched fields."""
        conds = []
        for o, fld in self.deps:
            seen = self.last_seen.get(o, {}).get(fld)
            ref = self.adapter.condition_ref(o, fld, self.urls.get(o))
            if seen is not None and ref is not None:
                conds.append({**ref, "value": seen[0], "role": "dependency"})
        out = {"decision": "pass", "object": obj, "reads": reads, "conditional": True}
        if t.get("form"):
            fp = await page.evaluate(self.adapter.FOOTPRINT_JS, str(act["id"]))
            if fp:
                ref = self.adapter.condition_ref(obj, None, t["url"])
                for name in fp["changed"]:
                    if ref is not None and name in fp["load"]:
                        conds.append({**ref, "field": name, "value": fp["load"][name], "role": "changed_field"})
                out["submitted_fields"] = fp["changed"]
                out["withheld_fields"] = await page.evaluate(self.adapter.STRIP_JS, {"id": str(act["id"]), "names": fp["unchanged"], "changed": fp["changed"]})
        out["conditions"] = conds
        await page.evaluate(self.adapter.ATTACH_JS, {"id": str(act["id"]), "value": json.dumps(conds)})
        return out

    # ---- recovery: one re-read after a passed commit -----------------------------------------------
    async def post_commit(self) -> dict | None:
        p, self.pending = self.pending, None
        if not p or not p["now"]:
            return None
        cache, changed = {}, {}
        for (o, fld), before in p["now"].items():
            u = self.urls.get(o)
            if u not in cache:
                cache[u] = await self._fetch(u)
            cur = next((f["value"] for f in cache[u] if f["object"] == o and f["field"] == fld and f["source"] in ("detail", "record")), None)
            if cur is not None and semantic(fld, cur) != semantic(fld, before):
                changed[self._label(o, fld)] = [before, cur]
        rec = {"reads": len(cache), "changed": changed}
        if changed:
            rec["notice"] = ("Your change was saved, but while it was being saved information it depends on changed: "
                             + "; ".join(f"{k} {a} -> {b}" for k, (a, b) in changed.items())
                             + ". Check whether your change is still correct and correct it if needed.")
        return rec
