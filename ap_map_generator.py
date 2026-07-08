import os
import re
import sys
import json
import argparse

AP_PICKUP_HITBOX_SIZE = 6
RPC_ENTITY_PREFIX = "ap_rpc_v3"
LEGACY_RPC_ENTITY_PREFIXES = ("ap_rpc_v2_",)
NOTIFICATION_ENTITY_PREFIX = "ap_notify_"
EVENT_ENTITY_PREFIX = "ap_event_"
TARGETS_REPLACED_PICKUPS = {
    "pickup_equipment_flame_belch_1",
    "pickup_equipment_ice_bomb",
}

# you don't need to use this to play the mod
# i'm making this file available simply for transparency and for anyone who wants to generate the AP targets in other maps by themselves, since the process is a bit tedious to do manually and this automates it
# you could just do the changes by hand, but why??

def remove_balanced_entity_blocks(content, name_prefix):
    pattern = re.compile(r'entity\s*\{\s*(layers\s*\{\s*"[^"]+"\s*\}\s*)?entityDef\s+' + re.escape(name_prefix) + r'\w*\s*\{', re.IGNORECASE)
    result = []
    pos = 0
    for m in pattern.finditer(content):
        if m.start() < pos:
            continue
        result.append(content[pos:m.start()])
        depth = 2
        if "layers" in m.group(0):
            depth = 3

        i = m.end()
        while depth > 0 and i < len(content):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        pos = i
    result.append(content[pos:])
    return ''.join(result)

def remove_property_blocks(content, property_name):
    pattern = re.compile(r'\s*' + re.escape(property_name) + r'\s*=\s*\{', re.IGNORECASE)
    result = []
    pos = 0
    for match in pattern.finditer(content):
        if match.start() < pos:
            continue
        result.append(content[pos:match.start()])
        depth = 1
        i = match.end()
        while depth > 0 and i < len(content):
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        pos = i
    result.append(content[pos:])
    return ''.join(result)


def replace_targets_block(block, target_names):
    targets_lines = [
        "targets = {",
        f"\t\t\tnum = {len(target_names)};",
    ]
    targets_lines.extend(
        f'\t\t\titem[{idx}] = "{target_name}";'
        for idx, target_name in enumerate(target_names)
    )
    targets_lines.append("\t\t}")
    new_targets_block = "\n".join(targets_lines)

    match = re.search(
        r'targets\s*=\s*\{\s*num\s*=\s*\d+;\s*(.*?)\s*\}',
        block,
        re.DOTALL,
    )
    if match:
        return block.replace(match.group(0), new_targets_block, 1)

    return block.replace(
        "edit = {",
        "edit = {\n\t\t" + new_targets_block + "\n",
        1,
    )


def add_ap_check_target(block, entity_name, ap_check_id):
    target_names = []
    preserve_existing_targets = entity_name not in TARGETS_REPLACED_PICKUPS
    if preserve_existing_targets:
        match = re.search(
            r'targets\s*=\s*\{\s*num\s*=\s*\d+;\s*(.*?)\s*\}',
            block,
            re.DOTALL,
        )
        if match:
            target_names.extend(
                re.findall(r'item\[\d+\]\s*=\s*"([^"]+)";', match.group(1))
            )

    target_names.append(ap_check_id)
    return replace_targets_block(block, target_names)

def generate_target_relay(ap_check_id, location_id, spawn_pos_text):
    notification_name = f"{NOTIFICATION_ENTITY_PREFIX}{ap_check_id}"
    event_name = f"{EVENT_ENTITY_PREFIX}{location_id}"
    return f"""entity {{
	entityDef {ap_check_id} {{
		inherit = "target/relay";
		class = "idTarget_Count";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			count = 1;
			targets = {{
				num = 2;
				item[0] = "{notification_name}";
				item[1] = "{event_name}";
			}}
{spawn_pos_text}		}}
	}}
}}
"""


def generate_check_event(location_id):
    event_name = f"{EVENT_ENTITY_PREFIX}{location_id}"
    return f"""entity {{
	entityDef {event_name} {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "echo AP_CHECK_EVENT_{location_id}; condump {event_name}.txt";
		}}
	}}
}}
"""


def generate_pickup_notification(ap_check_id):
    notification_name = f"{NOTIFICATION_ENTITY_PREFIX}{ap_check_id}"
    return f"""entity {{
	entityDef {notification_name} {{
		class = "idTarget_Notification";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			flags = {{
				noFlood = true;
			}}
			notificationType = "HUD_NOTIFY_SECRET_FOUND";
			notificationHudEventID = "HUD_EVENT_PLAYER_NOTIFICATION_SECRET_FOUND";
			doNotShowDuplicate = false;
			rootWidget = "tier3centered";
			icon = "art/ui/dossier/icons/ico_secrets_off";
			header = "#str_swf_notification_secret_found";
			notificationSound = "play_secret_encounter_found";
		}}
	}}
}}
"""

def generate_rpc_command_entities(items_dict):
    blocks = []
    for item_id, command_value in items_dict.items():
        if isinstance(command_value, dict):
            command_type = command_value.get("type")
            if command_type == "no_op":
                continue
            if command_type == "progressive_perk":
                perks = command_value.get("perks", [])
                if not perks:
                    raise ValueError(
                        f"Progressive perk item {item_id} has no perk stages"
                    )
                for stage, perk in enumerate(perks):
                    entity_name = f"{RPC_ENTITY_PREFIX}_{item_id}_{stage}"
                    blocks.append(f"""entity {{
	entityDef {entity_name} {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "ai_ScriptCmdEnt player1 givePlayerPerk {perk};ai_ScriptCmdEnt player1 activatePlayerPerk {perk}";
		}}
	}}
}}
""")
                continue

            if command_type == "perk":
                perk = command_value.get("perk")
                if not perk:
                    raise ValueError(f"Perk item {item_id} has no perk path")
                blocks.append(f"""entity {{
	entityDef {RPC_ENTITY_PREFIX}_{item_id} {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "ai_ScriptCmdEnt player1 givePlayerPerk {perk};ai_ScriptCmdEnt player1 activatePlayerPerk {perk}";
		}}
	}}
}}
""")
                continue

            if command_type != "currency":
                raise ValueError(f"Unsupported entity command type for item {item_id}: {command_value}")
            currency = command_value["currency"]
            count = int(command_value.get("count", 1))
            blocks.append(f"""entity {{
	entityDef {RPC_ENTITY_PREFIX}_{item_id} {{
		inherit = "target/give_item";
		class = "idTarget_GiveItems";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			flags = {{
				noFlood = true;
			}}
			itemList = {{
				num = 0;
			}}
			addUpToCount = false;
			whenToSave = "SGT_NO_SAVE";
			saveType = "SGS_NONE";
			currencyList = {{
				num = 1;
				item[0] = {{
					currencyType = "{currency}";
					count = {count};
				}}
			}}
		}}
	}}
}}
""")
            continue

        if isinstance(command_value, list):
            if not command_value:
                raise ValueError(f"Multi-command item {item_id} has no commands")
            relay_targets = []
            command_blocks = []
            for idx, cmd in enumerate(command_value):
                cmd_entity_name = f"{RPC_ENTITY_PREFIX}_{item_id}_{idx}"
                relay_targets.append(cmd_entity_name)
                command_blocks.append(f"""entity {{
	entityDef {cmd_entity_name} {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "{cmd}";
		}}
	}}
}}
""")

            targets_block = "\n\t\t\t\t".join(f'item[{i}] = "{t}";' for i, t in enumerate(relay_targets))
            blocks.append(f"""entity {{
	entityDef {RPC_ENTITY_PREFIX}_{item_id} {{
		inherit = "target/relay";
		class = "idTarget_Count";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			count = 1;
			targets = {{
				num = {len(relay_targets)};
				{targets_block}
			}}
		}}
	}}
}}
""")
            blocks.extend(command_blocks)
        else:
            blocks.append(f"""entity {{
	entityDef {RPC_ENTITY_PREFIX}_{item_id} {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "{command_value}";
		}}
	}}
}}
""")

    return "".join(blocks)

def generate_system_command_entities():
    return """entity {
	entityDef ap_deathlink {
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {
			commandText = "kill";
		}
	}
}
entity {
	entityDef ap_rpc_auto_enable {
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {
			commandText = "condump ap_telemetry_ready.txt";
		}
	}
}
"""

def generate_map(input_file, output_file, config_file, manifest_file, items_dict):
    with open(config_file, "r", encoding="utf-8") as f:
        level_config = json.load(f)

    config_entities = level_config.get("entities", {})
    manifest_data = {}

    with open(input_file, "r", encoding="utf-8") as f:
        content = f.read()

    content = remove_balanced_entity_blocks(content, "ap_logic_")
    content = remove_balanced_entity_blocks(content, "AP_CHECK_")
    content = remove_balanced_entity_blocks(content, NOTIFICATION_ENTITY_PREFIX)
    content = remove_balanced_entity_blocks(content, EVENT_ENTITY_PREFIX)
    content = remove_balanced_entity_blocks(content, "ap_cmd_")
    content = remove_balanced_entity_blocks(content, RPC_ENTITY_PREFIX)
    for legacy_prefix in LEGACY_RPC_ENTITY_PREFIXES:
        content = remove_balanced_entity_blocks(content, legacy_prefix)
    content = remove_balanced_entity_blocks(content, "ap_deathlink")
    content = remove_balanced_entity_blocks(content, "ap_rpc_auto_enable")
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"ap_logic_[^"]+";', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"AP_CHECK_[^"]+";', '', content, flags=re.IGNORECASE)

    blocks = content.split("entity {")
    new_blocks = [blocks[0]]

    modified_count = 0

    trigger_conditions = [
        'inherit = "progress/codex"',
        'inherit = "pickup/collectible/',
        'inherit = "pickup/weapon/',
        'inherit = "pickup/extra_life/',
        'inherit = "progress/mod_bot"',
        'inherit = "progress/cheats/',
        'inherit = "pickup/equipment/',
        'inherit = "progress/rune"',
        'inherit = "progress/argent_cell"',
        'inherit = "progress/sentinel_battery"',
        'inherit = "progress/blood_punch"',
        'inherit = "progress/dash"',
        'inherit = "progress/praetor_token"',
        'inherit = "pickup/keycard/slayer_key"',
        'inherit = "interact/slayer_gate/chest"'
    ]

    for block in blocks[1:]:
        if any(cond in block for cond in trigger_conditions):
            name_match = re.search(r'entityDef\s+([^\s{]+)', block)
            if not name_match:
                new_blocks.append("entity {" + block)
                continue

            entity_name = name_match.group(1).strip()
            ap_check_id = f"AP_CHECK_{entity_name.upper()}"

            # NOVIDADE: Só processa a entidade se ela estiver no arquivo de level config!
            if ap_check_id not in config_entities:
                new_blocks.append("entity {" + block)
                continue

            manifest_data[ap_check_id] = config_entities[ap_check_id]

            spawn_pos_text = ""
            pos_match = re.search(r'(\s*spawnPosition\s*=\s*\{\s*x\s*=\s*([0-9.-]+);\s*y\s*=\s*([0-9.-]+);\s*z\s*=\s*([0-9.-]+);\s*\})', block)
            if pos_match:
                x = pos_match.group(2)
                y = pos_match.group(3)
                z = str(float(pos_match.group(4)) + 10.0)
                spawn_pos_text = f"\t\t\tspawnPosition = {{\n\t\t\t\tx = {x};\n\t\t\t\ty = {y};\n\t\t\t\tz = {z};\n\t\t\t}}\n"
            else:
                pos_fallback = re.search(r'(\s*spawnPosition\s*=\s*\{[^}]+\})', block)
                if pos_fallback:
                    spawn_pos_text = pos_fallback.group(1) + "\n"

            if "edit = {" in block:
                block = add_ap_check_target(block, entity_name, ap_check_id)

                # gravity stuff
                block = re.sub(r'physicsAttributes\s*=\s*"[^"]+";', "", block)

                block = remove_property_blocks(block, "clipModelInfo")

                if 'triggerDef =' in block:
                    block = re.sub(r'triggerDef\s*=\s*"[^"]+";', 'triggerDef = "trigger/props/pickup_large";', block)

                # Keep checks approachable without activating them while the
                # player is still far from the question mark.
                hitbox_injection = f"""
                clipModelInfo = {{
                        type = "CLIPMODEL_BOX";
                        size = {{
                                x = {AP_PICKUP_HITBOX_SIZE};
                                y = {AP_PICKUP_HITBOX_SIZE};
                                z = {AP_PICKUP_HITBOX_SIZE};
                        }}
                }}"""
                block = block.replace('edit = {', 'edit = {' + hitbox_injection, 1)

                # question mark model for all
                if "renderModelInfo = {" in block:
                    block = re.sub(
                        r'(renderModelInfo\s*=\s*\{[\s\S]*?model\s*=\s*)(?:"[^"]+"|NULL)',
                        r'\1"art/pickups/question_mark_a.lwo"',
                        block,
                        count=1
                    )
                    block = re.sub(r'scale\s*=\s*\{\s*x\s*=\s*[^;]+;\s*y\s*=\s*[^;]+;\s*z\s*=\s*[^;]+;\s*\}', "", block)
                else:
                    render_injection = """
                renderModelInfo = {
                        model = "art/pickups/question_mark_a.lwo";
                }"""
                    block = block.replace("edit = {", "edit = {" + render_injection, 1)

                # necessary for this architecture
                block = re.sub(r'\s*useableComponentDecl\s*=\s*"[^"]*";', '', block)

                block = re.sub(r'inherit\s*=\s*"[^"]+";', 'inherit = "trigger/trigger";', block)
                block = re.sub(r'class\s*=\s*"[^"]+";', 'class = "idTrigger";', block)

                location_id = config_entities[ap_check_id]
                relay_entity_str = generate_target_relay(
                    ap_check_id, location_id, spawn_pos_text
                )
                new_blocks.append(relay_entity_str)
                new_blocks.append(generate_pickup_notification(ap_check_id))
                new_blocks.append(generate_check_event(location_id))

                modified_count += 1

        if 'class = "idPlayerStart";' in block:
            # We want to add a targets block inside the edit block
            # Find edit = {
            edit_match = re.search(r'edit\s*=\s*\{', block)
            if edit_match:
                insert_idx = edit_match.end()
                
                # Check if it already has targets
                targets_match = re.search(r'targets\s*=\s*\{([^}]*)\}', block)
                if targets_match:
                    # Append to existing targets
                    targets_content = targets_match.group(1)
                    num_match = re.search(r'num\s*=\s*(\d+);', targets_content)
                    if num_match:
                        num = int(num_match.group(1))
                        # Replace num with num+1
                        new_targets_content = re.sub(
                            r'num\s*=\s*\d+;',
                            f'num = {num + 1};',
                            targets_content
                        )
                        # Append the new item at the end, right before the closing brace
                        new_targets = f"targets = {{{new_targets_content}\n\t\t\t\titem[{num}] = \"ap_rpc_auto_enable\";\n\t\t\t}}"
                        block = block[:targets_match.start()] + new_targets + block[targets_match.end():]
                else:
                    # Insert new targets block
                    injection = '\n\t\t\ttargets = {\n\t\t\t\tnum = 1;\n\t\t\t\titem[0] = "ap_rpc_auto_enable";\n\t\t\t}'
                    block = block[:insert_idx] + injection + block[insert_idx:]

        new_blocks.append("entity {" + block)

    final_content = (
        "".join(new_blocks)
        + "\n"
        + generate_rpc_command_entities(items_dict)
        + generate_system_command_entities()
    )

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(final_content)

    os.makedirs(os.path.dirname(manifest_file), exist_ok=True)
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=4)

    print(f"Successfully generated {modified_count} GLOBAL AP Targets using idTrigger mutation!")
    print(f"Manifest saved to {manifest_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Doom Eternal AP Map Generator")
    parser.add_argument("--input", required=True, help="Input .entities file")
    parser.add_argument("--output", required=True, help="Output .entities file")
    parser.add_argument("--config", required=True, help="Level configuration JSON")
    parser.add_argument("--manifest", required=True, help="Output manifest JSON")
    parser.add_argument("--items", default="data/items.json", help="Items JSON containing commands")

    args = parser.parse_args()

    with open(args.items, "r", encoding="utf-8") as f:
        items_dict = json.load(f)

    generate_map(args.input, args.output, args.config, args.manifest, items_dict)
