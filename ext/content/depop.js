// Thrift crosslister (WO32): Depop's create-listing form — photos, the description (Depop has no title: its first line
// is the title), then Depop's own Downshift comboboxes, whose ids come from data/depop_catalog.json: group-input
// (category), variants-input (size), brand-input, condition-input, colour-input, source-input, age-input, style-input,
// attributes.<name>-input; each one's menu is <id without -input>-menu. Every combobox is cleared before typing (its
// menu filters by the typed text) and the option whose text IS the catalog value is picked. The category menu lists a
// name once per department ("Pants" under Women, Men, Kids): the one under the item's department is taken, never a
// guess. Then the price, Depop Shipping (the USPS radio) and its package size (the shippingMethods-input menu, recorded
// 2026-10-06). Boost ("Promote your item … Pay an extra 12% fee") is never turned on. Selectors: selectors.json → depop.
(function () {
  const T = globalThis.Thrift;
  const S = () => T.selectors.depop.steps;
  const menuOf = (cid) => document.getElementById(S().combo.menu.replace("{id}", cid.replace(/-input$/, "")));
  const optionsOf = (menu) => (menu ? [...menu.querySelectorAll(S().combo.options)].filter(T.visible) : []);
  const textOf = (el) => el.textContent.replace(/\s+/g, " ").trim();

  // The option whose text IS the value; a category path may show as its parts in that order (match_option, WO30).
  T.matchOption = (texts, value, category = false) => {
    const want = T.norm(value);
    let i = texts.findIndex((t) => T.norm(t) === want);
    if (i >= 0 || !category) return i;
    const parts = String(value).split(" > ").map(T.norm);
    return texts.findIndex((t) => {
      let n = T.norm(t), pos = 0;
      for (const p of parts) {
        const j = n.indexOf(p, pos);
        if (j < 0) return false;
        pos = j + p.length;
      }
      return true;
    });
  };

  const escape = (el) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

  async function options(cid, timeout = 8000) {
    const end = Date.now() + timeout * T.timeoutScale;
    for (;;) {
      const opts = optionsOf(menuOf(cid));
      if (opts.length || Date.now() > end) return opts;
      await new Promise((r) => setTimeout(r, 120));
    }
  }

  async function open(cid) {
    const box = document.getElementById(cid);
    if (!box || !T.visible(box)) throw new Error(`${cid}: not on the page`);
    await T.click(box);
    return box;
  }

  // The heading an option is listed under: its group's label, else the nearest text before it in the menu that is no
  // option ("Womenswear" above "Pants").
  T.headerOf = (opt, menu) => {
    const group = opt.closest("[role='group']");
    if (group && menu.contains(group)) {
      const by = group.getAttribute("aria-labelledby");
      const label = group.getAttribute("aria-label") || (by && document.getElementById(by)?.textContent);
      if (label && label.trim()) return label.trim();
    }
    let best = "";
    for (const el of menu.querySelectorAll("*")) {
      if (el.children.length || !el.textContent.trim() || el.closest("[role='option']")) continue;
      if (el.compareDocumentPosition(opt) & Node.DOCUMENT_POSITION_FOLLOWING) best = el.textContent.trim();
    }
    return best;
  };
  const DEPARTMENT = { women: /\bwom[ae]n/i, men: /\bmen/i, kids: /\bkid/i };

  async function choose(st, cid, value, { typed = null, category = false, optional = false, prefix = false } = {}) {
    if (optional && !document.getElementById(cid)) {
      st.notes.push(`${cid}: not on this form, left out`);
      return;
    }
    const box = await open(cid);
    await T.type(box, typed ?? value);                    // T.type clears the field first
    await T.sleep(500);
    const opts = await options(cid);
    const texts = opts.map(textOf);
    let i = prefix ? texts.findIndex((t) => T.norm(t).startsWith(T.norm(value))) : T.matchOption(texts, value, category);
    let shown = texts[i];
    if (i < 0 && category) {                             // the leaf's name, under the item's department heading
      const [dept, , name] = String(value).split(" > ");
      const menu = menuOf(cid);
      const cands = opts.map((o, k) => ({ k, t: texts[k], h: T.headerOf(o, menu) }))
        .filter((c) => T.norm(c.t) === T.norm(name));
      const mine = cands.filter((c) => DEPARTMENT[T.norm(dept)]?.test(c.h));
      st.notes.push(`category menu: ${cands.map((c) => `${c.h || "?"} › ${c.t}`).join(", ") || "nothing named " + name}`);
      if (mine.length === 1) {
        i = mine[0].k;
        shown = `${mine[0].h} › ${mine[0].t}`;
      }
    }
    if (i < 0) {
      await T.screenshot(`menu-${cid}`);                // the open menu, for the record
      escape(box);
      throw new Error(`${cid}: no option '${value}' (offered: ${texts.slice(0, 8).join(", ")})`);
    }
    await T.click(opts[i]);
    T.picked(st, cid, value, shown);
    await T.sleep(400);
  }

  // The size menu lags after a category change (the catalog's known gaps): read it until it shows the size.
  async function size(st, value) {
    const cid = S().combo.ids.size;
    const end = Date.now() + 15000 * T.timeoutScale;
    let texts = [];
    for (;;) {
      const box = await open(cid);
      T.setValue(box, "");
      texts = (await options(cid, 3000)).map(textOf);
      escape(box);
      if (T.matchOption(texts, value) >= 0 || Date.now() > end) break;
      await T.sleep(1000);
    }
    st.sizeMenu = texts.slice(0, 60);
    await choose(st, cid, value);
  }

  // Depop's brand menu answers the typed text from its server: read it until our brand (or its clean form) is offered,
  // 8 seconds at most. None → the field cleared (its clear button), never a nearby brand left selected.
  async function brand(st, value, typed) {
    const cid = S().combo.ids.brand;
    const box = await open(cid);
    await T.type(box, typed || value);
    const end = Date.now() + 8000 * T.timeoutScale;
    let opts = [], texts = [], pick = { choice: null, guess: null };
    for (;;) {
      opts = optionsOf(menuOf(cid));
      texts = opts.map(textOf);
      pick = T.strictPick(value, texts);
      if (pick.choice || Date.now() > end) break;
      await new Promise((r) => setTimeout(r, 300));
    }
    const { choice, guess } = pick;
    if (!choice) {
      await T.screenshot(`menu-${cid}`);
      T.setValue(box, "");
      escape(box);
      const clear = T.$(S().brand_clear.selectors);
      if (clear && T.visible(clear)) await T.click(clear);
      st.guesses.push(`brand left empty (Depop has no '${value}')`);
      return;
    }
    await T.click(opts[texts.indexOf(choice)]);
    T.picked(st, cid, value, choice);
    if (guess) st.guesses.push(guess);
  }

  // Boost's switch: a checkbox whose label offers the paid promotion (recorded 2026-10-06: "Promote your item in search
  // to help it sell faster. Pay an extra 12% fee, only if you sell.").
  const boosts = () => {
    const re = new RegExp(S().boost.text, "i");
    return T.$$(S().boost.selectors).filter((b) => re.test(T.norm((b.closest("label") || b.parentElement || b).textContent)));
  };

  T.sites.depop = {
    async fill(job, files, st) {
      const f = job.fields;
      const ids = S().combo.ids;
      await T.step("photos", st, async () => {
        const input = await T.need(S().photos.selectors, "photos", { visible: false });
        st.attached = await T.attach(input, files.slice(0, 8), S().photos.drop);
        await T.waitCount(S().photos.thumbs, Math.min(files.length, 8), 90000);
        await T.screenshot("photos");
      });
      await T.step("description", st, async () =>
        T.type(await T.need(S().description.selectors, "description"), job.copy.description));
      await T.step("category", st, async () =>
        choose(st, ids.category, f.category, { typed: f.category.split(" > ").pop(), category: true }));
      if (f.size) await T.step("size", st, async () => size(st, f.size));
      if (f.brand) await T.step("brand", st, async () => brand(st, f.brand, f.brand_typed));
      await T.step("condition", st, async () => choose(st, ids.condition, f.condition));
      for (const c of (f.colors || []).slice(0, 2)) await T.step(ids.colour, st, async () => choose(st, ids.colour, c));
      for (const v of f.source || []) await T.step(ids.source, st, async () => choose(st, ids.source, v, { optional: true }));
      if (f.age) await T.step(ids.age, st, async () => choose(st, ids.age, f.age, { optional: true }));
      for (const v of f.style || []) await T.step(ids.style, st, async () => choose(st, ids.style, v, { optional: true }));
      for (const [name, values] of Object.entries(f.attributes || {})) {
        const cid = ids.attribute.replace("{name}", name);
        for (const v of values) await T.step(`attributes.${name}`, st, async () => choose(st, cid, v, { optional: true }));
      }
      await T.step("price", st, async () => T.type(await T.need(S().price.selectors, "price"), String(job.price)));
      await T.step("shipping", st, async () => {         // Depop Shipping: its USPS radio (on by default)
        const usps = await T.need(S().shipping.selectors, "shipping", { visible: false });
        if (!usps.checked) await T.click(usps);
        if (!usps.checked) throw new Error("the Depop Shipping radio didn't take");
        st.shipping = f.shipping;
        await T.sleep(600);
      });
      await T.step("package", st, async () => {
        await choose(st, ids.package, f.package_size, { prefix: true });
        st.package = document.getElementById(ids.package)?.value || null;
      });
      // Boost is paid: never on. One that is on by itself is turned off, with a real click.
      for (const b of boosts().filter((x) => x.checked)) {
        await T.click(b);
        st.notes.push("Boost was on: turned off");
      }
    },

    readBack(job, st) {
      const value = (sel) => {
        const el = T.$(sel);
        return el ? el.value : null;
      };
      return {
        description: value(S().description.selectors), price: value(S().price.selectors),
        photos: String(T.$$(S().photos.thumbs).length), package: st.package || null, shipping: st.shipping || null,
        boost: boosts().some((b) => b.checked), boost_found: boosts().length, size_menu: st.sizeMenu || null,
        ...st.picked,
      };
    },

    submitButtons() {
      const re = new RegExp(S().submit.text, "i");
      return T.$$(S().submit.selectors).filter((b) => T.visible(b) && re.test(T.norm(b.textContent)));
    },

    async submit(job, st) {
      const buttons = this.submitButtons();
      if (buttons.length !== 1) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "submit", page: "form",
                        message: `${buttons.length} Post buttons: never a guess` });
      }
      if (boosts().some((b) => b.checked)) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "submit", page: "form", message: "Boost is on" });
      }
      T.emit({ event: "step", job_id: job.job_id, name: "submit", ok: true, clicked: true });
      await T.click(buttons[0]);                                        // exactly once
    },

    async verify(job) {
      await T.sleep(1500);
      const text = (sel) => (T.$(sel)?.textContent || "").replace(/\s+/g, " ").trim();
      return { url: location.href, title: text(S().listing_title.selectors), price: text(S().listing_price.selectors),
               photos: T.$$(S().listing_photos.selectors).length, body: T.textOf().slice(0, 3000) };
    },

    async delist(job, st) {
      // The listing this agent created: Mark as sold, and the confirmation if one shows. Never Delete (WO32 §4).
      const re = new RegExp(S().mark_sold.text, "i");
      const button = T.$$(S().mark_sold.selectors).find((b) => T.visible(b) && re.test(T.norm(b.textContent)));
      if (!button) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "mark_sold", page: "form",
                        message: "no Mark as sold button" });
      }
      T.emit({ event: "step", job_id: job.job_id, name: "mark_sold", ok: true, clicked: true });
      await T.click(button);
      await T.sleep(1200);
      const cre = new RegExp(S().confirm.text, "i");
      const confirm = T.$$(S().confirm.selectors).find((b) => T.visible(b) && cre.test(T.norm(b.textContent)));
      if (confirm) await T.click(confirm);
      await T.sleep(1500);
      await T.screenshot("delisted");
      T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
    },

    async find(job, st) {
      await T.sleep(1500);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      T.emit({ event: "result", job_id: job.job_id, listings });
    },
  };
})();
