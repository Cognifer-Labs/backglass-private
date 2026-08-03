"""Rendering the brief to email HTML and to plaintext.

docs/05 §Email constraints, and design/design-system.md. The constraints are not stylistic
preferences — they are what survives Gmail, Outlook and forced dark mode:

  - inline every style; no <style> block, no CSS variables, no external sheet
  - table-based layout; flexbox and grid are not reliable
  - background cream set explicitly on the outer table, not just on <body>
  - black rules for section breaks rather than colored headers
  - every ink fill carries a 2px black keyline
  - status ships as glyph plus label, never colour alone

The cream-and-black scheme is what makes this survivable: docs/05 notes that nothing here
depends on a subtle hue relationship, so a client that strips or inverts styles degrades
to legible black-on-white rather than to nonsense.

The plaintext alternative is generated from the same `Brief` object, per docs/10 §Email
delivery — "rather than by stripping tags", which is how plaintext versions rot.

`parse_markdown` at the bottom reads `brief.content_md` back into a `Brief`, so the
dashboard's /brief page renders the stored brief through these same objects rather
than through a second, divergent notion of what a brief looks like. It lives next to
`to_markdown` because it is that function's inverse and the two must move together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from backglass.brief.model import Brief, Line, Note, Section

# design/tokens.css. Duplicated as literals because email cannot use CSS variables; this
# is the one place in the codebase allowed to hard-code them, and validate-palette.mjs
# still governs the source of truth in tokens.css.
PAPER = "#FAF3DF"
INK = "#000000"
INK_MUTED = "#716f67"
VERMILION = "#D03D37"
GOLD = "#E8AC1D"
TURQUOISE = "#009592"

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

#: design-system.md §4. Every entry carries a glyph and a label as well as the ink,
#: because rule 3 in §8 is "status ships as icon plus label, never color alone".
STATUS: dict[str, tuple[str, str, str | None]] = {
    # status:        (glyph, label,      fill)
    "overdue": ("▲", "OVERDUE", VERMILION),
    "due_today": ("●", "DUE TODAY", INK),
    "slipping": ("◆", "SLIPPING", GOLD),
    "awaiting": ("○", "AWAITING", TURQUOISE),
    "needs_review": ("?", "REVIEW", None),  # dashed keyline, no fill — a guess gets no ink
    "protected": ("▬", "PROTECTED", None),
}


def _chip(status: str | None) -> str:
    """A status chip. design-system.md §4, and §8 rules 2, 3 and 5."""
    if not status or status not in STATUS:
        return ""
    glyph, label, fill = STATUS[status]
    if status == "needs_review":
        # "Low confidence renders as a dashed outline, never as a confident statement."
        style = (
            f"border:2px dashed {INK};color:{INK};background:transparent;"
            "padding:1px 5px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
        )
    elif status == "protected":
        style = (
            f"border:2px solid {INK};color:{INK};background:transparent;"
            "padding:1px 5px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
        )
    else:
        text = PAPER if fill == INK else INK
        style = (
            f"background:{fill};color:{text};border:2px solid {INK};"
            "padding:1px 5px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
        )
    return f'<span style="{style}">{glyph}&nbsp;{escape(label)}</span> '


def _line_html(line: Line, base_url: str) -> str:
    """One claim, with its source link. B2 — this is where provenance becomes visible."""
    href = escape(line.provenance.url(base_url), quote=True)
    label = escape(line.provenance.label)
    return (
        f'<tr><td style="padding:6px 0;border-bottom:1px solid #c9c4b5;'
        f'font-family:{FONT};font-size:15px;line-height:1.4;color:{INK};">'
        f"{_chip(line.status)}{escape(line.text)} "
        f'<a href="{href}" style="color:{INK_MUTED};font-size:12px;'
        f'text-decoration:underline;white-space:nowrap;">{label}</a>'
        f"</td></tr>"
    )


def _section_bar(title: str) -> str:
    """design-system.md §7: "Section headers are black bars." Paper uppercase condensed."""
    return (
        f'<tr><td style="background:{INK};color:{PAPER};padding:6px 10px;'
        f"font-family:{FONT};font-size:12px;font-weight:700;text-transform:uppercase;"
        f'letter-spacing:0.09em;">{escape(title)}</td></tr>'
    )


def to_html(brief: Brief, *, base_url: str, brief_id: int | None = None) -> str:
    """The email body. Inline styles only, tables only, cream set on the outer table."""
    brief.assert_provenance()  # B2, immediately before render. Hard failure.

    blocks: list[str] = []
    for section in brief.ordered():
        blocks.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0" style="margin:0 0 18px 0;">'
        )
        blocks.append(_section_bar(section.title))
        blocks.extend(_line_html(line, base_url) for line in section.lines)
        for note in section.notes:
            blocks.append(
                f'<tr><td style="padding:6px 0;font-family:{FONT};font-size:12px;'
                f'color:{INK_MUTED};font-style:italic;">{escape(note.text)}</td></tr>'
            )
        blocks.append("</table>")

    for note in brief.notes:
        blocks.append(
            f'<p style="font-family:{FONT};font-size:12px;color:{INK_MUTED};'
            f'font-style:italic;margin:0 0 10px 0;">{escape(note.text)}</p>'
        )

    # B7. A brief nobody opens is the signal that matters most, so it has to be measurable.
    pixel = ""
    if brief_id is not None:
        src = f"{base_url.rstrip('/')}/b/{brief_id}.gif"
        pixel = (
            f'<img src="{escape(src, quote=True)}" width="1" height="1" alt="" '
            'style="display:block;border:0;" />'
        )

    date_line = escape(brief.generated_for_date)
    return (
        # The outer table carries the cream explicitly. Setting it only on <body> is the
        # single most common way an email turns white in one client and cream in another.
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="background:{PAPER};margin:0;padding:0;">'
        f'<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="max-width:560px;background:{PAPER};">'
        f'<tr><td style="border-bottom:2px solid {INK};padding:0 0 8px 0;'
        f"font-family:{FONT};font-size:12px;font-weight:700;text-transform:uppercase;"
        f'letter-spacing:0.09em;color:{INK};">Backglass &middot; {date_line}</td></tr>'
        f'<tr><td style="padding:16px 0 0 0;">{"".join(blocks)}</td></tr>'
        f'<tr><td style="border-top:2px solid {INK};padding:8px 0 0 0;'
        f'font-family:{FONT};font-size:11px;color:{INK_MUTED};">'
        f"{brief.word_count()} words{pixel}</td></tr>"
        f"</table></td></tr></table>"
    )


def to_text(brief: Brief, *, base_url: str) -> str:
    """The plaintext alternative, from the same data. docs/10 §Email delivery."""
    brief.assert_provenance()
    out: list[str] = [f"BACKGLASS · {brief.generated_for_date}", ""]
    for section in brief.ordered():
        out.append(section.title.upper())
        for line in section.lines:
            marker = STATUS[line.status][1] + " " if line.status in STATUS else ""
            out.append(f"  {marker}{line.text}")
            out.append(f"    {line.provenance.url(base_url)}")
        out.extend(f"  ({note.text})" for note in section.notes)
        out.append("")
    out.extend(f"({note.text})" for note in brief.notes)
    return "\n".join(out).strip()


def to_markdown(brief: Brief, *, base_url: str) -> str:
    """What gets stored in `brief.content_md` for the feedback loop."""
    out: list[str] = [f"# {brief.generated_for_date}", ""]
    for section in brief.ordered():
        out.append(f"## {section.title}")
        for line in section.lines:
            tag = f"`{STATUS[line.status][1]}` " if line.status in STATUS else ""
            out.append(
                f"- {tag}{line.text} "
                f"([{line.provenance.label}]({line.provenance.url(base_url)}))"
            )
        out.extend(f"- _{note.text}_" for note in section.notes)
        out.append("")
    out.extend(f"_{note.text}_" for note in brief.notes)
    return "\n".join(out).strip()


# ── reading a stored brief back ───────────────────────────────────────────
# The brief row keeps `content_md` and nothing else that carries a source URL —
# `items_json` stores provenance as a label, not a link. So the stored markdown is the
# only record of B2 provenance for a brief that has already been generated, and reading
# a brief back means reading that.
#
# This is deliberately NOT a markdown parser and must never become one: it accepts the
# one shape `to_markdown` above emits, and nothing else. A general parser (or a
# dependency that is one) would be a second definition of the brief format, free to
# disagree with the first.

#: The inverse of the label column in STATUS, so a stored `OVERDUE` tag comes back as
#: the status key the chip vocabulary is written in.
_STATUS_BY_LABEL = {label: status for status, (_, label, _) in STATUS.items()}

_STORED_LINE = re.compile(
    r"^- (?:`(?P<tag>[A-Z ]+)` )?(?P<text>.+) \(\[(?P<label>[^\]]*)\]\((?P<url>[^)]*)\)\)$"
)
_STORED_NOTE = re.compile(r"^(?:- )?_(?P<text>.+)_$")
_STORED_HEADING = re.compile(r"^## (?P<title>.+)$")
_STORED_DATE = re.compile(r"^# (?P<date>.+)$")


@dataclass(frozen=True)
class StoredRef:
    """Provenance recovered from a stored brief. Satisfies the `Provenance` protocol.

    Neither a SourceRef nor a LedgerRef: the markdown kept the rendered label and the
    rendered href, not the source row they were built from, and inventing a row id to
    reconstruct one would be a fabricated citation.
    """

    described: str
    href: str

    @property
    def label(self) -> str:
        return self.described

    def url(self, base: str) -> str:
        """Re-base the dashboard's own links; leave external deep links alone.

        The href was written at generation time against DASHBOARD_BASE_URL. Serving it
        back verbatim sends the reader to whatever host that was, which is not
        necessarily the one they are reading on — a brief generated before a port change
        would link into nothing. A link that already points at `base` comes back as a
        path, so it resolves against the running dashboard; a Gmail deep link does not
        match and is returned exactly as generated.
        """
        prefix = base.rstrip("/")
        if prefix and self.href.startswith(f"{prefix}/"):
            return self.href[len(prefix) :]
        return self.href


def parse_markdown(content_md: str, *, kind: str = "daily") -> Brief:
    """A stored `content_md` back as a `Brief`. The inverse of `to_markdown`.

    Section priority is the stored order, so `Brief.ordered()` replays the brief exactly
    as it was sent rather than re-sorting it by a docs/05 precedence the truncation pass
    has already applied.

    A line that does not match the stored shape becomes a `Note`, never a `Line`. B2 is
    the reason: a claim whose provenance could not be read is a claim with no source, and
    the one thing it must not do is render as a sourced one. As a Note it keeps its text
    and loses its authority, which is the honest outcome.
    """
    brief = Brief(generated_for_date="", kind=kind)
    section: Section | None = None
    for raw in content_md.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        heading = _STORED_HEADING.match(line)
        if heading:
            section = Section(priority=len(brief.sections) + 1, title=heading["title"])
            brief.sections.append(section)
            continue
        stamp = _STORED_DATE.match(line)
        if stamp:
            brief.generated_for_date = stamp["date"]
            continue
        note = _STORED_NOTE.match(line)
        if note:
            (section.notes if section else brief.notes).append(Note(note["text"]))
            continue
        claim = _STORED_LINE.match(line)
        if claim and section is not None:
            section.lines.append(
                Line(
                    text=claim["text"],
                    provenance=StoredRef(described=claim["label"], href=claim["url"]),
                    status=_STATUS_BY_LABEL.get(claim["tag"] or ""),
                )
            )
            continue
        (section.notes if section else brief.notes).append(Note(line.lstrip("- ")))
    brief.sections = [s for s in brief.sections if not s.empty]
    return brief
