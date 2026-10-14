// StashCast service worker: push notifications only. There is deliberately no fetch
// handler and no offline cache - the app streams media from the server.
//
// Payloads come from media/notifications.py. Approval notifications carry
// approve_url / reject_url (with a signed token) and two action buttons. Platforms
// without action buttons (iOS) just open `url`, the item page, which has the same
// Download / Reject buttons.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'StashCast', body: event.data ? event.data.text() : '' };
  }

  const options = {
    body: data.body || '',
    icon: data.icon || undefined,
    badge: data.icon || undefined,
    tag: data.tag || undefined,
    requireInteraction: Boolean(data.requireInteraction),
    data: {
      url: data.url || '/',
      approve_url: data.approve_url || null,
      reject_url: data.reject_url || null,
      // Texts of the confirmation shown after a button press, translated server-side
      labels: data.labels || {},
    },
  };
  if (data.image) {
    options.image = data.image;
  }
  if (Array.isArray(data.actions) && data.actions.length) {
    options.actions = data.actions;
  }

  event.waitUntil(self.registration.showNotification(data.title || 'StashCast', options));
});

async function decide(notification, action) {
  const target = action === 'approve' ? notification.data.approve_url : notification.data.reject_url;
  const labels = notification.data.labels || {};
  let message;
  try {
    const response = await fetch(target, { method: 'POST', credentials: 'omit' });
    const result = await response.json();
    if (!response.ok) {
      message = result.error || labels.failed || 'Request failed';
    } else if (!result.changed) {
      message = labels.already_handled || 'Already handled';
    } else {
      message = action === 'approve'
        ? labels.downloading || 'Downloading'
        : labels.rejected || 'Rejected';
    }
  } catch (e) {
    message = labels.unreachable || 'Could not reach StashCast';
  }
  // Short confirmation in place of the answered notification (same tag replaces it)
  await self.registration.showNotification(message, {
    body: notification.title,
    icon: notification.icon || undefined,
    tag: notification.tag || undefined,
    data: { url: notification.data.url },
  });
}

async function openPage(url) {
  const target = new URL(url, self.location.origin).href;
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const client of windows) {
    if (client.url === target && 'focus' in client) {
      return client.focus();
    }
  }
  return self.clients.openWindow(target);
}

self.addEventListener('notificationclick', (event) => {
  const notification = event.notification;
  notification.close();

  if ((event.action === 'approve' || event.action === 'reject') && notification.data) {
    event.waitUntil(decide(notification, event.action));
    return;
  }
  event.waitUntil(openPage((notification.data && notification.data.url) || '/'));
});
