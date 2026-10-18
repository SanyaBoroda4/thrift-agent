// Thrift crosslister (WO32): Vinted's upload form, in its order — photos, title, description, category (the leaf by its
// id, else the tree walked by its path), brand (its own list, exact or the clean form only), size, condition, colours
// (≤ 2), materials (≤ 3), the skirt length Skirts require, price, the package size. Recorded live (2026-10-06): each
// field is a readonly input opening a "-dropdown-content" / "-grid-content" / "-list-content" panel whose rows are
// [role=button] (a category branch), [role=radio] (a leaf, a condition) or [role=checkbox] (a size, a colour, a
// material), a "Suggested" group above the full list; the package size is a cell per size with its radio, the chosen
// one turning role=presentation (Vinted picks a "Recommended" one by itself). A row already chosen is never clicked
// again. The values are read back from the inputs. Promotion offers are closed, never accepted.
(function () {
  const T = globalThis.Thrift;
  const S = () => T.selectors.vinted.steps;
  const PACKAGE = { X_SMALL: "extra small", SMALL: "small", MEDIUM: "medium", LARGE: "large" };

  const escape = () => {
    const target = document.activeElement || document.body;
    target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  };

  async function open(name) {
    const input = await T.need(S()[name].selectors, name);
    await T.click(input);
    // its panel shows: the chevron up, rows, or (the brand) its search box
    await T.until(() => isOpen(input) || (S()[name].search && T.$$(S()[name].search).some(T.visible)), 2000);
    return input;
  }

  // A row's own words: its title cell when it has one (Vinted's rows carry a title and sometimes a description).
  const rowText = (r) => (T.$(S().rows.title, r) || r).textContent.replace(/\s+/g, " ").trim();

  async function clickRow(text, timeout = 8000) {
    let texts = [];
    const found = await T.until(() => {                  // the row, as soon as the panel shows it
      const rows = T.$$(S().rows.selectors).filter(T.visible);
      texts = rows.map(rowText);
      const i = texts.findIndex((t) => T.norm(t) === T.norm(text) || T.norm(t.split(" | ")[0]) === T.norm(text));
      return i >= 0 ? { row: rows[i], shown: texts[i] } : null;
    }, timeout);
    if (!found) {
      await T.screenshot(`menu-${T.norm(text).replace(/[^a-z0-9]+/g, "-").slice(0, 30)}`);   // the open list
      throw new Error(`no '${text}' (offered: ${texts.slice(0, 8).join(", ")})`);
    }
    if (found.row.getAttribute("aria-checked") !== "true") await T.click(found.row);   // chosen already: leave it
    await T.sleep(120);
    return found.shown;
  }

  // The package sizes: a cell each, its name in the title's own span ("Recommended" is a badge beside it).
  const packageCells = () => T.$$(S().package.cells).filter(T.visible);
  const cellName = (c) => T.norm((T.$(S().package.title, c) || c).textContent);
  const chosenPackage = () => {
    const cell = packageCells().find((c) => T.$(S().package.radio, c)?.checked);
    if (!cell) return null;
    return Object.keys(PACKAGE).find((k) => PACKAGE[k] === cellName(cell)) || cellName(cell);
  };

  // WO35: the seller's newest items from Vinted's own JSON (/api/v2/wardrobe/<member>/items?order=newest_first) —
  // [{url, title, id}], [] when it can't be read.
  async function wardrobe(member) {
    const path = String(S().wardrobe?.url || "/api/v2/wardrobe/{member}/items?order=newest_first&per_page=40&page=1")
      .replace("{member}", encodeURIComponent(member));
    try {
      const r = await fetch(new URL(path, location.origin).href, { credentials: "include",
                                                                   headers: { Accept: "application/json" } });
      if (!r.ok) return [];
      const data = await r.json();
      return (data.items || []).filter((x) => x && x.id).map((x) => ({
        id: String(x.id), title: String(x.title || ""), text: String(x.title || ""),
        url: x.url ? new URL(x.url, location.origin).href : `${location.origin}/items/${x.id}`,
        ...itemFacts(x),
      }));
    } catch (e) {
      return [];
    }
  }

  // WO45 follow-up: what one wardrobe item says about itself — its price, its state, and its photo count when the
  // JSON carries the photos (a list) or a count; null when it doesn't (then the edit page is the only count). The
  // first item's keys are reported once (`shape`) so the live JSON's names are known.
  const itemFacts = (x) => ({
    price: x.price?.amount ?? x.price ?? null,
    is_reserved: !!x.is_reserved, is_hidden: !!x.is_hidden, is_closed: !!x.is_closed, is_draft: !!x.is_draft,
    photos: Array.isArray(x.photos) ? x.photos.length : (Number.isInteger(x.photos_count) ? x.photos_count
      : Number.isInteger(x.photo_count) ? x.photo_count : null),
  });

  // Every wardrobe item (96 a page, 5 pages at most): one JSON call a page — the bulk read of every listing's price,
  // state and photo count, never a listing page each (WO45 follow-up: the owner, "looks like a bot").
  async function wardrobeAll(member) {
    const base = String(S().wardrobe?.url || "/api/v2/wardrobe/{member}/items?order=newest_first&per_page=40&page=1")
      .replace("{member}", encodeURIComponent(member)).replace(/per_page=\d+/, "per_page=96");
    const out = [];
    let shape = null;
    for (let n = 1; n <= 5; n++) {
      const r = await fetch(new URL(base.replace(/page=\d+$/, `page=${n}`), location.origin).href,
                            { credentials: "include", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`Vinted's data answered HTTP ${r.status}`);
      const data = await r.json();
      const items = data.items || [];
      if (!shape && items[0]) shape = Object.keys(items[0]).slice(0, 40);
      for (const x of items) {
        if (x && x.id) out.push({ id: String(x.id), title: String(x.title || ""), ...itemFacts(x) });
      }
      if (items.length < 96) break;
    }
    return { items: out, shape };
  }

  // WO41: ONE of the seller's listings in Vinted's own JSON, by its id (the price drop reads its price and whether
  // it is reserved / hidden / closed): the wardrobe's pages, newest first, until it shows (5 pages of 96 at most).
  async function wardrobeItem(member, id) {
    const base = String(S().wardrobe?.url || "/api/v2/wardrobe/{member}/items?order=newest_first&per_page=40&page=1")
      .replace("{member}", encodeURIComponent(member)).replace(/per_page=\d+/, "per_page=96");
    for (let n = 1; n <= 5; n++) {
      const r = await fetch(new URL(base.replace(/page=\d+$/, `page=${n}`), location.origin).href,
                            { credentials: "include", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`Vinted's data answered HTTP ${r.status}`);
      const data = await r.json();
      const items = data.items || [];
      const x = items.find((i) => String(i?.id) === String(id));
      if (x) {
        return { id: String(x.id), title: String(x.title || ""), price: x.price ?? null,
                 is_reserved: !!x.is_reserved, is_hidden: !!x.is_hidden, is_closed: !!x.is_closed,
                 is_draft: !!x.is_draft };
      }
      if (items.length < 96) return null;
    }
    return null;
  }

  async function pick(st, name, value, { optional = false } = {}) {
    const input = await open(name);
    let shown;
    try {
      shown = await clickRow(value);
    } catch (e) {
      if (!optional) throw e;
      (st.left[name] = st.left[name] || []).push(value);   // WO36 §3: left empty — never another row
      st.notes.push(`${name}: ${e.message} — left empty`);
      await close(input);
      return;
    }
    T.picked(st, name, value, shown);
    await close(input);
  }

  // WO36 §4: a field's values as its input shows them (a multi-select lists its choices, comma-separated)
  const valuesOf = (name) => (T.$(S()[name]?.selectors)?.value || "").split(",").map((x) => x.trim()).filter(Boolean);

  // WO36 §4: a multi-select set to exactly our values, once — the rows checked that aren't ours unchecked, ours checked
  async function setMulti(st, name, want) {
    const input = await open(name);
    for (const row of T.$$(S().rows.selectors).filter(T.visible)) {
      const ours = want.some((w) => T.norm(rowText(row)) === T.norm(w));
      const on = row.getAttribute("aria-checked") === "true";
      if (on !== ours) {
        await T.click(row);
        await T.sleep(120);
      }
    }
    await close(input);
  }

  // A field's panel state is its chevron (recorded 2026-10-06: <field>-chevron-up while open, -chevron-down when closed,
  // inside the field's toggle [role=button]). A multi-select's panel stays open after a pick and Escape doesn't close it
  // (the material panel was left open over the price): it is closed with its toggle — so none is left over Upload.
  const baseOf = (input) => (input.getAttribute("data-testid") || "").replace(/-input$/, "");
  const chevronUp = (base) => base && document.querySelector(`[data-testid='${base}-chevron-up']`);
  const isOpen = (input) => {
    const base = baseOf(input);
    if (chevronUp(base)) return true;
    if (base && document.querySelector(`[data-testid='${base}-chevron-down']`)) return false;
    return T.$$(S().rows.selectors).some(T.visible);
  };

  async function close(input) {
    escape();
    await T.sleep(60);                                   // React takes the key (a settle, never a throttled timer)
    for (let k = 0; k < 3 && isOpen(input); k++) {       // a multi-select stays open: its own toggle closes it
      const up = chevronUp(baseOf(input));
      await T.click(up?.closest("[role='button']") || up || input);
      await T.until(() => !isOpen(input), 1500);
    }
  }

  // Every panel still open at the end (its chevron up): closed the same way.
  async function closeAll(st) {
    for (const up of T.$$(["[data-testid$='-chevron-up']"])) {
      st.notes.push(`closed the open ${up.getAttribute("data-testid").replace(/-chevron-up$/, "")} panel`);
      await T.click(up.closest("[role='button']") || up);
      await T.until(() => !up.isConnected || up.getAttribute("data-testid")?.endsWith("-chevron-down"), 1500);
    }
  }

  const noBrandRow = (t) => new RegExp(S().empty_brand.text, "i").test(T.norm(t));

  async function brand(st, f) {
    await open("brand");
    const search = await T.waitFor(S().brand.search, { timeout: 3000 });
    const box = search || (await T.need(S().brand.selectors, "brand"));
    const names = f.brand ? T.brandNames(f) : [];
    const seen = [];                                     // every brand Vinted offered for our names
    const find = async (query, match) => {               // one search: until the row shows, 5 s at most
      await T.type(box, query);
      let rows = [], texts = [];
      const hit = await T.until(() => {
        rows = T.$$(S().rows.selectors).filter(T.visible);
        texts = rows.map(rowText);
        const i = match(texts);
        return i >= 0 ? { row: rows[i], text: texts[i] } : null;
      }, 5000);
      for (const t of texts) if (!noBrandRow(t) && !seen.includes(t)) seen.push(t);
      return hit;
    };
    const take = async (hit, note) => {
      await T.click(hit.row);
      T.picked(st, "brand", f.brand, hit.text);
      st.brandChoice = hit.text;
      if (note) st.guesses.push(note);
      escape();
    };
    for (const name of names) {
      for (const query of T.spellings(name)) {
        const hit = await find(query, (texts) => texts.findIndex((t) => !noBrandRow(t) && T.brandKey(t) === T.brandKey(name)));
        if (hit) return take(hit, T.brandNote(f.brand, hit.text));
      }
    }
    const longer = T.brandChoose(names, seen);           // no exact name: the ONE longer one ("alo" → "Alo Yoga")
    if (longer.choice) {
      const hit = await find(longer.choice, (texts) => texts.indexOf(longer.choice));
      if (hit) return take(hit, `brand set to '${hit.text}' (from '${longer.from}')`);
    }
    if (f.brand) await T.screenshot("menu-brand");
    await T.type(box, "");
    const step = S().empty_brand;
    const none = await T.until(() => T.$$(step.selectors).find(T.visible) ||
      T.$$(S().rows.selectors).find((r) => T.visible(r) && noBrandRow(rowText(r))), 5000);
    if (!none) throw new Error("no 'List without brand' in Vinted's brand menu");
    if (none.getAttribute("aria-checked") !== "true") await T.click(none);
    T.picked(st, "brand", f.brand || "", "List without brand");
    st.brandChoice = "";
    const offered = T.offered(seen).replace(/^ \(/, "; ").replace(/\)$/, "");
    st.guesses.push(f.brand ? `listed without a brand (Vinted has no '${f.brand}'${offered})` : "listed without a brand");
    escape();
  }

  // The first package size of ours this item's category offers. The sizes come from Vinted's server after the details
  // (live 2026-10-10, WO35: none drawn yet 4 ms after the price, twice — "package MEDIUM not offered ()"): waited for,
  // 10 s at most.
  async function packageSize(f) {
    await T.until(() => packageCells().length > 0, 10000);
    const cells = packageCells();
    for (const code of f.package_sizes || []) {
      const cell = cells.find((c) => cellName(c) === PACKAGE[code]);
      if (!cell) continue;
      const radio = T.$(S().package.radio, cell);
      if (!(radio && radio.checked)) await T.click(cell);
      if (radio && !(await T.until(() => radio.checked, 3000))) {
        throw new Error(`the package size ${PACKAGE[code]} didn't take`);
      }
      return;
    }
    throw new Error(`package ${f.package_sizes} not offered (${cells.map(cellName).join(", ")})`);
  }

  // WO36 §4: before the form is reported, every field read back — a required one that isn't ours is chosen again
  // (once), an optional multi-select set to exactly our values (once; one it doesn't take is left out), the brand set
  // back when Vinted changed it. What still differs is the driver's diff: the listing waits, nothing is published.
  async function check(st, f) {
    const fixed = [];
    for (const [name, want] of [["size", f.size], ["condition", f.condition], ["skirt_length", f.skirt_length]]) {
      if (!want || !T.$(S()[name]?.selectors)) continue;
      const got = valuesOf(name)[0] || "";
      if (T.norm(got) !== T.norm(want)) {
        st.notes.push(`${name} read '${got}': chosen again`);
        await pick(st, name, want);
        fixed.push(name);
      }
    }
    for (const [name, list] of [["color", (f.colors || []).slice(0, 2)], ["material", (f.materials || []).slice(0, 3)]]) {
      if (!T.$(S()[name]?.selectors)) continue;
      const want = list.filter((v) => !(st.left[name] || []).includes(v));
      const got = valuesOf(name);
      const same = got.length === want.length && want.every((w) => got.some((g) => T.norm(g) === T.norm(w)));
      if (!same) {
        st.notes.push(`${name} read '${got.join(", ")}': set to '${want.join(", ")}'`);
        await setMulti(st, name, want);
        fixed.push(name);
      }
    }
    const shown = T.$(S().brand.selectors)?.value || "";
    const same = st.brandChoice ? T.brandKey(shown) === T.brandKey(st.brandChoice) : (!shown || noBrandRow(shown));
    if (st.brandChoice !== undefined && !same) {          // "List without brand" is what its field shows then (live)
      st.notes.push(`the brand read '${shown}': set again`);
      await brand(st, f);
      fixed.push("brand");
    }
    if (fixed.length && (f.package_sizes || []).length && !(f.package_sizes || []).includes(chosenPackage())) {
      st.notes.push(`the package size read '${chosenPackage()}' after the fix: chosen again`);   // a size re-pick may
      await packageSize(f);                                                                      // change it
      fixed.push("package");
    }
    await closeAll(st);
    return fixed.length ? `fixed: ${fixed.join(", ")}` : "as planned";
  }

  T.sites.vinted = {
    async fill(job, files, st) {
      const f = job.fields;
      await T.dismiss(S().promo_close.selectors);
      await T.step("photos", st, async () => {         // the file input is hidden (u-hidden): found, not "visible"
        const input = await T.need(S().photos.selectors, "photos", { visible: false });
        const n = Math.min(files.length, 20);
        st.attached = await T.attach(input, files.slice(0, 20), S().photos.drop);
        await T.waitCount(S().photos.thumbs, n, 60000);
        st.photosLoaded = await T.waitLoaded(S().photos.thumbs, n, 50000);
        await T.screenshot("photos");
        return `${st.photosLoaded}/${n} shown`;
      }, { limit: 120000 });
      await T.step("title", st, async () => T.type(await T.need(S().title.selectors, "title"), job.copy.title));
      await T.step("description", st, async () =>
        T.type(await T.need(S().description.selectors, "description"), job.copy.description));
      // The tree, level by level; at every level the leaf by its id when it shows (a leaf row is a radio, a branch a
      // button: recorded 2026-10-06).
      await T.step("category", st, async () => {
        await open("category");
        const leafSel = S().category.leaf.map((s) => s.replace("{id}", f.category_id));
        const parts = f.category_path.split(" > ");
        for (let k = 0; ; k++) {
          const leaf = T.$(leafSel);
          if (leaf && T.visible(leaf)) {
            await T.click(leaf);
            break;
          }
          if (k >= parts.length) throw new Error(`the leaf ${f.category_id} isn't under ${f.category_path}`);
          await clickRow(parts[k]);
        }
        await T.until(() => (T.$(S().category.selectors)?.value || "").trim(), 3000);   // the input shows the leaf
        T.picked(st, "category", f.category_path, T.$(S().category.selectors)?.value || "");
        escape();
      });
      // Vinted requires a brand (live 2026-10-09, WO35: the Solid & Striped set — Upload refused, "Fill in brand to
      // continue"). WO36 (the owner, 2026-10-12): our names in order (T.brandNames: the brand as shops list it, the
      // label's text, the alias file's names, the two parts of "MNG (Mango)"), each searched as written and in the other
      // spellings of the same name — never by a word of it; the name picked EXACTLY when Vinted lists it; else, once
      // every name was searched, the ONE brand that is a name of ours followed by more words ("alo" → "Alo Yoga"); else
      // Vinted's own "List without brand" (No brand indicated), the note listing what Vinted offered.
      await T.step("brand", st, async () => brand(st, f), { limit: 60000 });   // several names, each answered in turn
      if (f.size) await T.step("size", st, async () => pick(st, "size", f.size));
      await T.step("condition", st, async () => pick(st, "condition", f.condition));
      // colours and materials are optional on Vinted (its catalog): one it doesn't offer is left out (WO36 §3)
      for (const c of (f.colors || []).slice(0, 2)) {
        await T.step("color", st, async () => pick(st, "color", c, { optional: true }));
      }
      for (const m of (f.materials || []).slice(0, 3)) {
        await T.step("material", st, async () => pick(st, "material", m, { optional: true }));
      }
      if (f.skirt_length) await T.step("skirt_length", st, async () => pick(st, "skirt_length", f.skirt_length));
      await T.step("price", st, async () => T.type(await T.need(S().price.selectors, "price"), String(job.price)));
      await T.step("package", st, async () => packageSize(f));
      await T.dismiss(S().promo_close.selectors);
      await closeAll(st);
      await T.step("check", st, async () => check(st, f));   // WO36 §4: the form read back, fixed once
      st.photosLoadedEnd = await T.waitLoaded(S().photos.thumbs, Math.min(files.length, 20), 15000);
      await T.screenshot("photos-end");
      T.$(S().details_view?.selectors)?.scrollIntoView?.({ block: "start" });   // the form's screenshot: the details
      await T.sleep(500);
    },

    // WO43: the category chosen again (the leaf by its id down the new department's tree) and the size on the grid
    // that follows it, on the listing's edit page. Nothing else.
    async setFields(job, st) {
      const f = job.fields || {};
      // what the listing has now: a new category empties the condition and the package, and THOSE go back — the
      // owner's own choices (live 2026-10-18: the dry run had put our mapping's "Very good" / medium over her "Good" /
      // large); our values only where the page had none
      const before = { condition: (T.$(S().condition?.selectors)?.value || "").trim(), package: chosenPackage(),
                       brand: (T.$(S().brand?.selectors)?.value || "").trim() };
      st.notes.push(`before the change: brand ${before.brand || "none"}, condition ${before.condition || "none"}, `
                    + `package ${before.package || "none"}`);
      if (f.category_id) {
        await T.step("category", st, async () => {
          await open("category");
          const leafSel = S().category.leaf.map((s) => s.replace("{id}", f.category_id));
          const parts = (f.category_path || "").split(" > ");
          for (let k = 0; ; k++) {
            const leaf = T.$(leafSel);
            if (leaf && T.visible(leaf)) {
              await T.click(leaf);
              break;
            }
            if (k >= parts.length) throw new Error(`the leaf ${f.category_id} isn't under ${f.category_path}`);
            await clickRow(parts[k]);
          }
          await T.until(() => (T.$(S().category.selectors)?.value || "").trim(), 3000);
          T.picked(st, "category", f.category_path, T.$(S().category.selectors)?.value || "");
          escape();
        });
      }
      if (f.size) {
        await T.quiet(600, 4000);                        // the size grid follows the category
        await T.step("size", st, async () => pick(st, "size", f.size));
      }
      // live 2026-10-18: a new category empties the brand ("Fill in brand to continue" — Vinted refused the Save), the
      // condition and the package size — the listing's own set back, the same brand searched as the sell form does
      if (before.brand && !(T.$(S().brand?.selectors)?.value || "").trim()) {
        await T.step("brand", st, async () => brand(st, { brand: before.brand, brand_candidates: [before.brand] }),
                     { limit: 60000 });
      }
      const condition = before.condition || f.condition;
      const packages = [before.package, ...(f.package_sizes || [])].filter(Boolean);
      if (condition) await T.step("condition", st, async () => pick(st, "condition", condition));
      if (packages.length) await T.step("package", st, async () => packageSize({ package_sizes: packages }));
      await closeAll(st);
    },
    fieldsSeen(job, st) {
      const value = (sel) => (T.$(sel) ? T.$(sel).value : null);
      return { category: value(S().category.selectors), size: value(S().size?.selectors),
               condition: value(S().condition?.selectors), package: chosenPackage(),
               brand: value(S().brand?.selectors), picked: st.picked ? { ...st.picked } : null };
    },

    readBack(job, st) {
      const value = (sel) => {
        const el = T.$(sel);
        return el ? el.value : null;
      };
      const seen = {
        title: value(S().title.selectors), description: value(S().description.selectors),
        price: value(S().price.selectors), photos: String(T.$$(S().photos.thumbs).length),
        photos_loaded: String(T.loaded(S().photos.thumbs)), package: chosenPackage(), ...st.picked,
      };
      // The form's own values (a multi-select's input lists its choices, comma-separated).
      const list = (name) => (value(S()[name].selectors) || "").split(",").map((x) => x.trim()).filter(Boolean);
      for (const name of ["size", "condition", "skirt_length"]) if (T.$(S()[name]?.selectors)) seen[name] = list(name).slice(0, 1);
      for (const name of ["color", "material"]) if (T.$(S()[name].selectors)) seen[name] = list(name);
      if (T.$(S().brand.selectors)) seen.brand_shown = value(S().brand.selectors);
      seen.brand_choice = st.brandChoice ?? null;         // WO36: what the brand step picked ("" = without a brand)
      seen.left = st.left;                                 // WO36 §3: optional values the form didn't offer
      // The category input shows the leaf's name: our path only when it is that name.
      const cat = value(S().category.selectors) || "";
      const leaf = String(job.fields.category_path || "").split(" > ").pop();
      seen.category = cat ? [T.norm(cat) === T.norm(leaf) ? job.fields.category_path : cat] : [];
      return seen;
    },

    submitButtons() {
      const re = S().submit.text ? new RegExp(S().submit.text, "i") : null;
      return T.$$(S().submit.selectors).filter((b) => T.visible(b) && (!re || re.test(T.norm(b.textContent))));
    },

    async submit(job, st) {
      const buttons = this.submitButtons();
      if (buttons.length !== 1) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "submit", page: "form",
                        message: `${buttons.length} Upload buttons: never a guess` });
      }
      T.emit({ event: "step", job_id: job.job_id, name: "submit", ok: true, clicked: true });   // before: it navigates
      await T.click(buttons[0]);                                        // exactly once
    },

    async verify(job) {
      const text = (sel) => (T.$(sel)?.textContent || "").replace(/\s+/g, " ").trim();
      // the title's TEXT, not just its element: right after the page opens it can be there and still empty (live,
      // 2026-10-11: the first check of two live listings read "no title")
      await T.until(() => text(S().listing_title.selectors) || null, 10000);
      return { url: location.href, title: text(S().listing_title.selectors), price: text(S().listing_price.selectors),
               photos: T.$$(S().listing_photos.selectors).length, body: T.textOf().slice(0, 3000),
               first_photo: T.$(S().listing_photos.selectors)?.getAttribute("src") || null };   // WO37
    },

    // WO33, recorded from the first supervised publish (2026-10-08): Upload lands on the member page with an "Item
    // listed" dialog — "List another" [data-testid=list-promotion-submit-cta] and "Later"
    // [data-testid=list-promotion-cancel-cta]. Only Later is pressed (its id AND its text); List another and the page's
    // Bump buttons never are. No dialog within 6 s: nothing pressed.
    async landed(job, st) {
      const step = S().listed_later;
      const re = new RegExp(step.text, "i");
      const later = await T.until(() => T.$$(step.selectors).find((b) => T.visible(b) && re.test(T.norm(b.textContent))),
                                  6000);
      let did = "no Item listed dialog";
      if (later) {
        await T.click(later);
        const gone = await T.until(() => !later.isConnected || !T.visible(later), 4000);
        T.emit({ event: "step", job_id: job.job_id, name: "listed_later", ok: !!gone, clicked: true });
        did = gone ? "Item listed: Later" : "Item listed: Later pressed, the dialog still showing";
      }
      // WO35: the new listing by its address — Vinted's own JSON of the seller's newest items, the exact title.
      const member = (location.pathname.match(/\/member\/(\d+)/) || [])[1] || job.shop;
      const want = job.copy?.title;
      const end = Date.now() + 20000 * T.timeoutScale;
      while (member && want) {
        const items = await wardrobe(member);
        const mine = items.filter((x) => T.sameTitle(x.title, want));
        if (mine.length) return { url: mine[0].url, how: `${did}; the wardrobe's newest items`, listings: mine };
        if (Date.now() > end) break;
        await T.sleep(4000);
      }
      return did;
    },

    async delist(job, st) {
      // The listing this agent created: its Hide, and the confirmation if one shows. Never Delete (WO32 §4).
      const re = new RegExp(S().hide.text, "i");
      const findHide = () => T.$$(S().hide.selectors).find((b) => T.visible(b) && re.test(T.norm(b.textContent)));
      // WO40 (the UGG, 2026-10-16: "no Hide button" twice): the seller's buttons are drawn after the page script
      // starts — waited for, never read once. A Hide already pressed before a reload (job.pressed) is never pressed
      // again: its page only says whether it took.
      const unhide = () => T.$$("button").find((b) => T.visible(b) && /^unhide$/i.test(T.norm(b.textContent)));
      const hide = await T.until(() => findHide() || unhide(), 15000);
      if (hide && !findHide()) {
        await T.screenshot("delisted");
        return T.emit({ event: "result", job_id: job.job_id, delisted: true, hidden_already: true, url: location.href });
      }
      if (job.pressed) {
        await T.screenshot("delisted");
        if (hide) {
          return T.emit({ event: "error", job_id: job.job_id, stage: "hide", page: "listing",
                          message: "Hide pressed, the page reloaded, and it still offers Hide: not hidden" });
        }
        // Hidden only when it is still our listing's seller page (its Edit listing there) without Hide
        if (!T.$$("[data-testid='item-edit-button']").some((b) => T.visible(b))) {
          return T.emit({ event: "error", job_id: job.job_id, stage: "hide", page: "listing",
                          message: "Hide pressed, the page reloaded, and it isn't the listing's own page" });
        }
        return T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
      }
      if (!hide) {
        await T.screenshot("no-hide");
        return T.emit({ event: "error", job_id: job.job_id, stage: "hide", page: "form", message: "no Hide button" });
      }
      T.emit({ event: "step", job_id: job.job_id, name: "hide", ok: true, clicked: true });
      await T.click(hide);
      const cre = new RegExp(S().confirm.text, "i");
      const confirm = await T.until(() => T.$$(S().confirm.selectors)
        .find((b) => T.visible(b) && cre.test(T.norm(b.textContent))), 4000);
      if (confirm) {
        await T.click(confirm);
        await T.until(() => !confirm.isConnected, 4000);
      }
      // WO33: hidden only when the Hide button has gone (the page then offers to show it again) — a confirmation we
      // don't recognise leaves it there: an error, tried again, never a false "done".
      const gone = await T.until(() => !T.$$(S().hide.selectors)
        .some((b) => b.isConnected && T.visible(b) && re.test(T.norm(b.textContent))), 8000);
      await T.screenshot("delisted");
      if (!gone) {
        return T.emit({ event: "error", job_id: job.job_id, stage: "hide", page: "listing",
                        message: "Hide pressed, but the listing still offers Hide: not hidden" });
      }
      T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
    },

    // WO33: the take-down control (Hide) looked for on our listing's page — never clicked; the page and its picture
    // are the evidence the selector is recorded from.
    async probe(job, st) {
      const re = new RegExp(S().hide.text, "i");
      const button = await T.until(() => T.$$(S().hide.selectors).find((b) => T.visible(b) && re.test(T.norm(b.textContent))),
                                   6000);
      await T.screenshot("probe");
      T.emit({ event: "result", job_id: job.job_id, probe: true, found: !!button, url: location.href,
               text: button ? button.textContent.replace(/\s+/g, " ").trim() : null });
    },

    // WO38: the sold orders from Vinted's own JSON (same origin, the page's cookies), read only — each order's
    // transaction id, title, price, date and status sentence; nothing about the buyer leaves the page.
    async orders(job) {
      const path = String(T.selectors.vinted.orders?.json || "/api/v2/my_orders?type=sold&status=all&page=1&per_page=20");
      const r = await fetch(new URL(path, location.origin).href, { credentials: "include",
                                                                   headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`Vinted's sold orders answered ${r.status}`);
      const data = await r.json();
      const orders = (data.my_orders || []).filter((o) => o && o.transaction_id).map((o) => ({
        order_id: String(o.transaction_id), title: String(o.title || "").slice(0, 200) || null,
        price: o.price && o.price.amount != null ? String(o.price.amount) : null, sold_at: o.date || null,
        status_text: String(o.status || "").slice(0, 120) || null,
      }));
      return { source: "json", orders };
    },

    async find(job, st) {
      // WO35: Vinted's own JSON of the seller's newest items (same origin, from the member page): each listing's exact
      // title and address — the confirmation after an upload. The tiles below only when the JSON can't be read.
      const member = (location.pathname.match(/\/member\/(\d+)/) || [])[1] || job.shop;
      if (job.all) {                                    // WO45 follow-up: every listing's price, state, photo count
        if (!member) return T.emit({ event: "error", job_id: job.job_id, stage: "find", page: "unknown",
                                     message: "no member page to read Vinted's data from" });
        try {
          const all = await wardrobeAll(member);
          return T.emit({ event: "result", job_id: job.job_id, listings: all.items, shape: all.shape,
                          source: "wardrobe", url: location.href });
        } catch (e) {
          return T.emit({ event: "error", job_id: job.job_id, stage: "find", page: "unknown",
                          message: String(e && e.message ? e.message : e).slice(0, 200) });
        }
      }
      if (job.item_id) {                                // WO41: one listing's price and state, by its id
        if (!member) return T.emit({ event: "error", job_id: job.job_id, stage: "find", page: "unknown",
                                     message: "no member page to read Vinted's data from" });
        try {
          const item = await wardrobeItem(member, job.item_id);
          return T.emit({ event: "result", job_id: job.job_id, item, source: "wardrobe", url: location.href });
        } catch (e) {
          return T.emit({ event: "error", job_id: job.job_id, stage: "find", page: "unknown",
                          message: String(e && e.message ? e.message : e).slice(0, 200) });
        }
      }
      const items = member ? await wardrobe(member) : [];
      if (items.length) return T.emit({ event: "result", job_id: job.job_id, listings: items, source: "wardrobe",
                                        url: location.href });
      // The seller's own member page: its listings' addresses and titles, for the check after an interrupted upload.
      // The member page draws its tiles after it loads (a React app): wait for them, then for the grid to settle.
      await T.until(() => T.$$(S().shop_links.selectors).length > 0, 20000);
      await T.quiet(800, 5000);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      await T.screenshot("shop");
      T.emit({ event: "result", job_id: job.job_id, listings, url: location.href });
    },
  };
})();
