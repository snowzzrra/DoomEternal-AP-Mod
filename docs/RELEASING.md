# DOOM Eternal Archipelago Release Process

This document describes the procedure for producing official cross-platform release packages for DOOM Eternal Archipelago.

---

## 1. Overview & Architecture

Release artifact generation is split into two phases:

1. **GitHub Actions Build Handoff**: Compiles platform-native binaries (Linux standalone launcher on `ubuntu-latest`, Windows standalone launcher on `windows-latest`, and Windows-native `client/ap_client.exe` with MinGW/WIDL/Clang) and bundles `doometernal.apworld`.
   - Output: A single internal developer handoff artifact (`DoomEAP-crossplatform-build-<version>-<short-mod-sha>`).
   - GitHub Actions performs compilation only; it never creates tags or GitHub Releases.

2. **Local Release Assembly**: A local, zero-compilation assembly script (`scripts/release/assemble_ci_artifact.py`) consumes the downloaded build handoff artifact and packages repository-owned assets into exactly two public release archives:
   - `DoomEternalArchipelago-<version>-linux-x86_64.zip`
   - `DoomEternalArchipelago-<version>-windows-x86_64.zip`
   - `SHA256SUMS.txt`

---

## 2. Maintainer Release Workflow

### Step 1: Review & Commit
1. Verify source changes and run local fast validations.
2. Commit and push the repositories:
   - `DoomEternal-AP-Mod`
   - `DoomEternal-AP-World` (Archipelago world repository)
3. Record the exact resolved commit SHAs for both repositories:
   ```bash
   git -C path/to/DoomEternal-AP-Mod rev-parse HEAD
   git -C path/to/Archipelago rev-parse HEAD
   ```

### Step 2: Trigger GitHub Actions Build
1. Navigate to **Actions** in the `DoomEternal-AP-Mod` repository.
2. Select **DOOM Eternal AP Cross-Platform Build**.
3. Click **Run workflow** (`workflow_dispatch`).
4. Provide the exact inputs:
   - `mod_ref`: Full commit SHA or branch for `DoomEternal-AP-Mod`.
   - `apworld_ref`: Full commit SHA or branch for `Archipelago` / `DoomEternal-AP-World`.
   - `version_label`: Target version string (e.g., `v0.4.0-beta.4`).
5. Run the workflow and wait for all jobs (`resolve-metadata`, `build-apworld`, `build-native-support`, `build-linux-launcher`, `build-windows-launcher`, `consolidate-handoff`) to complete successfully.

### Step 3: Download Build Handoff Artifact
1. From the completed workflow run summary, download the single generated artifact:
   `DoomEAP-crossplatform-build-<version>-<short-mod-sha>.zip`

### Step 4: Assemble Public Release Archives Locally
1. Run the local no-compile assembly script against the downloaded zip:
   ```bash
   python3 scripts/release/assemble_ci_artifact.py \
       /path/to/DoomEAP-crossplatform-build-<version>-<short-mod-sha>.zip \
       --version <version> \
       --output-dir build/final-release
   ```
2. The script validates:
   - `BUILD-MANIFEST.json` and `SHA256SUMS.txt` integrity in the handoff.
   - ELF and PE magic bytes for platform executables.
   - Package parity across shared runtime assets (`doometernal.apworld`, `client/ap_client.exe`, schemas, templates, data).
   - Single top-level `DoomEternalArchipelago/` root in both output archives.

### Step 5: Publish Release
1. The resulting public artifacts in `build/final-release/` are:
   - `DoomEternalArchipelago-<version>-linux-x86_64.zip`
   - `DoomEternalArchipelago-<version>-windows-x86_64.zip`
   - `SHA256SUMS.txt`
2. Test the generated packages as needed.
3. Manually create the GitHub Release on `DoomEternal-AP-Mod` and upload the two platform ZIP files and `SHA256SUMS.txt`.
