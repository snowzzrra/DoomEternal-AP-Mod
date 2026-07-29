"""Utility to resolve resource archive priority according to packagemapspec.json.

The game engine loads archives in the exact order specified in the 'files'
array of packagemapspec.json. Candidates appearing earlier in the list (smaller
index) override candidates appearing later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

DEFAULT_PACKAGEMAPSPEC = Path(
    "/run/media/system/Eris/SteamLibrary/steamapps/common/DOOMEternal/base/packagemapspec.json"
)


def load_packagemapspec_indices(packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC) -> dict[str, int]:
    """Parse packagemapspec.json and return a map of archive name -> 0-based array index."""
    path = Path(packagemapspec_path)
    if not path.exists():
        raise FileNotFoundError(f"packagemapspec.json not found at {path}")
    
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files", [])
    if not isinstance(files, list):
        raise ValueError("packagemapspec.json 'files' must be a list")
    
    indices: dict[str, int] = {}
    for idx, entry in enumerate(files):
        if isinstance(entry, dict) and "name" in entry:
            name = entry["name"]
            if name not in indices:
                indices[name] = idx
    return indices


def resolve_owner(
    candidates: Sequence[str],
    packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC,
) -> str:
    """Select the candidate archive with the lowest index in packagemapspec.json.

    Args:
        candidates: Sequence of archive relative paths (e.g. ["game/sp/e3m1_slayer/e3m1_slayer_patch2.resources", ...])
        packagemapspec_path: Path to packagemapspec.json

    Returns:
        The candidate string that appears earliest in the files array.
    """
    if not candidates:
        raise ValueError("candidates list must not be empty")
    
    if len(candidates) != len(set(candidates)):
        raise ValueError(f"duplicate candidates provided: {candidates}")
    
    indices = load_packagemapspec_indices(packagemapspec_path)
    
    candidate_indices: list[tuple[int, str]] = []
    for candidate in candidates:
        if candidate not in indices:
            raise KeyError(
                f"Candidate archive {candidate!r} was not found in {packagemapspec_path}"
            )
        candidate_indices.append((indices[candidate], candidate))
    
    candidate_indices.sort(key=lambda item: item[0])
    
    # Check for tie/duplicate index (should not happen if candidates are unique and indices are unique per name)
    if len(candidate_indices) > 1 and candidate_indices[0][0] == candidate_indices[1][0]:
        raise ValueError(f"Tie in packagemapspec index for candidates: {candidate_indices}")
    
    return candidate_indices[0][1]


def resolve_owner_evidence(
    candidates: Sequence[str],
    packagemapspec_path: Path | str = DEFAULT_PACKAGEMAPSPEC,
) -> dict[str, Any]:
    """Build structured evidence object for onboarding / descriptors."""
    path = Path(packagemapspec_path)
    import hashlib
    content = path.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    indices = load_packagemapspec_indices(path)
    
    winner = resolve_owner(candidates, path)
    candidate_map = {c: indices[c] for c in candidates}
    
    return {
        "source": "packagemapspec",
        "winner": winner,
        "candidates": candidate_map,
        "packagemapspec_sha256": sha256,
    }
