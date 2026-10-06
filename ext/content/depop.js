// Thrift crosslister (WO32): Depop's create-listing form — photos, the description (Depop has no title: its first line
// is the title), then Depop's own Downshift comboboxes, whose ids come from data/depop_catalog.json: group-input
// (category), variants-input (size), brand-input, condition-input, colour-input, source-input, age-input, style-input,
// attributes.<name>-input, shippingMethods-input; each one's menu is <id without -input>-menu. Every combobox is
// cleared before typing (its menu filters by the typed text) and the option whose text IS the catalog value is picked.
// Then the price and Depop Shipping's package size. Boost is never turned on. Selectors: selectors.json → depop.
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

  async function choose(st, cid, value, { typed = null, category = false, optional = false } = {}) {
    if (optional && !document.getElementById(cid)) {
      st.notes.push(`${cid}: not on this form, left out`);
      return;
    }
    const box = await open(cid);
    await T.type(box, typed ?? value);                    // T.type clears the field first
    await T.sleep(500);
    const opts = await options(cid);
    const texts = opts.map(textOf);
    const i = T.matchOption(texts, value, category);
    if (i < 0) {
      escape(box);
      throw new Error(`${cid}: no option '${value}' (offered: ${texts.slice(0, 8).join(", ")})`);
    }
    await T.click(opts[i]);
    T.picked(st, cid, value, texts[i]);
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

  async function brand(st, value) {
    const cid = S().combo.ids.brand;
    const box = await open(cid);
    await T.type(box, value);
    await T.sleep(700);
    const opts = await options(cid, 5000);
    const texts = opts.map(textOf);
    const { choice, guess } = T.strictPick(value, texts);
    if (!choice) {
      T.setValue(box, "");
      escape(box);
      st.guesses.push(`brand left empty (Depop has no '${value}')`);
      return;
    }
    await T.click(opts[texts.indexOf(choice)]);
    T.picked(st, cid, value, choice);
    if (guess) st.guesses.push(guess);
  }

  const labelOf = (radio) => {
    const l = radio.id && [...document.querySelectorAll("label")].find((x) => x.htmlFor === radio.id);
    return textOf(l || radio.closest("label") || radio.parentElement || radio);
  };

  T.sites.depop = {
    async fill(job, files, st) {
      const f = job.fields;
      const ids = S().combo.ids;
      await T.step("photos", st, async () => {
        const input = await T.need(S().photos.selectors, "photos");
        st.attached = await T.attach(input, files.slice(0, 8), S().photos.drop);
        await T.waitCount(S().photos.thumbs, Math.min(files.length, 8), 90000);
      });
      await T.step("description", st, async () =>
        T.type(await T.need(S().description.selectors, "description"), job.copy.description));
      await T.step("category", st, async () =>
        choose(st, ids.category, f.category, { typed: f.category.split(" > ").pop(), category: true }));
      if (f.size) await T.step("size", st, async () => size(st, f.size));
      if (f.brand) await T.step("brand", st, async () => brand(st, f.brand));
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
      await T.step("shipping", st, async () => {
        await choose(st, ids.shipping, f.shipping);
        await T.sleep(600);
        const radios = T.$$(S().package.selectors);
        const r = radios.find((x) => T.norm(labelOf(x)).startsWith(T.norm(f.package_size)));
        if (!r) throw new Error(`package size '${f.package_size}' isn't offered (${radios.map(labelOf).join(", ")})`);
        await T.click(r);
        st.package = labelOf(r);
      });
      // Boost is paid: never on. One that is on by itself is turned off, with a real click.
      for (const b of T.$$(S().boost.selectors).filter((x) => x.checked)) {
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
        photos: String(T.$$(S().photos.thumbs).length), package: st.package || null,
        boost: T.$$(S().boost.selectors).some((b) => b.checked), size_menu: st.sizeMenu || null, ...st.picked,
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
      if (T.$$(S().boost.selectors).some((b) => b.checked)) {
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
