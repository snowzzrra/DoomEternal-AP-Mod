"""Select and cache the official, compatible Core distribution."""

import contextlib
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import zipfile
from pathlib import Path

from doom_eap.contracts.core_distribution import validate_manifest, verify_bytes, verify_runtime, version_key
from doom_eap.launcher.ui.strings import text as player_text

from .launcher_platform import ProcessInspectionError, UrlDownloadTransport, detect_doom_processes, probe_meathook

RELEASES_URL = "https://api.github.com/repos/snowzzrra/Sentinel-Core/releases?per_page=100"


class CoreRuntime:
    def __init__(self, root, *, transport=None, emit=None):
        self.root = Path(root)
        self.transport = transport or UrlDownloadTransport(timeout=15, max_retries=0)
        self.emit = emit or (lambda *args, **kwargs: None)
        self._lock = threading.RLock()
        self.checked = False

    def _metadata(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        saved = self.root / (key + ".json")
        try:
            cached = json.loads(saved.read_text()) if saved.exists() else {}
            if not isinstance(cached, dict):
                cached = {}
        except (OSError, ValueError):
            cached = {}
        headers = {"Accept": "application/vnd.github+json"}
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            path = Path(directory) / "response"
            try:
                response = self.transport.fetch(url, path, headers=headers)
            except urllib.error.HTTPError as error:
                if error.code == 304 and cached:
                    return cached["value"], cached.get("next")
                if error.code in (403, 429, 503):
                    delay = error.headers.get("Retry-After", "60")
                    reset = error.headers.get("X-RateLimit-Reset", "0")
                    from email.utils import parsedate_to_datetime
                    try:
                        retry = time.time() + int(delay) if delay.isdecimal() else parsedate_to_datetime(delay).timestamp()
                    except (ValueError, TypeError, OverflowError):
                        retry = time.time() + 60
                    retry = max(time.time(), retry, int(reset) if reset.isdecimal() else 0)
                    (self.root / "retry.json").write_text(json.dumps({"after": retry}))
                raise
            value = json.loads(path.read_text(encoding="utf-8"))
            received = {name.lower(): value for name, value in (response.headers or {}).items()}
            link = re.search(r'<([^>]+)>; rel="next"', received.get("link", ""))
            next_url = link.group(1) if link else None
            if next_url and not next_url.startswith(RELEASES_URL.split("?")[0] + "?"):
                raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
            temporary = saved.with_suffix(".incoming")
            temporary.write_text(json.dumps({"value": value, "etag": received.get("etag"), "next": next_url}))
            temporary.replace(saved)
            return value, next_url

    def cached(self):
        result = []
        for path in self.root.glob("*/distribution.json"):
            try:
                manifest, _ = verify_runtime(path)
                if manifest["channel"] == "stable":
                    result.append((manifest, path))
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                continue
        return sorted(result, key=lambda row: version_key(row[0]["version"]), reverse=True)

    def check(self, *, preferred_build=None):
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.checked = True
            retry = self.root / "retry.json"
            try:
                deferred_until = json.loads(retry.read_text()).get("after", 0) if retry.exists() else 0
            except (OSError, ValueError):
                deferred_until = 0
            if isinstance(deferred_until, int | float) and deferred_until > time.time():
                self.emit("core_update_status", state="deferred", message=player_text('launcher.core_runtime', 'update_service_is_busy_try_again_later'))
                return
            self.emit("core_update_status", state="checking", message=player_text('launcher.core_runtime', 'checking_for_game_connection_updates'))
            try:
                releases, url, visited = [], RELEASES_URL, set()
                while url:
                    if url in visited:
                        raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
                    visited.add(url)
                    page, url = self._metadata(url)
                    if not isinstance(page, list):
                        raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
                    releases.extend(page)
                candidates = []
                for release in releases:
                    if release.get("draft") or release.get("prerelease"):
                        continue
                    assets = {asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])}
                    if "distribution.json" not in assets or "sentinel-runtime.zip" not in assets:
                        continue
                    if any(not assets[name].startswith("https://github.com/snowzzrra/Sentinel-Core/releases/download/") for name in ("distribution.json", "sentinel-runtime.zip")):
                        raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
                    try:
                        manifest, _ = self._metadata(assets["distribution.json"])
                        validate_manifest(manifest)
                        if manifest["channel"] == "stable":
                            candidates.append((manifest, assets))
                    except (ValueError, KeyError, TypeError) as error:
                        self.emit("core_update_status", state="skipped",
                                  technical_message=player_text('launcher.core_runtime', 'game_connection_release_value_0_skipped_value_1', value_0=f"{release.get('tag_name', 'unknown')}", value_1=f'{error}'))
                if candidates:
                    manifest, assets = max(candidates, key=lambda row: version_key(row[0]["version"]))
                    self._download(manifest, assets)
                    for manifest, assets in candidates:
                        if manifest["build_id"] == preferred_build:
                            self._download(manifest, assets)
                            break
                self.emit("core_update_status", state="checked", message=player_text('launcher.core_runtime', 'update_check_finished'))
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
                self.emit("core_update_status", state="deferred", message=player_text('launcher.core_runtime', 'update_check_failed_retry_later'), technical_message=str(error))

    def _download(self, manifest, assets):
        identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        destination = self.root / identity
        if (destination / "distribution.json").exists():
            try:
                verify_runtime(destination / "distribution.json")
                return
            except (OSError, ValueError, zipfile.BadZipFile):
                pass
        self.emit("core_update_status", state="downloading", message=player_text('launcher.core_runtime', 'downloading_game_connection_files_value_0', value_0=f"{manifest['version']}"))
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            stage = Path(directory)
            archive = stage / "sentinel-runtime.zip"
            self.transport.fetch(assets[archive.name], archive)
            records = {item["path"]: item for item in manifest["artifacts"]}
            verify_bytes(archive.name, archive.read_bytes(), records[archive.name])
            with zipfile.ZipFile(archive) as package:
                if set(package.namelist()) != set(records) - {archive.name} or len(package.namelist()) != len(records) - 1:
                    raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
                for name in package.namelist():
                    if package.getinfo(name).file_size != records[name]["size"]:
                        raise ValueError(player_text('launcher.core_runtime', 'the_game_connection_download_could_not_be_checked_retry_later_if'))
                    data = package.read(name)
                    verify_bytes(name, data, records[name])
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            (stage / "distribution.json").write_text(json.dumps(manifest), encoding="utf-8")
            verify_runtime(stage / "distribution.json")
            destination.mkdir(parents=True, exist_ok=True)
            for path in stage.rglob("*"):
                if path.is_file():
                    target = destination / path.relative_to(stage)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)

    def resolve(self, config, game_root, *, refresh=False):
        override = config.get("core_runtime_manifest")
        if override:
            path = Path(str(override)).expanduser().resolve()
            if path.is_dir():
                path /= "distribution.json"
            verify_runtime(path, bootstrap_from_archive=True)
            return path
        installed = probe_meathook(game_root)
        selected = config.get("selected_core_runtime_manifest")
        if selected:
            try:
                manifest, _ = verify_runtime(Path(str(selected)), bootstrap_from_archive=True)
                if not installed.ok or manifest["build_id"] != installed.details["build_id"]:
                    selected = None
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
                selected = None
        try:
            running = bool(detect_doom_processes())
        except ProcessInspectionError:
            running = True
        hold = installed.ok and (running or (not config.get("core_auto_update", True) and not refresh))
        if hold and selected and not refresh:
            return Path(str(selected))
        preferred_build = installed.details["build_id"] if installed.ok and not config.get("core_auto_update", True) else None
        available = selected or any(
            not installed.ok or (row[0]["build_id"] == installed.details["build_id"] if hold else
                                version_key(row[0]["version"]) >= version_key(installed.details["version"]))
            for row in self.cached())
        if refresh or (not self.checked and not available):
            self.check(preferred_build=preferred_build)
        with self._lock if not available else contextlib.nullcontext():
            candidates = self.cached()
        if installed.ok:
            if hold:
                candidates = [row for row in candidates if row[0]["build_id"] == installed.details["build_id"]]
            else:
                candidates = [row for row in candidates if version_key(row[0]["version"]) >= version_key(installed.details["version"])]
        if candidates:
            return candidates[0][1]
        if selected:
            return Path(str(selected))
        raise RuntimeError(player_text('launcher.core_runtime', 'game_connection_files_are_unavailable_open_help_and_select_repai'))
