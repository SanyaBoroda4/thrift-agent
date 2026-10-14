// The idle page of a site's window (WO33): which site the window is for. WO38: the "Orders" window, where the sold
// orders of Depop and Vinted are read (read only), apart from the listing windows.
const NAMES = { vinted: "Vinted", depop: "Depop", orders: "Orders" };
const site = new URLSearchParams(location.search).get("site") || "";
const name = NAMES[site] || "Thrift";
document.getElementById("title").textContent = site === "orders" ? "Orders — Thrift (read only)"
  : `${name} — Thrift listing window`;
if (site === "orders") {
  document.getElementById("what").textContent = "The Mac reads your sold orders here (Depop, Vinted), a few times a "
    + "day — read only: it never clicks anything on these pages. Leave it open: behind other windows is fine, but "
    + "don't minimize it.";
}
document.title = `${name} — Thrift`;
