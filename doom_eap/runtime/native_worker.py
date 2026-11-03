"""Serialized blocking I/O with event-loop validation before accepting results."""
import asyncio
from concurrent.futures import ThreadPoolExecutor


class StaleNativeResult(asyncio.CancelledError):
    pass


class NativeWorker:
    def __init__(self, identity, process_probe):
        self.identity = identity
        self.process_probe = process_probe
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="native-owner")

    async def call(self, function, *args, process=None, **kwargs):
        identity = self.identity()

        def execute():
            if identity != self.identity():
                raise StaleNativeResult("Room or runtime session changed before native operation")
            if process is not None and self.process_probe() != process:
                raise StaleNativeResult("Game process changed before native operation")
            try:
                result = function(*args, **kwargs)
            finally:
                if process is not None and self.process_probe() != process:
                    raise StaleNativeResult("Game process changed during native operation")
            return result

        try:
            result = await asyncio.get_running_loop().run_in_executor(self.executor, execute)
        except Exception:
            if identity != self.identity():
                raise StaleNativeResult("Room or runtime session changed during native operation") from None
            raise
        if identity != self.identity():
            raise StaleNativeResult("Room or runtime session changed during native operation")
        return result

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
