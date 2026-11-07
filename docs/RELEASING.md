# Building a release

A release is three things built from matching sources: the mod package (launcher,
bridge, native client, APWorld), the frozen room resources it compiles rooms from,
and a Sentinel Core distribution. This page explains why it's split that way and
how to build each part. Run commands from the mod repository with the project's
Windows Python (`../.venv-windows/Scripts/python.exe`); reuse its installed packages.

You need the x64 MSVC tools, the Windows SDK and CMake for native code. Rebuilding
room resources also needs the verified retail maps in `vanillamaps/` and
`../Tools/idFileDeCompressor`.

## Why the build looks like this

**Rooms are compiled on the player's machine, not shipped prebuilt.** Every room
has its own items, checks and enemies, so the launcher builds a room mod when a
player connects. To do that without the developer's map folder, the release
carries *frozen room resources*: snapshots of the retail map sources plus the base
mod (`base_mod.zip`, `room_payloads.zip`, `room_payload_manifest.json`). The
compiler only reads those snapshots and checks their hashes before using them.

**Frozen means reproducible.** The bundle in `packaging/room_resources/<version>/`
records exactly which commits and files built it
(`ROOM_RESOURCES_PROVENANCE.json`) and carries a fingerprint. The APWorld and the
launcher both check it, so a room generated against one compiler is never built
with another. Whenever the compiler or the authored content changes, rebuild the
bundle; the `--check` below tells you when it's stale. Only the current version's
bundle lives in the tree; older ones are in the git history and in their releases.
The provenance records file hashes; it is not proof that something ran in the game.

**Sentinel Core ships on its own.** Core is the DLL inside the game. It has its own
version (2.x for this mod line), its own tests and its own distribution. The
launcher downloads it, checks its hashes, its ABI and that it declares support for
this launcher version, and refuses anything else. That lets us fix the mod without
touching Core, and the other way round, while never mixing incompatible pieces.

**Nothing is published from a local machine.** Building a ZIP locally is only a
candidate. Publishing is a separate step after the candidate passes review.

## 1. Check the sources

```powershell
../.venv-windows/Scripts/python.exe -m tools.content.compile_content_catalog --source-only --check
../.venv-windows/Scripts/python.exe -m tools.content.compile_options_schema --check
../.venv-windows/Scripts/python.exe -m tools.content.compile_player_strings --check
../.venv-windows/Scripts/python.exe -m tools.validation.authorial --check-authorial
./scripts/build/windows_release.ps1 -Preflight -Python ../.venv-windows/Scripts/python.exe -CoreSource ../Sentinel-Core
```

All of these must pass: no stale generated files, no invalid content. Then run the
mod test suite, the `doometernal` APWorld tests and Core's CTest. If a generated
file is stale, regenerate it (see [Generated files](DEVELOPMENT.md#generated-files))
and commit it with the change that caused it.

## 2. Rebuild the room resources

Use fresh output folders so a failed build can't leave half a bundle behind.

```powershell
../.venv-windows/Scripts/python.exe -m tools.release.stage_room_resources --repo-root . --output-dir build/review-authorial --compressor ../Tools/idFileDeCompressor
../.venv-windows/Scripts/python.exe -m tools.release.source_provenance --repo-root . --archipelago-source ../Archipelago --output build/review-inputs.json
../.venv-windows/Scripts/python.exe -m tools.release.build_room_resources --repo-root . --staged-mod build/review-authorial/mod --work-dir build/review-work --compressor ../Tools/idFileDeCompressor --map-sources data/map_sources.json --output-dir build/review-resources --cache-root build/review-cache
../.venv-windows/Scripts/python.exe -m tools.release.source_provenance --repo-root . --archipelago-source ../Archipelago --output build/review-inputs.json --verify
```

The first command prepares the authored mod tree, the second records every input
file and commit, the third compiles the bundle, and the last one confirms nothing
changed while it was building. `build/review-resources/` should now contain
`base_mod.zip`, `room_payloads.zip` and `room_payload_manifest.json`.

Compile a few representative rooms from these snapshots before accepting them.
Then copy the bundle into the repository, with the commits you built from:

```powershell
../.venv-windows/Scripts/python.exe -m tools.release.prebuilt_room_resources --publish-from build/review-resources --repo-root . --bundle-dir packaging/room_resources/<version> --version <version> --mod-commit <mod-commit> --apworld-commit <apworld-commit> --source-state build/review-inputs.json
../.venv-windows/Scripts/python.exe -m tools.release.prebuilt_room_resources --check --repo-root . --bundle-dir packaging/room_resources/<version> --version <version>
```

`--publish-from` only writes the local `packaging/room_resources/<version>/`
folder (checksums and provenance included); it doesn't upload anything. Point the
release at it with `ROOM_RESOURCES_VERSION` in `tools/release/prebuilt_room_resources.py`
and update the pinned hashes in `tests/fixtures/generation_cases.json`. The
resource version can stay behind the product version when the compiler didn't change.

## 3. Build Sentinel Core

In the `Sentinel-Core` repository: build, run CTest, then assemble the distribution
with `tools/distribute.py` and verify it with `tools/verify_distribution.py`
(`--help` lists the paths). A build from a working tree with uncommitted changes
needs `--dirty` and is only for local testing; CI qualification requires clean
sources. This mod line needs Core 2.0.x (protocol v2, automap ABI 2).

For a local test package, include the whole Core distribution folder so testers
can pick its `distribution.json` with **LOCAL BUILD…** in the launcher.

## 4. Assemble the Windows package

```powershell
./scripts/build/windows_release.ps1 -Python ../.venv-windows/Scripts/python.exe -CoreSource ../Sentinel-Core
```

This builds the native client, the session helper, the launcher and the APWorld,
runs a headless launcher check, checks the room bundle and writes
`build/final-release/`. `-SkipValidation` skips the broad test suite; only use it
when you already have a full test run recorded for exactly these sources.

`./scripts/build/windows_release.ps1 -RebuildRoomResources -StagedMod <staged mod>` does
the same rebuild in one go and first runs the full data audit against your retail files (`python -m tools.validation.validate_data --apworld-root
../Archipelago/worlds/doometernal`) before it builds the resources. That audit needs
`vanillamaps/`, `vanilla_decls/` and `../Tools/VanillaEntities/`, so it runs on your
machine, not in CI.

## 5. Linux and CI

There are two workflows. `.github/workflows/ci.yml` runs on every push (see
[Running the checks](DEVELOPMENT.md#running-the-checks)). The release workflow,
`.github/workflows/cross-platform-build.yml`, is started by hand with the commits you
select for the mod, the APWorld and Core (full SHAs). Its six jobs resolve those
sources, run the preflight (frozen resources, the full generation profile, the
APWorld's full suite), build the APWorld, build and qualify the Windows native client
(Linux players run it under Proton) with the native gates and no skips, build both
launchers with their self-test, and assemble the final ZIPs with their checksums.
`scripts/pipeline.sh release --build` does the same build locally on Linux.

In Sentinel Core, `windows.yml` builds and tests every push; qualifying and uploading a
Core distribution only happens when the workflow is started with an explicit version.

To assemble a package from existing build outputs:

```text
python scripts/release/assemble_ci_artifact.py --handoff <build-outputs> --room-resources-dir <resources> --platform windows --version v<version> --repo-root . --output-dir <new-output>
```

`--handoff` is a folder with the launcher, the native client, the APWorld and their
checksum list, as the build scripts produce it. Use `--platform linux` for Linux.
The assembler checks executables, checksums, the room resources and the final ZIP.

## 6. Check the package before publishing

Extract the real ZIP into an empty folder and test that copy, not your build folder:
hashes, packaged data, player guides, the headless self-test, and a few rooms
compiled from the packaged resources. Keep the test rooms' presets and campaign
settings; only regenerate seeds if generation changed.

Write down the ZIP path and SHA-256, which offline checks passed, and what still
needs testing in the game on Windows and on Linux/Proton. Then hand it over for
review and publishing.
