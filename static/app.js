const $ = (id) => document.getElementById(id);

const store = {
  get token(){ return localStorage.getItem('tbp_token') || ''; },
  set token(v){ localStorage.setItem('tbp_token', v || ''); fillTokens(); }
};

let countdownTimer = null;

function fillTokens(){
  ['statusToken','leaderKeyToken','payToken','testToken'].forEach(id=>{
    if($(id) && !$(id).value) $(id).value = store.token;
  });
}

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tabPanel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const panel = document.getElementById(btn.dataset.tab);
    if(panel) panel.classList.add('active');
  });
});

fillTokens();

function out(id, obj){ $(id).textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2); }
async function api(path, body, adminPass){
  const headers = {'Content-Type':'application/json'};
  if(adminPass) headers['X-Admin-Password'] = adminPass;
  const res = await fetch(path, {method:'POST', headers, body: JSON.stringify(body || {})});
  const json = await res.json().catch(()=>({ok:false,error:'bad_json'}));
  if(!res.ok && !json.error) json.error = 'HTTP ' + res.status;
  return json;
}
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

$('registerFactionBtn').onclick = async () => {
  const res = await api('/api/factions/register', {
    faction_id: $('regFactionId').value,
    faction_name: $('regFactionName').value,
    leader_api_key: $('regLeaderApiKey').value
  });
  if(res.ok && res.faction && res.faction.api_token) store.token = res.faction.api_token;
  out('registerOut', res);
  if(res.ok) showCountdown(res.faction);
};

$('statusBtn').onclick = async () => {
  const token = $('statusToken').value.trim();
  if(token) store.token = token;
  const res = await api('/api/factions/status', {api_token: token});
  out('statusOut', res);
  if(res.ok) showCountdown(res.faction);
};

function updatePaymentOptions(faction){
  const sel = $('payAmount');
  const hint = $('payPriceHint');
  if(!sel) return;
  const allowed = faction.allowed_payment_amounts || [];
  sel.innerHTML = allowed.length ? allowed.map((amount, idx) => {
    const days = (idx + 1) * 30;
    return `<option value="${amount}">${amount} Xanax = ${days} days</option>`;
  }).join('') : '<option value="">Create banker keys first</option>';
  if(hint){
    hint.textContent = `${faction.active_banker_keys || 0} active banker key(s). Cost: ${faction.monthly_cost_xanax || 0} Xanax every 30 days. Allowed now: ${allowed.length ? allowed.join(', ') : 'none'} Xanax.`;
  }
}

function showCountdown(faction){
  updatePaymentOptions(faction);
  if(countdownTimer) clearInterval(countdownTimer);
  const box = $('countdown');
  function tick(){
    const end = new Date(faction.active_until).getTime();
    const ms = end - Date.now();
    if(ms <= 0 || faction.locked){
      box.classList.add('locked');
      box.textContent = 'LOCKED — payment verification needed';
      return;
    }
    box.classList.remove('locked');
    const s = Math.floor(ms/1000);
    const d = Math.floor(s/86400);
    const h = Math.floor((s%86400)/3600);
    const m = Math.floor((s%3600)/60);
    const sec = s%60;
    box.textContent = `${d}d ${h}h ${m}m ${sec}s until next payment lock — ${faction.active_banker_keys || 0} active banker key(s), ${faction.monthly_cost_xanax || 0} Xanax / 30 days`;
  }
  tick(); countdownTimer = setInterval(tick, 1000);
}


async function renderBankerKeys(res){
  const box = $('bankerKeysList');
  if(!box) return;
  if(!res.ok){ box.innerHTML = '<pre>'+escapeHtml(JSON.stringify(res,null,2))+'</pre>'; return; }
  const keys = res.banker_keys || [];
  box.innerHTML = keys.map(k => `
    <div class="payment">
      <b>${escapeHtml(k.key_label || k.banker_name)}</b><br>For: ${escapeHtml(k.banker_name)} ${k.banker_torn_id ? '('+escapeHtml(k.banker_torn_id)+')' : ''}<br>
      Status: ${k.is_active ? 'active' : 'revoked'}<br>
      Key: <code>${escapeHtml(k.banker_key)}</code><br>
      Created: ${escapeHtml(k.created_at || '')}<br>
      <div class="row">
        <button onclick="copyText('${k.banker_key}')">Copy</button>
        ${k.is_active ? `<button onclick="revokeBankerKey('${k.banker_key}')">Revoke</button>` : `<button onclick="enableBankerKey('${k.banker_key}')">Enable</button>`}
      </div>
    </div>`).join('') || '<p>No banker keys yet.</p>';
}

$('createBankerKeyBtn').onclick = async () => {
  const token = $('leaderKeyToken').value.trim(); if(token) store.token = token;
  const res = await api('/api/banker-keys/create', {
    api_token: token,
    key_label: $('bankerKeyLabel').value,
    banker_name: $('bankerKeyName').value,
    banker_torn_id: $('bankerKeyTornId').value,
    leader_api_key: $('leaderApiKey').value
  });
  out('bankerKeyOut', res);
  if(res.ok) { $('listBankerKeysBtn').click(); $('statusBtn').click(); }
};

$('listBankerKeysBtn').onclick = async () => {
  const token = $('leaderKeyToken').value.trim(); if(token) store.token = token;
  const res = await api('/api/banker-keys/list', {api_token: token, leader_api_key: $('leaderApiKey').value});
  renderBankerKeys(res);
};

window.revokeBankerKey = async (bankerKey) => {
  const res = await api('/api/banker-keys/revoke', {api_token:$('leaderKeyToken').value.trim(), leader_api_key:$('leaderApiKey').value, banker_key:bankerKey});
  out('bankerKeyOut', res); $('listBankerKeysBtn').click(); $('statusBtn').click();
};
window.enableBankerKey = async (bankerKey) => {
  const res = await api('/api/banker-keys/enable', {api_token:$('leaderKeyToken').value.trim(), leader_api_key:$('leaderApiKey').value, banker_key:bankerKey});
  out('bankerKeyOut', res); $('listBankerKeysBtn').click(); $('statusBtn').click();
};
window.copyText = async (txt) => { try { await navigator.clipboard.writeText(txt); alert('Copied key'); } catch(e) { prompt('Copy this key', txt); } };

$('enablePushBtn').onclick = async () => {
  if(!('serviceWorker' in navigator) || !('PushManager' in window)) {
    out('deviceOut', 'This browser does not support web push. On iPhone, add this page to Home Screen first.'); return;
  }
  if(!window.TBP_CONFIG.vapidPublicKey){ out('deviceOut', 'Server missing VAPID_PUBLIC_KEY. Set it in Render first.'); return; }
  const reg = await navigator.serviceWorker.register('/static/sw.js');
  const permission = await Notification.requestPermission();
  if(permission !== 'granted'){ out('deviceOut', 'Notifications were not allowed.'); return; }
  const sub = await reg.pushManager.subscribe({ userVisibleOnly:true, applicationServerKey:urlBase64ToUint8Array(window.TBP_CONFIG.vapidPublicKey) });
  const token = $('deviceToken').value.trim();
  const res = await api('/api/devices/register', { api_token: token, name:$('deviceName').value, torn_id:$('deviceTornId').value, role:$('deviceRole').value, subscription:sub });
  out('deviceOut', res);
};

$('paymentBtn').onclick = async () => {
  const token = $('payToken').value.trim(); if(token) store.token = token;
  const res = await api('/api/payments/claim', { api_token:token, claimed_by:$('payName').value, claimed_by_torn_id:$('payTornId').value, xanax_amount:$('payAmount').value, proof_text:$('payProof').value });
  out('paymentOut', res);
};

$('testBtn').onclick = async () => {
  const token = $('testToken').value.trim(); if(token) store.token = token;
  const res = await api('/api/banking/request', { api_token:token, requester_name:$('testRequester').value || 'Test User', amount:$('testAmount').value || '$1', note:'Test alert from portal' });
  out('testOut', res);
};

$('adminPendingBtn').onclick = async () => {
  const pass = $('adminPass').value;
  const res = await api('/api/admin/payment/pending', {}, pass);
  if(!res.ok){ $('pendingPayments').innerHTML = '<pre>'+JSON.stringify(res,null,2)+'</pre>'; return; }
  $('pendingPayments').innerHTML = res.claims.map(c => `
    <div class="payment">
      <b>${c.faction_name}</b><br>
      Claim: ${c.xanax_amount} Xanax from ${c.claimed_by} ${c.claimed_by_torn_id || ''}<br>
      Banker keys at claim: ${c.active_banker_keys_at_claim || '?'} | Monthly cost: ${c.monthly_cost_at_claim || '?'} | Months: ${c.months_paid || '?'}<br>
      Proof: ${escapeHtml(c.proof_text || '')}<br>
      Created: ${c.created_at}<br>
      <div class="row">
        <button onclick="approveClaim('${c.id}')">Approve</button>
        <button onclick="rejectClaim('${c.id}')">Reject</button>
      </div>
    </div>`).join('') || '<p>No pending payments.</p>';
};
window.approveClaim = async (id) => {
  const res = await api('/api/admin/payment/approve', {claim_id:id, reviewed_by:'Fries91'}, $('adminPass').value);
  alert(JSON.stringify(res, null, 2)); $('adminPendingBtn').click();
};
window.rejectClaim = async (id) => {
  const note = prompt('Reject note?', 'Could not verify item send.');
  const res = await api('/api/admin/payment/reject', {claim_id:id, reviewed_by:'Fries91', admin_note:note}, $('adminPass').value);
  alert(JSON.stringify(res, null, 2)); $('adminPendingBtn').click();
};
function escapeHtml(s){ return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
