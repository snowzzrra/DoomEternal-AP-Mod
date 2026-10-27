"""Stable player-facing classification for Archipelago connection failures."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ConnectionFailure:
    category: str
    title: str
    message: str
    action: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


_BY_CODE = {
    "missing_slot": ConnectionFailure(
        "invalid_slot", "Player name required",
        "Enter your player name exactly as shown in the room.",
        "Copy the name from the room page, including capital letters.",
    ),
    "invalid_slot": ConnectionFailure(
        "invalid_slot", "Player name not found",
        "This room has no player with that name.",
        "Check the player name in the room and enter it exactly.",
    ),
    "invalid_password": ConnectionFailure(
        "invalid_password", "Incorrect room password",
        "The password does not match this room.",
        "Check the room password and try again.",
    ),
    "password_required": ConnectionFailure(
        "password_required", "Room password required",
        "Enter the password for this room.",
        "Enter the room password and try again.",
    ),
    "invalid_game": ConnectionFailure(
        "invalid_game", "This player is assigned another game",
        "This player name belongs to another game in the room.",
        "Enter the DOOM Eternal player name from this room.",
    ),
    "incompatible_version": ConnectionFailure(
        "incompatible", "DoomEAP and the room are incompatible",
        "This DoomEAP version cannot connect to the room.",
        "Select Help -> SAVE SUPPORT REPORT and ask the host which DoomEAP version to use.",
    ),
    "invalid_items_handling": ConnectionFailure(
        "incompatible", "Room settings are incompatible",
        "This DoomEAP version cannot use the room settings.",
        "Select Help -> SAVE SUPPORT REPORT and send it to the room host.",
    ),
    "room_incompatible": ConnectionFailure(
        "incompatible", "Room version mismatch",
        "This room is incompatible with your DoomEAP version.",
        "Select Help -> SAVE SUPPORT REPORT and ask the host which DoomEAP version to use.",
    ),
    "connection_refused": ConnectionFailure(
        "refused", "Cannot reach the server",
        "The server did not accept the connection.",
        "Check the room address and port. Ask the host whether the server is running.",
    ),
    "dns_failed": ConnectionFailure(
        "dns", "Server name not found",
        "The server name could not be found.",
        "Check the spelling of the server address or copy it again from the room page.",
    ),
    "connection_timeout": ConnectionFailure(
        "timeout", "Connection timed out",
        "The server did not respond in time.",
        "Check the room address and your internet connection, then retry. If it continues, select Help -> SAVE SUPPORT REPORT.",
    ),
    "tls_failed": ConnectionFailure(
        "tls", "Secure connection failed",
        "Windows could not connect securely to the server.",
        "Check the room address and Windows date and time, then retry. If it continues, select Help -> SAVE SUPPORT REPORT.",
    ),
    "malformed_address": ConnectionFailure(
        "malformed_address", "Invalid server address",
        "The room address has an incorrect format.",
        "Enter the address and port, for example archipelago.gg:12345, or paste the room's connection address.",
    ),
    "connection_closed": ConnectionFailure(
        "closed", "Server closed the connection",
        "The server closed the connection before you could join.",
        "Retry once. If it continues, ask the host whether the room is running and select Help -> SAVE SUPPORT REPORT.",
    ),
    "malformed_server_data": ConnectionFailure(
        "malformed", "Room data could not be read",
        "DoomEAP could not read the room settings.",
        "Select Help -> SAVE SUPPORT REPORT and send it to the room host.",
    ),
}


def _canonical_code(value: object) -> str:
    text = str(value or "").strip().replace("-", "_").casefold()
    aliases = {
        "invalidslot": "invalid_slot", "invalidpassword": "invalid_password",
        "invalidgame": "invalid_game", "incompatibleversion": "incompatible_version",
        "invaliditemshandling": "invalid_items_handling",
    }
    return aliases.get(text, text)


def normalize_connection_failure(event: Mapping[str, object]) -> ConnectionFailure:
    code = _canonical_code(event.get("code"))
    reason_codes = event.get("reason_codes")
    if isinstance(reason_codes, (list, tuple)):
        for reason in reason_codes:
            candidate = _canonical_code(reason)
            if candidate in _BY_CODE:
                code = candidate
                break
    if code in _BY_CODE:
        return _BY_CODE[code]

    technical = " ".join(str(event.get(key, "")) for key in (
        "message", "technical_message", "raw_message", "reason",
    )).casefold()
    if any(token in technical for token in ("getaddrinfo", "name or service not known", "nodename nor servname", "dns")):
        return _BY_CODE["dns_failed"]
    if any(token in technical for token in ("timed out", "timeout", "timeouterror")):
        return _BY_CODE["connection_timeout"]
    if any(token in technical for token in ("ssl", "tls", "certificate", "cert_verify")):
        return _BY_CODE["tls_failed"]
    if any(token in technical for token in ("connection refused", "connectionrefused", "winerror 10061")):
        return _BY_CODE["connection_refused"]
    if any(token in technical for token in ("connection closed", "server closed", "websocket connection is closed")):
        return _BY_CODE["connection_closed"]
    if any(token in technical for token in ("invalid uri", "invalid address", "invalid port", "malformed")):
        return _BY_CODE["malformed_address"]
    return ConnectionFailure(
        "unexpected", "Could not connect",
        "DoomEAP could not join the room.",
        "Retry once. If it still fails, select Help -> SAVE SUPPORT REPORT.",
    )


def enrich_connection_failure(event: Mapping[str, object], *, attempt_id: int) -> dict[str, object]:
    normalized = normalize_connection_failure(event)
    return {
        **event, **normalized.payload(),
        "failure_domain": "archipelago_connection", "attempt_id": attempt_id,
    }


def validate_server_address(value: str) -> str:
    endpoint = value.strip()
    if not endpoint or any(character.isspace() for character in endpoint):
        raise ValueError("invalid server address")
    candidate = endpoint if "://" in endpoint else f"ws://{endpoint}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid server address") from error
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("invalid server address")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid server address")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("invalid server address")
    return endpoint
