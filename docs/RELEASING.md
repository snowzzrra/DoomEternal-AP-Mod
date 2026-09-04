# DOOM Eternal Archipelago Release Process

This document describes the procedure for producing official cross-platform release packages for DOOM Eternal Archipelago.

---

## 1. Overview & Architecture

Release packages are generated through GitHub Actions:

1. **Frozen Canonical Room Resources**: Prebuilt room compiler resources (`base_mod.zip`, `room_payloads.zip`, `room_payload_manifest.json`) reside in `packaging/room_resources/v0.5.1/` along with `ROOM_RESOURCES_PROVENANCE.json` and `SHA256SUMS.txt`. These resources represent the boundary between local game-authorial compilation (requiring vanilla map and entity decl sources) and portable CI package assembly. GitHub Actions validates these resources against a deterministic content input fingerprint over all versioned gameplay/content sources before packaging them.

2. **GitHub Actions Build & Release Assembly**: A single workflow run (`.github/workflows/cross-platform-build.yml`) verifies authorial content and APWorld projection contracts, validates frozen room resources against the content input fingerprint, builds native components and platform launchers, and packages both ready-to-test public archives:
   - `DoomEternalArchipelago-<version>-windows-x86_64.zip` (standalone Windows launcher, native `client/ap_client.exe`, precompiled canonical room resources, APWorld, manifests, content, data)
   - `DoomEternalArchipelago-<version>-linux-x86_64.zip` (standalone Linux launcher, native client, precompiled canonical room resources, APWorld, manifests, content, data)
   - `SHA256SUMS.txt`
   - Developer handoff bundle (`DoomEAP-crossplatform-build-<version>-<short-mod-sha>`) containing intermediate binaries and room resources for audit.

3. **Offline Local Assembly (Fallback / Reproducibility)**: Maintainers can also assemble or inspect release archives locally using `scripts/release/assemble_ci_artifact.py` against a downloaded handoff bundle and local canonical room resources (`packaging/room_resources/v0.5.1/` or `build/release/client/resources/`).

---

## 2. Maintainer Release Workflow

### Step 1: Review & Commit
1. Verify source changes and run local fast validations.
2. If gameplay or room-affecting sources changed, refresh frozen room resources (see Section 4).
3. Commit and push the repositories:
   - `DoomEternal-AP-Mod`
   - `DoomEternal-AP-World` (Archipelago world repository)
4. Record the exact resolved commit SHAs for both repositories:
   ```bash
   git -C path/to/DoomEternal-AP-Mod rev-parse HEAD
   git -C path/to/Archipelago rev-parse HEAD
   ```

### Step 2: Trigger GitHub Actions Build
1. Navigate to **Actions** in the `DoomEternal-AP-Mod` repository.
2. Select **DOOM Eternal AP Cross-Platform Build**.
3. Click **Run workflow** (`workflow_dispatch`).
4. Provide the exact inputs:
   - `mod_ref`: Full commit SHA or branch for `DoomEternal-AP-Mod` (e.g., `main`).
   - `apworld_ref`: Full commit SHA or branch for `Archipelago` / `DoomEternal-AP-World` (e.g., `doom_eternal`).
   - `version_label`: Target version string (e.g., `v0.5.1`).
5. Run the workflow. It executes portable release preflight gates, validates frozen room compiler resources against the content input fingerprint, builds the native client and standalone platform launchers, and assembles the public release packages.

### Step 3: Download Public Release Artifacts
1. From the completed workflow run summary, download the generated public release artifact:
   `DoomEAP-release-<version>-<short-mod-sha>`
2. The downloaded archive contains:
   - `DoomEternalArchipelago-<version>-windows-x86_64.zip`
   - `DoomEternalArchipelago-<version>-linux-x86_64.zip`
   - `SHA256SUMS.txt`

### Step 4: Closed Testing & Distribution
1. Extract or send `DoomEternalArchipelago-<version>-windows-x86_64.zip` directly to Windows testers.
2. Testers run `DoomEternalArchipelagoLauncher.exe` directly; no local compilation or map processing is needed.

### Step 5: Publish Release
1. Create the GitHub Release on `DoomEternal-AP-Mod` for `v0.5.1`.
2. Upload `DoomEternalArchipelago-<version>-windows-x86_64.zip`, `DoomEternalArchipelago-<version>-linux-x86_64.zip`, and `SHA256SUMS.txt`.

---

## 3. Local Assembly Tool (Reproducibility & Offline Validation)

If assembling packages locally from a downloaded developer handoff artifact (`DoomEAP-crossplatform-build-*`):

```bash
python3 scripts/release/assemble_ci_artifact.py \
    --handoff /path/to/DoomEAP-crossplatform-build-<version>-<short-mod-sha> \
    --room-resources-dir build/release/client/resources \
    --version v0.5.1 \
    --output-dir build/final-release
```

The script validates:
- `BUILD-MANIFEST.json` and `SHA256SUMS.txt` integrity in the handoff.
- ELF and PE magic bytes for platform executables.
- Canonical room resources (`base_mod.zip`, `room_payloads.zip`, `room_payload_manifest.json`).
- Package parity across shared runtime assets (`doometernal.apworld`, `client/ap_client.exe`, schemas, templates, data).
- Single top-level `DoomEternalArchipelago/` root in both output archives.

---

## 4. Frozen Room Resources Refresh Procedure

When a modification touches versioned gameplay, maps, or content sources that alter room compiler output:

1. Run the local authorial release build with vanilla game sources present:
   ```bash
   ./scripts/pipeline.sh release --build
   ```
2. Copy the regenerated canonical room resources to the versioned packaging directory:
   ```bash
   cp build/release/client/resources/base_mod.zip packaging/room_resources/v0.5.1/
   cp build/release/client/resources/room_payloads.zip packaging/room_resources/v0.5.1/
   cp build/release/client/resources/room_payload_manifest.json packaging/room_resources/v0.5.1/
   ```
3. Update `ROOM_RESOURCES_PROVENANCE.json` and `SHA256SUMS.txt` using the updated content input fingerprint and file hashes.
4. Verify the frozen bundle:
   ```bash
   python3 -m tools.release.prebuilt_room_resources --check
   ```
5. Commit the refreshed resources alongside the content changes.

For changes confined to documentation, test suites, or CI workflows, the content input fingerprint remains identical and the frozen room resources require no regeneration.
