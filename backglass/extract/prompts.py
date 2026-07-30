"""Loads prompts from specs/extraction-prompts/*.md at runtime.

docs/10 §Model layer: "Prompts live in specs/extraction-prompts/*.md with a version in
the frontmatter, are loaded at runtime, and the version is stamped on every row the run
produces. Never inline a prompt in Python."

The stamp is `<id>@<version>`, and it is what `source_item.extraction_version` holds.
Bumping the version in the .md file makes every kept item pending again, which is the
re-extraction path in docs/02 §Immutable source items. No code change, no re-fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from backglass.config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "specs" / "extraction-prompts"

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_PROMPT_BLOCK = re.compile(r"^##\s+Prompt\s*$\n+^```\w*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    model_hint: str
    text: str
    path: Path

    @property
    def stamp(self) -> str:
        """What gets written to source_item.extraction_version."""
        return f"{self.id}@{self.version}"

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.text))

    def render(self, **values: object) -> str:
        """Substitute {{name}} placeholders. Every placeholder must be supplied.

        Strict on purpose: a silently-unsubstituted `{{occurred_at}}` would leave the
        model without the date to resolve against, which is exactly the failure CLAUDE.md
        rule 4 exists to prevent, and it would look like a model quality problem.
        """
        missing = self.placeholders() - set(values)
        if missing:
            raise PromptError(f"{self.id}: missing values for {sorted(missing)}")
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), self.text)


def load(name: str, directory: Path | None = None) -> Prompt:
    path = (directory or PROMPTS_DIR) / f"{name}.md"
    if not path.exists():
        raise PromptError(f"no prompt file at {path}")
    raw = path.read_text()

    front = _FRONTMATTER.match(raw)
    if not front:
        raise PromptError(f"{path.name} has no frontmatter block")
    meta: dict[str, str] = {}
    for line in front.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()

    for required in ("id", "version"):
        if required not in meta:
            raise PromptError(f"{path.name} frontmatter is missing {required!r}")

    block = _PROMPT_BLOCK.search(raw)
    if not block:
        raise PromptError(f"{path.name} has no fenced block under a '## Prompt' heading")

    return Prompt(
        id=meta["id"],
        version=meta["version"],
        model_hint=meta.get("model", ""),
        text=block.group(1).strip(),
        path=path,
    )
