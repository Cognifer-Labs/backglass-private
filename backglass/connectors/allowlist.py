"""The named-conversations rule, shared by every messaging connector.

docs/07 states it for Instagram — "a personal tool reads the handful of threads the owner
names" — but the rule is not about Instagram. Any connector pointed at a message store is
pointed at other people's words, most of which the owner never meant to file anywhere. An
inbox-wide read is the wrong default for all of them, so the allowlist lives here rather
than inside one connector, and the second connector to need it imports rather than
reimplements.

Matching is deliberately forgiving about shape and strict about membership: whitespace is
collapsed and case is folded, so "Pih Ball" and "pih ball" are the same chat, but nothing
is matched by prefix or substring — a chat called "Family" must not pull in "Family
Reunion 2027".
"""

from __future__ import annotations

from collections.abc import Sequence


def normalise(name: str) -> str:
    return " ".join(name.split()).casefold()


class Allowlist:
    """The owner's named chats and people."""

    def __init__(self, entries: Sequence[str]) -> None:
        self._entries = frozenset(normalise(e) for e in entries if e.strip())

    def __bool__(self) -> bool:
        return bool(self._entries)

    def allows(self, *, title: str | None, participants: Sequence[str]) -> bool:
        """A group thread by title; a one-to-one thread by either participant.

        The participant fallback is capped at two because a group's membership is not
        how the owner names it: allowing a ten-person thread because one member is on
        the list would let anybody drag the owner's whole ledger into a conversation
        they never opted into.
        """
        if title and normalise(title) in self._entries:
            return True
        if len(participants) <= 2:
            return any(normalise(p) in self._entries for p in participants)
        return False
