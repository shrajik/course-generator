/**
 * A tiny, framework-free "latest request wins" primitive for ignoring a
 * stale async response - e.g. a fetch for Course A that resolves after the
 * admin has already switched to Course B must never overwrite Course B's
 * state. No new dependency (no AbortController plumbing needed): a caller
 * grabs a request id from `start()` before awaiting, then checks
 * `isCurrent(id)` before applying the result. `invalidate()` lets a caller
 * mark any in-flight request stale WITHOUT starting a replacement - e.g.
 * switching away from a course entirely, with no new fetch issued yet.
 */

export interface RequestGuard {
  start: () => number;
  invalidate: () => void;
  isCurrent: (requestId: number) => boolean;
}

export function createRequestGuard(): RequestGuard {
  let current = 0;
  return {
    start: () => ++current,
    invalidate: () => {
      current += 1;
    },
    isCurrent: (requestId: number) => requestId === current,
  };
}
