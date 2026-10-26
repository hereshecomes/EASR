"""Build the compact event ledger for one episode (evaluator side, shared by all evaluator arms).

The ledger is a deterministic function of the raw episode folder. It keeps object identity,
delivered public facts (with whether each matched the backend when captured), actions and their
results, business mutations translated to business terms, background-write counts, external
events, guard checks, and evidence status at every business commit. Raw text is referenced by
(episode, step), never paraphrased.
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import urlsplit

from . import facts as fact_sem
from .apps import crm as crm_app
from .apps import oscar as oscar_app
from .apps import mantis as mantis_app

APPS = {"oscar": oscar_app, "crm": crm_app, "mantis": mantis_app}


def _read_jsonl(path: pathlib.Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _path(url: str) -> str:
    p = urlsplit(url or "")
    return p.path + (("?" + p.query) if p.query else "")


def _target_label(action: dict, controls: list) -> str:
    c = next((c for c in controls if c["id"] == str(action.get("id"))), None)
    if not c:
        return ""
    bits = [c["tag"], repr(c["label"]) if c["label"] else "", f"name={c['name']}" if c["name"] else ""]
    if c.get("href"):
        bits.append("-> " + _path(c["href"]))
    return " ".join(b for b in bits if b)


def build(episode_dir: pathlib.Path, relevant_facts: list[str]) -> dict:
    episode_dir = pathlib.Path(episode_dir)
    assignment = json.loads((episode_dir / "assignment.json").read_text())
    summary = json.loads((episode_dir / "episode.json").read_text())
    initial = json.loads((episode_dir / "initial_state.json").read_text())
    app = APPS[assignment["app"]]
    raw_records = _read_jsonl(episode_dir / "steps.jsonl")
    steps = [s for s in raw_records if "step" in s]
    start_events = [e for r in raw_records if "start_events" in r for e in r["start_events"]]
    events, n = [], 0
    last_seen = {}                       # "object.field" -> (value, step, event_id, current_at_capture, source)
    sources_seen = {}                    # "object.field" -> set of sources delivered so far
    backend_now = {}                     # "object.field" -> value, updated by external events
    state_prev = initial

    def add(kind, **body):
        nonlocal n
        n += 1
        ev = {"id": f"e{n:04d}", "kind": kind, **body}
        events.append(ev)
        return ev["id"]

    for f in relevant_facts:
        backend_now[f] = None

    def record_ext(ext, step):
        if ext.get("status") == "fired":
            for key, value in fact_sem.ext_facts(ext):
                backend_now[key] = value
        add("ext", **{k: v for k, v in ext.items() if k not in ("kind", "t")})

    for ext in start_events:
        record_ext(ext, -1)
    for s in steps:
        obs = s.get("obs", {})
        facts = [f for f in s.get("facts", []) if f["object"] not in ("page",)]
        notice = next((f["value"] for f in s.get("facts", []) if f["object"] == "page" and f["field"] == "notice"), None)
        delivered = s.get("delivered") is True
        obs_id = add("obs", step=s["step"], url=_path(obs.get("url")), title=obs.get("title"), delivered=delivered,
                     notice=notice, truncated=bool((obs.get("limits") or {}).get("text_truncated")),
                     facts=[{k: f[k] for k in ("object", "field", "value", "source", "current") if k in f} for f in facts],
                     raw_ref=f"{episode_dir.name}/step_{s['step']:03d}")
        if "module=OptimisticLock" in (obs.get("url") or ""):
            obj = next((f["object"] for f in facts if f.get("source") == "conflict"), None)
            add("reject", step=s["step"], by="application_native_lock", object=obj, observed_in=obs_id)
        if "pair_rejected=1" in (obs.get("url") or ""):
            obj = next((f["object"] for f in facts if f.get("source") in ("detail", "record")), None)
            add("reject", step=s["step"], by="application_conditional_commit", object=obj, observed_in=obs_id)
        if delivered:
            for f in facts:
                key = f"{f['object']}.{f['field']}"
                if f.get("source") in ("detail", "list", "basket", "record", "edit", "banner", "conflict"):
                    prev = last_seen.get(key)
                    # A cached display known to be stale at capture (a list or banner) does not replace an authoritative
                    # value the agent has already seen on the object's own page.
                    if (prev is not None and f.get("source") in ("list", "banner") and f.get("current") is False
                            and prev[4] in ("detail", "basket", "record", "edit")):
                        sources_seen.setdefault(key, set()).add(f.get("source"))
                        continue
                    last_seen[key] = (f["value"], s["step"], obs_id, f.get("current"), f.get("source"))
                    sources_seen.setdefault(key, set()).add(f.get("source"))
        race_events = [x for x in s.get("external_events", []) if x.get("where") == "before_commit"]
        for ext in s.get("external_events", []):
            if ext.get("where") != "before_commit":
                record_ext(ext, s["step"])
        if "parse" not in s:
            if s.get("infra_error"):
                add("error", step=s["step"], detail=s["infra_error"][:300])
            continue
        if not s["parse"]["valid"]:
            add("act", step=s["step"], valid=False, reason=s["parse"]["reason"])
            continue
        act = s["action"]
        act_body = {"type": act["type"]}
        for k in ("text", "value", "key", "url", "direction", "answer"):
            if k in act:
                act_body[k] = act[k]
        target = _target_label(act, obs.get("controls", []))
        if target:
            act_body["target"] = target
        act_id = add("act", step=s["step"], valid=True, action=act_body, note=(s.get("note") or "")[:300],
                     result=(s.get("exec") or {}).get("status"), error=((s.get("exec") or {}).get("error") or "")[:200] or None,
                     url_after=_path((s.get("exec") or {}).get("url_after")))
        if s.get("guard") and (s["guard"]["decision"] != "not_covered" or s["guard"].get("commit_like")):
            g = s["guard"]
            add("guard", step=s["step"], decision=g["decision"], object=g.get("object"),
                changed=g.get("changed"), commit_like=g.get("commit_like"))
        for ext in race_events:          # after the decision and any check, before the stored write
            record_ext(ext, s["step"])
        muts = s.get("mutations", [])
        state_after = state_prev  # translation only needs id->upc maps, stable within an episode
        bg = {}
        for m in muts:
            # Re-derive the class with the current declared rules (raw records keep what was logged).
            if m["op"] == "update" and m.get("before") in (None, "") and m.get("after") in (None, ""):
                continue
            m = {**m, "class": app.classify(m["table"], m.get("column"))}
            if m["class"] in ("background", "metadata"):
                bg[m["table"]] = bg.get(m["table"], 0) + 1
                continue
            desc = app.describe_mutation(m, state_prev, state_after)
            evidence = []
            for key in relevant_facts:
                seen = last_seen.get(key)
                at_commit = backend_now.get(key)
                if seen is None:
                    status = "never_seen"
                else:
                    value, step_seen, ev_id, current, source = seen
                    if at_commit is not None:
                        status = "fresh" if app.same_value(key.rsplit(".", 1)[1], value, at_commit) else "stale"
                    else:
                        status = "fresh" if current is not False else "stale"
                evidence.append({"fact": key, "status": status, "sources_seen": sorted(sources_seen.get(key, set())),
                                 **({"last_seen": seen[0], "seen_step": seen[1], "seen_event": seen[2], "seen_source": seen[4]} if seen else {})})
            add("mut", step=s["step"], cause=act_id, cls=m["class"], table=m["table"], **desc, evidence_at_commit=evidence)
        if bg:
            add("background", step=s["step"], cause=act_id, writes=bg)
        if act["type"] == "stop":
            add("stop", step=s["step"], answer=act.get("answer", ""))
    final_state = summary.get("final_state") or {}
    add("final", status=summary["status"], termination=summary["termination"],
        terminal=app.terminal_summary(final_state, initial) if final_state else None)
    return {"episode_id": assignment["episode_id"], "app": assignment["app"], "template": assignment["template"],
            "world": assignment["world"], "events": events,
            "costs": {**summary.get("usage", {}), "seconds": summary.get("seconds")},
            "counts": summary.get("counts", {}), "status": summary["status"], "termination": summary["termination"]}
