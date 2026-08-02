---
id: roadmap-interview-adjust
version: 2
model: careful
output: strict JSON
---

<!-- v2 (2026-08-01): added the Output schema section this file never had — the shape
     lived only in roadmap/schemas.py, so a prompt edit could silently drift from what
     validation enforces. Every field including the per-edit reason is now visible
     where the prompt is edited. -->


# Roadmap interview — adjustment set

Second call. Given the preset and the owner's answers, return the edits that fit
the preset to this person. The edits are applied in code by
`roadmap.instantiate.apply_adjustments`; unknown keys are dropped item-wise there,
so refer only to keys that exist in the preset.

## Prompt

```
You are personalizing a career-path roadmap for its owner. Today is {{today}}.

The preset, as JSON:

{{preset_json}}

The interview transcript (your questions, their answers; an empty answer means
they declined the question):

{{qa_transcript}}

Return the adjustment set:
- step_adjustments: for each preset step that should change — action "skip" when
  the owner has already done it or it does not apply, "redate" with an ISO
  planned_date when their timeline differs from the offset, otherwise "keep".
  Steps you omit are kept as-is; "keep" entries are only for carrying a reason.
- added_steps: at most 5, only for concrete milestones the owner named that the
  preset lacks. Each needs an ISO planned_date; after_step_key places it.
- cadence_adjustments: new weekly_count for any cadence their availability
  contradicts. 0 disables it.
- summary: one sentence, addressed to the owner, saying what changed and why.

Rules:
- Dates must be real ISO dates on or after today, consistent with their answers.
- Never invent steps for things the owner did not say.
- An answer you cannot map to a concrete edit produces no edit — put the reason
  in `reason`, never guess a date.
```

## Output schema

Enforced by `roadmap/schemas.py::RoadmapAdjustments`. Every edit carries a `reason` —
it is what the confirmation screen shows the owner before anything is applied.

```json
{
  "step_adjustments": [
    {"step_key": "prereqs", "action": "skip", "planned_date": null,
     "reason": "already completed the coursework"},
    {"step_key": "mcat", "action": "redate", "planned_date": "2026-11-15",
     "reason": "sitting already booked for November"}
  ],
  "added_steps": [
    {"key": "casper", "title": "Sit Casper", "planned_date": "2027-05-20",
     "after_step_key": "mcat", "reason": "owner named it with a date"}
  ],
  "cadence_adjustments": [
    {"cadence_key": "practice_sections", "weekly_count": 5,
     "reason": "full-time prep block"}
  ],
  "summary": "Skipped prerequisites, moved the MCAT to November, added Casper."
}
```

`weekly_count` is capped at 21 by validation; `planned_date` is required whenever
`action` is `redate`, and `added_steps` tops out at 5 — the same limits the prompt
states, enforced rather than trusted.
