"""Public object revalidation guard (repair policy).

It remembers the public facts the agent was actually shown for each object. Before a covered
commit it fetches that object's public page in a separate tab and compares the guard fields.
Unchanged: the action proceeds with no extra model call. Changed or never seen: the action is
held, the main tab shows the current public page, and the model decides again. It never reads
backend state or the correct answer, and it is not atomic (a change after the check can still
slip through; the evaluator logs that case).
"""
from __future__ import annotations


class RevalidationGuard:
    def __init__(self, adapter):
        self.adapter = adapter
        self.last_seen = {}       # object -> {field: (value, step, source)}
        self.context = None       # the guard's own browser context and login session

    async def setup(self, browser) -> None:
        """Separate context and login, so guard reads never share the agent's session lock or sockets."""
        self.context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        await self.context.route("**/*", self.adapter.route_handler)
        await self.adapter.prepare_context(self.context, record_initial=False)

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()

    def describe(self) -> dict:
        return {"guard": "public_object_revalidation_v1", "fields": list(self.adapter.GUARD_FIELDS),
                "coverage": self.adapter.GUARD_COVERAGE}

    def observe(self, step: int, facts: list, obs: dict | None = None) -> None:
        for f in facts:
            if (f["field"] in self.adapter.GUARD_FIELDS and not f["object"].startswith(("page", "basket"))
                    and f["source"] not in ("list", "banner")):
                self.last_seen.setdefault(f["object"], {})[f["field"]] = (f["value"], step, f["source"])

    async def check(self, act: dict, obs: dict, page, context) -> dict:
        target = await self.adapter.guard_target(page, act, obs)
        if target is None:
            return {"decision": "not_covered", "commit_like": self.adapter.commit_like(act, obs)}
        obj, url = target
        fresh = await (self.context or context).new_page()
        try:
            await fresh.goto(url, wait_until="domcontentloaded", timeout=30000)
            facts = await self.adapter.public_facts(fresh, None)
        except Exception as exc:
            # Fail open, but visibly: the action proceeds and the episode records a guard coverage failure.
            return {"decision": "check_failed", "object": obj, "url": url, "error": repr(exc)[:300]}
        finally:
            await fresh.close()
        current = {f["field"]: f["value"] for f in facts if f["object"] == obj and f["field"] in self.adapter.GUARD_FIELDS
                   and f["source"] in self.adapter.GUARD_SOURCES}
        seen = {k: v[0] for k, v in self.last_seen.get(obj, {}).items()}
        same = getattr(self.adapter, "same", lambda field, a, b: a == b)
        changed = {k: [seen.get(k), v] for k, v in current.items() if not same(k, seen.get(k), v)}
        record = {"object": obj, "url": url, "seen": self.last_seen.get(obj, {}), "current": current}
        if not current:
            return {"decision": "check_failed", "error": "no guard fields found on the current public page", **record}
        if not changed:
            return {"decision": "pass", **record}
        if not seen:
            message = ("Your action was not performed: you have not viewed this item's current details yet. "
                       "Its current page is now shown; decide again.")
        else:
            parts = [f"{k} {old if old is not None else '(not seen)'} -> {new}" for k, (old, new) in changed.items()]
            message = ("Your action was not performed because this item changed since you last viewed it: "
                       + "; ".join(parts) + ". Its current page is now shown; decide again.")
        # The runner moves the agent's tab to `url` (discarding any unsaved draft) before the next observation.
        return {"decision": "hold", "changed": changed, "message": message, **record}
