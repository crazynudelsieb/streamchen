/* streamchen service worker.
 *
 * A radio is not an offline app, so this is not a general-purpose cache: it
 * makes the shell install and start instantly, and it says something civil
 * when the network is gone. What it must never do is serve a stale view of a
 * live room, so the rule is narrow and negative — cache the things whose URL
 * decides their content, and stay out of the way of everything else.
 *
 * Never cached, ever:
 *   /api/...      — actions and state, all no-store
 *   /r/<token>... — the room page and its live fragment
 *   the stream    — cross-origin, and the whole point is that it is live
 */
var VERSION = 'streamchen-v1';
var SHELL = VERSION + '-shell';

/* Content-addressed: a versioned asset (?v=<release>) or an avatar drawn from
 * its own seed. The bytes behind one of these URLs never change. */
var IMMUTABLE = [/^\/static\//, /^\/a\//];

var OFFLINE_PAGE = '/static/offline.html';

var PRECACHE = [
  OFFLINE_PAGE,
  '/static/icon.svg',
  '/static/icon-192.png',
  '/static/site.webmanifest'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(SHELL).then(function (cache) {
      // Individually: one asset missing after a rename must not leave the
      // worker uninstalled and the page without an offline answer.
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function () { /* skipped */ });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name.indexOf(VERSION) !== 0) return caches.delete(name);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isImmutable(path) {
  return IMMUTABLE.some(function (pattern) { return pattern.test(path); });
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url;
  try { url = new URL(request.url); } catch (e) { return; }

  // Somebody else's origin: the stream, and nothing else. Never ours to touch.
  if (url.origin !== self.location.origin) return;

  if (isImmutable(url.pathname)) {
    event.respondWith(
      caches.match(request).then(function (hit) {
        if (hit) return hit;
        return fetch(request).then(function (response) {
          if (response && response.status === 200 && response.type === 'basic') {
            var copy = response.clone();
            caches.open(SHELL).then(function (cache) { cache.put(request, copy); });
          }
          return response;
        });
      })
    );
    return;
  }

  // Pages: always the network, because a room is only ever live. Offline, say
  // so on a page that was cached for exactly this moment.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(OFFLINE_PAGE).then(function (hit) {
          return hit || new Response('You are offline.', {
            status: 503,
            headers: { 'Content-Type': 'text/plain' }
          });
        });
      })
    );
    return;
  }

  // Everything else — /api, the live fragment — is left alone entirely.
});
