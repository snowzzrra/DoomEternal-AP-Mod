"""Publisher discovery for authorial trees and compiled runtime packages."""

from __future__ import annotations

import json
from pathlib import Path

from doom_eap.content.content_catalog import load_content_catalog
from doom_eap.contracts.publisher_contracts import (
    PublisherContract,
    publisher_contracts_from_document,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT
CONTRACT_PATH = REPO_ROOT / "data" / "publisher_contracts.json"


def load_publisher_contracts(path: Path | None = None) -> tuple[PublisherContract, ...]:
    if path is None and (ROOT / "content" / "maps").is_dir():
        return load_content_catalog().publishers
    path = path or CONTRACT_PATH
    return publisher_contracts_from_document(
        json.loads(path.read_text(encoding="utf-8"))
    )
