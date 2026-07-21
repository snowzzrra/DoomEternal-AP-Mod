#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path

WINDOWS_SYSTEM_DLLS = {
    "advapi32.dll",
    "bcrypt.dll",
    "combase.dll",
    "comctl32.dll",
    "crypt32.dll",
    "gdi32.dll",
    "imm32.dll",
    "kernel32.dll",
    "mpr.dll",
    "msvcrt.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "rpcrt4.dll",
    "secur32.dll",
    "setupapi.dll",
    "shell32.dll",
    "shlwapi.dll",
    "user32.dll",
    "ucrtbase.dll",
    "urlmon.dll",
    "version.dll",
    "winhttp.dll",
    "wininet.dll",
    "winmm.dll",
    "ws2_32.dll",
}

MINGW_EXTERNAL_DLLS = {
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libwinpthread-1.dll",
}


def run_objdump(path: Path) -> str:
    result = subprocess.run(
        ["objdump", "-p", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def parse_imports(path: Path) -> list[str]:
    imports: list[str] = []
    for line in run_objdump(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("DLL Name:"):
            imports.append(stripped.split(":", 1)[1].strip())
    return imports


def sha256(path: Path) -> str:
    result = subprocess.run(
        ["sha256sum", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()[0]


def normalize(name: str) -> str:
    return name.lower()


def classify_dll(name: str) -> str:
    if normalize(name) in WINDOWS_SYSTEM_DLLS:
        return "system"
    return "external"


def audit_bundle(bundle_dir: Path, exe_name: str, forbidden: list[str]) -> dict:
    exe_path = bundle_dir / exe_name
    if not exe_path.is_file():
        raise FileNotFoundError(f"Executable not found: {exe_path}")

    local_dlls = {
        dll.name.lower(): dll
        for dll in sorted(bundle_dir.glob("*.dll"))
        if dll.is_file()
    }

    exe_imports = parse_imports(exe_path)
    collisions: list[str] = []
    unsatisfied: list[str] = []
    transitive: dict[str, dict] = {}

    for imported in exe_imports:
        imported_lower = normalize(imported)
        if imported_lower in local_dlls and classify_dll(imported) == "system":
            collisions.append(
                f"Local DLL collision: {local_dlls[imported_lower]} shadows system import {imported}"
            )

    for dll_name, dll_path in local_dlls.items():
        dll_imports = parse_imports(dll_path)
        external_imports = []
        for imported in dll_imports:
            imported_lower = normalize(imported)
            if classify_dll(imported) == "system":
                continue
            external_imports.append(imported)
            if imported_lower not in local_dlls:
                unsatisfied.append(
                    f"Unsatisfied external dependency: {dll_path.name} imports {imported}"
                )

        transitive[dll_path.name] = {
            "path": str(dll_path),
            "size": dll_path.stat().st_size,
            "sha256": sha256(dll_path),
            "imports": dll_imports,
            "external_imports": external_imports,
        }

    forbidden_present = [
        name for name in forbidden if (bundle_dir / name).exists()
    ]

    direct_runtime_imports = [
        imported
        for imported in exe_imports
        if normalize(imported) in MINGW_EXTERNAL_DLLS
    ]

    errors = []
    errors.extend(collisions)
    errors.extend(unsatisfied)
    for name in forbidden_present:
        errors.append(f"Forbidden bundled DLL present: {bundle_dir / name}")
    for imported in direct_runtime_imports:
        errors.append(
            f"Executable imports dynamic MinGW runtime directly: {imported}"
        )

    return {
        "bundle_dir": str(bundle_dir),
        "exe_path": str(exe_path),
        "exe_size": exe_path.stat().st_size,
        "exe_sha256": sha256(exe_path),
        "exe_direct_imports": exe_imports,
        "exe_direct_runtime_imports": direct_runtime_imports,
        "local_dlls": transitive,
        "collisions": collisions,
        "unsatisfied_external_dependencies": unsatisfied,
        "forbidden_present": forbidden_present,
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument("--exe-name", default="ap_client.exe")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--forbid-local",
        action="append",
        default=[],
        help="DLL filename that must not exist in the bundle dir",
    )
    args = parser.parse_args()

    report = audit_bundle(args.bundle_dir, args.exe_name, args.forbid_local)

    if args.json_output:
        args.json_output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Bundle: {report['bundle_dir']}")
    print(f"Executable: {report['exe_path']}")
    print("Direct imports:")
    for imported in report["exe_direct_imports"]:
        print(f"  - {imported} ({classify_dll(imported)})")

    if report["local_dlls"]:
        print("Local DLL audit:")
        for name, info in sorted(report["local_dlls"].items()):
            print(f"  - {name}:")
            print(f"      size: {info['size']}")
            print(f"      sha256: {info['sha256']}")
            if info["imports"]:
                print("      imports:")
                for imported in info["imports"]:
                    print(f"        - {imported} ({classify_dll(imported)})")
            else:
                print("      imports: []")
    else:
        print("Local DLL audit: no local DLLs found")

    if report["errors"]:
        print("Validation errors:")
        for error in report["errors"]:
            print(f"  - {error}")
        return 1

    print("Validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
