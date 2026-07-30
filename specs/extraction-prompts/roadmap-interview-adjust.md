---
id: roadmap-interview-adjust
version: 1
model: careful
output: strict JSON
---

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
