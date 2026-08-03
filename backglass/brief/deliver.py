"""Sending the brief.

docs/10 §Email delivery: a transactional provider, **never the Gmail API**.

    "The Gmail connector holds gmail.readonly and docs/08 forbids the system from ever
    sending. Keeping send capability out of that credential entirely is worth an external
    dependency, because it makes 'this system cannot email anyone as me' true by
    construction rather than by discipline."

That property is the reason this module exists as a separate thing rather than a helper on
the Gmail connector, and it is worth protecting: nothing here imports from
`backglass.connectors`.

docs/08: the brief goes to one address, configured, and is never CC'd.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from backglass.config import Settings

RESEND_ENDPOINT = "https://api.resend.com/emails"


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Delivery:
    ok: bool
    detail: str


def unconfigured(settings: Settings) -> str | None:
    """Why this machine cannot deliver a brief, or None when it can.

    Split out of `Sender.send` so a surface that is not sending — the dashboard's
    /brief page — can say why no brief has ever arrived without a second, drifting copy
    of the same four checks. `send` still calls it, so there is one answer to the
    question and both callers get it.
    """
    recipient = settings.brief_to.strip()
    if not recipient:
        return "BRIEF_TO is not set"
    if "," in recipient:
        # docs/08: "The brief is sent to one address, configured, and never CC'd."
        return "BRIEF_TO must be a single address; the brief is never CC'd"
    if not settings.resend_api_key:
        return "RESEND_API_KEY is not set"
    if not settings.brief_from.strip():
        return "BRIEF_FROM is not set"
    return None


class Sender:
    """Sends one brief to one address."""

    def __init__(self, settings: Settings, *, endpoint: str = RESEND_ENDPOINT):
        self.settings = settings
        self.endpoint = endpoint

    def send(self, *, subject: str, html: str, text: str) -> Delivery:
        reason = unconfigured(self.settings)
        if reason:
            raise DeliveryError(reason)
        recipient = self.settings.brief_to.strip()

        payload = json.dumps(
            {
                "from": self.settings.brief_from,
                "to": [recipient],
                "subject": subject,
                "html": html,
                "text": text,
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.settings.resend_api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # The provider echoes the payload in some error bodies, and the payload is the
            # brief. docs/08 keeps content out of error reports, so only the status is kept.
            raise DeliveryError(f"provider returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise DeliveryError(f"delivery failed: {type(exc).__name__}") from exc
        return Delivery(ok=True, detail=str(body.get("id", "sent")))


def subject_for(generated_for_date: str, *, degraded: bool = False) -> str:
    """docs/05 §Tone: declarative and short. No greeting, no encouragement.

    The date leads because the reader is scanning a threaded inbox for this morning's,
    not for one that says "Your Daily Digest".
    """
    prefix = "Backglass — incomplete" if degraded else "Backglass"
    return f"{prefix} — {generated_for_date}"
