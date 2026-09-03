# DOOM Eternal Archipelago Release Process

This document describes the procedure for producing official cross-platform release packages for DOOM Eternal Archipelago.

---

## 1. Overview & Architecture

Release packages are generated through GitHub Actions:

1. **GitHub Actions Build & Release Assembly**: A single workflow run (`.github/workflows/cross-platform-build.yml`) validates the entire codebase, compiles platform binaries, bundles canonical room compiler resources, and deterministically packages both ready-to-test public archives:
   - `DoomEternalArchipelago-<version>-windows-x86_64.zip` (standalone Windows launcher, native `client/ap_client.exe`, precompiled canonical room resources, APWorld, manifests, content, data)
   - `DoomEternalArchipelago-<version>-linux-x86_64.zip` (standalone Linux launcher, native client, precompiled canonical room resources, APWorld, manifests, content, data)
   - `SHA256SUMS.txt`
   - Developer handoff bundle (`DoomEAP-crossplatform-build-<version>-<short-mod-sha>`) containing intermediate binaries and room resources for audit.

2. **Offline Local Assembly (Fallback / Reproducibility)**: Maintainers can also assemble or inspect release archives locally using `scripts/release/assemble_ci_artifact.py` against a downloaded handoff bundle and local canonical room resources (`build/release/client/resources/`).

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
   - `mod_ref`: Full commit SHA or branch for `DoomEternal-AP-Mod` (e.g., `main`).
   - `apworld_ref`: Full commit SHA or branch for `Archipelago` / `DoomEternal-AP-World` (e.g., `doom_eternal`).
   - `version_label`: Target version string (e.g., `v0.5.0`).
5. Run the workflow. It executes all validation gates (`fast`, `package-preflight`, `release --build`), generates canonical room compiler resources, builds the native client, standalone Linux and Windows launchers, and assembles the public release packages.

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
1. Create the GitHub Release on `DoomEternal-AP-Mod` for `v0.5.0`.
2. Upload `DoomEternalArchipelago-<version>-windows-x86_64.zip`, `DoomEternalArchipelago-<version>-linux-x86_64.zip`, and `SHA256SUMS.txt`.

---

## 3. Local Assembly Tool (Reproducibility & Offline Validation)

If assembling packages locally from a downloaded developer handoff artifact (`DoomEAP-crossplatform-build-*`):

```bash
python3 scripts/release/assemble_ci_artifact.py \
    --handoff /path/to/DoomEAP-crossplatform-build-<version>-<short-mod-sha> \
    --room-resources-dir build/release/client/resources \
    --version v0.5.0 \
    --output-dir build/final-release
```

The script validates:
- `BUILD-MANIFEST.json` and `SHA256SUMS.txt` integrity in the handoff.
- ELF and PE magic bytes for platform executables.
- Canonical room resources (`base_mod.zip`, `room_payloads.zip`, `room_payload_manifest.json`).
- Package parity across shared runtime assets (`doometernal.apworld`, `client/ap_client.exe`, schemas, templates, data).
- Single top-level `DoomEternalArchipelago/` root in both output archives.
