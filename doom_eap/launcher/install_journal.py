"""Durable installation file intents and checked recovery; no injector execution."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import tempfile
import time
from enum import Enum
from pathlib import Path

from doom_eap.launcher.launcher_platform import _atomic_write_bytes, publish_file
from doom_eap.launcher.ui.strings import text as player_text


class InstallPhase(str, Enum):
    PREPARING = 'PREPARING'
    PREPARED = 'PREPARED'
    INJECTING = 'INJECTING'
    AWAITING_CONFIRMATION = 'AWAITING_CONFIRMATION'
    CONFIRMED = 'CONFIRMED'
    FINALIZING_RESOURCES = 'FINALIZING_RESOURCES'
    VERIFYING = 'VERIFYING'
    PREPARING_SESSION = 'PREPARING_SESSION'
    COMPLETE = 'COMPLETE'
    ROLLING_BACK = 'ROLLING_BACK'
    ROLLED_BACK = 'ROLLED_BACK'
    RECOVERY_REQUIRED = 'RECOVERY_REQUIRED'


class InstallationRecoveryRequired(RuntimeError):
    code = "installation_recovery_required"

    def __init__(self, reason: str):
        super().__init__(player_text('launcher.setup', 'protection_stopped'))
        self.details = {"recovery_required": True, "recovery_error": reason}


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_path(path: Path) -> Path:
    for part in (path, *path.parents):
        junction = getattr(os.path, "isjunction", lambda _path: False)
        if part.is_symlink() or junction(part):
            raise InstallationRecoveryRequired('linked path: {value_0}'.format(value_0=f'{part}'))
    return path.resolve()


class InstallJournal:
    DIRECTORY = ".doomeap-installation"

    def __init__(self, root: Path, external_paths: tuple[Path, ...] = ()):
        self.root = checked_path(root)
        self.directory = checked_path(self.root / self.DIRECTORY)
        self.path = self.directory / "journal.json"
        self.external_paths = tuple(checked_path(path) for path in external_paths)
        self.document: dict = {}

    @property
    def phase(self) -> InstallPhase:
        return InstallPhase(self.document["phase"])

    @property
    def transaction_id(self) -> str:
        return self.document["transaction_id"]

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            self._check_document(document)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InstallationRecoveryRequired('invalid installation journal: {value_0}'.format(value_0=f'{error}')) from error
        self.document = document
        return True

    def _check_document(self, document: dict) -> None:
        self._check_header(document)
        for key in ("outcome", "injector_outcome", "resources_outcome"):
            if key in document["evidence"]:
                self._check_outcome(document["evidence"][key])
        if InstallPhase(document["phase"]) in {InstallPhase.COMPLETE, InstallPhase.ROLLED_BACK}:
            return
        if "injector_files" in document["evidence"]:
            raise ValueError('installation journal contains injector game-file backups')
        for entry in document["files"]:
            self._check_hashes(entry)
            self._allow_external_recovery(entry, document["transaction_id"])
            target = self._target(entry["path"])
            if entry["backup"] != str(self._local_directory(target, document["transaction_id"]) / entry["key"]):
                raise ValueError('installation backup path')

    @staticmethod
    def _check_outcome(value):
        adapter = value["adapter"]
        for text in (
            adapter["state"],
            adapter["message"],
            adapter["stdout"],
            adapter["stderr"],
            value["steam_option"],
            value["steam_diff"],
        ):
            if not isinstance(text, str):
                raise ValueError('installation result text')
        if not isinstance(adapter["command"], list | tuple) or not all(
            isinstance(arg, str) for arg in adapter["command"]
        ):
            raise ValueError('installation command')
        if adapter["returncode"] is not None and type(adapter["returncode"]) is not int:
            raise ValueError('installation exit code')
        InstallJournal._check_adapter_details(adapter["details"])

    @staticmethod
    def _check_adapter_details(details):
        if not isinstance(details, dict):
            raise ValueError('installation result details')
        for key in ("installation_mode", "injector_version", "post_install_verification"):
            if key in details and not isinstance(details[key], str):
                raise ValueError('installation result details')
        if "user_confirmed" in details and type(details["user_confirmed"]) is not bool:
            raise ValueError('installation confirmation')
        rows = details.get("required_resources", ())
        if not isinstance(rows, list | tuple) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('installation resource evidence')

    def _allow_external_recovery(self, entry, token):
        target = checked_path(Path(entry["path"]))
        if target.is_relative_to(self.root):
            return
        marker = checked_path(self._local_directory(target, token) / (entry["key"] + ".json"))
        record = json.loads(marker.read_text(encoding="utf-8"))
        expected = self._external_intent(entry, token)
        accepted = record.pop("accepted")
        expected.pop("accepted")
        if record != expected or not set(entry["accepted"]).issubset(accepted):
            raise ValueError('external installation intent differs')
        if target not in self.external_paths:
            self.external_paths += (target,)

    def _external_intent(self, entry, token):
        return {
            "transaction_id": token,
            "game_root": str(self.root),
            **{key: entry[key] for key in ("path", "before", "accepted", "mode")},
        }

    def _save_external_intent(self, entry):
        target = Path(entry["path"])
        if not target.is_relative_to(self.root):
            marker = checked_path(Path(entry["backup"]).with_suffix(".json"))
            _atomic_write_bytes(marker, json.dumps(self._external_intent(entry, self.transaction_id)).encode())

    def _check_header(self, document: dict) -> None:
        if document["schema"] != 1 or document["game_root"] != str(self.root):
            raise ValueError('installation journal identity')
        token = document["transaction_id"]
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError('installation transaction identity')
        InstallPhase(document["phase"])
        if not isinstance(document["revision"], int) or not isinstance(document["files"], list):
            raise ValueError('installation journal fields')
        if not isinstance(document["identity"], dict) or not isinstance(document["evidence"], dict):
            raise ValueError('installation journal identity fields')

    @staticmethod
    def _check_hashes(entry: dict) -> None:
        for digest in (entry["before"], *entry["accepted"]):
            if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError('installation file hash')
        if entry["key"] != hashlib.sha256(entry["path"].encode()).hexdigest():
            raise ValueError('installation file key')

    def _target(self, value: str) -> Path:
        path = checked_path(Path(value))
        if path not in self.external_paths and not path.is_relative_to(self.root):
            raise InstallationRecoveryRequired('unexpected installation target: {value_0}'.format(value_0=f'{path}'))
        if path.is_relative_to(self.directory):
            raise InstallationRecoveryRequired('journal cannot replace itself')
        return path

    def include_paths(self, paths: tuple[Path, ...]) -> None:
        self.external_paths += tuple(checked_path(path) for path in paths if path not in self.external_paths)

    def _local_directory(self, target: Path, token: str) -> Path:
        if target.is_relative_to(self.root):
            return self.directory / token
        return target.parent / f".doomeap-installation-{token}"

    def begin(self, identity: dict) -> None:
        self.document = {
            "schema": 1,
            "transaction_id": secrets.token_hex(16),
            "revision": 0,
            "game_root": str(self.root),
            "phase": InstallPhase.PREPARING.value,
            "created_at": time.time(),
            "identity": identity,
            "files": [],
            "evidence": {},
            "phase_times": {InstallPhase.PREPARING.value: time.time()},
        }
        self.save()

    def save(self) -> None:
        self.document["revision"] += 1
        self.document["updated_at"] = time.time()
        self.directory.mkdir(exist_ok=True)
        _atomic_write_bytes(self.path, (json.dumps(self.document, indent=2, sort_keys=True) + "\n").encode())

    def transition(self, phase: InstallPhase, **evidence) -> None:
        self.document["phase"] = phase.value
        self.document.setdefault("phase_times", {})[phase.value] = time.time()
        self.document["evidence"].update(evidence)
        self.save()

    def expect(self, path: Path, digest: str | None) -> dict:
        target = self._target(str(path))
        entry = next((item for item in self.document["files"] if item["path"] == str(target)), None)
        if entry is None:
            entry = self._capture(target)
            self.document["files"].append(entry)
        elif file_digest(target) not in entry["accepted"]:
            raise InstallationRecoveryRequired('externally changed: {value_0}'.format(value_0=f'{target}'))
        if digest not in entry["accepted"]:
            entry["accepted"].append(digest)
        self._save_external_intent(entry)
        self.save()
        return entry

    def _capture(self, target: Path) -> dict:
        key = hashlib.sha256(str(target).encode()).hexdigest()
        before = file_digest(target)
        backup = checked_path(self._local_directory(target, self.transaction_id) / key)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if before is not None:
            with target.open("rb") as source, backup.open("xb") as stream:
                backup.chmod(0o600)
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if file_digest(backup) != before:
                raise InstallationRecoveryRequired('backup differs: {value_0}'.format(value_0=f'{target}'))
        return {
            "path": str(target),
            "key": key,
            "backup": str(backup),
            "before": before,
            "accepted": [before],
            "published": False,
            "mode": stat.S_IMODE(target.stat().st_mode) if before is not None else None,
        }

    def replace(self, path: Path, content: bytes, *, operation: str) -> None:
        target = self._target(str(path))
        self.check_space(target, len(content))
        self.expect(target, hashlib.sha256(content).hexdigest())
        self._write(target, content, operation)
        self.record_publication(target)

    def replace_file(self, path: Path, source: Path, *, operation: str) -> None:
        target = self._target(str(path))
        self.check_space(target, source.stat().st_size)
        entry = self.expect(target, file_digest(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        incoming = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                incoming = Path(stream.name)
                with source.open("rb") as reader:
                    shutil.copyfileobj(reader, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if file_digest(incoming) != file_digest(source):
                raise InstallationRecoveryRequired('staged file changed: {value_0}'.format(value_0=f'{source}'))
            if entry['mode'] is not None:
                incoming.chmod(entry['mode'])
            publish_file(incoming, target, operation=operation)
            self.record_publication(target)
        finally:
            if incoming is not None and incoming.exists():
                incoming.chmod(stat.S_IWRITE | stat.S_IREAD)
                incoming.unlink()

    @staticmethod
    def check_space(target: Path, incoming_size: int) -> None:
        parent = next(path for path in target.parents if path.exists())
        before = target.stat().st_size if target.exists() else 0
        if shutil.disk_usage(parent).free < before + incoming_size:
            raise OSError(28, 'insufficient installation backup space', str(target))

    def _write(self, target: Path, content: bytes, operation: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        incoming = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=target.parent,
                prefix=f".{target.name}.{self.transaction_id}.",
                suffix=".incoming",
                delete=False,
            ) as stream:
                incoming = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            mode = next(entry["mode"] for entry in self.document["files"] if entry["path"] == str(target))
            if mode is not None:
                incoming.chmod(mode)
            publish_file(incoming, target, operation=operation)
        finally:
            if incoming is not None and incoming.exists():
                incoming.chmod(stat.S_IWRITE | stat.S_IREAD)
                incoming.unlink()

    def remove(self, path: Path) -> None:
        self.expect(path, None)
        path.unlink(missing_ok=True)
        self.record_publication(path)

    def record_publication(self, path: Path) -> None:
        entry = next(item for item in self.document["files"] if item["path"] == str(path.resolve()))
        if file_digest(path) not in entry["accepted"]:
            raise InstallationRecoveryRequired('publication differs: {value_0}'.format(value_0=f'{path}'))
        entry["published"] = True
        self.save()

    def rollback(self) -> None:
        self.transition(InstallPhase.ROLLING_BACK)
        try:
            self._check_recovery()
            for entry in reversed(self.document["files"]):
                target = self._target(entry["path"])
                if file_digest(target) == entry["before"]:
                    continue
                if entry["before"] is None:
                    target.unlink(missing_ok=True)
                else:
                    self.replace_file(target, Path(entry["backup"]), operation="installation_restore")
            self.transition(InstallPhase.ROLLED_BACK)
        except (OSError, InstallationRecoveryRequired) as error:
            self.transition(
                InstallPhase.RECOVERY_REQUIRED,
                recovery_error=getattr(error, "details", {}).get("recovery_error", str(error)),
                recovery_phase=InstallPhase.ROLLING_BACK.value,
            )
            raise

    def _check_recovery(self) -> None:
        for entry in self.document["files"]:
            target = self._target(entry["path"])
            if file_digest(target) not in entry["accepted"]:
                raise InstallationRecoveryRequired('externally changed: {value_0}'.format(value_0=f'{target}'))
            if file_digest(checked_path(Path(entry["backup"]))) != entry["before"]:
                raise InstallationRecoveryRequired('damaged backup: {value_0}'.format(value_0=f'{target}'))

    def cleanup(self) -> None:
        if self.document.get("cleanup_complete"):
            return
        for entry in self.document["files"]:
            backup = self._cleanup_backup(entry)
            if not Path(entry["path"]).is_relative_to(self.root):
                backup.with_suffix(".json").unlink(missing_ok=True)
            if backup.parent.exists() and not any(backup.parent.iterdir()):
                backup.parent.rmdir()
        self.document["cleanup_complete"] = True
        self.save()

    def _cleanup_backup(self, entry: dict) -> Path:
        self._check_hashes(entry)
        backup = checked_path(Path(entry["backup"]))
        if backup != self._local_directory(checked_path(Path(entry["path"])), self.transaction_id) / entry["key"]:
            raise InstallationRecoveryRequired('unexpected cleanup path')
        if entry.get("cleanup_complete"):
            return backup
        self._allow_external_recovery(entry, self.transaction_id)
        self._target(entry["path"])
        if backup.exists() and file_digest(backup) != entry["before"]:
            raise InstallationRecoveryRequired('installation backup changed before cleanup')
        backup.unlink(missing_ok=True)
        entry["cleanup_complete"] = True
        self.save()
        return backup

    def archive(self) -> None:
        _atomic_write_bytes(self.directory / f"{self.transaction_id}.json", self.path.read_bytes())

    def summary(self) -> dict:
        if not self.path.exists():
            return {"status": "absent"}
        try:
            if self.path.stat().st_size > 1024 * 1024:
                return {"status": "oversized"}
            document = json.loads(self.path.read_text(encoding="utf-8"))
            self._check_header(document)
            result = {
                key: document.get(key)
                for key in (
                    "schema",
                    "transaction_id",
                    "revision",
                    "phase",
                    "created_at",
                    "updated_at",
                    "phase_times",
                    "failure",
                )
            }
            result["evidence"] = {
                key: document["evidence"].get(key)
                for key in (
                    "tool",
                    "command",
                    "injector_pid",
                    "injector_finished",
                    "injector_state",
                    "outcome",
                    "recovery_error",
                    "cleanup_error",
                    "bank_cleanup",
                )
            }
            phase = InstallPhase(document["phase"])
            if phase is InstallPhase.INJECTING or document["evidence"].get("external_uncertain"):
                from .installation_adapters import injector_state

                result["evidence"]["injector_state"] = injector_state(document["evidence"]).value
            result["resources_verified"] = phase in {InstallPhase.PREPARING_SESSION, InstallPhase.COMPLETE}
            result["session_pending"] = phase is InstallPhase.PREPARING_SESSION
            return {"status": "available", **result}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"status": "unreadable", "error": str(error)}
