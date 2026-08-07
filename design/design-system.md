# Design System

**Product** Backglass
**Version** 2.1 · July 2026
**Scheme** Beige paper `#FCF8EC` and black `#000000`

Tokens in `tokens.css` and `tokens.json`. Rendered reference in `preview.html`.
Re-validate any change with `scripts/validate-palette.mjs`.

---

## 1. What an achromatic brand buys

This is a status product. Its job is telling you at a glance whether something is fine,
slipping, overdue, or done, and those states have conventional colors people read without
thinking.

Cream and black make the brand achromatic, which means **every drop of color in the
interface means something.** There is no decorative blue to tune out, no accent that might
be a link or might be an alert. If something is colored, it is telling you about state.

The aesthetic reference is screen-printed signage and pinball backglass art: warm paper
stock, hard black keylines, bold condensed uppercase, a small set of saturated inks laid
down flat. Two properties of that tradition turn out to be exactly what an accessible
status interface needs.

**Every color gets a black keyline.** In printing this hides registration gaps. Here it
separates a chip from the surface regardless of that color's own contrast, which is what
lets gold stay gold instead of a dimmed compromise.

**You pay per ink.** A five-ink job forces discipline. If you want a sixth, the answer is
a different treatment, not a new ink.

### One ink set, both modes

Paper sits at relative luminance 0.92 and black at 0. Any ink in the middle of the range
clears 3:1 against **both**. So there is one ink set, not two: light and dark use identical
hex values and dark mode is a true inversion rather than a separately designed palette.

---

## 2. Paper and ink

| token | hex | notes |
|---|---|---|
| paper | `#FCF8EC` | OKLCH L 0.979, C 0.016, hue 92 |
| ink | `#000000` | 18.95:1 against paper |

Dark mode swaps them. Nothing else changes.

### Neutral ramp (warm, hue 91)

Tinted to the paper's own hue, so a muted label reads as faded ink on paper rather than a
different material.

| step | hex | on paper | on black |
|---|---|---|---|
| 50 | `#f5eedb` | 1.04 | 18.14 |
| 100 | `#e4decd` | 1.21 | 15.63 |
| 200 | `#c9c4b5` | 1.57 | 12.05 |
| 300 | `#aca79b` | 2.16 | 8.75 |
| 400 | `#8e8a81` | 3.11 | 6.10 |
| 500 | `#716f67` | 4.54 | 4.17 |
| 600 | `#57554f` | 6.73 | 2.82 |
| 700 | `#3d3c37` | 9.97 | 1.90 |
| 800 | `#252421` | 14.01 | 1.35 |
| 900 | `#0e0d0b` | 17.53 | 1.08 |

On paper: 700 for secondary text, 500 for muted. On black: 200 and 300. Neutral 500 is the
pivot, the one step that works on both.

---

## 3. The five inks

| role | hex | on paper | on black | black text on it |
|---|---|---|---|---|
| vermilion | `#D03D37` | 4.30 | 4.40 | 4.40 |
| gold | `#E8AC1D` | 1.83 | 10.35 | 10.35 |
| green | `#249041` | 3.69 | 5.14 | 5.14 |
| turquoise | `#009592` | 3.32 | 5.71 | 7.20 |
| cobalt | `#2766C0` | 5.06 | 3.75 | 3.75 |

Gold is the one ink below 3:1 on paper, kept at full brightness on purpose because a
dimmed gold stops reading as gold. The black keyline plus label is the mitigation.

### The second exception, and why it stays

That paragraph used to end "it is the only such exception in the system." An axe-core
pass over every page in both themes on 2026-08-06 found that it is not: **black on
vermilion measures 4.40, under the 4.5 floor for normal text**, and in dark mode the
washes resolve to the full inks, so vermilion carries body copy — an overdue card's
title at 15px, its metadata at 13px, its OVERDUE chip at 11px bold, and the sidebar's
failing-source alert. The figure is not new; the table above has always recorded 4.40.
What was wrong was the claim that nothing else fell short.

It stays at 4.40, for the reason gold stays at 1.83. Reaching 4.5 means darkening
vermilion, and the argument against a dimmed gold applies with more force to the alarm
ink: an overdue mark that has been desaturated to pass a threshold is a quieter alarm,
which is a worse outcome than a 2% contrast shortfall on text that also carries a
keyline, a glyph and a word. The palette's all-pairs CVD separation was validated at
these exact values (see the appendix); moving one of three series slots to buy 0.1 of
contrast would have to be re-argued against deuteranopia, not just against WCAG.

Light mode is unaffected — a resting component there wears the pastel wash, and the
full ink appears only under a keyline. So the exception is precisely: **normal text on
full-strength vermilion, in dark mode.** Recorded here rather than fixed, because a
palette with two documented exceptions is honest and a palette with one documented and
one undocumented is not.

**Cobalt is chart-only.** Black text on cobalt measures 3.75, below the 4.5 threshold for
normal text, so cobalt never carries a chip label. It appears as a chart fill — and as
the work-block identity stripe in the schedule views, which is the same series-token use
(§5: categories that mean identity wear series tokens) and likewise never carries text.

---

## 4. Status

| state | ink | treatment |
|---|---|---|
| open, not due soon | none | neutral 500 text, no fill |
| due today | black | solid black chip, paper text |
| slipping | gold | fill + 2px black keyline + black text |
| overdue | vermilion | fill + 2px black keyline + black text |
| awaiting others | turquoise | fill + 2px black keyline + black text |
| done | green | fill + 2px black keyline + black text |
| needs review | none | 2px black **dashed** keyline, no fill |
| protected block | black | 2px black keyline, 45° hatch fill |

"Due today" being solid black rather than an ink is the payoff of an achromatic brand: the
most important item gets the strongest mark available, which here is maximum contrast
rather than a hue.

"Needs review" gets a dashed keyline and no fill. Low-confidence extractions are guesses,
and in a system where color means certainty, a guess does not get an ink.

Every status chip carries an icon and a text label. Color is never the only channel.

---

## 5. Chart colors

**Series slots**, validated all-pairs under simulated protanopia and deuteranopia against
both surfaces, identical values in both modes:

| slot | hex |
|---|---|
| 1 | cobalt `#2766C0` |
| 2 | vermilion `#D03D37` |
| 3 | turquoise `#009592` |

Worst all-pairs CVD ΔE 12.8, worst normal-vision ΔE 17.0, every slot above 3:1 on both
surfaces, lightness band and chroma floor passing in both.

**Three series maximum.** A fourth is not a new ink; it folds into "Other" or the chart
becomes small multiples.

**Single-series charts use black, not cobalt.** A bar chart of open commitments by
counterparty is one series, and on paper a solid black bar is both the most legible option
and the most on-brand. The ink slots are for charts with two or three things to tell apart.

**Bars get a 1px black keyline** when filled with an ink. On black surfaces the keyline
becomes paper.

**The collision rule.** Vermilion is both series slot 2 and the overdue status. That is
deliberate: this system has few inks by design. What keeps it honest is that a chart uses
**either** the status palette **or** the series palette, never both. Categories that mean
state wear status tokens; categories that mean identity wear series tokens. Mixing them in
one chart is the error, not sharing a hex across different charts.

**Sequential magnitude** uses black at descending opacity on paper, or paper at descending
opacity on black. One ink, no rainbow.

---

## 6. Type

```
Display / labels   "Helvetica Neue Condensed", "Arial Narrow", system-ui
                   700, uppercase, letter-spacing .02em
Body               system-ui, -apple-system, "Segoe UI", sans-serif

banner    24 / 28   700  uppercase     section headers in black bars
title     19 / 26   700
body      15 / 23   400                brief and dashboard default
small     13 / 19   400
label     11 / 14   700  uppercase +.06em
reel      22 / 22   700  tabular       numeric displays

tabular-nums everywhere. This is a numbers surface.
```

If you spend one webfont, spend it here. A real condensed grotesque (Archivo Narrow,
Oswald, Roboto Condensed) does more for the look than any other single change.

Script faces are for a wordmark only. Never for UI, never for data.

---

## 7. Structure

```
Radius   2px marks · 4px controls and rows · 8px containers. One scale, no literals.
Rules    2px solid black between sections; 1px neutral 200 between rows
Borders  2px black on chips, cards, buttons
Space    4 8 12 16 24 32 48
Shadow   none, ever
Targets  24px minimum, grown under the mark rather than around it
```

**Corners follow the box (owner ruling 2026-08-07).** The system was square everywhere
until this date, on the argument that a radius reads as a different design language. The
owner reversed it. What survives the reversal is the reasoning underneath: a curve has to
stay proportional to what it curves, so there are three steps and a component picks one
rather than a number. `--radius-sm` for marks a 4px curve would swallow — reel digit
windows, progress fills, chart bars. `--radius` for anything a hand acts on. `--radius-lg`
for the containers those things sit in.

Two consequences the reversal forced, both structural rather than cosmetic. The panel grid
lost its shared seams: panels used to carry a right and a bottom rule and touch, which is
what made the page read as one ledger, and two rounded corners meeting across a shared
seam leave a notch and nothing else. Panels now carry a keyline on all four sides and
stand apart. And every panel's banner rounds its own top corners rather than relying on
the panel to clip it — a `<summary>` inside a `<details>` is the one child WebKit does not
reliably clip to its parent's radius, which made the curve real in the CSS and invisible
on the page.

Shadows and gradients did not come back with the corners. §8 rule 7 forbade three things
for one reason each; exactly one of them was reconsidered.

**Targets are grown under the mark, not around it.** WCAG 2.5.8 asks for 24px, and
several marks in this system are deliberately smaller than that — a week grid's day box
is 16px because seven of them in a row is what makes the row read as a week. The rule is
that the drawing keeps its size and the hit area is expanded beneath it, so meeting the
minimum never costs the density the mark was chosen for.

Two exemptions, both the standard ones and both real here: a link inside a sentence of
metadata is inline text, and a block whose height *is* its duration has an essential
size. The week agenda's event blocks are the second case — they are 22px because they
are 22 minutes, and padding them would make the timeline lie. What is not exempt there
is spacing: stacked events touch, so the fix if it is ever wanted is a gap in the grid,
not a taller link.

Depth comes from the black rule and from figure-ground, the way it does on a printed sign.
A drop shadow reads as a different design language immediately.

**Section headers are black bars.** Full-width black block, paper uppercase condensed text.
The single most identity-carrying element in the interface, and it costs nothing.

**Reel digits.** The reference object is a score-reel clock, so numeric readouts get a
literal reel treatment: each digit in its own paper window, 2px radius, 1px black gap, on a
black plate. Use it for at most three numbers per view. On every number it becomes
wallpaper; on three it becomes the thing people remember.

The reel is a physical object and keeps its colors in both modes: black housing, paper
wheels. In dark mode it gains a paper keyline so it reads against the surface.

---

## 7a. Wordmark

`wordmark.svg`. A beige band cut diagonally through a black plate, the name in condensed
uppercase, a credit bar beneath carrying the maker line and a four-digit score reel.

The diagonal is the identity. It comes from mid-century screen-printed signage, where a
band cut across a solid plate was the cheapest way to get two colors doing structural
work at once. Keep the band angle at roughly −2.7°, shallow enough to read as deliberate
rather than jaunty.

Rules:

- The plate is black, the band is paper, the name is black. Never inverted; the wordmark
  does not have a dark-mode variant because it is already mostly black.
- Minimum width 240px. Below that, drop the credit bar and reel and keep the plate alone.
- Clear space on all sides equals the height of the credit bar.
- Never set the name in anything but condensed bold uppercase. No script face, ever.
- The reel shows a real number when the mark is used in-product, and `0600` — the brief
  delivery time — everywhere else.

## 8. Rules that are not negotiable

1. Color means state. Nothing decorative gets an ink.
2. Every colored fill carries a keyline. Amended by the 2026-07-31 wash ruling:
   a resting component wears the pastel wash with a keyline in its own saturated
   ink — except gold, whose keyline stays black, because §3 permits gold's
   sub-3:1 only with the black keyline as mitigation. Data fills (progress bars,
   chart marks) are not resting components: they stay full ink with the black
   keyline on the track. On black surfaces the keyline is paper.
3. Status ships as icon plus label, never color alone.
4. Every generated claim links to its source.
5. Low confidence renders as a dashed outline, never as a confident statement.
6. Three chart series maximum; a chart uses status tokens or series tokens, never both.
7. No shadows, no gradients. Corners are rounded, from the scale in §7.
8. Tabular figures everywhere.
9. Cobalt never carries text.

---

## 9. Motion

Added 2026-08-07. Until then the product had one line of motion in it, and that line
never played.

The governing question is not "would this look nice moving" but **how often does the
owner see it.** A dashboard is used, not toured. Something that animates the first time
you see it is charming; the same thing on the four-hundredth resolve is the application
being slow at you.

| How often | What it gets |
|---|---|
| Tens of times a day — Resolve, Done, a checklist tick, page keys 1–9 | 90ms, or nothing |
| Daily — opening a page, a panel replacing its contents | 160ms |
| Occasional — the failed-write strip, the theme flip | 240ms |
| Rare | Nothing here is rare enough to earn more |

### The vocabulary

Five tokens, and the same reasoning as §3's five inks: you pay per duration, and a
system with seven of them has no rhythm, only seven speeds.

| Token | Value | For |
|---|---|---|
| `--motion-fast` | 90ms | A press, a hover, the dim on a write in flight |
| `--motion` | 160ms | Content arriving — a swapped panel, a page |
| `--motion-slow` | 240ms | The failed-write strip, the theme cross-fade |
| `--motion-travel` | 4px | The only distance anything travels |
| `--motion-press` | 0.97 | The only depth anything presses |

Two curves, both ease-out family: `--ease-out` `cubic-bezier(.23,1,.32,1)` for anything
entering, `--ease-in-out` `cubic-bezier(.77,0,.175,1)` for anything moving on screen.
**`ease-in` appears nowhere.** It starts slow, which delays the exact instant the owner
is looking at, and a 200ms ease-in feels slower than a 200ms ease-out.

### What moves

1. **Content that arrives** fades up four pixels over 160ms. This is one mechanism, not
   three: a swapped panel, a swapped fragment and a whole navigated page are all
   "elements newly inserted into the document", which is what `@starting-style`
   selects. The nine-page sidebar therefore animates without any page-transition
   machinery.
2. **A pressed control** dips to 0.97 over 90ms, on buttons only.
3. **A colour that changes** cross-fades over 90ms — hover, selection, the enabled
   state of a source, and the 0.55 dim htmx puts on a control mid-write.
4. **The failed-write strip** slides eight pixels up from the bottom edge over 240ms.
5. **The theme flip** cross-fades every surface at once over 160ms, via a class the
   toggle adds and removes.

### What does not move, and why

The omissions carry the design more than the inclusions do.

- **Nothing leaves.** Only arrivals are animated, because only arrivals are free. Fading
  a panel out means holding the swap open while it fades — htmx's `defaultSwapDelay` is
  0 for exactly this reason — and that is latency added to the most frequent actions in
  the product.
- **No page cross-fade on navigation.** Cross-document view transitions were considered
  and rejected: page switching is bound to keys 1–9 and fired tens of times a day, and
  a whole-page fade is the single most effective way to make a keyboard-driven app feel
  sluggish. The arriving content already fades; the frame around it stays still, which
  is also what makes the sidebar read as persistent across a navigation.
- **Selection does not animate.** `j`/`k` move the card selection. Keyboard actions
  repeat hundreds of times a day, so the highlight is instant.
- **Focus rings are instant.** A focus ring is an answer to "where am I", and an answer
  that fades in is worse than one that is simply there.
- **Progress bars do not grow.** They could — htmx settles `style` attributes over a
  20ms window, which is the hook — but only for elements it can match by `id` across
  the swap, and no bar in this product has one. Adding ids to seven templates to buy
  one animation was not worth it; noted here so the next author knows it was weighed.
- **Hover reveals stay instant.** A card's action row is revealed with `display`, which
  no transition can interpolate. Converting it to opacity would make the row occupy
  space at all times and move the layout, which is a worse trade than a hard reveal.
- **No stagger anywhere.** Panels arrive together. Staggering nine panels puts the last
  one a third of a second behind the first on a page opened all day long.

### Rules

1. Animate `transform` and `opacity` only. They skip layout and paint. §8 rule 7 bans
   shadows and gradients because each announces a different design language; motion is
   held to the same standard, so surfaces fade and shift a hair and nothing lifts.
2. Nothing exceeds 300ms.
3. No duration or curve is written literally in a component. If a component needs a
   speed the five tokens do not have, that is a system decision, not a component one.
4. `prefers-reduced-motion: reduce` is honoured at the token layer — `--motion-travel`
   goes to 0 and `--motion-press` to 1, which resolves every transform in the product
   to the identity and leaves the cross-fades alone. Reduced motion means less
   movement, not less feedback. A rule written after this one inherits the setting
   instead of having to remember it.

---

## Appendix: validation

Contrast figures are WCAG relative luminance against `#FCF8EC` and `#000000`. The series
set was validated with `scripts/validate-palette.mjs` under Machado-Oliveira-Fernandes 2009
colorblindness simulation at severity 1.0, all-pairs in both modes.

Rejected alternatives: cobalt / turquoise / plum reached only ΔE 7.9, inside the warn band
requiring secondary encoding; cobalt / vermilion / green collapsed to ΔE 3.0 under
deuteranopia.

Re-run the validator after any color change:

```bash
node scripts/validate-palette.mjs
```
