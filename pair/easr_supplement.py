"""Sensitivity supplement (written 2026-10-22, after the main experiments; spec 13.1-13.2 factors that had not been run).

  breadth      write-footprint breadth on MantisBT: the update page carries the operation's own fields only (narrow), the workflow fields
               (moderate) or every standard field (broad, the frozen default). bug_update.php keeps the stored value of every field the
               form does not carry, so the level sets which fields a save writes. Each operation runs clean, with its own concurrent
               change (core untouched field) and with a change to steps_to_reproduce (peripheral: written by the broad form only).
               Arms B0 (exposure reference), B3 refresh+retry, B7 Repair v1, EASR v2.
  length       composites of 6 and 8 subtasks on distinct records (SuiteCRM, MantisBT; the shop and BreakingWeb pools have no 6
               independent forms); classes and arms of E7 (B_dependency, C_untouched, D_both; B3, B6, B7, EASR). Step and time budgets
               scale with the number of subtasks (10 steps and 225 s per subtask, as K4 in E7).
  interaction  breadth (narrow, broad) x intensity (one change: C_untouched; three changes: I3_dep_untouched_same) x length (single
               MantisBT operation; 4-subtask MantisBT composites); arms B3, B7, EASR.
One seed (42); runner, oracle and controllers unchanged. Usage:
  python -m pair.easr_supplement OUT_DIR KIND --endpoint-url URL --model NAME --actor LABEL     (KIND: breadth, length6, length8, interaction)"""
import argparse, json, pathlib, random
from . import cohort, easr_eval as E

PAGE_FIELD = {"priority": "priority", "severity": "severity", "status": "status", "handler": "handler", "category": "category_id",
              "summary": "summary", "description": "description", "additional_information": "additional_info",
              "steps_to_reproduce": "steps_to_reproduce", "reproducibility": "reproducibility"}       # custom fields are shown as configured
WORKFLOW = ["project", "category_id", "priority", "severity", "status", "handler", "summary", "reproducibility", "view_state"]
MANTIS = [k for k, o in E.OPS.items() if o["app"] == "mantis"]


def update_fields(level: str, op_ids: list):
    if level == "broad":
        return None                                         # the shipped field list (as in every frozen world)
    own = []
    for i in op_ids:
        o = E.op(i)
        for k in [o["target"]] + ([o["dep"]] if o["dep"] else []):
            f = PAGE_FIELD.get(k.rsplit(".", 1)[1])
            if f and f not in own:
                own.append(f)
    return ["id"] + own + ([f for f in WORKFLOW if f not in own] if level == "moderate" else [])


def _cell(op_id, cls, arm, spec, level, factors):
    a = E.composite_assignment(op_id, cls, arm, spec, 42) if op_id in E.COMPOSITES else E.assignment(op_id, cls, arm, spec, 42)
    ids = E.COMPOSITES.get(op_id, [op_id])
    if level is not None:
        a["build_spec"]["update_fields"] = update_fields(level, ids)
        a["scenario"] += f":{level}"
    a["factors"] = factors
    return a


def build(kind: str):
    spec = E.arms_spec()
    cells, budget = [], {}
    if kind == "breadth":
        for o in MANTIS:
            for level in ("narrow", "moderate", "broad"):
                for arm in ("B0_vanilla", "B3_refresh_retry", "B7_repair_v1", "EASR_v2"):
                    cells.append((o, "clean", arm, level, {"breadth": level, "change": "none"}))
                    cells.append((o, "C_untouched", arm, level, {"breadth": level, "change": "core"}))
                    cells.append((o + "_peri", "C_untouched", arm, level, {"breadth": level, "change": "peripheral"}))
    elif kind in ("length6", "length8"):
        k = int(kind[-1])
        budget = {"max_steps": 10 * k, "episode_seconds": 225 * k}
        for comp in (f"K{k}_crm", f"K{k}b_crm", f"K{k}_mantis", f"K{k}b_mantis"):
            for cls in ("B_dependency", "C_untouched", "D_both"):
                for arm in ("B3_refresh_retry", "B6_full_replan", "B7_repair_v1", "EASR_v2"):
                    cells.append((comp, cls, arm, None, {"length": k}))
    elif kind == "interaction":
        for o in MANTIS + ["K4_mantis", "K4b_mantis", "K4c_mantis"]:
            for level in ("narrow", "broad"):
                for cls in ("C_untouched", "I3_dep_untouched_same"):
                    for arm in ("B3_refresh_retry", "B7_repair_v1", "EASR_v2"):
                        cells.append((o, cls, arm, level, {"breadth": level, "intensity": 1 if cls == "C_untouched" else 3,
                                                           "length": 4 if o in E.COMPOSITES else 1}))
    else:
        raise SystemExit(f"unknown kind {kind}")
    random.Random(42).shuffle(cells)
    return [_cell(o, c, a, spec, lv, f) for o, c, a, lv, f in cells], budget


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("kind")
    ap.add_argument("--endpoint-url", required=True); ap.add_argument("--model", required=True); ap.add_argument("--actor", required=True)
    x = ap.parse_args(argv)
    out = pathlib.Path(x.out)
    assignments, budget = build(x.kind)
    proto = json.loads((E.ROOT / "protocol/easr_eval_v1.json").read_text())
    m = {"cohort_id": out.name, "role": "easr_sensitivity_supplement", "task_spec": "dev_v0", "repair_spec": E.SPEC, "actor": x.actor,
         "assignments": assignments, "run_config": {**proto["run_config"], **budget},
         "endpoint": {**proto["endpoint"], "url": x.endpoint_url, "model": x.model},
         "limits": {"actor_episodes": len(assignments), "stop_after_consecutive_infra": 5, "total_actor_tokens": 400_000_000,
                    "wall_hours": 48, "replacement_of_failed_assignments": "never", "reruns": "never"},
         "design": {"kind": x.kind, "module": "pair/easr_supplement.py", "seed": 42, "order": "shuffled with Random(42)",
                    "note": "sensitivity supplement written after the main experiments (spec 13.1-13.2 factors not run before)"}}
    cohort.write(out, m)
    print(len(assignments), "assignments")


if __name__ == "__main__":
    main()
