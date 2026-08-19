# Install DOOM Eternal Archipelago beta.4

DOOM Eternal Archipelago **0.4.0-beta.4** requires a legally obtained,
player-supplied DOOM Eternal installation. Releases do not include game files
or external modding tools.

## Requirements

Game and runtime prerequisites:

- DOOM Eternal (Steam installation);
- [Meathook](https://github.com/brongo/m3337ho0o0ok) v7.2 (`XINPUT1_3.dll` in the DOOM Eternal installation root directory, acquired and verified automatically by the launcher);
- `doometernal.apworld`;
- `DoomEternalArchipelagoLauncher` on Linux or
  `DoomEternalArchipelagoLauncher.exe` on Windows;
- the bundled `client/` support runtime and verified mod templates.

Supported external mod injectors:

- **Windows:** [EternalModInjector](https://gamebanana.com/tools/7475) (Community mod injector toolchain);
- **Linux/Proton:**
  [EternalModInjectorShell](https://github.com/leveste/EternalBasher)
  6.66-rev3.12.

Launcher requests consent before acquiring pinned dependencies (Meathook v7.2 and the platform mod injector) and verifies
SHA-256 before installation. An official verified artifact may be supplied instead.

## Install APWorld

1. Open `doometernal.apworld` with Archipelago Launcher.
2. Restart Archipelago Launcher.
3. Keep the release launcher and its bundled client runtime together.

## Launcher flow

The launcher opens on **Home**. Use **Resume** to reconnect with saved room
identity, or open **Join** to enter a server, slot, and optional password.

### Join

1. Confirm or select the DOOM Eternal installation and save folder.
2. Enter room address, slot name, and optional password.
3. Select **Join** or **Connect to Archipelago**.
4. Wait for room data and package identity check.

YAML is not required for joining. Connected-room options are server-authoritative.

Join connects the prepared room and starts the bridge.

## Resume

Select **Resume** to return to launcher session state. Reconnect to the room if
requested; passwords are entered again. Launcher verifies room identity and
package state before setup.

## Session and Help

**Session** shows room, mod, game, and RPC status, room-authoritative options,
activity, logs, setup actions, and Steam launch. Keep the launcher open while
playing. Open **Help** for launcher guidance. Run **Setup check** for bounded
installation, bridge, and handshake diagnostics; use **Repair/Fix** for supported
repairs and **Support report** for sanitized troubleshooting data.

## Create YAML

Use **Create YAML** or the **Options** tab for a future room:

1. Set player name, starting inventory, and supported room options.
2. Select **Save Player YAML**.
3. Use saved YAML with Archipelago generation.

Create YAML writes player options for room generation. Active room options come
from the server.

## Prepare and Install

After room connection reports that setup is required, select the explicit
**Prepare and install** action. Launcher then:

1. acquires, verifies, and installs the verified Meathook v7.2 Game Link runtime;
2. validates room identity and options;
3. builds the room-specific mod package;
4. stages the package in DOOM Eternal's mod directory;
5. invokes the platform external tool;
6. reports installation state.

Do not start DOOM Eternal until installation reports success. Start DOOM Eternal
explicit through Steam after setup.

### Windows

1. Approve verified EternalModInjector acquisition or provide an official
   verified local artifact.
2. Launcher stages the generated mod into `Mods/`, stages the injector toolchain
   into the DOOM Eternal root folder, and opens `EternalModInjector.bat` in a
   visible command window.
3. Follow all prompts in the command window. Close the command window when
   installation completes.
4. In the launcher, confirm whether mod installation completed successfully.
5. If automatic tool acquisition is unavailable or declined, follow the
   [Windows Manual Mod Installer](#windows-manual-mod-installer) guide.
6. Start DOOM Eternal through Steam.

## Windows Manual Mod Installer

If automatic installation is unavailable, install the mod loader manually:

1. Close DOOM Eternal if it is running.
2. Open the EternalModInjector Windows page:
   [https://gamebanana.com/tools/7475](https://gamebanana.com/tools/7475)
3. Download the current Windows EternalModInjector archive (`eternalmodinjector_19e3b.zip`).
4. Extract the contents of the archive directly into your DOOM Eternal installation folder.
   The resulting files in your DOOM Eternal installation folder must include:
   - `EternalModInjector.bat`
   - `EternalModManager.exe`
   - `base/BlangParser.dll`
   - `base/DEternal_loadMods.exe`
   - `base/DEternal_patchManifest.exe`
   - `base/EternalPatcher.def`
   - `base/EternalPatcher.exe`
   - `base/EternalPatcher.exe.config`
   - `base/Newtonsoft.Json.dll`
   - `base/idRehash.exe`
   - `base/opusdec.exe`
   - `base/opusenc.exe`
   - `base/rs_data`
   - `base/zlib64.dll`
   - `Mods/`

   Do not place the EternalModInjector ZIP file inside `Mods/`. The archive contents merge directly into the DOOM Eternal root folder.
5. The Doom Eternal Archipelago launcher automatically places the current Archipelago room mod into `<DOOM root>/Mods/`.
6. Ensure `EternalModInjector Settings.txt` in the DOOM Eternal folder contains:
   ```text
   :AUTO_LAUNCH_GAME=0
   ```
   so Doom Eternal Archipelago manages game startup.
7. Run `EternalModInjector.bat`.
8. Follow all prompts in the command window. On the first run, the tool displays informational pages and prompts.
9. Allow the installer to finish and close the command window when prompted.
10. Return to the Doom Eternal Archipelago launcher.
11. Select **I completed manual installation** and confirm completion.
12. Start DOOM Eternal through Steam.

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
the option into DOOM Eternal's Steam **Properties → Launch Options**.

## Stop and resume safely

Close DOOM Eternal normally. Stop **DOOM Eternal Client** to terminate the
supervised bridge. Do not run two bridge clients for one profile.

## Feature limits

Beta.4 campaign contract: `28` Sentinel Battery checks and `13` Battery
consumers.

### Automap

Automap progression items use ordinary AP inventory behavior. Native Automap
presentation and marker cleanup remain part of map content.

**Reveal AP Locations on Automap** is a slot setting that controls starting
ownership of the AP progression-item reveal capability.

### Traps

- **Trap Percentage** replaces that percentage of filler padding with traps. It
  does not replace progression items or other pool items.
- **Enabled Traps** selects trap types eligible for those filler slots. If no
  trap types are enabled, no traps are placed.

### Fast Travel

Fast Travel means that whenever the player has completed a level, it will always have Fast Travel enabled in posterior playthroughs, as in Mission Select replays.

### DeathLink

When enabled, a received DeathLink applies a short two-hit lethal burst during safe gameplay. Native Extra Life and Saving Throw protections are preserved, and local death is echo-suppressed to prevent loops. Hardcore tracking is deferred to Sentinel Core.

## Setup checks and support

When available in launcher, run **Setup check** for bounded checks covering
platform, game installation, processes, and launcher configuration. **Repair/Fix**
applies only explicit, supported repairs. **Support report** exports sanitized
diagnostics and bounded logs for troubleshooting.
Save contents are excluded and secrets are redacted. Review report contents
before sharing.

## Troubleshooting

- **Game Link / Meathook missing or incompatible:** launcher automatically downloads and verifies the official Meathook v7.2 runtime library. For manual setup, download `XINPUT1_3.dll` from the official Meathook v7.2 release and place it in the DOOM Eternal root directory.
- **Client runtime not found:** keep bundled client files with release launcher.
- **Room package mismatch:** run explicit Prepare and install for current room;
  do not reuse another room's package.
- **Hash mismatch:** discard artifact and retry verified acquisition or provide a
  verified official artifact.
- **Windows manual mod installation:** follow the [Windows Manual Mod Installer](#windows-manual-mod-installer) section to extract `EternalModInjector` into the DOOM Eternal folder, set `:AUTO_LAUNCH_GAME=0` in `EternalModInjector Settings.txt`, run `EternalModInjector.bat`, and confirm installation in the launcher.
- **Linux injector failure:** review interactive tool output and exit status,
  then retry setup.
- **Bridge cannot reach game:** verify mod installation, one bridge instance,
  Meathook availability, and the Proton DLL override.
