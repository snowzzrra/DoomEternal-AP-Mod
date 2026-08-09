import hashlib
import io
import json
import shlex
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from launcher_platform import (
    DependencyManager,
    DependencySpec,
    InstalledDependency,
    SteamInstallationLocator,
    SteamLaunchOptionsManager,
    WindowsModManagerAdapter,
    redact_secrets,
)


class DependencyManagerTests(unittest.TestCase):
    def test_local_verified_archive_installs_and_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            archive = temporary / "manager.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("bin/manager.exe", b"manager")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = DependencySpec(
                "manager", "1.0", "https://example.invalid/manager.zip", digest, "**/manager.exe", "zip"
            )
            manager = DependencyManager(temporary / "dependencies")

            installed = manager.acquire(spec, consent=lambda _: False, local_artifact=archive)
            reused = manager.acquire(spec, consent=lambda _: self.fail("reuse requested consent"))

            self.assertEqual(installed, reused)
            self.assertTrue(Path(installed.executable).is_file())
            receipt = json.loads((Path(installed.root) / manager.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(receipt["artifact_sha256"], digest)
            self.assertEqual(receipt["version"], "1.0")

    def test_hash_mismatch_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            archive = temporary / "manager.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("manager.exe", b"manager")
            spec = DependencySpec(
                "manager", "1.0", "https://example.invalid/manager.zip", "0" * 64, "manager.exe", "zip"
            )

            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                DependencyManager(temporary / "dependencies").acquire(
                    spec,
                    consent=lambda _: True,
                    local_artifact=archive,
                )
            self.assertFalse((temporary / "dependencies" / "manager-1.0").exists())

    def test_tar_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            archive = temporary / "injector.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo("../escape.sh")
                payload = b"bad"
                member.size = len(payload)
                output.addfile(member, io.BytesIO(payload))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            spec = DependencySpec(
                "injector", "1.0", "https://example.invalid/injector.tar.gz", digest, "**/*.sh", "tar.gz"
            )

            with self.assertRaisesRegex(ValueError, "unsafe archive member"):
                DependencyManager(temporary / "dependencies").acquire(
                    spec,
                    consent=lambda _: True,
                    local_artifact=archive,
                )


class AdapterTests(unittest.TestCase):
    def test_windows_manager_requires_verified_manual_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            executable = root / "EternalModManager.exe"
            executable.write_bytes(b"manager")
            mod_zip = root / "doom-ap.zip"
            with zipfile.ZipFile(mod_zip, "w") as output:
                output.writestr("EternalMod.json", "{}")
            opened: list[tuple[str, ...]] = []
            dependency = InstalledDependency("manager", "4.2.3", "a" * 64, "local", str(root), str(executable))

            result = WindowsModManagerAdapter(
                dependency, opener=lambda command: opened.append(tuple(command))
            ).activate(
                root / "game",
                mod_zip,
            )

            self.assertEqual(result.state, "manual_action_required")
            self.assertEqual(opened, [(str(executable), str(root / "game"))])
            self.assertTrue((root / "game" / "Mods" / mod_zip.name).is_file())


    


class SteamLaunchOptionTests(unittest.TestCase):
    def test_current_launch_option_is_read_from_vdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            localconfig = Path(temporary_name) / "localconfig.vdf"
            localconfig.write_text(
                '"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { '
                '"782330" { "LaunchOptions" "MANGOHUD=1 %command% --custom" } } } } } }',
                encoding="utf-8",
            )

            self.assertEqual(
                SteamLaunchOptionsManager.detect(localconfig),
                "MANGOHUD=1 %command% --custom",
            )

    def test_steam_installation_discovery_and_manual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            library = root / "library"
            steamapps = library / "steamapps"
            game = steamapps / "common/DOOMEternal"
            game.joinpath("base").mkdir(parents=True)
            game.joinpath("DOOMEternalx64vk.exe").write_bytes(b"game")
            steamapps.joinpath("appmanifest_782330.acf").write_text(
                '"AppState" { "appid" "782330" "installdir" "DOOMEternal" }',
                encoding="utf-8",
            )

            discovered = SteamInstallationLocator([library]).discover()
            manual = SteamInstallationLocator([]).discover(game)

            self.assertEqual(discovered[0].game_root, game.resolve())
            self.assertEqual(manual[0].game_root, game.resolve())

    def test_existing_arguments_and_override_are_preserved_without_duplication(self) -> None:
        current = 'MANGOHUD=1 WINEDLLOVERRIDES="xaudio2_7=n;XINPUT1_3=b" %command% -skipIntro +com_showFPS 1'
        plan = SteamLaunchOptionsManager.plan(current)

        self.assertEqual(plan.proposed.count("WINEDLLOVERRIDES="), 1)
        self.assertIn("xaudio2_7=n", plan.proposed)
        self.assertIn("XINPUT1_3=n,b", plan.proposed)
        self.assertIn("MANGOHUD=1", plan.proposed)
        self.assertTrue(plan.proposed.endswith("%command% -skipIntro +com_showFPS 1"))
        self.assertIn("--- current", plan.diff)


    def test_literal_required_launch_option_is_byte_identical(self) -> None:
        script = Path("/run/media/system/Eris/DoomEAP/DoomEternal-AP-Mod/build/release/client/run_bridge.sh")
        current = (
            'WINEDLLOVERRIDES="XINPUT1_3=n,b" AP_CLIENT_DELAY=5 '
            '"/run/media/system/Eris/DoomEAP/DoomEternal-AP-Mod/build/release/client/run_bridge.sh" %command%'
        )
        self.assertEqual(
            SteamLaunchOptionsManager.compose_bridge(current, script, delay=5),
            current,
        )

    def test_bridge_composition_preserves_arguments_and_quoting(self) -> None:
        script = Path("/tmp/client with spaces/run_bridge.sh")
        current = 'MANGOHUD=1 WINEDLLOVERRIDES="xaudio2_7=n" --before "two words" %command% --after custom'
        proposed = SteamLaunchOptionsManager.compose_bridge(current, script, delay=5)
        tokens = shlex.split(proposed)
        self.assertEqual(tokens.count("WINEDLLOVERRIDES=xaudio2_7=n;XINPUT1_3=n,b"), 1)
        self.assertEqual(tokens.count("AP_CLIENT_DELAY=5"), 1)
        self.assertEqual(tokens.count(str(script)), 1)
        self.assertEqual(tokens[tokens.index(str(script)) + 1 :], ["--before", "two words", "%command%", "--after", "custom"])
        self.assertEqual(tokens[2], "MANGOHUD=1")

    def test_backup_and_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            backup = Path(temporary_name) / "launch-option.json"
            plan = SteamLaunchOptionsManager.plan("gamemoderun %command% --custom")
            proposed = SteamLaunchOptionsManager.backup(plan, backup, consent=True)

            self.assertEqual(proposed, plan.proposed)
            self.assertEqual(SteamLaunchOptionsManager.restore(backup), plan.previous)

    def test_missing_command_fails_safe(self) -> None:
        with self.assertRaisesRegex(ValueError, "%command%"):
            SteamLaunchOptionsManager.compose("-skipIntro")


class RedactionTests(unittest.TestCase):
    def test_secrets_are_removed(self) -> None:
        redacted = redact_secrets(
            "connect password=hunter2 token=abc123 Authorization=BearerSecret",
            ["hunter2"],
        )
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("BearerSecret", redacted)


if __name__ == "__main__":
    unittest.main()
