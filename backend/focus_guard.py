"""Keeps whatever Mac app the user is actually looking at in front while a
background module (ad_repricer.py, order_watcher.py) drives Opera through a
CDP-controlled tab. Opera tends to raise its own window the moment a
CDP-driven tab navigates, even when it's a background tab the user never
asked to see - a one-shot refocus after the fact still lets it flash on top
for the whole multi-second operation, so this fights for focus on a loop
for as long as the operation runs instead.

2026-07-01: repeated user reports of "the browser keeps popping up" trace to
this - every reprice/order-check cycle was stealing window focus, which read
as way more disruptive than the actual underlying activity was.

Refcounted singleton, not one guard per call: order_watch_loop and
auto_reprice_loop run as independent ~30-60s-interval asyncio tasks with no
mutual exclusion between them, so their CDP work can genuinely overlap. Two
independent FocusGuard instances overlapping could capture each other's
activated app as their own "original" (code review, 2026-07-01) - sharing
one guard across all concurrent callers means only the first caller in ever
captures the real original app, and only the last caller out releases it.
"""
import asyncio
import subprocess


def _frontmost_app_name():
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _activate_app(name):
    if not name:
        return
    try:
        subprocess.run(["osascript", "-e", f'tell application "{name}" to activate'],
                        capture_output=True, timeout=3)
    except Exception:
        pass


async def _keep_focused(name, stop_event, interval):
    loop = asyncio.get_event_loop()
    while not stop_event.is_set():
        await loop.run_in_executor(None, _activate_app, name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


class FocusGuard:
    """Usage: async with FocusGuard(): ...do CDP/Playwright stuff...
    Module-level shared state means concurrent callers join the same guard
    instead of racing independent ones - see module docstring. No-ops
    cleanly if the frontmost app can't be read (e.g. Accessibility
    permission not granted) - never raises, never blocks the wrapped
    operation."""

    _lock = None  # created lazily on first use, not at import time - an
    # asyncio.Lock() built at module-import time can bind to a different
    # event loop than the one actually running by the time it's awaited
    # (confirmed by a direct test under this repo's Python 3.9), which
    # raises "attached to a different loop" the first time two callers
    # overlap.
    _refcount = 0
    _original_app = None
    _stop_event = None
    _task = None

    def __init__(self, interval=0.4):
        self.interval = interval

    async def __aenter__(self):
        cls = FocusGuard
        if cls._lock is None:
            cls._lock = asyncio.Lock()  # safe: no await between check and assignment
        async with cls._lock:
            if cls._refcount == 0:
                loop = asyncio.get_event_loop()
                cls._original_app = await loop.run_in_executor(None, _frontmost_app_name)
                if cls._original_app:
                    cls._stop_event = asyncio.Event()
                    cls._task = asyncio.create_task(
                        _keep_focused(cls._original_app, cls._stop_event, self.interval)
                    )
            cls._refcount += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        cls = FocusGuard
        async with cls._lock:
            cls._refcount = max(0, cls._refcount - 1)
            if cls._refcount == 0 and cls._task:
                cls._stop_event.set()
                await cls._task
                cls._task = None
                cls._stop_event = None
                cls._original_app = None
