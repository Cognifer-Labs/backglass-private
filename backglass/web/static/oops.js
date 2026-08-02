/* The failed-write strip.
 *
 * docs/11 §8's rule applied to write-backs: never let a failure be quiet. Every action
 * on this dashboard is an HTMX post whose response replaces a panel, and HTMX swaps
 * nothing on a non-2xx — so a refused write (a commitment already resolved, a source
 * that vanished) produced no swap, no message, and no console line. A click that does
 * nothing is indistinguishable from a click that worked, which is exactly the silent
 * failure the Sources panel exists to prevent one layer up.
 *
 * Served as a file rather than inlined so the page itself carries no vermilion class
 * name at rest: §8 rule 1 spends that ink on overdue and destroy alone, and a test
 * asserts a page with nothing wrong contains none of it.
 */
(function () {
  var strip = document.getElementById('oops');
  if (!strip) return;

  function show(message) {
    strip.textContent = '';
    var chip = document.createElement('span');
    chip.className = 'chip k-verm';
    chip.textContent = '▲ Not saved';
    var what = document.createElement('span');
    what.className = 'oops-what';
    what.textContent = message;
    var dismiss = document.createElement('button');
    dismiss.className = 'btn';
    dismiss.type = 'button';
    dismiss.textContent = 'Dismiss';
    dismiss.addEventListener('click', function () { strip.hidden = true; });
    strip.appendChild(chip);
    strip.appendChild(what);
    strip.appendChild(dismiss);
    strip.hidden = false;
  }

  // The server refused it.
  document.body.addEventListener('htmx:responseError', function (event) {
    var xhr = event.detail.xhr;
    var detail = '';
    try {
      // FastAPI's HTTPException body. Those detail strings are already written for the
      // owner ("that commitment is not open"), so they are shown verbatim rather than
      // replaced with a generic apology.
      detail = (JSON.parse(xhr.responseText) || {}).detail || '';
    } catch (err) { /* not JSON — fall through to the status line */ }
    show(detail || 'The server refused that (' + xhr.status + ').');
  });

  // It never arrived: the server died, or launchd restarted it mid-click.
  document.body.addEventListener('htmx:sendError', function () {
    show('No answer from Backglass — is the server still running?');
  });

  // A later success means the surface is working again; the stale warning goes.
  document.body.addEventListener('htmx:afterSwap', function () { strip.hidden = true; });
})();
