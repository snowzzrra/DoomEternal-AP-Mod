"""Ownership of finite CommonClient adapter jobs, distinct from gameplay epochs."""
import asyncio


class SessionTasks:
    def __init__(self):
        self._tasks = set()
        self._generation = 0

    def start(self, operation):
        generation = self._generation

        async def run():
            if generation == self._generation:
                await operation()

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def invalidate(self):
        self._generation += 1
        for task in tuple(self._tasks):
            task.cancel()

    async def close(self):
        tasks = tuple(self._tasks)
        self.invalidate()
        await asyncio.gather(*tasks, return_exceptions=True)
