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
