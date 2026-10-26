"""Common controller registry for the EASR study: browser-only baselines B0-B7, EASR v2 and its ablations.
B0/B1 differ only in the agent prompt (policy raw / verify) and have no controller."""
from __future__ import annotations

from .easr import EASR
import json

from .guards import GuardV2

LARGEST_FORM_VALUES_JS = r"""() => {
  let best = null, n = -1;
  for (const f of document.querySelectorAll('form')) { const k = f.querySelectorAll('[name]').length; if (k > n) { best = f; n = k; } }
  const out = {};
  if (!best) return out;
  for (const e of best.querySelectorAll('input[name],textarea[name],select[name]')) {
    if (['button', 'submit', 'image', 'reset', 'hidden', 'file'].includes(e.type)) continue;
    if (e.tagName === 'SELECT') { const o = [...e.options].find(o => o.defaultSelected); out[e.name] = o ? o.value : (e.options[0] ? e.options[0].value : ''); }
    else if (e.type === 'checkbox' || e.type === 'radio') out[e.name] = e.defaultChecked ? '1' : '0';
    else out[e.name] = e.defaultValue;
  }
  return out;
}"""


class GuardV2FormFix(GuardV2):
    """Repair v1 with one implementation defect fixed (informational comparator, not pre-registered): v1 reads the fresh copy of the
    edit form from `form#EditView` or the page's first form, which on MantisBT is the navigation bar's jump form, so its same-field
    conflict check saw nothing there. This variant reads the largest form instead; nothing else changes."""

    async def _fetch_form(self, url: str) -> dict:
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            wait = getattr(self.adapter, "EASR_FORM_WAIT", None)
            if wait:
                await page.wait_for_selector(wait, state="attached", timeout=15000)
            return await page.evaluate(LARGEST_FORM_VALUES_JS) or {}
        finally:
            await page.close()

BROWSER = {
    "B0_vanilla":            {"policy": "raw", "controller": None},
    "B1_prompted_verify":    {"policy": "verify", "controller": None},
    "B2_preaction_verify":   {"policy": "verify", "controller": "guardv2", "mode": "record"},
    "B3_refresh_retry":      {"policy": "verify", "controller": "guardv2", "mode": "refresh"},
    "B4_postcondition":      {"policy": "verify", "controller": "easr", "variant": {"verify_only": True}},
    "B5_dependency_only":    {"policy": "verify", "controller": "guardv2", "mode": "dependency"},
    "B6_full_replan":        {"policy": "verify", "controller": "easr", "variant": {"full_replan": True, "no_rebase": True}, "verify": False},
    "B7_repair_v1":          {"policy": "verify", "controller": "guardv2", "mode": "footprint", "post_check": True},
    "B7f_repair_v1_formfix": {"policy": "verify", "controller": "guardv2_formfix", "mode": "footprint", "post_check": True},
    "GF_entity_freshness":   {"policy": "verify", "controller": "easr", "variant": {"entity_freshness": True, "no_rebase": True}, "verify": False},
    "EASR_v2":               {"policy": "verify", "controller": "easr", "variant": {}},
    # v2.1.1 (declared 2026-10-21 after the fault suite): the post-commit check on the SPA save path waits for the request handler
    "EASR_v2_1_1":           {"policy": "verify", "controller": "easr", "variant": {"await_route": True}},
    "TB_integrated":         {"policy": "verify", "controller": "condition_attach"},
}
ABLATIONS = {
    "A1_no_dependencies":    {"no_dependencies": True},
    "A2_intended_only":      {"intended_only": True},
    "A3_dom_only":           {"dom_only": True},
    "A4_no_rebase":          {"no_rebase": True},
    "A5_syntactic":          {"syntactic": True},
    "A6_no_verify":          {"no_verify": True},
    "A7_verify_on_signal":   {"verify_on_signal": True},
    "A8_rewrite_only":       {"rewrite_only": True},
    # names of the specification's ablation table (section 9.1); A0 is EASR_v2 itself. The controller study (CP6) used the names above:
    # its A4_no_rebase = spec A5, A5_syntactic = spec A4, A6_no_verify = spec A6, A7_verify_on_signal = spec A7, A8_rewrite_only = extra.
    "A4_syntactic_overlap":      {"syntactic": True},
    "A5_hold_instead_of_rebase": {"no_rebase": True},
    "A6_no_postcommit":          {"no_verify": True},
    "A7_postcommit_on_signal":   {"verify_on_signal": True},
    "A8_full_replanning":        {"full_replan_holds": True},
}
class ConditionAttach(GuardV2):
    """Client half of the application-integrated tier: attach the dependency values the agent saw (pair_condition) to its own form; the
    application enforces them, together with its field-level compare-and-set, inside the request that writes. No withholding, no hold."""

    async def check(self, act, obs, page, context):
        t = await self.adapter.commit_target(page, act, obs)
        if t is None:
            return {"decision": "not_covered", "commit_like": self.adapter.commit_like(act, obs)}
        conds = []
        for o, fld in self.deps:
            seen = self.last_seen.get(o, {}).get(fld)
            ref = self.adapter.condition_ref(o, fld, self.urls.get(o))
            if seen is not None and ref is not None:
                conds.append({**ref, "value": seen[0], "role": "dependency"})
        if t.get("form") or t.get("kind") == "basket":
            await page.evaluate(self.adapter.ATTACH_JS, {"id": str(act["id"]), "value": json.dumps(conds)})
        return {"decision": "pass", "object": t["object"], "reads": 0, "conditional": True, "conditions": conds}

    async def post_commit(self):
        return None


SPEC_ABLATIONS = ["A1_no_dependencies", "A2_intended_only", "A3_dom_only", "A4_syntactic_overlap", "A5_hold_instead_of_rebase",
                  "A6_no_postcommit", "A7_postcommit_on_signal", "A8_full_replanning", "A9_global_freshness"]


CONTRACT_VARIANTS = {
    "CQ_omit10": {"omit_frac": 0.10}, "CQ_omit25": {"omit_frac": 0.25}, "CQ_omit50": {"omit_frac": 0.50},
    "CQ_omit_targeted": {"omit_targeted": True},
}                                                       # CQ_extra_dep / CQ_missing_dep change the assignment's dependency list instead


def make(name: str, adapter, dependencies: list[str], start_url: str | None = None, budget: int = 3, task: dict | None = None):
    if name in CONTRACT_VARIANTS:
        return EASR(adapter, dependencies, budget=budget, variant=CONTRACT_VARIANTS[name], start_url=start_url, name=name, task=task)
    if name == "A9_global_freshness":
        name = "GF_entity_freshness"
    if name in ABLATIONS:
        return EASR(adapter, dependencies, budget=budget, variant=ABLATIONS[name], start_url=start_url, name=name)
    cfg = BROWSER[name]
    if cfg["controller"] is None:
        return None
    if cfg["controller"] == "condition_attach":
        return ConditionAttach(adapter, "dependency", dependencies, post_check=False, max_holds_per_object=budget)
    if cfg["controller"] == "guardv2_formfix":
        return GuardV2FormFix(adapter, cfg["mode"], dependencies, post_check=bool(cfg.get("post_check")), max_holds_per_object=budget)
    if cfg["controller"] == "guardv2":
        return GuardV2(adapter, cfg["mode"], dependencies, post_check=bool(cfg.get("post_check")), max_holds_per_object=budget)
    return EASR(adapter, dependencies, budget=budget, verify=cfg.get("verify", True), variant=cfg.get("variant"), start_url=start_url, name=name)
