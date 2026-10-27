import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doom_eap.runtime.connection_diagnostics import ConnectionDiagnostics


def test_connection_failure_preserves_error_and_redacts_credentials(caplog):
    error = TimeoutError("wss://user:uri-secret@example.invalid/?token=query-secret room-secret")
    transport = SimpleNamespace(connect=AsyncMock(side_effect=error))
    diagnostics = ConnectionDiagnostics(transport, SimpleNamespace(password="room-secret"))
    diagnostics.resolve = AsyncMock()

    async def connect():
        try:
            with pytest.raises(TimeoutError) as caught:
                await diagnostics.connect("wss://user:uri-secret@example.invalid/?token=query-secret")
            assert caught.value is error
        finally:
            await diagnostics.close()
        assert diagnostics.dns_task.done()

    with caplog.at_level(logging.INFO, logger="doom_eternal_ap.bridge"):
        asyncio.run(connect())
    assert "AP_CONNECT_ATTEMPT" in caplog.text
    assert "AP_CONNECT_RESULT" in caplog.text and '"outcome": "failed"' in caplog.text
    assert all(secret not in caplog.text for secret in ("uri-secret", "query-secret", "room-secret"))
