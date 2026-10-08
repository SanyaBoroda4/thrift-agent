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

  async function pick(st, name, value) {
    const input = await open(name);
    const shown = await clickRow(value);
    T.picked(st, name, value, shown);
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
      if (f.brand) {
        await T.step("brand", st, async () => {
          await open("brand");
          const search = await T.waitFor(S().brand.search, { timeout: 3000 });
          await T.type(search || (await T.need(S().brand.selectors, "brand")), f.brand);
          let rows = [], texts = [];
          const { choice, guess } = (await T.until(() => {     // Vinted's list answers: until our brand shows, 5 s
            rows = T.$$(S().rows.selectors).filter(T.visible);
            texts = rows.map(rowText);
            const p = T.strictPick(f.brand, texts);
            return p.choice ? p : null;
          }, 5000)) || { choice: null, guess: null };
          if (!choice) {
            await T.screenshot("menu-brand");
            st.guesses.push(`brand left empty (Vinted has no '${f.brand}')`);
            escape();
            return;
          }
          await T.click(rows[texts.indexOf(choice)]);
          T.picked(st, "brand", f.brand, choice);
          if (guess) st.guesses.push(guess);
          escape();
        });
      }
      if (f.size) await T.step("size", st, async () => pick(st, "size", f.size));
      await T.step("condition", st, async () => pick(st, "condition", f.condition));
      for (const c of (f.colors || []).slice(0, 2)) await T.step("color", st, async () => pick(st, "color", c));
      for (const m of (f.materials || []).slice(0, 3)) await T.step("material", st, async () => pick(st, "material", m));
      if (f.skirt_length) await T.step("skirt_length", st, async () => pick(st, "skirt_length", f.skirt_length));
      await T.step("price", st, async () => T.type(await T.need(S().price.selectors, "price"), String(job.price)));
      await T.step("package", st, async () => {         // the first size of ours this item's category offers
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
      });
      await T.dismiss(S().promo_close.selectors);
      await closeAll(st);
      st.photosLoadedEnd = await T.waitLoaded(S().photos.thumbs, Math.min(files.length, 20), 15000);
      await T.screenshot("photos-end");
      T.$(S().details_view?.selectors)?.scrollIntoView?.({ block: "start" });   // the form's screenshot: the details
      await T.sleep(500);
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
      await T.waitFor(S().listing_title.selectors, { timeout: 10000 });
      const text = (sel) => (T.$(sel)?.textContent || "").replace(/\s+/g, " ").trim();
      return { url: location.href, title: text(S().listing_title.selectors), price: text(S().listing_price.selectors),
               photos: T.$$(S().listing_photos.selectors).length, body: T.textOf().slice(0, 3000) };
    },

    async delist(job, st) {
      // The listing this agent created: its Hide, and the confirmation if one shows. Never Delete (WO32 §4).
      const re = new RegExp(S().hide.text, "i");
      const hide = T.$$(S().hide.selectors).find((b) => T.visible(b) && re.test(T.norm(b.textContent)));
      if (!hide) {
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
      await T.screenshot("delisted");
      T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
    },

    async find(job, st) {
      // The seller's own member page: its listings' addresses and titles, for the check after an interrupted upload.
      await T.waitFor(S().shop_links.selectors, { timeout: 8000, visible: false });
      await T.sleep(150);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      T.emit({ event: "result", job_id: job.job_id, listings });
    },
  };
})();
