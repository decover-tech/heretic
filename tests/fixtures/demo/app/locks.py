import threading

_lock = threading.Lock()


def bad(flag):
    _lock.acquire()
    if flag:
        return 1  # BUG: lock still held
    _lock.release()
    return 0


def ok_finally(flag):
    _lock.acquire()
    try:
        if flag:
            return 1
        return 0
    finally:
        _lock.release()


def ok_nonblocking():
    if _lock.acquire(blocking=False):
        _lock.release()


class Pool:
    def __init__(self):
        self._slots = threading.BoundedSemaphore(4)
        self._space = threading.Semaphore(2)

    def ok_callback(self, pool, fn):
        self._slots.acquire()
        try:
            fut = pool.submit(fn)
        except Exception:
            self._slots.release()
            raise
        fut.add_done_callback(lambda _f: self._slots.release())

    def ok_signal(self, q, item):
        self._space.acquire()  # released by the consumer thread
        q.append(item)


async def ok_trylock(lock):
    if not await lock.acquire():
        return None
    try:
        return 1
    finally:
        await lock.release()
