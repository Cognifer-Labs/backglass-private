# One truth per fact, and a tool that enforces it

**Goal (owner, 2026-08-07):** "resolve everything and make sure everything has a state that
is the truth, and any conflictors will be removed if they are stale — but if a conflictor is
more recent than the state doc then the user will be asked."

## Why this exists

Four times in two days a fact was written down in more than one place and the copies
disagreed, silently, with a green suite:

- `tokens.json` declared paper `#FAF3DF` while `tokens.css` shipped `#FCF8EC` — and *every*
  published `onPaper` ratio in the repo had been computed against the old colour.
- `tasks/plan.md` still stated the retired "no rounded corners" rule.
- The `.oops` comment cited an exemption its own commit had retired.
- **Live right now:** `backglass/brief/render.py` sets `RADIUS_CHIP = "4px"` against a
  comment binding it to `--radius-2`, which the rescale moved to **6px**. So the morning
  brief's chips render 4px while the dashboard's render 6px. `test_brief.py` asserts
  `border-radius:{render.RADIUS_CHIP}` — the checker is keyed to the mirror, so the value
  and its test are wrong together and green together.

That last one is the whole thesis. **A checker keyed to a mirror cannot detect drift in that
mirror.** Agreement between two copies can be wrong in both at once; only a value recomputed
or re-read from its declared authority can be trusted.

## Two definitions, stated because the wording could be read literally

- **"Removed"** means *corrected to the authority's value*, not the file deleted. Taking it
  literally would delete the design-system's tables, which are the documentation.
- **"The user will be asked"** means the tool refuses to auto-fix, exits non-zero, and names
  the conflict. In an interactive session that is a question; in CI it is a failure.

## The mechanism

`scripts/truth.py` — a registry plus a checker.

Each **fact** declares one **authority** (`file` + extractor) and N **mirrors**. An extractor
returns `(value, line_number)`.

For each mirror:

| | |
|---|---|
| values agree | **OK** |
| differ, mirror's line is **older** than the authority's | **STALE** → `--fix` rewrites it |
| differ, mirror's line is **newer** | **ASK** → no auto-fix, non-zero exit |

Recency is **per line**, via `git blame -L n,n`, not file mtime — a file touched for an
unrelated reason must not read as "recent". An uncommitted mirror line counts as newest and
therefore ASKs, which is right: an uncommitted edit is fresh human intent.

**The escalation loop is the point.** If the user rules that a newer mirror is correct, the
fix is to update the *authority* and re-run — every other mirror then goes STALE and
auto-fixes. One decision propagates everywhere.

## Load-bearing constraint

**A failed extraction is a loud error, never a skip.** If a pattern stops matching because a
table was reformatted, the tool must fail rather than silently declare the fact clean. A
checker that is green while blind is worse than no checker — that is exactly how the
`onPaper` figures survived.

## Scope fences

- **Values, not prose.** Hexes, px scales, ratios extract cleanly. The exemption lists in
  `design-system.md` vs the `dashboard.css` header are prose; regex over prose is where this
  turns into a research project. Prose consistency stays with the adversarial workflow
  reviews, which have caught it twice already.
- **History is not a conflictor.** `tasks/lessons.md` and `tasks/todo.md` deliberately record
  superseded values ("4px where 6px was right"). Excluded by design, or the tool corrects its
  own history.
- **Build artifacts excluded** — the desktop bundle's frozen copies are stale until rebuilt,
  by design.
- **Seed with the burned classes only.** A fact earns registry membership by having drifted.
  Enumerating all truth in the repo up front is how this becomes shelfware.

## Steps

- [ ] 1. `scripts/truth.py`: registry, extractors, git line-recency, classify, `--fix`.
- [ ] 2. Seed the registry: paper, the five inks, neutral-500, the three radius steps, the
      border weight — across `tokens.css` (authority), `tokens.json`, `design-system.md`,
      `preview.html`, `wordmark.svg`, `render.py`, `validate-palette.mjs`, `CLAUDE.md`.
- [ ] 3. Run it on the clean tree. **It must find `RADIUS_CHIP` as STALE** — that is the
      tool's acceptance test, and it is why the fix ships *with* the tool rather than before.
- [ ] 4. `--fix` it, and fix `test_brief.py` to assert against the authority rather than the
      mirror it is supposed to be checking.
- [ ] 5. `tests/test_truth.py` so drift fails the suite like everything else.
- [ ] 6. Prove both directions by mutation: seed a stale mirror → STALE; commit a mirror
      change newer than the authority → ASK.

## Also registering, because it claims authority it does not have

`scripts/validate-palette.mjs:14` hardcodes `PAPER = '#FCF8EC'`. It is the declared source of
truth for every contrast figure, but it is itself an unregistered mirror of `tokens.css` — if
paper changes again, the validator will validate the wrong colour while claiming to be the
authority.

## Definition of done

1. `uv run pytest` green; `node scripts/validate-palette.mjs` green.
2. `uv run python scripts/truth.py` reports zero STALE and zero ASK on a clean tree.
3. `RADIUS_CHIP` agrees with `--radius-2`, and `test_brief.py` checks it against the
   authority.
4. Both classifications proved by mutation.
