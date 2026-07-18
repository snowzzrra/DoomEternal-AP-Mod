import os
import re
import sys
import json
import argparse
import hashlib
from pathlib import Path
from foundation import build_primitive, validate_primitive_registry
from bootstrap_actions import BOOTSTRAP_ENTITY_PREFIXES

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
FORBIDDEN_WEAPON_MASTERY_CURRENCY = "CURRENCY_WEAPON_MASTERY"
AP_QUESTION_MARK_MODEL = "art/pickups/question_mark_a.lwo"

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


def assert_no_weapon_mastery_token_currency(content, context):
    """Reject any Token currency source in an AP-generated map.

    Weapon Mastery Tokens are not an AP item and an AP run must have no
    vanilla source of this currency.  Checking both the registered vanilla
    source and the generated output also prevents a stripped pickup from
    retaining a Token grant through an unreviewed target branch.
    """
    if FORBIDDEN_WEAPON_MASTERY_CURRENCY in content:
        raise ValueError(
            f"{context} contains forbidden vanilla Token currency "
            f"{FORBIDDEN_WEAPON_MASTERY_CURRENCY}"
        )


def native_praetor_token_family(block):
    """Recognize only the reviewed native Praetor currency transaction."""
    return (
        'inherit = "progress/praetor_token";' in block
        and 'class = "idInteractable_GiveItems";' in block
        and 'automapPropertiesDecl = "praetor_token";' in block
    )

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
    block = re.sub(
        r'\s*automapPropertiesDecl\s*=\s*(?:"[^"]*"|[^;]+);', "", block
    )
    return replace_targets_block(block, [])


def generate_independent_pickup_trigger(entity_name, ap_check_id, block, policy=None):
    """Create an AP trigger independent from an ownership-hidden pickup."""
    policy = policy or {}
    layers_match = re.search(r'(\s*layers\s*\{\s*"[^"]+"\s*\})', block)
    layers = f"\t{layers_match.group(1).strip()}\n" if layers_match and policy.get("preserve_layers", True) else ""
    position_match = re.search(r'(spawnPosition\s*=\s*\{\s*x\s*=\s*[^;]+;\s*y\s*=\s*[^;]+;\s*z\s*=\s*[^;]+;\s*\})', block)
    if not position_match:
        raise ValueError(f"Independent AP trigger requires spawnPosition: {entity_name}")
    configured_position = policy.get("independent_position")
    if configured_position is not None:
        if not isinstance(configured_position, list) or len(configured_position) != 3:
            raise ValueError(f"Independent AP trigger position must have three values: {entity_name}")
        position = (
            "spawnPosition = {\n"
            f"\t\t\t\tx = {configured_position[0]};\n"
            f"\t\t\t\ty = {configured_position[1]};\n"
            f"\t\t\t\tz = {configured_position[2]};\n"
            "\t\t\t}"
        )
    else:
        position = position_match.group(1)
    hitbox_size = policy.get(
        "independent_size",
        [AP_PICKUP_HITBOX_SIZE, AP_PICKUP_HITBOX_SIZE, AP_PICKUP_HITBOX_SIZE],
    )
    bind_parent = policy.get("bind_parent")
    bind_info_line = (
        f"\t\t\tbindInfo = {{\n\t\t\t\tbindParent = \"{bind_parent}\";\n\t\t\t}}\n"
        if bind_parent else ""
    )
    if not isinstance(hitbox_size, list) or len(hitbox_size) != 3:
        raise ValueError(f"Independent AP trigger size must have three values: {entity_name}")
    independent_name = policy.get("independent_entity_name", f"ap_independent_{entity_name}")
    targets = policy.get(
        "independent_targets",
        [ap_check_id],
    )
    target_lines = "\n".join(
        f'\t\t\t\titem[{index}] = "{target}";' for index, target in enumerate(targets)
    )
    return f'''entity {{
{layers}\tentityDef {independent_name} {{
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
\t\t\t\t\tx = {hitbox_size[0]};
\t\t\t\t\ty = {hitbox_size[1]};
\t\t\t\t\tz = {hitbox_size[2]};
\t\t\t\t}}
{bind_info_line}
\t\t\t}}
\t\t\ttargets = {{
\t\t\t\tnum = {len(targets)};
{target_lines}
\t\t\t}}
\t\t}}
\t}}
}}
'''


def generate_inert_location_visual(block, policy):
    """Create a rendered marker and its optional isolated cleanup target."""
    visual = policy.get("independent_visual")
    if not visual:
        return ""
    required = {"entity_name", "model", "position", "scale"}
    missing = sorted(required - set(visual))
    if missing:
        raise ValueError(f"Independent AP visual is missing: {', '.join(missing)}")
    if len(visual["position"]) != 3 or len(visual["scale"]) != 3:
        raise ValueError("Independent AP visual position and scale require three values")
    layers_match = re.search(r'(\s*layers\s*\{\s*"[^"]+"\s*\})', block)
    layers = f"\t{layers_match.group(1).strip()}\n" if layers_match and policy.get("preserve_layers", True) else ""
    position = visual["position"]
    scale = visual["scale"]
    entity_class = visual.get("class", "idDynamicEntity")
    inherit = visual.get("inherit", "func/dynamic")
    inherit_line = f'\t\tinherit = "{inherit}";\n' if inherit else ""
    automap_decl = visual.get("automap_properties_decl")
    automap_line = (
        f'\t\t\tautomapPropertiesDecl = "{automap_decl}";\n'
        if automap_decl else ""
    )
    think_decl = visual.get("thinkComponentDecl")
    think_line = (
        f'\t\t\tthinkComponentDecl = "{think_decl}";\n'
        if think_decl else ""
    )
    bind_parent = policy.get("bind_parent")
    bind_info_line = (
        f"\t\t\tbindInfo = {{\n\t\t\t\tbindParent = \"{bind_parent}\";\n\t\t\t}}\n"
        if bind_parent else ""
    )
    cleanup_name = visual.get("cleanup_entity")
    cleanup = ""
    if cleanup_name:
        cleanup = f'''entity {{
{layers}\tentityDef {cleanup_name} {{
\t\tinherit = "target/remove";
\t\tclass = "idTarget_Remove";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoFlood = true;
{bind_info_line}
\t\t\t}}
\t\t\ttargets = {{
\t\t\t\tnum = 1;
\t\t\t\titem[0] = "{visual["entity_name"]}";
\t\t\t}}
\t\t}}
\t}}
}}
'''
    return f'''entity {{
{layers}\tentityDef {visual["entity_name"]} {{
{inherit_line}\t\tclass = "{entity_class}";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
{automap_line}{think_line}{bind_info_line}\t\t\tspawnPosition = {{
\t\t\t\tx = {position[0]};
\t\t\t\ty = {position[1]};
\t\t\t\tz = {position[2]};
\t\t\t}}
\t\t\trenderModelInfo = {{
\t\t\t\tmodel = "{visual["model"]}";
\t\t\t\tcontributesToLightProbeGen = false;
\t\t\t\tignoreDesaturate = true;
\t\t\t\tscale = {{
\t\t\t\t\tx = {scale[0]};
\t\t\t\t\ty = {scale[1]};
\t\t\t\t\tz = {scale[2]};
\t\t\t\t}}
\t\t\t}}
\t\t\tclipModelInfo = {{
\t\t\t\ttype = "CLIPMODEL_NONE";
\t\t\t}}
\t\t\tdormancy = {{
\t\t\t\tallowPvsDormancy = false;
\t\t\t}}
\t\t}}
\t}}
}}
''' + cleanup


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
    block = replace_targets_block(block, target_names)
    if target_policy.get("gate_relay"):
        # First Fortress Crystal must remain inert until vanilla Flame Belch
        # chain activates target_relay_argent_cell_useable.
        if not re.search(r'flags\s*=\s*\{\s*hide\s*=\s*true;', block):
            block = block.replace("edit = {", "edit = {\n\t\t\tflags = { hide = true; }", 1)
    return block


def append_target_to_named_entity(content, entity_name, target_name):
    """Append a target to one vanilla relay without changing its other edges."""
    marker = f"entityDef {entity_name} {{"
    start = content.find(marker)
    if start < 0:
        raise ValueError(f"gated AP target relay missing: {entity_name}")
    open_brace = content.find("{", start)
    depth = 0
    for end in range(open_brace, len(content)):
        if content[end] == "{":
            depth += 1
        elif content[end] == "}":
            depth -= 1
            if depth == 0:
                entity_block = content[start:end + 1]
                targets = extract_target_names(entity_block)
                if target_name not in targets:
                    entity_block = replace_targets_block(entity_block, [*targets, target_name])
                return content[:start] + entity_block + content[end + 1:]
    raise ValueError(f"unterminated gated AP target relay: {entity_name}")


def build_universal_physical_policy(ap_check_id, location_id, block):
    """Generate an independent trigger and visual for any generic physical location.

    Preserves the original vanilla relay targets so that doors, gates, and other
    world events that the pickup used to trigger still fire correctly. The independent
    AP trigger becomes the sole fire-point: vanilla relays first, then AP_CHECK, then
    visual cleanup.
    """
    visual_name = f"ap_location_visual_{location_id}"
    cleanup_name = f"ap_remove_location_visual_{location_id}"

    position_match = re.search(
        r'spawnPosition\s*=\s*\{\s*x\s*=\s*([-+0-9.eE]+);\s*y\s*=\s*([-+0-9.eE]+);\s*z\s*=\s*([-+0-9.eE]+);\s*\}',
        block,
    )
    if not position_match:
        position = [0.0, 0.0, 0.0]
    else:
        position = [
            float(position_match.group(1)),
            float(position_match.group(2)),
            float(position_match.group(3)) + 1.5,
        ]

    bind_match = re.search(r'bindParent\s*=\s*"([^"]+)";', block)
    bind_parent = bind_match.group(1) if bind_match else None

    independent_targets = [ap_check_id, cleanup_name]

    return {
        "independent_ap_trigger": True,
        "independent_targets": independent_targets,
        "independent_size": [5.0, 5.0, 5.0],
        "remove_original": True,
        "bind_parent": bind_parent,
        "independent_visual": {
            "entity_name": visual_name,
            "class": "idProp2",
            "inherit": None,
            "automap_properties_decl": "default",
            "model": "art/pickups/question_mark_a.lwo",
            "thinkComponentDecl": "bob_rotate_fast",
            "position": position,
            "scale": [1.0, 1.0, 1.0],
            "cleanup_entity": cleanup_name,
        },
        "completion_targets": [cleanup_name],
    }


def generate_automap_location_helper(source_block, location_id):
    """Emit the proven targetless idInfo owner for one physical AP marker."""
    position = re.search(
        r'spawnPosition\s*=\s*\{\s*x\s*=\s*([-+0-9.eE]+);\s*'
        r'y\s*=\s*([-+0-9.eE]+);\s*z\s*=\s*([-+0-9.eE]+);\s*\}',
        source_block,
    )
    if not position:
        raise ValueError(f"Automap helper source position is missing for {location_id}")
    marker = re.search(
        r'automapPropertiesDecl\s*=\s*"([^"]+)";', source_block
    )
    automap_decl = marker.group(1) if marker else "default"
    return f'''entity {{
	entityDef ap_automap_location_{location_id} {{
		inherit = "info/null";
		class = "idInfo";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			spawnPosition = {{
				x = {position.group(1)};
				y = {position.group(2)};
				z = {position.group(3)};
			}}
			automapPropertiesDecl = "{automap_decl}";
		}}
	}}
}}
'''


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

def generate_event_relay(
    ap_check_id, location_id, spawn_pos_text, include_notification=True,
    completion_targets=None,
):
    event_name = f"{EVENT_ENTITY_PREFIX}{location_id}"
    target_names = list(completion_targets or [])
    if include_notification:
        notification_name = f"{NOTIFICATION_ENTITY_PREFIX}{ap_check_id}"
        target_names.append(notification_name)
    target_names.append(event_name)
    target_lines = [
        f'\t\t\t\titem[{index}] = "{target_name}";'
        for index, target_name in enumerate(target_names)
    ]

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


def generate_target_relay(
    ap_check_id, location_id, spawn_pos_text, completion_targets=None,
):
    return generate_event_relay(
        ap_check_id, location_id, spawn_pos_text, include_notification=True,
        completion_targets=completion_targets,
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
    """Historical stat-write bootstraps are intentionally absent from v0.2.2."""
    return ""

def generate_map(input_file, output_file, config_file, manifest_file, items_dict):
    with open(config_file, "r", encoding="utf-8") as f:
        level_config = json.load(f)

    config_entities = level_config.get("entities", {})
    target_policies = level_config.get("target_policies", {})
    neutralize_pickups = level_config.get("neutralize_pickups", [])
    target_removals = level_config.get("target_removals", {})
    remove_entities = level_config.get("remove_entities", [])
    neutralize_entity_references = level_config.get("neutralize_entity_references", [])
    secret_encounters = level_config.get("secret_encounters", [])
    manifest_data = {}
    map_key = level_config.get("map_key")

    source_metadata = validate_source_file(input_file, output_file)
    content = source_metadata["content"]
    for entity_name, policy in target_policies.items():
        gate_relay = policy.get("gate_relay")
        if gate_relay:
            content = append_target_to_named_entity(content, gate_relay, entity_name)
    assert_no_weapon_mastery_token_currency(content, f"Registered vanilla map {map_key}")

    for entity_name in neutralize_pickups:
        content = neutralize_conditional_pickup(content, entity_name)

    for entity_name, removed_targets in target_removals.items():
        bounds = find_entity_block_bounds(content, entity_name)
        if bounds is None:
            raise ValueError(f"Target-removal entity not found: {entity_name}")
        start, end = bounds
        block = content[start:end]
        existing_targets = extract_target_names(block)
        missing = sorted(set(removed_targets) - set(existing_targets))
        if missing:
            raise ValueError(
                f"Target-removal entity {entity_name} is missing expected targets: "
                + ", ".join(missing)
            )
        block = replace_targets_block(
            block,
            [target for target in existing_targets if target not in removed_targets],
        )
        content = content[:start] + block + content[end:]

    for entity_name in remove_entities:
        bounds = find_entity_block_bounds(content, entity_name)
        if bounds is None:
            raise ValueError(f"Configured removal entity not found: {entity_name}")
        content = content[:bounds[0]] + content[bounds[1]:]

    for entity_name in neutralize_entity_references:
        reference = re.compile(rf'(\bentity\s*=\s*)"{re.escape(entity_name)}";')
        content, replacements = reference.subn(r'\1"";', content)
        if replacements != 1:
            raise ValueError(
                f"Expected exactly one neutralized entity reference for {entity_name}, "
                f"found {replacements}"
            )

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
                location_id = config_entities[ap_check_id]
                target_policy = target_policies.get(entity_name)
                
                if not target_policy:
                    target_policy = build_universal_physical_policy(ap_check_id, location_id, block)

                audit_preserved_target_graph(content, entity_name, target_policy)
                
                new_blocks.append(
                    generate_automap_location_helper(block, location_id)
                )
                if target_policy and target_policy.get("independent_ap_trigger"):
                    if target_policy.get("remove_original", False) or (not target_policy.get("independent_visual") and not target_policy.get("no_auto_visual")):
                        vanilla_targets = extract_target_names(block)
                        existing_independent = target_policy.get("independent_targets", [ap_check_id])
                        target_policy["independent_targets"] = list(dict.fromkeys(
                            [t for t in vanilla_targets if t] + existing_independent
                        ))

                    manifest_data[ap_check_id] = location_id
                    if target_policy.get("independent_visual"):
                        cleanup = target_policy["independent_visual"].get("cleanup_entity")
                        if cleanup and cleanup not in target_policy.setdefault("independent_targets", [ap_check_id]):
                            target_policy["independent_targets"].append(cleanup)

                    if not target_policy.get("independent_visual") and not target_policy.get("no_auto_visual"):
                        universal = build_universal_physical_policy(ap_check_id, location_id, block)
                        target_policy["independent_visual"] = universal["independent_visual"]
                        if universal["independent_visual"]["cleanup_entity"] not in target_policy.get("completion_targets", []):
                            target_policy.setdefault("completion_targets", []).append(universal["independent_visual"]["cleanup_entity"])
                        if universal["independent_visual"]["cleanup_entity"] not in target_policy.get("independent_targets", []):
                            target_policy.setdefault("independent_targets", target_policy.get("independent_targets", [ap_check_id])).append(universal["independent_visual"]["cleanup_entity"])
                    if not target_policy.get("remove_original", False):
                        new_blocks.append(
                            "entity {" + neutralize_conditional_pickup_block(block)
                        )
                    new_blocks.append(
                        generate_independent_pickup_trigger(entity_name, ap_check_id, block, target_policy)
                    )
                    visual = generate_inert_location_visual(block, target_policy)
                    if visual:
                        new_blocks.append(visual)
                    new_blocks.append(generate_target_relay(
                        ap_check_id,
                        location_id,
                        "",
                        completion_targets=target_policy.get("completion_targets"),
                    ))
                    new_blocks.append(generate_pickup_notification(ap_check_id))
                    new_blocks.append(generate_check_event(location_id))
                    modified_count += 1
                    continue
                else:
                    raise ValueError(f"Legacy non-independent physical check logic hit for {entity_name}")

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
    assert_no_weapon_mastery_token_currency(final_content, f"Generated map {map_key}")

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
