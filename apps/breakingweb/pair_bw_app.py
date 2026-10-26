"""BreakingWeb served unchanged, plus evaluator-only endpoints for the PAIR harness.

GET /pair/state/{session_id} returns the session's full state and the audit log of applied mutation payloads.
POST /pair/fault/{session_id} arms a server-side fault for the session's next settings saves (BreakingWeb-derived interventions,
  after its settings_retry family): {"kind": "silent_fail" | "drop_key", "count": n, "key": name}
    silent_fail  the save is answered normally (200, the settings as stored) but nothing is applied (misleading success feedback)
    drop_key     the save applies every submitted key except `key` (partial commit)
Both require the header X-Pair-Evaluator to equal $PAIR_EVAL_SECRET; agent browser contexts block every /pair/ path. The fault acts on
the request inside the server, so it applies equally to every agent and controller. Nothing else in the application is modified.
"""
import json
import os
import re

from fastapi import HTTPException, Request

from breakingweb.app import app as fastapi_app

FAULTS = {}                                   # session id -> {"kind", "count", "key"}
SETTINGS_PUT = re.compile(r"^/api/env/[^/]+/settings$")


def _check(request: Request):
    if request.headers.get("x-pair-evaluator") != os.environ.get("PAIR_EVAL_SECRET", "unset"):
        raise HTTPException(status_code=403, detail="evaluator only")


@fastapi_app.get("/pair/state/{session_id}")
def pair_state(session_id: str, request: Request):
    _check(request)
    try:
        st = request.app.state.session_manager.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit = [{"action": e.action, "payload": e.payload} for e in getattr(st, "audit_log", [])]
    return {"env_id": st.env_id, "task_id": st.task_id, "state": st.model_dump(mode="json", exclude={"audit_log"}), "audit": audit,
            "fault": FAULTS.get(session_id)}


@fastapi_app.post("/pair/fault/{session_id}")
async def pair_fault(session_id: str, request: Request):
    _check(request)
    spec = await request.json()
    if spec.get("kind") not in ("silent_fail", "drop_key"):
        raise HTTPException(status_code=400, detail="unknown fault")
    FAULTS[session_id] = {"kind": spec["kind"], "count": int(spec.get("count", 1)), "key": spec.get("key"), "applied": 0}
    return FAULTS[session_id]


def _apply_fault(body: bytes) -> bytes:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return body
    f = FAULTS.get(data.get("session_id")) if isinstance(data, dict) else None
    if not f or f["count"] <= 0:
        return body
    f["count"] -= 1
    f["applied"] += 1
    if f["kind"] == "silent_fail":
        data = {k: v for k, v in data.items() if k == "session_id"}
    else:
        data.pop(f["key"], None)
    return json.dumps(data).encode()


class FaultASGI:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):                 # expose the FastAPI app's attributes (state, routes) to callers
        return getattr(self.inner, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "PUT" or not SETTINGS_PUT.match(scope.get("path", "")) or not FAULTS:
            return await self.inner(scope, receive, send)
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        new = _apply_fault(body)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": new, "more_body": False}
            return await receive()
        headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"] + [(b"content-length", str(len(new)).encode())]
        return await self.inner(dict(scope, headers=headers), replay, send)


app = FaultASGI(fastapi_app)
