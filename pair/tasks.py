"""Load task specifications and resolve one world into an application build spec."""
from __future__ import annotations

import copy
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str = "dev_v0") -> dict:
    return json.loads((ROOT / "tasks" / f"{name}.json").read_text())


def template(spec: dict, name: str) -> dict:
    return next(t for t in spec["static_templates"] if t["template"] == name)


def resolve_world(spec: dict, tmpl: dict, world: str) -> dict:
    """Follow same_as links, then overlay the world's own fields; notices become text."""
    entry = tmpl["worlds"][world]
    base = resolve_world(spec, tmpl, entry["same_as"]) if "same_as" in entry else {}
    out = copy.deepcopy(base)
    for key, value in entry.items():
        if key != "same_as":
            out[key] = copy.deepcopy(value)
    if "notice" in entry:
        out["notice"] = spec["notices"][entry["notice"]]
    if "notice_text" in entry:
        out["notice"] = entry["notice_text"]
        out.pop("notice_text", None)
    out["world"] = world
    return out


def expected(spec: dict, tmpl: dict, world: str) -> dict:
    group = "base" if world in spec["world_groups"]["base_like"] else "causal"
    return tmpl["expected"][group]


def intervention_text(spec: dict, tmpl: dict, world: str) -> str:
    text = spec["interventions"][world]
    extra = {"causal": tmpl.get("causal_text"), "conflict": tmpl.get("conflict_text"),
             "distractor": tmpl.get("distractor_text")}.get(world)
    return text + (" " + extra if extra else "")
