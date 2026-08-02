---
id: roadmap-interview-questions
version: 2
model: careful
output: strict JSON
---

<!-- v2 (2026-08-01): added the Output schema section this file never had (every other
     prompt documents its shape; the enforced source of truth is roadmap/schemas.py),
     and one rule steering questions toward answers the adjust call can actually apply
     — a question whose answer maps to no edit wastes the owner's patience twice. -->


# Roadmap interview — question generation

First of the interview's two calls. Given a preset career path, produce the few
questions whose answers would most change how the preset should be personalized.
The owner answers in the terminal; the answers feed `roadmap-interview-adjust`.

## Prompt

```
You are helping {{owner_name}} personalize a career-path roadmap before it is
instantiated. Today is {{today}}.

The preset, as JSON:

{{preset_json}}

Ask between 3 and 6 questions, each targeting something that would materially
change the plan: where they already are on this path (steps possibly already
done), their real timeline (a target date, an exam sitting, an application
season), constraints (hours per week available, other obligations), and anything
about the cadences that looks wrong for their situation.

Rules:
- Every question must be answerable in one short sentence.
- Never ask for information already implied by the preset itself.
- Never ask more than one thing per question.
- Prefer questions whose answers map to an edit the adjuster can apply —
  a skip, a redate, or a cadence change. "How do you feel about X" maps
  to nothing.
- `key` is a short slug unique within the set.
```

## Output schema

Enforced by `roadmap/schemas.py::InterviewQuestions` (1–6 entries; the prompt asks
for 3–6, the schema tolerates fewer rather than failing the interview).

```json
{
  "questions": [
    {"key": "mcat_date", "question": "Do you have an MCAT sitting booked, and when?"},
    {"key": "hours", "question": "How many hours a week can this plan actually have?"}
  ]
}
```
