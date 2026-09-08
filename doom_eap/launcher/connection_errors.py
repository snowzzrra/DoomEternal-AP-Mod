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
        "invalid_slot", "Player name is required",
        "Enter the player name generated for this room.",
        "Use the exact player name, including spelling and capitalization.",
    ),
    "invalid_slot": ConnectionFailure(
        "invalid_slot", "Player name not found",
        "The room accepted the connection, but this player name is not in the generated room.",
        "Use the exact player name from the room, including spelling and capitalization.",
    ),
    "invalid_password": ConnectionFailure(
        "invalid_password", "Room password rejected",
        "The room accepted the connection, but rejected the password.",
        "Check the room password and try again.",
    ),
    "invalid_game": ConnectionFailure(
        "invalid_game", "Wrong game for this player",
        "This player entry is not a DOOM Eternal slot in the generated room.",
        "Choose the DOOM Eternal player name from this room.",
    ),
    "incompatible_version": ConnectionFailure(
        "incompatible", "Incompatible Archipelago version",
        "The room and this DoomEAP build use incompatible protocol versions.",
        "Update DoomEAP or use a compatible Archipelago server, then retry.",
    ),
    "invalid_items_handling": ConnectionFailure(
        "incompatible", "Incompatible room settings",
        "The room rejected the item-handling mode used by this client.",
        "Update DoomEAP or rebuild the room with compatible settings.",
    ),
    "room_incompatible": ConnectionFailure(
        "incompatible", "Incompatible room package",
        "The connection succeeded, but the room data is not compatible with this DoomEAP build.",
        "Update DoomEAP or rebuild the room package with the matching version.",
    ),
    "connection_refused": ConnectionFailure(
        "refused", "Server refused the connection",
        "Nothing accepted the Archipelago connection at that address.",
        "Check that the server is running and that the host and port are correct.",
    ),
    "dns_failed": ConnectionFailure(
        "dns", "Server name not found",
        "Windows could not resolve the server name.",
        "Check the hostname or use the room's numeric address.",
    ),
    "connection_timeout": ConnectionFailure(
        "timeout", "Connection timed out",
        "The Archipelago server did not respond in time.",
        "Check the address, firewall, VPN, and server availability, then retry.",
    ),
    "tls_failed": ConnectionFailure(
        "tls", "Secure connection failed",
        "Windows could not establish or verify the secure server connection.",
        "Check the server address, certificate, system clock, and network inspection software.",
    ),
    "malformed_address": ConnectionFailure(
        "malformed_address", "Server address is invalid",
        "The server address is not in a supported host:port, ws://, or wss:// form.",
        "Correct the server address and try again.",
    ),
    "connection_closed": ConnectionFailure(
        "closed", "Server closed the connection",
        "The Archipelago server closed the connection before login completed.",
        "Check the server log and room availability, then retry.",
    ),
    "malformed_server_data": ConnectionFailure(
        "malformed", "Malformed room data",
        "The server returned room data that DoomEAP could not safely use.",
        "Verify the room file and server build. If it persists, generate a Support Report.",
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
        "unexpected", "Could not connect to the room",
        "DoomEAP could not complete the Archipelago connection.",
        "Retry once. If it still fails, generate a Support Report.",
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
