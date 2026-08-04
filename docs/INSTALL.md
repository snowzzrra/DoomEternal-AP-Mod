# Installation

> [!IMPORTANT]
> The Archipelago client directory, the DOOM Eternal game directory, and the
> DOOM Eternal save directory are three different paths. Do not point all
> settings to the DOOM Eternal installation folder.

> [!WARNING]
> Extract every new release into a brand-new empty directory. Do not copy a new
> version over an older extracted release.

## Before you begin

You need three separate things:

- the extracted alpha release directory;
- the real DOOM Eternal game installation;
- the real DOOM Eternal save directory.

You will also install:

- `doometernal.apworld`;
- Meathook `v7.2`;
- `DoomEternalArchipelagoAlpha.zip`.

## The three paths are different

| Setting or file | What it must point to | Typical Windows example |
| --- | --- | --- |
| `doom_eternal_options.client_directory` | Extracted release `client/` folder containing `bridge_client.py`, `bridge_identity.json`, and `ap_client.exe` | `C:\Games\DoomEternalArchipelago-v0.3.8-alpha.1\client` |
| Game Base Path | DOOM Eternal `base/` folder containing `classicwads` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base` |
| Saved Games Path | DOOM Eternal save `base/` folder | `C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base` |
| `XINPUT1_3.dll` | Real game root, beside `DOOMEternalx64vk.exe` | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\XINPUT1_3.dll` |
| `DoomEternalArchipelagoAlpha.zip` | DOOM Eternal `Mods/` folder, still zipped | `C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\Mods\DoomEternalArchipelagoAlpha.zip` |

Examples:

```text
CLIENT DIRECTORY:
C:\Games\DoomEternalArchipelago-v0.3.8-alpha.1\client

GAME BASE PATH:
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base

SAVED GAMES PATH:
C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

These paths are not interchangeable.

## 1. Extract the release

Locate:

```text
DoomEternalArchipelagoPlayableTest-v0.3.8-alpha.1.zip
```

Extract it into a brand-new empty directory.

Do not:

- run files directly from inside the ZIP;
- extract the release over an older release directory;
- copy a new release on top of an older extracted release;
- extract `DoomEternalArchipelagoAlpha.zip`.

Expected layout:

```text
DoomEternalArchipelago-v0.3.8-alpha.1/
├── doometernal.apworld
├── DoomEternalArchipelagoAlpha.zip
├── README.md
├── RELEASE_MANIFEST.json
└── client/
    ├── bridge_client.py
    ├── bridge_identity.json
    ├── ap_client.exe
    ├── start_injector_windows.bat
    ├── run_bridge.sh
    └── ap_config.example.json
```

## 2. Install the APWorld

1. Open `doometernal.apworld` with `ArchipelagoLauncher`.
2. Close the launcher completely.
3. Start `ArchipelagoLauncher` again.
4. Confirm that `DOOM Eternal Client` appears in the launcher.

## 3. Set the Archipelago client directory

When you start `DOOM Eternal Client`, Archipelago needs the location of the
external alpha client files.

The correct path ends with:

```text
\client
```

The selected directory must contain:

- `bridge_client.py`;
- `bridge_identity.json`;
- `ap_client.exe`;
- `start_injector_windows.bat`.

Correct:

```text
C:\Games\DoomEternalArchipelago-v0.3.8-alpha.1\client
```

Incorrect:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal
C:\Games\DoomEternalArchipelago-v0.3.8-alpha.1
```

The first path is the game installation. The second is the parent release
directory. Neither is the extracted `client/` directory.

## 4. Configure the game and save paths

On first launch, `DOOM Eternal Client` asks for:

- Game Base Path;
- Saved Games Path.

Game Base Path must end with:

```text
DOOMEternal\base
```

That directory must contain `classicwads`. `DOOMEternalx64vk.exe` is one level
above it.

Valid Windows example:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\base
```

Saved Games Path example:

```text
C:\Users\YOUR_NAME\Saved Games\id Software\DOOMEternal\base
```

To open Windows Saved Games quickly:

1. Press `Win + R`.
2. Type `shell:SavedGames`.
3. Open `id Software`.
4. Open `DOOMEternal`.
5. Open `base`.

Setup writes:

```text
client/ap_config.json
```

Do not:

- reuse someone else's `ap_config.json`;
- copy a config from another computer;
- assume an old config remains valid after moving folders.

If `ap_client.exe` started before setup completed, close it and restart it
after `client/ap_config.json` exists.

The expected pre-setup warning is:

```text
Config not found yet. Run/setup the DOOM Eternal Client once, then restart ap_client.exe if needed.
```

Example `ap_config.json`:

```json
{
  "doom_base_dir": "C:\\Program Files (x86)\\Steam\\steamapps\\common\\DOOMEternal\\base",
  "save_games_dir": "C:\\Users\\YOUR_NAME\\Saved Games\\id Software\\DOOMEternal\\base"
}
```

## 5. Install Meathook

Use exactly:

https://github.com/brongo/m3337ho0o0ok/releases/tag/v7.2

On Windows, place `XINPUT1_3.dll` beside:

```text
DOOMEternalx64vk.exe
```

Correct Windows layout:

```text
DOOMEternal/
├── DOOMEternalx64vk.exe
├── XINPUT1_3.dll
├── base/
└── Mods/
```

Do not place `XINPUT1_3.dll` in:

- `DOOMEternal\base`;
- the extracted release `client/` directory on Windows.

## 6. Install the map mod

Windows injector:

https://gamebanana.com/tools/7475

Linux injector:

https://github.com/leveste/EternalBasher/releases/tag/v6.66-rev3.12

Copy:

```text
DoomEternalArchipelagoAlpha.zip
```

into:

```text
DOOMEternal/Mods/
```

Keep it zipped.

Do not:

- extract `DoomEternalArchipelagoAlpha.zip`;
- install loose `.entities` files;
- leave an older mod ZIP active without reinjecting after updates.

Run the injector again whenever you update the mod ZIP.

## 7. Start the clients on Windows

Startup order:

1. Open `Archipelago Launcher`.
2. Open `DOOM Eternal Client`.
3. Connect to the Archipelago server.
4. Start DOOM Eternal through Steam.
5. Run:

   ```text
   client\start_injector_windows.bat
   ```

6. Enter normal gameplay.
7. Wait for the memory gate to open.
8. Play.

Only one `ap_client.exe` should run at a time.

## 8. Start the clients on Linux / Proton

Set DOOM Eternal's Steam launch options to:

```text
WINEDLLOVERRIDES="XINPUT1_3=n,b" AP_CLIENT_DELAY=5 "/absolute/path/to/client/run_bridge.sh" %command%
```

Requirements:

- use an absolute path;
- do not use only `~/...`;
- place `XINPUT1_3.dll` beside `DOOMEternalx64vk.exe`.

Typical Linux/Proton paths:

```text
Game Base Path: /path/to/steamapps/common/DOOMEternal/base
Saved Games Path: /path/to/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base
```

Typical Bazzite-style examples:

```text
Game Base Path: /var/home/YOUR_NAME/.local/share/Steam/steamapps/common/DOOMEternal/base
Saved Games Path: /var/home/YOUR_NAME/.local/share/Steam/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base
```

## Correct directory layouts

Windows:

```text
C:\Games\DoomEternalArchipelago-v0.3.8-alpha.1\
├── doometernal.apworld
├── DoomEternalArchipelagoAlpha.zip
├── README.md
├── RELEASE_MANIFEST.json
└── client/
    ├── bridge_client.py
    ├── bridge_identity.json
    ├── ap_client.exe
    ├── start_injector_windows.bat
    ├── run_bridge.sh
    └── ap_config.example.json

C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal\
├── DOOMEternalx64vk.exe
├── XINPUT1_3.dll
├── base/
└── Mods/
    └── DoomEternalArchipelagoAlpha.zip
```

## Verify the installation

Native log:

```text
<DOOM Eternal>\base\ap_client.log
```

Expected lines:

```text
v0.3.8-alpha.1
Meathook RPC server verified.
RPC memory gate OPEN
```

`Memory state unavailable` can appear temporarily in menus, loading screens, or
transitions. It should not remain stuck during normal gameplay.

## Troubleshooting

### DOOM Eternal Client files not found

If the message says it searched in:

```text
C:\Program Files (x86)\Steam\steamapps\common\DOOMEternal
```

then the game directory was configured as the client directory.

Fix:

1. Open `Archipelago Launcher` and its Settings menu.
2. Find `doom_eternal_options.client_directory`.
3. Point it to the extracted release `client/` directory. Do not point it at a
   source checkout, Downloads, or `build/playable-test`.
4. Save the settings.
5. Close `Archipelago Launcher` completely.
6. Start it again.
7. Launch `DOOM Eternal Client`.

If the setting is not visible, use `Open host.yaml` in the launcher and set:

```yaml
doom_eternal_options:
  client_directory: "C:/Games/DoomEternalArchipelago-v0.3.8-alpha.1/client"
```

Forward slashes are valid in Windows YAML paths.

Do not use:

```yaml
doom_eternal_options:
  client_directory: "C:/Program Files (x86)/Steam/steamapps/common/DOOMEternal"
```

### Missing `libstdc++-6.dll` or `libgcc_s_seh-1.dll`

Do not download random DLLs.

These errors usually mean files from different releases were mixed or an old
extracted directory was reused.

Fix:

1. Delete the extracted alpha release directory.
2. Extract the current release ZIP again into a
   brand-new empty directory.

### The memory gate never opens

- Confirm the game is in normal campaign gameplay, not a menu or loading
  screen.
- Confirm Meathook `v7.2` is installed beside `DOOMEternalx64vk.exe`.
- Confirm only one `ap_client.exe` is running.
- Check `<DOOM Eternal>\base\ap_client.log`.
- Restart `ap_client.exe` after `ap_config.json` has been created.

### A location or scripted event appears stuck

Reload the current checkpoint first. Reaching another checkpoint, dying,
reloading, or returning to the main menu also forces another save-state scan.
Keep the Archipelago client visible to confirm whether the location was already
acknowledged by the server.
