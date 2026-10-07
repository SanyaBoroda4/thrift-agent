// Thrift crosslister (WO32): Vinted's upload form, in its order — photos, title, description, category (the leaf by its
// id, else the tree walked by its path), brand (its own list, exact or the clean form only), size, condition, colours
// (≤ 2), materials (≤ 3), the skirt length Skirts require, price, the package size radio. Promotion offers are closed,
// never accepted. Selectors: selectors.json → vinted (UNVERIFIED until a dry run on the Mac recorded them).
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
        await T.click(rows[i]);
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

  async function pick(st, name, value) {
    await open(name);
    const shown = await clickRow(value);
    T.picked(st, name, value, shown);
    escape();
  }

  T.sites.vinted = {
    async fill(job, files, st) {
      const f = job.fields;
      await T.dismiss(S().promo_close.selectors);
      await T.step("photos", st, async () => {         // the file input is hidden (u-hidden): found, not "visible"
        const input = await T.need(S().photos.selectors, "photos", { visible: false });
        st.attached = await T.attach(input, files.slice(0, 20), S().photos.drop);
        await T.waitCount(S().photos.thumbs, Math.min(files.length, 20), 90000);
        await T.screenshot("photos");
      });
      await T.step("title", st, async () => T.type(await T.need(S().title.selectors, "title"), job.copy.title));
      await T.step("description", st, async () =>
        T.type(await T.need(S().description.selectors, "description"), job.copy.description));
      await T.step("category", st, async () => {
        await open("category");
        const leaf = T.$(S().category.leaf.map((s) => s.replace("{id}", f.category_id)));
        if (leaf && T.visible(leaf)) {
          await T.click(leaf);
        } else {
          for (const part of f.category_path.split(" > ")) await clickRow(part);
        }
        T.picked(st, "category", f.category_path, String(f.category_id));
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
      await T.step("package", st, async () => {
        const radios = T.$$(S().package.selectors);
        const labelOf = (r) => {
          const l = r.id && [...document.querySelectorAll("label")].find((x) => x.htmlFor === r.id);
          return T.norm((l || r.closest("label") || r.parentElement || {}).textContent || "");
        };
        for (const code of f.package_sizes || []) {
          const want = PACKAGE[code];
          const r = radios.find((x) => labelOf(x).startsWith(want) && !(code === "SMALL" && labelOf(x).startsWith("extra")));
          if (r) {
            await T.click(r);
            st.package = code;
            return;
          }
        }
        throw new Error(`package ${f.package_sizes} not offered (${radios.map(labelOf).join(", ")})`);
      });
      await T.dismiss(S().promo_close.selectors);
    },

    readBack(job, st) {
      const value = (sel) => {
        const el = T.$(sel);
        return el ? el.value : null;
      };
      return {
        title: value(S().title.selectors), description: value(S().description.selectors),
        price: value(S().price.selectors), photos: String(T.$$(S().photos.thumbs).length),
        package: st.package || null, ...st.picked,
      };
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
