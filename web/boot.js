/* Runs before first paint: a hosted app never flashes a standalone canvas while the shell's
 * appearance message is on its way. */
try { if (window.parent !== window) document.documentElement.classList.add('wz-hosted'); }
catch (e) { document.documentElement.classList.add('wz-hosted'); }

/* Finances was Money, at /money/ (money#28). The box keeps answering that old address with this same app, so an old
 * link or bookmark is moved to /finances/, once that address is known to answer as Finances (never to a dead page). */
(function () {
  var m = /^\/money(\/.*)?$/.exec(location.pathname);
  if (!m || !window.fetch) return;
  fetch('/finances/api/health', { cache: 'no-store', credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (h) { if (h && h.app === 'finances') location.replace('/finances' + (m[1] || '/') + location.search + location.hash); })
    .catch(function () {});
})();
