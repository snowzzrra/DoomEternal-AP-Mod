# Install DOOM Eternal Archipelago beta.4

DOOM Eternal Archipelago **0.4.0-beta.4** requires a legally obtained,
player-supplied DOOM Eternal installation. Releases do not include game files
or external modding tools.

## Requirements

Release package must contain:

- `doometernal.apworld`;
- `DoomEternalArchipelagoLauncher` on Linux or
  `DoomEternalArchipelagoLauncher.exe` on Windows;
- the bundled `client/` support runtime and verified mod templates.

Use one supported external installer:

- **Windows:** [EternalModManager](https://github.com/brunoanc/EternalModManager)
  4.2.3;
- **Linux/Proton:**
  [EternalModInjectorShell](https://github.com/leveste/EternalBasher)
  6.66-rev3.12.

Launcher requests consent before acquiring pinned dependencies and verifies
SHA-256. An official verified artifact may be supplied instead. Do not run an
artifact after verification failure.

## Install APWorld

1. Open `doometernal.apworld` with Archipelago Launcher.
2. Restart Archipelago Launcher.
3. Keep the release launcher and its bundled client runtime together.

## Join without YAML

1. Start the DOOM Eternal Archipelago launcher.
2. Confirm or select the DOOM Eternal installation and save folder.
3. Enter room address, slot name, and optional password.
4. Select **Join** or **Connect to Archipelago**.
5. Wait for room data and package identity check.

YAML is not required for joining. Connected-room options are server-authoritative.

Join only connects the bridge. It does not prepare a package, install or inject
the mod, or start DOOM Eternal.

## Resume

Select **Resume** to return to launcher session state. Reconnect to the room if
requested; passwords are entered again. Launcher verifies room identity and
package state before setup.

## Create YAML

Use **Create YAML** or the **Options** tab for a future room:

1. Set player name, starting inventory, and supported room options.
2. Select **Save Player YAML**.
3. Use saved YAML with Archipelago generation.

Create YAML does not connect to a room or alter an active room. It cannot enable
features blocked by current runtime capability checks.

## Prepare and Install

After room connection reports that setup is required, select the explicit
**Prepare and install** action. Launcher then:

1. validates room identity and options;
2. builds the room-specific mod package;
3. stages the package in DOOM Eternal's mod directory;
4. invokes the platform external tool;
5. reports installation state and any manual action required.

Do not start DOOM Eternal until installation reports success. Launcher never
starts the game directly; open it through Steam.

### Windows

1. Approve verified EternalModManager 4.2.3 acquisition or provide an official
   verified local artifact.
2. Launcher stages the generated mod and opens EternalModManager.
3. In EternalModManager, select the generated DOOM Eternal Archipelago mod.
4. Press **Run Injector**.
5. Return to launcher and confirm whether installation succeeded.
6. Start DOOM Eternal through Steam.

EternalModManager has no stable public command-line injector. Launcher cannot
claim installation merely because its window opened.

### Linux / Steam / Proton

Launcher and bridge run as native Linux processes. DOOM Eternal remains managed
by Steam inside the configured Proton prefix. Do not launch the Windows game
executable directly through Wine.

1. Approve verified EternalModInjectorShell 6.66-rev3.12 acquisition or provide
   an official verified local artifact.
2. Launcher stages the generated mod.
3. Launcher opens the interactive EternalModInjectorShell workflow.
4. Complete its prompts and confirm successful exit.
5. Start DOOM Eternal through Steam.


## Steam launch option

Meathook under Proton requires this Steam launch option:

```text
WINEDLLOVERRIDES="XINPUT1_3=n,b" %command%
```

Launcher preserves existing custom arguments and existing
`WINEDLLOVERRIDES`, keeps one `%command%`, and shows the proposed result. Copy
the option into DOOM Eternal's Steam **Properties → Launch Options**. Launcher
does not edit Steam settings directly.

## Stop and resume safely

Close DOOM Eternal normally. Stop **DOOM Eternal Client** to terminate the
supervised bridge. Do not run two bridge clients for one profile.

## Feature limits

### DeathLink

When enabled, choose one room mode:

- **Soft:** one received death dispatch; no repeat after the attempt.
- **Hardcore:** retry received death until confirmed or bounded timeout.

## Diagnostics and support

When available in launcher, run **Doctor** for bounded checks covering platform,
game installation, processes, and launcher configuration. **Create support
bundle** exports sanitized diagnostics and bounded logs for troubleshooting.
Save contents are excluded and secrets are redacted. Review bundle contents
before sharing.

## Troubleshooting

- **Client runtime not found:** keep bundled client files with release launcher.
- **Room package mismatch:** run explicit Prepare and install for current room;
  do not reuse another room's package.
- **Hash mismatch:** discard artifact and retry verified acquisition or provide a
  verified official artifact.
- **Windows manual action required:** finish **Run Injector** in
  EternalModManager, then confirm result in launcher.
- **Linux injector failure:** review interactive tool output and exit status,
  then retry setup.
- **Bridge cannot reach game:** verify mod installation, one bridge instance,
  Meathook availability, and the Proton DLL override.
