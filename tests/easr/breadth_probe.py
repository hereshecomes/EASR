"""Model-free check of the write-breadth manipulation (pair/easr_supplement.py) on MantisBT: per level, the fields the update form
carries, and whether a plain full-form save (no controller) overwrites a concurrent change to the operation's core untouched field and to
steps_to_reproduce. Expected: a concurrent change is overwritten exactly when its field is on the form."""
import asyncio, sys
from playwright.async_api import async_playwright
from pair import easr_eval as E, easr_supplement as S, worker

OPS = sys.argv[1].split(",") if len(sys.argv) > 1 else ["M1_issue_priority", "M5_issue_priority", "M2_issue_summary", "M3_issue_handler"]


async def one(b, op_id, level):
    a = E.assignment(op_id, "clean", "B0_vanilla", E.arms_spec(), 42)
    a["build_spec"]["update_fields"] = S.update_fields(level, [op_id])
    inst = worker.instance_for("mantis", 46, {}); inst.build(a["build_spec"]); inst.start()
    try:
        ad = worker.make_adapter("mantis", 46, a, {"mantis": inst})
        o = E.op(op_id); peri = E.op(op_id + "_peri")
        ctx = await b.new_context(); await ctx.route("**/*", ad.route_handler); await ad.prepare_context(ctx)
        page = await ctx.new_page()
        await page.goto(inst.origin + o["edit_path"], wait_until="domcontentloaded")
        form = page.locator("form[action*='bug_update.php']").first
        names = await form.evaluate("f => [...f.elements].filter(e => e.name && e.type !== 'hidden' && e.type !== 'submit').map(e => e.name)")
        core_key, core_val = o["untouched"]; peri_key, peri_val = peri["untouched"]
        ad.write_value(*core_key.rsplit(".", 1), core_val); ad.write_value(*peri_key.rsplit(".", 1), peri_val)
        tobj, tfield = o["target"].rsplit(".", 1)
        el = form.locator(f"[name='{ {'handler': 'handler_id', 'category': 'category_id'}.get(tfield, tfield) }']").first
        if await el.count():
            if await el.evaluate("e => e.tagName") == "SELECT":
                opts = await el.evaluate("e => [...e.options].map(o => o.value).filter(v => v && v !== e.value)")
                await el.select_option(opts[0])
            else:
                await el.fill("Changed by probe " + level)
        async with page.expect_navigation():
            await form.locator("[type=submit]").first.click()
        core_now, peri_now = ad.read_value(*core_key.rsplit(".", 1)), ad.read_value(*peri_key.rsplit(".", 1))
        same = lambda x, y: " ".join(str(x).split()).lower() == " ".join(str(y).split()).lower()
        print(f"{op_id:20s} {level:9s} fields={len(names):2d} core({core_key.rsplit('.',1)[1]})={'kept' if same(core_now, core_val) else 'OVERWRITTEN'} "
              f"peripheral={'kept' if same(peri_now, peri_val) else 'OVERWRITTEN'} | {','.join(names)[:150]}", flush=True)
        await ctx.close()
    finally:
        inst.stop()


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        for op_id in OPS:
            for level in ("narrow", "moderate", "broad"):
                await one(b, op_id, level)
        await b.close()
asyncio.run(main())
