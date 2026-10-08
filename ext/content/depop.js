// Thrift crosslister (WO32): Depop's create-listing form — photos, the description (Depop has no title: its first line
// is the title), then Depop's own Downshift comboboxes, whose ids come from data/depop_catalog.json: group-input
// (category), variants-input (size), brand-input, condition-input, colour-input, source-input, age-input, style-input,
// attributes.<name>-input; each one's menu is <id without -input>-menu. Every combobox is cleared before typing (its
// menu filters by the typed text) and the option whose text IS the catalog value is picked. The category menu lists a
// name once per department ("Pants" under Women, Men, Kids): the one under the item's department is taken, never a
// guess. Then the price, Depop Shipping (the USPS radio) and its package size (the shippingMethods-input menu, recorded
// 2026-10-06). Boost ("Promote your item … Pay an extra 12% fee") is never turned on. Depop fills fields by itself once
// the photos are up (recorded: colour chips Cream + Grey, the brand, a package size): a multi-select keeps only our values
// (its other chips removed), a value already chosen is never clicked again (that would unselect it), and the form is read
// back from its own state — the inputs' values and the chips — not from what was clicked. Selectors: selectors.json →
// depop.
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
    return (await T.until(() => { const o = optionsOf(menuOf(cid)); return o.length ? o : null; }, timeout)) || [];
  }

  // A combobox, once the form shows it (the size and the attributes come after the category: fast, they may not be
  // there yet — the live miss, WO32b), then a real click.
  async function open(cid, timeout = 5000) {
    const box = await T.until(() => { const b = document.getElementById(cid); return b && T.visible(b) ? b : null; },
                              timeout);
    if (!box) throw new Error(`${cid}: not on the page`);
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

  // A multi-select's chosen values: its chips, each with a "Remove <value>" button (recorded 2026-10-06).
  const chipButtons = (cid) => {
    const box = document.getElementById(cid);
    return box ? [...box.parentElement.querySelectorAll(S().combo.chip_remove)] : [];
  };
  const chipValue = (b) => (b.getAttribute("aria-label") || "").replace(/^remove\s+/i, "").trim();
  T.chipsOf = (cid) => chipButtons(cid).map(chipValue);
  const single = (cid) => {
    const ids = S().combo.ids;
    return [ids.category, ids.brand, ids.condition, ids.size, ids.package].includes(cid);
  };

  async function choose(st, cid, value, { typed = null, category = false, optional = false, prefix = false } = {}) {
    if (optional && !(await T.until(() => document.getElementById(cid), 1500))) {
      st.notes.push(`${cid}: not on this form, left out`);
      return;
    }
    if (!single(cid) && T.chipsOf(cid).some((c) => T.norm(c) === T.norm(value))) {
      T.picked(st, cid, value, value);                   // chosen already (Depop's own suggestion): never clicked again
      return;
    }
    const box = await open(cid);
    await T.type(box, typed ?? value);                    // T.type clears the field first
    // The menu filters by the typed text: wait until it offers what we want (an older list may still be showing),
    // else take what it shows after 4 s.
    const leaf = T.norm(String(value).split(" > ").pop());
    const want = (texts) => (prefix ? texts.some((t) => T.norm(t).startsWith(T.norm(value)))
      : T.matchOption(texts, value, category) >= 0 || (category && texts.some((t) => T.norm(t) === leaf)));
    await T.until(() => want(optionsOf(menuOf(cid)).map(textOf)), 4000);
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
    if (single(cid)) await T.until(() => !optionsOf(menuOf(cid)).length, 1500);       // its menu closes
    else await T.until(() => T.chipsOf(cid).some((c) => T.norm(c) === T.norm(value)), 1500);   // its chip shows
  }

  // The size menu lags after a category change (the catalog's known gaps), and a menu opened during the lag keeps its
  // old list: read it until it shows the size, opening it again every 0.8 s, 12 s at most.
  async function size(st, value) {
    const cid = S().combo.ids.size;
    const end = Date.now() + 12000 * T.timeoutScale;
    let texts = [];
    for (;;) {
      const box = await open(cid);
      T.setValue(box, "");
      const shown = await T.until(() => {
        texts = optionsOf(menuOf(cid)).map(textOf);
        return T.matchOption(texts, value) >= 0;
      }, 800);
      escape(box);
      if (shown || Date.now() > end) break;
    }
    st.sizeMenu = texts.slice(0, 60);
    await choose(st, cid, value);
  }

  // Depop's brand menu answers the typed text from its server: read it until our brand (or its clean form) is offered,
  // 8 seconds at most. None → the field cleared (its clear button), never a nearby brand left selected.
  // Its search matches the typed text as written ("J. Crew" finds only "Other", "J.Crew" finds J.Crew: recorded
  // 2026-10-06), so the spellings are tried in turn: ours, without the space after a dot, without punctuation.
  T.brandSpellings = (value, typed) => [...new Set([typed, value, String(value).replace(/\.\s+/g, "."),
                                                    String(value).replace(/[^A-Za-z0-9& ]+/g, "").replace(/\s+/g, " ")]
                                                   .filter(Boolean))];

  async function brand(st, value, typed) {
    const cid = S().combo.ids.brand;
    const box = document.getElementById(cid);
    const already = box && box.value && T.strictPick(value, [box.value]);
    if (already && already.choice) {                    // Depop filled our brand in by itself
      T.picked(st, cid, value, box.value);
      if (already.guess) st.guesses.push(already.guess);
      return;
    }
    for (const spelling of T.brandSpellings(value, typed)) {
      await open(cid);
      await T.type(box, spelling);
      let opts = [], texts = [], since = performance.now(), last = "";
      const pick = (await T.until(() => {                // Depop's server answers: until our brand shows, 5 s at most
        opts = optionsOf(menuOf(cid));
        texts = opts.map(textOf);
        const p = T.strictPick(value, texts);
        if (p.choice) return p;
        const now = texts.join("|");
        if (now !== last) { last = now; since = performance.now(); }
        // only "Other", unchanged for 1.2 s: Depop has nothing under this spelling — the next one
        if (texts.length === 1 && T.norm(texts[0]) === "other" && performance.now() - since > 1200) return { none: true };
        return null;
      }, 5000)) || { choice: null, guess: null };
      if (pick.choice) {
        await T.click(opts[texts.indexOf(pick.choice)]);
        T.picked(st, cid, value, pick.choice);
        if (pick.guess) st.guesses.push(pick.guess);
        return;
      }
    }
    await T.screenshot(`menu-${cid}`);
    await clearBrand(box);
    st.guesses.push(`brand left empty (Depop has no '${value}')`);
  }

  async function clearBrand(box) {
    T.setValue(box, "");
    escape(box);
    const clear = T.$(S().brand_clear.selectors);
    if (clear && T.visible(clear)) await T.click(clear);
  }

  // What each multi-select should hold, from the job (every multi-select on the form: none unless planned).
  const planned = (f) => {
    const ids = S().combo.ids;
    const out = {};
    for (const box of document.querySelectorAll("form input[role='combobox']")) {
      if (box.id && !single(box.id) && box.id.endsWith("-input")) out[box.id] = [];
    }
    out[ids.colour] = (f.colors || []).slice(0, 2);
    out[ids.source] = f.source || [];
    out[ids.age] = f.age ? [f.age] : [];
    out[ids.style] = f.style || [];
    for (const [name, values] of Object.entries(f.attributes || {})) out[ids.attribute.replace("{name}", name)] = values;
    for (const cid of Object.keys(out)) if (!document.getElementById(cid)) delete out[cid];
    return out;
  };

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
      await T.step("photos", st, async () => {         // all at once; up to 120 s for the thumbnails to show
        const input = await T.need(S().photos.selectors, "photos", { visible: false });
        const n = Math.min(files.length, 8);
        st.attached = await T.attach(input, files.slice(0, 8), S().photos.drop);
        await T.waitCount(S().photos.thumbs, n, 60000);
        st.photosLoaded = await T.waitLoaded(S().photos.thumbs, n, 50000);
        await T.screenshot("photos");
        return `${st.photosLoaded}/${n} shown`;
      }, { limit: 120000 });
      // Depop fills colours, the brand and a package size by itself once the photos are up (recorded 2026-10-06). At
      // the fast pace its suggestions could land after our own picks and replace them (live 2026-10-07: the package
      // set to Medium after ours): they are waited for (8 s at most) and for the page to settle; ours go over them.
      await T.step("suggestions", st, async () => {
        const theirs = () => document.getElementById(ids.brand)?.value || document.getElementById(ids.package)?.value ||
          T.chipsOf(ids.colour).length;
        const came = await T.until(theirs, 8000);
        if (came) await T.quiet(600, 3000);
        return came ? "Depop's own came" : "none came";
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
        await T.sleep(150);
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
      st.photosLoadedEnd = await T.waitLoaded(S().photos.thumbs, Math.min(files.length, 8), 15000);
      await T.screenshot("photos-end");
      // Depop's own suggestions that aren't ours: every multi-select keeps only the values we chose; a brand we didn't
      // ask for is cleared (the listing says only what the facts support).
      await T.step("tidy", st, async () => {
        for (const [cid, want] of Object.entries(planned(f))) {
          for (const b of chipButtons(cid)) {
            if (!want.some((w) => T.norm(w) === T.norm(chipValue(b)))) {
              st.notes.push(`removed '${chipValue(b)}' from ${cid} (Depop's own suggestion)`);
              await T.click(b);
              await T.until(() => !b.isConnected, 1500);
            }
          }
        }
        const brandBox = document.getElementById(ids.brand);
        if (!f.brand && brandBox && brandBox.value) {
          st.notes.push(`cleared the brand '${brandBox.value}' (Depop's own suggestion)`);
          await clearBrand(brandBox);
        }
        // A single choice Depop replaced after ours (its suggestions came late): ours again.
        const pkg = document.getElementById(ids.package);
        if (f.package_size && pkg && !T.norm(pkg.value).startsWith(T.norm(f.package_size))) {
          st.notes.push(`Depop changed the package size to '${pkg.value}': set back to ${f.package_size}`);
          await choose(st, ids.package, f.package_size, { prefix: true });
        }
        if (f.brand && brandBox && brandBox.value && !T.strictPick(f.brand, [brandBox.value]).choice) {
          st.notes.push(`Depop changed the brand to '${brandBox.value}': set back`);
          await brand(st, f.brand, f.brand_typed);
        }
        // Our values a multi-select lost: added back.
        for (const [cid, want] of Object.entries(planned(f))) {
          for (const w of want) {
            if (!T.chipsOf(cid).some((c) => T.norm(c) === T.norm(w))) {
              st.notes.push(`'${w}' was gone from ${cid}: added back`);
              await choose(st, cid, w, { optional: cid !== ids.colour });
            }
          }
        }
      });
    },

    // The form as it is: the single-selects' values, the multi-selects' chips (the category's input shows its leaf
    // name: our path when it is that name, chosen under the department heading).
    readBack(job, st) {
      const f = job.fields;
      const ids = S().combo.ids;
      const value = (sel) => {
        const el = T.$(sel);
        return el ? el.value : null;
      };
      const of = (cid) => document.getElementById(cid)?.value || "";
      const seen = {
        description: value(S().description.selectors), price: value(S().price.selectors),
        photos: String(T.$$(S().photos.thumbs).length), photos_loaded: String(T.loaded(S().photos.thumbs)),
        package: of(ids.package) || null, shipping: st.shipping || null,
        boost: boosts().some((b) => b.checked), boost_found: boosts().length, size_menu: st.sizeMenu || null,
      };
      const leaf = String(f.category || "").split(" > ").pop();
      const cat = of(ids.category);
      seen[ids.category] = cat ? [T.norm(cat) === T.norm(leaf) && st.picked[ids.category] ? f.category : cat] : [];
      for (const cid of [ids.condition, ids.size, ids.package]) if (of(cid)) seen[cid] = [of(cid)];
      seen[ids.brand] = of(ids.brand) || null;
      for (const cid of Object.keys(planned(f))) seen[cid] = T.chipsOf(cid);
      return seen;
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

    // The listing page (recorded 2026-10-07: Post lands on /products/<slug>/manage/): its JSON-LD carries our
    // description (the title its first line), the price and one image per photo; Depop's own h1 is a name it makes up,
    // never compared. The shop link gives the shop's name (the shop check's page).
    async verify(job) {
      const script = await T.waitFor(S().listing_title.selectors, { timeout: 10000, visible: false });
      let ld = {};
      try { ld = JSON.parse(script?.textContent || "{}"); } catch (e) { /* the DOM below */ }
      const text = (sel) => (T.$(sel)?.textContent || "").replace(/\s+/g, " ").trim();
      const description = String(ld.description || "");
      const title = description.split("\n")[0].trim() || String(ld.name || "");
      const price = text(S().listing_price.selectors) || (ld.offers?.price ? `$${ld.offers.price}` : "");
      const photos = Array.isArray(ld.image) ? ld.image.length : T.$$(S().listing_photos.selectors).length;
      const attributes = text(S().listing_attributes.selectors);
      const href = T.$(S().shop_link.selectors)?.getAttribute("href") || "";
      const shop = (href.match(/^\/([^/?#]+)\/?/) || [])[1] || null;
      return { url: location.href, title, price, photos, attributes, shop,
               body: [title, price, attributes, description].join("\n").slice(0, 3000) };
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
      const cre = new RegExp(S().confirm.text, "i");
      const confirm = await T.until(() => T.$$(S().confirm.selectors)
        .find((b) => T.visible(b) && cre.test(T.norm(b.textContent))), 4000);
      if (confirm) {
        await T.click(confirm);
        await T.until(() => !confirm.isConnected, 4000);
      }
      await T.screenshot("delisted");
      T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
    },

    async find(job, st) {
      await T.waitFor(S().shop_links.selectors, { timeout: 8000, visible: false });   // the shop's tiles
      await T.sleep(150);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      T.emit({ event: "result", job_id: job.job_id, listings });
    },
  };
})();
