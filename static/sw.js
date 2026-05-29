self.addEventListener('push', event => {
  let payload = {title:'Torn Banking Push', body:'New banking alert', data:{url:'/'}};
  try { payload = event.data.json(); } catch(e) {}
  event.waitUntil(self.registration.showNotification(payload.title || 'Torn Banking Push', {
    body: payload.body || '', icon: '/static/icon-192.png', badge: '/static/icon-192.png', tag: payload.data?.alert_id || 'tbp-alert', data: payload.data || {}, requireInteraction: true
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(list => {
    for(const client of list){ if('focus' in client) return client.focus(); }
    return clients.openWindow(url);
  }));
});
