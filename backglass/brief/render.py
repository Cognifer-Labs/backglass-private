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
"""

from __future__ import annotations

from html import escape

from backglass.brief.model import Brief, Line

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
