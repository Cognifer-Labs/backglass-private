# Vendored assets

`htmx.min.js` — htmx 2.0.4, fetched 2026-07-30 from
`https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`.

    sha256  e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447
    bytes   50917

Vendored rather than loaded from a CDN so the dashboard works offline and so there is no
third party in the request path for a page rendering the owner's commitments. docs/10
§Web layer rules out npm and a build step; a single checked-in file is the version of
"no build step" that also survives the CDN going away.

To upgrade: fetch the new file, record its hash here, and re-run the dashboard tests.

`fonts/Mortend-Bold.woff2` — Mortend Bold display face, converted 2026-08-01 from the
owner's `MortendBold-2Odle.ttf` (fontTools woff2 compress). Source:
`https://www.fontspace.com/mortend-font-f61373`.

    sha256  42b544d2d548fe377828c3fcd12e5dfc8812b4084e99d5877112ec6592df454f
    bytes   10992
    license Freeware, Non-Commercial — fine for this single-owner personal tool;
            replace before any commercial distribution.

One weight only (Bold), declared `font-weight:700` in `dashboard.css` so it slots into
the existing display rules. It is the first entry in `--font-cond` (design/tokens.css);
the condensed Helvetica stack remains the fallback.
