"""Packaged source text produces identical ZIP bytes across Git checkout settings."""

import shutil
import subprocess
from pathlib import Path

from tools.release.room_payloads import zip_directory

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_text_zip_is_independent_of_checkout_line_endings(tmp_path):
    sources = [
        ROOT / "packaging/shell_menu_assetsinfo.json",
        ROOT / "packaging/hub_world_text_assetsinfo.json",
        *sorted(
            path
            for path in (ROOT / "packaging/mod_assets").rglob("*")
            if path.suffix in {".json", ".decl", ".entities", ".txt", ".strings"} and path.is_file()
        ),
    ]
    repository = tmp_path / "repository"
    repository.mkdir()
    shutil.copy2(ROOT / ".gitattributes", repository / ".gitattributes")
    expected = {}
    for source in sources:
        relative = source.relative_to(ROOT)
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        expected[relative.as_posix()] = source.read_bytes().replace(b"\r\n", b"\n")
    git = ["git", "-C", str(repository), "-c", "core.safecrlf=false", "-c", "core.longpaths=true"]
    subprocess.run([*git, "init", "--quiet"], check=True)
    subprocess.run([*git, "-c", "core.autocrlf=true", "add", ".gitattributes", "packaging"], check=True)
    archives = []
    for autocrlf in ("false", "true"):
        checkout = tmp_path / autocrlf
        subprocess.run(
            [
                *git,
                "-c",
                f"core.autocrlf={autocrlf}",
                "-c",
                "core.eol=lf",
                "checkout-index",
                "--all",
                f"--prefix={checkout.as_posix()}/",
            ],
            check=True,
        )
        actual = {
            source.relative_to(ROOT).as_posix(): (checkout / source.relative_to(ROOT)).read_bytes()
            for source in sources
        }
        assert actual == expected
        archive = tmp_path / f"{autocrlf}.zip"
        zip_directory(checkout / "packaging", archive)
        archives.append(archive.read_bytes())
    assert archives[0] == archives[1]
