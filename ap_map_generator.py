import os
import re
import sys
import json
import argparse
import hashlib
from pathlib import Path
from foundation import build_primitive, validate_primitive_registry
from bootstrap_actions import (
    BOOTSTRAP_ACTIONS,
    BOOTSTRAP_ENTITY_PREFIX,
    BOOTSTRAP_ENTITY_PREFIXES,
    BOOTSTRAP_STAT_PRIMITIVE,
    INVALID_BOOTSTRAP_INHERITS,
    validate_bootstrap_catalogue,
)

AP_PICKUP_HITBOX_SIZE = 6
RPC_ENTITY_PREFIX = "ap_rpc_v3"
LEGACY_RPC_ENTITY_PREFIXES = ("ap_rpc_v2_",)
NOTIFICATION_ENTITY_PREFIX = "ap_notify_"
EVENT_ENTITY_PREFIX = "ap_event_"
GENERATED_NAME_PREFIXES = (
    "AP_CHECK_",
    RPC_ENTITY_PREFIX,
    *BOOTSTRAP_ENTITY_PREFIXES,
    NOTIFICATION_ENTITY_PREFIX,
    EVENT_ENTITY_PREFIX,
    "ap_rpc_auto_enable",
)
SECRET_ENCOUNTER_ARG_LABEL = ""

# you don't need to use this to play the mod
# i'm making this file available simply for transparency and for anyone who wants to generate the AP targets in other maps by themselves, since the process is a bit tedious to do manually and this automates it
# you could just do the changes by hand, but why??


def compute_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_path(path):
    return Path(path).expanduser().resolve(strict=False)


def ensure_distinct_input_output_paths(input_file, output_file):
    input_path = normalize_path(input_file)
    output_path = normalize_path(output_file)
    if input_path == output_path:
        raise ValueError(
            f"Input and output must be different files: {input_path}"
        )


def find_generated_prefixes(content):
    matches = []
    for prefix in GENERATED_NAME_PREFIXES:
        if prefix in content:
            matches.append(prefix)
    return matches


def validate_source_file(input_file, output_file):
    ensure_distinct_input_output_paths(input_file, output_file)

    input_path = Path(input_file).expanduser().resolve(strict=True)
    source_hash_before = compute_file_sha256(input_path)
    content = input_path.read_text(encoding="utf-8")
    injected_prefixes = find_generated_prefixes(content)
    if injected_prefixes:
        prefix_list = ", ".join(injected_prefixes)
        raise ValueError(
            f"Input source already contains generated AP prefixes: {prefix_list}"
        )

    return {
        "input_path": input_path,
        "size": input_path.stat().st_size,
        "sha256_before": source_hash_before,
        "content": content,
    }

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


def find_matching_brace(content, open_brace_index):
    depth = 1
    i = open_brace_index + 1
    while depth > 0 and i < len(content):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    if depth != 0:
        raise ValueError("Unbalanced braces while parsing entities content")
    return i


def find_entity_block_bounds(content, entity_name):
    entity_match = re.search(
        r"entityDef\s+" + re.escape(entity_name) + r"\s*\{",
        content,
    )
    if not entity_match:
        return None

    block_start = content.rfind("entity {", 0, entity_match.start())
    if block_start == -1:
        raise ValueError(f"Could not locate enclosing entity block for {entity_name}")

    open_brace_index = content.find("{", block_start)
    block_end = find_matching_brace(content, open_brace_index)
    return block_start, block_end


def neutralize_conditional_pickup(content, entity_name):
    """Keep a script-addressable entity name while removing pickup behavior."""
    bounds = find_entity_block_bounds(content, entity_name)
    if bounds is None:
        raise ValueError(f"Conditional pickup not found: {entity_name}")
    start, end = bounds
    block = content[start:end]
    block = re.sub(r'inherit\s*=\s*"[^"]+";', 'inherit = "info/null";', block, count=1)
    block = re.sub(r'class\s*=\s*"[^"]+";', 'class = "idInfo";', block, count=1)
    block = remove_property_blocks(block, "renderModelInfo")
    block = remove_property_blocks(block, "clipModelInfo")
    for property_name in (
        "useableComponentDecl", "triggerDef", "equipOnPickup", "lootStyle",
        "forceEquip", "canBePossessed",
    ):
        block = re.sub(
            rf'\s*{property_name}\s*=\s*(?:"[^"]*"|[^;]+);', "", block
        )
    return content[:start] + block + content[end:]


def neutralize_conditional_pickup_block(block):
    """Leave a named vanilla pickup inert without preserving its targets."""
    block = re.sub(r'inherit\s*=\s*"[^"]+";', 'inherit = "info/null";', block, count=1)
    block = re.sub(r'class\s*=\s*"[^"]+";', 'class = "idInfo";', block, count=1)
    block = remove_property_blocks(block, "renderModelInfo")
    block = remove_property_blocks(block, "clipModelInfo")
    for property_name in (
        "useableComponentDecl", "triggerDef", "equipOnPickup", "lootStyle",
        "forceEquip", "canBePossessed",
    ):
        block = re.sub(rf'\s*{property_name}\s*=\s*(?:"[^"]*"|[^;]+);', "", block)
    return replace_targets_block(block, [])


def generate_independent_pickup_trigger(entity_name, ap_check_id, block):
    """Create an AP trigger independent from an ownership-hidden pickup."""
    layers_match = re.search(r'(\s*layers\s*\{\s*"[^"]+"\s*\})', block)
    layers = f"\t{layers_match.group(1).strip()}\n" if layers_match else ""
    position_match = re.search(r'(spawnPosition\s*=\s*\{\s*x\s*=\s*[^;]+;\s*y\s*=\s*[^;]+;\s*z\s*=\s*[^;]+;\s*\})', block)
    if not position_match:
        raise ValueError(f"Independent AP trigger requires spawnPosition: {entity_name}")
    position = position_match.group(1)
    return f'''entity {{
{layers}\tentityDef ap_independent_{entity_name} {{
\t\tinherit = "trigger/trigger";
\t\tclass = "idTrigger";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\ttriggerOnce = true;
\t\t\tflags = {{
\t\t\t\tnoFlood = true;
\t\t\t}}
\t\t\t{position}
\t\t\tclipModelInfo = {{
\t\t\t\ttype = "CLIPMODEL_BOX";
\t\t\t\tsize = {{
\t\t\t\t\tx = {AP_PICKUP_HITBOX_SIZE};
\t\t\t\t\ty = {AP_PICKUP_HITBOX_SIZE};
\t\t\t\t\tz = {AP_PICKUP_HITBOX_SIZE};
\t\t\t\t}}
\t\t\t}}
\t\t\ttargets = {{
\t\t\t\tnum = 3;
\t\t\t\titem[0] = "target_relay_pickup_ice_bomb";
\t\t\t\titem[1] = "{ap_check_id}";
\t\t\t\titem[2] = "ap_ice_ripatorium_live_refresh";
\t\t\t}}
\t\t}}
\t}}
}}
'''


def generate_ice_ripatorium_live_refresh():
    """Replay only the vanilla delayed live transition after Ice completion."""
    return '''entity {
\tentityDef ap_ice_ripatorium_live_refresh {
\t\tinherit = "target/relay";
\t\tclass = "idTarget_Count";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {
\t\t\tflags = {
\t\t\t\tnoFlood = true;
\t\t\t}
\t\t\tcount = 1;
\t\t\tdelay = 7.25;
\t\t\ttargets = {
\t\t\t\tnum = 4;
\t\t\t\titem[0] = "target_objective_prison_lift";
\t\t\t\titem[1] = "trigger_lift_poi_off";
\t\t\t\titem[2] = "target_poi_lift_door";
\t\t\t\titem[3] = "target_relay_enable_prison_lift";
\t\t\t}
\t\t}
\t}
}
'''


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


def extract_target_names(block):
    match = re.search(
        r'targets\s*=\s*\{\s*num\s*=\s*\d+;\s*(.*?)\s*\}',
        block,
        re.DOTALL,
    )
    if not match:
        return []
    return re.findall(r'item\[\d+\]\s*=\s*"([^"]+)";', match.group(1))


def add_ap_check_target(block, entity_name, ap_check_id, target_policy=None):
    existing_targets = extract_target_names(block)
    target_policy = target_policy or {}
    preserve_targets = target_policy.get("preserve_targets")
    drop_targets = set(target_policy.get("drop_targets", []))

    required_targets = set(drop_targets)
    if preserve_targets is not None:
        required_targets.update(preserve_targets)
    missing_targets = sorted(required_targets - set(existing_targets))
    if missing_targets:
        missing = ", ".join(missing_targets)
        raise ValueError(
            f"{entity_name} target policy expected missing target(s): {missing}"
        )

    if preserve_targets is None:
        target_names = [
            target for target in existing_targets if target not in drop_targets
        ]
    else:
        preserve_set = set(preserve_targets)
        target_names = [
            target for target in existing_targets if target in preserve_set
        ]

    target_names.append(ap_check_id)
    return replace_targets_block(block, target_names)


def audit_preserved_target_graph(content, entity_name, target_policy):
    """Fail closed when a pickup's retained vanilla branch can grant a reward.

    Scripted tutorial pickups are converted to ordinary AP triggers.  Their
    retained targets therefore need an explicit, source-verified graph rather
    than relying on a propitem DECL to suppress a hidden idProp2 reward.
    """
    if not target_policy:
        return
    roots = target_policy.get("preserve_targets", [])
    expected_graph = target_policy.get("safe_target_graph")
    forbidden_terms = target_policy.get("forbidden_target_terms", [])
    if not roots or expected_graph is None or not forbidden_terms:
        return

    pending = list(roots)
    visited = set()
    while pending:
        target_name = pending.pop()
        if target_name in visited:
            continue
        bounds = find_entity_block_bounds(content, target_name)
        if bounds is None:
            raise ValueError(
                f"{entity_name} preserved target graph is missing: {target_name}"
            )
        block = content[bounds[0]:bounds[1]]
        for term in forbidden_terms:
            if term.lower() in block.lower():
                raise ValueError(
                    f"{entity_name} preserved target graph reaches forbidden reward "
                    f"term {term!r} in {target_name}"
                )
        actual_targets = extract_target_names(block)
        if target_name not in expected_graph:
            raise ValueError(
                f"{entity_name} preserved target graph has unexpected node: {target_name}"
            )
        expected_targets = expected_graph[target_name]
        if actual_targets != expected_targets:
            raise ValueError(
                f"{entity_name} preserved target graph drift at {target_name}: "
                f"expected {expected_targets}, got {actual_targets}"
            )
        pending.extend(actual_targets)
        visited.add(target_name)

def generate_event_relay(ap_check_id, location_id, spawn_pos_text, include_notification=True):
    event_name = f"{EVENT_ENTITY_PREFIX}{location_id}"
    target_lines = []
    if include_notification:
        notification_name = f"{NOTIFICATION_ENTITY_PREFIX}{ap_check_id}"
        target_lines.append(f'\t\t\t\titem[0] = "{notification_name}";')
        target_lines.append(f'\t\t\t\titem[1] = "{event_name}";')
    else:
        target_lines.append(f'\t\t\t\titem[0] = "{event_name}";')

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
				num = {len(target_lines)};
{chr(10).join(target_lines)}
			}}
{spawn_pos_text}		}}
	}}
}}
"""


def generate_target_relay(ap_check_id, location_id, spawn_pos_text):
    return generate_event_relay(
        ap_check_id, location_id, spawn_pos_text, include_notification=True
    )


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


def inject_secret_encounter_completion(
    content,
    manager_name,
    ap_check_id,
    expected_last_event_index,
):
    bounds = find_entity_block_bounds(content, manager_name)
    if bounds is None:
        raise ValueError(f"Secret encounter manager not found: {manager_name}")

    block_start, block_end = bounds
    block = content[block_start:block_end]
    if f'entity = "{ap_check_id}";' in block:
        return content

    manager_marker = f'entity = "{manager_name}";'
    manager_marker_index = block.find(manager_marker)
    if manager_marker_index == -1:
        raise ValueError(
            f"Secret encounter manager self-reference not found: {manager_name}"
        )

    events_match = re.search(
        r"events\s*=\s*\{\s*num\s*=\s*(\d+);",
        block[manager_marker_index:],
    )
    if not events_match:
        raise ValueError(f"Events block not found for manager {manager_name}")

    events_num = int(events_match.group(1))
    if events_num == expected_last_event_index + 1:
        pass
    else:
        raise ValueError(
            f"Manager {manager_name} has {events_num} events, expected "
            f"{expected_last_event_index + 1} before AP hook insertion"
        )

    events_header_start = manager_marker_index + events_match.start()
    events_num_start = manager_marker_index + events_match.start(1)
    events_num_end = manager_marker_index + events_match.end(1)

    updated_block = block[:events_num_start] + str(events_num + 1) + block[events_num_end:]

    updated_events_open_brace = updated_block.find("{", events_header_start)
    updated_events_close_brace = find_matching_brace(
        updated_block, updated_events_open_brace
    )
    insertion = f"""
						item[{events_num}] = {{
							eventCall = {{
								eventDef = "activateTarget";
								args = {{
									num = 2;
									item[0] = {{
										entity = "{ap_check_id}";
									}}
									item[1] = {{
										string = "{SECRET_ENCOUNTER_ARG_LABEL}";
									}}
								}}
							}}
						}}
"""
    updated_block = (
        updated_block[: updated_events_close_brace - 1]
        + insertion
        + updated_block[updated_events_close_brace - 1 :]
    )
    return content[:block_start] + updated_block + content[block_end:]


def command_requires_map_side_rpc(command):
    return isinstance(command, str) and bool(command.strip())

def generate_rpc_command_entities(items_dict):
    validate_primitive_registry()
    blocks = []
    required_entities = []
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
                    blocks.append(build_primitive(
                        "target_command", entity_name,
                        {"command": f"ai_ScriptCmdEnt player1 givePlayerPerk {perk};ai_ScriptCmdEnt player1 activatePlayerPerk {perk}"},
                    ))
                continue

            if command_type == "perk":
                perk = command_value.get("perk")
                if not perk:
                    raise ValueError(f"Perk item {item_id} has no perk path")
                blocks.append(build_primitive(
                    "target_command", f"{RPC_ENTITY_PREFIX}_{item_id}",
                    {"command": f"ai_ScriptCmdEnt player1 givePlayerPerk {perk};ai_ScriptCmdEnt player1 activatePlayerPerk {perk}"},
                ))
                continue

            if command_type != "currency":
                raise ValueError(f"Unsupported entity command type for item {item_id}: {command_value}")
            currency = command_value["currency"]
            count = int(command_value.get("count", 1))
            blocks.append(build_primitive(
                "currency_grant_direct", f"{RPC_ENTITY_PREFIX}_{item_id}",
                {"currency": currency, "count": count},
            ))
            continue

        if isinstance(command_value, list):
            if not command_value:
                raise ValueError(f"Multi-command item {item_id} has no commands")
            relay_targets = []
            command_blocks = []
            for idx, cmd in enumerate(command_value):
                cmd_entity_name = f"{RPC_ENTITY_PREFIX}_{item_id}_{idx}"
                if command_requires_map_side_rpc(cmd):
                    required_entities.append(cmd_entity_name)
                relay_targets.append(cmd_entity_name)
                command_blocks.append(build_primitive(
                    "target_command", cmd_entity_name, {"command": cmd}
                ))

            blocks.append(build_primitive(
                "target_count_relay", f"{RPC_ENTITY_PREFIX}_{item_id}",
                {"targets": relay_targets},
            ))
            blocks.extend(command_blocks)
        else:
            entity_name = f"{RPC_ENTITY_PREFIX}_{item_id}"
            if command_requires_map_side_rpc(command_value):
                required_entities.append(entity_name)
            blocks.append(build_primitive(
                "target_command", entity_name, {"command": command_value}
            ))

    generated = "".join(blocks)
    missing_entities = [
        entity_name
        for entity_name in required_entities
        if f"entityDef {entity_name} {{" not in generated
    ]
    if missing_entities:
        raise ValueError(
            "Map-side RPC entity missing for unsafe command(s): "
            + ", ".join(sorted(missing_entities))
        )
    return generated

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


def generate_bootstrap_entities():
    """Emit historical v2 controls; bridge automation remains disabled."""
    validate_bootstrap_catalogue()
    blocks = []
    for action in BOOTSTRAP_ACTIONS.values():
        block = build_primitive(
            "boolean_stat_modifier_direct",
            action["entity_name"],
            {"stat": action["stat"], "value": 1},
            release=False,
        )
        lowered = block.lower()
        if any(term.lower() in lowered for term in action["forbidden_effects"]):
            raise ValueError(
                f"Bootstrap entity contains forbidden effect: {action['action']}"
            )
        if (
            block.count('class = "idTarget_PlayerStatModifier";') != 1
            or block.count("gameStat = ") != 1
            or block.count("value = 1;") != 1
            or "item[" in lowered
            or "inherit =" in lowered
            or "target/player_stat_modifier" in lowered
        ):
            raise ValueError(f"Bootstrap entity is not a one-stat modifier: {action['action']}")
        blocks.append(block)
    return "".join(blocks)

def generate_map(input_file, output_file, config_file, manifest_file, items_dict):
    with open(config_file, "r", encoding="utf-8") as f:
        level_config = json.load(f)

    config_entities = level_config.get("entities", {})
    target_policies = level_config.get("target_policies", {})
    neutralize_pickups = level_config.get("neutralize_pickups", [])
    secret_encounters = level_config.get("secret_encounters", [])
    manifest_data = {}

    source_metadata = validate_source_file(input_file, output_file)
    content = source_metadata["content"]

    for entity_name in neutralize_pickups:
        content = neutralize_conditional_pickup(content, entity_name)

    content = remove_balanced_entity_blocks(content, "ap_logic_")
    content = remove_balanced_entity_blocks(content, "AP_CHECK_")
    content = remove_balanced_entity_blocks(content, NOTIFICATION_ENTITY_PREFIX)
    content = remove_balanced_entity_blocks(content, EVENT_ENTITY_PREFIX)
    content = remove_balanced_entity_blocks(content, "ap_cmd_")
    content = remove_balanced_entity_blocks(content, RPC_ENTITY_PREFIX)
    for bootstrap_prefix in BOOTSTRAP_ENTITY_PREFIXES:
        content = remove_balanced_entity_blocks(content, bootstrap_prefix)
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
                target_policy = target_policies.get(entity_name)
                audit_preserved_target_graph(content, entity_name, target_policy)
                if target_policy and target_policy.get("independent_ap_trigger"):
                    # The Hub Ice pickup can be hidden/removed when Ice is
                    # already owned. Its AP check must therefore not be a
                    # mutation of that pickup. Keep its audited objective
                    # branch on a separate trigger and leave the vanilla
                    # reward carrier inert.
                    location_id = config_entities[ap_check_id]
                    manifest_data[ap_check_id] = location_id
                    new_blocks.append(
                        "entity {" + neutralize_conditional_pickup_block(block)
                    )
                    new_blocks.append(
                        generate_independent_pickup_trigger(entity_name, ap_check_id, block)
                    )
                    new_blocks.append(generate_ice_ripatorium_live_refresh())
                    new_blocks.append(generate_target_relay(ap_check_id, location_id, ""))
                    new_blocks.append(generate_pickup_notification(ap_check_id))
                    new_blocks.append(generate_check_event(location_id))
                    modified_count += 1
                    continue
                block = add_ap_check_target(
                    block,
                    entity_name,
                    ap_check_id,
                    target_policy,
                )

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
                block = re.sub(r'\s*equipOnPickup\s*=\s*\w+;', '', block)
                block = re.sub(r'\s*forceEquip\s*=\s*\w+;', '', block)

                block = re.sub(r'inherit\s*=\s*"[^"]+";', 'inherit = "trigger/trigger";', block)
                block = re.sub(r'class\s*=\s*"[^"]+";', 'class = "idTrigger";', block)
                if not re.search(r'\btriggerOnce\s*=', block):
                    block = block.replace("edit = {", "edit = {\n\t\t\ttriggerOnce = true;", 1)
                else:
                    block = re.sub(r'\btriggerOnce\s*=\s*\w+;', 'triggerOnce = true;', block)

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

    map_content = "".join(new_blocks)
    secret_blocks = []
    for secret_hook in secret_encounters:
        ap_check_id = secret_hook["ap_check"]
        location_id = secret_hook["location_id"]
        manager_name = secret_hook.get("manager", secret_hook.get("manager_entity"))
        if not manager_name:
            raise ValueError(
                f"Secret encounter {ap_check_id} is missing a manager entity name"
            )
        expected_last_event_index = secret_hook["after_event_index"]
        map_content = inject_secret_encounter_completion(
            map_content,
            manager_name,
            ap_check_id,
            expected_last_event_index,
        )
        manifest_data[ap_check_id] = location_id
        secret_blocks.append(
            generate_event_relay(ap_check_id, location_id, "", include_notification=False)
        )
        secret_blocks.append(generate_check_event(location_id))
        modified_count += 1

    final_content = (
        map_content
        + "\n"
        + "".join(secret_blocks)
        + generate_rpc_command_entities(items_dict)
        + generate_bootstrap_entities()
        + generate_system_command_entities()
    )

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(final_content)

    source_hash_after = compute_file_sha256(source_metadata["input_path"])
    if source_hash_after != source_metadata["sha256_before"]:
        raise ValueError(
            f"Input source was modified during generation: {source_metadata['input_path']}"
        )

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
