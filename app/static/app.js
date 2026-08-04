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
  var VOLUME_KEY = 'streamchen:volume';

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
    rememberRoom(token, roomName());

    wirePlayer(root, streamUrl);
    wireQueueActions(token);
    wireAddTrack(token);
    wireCopyButtons();
    wireShare();
    wireName(token);
    wireRoomRename(token);
    wireStreamToggle(token);
    wireChat(token);
    wireListeners(token);
    wireHostControls(token, root);
    wireClaimHost(token, root);
    connect(token);
  }

  function roomName() {
    var title = document.getElementById('roomTitle');
    return title ? title.textContent.trim() : '';
  }

  // -- Your name -----------------------------------------------------------
  /* Names are generated on join so that taking part costs nothing; this is the
   * override for people who would rather be recognisable. Sending a blank name
   * asks for another generated one, which is what the shuffle button does.
   *
   * The avatar sits in the same menu: it is the other half of "who am I here",
   * and the one part of it that cannot be typed. */
  function wireName(token) {
    var edit = document.getElementById('nameEdit');
    var form = document.getElementById('nameForm');
    var input = document.getElementById('nameInput');
    var label = document.getElementById('myName');
    var save = document.getElementById('nameSave');
    var shuffle = document.getElementById('nameShuffle');
    var newCat = document.getElementById('avatarShuffle');
    var preview = document.getElementById('avatarPreview');
    var mine = document.getElementById('myAvatar');
    if (!edit || !form || !input || !label || !save || !shuffle) return;

    function openForm(value) {
      form.classList.remove('d-none');
      edit.setAttribute('aria-expanded', 'true');
      input.value = value;
      input.focus();
      input.select();
    }

    function closeForm() {
      form.classList.add('d-none');
      edit.setAttribute('aria-expanded', 'false');
    }

    edit.addEventListener('click', function () {
      if (form.classList.contains('d-none')) openForm(label.textContent.trim());
      else closeForm();
    });

    /* The cat is content-addressed, so a new one is simply a new src -- both
     * where it is being chosen and where the page says who you are. */
    function showAvatar(seed) {
      if (!seed) return;
      var src = '/a/' + encodeURIComponent(seed) + '.svg';
      if (preview) preview.src = src;
      if (mine) mine.src = src;
    }

    function busy(state) {
      save.disabled = state;
      shuffle.disabled = state;
      if (newCat) newCat.disabled = state;
    }

    /* Both requests answer with the same listener. Only what was asked for is
     * written back -- a new cat must not put a half-typed name back to what the
     * server still has -- and then the room is refetched either way, because
     * your name and your cat are on every track you queued and on the history. */
    function change(request, failure, apply, keepOpen) {
      busy(true);
      return request.then(function (me) {
        apply(me);
        if (!keepOpen) closeForm();
        return refresh(token);
      }).catch(function (error) {
        toast(error.message || failure, 'danger');
      }).finally(function () {
        busy(false);
      });
    }

    function submit(value, keepOpen) {
      return change(api('/rooms/' + token + '/me', {
        method: 'PATCH', body: { display_name: value }, token: token
      }), 'Could not change your name', function (me) {
        label.textContent = me.display_name;
        input.value = me.display_name;
      }, keepOpen);
    }

    save.addEventListener('click', function () { submit(input.value, false); });
    shuffle.addEventListener('click', function () { submit('', true); });

    if (newCat) {
      // Kept open: somebody who did not like that cat wants another press, not
      // the menu shutting on them.
      newCat.addEventListener('click', function () {
        change(api('/rooms/' + token + '/me/avatar', {
          method: 'POST', token: token
        }), 'Could not draw you another cat', function (me) {
          showAvatar(me.avatar);
        }, true);
      });
    }

    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); submit(input.value, false); }
      if (event.key === 'Escape') closeForm();
    });
  }

  // -- Audio ---------------------------------------------------------------
  var ticker = null;

  /* The player, once wired: how the rest of the page tells it the stream has
   * been stopped or started without knowing anything else about it. */
  var player = null;

  /* Pressing play can legitimately be early: the worker may still be
   * connecting its source, in which case the mount does not exist yet and the
   * request 404s. That is a wait, not a failure, so keep asking for a few
   * seconds before telling the listener anything is wrong.
   *
   * What is being waited for is one event — a mount appearing — and how long
   * the listener then hears nothing is however much of the retry delay was
   * left. So the delay stays short across the seconds a source normally takes
   * to connect, and only lengthens past that, where the wait is no longer
   * ordinary and hammering it would be. The window is about as patient as
   * before; it is the time between asking that is different. */
  var CONNECT_QUICK_ATTEMPTS = 10;
  var CONNECT_QUICK_MS = 300;
  var CONNECT_ATTEMPTS = 20;
  var CONNECT_SLOW_MS = 1000;

  function wirePlayer(root, streamUrl) {
    var audio = document.getElementById('audio');
    var button = document.getElementById('playButton');
    var icon = document.getElementById('playIcon');
    var dot = document.getElementById('liveDot');
    var label = document.getElementById('liveLabel');
    var hint = document.getElementById('playbackHint');
    var volume = document.getElementById('volume');

    // paused | connecting | live. Connecting is its own state because it is the
    // one the listener used to see as "nothing happened".
    var state = 'paused';
    var attempt = 0;
    var retry = null;
    // The host stopped the stream. Different from paused: there is nothing to
    // connect to, so trying is not a matter of pressing harder.
    var stopped = root.getAttribute('data-stopped') === '1';

    function render() {
      var icons = { paused: 'play-fill', connecting: 'arrow-repeat', live: 'pause-fill' };
      var labels = { paused: 'Paused', connecting: 'Connecting…', live: 'Live' };
      icon.className = 'bi bi-' + icons[state];
      dot.classList.toggle('off', state !== 'live');
      label.textContent = stopped ? 'Stream stopped' : labels[state];
      button.setAttribute('aria-label', state === 'paused' ? 'Start listening' : 'Stop listening');
      button.disabled = stopped;
    }

    function stopStream() {
      if (retry !== null) { window.clearTimeout(retry); retry = null; }
      state = 'paused';
      attempt = 0;
      audio.pause();
      // Drop the buffer: pressing play again has to rejoin *live*, not
      // continue from where the listener stopped.
      audio.removeAttribute('src');
      audio.load();
      render();
    }

    function attach() {
      // Reset the element rather than reassigning the same src: after a 404 it
      // will not retry the same URL on its own.
      audio.removeAttribute('src');
      audio.load();
      audio.src = streamUrl;
      return audio.play();
    }

    function tryConnect() {
      attach().then(function () {
        state = 'live';
        attempt = 0;
        hint.classList.add('d-none');
        render();
      }).catch(function (error) {
        // A blocked autoplay is the browser saying no, and retrying will not
        // change its mind — only a real gesture will.
        if (error && error.name === 'NotAllowedError') { giveUp(); return; }
        if (state !== 'connecting') return;
        if (++attempt >= CONNECT_ATTEMPTS) { giveUp(); return; }
        retry = window.setTimeout(
          tryConnect,
          attempt < CONNECT_QUICK_ATTEMPTS ? CONNECT_QUICK_MS : CONNECT_SLOW_MS
        );
      });
    }

    function giveUp() {
      stopStream();
      hint.classList.remove('d-none');
    }

    function startStream() {
      if (stopped) return;
      if (retry !== null) { window.clearTimeout(retry); retry = null; }
      state = 'connecting';
      attempt = 0;
      hint.classList.add('d-none');
      render();
      tryConnect();
    }

    button.addEventListener('click', function () {
      if (state === 'paused') startStream(); else stopStream();
    });

    /* Lock screen, headphone buttons, the car. A radio in a pocket is judged
     * on this, and it is the whole difference between a page that plays audio
     * and something that behaves like an app. */
    function updateMediaSession() {
      if (!('mediaSession' in window.navigator)) return;

      var body = document.querySelector('#nowPlaying .player-body[data-title]');
      try {
        if (body && window.MediaMetadata) {
          var art = body.getAttribute('data-art');
          window.navigator.mediaSession.metadata = new window.MediaMetadata({
            title: body.getAttribute('data-title') || 'streamchen',
            artist: body.getAttribute('data-channel') || '',
            album: roomName(),
            // The same thumbnail the page is already showing, so this exposes
            // nothing the browser had not already fetched.
            artwork: art ? [{ src: art, sizes: '512x512', type: 'image/jpeg' }] : []
          });
        }
        window.navigator.mediaSession.playbackState = state === 'live' ? 'playing' : 'paused';
      } catch (e) { /* an older implementation; not worth a broken player */ }
    }

    if ('mediaSession' in window.navigator) {
      try {
        window.navigator.mediaSession.setActionHandler('play', startStream);
        window.navigator.mediaSession.setActionHandler('pause', stopStream);
        window.navigator.mediaSession.setActionHandler('stop', stopStream);
      } catch (e) { /* nothing to do about it */ }
    }

    player = {
      /* Called when the server says the stream was stopped or started —
       * either by this host or by another one in another browser. */
      setStopped: function (value) {
        if (value === stopped) return;
        stopped = value;
        root.classList.toggle('stream-off', stopped);

        var notice = document.getElementById('streamStoppedNotice');
        if (notice) notice.classList.toggle('d-none', !stopped);

        if (stopped) {
          stopStream();
        } else {
          hint.classList.add('d-none');
          render();
        }
      },
      isStopped: function () { return stopped; },
      refreshMetadata: updateMediaSession
    };

    /* A live stream ending means the source went away — a worker restart, or
     * the room being handed to another one. Rejoining is almost always the
     * right answer, and the attempt cap stops it spinning on a deleted room. */
    function dropped() {
      // A stopped stream has no source to rejoin; that is not a drop, it is
      // the room being quiet on purpose.
      if (state !== 'live' || stopped) return;
      state = 'connecting';
      attempt = 0;
      render();
      tryConnect();
    }

    audio.addEventListener('ended', dropped);
    audio.addEventListener('error', dropped);
    audio.addEventListener('playing', updateMediaSession);
    audio.addEventListener('pause', updateMediaSession);

    volume.addEventListener('input', function () {
      audio.volume = Number(volume.value);
      try { window.localStorage.setItem(VOLUME_KEY, volume.value); } catch (e) { /* private */ }
    });

    // Volume is a preference, not room state: it belongs to this browser.
    try {
      var saved = window.localStorage.getItem(VOLUME_KEY);
      if (saved !== null) volume.value = saved;
    } catch (e) { /* private mode */ }
    audio.volume = Number(volume.value);

    root.classList.toggle('stream-off', stopped);
    render();
    updateMediaSession();
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

  // -- Chat ----------------------------------------------------------------
  /* Where a live chat message is delivered. Set by wireChat, read by the
   * socket; null when the room has chat turned off. */
  var chatSink = null;
  var chatReload = null;

  /* A socket that was down missed whatever was said meanwhile; the history
   * endpoint is what makes that cost nothing. */
  function reloadChat() {
    if (chatReload) chatReload();
  }

  function wireChat(token) {
    var log = document.getElementById('chatLog');
    var form = document.getElementById('chatForm');
    var input = document.getElementById('chatInput');
    if (!log || !form || !input) return;

    var empty = document.getElementById('chatEmpty');
    // Our own message is appended the moment the POST returns, and arrives
    // again on the socket a heartbeat later. Ids are how the second one is
    // recognised as the same message.
    var seen = Object.create(null);

    function atBottom() {
      return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    }

    function timeOf(value) {
      var when = new Date(value);
      if (isNaN(when.getTime())) return '';
      return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function append(message, mine) {
      if (!message || !message.id || seen[message.id]) return;
      seen[message.id] = true;

      // Was the reader at the bottom *before* this arrived? Deciding after
      // would be deciding whether to follow a message by whether it exists.
      var follow = atBottom();
      if (empty) empty.remove();

      var row = document.createElement('div');
      row.className = 'chat-msg' + (mine ? ' mine' : '');

      var art = document.createElement('img');
      art.className = 'avatar avatar-sm';
      art.src = '/a/' + encodeURIComponent(message.avatar || '') + '.svg';
      art.alt = '';
      art.width = 26;
      art.height = 26;
      art.loading = 'lazy';
      row.appendChild(art);

      var body = document.createElement('div');
      body.className = 'chat-body';

      var who = document.createElement('div');
      who.className = 'chat-who';
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = message.name || 'guest';
      who.appendChild(name);
      var at = document.createElement('span');
      at.textContent = timeOf(message.at);
      who.appendChild(at);
      body.appendChild(who);

      // textContent, never innerHTML: this is the one place on the page where
      // a listener's own words are rendered.
      var text = document.createElement('div');
      text.className = 'chat-text';
      text.textContent = message.text || '';
      body.appendChild(text);

      row.appendChild(body);
      log.appendChild(row);

      if (follow) log.scrollTop = log.scrollHeight;
    }

    chatSink = function (message) { append(message, false); };
    chatReload = function () { load(); };

    function load() {
      api('/rooms/' + token + '/chat', { token: token })
        .then(function (messages) {
          (messages || []).forEach(function (message) { append(message, false); });
          log.scrollTop = log.scrollHeight;
        })
        .catch(function () { /* chat is the least important thing on the page */ });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var text = (input.value || '').trim();
      if (!text) return;

      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      input.value = '';

      api('/rooms/' + token + '/chat', {
        method: 'POST', body: { text: text }, token: token
      })
        .then(function (message) { append(message, true); })
        .catch(function (error) {
          // Give it back rather than losing what they typed.
          input.value = text;
          toast(error.message || 'Could not send that', 'danger');
        })
        .finally(function () {
          button.disabled = false;
          input.focus();
        });
    });

    load();
  }

  // -- The room itself -----------------------------------------------------
  /* Renaming is a host changing the room's name and nothing else: the link is
   * the token and is never derived from it, so every bookmark, every open tab
   * and the stream itself survive a rename. Hence no reload. */
  function wireRoomRename(token) {
    var open = document.getElementById('roomRenameOpen');
    var form = document.getElementById('roomRenameForm');
    var input = document.getElementById('roomName');
    var save = document.getElementById('roomRenameSave');
    if (!open || !form || !input || !save) return;

    open.addEventListener('click', function () {
      var hidden = form.classList.toggle('d-none');
      if (!hidden) { input.value = roomName(); input.focus(); input.select(); }
    });

    function submit() {
      var name = (input.value || '').trim();
      if (!name) return;

      save.disabled = true;
      api('/rooms/' + token, { method: 'PATCH', body: { name: name }, token: token })
        .then(function (state) {
          setRoomName(state.name);
          rememberRoom(token, state.name);
          form.classList.add('d-none');
          toast('Renamed. The link is unchanged.', 'success');
        })
        .catch(function (error) { toast(error.message || 'Could not rename', 'danger'); })
        .finally(function () { save.disabled = false; });
    }

    save.addEventListener('click', submit);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); submit(); }
      if (event.key === 'Escape') form.classList.add('d-none');
    });
  }

  function setRoomName(name) {
    var title = document.getElementById('roomTitle');
    if (title && title.textContent !== name) title.textContent = name;

    var input = document.getElementById('roomName');
    if (input && document.activeElement !== input) input.value = name;

    // The tab, and the name a PWA shows in the task switcher.
    var suffix = document.title.indexOf('—') === -1 ? '' : document.title.split('—').pop();
    document.title = name + (suffix ? ' —' + suffix : '');
  }

  /* Stopping the stream keeps the room: the queue, the listeners and the link
   * are all still there, and the audio source is not. */
  function wireStreamToggle(token) {
    var button = document.getElementById('streamToggle');
    if (!button) return;

    button.addEventListener('click', function () {
      var stopping = button.getAttribute('data-stopped') !== '1';
      button.disabled = true;

      api('/rooms/' + token, {
        method: 'PATCH', body: { stream_stopped: stopping }, token: token
      })
        .then(function (state) {
          setStreamStopped(state.settings.stream_stopped);
          toast(stopping ? 'Stream stopped. The room stays.' : 'Stream starting…', 'success');
        })
        .catch(function (error) { toast(error.message || 'Could not do that', 'danger'); })
        .finally(function () { button.disabled = false; });
    });
  }

  function setStreamStopped(stopped) {
    var root = document.getElementById('room');
    if (root) root.setAttribute('data-stopped', stopped ? '1' : '0');
    if (player) player.setStopped(stopped);

    [document.getElementById('streamToggle'),
     document.querySelector('[data-host-action="stream"]')].forEach(function (button) {
      if (!button) return;
      button.setAttribute('data-stopped', stopped ? '1' : '0');

      var icon = button.querySelector('i');
      if (icon) icon.className = 'bi bi-' + (stopped ? 'play-circle' : 'stop-circle') + ' me-1';

      var label = button.querySelector('span') || button;
      label.textContent = stopped ? 'Start stream' : 'Stop stream';
    });
  }

  /* The share sheet where there is one: on a phone this is how a link is
   * actually sent, and it beats "copy, open the other app, paste". */
  function wireShare() {
    var button = document.getElementById('shareNative');
    var link = document.getElementById('shareLink');
    if (!button || !link || !navigator.share) return;

    button.classList.remove('d-none');
    button.addEventListener('click', function () {
      navigator.share({
        title: roomName(),
        text: 'Listen with me on ' + roomName(),
        url: link.value
      }).catch(function () { /* dismissed */ });
    });
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

  // -- Who is here ---------------------------------------------------------
  /* Reloads the roster if somebody is looking at it. Set by wireListeners,
   * called by the socket when the room's population changes. */
  var rosterSink = null;

  /* The listener count in the header is a button, because "two listeners" is
   * the beginning of a question. The list behind it is who is in the room right
   * now -- not everybody who ever opened the link, which is the host's list and
   * a different question. */
  function wireListeners(token) {
    var modal = document.getElementById('listenersModal');
    var list = document.getElementById('roomListenerList');
    if (!modal || !list) return;

    function load() {
      // No host secret: being in the room is the whole permission this needs.
      api('/rooms/' + token + '/roster').then(function (rows) {
        list.textContent = '';
        rows.forEach(function (person) {
          list.appendChild(personRow(person));
        });
      }).catch(function () {
        list.textContent = '';
        list.appendChild(note('Could not load who is here.'));
      });
    }

    modal.addEventListener('show.bs.modal', load);
    // Only while it is open; the count in the header updates on its own.
    rosterSink = function () {
      if (modal.classList.contains('show')) load();
    };
  }

  function note(text) {
    var node = document.createElement('p');
    node.className = 'text-muted small mb-0';
    node.textContent = text;
    return node;
  }

  function personRow(person) {
    var row = document.createElement('div');
    row.className = 'list-row';

    var art = document.createElement('img');
    art.className = 'avatar avatar-md';
    art.src = '/a/' + encodeURIComponent(person.avatar || '') + '.svg';
    art.alt = '';
    art.width = 34;
    art.height = 34;
    art.loading = 'lazy';

    var name = document.createElement('span');
    name.textContent = person.display_name;

    var labels = document.createElement('div');
    labels.appendChild(name);
    if (person.is_host) labels.appendChild(badge('host'));
    if (person.is_me) labels.appendChild(badge('you'));

    var head = document.createElement('div');
    head.className = 'd-flex align-items-center gap-2';
    head.appendChild(art);
    head.appendChild(labels);
    row.appendChild(head);
    return row;
  }

  // -- Host ----------------------------------------------------------------
  function wireHostControls(token, root) {
    var modal = document.getElementById('hostModal');
    var panel = document.getElementById('hostPanel');
    if (!modal || !panel) return;

    // Who is in the room is only worth fetching while somebody is looking.
    modal.addEventListener('show.bs.modal', function () { loadListeners(token); });

    /* Most settings change what the server renders — which buttons the queue
     * has, whether the add box is disabled — so the honest answer is to let
     * the server say. The exceptions are the ones with a live control on the
     * page already, which update in place instead of throwing playback away. */
    function patch(changes, reload) {
      return api('/rooms/' + token, { method: 'PATCH', body: changes, token: token })
        .then(function (state) {
          if (reload === false) return state;
          window.location.reload();
          return state;
        })
        .catch(function (error) {
          toast(error.message || 'Could not apply that', 'danger');
        });
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
      } else if (action === 'chat') {
        patch({ chat_enabled: root.getAttribute('data-chat') !== '1' });
      } else if (action === 'news') {
        patch({ news_enabled: root.getAttribute('data-news') !== '1' });
      } else if (action === 'stream') {
        var stopping = button.getAttribute('data-stopped') !== '1';
        patch({ stream_stopped: stopping }, false).then(function (state) {
          if (state) setStreamStopped(state.settings.stream_stopped);
        });
      } else if (action === 'playlist') {
        patch({ fallback_playlist: document.getElementById('fallbackPlaylist').value }, false)
          .then(function () { toast('Radio playlist saved', 'success'); });
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
        patch(changes, false).then(function () { toast('Saved', 'success'); });
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

        var art = document.createElement('img');
        art.className = 'avatar avatar-md';
        art.src = '/a/' + encodeURIComponent(listener.avatar || '') + '.svg';
        art.alt = '';
        art.width = 34;
        art.height = 34;
        art.loading = 'lazy';

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

        var head = document.createElement('div');
        head.className = 'd-flex align-items-center gap-2';
        head.appendChild(art);
        head.appendChild(left);
        row.appendChild(head);

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

    var button = document.getElementById('claimButton');
    if (!button) return;

    button.addEventListener('click', function () {
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

  // The server's two "do not come back" close codes (app/api/routers/ws.py).
  var CLOSE_REMOVED = 4403;
  var CLOSE_NO_ROOM = 4404;

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
      var plural = document.getElementById('listenerPlural');
      if (plural) plural.textContent = listeners === '1' ? '' : 's';

      // A rename by another host, or by this one in another tab.
      setRoomName(meta.getAttribute('data-room-name') || roomName());

      // The stream may have been stopped or started by a host elsewhere.
      setStreamStopped(meta.getAttribute('data-stream-stopped') === '1');

      var root = document.getElementById('room');
      if (root) root.setAttribute('data-chat', meta.getAttribute('data-chat-enabled'));

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

    if (player) player.refreshMetadata();
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
        // A socket that was down missed whatever was said while it was; the
        // room itself is refetched anyway, and chat has its own history.
        var reconnected = attempt > 0;
        attempt = 0;
        setConnection('connected');
        ping = window.setInterval(function () { socket.send('ping'); }, 20000);
        if (reconnected) {
          refresh(token);
          reloadChat();
        }
      };

      socket.onmessage = function (message) {
        var event;
        try { event = JSON.parse(message.data); } catch (e) { return; }
        if (event.type === 'PONG' || event.type === 'READY') return;

        // The one event that carries what happened rather than a hint to go
        // and look: a chat line is appended, and nothing is refetched.
        if (event.type === 'CHAT_MESSAGE') {
          if (chatSink) chatSink(event.data);
          return;
        }

        if (event.type === 'PLAYBACK_POSITION') {
          var body = document.querySelector('#nowPlaying .player-body[data-duration]');
          if (body && typeof event.data.position_s === 'number') {
            body.setAttribute('data-position', String(event.data.position_s));
          }
          return;
        }

        // Somebody arrived or left, so the open roster is now out of date.
        if (event.type === 'LISTENER_JOINED' || event.type === 'LISTENER_LEFT') {
          if (rosterSink) rosterSink();
        }

        scheduleRefresh(token);
      };

      socket.onerror = function () { setConnection('error'); };

      socket.onclose = function (event) {
        if (ping !== null) { window.clearInterval(ping); ping = null; }
        setConnection('disconnected');

        /* Removed from the room, or the room is gone. Reconnecting would be a
         * loop with nothing at the end of it, and this page is now a lie:
         * reload and let the server say so. */
        if (event && (event.code === CLOSE_REMOVED || event.code === CLOSE_NO_ROOM)) {
          window.location.reload();
          return;
        }

        // Back off, but keep trying: reconnecting on its own is the difference
        // between a hiccup and a dead page.
        var delay = Math.min(15000, 1000 * Math.pow(2, attempt++));
        window.setTimeout(open, delay);
      };
    }

    open();
  }

  // ---- Installability ------------------------------------------------------
  /* "Not now" is remembered, so the banner is offered once per browser rather
   * than on every visit. The button in the navbar stays either way: it is the
   * answer to "where did that go?". */
  var INSTALL_HINT_KEY = 'streamchen:install-hint';
  var INSTALL_BANNER_DELAY_MS = 5000;

  function installedAsApp() {
    try {
      if (window.matchMedia('(display-mode: standalone)').matches) return true;
      if (window.matchMedia('(display-mode: minimal-ui)').matches) return true;
    } catch (e) { /* an old browser; fall through to the iOS flag */ }
    return window.navigator.standalone === true;
  }

  function isIos() {
    var ua = window.navigator.userAgent || '';
    // iPadOS 13+ calls itself a Mac; the touch API is what tells them apart.
    return /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && 'ontouchend' in document);
  }

  function installHintSilenced() {
    try { return window.localStorage.getItem(INSTALL_HINT_KEY) === 'off'; } catch (e) { return false; }
  }

  function silenceInstallHint() {
    try { window.localStorage.setItem(INSTALL_HINT_KEY, 'off'); } catch (e) { /* private mode */ }
  }

  /* Two different worlds. Chromium fires beforeinstallprompt and hands us an
   * event to keep until somebody asks; Safari has no such thing and installs
   * from the share sheet, so there the same button explains instead of
   * prompting. Everything below is only ever shown when one of the two applies,
   * because a dead "Install" button is worse than none. */
  function wireInstall() {
    var button = document.getElementById('installButton');
    var banner = document.getElementById('installBanner');
    var accept = document.getElementById('installAccept');
    var dismiss = document.getElementById('installDismiss');
    if (!button) return;

    var deferred = null;
    var offered = false;

    function hide() {
      button.classList.add('d-none');
      if (banner) banner.classList.add('d-none');
    }

    function showBanner() {
      if (!banner || installedAsApp() || installHintSilenced()) return;
      banner.classList.remove('d-none');
    }

    /* Given a moment first: an install prompt is the wrong thing to meet a
     * listener with before they have heard anything. */
    function offer() {
      if (offered || installedAsApp()) return;
      offered = true;
      button.classList.remove('d-none');
      if (!installHintSilenced()) window.setTimeout(showBanner, INSTALL_BANNER_DELAY_MS);
    }

    function instructions() {
      var ios = isIos();
      [['installStepsIntro', ios], ['installStepsIos', ios],
       ['installStepsDesktopIntro', !ios], ['installStepsDesktop', !ios]
      ].forEach(function (pair) {
        var node = document.getElementById(pair[0]);
        if (node) node.classList.toggle('d-none', !pair[1]);
      });

      var modal = document.getElementById('installModal');
      if (modal && window.bootstrap) window.bootstrap.Modal.getOrCreateInstance(modal).show();
    }

    function install() {
      if (banner) banner.classList.add('d-none');
      if (!deferred) { instructions(); return; }

      var event = deferred;
      // Single use: the browser will fire beforeinstallprompt again if it still
      // wants to offer, and prompting twice with one event throws.
      deferred = null;
      event.prompt();
      var choice = event.userChoice;
      if (!choice || !choice.then) return;
      choice.then(function (result) {
        if (result && result.outcome === 'accepted') { silenceInstallHint(); hide(); }
      }).catch(function () { /* dismissed by the browser */ });
    }

    window.addEventListener('beforeinstallprompt', function (event) {
      // Ours to decide when: the browser's own bar is not something the page
      // can style, place or delay.
      event.preventDefault();
      deferred = event;
      offer();
    });

    // Nothing to defer here; Safari's install lives in the share sheet.
    if (isIos() && !installedAsApp()) offer();

    window.addEventListener('appinstalled', function () { silenceInstallHint(); hide(); });

    button.addEventListener('click', install);
    if (accept) accept.addEventListener('click', install);
    if (dismiss) dismiss.addEventListener('click', function () {
      silenceInstallHint();
      if (banner) banner.classList.add('d-none');
    });
  }

  /* The service worker caches the shell and answers navigations when the
   * network is gone. It deliberately never touches the API, the room page or
   * the stream — see sw.js. Registered from the root so its scope is the whole
   * app rather than /static. */
  function registerServiceWorker() {
    // Absent entirely outside a secure context, which is the whole of the
    // check: https, localhost and 127.0.0.1 have it, plain http does not.
    if (!('serviceWorker' in navigator)) return;

    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {
        /* an old browser, or a private window; the app works without it */
      });
    });
  }

  // ---- Boot ----------------------------------------------------------------
  document.addEventListener('DOMContentLoaded', function () {
    wireMailLinks();
    wireHome();
    wireRoom();
    wireInstall();
    registerServiceWorker();
  });
})();
