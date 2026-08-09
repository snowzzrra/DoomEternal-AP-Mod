"""Verified dependencies and platform launch adapters for beta launcher."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

DOOM_ETERNAL_APP_ID = "782330"
REQUIRED_DLL_OVERRIDE = "XINPUT1_3=n,b"


@dataclass(frozen=True)
class DependencySpec:
    name: str
    version: str
    url: str
    sha256: str
    executable_glob: str
    archive_type: str


WINDOWS_MOD_MANAGER = DependencySpec(
    name="EternalModManager",
    version="4.2.3",
    url=("https://github.com/brunoanc/EternalModManager/releases/download/v4.2.3/EternalModManager-4.2.3-win64.zip"),
    sha256="5701f30683b06a74fcbd9b56891f60fa5a80ca9019337141aa9908356f766b59",
    executable_glob="**/EternalModManager.exe",
    archive_type="zip",
)

LINUX_MOD_INJECTOR = DependencySpec(
    name="EternalModInjectorShell",
    version="6.66-rev3.12",
    url=("https://github.com/leveste/EternalBasher/releases/download/v6.66-rev3.12/EternalModInjectorShell.tar.gz"),
    sha256="f9f33f701244b9b274fcff4062cf7cfc33d233b33ec0872a38ae01225aee116c",
    executable_glob="**/EternalModInjectorShell.sh",
    archive_type="tar.gz",
)


class DownloadTransport(Protocol):
    def fetch(self, url: str, destination: Path) -> None: ...


class UrlDownloadTransport:
    def fetch(self, url: str, destination: Path) -> None:
        with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)


@dataclass(frozen=True)
class InstalledDependency:
    name: str
    version: str
    artifact_sha256: str
    source_url: str
    root: str
    executable: str


class DependencyManager:
    """Acquire pinned archives only after consent, then verify and atomically install."""

    RECEIPT = "dependency.json"

    def __init__(self, root: Path, transport: DownloadTransport | None = None):
        self.root = root
        self.transport = transport or UrlDownloadTransport()

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _safe_member(name: str) -> bool:
        path = PurePosixPath(name.replace("\\", "/"))
        return not path.is_absolute() and ".." not in path.parts

    @classmethod
    def _extract(cls, spec: DependencySpec, archive: Path, destination: Path) -> None:
        if spec.archive_type == "zip":
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    if not cls._safe_member(member.filename):
                        raise ValueError(f"unsafe archive path: {member.filename}")
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ValueError(f"archive symlink rejected: {member.filename}")
                source.extractall(destination)
        elif spec.archive_type == "tar.gz":
            with tarfile.open(archive, "r:gz") as source:
                members = source.getmembers()
                for member in members:
                    if (
                        not cls._safe_member(member.name)
                        or member.issym()
                        or member.islnk()
                        or not (member.isfile() or member.isdir())
                    ):
                        raise ValueError(f"unsafe archive member: {member.name}")
                source.extractall(destination, members=members)
        else:
            raise ValueError(f"unsupported archive type: {spec.archive_type}")

    def _installed(self, spec: DependencySpec, destination: Path) -> InstalledDependency | None:
        receipt_path = destination / self.RECEIPT
        if not receipt_path.is_file():
            return None
        receipt = InstalledDependency(**json.loads(receipt_path.read_text(encoding="utf-8")))
        executable = Path(receipt.executable)
        if (
            receipt.name == spec.name
            and receipt.version == spec.version
            and receipt.artifact_sha256 == spec.sha256
            and executable.is_file()
            and executable.is_relative_to(destination)
        ):
            return receipt
        return None

    def acquire(
        self,
        spec: DependencySpec,
        *,
        consent: Callable[[DependencySpec], bool],
        local_artifact: Path | None = None,
    ) -> InstalledDependency:
        destination = self.root / f"{spec.name}-{spec.version}"
        installed = self._installed(spec, destination)
        if installed is not None:
            return installed
        if local_artifact is None and not consent(spec):
            raise PermissionError(f"dependency acquisition declined: {spec.name}")

        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{spec.name}-", dir=self.root) as temporary_name:
            temporary = Path(temporary_name)
            archive = temporary / "artifact"
            if local_artifact is None:
                self.transport.fetch(spec.url, archive)
            else:
                if not local_artifact.is_file():
                    raise FileNotFoundError(local_artifact)
                shutil.copy2(local_artifact, archive)
            actual = self._sha256(archive)
            if actual != spec.sha256:
                raise ValueError(f"SHA-256 mismatch for {spec.name}: expected {spec.sha256}, got {actual}")

            extracted = temporary / "extracted"
            extracted.mkdir()
            self._extract(spec, archive, extracted)
            executables = sorted(extracted.glob(spec.executable_glob))
            if len(executables) != 1:
                raise ValueError(f"expected one {spec.executable_glob}, found {len(executables)}")
            executable_relative = executables[0].relative_to(extracted)
            executables[0].chmod(executables[0].stat().st_mode | 0o100)

            staged = self.root / f".{spec.name}-{spec.version}.incoming"
            shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(extracted, staged)
            receipt = InstalledDependency(
                name=spec.name,
                version=spec.version,
                artifact_sha256=actual,
                source_url=spec.url if local_artifact is None else str(local_artifact.resolve()),
                root=str(destination.resolve()),
                executable=str((destination / executable_relative).resolve()),
            )
            (staged / self.RECEIPT).write_text(
                json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            shutil.rmtree(destination, ignore_errors=True)
            os.replace(staged, destination)
            if not Path(receipt.executable).is_file():
                raise RuntimeError("dependency installation failed")
            return receipt


@dataclass(frozen=True)
class AdapterResult:
    state: str
    message: str
    command: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None


def _stage_mod(mod_zip: Path, game_root: Path) -> Path:
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError(f"mod is not a valid ZIP: {mod_zip}")
    mods = game_root / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    destination = mods / mod_zip.name
    incoming = destination.with_name(f".{destination.name}.incoming")
    shutil.copy2(mod_zip, incoming)
    os.replace(incoming, destination)
    return destination


def stage_room_mod(
    mod_zip: Path,
    game_root: Path,
    ownership_receipt: Path,
    *,
    trusted_template_hashes: set[str],
    manifest_hash: str,
) -> Path:
    """Atomically stage one room mod without deleting unverified ZIPs."""
    if not mod_zip.is_file() or not zipfile.is_zipfile(mod_zip):
        raise ValueError(f"mod is not a valid ZIP: {mod_zip}")
    mods = game_root / "Mods"
    mods.mkdir(parents=True, exist_ok=True)
    destination = mods / mod_zip.name
    previous: dict[str, object] = {}
    if ownership_receipt.is_file():
        try:
            loaded = json.loads(ownership_receipt.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            previous = {}

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    previous_path = Path(str(previous.get("staged_mod", ""))) if previous.get("staged_mod") else None
    previous_sha = str(previous.get("staged_sha256", ""))
    for candidate in sorted(mods.glob("DoomEternalArchipelago*.zip")):
        if candidate == destination:
            continue
        candidate_sha = digest(candidate)
        owned = (
            previous_path is not None
            and candidate.resolve() == previous_path.resolve()
            and candidate_sha == previous_sha
        )
        if owned or candidate_sha in trusted_template_hashes:
            candidate.unlink()
            continue
        raise RuntimeError(
            f"unverified older DOOM Eternal Archipelago mod remains in Mods: {candidate}. "
            "Remove it manually, then retry setup."
        )
    if destination.is_file():
        destination_sha = digest(destination)
        owned_destination = (
            previous_path is not None
            and destination.resolve() == previous_path.resolve()
            and destination_sha == previous_sha
        )
        if not owned_destination and destination_sha not in trusted_template_hashes:
            raise RuntimeError(
                f"refusing to overwrite unverified mod ZIP: {destination}. Remove it manually, then retry setup."
            )
    staged = _stage_mod(mod_zip, game_root)
    receipt = {
        "schema": 1,
        "manifest_hash": manifest_hash,
        "staged_mod": str(staged.resolve()),
        "staged_sha256": digest(staged),
    }
    ownership_receipt.parent.mkdir(parents=True, exist_ok=True)
    temporary = ownership_receipt.with_suffix(f"{ownership_receipt.suffix}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, ownership_receipt)
    return staged


class WindowsModManagerAdapter:
    """EternalModManager has no public CLI; stage mod and request smallest manual action."""

    def __init__(self, dependency: InstalledDependency, opener: Callable[[Sequence[str]], object] | None = None):
        self.dependency = dependency
        self.opener = opener or (lambda command: subprocess.Popen(command))

    def activate(self, game_root: Path, mod_zip: Path) -> AdapterResult:
        staged = _stage_mod(mod_zip, game_root)
        command = (self.dependency.executable, str(game_root))
        self.opener(command)
        return AdapterResult(
            state="manual_action_required",
            message=f"Select {staged.name}, then press Run Injector in EternalModManager.",
            command=command,
        )


class LinuxModManagerAdapter:
    """EternalModInjectorShell is interactive; prepare it, never fake unattended success."""

    def __init__(self, dependency: InstalledDependency):
        self.dependency = dependency

    def _prepare_tools(self, game_root: Path) -> Path:
        source_root = Path(self.dependency.root)
        executable = Path(self.dependency.executable)
        relative_executable = executable.relative_to(source_root)
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.name == DependencyManager.RECEIPT:
                continue
            relative = source.relative_to(source_root)
            destination = game_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                    raise RuntimeError(f"refusing to overwrite different injector tool: {destination}")
                continue
            shutil.copy2(source, destination)
        prepared = game_root / relative_executable
        prepared.chmod(prepared.stat().st_mode | 0o100)
        return prepared

    def prepare(self, game_root: Path, mod_zip: Path) -> AdapterResult:
        staged = _stage_mod(mod_zip, game_root)
        executable = self._prepare_tools(game_root)
        command = (str(executable),)
        return AdapterResult(
            state="manual_action_required",
            message=(
                f"Prepared {staged.name} and EternalModInjectorShell in the game directory. "
                "Run the interactive injector, keep AUTO_LAUNCH_GAME disabled, then start DOOM Eternal through Steam."
            ),
            command=command,
        )

    def activate_interactive(self, game_root: Path, mod_zip: Path) -> AdapterResult:
        _stage_mod(mod_zip, game_root)
        executable = self._prepare_tools(game_root)
        environment = os.environ.copy()
        environment.update({"skip": "1", "skip_debug_check": "1"})
        completed = subprocess.run(
            [executable],
            cwd=game_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        state = "applied" if completed.returncode == 0 else "failed"
        return AdapterResult(
            state=state,
            message="EternalModInjectorShell finished." if state == "applied" else "EternalModInjectorShell failed.",
            command=(str(executable),),
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


@dataclass(frozen=True)
class SteamInstallation:
    library_root: Path
    game_root: Path
    manifest: Path


class SteamInstallationLocator:
    """Locate Steam App ID 782330 without starting Steam or game."""

    APP_MANIFEST = f"appmanifest_{DOOM_ETERNAL_APP_ID}.acf"

    def __init__(self, candidate_roots: Sequence[Path] | None = None):
        self.candidate_roots = tuple(candidate_roots or self.default_roots())

    @staticmethod
    def default_roots() -> tuple[Path, ...]:
        home = Path.home()
        roots = [
            home / ".local/share/Steam",
            home / ".steam/steam",
        ]
        program_files = os.environ.get("PROGRAMFILES(X86)") or os.environ.get("PROGRAMFILES")
        if program_files:
            roots.append(Path(program_files) / "Steam")
        return tuple(dict.fromkeys(roots))

    @staticmethod
    def _declared_libraries(steam_root: Path) -> tuple[Path, ...]:
        configuration = steam_root / "steamapps/libraryfolders.vdf"
        libraries = [steam_root]
        if configuration.is_file():
            text = configuration.read_text(encoding="utf-8", errors="replace")
            for raw in re.findall(r'"path"\s+"((?:\\\\.|[^"\\])*)"', text):
                value = raw.replace("\\\\", "\\")
                libraries.append(Path(value))
        return tuple(dict.fromkeys(libraries))

    @staticmethod
    def _installation(library: Path) -> SteamInstallation | None:
        manifest = library / "steamapps" / SteamInstallationLocator.APP_MANIFEST
        if not manifest.is_file():
            return None
        text = manifest.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'"installdir"\s+"([^"]+)"', text)
        if match is None:
            return None
        game_root = library / "steamapps/common" / match.group(1)
        if not (game_root / "DOOMEternalx64vk.exe").is_file() or not (game_root / "base").is_dir():
            return None
        return SteamInstallation(library.resolve(), game_root.resolve(), manifest.resolve())

    def discover(self, manual_game_root: Path | None = None) -> tuple[SteamInstallation, ...]:
        installations: list[SteamInstallation] = []
        if manual_game_root is not None:
            game_root = manual_game_root.resolve()
            if not (game_root / "DOOMEternalx64vk.exe").is_file() or not (game_root / "base").is_dir():
                raise ValueError(f"invalid DOOM Eternal installation: {game_root}")
            installations.append(SteamInstallation(game_root.parent, game_root, Path()))
        for root in self.candidate_roots:
            for library in self._declared_libraries(root):
                installation = self._installation(library)
                if installation is not None and installation not in installations:
                    installations.append(installation)
        return tuple(installations)


@dataclass(frozen=True)
class LaunchOptionPlan:
    previous: str
    proposed: str
    diff: str


class ValveVdfParser:
    """Small KeyValues parser used only for reading Steam configuration."""

    TOKEN = re.compile(r'\s*(?://[^\n]*(?:\n|$)|("(?:\\.|[^"\\])*")|([{}])|([^\s{}"]+))')

    @classmethod
    def parse(cls, text: str) -> dict[str, object]:
        tokens: list[str] = []
        position = 0
        while position < len(text):
            match = cls.TOKEN.match(text, position)
            if match is None:
                raise ValueError(f"invalid VDF at offset {position}")
            position = match.end()
            quoted, brace, bare = match.groups()
            if quoted is not None:
                tokens.append(bytes(quoted[1:-1], "utf-8").decode("unicode_escape"))
            elif brace is not None:
                tokens.append(brace)
            elif bare is not None:
                tokens.append(bare)
        index = 0

        def mapping(expect_close: bool) -> dict[str, object]:
            nonlocal index
            result: dict[str, object] = {}
            while index < len(tokens):
                if tokens[index] == "}":
                    if not expect_close:
                        raise ValueError("unexpected VDF closing brace")
                    index += 1
                    return result
                key = tokens[index]
                index += 1
                if index >= len(tokens):
                    raise ValueError(f"missing VDF value for {key}")
                if tokens[index] == "{":
                    index += 1
                    result[key] = mapping(True)
                else:
                    result[key] = tokens[index]
                    index += 1
            if expect_close:
                raise ValueError("unterminated VDF mapping")
            return result

        document = mapping(False)
        if index != len(tokens):
            raise ValueError("trailing VDF tokens")
        return document


class SteamLaunchOptionsManager:
    """Detect and compose reversible options without writing Steam VDF files."""

    @staticmethod
    def _get(mapping: dict[str, object], key: str) -> object | None:
        return next((value for name, value in mapping.items() if name.casefold() == key.casefold()), None)

    @classmethod
    def detect(cls, localconfig: Path) -> str:
        document = ValveVdfParser.parse(localconfig.read_text(encoding="utf-8", errors="strict"))
        current: object = document
        for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", DOOM_ETERNAL_APP_ID):
            if not isinstance(current, dict):
                return "%command%"
            current = cls._get(current, key)
            if current is None:
                return "%command%"
        if not isinstance(current, dict):
            return "%command%"
        options = cls._get(current, "LaunchOptions")
        return str(options) if options else "%command%"

    ASSIGNMENT = re.compile(r"(?:^|\s)WINEDLLOVERRIDES=(?:\"([^\"]*)\"|'([^']*)'|(\S+))")

    @classmethod
    def compose(cls, current: str) -> str:
        if "%command%" not in current:
            raise ValueError("Steam launch options must contain %command%")
        match = cls.ASSIGNMENT.search(current)
        values: list[str] = []
        if match:
            raw = next(value for value in match.groups() if value is not None)
            values = [value for value in raw.split(";") if value]
            current = f"{current[: match.start()]} {current[match.end() :]}".strip()
        filtered = [value for value in values if value.split("=", 1)[0].casefold() != "xinput1_3"]
        filtered.append(REQUIRED_DLL_OVERRIDE)
        override = f'WINEDLLOVERRIDES="{";".join(filtered)}"'
        return f"{override} {current}".strip()


    @classmethod
    def compose_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> str:
        """Place Linux bridge wrapper before %command% while preserving command arguments."""
        if delay < 0:
            raise ValueError("AP client delay must be non-negative")
        try:
            tokens = shlex.split(current, posix=True)
        except ValueError as error:
            raise ValueError(f"Steam launch options have invalid quoting: {error}") from error
        if tokens.count("%command%") != 1:
            raise ValueError("Steam launch options must contain exactly one %command%")
        script = str(bridge_script)
        override_tokens = [token for token in tokens if token.startswith("WINEDLLOVERRIDES=")]
        delay_tokens = [token for token in tokens if token.startswith("AP_CLIENT_DELAY=")]
        script_tokens = [token for token in tokens if Path(token).name == "run_bridge.sh"]

        def override_values() -> list[str]:
            values: list[str] = []
            for token in override_tokens:
                values.extend(value for value in token.split("=", 1)[1].split(";") if value)
            return values

        existing_overrides = override_values()
        required_present = any(
            value.split("=", 1)[0].casefold() == "xinput1_3"
            and value.split("=", 1)[1].casefold() == "n,b"
            for value in existing_overrides
            if "=" in value
        )
        if (
            len(override_tokens) == 1
            and len(delay_tokens) == 1
            and delay_tokens[0] == f"AP_CLIENT_DELAY={delay}"
            and script_tokens == [script]
            and tokens.index(script) < tokens.index("%command%")
            and required_present
        ):
            return current

        filtered_overrides = [
            value
            for value in existing_overrides
            if value.split("=", 1)[0].casefold() != "xinput1_3"
        ]
        filtered_overrides.append(REQUIRED_DLL_OVERRIDE)
        remaining = [
            token
            for token in tokens
            if not token.startswith(("WINEDLLOVERRIDES=", "AP_CLIENT_DELAY="))
            and Path(token).name != "run_bridge.sh"
        ]
        command_index = remaining.index("%command%")
        before_command = remaining[:command_index]
        after_command = remaining[command_index:]
        leading_environment: list[str] = []
        while before_command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", before_command[0]):
            leading_environment.append(before_command.pop(0))
        rendered = [
            f'WINEDLLOVERRIDES="{";".join(filtered_overrides)}"',
            f"AP_CLIENT_DELAY={delay}",
            *(shlex.quote(token) for token in leading_environment),
            shlex.quote(script),
            *(shlex.quote(token) for token in before_command),
            *(shlex.quote(token) for token in after_command),
        ]
        return " ".join(rendered)

    @classmethod
    def plan_bridge(cls, current: str, bridge_script: Path, *, delay: int = 5) -> LaunchOptionPlan:
        proposed = cls.compose_bridge(current, bridge_script, delay=delay)
        diff = "\n".join(
            difflib.unified_diff(
                [current + "\n"],
                [proposed + "\n"],
                fromfile="current",
                tofile="proposed",
            )
        )
        return LaunchOptionPlan(current, proposed, diff)

    @classmethod
    def plan(cls, current: str) -> LaunchOptionPlan:
        proposed = cls.compose(current)
        diff = "\n".join(
            difflib.unified_diff(
                [current + "\n"],
                [proposed + "\n"],
                fromfile="current",
                tofile="proposed",
            )
        )
        return LaunchOptionPlan(current, proposed, diff)

    @staticmethod
    def backup(plan: LaunchOptionPlan, backup_path: Path, *, consent: bool) -> str:
        if not consent:
            raise PermissionError("Steam launch option change declined")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"app_id": DOOM_ETERNAL_APP_ID, "launch_options": plan.previous}
        temporary = backup_path.with_suffix(f"{backup_path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, backup_path)
        return plan.proposed

    @staticmethod
    def restore(backup_path: Path) -> str:
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
        if payload.get("app_id") != DOOM_ETERNAL_APP_ID:
            raise ValueError("Steam launch option backup belongs to another app")
        return str(payload["launch_options"])


def redact_secrets(text: str, secrets: Sequence[str] = ()) -> str:
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(password|passwd|token|secret|authorization)(\s*[=:]\s*)([^\s&]+)",
        r"\1\2[REDACTED]",
        text,
    )
    return text
