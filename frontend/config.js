/* NitoCAD frontend config. One place for the backend address.
   Every page that talks to the backend reads window.NITOCAD_CONFIG.apiBase.
   To point the site at a different Railway service, change the default below.
   For a quick test without redeploying, run this in the browser console:
     localStorage.setItem('nitocad_api_base', 'https://your-service.up.railway.app')
   and clear it with localStorage.removeItem('nitocad_api_base'). */
(function () {
  "use strict";
  var DEFAULT_API_BASE = "https://nitocad-production-2df5.up.railway.app";
  var base = DEFAULT_API_BASE;
  try {
    var override = localStorage.getItem("nitocad_api_base");
    if (override) base = override;
  } catch (e) { /* storage blocked, use the default */ }
  window.NITOCAD_CONFIG = { apiBase: base.replace(/\/+$/, "") };

  /* Live prices from the backend (GET /config/chain). Fills any element marked
     data-price="credit | pack1000 | pack10000 | pack1000_credits | pack10000_credits |
     rate1000 | rate10000". The text already in the page is the fallback if the
     request fails, so the page never shows an empty price. */
  function fmt(n) { return String(parseFloat(Number(n).toFixed(6))); }
  function fmtInt(n) { return Number(n).toLocaleString("en-US"); }
  window.NITOCAD_CONFIG.loadPrices = function () {
    return fetch(window.NITOCAD_CONFIG.apiBase + "/config/chain")
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (c) {
        var v = {
          credit: fmt(c.credit_price_bot),
          pack1000: fmt(c.pack_1000_price_bot),
          pack10000: fmt(c.pack_10000_price_bot),
          pack1000_credits: fmtInt(c.pack_1000_credits),
          pack10000_credits: fmtInt(c.pack_10000_credits),
          rate1000: fmt(c.pack_1000_price_bot / c.pack_1000_credits),
          rate10000: fmt(c.pack_10000_price_bot / c.pack_10000_credits)
        };
        Object.keys(v).forEach(function (k) {
          document.querySelectorAll('[data-price="' + k + '"]').forEach(function (el) { el.textContent = v[k]; });
        });
      })
      .catch(function () { /* keep the fallback text */ });
  };
  document.addEventListener("DOMContentLoaded", function () {
    if (document.querySelector("[data-price]")) window.NITOCAD_CONFIG.loadPrices();
  });
})();
