/* NitoCAD shared sign-in. Used by the landing page and CAD Studio.
   Identity is either a wallet (free signature, no gas) or Google. Both end up as the same
   kind of session, stored in localStorage under the key the backend flow already used,
   so anyone already signed in on the old page stays signed in. */
(function () {
  "use strict";

  var API_BASE = (window.NITOCAD_CONFIG && window.NITOCAD_CONFIG.apiBase) || "https://nitocad-production-2df5.up.railway.app";
  var SESSION_KEY = "nitocad_wallet_session_" + API_BASE;

  // Payment destination and chain params are pinned in the frontend so a backend change
  // can never redirect funds. Keep in sync with Railway env (TREASURY_ADDRESS) and config.py.
  var PINNED_CHAIN = {
    treasury_address: "0x4ef8a7cd822a1b4eb13c5ca565bf21491a08fc37",
    chain_id_hex: "0x2a5",
    chain_name: "BOT Chain",
    rpc_url: "https://rpc.botchain.ai",
    explorer_url: "https://scan.botchain.ai",
    currency_symbol: "BOT"
  };

  var listeners = [];
  var chainConfig = null;

  function emit() {
    var s = getSession();
    listeners.slice().forEach(function (fn) { try { fn(s); } catch (e) { /* listener errors stay local */ } });
  }

  function getSession() {
    var raw = null;
    try { raw = localStorage.getItem(SESSION_KEY); } catch (e) { return null; }
    if (!raw) return null;
    try {
      var s = JSON.parse(raw);
      if (new Date(s.expires_at) <= new Date()) { localStorage.removeItem(SESSION_KEY); return null; }
      return s;
    } catch (e) { return null; }
  }
  function setSession(s) { try { localStorage.setItem(SESSION_KEY, JSON.stringify(s)); } catch (e) {} emit(); }
  function clearSession() { try { localStorage.removeItem(SESSION_KEY); } catch (e) {} emit(); }

  function shortAddress(a) { return a ? a.slice(0, 6) + "…" + a.slice(-4) : ""; }
  function displayName(s) { return s ? (s.email || shortAddress(s.wallet_address)) : ""; }
  function authHeader() { var s = getSession(); return s ? { Authorization: "Bearer " + s.session_id } : {}; }

  function getChainConfig() {
    if (chainConfig) return Promise.resolve(chainConfig);
    return fetch(API_BASE + "/config/chain").then(function (r) { return r.json(); }).then(function (server) {
      if (server.treasury_address && server.treasury_address.toLowerCase() !== PINNED_CHAIN.treasury_address.toLowerCase()) {
        throw new Error("Treasury address mismatch. Payment blocked.");
      }
      chainConfig = Object.assign({}, server, PINNED_CHAIN);
      return chainConfig;
    });
  }

  async function connectWallet() {
    if (!window.ethereum) throw new Error("No wallet found. Install MetaMask or a compatible wallet, or sign in with Google.");
    var cfg = await getChainConfig();
    var accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    try {
      await window.ethereum.request({ method: "wallet_switchEthereumChain", params: [{ chainId: cfg.chain_id_hex }] });
    } catch (err) {
      if (err.code === 4902) {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [{
            chainId: cfg.chain_id_hex, chainName: cfg.chain_name,
            nativeCurrency: { name: "BOT", symbol: cfg.currency_symbol, decimals: 18 },
            rpcUrls: [cfg.rpc_url], blockExplorerUrls: [cfg.explorer_url]
          }]
        });
      } else { throw err; }
    }
    return accounts[0];
  }

  async function signInWithWallet() {
    var account = await connectWallet();
    var nonceResp = await fetch(API_BASE + "/auth/nonce", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wallet_address: account })
    });
    var nonceData = await nonceResp.json();
    if (!nonceResp.ok) throw new Error(nonceData.detail || "Could not start sign-in.");
    var signature = await window.ethereum.request({ method: "personal_sign", params: [nonceData.message, account] });
    var verifyResp = await fetch(API_BASE + "/auth/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wallet_address: account, nonce: nonceData.nonce, signature: signature })
    });
    var session = await verifyResp.json();
    if (!verifyResp.ok) throw new Error(session.detail || "Sign-in failed.");
    setSession(session);
    return session;
  }

  async function signOut() {
    var s = getSession();
    clearSession();
    if (s) {
      try { await fetch(API_BASE + "/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + s.session_id } }); }
      catch (e) { /* already signed out locally */ }
    }
  }

  // Google is optional: only wired up when the backend reports it enabled.
  var googleReady = null;
  function loadGoogle() {
    if (googleReady) return googleReady;
    googleReady = (async function () {
      if (getSession()) return false;
      var cfg;
      try { cfg = await (await fetch(API_BASE + "/config/auth")).json(); } catch (e) { return false; }
      if (!cfg || !cfg.google_signin_enabled) return false;
      await new Promise(function (resolve) {
        var sc = document.createElement("script");
        sc.src = "https://accounts.google.com/gsi/client"; sc.async = true; sc.defer = true;
        sc.onload = resolve; sc.onerror = resolve;
        document.head.appendChild(sc);
      });
      if (!window.google || !window.google.accounts) return false;
      window.google.accounts.id.initialize({
        client_id: cfg.google_client_id,
        callback: async function (response) {
          try {
            var resp = await fetch(API_BASE + "/auth/google", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ id_token: response.credential })
            });
            var session = await resp.json();
            if (!resp.ok) throw new Error(session.detail || "Google sign-in failed.");
            setSession(session);
          } catch (err) {
            if (api.onError) api.onError(err);
          }
        }
      });
      return true;
    })();
    return googleReady;
  }

  // Shows each given button once Google is available and wires it up.
  async function initGoogle(buttons) {
    var ok = await loadGoogle();
    if (!ok) return false;
    buttons.filter(Boolean).forEach(function (btn) {
      btn.style.display = "";
      btn.onclick = function () {
        try {
          window.google.accounts.id.prompt(function (n) {
            if (n.isNotDisplayed() || n.isSkippedMoment()) {
              var host = document.createElement("div");
              host.style.cssText = "position:fixed;left:-9999px;top:0;";
              document.body.appendChild(host);
              window.google.accounts.id.renderButton(host, { theme: "outline", size: "large", text: "signin_with", width: 280 });
              setTimeout(function () {
                var g = host.querySelector('div[role="button"]');
                if (g) g.click();
              }, 100);
            }
          });
        } catch (err) { if (api.onError) api.onError(err); }
      };
    });
    return true;
  }

  async function fetchBalance() {
    var s = getSession();
    if (!s) return null;
    try {
      var r = await fetch(API_BASE + "/credits/balance", { headers: { Authorization: "Bearer " + s.session_id } });
      if (r.status === 401) { clearSession(); return null; }
      if (!r.ok) return null;
      return (await r.json()).balance;
    } catch (e) { return null; }
  }

  var api = {
    API_BASE: API_BASE, PINNED_CHAIN: PINNED_CHAIN,
    getSession: getSession, clearSession: clearSession, authHeader: authHeader,
    shortAddress: shortAddress, displayName: displayName,
    getChainConfig: getChainConfig, connectWallet: connectWallet,
    signInWithWallet: signInWithWallet, signOut: signOut,
    initGoogle: initGoogle, fetchBalance: fetchBalance,
    onChange: function (fn) { listeners.push(fn); },
    onError: null
  };
  window.NitoAuth = api;
})();
