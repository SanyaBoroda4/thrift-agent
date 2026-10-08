// The idle page of a site's window (WO33): which site the window is for.
const NAMES = { vinted: "Vinted", depop: "Depop" };
const site = new URLSearchParams(location.search).get("site") || "";
const name = NAMES[site] || "Thrift";
document.getElementById("title").textContent = `${name} — Thrift listing window`;
document.title = `${name} — Thrift`;
