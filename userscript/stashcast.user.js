// ==UserScript==
// @name         StashCast – YouTube button
// @namespace    https://github.com/jonocodes/stashcast
// @version      1.0.0
// @description  Adds a StashCast button to every YouTube video. One click downloads it into StashCast: into the group that follows its channel, or into your default group.
// @match        https://www.youtube.com/*
// @match        https://m.youtube.com/*
// @grant        GM.xmlHttpRequest
// @grant        GM_xmlhttpRequest
// @connect      *
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  // ---- Configuration -------------------------------------------------------------
  // The admin page "YouTube Button" in StashCast fills these in for you.
  const STASHCAST_URL = 'https://stashcast.example.com';
  const TOKEN = 'paste-your-STASHCAST_USER_TOKEN-here';
  // Slug of the group for videos whose channel no group follows; '' = no group
  const DEFAULT_GROUP = '';
  // auto | audio | video. 'auto' uses the group's own "download type" setting.
  const TYPE = 'auto';
  // ---- End of configuration ------------------------------------------------------

  const ENDPOINT = STASHCAST_URL.replace(/\/+$/, '') + '/stash/';
  const BUTTON_CLASS = 'stashcast-btn';
  const MARK = 'data-stashcast';

  const TEXTS = {
    en: {
      add: 'Download with StashCast',
      working: 'Adding to StashCast…',
      added: 'Added to StashCast',
      addedTo: 'Added to StashCast – group “%s”',
      channelGroup: ' (channel group)',
      already: 'Already in StashCast',
      failed: 'StashCast: %s',
      unreachable: 'Could not reach StashCast',
      noGrant: 'The script has no permission for network requests (GM.xmlHttpRequest)',
    },
    pl: {
      add: 'Pobierz do StashCast',
      working: 'Dodawanie do StashCast…',
      added: 'Dodano do StashCast',
      addedTo: 'Dodano do StashCast – grupa „%s”',
      channelGroup: ' (grupa kanału)',
      already: 'Już jest w StashCast',
      failed: 'StashCast: %s',
      unreachable: 'Brak połączenia ze StashCast',
      noGrant: 'Skrypt nie ma uprawnień do zapytań sieciowych (GM.xmlHttpRequest)',
    },
  };
  const LANG = (navigator.language || 'en').toLowerCase().startsWith('pl') ? 'pl' : 'en';
  const t = (key, value) => TEXTS[LANG][key].replace('%s', value == null ? '' : value);

  // Videos handled in this session, so re-rendered thumbnails keep their state
  const states = new Map();

  // ---- Styles ---------------------------------------------------------------------
  const style = document.createElement('style');
  style.textContent = `
    .${BUTTON_CLASS} {
      position: absolute; top: 6px; left: 6px; z-index: 2000;
      width: 30px; height: 30px; border-radius: 50%; border: none; padding: 0;
      display: flex; align-items: center; justify-content: center;
      font: 600 18px/1 -apple-system, system-ui, sans-serif;
      color: #fff; background: rgba(15, 23, 42, .82); cursor: pointer;
      box-shadow: 0 1px 4px rgba(0, 0, 0, .4); opacity: .9;
      -webkit-tap-highlight-color: transparent;
    }
    .${BUTTON_CLASS}:hover { opacity: 1; background: #2563eb; }
    .${BUTTON_CLASS}[data-state="working"] { background: #64748b; cursor: wait; }
    .${BUTTON_CLASS}[data-state="done"] { background: #16a34a; }
    .${BUTTON_CLASS}[data-state="already"] { background: #0891b2; }
    .${BUTTON_CLASS}[data-state="error"] { background: #dc2626; }
    .${BUTTON_CLASS}.stashcast-floating {
      position: fixed; top: auto; left: auto; right: 16px; bottom: 84px;
      width: 52px; height: 52px; font-size: 26px;
    }
    .stashcast-toast {
      position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
      z-index: 2001; max-width: min(90vw, 480px); padding: 10px 16px; border-radius: 8px;
      font: 14px/1.4 -apple-system, system-ui, sans-serif; color: #fff;
      background: rgba(15, 23, 42, .92); box-shadow: 0 2px 10px rgba(0, 0, 0, .35);
      transition: opacity .3s; pointer-events: none;
    }
  `;
  document.head.appendChild(style);

  // ---- Helpers ----------------------------------------------------------------------
  function videoIdFrom(href) {
    if (!href) return null;
    const match = href.match(/(?:[?&]v=|\/shorts\/|youtu\.be\/|\/live\/)([A-Za-z0-9_-]{11})/);
    return match ? match[1] : null;
  }

  // One canonical form, the same one the channel sync stores - so the server can tell
  // a video it already has
  const videoUrl = (id) => `https://www.youtube.com/watch?v=${id}`;

  let toastTimer = null;
  function toast(message) {
    let el = document.querySelector('.stashcast-toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'stashcast-toast';
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.style.opacity = '1';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 4000);
  }

  function httpRequest(options) {
    if (typeof GM !== 'undefined' && GM.xmlHttpRequest) return GM.xmlHttpRequest(options);
    if (typeof GM_xmlhttpRequest !== 'undefined') return GM_xmlhttpRequest(options);
    options.onerror(new Error(t('noGrant')));
    return null;
  }

  // Ask StashCast to download a video. Resolves with the parsed JSON reply.
  function stash(id) {
    const body = new URLSearchParams({
      token: TOKEN,
      url: videoUrl(id),
      type: TYPE,
      skip_existing: 'true',
    });
    if (DEFAULT_GROUP) body.set('group', DEFAULT_GROUP);

    return new Promise((resolve, reject) => {
      httpRequest({
        method: 'POST',
        url: ENDPOINT,
        data: body.toString(),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        // The server looks the video up on YouTube before answering
        timeout: 90000,
        onload: (response) => {
          let data = {};
          try { data = JSON.parse(response.responseText); } catch (e) { /* not JSON */ }
          if (response.status >= 200 && response.status < 300) {
            resolve(data);
          } else {
            reject(new Error(data.error || `HTTP ${response.status}`));
          }
        },
        onerror: (error) => reject(error instanceof Error ? error : new Error(t('unreachable'))),
        ontimeout: () => reject(new Error(t('unreachable'))),
      });
    });
  }

  function describe(data) {
    if (data.already) return t('already');
    if (data.group) return t('addedTo', data.group) + (data.matched_channel ? t('channelGroup') : '');
    return t('added');
  }

  // ---- Buttons ------------------------------------------------------------------------
  const SYMBOLS = { idle: '+', working: '…', done: '✓', already: '✓', error: '!' };

  function render(button, id) {
    const state = states.get(id) || { name: 'idle', message: t('add') };
    button.dataset.state = state.name;
    button.textContent = SYMBOLS[state.name];
    button.title = state.message;
    button.setAttribute('aria-label', state.message);
  }

  function renderAll(id) {
    document.querySelectorAll(`.${BUTTON_CLASS}[data-video="${id}"]`).forEach((b) => render(b, id));
  }

  async function onClick(event) {
    // Never let the click reach YouTube: it would open the video
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();

    const id = event.currentTarget.dataset.video;
    const current = states.get(id);
    if (current && ['working', 'done', 'already'].includes(current.name)) {
      if (current.name !== 'working') toast(current.message);
      return;
    }

    states.set(id, { name: 'working', message: t('working') });
    renderAll(id);
    try {
      const data = await stash(id);
      const message = describe(data);
      states.set(id, { name: data.already ? 'already' : 'done', message });
      toast(message);
    } catch (error) {
      const message = t('failed', error.message);
      states.set(id, { name: 'error', message });
      toast(message);
    }
    renderAll(id);
  }

  function makeButton(id, extraClass) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = BUTTON_CLASS + (extraClass ? ` ${extraClass}` : '');
    button.dataset.video = id;
    // pointerdown/touchstart would otherwise start YouTube's inline preview or a drag
    ['pointerdown', 'mousedown', 'touchstart'].forEach((type) =>
      button.addEventListener(type, (e) => e.stopPropagation(), { passive: true })
    );
    button.addEventListener('click', onClick, true);
    render(button, id);
    return button;
  }

  // A button on every video thumbnail: any link to a video that shows an image
  function decorateThumbnails() {
    const links = document.querySelectorAll(
      `a[href*="/watch?v="]:not([${MARK}]), a[href*="/shorts/"]:not([${MARK}])`
    );
    links.forEach((link) => {
      // A title link, or a thumbnail whose image is not rendered yet - YouTube loads
      // them lazily, so it is not marked and gets another look on the next scan
      if (!link.querySelector('img')) return;
      link.setAttribute(MARK, '1');
      const id = videoIdFrom(link.getAttribute('href'));
      const holder = link.parentElement;
      if (!id || !holder || holder.querySelector(`:scope > .${BUTTON_CLASS}`)) return;
      // Next to the link rather than inside it, so the click never navigates
      if (getComputedStyle(holder).position === 'static') holder.style.position = 'relative';
      holder.appendChild(makeButton(id));
    });
  }

  // A large floating button for the video that is open right now
  function decorateCurrentVideo() {
    const id = /^\/(watch|shorts\/)/.test(location.pathname) ? videoIdFrom(location.href) : null;
    let floating = document.querySelector('.stashcast-floating');
    if (!id) {
      if (floating) floating.remove();
      return;
    }
    if (floating && floating.dataset.video === id) return;
    if (floating) floating.remove();
    floating = makeButton(id, 'stashcast-floating');
    document.body.appendChild(floating);
  }

  // YouTube is a single-page app that renders lazily: rescan after every change
  let scheduled = false;
  function scan() {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => {
      scheduled = false;
      decorateThumbnails();
      decorateCurrentVideo();
    }, 300);
  }

  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('yt-navigate-finish', scan);
  window.addEventListener('popstate', scan);
  scan();
})();
