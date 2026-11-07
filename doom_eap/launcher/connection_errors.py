"""Stable player-facing classification for Archipelago connection failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from doom_eap.launcher.ui.strings import text as player_text


@dataclass(frozen=True)
class ConnectionFailure:
    category: str
    title: str
    message: str
    action: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


_BY_CODE = {
    "missing_slot": (
        "invalid_slot",
        ("launcher.connection_errors", "player_name_required"),
        ("launcher.connection_errors", "player_name_help"),
        ("launcher.connection_errors", "copy_player_name"),
    ),
    "invalid_slot": (
        "invalid_slot",
        ("launcher.connection_errors", "player_name_not_found"),
        ("launcher.connection_errors", "this_room_has_no_player_with_that_name"),
        ("launcher.connection_errors", "verify_player_name"),
    ),
    "invalid_password": (
        "invalid_password",
        ("launcher.connection_errors", "incorrect_room_password"),
        ("launcher.connection_errors", "the_password_does_not_match_this_room"),
        ("launcher.connection_errors", "check_the_room_password_and_try_again"),
    ),
    "password_required": (
        "password_required",
        ("launcher.connection_errors", "room_password_required"),
        ("launcher.connection_errors", "enter_the_password_for_this_room"),
        ("launcher.connection_errors", "enter_the_room_password_and_try_again"),
    ),
    "invalid_game": (
        "invalid_game",
        ("launcher.connection_errors", "this_player_is_assigned_another_game"),
        ("launcher.connection_errors", "wrong_player_game"),
        ("launcher.connection_errors", "enter_the_doom_eternal_player_name_from_this_room"),
    ),
    "incompatible_version": (
        "incompatible",
        ("launcher.connection_errors", "doomeap_and_the_room_are_incompatible"),
        ("launcher.connection_errors", "this_doomeap_version_cannot_connect_to_the_room"),
        ("launcher.connection_errors", "check_launcher_version"),
    ),
    "invalid_items_handling": (
        "incompatible",
        ("launcher.connection_errors", "room_settings_are_incompatible"),
        ("launcher.connection_errors", "this_doomeap_version_cannot_use_the_room_settings"),
        ("launcher.connection_errors", "report_to_host"),
    ),
    "room_incompatible": (
        "incompatible",
        ("launcher.connection_errors", "room_version_mismatch"),
        ("launcher.connection_errors", "incompatible_room"),
        ("launcher.connection_errors", "check_launcher_version"),
    ),
    "connection_refused": (
        "refused",
        ("launcher.connection_errors", "cannot_reach_the_server"),
        ("launcher.connection_errors", "the_server_did_not_accept_the_connection"),
        ("launcher.connection_errors", "verify_server"),
    ),
    "dns_failed": (
        "dns",
        ("launcher.connection_errors", "server_name_not_found"),
        ("launcher.connection_errors", "the_server_name_could_not_be_found"),
        ("launcher.connection_errors", "verify_address_spelling"),
    ),
    "connection_timeout": (
        "timeout",
        ("launcher.connection_errors", "connection_timed_out"),
        ("launcher.connection_errors", "the_server_did_not_respond_in_time"),
        ("launcher.connection_errors", "verify_connection"),
    ),
    "tls_failed": (
        "tls",
        ("launcher.connection_errors", "secure_connection_failed"),
        ("launcher.connection_errors", "windows_could_not_connect_securely_to_the_server"),
        ("launcher.connection_errors", "verify_address_clock"),
    ),
    "malformed_address": (
        "malformed_address",
        ("launcher.connection_errors", "invalid_server_address"),
        ("launcher.connection_errors", "the_room_address_has_an_incorrect_format"),
        ("launcher.connection_errors", "address_help"),
    ),
    "connection_closed": (
        "closed",
        ("launcher.connection_errors", "server_closed_the_connection"),
        ("launcher.connection_errors", "server_closed"),
        ("launcher.connection_errors", "retry_with_host"),
    ),
    "malformed_server_data": (
        "malformed",
        ("launcher.connection_errors", "room_data_could_not_be_read"),
        ("launcher.connection_errors", "doomeap_could_not_read_the_room_settings"),
        ("launcher.connection_errors", "report_to_host"),
    ),
}


def _failure_for(code):
    category, *entries = _BY_CODE[code]
    return ConnectionFailure(category, *(player_text(*entry) for entry in entries))


def _canonical_code(value: object) -> str:
    text = str(value or "").strip().replace("-", "_").casefold()
    aliases = {
        "invalidslot": "invalid_slot",
        "invalidpassword": "invalid_password",
        "invalidgame": "invalid_game",
        "incompatibleversion": "incompatible_version",
        "invaliditemshandling": "invalid_items_handling",
    }
    return aliases.get(text, text)


def normalize_connection_failure(event: Mapping[str, object]) -> ConnectionFailure:
    code = _canonical_code(event.get("code"))
    reason_codes = event.get("reason_codes")
    if isinstance(reason_codes, list | tuple):
        for reason in reason_codes:
            candidate = _canonical_code(reason)
            if candidate in _BY_CODE:
                code = candidate
                break
    if code in _BY_CODE:
        return _failure_for(code)

    return ConnectionFailure(
        "unexpected",
        player_text("launcher.connection_errors", "could_not_connect"),
        player_text("launcher.connection_errors", "doomeap_could_not_join_the_room"),
        player_text("launcher.connection_errors", "retry_then_report"),
    )


def enrich_connection_failure(event: Mapping[str, object], *, attempt_id: int) -> dict[str, object]:
    normalized = normalize_connection_failure(event)
    return {
        **event,
        **normalized.payload(),
        "failure_domain": "archipelago_connection",
        "attempt_id": attempt_id,
    }


def _validate_server_url(parsed, port):
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError('invalid server address')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('invalid server address')
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('invalid server address')

def validate_server_address(value: str) -> str:
    endpoint = value.strip()
    if not endpoint or any(character.isspace() for character in endpoint):
        raise ValueError('invalid server address')
    candidate = endpoint if "://" in endpoint else f"ws://{endpoint}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError('invalid server address') from error
    _validate_server_url(parsed, port)
    return endpoint


def connection_error_code(error):
    import socket
    import ssl

    from websockets.exceptions import ConnectionClosed, InvalidURI

    for kind, code in (
        (socket.gaierror, "dns_failed"),
        (TimeoutError, "connection_timeout"),
        (ssl.SSLError, "tls_failed"),
        (ConnectionRefusedError, "connection_refused"),
        (ConnectionClosed, "connection_closed"),
        (InvalidURI, "malformed_address"),
    ):
        if isinstance(error, kind):
            return code
    return "connection_failed"
