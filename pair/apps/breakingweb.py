"""Suite D3: BreakingWeb (self-hosted SPA environments, external/BreakingWeb), settings pages of the environments with a
settings API (gmail, booking, amazon, reddit). The backend is one shared service (apps/breakingweb/pair_bw_app.py on 127.0.0.1:18700);
isolation is per session. Writes are JSON PUTs built by React from component state: DOM inputs carry no names and sit in no form.
The PUT handlers apply only the keys present (exclude_none), so omitting a key keeps its stored value (omission-safe).
Evaluator side: /pair/state/<session> (secret header) for state and the server-applied audit payloads.
"""
from __future__ import annotations

import glob
import json
import os
import urllib.error
import urllib.request

from ..dynamics import EventSchedule
from .common import make_route_handler, FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS, enter_as_click

ORIGIN = os.environ.get("PAIR_BW_URL", "http://127.0.0.1:18700")
TASKS = os.path.join(os.environ.get("BREAKINGWEB_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                                                     "external", "BreakingWeb")), "breakingweb", "tasks")
WRAPPED = {"gmail": True, "amazon": True, "reddit": True, "booking": False}       # GET /settings returns {"settings": {...}} or the dict
SAVE_LABEL = {"gmail": "Save Changes", "booking": "Save Settings", "amazon": "Save Changes", "reddit": "Save all settings"}


def call(method: str, path: str, body=None, headers=None, attempts: int = 4):
    """One API call; transient timeouts are retried (session creation, idempotent PUTs and GETs only)."""
    import time
    for i in range(attempts):
        req = urllib.request.Request(ORIGIN + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"content-type": "application/json", **(headers or {})})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))


class BwSession:
    """Stands in for an application instance: one BreakingWeb session on the shared backend."""

    def __init__(self, env: str, task_id: str | None = None, seed: int = 42):
        self.env, self.seed = env, seed
        self.task_id = task_id or sorted(os.path.basename(p)[:-5] for p in glob.glob(f"{TASKS}/{env}/*.yaml"))[0]
        self.origin, self.sid = ORIGIN, None

    def build(self, spec: dict | None = None) -> None:
        self.sid = call("POST", f"/api/env/{self.env}/session", {"task_id": self.task_id, "seed": self.seed})["session_id"]

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def url(self, path: str) -> str:
        return f"{ORIGIN}/env/{self.env}{path}?session={self.sid}&agent_mode=1"

    def start_url(self, assignment: dict) -> str:
        return self.url(assignment.get("start_path", "/"))


FACTS_JS = r"""() => {
  const out = [];
  for (const e of document.querySelectorAll('input,select,textarea')) {
    const label = (e.labels && e.labels[0] ? e.labels[0].innerText : (e.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
    if (!label || /^search/i.test(label)) continue;
    let v = e.type === 'checkbox' ? String(e.checked) : e.type === 'radio' ? (e.checked ? label : null) : e.value;
    if (v === null) continue;
    out.push({field: e.type === 'radio' ? (e.name || label) : (e.id || label), value: v});
  }
  return out;
}"""


class BwAdapter:
    EASR_OMISSION_SAFE = True
    METADATA_FIELDS = ()
    GUARD_COVERAGE_V2 = "BreakingWeb settings saves (React; JSON PUT)"
    FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS = FOOTPRINT_JS, STRIP_JS, ATTACH_JS, FORM_VALUES_JS

    app = "breakingweb"

    def __init__(self, inst: BwSession, dynamic: dict | None = None):
        self.inst, self.env = inst, inst.env
        self.blocked = []
        self.route_handler = make_route_handler({ORIGIN}, self.blocked, ("/pair/",))   # evaluator-only paths
        self.obj = f"settings:{self.env}"
        self.dynamic = dynamic
        self.dynamic_state = None
        self._initial = None
        self.schedule = EventSchedule(dynamic["events"], self.write_value, self.read_value) if dynamic and "events" in dynamic else None

    def describe(self) -> dict:
        return {"app": "BreakingWeb", "env": self.env, "task_id": self.inst.task_id, "session": self.inst.sid, "origin": ORIGIN,
                "dynamic": self.dynamic}

    # ---- episode interface (runner) ------------------------------------------------------------------------------------------------
    def state(self) -> dict:
        return {"settings": dict(self.settings())}

    def diff(self, before: dict, after: dict) -> list[dict]:
        b, a = before.get("settings", {}), after.get("settings", {})
        return [{"class": "business", "table": "settings", "op": "update", "object": self.obj, "field": k, "before": b.get(k), "after": a.get(k)}
                for k in sorted(set(b) | set(a)) if b.get(k) != a.get(k)]

    def on_start(self) -> list:
        self._initial = self.settings()
        return self.schedule.on_start() if self.schedule else []

    def before_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.before_commit(step) if self.schedule else []

    def after_commit(self, step: int, act: dict, obs: dict) -> list:
        return self.schedule.after_commit(step) if self.schedule else []

    def after_delivery(self, step: int, obs: dict, facts: list) -> list:
        if self.schedule is None:
            return []
        fired = {r.get("field") for r in self.schedule.fired.values() if r.get("status") == "fired"}
        cur = self.settings()
        changed = any(cur.get(k) != v for k, v in (self._initial or {}).items() if k not in fired)
        return self.schedule.after_delivery(step, facts, changed)

    async def prepare_context(self, context, record_initial: bool = True) -> None:
        pass

    def json_resource(self) -> dict:
        return {"path": f"/api/env/{self.env}/settings", "get": f"/api/env/{self.env}/settings?session_id={self.inst.sid}",
                "wrapper": "settings" if WRAPPED[self.env] else None, "control_keys": ["session_id", "id"]}

    async def public_facts(self, page, obs) -> list[dict]:
        if "/settings" not in page.url:
            return []
        try:
            await page.wait_for_selector(f"button:has-text('{SAVE_LABEL[self.env]}')", timeout=15000)
        except Exception:
            return []
        return [{"object": self.obj, "field": f["field"], "value": f["value"], "source": "record"} for f in await page.evaluate(FACTS_JS)]

    def link_targets(self, obs: dict) -> dict:
        return {}

    def object_label(self, obj: str) -> str:
        return f"{self.env} settings"

    def condition_ref(self, obj, field, url):
        return None

    async def commit_target(self, page, act: dict, obs: dict):
        act = enter_as_click(act, obs)
        if act["type"] != "click":
            return None
        text = await page.evaluate("(id) => { const e = document.querySelector(`[data-pair-id=\"${id}\"]`); return e ? (e.innerText || e.value || '').trim() : null; }",
                                   str(act["id"]))
        if not text or text != SAVE_LABEL[self.env] or "/settings" not in page.url:
            return None
        return {"object": self.obj, "url": page.url, "edit_url": page.url, "kind": "json", "form": False, "json": self.json_resource()}

    def commit_like(self, act: dict, obs: dict) -> bool:
        c = next((c for c in obs.get("controls", []) if c["id"] == str(act.get("id"))), None)
        return bool(c and (c.get("label") or "").strip() == SAVE_LABEL[self.env])

    # ---- evaluator side and the other party ------------------------------------------------------------------------------------------
    def settings(self) -> dict:
        st = call("GET", f"/pair/state/{self.inst.sid}", headers={"x-pair-evaluator": os.environ.get("PAIR_EVAL_SECRET", "pair-eval-local")})
        return st["state"].get("settings") or {}

    def read_value(self, obj: str, field: str):
        return self.settings().get(field)

    def arm_faults(self, faults: list) -> list:
        """Evaluator side: arm server-side faults for this session (BreakingWeb-derived interventions)."""
        return [call("POST", f"/pair/fault/{self.inst.sid}", f, headers={"x-pair-evaluator": os.environ.get("PAIR_EVAL_SECRET", "pair-eval-local")})
                for f in faults]

    def write_value(self, obj: str, field: str, value) -> None:
        """Another client of the same account saving one setting through the public API."""
        call("PUT", f"/api/env/{self.env}/settings", {"session_id": self.inst.sid, field: value})
