# Installing DOOM Eternal Archipelago

This guide takes you from a fresh download to playing in an Archipelago room.
If something goes wrong, jump to [Common problems](#common-problems) at the end.

## What you need

- **DOOM Eternal on Steam.** If your room uses DLC missions, enemies or equipment,
  you also need **The Ancient Gods** (Part One and Two).
- **This mod's release ZIP**, the same version as the APWorld used to make the room.
  Extract the whole ZIP into its own folder and keep the files together.
- **Your room details**: server address (like `archipelago.gg:38281`), your player
  name, and the password if the room has one.
- **DOOM Eternal closed** whenever the launcher installs, updates or repairs something.

The launcher downloads the rest for you (the mod installer and Sentinel Core, the
part that talks to the game). It asks before downloading and checks every file.

> Using **Meathook**? Move its `XINPUT1_3.dll` out of the game folder (keep a copy)
> before setting up. The launcher won't touch files it doesn't recognize.

## Step 1 — Get a room

If someone already made the room, skip to [Step 2](#step-2--connect).

To make your player file:

1. Install the bundled `doometernal.apworld`: double-click it, or open it from the
   Archipelago Launcher, then restart the Archipelago Launcher.
2. Open the DOOM launcher (`DoomEternalArchipelagoLauncher.exe` on Windows,
   `DoomEternalArchipelagoLauncher` on Linux) and go to **Create player**.
3. Pick your options and choose **CREATE PLAYER YAML**.
4. Generate the room in Archipelago with that file, or send it to whoever hosts.

Your options are fixed when the room is generated. Changing the YAML later doesn't
change a room that already exists.

## Step 2 — Connect

1. In the launcher, go to **Play** and choose **JOIN ROOM**.
2. Check the game and save folders the launcher found. If it got them wrong, use
   **CHOOSE GAME FOLDER** and pick the folder that has `DOOMEternalx64vk.exe`.
3. Type the server, your player name and the password (if any), then **CONNECT**.

The Play screen shows three steps: **Game found**, **Room connected** and
**Ready to play**. If it asks for setup, choose **INSTALL ROOM MOD** (or
**UPDATE ROOM MOD** if you played this room before).

Each room gets its own mod, built for you. Don't copy a mod from another room, even
if the options look the same.

## Step 3 — Install the room mod

### On Windows

1. Approve the **EternalModInjector** download when the launcher asks.
2. A black command window opens. That's the mod installer. Answer what it asks,
   and **press a key when it says "Press any key"**. Don't close it with the X:
   the launcher only knows it finished when you press the key.
3. Back in the launcher, confirm the installation worked.
4. Wait for **SETUP READY**.

The launcher puts the room mod and Sentinel Core in the right places. You don't
copy any DLL by hand.

### On Linux (Steam + Proton)

Run the Linux launcher normally, outside Proton. The game runs inside Proton as usual.

1. In Steam, check which Proton DOOM Eternal uses: **DOOM Eternal → Properties →
   Compatibility**. If the launcher can't find it, go to **PROTON PROGRAM →
   BROWSE** and select the file named `proton` inside that Proton version.
   Proton Experimental usually lives in
   `~/.local/share/Steam/steamapps/common/Proton - Experimental/`;
   GE-Proton in `~/.local/share/Steam/compatibilitytools.d/`.
2. Approve the **EternalModInjectorShell** download. It runs in the background and
   the launcher shows the result.
3. Choose **COPY OPTION** next to **STEAM LAUNCH OPTION** and paste it into
   **DOOM Eternal → Properties → Launch Options**. It looks like:

   ```text
   WINEDLLOVERRIDES="msimg32=n,b" "/path/to/client/run_bridge.sh" %command%
   ```

4. Wait for **SETUP READY**.

## Step 4 — Play

1. Keep the launcher open.
2. Start DOOM Eternal from Steam with the normal **Play** button, not "Play DOOM
   Eternal with mods".
3. Turn off tutorials in the game settings.
4. Use the campaign the launcher prepared for this room.

Items you receive appear in the launcher's **Items** tab, and what happened in the
session is in **Activity**. For how items, options and goals work in the game, see
[Gameplay and options](GAMEPLAY.md).

When you're done, quit DOOM normally first, then close the launcher. Next time,
open the launcher, choose **RESUME**, and wait for the checks before starting the game.

Use one launcher per game profile. Only the first computer that spent something in
a room (like an Ammo Refill) can spend in that room; other computers still receive items.

### Your saves

Each room has its own DOOM save. **AP SAVE…** lets you look at it, back it up or
restore a backup made for the same room. Don't put a save from another room there.

If DOOM crashed or closed while saving, the launcher notices on the next start.
It keeps a copy of your files and recovers the last good save when it can prove
it's intact. If it can't, it stops and tells you what it found, so nothing is
overwritten.

## Windows manual installation

Use this if the automatic install keeps failing on Windows.

1. Download EternalModInjector from its
   [GameBanana page](https://gamebanana.com/tools/7475), the version the launcher asks for.
2. Extract it into the DOOM Eternal folder: `EternalModInjector.bat` and
   `EternalModManager.exe` go next to the game's `.exe`, the rest in `base/`.
   Don't put the injector ZIP in `Mods/`.
3. Let the launcher prepare the room mod (it goes into `Mods/`).
4. In `EternalModInjector Settings.txt`, set `:AUTO_LAUNCH_GAME=0`.
5. Run `EternalModInjector.bat`, answer it, and press a key when it asks.
6. In the launcher, choose **INSTALLATION FINISHED** and confirm.
7. Wait for **SETUP READY** and play.

## Common problems

**Can't connect to the room**
Check the server address, player name and password with the host. The player name
must be exactly the one used when the room was generated.

**The launcher can't find the game or the saves**
Use **CHOOSE GAME FOLDER** for the Steam install folder. On Linux, point the save
folder to the one inside the Proton prefix of DOOM Eternal.

**It asks to update the room mod**
Close DOOM and choose **UPDATE ROOM MOD**. If it keeps asking, make sure the host
generated the room with the same APWorld version as your release.

**The mod installer failed**
Close DOOM and choose **INSTALL AGAIN**. On Windows, if the download or the
installer keeps failing, use the [manual steps](#windows-manual-installation).

**It says the installer from the last attempt is still running, or it couldn't confirm it finished**
This happens if the launcher was closed during installation. Let the installer
window finish (press a key when it asks), then choose **INSTALL AGAIN**.

**The game starts but doesn't connect**
Keep the launcher open and connected to the room. On Linux, check the Steam launch
option you pasted. Then try **Help → CHECK SETUP**.

**Sentinel Core is missing or wrong**
Close DOOM and choose **REPAIR GAME CONNECTION** in Help.

**An item seems missing**
Load a level, choose **REPAIR INVENTORY**, then go back to the game and unpause.

**An item shows "Try again" / "Mark as received" in Items**
The launcher sent it but couldn't confirm the game applied it. Check in the game.
If you don't have it, choose **Try again**. If you do, choose **Mark as received**.
Trying again when you already have it may give you a second copy.

**The save needs attention**
Close DOOM, open **AP SAVE…** and follow what it says, or restore a backup of the
same room.

**Still stuck?**
Go to **Help → SAVE SUPPORT REPORT** and share the report with the developers. It
doesn't include your save files, and passwords are removed; you can read it
before sharing. **FIX SETUP** shows what it can repair before doing it.
