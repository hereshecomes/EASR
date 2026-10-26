"""Build frozen cohort manifests (assignments + ceilings) from task specifications.

Usage:
  python -m pair.cohort smoke OUT_DIR TEMPLATE WORLD POLICY [SEED]
  python -m pair.cohort smoke_dynamic OUT_DIR CASE POLICY CONDITION [SEED]
  python -m pair.cohort dev OUT_DIR PROTOCOL_JSON
A manifest is written once; the command refuses to overwrite an existing cohort directory.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

from . import tasks

WORLDS = ["base", "irrelevant", "causal", "distractor", "conflict", "base_repeat"]


def rules_for(spec: dict, tmpl: dict) -> list[str]:
    return spec["rules"][tmpl["app"]] + tmpl.get("extra_rules", [])


def static_assignment(spec: dict, tmpl: dict, world: str, policy: str, seed: int, kind: str, rep: int) -> dict:
    build = tasks.resolve_world(spec, tmpl, world if world != "base_extra" else "base")
    w = "base" if world == "base_extra" else world
    return {"kind": kind, "app": tmpl["app"], "template": tmpl["template"], "instance": tmpl["instance"], "world": w,
            "repetition": rep, "policy": policy, "seed": seed, "goal": tmpl["goal"], "rules": rules_for(spec, tmpl),
            "build_spec": build, "intervention": tasks.intervention_text(spec, tmpl, w),
            "expected": tasks.expected(spec, tmpl, w), "relevant_facts": tmpl["relevant_facts"]}


def dynamic_assignment(spec: dict, case: dict, policy: str, condition: str, seed: int) -> dict:
    tmpl = tasks.template(spec, case["template"])
    build = {**case["world"], "world": "dynamic_" + condition}
    return {"kind": "dynamic", "app": case["app"], "template": case["template"], "instance": case["case"],
            "dynamic_case": case["case"], "world": "dynamic", "condition": condition, "repetition": 0, "policy": policy,
            "seed": seed, "goal": tmpl["goal"] if case["case"] != "oscar_select_none" else tmpl["goal"],
            "rules": rules_for(spec, tmpl), "build_spec": build, "event": case["event"],
            "intervention": "External update after delivery: " + case["event"]["trigger"] if condition == "external_update" else "No external update (control).",
            "expected": {"no_update": case["expected_no_update"], "after_update": case["expected_after_update"]},
            "relevant_facts": tmpl["relevant_facts"]}


def write(out: pathlib.Path, manifest: dict) -> None:
    out.mkdir(parents=True, exist_ok=False)
    for i, a in enumerate(manifest["assignments"]):
        a["index"] = i
        a["episode_id"] = f"{manifest['cohort_id']}_{i:03d}"
    manifest["created_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    (out / "episodes").mkdir()


def main(argv):
    mode, out = argv[0], pathlib.Path(argv[1])
    spec = tasks.load("dev_v0")
    if mode == "smoke":
        tmpl = tasks.template(spec, argv[2])
        a = static_assignment(spec, tmpl, argv[3], argv[4], int(argv[5]) if len(argv) > 5 else 1, "engineering_smoke", 0)
        manifest = {"cohort_id": out.name, "role": "engineering_probe", "task_spec": "dev_v0", "assignments": [a],
                    "run_config": {}, "endpoint": {"url": "http://127.0.0.1:18500/v1/chat/completions", "model": "pair-actor"},
                    "limits": {"stop_after_consecutive_infra": 1}}
        write(out, manifest)
        return
    if mode == "smoke_dynamic":
        case = next(c for c in spec["dynamic_cases"] if c["case"] == argv[2])
        a = dynamic_assignment(spec, case, argv[3], argv[4], int(argv[5]) if len(argv) > 5 else 1)
        a["kind"] = "engineering_smoke"
        manifest = {"cohort_id": out.name, "role": "engineering_probe", "task_spec": "dev_v0", "assignments": [a],
                    "run_config": {}, "endpoint": {"url": "http://127.0.0.1:18500/v1/chat/completions", "model": "pair-actor"},
                    "limits": {"stop_after_consecutive_infra": 1}}
        write(out, manifest)
        return
    protocol = json.loads(pathlib.Path(argv[2]).read_text())
    assignments = []
    for unit in protocol["assignments"]:
        if unit["kind"] == "dynamic":
            case = next(c for c in spec["dynamic_cases"] if c["case"] == unit["case"])
            assignments.append(dynamic_assignment(spec, case, unit["policy"], unit["condition"], unit["seed"]))
        else:
            tmpl = tasks.template(spec, unit["template"])
            assignments.append(static_assignment(spec, tmpl, unit["world"], unit["policy"], unit["seed"], unit["kind"], unit["repetition"]))
    manifest = {"cohort_id": protocol["cohort_id"], "role": protocol["role"], "task_spec": "dev_v0", "protocol": argv[2],
                "assignments": assignments, "run_config": protocol["run_config"], "endpoint": protocol["endpoint"],
                "limits": protocol["limits"]}
    write(out, manifest)


if __name__ == "__main__":
    main(sys.argv[1:])
