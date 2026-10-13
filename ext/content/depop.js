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
  // WO35: the SKUs a page shows (the seller's /manage/ page: "SKU: <our item id>") — our item ids anywhere in its text or
  // its inputs, so the driver can tell our listing by its SKU.
  const skusShown = () => {
    const ids = new Set();
    const body = document.body;
    const text = body ? `${body.innerText || ""}
${body.textContent || ""}` : "";   // jsdom: no innerText
    for (const m of text.matchAll(/\bi_\d{6}_[0-9a-f]{6}\b/g)) ids.add(m[0]);
    for (const el of document.querySelectorAll("input")) {
      for (const m of String(el.value || "").matchAll(/\bi_\d{6}_[0-9a-f]{6}\b/g)) ids.add(m[0]);
    }
    return [...ids];
  };
  // WO33: the listing's own state, from its JSON-LD (schema.org Product): sold out?
  const productLd = () => {
    try { return JSON.parse(T.$(S().listing_title.selectors)?.textContent || "null"); } catch (e) { return null; }
  };
  const soldOut = () => /SoldOut/i.test(String(productLd()?.offers?.availability || ""));
  // A button inside `root` whose text (or label) matches `pattern` exactly.
  const button = (root, pattern) => {
    const re = new RegExp(pattern, "i");
    return T.$$("button", root).find((b) => T.visible(b) && re.test(T.norm(b.textContent || b.getAttribute("aria-label") || "")));
  };
  // The bin next to Copy listing, then the "Delete listing" window (null, with an error emitted, when either is missing).
  const openDeleteWindow = async (job) => {
    const bin = await T.until(() => T.$$(S().delete_button.selectors).find(T.visible), 10000);
    if (!bin) {
      T.emit({ event: "error", job_id: job.job_id, stage: "delete_button", page: "listing", message: "no Delete listing bin on the page" });
      return null;
    }
    T.emit({ event: "step", job_id: job.job_id, name: "delete_bin", ok: true, clicked: true });
    await T.click(bin);
    const want = new RegExp(S().delete_dialog.text, "i");
    const dialog = await T.until(() => T.$$(S().delete_dialog.selectors).find((d) => T.visible(d) && want.test(T.norm(d.textContent))), 8000);
    if (!dialog) {
      await T.screenshot("no-delete-window");
      T.emit({ event: "error", job_id: job.job_id, stage: "delete_dialog", page: "listing", message: "the bin opened no Delete listing window" });
      return null;
    }
    return dialog;
  };
  // Is the listing still there? Its page fetched again (same site, the seller's cookies): its Product JSON-LD.
  const stillThere = async (url) => {
    try {
      const r = await fetch(url, { credentials: "include", cache: "no-store" });
      if (r.status === 404 || r.status === 410) return false;
      return /"@type"\s*:\s*"Product"/.test(await r.text());
    } catch (e) {
      return null;
    }
  };

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
    const current = document.getElementById(cid)?.value || "";
    if (single(cid) && !category && current &&            // a single choice already ours (live 2026-10-11: Depop had
        (prefix ? T.norm(current).startsWith(T.norm(value)) : T.norm(current) === T.norm(value))) {   // put "Small" in,
      T.picked(st, cid, value, current);                  // and its menu answered "No option" to "Small" typed again)
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
    if (i < 0 && !category && !texts.length) {           // a menu that offers nothing for the typed text: its whole
      await T.type(box, "");                             // list, our value picked from it
      const all = await options(cid, 3000);
      const allTexts = all.map(textOf);
      const k = prefix ? allTexts.findIndex((t) => T.norm(t).startsWith(T.norm(value))) : T.matchOption(allTexts, value, false);
      if (k >= 0) {
        await T.click(all[k]);
        T.picked(st, cid, value, allTexts[k]);
        if (single(cid)) await T.until(() => !optionsOf(menuOf(cid)).length, 1500);
        return;
      }
    }
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
      if (optional) {                                    // WO36: an optional field without our value stays empty —
        T.setValue(box, "");                             // never the first option, never a near one
        leftOut(st, cid, value, `no option '${value}'${T.offered(texts, 8)}`);
        return;
      }
      throw new Error(`${cid}: no option '${value}' (offered: ${texts.slice(0, 8).join(", ")})`);
    }
    await T.click(opts[i]);
    T.picked(st, cid, value, shown);
    if (single(cid)) await T.until(() => !optionsOf(menuOf(cid)).length, 1500);       // its menu closes
    else await T.until(() => T.chipsOf(cid).some((c) => T.norm(c) === T.norm(value)), 1500);   // its chip shows
  }

  // WO36 §3: an optional value the form doesn't offer is left out — said in the notes, and the read-back expects it
  // absent (st.left, which the driver takes off its plan).
  const leftOut = (st, cid, value, why) => {
    (st.left[cid] = st.left[cid] || []).push(value);
    st.notes.push(`${cid}: ${why} — left empty`);
  };

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

  // WO36 (the owner, 2026-10-12 — the Alo set: "Alo" typed found no "Alo" and Depop's own right "Alo Yoga" was replaced
  // by "Other"): our names in order (T.brandNames: the brand as shops list it, the label's text, the alias file's names,
  // the two parts of "MNG (Mango)"), each searched as written and in the other spellings of the same name; the name
  // picked EXACTLY when Depop offers it; else, once every name was searched, the ONE option that is a name of ours
  // followed by more words ("alo" → "Alo Yoga"); else Depop's "Other" (it requires a brand), the note listing what
  // Depop offered. Depop's own fill-in (it puts a brand in once the photos are up) is kept when it IS one of our names.
  async function brand(st, value, typed, names = [value]) {
    const cid = S().combo.ids.brand;
    const box = document.getElementById(cid);
    const theirs = box && box.value;
    const own = theirs ? T.brandChoose(names, [theirs]) : { choice: null };
    if (own.choice && own.how === "exact") {             // Depop filled one of our names in by itself
      T.picked(st, cid, value, theirs);
      st.brandChoice = theirs;
      const note = T.brandNote(value, theirs);
      if (note) st.guesses.push(note);
      return;
    }
    const seen = [];                                     // every brand Depop offered for our names
    for (const name of names) {
      for (const spelling of T.brandSpellings(name, name === value ? typed : name)) {
        const hit = await brandSearch(cid, box, spelling, (texts) => texts.findIndex((t) => T.brandKey(t) === T.brandKey(name)), seen);
        if (hit) {
          await T.click(hit.option);
          T.picked(st, cid, value, hit.text);
          st.brandChoice = hit.text;
          const note = T.brandNote(value, hit.text);
          if (note) st.guesses.push(note);
          return;
        }
      }
    }
    const longer = T.brandChoose(names, seen);           // no exact name: the ONE longer one ("alo" → "Alo Yoga")
    if (longer.choice) {
      const hit = await brandSearch(cid, box, longer.choice, (texts) => texts.indexOf(longer.choice), []);
      if (hit) {
        await T.click(hit.option);
        T.picked(st, cid, value, hit.text);
        st.brandChoice = hit.text;
        st.guesses.push(`brand set to '${hit.text}' (from '${longer.from}')`);
        return;
      }
    }
    await T.screenshot(`menu-${cid}`);
    await brandOther(st, value, seen);
  }

  // One search of Depop's brand menu (its server answers the typed text a moment later): the option `find` points at,
  // as soon as it shows; null after 5 s, or once the menu has offered only "Other" for 1.2 s (Depop has nothing under
  // this spelling). What it offered is added to `seen`.
  async function brandSearch(cid, box, text, find, seen) {
    await open(cid);
    await T.type(box, text);
    let opts = [], texts = [], since = performance.now(), last = "";
    const found = await T.until(() => {
      opts = optionsOf(menuOf(cid));
      texts = opts.map(textOf);
      const i = find(texts);
      if (i >= 0) return { option: opts[i], text: texts[i] };
      const now = texts.join("|");
      if (now !== last) { last = now; since = performance.now(); }
      if (texts.length === 1 && T.norm(texts[0]) === "other" && performance.now() - since > 1200) return { none: true };
      return null;
    }, 5000);
    for (const t of texts) if (T.norm(t) !== "other" && !seen.includes(t)) seen.push(t);
    return found && found.option ? found : null;
  }

  // Depop requires a brand (live 2026-10-09, WO35: the silk pants have none — Post was refused, "This field is
  // required" under Brand, and the form stayed). An item with no brand, or one Depop doesn't list, gets Depop's own
  // "Other": typed, and the option that reads exactly "Other" clicked.
  async function brandOther(st, value, seen = []) {
    const cid = S().combo.ids.brand;
    const box = await open(cid);
    await T.type(box, "Other");
    let opts = [], texts = [];
    const other = await T.until(() => {
      opts = optionsOf(menuOf(cid));
      texts = opts.map(textOf);
      const i = texts.findIndex((t) => T.norm(t) === "other");
      return i >= 0 ? opts[i] : null;
    }, 5000);
    if (!other) {
      await T.screenshot(`menu-${cid}-other`);
      await clearBrand(box);
      throw new Error(`no 'Other' in Depop's brand menu (offered: ${texts.slice(0, 6).join(", ")})`);
    }
    await T.click(other);
    st.brandOther = true;
    T.picked(st, cid, value || "Other", "Other");
    st.brandChoice = "Other";
    st.guesses.push(value ? `brand set to 'Other' (Depop has no '${value}'${T.offered(seen).replace(/^ \(/, "; ").replace(/\)$/, "")})`
      : "brand set to 'Other' (no brand)");
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
    out[ids.style] = [];                                 // never (the owner, 2026-10-12): Depop's Style stays empty
    for (const [name, values] of Object.entries(f.attributes || {})) out[ids.attribute.replace("{name}", name)] = values;
    for (const cid of Object.keys(out)) if (!document.getElementById(cid)) delete out[cid];
    return out;
  };

  // Every multi-select holds only our values: Depop's own chips removed. Returns how many were removed.
  async function chipsOurs(st, f) {
    let n = 0;
    for (const [cid, want] of Object.entries(planned(f))) {
      for (const b of chipButtons(cid)) {
        if (!want.some((w) => T.norm(w) === T.norm(chipValue(b)))) {
          st.notes.push(`removed '${chipValue(b)}' from ${cid} (Depop's own suggestion)`);
          await T.click(b);
          await T.until(() => !b.isConnected, 1500);
          n++;
        }
      }
    }
    return n;
  }

  // Our values a multi-select lost: added back (one it doesn't take is left out). Returns how many were added.
  async function addBack(st, f) {
    let n = 0;
    for (const [cid, want] of Object.entries(planned(f))) {
      for (const w of want) {
        if ((st.left[cid] || []).includes(w)) continue;
        if (!T.chipsOf(cid).some((c) => T.norm(c) === T.norm(w))) {
          st.notes.push(`'${w}' was gone from ${cid}: added back`);
          await choose(st, cid, w, { optional: true });
          n++;
        }
      }
    }
    return n;
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
      await T.step("brand", st, async () => (f.brand ? brand(st, f.brand, f.brand_typed, T.brandNames(f))
        : brandOther(st, null)), { limit: 60000 });       // WO35: Depop requires one — no brand is "Other"
      await T.step("condition", st, async () => choose(st, ids.condition, f.condition));
      for (const c of (f.colors || []).slice(0, 2)) {     // optional on Depop: one it doesn't offer is left out
        await T.step(ids.colour, st, async () => choose(st, ids.colour, c, { optional: true }));
      }
      for (const v of f.source || []) await T.step(ids.source, st, async () => choose(st, ids.source, v, { optional: true }));
      if (f.age) await T.step(ids.age, st, async () => choose(st, ids.age, f.age, { optional: true }));
      for (const [name, values] of Object.entries(f.attributes || {})) {
        const cid = ids.attribute.replace("{name}", name);
        for (const v of values) await T.step(`attributes.${name}`, st, async () => choose(st, cid, v, { optional: true }));
      }
      await T.step("price", st, async () => T.type(await T.need(S().price.selectors, "price"), String(job.price)));
      // Depop's SKU (WO33 A4): our item id — only the seller sees it; sales are matched by it, and Depop's Selling API
      // keys products by it.
      if (job.fields.sku && S().sku) {
        await T.step("sku", st, async () => T.type(await T.need(S().sku.selectors, "sku"), String(job.fields.sku)));
      }
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
        await chipsOurs(st, f);
        // WO36 §4: the required single choices read back — one that isn't ours is chosen again, once (the category
        // first: the size and the attributes come after it)
        const leaf = String(f.category || "").split(" > ").pop();
        const valueOf = (cid) => document.getElementById(cid)?.value || "";
        if (st.picked[ids.category] && T.norm(valueOf(ids.category)) !== T.norm(leaf)) {
          st.notes.push(`the category read '${valueOf(ids.category)}': chosen again`);
          await choose(st, ids.category, f.category, { typed: leaf, category: true });
        }
        if (f.size && T.norm(valueOf(ids.size)) !== T.norm(f.size)) {
          st.notes.push(`the size read '${valueOf(ids.size)}': chosen again`);
          await size(st, f.size);
        }
        if (T.norm(valueOf(ids.condition)) !== T.norm(f.condition)) {
          st.notes.push(`the condition read '${valueOf(ids.condition)}': chosen again`);
          await choose(st, ids.condition, f.condition);
        }
        const brandBox = document.getElementById(ids.brand);
        if ((!f.brand || st.brandOther) && brandBox && T.norm(brandBox.value) !== "other") {
          st.notes.push(`Depop changed the brand to '${brandBox.value}': set back to Other`);
          await brandOther(st, f.brand || null);
        }
        // A single choice Depop replaced after ours (its suggestions came late): ours again.
        const pkg = document.getElementById(ids.package);
        if (f.package_size && pkg && !T.norm(pkg.value).startsWith(T.norm(f.package_size))) {
          st.notes.push(`Depop changed the package size to '${pkg.value}': set back to ${f.package_size}`);
          await choose(st, ids.package, f.package_size, { prefix: true });
        }
        if (f.brand && !st.brandOther && brandBox && T.brandKey(brandBox.value) !== T.brandKey(st.brandChoice)) {
          st.notes.push(`Depop changed the brand to '${brandBox.value}': set back`);
          await brand(st, f.brand, f.brand_typed, T.brandNames(f));
        }
        await addBack(st, f);
        // WO37 (live 2026-10-12, twice): Depop put its own colour back AFTER this tidy — the page left to settle,
        // then the multi-selects read once more and set to ours again if they changed
        await T.quiet(1200, 6000);
        if (await chipsOurs(st, f) + await addBack(st, f)) {
          st.notes.push("Depop changed the form again after the tidy: set back once more");
          await T.quiet(800, 4000);
        }
      }, { limit: 60000 });                                 // the checks, a size read again, the page left to settle
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
        sku: S().sku ? value(S().sku.selectors) : null,
        photos: String(T.$$(S().photos.thumbs).length), photos_loaded: String(T.loaded(S().photos.thumbs)),
        package: of(ids.package) || null, shipping: st.shipping || null,
        boost: boosts().some((b) => b.checked), boost_found: boosts().length, size_menu: st.sizeMenu || null,
      };
      const leaf = String(f.category || "").split(" > ").pop();
      const cat = of(ids.category);
      seen[ids.category] = cat ? [T.norm(cat) === T.norm(leaf) && st.picked[ids.category] ? f.category : cat] : [];
      for (const cid of [ids.condition, ids.size, ids.package]) if (of(cid)) seen[cid] = [of(cid)];
      seen[ids.brand] = of(ids.brand) || null;
      seen.brand_choice = st.brandChoice || null;         // WO36: what the brand step picked, and how
      for (const cid of Object.keys(planned(f))) seen[cid] = T.chipsOf(cid);
      seen.left = st.left;                                 // WO36 §3: optional values the form didn't offer
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
      return { url: location.href, title, price, photos, attributes, shop, skus: skusShown(),
               product: String(ld["@type"] || "") === "Product" || !!ld.offers,
               availability: String(ld.offers?.availability || "") || null,
               body: [title, price, attributes, description].join("\n").slice(0, 3000) };
    },

    // WO35: Post lands on Depop's success page ("Nice! It's listed", /products/create/success/?productId=<n>, live
    // 2026-10-09) — its "View listing" link is the new listing's /products/<slug>/manage/ page: that address is the
    // listing's (the driver then reads the SKU there). The seller's /manage/ landing (2026-10-07) is a listing page.
    async landed(job, st) {
      const step = S().view_listing;
      const re = new RegExp(step.text, "i");
      const link = await T.until(() => T.$$(step.selectors).find((a) => /\/products\/[^/]+\/manage\/?$/.test(
        new URL(a.getAttribute("href") || "", location.href).pathname) || re.test(T.norm(a.textContent))), 10000);
      if (!link) return "no View listing link";
      const url = new URL(link.getAttribute("href"), location.href).href;
      return { url, how: "the success page's View listing" };
    },

    // WO33, the owner's decision (2026-10-08): Depop's take-down DELETES the listing — its page offers no Mark as
    // sold. On our listing's page (/products/<slug>/manage/): already sold (its JSON-LD says SoldOut) → nothing pressed,
    // "sold" (the group hears it sold twice); else the bin next to Copy listing, the "Delete listing" window, and its
    // Delete listing button — pressed once. The driver then opens the address again: gone = taken down.
    async delist(job, st) {
      if (soldOut()) {
        await T.screenshot("sold");
        return T.emit({ event: "result", job_id: job.job_id, sold: true, url: location.href });
      }
      const dialog = await openDeleteWindow(job);
      if (!dialog) return;
      const yes = button(dialog, S().delete_confirm.text);
      if (!yes) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "delete_confirm", page: "listing",
                        message: "the Delete listing window has no Delete listing button" });
      }
      T.emit({ event: "step", job_id: job.job_id, name: "delete_listing", ok: true, clicked: true });
      await T.click(yes);
      await T.until(() => !dialog.isConnected || !T.visible(dialog), 15000);
      await T.screenshot("deleted");
      T.emit({ event: "result", job_id: job.job_id, delete_pressed: true, url: location.href });
    },

    // WO33, the owner's practice run: the bin pressed, the window recorded, Cancel pressed — never Delete listing —
    // and the listing checked still live.
    async practice(job, st) {
      const dialog = await openDeleteWindow(job);
      if (!dialog) return;
      const seen = { title: T.norm(T.$("h1, h2, h3, [class*='title' i]", dialog)?.textContent || "").slice(0, 80),
                     text: T.norm(dialog.textContent).slice(0, 300),
                     buttons: T.$$("button", dialog).filter(T.visible).map((b) => T.norm(b.textContent || b.getAttribute("aria-label") || "")) };
      await T.screenshot("delete-window");
      const cancel = button(dialog, S().delete_cancel.text);
      if (!cancel) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "delete_cancel", page: "listing",
                        message: `the window has no Cancel (${JSON.stringify(seen).slice(0, 300)})` });
      }
      await T.click(cancel);
      const closed = await T.until(() => !dialog.isConnected || !T.visible(dialog), 6000);
      const live = await stillThere(job.check_url || location.href);
      await T.screenshot("after-cancel");
      T.emit({ event: "result", job_id: job.job_id, practice: true, window: seen, closed: !!closed, live });
    },

    // WO33: the take-down control (Mark as sold) looked for on our listing's page — never clicked; the page and its
    // picture are the evidence the selector is recorded from.
    async probe(job, st) {
      // WO33: Depop's take-down is the delete (its page has no Mark as sold) — the bin looked for, never pressed
      const button = await T.until(() => T.$$(S().delete_button.selectors).find(T.visible), 6000);
      await T.screenshot("probe");
      T.emit({ event: "result", job_id: job.job_id, probe: true, found: !!button, url: location.href,
               text: button ? (button.getAttribute("aria-label") || button.textContent).replace(/\s+/g, " ").trim() : null });
    },

    async find(job, st) {
      await T.waitFor(S().shop_links.selectors, { timeout: 8000, visible: false });   // the shop's tiles
      await T.quiet(600, 3000);
      await T.sleep(150);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      T.emit({ event: "result", job_id: job.job_id, listings });
    },
  };
})();
