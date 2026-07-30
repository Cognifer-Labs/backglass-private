---
id: roadmap-interview-questions
version: 1
model: careful
output: strict JSON
---

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
- `key` is a short slug unique within the set.
```
