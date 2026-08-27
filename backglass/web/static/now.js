/* The Now line, moving.
 *
 * The schedule was a photograph. `_now_top` runs once, on the server, at the moment the
 * page is built — so a day page opened at 8am still says "Now" at 8am when it is one in
 * the afternoon, and the one mark on the canvas whose whole job is to say *where you
 * are* is the one thing on it that is wrong. Owner's ask, 2026-08-27: "make the time
 * move in real time."
 *
 * What this does and nothing else: re-run `_now_top`'s arithmetic, in the browser, once
 * a quarter of a minute, and set `top`. Every input comes from the server as a data
 * attribute on the canvas — the drawn window in minutes, the pixels per minute, the day,
 * the timezone — so the line and the ruler under it can only ever disagree if the ruler
 * is wrong too. No fetch, no htmx poll: a swap every minute would fight hover, focus and
 * the arm-then-send confirmation on every block, to move one element two pixels.
 *
 * The timezone is the owner's, not the browser's. `view.tz` is what the day was drawn
 * in, and the owner moves between UTC-7 and UTC+5:30 — a line placed from the laptop's
 * clock would sit twelve and a half hours off the ruler beside it on the flight over.
 * `Intl.DateTimeFormat` resolves both the clock and the date in that zone, which is also
 * what makes midnight work: past it the day no longer matches `data-now-day`, the line
 * hides, and it does not slide off the bottom of yesterday.
 *
 * Not §9 motion. design-system.md §9 governs things that animate; this is a position
 * being corrected, at half a pixel a minute on the week grid and one on the day. There
 * is deliberately no transition on `top` — §9 rule 1 is transform and opacity only, and
 * a movement no eye can resolve does not need easing to be noticed. `motion.js` keeps
 * its two moments; this is a third thing and lives in its own file.
 *
 * Elements are moved, never created. The server draws the line on every view of today
 * and hides it while the clock is outside the drawn window, so the browser's whole job
 * is `top` and `hidden`. Everything is re-queried each tick rather than bound once,
 * which is what makes it survive htmx: `#panel-timeline` is replaced outerHTML by every
 * action on the page, and a listener attached to the old canvas would be gone with it.
 */
(function () {
  const TICK_MS = 15000;

  /* Minute of the day and the calendar date, both in `tz`. One formatter per zone,
     built once: `Intl.DateTimeFormat` is expensive to construct and free to reuse, and
     this runs four times a minute forever. */
  const formatters = new Map();
  function readClock(tz) {
    let fmt = formatters.get(tz);
    if (!fmt) {
      try {
        fmt = new Intl.DateTimeFormat('en-CA', {
          timeZone: tz,
          hourCycle: 'h23',
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        });
      } catch (err) {
        // An unknown zone is a server-side problem and not one to take the page down
        // for: the line stops where the server put it, which is where it was anyway.
        return null;
      }
      formatters.set(tz, fmt);
    }
    const parts = {};
    for (const part of fmt.formatToParts(new Date())) parts[part.type] = part.value;
    if (!parts.year || !parts.hour) return null;
    return {
      day: parts.year + '-' + parts.month + '-' + parts.day,
      minute: Number(parts.hour) * 60 + Number(parts.minute),
    };
  }

  function tick() {
    for (const host of document.querySelectorAll('[data-now-tz]')) {
      const line = host.querySelector('.now, .wnow');
      if (!line) continue;
      const clock = readClock(host.dataset.nowTz);
      if (!clock) continue;
      const start = Number(host.dataset.nowStart);
      const end = Number(host.dataset.nowEnd);
      const px = Number(host.dataset.nowPx);
      const inside =
        clock.day === host.dataset.nowDay &&
        clock.minute >= start &&
        clock.minute <= end;
      if (!inside) {
        line.hidden = true;
        continue;
      }
      line.style.top = Math.round((clock.minute - start) * px) + 'px';
      line.hidden = false;
    }
  }

  tick();
  window.setInterval(tick, TICK_MS);
  // A backgrounded tab is throttled to about a minute, and a laptop that slept wakes up
  // with a line an hour stale. Correcting on the way back is the difference between a
  // clock and a timestamp.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') tick();
  });
})();
