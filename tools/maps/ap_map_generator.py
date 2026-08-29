import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path

from doom_eap.contracts.ap_visual_contract import load_ap_visual_contract
from doom_eap.runtime.bootstrap_actions import BOOTSTRAP_ENTITY_PREFIXES
from doom_eap.contracts.foundation import (
    ITEM_NOTIFICATION_PREFIX,
    build_primitive,
    validate_primitive_registry,
)
from doom_eap.content.item_classification import (
    load_item_classifications,
    notification_entity_name,
    notification_style_for_item,
)
from doom_eap.runtime.item_reconciliation import (
    AP_RECEIPT_FEEDBACK,
    SUPPORTED_RECEIPT_FEEDBACK,
    load_policy_registry,
)
from tools.maps.notification_formatting import (
    ITEM_NOTIFICATION_HEADER_KEY,
    LOCATION_NOTIFICATION_HEADER_KEY,
    location_notification_key,
    major_notification_key_from_item_key,
    notification_key,
    placement_sent_key,
    progressive_notification_stage_count,
)
AP_PICKUP_HITBOX_SIZE = 6
RPC_ENTITY_PREFIX = "ap_rpc_v3"
LEGACY_RPC_ENTITY_PREFIXES = ("ap_rpc_v2_",)
NOTIFICATION_ENTITY_PREFIX = "ap_notify_"
LOCATION_NOTIFICATION_PREFIX = "ap_notify_location_"
EVENT_ENTITY_PREFIX = "ap_event_"
AP_LIFECYCLE_ENTITY_PREFIX = "ap_lifecycle_"
GENERATED_NAME_PREFIXES = (
    "AP_CHECK_",
    RPC_ENTITY_PREFIX,
    *BOOTSTRAP_ENTITY_PREFIXES,
    NOTIFICATION_ENTITY_PREFIX,
    EVENT_ENTITY_PREFIX,
    AP_LIFECYCLE_ENTITY_PREFIX,
    "ap_rpc_auto_enable",
    ITEM_NOTIFICATION_PREFIX.rstrip("_"),  # ap_notify_item
)
SECRET_ENCOUNTER_ARG_LABEL = ""
FORBIDDEN_WEAPON_MASTERY_CURRENCY = "CURRENCY_WEAPON_MASTERY"
AP_QUESTION_MARK_MODEL = "art/pickups/question_mark_a.lwo"


def canonical_ap_visual_for_map(map_key):
    """Resolve the global AP visual only for registry-enabled campaign maps."""
    if not map_key:
        return None
    from doom_eap.content.content_catalog import load_content_catalog

    catalog = load_content_catalog()
    if map_key not in {spec.key for spec in catalog.enabled_maps()}:
        return None
    return load_ap_visual_contract()

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


def remove_visual_child_entities(content, removals):
    """Remove configured visual children only when their vanilla shape is exact."""
    if not isinstance(removals, list):
        raise ValueError("visual_child_removals must be a list")

    validated = []
    seen_entities = set()
    for removal in removals:
        required = {"entity", "model", "bind_parent", "preserve_entity"}
        if not isinstance(removal, dict) or set(removal) != required:
            raise ValueError(
                "visual child removal requires exactly entity, model, bind_parent, "
                "preserve_entity"
            )
        entity_name = removal["entity"]
        model = removal["model"]
        bind_parent = removal["bind_parent"]
        preserve_entity = removal["preserve_entity"]
        if not all(
            isinstance(value, str) and value
            for value in (entity_name, model, bind_parent, preserve_entity)
        ):
            raise ValueError("visual child removal fields must be non-empty strings")
        if entity_name in seen_entities:
            raise ValueError(f"Duplicate visual child removal entity: {entity_name}")
        seen_entities.add(entity_name)
        if content.count(f"entityDef {entity_name} {{") != 1:
            raise ValueError(f"Visual child removal entity is not unique: {entity_name}")
        bounds = find_entity_block_bounds(content, entity_name)
        if bounds is None:
            raise ValueError(f"Visual child removal entity not found: {entity_name}")
        block = content[bounds[0]:bounds[1]]
        models = re.findall(r'\bmodel\s*=\s*"([^"]+)";', block)
        bind_parents = re.findall(r'\bbindParent\s*=\s*"([^"]+)";', block)
        if models != [model] or bind_parents != [bind_parent]:
            raise ValueError(
                f"Visual child removal signature drifted: {entity_name}; "
                f"models={models}, bind_parents={bind_parents}"
            )
        if (
            'inherit = "func/animated";' not in block
            or 'class = "idAnimated";' not in block
            or extract_target_names(block)
        ):
            raise ValueError(f"Visual child removal is not a targetless animated child: {entity_name}")
        if content.count(f"entityDef {preserve_entity} {{") != 1:
            raise ValueError(
                f"Visual child removal preserve entity is not unique: {preserve_entity}"
            )
        validated.append((entity_name, preserve_entity, bounds))

    for _entity_name, _preserve_entity, bounds in sorted(
        validated, key=lambda item: item[2][0], reverse=True
    ):
        content = content[:bounds[0]] + content[bounds[1]:]

    for entity_name, preserve_entity, _bounds in validated:
        if content.count(f"entityDef {entity_name} {{") != 0:
            raise ValueError(f"Visual child removal was not applied: {entity_name}")
        if content.count(f"entityDef {preserve_entity} {{") != 1:
            raise ValueError(
                f"Visual child removal changed preserve entity count: {preserve_entity}"
            )
    return content


SECRET_PRESENTATION_FIELDS = (
    "notificationType", "notificationHudEventID", "notificationEndHudEventID",
    "notificationSound", "notificationTime", "rootWidget", "icon", "header",
    "subtext", "showCVar", "priority", "doNotShowDuplicate", "showDuringCombat",
)

SECRET_INHERIT_RE = re.compile(
    r'(?i)(\binherit[ \t]*=[ \t]*)"target/secret"([ \t]*;)'
)
SECRET_CLASS_RE = re.compile(
    r'(?i)(\bclass[ \t]*=[ \t]*)"idTarget_Secret"([ \t]*;)'
)
SECRET_IDENTITY_HINT_RE = re.compile(
    r'(?i)\b(?:inherit|class)[ \t]*=[^\r\n;]*'
    r'(?:target/secret|idTarget_Secret)'
)
ACTIVE_SECRET_INHERIT_RE = re.compile(
    r'(?im)\binherit[ \t]*=[ \t]*"target/secret"'
)
ACTIVE_SECRET_CLASS_RE = re.compile(
    r'(?im)\bclass[ \t]*=[ \t]*"idTarget_Secret"'
)
EXPAND_INHERITANCE_FALSE_RE = re.compile(
    r'(?i)\bexpandInheritance[ \t]*=[ \t]*false[ \t]*;'
)
COUNT_ASSIGNMENT_RE = re.compile(
    r'(?i)\bcount[ \t]*=[ \t]*([^;\r\n]+)[ \t]*;'
)
EDIT_OPEN_RE = re.compile(
    r'(?im)^(?P<indent>[ \t]*)edit[ \t]*=[ \t]*\{[ \t]*(?P<newline>\r?\n)'
)


def suppress_vanilla_secret_found_ui(content):
    """Replace inherited Secret targets with local non-popup relay semantics.

    ``target/secret`` owns player notification dispatch through the global
    ``idPlayer.notificationManager`` definition.  ``target/relay`` with the
    locally evidenced ``idTarget_Count`` class retains ordinary target
    activation while avoiding that inherited player-facing behavior.  Only
    declaration identity changes; source/target edges and existing edit state
    remain byte-for-byte unchanged, with required relay ``count = 1`` added.
    Unknown Secret declaration shapes fail closed.
    """
    blocks = content.split("entity {")
    updated = [blocks[0]]
    for block in blocks[1:]:
        if not SECRET_IDENTITY_HINT_RE.search(block):
            updated.append("entity {" + block)
            continue

        inherit_matches = list(SECRET_INHERIT_RE.finditer(block))
        class_matches = list(SECRET_CLASS_RE.finditer(block))
        if len(inherit_matches) != 1 or len(class_matches) != 1:
            raise ValueError(
                "Unexpected Secret target declaration: malformed, mismatched, "
                "or duplicate identity"
            )
        if len(EXPAND_INHERITANCE_FALSE_RE.findall(block)) != 1:
            raise ValueError(
                "Unexpected Secret target declaration: expandInheritance must be false"
            )
        if any(
            re.search(rf'\b{re.escape(field)}\s*=', block, flags=re.IGNORECASE)
            for field in SECRET_PRESENTATION_FIELDS
        ):
            raise ValueError(
                "Unexpected Secret target declaration: inline presentation fields"
            )
        if COUNT_ASSIGNMENT_RE.search(block):
            raise ValueError(
                "Unexpected Secret target declaration: count already present"
            )
        edit_matches = list(EDIT_OPEN_RE.finditer(block))
        if len(edit_matches) != 1:
            raise ValueError(
                "Unexpected Secret target declaration: edit block is not unique"
            )

        block = SECRET_INHERIT_RE.sub(
            lambda match: (
                f'{match.group(1)}"target/relay"{match.group(2)}'
            ),
            block,
            count=1,
        )
        block = SECRET_CLASS_RE.sub(
            lambda match: (
                f'{match.group(1)}"idTarget_Count"{match.group(2)}'
            ),
            block,
            count=1,
        )
        block = EDIT_OPEN_RE.sub(
            lambda match: (
                f'{match.group(0)}{match.group("indent")}\tcount = 1;'
                f'{match.group("newline")}'
            ),
            block,
            count=1,
        )
        if len(COUNT_ASSIGNMENT_RE.findall(block)) != 1:
            raise ValueError(
                "Secret target suppression failed to add exactly one count"
            )
        updated.append("entity {" + block)
    result = "".join(updated)
    if ACTIVE_SECRET_INHERIT_RE.search(result) or ACTIVE_SECRET_CLASS_RE.search(result):
        raise ValueError("Secret target suppression left active vanilla identity")
    return result


def retain_single_stat_increase(content, property_name, stat_name):
    pattern = re.compile(
        r'\b' + re.escape(property_name) + r'\s*=\s*\{',
        re.IGNORECASE,
    )
    match = pattern.search(content)
    if match is None:
        raise ValueError(
            f"Native entity contract lacks {property_name} stat block"
        )
    close_brace = find_matching_brace(content, match.end() - 1)
    original = content[match.start():close_brace + 1]
    stat_pattern = re.compile(
        r'item\[\d+\]\s*=\s*\{\s*'
        rf'stat\s*=\s*"{re.escape(stat_name)}";\s*'
        r'increase\s*=\s*(-?\d+);\s*\}',
        re.DOTALL,
    )
    stat_matches = stat_pattern.findall(original)
    if stat_matches != ["1"]:
        raise ValueError(
            f"Native entity contract expected one {stat_name} increase"
        )
    replacement = (
        f'{property_name} = {{\n'
        '\t\t\tnum = 1;\n'
        '\t\t\titem[0] = {\n'
        f'\t\t\t\tstat = "{stat_name}";\n'
        '\t\t\t\tincrease = 1;\n'
        '\t\t\t}\n'
        '\t\t}'
    )
    return content[:match.start()] + replacement + content[close_brace + 1:]


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


def remove_inline_currency_transaction(content, contract):
    """Remove one exact inline GiveItems currency mutation, fail closed."""
    required = {
        "entity", "class", "property", "currency", "count",
        "original_targets", "preserved_requirement",
    }
    if not isinstance(contract, dict) or set(contract) != required:
        raise ValueError("Inline currency removal contract schema drift")
    bounds = find_entity_block_bounds(content, contract["entity"])
    if bounds is None:
        raise ValueError(
            f"Inline currency owner not found: {contract['entity']}"
        )
    start, end = bounds
    block = content[start:end]
    class_line = f'class = "{contract["class"]}";'
    if block.count(class_line) != 1:
        raise ValueError("Inline currency owner class drift")
    targets = extract_target_names(block)
    if targets != contract["original_targets"]:
        raise ValueError(
            f"Inline currency owner target drift: {targets}"
        )

    pattern = re.compile(
        rf'\n\s*{re.escape(contract["property"])}\s*=\s*\{{'
    )
    matches = list(pattern.finditer(block))
    if len(matches) != 1:
        raise ValueError("Inline currency transaction must occur exactly once")
    match = matches[0]
    open_brace = block.find("{", match.start())
    close_exclusive = find_matching_brace(block, open_brace)
    transaction = block[match.start():close_exclusive]
    expected_currency = (
        f'currencyType = "{contract["currency"]}";'
    )
    expected_count = f'count = {contract["count"]};'
    if (
        transaction.count("num = 1;") != 1
        or transaction.count("item[0] = {") != 1
        or transaction.count(expected_currency) != 1
        or transaction.count(expected_count) != 1
    ):
        raise ValueError("Inline currency transaction payload drift")
    updated = block[:match.start()] + block[close_exclusive:]
    if contract["property"] in updated or contract["currency"] in updated:
        raise ValueError("Inline currency transaction was not fully removed")
    if extract_target_names(updated) != contract["original_targets"]:
        raise ValueError("Inline currency removal changed target ordering")
    if updated.count(class_line) != 1:
        raise ValueError("Inline currency removal changed owner class")
    return content[:start] + updated + content[end:]


def assert_canonical_ap_visuals(content, map_key, contract):
    """Fail closed if any generated visual escapes the canonical UV bundle."""
    names = re.findall(r"entityDef (ap_location_visual_\d+) \{", content)
    if not names:
        raise ValueError(f"Enabled map has no generated AP visuals: {map_key}")
    for name in names:
        bounds = find_entity_block_bounds(content, name)
        if bounds is None:
            raise ValueError(f"Canonical AP visual entity missing: {name}")
        block = content[bounds[0]:bounds[1]]
        models = re.findall(r'\bmodel\s*=\s*"([^"]+)";', block)
        if models != [contract["model"]]:
            raise ValueError(
                f"AP visual {name} is not canonical: {models}"
            )
        if (
            contract["forbidden_geometry"] in block
            or contract["forbidden_streamdb_payload"] in block
        ):
            raise ValueError(f"AP visual {name} uses question-mark UV payload")


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


def apply_native_entity_contract(block, contract):
    required_snippets = contract.get("required_snippets", [])
    missing_snippets = [snippet for snippet in required_snippets if snippet not in block]
    if missing_snippets:
        raise ValueError(
            "Native entity contract source drift; missing required snippet(s): "
            + ", ".join(repr(snippet) for snippet in missing_snippets)
        )
    expected_targets = contract.get("original_targets")
    if expected_targets is not None and extract_target_names(block) != expected_targets:
        raise ValueError(
            "Native entity contract source target drift: "
            f"expected {expected_targets}, got {extract_target_names(block)}"
        )
    if "remove_block" in contract:
        if not re.search(rf'\b{re.escape(contract["remove_block"])}\s*=\s*\{{', block):
            raise ValueError(
                "Native entity contract source drift; missing removable block: "
                f"{contract['remove_block']}"
            )
        block = remove_property_blocks(block, contract["remove_block"])
    for property_name, value in contract.get("set_properties", {}).items():
        if isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, str):
            rendered = f'"{value}"'
        else:
            rendered = str(value)
        scalar = re.compile(
            rf'(\b{re.escape(property_name)}\s*=\s*)'
            r'(?:"[^"]*"|true|false|-?\d+(?:\.\d+)?);'
        )
        block, replacements = scalar.subn(
            rf'\g<1>{rendered};', block, count=1
        )
        if replacements != 1:
            raise ValueError(
                "Native entity contract source drift; missing scalar property: "
                f"{property_name}"
            )
    retain_stat = contract.get("retain_pickup_stat")
    if retain_stat:
        block = retain_single_stat_increase(
            block, "pickup_statIncreases", retain_stat
        )
    return block


TARGET_POLICY_CONSUMERS = {
    "independent_ap_trigger": "generate_map independent-trigger branch",
    "remove_original": "generate_map original-owner branch",
    "drop_targets": "build_independent_targets",
    "preserve_targets": "build_independent_targets",
    "safe_target_graph": "audit_preserved_target_graph",
    "forbidden_target_terms": "audit_preserved_target_graph",
    "gate_relay": "append_target_to_named_entity",
    "independent_entity_name": "generate_independent_pickup_trigger",
    "independent_position": "generate_independent_pickup_trigger",
    "independent_visual_z_offset": "build_universal_physical_policy",
    "independent_size": "generate_independent_pickup_trigger",
    "independent_targets": "generate_independent_pickup_trigger",
    "independent_visual": "generate_inert_location_visual",
    "completion_targets": "generate_target_relay",
    "no_auto_visual": "generate_map visual branch",
    "no_auto_automap_helper": "generate_map automap helper branch",
    "independent_automap_properties_decl": "build_universal_physical_policy",
    "preserve_layers": "generate_independent_pickup_trigger",
    "bind_parent": "generate_independent_pickup_trigger/generate_inert_location_visual",
    "preserve_original_visual": "generate_map original-owner branch",
    "native_entity_contract": "apply_native_entity_contract",
    "checkpoint_cleanup": "apply_checkpoint_cleanup_contract",
    "ap_touch_only": "native_entity_contract touch-only policy",
    "duplicate_policy": "native_entity_contract duplicate policy",
}

NATIVE_ENTITY_CONTRACT_KEYS = {
    "remove_block", "original_targets", "required_snippets",
    "set_properties", "retain_pickup_stat",
}

DUPLICATE_POLICIES = {"native_only"}

CHECKPOINT_CLEANUP_CONTRACT_KEYS = {
    "source_entity", "source_sha256", "event_index", "event_def",
    "original_target", "replacement_target", "original_targets",
    "replacement_targets",
}


def _timeline_event_targets(block):
    return [
        target
        for _, target in re.findall(
            r'^\t{4}item\[(\d+)\] = \{\n\t{5}entity = "([^"]*)";',
            block,
            re.MULTILINE,
        )
    ]


def apply_checkpoint_cleanup_contract(content, contract):
    """Retarget one hash-locked native timeline event without moving it."""
    if not isinstance(contract, dict):
        raise ValueError("checkpoint_cleanup must be an object")
    unknown = sorted(set(contract) - CHECKPOINT_CLEANUP_CONTRACT_KEYS)
    missing = sorted(CHECKPOINT_CLEANUP_CONTRACT_KEYS - set(contract))
    if unknown or missing:
        details = []
        if unknown:
            details.append("unsupported key(s): " + ", ".join(unknown))
        if missing:
            details.append("missing key(s): " + ", ".join(missing))
        raise ValueError("Checkpoint cleanup contract " + "; ".join(details))

    source_entity = contract["source_entity"]
    bounds = find_entity_block_bounds(content, source_entity)
    if bounds is None:
        raise ValueError(
            f"Checkpoint cleanup source entity not found: {source_entity}"
        )
    start, end = bounds
    block = content[start:end]
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    if digest != contract["source_sha256"]:
        raise ValueError(
            f"Checkpoint cleanup source hash drift for {source_entity}: "
            f"expected {contract['source_sha256']}, got {digest}"
        )
    original_targets = _timeline_event_targets(block)
    if original_targets != contract["original_targets"]:
        raise ValueError(
            f"Checkpoint cleanup original target order drift for {source_entity}: "
            f"expected {contract['original_targets']}, got {original_targets}"
        )

    event_index = contract["event_index"]
    header = re.search(
        rf'^\t{{4}}item\[{event_index}\] = \{{\n'
        rf'\t{{5}}entity = "{re.escape(contract["original_target"])}";',
        block,
        re.MULTILINE,
    )
    if header is None:
        raise ValueError(
            f"Checkpoint cleanup event {event_index} target drift for {source_entity}"
        )
    item_open = block.find("{", header.start())
    item_end = find_matching_brace(block, item_open)
    event_block = block[header.start():item_end]
    event_defs = re.findall(r'eventDef\s*=\s*"([^"]+)";', event_block)
    if event_defs != [contract["event_def"]]:
        raise ValueError(
            f"Checkpoint cleanup event definition drift for {source_entity}: "
            f"expected {[contract['event_def']]}, got {event_defs}"
        )

    original_line = f'entity = "{contract["original_target"]}";'
    replacement_line = f'entity = "{contract["replacement_target"]}";'
    if block.count(original_line) != 1:
        raise ValueError(
            f"Checkpoint cleanup target is not unique in {source_entity}"
        )
    updated = block.replace(original_line, replacement_line, 1)
    replacement_targets = _timeline_event_targets(updated)
    if replacement_targets != contract["replacement_targets"]:
        raise ValueError(
            f"Checkpoint cleanup replacement target order invalid for {source_entity}: "
            f"expected {contract['replacement_targets']}, got {replacement_targets}"
        )
    if len(block) - len(original_line) + len(replacement_line) != len(updated):
        raise ValueError("Checkpoint cleanup changed more than the approved target")
    return content[:start] + updated + content[end:]


def validate_target_policies(config_entities, target_policies, content):
    """Fail closed for configured policy keys before map generation mutates them."""
    if not isinstance(target_policies, dict):
        raise ValueError("target_policies must be an object")
    for entity_name, policy in target_policies.items():
        expected_check = f"AP_CHECK_{entity_name.upper()}"
        if expected_check not in config_entities:
            raise ValueError(
                f"Target policy has no configured AP check: {entity_name}"
            )
        if not isinstance(policy, dict):
            raise ValueError(f"Target policy must be an object: {entity_name}")
        unknown = sorted(set(policy) - set(TARGET_POLICY_CONSUMERS))
        if unknown:
            raise ValueError(
                f"Target policy has unsupported key(s) for {entity_name}: "
                + ", ".join(unknown)
            )
        if policy.get("ap_touch_only"):
            if "native_entity_contract" not in policy:
                raise ValueError(
                    f"ap_touch_only requires native_entity_contract: {entity_name}"
                )
            if policy.get("independent_ap_trigger"):
                raise ValueError(
                    f"ap_touch_only cannot use independent_ap_trigger: {entity_name}"
                )
            if not policy.get("no_auto_visual"):
                raise ValueError(
                    f"ap_touch_only requires no_auto_visual: {entity_name}"
                )
        duplicate_policy = policy.get("duplicate_policy")
        if duplicate_policy not in (None, *DUPLICATE_POLICIES):
            raise ValueError(
                f"Unsupported duplicate policy for {entity_name}: {duplicate_policy}"
            )
        if duplicate_policy == "native_only" and "native_entity_contract" not in policy:
            raise ValueError(
                f"native_only duplicate policy requires native_entity_contract: {entity_name}"
            )
        independent_only = {
            "remove_original", "independent_entity_name", "independent_position",
            "independent_size", "independent_targets", "independent_visual",
            "completion_targets", "no_auto_visual", "preserve_layers", "bind_parent",
        }
        unused_independent = sorted(
            set(policy) & independent_only
            if not policy.get("independent_ap_trigger")
            else ()
        )
        if policy.get("ap_touch_only"):
            unused_independent = [
                key for key in unused_independent if key != "no_auto_visual"
            ]
        if unused_independent:
            raise ValueError(
                f"Target policy has unused independent-trigger field(s) for {entity_name}: "
                + ", ".join(unused_independent)
            )
        graph_only = {"safe_target_graph", "forbidden_target_terms"}
        unused_graph = sorted(
            set(policy) & graph_only if "preserve_targets" not in policy else ()
        )
        if unused_graph:
            raise ValueError(
                f"Target policy has unused target-graph field(s) for {entity_name}: "
                + ", ".join(unused_graph)
            )
        bounds = find_entity_block_bounds(content, entity_name)
        if bounds is None:
            raise ValueError(f"Target policy source entity not found: {entity_name}")
        source_block = content[bounds[0]:bounds[1]]
        validate_sentinel_crystal_policy(entity_name, policy, source_block)
        source_targets = extract_target_names(source_block)
        for key in ("drop_targets", "preserve_targets"):
            if key not in policy:
                continue
            targets = policy[key]
            if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
                raise ValueError(f"{key} must be a list of target names: {entity_name}")
            missing = sorted(set(targets) - set(source_targets))
            if missing:
                raise ValueError(
                    f"{entity_name} {key} missing from source targets: "
                    + ", ".join(missing)
                )
        if set(policy.get("drop_targets", [])) & set(policy.get("preserve_targets", [])):
            raise ValueError(f"{entity_name} cannot preserve and drop the same target")
        if "native_entity_contract" in policy:
            contract = policy["native_entity_contract"]
            if not isinstance(contract, dict) or not contract:
                raise ValueError(f"native_entity_contract must be an object: {entity_name}")
            unknown_contract = sorted(set(contract) - NATIVE_ENTITY_CONTRACT_KEYS)
            if unknown_contract:
                raise ValueError(
                    f"Native entity contract has unsupported key(s) for {entity_name}: "
                    + ", ".join(unknown_contract)
                )
            if (
                "original_targets" not in contract
                or not isinstance(contract["original_targets"], list)
                or not all(
                    isinstance(target, str)
                    for target in contract["original_targets"]
                )
            ):
                raise ValueError(
                    f"native_entity_contract requires exact original_targets: {entity_name}"
                )
            snippets = contract.get("required_snippets")
            if (
                not isinstance(snippets, list)
                or len(snippets) < 3
                or not all(isinstance(snippet, str) and snippet for snippet in snippets)
            ):
                raise ValueError(
                    f"native_entity_contract requires stable required_snippets: {entity_name}"
                )
            evidence = "\n".join(snippets)
            if not any(token in evidence for token in ("inherit =", "class =")):
                raise ValueError(
                    f"native_entity_contract lacks a stable owner class: {entity_name}"
                )
            if not any(
                token in evidence
                for token in ("whenToSave =", "saveType =", "stat =")
            ):
                raise ValueError(
                    f"native_entity_contract lacks a save writer: {entity_name}"
                )
            if not any(
                token in evidence
                for token in (
                    "useableComponentDecl =",
                    "currency",
                    "currencyAmount",
                    "target_relay",
                    "2_battery_required",
                    "triggerDef =",
                    "transitionName =",
                    "class = \"idTarget_Count\";",
                )
            ):
                raise ValueError(
                    f"native_entity_contract lacks a reward/progression edge: {entity_name}"
                )
            apply_native_entity_contract(source_block, contract)
        cleanup = policy.get("checkpoint_cleanup")
        if cleanup is not None and not isinstance(cleanup, dict):
            raise ValueError(f"checkpoint_cleanup must be an object: {entity_name}")


def bind_parent_from_source(policy, block):
    """Propagate vanilla moving-platform ownership to every generated physical owner."""
    bind_match = re.search(r'bindParent\s*=\s*"([^"]+)";', block)
    if bind_match and not policy.get("bind_parent"):
        policy["bind_parent"] = bind_match.group(1)
    return policy

def neutralize_conditional_pickup_block(block, preserve_original_visual=False):
    """Leave a named vanilla pickup inert without preserving its targets."""
    block = re.sub(r'inherit\s*=\s*"[^"]+";', 'inherit = "info/null";', block, count=1)
    block = re.sub(r'class\s*=\s*"[^"]+";', 'class = "idInfo";', block, count=1)
    if not preserve_original_visual:
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
    if preserve_original_visual:
        for property_name in ("fxDecl", "thinkComponentDecl"):
            block = re.sub(
                rf'\s*{property_name}\s*=\s*(?:"[^"]*"|[^;]+);', "", block
            )
    return replace_targets_block(block, [])


def is_sentinel_crystal_source(block):
    return all(
        marker in block
        for marker in (
            'inherit = "progress/argent_cell";',
            'class = "idInteractable_WorldCache";',
            'automapPropertiesDecl = "argent_cell";',
        )
    )


SENTINEL_CRYSTAL_TOP_MODEL = "art/kit/sentinel/prop/argent_cell_top.lwo"
SENTINEL_CRYSTAL_BRIDGE_OWNER = "progress_argent_cell_1_1072112848"
SENTINEL_CRYSTAL_OBJECTIVE_TARGET = "target_objective_complete_argent_cell"
SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET = "target_relay_argent_cell_used"
SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS = [
    "currency", "give", "grant", "inventory", "perk",
]
SENTINEL_CRYSTAL_FORBIDDEN_PRESENTATION_MARKERS = (
    'inherit = "progress/argent_cell";',
    'class = "idInteractable_WorldCache";',
    'model = "md6def/objects/interact/argent_cell/argent_cell.md6";',
    "animWebDecl =",
    "interactionGraph =",
    "markForGameUsed = true;",
    "useableComponentDecl =",
    "triggerDef =",
    "upgrade",
    "reward",
)


def is_sentinel_crystal_top(block):
    return (
        re.findall(r'\binherit\s*=\s*"([^"]+)";', block)
        == ["destructible/interact/argent_cell"]
        and re.findall(r'\bclass\s*=\s*"([^"]+)";', block)
        == ["idDestructible"]
        and re.findall(
            r'\brenderModelInfo\s*=\s*\{.*?\bmodel\s*=\s*"([^"]+)";',
            block,
            flags=re.DOTALL,
        )
        == [SENTINEL_CRYSTAL_TOP_MODEL]
    )


def _entity_name_from_block(block):
    match = re.search(r'\bentityDef\s+([^\s{]+)', block)
    return match.group(1) if match else None


def _spawn_position_from_block(block):
    match = re.search(r'\bspawnPosition\s*=\s*\{([^}]*)\}', block)
    if match is None:
        return None
    values = []
    for axis in ("x", "y", "z"):
        axis_match = re.search(
            rf'\b{axis}\s*=\s*([-+0-9.eE]+);', match.group(1)
        )
        if axis_match is None:
            return None
        values.append(float(axis_match.group(1)))
    return tuple(values)


def _list_item_names(block, property_name):
    property_match = re.search(
        rf'\b{re.escape(property_name)}\s*=\s*\{{', block
    )
    if property_match is None:
        return []
    property_end = find_matching_brace(block, property_match.end() - 1)
    return re.findall(
        r'\bitem\[\d+\]\s*=\s*"([^"]+)";',
        block[property_match.start():property_end],
    )


def find_sentinel_crystal_pairs(blocks):
    """Resolve each functional crystal owner to its exact destructible top."""
    named_blocks = [
        (_entity_name_from_block(block), block)
        for block in blocks
    ]
    top_blocks = [
        (name, block)
        for name, block in named_blocks
        if name and is_sentinel_crystal_top(block)
    ]
    pairs = {}
    for source_name, source_block in named_blocks:
        if not source_name or not is_sentinel_crystal_source(source_block):
            continue
        owner_names = _list_item_names(source_block, "stateActivateList")
        candidates = [
            (top_name, top_block)
            for top_name, top_block in top_blocks
            if top_name in owner_names
            and _spawn_position_from_block(top_block)
            == _spawn_position_from_block(source_block)
        ]
        if len(candidates) != 1:
            raise ValueError(
                "Sentinel Crystal requires exactly one paired top: "
                f"{source_name} (found {len(candidates)})"
            )
        pairs[source_name] = candidates[0][0]
    return pairs


def validate_sentinel_crystal_policy(entity_name, policy, source_block):
    """Fail closed on Sentinel Crystal target ownership and story graph."""
    if not is_sentinel_crystal_source(source_block):
        return
    if not policy.get("independent_ap_trigger") or not policy.get("remove_original"):
        raise ValueError(
            f"Sentinel Crystal requires independent AP removal: {entity_name}"
        )
    if "independent_size" in policy:
        raise ValueError(
            f"Sentinel Crystal cannot define independent_size: {entity_name}"
        )
    if policy.get("drop_targets", []) != []:
        raise ValueError(
            f"Sentinel Crystal drop_targets must be empty: {entity_name}"
        )
    preserve_targets = policy.get("preserve_targets")
    if entity_name == SENTINEL_CRYSTAL_BRIDGE_OWNER:
        expected_preserve_targets = [
            SENTINEL_CRYSTAL_OBJECTIVE_TARGET,
            SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET,
        ]
        if preserve_targets != expected_preserve_targets:
            raise ValueError(
                "Bridge Sentinel Crystal must preserve objective and continuation "
                "targets: "
                f"{entity_name}"
            )
        if policy.get("safe_target_graph") != {
            SENTINEL_CRYSTAL_OBJECTIVE_TARGET: [],
            SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET: [],
        }:
            raise ValueError(
                "Bridge Sentinel Crystal objective graph is not exact: "
                f"{entity_name}"
            )
        if policy.get("independent_targets") != [
            f"AP_CHECK_{entity_name.upper()}",
            SENTINEL_CRYSTAL_OBJECTIVE_TARGET,
            SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET,
        ]:
            raise ValueError(
                "Bridge Sentinel Crystal AP target sequence is not exact: "
                f"{entity_name}"
            )
        if policy.get("forbidden_target_terms") != SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS:
            raise ValueError(
                "Bridge Sentinel Crystal forbidden target terms are not exact: "
                f"{entity_name}"
            )
        return
    if preserve_targets != []:
        raise ValueError(
            "Only bridge Sentinel Crystal may preserve a target: "
            f"{entity_name}"
        )
    if "safe_target_graph" in policy or "forbidden_target_terms" in policy:
        raise ValueError(
            "Non-bridge Sentinel Crystal cannot define preserved story graph: "
            f"{entity_name}"
        )


def assert_sentinel_crystal_transform(
    content, pairs, location_ids=None, check_ids=None
):
    """Fail closed when Sentinel Crystal output retains vanilla ownership."""
    location_ids = location_ids or {}
    check_ids = check_ids or {}
    for source_name, top_name in pairs.items():
        if find_entity_block_bounds(content, source_name) is not None:
            raise ValueError(f"Sentinel Crystal source was emitted: {source_name}")
        for block in content.split("entity {")[1:]:
            if source_name in extract_target_names(block):
                referrer = _entity_name_from_block(block) or "<unnamed>"
                raise ValueError(
                    "Sentinel Crystal source has generated target reference: "
                    f"{source_name} <- {referrer}"
                )
        if content.count(f"entityDef {top_name} {{") != 0:
            raise ValueError(f"Sentinel Crystal top was not removed: {top_name}")
        if SENTINEL_CRYSTAL_TOP_MODEL in content:
            raise ValueError(
                f"Sentinel Crystal top model remains after removal: {top_name}"
            )
        trigger_name = f"ap_independent_{source_name}"
        trigger_bounds = find_entity_block_bounds(content, trigger_name)
        if trigger_bounds is None:
            raise ValueError(f"Sentinel Crystal AP trigger missing: {trigger_name}")
        if content.count(f"entityDef {trigger_name} {{") != 1:
            raise ValueError(f"Sentinel Crystal AP trigger is not unique: {trigger_name}")
        trigger_block = content[trigger_bounds[0]:trigger_bounds[1]]
        trigger_targets = extract_target_names(trigger_block)
        ap_check = check_ids.get(source_name, f"AP_CHECK_{source_name.upper()}")
        expected_trigger_targets = [ap_check]
        if source_name == SENTINEL_CRYSTAL_BRIDGE_OWNER:
            expected_trigger_targets.extend(
                (
                    SENTINEL_CRYSTAL_OBJECTIVE_TARGET,
                    SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET,
                )
            )
        if trigger_targets != expected_trigger_targets:
            raise ValueError(
                f"Sentinel Crystal AP trigger target drift: {trigger_name}; "
                f"expected {expected_trigger_targets}, got {trigger_targets}"
            )
        if any(
            term.lower() in target.lower()
            for target in trigger_targets
            for term in SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS
        ):
            raise ValueError(f"Sentinel Crystal AP trigger has forbidden target: {trigger_name}")
        trigger_markers = [
            marker for marker in SENTINEL_CRYSTAL_FORBIDDEN_PRESENTATION_MARKERS
            if marker.lower() in trigger_block.lower()
        ]
        if trigger_markers:
            raise ValueError(
                f"Sentinel Crystal AP trigger retains vanilla markers: {trigger_name}; "
                + ", ".join(trigger_markers)
            )

        if content.count(f"entityDef {ap_check} {{") != 1:
            raise ValueError(f"Sentinel Crystal AP check relay is not unique: {ap_check}")
        ap_check_bounds = find_entity_block_bounds(content, ap_check)
        if ap_check_bounds is None:
            raise ValueError(f"Sentinel Crystal AP check relay missing: {ap_check}")
        ap_check_block = content[ap_check_bounds[0]:ap_check_bounds[1]]
        ap_check_targets = extract_target_names(ap_check_block)
        if any(
            term.lower() in target.lower()
            for target in ap_check_targets
            for term in SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS
        ):
            raise ValueError(f"Sentinel Crystal AP check has forbidden target: {ap_check}")

        visual_matches = re.findall(r'entityDef (ap_location_visual_\d+) \{', content)
        location_id = location_ids.get(ap_check)
        if location_id is not None:
            matching_visuals = [f"ap_location_visual_{location_id}"]
        elif len(visual_matches) == 1:
            matching_visuals = visual_matches
        else:
            matching_visuals = []
        if len(matching_visuals) != 1:
            raise ValueError(
                f"Sentinel Crystal AP visual is not unique: {source_name}"
            )
        if content.count(f"entityDef {matching_visuals[0]} {{") != 1:
            raise ValueError(
                f"Sentinel Crystal AP visual is not unique: {matching_visuals[0]}"
            )
        visual_bounds = find_entity_block_bounds(content, matching_visuals[0])
        if visual_bounds is None:
            raise ValueError(f"Sentinel Crystal AP visual missing: {matching_visuals[0]}")
        visual_block = content[visual_bounds[0]:visual_bounds[1]]
        forbidden_markers = [
            marker for marker in SENTINEL_CRYSTAL_FORBIDDEN_PRESENTATION_MARKERS
            if marker.lower() in visual_block.lower()
        ]
        if forbidden_markers:
            raise ValueError(
                f"Sentinel Crystal AP visual retains vanilla markers: {source_name}; "
                + ", ".join(forbidden_markers)
            )

        if source_name == SENTINEL_CRYSTAL_BRIDGE_OWNER:
            objective_bounds = find_entity_block_bounds(
                content, SENTINEL_CRYSTAL_OBJECTIVE_TARGET
            )
            if objective_bounds is None:
                raise ValueError(
                    "Bridge Sentinel Crystal objective target is missing"
                )
            objective_block = content[objective_bounds[0]:objective_bounds[1]]
            if extract_target_names(objective_block) != []:
                raise ValueError(
                    "Bridge Sentinel Crystal objective target has native targets"
                )
            for term in SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS:
                if term.lower() in objective_block.lower():
                    raise ValueError(
                        "Bridge Sentinel Crystal objective target has forbidden "
                        f"reward term: {term}"
                    )
            continuation_bounds = find_entity_block_bounds(
                content, SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET
            )
            if continuation_bounds is None:
                raise ValueError(
                    "Bridge Sentinel Crystal continuation target is missing"
                )
            continuation_block = content[
                continuation_bounds[0]:continuation_bounds[1]
            ]
            if extract_target_names(continuation_block) != []:
                raise ValueError(
                    "Bridge Sentinel Crystal continuation target has native targets"
                )
            for term in SENTINEL_CRYSTAL_FORBIDDEN_TARGET_TERMS:
                if term.lower() in continuation_block.lower():
                    raise ValueError(
                        "Bridge Sentinel Crystal continuation target has forbidden "
                        f"reward term: {term}"
                    )


def generate_independent_pickup_trigger(entity_name, ap_check_id, block, policy=None):
    """Create an AP trigger independent from an ownership-hidden pickup."""
    policy = policy or {}
    layers_match = re.search(r'(\s*layers\s*\{\s*"[^"]+"\s*\})', block)
    layers = f"\t{layers_match.group(1).strip()}\n" if layers_match and policy.get("preserve_layers", True) else ""
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
        position_match = re.search(r'(spawnPosition\s*=\s*\{\s*x\s*=\s*[^;]+;\s*y\s*=\s*[^;]+;\s*z\s*=\s*[^;]+;\s*\})', block)
        if not position_match:
            raise ValueError(f"Independent AP trigger requires spawnPosition: {entity_name}")
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
\t\t\t}}
{bind_info_line}
\t\t\ttargets = {{
\t\t\t\tnum = {len(targets)};
{target_lines}
\t\t\t}}
\t\t}}
\t}}
}}
'''


def generate_inert_location_visual(block, policy):
    """Create rendered marker plus local-removal and reconciliation targets."""
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
    layers = (
        f"\t{layers_match.group(1).strip()}\n"
        if layers_match and policy.get("preserve_visual_layers", True)
        else ""
    )
    position = visual["position"]
    scale = visual["scale"]
    entity_class = visual.get("class", "idDynamicEntity")
    inherit = visual.get("inherit")
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
    orientation = visual.get("spawn_orientation")
    if orientation is None and policy.get("preserve_rotation"):
        orientation_match = re.search(r'\bspawnOrientation\s*=\s*\{', block)
        if orientation_match:
            orientation_end = find_matching_brace(block, orientation_match.end() - 1)
            orientation = block[orientation_match.start():orientation_end + 1]
    orientation_line = ""
    if orientation:
        orientation_line = "\n".join(
            "\t" + line if line else line for line in orientation.splitlines()
        ) + "\n"
    bind_parent = policy.get("bind_parent")
    bind_info_line = (
        f"\t\t\tbindInfo = {{\n\t\t\t\tbindParent = \"{bind_parent}\";\n\t\t\t}}\n"
        if bind_parent else ""
    )
    cleanup_name = visual.get("cleanup_entity")
    reconciliation_name = visual.get("reconciliation_entity")
    if cleanup_name and not reconciliation_name:
        location_match = re.fullmatch(
            r"ap_location_visual_(\d+)", visual["entity_name"]
        )
        if location_match:
            reconciliation_name = (
                f"ap_hide_location_visual_{location_match.group(1)}"
            )
    if cleanup_name and not reconciliation_name:
        raise ValueError(
            f"AP visual reconciliation entity is missing: {visual['entity_name']}"
        )
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
    reconciliation = ""
    if reconciliation_name:
        reconciliation = f'''entity {{
{layers}\tentityDef {reconciliation_name} {{
\t\tinherit = "target/hide";
\t\tclass = "idTarget_Hide";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\treuseable = true;
\t\t\tflags = {{
\t\t\t\tnoFlood = true;
{bind_info_line}\t\t\t\t}}
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
{automap_line}{think_line}\t\t\tisStatic = false;
{bind_info_line}\t\t\tspawnPosition = {{
\t\t\t\tx = {position[0]};
\t\t\t\ty = {position[1]};
\t\t\t\tz = {position[2]};
\t\t\t}}
{orientation_line}\t\t\trenderModelInfo = {{
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
''' + cleanup + reconciliation


def donor_kind_from_block(block):
    """Classify a structural donor without depending on a map key."""
    inherit_match = re.search(r'\binherit\s*=\s*"([^"]+)";', block)
    inherit = inherit_match.group(1) if inherit_match else ""
    model_match = re.search(
        r'renderModelInfo\s*=\s*\{.*?\bmodel\s*=\s*"([^"]+)";',
        block,
        flags=re.DOTALL,
    )
    model = model_match.group(1) if model_match else ""
    if inherit.startswith("progress/codex"):
        return "codex"
    if model.endswith("/question_mark_a.lwo"):
        return "question_mark"
    if inherit.startswith("pickup/extra_life"):
        return "extra_life"
    return inherit.split("/", 1)[0] if inherit else "other"


def resolve_donor_model_override(content, location_block, asset):
    """Resolve the structural donor independently from its replacement slot."""
    if asset.get("strategy") != "donor_model_override":
        raise ValueError("asset is not a donor_model_override")
    replacement_policy = asset.get("replacement_slot_policy")
    replacement_slot = asset.get("replacement_slot", {})
    if replacement_policy == "safe_resident_static_lwo":
        if replacement_slot.get("model_path") != asset.get("model"):
            raise ValueError(
                "replacement slot model_path must match the visual model"
            )
    elif replacement_policy != "native_question_mark":
        raise ValueError("unsupported replacement slot policy")
    donor = asset.get("donor", {})
    selection = donor.get("selection")
    if selection == "per_location_source":
        donor_block = location_block
    elif selection == "named_entity":
        entity_name = donor.get("entity")
        bounds = find_entity_block_bounds(content, entity_name) if entity_name else None
        if bounds is None:
            raise ValueError(f"donor entity not found: {entity_name}")
        donor_block = content[bounds[0]:bounds[1]]
    else:
        raise ValueError(f"unsupported donor selection: {selection}")
    resolved_kind = donor_kind_from_block(donor_block)
    if resolved_kind != donor.get("kind"):
        raise ValueError(
            f"donor kind mismatch: expected {donor.get('kind')}, got {resolved_kind}"
        )
    model_match = re.search(
        r'renderModelInfo\s*=\s*\{.*?\bmodel\s*=\s*"([^"]+)";',
        donor_block,
        flags=re.DOTALL,
    )
    if model_match is None:
        raise ValueError("donor model is missing")
    return model_match.group(1)


def apply_injected_entity_model_override(entity_text, replacement_model):
    """Replace only an injected AP visual's render model."""
    name_match = re.search(r'\bentityDef\s+([^\s{]+)', entity_text)
    entity_name = name_match.group(1) if name_match else ""
    if not entity_name.startswith("ap_location_visual_"):
        raise ValueError("model override is limited to injected AP visual entities")
    pattern = re.compile(
        r'(renderModelInfo\s*=\s*\{.*?\bmodel\s*=\s*")[^"]+(";\s*)',
        flags=re.DOTALL,
    )
    updated, count = pattern.subn(
        lambda match: f"{match.group(1)}{replacement_model}{match.group(2)}",
        entity_text,
        count=1,
    )
    if count != 1:
        raise ValueError(f"injected AP visual model is missing: {entity_name}")
    return updated


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


def remove_sentinel_crystal_source_references(content, source_names):
    """Remove target edges to Sentinel owners omitted from generated output."""
    removed_names = set(source_names)
    blocks = content.split("entity {")
    rewritten = [blocks[0]]
    for block in blocks[1:]:
        target_names = extract_target_names(block)
        retained_targets = [
            target for target in target_names if target not in removed_names
        ]
        if retained_targets != target_names:
            block = replace_targets_block(block, retained_targets)
        rewritten.append("entity {" + block)
    return "".join(rewritten)


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


def build_universal_physical_policy(
    ap_check_id, location_id, block, visual_model=AP_QUESTION_MARK_MODEL, policy=None
):
    """Generate an independent trigger and visual for any generic physical location.

    Preserves original vanilla relay targets for doors, gates, and other world
    events. Independent AP trigger fires vanilla relays, AP_CHECK, then visual
    cleanup in sequence.
    """
    visual_name = f"ap_location_visual_{location_id}"
    cleanup_name = f"ap_remove_location_visual_{location_id}"
    reconciliation_name = f"ap_hide_location_visual_{location_id}"

    policy = policy or {}
    policy.setdefault("preserve_rotation", False)
    configured_position = policy.get("independent_position")
    visual_z_offset = policy.get("independent_visual_z_offset", 1.5)
    if (
        isinstance(visual_z_offset, bool)
        or not isinstance(visual_z_offset, (int, float))
    ):
        raise ValueError("Independent AP visual z offset must be numeric")
    if configured_position is not None:
        position = [
            configured_position[0],
            configured_position[1],
            configured_position[2] + visual_z_offset,
        ]
    else:
        position_block = re.search(r'spawnPosition\s*=\s*\{([^}]*)\}', block)
        if not position_block:
            position = [0.0, 0.0, 0.0]
        else:
            coordinates = []
            for axis in ("x", "y", "z"):
                match = re.search(
                    rf'\b{axis}\s*=\s*([-+0-9.eE]+);', position_block.group(1)
                )
                coordinates.append(float(match.group(1)) if match else 0.0)
            position = [
                coordinates[0],
                coordinates[1],
                coordinates[2] + visual_z_offset,
            ]

    independent_targets = [ap_check_id, cleanup_name]

    think_match = re.search(r'\bthinkComponentDecl\s*=\s*"([^"]+)";', block)
    think_component = think_match.group(1) if think_match else None
    return {
        "independent_ap_trigger": True,
        "independent_targets": independent_targets,
        "independent_size": [5.0, 5.0, 5.0],
        "remove_original": True,
        "independent_visual": {
            "entity_name": visual_name,
            "class": "idProp2",
            "inherit": None,
            "automap_properties_decl": policy.get(
                "independent_automap_properties_decl", "default"
            ),
            "model": visual_model,
            "thinkComponentDecl": policy.get("thinkComponentDecl", "bob_rotate_slow"),
            "position": position,
            "scale": [1.0, 1.0, 1.0],
            "cleanup_entity": cleanup_name,
            "reconciliation_entity": reconciliation_name,
        },
        "preserve_rotation": policy["preserve_rotation"],
        "completion_targets": [cleanup_name],
    }


def resolved_automap_visual_policy(location_id, policy):
    """Resolve packaged presentation names from generator visual policy."""
    if policy.get("no_auto_visual") or policy.get("duplicate_policy") == "native_only":
        return {"classification": "no_visual", "policy": "no_auto_visual"}
    visual = policy.get("independent_visual")
    if visual is None:
        return {
            "classification": "visible_cleanup",
            "presentation_entity": f"ap_location_visual_{location_id}",
            "cleanup_entity": f"ap_remove_location_visual_{location_id}",
            "reconciliation_entity": f"ap_hide_location_visual_{location_id}",
            "policy": "generated_universal",
        }
    if not isinstance(visual, dict):
        raise ValueError(f"location {location_id}: independent_visual must be object")
    presentation = visual.get("entity_name", visual.get("entity"))
    cleanup = visual.get("cleanup_entity")
    reconciliation = f"ap_hide_location_visual_{location_id}"
    if not isinstance(presentation, str) or not isinstance(cleanup, str):
        raise ValueError(f"location {location_id}: independent_visual names must be strings")
    return {
        "classification": "visible_cleanup",
        "presentation_entity": presentation,
        "cleanup_entity": cleanup,
        "reconciliation_entity": reconciliation,
        "policy": "explicit_independent_visual",
    }


def build_independent_targets(block, ap_check_id, policy):
    """Keep only explicit safe vanilla targets, then append one AP check."""
    vanilla_targets = extract_target_names(block)
    drop_targets = set(policy.get("drop_targets", []))
    preserve_targets = policy.get("preserve_targets")
    if preserve_targets is None:
        retained = [target for target in vanilla_targets if target not in drop_targets]
    else:
        preserve_set = set(preserve_targets)
        retained = [target for target in vanilla_targets if target in preserve_set]
    configured = policy.get("independent_targets", [ap_check_id])
    return list(dict.fromkeys([*retained, *configured]))


def generate_automap_location_helper(source_block, location_id, policy=None):
    """Emit the proven targetless idInfo owner for one physical AP marker."""
    policy = policy or {}
    configured_position = policy.get("independent_position")
    if configured_position is not None:
        coordinates = {
            "x": str(configured_position[0]),
            "y": str(configured_position[1]),
            "z": str(configured_position[2]),
        }
    else:
        position_block = re.search(r'spawnPosition\s*=\s*\{([^}]*)\}', source_block)
        if not position_block:
            raise ValueError(f"Automap helper source position is missing for {location_id}")
        position_values = {
            axis: re.search(
                rf'\b{axis}\s*=\s*([-+0-9.eE]+);', position_block.group(1)
            )
            for axis in ("x", "y", "z")
        }
        coordinates = {}
        for axis in ("x", "y", "z"):
            match = position_values[axis]
            coordinates[axis] = match.group(1) if match is not None else "0"
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
				x = {coordinates["x"]};
				y = {coordinates["y"]};
				z = {coordinates["z"]};
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
        target_names.append(f"{LOCATION_NOTIFICATION_PREFIX}{location_id}")
    target_names.append(event_name)
    target_lines = [
        f'\t\t\t\titem[{index}] = "{target_name}";'
        for index, target_name in enumerate(target_names)
    ]

    return f"""entity {{
\tentityDef {ap_check_id} {{
\t\tinherit = "target/relay";
\t\tclass = "idTarget_Count";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tcount = 1;
\t\t\ttargets = {{
\t\t\t\tnum = {len(target_lines)};
{chr(10).join(target_lines)}
\t\t\t}}
{spawn_pos_text}\t\t}}
\t}}
}}
"""


def generate_target_relay(
    ap_check_id, location_id, spawn_pos_text, completion_targets=None,
    include_notification=True,
):
    return generate_event_relay(
        ap_check_id, location_id, spawn_pos_text,
        include_notification=include_notification,
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


def generate_pickup_notification(location_id, placement=None):
    if placement is not None:
        normalize_placement_metadata([placement])
        header_key = placement_sent_key(location_id)
    else:
        header_key = LOCATION_NOTIFICATION_HEADER_KEY
    return build_primitive(
        "location_notification_codex",
        f"{LOCATION_NOTIFICATION_PREFIX}{location_id}",
        {
            "header_key": header_key,
            "subtext_key": location_notification_key(location_id),
        },
    )


PLACEMENT_FIELDS = {
    "location_id", "location_name", "item_id", "item_name",
    "recipient_slot", "recipient_name", "classification", "trap", "local",
}


def normalize_placement_metadata(placement_metadata):
    """Validate immutable scout output before it can affect generated sources."""
    if placement_metadata is None:
        return None
    if not isinstance(placement_metadata, (list, tuple)):
        raise ValueError("placement_metadata must be a list")
    records = []
    seen = set()
    for value in placement_metadata:
        if not isinstance(value, dict) or set(value) != PLACEMENT_FIELDS:
            raise ValueError("placement metadata has invalid fields")
        location_id = value["location_id"]
        if not isinstance(location_id, int) or isinstance(location_id, bool) or location_id <= 0:
            raise ValueError("placement metadata has invalid location_id")
        if location_id in seen:
            raise ValueError(f"placement metadata duplicates location {location_id}")
        seen.add(location_id)
        for field in ("location_name", "item_name", "recipient_name"):
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError(f"placement metadata {field} must be non-empty")
        for field in ("item_id", "recipient_slot", "classification"):
            if not isinstance(value[field], int) or isinstance(value[field], bool):
                raise ValueError(f"placement metadata {field} must be an integer")
        if not isinstance(value["trap"], bool) or not isinstance(value["local"], bool):
            raise ValueError("placement metadata trap/local must be boolean")
        if value["trap"] != bool(value["classification"] & 0b00100):
            raise ValueError(f"placement metadata trap mismatch at {location_id}")
        records.append(dict(value))
    return sorted(records, key=lambda value: value["location_id"])


def location_feedback_policy(location_feedback, ap_check_id):
    if ap_check_id not in location_feedback:
        raise ValueError(f"Missing explicit location feedback policy for {ap_check_id}")
    feedback = location_feedback[ap_check_id]
    if not isinstance(feedback, dict):
        raise ValueError(
            f"Invalid location feedback config for {ap_check_id}"
        )
    policy = feedback.get("policy")
    if policy not in {"vanilla_only", "ap_only", "vanilla_and_ap"}:
        raise ValueError(
            f"Invalid location feedback policy for {ap_check_id}"
        )
    if policy == "vanilla_and_ap" and not feedback.get("justification"):
        raise ValueError(
            f"vanilla_and_ap requires justification for {ap_check_id}"
        )
    return policy


def generate_item_notification(item_id, subtext_key, classification, stage=None, slot=None):
    """Generate the one classification-selected received-item notification."""
    style = notification_style_for_item(item_id, classification)
    entity_name = notification_entity_name(
        item_id, classification, stage=stage, slot=slot
    )
    if style == "major":
        parameters = {
            "header_key": major_notification_key_from_item_key(subtext_key),
        }
    else:
        parameters = {
            "header_key": ITEM_NOTIFICATION_HEADER_KEY,
            "subtext_key": subtext_key,
        }
    return build_primitive(f"item_notification_{style}", entity_name, parameters)


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


def discover_map_group_unlocks(content):
    """Return sorted entity names of all idTarget_MapGroupUnlock blocks in map content."""
    class_marker = 'class = "idTarget_MapGroupUnlock";'
    results = []
    start = 0
    while True:
        pos = content.find(class_marker, start)
        if pos == -1:
            break
        # Walk backwards to find the nearest entityDef declaration
        entity_def_pos = content.rfind("entityDef ", 0, pos)
        if entity_def_pos != -1:
            # Extract the entity name from "entityDef <name> {"
            snippet = content[entity_def_pos:entity_def_pos + 200]
            match = re.match(r'entityDef\s+(\S+)', snippet)
            if match:
                results.append(match.group(1))
        start = pos + len(class_marker)
    return sorted(set(results))


def generate_fast_travel_relay(map_key, fast_travel_coords, vanilla_content):
    """Emit ap_fast_travel_unlock relay + ap_fast_travel_unlock_native primitive.

    The relay activates all native idTarget_MapGroupUnlock entities
    in the map, then activates ap_fast_travel_unlock_native (the actual
    idTarget_FastTravelUnlock). Zero-group maps emit a relay with only
    the native target.
    """
    map_group_unlocks = discover_map_group_unlocks(vanilla_content)
    native_block = build_primitive(
        "fast_travel_unlock",
        "ap_fast_travel_unlock_native",
        fast_travel_coords,
    )
    relay_targets = [*map_group_unlocks, "ap_fast_travel_unlock_native"]
    relay_block = build_primitive(
        "target_count_relay",
        "ap_fast_travel_unlock",
        {"targets": relay_targets},
    )
    return relay_block + native_block


def command_requires_map_side_rpc(command):
    return isinstance(command, str) and bool(command.strip())


def progressive_effect_command(effect):
    """Compile physical item effects and perk effects to native-safe commands."""
    if effect.startswith("weapon/"):
        return f"give {effect}"
    if effect.startswith(("ability_", "equipmentlauncher/", "throwable/", "ammo/", "inventory/")):
        return f"ai_ScriptCmdEnt player1 give {effect}"
    return (
        f"ai_ScriptCmdEnt player1 givePlayerPerk {effect};"
        f"ai_ScriptCmdEnt player1 activatePlayerPerk {effect}"
    )


def generate_physical_pickup_spawn(item_id, command_value):
    """Emit one repeatable native spawner and one concrete Berserk pickup template."""
    if command_value != {
        "type": "physical_pickup_spawn",
        "entity_def": "pickup/powerup/berserk",
    }:
        raise ValueError(f"Unsupported physical pickup definition for item {item_id}")
    spawner_name = f"{RPC_ENTITY_PREFIX}_{item_id}"
    pickup_name = f"{spawner_name}_pickup"
    spawner = build_primitive(
        "physical_pickup_spawn",
        spawner_name,
        {"pickup_entity": pickup_name, "spawn_at": "player1"},
    )
    pickup = f'''entity {{
	entityDef {pickup_name} {{
		inherit = "pickup/powerup/berserk";
		class = "idProp2";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = true;
		disableAIPooling = false;
		edit = {{
			whenToSave = "SGT_CHECKPOINT";
			removeFlag = "RMV_CHECKPOINT_ALLOW_MS";
			flags = {{
				canBecomeDormant = true;
			}}
			renderModelInfo = {{
				contributesToLightProbeGen = false;
				ignoreDesaturate = true;
				fadeVisibilityOver = 400;
				scale = {{
					x = 2;
					y = 2;
					z = 2;
				}}
				model = "art/pickups/powerup/berserker_powerup.lwo";
			}}
			dormancy = {{
				delay = 5;
				distance = 2048;
			}}
			spawn_statIncreases = {{
				num = 1;
				item[0] = {{
					stat = "STAT_ITEMS_SPAWNED";
					increase = 1;
				}}
			}}
			isStatic = false;
			equipOnPickup = true;
			displayPickupMessage = false;
			supportsShowingGui = false;
			lootStyle = "LOOT_TOUCH";
			triggerDef = "trigger/props/pickup_large";
			updateFX = true;
			canBePossessed = true;
			sendNotableItemTelemetryEvent = true;
			clipModelInfo = {{
				contentsFilter = {{
					monsterClip = false;
				}}
			}}
			fxDecl = "gameplay/powerups/berserk";
			useableComponentDecl = "propstatuseffect/berserk";
			thinkComponentDecl = "bobthink";
			sound_spawn = "play_berserk_pickup_loop";
			sound_stop = "stop_berserk_pickup_loop";
			pickup_statIncreases = {{
				num = 2;
				item[0] = {{
					stat = "STAT_ITEMS_COLLECTED";
					increase = 1;
				}}
				item[1] = {{
					stat = "STAT_POWERUPS";
					increase = 1;
				}}
			}}
			spawnPosition = {{
				x = 0;
				y = 0;
				z = 0;
			}}
		}}
	}}
}}
'''
    return spawner + pickup


def generate_rpc_command_entities(
    items_dict,
    item_names=None,
    item_classifications=None,
    receipt_feedback=None,
    enable_notifications=False,
):
    """Generate ap_rpc_v3_* effects and independent receipt notifications.
    
    item_names: dict[int, str] — item_id -> canonical name (from replay_policies)
    When enable_notifications is True, also generates ap_notify_item_<id> entities.
    """
    validate_primitive_registry()
    receipt_feedback = receipt_feedback or {}
    unsupported_feedback = {
        item_id: feedback
        for item_id, feedback in receipt_feedback.items()
        if feedback not in SUPPORTED_RECEIPT_FEEDBACK
    }
    if unsupported_feedback:
        raise ValueError(f"unsupported item receipt feedback: {unsupported_feedback}")
    blocks = []
    required_entities = []
    for item_id, command_value in items_dict.items():
        if isinstance(command_value, dict):
            command_type = command_value.get("type")
            if command_type == "no_op":
                continue
            if command_type == "transient_effect":
                continue
            if command_type == "physical_pickup_spawn":
                blocks.append(generate_physical_pickup_spawn(item_id, command_value))
                continue
            if command_type in {"progressive_perk", "progressive_item"}:
                perks = command_value.get("perks", [])
                if not perks:
                    raise ValueError(
                        f"Progressive perk item {item_id} has no perk stages"
                    )
                for stage, perk in enumerate(perks):
                    effects = [perk] if isinstance(perk, str) else perk
                    if not isinstance(effects, list) or not effects or any(not isinstance(effect, str) for effect in effects):
                        raise ValueError(f"Progressive perk item {item_id} has invalid stage effects")
                    for index, effect in enumerate(effects):
                        entity_name = (
                            f"{RPC_ENTITY_PREFIX}_{item_id}_{stage}"
                            if isinstance(perk, str)
                            else f"{RPC_ENTITY_PREFIX}_{item_id}_{stage}_{index}"
                        )
                        blocks.append(build_primitive(
                            "target_command", entity_name,
                            {"command": progressive_effect_command(effect)},
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

    # Only non-no_op items generate independent notification entities.
    if item_names and enable_notifications:
        if item_classifications is None:
            raise ValueError("item notifications require packaged classifications")
        for item_id, command_value in items_dict.items():
            is_no_op = isinstance(command_value, dict) and command_value.get("type") == "no_op"
            if is_no_op:
                continue
            item_id_int = int(item_id)
            feedback = receipt_feedback.get(
                item_id_int, receipt_feedback.get(str(item_id_int), AP_RECEIPT_FEEDBACK)
            )
            if feedback != AP_RECEIPT_FEEDBACK:
                continue
            name = item_names.get(item_id_int)
            if not name:
                raise ValueError(f"Item {item_id} has no name in item_names; notification requires it")
            if item_id_int not in item_classifications:
                raise ValueError(
                    f"Item {item_id} has no packaged classification"
                )
            classification = item_classifications[item_id_int]
            
            progressive_stage_count = progressive_notification_stage_count(
                item_id_int, command_value
            )
            if progressive_stage_count is not None:
                for stage in range(progressive_stage_count):
                    subtext_key = notification_key(item_id_int, command_value, stage=stage)
                    for slot in ("a", "b"):
                        blocks.append(generate_item_notification(
                            item_id_int, subtext_key, classification,
                            stage=stage, slot=slot,
                        ))
            else:
                subtext_key = notification_key(item_id_int, command_value)
                for slot in ("a", "b"):
                    blocks.append(generate_item_notification(
                        item_id_int, subtext_key, classification, slot=slot
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

def generate_system_command_entities(map_key="", runtime_map=""):
    cmd_text = "condump ap_telemetry_ready.txt"
    if map_key and runtime_map:
        cmd_text = (
            f"echo AP_ACTIVE_MAP_V1 map_key={map_key} runtime_map={runtime_map} "
            f"marker=AP_MAP_START_{map_key.upper()}; "
            f"condump ap_active_map_{map_key}.txt; condump ap_telemetry_ready.txt"
        )
    return f"""entity {{
	entityDef ap_deathlink {{
		class = "idTarget_Damage";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			damageDecl = "damage/triggerhurt/triggerhurt1000_instagib";
			radiusDamage = 0.0;
			damageActivator = true;
		}}
	}}
}}
entity {{
	entityDef ap_rpc_auto_enable {{
		class = "idTarget_Command";
		expandInheritance = false;
		poolCount = 0;
		poolGranularity = 2;
		networkReplicated = false;
		disableAIPooling = false;
		edit = {{
			commandText = "{cmd_text}";
		}}
	}}
}}
"""


def generate_ap_lifecycle_entity(map_key):
    """Return one unlayered AP-owned first-think lifecycle entrypoint."""
    if not isinstance(map_key, str) or not re.fullmatch(r"[a-z0-9_]+", map_key):
        raise ValueError(f"invalid AP lifecycle map key: {map_key!r}")
    return f'''entity {{
\tentityDef {AP_LIFECYCLE_ENTITY_PREFIX}{map_key} {{
\t\tinherit = "target/level_activate";
\t\tclass = "idTarget_FirstThinkActivate";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tflags = {{
\t\t\t\tnoFlood = true;
\t\t\t}}
\t\t\ttargets = {{
\t\t\t\tnum = 1;
\t\t\t\titem[0] = "ap_rpc_auto_enable";
\t\t\t}}
\t\t}}
\t}}
}}
'''


def generate_context_marker_overlay(map_key, runtime_map, items_dict=None):
    """Generate technical marker, lifecycle, and persistent RPC entities."""
    if not isinstance(map_key, str) or not re.fullmatch(r"[a-z0-9_]+", map_key):
        raise ValueError(f"invalid context marker map key: {map_key!r}")
    if not isinstance(runtime_map, str) or not runtime_map:
        raise ValueError("context marker runtime map is required")
    if items_dict is None:
        item_path = Path(__file__).resolve().parents[2] / "data" / "items.json"
        document = json.loads(item_path.read_text(encoding="utf-8"))
        items_dict = {int(item_id): definition for item_id, definition in document.items()}
    return (
        generate_rpc_command_entities(items_dict)
        + generate_system_command_entities(map_key=map_key, runtime_map=runtime_map)
        + generate_ap_lifecycle_entity(map_key)
    )


def validate_ap_lifecycle_entity(content, map_key):
    """Enforce exactly one unlayered AP lifecycle target in generated content."""
    name = f"{AP_LIFECYCLE_ENTITY_PREFIX}{map_key}"
    if content.count(f"entityDef {name} {{") != 1:
        raise ValueError(f"AP lifecycle entity count mismatch: {name}")
    bounds = find_entity_block_bounds(content, name)
    if bounds is None:
        raise ValueError(f"AP lifecycle entity missing: {name}")
    block = content[bounds[0]:bounds[1]]
    if re.search(r"\blayers\s*=", block):
        raise ValueError(f"AP lifecycle entity must be unlayered: {name}")
    if extract_target_names(block) != ["ap_rpc_auto_enable"]:
        raise ValueError(f"AP lifecycle target mismatch: {name}")


def generate_bootstrap_entities():
    """Return map bootstrap entities."""
    return ""

def load_item_notification_policies(
    policy_path: str | Path = "data/item_replay_policies.json",
):
    """Load validated item names and receipt feedback from canonical policies."""
    root = Path(__file__).resolve().parents[2]
    path = Path(policy_path).expanduser()
    if not path.is_absolute():
        path = root / path
    definitions = json.loads(
        (root / "data" / "items.json").read_text(encoding="utf-8")
    )
    policies = load_policy_registry(
        path,
        {int(item_id): definition for item_id, definition in definitions.items()},
    )
    return (
        {item_id: policy.name for item_id, policy in policies.items()},
        {
            item_id: policy.receipt_feedback
            for item_id, policy in policies.items()
        },
    )


def load_explicit_location_feedback(
    map_key, configured, declared_checks=(),
):
    """Merge the reviewed AP-only records with per-map exceptional owners."""
    path = Path(__file__).resolve().parents[2] / "data/location_feedback_policies.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported location feedback policy schema")
    explicit = {
        key: value for key, value in configured.items()
        if key != "default_policy"
    }
    default_policy = configured.get("default_policy")
    if default_policy:
        for ap_check in declared_checks:
            explicit.setdefault(ap_check, {"policy": default_policy})
    for ap_check in document.get("policies", {}).get(map_key, []):
        if ap_check in explicit:
            raise ValueError(f"duplicate explicit location feedback policy for {ap_check}")
        explicit[ap_check] = {"policy": "ap_only"}
    return explicit


def generate_map(
    input_file,
    output_file,
    config_file,
    manifest_file,
    items_dict,
    item_names=None,
    item_classifications=None,
    receipt_feedback=None,
    enable_notifications=True,
    placement_metadata=None,
):
    with open(config_file, encoding="utf-8") as f:
        level_config = json.load(f)
    config_path = Path(config_file)
    descriptor_path = config_path.with_name("descriptor.json")
    if descriptor_path.is_file():
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        level_config.setdefault("runtime_map", descriptor.get("runtime_map", ""))
        level_config.setdefault("map_key", descriptor.get("key", ""))
    package_assets = config_path.with_name("assets.json")
    if config_path.name == "locations.json" and package_assets.is_file():
        asset_document = json.loads(package_assets.read_text(encoding="utf-8"))
        level_config = {
            **level_config,
            "assets": asset_document.get("assets", []),
            "default_visual_asset": asset_document.get("default_visual_asset"),
        }
    if item_classifications is None:
        item_classifications = load_item_classifications(
            Path(__file__).resolve().parents[2]
            / "data"
            / "item_classifications.json"
        )
    map_key = level_config.get("map_key")
    if not isinstance(map_key, str):
        raise ValueError("map config requires string map_key")
    preserve_rotation = level_config.get("preserve_rotation", False)
    if not isinstance(preserve_rotation, bool):
        raise ValueError("map config preserve_rotation must be boolean")
    preserve_visual_layers = level_config.get("preserve_visual_layers", True)
    if not isinstance(preserve_visual_layers, bool):
        raise ValueError("map config preserve_visual_layers must be boolean")
    fast_travel_path = Path(__file__).resolve().parents[2] / "data" / "fast_travel.json"
    fast_travel = json.loads(fast_travel_path.read_text(encoding="utf-8"))
    canonical_visual = canonical_ap_visual_for_map(map_key)
    config_entities = level_config.get("entities", {})
    target_policies = level_config.get("target_policies", {})
    asset_specs = {
        asset["key"]: asset for asset in level_config.get("assets", [])
    }
    default_visual_asset = level_config.get("default_visual_asset")
    if canonical_visual:
        asset_specs[canonical_visual["key"]] = canonical_visual
        default_visual_asset = canonical_visual["key"]
    if default_visual_asset and default_visual_asset not in asset_specs:
        raise ValueError(f"Unknown default_visual_asset: {default_visual_asset}")
    default_visual_model = (
        asset_specs[default_visual_asset]["model"]
        if default_visual_asset
        else AP_QUESTION_MARK_MODEL
    )
    neutralize_pickups = level_config.get("neutralize_pickups", [])
    target_removals = level_config.get("target_removals", {})
    remove_entities = level_config.get("remove_entities", [])
    visual_child_removals = level_config.get("visual_child_removals", [])
    neutralize_entity_references = level_config.get("neutralize_entity_references", [])
    secret_encounters = level_config.get("secret_encounters", [])
    declared_checks = (
        *config_entities,
        *(entry["ap_check"] for entry in secret_encounters),
    )
    placement_metadata = normalize_placement_metadata(placement_metadata)
    if placement_metadata is not None:
        placement_by_id = {
            record["location_id"]: record for record in placement_metadata
        }
        declared_location_ids = set(config_entities.values()) | {
            entry["location_id"] for entry in secret_encounters
        }
        missing_placements = sorted(declared_location_ids - set(placement_by_id))
        if missing_placements:
            raise ValueError(
                "placement metadata is incomplete for generated map: "
                + ", ".join(str(value) for value in missing_placements)
            )
    else:
        placement_by_id = {}
    location_feedback = load_explicit_location_feedback(
        level_config.get("map_key"),
        level_config.get("location_feedback", {}),
        declared_checks,
    )
    manifest_data = {}
    sentinel_check_ids = {}

    source_metadata = validate_source_file(input_file, output_file)
    content = suppress_vanilla_secret_found_ui(source_metadata["content"])
    for contract in level_config.get("inline_currency_removals", []):
        content = remove_inline_currency_transaction(content, contract)
    validate_target_policies(config_entities, target_policies, content)
    for entity_name, policy in target_policies.items():
        gate_relay = policy.get("gate_relay")
        source_bounds = find_entity_block_bounds(content, entity_name)
        sentinel_source = (
            source_bounds is not None
            and is_sentinel_crystal_source(
                content[source_bounds[0]:source_bounds[1]]
            )
        )
        if gate_relay and not sentinel_source:
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

    content = remove_visual_child_entities(content, visual_child_removals)

    for _entity_name, policy in target_policies.items():
        cleanup = policy.get("checkpoint_cleanup")
        if cleanup:
            content = apply_checkpoint_cleanup_contract(content, cleanup)

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
    content = remove_balanced_entity_blocks(content, "ap_fast_travel_unlock_native")
    content = remove_balanced_entity_blocks(content, "ap_fast_travel_unlock")
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"ap_logic_[^"]+";', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"AP_CHECK_[^"]+";', '', content, flags=re.IGNORECASE)

    blocks = content.split("entity {")
    sentinel_crystal_pairs = find_sentinel_crystal_pairs(blocks[1:])
    sentinel_crystal_top_names = set(sentinel_crystal_pairs.values())
    missing_crystal_checks = sorted(
        f"AP_CHECK_{source_name.upper()}"
        for source_name in sentinel_crystal_pairs
        if f"AP_CHECK_{source_name.upper()}" not in config_entities
    )
    if missing_crystal_checks:
        raise ValueError(
            "Every discovered Sentinel Crystal requires an AP check: "
            + ", ".join(missing_crystal_checks)
        )
    new_blocks = [blocks[0]]

    modified_count = 0
    for block in blocks[1:]:
        # Normalized content assigns each declared entity its generation strategy.
        declared_match = re.search(r'entityDef\s+([^\s{]+)', block)
        declared_entity_name = declared_match.group(1).strip() if declared_match else ""
        if declared_entity_name in sentinel_crystal_top_names:
            continue
        declared_ap_check = (
            f"AP_CHECK_{declared_entity_name.upper()}"
            if declared_match else ""
        )
        if declared_ap_check in config_entities:
            name_match = re.search(r'entityDef\s+([^\s{]+)', block)
            if not name_match:
                new_blocks.append("entity {" + block)
                continue

            entity_name = name_match.group(1).strip()
            ap_check_id = f"AP_CHECK_{entity_name.upper()}"

            manifest_data[ap_check_id] = config_entities[ap_check_id]



            if "edit = {" in block:
                location_id = config_entities[ap_check_id]
                feedback_policy = location_feedback_policy(
                    location_feedback, ap_check_id
                )
                include_ap_feedback = feedback_policy != "vanilla_only"
                target_policy = copy.deepcopy(target_policies.get(entity_name, {}))
                if not target_policy:
                    target_policy = build_universal_physical_policy(
                        ap_check_id,
                        location_id,
                        block,
                        default_visual_model,
                        policy={
                            "preserve_rotation": preserve_rotation,
                            "preserve_visual_layers": preserve_visual_layers,
                        },
                    )
                elif is_sentinel_crystal_source(block) and not target_policy.get(
                    "independent_ap_trigger"
                ):
                    generic = build_universal_physical_policy(
                        ap_check_id,
                        location_id,
                        block,
                        default_visual_model,
                        policy={
                            "preserve_rotation": preserve_rotation,
                            "preserve_visual_layers": preserve_visual_layers,
                        },
                    )
                    generic.update(target_policy)
                    target_policy = generic
                target_policy.setdefault("preserve_rotation", preserve_rotation)
                target_policy.setdefault(
                    "preserve_visual_layers", preserve_visual_layers
                )
                if is_sentinel_crystal_source(block):
                    target_policy.setdefault("independent_ap_trigger", True)
                    target_policy.setdefault("remove_original", True)
                    if entity_name != SENTINEL_CRYSTAL_BRIDGE_OWNER:
                        target_policy["preserve_targets"] = []
                visual = target_policy.get("independent_visual")
                if visual:
                    asset_key = visual.get("asset") or default_visual_asset
                    if asset_key:
                        if asset_key not in asset_specs:
                            raise ValueError(
                                f"Unknown independent_visual asset: {asset_key}"
                            )
                        visual_asset = asset_specs[asset_key]
                        if visual_asset["strategy"] == "donor_model_override":
                            visual["model"] = resolve_donor_model_override(
                                content, block, visual_asset
                            )
                            visual["_model_override"] = visual_asset["model"]
                        else:
                            visual["model"] = visual_asset["model"]
                if target_policy.get("duplicate_policy") == "native_only":
                    target_policy["no_auto_visual"] = True
                if (
                    "native_entity_contract" in target_policy
                    and not target_policy.get("no_auto_visual")
                    and not target_policy.get("independent_visual")
                ):
                    universal = build_universal_physical_policy(
                        ap_check_id, location_id, block, default_visual_model,
                        policy=target_policy,
                    )
                    target_policy["independent_visual"] = universal["independent_visual"]
                    target_policy.setdefault("completion_targets", []).extend(
                        target for target in universal["completion_targets"]
                        if target not in target_policy["completion_targets"]
                    )
                target_policy = bind_parent_from_source(target_policy, block)
                if target_policy.get("independent_visual"):
                    target_policy["independent_visual"].setdefault(
                        "reconciliation_entity",
                        f"ap_hide_location_visual_{location_id}",
                    )

                audit_preserved_target_graph(content, entity_name, target_policy)
                
                if not target_policy.get("no_auto_automap_helper"):
                    new_blocks.append(
                        generate_automap_location_helper(block, location_id, policy=target_policy)
                    )
                if "native_entity_contract" in target_policy and not target_policy.get("independent_ap_trigger"):
                    native_contract = target_policy["native_entity_contract"]
                    native = apply_native_entity_contract(block, native_contract)
                    native = add_ap_check_target(
                        native,
                        entity_name,
                        ap_check_id,
                        {
                            "original_targets": native_contract.get(
                                "original_targets", extract_target_names(block)
                            ),
                            "drop_targets": target_policy.get("drop_targets", []),
                            "preserve_targets": target_policy.get("preserve_targets"),
                        },
                    )
                    new_blocks.append("entity {" + native)
                    new_blocks.append(generate_target_relay(
                        ap_check_id,
                        location_id,
                        "",
                        completion_targets=target_policy.get("completion_targets"),
                        include_notification=include_ap_feedback,
                    ))
                    visual = generate_inert_location_visual(block, target_policy)
                    if visual:
                        new_blocks.append(visual)
                    if include_ap_feedback:
                        new_blocks.append(
                            generate_pickup_notification(location_id, placement_by_id.get(location_id))
                        )
                    new_blocks.append(generate_check_event(location_id))
                    modified_count += 1
                    continue

                if target_policy.get("independent_ap_trigger"):
                    sentinel_source = is_sentinel_crystal_source(block)
                    sentinel_top_name = sentinel_crystal_pairs.get(entity_name)
                    if sentinel_source:
                        sentinel_check_ids[entity_name] = ap_check_id
                        target_policy["preserve_targets"] = []
                        if entity_name == SENTINEL_CRYSTAL_BRIDGE_OWNER:
                            target_policy["independent_targets"] = [
                                ap_check_id,
                                SENTINEL_CRYSTAL_OBJECTIVE_TARGET,
                                SENTINEL_CRYSTAL_BRIDGE_CONTINUATION_TARGET,
                            ]
                        else:
                            target_policy["independent_targets"] = [ap_check_id]
                    elif sentinel_top_name:
                        target_policy["preserve_targets"] = [
                            target
                            for target in target_policy.get("preserve_targets", [])
                            if target != sentinel_top_name
                        ]
                    if not sentinel_source and (
                        target_policy.get("remove_original", False)
                        or (
                            not target_policy.get("independent_visual")
                            and not target_policy.get("no_auto_visual")
                        )
                    ):
                        target_policy["independent_targets"] = build_independent_targets(
                            block, ap_check_id, target_policy
                        )

                    manifest_data[ap_check_id] = location_id
                    if not target_policy.get("independent_visual") and not target_policy.get("no_auto_visual"):
                        universal = build_universal_physical_policy(
                            ap_check_id, location_id, block, default_visual_model, policy=target_policy
                        )
                        target_policy["independent_visual"] = universal["independent_visual"]
                        if universal["independent_visual"]["cleanup_entity"] not in target_policy.get("completion_targets", []):
                            target_policy.setdefault("completion_targets", []).append(universal["independent_visual"]["cleanup_entity"])
                    if target_policy.get("independent_visual"):
                        cleanup = target_policy["independent_visual"].get("cleanup_entity")
                        if cleanup and not sentinel_source and cleanup not in target_policy.setdefault(
                            "independent_targets", [ap_check_id]
                        ):
                            target_policy["independent_targets"].append(cleanup)
                        if not sentinel_source:
                            target_policy["completion_targets"] = [
                                target
                                for target in target_policy.get("completion_targets", [])
                                if target != cleanup
                            ]
                    visual_policy = target_policy.get("independent_visual")
                    if visual_policy:
                        visual_policy.setdefault(
                            "reconciliation_entity",
                            f"ap_hide_location_visual_{location_id}",
                        )
                    if visual_policy and "_model_override" not in visual_policy:
                        asset_key = (
                            visual_policy.get("asset") or default_visual_asset
                        )
                        visual_asset = asset_specs.get(asset_key) if asset_key else None
                        if (
                            visual_asset
                            and visual_asset["strategy"] == "donor_model_override"
                        ):
                            visual_policy["model"] = resolve_donor_model_override(
                                content, block, visual_asset
                            )
                            visual_policy["_model_override"] = visual_asset["model"]
                    if not target_policy.get("remove_original", False):
                        if "native_entity_contract" in target_policy:
                            new_blocks.append(
                                "entity {" + apply_native_entity_contract(block, target_policy["native_entity_contract"])
                            )
                        else:
                            new_blocks.append(
                                "entity {" + neutralize_conditional_pickup_block(
                                    block,
                                    preserve_original_visual=target_policy.get(
                                        "preserve_original_visual", False
                                    ),
                                )
                            )
                    new_blocks.append(
                        generate_independent_pickup_trigger(entity_name, ap_check_id, block, target_policy)
                    )
                    visual = generate_inert_location_visual(block, target_policy)
                    if visual:
                        replacement_model = target_policy[
                            "independent_visual"
                        ].get("_model_override")
                        if replacement_model:
                            visual = apply_injected_entity_model_override(
                                visual, replacement_model
                            )
                        new_blocks.append(visual)
                    secondary_ap_check_id = f"{ap_check_id}_B"
                    secondary_location_id = config_entities.get(secondary_ap_check_id)
                    completion_targets = list(target_policy.get("completion_targets") or [])
                    if secondary_location_id is not None:
                        manifest_data[secondary_ap_check_id] = secondary_location_id
                        completion_targets.append(f"{EVENT_ENTITY_PREFIX}{secondary_location_id}")
                        if include_ap_feedback:
                            completion_targets.append(f"{LOCATION_NOTIFICATION_PREFIX}{secondary_location_id}")
                            new_blocks.append(
                                generate_pickup_notification(
                                    secondary_location_id, placement_by_id.get(secondary_location_id)
                                )
                            )
                        new_blocks.append(generate_check_event(secondary_location_id))
                        if not target_policy.get("no_auto_automap_helper"):
                            new_blocks.append(
                                generate_automap_location_helper(block, secondary_location_id, policy=target_policy)
                            )
                        secondary_universal = build_universal_physical_policy(
                            secondary_ap_check_id,
                            secondary_location_id,
                            block,
                            default_visual_model,
                            policy=target_policy,
                        )
                        secondary_visual = generate_inert_location_visual(block, secondary_universal)
                        if secondary_visual:
                            new_blocks.append(secondary_visual)
                        completion_targets.append(
                            secondary_universal["independent_visual"]["cleanup_entity"]
                        )
                    new_blocks.append(generate_target_relay(
                        ap_check_id,
                        location_id,
                        "",
                        completion_targets=completion_targets,
                        include_notification=include_ap_feedback,
                    ))
                    if include_ap_feedback:
                        new_blocks.append(
                            generate_pickup_notification(location_id, placement_by_id.get(location_id))
                        )
                    new_blocks.append(generate_check_event(location_id))
                    modified_count += 1
                    if secondary_location_id is not None:
                        modified_count += 1
                    continue
                else:
                    raise ValueError(f"Legacy non-independent physical check logic hit for {entity_name}")

        new_blocks.append("entity {" + block)

    map_content = remove_sentinel_crystal_source_references(
        "".join(new_blocks),
        sentinel_crystal_pairs,
    )
    assert_sentinel_crystal_transform(
        map_content,
        sentinel_crystal_pairs,
        manifest_data,
        sentinel_check_ids,
    )
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
        automap_owner = secret_hook.get("automap_owner")
        if automap_owner:
            if len(re.findall(rf"\bentityDef\s+{re.escape(automap_owner)}\b", map_content)) != 1:
                raise ValueError(
                    f"Secret encounter automap owner is missing or duplicated: {automap_owner}"
                )
            automap_bounds = find_entity_block_bounds(map_content, automap_owner)
            if automap_bounds is None:
                raise ValueError(
                    f"Secret encounter automap owner not found: {automap_owner}"
                )
            automap_block = map_content[automap_bounds[0]:automap_bounds[1]]
            secret_blocks.append(
                generate_automap_location_helper(automap_block, location_id)
            )
            map_content = (
                map_content[:automap_bounds[0]]
                + map_content[automap_bounds[1]:]
            )
        map_content = inject_secret_encounter_completion(
            map_content,
            manager_name,
            ap_check_id,
            expected_last_event_index,
        )
        manifest_data[ap_check_id] = location_id
        feedback_policy = location_feedback_policy(
            location_feedback, ap_check_id
        )
        include_ap_feedback = feedback_policy != "vanilla_only"
        secret_blocks.append(
            generate_event_relay(
                ap_check_id,
                location_id,
                "",
                include_notification=include_ap_feedback,
            )
        )
        if include_ap_feedback:
            secret_blocks.append(
                generate_pickup_notification(location_id, placement_by_id.get(location_id))
            )
        secret_blocks.append(generate_check_event(location_id))
        modified_count += 1

    final_content = (
        map_content
        + "\n"
        + "".join(secret_blocks)
        + generate_rpc_command_entities(
            items_dict,
            item_names=item_names,
            item_classifications=item_classifications,
            receipt_feedback=receipt_feedback,
            enable_notifications=enable_notifications,
        )
        + generate_bootstrap_entities()
        + generate_system_command_entities(map_key=map_key, runtime_map=level_config.get("runtime_map", ""))
        + generate_ap_lifecycle_entity(map_key)
        + (generate_fast_travel_relay(map_key, fast_travel["maps"][map_key], source_metadata["content"]) if map_key in fast_travel["maps"] else "")
    )
    validate_ap_lifecycle_entity(final_content, map_key)
    assert_no_weapon_mastery_token_currency(final_content, f"Generated map {map_key}")
    if canonical_visual and modified_count:
        assert_canonical_ap_visuals(final_content, map_key, canonical_visual)

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
    if placement_metadata is not None:
        placement_path = Path(manifest_file).with_suffix(".placements.json")
        placement_path.write_text(
            json.dumps(placement_metadata, indent=4, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(f"Successfully generated {modified_count} GLOBAL AP Targets using idTrigger mutation!")
    print(f"Manifest saved to {manifest_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Doom Eternal AP Map Generator")
    parser.add_argument("--input", required=True, help="Input .entities file")
    parser.add_argument("--output", required=True, help="Output .entities file")
    parser.add_argument("--config", required=True, help="Level configuration JSON")
    parser.add_argument("--manifest", required=True, help="Output manifest JSON")
    parser.add_argument("--items", default="data/items.json", help="Items JSON containing commands")
    parser.add_argument("--item-names", default="data/item_replay_policies.json", help="Item names JSON")
    parser.add_argument("--disable-item-notifications", action="store_true", help="Omit item notification UI primitives for local diagnostics")

    args = parser.parse_args()

    with open(args.items, encoding="utf-8") as f:
        items_dict = json.load(f)

    item_names, receipt_feedback = load_item_notification_policies(args.item_names)

    generate_map(
        args.input, args.output, args.config, args.manifest, items_dict, 
        item_names=item_names,
        receipt_feedback=receipt_feedback,
        enable_notifications=not args.disable_item_notifications,
    )
