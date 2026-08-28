# Motion and smoothness, audited — 2026-08-27

## The ask

*"add animation and smoothness audit and implementation to app"*.

The premise needed checking first, and it did not survive: this app is not missing an
animation system. §9 of `design/design-system.md` is a deliberate eight-mechanism system
with five tokens, two curves, a frequency table, and a written list of what deliberately
does *not* move. The audit's job was to find what is actually wrong, not to add motion to
a product that had thought about it harder than most.

## What is already right, so nobody re-litigates it

Measured against the sheet, not assumed:

- **No `transition: all`** anywhere — zero occurrences in 2,236 lines.
- **Only compositable properties animate.** Every `transition` and `animation` names
  `opacity`, `transform`, or a colour. Nothing animates width, height, top or margin,
  which is the usual source of jank in a hand-written stylesheet.
- **No literal durations or curves in components** — `tests/test_motion.py` enforces it.
- **`prefers-reduced-motion` is honoured at the token layer**, so a rule written later
  inherits the setting instead of remembering it.
- **`content-visibility: auto` with `contain-intrinsic-size`** on `.row.card` and `.rrow`
  — the long lists are already skipping layout for what is off screen.
- **`ease-in` appears nowhere**, by rule and by test.

That is a better starting position than the ask implies, and three of the four findings
below are gaps *around* the system rather than inside it.

## A — the owner's own ruling was stranded

The owner asked why the app had no animations. A concurrent session diagnosed it
correctly: every mechanism was running, and `4px over 160ms` is below the threshold where
a fade-up reads as a fade-up. Travel went to `10px` and `--motion` to `220ms`.

**That fix was never on `main`.** It sat in an uncommitted working tree, then in the WIP
commit `30ddb9e` this session parked to rebuild the app. So the app the owner has been
looking at all evening still served the values they complained about.

Landed here, byte-identical to their hunks in `tokens.css`, `motion.js` and
`design-system.md` so it merges as a no-op when they un-park. The `dashboard.css` hunk was
taken by hand because their version of that file also carries error-page work that belongs
to their branch, not this one. **The diagnosis and the numbers are theirs.**

They also recorded the consequence rather than hiding it, which is the right call and is
repeated here so it is not lost: `--motion` at 220ms and `--motion-slow` at 240ms are
twenty milliseconds apart, which is not a rung. They never appear together — `--motion-slow`
is spent on the failed-write strip alone — so it is recorded rather than fixed. **If a
second thing ever earns the slow duration, the scale needs a real gap first.**

## B — the biggest thing on screen never moved

The sidebar is nine plain `<a href>` links. Every click is a full document load, so the
page did not transition at all: the old one was replaced by white and then by the new one.
Mechanism 1 was already animating the arrival — `@starting-style` fires on a navigated
document — into a flash.

`@view-transition { navigation: auto }` is the entire fix. No script, no page-transition
machinery, and it degrades to nothing where unsupported.

**Verified before it was written**, because a rule that silently does nothing is a failure
this product keeps finding. On this machine's WebKit (Safari 27, macOS 27):

```json
{ "sameDocumentAPI": true, "viewTransitionNameProperty": true,
  "atRuleParsed": "CSSViewTransitionRule", "pseudoSelectorParses": true }
```

Three decisions inside it, each with a test:

1. **Only the outgoing page animates.** The default cross-fades both halves, and this
   document already animates its own arrival, so leaving both on made content fade up
   through a fading page — two motions disagreeing about one event.
   `::view-transition-new(root)` takes `animation: none`; mechanism 1 keeps the arrival.
2. **The exit takes `--motion-fast`.** §9's table pays for frequency, and a leaving page
   is not content arriving.
3. **It names `prefers-reduced-motion` itself**, and that is the finding under the finding
   — see D.

## C — every cold navigation reflowed the masthead

`@font-face` for Oswald-Bold carries `font-display: swap` and the file was discovered only
when the stylesheet parsed. So a cold load painted every condensed header in the system
sans and then reflowed it into Oswald: a layout shift on the largest text on the page, on
the first thing read.

One `<link rel="preload" as="font" type="font/woff2" crossorigin>`, before the stylesheet.
`crossorigin` is the half that is easy to omit and worse than omitting the preload
entirely — fonts are fetched in CORS mode, so without it the file is fetched twice.

## D — §9 rule 4 has a hole, and it was about to get bigger

Rule 4 says reduced motion is honoured *at the token layer*: zero `--motion-travel` and
`--motion-press` and every transform in the product resolves to the identity. It is a good
rule and it is why no component mentions the media query.

It does not reach a view transition. A view transition is a cross-fade of two document
snapshots; it carries no transform, so zeroing those tokens does nothing to it. Ungated,
mechanism 9 would have been the first thing in the product to keep moving for a reader who
asked the system for less movement — and it would have done it on every navigation.

The theme flip was already outside rule 4 for the same shape of reason and already named
the media query. That made two, which is a pattern rather than an exception, so rule 4 now
states the test: **not "did I use the tokens" but "does zeroing travel and press actually
stop this" — if it does not, gate it.**

## Considered and declined

- **`scroll-behavior: smooth`.** The house position is that a dashboard is used, not
  toured, and the anchor jumps here are navigation rather than reading. Smooth scrolling
  makes a 1–9 page key feel slower for no information gained. Declined; if it is ever
  added it must be reduced-motion gated, because unlike the token layer it is not.
- **Same-document view transitions on htmx swaps.** `globalViewTransitions` is off in the
  bundled htmx and should stay off: §9's first omission is that nothing leaves on the
  swap's critical path, because Resolve and Done are the most frequent actions in the
  product and a transition there is latency in front of every write.
- **`will-change`.** Nothing here animates long enough or large enough to need a promoted
  layer, and a permanent hint costs memory at rest.

## Verification

- 4 new tests in `tests/test_motion.py`, and each was **mutation-checked**: ungating the
  view transition, letting the incoming page animate, paying for the exit at `--motion`,
  writing a literal duration, dropping `crossorigin`, and removing the preload were all
  caught.
- The whole page was rendered and read back from a real WebKit against the live ledger to
  confirm nothing regressed. Two screenshots came back blank and were **not** a
  regression — that is the known sticky-capture artifact of the Safari bridge, and
  `read_page` showed the full document.

## Left for someone else

The Sources panel is reporting a live error unrelated to any of this, seen while verifying:

```
ERROR replan: AttributeError: 'Settings' object has no attribute 'walk_minutes'
  · 1 of the last 20 syncs · 10h ago
```

Recorded, not chased — it is a planner bug, not a motion one.
