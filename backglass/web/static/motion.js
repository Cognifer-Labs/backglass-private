/* The two moments a stylesheet cannot reach.
 *
 * design-system.md §9 owns everything that moves in this product, and it keeps owning
 * it: arrival is `@starting-style`, the press is `:active`, colour changes cross-fade,
 * the failed-write strip keyframes in. None of that is reimplemented here. §9 also lists
 * what deliberately does *not* move — exits, page cross-fades, selection, focus rings,
 * hover reveals, progress bars, and stagger — and this file does not reopen any of it.
 *
 * What is left is two states CSS cannot observe, both of them §9 vocabulary applied to a
 * moment §9's own mechanisms cannot select:
 *
 *   1. A row that moves because another row left. Resolve a commitment and htmx swaps
 *      the panel: everything below the gap jumps up, instantly, with nothing connecting
 *      where a row was to where it now is. FLIP — measure before, measure after, animate
 *      the difference away — needs two measurements around a DOM mutation, which is a
 *      script or it is nothing. §9 defines `--ease-in-out` for "anything moving on
 *      screen" and nothing had ever used it; this is what that curve was held for.
 *
 *   2. A panel opening. `<details>` reveals its contents by flipping a boolean, and a
 *      revealed element is not a newly inserted one, so `@starting-style` never sees it
 *      — the one arrival in the product that CSS cannot select. It gets §9 mechanism 1
 *      exactly: fade up four pixels over `--motion`, on `--ease-out`, because the
 *      content is entering. Opening only. Nothing leaves.
 *
 * `Element.animate` — the Web Animations API, already in the browser. This file briefly
 * shipped on Motion's `mini` build, which is a wrapper over exactly this call; docs/10
 * §Web layer's "no build step, no npm, no bundler" is better served by the platform than
 * by a vendored copy of a wrapper around one.
 *
 * Transform and opacity only (§9 rule 1) — which is also the pair the compositor can
 * animate without the main thread, on a page whose whole problem was the main thread
 * being busy. Every number is a token (§9 rule 3).
 *
 * Reduced motion (§9 rule 4) is honoured two different ways, on purpose. The fold reveal
 * travels `--motion-travel`, which the token layer already zeroes, so it goes still by
 * itself and keeps its cross-fade — less movement, not less feedback. The FLIP has no
 * distance token to zero, because its distance is however far the row happened to move,
 * so it is gated on the query directly and simply does not run.
 */
const root = document.documentElement;

/** A token, in the unit it is written in. `160ms` and `4px` come back as 160 and 4. */
function token(name) {
  return parseFloat(getComputedStyle(root).getPropertyValue(name)) || 0;
}

/** A token verbatim. The Web Animations API takes a CSS easing string as written, so a
 *  curve travels from tokens.css to the animation without being reformatted — one fewer
 *  place for it to become a second, slightly different curve. */
function ease(name) {
  return getComputedStyle(root).getPropertyValue(name).trim() || "ease-out";
}

const still = window.matchMedia("(prefers-reduced-motion: reduce)");

/* ── 1. The row that moved ─────────────────────────────────────────────────
 * Positions are read in `htmx:beforeSwap`, while the old panel is still on screen, and
 * spent in `htmx:afterSwap`, which htmx fires synchronously after the new markup is in
 * the document and before the browser has painted it. That ordering is the whole trick:
 * the inverse transform is applied in the same frame the row moved, so the row is never
 * seen in its new place before it starts travelling there.
 *
 * Only rows already on screen are measured. `getBoundingClientRect` forces layout, and
 * forcing it for a row scrolled two thousand pixels away buys a correction nobody is in
 * a position to see — on the board that is six rows measured instead of sixty.
 *
 * Commitment cards only. They are the rows that carry an identity across a swap
 * (`data-commitment`), and they are the rows the frequent writes rearrange. */
const FLIP_ROWS = 60;
let before = new Map();

document.body.addEventListener("htmx:beforeSwap", () => {
  before = new Map();
  if (still.matches) return;
  const height = window.innerHeight;
  let measured = 0;
  for (const el of document.querySelectorAll("[data-commitment]")) {
    if (measured >= FLIP_ROWS) break;
    const box = el.getBoundingClientRect();
    if (box.bottom < 0 || box.top > height) continue;
    before.set(el.dataset.commitment, box.top);
    measured += 1;
  }
});

document.body.addEventListener("htmx:afterSwap", () => {
  if (still.matches || before.size === 0) return;
  const duration = token("--motion");
  const easing = ease("--ease-in-out");
  // `document`, deliberately, and not `event.detail.target`. For an `outerHTML` swap —
  // which is what every write on this dashboard uses — htmx reports the target as the
  // element it replaced, and that element is already detached by the time this runs.
  // Measuring it returns a rect of zeros, so the delta came out as each row's absolute
  // offset instead of how far it moved, and the animation was then handed to a node
  // that would never be painted again. It logged perfectly and drew nothing for a day.
  // The map is keyed by commitment id, so the live document is the right thing to ask.
  for (const el of document.querySelectorAll("[data-commitment]")) {
    const was = before.get(el.dataset.commitment);
    if (was === undefined) continue;
    // Draw it moving, or leave it alone — a sub-pixel correction animated is a frame
    // spent on something no eye can see.
    const dy = was - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    el.animate({ transform: [`translateY(${dy}px)`, "none"] }, { duration, easing });
  }
  before = new Map();
});

/* ── 2. The panel that opened ──────────────────────────────────────────────
 * Every panel, every fold and every quick-add form on this dashboard is a `<details>`,
 * and opening one is the most common way content appears without arriving: the elements
 * were already in the document, so §9 mechanism 1 has nothing to select them with.
 *
 * The children are animated rather than the `<details>` itself, because the `<summary>`
 * is not part of what appeared — it was on screen the whole time, and fading the header
 * of a panel the owner just clicked is an answer to a question nobody asked. They all
 * take the same timing, with no stagger: §9 refuses one, and the result reads as a
 * single block of content arriving, which is what it is.
 *
 * The `toggle` event rather than a click handler, because a panel is also opened by the
 * `r` key and by the fold-persistence script, and all three should look the same.
 * Closing is not animated and that is not an oversight — §9's first omission is that
 * nothing leaves.
 *
 * `opened` is the correction for something only a browser would tell you. WebKit fires
 * `toggle` on a `<details open>` that is *inserted* into the document, not just on one
 * that is opened — so every htmx swap announced every panel it replaced as freshly
 * opened, and the contents faded up on top of the `@starting-style` arrival already
 * animating them: two animations on the most frequent action in the product.
 *
 * The fix is a remembered state rather than a time window, and that distinction was
 * earned. The echo arrived two milliseconds after one swap and a hundred and seventy-
 * eight after the next, so anything phrased as "ignore toggles for the next little
 * while" is a coin toss. What is reliable is the fold's own history: a panel that was
 * already open and is still open did not open, whoever fired the event. Panels are
 * recorded as they arrive, so the echo finds its own state waiting for it. */
const opened = new WeakMap();

function remember(root) {
  for (const panel of (root || document).querySelectorAll("details")) {
    if (!opened.has(panel)) opened.set(panel, panel.open);
  }
}

document.body.addEventListener("htmx:afterSwap", () => remember());
remember();

document.addEventListener(
  "toggle",
  (event) => {
    const panel = event.target;
    if (!panel.matches?.("details")) return;
    const was = opened.get(panel);
    opened.set(panel, panel.open);
    // Not a change of state, so not an opening — this is the insertion echo above.
    if (was === panel.open) return;
    if (!panel.open) return;
    const duration = token("--motion");
    const easing = ease("--ease-out");
    const travel = token("--motion-travel");
    for (const child of panel.children) {
      if (child.tagName === "SUMMARY") continue;
      child.animate(
        { opacity: [0, 1], transform: [`translateY(${travel}px)`, "none"] },
        { duration, easing },
      );
    }
  },
  true,
);
