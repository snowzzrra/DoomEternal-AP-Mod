# Build a release

Run commands from the mod repository. Use matching MOD, APWorld and Sentinel Core
sources. Windows builds need the installed x64 MSVC tools, Windows SDK, CMake and
the project's Windows Python environment. Reuse its installed dependencies.
Map builds also need the verified retail inputs in `vanillamaps/` and
`../Tools/idFileDeCompressor`.

## Check the sources

```powershell
../.venv-windows/Scripts/python.exe -m tools.content.compile_content_catalog --source-only --check
../.venv-windows/Scripts/python.exe -m tools.content.compile_options_schema --check
../.venv-windows/Scripts/python.exe -m tools.validation.authorial --check-authorial
./scripts/build/windows_release.ps1 -Preflight -Python ../.venv-windows/Scripts/python.exe -CoreSource ../Sentinel-Core
```

The checks must finish without stale projections or invalid content. Run focused
tests for changed behavior, then the mod suite, the `doometernal` APWorld suite
and Core's CTest harnesses before delivering a candidate. See each repository's
development instructions for the installed test environment.

## Rebuild room resources

Choose unused output directories. Preserve the current bundle if it contains
uncommitted work. These commands prepare the authored mod tree, record the exact
input files and compile a fresh resource bundle:

```powershell
../.venv-windows/Scripts/python.exe -m tools.release.stage_room_resources --repo-root . --output-dir build/review-authorial --compressor ../Tools/idFileDeCompressor
../.venv-windows/Scripts/python.exe -m tools.release.source_provenance --repo-root . --archipelago-source ../Archipelago --output build/review-inputs.json
../.venv-windows/Scripts/python.exe -m tools.release.build_room_resources --repo-root . --staged-mod build/review-authorial/mod --work-dir build/review-work --compressor ../Tools/idFileDeCompressor --map-sources data/map_sources.json --output-dir build/review-resources --cache-root build/review-cache
../.venv-windows/Scripts/python.exe -m tools.release.source_provenance --repo-root . --archipelago-source ../Archipelago --output build/review-inputs.json --verify
```

`build/review-resources/` must contain `base_mod.zip`, `room_payloads.zip` and
`room_payload_manifest.json`. The final command checks that compiler inputs did
not change during the build. The file named `review-inputs.json` records those
inputs, hashes and ancestor commits, including local changes.

Validate the resources and compile representative rooms from packaged source
snapshots before selecting them for release. Then update the local canonical
bundle, replacing the commit placeholders with the source commits used:

```powershell
../.venv-windows/Scripts/python.exe -m tools.release.prebuilt_room_resources --publish-from build/review-resources --repo-root . --version 0.7.1 --mod-commit <mod-commit> --apworld-commit <apworld-commit> --source-state build/review-inputs.json
../.venv-windows/Scripts/python.exe -m tools.release.prebuilt_room_resources --check --repo-root .
```

Despite its `--publish-from` name, this command only updates local
`packaging/room_resources/v0.7.1/`. It writes checksums and
`ROOM_RESOURCES_PROVENANCE.json`, the record of which inputs built the bundle.

## Assemble Windows

```powershell
./scripts/build/windows_release.ps1 -Python ../.venv-windows/Scripts/python.exe -CoreSource ../Sentinel-Core
```

The script builds the native client, session helper, launcher and APWorld, runs
the headless launcher check and assembles `build/final-release/`. It checks the
canonical room bundle before packaging. `-SkipValidation` skips the broad suite;
use it only after recording an integrated test run for these sources.

Sentinel Core has its own distribution. Build and test it in its repository,
assemble with `tools/distribute.py`, and verify with `tools/verify_distribution.py`.
Use `--help` for required paths. A local dirty build must use `--dirty`; CI
qualification requires clean source. Include the complete Core distribution
for local candidates so testers can select its `distribution.json`.

## Linux and CI

`.github/workflows/cross-platform-build.yml` builds both launcher platforms,
the Windows native client used under Proton, and the APWorld. Select exact source
commits for MOD, APWorld and Core. `scripts/pipeline.sh release --build` runs the
local shell build.

To assemble existing build outputs, use:

```text
python scripts/release/assemble_ci_artifact.py --handoff <build-outputs> --room-resources-dir <resources> --platform windows --version v0.7.1 --repo-root . --output-dir <new-output>
```

Use `--platform linux` for Linux. Here `--handoff` means a directory containing
the launcher, shared native client, APWorld and their checksum manifest. Build
scripts create this layout. The assembler verifies executables, checksums,
resource membership and the final ZIP contents.

## Check the delivery before publishing

Extract the actual ZIP into a clean directory. Check its hashes, required package
data, bundled player guides and headless self-test. Compile the test rooms using
the packaged resources. Preserve their chosen presets and campaign settings;
regenerate seeds only if generation changes make it necessary.

Record the ZIP's actual path and SHA-256. State which offline checks passed and
which gameplay or Linux/Proton checks remain. Local assembly does not publish a
release. Uploading or publishing is a separate maintainer action after review.
