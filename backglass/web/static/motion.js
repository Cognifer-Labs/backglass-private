/* The three moments a stylesheet cannot reach.
 *
 * design-system.md §9 owns everything that moves in this product, and it keeps owning
 * it: arrival is `@starting-style`, the press is `:active`, colour changes cross-fade,
 * the failed-write strip keyframes in. None of that is reimplemented here. §9 also lists
 * what deliberately does *not* move — page cross-fades, selection, focus rings, hover
 * reveals, progress bars, and stagger — and this file does not reopen any of it. Exits
 * used to be on that list and are not, as of the owner's 2026-08-27 ruling; §9 mechanism
 * 8 states the boundary that replaced the ban, and mechanism 3 below is the whole of it.
 *
 * What is left is three states CSS cannot observe, all of them §9 vocabulary applied to a
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
 *      content is entering. Opening only — a panel closing is still a disappearance the
 *      owner asked for and got instantly.
 *
 *   3. A row the owner just removed. Resolve or Drop and the row is gone from the ledger
 *      the moment the click lands, but it stays on screen for as long as the round trip
 *      and the re-render take — a quarter of a second on the board, which the owner
 *      reported as the application not responding. The row now leaves at the click and
 *      the write goes on underneath it. Nothing here is on the swap's critical path,
 *      which is the whole of §9's original argument against exits and the reason this
 *      one is allowed.
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

/** The rows currently on their way out. Read by mechanism 1 as well as 3 — the two have
 *  to agree about whether a gap has already been closed.
 *
 *  A set and not one slot, because two can be in flight at once and routinely are: the
 *  review queue is worked in streaks, and a write that lands while the half-hourly sync
 *  holds the lock waits up to thirty seconds for it (the 2026-08-11 lesson). With a
 *  single slot the second click overwrites the first, and a refusal then puts back
 *  whichever row happened to be in the slot — leaving a row the ledger still holds open
 *  invisible, with nothing on screen saying so. */
const leaving = new Set();

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

/** Where every commitment row on screen is, right now, keyed by commitment id.
 *
 * Keyed by id and not by element because this measurement is spent on the far side of a
 * swap, where every element it measured has been replaced by a new one carrying the same
 * id. Mechanism 3 measures inside one live document and keys by the element itself; the
 * two are not the same function wearing different names.
 *
 * Only rows already on screen are measured. `getBoundingClientRect` forces layout, and
 * forcing it for a row scrolled two thousand pixels away buys a correction nobody is in
 * a position to see — on the board that is six rows measured instead of sixty. A hidden
 * row is skipped: it has no position to return to, and its rect of zeros would read as
 * a row at the top of the viewport.
 */
function positions() {
  const map = new Map();
  if (still.matches) return map;
  const height = window.innerHeight;
  let measured = 0;
  for (const el of document.querySelectorAll("[data-commitment]")) {
    if (measured >= FLIP_ROWS) break;
    if (el.hidden) continue;
    const box = el.getBoundingClientRect();
    if (box.bottom < 0 || box.top > height) continue;
    map.set(el.dataset.commitment, box.top);
    measured += 1;
  }
  return map;
}

/** Animate away the difference between then and now — the second half of the FLIP.
 *
 * `document`, deliberately, and not `event.detail.target`. For an `outerHTML` swap —
 * which is what every write on this dashboard uses — htmx reports the target as the
 * element it replaced, and that element is already detached by the time this runs.
 * Measuring it returns a rect of zeros, so the delta came out as each row's absolute
 * offset instead of how far it moved, and the animation was then handed to a node that
 * would never be painted again. It logged perfectly and drew nothing for a day. The map
 * is keyed by commitment id, so the live document is the right thing to ask.
 */
function travel(was) {
  if (still.matches || was.size === 0) return;
  const duration = token("--motion");
  const easing = ease("--ease-in-out");
  for (const el of document.querySelectorAll("[data-commitment]")) {
    const from = was.get(el.dataset.commitment);
    if (from === undefined || el.hidden) continue;
    // Draw it moving, or leave it alone — a sub-pixel correction animated is a frame
    // spent on something no eye can see.
    const dy = from - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    el.animate({ transform: [`translateY(${dy}px)`, "none"] }, { duration, easing });
  }
}

document.body.addEventListener("htmx:beforeSwap", () => {
  // A response that beat an exit animation home. Settling the row now rather than
  // letting it finish is what keeps the two mechanisms from both animating the same
  // gap: measured after the row is out, the swap has nothing left to move.
  for (const row of leaving) row.hidden = true;
  before = positions();
});

document.body.addEventListener("htmx:afterSwap", () => {
  travel(before);
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


/* ── 3. The row the owner removed ──────────────────────────────────────────
 * Owner's ruling, 2026-08-27: "when i drop something it should disappear."
 *
 * What was happening: Drop arms, Drop confirms, and then nothing at all for the length
 * of a POST plus a re-render of the whole board — the largest fragment in the product —
 * after which the entire panel replaced itself at once. The write was never slow in the
 * sense that matters; it was slow in the only sense the owner can see.
 *
 * So the row leaves on the click. The request is still sent, the panel still swaps, and
 * the swap still wins any disagreement — this is an optimistic *view*, never an
 * optimistic *record*, and the ledger is not asked to believe anything the server has
 * not confirmed. If the write is refused the row comes back and `oops.js` says why;
 * a row that vanished on a write that failed would be the one unforgivable version of
 * this feature, because it would make the surface lie about what the ledger holds.
 *
 * §9 said "nothing leaves", and the reason it gave was latency: fading a panel out means
 * holding the swap open while it fades, and htmx's `defaultSwapDelay` is 0 for exactly
 * that reason. That argument is untouched and still enforced — `.htmx-swapping` carries
 * no transition and `test_nothing_animates_on_the_way_out` still holds it. This exit is
 * not on that path at all: it runs *before* the request, in time the owner was going to
 * spend waiting either way, so it costs nothing and returns the quarter second.
 *
 * Which control removes which row is stated in the template, never inferred here. A
 * script that guessed from the URL would vanish a row on Snooze — which moves a
 * commitment and does not remove it — the first time a verb was added.
 */
/** Everything that will move when `row` goes, keyed by the element itself.
 *
 * Two sets, because a vanishing row moves two different things. Its own siblings are the
 * rows immediately under it — the review queue and the roadmap's logged entries are
 * lists and nothing else on those pages shifts. And every commitment card on screen,
 * because the board is laid out in lanes: dropping something from Overdue moves the
 * whole of Today, which is not a sibling of anything that just left.
 *
 * Elements rather than ids: nothing is replaced between the two measurements here, so
 * the element is the most exact key available, and it also means a row that carries no
 * id of its own can still be animated.
 */
function neighbours(row) {
  const map = new Map();
  if (still.matches) return map;
  const height = window.innerHeight;
  const seen = (el) => {
    if (el === row || map.has(el) || el.hidden) return;
    const box = el.getBoundingClientRect();
    if (box.bottom < 0 || box.top > height) return;
    map.set(el, box.top);
  };
  for (const el of row.parentElement?.children || []) seen(el);
  let measured = 0;
  for (const el of document.querySelectorAll("[data-commitment]")) {
    if (measured >= FLIP_ROWS) break;
    seen(el);
    measured += 1;
  }
  return map;
}

/** The gap closing: the second half of a FLIP whose elements are still the same ones. */
function close(was) {
  if (still.matches) return;
  const duration = token("--motion");
  const easing = ease("--ease-in-out");
  for (const [el, from] of was) {
    if (!el.isConnected || el.hidden) continue;
    const dy = from - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    el.animate({ transform: [`translateY(${dy}px)`, "none"] }, { duration, easing });
  }
}

document.body.addEventListener("htmx:beforeRequest", (event) => {
  const control = event.detail?.elt?.closest?.("[data-vanish]");
  if (!control) return;
  const row = control.closest(control.dataset.vanish);
  if (!row || row.hidden) return;
  leaving.add(row);

  const was = neighbours(row);
  const settle = () => {
    if (!leaving.has(row)) return; // the swap landed first and already settled it
    row.hidden = true;
    close(was);
  };
  // Reduced motion means less movement, not less feedback (§9 rule 4). The row still
  // goes, at the same moment; it simply does not travel on its way.
  if (still.matches) {
    settle();
    return;
  }
  row
    .animate(
      { opacity: [1, 0], transform: ["none", `translateY(${token("--motion-travel")}px)`] },
      { duration: token("--motion-fast"), easing: ease("--ease-out"), fill: "forwards" },
    )
    .finished.then(settle, settle);
});

/* The write was refused, or never arrived. The row is still true, so it comes back —
 * arriving, which is §9 mechanism 1, because that is what it is doing. `oops.js` has
 * already put the reason on screen; this is only the half that puts the row back.
 *
 * The row comes from the failing request's own element, never from "the last one that
 * left": both error events carry `detail.elt`, and with two exits in flight the wrong
 * row restored is worse than none — one row resurrected that is gone and one still
 * missing that is not. */
function restore(event) {
  const control = event.detail?.elt?.closest?.("[data-vanish]");
  const row = control && control.closest(control.dataset.vanish);
  if (!row || !leaving.has(row)) return;
  leaving.delete(row);
  const was = neighbours(row);
  for (const animation of row.getAnimations()) animation.cancel();
  row.hidden = false;
  close(was);
  if (still.matches) return;
  row.animate(
    { opacity: [0, 1], transform: [`translateY(${token("--motion-travel")}px)`, "none"] },
    { duration: token("--motion"), easing: ease("--ease-out") },
  );
}

document.body.addEventListener("htmx:responseError", restore);
document.body.addEventListener("htmx:sendError", restore);
// The panel that arrived is the truth about what is on the board, so whatever these rows
// were doing stops being this script's business.
document.body.addEventListener("htmx:afterSwap", () => {
  leaving.clear();
});
