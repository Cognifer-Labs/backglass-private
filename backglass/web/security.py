"""Request guards for a localhost app that is not an authenticated one.

The dashboard binds to 127.0.0.1 and has no login, because docs/08 says the owner's
ledger never leaves the machine and one person does not authenticate to their own
desk. That reasoning is sound about the *network* and wrong about the *browser*:
loopback is not an origin boundary. Every page the owner visits in any browser on
this Mac can reach 127.0.0.1:8765, and every write endpoint here takes an empty body
and no token, so a forged form post is byte-identical to a real click. Two guards
close that, and they are deliberately the smallest thing that does:

1. **Host allowlist.** A request whose Host names anything but loopback is refused.
   Without this, a hostile page on evil.tld with a short-TTL record that rebinds to
   127.0.0.1 becomes same-origin with the dashboard and can *read* every response —
   commitments, people, memory, credential errors. The bind address does not stop
   this; only checking the name the browser used does.

2. **Cross-site write refusal.** State-changing methods must arrive same-origin.
   `Sec-Fetch-Site` is the browser's own statement about who initiated the request
   and cannot be set by page script; `Origin` is the fallback for anything that
   omits it. When neither header is present the caller is not a browser (curl, the
   test client, a script), and a non-browser was never subject to CSRF in the first
   place — refusing it would buy nothing and break the CLI.

Deliberately NOT here: a CSRF token. Tokens are for apps with sessions and multiple
users; this app has neither, and a token would add a rendering dependency to every
one of ~35 forms to defend a threat the header check already covers.

The pixel route (`/b/{id}.gif`) is a GET and therefore outside guard 2 by design —
it is loaded by a mail client on some other origin, which is the entire point. It
stays reachable, and it stays idempotent (`WHERE opened_at IS NULL`), so the residual
exposure is a metric a hostile page could mark read, not a write to the ledger.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

#: Hostnames that mean "this machine, addressed as this machine". A port is never
#: part of the check: rebinding attacks turn on the *name*, and pinning the port here
#: would only couple this module to whatever `--port` the owner passed.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: The methods that change the ledger. GET/HEAD/OPTIONS are covered by the Host
#: allowlist alone.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Sec-Fetch-Site values that mean the request started somewhere else. "none" is a
#: user-typed URL or a bookmark; "same-origin" is the dashboard's own HTMX calls.
FOREIGN_SITES = frozenset({"cross-site", "same-site"})

SECURITY_HEADERS = {
    # Clickjacking: the dashboard's buttons are one-click destructive (Drop, Merge),
    # so a transparent frame over a hostile page is a real path to the same writes
    # guard 2 refuses directly.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # 'unsafe-inline' is honest: the page's keyboard handler and fold-persistence
    # script are inline by design (docs/10 — no build step, no bundler). The value
    # of the policy here is the default-src/connect-src floor, which stops an
    # injected string from reaching a remote host even if one ever got through
    # Jinja's autoescape.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
}


def _hostname(value: str) -> str:
    """The host part of a Host or Origin header, lowercased, without the port.

    Host arrives bare (`127.0.0.1:8765`); Origin arrives as a URL
    (`http://127.0.0.1:8765`). `urlsplit` handles the second and leaves the first
    alone once a scheme is prepended, which also gets IPv6 bracket forms right.
    """
    raw = value.strip().lower()
    if "://" not in raw:
        raw = f"//{raw}"
    return urlsplit(raw).hostname or ""


def is_loopback(value: str) -> bool:
    return _hostname(value) in LOOPBACK_HOSTS


def is_cross_site_write(request: Request) -> bool:
    """True when a state-changing request did not come from the dashboard itself."""
    if request.method not in WRITE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site in FOREIGN_SITES
    origin = request.headers.get("origin", "")
    if origin:
        # "null" (a sandboxed frame, a data: URL) is not this app.
        return not is_loopback(origin)
    return False


def install(app: FastAPI) -> None:
    """Add the guards to `app`.

    Ordering matters and is not obvious: Starlette inserts each middleware at the
    front of the stack, so the one registered LAST runs FIRST. This must be called
    after every other `app.middleware` registration, or a refused request would
    still run the sidebar query against the ledger before being turned away.
    """

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "")
        if not is_loopback(host):
            # 421 is the honest code: the request reached a server that does not
            # answer for that name. It also tells a rebinding attempt nothing.
            return PlainTextResponse(
                "Backglass answers on loopback only.",
                status_code=421,
                headers=SECURITY_HEADERS,
            )
        if is_cross_site_write(request):
            return PlainTextResponse(
                "Cross-site writes are refused.",
                status_code=403,
                headers=SECURITY_HEADERS,
            )
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
