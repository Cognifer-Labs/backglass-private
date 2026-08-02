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

`fonts/Oswald-Bold.woff2` — Oswald Bold display face, converted 2026-08-02 from the
Google Fonts variable source `ofl/oswald/Oswald[wght].ttf` (fontTools
`varLib.instancer` pinned to `wght=700`, then `ttLib.woff2 compress`). Source:
`https://github.com/google/fonts/raw/main/ofl/oswald/Oswald%5Bwght%5D.ttf`.

    sha256  23b071107844095a858ca18c9ebfa917bf899d111d07522103308e3de5851c9d
    bytes   33908
    license OFL 1.1 — permissive, ships freely.

One weight only (Bold), declared `font-weight:700` in `dashboard.css` so it slots into
the existing display rules. It is the first entry in `--font-cond` (design/tokens.css);
the condensed Helvetica stack remains the fallback.
