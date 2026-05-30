// ==UserScript==
// @name         Torn Banking Push Premium Button
// @namespace    Fries91.Torn.BankingPushPremium
// @version      1.0.0
// @description  Adds a premium phone ping button for Torn Banking Push.
// @author       Fries91
// @match        https://www.torn.com/*
// @grant        GM_addStyle
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const PUSH_APP_URL = 'https://torn-banking-push.onrender.com';

  const STORAGE_KEY = 'fries91_premium_push_key';

  GM_addStyle(`
    #fries-premium-ping-btn {
      position: fixed !important;
      right: 12px !important;
      bottom: 82px !important;
      z-index: 2147483647 !important;
      width: 46px !important;
      height: 46px !important;
      border-radius: 50% !important;
      border: 2px solid #f2b233 !important;
      background: linear-gradient(180deg, #1b1b1b, #050505) !important;
      color: #f2b233 !important;
      font-size: 22px !important;
      font-weight: 900 !important;
      box-shadow: 0 0 18px rgba(242,178,51,.55) !important;
      display: flex !important;
      align-items: center !important;
      justify-content: center !important;
      cursor: pointer !important;
      font-family: Arial, sans-serif !important;
    }

    #fries-premium-ping-btn.fries-pulse {
      animation: friesPingPulse 1s infinite !important;
    }

    @keyframes friesPingPulse {
      0% { transform: scale(1); box-shadow: 0 0 10px rgba(242,178,51,.4); }
      50% { transform: scale(1.08); box-shadow: 0 0 24px rgba(242,178,51,.9); }
      100% { transform: scale(1); box-shadow: 0 0 10px rgba(242,178,51,.4); }
    }

    #fries-premium-ping-modal {
      position: fixed !important;
      inset: 0 !important;
      z-index: 2147483647 !important;
      background: rgba(0,0,0,.74) !important;
      display: flex !important;
      align-items: center !important;
      justify-content: center !important;
      padding: 16px !important;
      font-family: Arial, sans-serif !important;
    }

    .fries-premium-card {
      width: min(94vw, 390px) !important;
      background: #10151f !important;
      color: #f8e6a0 !important;
      border: 2px solid #f2b233 !important;
      border-radius: 18px !important;
      padding: 16px !important;
      box-shadow: 0 0 40px rgba(0,0,0,.9) !important;
    }

    .fries-premium-card h2 {
      margin: 0 0 10px !important;
      color: #ffd86b !important;
      font-size: 20px !important;
    }

    .fries-premium-card p {
      color: #ddd !important;
      font-size: 13px !important;
      line-height: 1.35 !important;
    }

    .fries-premium-card input {
      width: 100% !important;
      box-sizing: border-box !important;
      margin: 8px 0 !important;
      padding: 12px !important;
      border-radius: 12px !important;
      border: 1px solid #31394a !important;
      background: #050914 !important;
      color: #fff !important;
      font-size: 14px !important;
    }

    .fries-premium-card button {
      width: 100% !important;
      margin-top: 8px !important;
      padding: 12px !important;
      border: 0 !important;
      border-radius: 12px !important;
      background: #f2b233 !important;
      color: #111 !important;
      font-weight: 900 !important;
      font-size: 14px !important;
    }

    .fries-premium-row {
      display: grid !important;
      grid-template-columns: 1fr 1fr !important;
      gap: 8px !important;
    }

    .fries-premium-close {
      background: #273043 !important;
      color: #fff !important;
    }
  `);

  function getTornIdFromPage() {
    const href = location.href;
    const xid = href.match(/[?&]XID=(\d+)/i);
    if (xid) return xid[1];

    const links = Array.from(document.querySelectorAll('a[href*="profiles.php?XID="]'));
    for (const a of links) {
      const m = a.href.match(/XID=(\d+)/i);
      if (m) return m[1];
    }

    return '';
  }

  function openPushApp() {
    const tornId = getTornIdFromPage();
    const name = document.querySelector('.user.name, .menu-value, [class*="username"]')?.textContent?.trim() || '';

    const url = new URL(PUSH_APP_URL);
    if (tornId) url.searchParams.set('torn_id', tornId);
    if (name) url.searchParams.set('name', name);

    window.open(url.toString(), '_blank');
  }

  function showModal() {
    const old = document.getElementById('fries-premium-ping-modal');
    if (old) old.remove();

    const savedKey = localStorage.getItem(STORAGE_KEY) || '';

    const modal = document.createElement('div');
    modal.id = 'fries-premium-ping-modal';

    modal.innerHTML = `
      <div class="fries-premium-card">
        <h2>📲 Premium Ping to Phone</h2>
        <p>
          Open the premium app to reserve your key, pay Fries91, enable phone alerts,
          or paste your existing premium key here for this banking script.
        </p>

        <input id="fries-premium-key-input" placeholder="premium_..." value="${savedKey.replace(/"/g, '&quot;')}" />

        <button id="fries-save-premium-key">Save premium key</button>
        <button id="fries-open-premium-app">Open premium app</button>

        <div class="fries-premium-row">
          <button id="fries-test-premium-key">Test ping</button>
          <button id="fries-close-premium" class="fries-premium-close">Close</button>
        </div>
      </div>
    `;

    document.body.appendChild(modal);

    document.getElementById('fries-save-premium-key').onclick = () => {
      const key = document.getElementById('fries-premium-key-input').value.trim();
      localStorage.setItem(STORAGE_KEY, key);
      alert('Premium key saved.');
    };

    document.getElementById('fries-open-premium-app').onclick = () => {
      openPushApp();
    };

    document.getElementById('fries-test-premium-key').onclick = async () => {
      const key = document.getElementById('fries-premium-key-input').value.trim();

      if (!key.startsWith('premium_')) {
        alert('Paste your premium_ key first.');
        return;
      }

      localStorage.setItem(STORAGE_KEY, key);

      try {
        const res = await fetch(`${PUSH_APP_URL}/api/banking/request`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            premium_key: key,
            requester_name: 'Test',
            amount: '$1',
            note: 'Test ping from Torn banking script',
            url: location.href
          })
        });

        const json = await res.json();
        alert(JSON.stringify(json, null, 2));
      } catch (err) {
        alert('Test failed: ' + err.message);
      }
    };

    document.getElementById('fries-close-premium').onclick = () => {
      modal.remove();
    };
  }

  function makeButton() {
    if (document.getElementById('fries-premium-ping-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'fries-premium-ping-btn';
    btn.title = 'Premium Ping to Phone';
    btn.textContent = '📲';

    btn.onclick = showModal;

    document.body.appendChild(btn);
  }

  makeButton();

  setInterval(makeButton, 2500);

  window.FriesPremiumPush = {
    getKey() {
      return localStorage.getItem(STORAGE_KEY) || '';
    },

    async sendBankPing(requesterName, amount, note) {
      const key = localStorage.getItem(STORAGE_KEY) || '';

      if (!key.startsWith('premium_')) {
        return { ok: false, error: 'No premium key saved.' };
      }

      const res = await fetch(`${PUSH_APP_URL}/api/banking/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          premium_key: key,
          requester_name: requesterName || 'Bank request',
          amount: amount || '',
          note: note || 'New banking request',
          url: location.href
        })
      });

      return await res.json();
    }
  };
})();
