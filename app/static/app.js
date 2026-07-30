/* streamchen — client behaviour.
 *
 * Deliberately small. The server renders every view; this file only does the
 * things a server cannot: play audio, listen on the WebSocket, and swap in a
 * freshly rendered fragment when the room changes.
 */
(function () {
  'use strict';

  // ---- Helpers -------------------------------------------------------------
  function cookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function toast(message, level) {
    var stack = document.getElementById('toastStack');
    if (!stack) return;

    var node = document.createElement('div');
    node.className = 'alert alert-' + (level || 'info') + ' alert-dismissible mb-0';
    node.setAttribute('role', 'alert');
    node.textContent = message;

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.addEventListener('click', function () { node.remove(); });
    node.appendChild(close);

    stack.appendChild(node);
    window.setTimeout(function () { node.remove(); }, 4000);
  }

  /* Every unsafe request needs the CSRF token echoed from its cookie, and the
   * host secret when we are holding one. One place to attach both. */
  function api(path, options) {
    options = options || {};
    var headers = {};

    if (options.body !== undefined) headers['Content-Type'] = 'application/json';

    var method = options.method || 'GET';
    if (method !== 'GET' && method !== 'HEAD') {
      var csrf = cookie('sc_csrf');
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }

    if (options.token) {
      var secret = hostSecret(options.token);
      if (secret) headers['X-Host-Secret'] = secret;
    }

    return fetch('/api' + path, {
      method: method,
      headers: headers,
      credentials: 'same-origin',
      cache: 'no-store',
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      if (response.status === 204) return null;
      return response.text().then(function (text) {
        var payload = null;
        try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = null; }
        if (!response.ok) {
          var detail = payload && payload.detail;
          if (Array.isArray(detail)) detail = detail[0] && detail[0].msg;
          var error = new Error(typeof detail === 'string' ? detail : response.statusText);
          error.status = response.status;
          throw error;
        }
        return payload;
      });
    });
  }

  function formatDuration(seconds) {
    var total = Math.max(0, Math.floor(seconds || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    var pad = function (n) { return String(n).padStart(2, '0'); };
    return hours > 0 ? hours + ':' + pad(minutes) + ':' + pad(secs) : minutes + ':' + pad(secs);
  }

  // ---- Local storage -------------------------------------------------------
  // The host key is the one credential the server cannot reissue: it keeps
  // only an Argon2 hash. Host rights normally ride on the session cookie; this
  // is what lets a host reclaim them elsewhere.
  var HOST_PREFIX = 'streamchen:host:';
  var RECENT_KEY = 'streamchen:recent';
  var RECENT_LIMIT = 6;

  function hostSecret(token) {
    try { return window.localStorage.getItem(HOST_PREFIX + token); } catch (e) { return null; }
  }

  function storeHostSecret(token, secret) {
    try { window.localStorage.setItem(HOST_PREFIX + token, secret); } catch (e) { /* private mode */ }
  }

  function forgetRoom(token) {
    try {
      window.localStorage.removeItem(HOST_PREFIX + token);
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(recentRooms().filter(function (entry) {
        return entry.token !== token;
      })));
    } catch (e) { /* private mode */ }
  }

  function recentRooms() {
    try {
      var raw = window.localStorage.getItem(RECENT_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter(function (e) { return e && e.token && e.name; }) : [];
    } catch (e) { return []; }
  }

  function rememberRoom(token, name) {
    try {
      var others = recentRooms().filter(function (entry) { return entry.token !== token; });
      var updated = [{ token: token, name: name, host: Boolean(hostSecret(token)) }]
        .concat(others).slice(0, RECENT_LIMIT);
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(updated));
    } catch (e) { /* private mode */ }
  }

  // ---- Obfuscated email links ---------------------------------------------
  // The server never emits a literal address or a mailto:, which is what makes
  // the naive harvesters come away empty. Reassembled here.
  function wireMailLinks() {
    document.querySelectorAll('a.js-mail').forEach(function (anchor) {
      var user = anchor.getAttribute('data-user');
      var domain = anchor.getAttribute('data-domain');
      if (!user || !domain) return;

      var address = user + '@' + domain;
      anchor.setAttribute('href', 'mailto:' + address);
      var label = anchor.querySelector('.mail-text');
      if (label) label.textContent = address;
    });
  }

  // ---- Home page -----------------------------------------------------------
  function wireHome() {
    var form = document.getElementById('createRoomForm');
    if (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        var button = form.querySelector('button[type="submit"]');
        var input = form.querySelector('input[name="name"]');
        button.disabled = true;

        api('/rooms', { method: 'POST', body: { name: (input.value || '').trim() } })
          .then(function (room) {
            storeHostSecret(room.token, room.host_secret);
            rememberRoom(room.token, room.name);
            // The key travels in the URL exactly once, to be shown and saved.
            window.location.href = '/r/' + room.token + '?secret=' + encodeURIComponent(room.host_secret);
          })
          .catch(function (error) {
            toast(error.message || 'Could not create the room', 'danger');
            button.disabled = false;
          });
      });
    }

    var card = document.getElementById('recentRooms');
    var body = document.getElementById('recentRoomsBody');
    if (!card || !body) return;

    var rooms = recentRooms();
    if (rooms.length === 0) return;

    rooms.forEach(function (entry) {
      var row = document.createElement('div');
      row.className = 'list-row';

      var left = document.createElement('div');
      var link = document.createElement('a');
      link.href = '/r/' + entry.token;
      link.className = 'fw-semibold';
      link.textContent = entry.name;
      left.appendChild(link);

      if (entry.host) {
        var badge = document.createElement('span');
        badge.className = 'badge-soft badge-mine ms-2';
        badge.textContent = 'host';
        left.appendChild(badge);
      }

      var drop = document.createElement('button');
      drop.type = 'button';
      drop.className = 'btn btn-outline-secondary btn-sm';
      drop.setAttribute('aria-label', 'Forget ' + entry.name);
      drop.innerHTML = '<i class="bi bi-x-lg"></i>';
      drop.addEventListener('click', function () {
        forgetRoom(entry.token);
        row.remove();
        if (!body.children.length) card.classList.add('d-none');
      });

      row.appendChild(left);
      row.appendChild(drop);
      body.appendChild(row);
    });

    card.classList.remove('d-none');
  }

  // ---- Room page -----------------------------------------------------------
  function wireRoom() {
    var root = document.getElementById('room');
    if (!root) return;

    var token = root.getAttribute('data-token');
    var streamUrl = root.getAttribute('data-stream');
    rememberRoom(token, document.querySelector('h1').textContent.trim());

    wirePlayer(root, streamUrl);
    wireQueueActions(token);
    wireAddTrack(token);
    wireCopyButtons();
    wireHostControls(token, root);
    wireClaimHost(token, root);
    connect(token);
  }

  // -- Audio ---------------------------------------------------------------
  var ticker = null;

  function wirePlayer(root, streamUrl) {
    var audio = document.getElementById('audio');
    var button = document.getElementById('playButton');
    var icon = document.getElementById('playIcon');
    var dot = document.getElementById('liveDot');
    var label = document.getElementById('liveLabel');
    var hint = document.getElementById('playbackHint');
    var volume = document.getElementById('volume');
    var playing = false;

    function setPlaying(value) {
      playing = value;
      icon.className = value ? 'bi bi-pause-fill' : 'bi bi-play-fill';
      dot.classList.toggle('off', !value);
      label.textContent = value ? 'Live' : 'Paused';
      button.setAttribute('aria-label', value ? 'Stop listening' : 'Start listening');
    }

    button.addEventListener('click', function () {
      if (playing) {
        audio.pause();
        // Drop the buffer: pressing play again has to rejoin *live*, not
        // continue from where the listener stopped.
        audio.removeAttribute('src');
        audio.load();
        setPlaying(false);
        return;
      }

      audio.src = streamUrl;
      audio.play().then(function () {
        setPlaying(true);
        hint.classList.add('d-none');
      }).catch(function () {
        // Autoplay policy, or nothing connected to the mount yet.
        setPlaying(false);
        hint.classList.remove('d-none');
      });
    });

    audio.addEventListener('ended', function () { setPlaying(false); });
    volume.addEventListener('input', function () { audio.volume = Number(volume.value); });
    audio.volume = Number(volume.value);

    startTicker();
  }

  /* The progress bar advances locally between server updates; the server is
   * still the authority, and every fragment swap resets it. */
  function startTicker() {
    if (ticker !== null) window.clearInterval(ticker);
    ticker = window.setInterval(function () {
      var body = document.querySelector('#nowPlaying .player-body[data-duration]');
      if (!body) return;

      var duration = Number(body.getAttribute('data-duration')) || 0;
      var position = Number(body.getAttribute('data-position')) || 0;
      if (duration <= 0) return;

      position = Math.min(duration, position + 1);
      body.setAttribute('data-position', String(position));

      var bar = body.querySelector('.player-progress span');
      if (bar) bar.style.width = Math.min(100, (position / duration) * 100) + '%';

      var label = body.querySelector('#positionLabel');
      if (label) label.textContent = formatDuration(position);
    }, 1000);
  }

  // -- Queue ---------------------------------------------------------------
  function wireQueueActions(token) {
    var container = document.getElementById('queue');
    if (!container) return;

    // One delegated listener: the queue is replaced wholesale on every update,
    // so per-button handlers would have to be re-attached every time.
    container.addEventListener('click', function (event) {
      var button = event.target.closest('button[data-action]');
      if (!button) return;

      var action = button.getAttribute('data-action');
      var trackId = button.getAttribute('data-track');
      button.disabled = true;

      var request;
      if (action === 'vote') {
        // Clicking your own vote again withdraws it.
        var value = Number(button.getAttribute('data-value'));
        var active = button.classList.contains('active');
        request = api('/rooms/' + token + '/tracks/' + trackId + '/vote', {
          method: 'POST', body: { value: active ? 0 : value }, token: token
        });
      } else if (action === 'remove') {
        request = api('/rooms/' + token + '/tracks/' + trackId, { method: 'DELETE', token: token });
      } else if (action === 'promote') {
        request = api('/rooms/' + token + '/tracks/' + trackId + '/promote', {
          method: 'POST', token: token
        });
      } else {
        button.disabled = false;
        return;
      }

      request.then(function () { return refresh(token); })
        .catch(function (error) { toast(error.message || 'That did not work', 'danger'); })
        .finally(function () { button.disabled = false; });
    });
  }

  /* A bare id, or anything that names youtube. Everything else is a search
   * term — the server makes the same call, this only decides which endpoint
   * the box hits first. */
  function looksLikeLink(value) {
    return /^[A-Za-z0-9_-]{11}$/.test(value) ||
      /youtu\.?be/i.test(value) ||
      value.indexOf('://') !== -1;
  }

  function wireAddTrack(token) {
    var form = document.getElementById('addTrackForm');
    if (!form) return;

    var input = form.querySelector('input[name="url"]');
    var button = form.querySelector('button[type="submit"]');
    var results = document.getElementById('searchResults');
    var body = document.getElementById('searchResultsBody');

    function closeResults() {
      if (results) results.classList.add('d-none');
      if (body) body.textContent = '';
    }

    var close = document.getElementById('searchClose');
    if (close) close.addEventListener('click', closeResults);

    function add(value, label) {
      button.disabled = true;
      return api('/rooms/' + token + '/tracks', {
        method: 'POST', body: { url: value }, token: token
      })
        .then(function (track) {
          input.value = '';
          closeResults();
          toast('Queued “' + track.title + '”', 'success');
          return refresh(token);
        })
        .catch(function (error) {
          toast(error.message || 'Could not add ' + (label || 'that song'), 'danger');
        })
        .finally(function () { button.disabled = false; });
    }

    function render(matches, query) {
      body.textContent = '';
      results.classList.remove('d-none');

      if (!matches.length) {
        var empty = document.createElement('p');
        empty.className = 'text-muted small mb-0';
        empty.textContent = 'Nothing found for “' + query + '”.';
        body.appendChild(empty);
        return;
      }

      matches.forEach(function (match) {
        var row = document.createElement('div');
        row.className = 'queue-row';

        var art = document.createElement(match.thumbnail_url ? 'img' : 'div');
        art.className = 'queue-art';
        if (match.thumbnail_url) {
          art.src = match.thumbnail_url;
          art.alt = '';
          art.loading = 'lazy';
        }
        row.appendChild(art);

        var text = document.createElement('div');
        text.className = 'queue-body';

        var title = document.createElement('div');
        title.className = 'queue-title';
        title.title = match.title;
        title.textContent = match.title;
        text.appendChild(title);

        var meta = document.createElement('div');
        meta.className = 'queue-meta';
        meta.textContent = formatDuration(match.duration_s) +
          (match.channel ? ' · ' + match.channel : '');
        text.appendChild(meta);
        row.appendChild(text);

        var actions = document.createElement('div');
        actions.className = 'queue-actions';

        // Say why up front rather than letting the add fail.
        var blocked = match.too_long ? 'Too long' : (match.queued ? 'Queued' : null);
        if (blocked) {
          var note = document.createElement('span');
          note.className = 'badge-soft';
          note.textContent = blocked;
          actions.appendChild(note);
        } else {
          var pick = document.createElement('button');
          pick.type = 'button';
          pick.className = 'btn btn-primary btn-sm';
          pick.setAttribute('aria-label', 'Add ' + match.title);
          pick.innerHTML = '<i class="bi bi-plus-lg"></i>';
          pick.addEventListener('click', function () {
            pick.disabled = true;
            add(match.youtube_id, match.title).then(function () { pick.disabled = false; });
          });
          actions.appendChild(pick);
        }

        row.appendChild(actions);
        body.appendChild(row);
      });
    }

    function search(query) {
      button.disabled = true;
      results.classList.remove('d-none');
      body.textContent = '';

      var pending = document.createElement('p');
      pending.className = 'text-muted small mb-0';
      pending.textContent = 'Searching…';
      body.appendChild(pending);

      api('/rooms/' + token + '/search?q=' + encodeURIComponent(query), { token: token })
        .then(function (matches) { render(matches || [], query); })
        .catch(function (error) {
          closeResults();
          toast(error.message || 'Could not search right now', 'danger');
        })
        .finally(function () { button.disabled = false; });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var value = (input.value || '').trim();
      if (!value) return;
      if (looksLikeLink(value)) add(value); else search(value);
    });
  }

  function wireCopyButtons() {
    function copyFrom(button, getText, label) {
      if (!button) return;
      button.addEventListener('click', function () {
        navigator.clipboard.writeText(getText()).then(function () {
          toast(label + ' copied', 'success');
        }).catch(function () {
          toast('Copy failed — select it and copy manually', 'warning');
        });
      });
    }

    copyFrom(document.getElementById('copyLink'), function () {
      return document.getElementById('shareLink').value;
    }, 'Link');

    copyFrom(document.getElementById('copySecret'), function () {
      return document.getElementById('hostSecret').textContent.trim();
    }, 'Host key');
  }

  // -- Host ----------------------------------------------------------------
  function wireHostControls(token, root) {
    var toggle = document.getElementById('hostToggle');
    var panel = document.getElementById('hostPanel');
    if (!toggle || !panel) return;

    toggle.addEventListener('click', function () {
      var open = panel.classList.toggle('d-none') === false;
      toggle.setAttribute('aria-expanded', String(open));
      document.getElementById('hostChevron').className =
        'bi bi-chevron-' + (open ? 'up' : 'down');
      if (open) loadListeners(token);
    });

    function patch(changes) {
      return api('/rooms/' + token, { method: 'PATCH', body: changes, token: token })
        .then(function () { window.location.reload(); })
        .catch(function (error) { toast(error.message || 'Could not apply that', 'danger'); });
    }

    panel.addEventListener('click', function (event) {
      var button = event.target.closest('button[data-host-action]');
      if (!button) return;
      var action = button.getAttribute('data-host-action');

      if (action === 'skip') {
        api('/rooms/' + token + '/skip', { method: 'POST', token: token })
          .then(function () { toast('Skipped', 'success'); return refresh(token); })
          .catch(function (error) { toast(error.message || 'Could not skip', 'danger'); });
      } else if (action === 'lock') {
        patch({ queue_locked: root.getAttribute('data-locked') !== '1' });
      } else if (action === 'voting') {
        patch({ voting_enabled: root.getAttribute('data-voting') !== '1' });
      } else if (action === 'rename') {
        patch({ name: document.getElementById('roomName').value });
      } else if (action === 'playlist') {
        patch({ fallback_playlist: document.getElementById('fallbackPlaylist').value });
      } else if (action === 'delete') {
        if (!window.confirm('Delete this room? The stream stops and the queue is gone.')) return;
        api('/rooms/' + token, { method: 'DELETE', token: token })
          .then(function () { forgetRoom(token); window.location.href = '/'; })
          .catch(function (error) { toast(error.message || 'Could not delete', 'danger'); });
      }
    });

    panel.querySelectorAll('input[data-host-setting]').forEach(function (input) {
      input.addEventListener('change', function () {
        var changes = {};
        changes[input.getAttribute('data-host-setting')] = Number(input.value);
        patch(changes);
      });
    });
  }

  function loadListeners(token) {
    var list = document.getElementById('listenerList');
    if (!list) return;

    api('/rooms/' + token + '/listeners', { token: token }).then(function (rows) {
      list.textContent = '';
      if (!rows.length) {
        var empty = document.createElement('p');
        empty.className = 'text-muted small mb-0';
        empty.textContent = 'Nobody else here yet.';
        list.appendChild(empty);
        return;
      }

      rows.forEach(function (listener) {
        var row = document.createElement('div');
        row.className = 'list-row';

        var left = document.createElement('div');
        var name = document.createElement('span');
        if (!listener.online) name.className = 'text-muted';
        name.textContent = listener.display_name;
        left.appendChild(name);

        if (listener.is_host) left.appendChild(badge('host'));
        if (listener.shadow_banned) left.appendChild(badge('muted'));

        var meta = document.createElement('div');
        meta.className = 'queue-meta';
        meta.textContent = (listener.online ? 'online' : 'away') + ' · ' + listener.queued + ' queued';
        left.appendChild(meta);

        row.appendChild(left);

        if (!listener.is_host) {
          var kick = document.createElement('button');
          kick.type = 'button';
          kick.className = 'btn btn-outline-secondary btn-sm';
          kick.setAttribute('aria-label', 'Remove ' + listener.display_name);
          kick.innerHTML = '<i class="bi bi-person-slash"></i>';
          kick.addEventListener('click', function () {
            kick.disabled = true;
            api('/rooms/' + token + '/bans', {
              method: 'POST', body: { listener_id: listener.id }, token: token
            }).then(function () {
              toast(listener.display_name + ' was removed', 'success');
              loadListeners(token);
              return refresh(token);
            }).catch(function (error) {
              toast(error.message || 'Could not remove them', 'danger');
              kick.disabled = false;
            });
          });
          row.appendChild(kick);
        }

        list.appendChild(row);
      });
    }).catch(function () {
      list.textContent = '';
    });
  }

  function badge(text) {
    var node = document.createElement('span');
    node.className = 'badge-soft ms-2';
    node.textContent = text;
    return node;
  }

  /* A host arriving in a browser that still holds the key: prove it once and
   * the session carries host rights from then on. */
  function wireClaimHost(token, root) {
    var toggle = document.getElementById('claimToggle');
    var panel = document.getElementById('claimPanel');

    /* Only worth reloading when the server would now render something this
     * page does not already show. Reloading whenever the server says "host"
     * spins forever: the stored key still proves host on the next load, which
     * asks for another reload. */
    var renderedAsHost = root.getAttribute('data-host') === '1';
    var stored = hostSecret(token);
    if (stored && !renderedAsHost) {
      api('/rooms/' + token, { token: token }).then(function (state) {
        if (state.is_host) window.location.reload();
      }).catch(function () { /* stale key; the form below still works */ });
    }

    if (!toggle || !panel) return;

    toggle.addEventListener('click', function () {
      var open = panel.classList.toggle('d-none') === false;
      toggle.setAttribute('aria-expanded', String(open));
    });

    document.getElementById('claimButton').addEventListener('click', function () {
      var secret = (document.getElementById('claimSecret').value || '').trim();
      if (!secret) return;

      storeHostSecret(token, secret);
      api('/rooms/' + token, { token: token }).then(function (state) {
        if (state.is_host) {
          window.location.reload();
        } else {
          forgetRoom(token);
          toast('That key does not match this room', 'danger');
        }
      }).catch(function () { toast('Could not check that key', 'danger'); });
    });
  }

  // -- Realtime ------------------------------------------------------------
  var refreshTimer = null;

  /* Events say "something changed", never what. Refetching the rendered
   * fragment means a missed event, a dropped socket or a cleared Redis all
   * heal on the next update. */
  function refresh(token) {
    return fetch('/r/' + token + '/live', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) throw new Error('refresh failed');
        return response.text();
      })
      .then(function (html) { applyFragment(html); })
      .catch(function () { /* the next event will try again */ });
  }

  function scheduleRefresh(token) {
    // A vote changes the queue too, so several events usually arrive together.
    if (refreshTimer !== null) return;
    refreshTimer = window.setTimeout(function () {
      refreshTimer = null;
      refresh(token);
    }, 250);
  }

  function applyFragment(html) {
    var holder = document.createElement('div');
    holder.innerHTML = html;

    var regions = {
      nowplaying: document.getElementById('nowPlaying'),
      queue: document.getElementById('queue'),
      history: document.getElementById('history')
    };

    Object.keys(regions).forEach(function (name) {
      var source = holder.querySelector('[data-region="' + name + '"]');
      if (source && regions[name]) regions[name].innerHTML = source.innerHTML;
    });

    var meta = holder.querySelector('[data-region="meta"]');
    if (meta) {
      var listeners = meta.getAttribute('data-listeners');
      ['listenerCount', 'listenerCount2'].forEach(function (id) {
        var node = document.getElementById(id);
        if (node) node.textContent = listeners;
      });

      var count = document.getElementById('queueCount');
      if (count) count.textContent = meta.getAttribute('data-queue-count');

      var historyCard = document.getElementById('historyCard');
      if (historyCard) {
        historyCard.classList.toggle('d-none', meta.getAttribute('data-has-history') !== '1');
      }

      var disabled = meta.getAttribute('data-add-disabled') === '1';
      var form = document.getElementById('addTrackForm');
      if (form) {
        form.querySelector('input[name="url"]').disabled = disabled;
        form.querySelector('button[type="submit"]').disabled = disabled;
        var reason = document.getElementById('addDisabledReason');
        if (reason) {
          reason.classList.toggle('d-none', !disabled);
          reason.innerHTML = '<i class="bi bi-lock me-1"></i>' + meta.getAttribute('data-add-reason');
        }
      }
    }

    startTicker();
  }

  function setConnection(state) {
    var dot = document.getElementById('connection-status');
    if (!dot) return;

    var titles = {
      connected: 'Real-time updates connected',
      disconnected: 'Real-time updates disconnected — reconnecting…',
      error: 'Real-time connection error'
    };
    dot.className = 'connection-status ' + (state === 'unknown' ? '' : state);
    dot.title = titles[state] || 'Real-time status unknown';
  }

  function connect(token) {
    var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = protocol + '//' + window.location.host + '/api/rooms/' + token + '/ws';
    var attempt = 0;
    var socket = null;
    var ping = null;

    function open() {
      socket = new WebSocket(url);

      socket.onopen = function () {
        attempt = 0;
        setConnection('connected');
        ping = window.setInterval(function () { socket.send('ping'); }, 20000);
      };

      socket.onmessage = function (message) {
        var event;
        try { event = JSON.parse(message.data); } catch (e) { return; }
        if (event.type === 'PONG' || event.type === 'READY') return;

        if (event.type === 'PLAYBACK_POSITION') {
          var body = document.querySelector('#nowPlaying .player-body[data-duration]');
          if (body && typeof event.data.position_s === 'number') {
            body.setAttribute('data-position', String(event.data.position_s));
          }
          return;
        }

        scheduleRefresh(token);
      };

      socket.onerror = function () { setConnection('error'); };

      socket.onclose = function () {
        if (ping !== null) { window.clearInterval(ping); ping = null; }
        setConnection('disconnected');
        // Back off, but keep trying: reconnecting on its own is the difference
        // between a hiccup and a dead page.
        var delay = Math.min(15000, 1000 * Math.pow(2, attempt++));
        window.setTimeout(open, delay);
      };
    }

    open();
  }

  // ---- Boot ----------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    wireMailLinks();
    wireHome();
    wireRoom();
  });
})();
