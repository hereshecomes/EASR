"""Cohort worker: runs assigned episodes on one slot (its own Oscar and CRM instances).

Usage: env/tools/bin/python -m pair.worker COHORT_DIR SLOT
COHORT_DIR holds manifest.json (frozen assignments and ceilings). Workers claim assignments in
manifest order through an flock-protected claims file, so every assignment runs at most once and
is never replaced. Every claimed assignment ends with exactly one line in index.jsonl, whatever
happens. A shared consecutive-infrastructure-failure counter stops all workers when it reaches
the manifest's limit; unclaimed assignments are then recorded as not_run by the finalizer.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pathlib
import sys
import time
import traceback

from playwright.async_api import async_playwright

from . import llm, tasks
from .guard import RevalidationGuard
from .runner import Episode, RunConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Claims:
    def __init__(self, cohort: pathlib.Path):
        self.path = cohort / "claims.json"
        self.lock = cohort / "claims.lock"

    def _update(self, fn):
        with self.lock.open("a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = json.loads(self.path.read_text()) if self.path.exists() else {"claimed": [], "consecutive_infra": 0, "stopped": None}
            result = fn(data)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
            return result

    def claim(self, ids: list, limit: int, slot: int, limits: dict | None = None):
        index = self.path.parent / "index.jsonl"
        tokens = 0
        if index.exists():
            for line in index.read_text().splitlines():
                u = json.loads(line).get("usage") or {}
                tokens += int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))

        def fn(data):
            data.setdefault("started", time.time())
            if limits and not data["stopped"]:
                if tokens >= limits.get("total_actor_tokens", float("inf")):
                    data["stopped"] = {"reason": "total_actor_token_ceiling", "tokens": tokens, "t": time.time()}
                elif time.time() - data["started"] >= limits.get("wall_hours", float("inf")) * 3600:
                    data["stopped"] = {"reason": "wall_time_ceiling", "t": time.time()}
            if data["stopped"]:
                return None
            for eid in ids:
                if eid not in data["claimed"]:
                    data["claimed"].append(eid)
                    return eid
            return None
        return self._update(fn)

    def report(self, infra: bool, limit: int):
        def fn(data):
            data["consecutive_infra"] = data["consecutive_infra"] + 1 if infra else 0
            if data["consecutive_infra"] >= limit and not data["stopped"]:
                data["stopped"] = {"reason": "consecutive_infrastructure_failures", "t": time.time()}
            return data["stopped"]
        return self._update(fn)


def make_adapter(app: str, slot: int, assignment: dict, instances: dict):
    if app == "breakingweb":
        from .apps.breakingweb import BwAdapter
        return BwAdapter(instances["breakingweb"], dynamic={"events": assignment["events"]} if assignment.get("events") else None)
    if app == "mantis":
        from .apps.mantis import MantisAdapter
        return MantisAdapter(instances["mantis"], dynamic={"events": assignment["events"]} if assignment.get("events") else None)
    if assignment.get("events"):              # v2 explicit event schedule (repair study)
        if app == "oscar":
            from .apps.oscar import OscarAdapter
            return OscarAdapter(instances["oscar"], dynamic={"events": assignment["events"]})
        from .apps.crm import CrmAdapter
        return CrmAdapter(instances["crm"], dynamic={"events": assignment["events"]})
    if app == "oscar":
        from .apps.oscar import OscarAdapter
        dynamic = None
        if assignment.get("dynamic_case"):
            ev = assignment["event"]
            dynamic = {"condition": assignment["condition"], "object": ev["object"], "old": ev["old"], "new": ev["new"]}
        return OscarAdapter(instances["oscar"], dynamic=dynamic)
    from .apps.crm import CrmAdapter
    dynamic = None
    if assignment.get("dynamic_case"):
        dynamic = {"condition": assignment["condition"], **assignment["event"]}
    return CrmAdapter(instances["crm"], dynamic=dynamic)


def instance_for(app: str, slot: int, instances: dict):
    if app not in instances:
        if app == "oscar":
            from .apps.oscar import OscarInstance
            instances[app] = OscarInstance(slot)
        elif app == "mantis":
            from .apps.mantis import MantisInstance
            instances[app] = MantisInstance(slot)
        else:
            from .apps.crm import CrmInstance
            instances[app] = CrmInstance(slot)
    return instances[app]


async def run_one(browser, cohort: pathlib.Path, manifest: dict, a: dict, slot: int, instances: dict) -> dict:
    spec = tasks.load(manifest["task_spec"])
    if a["app"] == "breakingweb":             # one backend session per episode (the shared service holds no other state of ours)
        from .apps.breakingweb import BwSession
        instances["breakingweb"] = BwSession(a["build_spec"]["env"], task_id=a["build_spec"].get("task_id"))
    inst = instance_for(a["app"], slot, instances)
    build = inst.build(a["build_spec"])
    inst.start()
    adapter = make_adapter(a["app"], slot, a, instances)
    if a.get("faults") and hasattr(adapter, "arm_faults"):                   # server-side faults of the intervention class
        a["faults_armed"] = adapter.arm_faults(a["faults"])
    for key, value in a.get("setup", []):    # world corrections declared by the task (before the agent starts)
        adapter.write_value(*key.rsplit(".", 1), value)
    watch = (a.get("easr") or {}).get("watch", [])
    watched_initial = {k: adapter.read_value(*k.rsplit(".", 1)) for k in watch}
    sched = getattr(adapter, "schedule", None)
    for ev in (sched.events if sched else []):         # an event that restores a field's initial value (repeated-change classes)
        if ev.get("value") == "__initial__":
            ev["value"] = watched_initial.get(f"{ev['object']}.{ev['field']}")
    cfg = RunConfig(policy=a["policy"], seed=a["seed"], **manifest["run_config"])
    ep = llm.Endpoint(**manifest["endpoint"])
    if a.get("arm"):
        from . import repair
        guard = repair.make_guard(a, adapter, json.loads((ROOT / "tasks" / f"{manifest['repair_spec']}.json").read_text()))
    else:
        guard = RevalidationGuard(adapter) if a["policy"] == "guard" else None
    out = cohort / "episodes" / a["episode_id"]
    probe = (lambda: {k: adapter.read_value(*k.rsplit(".", 1)) for k in watch}) if watch else None
    episode = Episode(cfg, ep, adapter, out, a["goal"], a["rules"], guard=guard, watch=probe)
    (cohort / "episodes" / a["episode_id"]).mkdir(parents=True, exist_ok=True)
    (out / "assignment.json").write_text(json.dumps({**a, "slot": slot, "world_build": build}, indent=1))
    watched_final = None
    try:
        summary = await episode.run(browser, inst.start_url(a))
        watched_final = {k: adapter.read_value(*k.rsplit(".", 1)) for k in watch}
    finally:
        inst.stop()
    if watch:
        summary["watched"] = {"initial": watched_initial, "final": watched_final}
    summary["blocked_requests"] = len(adapter.blocked)
    summary["dynamic"] = getattr(adapter, "dynamic_state", None)
    if a.get("events"):
        from . import repair
        summary["fired_events"] = repair.fired_events(adapter)
    (out / "episode.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


async def main(cohort: pathlib.Path, slot: int) -> None:
    manifest = json.loads((cohort / "manifest.json").read_text())
    order = [a["episode_id"] for a in manifest["assignments"]]
    by_id = {a["episode_id"]: a for a in manifest["assignments"]}
    claims = Claims(cohort)
    instances = {}
    limit = manifest["limits"]["stop_after_consecutive_infra"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        while True:
            eid = claims.claim(order, limit, slot, manifest["limits"])
            if eid is None:
                break
            a = by_id[eid]
            t0 = time.time()
            try:
                summary = await run_one(browser, cohort, manifest, a, slot, instances)
                row = {"episode_id": eid, "slot": slot, "status": summary["status"], "termination": summary["termination"],
                       "usage": summary["usage"], "counts": summary["counts"], "seconds": summary["seconds"]}
            except Exception as exc:
                row = {"episode_id": eid, "slot": slot, "status": "infra_failure", "termination": "worker_error",
                       "error": repr(exc)[:500], "traceback": traceback.format_exc()[-3000:]}
            row["wall_seconds"] = round(time.time() - t0, 1)
            with (cohort / "index.jsonl").open("a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.write(json.dumps(row) + "\n")
            stopped = claims.report(row["status"] == "infra_failure", limit)
            if stopped:
                break
        await browser.close()
    for inst in instances.values():
        inst.stop()


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "env/browsers"))
    asyncio.run(main(pathlib.Path(sys.argv[1]), int(sys.argv[2])))
