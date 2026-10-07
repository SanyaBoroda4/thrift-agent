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
    await T.sleep(350);
    return input;
  }

  // A row's own words: its title cell when it has one (Vinted's rows carry a title and sometimes a description).
  const rowText = (r) => (T.$(S().rows.title, r) || r).textContent.replace(/\s+/g, " ").trim();

  async function clickRow(text, timeout = 8000) {
    const end = Date.now() + timeout * T.timeoutScale;
    for (;;) {
      const rows = T.$$(S().rows.selectors).filter(T.visible);
      const texts = rows.map(rowText);
      const i = texts.findIndex((t) => T.norm(t) === T.norm(text) || T.norm(t.split(" | ")[0]) === T.norm(text));
      if (i >= 0) {
        if (rows[i].getAttribute("aria-checked") !== "true") await T.click(rows[i]);   // chosen already: leave it
        await T.sleep(300);
        return texts[i];
      }
      if (Date.now() > end) {
        await T.screenshot(`menu-${T.norm(text).replace(/[^a-z0-9]+/g, "-").slice(0, 30)}`);   // the open list
        throw new Error(`no '${text}' (offered: ${texts.slice(0, 8).join(", ")})`);
      }
      await new Promise((r) => setTimeout(r, 150));
    }
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

  // A multi-select's panel stays open after a pick and Escape doesn't close it (recorded 2026-10-06: the material panel
  // still open over the price): its own input closes it — so no panel is left open over the Upload button.
  async function close(input) {
    escape();
    await T.sleep(250);
    if (T.$$(S().rows.selectors).some(T.visible)) {
      await T.click(input);
      await T.sleep(300);
    }
  }

  T.sites.vinted = {
    async fill(job, files, st) {
      const f = job.fields;
      await T.dismiss(S().promo_close.selectors);
      await T.step("photos", st, async () => {         // the file input is hidden (u-hidden): found, not "visible"
        const input = await T.need(S().photos.selectors, "photos", { visible: false });
        st.attached = await T.attach(input, files.slice(0, 20), S().photos.drop);
        await T.waitCount(S().photos.thumbs, Math.min(files.length, 20), 90000);
        st.photosLoaded = await T.waitLoaded(S().photos.thumbs, Math.min(files.length, 20), 30000);
        await T.screenshot("photos");
      });
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
        await T.sleep(800);
        T.picked(st, "category", f.category_path, T.$(S().category.selectors)?.value || "");
        escape();
      });
      if (f.brand) {
        await T.step("brand", st, async () => {
          await open("brand");
          const search = await T.waitFor(S().brand.search, { timeout: 3000 });
          await T.type(search || (await T.need(S().brand.selectors, "brand")), f.brand);
          await T.sleep(900);
          const rows = T.$$(S().rows.selectors).filter(T.visible);
          const texts = rows.map(rowText);
          const { choice, guess } = T.strictPick(f.brand, texts);
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
          await T.sleep(400);
          if (radio && !radio.checked) throw new Error(`the package size ${PACKAGE[code]} didn't take`);
          return;
        }
        throw new Error(`package ${f.package_sizes} not offered (${cells.map(cellName).join(", ")})`);
      });
      await T.dismiss(S().promo_close.selectors);
      st.photosLoadedEnd = await T.waitLoaded(S().photos.thumbs, Math.min(files.length, 20), 20000);
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
      await T.sleep(1500);
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
      await T.sleep(1200);
      const cre = new RegExp(S().confirm.text, "i");
      const confirm = T.$$(S().confirm.selectors).find((b) => T.visible(b) && cre.test(T.norm(b.textContent)));
      if (confirm) await T.click(confirm);
      await T.sleep(1500);
      await T.screenshot("delisted");
      T.emit({ event: "result", job_id: job.job_id, delisted: true, url: location.href });
    },

    async find(job, st) {
      // The seller's own member page: its listings' addresses and titles, for the check after an interrupted upload.
      await T.sleep(1500);
      const listings = T.$$(S().shop_links.selectors).map((a) => ({
        url: a.href, text: (a.getAttribute("title") || a.textContent || "").replace(/\s+/g, " ").trim(),
      }));
      T.emit({ event: "result", job_id: job.job_id, listings });
    },
  };
})();
