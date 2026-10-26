"""External-update schedule (v2): explicit, recorded timing for every environment change.

An event is {id, object, field, value, expect (optional current value), relevance, trigger}.
relevance is the author's label: relevant | irrelevant | compatible | conflicting (analysis only;
no runtime mechanism reads it). Triggers:
  {"kind": "start"}                      before the agent's first observation
  {"kind": "after_delivery", "object", "field", "sources": [...], "value": optional,
   "before_business_change": true}       after a delivered observation shows that fact
  {"kind": "before_commit", "nth": 1}    immediately before the nth commit-like action executes,
                                         after any guard or validation (the residual race window)
  {"kind": "after_commit", "nth": 1}     right after the nth commit-like action has executed (a legitimate later update)
  {"kind": "after_event", "event": id, "steps": k}
                                         k delivered steps after another event fired
  {"kind": "at_step", "step": k, "before_business_change": true}
                                         after the k-th delivered observation (random-timing baseline)
Each firing writes through the application's own tables and returns a receipt with the value
before and after; nothing is retried, and a precondition mismatch is recorded, not forced.
"""
from __future__ import annotations

import time


class EventSchedule:
    def __init__(self, events: list, write, read):
        self.events = [dict(e) for e in events]
        self.write, self.read = write, read
        self.fired = {}            # event id -> receipt
        self.commit_count = 0
        self.after_commit_count = 0

    def describe(self) -> list:
        return self.events

    def _fire(self, ev: dict, step, where: str) -> dict:
        before = self.read(ev["object"], ev["field"])
        if ev.get("expect") is not None and str(before) != str(ev["expect"]):
            rec = {"kind": "ext", "status": "precondition_mismatch", "event": ev["id"], "step": step, "where": where,
                   "object": ev["object"], "field": ev["field"], "observed_before": before, "expected_before": ev["expect"]}
        else:
            self.write(ev["object"], ev["field"], ev["value"])
            after = self.read(ev["object"], ev["field"])
            rec = {"kind": "ext", "status": "fired", "event": ev["id"], "step": step, "where": where, "object": ev["object"],
                   "field": ev["field"], "before": before, "after": after, "relevance": ev.get("relevance"), "t": time.time(),
                   "after_delivery_of_step": step if where == "after_delivery" else None}
        self.fired[ev["id"]] = {**rec, "delivered_steps_since": 0}
        return rec

    def on_start(self) -> list:
        return [self._fire(ev, -1, "start") for ev in self.events if ev["trigger"]["kind"] == "start" and ev["id"] not in self.fired]

    def after_delivery(self, step: int, facts: list, business_changed: bool) -> list:
        out = []
        for f in self.fired.values():
            f["delivered_steps_since"] += 1
        for ev in self.events:
            if ev["id"] in self.fired:
                continue
            t = ev["trigger"]
            if t["kind"] == "after_delivery":
                hit = any(f["object"] == t["object"] and f["field"] == t["field"] and f.get("source") in t.get("sources", [f.get("source")])
                          and (t.get("value") is None or _norm(f["value"]) == _norm(t["value"])) for f in facts)
                if not hit:
                    continue
                if t.get("before_business_change", True) and business_changed:
                    self.fired[ev["id"]] = {"kind": "ext", "status": "not_eligible_business_change_already_recorded", "event": ev["id"], "step": step,
                                            "delivered_steps_since": 0}
                    out.append(dict(self.fired[ev["id"]]))
                    continue
                out.append(self._fire(ev, step, "after_delivery"))
            elif t["kind"] == "at_step":
                if step < t["step"]:
                    continue
                if t.get("before_business_change", True) and business_changed:
                    self.fired[ev["id"]] = {"kind": "ext", "status": "not_eligible_business_change_already_recorded", "event": ev["id"], "step": step,
                                            "delivered_steps_since": 0}
                    out.append(dict(self.fired[ev["id"]]))
                    continue
                out.append(self._fire(ev, step, "at_step"))
            elif t["kind"] == "after_event":
                prev = self.fired.get(t["event"])
                if prev and prev.get("status") == "fired" and prev["delivered_steps_since"] >= t.get("steps", 1):
                    out.append(self._fire(ev, step, "after_event"))
        return out

    def before_commit(self, step: int) -> list:
        self.commit_count += 1
        return [self._fire(ev, step, "before_commit") for ev in self.events
                if ev["id"] not in self.fired and ev["trigger"]["kind"] == "before_commit" and ev["trigger"].get("nth", 1) == self.commit_count]


def _after_commit(self, step: int) -> list:
    self.after_commit_count += 1
    return [self._fire(ev, step, "after_commit") for ev in self.events
            if ev["id"] not in self.fired and ev["trigger"]["kind"] == "after_commit" and ev["trigger"].get("nth", 1) == self.after_commit_count]


EventSchedule.after_commit = _after_commit


def _norm(v) -> str:
    s = str(v).replace("$", "").replace(",", "").strip()
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return " ".join(s.split()).lower()
