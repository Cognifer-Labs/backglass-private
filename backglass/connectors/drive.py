"""Google Drive. docs/07 §Google Drive.

  - Drive API changes feed, same cursor pattern as Gmail's historyId.
  - Text extracted from Docs, Sheets and PDFs. Binary and media skipped.
  - Only files owned by or explicitly shared with the owner. **Do not crawl shared drives.**

The last of those is a data-boundary requirement wearing a performance requirement's
clothes. docs/08 extends the boundary to attachments and Drive files "by ownership and
sharing", so a shared drive full of client material is precisely what must not be walked.
`corpora='user'` plus the ownership filter is that rule in code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from backglass.connectors.base import Cursor, Health, SourceItem, content_hash
from backglass.connectors.boundary import Boundary

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

#: docs/07: "Text extracted from Docs, Sheets, and PDFs. Binary and media skipped."
EXPORTABLE = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DOWNLOADABLE = {"application/pdf", "text/plain", "text/markdown", "text/csv"}

#: A spreadsheet export can be enormous and is mostly numbers. docs/02's per-item ceiling
#: parks anything larger at extraction; this avoids pulling it over the wire at all.
MAX_BYTES = 400_000


@dataclass
class DriveConnector:
    label: str
    service: Any
    boundary: Boundary
    owner_emails: tuple[str, ...] = ()

    cursor: Cursor = None
    excluded: int = 0
    excluded_by_rule: dict[str, int] = field(default_factory=dict)
    _error: str | None = None

    @property
    def name(self) -> str:
        return f"drive:{self.label}"

    def health(self) -> Health:
        if self._error:
            return Health(name=self.name, ok=False, detail=self._error)
        try:
            self.service.about().get(fields="user").execute()
        except Exception as exc:  # noqa: BLE001
            return Health(name=self.name, ok=False, detail=_safe(exc))
        return Health(name=self.name, ok=True)

    def fetch(self, since: Cursor) -> Iterator[SourceItem]:
        self.excluded = 0
        self.excluded_by_rule = {}

        if not since:
            token = self.service.changes().getStartPageToken().execute()
            self.cursor = str(token.get("startPageToken"))
            yield from self._initial_scan()
            return

        page: Any = since
        while page:
            response = (
                self.service.changes()
                .list(
                    pageToken=page,
                    spaces="drive",
                    # docs/07: only the owner's own corpus. Never a shared drive.
                    includeItemsFromAllDrives=False,
                    supportsAllDrives=False,
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,file(id,name,mimeType,size,modifiedTime,"
                        "owners(emailAddress),permissions(emailAddress),trashed,webViewLink))"
                    ),
                )
                .execute()
            )
            for change in response.get("changes", []) or []:
                if change.get("removed"):
                    continue
                item = self._to_item(change.get("file") or {})
                if item is not None:
                    yield item
            if response.get("newStartPageToken"):
                self.cursor = str(response["newStartPageToken"])
            page = response.get("nextPageToken")

    def _initial_scan(self) -> Iterator[SourceItem]:
        request: Any = self.service.files().list(
            # `corpora='user'` is the "do not crawl shared drives" rule, in one argument.
            corpora="user",
            includeItemsFromAllDrives=False,
            supportsAllDrives=False,
            q="trashed = false",
            orderBy="modifiedTime desc",
            pageSize=100,
            fields=(
                "nextPageToken,files(id,name,mimeType,size,modifiedTime,"
                "owners(emailAddress),permissions(emailAddress),trashed,webViewLink)"
            ),
        )
        while request is not None:
            response = request.execute()
            for meta in response.get("files", []) or []:
                item = self._to_item(meta)
                if item is not None:
                    yield item
            request = self.service.files().list_next(request, response)

    def _to_item(self, meta: dict[str, Any]) -> SourceItem | None:
        if not meta or meta.get("trashed"):
            return None
        mime = str(meta.get("mimeType", ""))
        if mime not in EXPORTABLE and mime not in DOWNLOADABLE:
            return None  # binary and media skipped
        try:
            if int(meta.get("size") or 0) > MAX_BYTES:
                return None
        except (TypeError, ValueError):
            pass

        owners = [o.get("emailAddress", "") for o in meta.get("owners", []) or []]
        shared_with = [p.get("emailAddress", "") for p in meta.get("permissions", []) or []]

        # docs/07: "Only files owned by or explicitly shared with the owner."
        if self.owner_emails:
            reachable = {e.lower() for e in owners + shared_with if e}
            if not reachable & set(self.owner_emails):
                return None

        # docs/08: attachments and Drive files inherit the boundary "by ownership and
        # sharing". A document shared with a denylisted domain is client material.
        verdict = self.boundary.check([*owners, *shared_with])
        if not verdict.allowed:
            self.excluded += 1
            rule = verdict.matched_rule or "?"
            self.excluded_by_rule[rule] = self.excluded_by_rule.get(rule, 0) + 1
            return None

        text = self._text_of(str(meta["id"]), mime)
        if not text or not text.strip():
            return None

        occurred = str(meta.get("modifiedTime") or "").replace("Z", "+00:00")
        if not occurred:
            return None
        title = str(meta.get("name") or "Untitled")
        return SourceItem(
            source=self.name,
            external_id=str(meta["id"]),
            occurred_at=occurred,
            author=owners[0] if owners else None,
            title=title,
            body_text=text.strip()[:MAX_BYTES],
            raw_json=json.dumps(
                {"mime_type": mime, "web_view_link": meta.get("webViewLink")}, sort_keys=True
            ),
            content_hash=content_hash(
                author=owners[0] if owners else None,
                title=title,
                body_text=text,
                occurred_at=occurred,
            ),
        )

    def _text_of(self, file_id: str, mime: str) -> str:
        try:
            if mime in EXPORTABLE:
                raw = (
                    self.service.files()
                    .export(fileId=file_id, mimeType=EXPORTABLE[mime])
                    .execute()
                )
            else:
                raw = self.service.files().get_media(fileId=file_id).execute()
        except Exception:  # noqa: BLE001 - one unreadable file is not a source failure
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw or "")


def _safe(exc: Exception) -> str:
    import re

    text = f"{type(exc).__name__}: {exc}"
    return re.sub(r"(access_token|key|token)=[^&\s]+", r"\1=[redacted]", text)[:500]
