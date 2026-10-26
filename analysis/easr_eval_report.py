"""Report for the model-based EASR evaluation (paper Section 4.1; estimator fixed before the main matrix).
Usage: python analysis/easr_eval_report.py OUT_PREFIX COHORT [COHORT ...]   (each cohort evaluated with `python -m pair.easr_eval evaluate`)
Primary: correct-and-safe, EASR minus each baseline, per agent and pooled; mean over operations of per-operation task means; bootstrap
over operations (5,000, seed 7). Secondary tables per suite, class, development vs new operations.
"""
import collections, json, pathlib, random, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEV_OPS = {"S1_basket_line", "S2_address_phone", "S3_profile_name", "C1_opportunity_stage", "C2_contact_phone", "C3_contact_title",
           "M1_issue_priority", "M2_issue_summary", "M3_issue_handler", "BW1_gmail_page_size", "BW2_gmail_undo_send",
           "BW3_booking_language", "BW4_booking_currency", "BW5_amazon_language", "BW7_gmail_language"}
CD = ("C_untouched", "C_same_field", "C_race", "D_both")
REPS, SEED = 5000, 7
BASELINES = ["B0_vanilla", "B1_prompted_verify", "B2_preaction_verify", "B3_refresh_retry", "B4_postcondition", "B5_dependency_only",
             "B6_full_replan", "B7_repair_v1"]
SUITE = {"oscar": "D1 shop", "crm": "D2 crm", "breakingweb": "D3 breakingweb", "mantis": "D4 mantis"}


def load(cohorts):
    rows = []
    for c in cohorts:
        c = pathlib.Path(c)
        man = json.loads((c / "manifest.json").read_text())
        by_id = {a["episode_id"]: a for a in man["assignments"]}
        idx = {json.loads(l)["episode_id"]: json.loads(l) for l in (c / "index.jsonl").read_text().splitlines() if l.strip()}
        for l in (c / "easr_eval.jsonl").read_text().splitlines():
            r = json.loads(l)
            r["agent"], r["cohort"] = man["actor"], c.name
            if "error" in r:
                if idx.get(r["episode"], {}).get("status") != "infra_failure":
                    continue                      # still running (no episode record yet)
                a = by_id[r["episode"]]
                r.update(op=a["op"], app=a["app"], arm=a["arm"], infra=True, **{"class": a["class"]})
            rows.append(r)
        for eid, x in idx.items():           # assigned episodes that never produced an episode folder count as infra failures
            if not (c / "episodes" / eid / "episode.json").exists() and x.get("status") == "infra_failure" and \
                    not any(r.get("episode") == eid for r in rows):
                a = by_id[eid]
                rows.append({"episode": eid, "op": a["op"], "class": a["class"], "arm": a["arm"], "app": a["app"], "agent": man["actor"],
                             "infra": True, "cohort": c.name})
    return rows


def val(r, k):
    if r.get("infra"):
        return 0.0 if k in ("correct_and_safe",) else None
    v = r[k]
    return float(v if not isinstance(v, bool) else int(v))


def per_op(rows, arm, metric, classes=None, ops=None):
    out = collections.defaultdict(list)
    for r in rows:
        if r["arm"] != arm or (classes and r["class"] not in classes) or (ops and r["op"] not in ops):
            continue
        v = val(r, metric)
        if v is not None:
            out[r["op"]].append(v)
    return {o: sum(v) / len(v) for o, v in out.items()}


def paired(rows, a, b, metric, classes=None, ops=None):
    A, B = per_op(rows, a, metric, classes, ops), per_op(rows, b, metric, classes, ops)
    keys = sorted(set(A) & set(B))
    if not keys:
        return None
    d = {k: A[k] - B[k] for k in keys}
    rng = random.Random(SEED)
    boots = sorted(sum(d[rng.choice(keys)] for _ in keys) / len(keys) for _ in range(REPS))
    return {"diff": round(sum(d.values()) / len(d), 3), "ci95": [round(boots[int(.025 * REPS)], 3), round(boots[int(.975 * REPS) - 1], 3)], "ops": len(keys)}


def means(rows, arm, classes=None):
    rs = [r for r in rows if r["arm"] == arm and (not classes or r["class"] in classes)]
    ok = [r for r in rs if not r.get("infra")]
    m = lambda k: round(sum(val(r, k) for r in ok) / max(1, len(ok)), 3)
    return {"n": len(rs), "infra": len(rs) - len(ok), "correct_and_safe": round(sum(val(r, "correct_and_safe") for r in rs) / max(1, len(rs)), 3),
            "intended_ok": m("intended_ok"), "unsafe": m("unsafe"), "lost": round(sum(1 - val(r, "preserved") for r in ok) / max(1, len(ok)), 3),
            "blind": round(sum(r["blind"] > 0 for r in ok) / max(1, len(ok)), 3), "stale": round(sum(r["stale"] > 0 for r in ok) / max(1, len(ok)), 3),
            "holds": m("holds"), "unneeded_holds": m("unneeded_holds"), "model_calls": m("model_calls"), "ktok": round(m("tokens") / 1000, 1),
            "completed": round(sum(r.get("completion") == "completed" for r in ok) / max(1, len(ok)), 3)}


def main(out, cohorts):
    rows = load(cohorts)
    agents = sorted({r["agent"] for r in rows})
    arms = [a for a in BASELINES + ["EASR_v2"] if any(r["arm"] == a for r in rows)]
    res = {"cohorts": cohorts, "episodes": len(rows), "agents": agents, "per_agent": {}}
    L = [f"{len(rows)} episodes; agents {agents}", ""]
    for ag in agents + (["POOLED"] if len(agents) > 1 else []):
        rs = rows if ag == "POOLED" else [r for r in rows if r["agent"] == ag]
        if ag == "POOLED":   # pooled: average over agents within each op first (same estimator)
            rs = rows
        R = res["per_agent"][ag] = {"means": {a: means(rs, a) for a in arms}, "means_CD": {a: means(rs, a, CD) for a in arms},
                                    "means_clean": {a: means(rs, a, ("clean",)) for a in arms}, "primary": {}, "secondary": {}}
        L.append(f"== {ag} ==  arm: n infra c&s intended unsafe lost blind stale holds unneeded calls ktok completed")
        for a in arms:
            v = R["means"][a]
            L.append(f"  {a:20s} {v['n']:4d} {v['infra']:3d}  {v['correct_and_safe']:.3f} {v['intended_ok']:.3f} {v['unsafe']:.3f} {v['lost']:.3f} "
                     f"{v['blind']:.3f} {v['stale']:.3f}  {v['holds']:.2f} {v['unneeded_holds']:.2f} {v['model_calls']:.1f} {v['ktok']:.1f} {v['completed']:.3f}")
        if "EASR_v2" in arms:
            L.append("  primary: correct-and-safe, EASR minus baseline (all tasks) | clean tasks only | unsafe on C/D | dev ops | new ops")
            for b in arms:
                if b == "EASR_v2":
                    continue
                t = {"all": paired(rs, "EASR_v2", b, "correct_and_safe"), "clean": paired(rs, "EASR_v2", b, "correct_and_safe", ("clean",)),
                     "unsafe_CD": paired(rs, "EASR_v2", b, "unsafe", CD),
                     "dev_ops": paired(rs, "EASR_v2", b, "correct_and_safe", ops=DEV_OPS),
                     "new_ops": paired(rs, "EASR_v2", b, "correct_and_safe", ops={r["op"] for r in rs} - DEV_OPS),
                     "calls": paired(rs, "EASR_v2", b, "model_calls")}
                R["primary"][b] = t
                f = lambda x: "n/a" if not x else f"{x['diff']:+.3f}[{x['ci95'][0]:+.3f},{x['ci95'][1]:+.3f}]"
                verdict = ("better" if t["all"] and t["all"]["ci95"][0] > 0 else "worse" if t["all"] and t["all"]["ci95"][1] < 0 else "n.e.")
                L.append(f"    vs {b:20s} {f(t['all'])} {verdict:7s} | {f(t['clean'])} | {f(t['unsafe_CD'])} | {f(t['dev_ops'])} | {f(t['new_ops'])} | calls {f(t['calls'])}")
        L.append("  correct-and-safe by suite:")
        for app, name in SUITE.items():
            line = "    " + f"{name:16s}" + " ".join(f"{a.split('_')[0]}={means([r for r in rs if r['app'] == app], a)['correct_and_safe']:.2f}" for a in arms)
            L.append(line)
        L.append("  correct-and-safe by class:")
        for c in ["clean", "A_convergent", "B_dependency", "C_untouched", "C_same_field", "D_both", "B_race", "C_race"]:
            L.append("    " + f"{c:16s}" + " ".join(f"{a.split('_')[0]}={means(rs, a, (c,))['correct_and_safe']:.2f}" for a in arms))
        L.append("")
    pathlib.Path(out + ".json").write_text(json.dumps(res, indent=1))
    pathlib.Path(out + ".txt").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
