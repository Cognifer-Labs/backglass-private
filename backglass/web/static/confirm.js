/* Drop asks twice, in the page.
 *
 * docs/06: "No confirmation dialogs except for drop, which is destructive and rare."
 * That was implemented with `hx-confirm`, which calls `window.confirm` — and in the
 * desktop app's WKWebView nothing implements the confirm panel, so the call returns
 * false and htmx cancels the request. Drop was not slow or refused: it never left the
 * page. The server logged zero drop requests across a day of clicking, while resolve
 * and snooze from the same card went through, because those two carry no dialog.
 *
 * So the confirmation stops being native and becomes the button itself: the first
 * press arms it, the second sends it. That is the same two-decision shape a dialog
 * has, minus the dialog — no focus to trap, no escape key to bind, nothing to render
 * over the board, and it works in any webview because it is the page's own state.
 * It also keeps the keyboard honest: `d` twice drops, and `d` once is a question,
 * which is exactly what the mouse does.
 *
 * `hx-confirm` stays in the markup. It is still the declaration that this action is
 * destructive — this file reads it as the question to ask and as the label a screen
 * reader gets, so the template goes on saying what it means and only the mechanism
 * moves. An element that arrives with `hx-confirm` tomorrow inherits this.
 */
(function () {
  // Long enough to read "Sure?" and reach for the button, short enough that a Drop
  // armed by a misclick is not still armed when the owner comes back to the tab.
  var DISARM_AFTER = 4000;
  var armed = null;
  var timer = null;

  function disarm() {
    if (timer) { window.clearTimeout(timer); timer = null; }
    if (!armed) return;
    // Restored from the element rather than from a closure: the board re-renders on
    // every write, so the node this was armed on may not be in the document by the
    // time anything disarms it, and reading state off a detached node is safe while
    // reading it off a stale closure is how the label comes back wrong.
    if (armed.dataset.label !== undefined) {
      armed.textContent = armed.dataset.label;
      delete armed.dataset.label;
    }
    delete armed.dataset.armed;
    armed = null;
  }

  function arm(button) {
    disarm();
    armed = button;
    button.dataset.label = button.textContent;
    button.dataset.armed = 'true';
    button.textContent = 'Sure?';
    timer = window.setTimeout(disarm, DISARM_AFTER);
  }

  document.body.addEventListener('htmx:confirm', function (event) {
    // htmx fires this for every request; only the ones carrying hx-confirm have a
    // question, and the rest must be left alone or nothing on the page would send.
    if (!event.detail.question) return;
    // Unconditionally: whether this press arms or sends, the native dialog never runs.
    event.preventDefault();
    var button = event.detail.elt;
    if (button.dataset.armed === 'true') {
      disarm();
      // `true` skips htmx's own confirm — it has already been asked and answered.
      event.detail.issueRequest(true);
      return;
    }
    // The visible label shrinks to one word, so the stakes move to the accessible
    // name, which is the hx-confirm sentence the template already wrote.
    button.setAttribute('aria-label', event.detail.question);
    arm(button);
  });

  // Anywhere else is an answer of no. Capture, because the board's own click handler
  // stops at the card and this has to see presses that land outside one.
  document.addEventListener('click', function (event) {
    if (armed && event.target !== armed) disarm();
  }, true);
  document.addEventListener('keydown', function (event) {
    if (armed && event.key === 'Escape') disarm();
  });
  // A write swaps the whole panel, taking the armed node with it. Without this the
  // module would hold a detached button forever and the next Drop would be its second
  // press — a destructive action fired by one click, which is the one outcome this
  // file exists to prevent.
  document.body.addEventListener('htmx:afterSwap', disarm);
})();
