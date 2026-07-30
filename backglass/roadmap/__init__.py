"""Roadmaps — preset career paths instantiated as a goal with dated steps.

A roadmap owns exactly one goal row. Its steps and cadences own target rows, so the goal
engine (`goals.targets`, `goals.checkpoints`, `goals.health`) reads a roadmap with no
changes at all: a step is a milestone target, a cadence is a cadence target.

Three modules:

  `presets`      reads specs/roadmaps/*.md, the same frontmatter-plus-fenced-block shape
                 as specs/extraction-prompts (see extract/prompts.py)
  `instantiate`  preset + start date -> rows, in one transaction
  `adjust`       the owner's post-instantiation edits
"""

from __future__ import annotations
