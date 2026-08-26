const STOPPED_SESSION_STATUSES = new Set([
  "paused", "failed", "completed", "cancelled", "stale",
]);

export function fullDeckSessionPollDelay(hidden) {
  return hidden ? 5000 : 1000;
}

export function isStoppedFullDeckSession(status) {
  return STOPPED_SESSION_STATUSES.has(status);
}

export function shouldApplyFullDeckSession(previousVersion, snapshot) {
  return Number(snapshot?.session_version) !== Number(previousVersion);
}

export function createFullDeckSessionPoller({
  fetchSession,
  onSnapshot,
  onStop,
  onError,
  isHidden = () => document.hidden,
  wait,
} = {}) {
  let generation = 0;
  let wakePending = null;

  const defaultWait = (delay) => new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      wakePending = null;
      resolve();
    }, delay);
    wakePending = () => {
      window.clearTimeout(timer);
      wakePending = null;
      resolve();
    };
  });
  const waitForNext = wait || defaultWait;

  return {
    stop() {
      generation += 1;
      wakePending?.();
    },
    wake() {
      wakePending?.();
    },
    async start(sessionId, initialVersion, { waitForVersionChangeFromStopped = false } = {}) {
      const currentGeneration = ++generation;
      let version = Number(initialVersion);
      let observedVersionChange = false;
      while (currentGeneration === generation) {
        await waitForNext(fullDeckSessionPollDelay(Boolean(isHidden())));
        if (currentGeneration !== generation) return;
        let snapshot;
        try {
          snapshot = await fetchSession(sessionId);
        } catch (error) {
          if (currentGeneration === generation) onError?.(error);
          continue;
        }
        if (currentGeneration !== generation) return;
        if (shouldApplyFullDeckSession(version, snapshot)) {
          version = Number(snapshot.session_version);
          observedVersionChange = true;
          await onSnapshot?.(snapshot);
        }
        if (isStoppedFullDeckSession(snapshot.status) && (!waitForVersionChangeFromStopped || observedVersionChange)) {
          if (currentGeneration === generation) await onStop?.(snapshot);
          return;
        }
      }
    },
  };
}
