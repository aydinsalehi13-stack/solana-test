/* Runs before the first paint so a saved dark theme never flashes light. Light is the default. */
(function () {
  var t = 'light';
  try { t = localStorage.getItem('desk_theme') || 'light'; } catch (e) { /* private mode */ }
  document.documentElement.dataset.theme = t === 'dark' ? 'dark' : 'light';
})();
