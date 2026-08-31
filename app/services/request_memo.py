from __future__ import annotations

"""Track A1 (A1.4/A1.5) perf-fix building block: request/composition-
scoped memoization, explicitly NOT a global/app-lifetime cache -- see
docs/PRODUCTIZATION_AUDIT.md's P0.18 finding and
scripts/benchmark_changes_list.py for the confirmed root cause this
exists to fix (evaluate_workflow() calling TaskDecisionService.evaluate()
on the same task_id ~2.8x per Change, and WorkProductService.
list_for_change() being re-queried per gate).

A thread-local stack of dicts, reentrant across nested `with memo.
scope():` blocks on the same thread (so a route-level scope wrapping a
call that itself also opens one still shares a single cache), and
isolated across threads (FastAPI runs sync routes on a threadpool, so a
plain instance-attribute cache would leak between concurrent requests).
Each service that wants memoization owns its OWN RequestMemo instance
(its own key namespace) -- never one shared cache guessed to be safe
across unrelated services.

Safe only because a caller opens a scope around ONE HTTP request or
composition operation (e.g. evaluate_workflow() itself, or one /changes
request) where nothing mutates the DB between reads -- never held open
across requests, never invalidated on write (there is nothing to
invalidate: it never outlives the scope)."""

import threading
from contextlib import contextmanager


class RequestMemo:
    def __init__(self):
        self._local = threading.local()

    @contextmanager
    def scope(self):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        cache = stack[-1] if stack else {}
        stack.append(cache)
        try:
            yield cache
        finally:
            stack.pop()

    def _cache(self):
        stack = getattr(self._local, "stack", None)
        return stack[-1] if stack else None

    def get(self, key, compute):
        """Return compute() the first time `key` is asked for inside the
        current scope (or every time, unmemoized, when no scope is open
        -- the exact same result as calling compute() directly, just
        without the caching benefit); the identical cached value on every
        later ask for the same key within that same scope."""
        cache = self._cache()
        if cache is not None and key in cache:
            return cache[key]
        result = compute()
        if cache is not None:
            cache[key] = result
        return result
