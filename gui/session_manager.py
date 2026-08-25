import gc
import time

_SESSION_DEBUG_LOGS = False


class SessionManager:
    def __init__(self):
        self._last_cleanup = 0.0

    def maintenance(self):
        now = time.time()
        if now - self._last_cleanup > 120:
            self._last_cleanup = now
            if _SESSION_DEBUG_LOGS:
                print("Session maintenance: GC running")
            gc.collect()


SESSION = SessionManager()
