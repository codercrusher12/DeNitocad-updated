/* NitoCAD dark-only theme. Load in <head> so it applies before first paint. */
(function () {
  var root = document.documentElement;
  root.setAttribute('data-theme', 'dark');
  try { localStorage.removeItem('nitocad-theme'); } catch (e) {}
  var m = document.querySelector('meta[name="theme-color"]');
  if (!m && document.head) { m = document.createElement('meta'); m.name = 'theme-color'; document.head.appendChild(m); }
  if (m) m.setAttribute('content', '#000000');
})();
