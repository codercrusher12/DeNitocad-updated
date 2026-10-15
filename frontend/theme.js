/* NitoCAD theme switch. Load in <head> so the theme applies before first paint. */
(function () {
  var KEY = 'nitocad-theme';
  var root = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function system() { return mq && mq.matches ? 'dark' : 'light'; }

  function apply(t) {
    root.setAttribute('data-theme', t);
    var m = document.querySelector('meta[name="theme-color"]');
    if (!m && document.head) { m = document.createElement('meta'); m.name = 'theme-color'; document.head.appendChild(m); }
    if (m) m.setAttribute('content', t === 'dark' ? '#0A111C' : '#FBF8F3');
    var b = document.querySelector('.theme-toggle');
    if (b) {
      b.setAttribute('aria-pressed', t === 'dark' ? 'true' : 'false');
      b.setAttribute('aria-label', t === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
      b.title = b.getAttribute('aria-label');
    }
  }

  apply(stored() || system());

  if (mq && mq.addEventListener) {
    mq.addEventListener('change', function () { if (!stored()) apply(system()); });
  }

  function build() {
    if (document.querySelector('.theme-toggle')) return;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-toggle';
    b.innerHTML =
      '<svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>' +
      '<svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
    b.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      save(next);
      apply(next);
    });

    var burger = document.querySelector('header .burger');
    var back = document.querySelector('.lg-head .back');
    if (burger && burger.parentNode) {
      burger.parentNode.insertBefore(b, burger);
    } else if (back && back.parentNode) {
      var slot = document.createElement('div');
      slot.className = 'theme-slot';
      back.parentNode.insertBefore(slot, back);
      slot.appendChild(back);
      slot.appendChild(b);
    } else {
      b.classList.add('floating');
      document.body.appendChild(b);
    }
    apply(root.getAttribute('data-theme'));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
