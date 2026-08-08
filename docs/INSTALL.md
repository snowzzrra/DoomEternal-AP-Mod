# Installation

DOOM Eternal Archipelago 0.4.0-beta.1 requires a legally obtained, player-supplied DOOM Eternal installation. Game
files and external modding tools are not distributed in project releases.

## Required files

- `doometernal.apworld`;
- `DoomEternalArchipelagoBeta.zip`;
- matching `client/` directory;
- [EternalModManager](https://github.com/brunoanc/EternalModManager) 4.2.3 on Windows;
- [EternalModInjectorShell](https://github.com/leveste/EternalBasher) 6.66-rev3.12 on Linux.

Launcher asks before acquiring pinned dependencies, verifies SHA-256, and can use a local official artifact instead.
Never run an artifact after verification failure.

## Install APWorld and connect

1. Open `doometernal.apworld` with Archipelago Launcher and restart launcher.
2. Select **DOOM Eternal Client**.
3. Point `doom_eternal_options.client_directory` to extracted matching `client/` directory.
4. Enter room address, slot name, and password when requested.
5. Wait for RoomSnapshot processing, manifest generation, compilation, and mod staging.

Launcher supervises native bridge. It never starts DOOM Eternal and exposes no game-launch button. Open DOOM Eternal
yourself through Steam whenever ready.

## Windows

1. Select game directory containing `DOOMEternalx64vk.exe`.
2. Consent to verified EternalModManager acquisition, or select verified local 4.2.3 ZIP.
3. Launcher places generated mod ZIP in `DOOMEternal/Mods` and opens manager.
4. Select generated mod and press **Run Injector**.
5. Wait for manager to finish, then open DOOM Eternal yourself through Steam.

EternalModManager 4.2.3 has no stable public command-line injector. Passing game directory opens GUI; passing mod ZIP
does not import or inject it. Launcher therefore reports `manual_action_required` and never claims application merely
because manager opened.

## Linux / Steam / Proton

Launcher and bridge remain native Linux processes. DOOM Eternal remains Steam-managed inside player's configured Proton
prefix. Do not start `DOOMEternalx64vk.exe` directly through Wine.

1. Select Steam library containing App ID `782330` if discovery is ambiguous.
2. Consent to verified EternalModInjectorShell acquisition, or select verified local 6.66-rev3.12 archive.
3. Launcher places generated mod ZIP in `DOOMEternal/Mods`.
4. Run EternalModInjectorShell from game directory and confirm successful exit.
5. Open DOOM Eternal yourself through Steam when ready.

EternalModInjectorShell is an interactive script without positional or unattended CLI. Launcher must not fabricate
flags. Interactive execution uses official script and reports its exit code, stdout, and stderr.

## Steam launch option

Meathook under Proton requires:

```text
WINEDLLOVERRIDES="XINPUT1_3=n,b" %command%
```

Launch-option preparation preserves existing custom arguments, merges existing `WINEDLLOVERRIDES`, keeps one
`%command%`, and preserves arguments after it. Review proposed diff before consent. Previous value is backed up and can
be restored. Current beta does not rewrite Steam VDF directly; copy proposed instruction into
**DOOM Eternal → Properties → Launch Options** while Steam is closed.

## Stop session

Close DOOM Eternal normally. Stop **DOOM Eternal Client** to terminate supervised bridge. Do not run two bridge clients
for same profile.

## Troubleshooting

- **Client files not found:** select extracted matching `client/`, not game directory.
- **Hash mismatch:** discard artifact; retry official pinned URL or select official local copy.
- **Manager says manual action required:** complete injector action in official manager.
- **Bridge cannot reach game:** verify mod injection, one bridge instance, and required Proton DLL override.
