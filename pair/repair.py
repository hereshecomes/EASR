"""Repair study (claim C): scenario x arm -> episode assignment, validator construction, and evaluation.

Scenarios: tasks/repair_dev_v1.json (dev only). Arms are defined in the same file; every arm shares the
interface, budgets and model settings, and differs only in policy prompt (raw vs verify) and mechanism.
"""
from __future__ import annotations

import json
import pathlib

from . import cohort, ledger, oracle, tasks

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str = "repair_dev_v1") -> dict:
    return json.loads((ROOT / "tasks" / f"{name}.json").read_text())


def applicable(spec: dict, scn: dict, arm: str) -> bool:
    return scn["app"] in spec["arms"][arm].get("apps", [scn["app"]])


def assignment(spec: dict, scn: dict, arm: str, seed: int, kind: str = "repair_dev", task_spec: str = "dev_v0") -> dict:
    base = tasks.load(task_spec)
    tmpl = tasks.template(base, scn["template"])
    cfg = spec["arms"][arm]
    build = tasks.resolve_world(base, tmpl, scn["world"])
    build.update(world=f"repair_{scn['id']}", native_occ=cfg.get("native_occ", "off"), app_tier=cfg.get("app_tier", "none"))
    return {"kind": kind, "app": scn["app"], "template": scn["template"], "instance": tmpl["instance"], "world": scn["world"],
            "scenario": scn["id"], "relevance": scn["relevance"], "dynamic_case": scn["id"], "arm": arm, "tier": cfg["tier"],
            "policy": cfg["policy"], "seed": seed, "goal": tmpl["goal"], "rules": cohort.rules_for(base, tmpl),
            "build_spec": build, "events": scn["events"], "dependencies": list(tmpl["relevant_facts"]),
            "expected": scn["expected"], "relevant_facts": list(tmpl["relevant_facts"]), "repetition": 0}


def make_guard(a: dict, adapter, spec: dict | None = None):
    cfg = (spec or load())["arms"][a["arm"]]
    if cfg.get("controller"):                          # EASR study arms: the common registry (pair/controllers.py)
        from . import controllers
        return controllers.make(cfg["controller"], adapter, a.get("dependencies") or [], start_url=a.get("start_url"), task=a)
    kind = cfg.get("guard")
    if kind is None:
        return None
    if kind == "v1":
        from .guard import RevalidationGuard
        return RevalidationGuard(adapter)
    from .guards import GuardV2
    return GuardV2(adapter, kind, a["dependencies"], post_check=bool(cfg.get("post_check")))


def fired_events(adapter) -> list:
    sched = getattr(adapter, "schedule", None)
    return sorted(k for k, r in (sched.fired.items() if sched else []) if r.get("status") == "fired")


def evaluate_episode(d: pathlib.Path) -> dict:
    """Oracle v2 verdict plus repair-study metrics for one finished episode folder."""
    d = pathlib.Path(d)
    a = json.loads((d / "assignment.json").read_text())
    ep = json.loads((d / "episode.json").read_text())
    init = json.loads((d / "initial_state.json").read_text())
    app = __import__("pair.apps." + a["app"], fromlist=["x"])
    fired = ep.get("fired_events", [])
    L = ledger.build(d, a["relevant_facts"])
    o = oracle.evaluate(L, a, app.terminal_summary(init, init), fired=set(fired))
    exp = oracle.expected_for(a, set(fired))
    steps = [json.loads(x) for x in (d / "steps.jsonl").read_text().splitlines() if x.strip()]
    steps = [s for s in steps if "step" in s]
    guard_recs = [s["guard"] for s in steps if s.get("guard")]
    r = o["repair"]
    # the v1 guard does not report reads: it reads the target's page once per covered check
    reads = sum(int(g.get("reads", 1 if g.get("decision") in ("pass", "hold", "check_failed") else 0) or 0) for g in guard_recs) + sum(int((s.get("post_check") or {}).get("reads", 0) or 0) for s in steps)
    lost = [v for v in o["violations"] if v.get("lost_update")]
    # transient wrong commitment: a stored change of an authorized target to a value the final answer does not have
    wrong = wrong_commitments(L, a, exp)
    return {"episode": d.name, "scenario": a["scenario"], "arm": a["arm"], "tier": a["tier"], "app": a["app"], "seed": a["seed"],
            "relevance": a["relevance"], "fired": fired, "completion": o["completion"], "final_correct": o["final_state_correct"],
            "agent_compliant": o["agent_compliant"], "lost_update": bool(lost), "blind_overwrites": len(r["blind_overwrites"]),
            "unsafe_commit": r["unsafe_mutation_committed"], "race_commit": any(s.get("race") for s in r["unsafe_commits"]),
            "wrong_commitments": wrong, "holds": len(r["holds"]), "justified_holds": r["unsafe_action_intercepted"],
            "false_holds": r["legitimate_action_held"], "post_notices": sum(1 for s in steps if (s.get("post_check") or {}).get("notice")),
            "rejects": sum(1 for e in L["events"] if e["kind"] == "reject"), "recovery": r["recovery"],
            "steps": len(steps), "validator_reads": reads, "model_calls": ep["usage"]["calls"],
            "tokens": ep["usage"]["prompt_tokens"] + ep["usage"]["completion_tokens"], "seconds": ep["seconds"],
            "termination": ep["termination"], "violations": sorted({(v["category"], v["attribution"]) for v in o["violations"]})}


def wrong_commitments(L, a, exp) -> int:
    muts = [m for m in L["events"] if m["kind"] == "mut" and m["cls"] in ("business", "unlisted")]
    if a["app"] == "oscar":
        want = exp.get("chargers", {})
        return sum(1 for m in muts if m["object"].split(":")[0] == "basket_line" and m["object"].split(":")[1] in oracle.CHARGERS
                   and m["op"] == "add" and m["object"].split(":")[1] not in want)
    targets = oracle.crm_targets(a, exp)
    n = 0
    for m in muts:
        want = targets.get(m["object"], {}).get(m["field"])
        if (m["object"], m["field"]) in {(o, f) for o, fs in targets.items() for f in fs} and str(m["after"]) != str(want):
            n += 1
        elif not targets and m["object"].startswith("contact:alex") and m["field"] == "phone_work":
            n += 1
    return n


# ---- cohort manifests and summaries ---------------------------------------------------------------
def manifest(out: pathlib.Path, arms: list, seeds: list, scenarios: list | None = None, role: str = "repair_dev_pilot",
             endpoint: dict | None = None, actor: str = "Qwen3.8-27B-FP8", spec_name: str = "repair_dev_v1") -> dict:
    import random
    spec = load(spec_name)
    proto = json.loads((ROOT / "protocol/dev_actor_v1.json").read_text())
    scns = [s for s in spec["scenarios"] if not scenarios or s["id"] in scenarios]
    assignments = []
    for seed in seeds:
        rng = random.Random(1000 + seed)
        block = [(s, arm) for s in scns for arm in arms if applicable(spec, s, arm)]
        rng.shuffle(block)                       # interleave arms and scenarios within each seed block
        assignments += [assignment(spec, s, arm, seed, kind=role) for s, arm in block]
    m = {"cohort_id": out.name, "role": role, "task_spec": "dev_v0", "repair_spec": spec_name, "actor": actor,
         "assignments": assignments, "run_config": proto["run_config"], "endpoint": endpoint or proto["endpoint"],
         "limits": {"actor_episodes": len(assignments), "stop_after_consecutive_infra": 3, "total_actor_tokens": 60_000_000,
                    "wall_hours": 10, "replacement_of_failed_assignments": "never", "reruns": "never"},
         "design": {"arms": arms, "seeds": seeds, "scenarios": [s["id"] for s in scns], "order": "per seed block, shuffled with Random(1000+seed)"}}
    cohort.write(out, m)
    return m


def summarize(rows: list, by=("arm",)) -> list:
    """Means per group; rows from evaluate_episode (or micro results)."""
    groups = {}
    for r in rows:
        if "error" in r:
            continue
        groups.setdefault(tuple(r[k] for k in by), []).append(r)
    out = []
    for key, rs in sorted(groups.items()):
        n = len(rs)
        mean = lambda f: round(sum(float(f(r)) for r in rs) / n, 3)
        out.append({**dict(zip(by, key)), "n": n,
                    "final_correct": mean(lambda r: r["final_correct"]), "completed": mean(lambda r: r["completion"] == "completed"),
                    "lost_update": mean(lambda r: r["lost_update"]), "blind_overwrite": mean(lambda r: r["blind_overwrites"] > 0),
                    "unsafe_commit": mean(lambda r: r["unsafe_commit"]), "wrong_commit": mean(lambda r: r["wrong_commitments"] > 0),
                    "holds": mean(lambda r: r["holds"]), "false_holds": mean(lambda r: r["false_holds"]),
                    "justified_holds": mean(lambda r: r["justified_holds"]), "post_notices": mean(lambda r: r["post_notices"]),
                    "steps": mean(lambda r: r["steps"]), "model_calls": mean(lambda r: r["model_calls"]),
                    "tokens": mean(lambda r: r["tokens"]), "reads": mean(lambda r: r["validator_reads"]),
                    "agent_compliant": mean(lambda r: r["agent_compliant"])})
    return out


def scenario_bootstrap(rows: list, arm_a: str, arm_b: str, metric: str = "final_correct", reps: int = 5000, seed: int = 7) -> dict:
    """Paired difference arm_a - arm_b in a metric, resampling scenarios (the family-level unit) with replacement."""
    import random
    per = {}
    for r in rows:
        if "error" in r or r["arm"] not in (arm_a, arm_b):
            continue
        per.setdefault(r["scenario"], {}).setdefault(r["arm"], []).append(float(r[metric]))
    scn = [s for s, v in per.items() if arm_a in v and arm_b in v]
    if not scn:
        return {"scenarios": 0}
    diff = {s: sum(per[s][arm_a]) / len(per[s][arm_a]) - sum(per[s][arm_b]) / len(per[s][arm_b]) for s in scn}
    rng = random.Random(seed)
    boots = sorted(sum(diff[rng.choice(scn)] for _ in scn) / len(scn) for _ in range(reps))
    return {"scenarios": len(scn), "mean_diff": round(sum(diff.values()) / len(scn), 3),
            "ci95": [round(boots[int(0.025 * reps)], 3), round(boots[int(0.975 * reps) - 1], 3)],
            "per_scenario": {s: round(v, 2) for s, v in sorted(diff.items())}}


def main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="python -m pair.repair")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("out"); m.add_argument("--arms", required=True); m.add_argument("--seeds", required=True)
    m.add_argument("--scenarios", default=""); m.add_argument("--role", default="repair_dev_pilot")
    m.add_argument("--endpoint-url", default=None); m.add_argument("--model", default=None); m.add_argument("--actor", default="Qwen3.8-27B-FP8")
    e = sub.add_parser("evaluate")
    e.add_argument("cohort")
    a = p.parse_args(argv)
    if a.cmd == "manifest":
        ep = {"url": a.endpoint_url, "model": a.model} if a.endpoint_url else None
        man = manifest(ROOT / a.out, a.arms.split(","), [int(x) for x in a.seeds.split(",")],
                       [x for x in a.scenarios.split(",") if x] or None, a.role, ep, a.actor)
        print(json.dumps({"cohort": a.out, "assignments": len(man["assignments"])}))
    else:
        c = ROOT / a.cohort
        rows = []
        for d in sorted((c / "episodes").iterdir()):
            if (d / "episode.json").exists():
                try:
                    rows.append(evaluate_episode(d))
                except Exception as exc:
                    rows.append({"episode": d.name, "error": repr(exc)[:300]})
        (c / "repair_eval.jsonl").write_text("".join(json.dumps(r, default=list) + "\n" for r in rows))
        print(json.dumps(summarize(rows), indent=1))


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
