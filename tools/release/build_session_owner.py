"""Bundle the Windows session owner for the selected Proton prefix."""
import argparse
from pathlib import Path
import subprocess
import sys

from doom_eap.contracts.core_distribution import verify_runtime


def build(output: Path, core_runtime: Path) -> Path:
    core_runtime = core_runtime.resolve()
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows owner on Windows")
    root = Path(__file__).resolve().parents[2]
    output = output.resolve()
    if not output.is_relative_to(root / "build/release"):
        raise ValueError("Session owner output must remain under build/release")
    verify_runtime(core_runtime / "distribution.json")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--console",
               "--hide-console", "hide-early", "--name", "APSessionOwner",
               "--distpath", str(output), "--workpath", str(output / "work"),
               "--specpath", str(output / "spec"), "--paths", str(root)]
    for name, source in (("core", core_runtime), ("data", root / "data"), ("manifests", root / "manifests"), ("content", root / "content")):
        command.extend(["--add-data", f"{source};{name}"])
    command.append(str(root / "doom_eap/launcher/session_owner_app.py"))
    subprocess.run(command, cwd=root, check=True)
    return output / "APSessionOwner.exe"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--core-runtime", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.output, args.core_runtime))
