/* Review exposure, not attention. One parent clock also measures the Summary iframe. */
window.ReviewTiming = (() => {
  const SCHEMA = "review_timing_v1";
  const HEARTBEAT_MS = 5000;
  const MAX_OBSERVED_GAP_MS = 15000;

  function create(options) {
    const win = options.window || window;
    const doc = options.document || win.document;
    const now = options.now || (() => win.performance.now());
    const wallNow = options.wallNow || (() => Date.now());
    const uuid = options.uuid || (() => {
      if (win.crypto.randomUUID) return win.crypto.randomUUID();
      // randomUUID requires a secure context; local study servers may also be
      // accessed over plain HTTP on a laboratory network.
      const bytes = win.crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    });
    const prefix = `${SCHEMA}:${encodeURIComponent(options.participantId)}:${encodeURIComponent(options.runId)}:`;
    let storage;
    try { storage = options.storage || win.localStorage; } catch { storage = null; }
    const pending = new Map();
    let visit = null, setup = null, video = null, mediaPlaying = false;
    let suspended = false, foreground = false, playing = false;
    let lastMono = 0, lastWall = 0, offset = 0, queued = Promise.resolve();
    let destroyed = false;
    const listeners = [], videoListeners = [];

    function listen(target, type, handler, bucket = listeners) {
      target?.addEventListener(type, handler);
      bucket.push(() => target?.removeEventListener(type, handler));
    }

    function stored(key) {
      try { return storage?.getItem(key) || null; } catch { return null; }
    }

    function persist(snapshot) {
      const key = prefix + snapshot.visit_id;
      const json = JSON.stringify(snapshot);
      pending.set(key, json);
      try { storage?.setItem(key, json); } catch { /* Memory retry still works in this tab. */ }
    }

    function importPending() {
      try {
        for (let i = 0; i < (storage?.length || 0); i += 1) {
          const key = storage.key(i);
          if (!key?.startsWith(prefix)) continue;
          try {
            const json = stored(key);
            const item = JSON.parse(json);
            const current = pending.has(key) ? JSON.parse(pending.get(key)) : null;
            if (item?.schema === SCHEMA && key === prefix + item.visit_id
                && (!current || item.sequence > current.sequence)) pending.set(key, json);
          } catch { /* One corrupt entry must not hide other recoverable visits. */ }
        }
      } catch { /* Corrupt/unavailable browser storage does not stop a review. */ }
    }

    function updateFlags() {
      foreground = !suspended && doc.visibilityState === "visible" && doc.hasFocus();
      playing = foreground && mediaPlaying && Boolean(video && !video.paused && !video.ended
        && !video.seeking && video.readyState >= 3);
    }

    function appendInterval(start, end, focused, videoPlaying) {
      if (end <= start) return;
      const tail = visit.intervals.at(-1);
      if (tail && tail[1] === start && tail[2] === focused && tail[3] === videoPlaying) tail[1] = end;
      else visit.intervals.push([start, end, focused, videoPlaying]);
    }

    function sample() {
      if (!visit || visit.closed || suspended) return;
      const mono = now(), wall = wallNow();
      const monoDelta = Math.max(0, mono - lastMono);
      const wallDelta = wall - lastWall;
      const discontinuity = Math.abs(wallDelta - monoDelta) > 2000;
      // Some platforms stop the monotonic clock during sleep. Preserve the gap,
      // but never infer that the participant was reading or playing video in it.
      const delta = Math.max(monoDelta, discontinuity ? wallDelta : 0);
      const end = offset + delta;
      if (delta <= MAX_OBSERVED_GAP_MS && !discontinuity) appendInterval(offset, end, foreground, playing);
      offset = end;
      visit.observed_until_ms = visit.started_at_ms + offset;
      lastMono = mono;
      lastWall = wall;
      updateFlags();
    }

    function snapshot() {
      sample();
      return visit ? JSON.parse(JSON.stringify(visit)) : null;
    }

    function saveCurrent() {
      if (!visit) return;
      sample();
      visit.sequence += 1;
      persist(visit);
    }

    function transmit(settings = {}) {
      // The latest cumulative snapshot replaces earlier pending snapshots. A late
      // response must not erase a newer heartbeat saved while the request ran.
      const work = async () => {
        importPending();
        let firstError = null;
        for (const [key, json] of [...pending]) {
          try {
            await options.send(JSON.parse(json), settings);
            if (pending.get(key) === json) pending.delete(key);
            if (stored(key) === json) {
              try { storage?.removeItem(key); } catch { /* A duplicate retry is safe. */ }
            }
          } catch (error) { firstError ||= error; }
        }
        if (firstError) throw firstError;
      };
      // Unload delivery cannot wait behind an ordinary network request. The
      // service deduplicates its sequence if this races an earlier heartbeat.
      if (settings.keepalive) return work();
      const result = queued.then(work, work);
      queued = result.catch(() => {});
      return result;
    }

    function flush(settings = {}) {
      saveCurrent();
      return transmit(settings);
    }

    function boundary() {
      sample();
      updateFlags();
      if (visit && !visit.closed) flush().catch(() => {});
    }

    function bindVideo(nextVideo) {
      videoListeners.splice(0).forEach(remove => remove());
      video = nextVideo || null;
      mediaPlaying = Boolean(video && !video.paused && !video.seeking && video.readyState >= 3);
      const change = value => { sample(); mediaPlaying = value; updateFlags(); };
      listen(video, "playing", () => change(true), videoListeners);
      for (const type of ["pause", "waiting", "seeking", "ended", "emptied", "error"]) {
        listen(video, type, () => change(false), videoListeners);
      }
      listen(video, "seeked", () => change(Boolean(video && !video.paused && video.readyState >= 3)), videoListeners);
      const mediaWindow = video?.ownerDocument?.defaultView;
      if (mediaWindow && mediaWindow !== win) {
        listen(mediaWindow, "focus", boundary, videoListeners);
        listen(mediaWindow, "blur", () => win.setTimeout(boundary, 0), videoListeners);
      }
    }

    function start(config) {
      if (destroyed) return;
      if (visit && !visit.closed && visit.step_id === config.stepId) return;
      if (visit && !visit.closed) {
        sample();
        visit.closed = true;
        visit.end_reason = "superseded";
        saveCurrent();
      }
      setup = config;
      suspended = false;
      offset = 0;
      lastMono = now();
      lastWall = wallNow();
      visit = { schema: SCHEMA, visit_id: uuid(), step_id: config.stepId,
        view: config.view, review_index: config.reviewIndex, sequence: 0,
        started_at_ms: lastWall, observed_until_ms: lastWall, intervals: [], coverage: [],
        closed: false, end_reason: null };
      bindVideo(config.video);
      updateFlags();
      flush().catch(() => {});
    }

    function cover(value) {
      if (!visit || visit.closed || suspended || typeof value !== "string" || !value || visit.coverage.includes(value)) return;
      visit.coverage.push(value);
      flush().catch(() => {});
    }

    function suspend() {
      sample();
      suspended = true;
      updateFlags();
    }

    function resume() {
      if (!visit || visit.closed || !suspended) return;
      suspended = false;
      // The submission wait is known to be outside review exposure. On a failed
      // Next request it remains part of elapsed time, never foreground time.
      sample();
      updateFlags();
    }

    function finish(reason = "next") {
      if (visit && !visit.closed) {
        sample();
        visit.closed = true;
        visit.end_reason = reason;
      }
      return flush();
    }

    listen(doc, "visibilitychange", boundary);
    listen(win, "focus", boundary);
    // Moving focus into an iframe can dispatch blur on its parent window.
    // Recheck the containing document after focus settles, rather than treating
    // every parent blur as a departure from the study.
    listen(win, "blur", () => win.setTimeout(boundary, 0));
    listen(win, "pagehide", () => {
      if (visit && !visit.closed) {
        sample();
        visit.closed = true;
        visit.end_reason = "pagehide";
      }
      flush({ keepalive: true }).catch(() => {});
    });
    listen(win, "pageshow", event => {
      if (event.persisted && setup && visit?.end_reason === "pagehide") start(setup);
    });
    const heartbeat = win.setInterval(() => {
      if (visit && !visit.closed && !suspended) flush().catch(() => {});
      else if (pending.size) transmit().catch(() => {});
    }, HEARTBEAT_MS);
    importPending();
    transmit().catch(() => {});

    function destroy() {
      win.clearInterval(heartbeat);
      listeners.splice(0).forEach(remove => remove());
      videoListeners.splice(0).forEach(remove => remove());
      destroyed = true;
    }

    return { start, cover, suspend, resume, finish, flush, snapshot, destroy };
  }
  return { create };
})();
