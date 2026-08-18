"""Verified dependencies and platform launch adapters for beta launcher."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import webbrowser
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol

DOOM_ETERNAL_APP_ID = "782330"
REQUIRED_DLL_OVERRIDE = "XINPUT1_3=n,b"
STEAM_GAME_URL = f"steam://rungameid/{DOOM_ETERNAL_APP_ID}"


def create_secure_ssl_context(cafile: str | None = None) -> ssl.SSLContext:
    """Create a secure default SSL context using certifi CA bundle or supplied cafile.

    Certificate and hostname verification are strictly enforced (CERT_REQUIRED).
    """
    import certifi

    ca_bundle = cafile or certifi.where()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


@dataclass(frozen=True)
class LauncherUserPaths:
    """Per-user launcher roots for configuration, state, cache, and logs."""

    config_dir: Path
    state_dir: Path
    data_dir: Path


class DownloadTransport(Protocol):
    def fetch(self, url: str, destination: Path) -> None: ...


class UrlDownloadTransport:
    """Secure HTTPS download transport with validated SSL context and bounded timeout."""

    def __init__(
        self,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 60.0,
    ):
        self.ssl_context = ssl_context if ssl_context is not None else create_secure_ssl_context()
        self.timeout = timeout

    def fetch(self, url: str, destination: Path) -> None:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "DoomEternal-AP-Launcher"},
        )
        with urllib.request.urlopen(
            request,
            timeout=self.timeout,
            context=self.ssl_context,
        ) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def launcher_user_paths(
    *,
    environment: dict[str, str] | None = None,
    platform_name: str | None = None,
) -> LauncherUserPaths:
    env = os.environ if environment is None else environment
    platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
    if platform_name == "windows":
        root = Path(env.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        root = root / "Doom Eternal Archipelago"
        return LauncherUserPaths(root / "config", root / "state", root / "data")
    root_name = "doom-eternal-archipelago"
    return LauncherUserPaths(
        Path(env.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / root_name,
        Path(env.get("XDG_STATE_HOME") or (Path.home() / ".local/state")) / root_name,
        Path(env.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / root_name,
    )


def migrate_legacy_launcher_data(legacy_dir: Path, paths: LauncherUserPaths) -> None:
    """Copy legacy launcher data into user roots without deleting source files."""
    if not legacy_dir.is_dir():
        return
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(legacy_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(legacy_dir)
        destination_root = paths.config_dir if relative.name == "launcher.json" and len(relative.parts) == 1 else paths.state_dir
        destination = destination_root / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.name == "launcher.json" and len(relative.parts) == 1:
            try:
                document = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(document, dict):
                    document.pop("password", None)
                    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    continue
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        shutil.copy2(source, destination)


class DiscoveryStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


@dataclass(frozen=True)
class DiscoverySentinel:
    status: str
    path: str = ""
    candidates: tuple[str, ...] = ()
    reason: str = ""
    trace: tuple[dict[str, object], ...] = ()


def validate_game_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not (root / "DOOMEternalx64vk.exe").is_file() or not (root / "base").is_dir():
        raise ValueError(f"invalid DOOM Eternal installation: {root}")
    return root


def validate_save_directory(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"invalid DOOM Eternal save directory: {root}")
    return root


class PrerequisiteStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"
    NEEDS_USER_ACTION = "needs_user_action"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PrerequisiteCheck:
    key: str
    status: PrerequisiteStatus
    message: str
    details: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == PrerequisiteStatus.OK


@dataclass(frozen=True)
class RuntimePrerequisiteReport:
    checks: tuple[PrerequisiteCheck, ...]

    @property
    def ok(self) -> bool:
        """All mandatory beta.4 runtime prerequisites must be satisfied."""
        return all(
            check.ok
            for check in self.checks
            if check.key in {"game", "meathook", "client_runtime"}
        )

    def get(self, key: str) -> PrerequisiteCheck | None:
        for check in self.checks:
            if check.key == key:
                return check
        return None

    @property
    def meathook(self) -> PrerequisiteCheck | None:
        return self.get("meathook")

    @property
    def game(self) -> PrerequisiteCheck | None:
        return self.get("game")


def probe_meathook(game_root: Path | None) -> PrerequisiteCheck:
    """Probe the canonical Game Link / Meathook runtime library in DOOM Eternal root."""
    if game_root is None:
        return PrerequisiteCheck(
            key="meathook",
            status=PrerequisiteStatus.MISSING,
            message="DOOM Eternal folder is not configured.",
            details={"expected_path": "", "present": False, "status": "missing"},
        )
    try:
        root = game_root.expanduser().resolve()
        if root.name.casefold() == "base":
            root = root.parent
        dll_path = root / "XINPUT1_3.dll"
    except (OSError, ValueError) as error:
        return PrerequisiteCheck(
            key="meathook",
            status=PrerequisiteStatus.INVALID,
            message=f"Invalid DOOM Eternal folder path: {error}",
            details={"expected_path": "", "present": False, "status": "invalid"},
        )

    if not dll_path.is_file():
        return PrerequisiteCheck(
            key="meathook",
            status=PrerequisiteStatus.MISSING,
            message="Meathook runtime is not installed. Place XINPUT1_3.dll in the DOOM Eternal installation folder.",
            details={"expected_path": str(dll_path), "present": False, "status": "missing"},
        )

    try:
        size = dll_path.stat().st_size
    except OSError as error:
        return PrerequisiteCheck(
            key="meathook",
            status=PrerequisiteStatus.INVALID,
            message=f"Could not read XINPUT1_3.dll: {error}",
            details={"expected_path": str(dll_path), "present": True, "status": "invalid"},
        )

    if size <= 0:
        return PrerequisiteCheck(
            key="meathook",
            status=PrerequisiteStatus.INVALID,
            message="XINPUT1_3.dll is empty (0 bytes). Replace it with the official Meathook runtime library.",
            details={"expected_path": str(dll_path), "present": True, "size_bytes": 0, "status": "invalid"},
        )

    return PrerequisiteCheck(
        key="meathook",
        status=PrerequisiteStatus.OK,
        message="Meathook runtime found",
        details={
            "expected_path": str(dll_path),
            "present": True,
            "size_bytes": size,
            "status": "present_unverified",
        },
    )


def probe_runtime_prerequisites(
    game_root: Path | None,
    client_dir: Path | None = None,
    config: Mapping[str, object] | None = None,
) -> RuntimePrerequisiteReport:
    """Probe all mandatory and advisory beta.4 runtime prerequisites."""
    checks: list[PrerequisiteCheck] = []

    # 1. Game installation check
    if game_root is None:
        checks.append(PrerequisiteCheck(
            key="game",
            status=PrerequisiteStatus.MISSING,
            message="DOOM Eternal installation is not configured",
            details={},
        ))
    else:
        try:
            validated_root = validate_game_root(game_root)
            checks.append(PrerequisiteCheck(
                key="game",
                status=PrerequisiteStatus.OK,
                message="DOOM Eternal installation validated",
                details={"path": str(validated_root)},
            ))
        except ValueError as error:
            checks.append(PrerequisiteCheck(
                key="game",
                status=PrerequisiteStatus.INVALID,
                message=str(error),
                details={"path": str(game_root)},
            ))

    # 2. Meathook check
    checks.append(probe_meathook(game_root))

    # 3. Client runtime check
    if client_dir is not None:
        packaged_bridge = client_dir / "bridge_client.py"
        if packaged_bridge.is_file() or getattr(sys, "frozen", False):
            checks.append(PrerequisiteCheck(
                key="client_runtime",
                status=PrerequisiteStatus.OK,
                message="Bundled client runtime validated",
                details={"path": str(client_dir)},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key="client_runtime",
                status=PrerequisiteStatus.MISSING,
                message=f"Client runtime bridge is missing: {packaged_bridge}",
                details={"expected_path": str(packaged_bridge)},
            ))

    # 4. Linux Steam Launch Options override check
    if os.name != "nt" and config is not None:
        launch_opts = str(config.get("steam_launch_options") or "")
        if REQUIRED_DLL_OVERRIDE in launch_opts:
            checks.append(PrerequisiteCheck(
                key="linux_steam_override",
                status=PrerequisiteStatus.OK,
                message="Steam launch option configured with XINPUT1_3 override",
                details={"configured": True},
            ))
        else:
            checks.append(PrerequisiteCheck(
                key="linux_steam_override",
                status=PrerequisiteStatus.NEEDS_USER_ACTION,
                message=f'Set Steam launch option: WINEDLLOVERRIDES="{REQUIRED_DLL_OVERRIDE}" %command%',
                details={"required_override": REQUIRED_DLL_OVERRIDE, "configured": False},
            ))

    return RuntimePrerequisiteReport(tuple(checks))


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


LEGACY_DOOM_AP_MOD_NAMES = frozenset(
    {
        "ap_mod.zip",
        "doometernalarchipelagoalpha.zip",
        "doometernalarchipelagobeta.zip",
        "doometernalarchipelagoprealpha.zip",
    }
)
LEGACY_DOOM_AP_VERSIONED_MOD = re.compile(
    r"doometernalarchipelago-(?:[0-9a-f]{16}|v[0-9][0-9a-z._-]*)\.zip",
    re.IGNORECASE,
)


def is_legacy_doom_ap_mod(path: Path) -> bool:
    """Recognize only historical or room-bound package names owned by this project."""
    name = path.name
    return (
        name.casefold() in LEGACY_DOOM_AP_MOD_NAMES
        or LEGACY_DOOM_AP_VERSIONED_MOD.fullmatch(name) is not None
    )


def stage_room_mod(
    mod_zip: Path,
    game_root: Path,
    ownership_receipt: Path,
    *,
    manifest_hash: str,
    legacy_removal_sink: Callable[[Path], None] | None = None,
) -> Path:
    """Atomically replace only recognized DOOM Eternal Archipelago mod ZIPs."""
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
    for candidate in sorted(mods.glob("*.zip")):
        owned = False
        if previous_path is not None and candidate.resolve() == previous_path.resolve():
            owned = digest(candidate) == previous_sha
        recognized = is_legacy_doom_ap_mod(candidate)
        if not (recognized or owned):
            continue
        try:
            candidate.unlink()
        except OSError as error:
            raise RuntimeError(
                f"could not remove legacy DOOM Eternal Archipelago mod {candidate}: {error}"
            ) from error
        if legacy_removal_sink is not None:
            legacy_removal_sink(candidate)

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
    """Stage bundled InjectorShell tools and apply mods without launching Steam."""

    TIMEOUT_SECONDS = 15 * 60

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

    @staticmethod
    def _configure_first_run(game_root: Path) -> None:
        """Configure InjectorShell for explicit launcher-controlled startup."""
        config = game_root / "EternalModInjector Settings.txt"
        if config.is_file():
            return
        config.write_text(
            "\n".join(
                (
                    ":ASSET_VERSION=2026-04-03",
                    ":AUTO_LAUNCH_GAME=0",
                    ":GAME_PARAMETERS=",
                    ":HAS_CHECKED_RESOURCES=0",
                    ":HAS_READ_FIRST_TIME=1",
                    ":RESET_BACKUPS=0",
                    ":AUTO_UPDATE=0",
                    ":VERBOSE=0",
                    ":SLOW=0",
                    ":COMPRESS_TEXTURES=0",
                    ":DISABLE_MULTITHREADING=0",
                    ":ONLINE_SAFE=0",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def activate(self, game_root: Path, mod_zip: Path) -> AdapterResult:
        _stage_mod(mod_zip, game_root)
        executable = self._prepare_tools(game_root)
        self._configure_first_run(game_root)
        environment = os.environ.copy()
        environment.update({"skip": "1", "skip_debug_check": "1"})
        command = (str(executable),)
        try:
            completed = subprocess.run(
                command,
                cwd=game_root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=self.TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return AdapterResult(
                state="timed_out",
                message="Mod installation timed out. Review details and try again.",
                command=command,
                stdout=(error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else error.stdout or ""),
                stderr=(error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr or ""),
            )
        state = "applied" if completed.returncode == 0 else "failed"
        return AdapterResult(
            state=state,
            message=(
                "Mod installed successfully. Start DOOM Eternal through Steam."
                if state == "applied"
                else "Mod installation failed. Review details and try again."
            ),
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


@dataclass(frozen=True)
class SteamInstallation:
    library_root: Path
    game_root: Path
    manifest: Path


def query_windows_registry_steam_roots(
    registry_query: Callable[[], Sequence[Path]] | None = None,
) -> tuple[Path, ...]:
    """Discover installed Steam client roots from authoritative Windows registry locations."""
    if registry_query is not None:
        return tuple(dict.fromkeys(registry_query()))
    if os.name != "nt":
        return ()
    roots: list[Path] = []
    try:
        import winreg

        registry_values = (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        )
        for hive, key_name, value_name in registry_values:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                if value and isinstance(value, str):
                    path = Path(value.replace("/", "\\")).expanduser()
                    roots.append(path)
            except (FileNotFoundError, OSError):
                continue
    except ImportError:
        pass
    return tuple(dict.fromkeys(roots))


class SteamInstallationLocator:
    """Locate Steam App ID 782330 without starting Steam or game."""

    APP_MANIFEST = f"appmanifest_{DOOM_ETERNAL_APP_ID}.acf"

    def __init__(
        self,
        candidate_roots: Sequence[Path] | None = None,
        *,
        platform_name: str | None = None,
        registry_query: Callable[[], Sequence[Path]] | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.candidate_roots = tuple(
            self.default_roots(
                platform_name=platform_name,
                registry_query=registry_query,
                environment=environment,
            )
            if candidate_roots is None
            else candidate_roots
        )

    @staticmethod
    def default_roots(
        *,
        platform_name: str | None = None,
        registry_query: Callable[[], Sequence[Path]] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[Path, ...]:
        env = os.environ if environment is None else environment
        platform_name = platform_name or ("windows" if os.name == "nt" else "linux")
        roots: list[Path] = []
        if platform_name == "windows":
            roots.extend(query_windows_registry_steam_roots(registry_query))
            for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "PROGRAMW6432"):
                value = env.get(variable)
                if value:
                    roots.append(Path(value) / "Steam")
        else:
            home = Path.home()
            roots.extend(
                (
                    home / ".local/share/Steam",
                    home / ".steam/steam",
                    home / ".steam/root",
                    home / ".steam",
                )
            )
        return tuple(dict.fromkeys(roots))

    @staticmethod
    def _declared_libraries(steam_root: Path) -> tuple[Path, ...]:
        configuration = steam_root / "steamapps/libraryfolders.vdf"
        libraries = [steam_root]
        if configuration.is_file():
            text = configuration.read_text(encoding="utf-8", errors="replace")
            parsed_paths: list[Path] = []
            try:
                document = ValveVdfParser.parse(text)
                for top_key, top_val in document.items():
                    if isinstance(top_val, dict):
                        for item_key, item_val in top_val.items():
                            if isinstance(item_val, dict):
                                for prop_key, prop_val in item_val.items():
                                    if prop_key.casefold() == "path" and isinstance(prop_val, str):
                                        parsed_paths.append(Path(prop_val))
                            elif isinstance(item_val, str) and item_key.isdigit():
                                parsed_paths.append(Path(item_val))
            except Exception:
                pass

            for raw in re.findall(r'"path"\s+"((?:\\\\.|[^"\\])*)"', text, re.IGNORECASE):
                value = raw.replace("\\\\", "\\")
                parsed_paths.append(Path(value))
            for raw in re.findall(r'"\d+"\s+"((?:\\\\.|[^"\\])*)"', text):
                value = raw.replace("\\\\", "\\")
                parsed_paths.append(Path(value))

            for path in parsed_paths:
                if path not in libraries:
                    libraries.append(path)
        return tuple(dict.fromkeys(libraries))

    @staticmethod
    def _installation(library: Path) -> SteamInstallation | None:
        manifest = library / "steamapps" / SteamInstallationLocator.APP_MANIFEST
        if not manifest.is_file():
            return None
        text = manifest.read_text(encoding="utf-8", errors="replace")
        installdir: str | None = None
        try:
            document = ValveVdfParser.parse(text)
            for top_key, top_val in document.items():
                if isinstance(top_val, dict):
                    for prop_key, prop_val in top_val.items():
                        if prop_key.casefold() == "installdir" and isinstance(prop_val, str):
                            installdir = prop_val
                            break
        except Exception:
            pass
        if not installdir:
            match = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
            if match is not None:
                installdir = match.group(1)
        if not installdir:
            return None
        game_root = library / "steamapps/common" / installdir
        if not (game_root / "DOOMEternalx64vk.exe").is_file() or not (game_root / "base").is_dir():
            return None
        return SteamInstallation(library.resolve(), game_root.resolve(), manifest.resolve())

    def inspect_discovery(
        self, manual_game_root: Path | None = None
    ) -> tuple[tuple[SteamInstallation, ...], DiscoverySentinel]:
        installations: list[SteamInstallation] = []
        trace: list[dict[str, object]] = []
        any_root_exists = False
        any_manifest_exists = False
        any_installdir_found = False

        if manual_game_root is not None:
            game_root = manual_game_root.resolve()
            valid = (game_root / "DOOMEternalx64vk.exe").is_file() and (game_root / "base").is_dir()
            trace.append({
                "source": "manual",
                "path": str(game_root),
                "valid": valid,
            })
            if not valid:
                return (), DiscoverySentinel(
                    DiscoveryStatus.INVALID.value,
                    reason=f"invalid DOOM Eternal installation: {game_root}",
                    trace=tuple(trace),
                )
            installations.append(SteamInstallation(game_root.parent, game_root, Path()))

        for root in self.candidate_roots:
            root_exists = root.is_dir()
            root_info: dict[str, object] = {
                "root": str(root),
                "exists": root_exists,
            }
            if not root_exists:
                trace.append(root_info)
                continue

            any_root_exists = True
            vdf_path = root / "steamapps/libraryfolders.vdf"
            vdf_exists = vdf_path.is_file()
            declared = self._declared_libraries(root)
            root_info["vdf_path"] = str(vdf_path)
            root_info["vdf_exists"] = vdf_exists
            root_info["declared_library_count"] = len(declared)
            root_info["declared_libraries"] = [str(lib) for lib in declared]

            lib_checks: list[dict[str, object]] = []
            for library in declared:
                manifest_path = library / "steamapps" / self.APP_MANIFEST
                manifest_exists = manifest_path.is_file()
                check: dict[str, object] = {
                    "library": str(library),
                    "manifest_path": str(manifest_path),
                    "manifest_exists": manifest_exists,
                }
                if manifest_exists:
                    any_manifest_exists = True
                    installation = self._installation(library)
                    if installation is not None:
                        any_installdir_found = True
                        check["game_root"] = str(installation.game_root)
                        check["valid"] = True
                        if not any(item.game_root == installation.game_root for item in installations):
                            installations.append(installation)
                    else:
                        text = manifest_path.read_text(encoding="utf-8", errors="replace")
                        match = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
                        if match:
                            any_installdir_found = True
                            candidate_game = library / "steamapps/common" / match.group(1)
                            check["game_root"] = str(candidate_game)
                            check["valid"] = False
                            check["has_exe"] = (candidate_game / "DOOMEternalx64vk.exe").is_file()
                            check["has_base"] = (candidate_game / "base").is_dir()
                        else:
                            check["valid"] = False
                            check["error"] = "installdir missing from manifest"
                lib_checks.append(check)

            root_info["checks"] = lib_checks
            trace.append(root_info)

        inst_roots = tuple(str(item.game_root) for item in installations)
        if len(inst_roots) == 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.FOUND.value,
                path=inst_roots[0],
                reason="DOOM Eternal installation discovered",
                trace=tuple(trace),
            )
        elif len(inst_roots) > 1:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.AMBIGUOUS.value,
                candidates=inst_roots,
                reason=f"multiple DOOM Eternal installations found ({len(inst_roots)})",
                trace=tuple(trace),
            )
        elif not any_root_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason="Steam root directory not found",
                trace=tuple(trace),
            )
        elif not any_manifest_exists:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason=f"DOOM Eternal app manifest ({self.APP_MANIFEST}) not found in declared Steam libraries",
                trace=tuple(trace),
            )
        elif any_installdir_found and not installations:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.INVALID.value,
                reason="DOOM Eternal manifest found but game files (DOOMEternalx64vk.exe/base) are invalid",
                trace=tuple(trace),
            )
        else:
            sentinel = DiscoverySentinel(
                DiscoveryStatus.NOT_FOUND.value,
                reason="Steam installation was not found",
                trace=tuple(trace),
            )

        return tuple(installations), sentinel

    def discover(self, manual_game_root: Path | None = None) -> tuple[SteamInstallation, ...]:
        installations, _ = self.inspect_discovery(manual_game_root)
        return installations

    def discover_sentinel(self, manual_game_root: Path | None = None) -> DiscoverySentinel:
        _, sentinel = self.inspect_discovery(manual_game_root)
        return sentinel

    def diagnose(self, manual_game_root: Path | None = None) -> dict[str, object]:
        installations, sentinel = self.inspect_discovery(manual_game_root)
        return {
            "sentinel": asdict(sentinel),
            "installations": [
                {
                    "library_root": str(item.library_root),
                    "game_root": str(item.game_root),
                    "manifest": str(item.manifest),
                }
                for item in installations
            ],
        }


def detect_doom_processes(
    *,
    process_list: Sequence[Sequence[str]] | None = None,
) -> tuple[dict[str, object], ...]:
    """Return bounded process facts suitable for diagnostics."""
    rows: list[dict[str, object]] = []
    if process_list is None:
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["tasklist", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                process_list = tuple((row[0], row[1]) for row in csv.reader(completed.stdout.splitlines()) if row)
            except (OSError, subprocess.SubprocessError):
                process_list = ()
        else:
            try:
                completed = subprocess.run(
                    ["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=3, check=False
                )
                process_list = tuple(tuple(line.split(None, 1)) for line in completed.stdout.splitlines() if line.strip())
            except (OSError, subprocess.SubprocessError):
                process_list = ()
    for row in process_list:
        if not row:
            continue
        name = Path(str(row[-1])).name.casefold()
        if name not in {"doometernalx64vk.exe", "doometernalx64vk", "ap_client.exe", "ap_client"}:
            continue
        pid = str(row[0]) if len(row) > 1 else ""
        rows.append({"name": name, "pid": pid})
    return tuple(rows)


def launch_doom_via_steam(*, opener: Callable[[str], object] | None = None) -> str:
    """Open DOOM Eternal through its Steam game URL."""
    (opener or webbrowser.open)(STEAM_GAME_URL)
    return STEAM_GAME_URL


def read_handshake_probe(path: Path) -> dict[str, object]:
    """Read native handshake marker without opening, changing, or creating it."""
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()[:32]
    except (OSError, UnicodeError):
        return {"status": "unavailable", "path": str(path)}
    values: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            if re.fullmatch(r"[A-Za-z0-9_]{1,40}", key):
                values[key] = value[:200]
    state = values.get("state", "")
    if state not in {"menu", "gameplay"}:
        return {"status": "invalid", "path": str(path)}
    try:
        epoch = int(values.get("epoch", "-1"))
    except ValueError:
        return {"status": "invalid", "path": str(path)}
    result: dict[str, object] = {"status": "ok", "state": state, "epoch": epoch}
    if state == "gameplay":
        result.update({"slot": values.get("slot", ""), "map_name": values.get("map_name", "")})
    return result


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
                if not text[position:].strip():
                    break
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
        try:
            text = localconfig.read_text(encoding="utf-8", errors="strict")
            document = ValveVdfParser.parse(text)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f"Failed to parse Steam VDF {localconfig}: {error}") from error
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
