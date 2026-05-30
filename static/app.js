const $ = (id) => document.getElementById(id);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', async () => {
    try {
      await navigator.serviceWorker.register('/static/sw.js');
      console.log('Service worker registered');
    } catch (err) {
      console.error('Service worker failed', err);
    }
  });
}

const store = {
  get key(){ return localStorage.getItem('tbp_premium_key') || ''; },
  set key(v){ localStorage.setItem('tbp_premium_key', v || ''); fillKeys(); }
};

let countdownTimer = null;
let deferredInstallPrompt = null;

function isiOS(){ return /iphone|ipad|ipod/i.test(navigator.userAgent); }

function updateInstallHelp(){
  const help = $('installHelp');
  if(!help) return;
  if(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches){
    help.textContent = 'App icon installed. Open Bank Push from your home screen.';
    return;
  }
  help.textContent = isiOS()
    ? 'iPhone/iPad: open in Safari, tap Share, then Add to Home Screen.'
    : 'Android: tap Install app icon. If no popup appears, use browser menu → Add to Home screen.';
}

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallHelp();
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  updateInstallHelp();
});

document.addEventListener('DOMContentLoaded', updateInstallHelp);

if($('installAppBtn')){
  $('installAppBtn').onclick = async () => {
    if(deferredInstallPrompt){
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice.catch(()=>null);
      deferredInstallPrompt = null;
      updateInstallHelp();
      return;
    }
    alert(isiOS()
      ? 'On iPhone/iPad: open this page in Safari, tap Share, then Add to Home Screen.'
      : 'If the install popup does not appear, use Chrome menu ⋮ then Add to Home screen or Install app.'
    );
  };
}

function fillKeys(){
  ['statusKey','payKey','deviceKey','testKey'].forEach(id=>{
    if($(id) && !$(id).value) $(id).value = store.key;
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

fillKeys();

function out(id, obj){
  if($(id)) $(id).textContent = typeof obj === 'string' ? obj : JSON.stringify(obj, null, 2);
}

async function api(path, body, adminPass){
  const headers = {'Content-Type':'application/json'};
  if(adminPass) headers['X-Admin-Password'] = adminPass;
  const res = await fetch(path, {
    method:'POST',
    headers,
    body: JSON.stringify(body || {})
  });
  const json = await res.json().catch(()=>({ok:false,error:'bad_json'}));
  if(!res.ok && !json.error) json.error = 'HTTP ' + res.status;
  return json;
}

function showCountdown(key){
  if(countdownTimer) clearInterval(countdownTimer);
  const box = $('countdown');
  if(!box || !key) return;

  function tick(){
    const end = new Date(key.paid_until).getTime();
    const ms = end - Date.now();

    if(ms <= 0 || key.locked || !key.is_active){
      box.classList.add('locked');
      box.textContent = 'LOCKED — send payment to Fries91, then scan/check again.';
      return;
    }

    box.classList.remove('locked');

    const s = Math.floor(ms/1000);
    const d = Math.floor(s/86400);
    const h = Math.floor((s%86400)/3600);
    const m = Math.floor((s%3600)/60);
    const sec = s%60;

    box.textContent = `${d}d ${h}h ${m}m ${sec}s left — devices enabled: ${key.enabled_devices || 0}`;
  }

  tick();
  countdownTimer = setInterval(tick, 1000);
}

$('reserveKeyBtn').onclick = async () => {
  const res = await api('/api/key/request', {
    owner_name: $('reserveName').value,
    owner_torn_id: $('reserveTornId').value
  });
  if(res.ok && res.key && res.key.premium_key) store.key = res.key.premium_key;
  out('reserveOut', res);
  if(res.ok) showCountdown(res.key);
};

$('findKeyBtn').onclick = async () => {
  const res = await api('/api/key/find', {
    owner_torn_id: $('reserveTornId').value
  });
  if(res.ok && res.key && res.key.premium_key) store.key = res.key.premium_key;
  out('reserveOut', res);
  if(res.ok) showCountdown(res.key);
};

$('statusBtn').onclick = async () => {
  const key = $('statusKey').value.trim();
  if(key) store.key = key;
  const res = await api('/api/key/status', {premium_key: key});
  out('statusOut', res);
  if(res.ok) showCountdown(res.key);
};

$('paymentBtn').onclick = async () => {
  const key = $('payKey').value.trim();
  if(key) store.key = key;
  const res = await api('/api/payments/claim', {
    premium_key:key,
    claimed_by:$('payName').value,
    claimed_by_torn_id:$('payTornId').value,
    xanax_amount:$('payAmount').value,
    proof_text:$('payProof').value
  });
  out('paymentOut', res);
};

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

$('enablePushBtn').onclick = async () => {
  if(!('serviceWorker' in navigator) || !('PushManager' in window)) {
    out('deviceOut', 'This browser does not support web push. On iPhone, add this page to Home Screen first.');
    return;
  }
  if(!window.TBP_CONFIG.vapidPublicKey){
    out('deviceOut', 'Server missing VAPID_PUBLIC_KEY. Set it in Render first.');
    return;
  }
  const reg = await navigator.serviceWorker.register('/static/sw.js');
  const permission = await Notification.requestPermission();
  if(permission !== 'granted'){
    out('deviceOut', 'Notifications were not allowed.');
    return;
  }
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly:true,
    applicationServerKey:urlBase64ToUint8Array(window.TBP_CONFIG.vapidPublicKey)
  });
  const key = $('deviceKey').value.trim();
  if(key) store.key = key;
  const res = await api('/api/devices/register', {
    premium_key: key,
    device_name: $('deviceName').value || 'Phone',
    subscription:sub
  });
  out('deviceOut', res);
  if(res.ok) showCountdown(res.key);
};

$('disableNotifyBtn').onclick = async () => {
  const key = $('deviceKey').value.trim();
  const res = await api('/api/devices/toggle', {
    premium_key: key,
    enabled: false
  });
  out('deviceOut', res);
  if(res.ok) showCountdown(res.key);
};

$('enableNotifyBtn').onclick = async () => {
  const key = $('deviceKey').value.trim();
  const res = await api('/api/devices/toggle', {
    premium_key: key,
    enabled: true
  });
  out('deviceOut', res);
  if(res.ok) showCountdown(res.key);
};

$('testBtn').onclick = async () => {
  const key = $('testKey').value.trim();
  if(key) store.key = key;
  const res = await api('/api/banking/request', {
    premium_key:key,
    requester_name:$('testRequester').value || 'Test User',
    amount:$('testAmount').value || '$1',
    note:'Test alert from premium portal'
  });
  out('testOut', res);
};

$('scanNowBtn').onclick = async () => {
  const res = await api('/api/admin/scan-payments', {}, $('adminPass').value);
  out('scanOut', res);
};

$('scanStatusBtn').onclick = async () => {
  const res = await api('/api/admin/scan-status', {}, $('adminPass').value);
  out('scanOut', res);
};

$('adminPendingBtn').onclick = async () => {
  const pass = $('adminPass').value;
  const res = await api('/api/admin/payment/pending', {}, pass);

  if(!res.ok){
    $('pendingPayments').innerHTML = '<pre>'+escapeHtml(JSON.stringify(res,null,2))+'</pre>';
    return;
  }

  $('pendingPayments').innerHTML = res.claims.map(c => `
    <div class="payment">
      <b>${escapeHtml(c.claimed_by)}</b><br>
      Claim: ${c.xanax_amount} Xanax | Torn ID: ${escapeHtml(c.claimed_by_torn_id || '')}<br>
      Key: ${escapeHtml(c.premium_key || 'will create/find by Torn ID')}<br>
      Proof: ${escapeHtml(c.proof_text || '')}<br>
      Created: ${escapeHtml(c.created_at)}<br>
      <div class="row">
        <button onclick="approveClaim('${c.id}')">Approve</button>
        <button onclick="rejectClaim('${c.id}')">Reject</button>
      </div>
    </div>
  `).join('') || '<p>No pending payments.</p>';
};

window.approveClaim = async (id) => {
  const res = await api('/api/admin/payment/approve', {
    claim_id:id,
    reviewed_by:'Fries91'
  }, $('adminPass').value);
  alert(JSON.stringify(res, null, 2));
  $('adminPendingBtn').click();
};

window.rejectClaim = async (id) => {
  const note = prompt('Reject note?', 'Could not verify item send.');
  const res = await api('/api/admin/payment/reject', {
    claim_id:id,
    reviewed_by:'Fries91',
    admin_note:note
  }, $('adminPass').value);
  alert(JSON.stringify(res, null, 2));
  $('adminPendingBtn').click();
};

$('adminKeysBtn').onclick = async () => {
  const res = await api('/api/admin/keys', {}, $('adminPass').value);
  const box = $('adminKeysList');

  if(!res.ok){
    box.innerHTML = '<pre>'+escapeHtml(JSON.stringify(res,null,2))+'</pre>';
    return;
  }

  box.innerHTML = res.keys.map(k => `
    <div class="payment">
      <b>${escapeHtml(k.owner_name || '')}</b> [${escapeHtml(k.owner_torn_id || '')}]<br>
      Key: <code>${escapeHtml(k.premium_key)}</code><br>
      Status: ${k.is_active && !k.locked ? 'active' : 'locked/expired'}<br>
      Paid until: ${escapeHtml(k.paid_until || '')}<br>
      Devices: ${k.enabled_devices || 0}<br>
      <div class="row">
        <button onclick="copyText('${k.premium_key}')">Copy</button>
        <button onclick="togglePremiumKey('${k.premium_key}', true)">Activate</button>
        <button onclick="togglePremiumKey('${k.premium_key}', false)">Lock</button>
      </div>
    </div>
  `).join('') || '<p>No keys yet.</p>';
};

window.togglePremiumKey = async (key, enabled) => {
  const res = await api('/api/admin/key/toggle', {
    premium_key:key,
    enabled
  }, $('adminPass').value);
  alert(JSON.stringify(res, null, 2));
  $('adminKeysBtn').click();
};

window.copyText = async (txt) => {
  try {
    await navigator.clipboard.writeText(txt);
    alert('Copied key');
  } catch(e) {
    prompt('Copy this key', txt);
  }
};

function escapeHtml(s){
  return String(s).replace(/[&<>'"]/g, c => ({
    '&':'&amp;',
    '<':'&lt;',
    '>':'&gt;',
    "'":'&#39;',
    '"':'&quot;'
  }[c]));
}
