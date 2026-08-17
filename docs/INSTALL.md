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

1. validates room identity and options;
2. builds the room-specific mod package;
3. stages the package in DOOM Eternal's mod directory;
4. invokes the platform external tool;
5. reports installation state and any manual action required.

Do not start DOOM Eternal until installation reports success. Start DOOM Eternal
explicitly through Steam after setup.

### Windows

1. Approve verified EternalModManager 4.2.3 acquisition or provide an official
   verified local artifact.
2. Launcher stages the generated mod and opens EternalModManager.
3. In EternalModManager, select the generated DOOM Eternal Archipelago mod.
4. Press **Run Injector**.
5. Return to launcher and confirm whether installation succeeded.
6. Start DOOM Eternal through Steam.

EternalModManager performs the injection through its interactive window. The
launcher reports success after the workflow confirms completion.

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

When enabled, choose one room mode:

- **Soft:** one received death applies to its target; normal respawn/checkpoint
  flow continues. The event is dispatched once.
- **Hardcore:** the same received death remains pending until
  death is detected.

## Setup checks and support

When available in launcher, run **Setup check** for bounded checks covering
platform, game installation, processes, and launcher configuration. **Repair/Fix**
applies only explicit, supported repairs. **Support report** exports sanitized
diagnostics and bounded logs for troubleshooting.
Save contents are excluded and secrets are redacted. Review report contents
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
