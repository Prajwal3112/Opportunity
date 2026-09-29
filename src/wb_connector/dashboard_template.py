"""Markup for the dashboard.

**A register, not a feed.** Public procurement runs on ruled lists of reference numbers,
buyers and closing dates, and the reader scans one rather than browsing it. So rows are
separated by rules, not boxed into cards with a uniform radius and shadow -- forty
identical cards read as forty equally important objects, which is the opposite of the
point.

**Width is used structurally, not by stretching text.** A single column left most of a
desktop screen empty, and widening the text would push line length past readability. So
the page is a fixed control rail beside a wide register: the controls stop consuming the
vertical space above the tenders, the tenders get the whole width, and a subject line is
still capped near 62 characters wherever it sits.

**Two tones.** The rail is dark and the register is light, and that split is itself the
information: one side is what you are looking for, the other is what was found. Teal marks
your own terms and anything clickable; amber is reserved for time running out and appears
nowhere else.

**Time is the structure.** Everything in procurement orbits a closing date, so tenders
group under closing horizons and the heading carries the urgency. The group answers "what
must I act on this week" without the reader doing arithmetic, which keeps each row quiet.

**Two faces, one job each.** *Atkinson Hyperlegible* carries every data-bearing element:
it was designed to disambiguate glyphs that collide (1/l/I, 0/O, rn/m), and this screen is
dense with codes like SZ-MOET-556321-GO-RFB where a misread is a wasted call. *Newsreader*
sets the masthead, the horizon counts and the tender subject -- the only human-written
prose here, and the only line a reader actually reads rather than scans.

**No score anywhere.** An earlier version led with match confidence and was rejected: a
score exists to triage volume, and at a couple of dozen open tenders the list is short
enough to read end to end. The engine still runs, and is what keeps Siem Reap, Defect
Liability Period and "prices soar" out of the list. It is simply invisible.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote as urlquote

from opportunity_engine.matching.textclean import first_sentence, strip_html

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Tender register</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500&display=swap">
<style>
:root{{
  /* Bone, not cream. The warm off-white that has become the default ground for generated
     pages is avoided on purpose; this one is faintly cool so the teal sits cleanly on it. */
  --bone:#FAFAF8; --sheet:#FFFFFF;
  --ink:#16191C; --ink-2:#4B5257; --ink-3:#5F666C;
  --rule:#E2E3DF; --rule-2:#EEEFEB;
  /* The rail. Dark enough to read as a different surface, never pure black. */
  --rail:#191D20; --rail-2:#23282C; --rail-ink:#EDEEEA; --rail-ink-2:#A6AEB4;
  --rail-rule:#2E3438;
  /* Teal marks your terms and anything clickable. Amber means time, and nothing else. */
  --mark:#0F6F72; --mark-lift:#6FC0C3;
  --amber:#96570C; --amber-soft:#F7ECDD; --amber-rail:#D99A4A;
  --good:#2F6B4A;
  --focus:#0F6F72;
}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
  --bone:#111416; --sheet:#171B1D;
  --ink:#ECEDE9; --ink-2:#B0B7BB; --ink-3:#949BA1;
  --rule:#272C2F; --rule-2:#1E2325;
  --rail:#0C0F10; --rail-2:#161A1C; --rail-ink:#ECEDE9; --rail-ink-2:#9EA6AC;
  --rail-rule:#232829;
  --mark:#6FC0C3; --mark-lift:#8AD2D4;
  --amber:#D99A4A; --amber-soft:#2A1F12; --amber-rail:#D99A4A;
  --good:#6FAF8B;
  --focus:#6FC0C3;
  color-scheme:dark;
}}}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{margin:0;background:var(--bone);color:var(--ink);
  font-family:"Atkinson Hyperlegible",system-ui,sans-serif;font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased}}
:focus-visible{{outline:2px solid var(--focus);outline-offset:2px;border-radius:2px}}
a{{color:var(--mark)}}

/* ---------------------------------------------------------------- masthead */
.masthead{{display:flex;align-items:baseline;justify-content:space-between;gap:.75rem 1.5rem;
  flex-wrap:wrap;background:var(--rail);color:var(--rail-ink);
  padding:calc(1rem + env(safe-area-inset-top,0px)) 1.5rem 1rem;
  border-bottom:1px solid var(--rail-rule)}}
h1{{font-family:Newsreader,Georgia,serif;font-weight:300;font-size:1.5rem;margin:0;
  letter-spacing:.005em;line-height:1.1}}
.head-right{{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap}}
.tally{{margin:0;font-size:.8125rem;color:var(--rail-ink-2);font-variant-numeric:tabular-nums}}
.tally b{{color:var(--rail-ink);font-weight:700}}
.refresh{{font:inherit;font-size:.75rem;font-weight:700;cursor:pointer;color:var(--rail-ink);
  background:transparent;border:1px solid var(--rail-rule);border-radius:.125rem;
  padding:.3125rem .6875rem;min-height:1.875rem;
  transition:border-color .16s ease,color .16s ease}}
.refresh:hover{{border-color:var(--mark-lift);color:var(--mark-lift)}}

/* ------------------------------------------------------------------ shell */
.shell{{display:grid;grid-template-columns:minmax(0,1fr);align-items:start}}
@media (min-width:64rem){{
  .shell{{grid-template-columns:19rem minmax(0,1fr)}}
  /* A fixed viewport height, not max-height. With max-height the rail collapsed to its
     own contents, so clearing the filters left a short dark stub floating above the
     page background instead of a column. `height` makes it hold the screen whether the
     controls are two chips or two hundred, and the overflow keeps the tall case usable. */
  .rail{{position:sticky;top:0;height:100vh;overflow-y:auto}}
}}

/* ------------------------------------------------------------------- rail */
.rail{{background:var(--rail);color:var(--rail-ink);padding:1.25rem 1.5rem 2rem;
  border-right:1px solid var(--rail-rule)}}
.group + .group{{margin-top:1.375rem;padding-top:1.375rem;border-top:1px solid var(--rail-rule)}}
.group > h2{{font-family:inherit;font-size:.75rem;font-weight:700;margin:0 0 .5625rem;
  color:var(--rail-ink-2)}}
.terms{{display:flex;flex-wrap:wrap;gap:.3125rem;align-items:center}}
.term{{display:inline-flex;align-items:center;gap:.1875rem;border-radius:.125rem;
  padding:.1875rem .25rem .1875rem .4375rem;font-size:.8125rem;font-weight:700;
  background:var(--rail-2);color:var(--mark-lift)}}
.term.negative{{color:var(--rail-ink-2);font-weight:400}}
.term.product{{background:transparent;box-shadow:inset 0 0 0 1px var(--rail-rule);
  color:var(--rail-ink-2);font-weight:400}}
.term.market{{background:transparent;box-shadow:inset 0 0 0 1px var(--mark);
  color:var(--mark-lift);font-weight:400}}
.term form{{display:inline-flex}}
.term button{{background:none;border:0;padding:0 .125rem;cursor:pointer;color:inherit;
  opacity:.6;font-size:.9375rem;line-height:1;font-family:inherit;min-height:1.375rem}}
.term button:hover{{opacity:1}}
.entry-form{{display:flex;gap:.3125rem;margin-top:.5rem;width:100%}}
input[type=text]{{font:inherit;font-size:.8125rem;flex:1 1 auto;min-width:0;
  border:1px solid var(--rail-rule);border-radius:.125rem;padding:.375rem .5rem;
  background:var(--rail-2);color:var(--rail-ink);min-height:2rem}}
input[type=text]::placeholder{{color:var(--rail-ink-2)}}
.add{{font:inherit;font-size:.8125rem;font-weight:700;cursor:pointer;flex:none;
  border:1px solid var(--rail-rule);background:transparent;color:var(--rail-ink-2);
  border-radius:.125rem;padding:.375rem .625rem;min-height:2rem;
  transition:border-color .16s ease,color .16s ease}}
.add:hover{{border-color:var(--mark-lift);color:var(--mark-lift)}}
.hint{{font-size:.75rem;color:var(--rail-ink-2);margin:.5rem 0 0;line-height:1.55}}
.shipped{{flex-basis:100%;margin-top:.25rem}}
.shipped summary{{font-size:.75rem;color:var(--rail-ink-2)}}
.shipped summary:hover{{color:var(--mark-lift)}}
.shipped-list{{font-size:.75rem;color:var(--rail-ink-2);line-height:1.7;margin:.4375rem 0 0}}
.switch form{{display:inline}}
.switch button{{font:inherit;font-size:.8125rem;cursor:pointer;background:none;border:0;
  color:var(--rail-ink);padding:.1875rem 0;display:inline-flex;align-items:flex-start;
  gap:.5rem;min-height:1.75rem;text-align:left}}
.box{{width:.875rem;height:.875rem;border:1px solid var(--rail-rule);border-radius:.125rem;
  display:inline-block;position:relative;background:var(--rail-2);flex:none;margin-top:.1875rem}}
.box.on{{background:var(--mark);border-color:var(--mark)}}
.box.on::after{{content:"";position:absolute;left:.1875rem;top:.0625rem;width:.25rem;
  height:.4375rem;border:solid #fff;border-width:0 1.5px 1.5px 0;transform:rotate(40deg)}}
.switch button:hover{{color:var(--mark-lift)}}
/* Corpus reach. A zero here teaches more than documentation could. */
.reach{{display:grid;gap:.125rem;font-size:.75rem;font-variant-numeric:tabular-nums}}
.reach > span{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:.625rem;
  color:var(--rail-ink-2);padding:.1875rem 0;border-bottom:1px solid var(--rail-rule)}}
.reach b{{font-weight:400;color:var(--rail-ink);overflow-wrap:anywhere}}
.reach .none,.reach .none b{{color:var(--amber-rail)}}

/* --------------------------------------------------------------- register */
.register{{padding:1.5rem 1.5rem calc(4rem + env(safe-area-inset-bottom,0px));min-width:0}}
.notice{{background:var(--amber-soft);color:var(--amber);border-left:3px solid var(--amber);
  border-radius:.125rem;padding:.625rem .875rem;font-size:.8125rem;margin-bottom:.625rem;
  max-width:80ch}}

/* The horizon divider is the loud element. The count is the number that matters. */
.horizon{{margin-top:2.5rem}}
.horizon:first-of-type{{margin-top:0}}
.horizon h2{{display:flex;align-items:baseline;gap:.75rem;margin:0 0 .375rem;
  font-size:.875rem;font-weight:700;color:var(--ink)}}
.horizon h2 .count{{font-family:Newsreader,Georgia,serif;font-weight:300;font-size:2rem;
  line-height:.85;color:var(--ink-3);font-variant-numeric:tabular-nums}}
.horizon h2::after{{content:"";flex:1;height:1px;background:var(--rule);
  transform:translateY(-.25em)}}
.horizon.soon h2,.horizon.soon h2 .count{{color:var(--amber)}}
.horizon.soon h2::after{{background:var(--amber)}}
.gloss{{font-size:.75rem;color:var(--ink-3);margin:.25rem 0 .75rem;max-width:64ch}}

/* One tender: a ruled register line. Width gives the date its own column instead of
   burying it in a sentence. */
.tender{{display:grid;grid-template-columns:minmax(0,1fr);gap:.1875rem .5rem;
  padding:.9375rem 0;border-bottom:1px solid var(--rule-2)}}
@media (min-width:52rem){{
  .tender{{grid-template-columns:minmax(0,1fr) 11.5rem;align-items:start;column-gap:1.5rem}}
  /* Everything except the ledger stays in column one. Without this, auto-placement fills
     row by row, so once the ledger stops reserving column two the next child -- the
     actions, then the disclosures -- drops into an 11.5rem gutter. */
  .tender > *{{grid-column:1}}
  .tender > .ledger{{grid-column:2;grid-row:1 / span 3;text-align:right}}
}}
.tender.soon{{border-left:2px solid var(--amber);padding-left:.875rem;margin-left:-.875rem}}
.tender.dropped{{opacity:.42}}
.subject{{font-family:Newsreader,Georgia,serif;font-size:1.1875rem;font-weight:400;
  line-height:1.3;margin:0;color:var(--ink);max-width:62ch}}
.fresh{{display:inline-block;width:.4375rem;height:.4375rem;border-radius:50%;
  background:var(--mark);vertical-align:.3em;margin-right:.4375rem}}
.where{{font-size:.8125rem;color:var(--ink-3);margin:0}}
.where .place{{color:var(--ink-2);font-weight:700}}
.quote{{font-size:.8125rem;color:var(--ink-3);margin:0;font-style:italic}}
/* Right-hand column: when it closes, and its reference. */
.ledger{{font-size:.8125rem;line-height:1.45}}
.when{{display:block;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}}
.soon .when{{color:var(--amber)}}
.ref{{display:block;color:var(--ink-3);font-variant-numeric:tabular-nums;
  overflow-wrap:anywhere;margin-top:.0625rem}}
.entry{{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .875rem;
  font-size:.8125rem;margin-top:.1875rem}}
.entry form{{display:inline}}
.entry a{{text-decoration:underline;text-underline-offset:.15em;text-decoration-thickness:1px}}
.entry a:hover{{text-decoration-thickness:2px}}
.chasing{{color:var(--good);font-weight:700}}
.entry button{{font:inherit;font-size:.8125rem;cursor:pointer;background:none;border:0;
  color:var(--ink-3);padding:.1875rem 0;text-decoration:underline;
  text-underline-offset:.15em;text-decoration-color:var(--rule);min-height:1.625rem}}
.entry button:hover{{color:var(--ink)}}

details{{margin-top:.375rem}}
summary{{font-size:.8125rem;color:var(--ink-3);cursor:pointer;padding:.1875rem 0;
  min-height:1.625rem}}
summary:hover{{color:var(--mark)}}
.full{{font-size:.8125rem;line-height:1.65;color:var(--ink-2);white-space:pre-wrap;
  max-height:24rem;overflow:auto;margin:.4375rem 0 0;padding:.875rem;
  background:var(--sheet);border:1px solid var(--rule-2);border-radius:.125rem;
  max-width:90ch}}
.papers{{list-style:none;margin:.4375rem 0 0;padding:0 0 0 .8125rem;
  border-left:2px solid var(--rule)}}
.papers li{{padding:.3125rem 0;font-size:.8125rem;line-height:1.4}}
.papers li + li{{border-top:1px solid var(--rule-2)}}
.papers a{{text-decoration:underline;text-underline-offset:.15em}}
.papers .meta{{color:var(--ink-3);font-size:.75rem;display:block;margin-top:.0625rem;
  font-variant-numeric:tabular-nums}}
.papers .scan{{color:var(--amber)}}
.no-papers{{font-size:.8125rem;color:var(--ink-3);margin:.4375rem 0 0;max-width:64ch}}

.blank{{border:1px solid var(--rule);border-radius:.125rem;background:var(--sheet);
  padding:1.75rem;font-size:.875rem;color:var(--ink-2);line-height:1.65;max-width:58ch}}
.blank strong{{display:block;color:var(--ink);font-family:Newsreader,Georgia,serif;
  font-weight:400;font-size:1.25rem;margin-bottom:.5rem}}
code{{font-family:ui-monospace,"Cascadia Mono",Menlo,monospace;font-size:.9em;
  background:var(--rule-2);padding:.0625rem .25rem;border-radius:.125rem}}
footer{{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--rule);
  font-size:.75rem;color:var(--ink-3);line-height:1.65;max-width:78ch}}

@media (prefers-reduced-motion:reduce){{
  *{{transition-duration:.01ms !important;animation-duration:.01ms !important}}
}}
</style></head><body>

<header class="masthead">
  <h1>Tender register</h1>
  <div class="head-right">
    <p class="tally">{tally}</p>
    <form method="post" action="/refresh"><button class="refresh" type="submit">Refresh</button></form>
  </div>
</header>

<div class="shell">
<aside class="rail">
  <div class="group">
    <h2><label for="add-term">Searching for</label></h2>
    <div class="terms">{find_terms}</div>
    <form class="entry-form" method="post" action="/add">
      <input type="hidden" name="kind" value="find">
      <input type="text" id="add-term" name="term" placeholder="a word or phrase" autocomplete="off">
      <button class="add" type="submit">Add</button>
    </form>
  </div>

  <div class="group">
    <h2><label for="hide-term">Not interested in</label></h2>
    <div class="terms">{hide_terms}</div>
    <form class="entry-form" method="post" action="/add">
      <input type="hidden" name="kind" value="hide">
      <input type="text" id="hide-term" name="term" placeholder="a word to exclude" autocomplete="off">
      <button class="add" type="submit">Exclude</button>
    </form>
  </div>

  <div class="group">
    <h2><label for="add-product">What we sell</label></h2>
    <div class="terms">{catalogue}</div>
    <form class="entry-form" method="post" action="/catalogue/add">
      <input type="text" id="add-product" name="text" placeholder="a product or module" autocomplete="off">
      <button class="add" type="submit">Add</button>
    </form>
    <p class="hint">Adding a product also starts searching for it.</p>
  </div>

  <div class="group">
    <h2><label for="add-region">Countries and regions</label></h2>
    <div class="terms">{regions}</div>
    <form class="entry-form" method="post" action="/add">
      <input type="hidden" name="kind" value="regions">
      <input type="text" id="add-region" name="term" placeholder="e.g. Kenya, or Africa" autocomplete="off">
      <button class="add" type="submit">Add</button>
    </form>
    <p class="hint">{markets_hint}</p>
  </div>

  <div class="group">{switches}</div>

  <div class="group">
    <h2>How far each word reaches</h2>
    <div class="reach">{reach}</div>
  </div>
</aside>

<main class="register">
{warnings}
{groups}
<footer>{footer}</footer>
</main>
</div>
</body></html>"""


# Closing horizons. The band is the heading, so no row carries a countdown badge.
HORIZONS: list[tuple[str, str, int]] = [
    ("Closing within a week", "soon", 7),
    ("Closing this month", "", 31),
    ("Closing later", "", 10_000),
]
UNKNOWN_BAND = "Closing date not published"
UNKNOWN_GLOSS = ("The date is set out in the bid document. Ask the buyer before you commit "
                 "time — about a quarter of biddable notices publish no date here.")
CLOSED_BAND = "Already closed"


def render_terms(terms: list[str], kind: str) -> str:
    css = "term" if kind == "find" else "term negative"
    return "".join(
        f'<span class="{css}">{_esc(t)}'
        f'<form method="post" action="/remove">'
        f'<input type="hidden" name="kind" value="{kind}">'
        f'<input type="hidden" name="term" value="{_esc(t)}">'
        f'<button type="submit" aria-label="Remove {_esc(t)}">&times;</button>'
        f"</form></span>"
        for t in terms
    )


def render_markets(regions: list[str], available: list[tuple[str, int]]) -> str:
    """The chosen markets, plus a hint naming what is actually on offer.

    The hint matters more than it looks. A business developer knows their own markets but
    not how the World Bank spells them -- a third of these notices are filed under
    regional names like "Eastern and Southern Africa" rather than a country -- so a
    filter typed blind would quietly exclude real work. Listing what the current results
    contain turns guessing into picking.
    """
    chips = "".join(
        f'<span class="term market">{_esc(r)}'
        f'<form method="post" action="/remove">'
        f'<input type="hidden" name="kind" value="regions">'
        f'<input type="hidden" name="term" value="{_esc(r)}">'
        f'<button type="submit" aria-label="Remove {_esc(r)}">&times;</button>'
        f"</form></span>"
        for r in regions
    )
    return chips


def markets_hint(regions: list[str], available: list[tuple[str, int]]) -> str:
    if not available:
        return ("Leave empty to search everywhere. Add a country, or part of one — "
                "&ldquo;Africa&rdquo; also catches &ldquo;Eastern and Southern Africa&rdquo;.")
    listed = ", ".join(f"{_esc(name)} ({count})" for name, count in available[:8])
    more = f" and {len(available) - 8} more" if len(available) > 8 else ""
    lead = "Currently showing" if regions else "Available right now"
    return f"{lead}: {listed}{more}."


def render_catalogue(items: list[Any]) -> str:
    """The product catalogue: own products as chips, the shipped profile as a count.

    The profile carries about ninety phrases. Rendering them all as chips buried the three
    or four the user actually added, and implied all ninety were theirs to edit. So the
    terms this screen created lead, and the rest collapse behind a disclosure -- visible
    and inspectable, but not competing for attention.

    Hand-written terms are shown and not removable: they carry context guards, blocklists
    and case rules a text box cannot express, so a delete button here would quietly
    discard reasoning nobody can see.
    """
    mine = [i for i in items if i.editable]
    shipped = [i for i in items if not i.editable]

    out = [
        f'<span class="term product">{_esc(i.text)}'
        f'<form method="post" action="/catalogue/remove">'
        f'<input type="hidden" name="term_id" value="{_esc(i.id)}">'
        f'<button type="submit" aria-label="Remove {_esc(i.text)}">&times;</button>'
        f"</form></span>"
        for i in mine
    ]
    if shipped:
        listed = ", ".join(_esc(i.text) for i in shipped)
        word = "phrase" if len(shipped) == 1 else "phrases"
        out.append(
            f'<details class="shipped"><summary>{len(shipped)} {word} from the '
            f"cybersecurity profile</summary>"
            f'<p class="shipped-list">{listed}</p></details>'
        )
    return "".join(out)


def render_switches(only_biddable: bool) -> str:
    """One switch, not two.

    There was a second, "Firms only, not individual consultants". It could not change the
    list: ``biddability.assess`` already returns INDIVIDUAL_ONLY for that procurement
    method, and that is not open to a firm, so the biddability filter subsumes it --
    measured live, 262 survivors either way. A control that cannot alter what is shown is
    worse than no control, so it was removed rather than explained.
    """
    pressed = "true" if only_biddable else "false"
    on = " on" if only_biddable else ""
    return (
        f'<span class="switch"><form method="post" action="/toggle">'
        f'<input type="hidden" name="field" value="only_biddable">'
        f'<button type="submit" aria-pressed="{pressed}">'
        f'<span class="box{on}" aria-hidden="true"></span>'
        f"<span>Only what I can bid on</span></button></form></span>"
    )


def render_reach(totals: dict[str, int | None]) -> str:
    """How far each term reaches across the whole corpus.

    Free -- the count arrives in the response envelope -- and the most useful control on
    the page: it says which of the reader's own words are worth having. A zero teaches
    more than documentation could.
    """
    if not totals:
        return ""
    parts = []
    for term, count in sorted(totals.items(), key=lambda kv: -(kv[1] or 0)):
        if count is None:
            parts.append(f'<span class="none"><b>{_esc(term)}</b> unreachable</span>')
        elif count == 0:
            parts.append(f'<span class="none"><b>{_esc(term)}</b> nothing</span>')
        else:
            parts.append(f"<span><b>{_esc(term)}</b> {count:,}</span>")
    return "".join(parts)


def _band(row: Any) -> tuple[int, str, str, str]:
    """(order, heading, css, gloss) for the horizon a tender belongs in."""
    if row.days is None:
        return 3, UNKNOWN_BAND, "", UNKNOWN_GLOSS
    if row.days < 0:
        return 4, CLOSED_BAND, "", ""
    for index, (heading, css, limit) in enumerate(HORIZONS):
        if row.days <= limit:
            return index, heading, css, ""
    return 2, HORIZONS[-1][0], "", ""


def render_groups(rows: list[Any]) -> str:
    buckets: dict[int, tuple[str, str, str, list[Any]]] = {}
    for row in rows:
        order, heading, css, gloss = _band(row)
        buckets.setdefault(order, (heading, css, gloss, []))[3].append(row)
    out = []
    for order in sorted(buckets):
        heading, css, gloss, members = buckets[order]
        note = f'<p class="gloss">{gloss}</p>' if gloss else ""
        body = "".join(_tender(r, css) for r in members)
        out.append(f'<section class="horizon {css}"><h2>{heading}'
                   f'<span class="count">{len(members)}</span></h2>{note}{body}</section>')
    return "".join(out)


def render_rows(rows: list[Any]) -> str:
    """Kept for callers that want a flat list rather than horizon groups."""
    return "".join(_tender(r, "") for r in rows)


def _tender(r: Any, band_css: str) -> str:
    n = r.notice
    # The body arrives already stripped: stripping it on every page load cost more than
    # fetching it did. strip_html stays as a guard for any source that was not stripped.
    body = strip_html(n.notice_text)
    subject = _esc(n.bid_description or "") or _esc(first_sentence(body)) or "Untitled notice"

    if r.days is None:
        when = "Date in the bid document"
    elif r.days < 0:
        when = f"Closed {abs(r.days)} days ago"
    elif r.days == 0:
        when = "Closes today"
    else:
        plural = "s" if r.days != 1 else ""
        when = f"{_day(n.submission_deadline_date)}, {r.days} day{plural} left"

    place = _esc(n.project_country_name) or "Location not stated"
    kind = _esc(n.notice_type) or ""
    quoted = f'<p class="quote">&ldquo;{_esc(r.why)}&rdquo;</p>' if r.why else ""

    entry = []
    if n.contact_email:
        subject_line = f"Enquiry - {n.bid_reference_no or n.external_id}"
        entry.append(f'<a href="mailto:{quote_url(str(n.contact_email))}'
                     f'?subject={quote_url(subject_line)}">Write to the buyer</a>')
    elif n.contact_web_url:
        # 99.9% of bid-eligible notices carry an email, but the handful that do not must
        # still offer a way through rather than an action that goes nowhere.
        entry.append(f'<a href="{_esc(str(n.contact_web_url))}" rel="noreferrer">'
                     "Buyer&rsquo;s page</a>")
    if r.state == "PURSUING":
        entry.append('<span class="chasing">Pursuing</span>')
    else:
        entry.append(_action(n.external_id, "PURSUING", "Pursue this"))
    if r.state != "DISMISSED":
        entry.append(_action(n.external_id, "DISMISSED", "Not for us"))

    detail = ""
    if body:
        detail = ('<details><summary>Read the full notice</summary>'
                  f'<p class="full">{_esc(body[:8000])}</p></details>')
    detail += _papers(getattr(r, "docs", ()), n.project_id)

    fresh = '<span class="fresh" title="New since you last looked"></span>' if r.is_new else ""
    dropped = " dropped" if r.state == "DISMISSED" else ""
    reference = f'<span class="ref">{_esc(n.bid_reference_no)}</span>' if n.bid_reference_no else ""
    ledger = f'<div class="ledger"><span class="when">{when}</span>{reference}</div>'
    return (f'<article class="tender{dropped} {band_css}">'
            f'<p class="subject">{fresh}{subject}</p>'
            f'<p class="where"><span class="place">{place}</span>'
            f'{"&nbsp;&nbsp; " + kind if kind else ""}</p>'
            f'{quoted}{ledger}'
            f'<div class="entry">{"".join(entry)}</div>{detail}</article>')


def _papers(docs: Any, project_id: str | None) -> str:
    """The project's published documents, folded away until asked for.

    Two things this deliberately does not claim. It is not the bid document -- the tender
    package is not published through any World Bank API and comes from the buyer -- and it
    is not a guarantee of text: where a document exists only as a PDF the link says so,
    because that is the one case where OCR is actually needed.
    """
    docs = list(docs or ())
    if not docs:
        if not project_id:
            return ""
        return ('<details><summary>Project documents</summary>'
                '<p class="no-papers">Nothing published for this project yet. The bid '
                'document itself is not published here &mdash; ask the buyer for it.</p>'
                "</details>")
    rows = []
    for doc in docs:
        url = doc.readable_url
        title = _esc(doc.title)
        link = (f'<a href="/document?url={quote_url(url)}" rel="noreferrer">{title}</a>'
                if url else title)
        bits = [b for b in (_esc(doc.kind), _esc((doc.published or "")[:4])) if b]
        if doc.needs_ocr:
            bits.append('<span class="scan">PDF only, needs OCR</span>')
        meta = f'<span class="meta">{" &nbsp; ".join(bits)}</span>' if bits else ""
        rows.append(f"<li>{link}{meta}</li>")
    count = len(rows)
    word = "document" if count == 1 else "documents"
    return (f'<details><summary>{count} project {word}</summary>'
            f'<ul class="papers">{"".join(rows)}</ul>'
            '<p class="no-papers">These are the reports published against the project, '
            'not the bid document. Ask the buyer for the tender package.</p></details>')


def _action(notice_id: str, state: str, label: str) -> str:
    return ('<form method="post" action="/mark">'
            f'<input type="hidden" name="notice_id" value="{_esc(notice_id)}">'
            f'<input type="hidden" name="state" value="{state}">'
            f'<button type="submit">{label}</button></form>')


def _day(value: date | None) -> str:
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b')}" if value else ""


def render_blank(totals: dict[str, int | None]) -> str:
    dead = [t for t, n in totals.items() if n == 0]
    if totals and all(v is None for v in totals.values()):
        return ('<div class="blank"><strong>Cannot reach the World Bank right now.</strong>'
                "Your network may be blocking <code>search.worldbank.org</code>. This is "
                "not the same as a quiet week &mdash; try again, or from another connection."
                "</div>")
    if dead:
        listed = ", ".join(f"<code>{_esc(t)}</code>" for t in dead[:4])
        return (f'<div class="blank"><strong>Nothing open for these terms.</strong>{listed} '
                "match no World Bank notice at all. Try the spelled-out form rather than "
                "the acronym.</div>")
    return ('<div class="blank"><strong>Nothing open for these terms.</strong>Tenders '
            "matching your words exist, but none is accepting bids today. New ones appear "
            "every few days.</div>")


def quote_url(value: str) -> str:
    """URL-encode. HTML-escaping was used here, and `&` round-tripped to a literal `&`,
    which turned a reference number containing `&` into extra mailto headers."""
    return urlquote(value, safe="@")


def _esc(value: Any) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))
