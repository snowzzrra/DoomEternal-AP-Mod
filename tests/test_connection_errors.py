import socket
import ssl

import pytest

from doom_eap.launcher.connection_errors import connection_error_code, normalize_connection_failure


@pytest.mark.parametrize(
    "error, code",
    [
        (socket.gaierror("arbitrary message"), "dns_failed"),
        (TimeoutError("arbitrary message"), "connection_timeout"),
        (ssl.SSLError("arbitrary message"), "tls_failed"),
        (ConnectionRefusedError("arbitrary message"), "connection_refused"),
        (RuntimeError("DNS timeout SSL connection refused"), "connection_failed"),
    ],
)
def test_connection_failure_uses_error_type_and_code(error, code):
    assert connection_error_code(error) == code
    event = {"code": code, "message": "unrelated translated text"}
    original = normalize_connection_failure(event)
    assert normalize_connection_failure({**event, "message": "different text"}) == original
    assert normalize_connection_failure({"message": "DNS timeout SSL connection refused"}).category == "unexpected"


def test_protocol_reason_codes_select_the_player_guidance():
    result = normalize_connection_failure(
        {"code": "connection_refused", "reason_codes": ["InvalidSlot"], "message": "invalid password"}
    )
    assert result.category == "invalid_slot"
