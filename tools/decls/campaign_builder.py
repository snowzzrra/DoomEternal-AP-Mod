"""Room-scoped native campaign presentation and fresh Fortress DevMenu entry."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "data" / "campaign_sources"
PREFIX = "gameresources_patch2/generated/decls/"


def _entries(source: str) -> list[str]:
    """Read the authored outer list entries, preserving nested layer lists."""
    entries = []
    for match in re.finditer(r"^\t\t\titem\[\d+\] = \{", source, re.MULTILINE):
        depth, end = 1, match.end()
        while depth:
            depth += (source[end] == "{") - (source[end] == "}")
            end += 1
        entries.append(source[match.start():end] + "\n")
    return entries


def _source(key: str, manifest: dict) -> str:
    raw = (SOURCES / f"{key}.decl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest[key]["sha256"]:
        raise ValueError(f"Native campaign source drifted: {key}")
    return raw.decode("utf-8")


def build_campaign_overrides(hub_assetsinfo: bytes) -> dict[str, bytes]:
    manifest = json.loads((SOURCES / "manifest.json").read_text(encoding="utf-8"))
    result = {}
    for key in ("dlc1", "dlc2"):
        raw = (SOURCES / f"{key}.decl").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest[key]["sha256"]:
            raise ValueError(f"Native campaign source drifted: {key}")
        source = raw.decode("utf-8")
        before = "showDossierCurrency = false;"
        if source.count(before) != 1:
            raise ValueError(f"Native campaign currency owner missing: {key}")
        # The ordinary dossier reads the player's existing global currencies.
        # Change only visibility; preserve slots, purchase logic and balances.
        result[PREFIX + f"campaign/campaign/{key}.decl"] = source.replace(
            before, "showDossierCurrency = true;"
        ).encode("utf-8")
    hub = (SOURCES / "hub.decl").read_bytes()
    if hashlib.sha256(hub).hexdigest() != manifest["hub"]["sha256"]:
        raise ValueError("Native Fortress DevMenu source drifted")
    # This is the authored first Fortress DevMenu option: exact native layers,
    # spawn map and inventory declaration. Only the display label is AP-specific.
    source = hub.decode("utf-8")
    hub_entries = _entries(source)
    entry = hub_entries[0].replace("From E1M1-Intro", "Unified Campaign")
    result[PREFIX + "devmenuoption/devmenuoption/ap_unified_campaign.decl"] = (
        "{\n\tedit = {\n\t\tdevMenuList = {\n\t\t\tnum = 1;\n"
        + entry + "\t\t}\n\t}\n}\n"
    ).encode("utf-8")
    # Retain the native replay layers for all twenty stages. They differ from
    # new-game/checkpoint layers, including World Spear's early mission setup.
    from doom_eap.content.content_catalog import load_content_catalog
    from doom_eap.content.campaign_stages import campaign_stages
    stages = campaign_stages(load_content_catalog(SOURCES.parents[1]))
    authored = {}
    for key in ("missionlist", "missionlist_dlc1", "missionlist_dlc2"):
        for entry in _entries(_source(key, manifest)):
            authored[re.search(r'mapName = "([^"]+)";', entry)[1]] = entry
    rows = [authored[stage["map"]] for stage in stages]
    # Hub phase0 and phase1 use the authored first-visit layers. Later phases
    # select their own native layer sets; no inventory or completion is imported.
    for phase in range(8):
        from tools.maps.fortress_campaign import content_layers
        entry = hub_entries[max(0, phase - 1)]
        layers = re.search(r"devMenuActiveLayers = \{.*?\n\t\t\t\t\}", entry, re.DOTALL)[0]
        values = re.findall(r'item\[\d+\] = "([^"]+)";', layers) + content_layers(phase)
        layers = ('devMenuActiveLayers = {\n\t\t\t\t\tnum = ' + str(len(values)) + ';\n' +
                  ''.join(f'\t\t\t\t\titem[{i}] = "{value}";\n' for i, value in enumerate(values)) +
                  '\t\t\t\t}')
        rows.append('\t\t\titem[0] = {\n\t\t\t\tmapName = "game/hub/hub";\n\t\t\t\t'
                    + layers.replace("devMenuActiveLayers", "activeLayers") + "\n\t\t\t}\n")
    rows = [re.sub(r"item\[\d+\]", f"item[{i}]", entry, count=1) for i, entry in enumerate(rows)]
    result[PREFIX + "missionselectinfolist/missionlist_ap_unified.decl"] = (
        "{\n\tedit = {\n\t\tmissionSelectList = {\n\t\t\tnum = 28;\n"
        + "".join(rows) + "\t\t}\n\t}\n}\n"
    ).encode("utf-8")
    main = _source("main", manifest)
    if main.count('missionSelectList = "missionlist";') != 1:
        raise ValueError("Native Base campaign mission roster is missing")
    result[PREFIX + "campaign/campaign/main.decl"] = main.replace(
        'missionSelectList = "missionlist";', 'missionSelectList = "missionlist_ap_unified";'
    ).encode("utf-8")
    for layer in content_layers(7):
        result[PREFIX + f"layer/{layer}.decl"] = b"{\n}\n"
    # New streamfiles also need typed entries in the effective common catalog.
    # patch3 owns the last common.mapresources override on the supported build.
    assets = [{"name": layer, "mapResourceType": "layer"} for layer in content_layers(7)]
    assets += [
        {"name": "devmenuoption/ap_unified_campaign", "mapResourceType": "devMenuOption"},
        {"name": "missionlist_ap_unified", "mapResourceType": "missionSelectInfoList"},
    ]
    result["gameresources_patch3/EternalMod/assetsinfo/common.json"] = (
        json.dumps({"assets": assets}, indent=2) + "\n"
    ).encode("utf-8")
    # Entity layer membership belongs to the destination map's separate table.
    hub_assets = json.loads(hub_assetsinfo)
    hub_assets["layers"] = hub_assets.get("layers", []) + [{"name": layer} for layer in content_layers(7)]
    result["hub_patch2/EternalMod/assetsinfo/hub.json"] = (
        json.dumps(hub_assets, indent=2) + "\n"
    ).encode("utf-8")
    return result
