"""The group's card as the tests expect it (WO34): "✅ <short name> · $<price>", then each site on its own line — its
name the link, "⏳ <Site> — later" while it hasn't posted — an empty line between, and the 💤 line last."""
DOT = {"poshmark": "🟣", "depop": "🔴", "vinted": "🟢"}
LABEL = {"poshmark": "Poshmark", "depop": "Depop", "vinted": "Vinted"}
ALL_DONE = "💤 All done — you can close the Mac."


def card(name: str, price, sites: list[tuple[str, str | None]], last: str | None = None) -> str:
    lines = [f"✅ {name} · ${price}"]
    for mp, url in sites:
        lines.append(f'{DOT[mp]} <a href="{url}">{LABEL[mp]}</a>' if url else f"⏳ {LABEL[mp]} — later")
    if last:
        lines.append(last)
    return "\n\n".join(lines)


def done_for_now(*sites: str) -> str:
    names = [LABEL[s] for s in sites]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"💤 Done for now — you can close the Mac. {joined} catch{'es' if len(names) == 1 else ''} up next time."
