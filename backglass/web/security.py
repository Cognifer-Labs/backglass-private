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

Both guards assume the bind is loopback. Guard 2 deliberately fails open when neither
Sec-Fetch-Site nor Origin is present ("a non-browser was never subject to CSRF") —
that reasoning is only true while non-browsers can already reach the socket some
other way, i.e. while the bind is 127.0.0.1. Under `--host 0.0.0.0
--expose-unauthenticated` a remote curl with a forged loopback Host header passes
both guards by design of that flag; nothing in this module defends a non-loopback
bind, and nothing should be added here that pretends to.

`DASHBOARD_ALLOWED_HOSTS` extends guard 1 with extra names, and does not weaken that
sentence, because the bind stays 127.0.0.1. It exists for a reverse proxy that
terminates on this machine and forwards to loopback — `tailscale serve`, whose
listener is tailscaled on the tailnet address and whose upstream is 127.0.0.1:8765.
The socket is still unreachable from the network; what reaches it is a proxy that
already decided the caller is one of the owner's own devices. That is why the name
goes in configuration rather than being inferred from `X-Forwarded-Host`: a forwarded
header is the *caller's* claim, and guard 1 exists precisely because the caller's
claim about who they are addressing cannot be trusted. An entry here is the owner
naming a name, once, on the machine.

It follows that an added name must reach BOTH guards, not just the first. Guard 2's
Origin fallback compares against the same set: a browser that omits Sec-Fetch-Site
(Safari before 16.4, some webviews) sends `Origin: https://<the-proxy-name>` on its
own same-origin POSTs, and checking that against loopback alone would 403 every write
the owner made through the proxy while letting the reads through — the worst of the
two failure modes, because it looks like a broken button rather than a refusal.

Deliberately NOT here: a CSRF token. Tokens are for apps with sessions and multiple
users; this app has neither, and a token would add a rendering dependency to every
one of ~35 forms to defend a threat the header check already covers.

The pixel route (`/b/{id}.gif`) is a GET and therefore outside guard 2 by design —
it is loaded by a mail client on some other origin, which is the entire point. It
stays reachable, and it stays idempotent (`WHERE opened_at IS NULL`), so the residual
exposure is a metric a hostile page could mark read, not a write to the ledger.
"""

from __future__ import annotations

from collections.abc import Iterable
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


def resolve_allowed(extra: Iterable[str] = ()) -> frozenset[str]:
    """The names this app answers for: loopback, plus whatever the owner configured.

    Entries are run through `_hostname` rather than trusted as typed, so a value
    copied out of a browser bar (`https://backglass-mac.tail1234.ts.net/`) or off
    `tailscale serve status` (with its `:443`) means the same thing as the bare name.
    An entry that reduces to nothing — a stray comma, a lone port — is dropped rather
    than admitted as the empty host, which `_hostname` also returns for a missing
    Host header.
    """
    return LOOPBACK_HOSTS | frozenset(h for name in extra if (h := _hostname(name)))


def is_cross_site_write(
    request: Request, allowed: frozenset[str] = LOOPBACK_HOSTS
) -> bool:
    """True when a state-changing request did not come from the dashboard itself."""
    if request.method not in WRITE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site in FOREIGN_SITES
    origin = request.headers.get("origin", "")
    if origin:
        # "null" (a sandboxed frame, a data: URL) is not this app.
        return _hostname(origin) not in allowed
    return False


def install(app: FastAPI, allowed_hosts: Iterable[str] = ()) -> None:
    """Add the guards to `app`, answering for loopback plus `allowed_hosts`.

    Ordering matters and is not obvious: Starlette inserts each middleware at the
    front of the stack, so the one registered LAST runs FIRST. This must be called
    after every other `app.middleware` registration, or a refused request would
    still run the sidebar query against the ledger before being turned away.

    The set is resolved once here, at wiring time, so a malformed entry costs nothing
    per request and the guard compares against a frozen set rather than re-parsing
    configuration on every call.
    """
    allowed = resolve_allowed(allowed_hosts)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "")
        if _hostname(host) not in allowed:
            # 421 is the honest code: the request reached a server that does not
            # answer for that name. It also tells a rebinding attempt nothing.
            # The wording stays independent of what is configured: naming the allowed
            # hosts here would hand a rebinding attempt the one thing it lacks.
            return PlainTextResponse(
                "Backglass does not answer for that name.",
                status_code=421,
                headers=SECURITY_HEADERS,
            )
        if is_cross_site_write(request, allowed):
            return PlainTextResponse(
                "Cross-site writes are refused.",
                status_code=403,
                headers=SECURITY_HEADERS,
            )
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
