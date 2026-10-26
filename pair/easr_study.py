"""Controller-only study for the EASR novelty gate (paper, Appendix A.5): a scripted, model-free agent performs one dependent edit per operation on
the real applications while another party writes at controlled moments; every browser-only controller sees the same pages and
events. Development pool only (no frozen held-out family).

Scripted agent: subgoal 1 opens the task's list page; subgoal 2 reads the target's own page, decides the value with the operation's
rule from the latest values it has seen, opens the edit page, fills the one intended field and saves. On a hold it redoes subgoal 2
(local) or both subgoals (a FULL_REPLAN hold); after a post-commit notice it re-checks subgoal 2 once more. Decisions (one per subgoal
execution) stand in for model calls.

Usage: python -m pair.easr_study run OUT_DIR SLOT [--ops ..] [--controllers ..] [--classes ..]
       python -m pair.easr_study plan            (print the cell list)
"""
from __future__ import annotations

import argparse, asyncio, json, pathlib, re, sqlite3, sys, time, traceback

from playwright.async_api import async_playwright

from . import controllers, tasks
from .apps.crm import CrmAdapter, CrmInstance
from .apps.mantis import MantisAdapter, MantisInstance
from .apps.oscar import OscarAdapter, OscarInstance, FIXTURE as SHOP
from .apps.breakingweb import BwAdapter, BwSession, SAVE_LABEL

TITLE = {p["upc"]: p["title"] for p in SHOP["products"]}
CRM_D = "/index.php?module={m}&action=DetailView&record={r}"
CRM_E = "/index.php?module={m}&action=EditView&record={r}"


def norm(v) -> str:
    s = " ".join(str("" if v is None else v).split()).lower()
    s = re.sub(r"^\$", "", s)
    s = re.sub(r"[\s()\-]", "", s) if re.fullmatch(r"[+\d\s()\-]{6,}", s) else s
    try:
        return f"{float(s.replace(',', '')):.2f}"
    except ValueError:
        return s


def money(v) -> float:
    return float(re.sub(r"[^\d.]", "", str(v)) or 0)


# ---- operations ---------------------------------------------------------------------------------------------------------------------
# keys: "obj.field" go through the adapter's evaluator-side read/write; "sql:table.col" are rows with id=1 in the shop database.
OPS = {
    "S1_basket_line": dict(
        suite="shop", start="/catalogue/", detail=("product", "PAIR-A"), edit="/basket/", save="button:has-text('Update')",
        intended="basket_line:PAIR-A.quantity", fill=("basket_line", TITLE["PAIR-A"]),
        dep="product:PAIR-A.price", dep_fact=("product:PAIR-A", "price"), rule=lambda d: "2" if money(d) <= 90 else "1",
        dep_new="110.00", untouched=("basket_line:PAIR-CABLE.quantity", 2), same=3, build={"initial_lines": [["PAIR-CABLE", 1], ["PAIR-A", 1]]}),
    "S2_address_phone": dict(
        suite="shop", start="/accounts/addresses/", detail=None, edit="/accounts/addresses/1/", save="form:has(input[name=phone_number]) button[type=submit]",
        intended="sql:address_useraddress.phone_number", fill=("input", "phone_number"), dep=None, rule=lambda d: "+44 20 7946 0958",
        untouched=("sql:address_useraddress.line2", "Flat 2"), same="+44 20 7946 0000", build={},
        # the fixture address pairs a UK postcode with the form's only country (United States), so every save fails validation
        # before any change; the study world gives it a valid US ZIP first
        setup=[("sql:address_useraddress.postcode", "10001")]),
    "S3_profile_name": dict(
        suite="shop", start="/accounts/profile/", detail=None, edit="/accounts/profile/edit/", save="#profile_form button[type=submit]",
        intended="sql:auth_user.first_name", fill=("input", "first_name"), dep=None, rule=lambda d: "Patricia",
        untouched=("sql:auth_user.last_name", "Shopper-Lee"), same="Patty", build={}),
    "C1_opportunity_stage": dict(
        suite="crm", start="/index.php?module=Opportunities&action=index", detail=CRM_D.format(m="Opportunities", r="pair-opp-renewal"),
        edit=CRM_E.format(m="Opportunities", r="pair-opp-renewal"), save="#SAVE",
        intended="opportunity:renewal.sales_stage", fill=("select", "sales_stage"),
        dep="opportunity:renewal.amount", dep_fact=("opportunity:renewal", "amount"),
        rule=lambda d: "Proposal/Price Quote" if money(d) >= 10000 else "Qualification", dep_new="15000",
        untouched=("opportunity:renewal.description", "Renewal call moved to Friday."), same="Needs Analysis", build={}),
    "C2_contact_phone": dict(
        suite="crm", start="/index.php?module=Contacts&action=index", detail=CRM_D.format(m="Contacts", r="pair-ct-phone"),
        edit=CRM_E.format(m="Contacts", r="pair-ct-phone"), save="#SAVE",
        intended="contact:phone_contact.phone_work", fill=("input", "phone_work"),
        dep="contact:phone_contact.title", dep_fact=("contact:phone_contact", "title"),
        rule=lambda d: "+1-555-0142" if norm(d) == "procurement lead" else "+1-555-0143", dep_new="Account Manager",
        untouched=("contact:phone_contact.department", "Procurement"), same="+1-555-0177", build={}),
    "C3_contact_title": dict(
        suite="crm", start="/index.php?module=Contacts&action=index", detail=CRM_D.format(m="Contacts", r="pair-ct-sentinel"),
        edit=CRM_E.format(m="Contacts", r="pair-ct-sentinel"), save="#SAVE",
        intended="contact:sentinel.title", fill=("input", "title"),
        dep="contact:sentinel.account_name", dep_fact=("contact:sentinel", "account_name"),
        rule=lambda d: "Senior Account Manager" if norm(d) == "pair primary account" else "Account Manager (Other)", dep_new="PAIR Other Account",
        untouched=("contact:sentinel.phone_work", "+1-555-0399"), same="Regional Manager", build={}),
    "M1_issue_priority": dict(
        suite="mantis", start="/view_all_bug_page.php", detail="/view.php?id=1", edit="/bug_update_page.php?bug_id=1",
        save="input[type=submit][value='Update Information']", intended="issue:vpn.priority", fill=("select_label", "priority"),
        dep="issue:vpn.contract_tier", dep_fact=("issue:vpn", "contract_tier"), rule=lambda d: "urgent" if norm(d) == "gold" else "high",
        dep_new="Silver", untouched=("issue:vpn.status", "feedback"), same="low", build={}),
    "M2_issue_summary": dict(
        suite="mantis", start="/view_all_bug_page.php", detail="/view.php?id=2", edit="/bug_update_page.php?bug_id=2",
        save="input[type=submit][value='Update Information']", intended="issue:vat.summary", fill=("input", "summary"),
        dep="issue:vat.customer_account", dep_fact=("issue:vat", "customer_account"),
        rule=lambda d: f"Invoice 4471 applies VAT to exempt customer {d}", dep_new="Contoso Ltd",
        untouched=("issue:vat.contract_tier", "Gold"), same="Invoice 4471 VAT under review", build={}),
    "M3_issue_handler": dict(
        suite="mantis", start="/view_all_bug_page.php", detail="/view.php?id=5", edit="/bug_update_page.php?bug_id=5",
        save="input[type=submit][value='Update Information']", intended="issue:printer.handler", fill=("select_contains", "handler_id"),
        dep="issue:printer.category", dep_fact=("issue:printer", "category"), rule=lambda d: "pairagent" if norm(d) == "hardware" else "jamie.ops",
        dep_new="General", untouched=("issue:printer.priority", "high"), same="administrator", build={},
        read_map={"Pat Agent": "pairagent", "Jamie Ops": "jamie.ops", "Riley Customer": "riley.customer"}),   # stored handler reads back as the real name
}


def bw_op(env, ctl, field, value, untouched, same):
    return dict(suite="bw", env=env, start="/", detail=None, edit="/settings", save=f"button:has-text('{SAVE_LABEL[env]}')",
                intended=f"settings:{env}.{field}", fill=ctl, dep=None, rule=lambda d, v=value: v, untouched=(f"settings:{env}.{untouched[0]}", untouched[1]),
                same=same, build={})


OPS.update({
    "BW1_gmail_page_size": bw_op("gmail", ("label_select", "Maximum page size"), "max_page_size", "25", ("signature", "Avery Quinn\nOperations Lead, EMEA"), 100),
    "BW2_gmail_undo_send": bw_op("gmail", ("label_select", "Undo send seconds"), "undo_send_seconds", "20", ("display_density", "compact"), 30),
    "BW3_booking_language": bw_op("booking", ("css_select", "#settings-language"), "language", "French", ("currency", "EUR"), "German"),
    "BW4_booking_currency": bw_op("booking", ("css_select", "#settings-currency"), "currency", "GBP", ("newsletter", True), "JPY"),
    "BW5_amazon_language": bw_op("amazon", ("css_select", "#settings-language"), "language", "German", ("one_click_enabled", True), "Spanish"),
    "BW7_gmail_language": bw_op("gmail", ("label_select", "Gmail display language"), "language", "French", ("vacation_responder_message", "Out until Monday."), "German"),
})
# Not used: the amazon currency input does not accept typing (fill times out); reddit settings save each control on change with a one-key PUT (the "Save all settings" click sends nothing), so there
# is no multi-field submission and no click-time commit to control; robinhood has no settings page.

CLASSES = {   # class -> [(event, timing)]; timings: "pre" after the edit page loaded, "race" after the controller's check passed
    "A_clean": [], "A_convergent": [("conv", "pre")], "B_dependency": [("dep", "pre")], "C_untouched": [("untouched", "pre")],
    "C_same_field": [("same", "pre")], "D_both": [("dep", "pre"), ("untouched", "pre")], "B_race": [("dep", "race")],
    "C_race": [("untouched", "race")],
}
NO_DEP_CLASSES = ("A_clean", "A_convergent", "C_untouched", "C_same_field", "C_race")
STUDY_CONTROLLERS = ["B0_vanilla", "B2_preaction_verify", "B3_refresh_retry", "B4_postcondition", "B5_dependency_only", "B6_full_replan",
                     "B7_repair_v1", "GF_entity_freshness", "EASR_v2"] + list(controllers.ABLATIONS)


def cells(ops=None, ctrls=None, classes=None):
    out = []
    for op in ops or OPS:
        for cls in classes or CLASSES:
            if OPS[op]["dep"] is None and cls not in NO_DEP_CLASSES:
                continue
            for c in ctrls or STUDY_CONTROLLERS:
                out.append((op, cls, c))
    return out


# ---- coverage shared by every controller (to be folded into the adapters) -------------------------------------------------------------
# Coverage and EASR flags now live in the adapters (pair/apps/*); the study names are kept for the recorded rows.
StudyOscar, StudyCrm, StudyMantis = OscarAdapter, CrmAdapter, MantisAdapter


def build_spec(op: dict, name: str) -> dict:
    spec = tasks.load("dev_v0")
    if op["suite"] == "shop":
        w = tasks.resolve_world(spec, tasks.template(spec, "shop_select"), "base")
        w.update(op["build"]); w["world"] = name
        return w
    if op["suite"] == "crm":
        w = tasks.resolve_world(spec, tasks.template(spec, "crm_stage"), "base")
        w.update(native_occ="off", world=name)
        return w
    return {"world": name, "native_occ": "off"}


def make(suite, slot, op=None):
    if suite == "bw":
        return BwSession(op["env"]), BwAdapter
    I, A = {"shop": (OscarInstance, StudyOscar), "crm": (CrmInstance, StudyCrm), "mantis": (MantisInstance, StudyMantis)}[suite]
    return I(slot), A


def write(inst, ad, key, value):
    if key.startswith("sql:"):
        table, col = key[4:].split(".")
        with sqlite3.connect(str(inst.db), timeout=20) as c:
            c.execute(f"update {table} set {col}=? where id=1", (value,))
        return
    obj, field = key.rsplit(".", 1)
    ad.write_value(obj, field, value)


def read_op(inst, ad, op, key):
    v = read(inst, ad, key)
    return op.get("read_map", {}).get(str(v), v) if key == op["intended"] else v


def read(inst, ad, key):
    if key.startswith("sql:"):
        table, col = key[4:].split(".")
        with sqlite3.connect(f"file:{inst.db}?mode=ro", uri=True) as c:
            return c.execute(f"select {col} from {table} where id=1").fetchone()[0]
    obj, field = key.rsplit(".", 1)
    return ad.read_value(obj, field)


FILL_LINE_JS = r"""({title, value}) => {
  for (const q of document.querySelectorAll("input[name$='-quantity']")) {
    let n = q.parentElement;
    while (n && n.querySelectorAll("input[name$='-quantity']").length === 1 && !n.innerText.includes(title)) n = n.parentElement;
    if (n && n.querySelectorAll("input[name$='-quantity']").length === 1 && n.innerText.includes(title)) {
      q.value = value; q.dispatchEvent(new Event('input', {bubbles: true})); q.dispatchEvent(new Event('change', {bubbles: true}));
      return {name: q.name, load: q.defaultValue};
    }
  }
  return null;
}"""
CURRENT_JS = r"""({kind, name, title}) => {
  if (kind === 'basket_line') {
    for (const q of document.querySelectorAll("input[name$='-quantity']")) {
      let n = q.parentElement;
      while (n && n.querySelectorAll("input[name$='-quantity']").length === 1 && !n.innerText.includes(title)) n = n.parentElement;
      if (n && n.querySelectorAll("input[name$='-quantity']").length === 1 && n.innerText.includes(title)) return q.defaultValue;
    }
    return null;
  }
  const e = document.querySelector(`[name="${name}"]`);
  if (!e) return null;
  if (e.tagName === 'SELECT') { const o = [...e.options].find(o => o.defaultSelected) || e.options[0]; return kind === 'select' ? o.value : o.text; }
  return e.defaultValue;
}"""


async def fill(page, how, value):
    kind, name = how
    if kind == "basket_line":
        return await page.evaluate(FILL_LINE_JS, {"title": name, "value": str(value)})
    if kind == "label_select":
        await page.get_by_label(name, exact=True).select_option(str(value))
        return {"name": name}
    if kind == "css_select":
        await page.locator(name).first.select_option(str(value))
        return {"name": name}
    if kind == "css_input":
        await page.locator(name).first.fill(str(value))
        return {"name": name}
    loc = page.locator(f"[name='{name}']").first
    if kind == "input":
        await loc.fill(str(value))
    elif kind == "select":
        await loc.select_option(value=str(value))
    elif kind == "select_label":
        await loc.select_option(label=str(value))
    elif kind == "select_contains":
        opts = await loc.evaluate("e => [...e.options].map(o => [o.value, o.text])")
        v = next(v for v, t in opts if str(value).lower() in t.lower() and v != "0")
        await loc.select_option(value=v)
    return {"name": name}


async def current_form_value(page, how):
    kind, name = how
    if kind == "label_select":
        return await page.get_by_label(name, exact=True).input_value()
    if kind in ("css_select", "css_input"):
        return await page.locator(name).first.input_value()
    return await page.evaluate(CURRENT_JS, {"kind": kind, "name": name, "title": name})


def same_value(how, form_value, decided) -> bool:
    if form_value is None:
        return False
    if how[0] == "select_contains":
        return str(decided).lower() in str(form_value).lower()
    return norm(form_value) == norm(decided)


async def product_url(page, origin, upc):
    await page.goto(origin + "/catalogue/", wait_until="domcontentloaded")
    href = await page.evaluate("(t) => { const a = [...document.querySelectorAll('a')].find(a => a.innerText.trim() === t); return a ? a.href : null; }", TITLE[upc])
    return href


async def run_cell(browser, op_id, cls, ctrl, slot, out_dir: pathlib.Path):
    op = OPS[op_id]
    inst, A = make(op["suite"], slot, op)
    name = f"easr_{op_id}_{cls}_{ctrl}"
    inst.build(build_spec(op, name))
    inst.start()
    t0 = time.time()
    rec = {"op": op_id, "suite": op["suite"], "class": cls, "controller": ctrl, "slot": slot}
    guard = ctx = None
    try:
        ad = A(inst)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        await ctx.route("**/*", ad.route_handler)
        await ad.prepare_context(ctx)
        bw = op["suite"] == "bw"
        start_url = inst.url(op["start"]) if bw else inst.origin + op["start"]
        edit_url = inst.url(op["edit"]) if bw else inst.origin + op["edit"]
        guard = controllers.make(ctrl, ad, [op["dep"]] if op["dep"] else [], start_url=start_url)
        if guard is not None:
            await guard.setup(browser)
            if hasattr(guard, "attach"):
                await guard.attach(ctx)
        page = await ctx.new_page()
        detail = op["detail"]
        if isinstance(detail, tuple):
            detail = await product_url(page, inst.origin, detail[1])
        elif detail:
            detail = inst.origin + detail
        for key, val in op.get("setup", []):
            write(inst, ad, key, val)
        initial = {"intended": read_op(inst, ad, op, op["intended"]), "untouched": read(inst, ad, op["untouched"][0]),
                   "dep": read(inst, ad, op["dep"]) if op["dep"] else None}
        v0 = op["rule"](initial["dep"])
        values = {"dep": (op["dep"], op.get("dep_new")), "untouched": op["untouched"], "same": (op["intended"], op["same"]),
                  "conv": (op["intended"], v0)}
        pending = list(CLASSES[cls])
        fired, events = set(), []

        def fire(timing):
            for ev, tm in list(pending):
                if tm == timing:
                    key, val = values[ev]
                    write(inst, ad, key, val)
                    pending.remove((ev, tm))
                    fired.add(ev)
                    events.append({"event": ev, "key": key, "value": val, "timing": timing, "t": round(time.time() - t0, 2)})

        m = {"decisions": 0, "page_loads": 0, "holds": 0, "full_replans": 0, "notices": 0, "commits": 0, "guard_reads": 0,
             "stale_commits": 0, "failed_saves": 0, "blind_overwrites": 0}
        guard_log, step, seen = [], 0, {}
        conflict_seen = False
        phase = "start"                    # start -> target -> done
        recheck_left = 1
        for _ in range(8):
            if phase == "start":
                m["decisions"] += 1
                await page.goto(start_url, wait_until="domcontentloaded"); m["page_loads"] += 1; step += 1
                if guard is not None:
                    guard.observe(step, await ad.public_facts(page, None), {"url": page.url, "controls": []})
                phase = "target"
            # subgoal 2: read the target, decide, edit, save
            m["decisions"] += 1
            if detail:
                await page.goto(detail, wait_until="domcontentloaded"); m["page_loads"] += 1; step += 1
                facts = await ad.public_facts(page, None)
                if guard is not None:
                    guard.observe(step, facts, {"url": page.url, "controls": []})
                for f in facts:
                    seen[(f["object"], f["field"])] = f["value"]
            await page.goto(edit_url, wait_until="networkidle" if bw else "domcontentloaded"); m["page_loads"] += 1; step += 1
            if op["suite"] == "crm":
                await page.wait_for_selector("form#EditView input[name=record]", state="attached", timeout=20000)
            facts = await ad.public_facts(page, None)
            if guard is not None:
                guard.observe(step, facts, {"url": page.url, "controls": []})
            for f in facts:
                seen[(f["object"], f["field"])] = f["value"]
            dep_seen = seen.get(op["dep_fact"]) if op["dep"] else None
            decided = op["rule"](dep_seen)
            cur = await current_form_value(page, op["fill"])
            if "same" in fired and same_value(op["fill"], cur, op["same"]):
                conflict_seen = True
            fire("pre")
            if same_value(op["fill"], cur, decided):
                phase = "done"                  # nothing to change (already as decided)
                break
            await fill(page, op["fill"], decided)
            save = page.locator(op["save"]).first
            await save.evaluate("e => e.setAttribute('data-pair-id', 'pair-save')")
            ctl = await save.evaluate("""e => ({id: 'pair-save', label: (e.innerText || e.value || '').trim(), value: e.value || '',
                                                name: e.name || '', form_action: e.form ? e.form.action : ''})""")
            act, obs = {"type": "click", "id": "pair-save"}, {"url": page.url, "controls": [ctl]}
            d = None
            if guard is not None:
                d = await guard.check(act, obs, page, ctx)
                m["guard_reads"] += int(d.get("reads") or 0)
                guard_log.append({k: d.get(k) for k in ("decision", "easr", "conflict_class", "changed", "withheld_fields", "rewritten_fields",
                                                         "interference", "structure_changed", "note", "error")})
                if d["decision"] == "hold":
                    m["holds"] += 1
                    if d.get("easr") == "FULL_REPLAN":
                        m["full_replans"] += 1
                        phase = "start"
                    continue
            fire("race")
            dep_now = read(inst, ad, op["dep"]) if op["dep"] else None
            stale = bool(op["dep"] and norm(op["rule"](dep_now)) != norm(decided))
            blind = bool("same" in fired and not conflict_seen and not same_value(op["fill"], read_op(inst, ad, op, op["intended"]), decided))
            try:
                async with page.expect_response(lambda r: r.request.method in ("POST", "PUT", "PATCH"), timeout=30000):
                    await save.click(force=bw)
                await page.wait_for_load_state("domcontentloaded")
            except Exception as exc:
                rec.setdefault("click_errors", []).append(repr(exc)[:200])
            await asyncio.sleep(0.7)
            pc = None
            if d is not None and d["decision"] == "pass" and hasattr(guard, "post_commit"):
                pc = await guard.post_commit()
                if pc:
                    m["guard_reads"] += int(pc.get("reads") or 0)
                    guard_log.append({"post_commit": pc.get("findings") or pc.get("changed"), "notice": bool(pc.get("notice")),
                                      "easr": pc.get("easr"), "request_hold": pc.get("request_hold"), "interference": pc.get("interference")})
            if pc and pc.get("request_hold"):            # the request was answered by the controller (409): nothing was written
                m["holds"] += 1
                if pc.get("easr") == "FULL_REPLAN":
                    m["full_replans"] += 1
                    phase = "start"
                continue
            m["stale_commits"] += stale
            m["blind_overwrites"] += blind
            m["commits"] += 1
            after = read_op(inst, ad, op, op["intended"])
            if not same_value(("select_contains",) if op["fill"][0] == "select_contains" else ("x",), after, decided):
                m["failed_saves"] += 1
            if pc and pc.get("notice") and recheck_left > 0:
                m["notices"] += 1
                recheck_left -= 1
                continue
            phase = "done"
            break
        final = {"intended": read_op(inst, ad, op, op["intended"]), "untouched": read(inst, ad, op["untouched"][0]),
                 "dep": read(inst, ad, op["dep"]) if op["dep"] else None}
        world_dep = op.get("dep_new") if "dep" in fired else initial["dep"]          # the true current fact (another party's write)
        want_intended = op["rule"](world_dep)
        want_untouched = op["untouched"][1] if "untouched" in fired else initial["untouched"]
        how = ("select_contains",) if op["fill"][0] == "select_contains" else ("x",)
        intended_ok = same_value(how, final["intended"], want_intended)
        # every value another party wrote outside the intended field, and the untouched field, must survive the agent's saves
        preserved = norm(final["untouched"]) == norm(want_untouched) and (not op["dep"] or norm(final["dep"]) == norm(world_dep))
        rec.update(initial=initial, final=final, events=events, metrics=m, guard=guard_log, finished=phase == "done",
                   want={"intended": want_intended, "untouched": want_untouched}, intended_ok=intended_ok, preserved=preserved,
                   lost_update=not preserved, correct_and_safe=bool(intended_ok and preserved and m["blind_overwrites"] == 0 and phase == "done"),
                   seconds=round(time.time() - t0, 1))
    except Exception as exc:
        rec.update(error=repr(exc)[:400], trace=traceback.format_exc()[-1500:])
    finally:
        for c in ([guard.close] if guard is not None else []) + ([ctx.close] if ctx is not None else []):
            try:
                await c()
            except Exception:
                pass
        inst.stop()
    with open(out_dir / f"rows_slot{slot}.jsonl", "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


async def run(out_dir, slot, todo):
    out_dir.mkdir(parents=True, exist_ok=True)
    done = set()
    for p in out_dir.glob("rows_slot*.jsonl"):
        for line in p.read_text().splitlines():
            r = json.loads(line)
            if "error" not in r:
                done.add((r["op"], r["class"], r["controller"]))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for cell in todo:
            if cell in done:
                continue
            r = await run_cell(browser, *cell, slot, out_dir)
            print(json.dumps({k: r.get(k) for k in ("op", "class", "controller", "intended_ok", "preserved", "correct_and_safe", "seconds", "error")}
                             | {"m": r.get("metrics")}), flush=True)
        await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "plan"])
    ap.add_argument("out", nargs="?")
    ap.add_argument("slot", nargs="?", type=int, default=40)
    ap.add_argument("--ops", nargs="*")
    ap.add_argument("--controllers", nargs="*")
    ap.add_argument("--classes", nargs="*")
    ap.add_argument("--shard", default="0/1")
    a = ap.parse_args()
    todo = cells(a.ops, a.controllers, a.classes)
    k, n = map(int, a.shard.split("/"))
    todo = [c for i, c in enumerate(todo) if i % n == k]
    if a.cmd == "plan":
        print(len(todo)); return
    asyncio.run(run(pathlib.Path(a.out), a.slot, todo))


if __name__ == "__main__":
    main()
