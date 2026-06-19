import asyncio
import os
import sys
import glob
import time
import re

import json

CONFIG_FILE = "ap_config.json"
config = {}

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

AP_PATH = config.get("archipelago_path", "")
if not AP_PATH or not os.path.exists(os.path.join(AP_PATH, "CommonClient.py")):
    print("\n=== DOOM Eternal AP Client Setup ===")
    print("Please enter the path to your Archipelago installation folder (where CommonClient.py is).")
    print("Example: /path/to/Archipelago")
    while True:
        AP_PATH = input("Archipelago Path: ").strip()
        if os.path.exists(os.path.join(AP_PATH, "CommonClient.py")):
            break
        print("Validation Error: Could not find CommonClient.py there.")
    config["archipelago_path"] = AP_PATH
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

sys.path.append(os.path.abspath(AP_PATH))

# IMPORTANT TO-DO: use the visual client for this instead of a simple console, as is default with archipelago clients
import Utils
import colorama
from CommonClient import CommonContext, server_loop, gui_enabled, ClientCommandProcessor, get_base_parser

if "doom_base_dir" in config and "save_games_dir" in config:
    DOOM_BASE_DIR = config["doom_base_dir"]
    SAVE_GAMES_DIR = config["save_games_dir"]
else:
    if "colorama" in sys.modules:
        print(colorama.Fore.CYAN + "\n=== DOOM Eternal AP Client Setup ===" + colorama.Style.RESET_ALL)
    else:
        print("\n=== DOOM Eternal AP Client Setup ===")
    print("The client needs to know where your game is installed and where it saves files (condump).")
    print("\n1. Game Base Directory (Where DOOMEternalx64vk.exe and ap_client.exe are located)")
    print("Windows Example: C:\\Program Files (x86)\\Steam\\steamapps\\common\\DOOMEternal\\base")
    print("Linux Example: /run/media/usuario/SteamLibrary/steamapps/common/DOOMEternal/base")
    while True:
        DOOM_BASE_DIR = input("Game Base Path: ").strip()
        if os.path.exists(os.path.join(DOOM_BASE_DIR, "classicwads")):
            break
        print(colorama.Fore.RED + "Validation Error: Could not find 'classicwads' folder here. This doesn't look like the DOOM Eternal base directory!" + colorama.Style.RESET_ALL)

    print("\n2. DOOM Saved Games Directory (Where the engine spits out the condump.txt files)")
    print("Windows Example: C:\\Users\\YourUser\\Saved Games\\id Software\\DOOMEternal\\base")
    print("Linux (Proton) Example: /path/to/steamapps/compatdata/782330/pfx/drive_c/users/steamuser/Saved Games/id Software/DOOMEternal/base")
    while True:
        SAVE_GAMES_DIR = input("Saved Games Path: ").strip()
        if not SAVE_GAMES_DIR:
            SAVE_GAMES_DIR = DOOM_BASE_DIR
            
        if "id Software" in SAVE_GAMES_DIR or os.path.exists(os.path.join(SAVE_GAMES_DIR, "id Software")):
            break
        print(colorama.Fore.RED + "Validation Error: The 'id Software' folder doesn't exist in this path. Are you sure this is the root directory for your Saves?" + colorama.Style.RESET_ALL)
        
    config["doom_base_dir"] = DOOM_BASE_DIR
    config["save_games_dir"] = SAVE_GAMES_DIR
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
    print(colorama.Fore.GREEN + "Configuration saved to ap_config.json!\n" + colorama.Style.RESET_ALL)

# makes sure the path is correct for save files
if "id Software" not in SAVE_GAMES_DIR:
    SAVE_GAMES_DIR = os.path.join(SAVE_GAMES_DIR, "id Software", "DOOMEternal", "base")

QUEUE_FILE = os.path.join(DOOM_BASE_DIR, "ap_queue.txt")
INV_DUMP_DIR = SAVE_GAMES_DIR

ITEM_ID_TO_COMMAND = {
    # Progression
    7770000: "give weapon/player/heavy_cannon",
    7770001: "give weapon/player/plasma_rifle",
    7770002: "give weapon/player/rocket_launcher",
    7770003: "give weapon/player/double_barrel",
    7770004: "give weapon/player/gauss_rifle",
    7770005: "give weapon/player/chaingun",
    7770006: "give weapon/player/bfg ; give ammo/sharedammopool/bfg 30",
    7770007: "give weapon/player/crucible ; give ammo/sharedammopool/crucible 3",
    7770008: "give weapon/player/unmaykr",
    7770009: "give weapon/player/hammer",
    7770010: "give weapon/player/chainsaw",
    7770011: "give throwable/player/frag_grenade",
    7770012: "give equipmentlauncher/equipmentlauncherleft ; give weapon/player/equipment_flame_belch",
    7770013: "give throwable/player/ice_bomb",
    7770014: "give abilities/blood_punch",
    7770015: "give ability_dash",
    7770016: "give inventory/battery", # Placeholder
    7770017: "give inventory/crystal", # Placeholder
    7770018: "give inventory/key", # Placeholder
    
    # Useful
    7770019: "give inventory/mastery_coin", # Placeholder
    7770020: "give inventory/rune", # Placeholder
    7770021: "give inventory/suit_point", # Placeholder
    7770022: "g_giveExtraLives 1",
    7770023: "g_giveExtraLives 3", # Extra life pack
    7770024: "give ammo",
    7770025: "give health",
    7770026: "give armor",
    7770027: "give ammo/sharedammopool/fuel 1",
    7770028: "give ammo/sharedammopool/bfg 1",
    7770029: "give health 200 ; give armor 200", # Soulsphere
    7770030: "chrispy pickup/powerup/berserk",
    
    # Filler. As of now, all of these work fine.
    7770031: "give health 25",
    7770032: "give health 100",
    7770033: "give armor 25",
    7770034: "give armor 100",
    7770035: "give ammo/sharedammopool/shells 10",
    7770036: "give ammo/sharedammopool/bullets 30",
    7770037: "give ammo/sharedammopool/cells 50",
    7770038: "give ammo/sharedammopool/rockets 3",
    7770039: "give ammo/sharedammopool/fuel 1",
    7770040: "give extra_life",
    7770041: "give armor 5",
    7770042: "give ammo",
    
    # Traps! These now work safely via the idTarget_Command delegation!
    7770043: "chrispy ai/fodder/imp",
    7770044: "chrispy ai/fodder/carcass",
    7770045: "chrispy ai/heavy/revenant",
    7770046: "chrispy ai/heavy/arachnotron",
    7770047: "chrispy ai/heavy/hellknight",
    7770048: "chrispy ai/heavy/dreadknight",
    7770049: "chrispy ai/superheavy/baron",
    7770050: "chrispy ai/superheavy/tyrant",
    7770051: "chrispy ai/superheavy/marauder",
    7770052: "chrispy ai/superheavy/archvile",
    7770053: "chrispy ai/ambient/zombie_cueball",
    7770054: "give ammo 0",
    7770055: "give ammo/sharedammopool/fuel 0",
    7770056: "give ammo/sharedammopool/bfg 0",
    7770057: "give armor 0",
    
    # Weapon Mods (Perks). they work! thanks zwip zwap zapony and alby for helping me figure out the correct commands for these <3
    7770058: "ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/shotgun/pop_rocket",
    7770059: "ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/shotgun/secondary_full_auto",
    7770060: "ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/heavy_cannon/bolt_action",
    7770061: "ai_ScriptCmdEnt player1 givePlayerPerk perk/player/weapons/heavy_cannon/burst_detonate",
}

# e1m1 only
DECL_TO_LOCATION = {
    "AP_CHECK_BARGE_PICKUP_WEAPON_CHAINSAW_1": 7770001,
    "AP_CHECK_CATHEDRAL_PICKUP_WEAPON_HEAVY_CANNON_1": 7770002,
    "AP_CHECK_SUBWAY_PICKUP_EQUIPMENT_FRAG_GRENADE_1": 7770003,
    "AP_CHECK_PICKUP_PICKUP_EXTRA_LIFE_EXTRA_LIFE_1_6_E1M1": 7770004,
    "AP_CHECK_UAC_HQ_PICKUP_EXTRA_LIFE_EXTRA_LIFE_1_1_E1M1": 7770005,
    "AP_CHECK_CORNER_STREET_PICKUP_EXTRA_LIFE_EXTRA_LIFE_1_1_E1M1": 7770006,
    "AP_CHECK_CORNER_STREET_PICKUP_EXTRA_LIFE_EXTRA_LIFE_1_7_E1M1": 7770007,
    "AP_CHECK_BARGE_PROGRESS_CODEX_1": 7770008,
    "AP_CHECK_MECH_STREET_PROGRESS_CODEX_1": 7770009,
    "AP_CHECK_CORNER_STREET_PROGRESS_CODEX_1": 7770010,
    "AP_CHECK_UAC_BASEMENT_PROGRESS_CODEX_1": 7770011,
    "AP_CHECK_CITADEL_PROGRESS_CODEX_2": 7770012,
    "AP_CHECK_CITADEL_PROGRESS_CODEX_3": 7770013,
    "AP_CHECK_BARGE_PROGRESS_MOD_BOT_1_E1M1": 7770014,
    "AP_CHECK_MECH_STREET_PROGRESS_MOD_BOT_1_E1M1": 7770015,
    "AP_CHECK_SUBWAY_PROGRESS_MOD_BOT_1_E1M1": 7770016,
    "AP_CHECK_BARGE_PICKUP_COLLECTIBLE_TOYS_ZOMBIE_1": 7770017,
    "AP_CHECK_MECH_STREET_PICKUP_COLLECTIBLE_TOYS_DOOMGUY_1": 7770018,
    "AP_CHECK_CORNER_STREET_PICKUP_COLLECTIBLE_TOYS_IMP_2": 7770019,
    "AP_CHECK_UAC_BASEMENT_PROGRESS_CHEATS_INFINITE_EXTRA_LIVES_2": 7770020,
}

poll_counter = 0

def send_command(cmd):
    # MeatHook's ExecuteConsoleCommand does NOT parse semicolons correctly.
    # It passes them as literal arguments. We must send pure commands.
    final_cmd = cmd
    
    try:
        with open(QUEUE_FILE, "w") as f:
            f.write(final_cmd)
    except Exception as e:
        print(f"[Error] Failed to write to ap_queue.txt: {e}")

def request_telemetry_dump():
    send_command("condump ap_condump.txt")

def read_telemetry_dump():
    search_pattern = os.path.join(INV_DUMP_DIR, "ap_condump*.txt")
    files = glob.glob(search_pattern)
    
    if not files:
        return []
        
    latest_file = max(files, key=os.path.getmtime)
    checks_found = set()
    
    try:
        with open(latest_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines:
                lower_line = line.lower()
                if "idbloatedentity::activate" in lower_line and "ap_check_" in lower_line:
                    match = re.search(r'(ap_check_[a-z0-9_]+)', lower_line)
                    if match:
                        checks_found.add(match.group(1).upper())
                        
        # erases old condump before reading it
        for file in files:
            try:
                os.remove(file)
            except:
                pass
                
        return list(checks_found)
    except Exception as e:
        print(f"[Error] Failed to process telemetry condump: {e}")
        return []

class DoomEternalContext(CommonContext):
    command_processor: type = ClientCommandProcessor
    game = "Doom Eternal"
    items_handling = 0b111

    def __init__(self, server_address, password):
        super().__init__(server_address, password)
        self.tracking_task = None
        self.items_processed = 0

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super(DoomEternalContext, self).server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    async def tracker_loop(self):
        print(colorama.Fore.GREEN + "[Tracking] Starting Doom Eternal telemetry loop (Polling every 4 seconds)..." + colorama.Style.RESET_ALL)
        
        # Disable console warnings in-game to prevent spam
        send_command("warning_disable all")
        
        while not self.exit_event.is_set():
            if self.server and self.server.socket and not self.server.socket.closed:
                # Batch all commands to be sent to the game in a single string
                commands_batch = []
                
                # Process any received items
                while len(self.items_received) > self.items_processed:
                    network_item = self.items_received[self.items_processed]
                    item_id = network_item.item
                    self.items_processed += 1
                    
                    if item_id in ITEM_ID_TO_COMMAND:
                        cmd = ITEM_ID_TO_COMMAND[item_id]
                        print(colorama.Fore.CYAN + f"[To Game] Item received! Delegating command to map entity ap_cmd_{item_id}: {cmd}" + colorama.Style.RESET_ALL)
                        # Delegate the execution to the map's idTarget_Command entity
                        commands_batch.append(f"ai_ScriptCmdEnt ap_cmd_{item_id} activate")
                        
                if commands_batch:
                    # Send each command individually, waiting for the C++ injector to clear the queue
                    for single_cmd in commands_batch:
                        send_command(single_cmd)
                        # Wait 1.0s to ensure the injector's 500ms loop picks it up and clears it
                        await asyncio.sleep(1.0)
                
                # Send the telemetry dump request as a completely separate execution
                send_command("condump ap_condump.txt")
                
                # Wait a bit for the game to process the entire batch and create the condump
                await asyncio.sleep(1.5)
                
                checks = read_telemetry_dump()
                if checks:
                    new_locs = []
                    for check_decl in checks:
                        if check_decl in DECL_TO_LOCATION:
                            loc_id = DECL_TO_LOCATION[check_decl]
                            if loc_id not in self.locations_checked:
                                print(colorama.Fore.GREEN + f"[Trigger] Telemetry detected AP Item Pickup: {check_decl} -> Sending Location {loc_id}" + colorama.Style.RESET_ALL)
                                self.locations_checked.add(loc_id)
                                new_locs.append(loc_id)
                                        
                    if new_locs:
                        await self.send_msgs([{"cmd": 'LocationChecks', "locations": new_locs}])

            await asyncio.sleep(4.0)

    def run_gui(self):
        pass

async def amain():
    Utils.init_logging("DoomEternalClient")
    parser = get_base_parser()
    parser.add_argument('--name', default=None, help="Player name no Archipelago")
    args = parser.parse_args()

    ctx = DoomEternalContext(args.connect, args.password)
    ctx.auth = args.name
    ctx.tracking_task = asyncio.create_task(ctx.tracker_loop())
    
    print(colorama.Fore.YELLOW + "=== DOOM ETERNAL ARCHIPELAGO CLIENT ===")
    if not args.connect or not args.name:
        print("Tip: You can skip these menus by running: python bridge_client.py --connect localhost:38281 --name YourName" + colorama.Style.RESET_ALL)
    else:
        print(f"Auto-connecting to {args.connect} as {args.name}..." + colorama.Style.RESET_ALL)
    
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

    import aioconsole
    async def console_loop():
        while not ctx.exit_event.is_set():
            input_msg = await aioconsole.ainput()
            if input_msg:
                ctx.command_processor(ctx)(input_msg)
            
    await asyncio.gather(ctx.server_task, ctx.tracking_task, console_loop())

if __name__ == '__main__':
    colorama.init()
    asyncio.run(amain())
