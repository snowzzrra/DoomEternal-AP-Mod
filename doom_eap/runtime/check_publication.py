"""Current CommonClient wire adapter and submitted-location projection."""


class CheckPublication:
    def __init__(self, send, mark_submitted, goal_status):
        self._send, self._mark_submitted, self._goal_status = send, mark_submitted, goal_status

    async def location(self, location_id):
        await self._send([{"cmd": "LocationChecks", "locations": [location_id]}])

    async def locations(self, location_ids):
        await self._send([{"cmd": "LocationChecks", "locations": list(location_ids)}])

    async def scout(self, location_ids):
        await self._send([{"cmd": "LocationScouts", "locations": list(location_ids), "create_as_hint": 0}])

    async def goal(self):
        await self._send([{"cmd": "StatusUpdate", "status": self._goal_status}])

    def mark_submitted(self, location_id):
        self._mark_submitted(location_id)
