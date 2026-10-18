"""Materialize frozen encounter operations without choosing enemies or waves.

Schema 1 contains map_key, source_sha256 and operations. Each operation selects
manager {entity, sha256}, script_index, event_index, event_def, event_sha256 and
from {species, spawn_type, spawner}. Its sole wave contains one instance with
species, spawn_type, ai, ai_name, spawner_name and four integer attributes.
Entity references contain map_key, entity and sha256. Hashes use UTF-8 text;
entity and eventCall hashes strip surrounding whitespace. species_profiles
supplies spawn_type, optional proved spawn_types and permitted inherits.
Schema 2 also accepts finite spawnAI groups. Each instance pins origin_spawner;
multiple waves pin the immediately following ANY/zero wait as barrier, give each
wave label and equal min_spawn_stagger/max_spawn_stagger, and each instance group_name.
Schema 3 staged operations can pin a private native reserve and its commit/finish triggers.
Schema 4 also stages single and staggered sources within finite, control-free phases.
Schema 5 combines reinforcements by phase and accepts removed spawn calls.
Schema 6 supports finite ambient emissions. Schema 7 retains native source
tracking, typed clearance gates, kill-count waits and additional manager scripts.
"""

import hashlib
import json
import math
import re
from functools import cache
from pathlib import Path

from doom_eap.content.enemy_options import SPECIES_IDS
from doom_eap.content.enemy_species import AIRBORNE_SPECIES, AMMUNITION_SPECIES, enemy_spawn_mask, enemy_tracking_suffix, selected_spawn_type, validate_enemy_speed_percent
from tools.maps.ap_map_generator import find_entity_block_bounds, find_matching_brace
from tools.maps.encounter_catalog import ENTITY_HEADER, entity_records, list_items, walk_values
from tools.maps.encounter_source_definitions import SourceDefinitions
from tools.maps.encounter_spawn_admission import source_group_points, spawn_position
from tools.maps.enemy_attribute_baselines import resolve_enemy_attribute_baselines
from tools.maps.enemy_attributes import _CLONE_NAME, _MISSING, _get, _member, _render, _set, materialize_enemy_attributes

GENERAL_SPAWN_STATES = {None, "AIOVERRIDE_NONE", "AIOVERRIDE_DEFAULT",
                        "AIOVERRIDE_FORCE_AWARENESS_OF_PLAYER", "AIOVERRIDE_FORCE_AWARENESS_OF_ENTITY"}


@cache
def _shared_entrance_models():
    path = Path(__file__).resolve().parents[2] / 'content/catalog/enemy_entrances.json'
    catalog = json.loads(path.read_bytes())
    if catalog.get('schema_version')!=1 or catalog.get('qualification')!='native_shared_entrance_animations':
        raise ValueError('Unqualified shared entrance animations')
    result = {}
    for row in catalog['records']:
        identity = row['state'],row['animation']
        if identity in result or not all(isinstance(value,str) and value for value in identity):
            raise ValueError('Invalid shared entrance animation')
        models = set()
        for model in row['models']:
            pair = model['inherit'],model['model']
            if any(not isinstance(value,str) or not value for value in pair):
                raise ValueError('Invalid shared entrance model')
            models.add(pair)
        if len(models)<2:
            raise ValueError('Shared entrance needs two qualified models')
        result[identity] = models
    return result


def spawn_entrance_compatible(point, original, replacement):
    entrance = point["edit"].get("spawnEditable", {})
    state = entrance.get("aiStateOverride")
    if state in GENERAL_SPAWN_STATES:
        return True
    if state in {"AIOVERRIDE_TELEPORT", "AIOVERRIDE_SPAWN_ANIM_IDLE"} and not entrance.get("spawnAnim"):
        return True
    if original is None:
        return False
    if original["inherit"] != replacement["inherit"]:
        if state=='AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL':
            return False
        models = _shared_entrance_models().get((state,entrance.get('spawnAnim')),set())
        pairs = [(actor['inherit'],actor['edit'].get('renderModelInfo',{}).get('model')) for actor in (original,replacement)]
        if any(not isinstance(model,str) or (inherit,model) not in models for inherit,model in pairs):
            return False
    if state in {"AIOVERRIDE_TELEPORT", "AIOVERRIDE_SPAWN_ANIM_IDLE", "AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL"}:
        return True
    return state in {"AIOVERRIDE_PLAY_ENTRANCE_ANIMATION", "AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION"} and bool(entrance.get("spawnAnim"))


def _entrance_has_effects(point, actor, *, traversal=False):
    edit = point['edit']
    definitions = {value['name'] for value in list_items(edit.get('entityDefs')) if set(value)=={'name'}}
    if set(list_items(edit.get('targets'))) - definitions - ({actor['name']} if actor else set()):
        return True
    entrance = edit.get('spawnEditable', {})
    if (edit.get('portal') or entrance.get('spawnAt') or entrance.get('initialTargetOverride')
            or edit.get('spawnEditableShared', {}).get('groupName')):
        return True
    for path, value in walk_values(edit):
        if path.startswith('targets.') or traversal and path.startswith(('additionalTargets.', 'spawnEditable.additionalTargets.')):
            continue
        if re.search(r'targets(?:\.|$)|trigger|callback|onspawn', path, re.I) and value:
            return True
    return False


def replacement_spawn_point(point, original, replacement, alternatives=(), *, record=None):
    if spawn_entrance_compatible(point, original, replacement):
        return point
    entrance = point['edit'].get('spawnEditable', {})
    traversal = entrance.get('aiStateOverride') == 'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL'
    if traversal:
        targets = list_items(entrance.get('additionalTargets', point['edit'].get('additionalTargets')))
        if not targets or record is None:
            return None
        for name in targets:
            node = record(name)
            if (node is None or node['class'] != 'idInfoTraversal' or not node['inherit'].startswith('info/traversal/')
                    or set(node['edit']) - {'traversalAnim', 'monsterType', 'snapToGround', 'spawnPosition',
                                           'spawnOrientation', 'traversalEndpoint'}
                    or re.search(r'wall|break|breach|door|sync|destroy|smash|kick', node['edit'].get('traversalAnim', ''), re.I)):
                return None
    generic = GENERAL_SPAWN_STATES - {'AIOVERRIDE_FORCE_AWARENESS_OF_ENTITY'}
    if (entrance.get('aiStateOverride') not in generic | {'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_SPAWN_ANIM_IDLE',
            'AIOVERRIDE_FORCE_TELEPORT_TO_TRAVERSAL',
            'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION', 'AIOVERRIDE_PLAY_ENTRANCE_ANIMATION_WITH_CORRECTION'}
            or re.search(r'wall|break|breach|door|sync|destroy|smash|kick', entrance.get('spawnAnim', ''), re.I)):
        return None
    spawn_position(point['edit'])
    if _entrance_has_effects(point, original, traversal=traversal):
        return None
    for candidate, actor in alternatives:
        entrance = candidate['edit'].get('spawnEditable', {})
        if (candidate['class'] != 'idTarget_Spawn' or candidate['inherit'] != 'target/spawn'
                or candidate['layers'] != point['layers'] or candidate['edit'].get('flags', {}).get('touchesTriggers', False)
                or _entrance_has_effects(candidate, actor)
                or entrance.get('aiStateOverride') not in generic
                and not (entrance.get('aiStateOverride') in {'AIOVERRIDE_TELEPORT', 'AIOVERRIDE_SPAWN_ANIM_IDLE'}
                         and not entrance.get('spawnAnim'))):
            continue
        spawn_position(candidate['edit'])
        return candidate
    return None


def earlier_phase_sources(operations, operation, events, replacement_species):
    if operation.get('barrier') is None:
        return
    controls = {'spawnSingleAI', 'spawnAI', 'staggeredAISpawn', 'wait', 'waitStaggeredSpawnComplete',
                'waitAIRemaining', 'waitKillCount', 'waitAIHealthLevel', 'setMusicState', 'makeAIAwareOfPlayer',
                'setFactionRelation', 'clearFactionOverrides', 'designerComment', 'damageAI'}
    for previous in operations:
        if (previous['manager'] != operation['manager'] or previous['script_index'] != operation['script_index']
                or previous.get('barrier') != operation['barrier']
                or previous['event_index'] >= operation['event_index']
                or (previous['from']['species'] in AIRBORNE_SPECIES) != (replacement_species in AIRBORNE_SPECIES)):
            continue
        if all(event['eventCall']['eventDef'] in controls
               for event in events[previous['event_index'] + 1:operation['event_index']]):
            yield previous


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keys(value, keys, context):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"Unsupported frozen {context} fields")


def _edit_span(block):
    match = re.search(r"\bedit\s*=\s*\{", block)
    if match is None:
        raise ValueError("Entity has no edit object")
    return match.end() - 1, find_matching_brace(block, match.end() - 1)


def _value_span(block, path):
    opening, end = _edit_span(block)
    start = opening + 1
    end -= 1
    for name in path:
        member = _member(block[start:end], name)
        if member is None:
            raise ValueError("Frozen event path is absent: " + ".".join(path))
        relative_start, relative_end, is_object = member
        if not is_object:
            raise ValueError("Frozen event path is not an object: " + ".".join(path))
        opening = start + relative_start
        end = find_matching_brace(block, opening)
        start = opening + 1
    return opening, end


def _edit(block, path, value):
    opening, end = _edit_span(block)
    body = _set(block[opening + 1:end - 1], path, value)
    return block[:opening + 1] + body + block[end - 1:]


def _rename(block, name):
    header = ENTITY_HEADER.search(block)
    return block[:header.start(1)] + name + block[header.end(1):]


def _call(name, args):
    return _render({"eventDef": name, "args": {"num": len(args), **{
        f"item[{index}]": value for index, value in enumerate(args)}}}, 4)


def _layers(block, layers):
    value = "layers { " + " ".join(_render(layer, 0) for layer in layers) + " }"
    header = ENTITY_HEADER.search(block)
    match = re.search(r"\blayers\s*\{", block[:header.start()])
    if match is not None:
        end = find_matching_brace(block, match.end() - 1)
        return block[:match.start()] + value + block[end:]
    opening = block.index("{") + 1
    return block[:opening] + "\n" + value + block[opening:]


def _replace_events(body, events, changes):
    replacements = []
    next_index = 0
    for index in range(len(events)):
        start, end, _ = _member(body, f"item[{index}]")
        field = re.search(rf"\bitem\[{index}\]\s*=\s*$", body[:start])
        indent = re.search(r"[\t ]*$", body[:field.start()]).group()
        values = changes.get(index)
        if values is None:
            values = [body[start:end]]
        else:
            values = [_render(value, 2) if isinstance(value, dict) else "{ eventCall = " + value + " }" for value in values]
        replacement = ("\n" + indent).join(
            f"item[{next_index + offset}] = " + value for offset, value in enumerate(values))
        replacements.append((field.start(), end, replacement))
        next_index += len(values)
    for start, end, replacement in reversed(replacements):
        body = body[:start] + replacement + body[end:]
    return _set(body, ("num",), next_index)


def encounter_traversal_flags(manager, lookup):
    flags = set()
    for name in list_items(manager["edit"].get("userFlagTriggers")):
        trigger = lookup(name)
        if trigger is None:
            continue
        edit = trigger["edit"]
        if (trigger["class"] != "idEncounterTrigger_RaiseUserFlag" or trigger["inherit"] != "encounter/trigger/user_flag"
                or set(edit) - {"clipModelInfo", "renderModelInfo", "spawnPosition", "triggerOnce", "triggerFirst", "userFlag", "whenToSave"}
                or not isinstance(edit.get("userFlag"), str) or not edit["userFlag"]
                or not isinstance(edit.get("clipModelInfo", {}).get("clipModelName"), str)
                or not edit["clipModelInfo"]["clipModelName"]):
            continue
        flags.add(edit["userFlag"])
    return flags


def encounter_effect_targets(events, lookup):
    cache = {}
    def ordinary_door(name):
        door = lookup(name)
        return (door is not None and door['class']=='idInteractable_Obstacle'
                and door['inherit'].startswith('interact/doors/')
                and door['edit'].get('interaction',{}).get('interactionGraph')=='interactables/door'
                and not any(value and path!='touchData.triggerDef'
                            and re.search(r'target|callback|script|event|activat|trigger',path,re.I)
                            for path,value in walk_values(door['edit'])))
    def qualified(name, visiting):
        if not isinstance(name, str) or name in visiting:
            return False
        if name in cache:
            return cache[name]
        record = lookup(name)
        if record is None:
            return False
        edit = record["edit"]
        common = {"flags", "spawnPosition", "spawnOrientation", "renderModelInfo", "dormancy", "whenToSave", "targets"}
        if record["class"] == "idParticleEmitter" and record["inherit"] == "func/emitter":
            valid = (not set(edit) - common - {"fadeIn", "fadeOut", "startOff", "cycleTrigger", "particleSystem"}
                     and not list_items(edit.get("targets")) and isinstance(edit.get("particleSystem"), str) and bool(edit["particleSystem"]))
        elif record['class']=='idTarget_InteractionAction' and record['inherit']=='target/interact_action':
            targets = list_items(edit.get('targets'))
            valid = (not set(edit)-common-{'clipModelInfo','action'} and edit.get('action') in {'IA_DOOR_OPEN','IA_DOOR_CLOSE'}
                     and bool(targets) and all(isinstance(target,str) and ordinary_door(target) for target in targets))
        elif record["class"] == "idTarget_Count" and record["inherit"] == "target/relay":
            targets = list_items(edit.get("targets"))
            valid = (not set(edit) - common - {"networkSerializeTransforms", "count", "repeat"}
                     and type(edit.get("count")) is int and edit["count"] > 0 and bool(targets)
                     and all(qualified(target, visiting | {name}) for target in targets))
        else:
            valid = False
        cache[name] = valid
        return valid
    targets = {args[0]["entity"] for item in events if item.get("eventCall", {}).get("eventDef") == "activateTarget"
               and (args := list_items(item["eventCall"].get("args"))) and set(args[0]) == {"entity"} and isinstance(args[0]["entity"], str)}
    return {name for name in targets if qualified(name, set())}


def encounter_combat_groupings(events,lookup):
    names={args[0]['entity'] for item in events if item.get('eventCall',{}).get('eventDef')=='activateCombatGrouping'
           and (args:=list_items(item['eventCall'].get('args'))) and set(args[0])=={'entity'} and isinstance(args[0]['entity'],str)}
    common={'flags','spawnPosition','spawnOrientation','renderModelInfo','dormancy','whenToSave'}
    qualified=set()
    for name in names:
        group=lookup(name)
        if (group is None or group['class']!='idCombatGrouping' or group['inherit']!='encounter/combat_role/manager'
                or set(group['edit'])-common-{'targets','rolePriorities'}):
            continue
        try:
            roles=list_items(group['edit'].get('rolePriorities'))
            targets=list_items(group['edit'].get('targets'))
        except (KeyError,TypeError,ValueError):
            continue
        if (any(not isinstance(role,str) or re.fullmatch(r'ROLE_[A-Z_]+',role) is None for role in roles)
                or any(not isinstance(target,str) for target in targets)):
            continue
        hints=[lookup(target) for target in targets]
        if all(hint is not None and hint['class']=='idAICombatHint' and hint['inherit']=='encounter/combat_role/point'
               and not set(hint['edit'])-common-{'combatHintType','primitive','minWeight','delay','hintClass'} for hint in hints):
            qualified.add(name)
    return qualified


def _actor_control_overlaps(call, spawn_type, label):
    args = list_items(call['args'])
    offset = int(call['eventDef']=='damageAI')
    mask = enemy_spawn_mask(args[offset]['eEncounterSpawnType_t'])
    selected = bool(mask & enemy_spawn_mask(spawn_type))
    if call['eventDef']=='forceAIToFlee' and args[3]['bool']:
        return selected
    return selected and (not args[offset+1]['string'] or not label or args[offset+1]['string']==label)


def _awareness_args_valid(args):
    return (len(args)==4 and all(set(arg)=={'bool'} and type(arg['bool']) is bool for arg in args[:2])
            and set(args[2])=={'string'} and isinstance(args[2]['string'],str)
            and set(args[3])=={'bool'} and type(args[3]['bool']) is bool)


def finite_phase_event(call, *, tracked=False, user_flags=(), effect_targets=(), combat_groupings=(), condition=False, cohort=None):
    kind = call.get("eventDef")
    if kind in ("spawnSingleAI", "spawnAI", "staggeredAISpawn", "wait", "waitStaggeredSpawnComplete"):
        return True
    if not tracked:
        return False
    if kind == "waitKillCount":
        return True
    args = list_items(call.get("args"))
    if kind == 'spawnArchvile':
        return (cohort is not None and not enemy_spawn_mask(cohort[0]) & enemy_spawn_mask('ENCOUNTER_SPAWN_ARCHVILE')
                and len(args)==4
                and all(set(arg)=={'entity'} and isinstance(arg['entity'],str) and bool(arg['entity']) for arg in args[:2])
                and all(set(arg)=={'string'} and isinstance(arg['string'],str) for arg in args[2:]))
    if kind == 'spawnPossessedAI':
        if (cohort is None or len(args)!=7
                or any(set(args[i])!={'eEncounterSpawnType_t'} for i in (0,4))
                or any(set(args[i])!={'entity'} or not isinstance(args[i]['entity'],str) or not args[i]['entity'] for i in (1,3))
                or any(set(args[i])!={'string'} or not isinstance(args[i]['string'],str) for i in (2,5))
                or args[6]!={'bool':True} or type(args[6].get('bool')) is not bool):
            return False
        host = enemy_spawn_mask(args[0]['eEncounterSpawnType_t'])
        allowed = enemy_spawn_mask(args[4]['eEncounterSpawnType_t'])
        return (not enemy_spawn_mask(cohort[0]) & (host | enemy_spawn_mask('ENCOUNTER_SPAWN_SPIRIT'))
                and not allowed & enemy_spawn_mask(' '.join(AMMUNITION_SPECIES.values())))
    if kind == 'setNextScriptIndex':
        return len(args)==1 and set(args[0])=={'int'} and type(args[0]['int']) is int and -1<=args[0]['int']<=0x7fffffff
    if kind in ('damageAI','removeAI','forceAIToFlee'):
        offset = int(kind=='damageAI')
        if (cohort is None or len(args)!={'damageAI':3,'removeAI':2,'forceAIToFlee':4}[kind]
                or set(args[offset])!={'eEncounterSpawnType_t'} or set(args[offset+1])!={'string'}
                or not isinstance(args[offset+1]['string'],str)):
            return False
        if kind=='damageAI' and (set(args[0])!={'decl'} or not isinstance(args[0]['decl'],dict)
                or set(args[0]['decl'])!={'damage'} or not isinstance(args[0]['decl']['damage'],str) or not args[0]['decl']['damage']):
            return False
        mask=enemy_spawn_mask(args[offset]['eEncounterSpawnType_t'])
        if kind=='removeAI' and mask & 0x840000000:
            # these native removal masks also clean up summoned actors.
            return False
        if kind=='forceAIToFlee':
            return (set(args[2])=={'float'} and type(args[2]['float']) in (int,float)
                    and math.isfinite(args[2]['float']) and 0<=args[2]['float']<=3.4028234663852886e38
                    and set(args[3])=={'bool'} and type(args[3]['bool']) is bool
                    and not mask & enemy_spawn_mask(cohort[0]))
        return kind=='damageAI' or not _actor_control_overlaps(call,*cohort)
    if kind == "makeAIAwareOfPlayer":
        if not _awareness_args_valid(args):
            return False
        return (args[:2] == [{'bool':True},{'bool':True}] or args[:2] == [{'bool':False},{'bool':False}]
                or cohort is not None and bool(args[2]['string']) and args[2]['string'] != cohort[1])
    if kind == 'designerComment':
        return (len(args)==2 and set(args[0])=={'string'} and isinstance(args[0]['string'],str)
                and args[1]=={'bool':False} and type(args[1]['bool']) is bool)
    if kind=='activateCombatGrouping':
        return (len(args)==3 and set(args[0])=={'entity'} and isinstance(args[0]['entity'],str) and args[0]['entity'] in combat_groupings
                and set(args[1])=={'string'} and isinstance(args[1]['string'],str)
                and set(args[2])=={'float'} and type(args[2]['float']) in (int,float)
                and math.isfinite(args[2]['float']) and 0<=args[2]['float']<=3.4028234663852886e38)
    if kind in ("forceChargeOnAllAI","clearFactionOverrides"):
        return not args
    if kind=='setFactionRelation':
        if len(args)!=4 or any(set(args[index])!={'eEncounterSpawnType_t'} for index in (0,2)):
            return False
        for index in (0,2):enemy_spawn_mask(args[index]['eEncounterSpawnType_t'])
        return (set(args[1])=={'string'} and isinstance(args[1]['string'],str) and set(args[3])=={'socialEmotion_t'}
                and isinstance(args[3]['socialEmotion_t'],str) and re.fullmatch(r'EMOTION_[A-Z_]+',args[3]['socialEmotion_t']) is not None)
    if kind == "maintainAICount":
        if len(args) != 8 or set(args[0]) != {"eEncounterSpawnType_t"}:
            return False
        enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
        return (all(set(args[index]) == {"int"} and type(args[index]["int"]) is int for index in (1,2,4))
                and args[1]["int"] >= 0 and args[4]["int"] >= 0
                and all(set(args[index]) == {"float"} and type(args[index]["float"]) in (int,float)
                    and math.isfinite(args[index]["float"]) for index in (3,7))
                and 0 <= args[3]["float"] <= args[7]["float"] <= 3.4028234663852886e38
                and set(args[5]) == {"entity"} and isinstance(args[5]["entity"],str) and bool(args[5]["entity"])
                and set(args[6]) == {"string"} and isinstance(args[6]["string"],str))
    if kind == "waitAIHealthLevel":
        if len(args) != 4 or set(args[0]) != {"eEncounterSpawnType_t"}:
            return False
        enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
        return (set(args[1]) == {"float"} and type(args[1]["float"]) in (int, float) and 0 <= args[1]["float"] <= 1
                and set(args[2]) == {"string"} and isinstance(args[2]["string"], str)
                and set(args[3]) == {"bool"} and type(args[3]["bool"]) is bool)
    if kind == "activateTarget":
        return (len(args) == 2 and set(args[0]) == {"entity"} and isinstance(args[0]["entity"], str) and args[0]["entity"] in effect_targets
                and set(args[1]) == {"string"} and isinstance(args[1]["string"], str))
    if kind == "waitForEventFlag":
        return (len(args) == 4 and args[0] == {"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_USER_EVENT"}
                and set(args[1]) == {"string"} and isinstance(args[1]["string"], str) and args[1]["string"] in user_flags
                and all(set(arg) == {"bool"} and type(arg["bool"]) is bool for arg in args[2:]))
    if kind in ("waitAIRemaining", "stopMaintainingAICount",'clearCombatRoles'):
        if not args or set(args[0]) != {"eEncounterSpawnType_t"}:
            return False
        # native role clearing uses INVALID to include every actor type.
        if kind!='clearCombatRoles' or args[0]['eEncounterSpawnType_t']!='ENCOUNTER_SPAWN_INVALID':
            enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
        if kind in ("stopMaintainingAICount",'clearCombatRoles'):
            return len(args) == 2 and set(args[1]) == {"string"} and isinstance(args[1]["string"], str)
        return (len(args) == 4 and set(args[1]) == {"int"} and type(args[1]["int"]) is int
                and args[1]["int"] >= 0 and set(args[2]) == {"string"} and isinstance(args[2]["string"], str)
                and set(args[3]) == {"bool"} and type(args[3]["bool"]) is bool)
    return (kind == "setMusicState" and len(args) == 3 and set(args[0]) == {"entity"}
            and isinstance(args[0]["entity"], str) and set(args[1]) == {"decl"}
            and isinstance(args[1]["decl"], dict) and set(args[1]["decl"]) == {"soundstate"}
            and isinstance(args[1]["decl"]["soundstate"], str)
            and set(args[2]) == {"string"} and isinstance(args[2]["string"], str))


def maintenance_active(events, event_index, spawn_type, label):
    active = set()
    for item in events[:event_index]:
        call = item["eventCall"]
        args = list_items(call.get("args"))
        if call["eventDef"] == "syncMaintainingAICounts":
            return True
        if call["eventDef"] == "maintainAICount":
            active.add((enemy_spawn_mask(args[0]["eEncounterSpawnType_t"]), args[6]["string"]))
        elif call["eventDef"] == "stopMaintainingAICount":
            mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
            stopped_label = args[1]["string"]
            active = {(source, cohort) for source, cohort in active if not source & mask or stopped_label and cohort != stopped_label}
    mask = enemy_spawn_mask(spawn_type)
    return any(source & mask and (not cohort or cohort == label) for source, cohort in active)


def maintenance_overlaps(events, event_index, barrier_index, spawn_type, label):
    if maintenance_active(events,event_index,spawn_type,label):
        return True
    source = enemy_spawn_mask(spawn_type)
    for item in events[event_index+1:barrier_index]:
        call = item["eventCall"]
        if call["eventDef"] == "syncMaintainingAICounts":
            return True
        if call["eventDef"] == "maintainAICount":
            args = list_items(call.get("args"))
            if (not finite_phase_event(call,tracked=True) or args[1]["int"] > 0
                    and enemy_spawn_mask(args[0]["eEncounterSpawnType_t"]) & source
                    and (not args[6]["string"] or args[6]["string"] == label)):
                return True
    return False


def other_script_maintenance_overlaps(scripts, script_index, spawn_type, label):
    source = enemy_spawn_mask(spawn_type)
    for index, script in enumerate(scripts):
        if index == script_index:
            continue
        for event in list_items(script['events']):
            call = event['eventCall']
            if call['eventDef'] == 'syncMaintainingAICounts':
                return True
            if call['eventDef'] == 'maintainAICount':
                args = list_items(call.get('args'))
                if (not finite_phase_event(call, tracked=True) or args[1]['int'] > 0
                        and enemy_spawn_mask(args[0]['eEncounterSpawnType_t']) & source
                        and (not args[6]['string'] or args[6]['string'] == label)):
                    return True
    return False


def compound_condition_indices(events):
    members = set()
    pending = []
    for index, wrapped in enumerate(events):
        if pending:
            members.add(index)
            pending[-1] -= 1
            if not pending[-1]:
                pending.pop()
        call = wrapped.get("eventCall", {})
        if call.get("eventDef") != "waitMulitpleConditions":
            continue
        args = list_items(call.get("args"))
        if (len(args) != 3 or set(args[0]) != {"int"} or type(args[0]["int"]) is not int or args[0]["int"] <= 0
                or set(args[1]) != {"encounterLogicOperator_t"} or args[1]["encounterLogicOperator_t"] not in
                {"ENCOUNTER_LOGICAL_OP_AND", "ENCOUNTER_LOGICAL_OP_OR"}
                or set(args[2]) != {"bool"} or type(args[2]["bool"]) is not bool):
            raise ValueError("Compound condition header is invalid")
        pending.append(args[0]["int"])
    if pending:
        raise ValueError("Compound condition members are incomplete")
    return members


def compound_population_gate(events, index, spawn_type, label):
    compound_condition_indices(events)
    source = enemy_spawn_mask(spawn_type)
    def evaluate(cursor):
        call = events[cursor]["eventCall"]
        args = list_items(call.get("args"))
        if call["eventDef"] == "waitMulitpleConditions":
            cursor += 1
            values = []
            for _ in range(args[0]["int"]):
                value, cursor = evaluate(cursor)
                values.append(value)
            # AND needs one death requirement; OR needs one in every choice.
            return (any(values) if args[1]["encounterLogicOperator_t"] == "ENCOUNTER_LOGICAL_OP_AND" else all(values)), cursor
        clears = (call["eventDef"] == "waitAIRemaining" and finite_phase_event(call,tracked=True,condition=True)
                  and args[1]["int"] == 0 and not source & ~enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
                  and (not args[2]["string"] or args[2]["string"] == label))
        return clears, cursor+1
    if events[index]["eventCall"]["eventDef"] != "waitMulitpleConditions":
        raise ValueError("Expected a compound population gate")
    return evaluate(index)


def finite_phase_events(events, first_index, barrier_index, *, tracked=False, user_flags=(), effect_targets=(), combat_groupings=(), cohort=None):
    members = compound_condition_indices(events) if tracked else set()
    conditions = {"wait", "waitKillCount", "waitAIHealthLevel", "waitAIRemaining", "waitStaggeredSpawnComplete", "waitForEventFlag"}
    for index in range(first_index, barrier_index):
        call = events[index].get("eventCall", {})
        kind = call.get("eventDef")
        if tracked and kind == "waitMulitpleConditions":
            continue
        if index in members and kind not in conditions:
            return False
        if not finite_phase_event(call, tracked=tracked, user_flags=user_flags, effect_targets=effect_targets,
                                  combat_groupings=combat_groupings,condition=index in members,cohort=cohort):
            return False
    return True


def native_ammunition_sources(events, first_index, barrier_index, replacements, *, effect_targets=(), user_flags=(), combat_groupings=()):
    reliable = enemy_spawn_mask(" ".join(AMMUNITION_SPECIES.values()))
    conditions = compound_condition_indices(events)
    def source_label(index):
        source = events[index]['eventCall']
        return list_items(source['args'])[2 if source['eventDef']=='spawnSingleAI' else 3]['string']
    def cleared_before(index, before):
        label = source_label(index)
        mask = enemy_spawn_mask(replacements[index])
        for cursor in range(index+1,before):
            if cursor in conditions:
                continue
            call = events[cursor]['eventCall']
            values = list_items(call.get('args'))
            if call['eventDef']=='waitMulitpleConditions':
                cleared,end = compound_population_gate(events,cursor,replacements[index],label)
                if cleared and end<=before:
                    return True
            elif (call['eventDef']=='waitAIRemaining' and finite_phase_event(call,tracked=True)
                    and values[1]=={'int':0} and not mask & ~enemy_spawn_mask(values[0]['eEncounterSpawnType_t'])
                    and (not values[2]['string'] or values[2]['string']==label)):
                return True
        return False
    sources = []
    for index, wrapped in enumerate(events[:first_index]):
        call = wrapped["eventCall"]
        args = list_items(call.get("args"))
        kind = call["eventDef"]
        if (kind not in {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "maintainAICount"}
                or index in replacements or any(i<index and not cleared_before(i,index) for i in replacements)
                or set(wrapped) != {"eventCall"}):
            continue
        mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
        label = args[6 if kind == "maintainAICount" else 2 if kind == "spawnSingleAI" else 3]["string"]
        if mask & ~reliable or kind != "spawnSingleAI" and (type(args[1].get("int")) is not int or args[1]["int"] <= 0):
            continue
        if kind == "maintainAICount" and (any(i>index and enemy_spawn_mask(value) & mask
                and (not label or source_label(i)==label) for i,value in replacements.items())
                or not maintenance_active(events, barrier_index, args[0]["eEncounterSpawnType_t"], label)
                or any(item["eventCall"]["eventDef"] == "stopMaintainingAICount"
                    and enemy_spawn_mask((stop := list_items(item["eventCall"]["args"]))[0]["eEncounterSpawnType_t"]) & mask
                    and (not stop[1]["string"] or stop[1]["string"] == label) for item in events[index+1:barrier_index])):
            continue
        available = True
        for item in events[first_index:barrier_index]:
            later_call = item['eventCall']
            if later_call['eventDef'] in ('damageAI','removeAI','forceAIToFlee'):
                if (not finite_phase_event(later_call,tracked=True,cohort=(args[0]['eEncounterSpawnType_t'],label))
                        or _actor_control_overlaps(later_call,args[0]['eEncounterSpawnType_t'],label)):
                    available = False
                    break
        if not available:
            continue
        creation_kind = kind
        emitted = kind != "staggeredAISpawn"
        for later in range(index + 1, first_index):
            kind = events[later]["eventCall"]["eventDef"]
            values = list_items(events[later]["eventCall"].get("args"))
            if kind == "waitMulitpleConditions":
                compound_condition_indices(events)
                continue
            if kind == "waitStaggeredSpawnComplete" and len(values) == 4 and values[1] == {"int": 0}:
                emitted |= not mask & ~enemy_spawn_mask(values[0]["eEncounterSpawnType_t"]) and (not values[2]["string"] or values[2]["string"] == label)
            if creation_kind != "maintainAICount" and emitted and kind == "waitForEventFlag" and finite_phase_event(
                    events[later]["eventCall"], tracked=True, user_flags=user_flags):
                break
            if kind in ('removeAI','damageAI','forceAIToFlee'):
                if (not finite_phase_event(events[later]['eventCall'],tracked=True,cohort=(args[0]['eEncounterSpawnType_t'],label))
                        or _actor_control_overlaps(events[later]['eventCall'],args[0]['eEncounterSpawnType_t'],label)):
                    available = False
                    break
            elif kind == "activateTarget" and finite_phase_event(events[later]["eventCall"], tracked=True, effect_targets=effect_targets):
                continue
            elif kind in ("forceChargeOnAllAI",'activateCombatGrouping','clearCombatRoles') and finite_phase_event(
                    events[later]["eventCall"],tracked=True,combat_groupings=combat_groupings):
                continue
            elif kind not in {"spawnSingleAI", "spawnAI", "staggeredAISpawn", "maintainAICount", "stopMaintainingAICount",
                    "wait", "waitKillCount", "waitAIRemaining", "waitAIHealthLevel", "waitStaggeredSpawnComplete", "waitForEventFlag",
                    "setMusicState", "makeAIAwareOfPlayer", "setFactionRelation", "clearFactionOverrides", "designerComment", "setNextScriptIndex"}:
                available = False
                break
        if available:
            sources.append(index)
    return sources


def finite_population_cleared(events, barrier_index, *, user_flags=(), effect_targets=(), combat_groupings=()):
    live = []
    pending = []
    maintained = []
    conditions = compound_condition_indices(events)
    for index, item in enumerate(events[:barrier_index + 1]):
        call = item["eventCall"]
        kind = call["eventDef"]
        args = list_items(call.get("args"))
        if kind == "waitMulitpleConditions" or index in conditions:
            if not finite_phase_events(events, index, index+1, tracked=True, user_flags=user_flags,
                                       effect_targets=effect_targets,combat_groupings=combat_groupings):
                return False
            continue
        if kind in ("spawnSingleAI", "spawnAI", "staggeredAISpawn"):
            if kind != "spawnSingleAI" and (type(args[1].get("int")) is not int or args[1]["int"] <= 0):
                return False
            cohort = (enemy_spawn_mask(args[0]["eEncounterSpawnType_t"]), args[2 if kind == "spawnSingleAI" else 3]["string"])
            live.append(cohort)
            if kind == "staggeredAISpawn":
                pending.append(cohort)
        elif kind == "maintainAICount":
            if len(args) != 8 or type(args[1].get("int")) is not int or args[1]["int"] < 0:
                return False
            if args[1]["int"]:
                cohort = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"]), args[6]["string"]
                maintained.append(cohort)
                live.append(cohort)
        elif kind == "stopMaintainingAICount" and finite_phase_event(call, tracked=True):
            mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
            label = args[1]["string"]
            maintained = [(source, cohort) for source, cohort in maintained
                          if not source & mask or label and cohort != label]
        elif kind == "waitStaggeredSpawnComplete":
            if len(args) == 4 and type(args[1].get("int")) is int and args[1]["int"] == 0:
                mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
                label = args[2]["string"]
                pending = [(source, cohort) for source, cohort in pending if source & ~mask or (label and cohort != label)]
        elif kind == "waitAIRemaining":
            if len(args) != 4 or type(args[1].get("int")) is not int or args[1]["int"] < 0:
                return False
            if args[1]["int"]:
                continue
            mask = enemy_spawn_mask(args[0]["eEncounterSpawnType_t"])
            label = args[2]["string"]
            # A temporarily empty cohort can still have pending native emissions.
            if any(source & mask and (not label or cohort == label) for source, cohort in [*pending, *maintained]):
                return False
            remaining = [(source, cohort) for source, cohort in live if source & ~mask or (label and cohort != label)]
            if index == barrier_index:
                return bool(live) and not remaining
            live = remaining
        elif kind=='makeAIAwareOfPlayer' and _awareness_args_valid(args):
            continue
        elif kind in ("setMusicState", "waitAIHealthLevel", "forceChargeOnAllAI", "setFactionRelation", "clearFactionOverrides", "designerComment", "setNextScriptIndex") and finite_phase_event(call, tracked=True):
            continue
        elif kind == "activateTarget" and finite_phase_event(call, tracked=True, effect_targets=effect_targets):
            continue
        elif kind in ('activateCombatGrouping','clearCombatRoles') and finite_phase_event(call,tracked=True,combat_groupings=combat_groupings):
            continue
        elif kind == "waitForEventFlag" and finite_phase_event(call, tracked=True, user_flags=user_flags):
            continue
        elif (kind == "waitForEventFlag" and not live and not pending and not maintained and len(args) == 4
              and args[0] == {"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_COMMIT"}
              and args[1] == {"string": ""} and all(set(arg) == {"bool"} and type(arg["bool"]) is bool for arg in args[2:])):
            continue
        elif kind not in ("wait", "waitStaggeredSpawnComplete", "waitKillCount"):
            return False
    return False


def materialize_frozen_encounters(source_text, plan, source_maps, *, species_profiles, source_registry=None, source_root=None):
    """Return a complete map only after every frozen operation passes validation.

    Schema 1 supports one instance per spawnSingleAI. Schema 2 expands finite
    spawnAI groups and stages waves only before an immediately following barrier.
    Schema 3 optionally reserves native ZombieT1 in a private manager with queued cleanup.
    Inherited values require a source-pinned baseline; foreign linked entities
    need separate qualification. Inline base_properties is unsupported.
    """
    _keys(plan, ("schema", "map_key", "source_sha256", "operations"), "plan")
    if type(plan["schema"]) is not int or plan["schema"] not in (1, 2, 3, 4, 5, 6, 7):
        raise ValueError("Unsupported frozen plan schema")
    map_key = plan["map_key"]
    if not isinstance(map_key, str) or text_sha256(source_text) != plan["source_sha256"]:
        raise ValueError("Frozen source hash mismatch")
    if not isinstance(plan["operations"], list):
        raise ValueError("Frozen operations must be a list")
    if map_key in source_maps and source_maps[map_key] != source_text:
        raise ValueError("Target map differs from frozen source map")
    if not plan["operations"]:
        return source_text
    source_definitions = SourceDefinitions(source_registry, source_root)
    maps = {**source_maps, map_key: source_text}
    names = {}
    cache = {}
    attribute_bases = {}

    def entity_names(key):
        if key not in names:
            headers = list(ENTITY_HEADER.finditer(maps[key]))
            names[key] = {header.group(1) for header in headers}
            if len(headers) != len(names[key]):
                raise ValueError(f"Duplicate entity name in source map: {key}")
        return names[key]

    def source_entity(key, name):
        if key not in maps or name not in entity_names(key):
            raise ValueError(f"Frozen source entity is absent: {key}/{name}")
        if (key, name) not in cache:
            bounds = find_entity_block_bounds(maps[key], name)
            block = maps[key][bounds[0]:bounds[1]]
            record = next(entity_records(block, include_names={name}))
            cache[key, name] = block, record, bounds
        return cache[key, name]

    def entity(reference, class_name):
        _keys(reference, ("map_key", "entity", "sha256"), "entity reference")
        key, name = reference["map_key"], reference["entity"]
        block, record, bounds = source_entity(key, name)
        if text_sha256(block.strip()) != reference["sha256"]:
            raise ValueError(f"Frozen entity hash mismatch: {key}/{name}")
        if record["class"] != class_name:
            raise ValueError(f"Frozen entity class mismatch: {key}/{name}")
        return block, record, bounds

    def species(value, actor, *, source=False):
        canonical = value["species"]
        profile = species_profiles.get(canonical)
        if canonical not in SPECIES_IDS or not isinstance(profile, dict):
            raise ValueError(f"Unknown canonical species: {canonical}")
        identity_matches=actor["inherit"] in profile.get("inherits", ())
        if source and not identity_matches:
            identity_matches=source_definitions.species_for_definition(actor['inherit'])==canonical
        if value["spawn_type"] not in profile.get("spawn_types", (profile.get("spawn_type"),)) or not identity_matches:
            raise ValueError(f"Frozen species/profile/template mismatch: {canonical}")

    def source_record(name):
        return source_entity(map_key, name)[1]

    def source_point_actor(point, spawn_type, manager, group=None, *, slot=False):
        if slot:
            return source_definitions.source_slot(point,spawn_type,manager['edit'].get('aiTypeDefAssignments'),
                source_record,group=group,group_names=source_group_names,context_layers=manager['layers'])[1]
        name = source_definitions.select_source_point(point, spawn_type, manager['edit'].get('aiTypeDefAssignments'),
            source_record, group=group, group_names=source_group_names, context_layers=manager['layers'])
        actor = source_record(name)
        if actor['class'] != 'idAI2':
            raise ValueError('Origin spawner does not reference idAI2')
        return actor

    def group_points(group, manager):
        key = group['name'], tuple(manager['layers'])
        if key not in admitted_source_points:
            admitted_source_points[key] = source_group_points(group['name'], source_point_names, source_group_names,
                                                              source_record, manager['layers'])
        return admitted_source_points[key]

    def group_entrance_points(group, spawn_type, manager):
        members = list_items(group['edit'].get('spawners'))
        if not members or list_items(group['edit'].get('targetSpawnSubGroups')):
            members = group_points(group, manager)
        for name in members:
            candidate = source_record(name)
            yield candidate, source_point_actor(candidate, spawn_type, manager, group,slot=plan['schema']>=7)

    def earlier_entrance_points(operation, events, replacement_species, manager):
        for previous in earlier_phase_sources(plan['operations'], operation, events, replacement_species):
            source_type = selected_spawn_type(previous['from'])
            if previous['event_def'] == 'spawnSingleAI':
                _, point, _ = entity(previous['from']['spawner'], 'idTarget_Spawn')
                points = ((point, source_point_actor(point, source_type, manager,slot=plan['schema']>=7)),)
            else:
                _, group, _ = entity(previous['from']['spawner'], 'idTargetSpawnGroup')
                points = group_entrance_points(group, source_type, manager)
            for point, actor in points:
                if actor is not None and enemy_spawn_mask(previous['from']['spawn_type'])!=enemy_spawn_mask(source_type):
                    species({**previous['from'], 'spawn_type': source_type}, actor, source=True)
                yield point, actor

    original_names = entity_names(map_key)
    source_group_names = set()
    source_point_names = []
    admitted_source_points = {}
    for group in entity_records(source_text):
        if group['class']=='idTarget_Spawn':
            source_point_names.append(group['name'])
        if group["class"] != "idTargetSpawnGroup":
            continue
        source_group_names.add(group["name"])
        if group["inherit"] not in ("encounter/spawn_group/zone", "encounter/spawn_group/cylinder"):
            raise ValueError("Unqualified source spawn group defaults")
        if group["edit"].get("exclusive", False) and group['edit'].get('includeTouchingSpawners',True):
            raise ValueError("Source exclusive spawn group needs overlap qualification: " + group["name"])
    generated_names = set()
    selected_events = set()
    replacements = []
    clones = []
    scripts = {}
    phase_waves = {}
    phase_queues = {}
    phase_supplies = {}
    compound_barriers = {}
    source_ready_flags = {}
    traversal_flags = {}
    effect_targets = {}
    combat_grouping_targets = {}
    wave_labels = set()
    reserve_tokens = set()

    def reserve_recipe(reserve, manager, manager_record, origin, operation):
        _keys(reserve, ("commit_template", "finish_template", "manager_name", "commit_name", "finish_name",
            "label", "completion_flag", "desired_count", "min_spawn_delay", "max_spawn_delay",
            *(("supply_source",) if isinstance(reserve, dict) and "supply_source" in reserve else ())), "reserve")
        supply = reserve.get("supply_source")
        if supply:
            _keys(supply, ("species", "spawn_type", "ai", "spawners"), "supply source")
            if supply["species"] not in AMMUNITION_SPECIES:
                raise ValueError("Native reserve needs a normal ammunition supplier")
            if not isinstance(supply["spawners"], list) or not supply["spawners"]:
                raise ValueError("Native reserve source points are missing")
            ai, ai_record, _ = entity(supply["ai"], "idAI2")
            species(supply, ai_record)
            ai_name = reserve["label"] + "_ai__ap_v070_s00100"
            group_name = reserve["label"] + "_spawns"
            names = [ai_name, group_name]
            ai_clone = _rename(ai, ai_name).replace('"' + ai_record["name"] + '"', '"' + ai_name + '"')
            foreign = entity_names(supply["ai"]["map_key"]) - original_names - {ai_record["name"]}
            if any('"' + name + '"' in ai_clone for name in foreign):
                raise ValueError("Native reserve AI has unresolved foreign entity references")
            points = []
            definitions = {"num": 1, "item[0]": {"name": ai_name}}
            for index, reference in enumerate(supply["spawners"]):
                if reference["map_key"] != map_key:
                    raise ValueError("Native reserve source points must belong to the map")
                point, point_record, _ = entity(reference, "idTarget_Spawn")
                entrance = point_record["edit"].get("spawnEditable", {})
                relocated = False
                if not spawn_entrance_compatible(point_record, ai_record, ai_record):
                    raise ValueError("The ammunition supplier cannot use this spawn entrance")
                if (entrance.get("aiStateOverride") not in GENERAL_SPAWN_STATES
                        and not (entrance.get("aiStateOverride") in {"AIOVERRIDE_TELEPORT", "AIOVERRIDE_SPAWN_ANIM_IDLE"}
                                 and not entrance.get("spawnAnim"))):
                    source_operation, source_group = None, None
                    for candidate in plan['operations']:
                        if (candidate['manager'] != operation['manager'] or candidate['script_index'] != operation['script_index']
                                or candidate.get('barrier') != operation.get('barrier')):
                            continue
                        source = source_record(candidate['from']['spawner']['entity'])
                        if candidate['event_def']=='spawnSingleAI':
                            matches = source['name']==point_record['name']
                        else:
                            members = list_items(source['edit'].get('spawners'))
                            members.extend(actor['origin_spawner']['entity'] for wave in candidate['waves']
                                           for actor in wave['instances'])
                            matches = point_record['name'] in members
                        if matches:
                            source_operation = candidate
                            source_group = source if candidate['event_def']!='spawnSingleAI' else None
                            break
                    original = source_point_actor(point_record,
                        selected_spawn_type(source_operation['from']) if source_operation else supply['spawn_type'],
                        manager_record, source_group,slot=plan['schema']>=7)
                    alternatives = group_entrance_points(source_group, selected_spawn_type(source_operation['from']), manager_record) if source_group else ()
                    placement = replacement_spawn_point(point_record, original, ai_record, alternatives, record=source_record)
                    if placement is None:
                        owner = source_record(operation['from']['spawner']['entity'])
                        owner_type = selected_spawn_type(operation['from'])
                        candidates = ([(owner, source_point_actor(owner, owner_type, manager_record,slot=plan['schema']>=7))]
                                      if operation['event_def']=='spawnSingleAI'
                                      else group_entrance_points(owner, owner_type, manager_record))
                        placement = replacement_spawn_point(point_record, original, ai_record, candidates, record=source_record)
                    if placement is None and source_operation is not None and plan['schema']>=7:
                        events = list_items(list_items(manager_record['edit']['encounterComponent']['entityEvents'])[source_operation['script_index']]['events'])
                        placement = replacement_spawn_point(point_record, original, ai_record,
                            earlier_entrance_points(source_operation, events, supply['species'], manager_record), record=source_record)
                    if placement is None:
                        raise ValueError("The ammunition supplier cannot use this spawn entrance")
                    relocated = placement is not point_record
                    if relocated:
                        point, point_record, _ = source_entity(map_key, placement['name'])
                point_name = reserve["label"] + "_point_" + str(index)
                point = _rename(point, point_name)
                point = _edit(point, ("entityDefs",), definitions)
                point = _edit(point, ("initialEntityDefs",), definitions)
                point = _edit(point, ("targetSpawnParent",), "")
                point = _edit(point, ('targets',), {'num':1,'item[0]':ai_name})
                point = _edit(point, ('spawnEditableShared','deathTrigger'), '')
                point = _edit(point, ('spawnEditableShared','groupName'), '')
                point = _edit(point, ('spawnEditable','copyTargets'), False)
                points.append(point_name)
                names.append(point_name)
                clones.append(point)
            if any(name in original_names or name in generated_names for name in names):
                raise ValueError("Native reserve identifier collision")
            settings = {"spawners": {"num": len(points), **{f"item[{i}]": name for i, name in enumerate(points)}},
                        "exclusive": True, "includeTouchingSpawners": False, "targetSpawnSubGroups": {"num": 0},
                        "remainActiveAfterTouch": False, "includeInGlobalOverrideGroup": False}
            group = ('entity { entityDef ' + group_name + ' { inherit = "encounter/spawn_group/zone"; '
                     'class = "idTargetSpawnGroup"; edit = ' + _render(settings, 2) + ' } }')
            clones.extend((_layers(group, manager_record["layers"]), ai_clone))
            generated_names.update(names)
            origin = {**supply, "spawner": {"entity": group_name}}
        elif origin["species"] != "zombie_tier_1" or origin["spawn_type"] != "ENCOUNTER_SPAWN_ZOMBIE_TIER_1":
            raise ValueError("Native reserve requires authored ZombieT1 supply")
        tokens = [reserve[field] for field in ("manager_name", "commit_name", "finish_name", "label", "completion_flag")]
        if any(not isinstance(token, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token) is None for token in tokens):
            raise ValueError("Frozen reserve needs private identifiers")
        if len(set(tokens)) != len(tokens) or any(token in original_names or token in generated_names or token in wave_labels
                or token in reserve_tokens or f'"{token}"' in source_text for token in tokens):
            raise ValueError("Frozen reserve identifier collision")
        count = reserve["desired_count"]
        if type(count) is not int or not 1 <= count <= 0x7fffffff:
            raise ValueError("Frozen reserve desired_count must be a positive int32")
        minimum, maximum = reserve["min_spawn_delay"], reserve["max_spawn_delay"]
        if any(type(value) not in (int, float) or not 0 <= value <= 3.4028234663852886e38 or not math.isfinite(value)
               for value in (minimum, maximum)) or maximum < minimum:
            raise ValueError("Frozen reserve needs finite ordered spawn delays")
        trigger_blocks = []
        for field, name, class_name, inherit in (
                ("commit_template", reserve["commit_name"], "idEncounterTrigger_Commit", "encounter/trigger/commit"),
                ("finish_template", reserve["finish_name"], "idEncounterTrigger_RaiseUserFlag", "encounter/trigger/user_flag")):
            trigger, record, _ = entity(reserve[field], class_name)
            if record["inherit"] != inherit:
                raise ValueError("Unqualified reserve trigger inherit")
            trigger = _layers(_rename(trigger, name), manager_record["layers"])
            position = manager_record["edit"].get("spawnPosition")
            if position is not None:
                trigger = _edit(trigger, ("spawnPosition",), position)
            trigger = _edit(trigger, ("clipModelInfo", "clipModelName"), None)
            trigger = _edit(trigger, ("triggerFirst",), False)
            if field == "finish_template":
                trigger = _edit(trigger, ("userFlag",), reserve["completion_flag"])
            trigger_blocks.append(trigger)
        label, flag = reserve["label"], reserve["completion_flag"]
        ready_calls = []
        start_calls = [_call("activateTarget", [{"entity": reserve["commit_name"]}, {"string": label}])]
        if supply and plan["schema"] >= 5:
            ready_name, ready_flag = reserve["manager_name"] + "_ready", flag + "_ready"
            if ready_name in original_names or ready_name in generated_names:
                raise ValueError("Native reserve readiness identifier collision")
            trigger_blocks.append(_edit(_rename(trigger_blocks[-1], ready_name), ("userFlag",), ready_flag))
            generated_names.add(ready_name)
            source_ready_flags.setdefault(manager_record["name"], []).append(ready_name)
            ready_calls = [
                ("staggeredAISpawn", [{"eEncounterSpawnType_t": origin["spawn_type"]}, {"int": 1},
                    {"entity": origin["spawner"]["entity"]}, {"string": label}, {"float": 0}, {"float": 0}]),
                ("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}]),
                ("activateTarget", [{"entity": ready_name}, {"string": ready_flag}]),
            ]
            start_calls.append(_call("waitForEventFlag", [{"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_USER_EVENT"},
                {"string": ready_flag}, {"bool": True}, {"bool": False}]))
        reserve_calls = [*ready_calls,
            ("maintainAICount", [{"eEncounterSpawnType_t": origin["spawn_type"]}, {"int": count}, {"int": 0},
                {"float": minimum}, {"int": 0}, {"entity": origin["spawner"]["entity"]}, {"string": label}, {"float": maximum}]),
            ("waitForEventFlag", [{"eEncounterEventFlags_t": "ENCOUNTER_EVENT_FLAG_USER_EVENT"}, {"string": flag},
                {"bool": True}, {"bool": False}]),
            ("stopMaintainingAICount", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": label}]),
            # native remove detaches manager records before queued entity destruction
            ("removeAI", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"string": label}]),
        ]
        private_manager = _rename(manager, reserve["manager_name"])
        events = {"num": len(reserve_calls), **{f"item[{index}]": {"eventCall": {"eventDef": name,
            "args": {"num": len(args), **{f"item[{arg_index}]": arg for arg_index, arg in enumerate(args)}}}}
            for index, (name, args) in enumerate(reserve_calls)}}
        component = {"entityEvents": {"num": 1, "item[0]": {"entity": reserve["manager_name"], "events": events}}}
        private_manager = _edit(private_manager, ("encounterComponent",), component)
        for field, targets in (("preCombatTriggers", []), ("commitTriggers", [reserve["commit_name"]]),
                ("exitTriggers", []), ("userFlagTriggers", [reserve["finish_name"]])):
            private_manager = _edit(private_manager, (field,), {"num": len(targets), **{
                f"item[{index}]": target for index, target in enumerate(targets)}})
        private_manager = _edit(private_manager, ("ignoreTelemetry",), True)
        private_manager = _edit(private_manager, ("enableAIHighlightOnFinish",), False)
        generated_names.update(tokens[:3])
        reserve_tokens.update(tokens)
        clones.extend((private_manager, *trigger_blocks))
        return (start_calls, _call("activateTarget", [{"entity": reserve["finish_name"]}, {"string": flag}]))

    for operation in plan["operations"]:
        waves = operation.get("waves")
        if not isinstance(waves, list) or (not waves and plan["schema"] < 5):
            raise ValueError("Frozen operation requires at least one wave")
        staged = len(waves) > 1
        ambient = plan["schema"] >= 6 and operation.get("barrier") is None
        if ambient and staged:
            raise ValueError("Ambient emissions require a single frozen queue")
        operation_keys = ("manager", "script_index", "event_index", "event_def", "event_sha256", "from", "waves")
        extra = ("barrier",) if (staged and plan["schema"] >= 2) or plan["schema"] >= 4 else ()
        if "reserve" in operation and (plan["schema"] >= 5 or staged and plan["schema"] == 3):
            extra += ("reserve",)
        if "reduction" in operation and plan["schema"] >= 7:
            if (not isinstance(operation["reduction"], dict)
                    or set(operation["reduction"]) != {"reason", "requested_count"}
                    or operation["reduction"]["reason"] != "no_weighted_enemy_fits" or waves
                    or operation["reduction"]["requested_count"] is not None and (
                        type(operation["reduction"]["requested_count"]) is not int
                        or operation["reduction"]["requested_count"] < 1)):
                raise ValueError("Frozen encounter reduction is invalid")
            extra += ("reduction",)
        _keys(operation, (*operation_keys, *extra), "operation")
        _keys(operation["manager"], ("entity", "sha256"), "manager")
        manager_name = operation["manager"]["entity"]
        manager_ref = {"map_key": map_key, **operation["manager"]}
        manager, manager_record, manager_bounds = entity(manager_ref, "idEncounterManager")
        script_index, event_index = operation["script_index"], operation["event_index"]
        if any(type(index) is not int or index < 0 for index in (script_index, event_index)):
            raise ValueError("Frozen script/event indices must be nonnegative integers")
        event_key = manager_name, script_index, event_index
        if event_key in selected_events:
            raise ValueError("Duplicate frozen operation for one event")
        selected_events.add(event_key)
        event_path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{event_index}]", "eventCall")
        event_start, event_end = _value_span(manager, event_path)
        event_text = manager[event_start:event_end]
        if text_sha256(event_text.strip()) != operation["event_sha256"]:
            raise ValueError("Frozen event hash mismatch")
        event = _get(manager_record["edit"], event_path)
        event_def = event.get("eventDef")
        supported = ("spawnSingleAI",) if plan["schema"] == 1 else ("spawnSingleAI", "spawnAI")
        if plan["schema"] >= 4:
            supported += ("staggeredAISpawn",)
        if operation["event_def"] != event_def or event_def not in supported:
            raise ValueError("Unsupported frozen spawn event")
        args = list_items(event.get("args"))
        grouped = event_def != "spawnSingleAI"
        layout = ("eEncounterSpawnType_t", "int", "entity", "string") if grouped else ("eEncounterSpawnType_t", "entity", "string")
        if event_def == "staggeredAISpawn":
            layout += ("float", "float")
        if len(args) != len(layout) or any(set(arg) != {kind} for arg, kind in zip(args, layout)):
            raise ValueError("Unqualified spawn argument layout")
        if grouped and (type(args[1]["int"]) is not int or args[1]["int"] <= 0):
            raise ValueError("Frozen spawnAI requires a positive finite total")
        if staged and not grouped and plan["schema"] < 4:
            raise ValueError("Staged waves require an authored spawnAI group")
        origin = operation["from"]
        _keys(origin, ("species", "spawn_type", "spawner", *(("selected_spawn_type",)
              if plan["schema"] >= 7 and "selected_spawn_type" in origin else ())), "origin")
        source_type = selected_spawn_type(origin) if plan["schema"] >= 7 else origin["spawn_type"]
        if origin["spawner"]["map_key"] != map_key:
            raise ValueError("Frozen origin spawner must belong to the target map")
        origin_block, origin_record, _ = entity(origin["spawner"], "idTargetSpawnGroup" if grouped else "idTarget_Spawn")
        if args[0]["eEncounterSpawnType_t"] != origin["spawn_type"] or args[2 if grouped else 1]["entity"] != origin_record["name"]:
            raise ValueError("Frozen origin does not match the selected event")
        source_events = _get(manager_record["edit"], event_path[:-2])
        source_event_items = list_items(source_events)
        faction_controlled=plan['schema']>=7 and any(item['eventCall']['eventDef']=='setFactionRelation'
            for script in list_items(manager_record['edit']['encounterComponent']['entityEvents']) for item in list_items(script.get('events')))
        manager_events=[item for script in list_items(manager_record['edit']['encounterComponent']['entityEvents']) for item in list_items(script.get('events'))]
        if manager_name not in combat_grouping_targets:
            combat_grouping_targets[manager_name]=encounter_combat_groupings(manager_events,
                lambda name:source_entity(map_key,name)[1] if name in original_names else None)
        groupings=combat_grouping_targets[manager_name]
        role_controlled=plan['schema']>=7 and any(item['eventCall']['eventDef'] in ('activateCombatGrouping','clearCombatRoles')
            and finite_phase_event(item['eventCall'],tracked=True,combat_groupings=groupings) for item in manager_events)
        if plan["schema"] >= 2:
            _keys(source_event_items[event_index], ("eventCall",), "event wrapper")
        generated_calls = []
        emit_at_source = faction_controlled or role_controlled
        compound_end = None
        if ambient:
            source_scripts = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
            allowed = {"spawnSingleAI", "spawnAI", "wait", "waitStaggeredSpawnComplete"}
            if (len(source_scripts) != 1 or source_scripts[0].get("entity") != manager_name
                    or any(row.get("eventCall", {}).get("eventDef") not in allowed for row in source_event_items)):
                raise ValueError("Ambient emissions cannot alter encounter control or death gates")
            for row in source_event_items:
                call = row["eventCall"]
                if call["eventDef"] in ("spawnAI", "staggeredAISpawn"):
                    native_args = list_items(call["args"])
                    if type(native_args[1].get("int")) is not int or native_args[1]["int"] <= 0:
                        raise ValueError("Ambient emissions require positive finite source requests")
        if not ambient and (staged or plan["schema"] >= 4):
            barrier = operation["barrier"]
            _keys(barrier, ("event_index", "event_sha256"), "barrier")
            if type(barrier["event_index"]) is not int or not event_index < barrier["event_index"] < len(source_event_items):
                raise ValueError("Frozen barrier must follow the spawn event")
            if plan["schema"] < 4 and barrier["event_index"] != event_index + 1:
                raise ValueError("Frozen barrier must immediately follow the spawn event")
            barrier_index = barrier["event_index"]
            if plan["schema"] >= 7:
                condition_members = compound_condition_indices(source_event_items)
                if event_index in condition_members or barrier_index in condition_members:
                    raise ValueError("Finite phases cannot replace compound-condition members")
            if plan["schema"] >= 4:
                scripts_in_source = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
                if manager_name not in traversal_flags:
                    traversal_flags[manager_name] = encounter_traversal_flags(manager_record,
                        lambda name: source_entity(map_key, name)[1] if name in original_names else None) if plan["schema"] >= 7 else set()
                user_flags = traversal_flags[manager_name]
                script_key = manager_name, script_index
                if script_key not in effect_targets:
                    effect_targets[script_key] = encounter_effect_targets(source_event_items,
                        lambda name: source_entity(map_key, name)[1] if name in original_names else None) if plan["schema"] >= 7 else set()
                effects = effect_targets[script_key]
                if (scripts_in_source[script_index].get("entity") != manager_name
                        or plan["schema"] < 7 and len(scripts_in_source) != 1):
                    raise ValueError("Finite phase waves require one manager script")
                if other_script_maintenance_overlaps(scripts_in_source, script_index,
                        source_type, args[3 if grouped else 2]['string']):
                    raise ValueError("Checkpoint waves require a separate maintained-cohort review")
                if not finite_phase_events(source_event_items, event_index+1, barrier_index,
                        tracked=plan["schema"] >= 7, user_flags=user_flags, effect_targets=effects,combat_groupings=groupings,
                        cohort=(source_type,args[3 if grouped else 2]['string']) if plan['schema']>=7 else None):
                    raise ValueError("Finite phase waves cannot cross control or death-count gates")
                emit_at_source |= any(item["eventCall"]["eventDef"] == "waitForEventFlag"
                    or plan["schema"] >= 7 and index not in condition_members and item["eventCall"]["eventDef"] == "waitAIRemaining"
                        and list_items(item["eventCall"]["args"])[1] == {"int":0}
                    for index,item in enumerate(source_event_items[event_index + 1:barrier_index],event_index+1))
                if plan['schema']>=7:
                    emit_at_source |= any(item['eventCall']['eventDef']=='damageAI'
                        and _actor_control_overlaps(item['eventCall'],source_type,args[3 if grouped else 2]['string'])
                        for item in source_event_items[event_index+1:barrier_index])
                if plan['schema']>=7:
                    emit_at_source |= any(item['eventCall']['eventDef']=='makeAIAwareOfPlayer'
                        and finite_phase_event(item['eventCall'],tracked=True)
                        and list_items(item['eventCall']['args'])[2]==args[3 if grouped else 2]
                        and args[3 if grouped else 2]!={'string':''} for item in source_event_items[:barrier_index])
                for item in source_event_items:
                    source_call = item.get("eventCall", {})
                    source_def = source_call.get("eventDef")
                    if plan["schema"] < 7 and source_def in ("waitKillCount", "maintainAICount"):
                        raise ValueError("Finite phase waves cannot alter renewable or kill-count scripts")
                    if source_def in ("spawnAI", "staggeredAISpawn"):
                        source_args = list_items(source_call.get("args"))
                        if len(source_args) < 2 or type(source_args[1].get("int")) is not int or source_args[1]["int"] <= 0:
                            raise ValueError("Finite phase waves require finite source requests")
                if plan["schema"] >= 7 and maintenance_overlaps(source_event_items, event_index, barrier_index,
                        source_type, args[3 if grouped else 2]["string"]):
                    raise ValueError("Finite phase waves cannot replace an active maintained cohort")
                pending_labels = dict.fromkeys(list_items(item["eventCall"]["args"])[3]["string"]
                    for item in source_event_items[:event_index] if item.get("eventCall", {}).get("eventDef") == "staggeredAISpawn")
                for pending_label in pending_labels if staged and plan["schema"] == 4 else ():
                    generated_calls.append(_call("waitStaggeredSpawnComplete", [
                        {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}, {"int": 0}, {"string": pending_label}, {"bool": False}]))
            barrier_path = (*event_path[:-2], f"item[{barrier_index}]", "eventCall")
            barrier_start, barrier_end = _value_span(manager, barrier_path)
            barrier_call = source_event_items[barrier_index]["eventCall"]
            if text_sha256(manager[barrier_start:barrier_end].strip()) != barrier["event_sha256"]:
                raise ValueError("Frozen barrier hash mismatch")
            barrier_args = list_items(barrier_call.get("args"))
            source_label = args[3 if grouped else 2]["string"]
            barrier_key = manager_name, script_index, barrier_index
            if plan["schema"] >= 7 and barrier_call.get("eventDef") == "waitMulitpleConditions":
                clears_population, compound_end = compound_population_gate(source_event_items,barrier_index,source_type,source_label)
                if not clears_population or not finite_phase_events(source_event_items,barrier_index,compound_end,
                        tracked=True,user_flags=user_flags,effect_targets=effects,combat_groupings=groupings):
                    raise ValueError("Compound completion must require the generated cohort to die")
                emit_at_source = True
                compound_barriers[barrier_key] = compound_end
            else:
                _keys(source_event_items[barrier_index], ("eventCall",), "barrier wrapper")
                clears_population = bool(barrier_args) and barrier_args[0] == {"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"}
                if not clears_population and plan["schema"] >= 7:
                    private_cleanup = any('reserve' in other and other['manager'] == operation['manager']
                        and other['script_index'] == script_index and other.get('barrier') == barrier
                        for other in plan['operations'])
                    clears_source = (barrier_call.get('eventDef') == 'waitAIRemaining'
                        and finite_phase_event(barrier_call, tracked=True) and len(barrier_args) == 4
                        and not enemy_spawn_mask(source_type) & ~enemy_spawn_mask(barrier_args[0]['eEncounterSpawnType_t']))
                    clears_population = (len(scripts_in_source) == 1
                                         and not list_items(manager_record["edit"].get("adoptedStandAloneAI"))
                                         and not list_items(manager_record["edit"].get("preCombatTriggers"))
                                         and (finite_population_cleared(source_event_items, barrier_index, user_flags=user_flags,
                                                                        effect_targets=effects,combat_groupings=groupings)
                                              or clears_source and not private_cleanup))
                if (barrier_call.get("eventDef") != "waitAIRemaining" or len(barrier_args) != 4
                        or not clears_population or type(barrier_args[1].get("int")) is not int or barrier_args[1]["int"] != 0
                        or barrier_args[2] not in ({"string": ""}, {"string": source_label})
                        or set(barrier_args[3]) != {"bool"} or type(barrier_args[3]["bool"]) is not bool
                        or (plan["schema"] < 4 and barrier_args[2:] != [{"string": ""}, {"bool": False}])):
                    raise ValueError("Frozen barrier must wait for ANY remaining zero")
            if staged and plan["schema"] < 4 and barrier_index == event_index + 1:
                if barrier_key in selected_events:
                    raise ValueError("Duplicate frozen barrier")
                selected_events.add(barrier_key)
        reserve = operation.get("reserve")
        if "reserve" in operation:
            source_scripts = list_items(manager_record["edit"]["encounterComponent"]["entityEvents"])
            if (source_scripts[script_index].get("entity") != manager_name
                    or plan["schema"] < 7 and (len(source_scripts) != 1 or script_index != 0)):
                raise ValueError("Native reserve requires one self-owned manager script")
            if plan["schema"] < 5:
                if barrier_index != len(source_event_items) - 2:
                    raise ValueError("Native reserve barrier must be the penultimate source event")
                relay_call = source_event_items[-1].get("eventCall", {})
                relay_args = list_items(relay_call.get("args"))
                if relay_call.get("eventDef") != "activateTarget" or len(relay_args) != 2 or set(relay_args[0]) != {"entity"} or set(relay_args[1]) != {"string"}:
                    raise ValueError("Native reserve requires an authored final relay")
                _, relay, _ = source_entity(map_key, relay_args[0]["entity"])
                if relay["class"] != "idTarget_Count" or relay["inherit"] != "target/relay":
                    raise ValueError("Unqualified native reserve final relay")
            reserve_start, reserve_finish = reserve_recipe(reserve, manager, manager_record, origin, operation)
            generated_calls.extend(reserve_start)
            if plan["schema"] >= 5:
                if ambient:
                    raise ValueError("Renewable combat supply needs a closing native gate")
                phase_supplies.setdefault(barrier_key, []).append(reserve_finish)
        if plan["schema"] >= 5 and not ambient and waves and compound_end is None:
            queue_type = origin["spawn_type"] if plan["schema"] >= 7 else "ENCOUNTER_SPAWN_ANY"
            phase_queues.setdefault(barrier_key, set()).add((queue_type, args[3 if grouped else 2]["string"]))
        for wave_index, wave in enumerate(waves):
            queued = staged and (plan["schema"] < 5 or wave_index > 0)
            wave_calls = []
            _keys(wave, ("instances", "label", "min_spawn_stagger", "max_spawn_stagger") if queued else ("instances",), "wave")
            instances = wave["instances"]
            if not isinstance(instances, list) or not instances or (not ambient and not grouped and len(instances) != 1):
                raise ValueError("Frozen wave has an unsupported instance count")
            if not ambient and plan["schema"] >= 4 and len(instances) > (args[1]["int"] if grouped else 1):
                raise ValueError("Frozen wave exceeds its finite source request")
            if queued:
                label = wave["label"]
                if not isinstance(label, str) or not label or label in wave_labels or label in reserve_tokens or any(value == label for _, value in walk_values(manager_record["edit"])):
                    raise ValueError("Frozen wave needs a private unique label")
                wave_labels.add(label)
                minimum, maximum = wave["min_spawn_stagger"], wave["max_spawn_stagger"]
                if any(type(value) not in (int, float) or value < 0 or value > 3.4028234663852886e38 or not math.isfinite(value)
                       for value in (minimum, maximum)) or maximum != minimum:
                    raise ValueError("Single-member stagger commands require one finite frozen spawn time")
            for instance in instances:
                instance_keys = ("species", "spawn_type", "ai", "ai_name", "spawner_name", "attributes")
                instance_fields = (*instance_keys, "origin_spawner") if grouped else instance_keys
                if staged or plan["schema"] >= 7:
                    instance_fields += ("group_name",)
                _keys(instance, instance_fields, "instance")
                if plan["schema"] >= 7:
                    tracking = enemy_tracking_suffix(source_type, instance["spawn_type"])
                    if any(not instance[field].rsplit("_l", 1)[0].endswith(tracking)
                           for field in ("ai_name", "spawner_name", "group_name")):
                        raise ValueError("Frozen tracking differs from the native source slot")
                if grouped:
                    if instance["origin_spawner"]["map_key"] != map_key:
                        raise ValueError("Frozen member spawner must belong to the target map")
                    spawner, spawner_record, _ = entity(instance["origin_spawner"], "idTarget_Spawn")
                    if spawner_record["name"] not in list_items(origin_record["edit"].get("spawners")):
                        if spawner_record['name'] not in group_points(origin_record, manager_record):
                            raise ValueError("Frozen member spawner is outside the authored group")
                else:
                    spawner, spawner_record = origin_block, origin_record
                explicit_slot = plan['schema']>=7 and enemy_spawn_mask(origin['spawn_type'])==enemy_spawn_mask(source_type)
                original_ai = source_point_actor(spawner_record, source_type, manager_record, origin_record if grouped else None,slot=explicit_slot)
                if explicit_slot:
                    if source_definitions.species_for_spawn_type(source_type)!=origin['species']:
                        raise ValueError('Frozen source species differs from its native control type')
                else:
                    species({**origin, "spawn_type": source_type}, original_ai, source=True)
                source_templates = ([original_ai] if original_ai is not None else
                    source_definitions.point_templates(spawner_record,source_record,group=origin_record if grouped else None))
                original_ai_names = {actor['name'] for actor in source_templates}
                ai, ai_record, _ = entity(instance["ai"], "idAI2")
                species(instance, ai_record)
                alternatives = group_entrance_points(origin_record, source_type, manager_record) if grouped else ()
                entrance_point = replacement_spawn_point(spawner_record, original_ai, ai_record, alternatives, record=source_record)
                if entrance_point is None and plan['schema'] >= 7 and operation.get('barrier') is not None:
                    entrance_point = replacement_spawn_point(spawner_record, original_ai, ai_record,
                        earlier_entrance_points(operation, source_event_items, instance['species'], manager_record), record=source_record)
                if entrance_point is None:
                    raise ValueError(f"{map_key}/{spawner_record['name']}: {instance['species']} cannot use this spawn entrance")
                relocated = entrance_point is not spawner_record
                if relocated:
                    spawner, spawner_record, _ = source_entity(map_key, entrance_point['name'])
                attributes = instance["attributes"]
                _keys(attributes, ("health_percent", "damage_percent", "speed_percent", "size_percent"), "attributes")
                speed = attributes["speed_percent"]
                validate_enemy_speed_percent(instance["species"], speed)
                group_name = instance["group_name"] if staged or plan["schema"] >= 7 else "owner_" + instance["spawner_name"]
                for name in (instance["ai_name"], instance["spawner_name"], group_name):
                    match = _CLONE_NAME.fullmatch(name)
                    if match is None or int(match.group(1)) != speed:
                        raise ValueError("Frozen clone name does not encode its speed_percent")
                    if name in original_names or name in generated_names or name in reserve_tokens:
                        raise ValueError(f"Duplicate frozen clone name: {name}")
                    generated_names.add(name)
                paths = []
                if attributes["health_percent"] != 100:
                    paths.extend(("aiHealth", "components", "components[0]", field) for field in ("max", "starting"))
                if attributes["damage_percent"] != 100:
                    paths.append(("aiEditable", "damageOutModifiers"))
                if attributes["size_percent"] != 100:
                    paths.extend(("renderModelInfo", "scale", axis) for axis in ("x", "y", "z"))
                inherited = any(_get(ai_record["edit"], path) is _MISSING for path in paths)
                if inherited and ai_record["sha256"] not in attribute_bases:
                    attribute_bases[ai_record["sha256"]] = resolve_enemy_attribute_baselines(ai)
                ai_clone = materialize_enemy_attributes(ai, instance["ai_name"],
                    base_properties=attribute_bases[ai_record["sha256"]] if inherited else None,
                    **{key: value for key, value in attributes.items() if key != "speed_percent"})
                donor = instance['ai']['map_key']
                template_faction = ai_record['edit'].get('factionName')
                rebind_faction = faction_controlled or (donor != map_key and isinstance(template_faction,str)
                                                        and template_faction in entity_names(donor)
                                                        and template_faction not in original_names)
                if rebind_faction:
                    factions = [actor['edit'].get('factionName') for actor in source_templates]
                    faction = factions[0] if factions and all(value==factions[0] for value in factions) else None
                    if not isinstance(faction,str) or not faction:
                        raise ValueError('Enemy replacement needs an explicit source factionName')
                    ai_clone=_edit(ai_clone,('factionName',),faction)
                for path, value in walk_values(ai_record["edit"]):
                    if rebind_faction and path=='factionName':
                        continue
                    if value == ai_record["name"]:
                        ai_clone = _edit(ai_clone, tuple(path.split(".")), instance["ai_name"])
                    elif isinstance(value, str) and value in entity_names(instance["ai"]["map_key"]) and instance["ai"]["map_key"] != map_key:
                        raise ValueError(f"Foreign AI has an unresolved entity reference: {path}")
                point = dict(zip('xyz',spawn_position(spawner_record["edit"])))
                ai_clone = _edit(ai_clone, ("spawnPosition",), point)
                orientation = spawner_record["edit"].get("spawnOrientation")
                if orientation is not None:
                    ai_clone = _edit(ai_clone, ("spawnOrientation",), orientation)
                # native spawning applies the point world axis even when its decl omits it.
                spawner_clone = _rename(spawner, instance["spawner_name"])
                for path, value in walk_values(spawner_record["edit"]):
                    if isinstance(value,str) and value in original_ai_names:
                        spawner_clone = _edit(spawner_clone, tuple(path.split(".")), instance["ai_name"])
                if original_ai is None and len(original_ai_names)>1:
                    targets=[]
                    for value in list_items(spawner_record['edit'].get('targets')):
                        target=instance['ai_name'] if value in original_ai_names else value
                        if target!=instance['ai_name'] or target not in targets:targets.append(target)
                    spawner_clone=_edit(spawner_clone,('targets',),{'num':len(targets),**{f'item[{i}]':value for i,value in enumerate(targets)}})
                definitions = {"num": 1, "item[0]": {"name": instance["ai_name"]}}
                spawner_clone = _edit(spawner_clone, ("entityDefs",), definitions)
                spawner_clone = _edit(spawner_clone, ("initialEntityDefs",), definitions)
                spawner_clone = _edit(spawner_clone, ("targetSpawnParent",), "")
                if relocated and list_items(spawner_record['edit'].get('targets')):
                    spawner_clone = _edit(spawner_clone, ('targets',), {'num':1,'item[0]':instance['ai_name']})
                group_settings = {"spawners": {"num": 1, "item[0]": instance["spawner_name"]},
                    "exclusive": True, "includeTouchingSpawners": False, "targetSpawnSubGroups": {"num": 0},
                    "remainActiveAfterTouch": False, "includeInGlobalOverrideGroup": False}
                if staged and grouped:
                    group_clone = _rename(origin_block, group_name)
                    for field, value in group_settings.items():
                        group_clone = _edit(group_clone, (field,), value)
                else:
                    group_clone = ('entity {\nentityDef ' + group_name + ' {\n'
                        'inherit = "encounter/spawn_group/zone";\nclass = "idTargetSpawnGroup";\nedit = '
                        + _render(group_settings, 2) + '\n}\n}')
                spawn_type = source_type if plan["schema"] >= 7 else instance["spawn_type"]
                if ambient:
                    native_label = args[3 if grouped else 2]["string"]
                    delays = [{"float": 0}, {"float": 0}]
                    wave_calls.append(_call("staggeredAISpawn", [{"eEncounterSpawnType_t": spawn_type},
                        {"int": 1}, {"entity": group_name}, {"string": native_label}, *delays]))
                elif plan["schema"] >= 5 or queued or event_def == "staggeredAISpawn":
                    delays = ([{"float": minimum}, {"float": maximum}] if queued else args[4:6]
                              if event_def == "staggeredAISpawn" else [{"float": 0}, {"float": 0}])
                    wave_calls.append(_call("staggeredAISpawn", [{"eEncounterSpawnType_t": spawn_type},
                        {"int": 1}, {"entity": group_name},
                        {"string": label if queued and not emit_at_source else args[3 if grouped else 2]["string"]}, *delays]))
                else:
                    wave_calls.append(_call("spawnSingleAI", [{"eEncounterSpawnType_t": spawn_type},
                        {"entity": instance["spawner_name"]}, {"string": args[3 if grouped else 2]["string"]}]))
                clones.extend((group_clone, spawner_clone, ai_clone))
            if ambient:
                wave_calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": native_label}, {"bool": False}]))
            # keep native conditions and traversal running while emissions retry at their source.
            if plan["schema"] >= 5 and wave_index and not emit_at_source:
                wait = _call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}])
                clear = _call("waitAIRemaining", [*barrier_args[:2], {"string": label}, barrier_args[3]])
                phase_waves.setdefault(barrier_key, {}).setdefault(wave_index, []).append((wave_calls, wait, clear))
            elif staged and plan["schema"] < 5:
                wave_calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                    {"int": 0}, {"string": label}, {"bool": False}]))
                if plan["schema"] == 4:
                    # isolate each wave while preserving the final native gate
                    wave_calls.append(_call("waitAIRemaining", [*barrier_args[:2], {"string": label}, barrier_args[3]]))
                else:
                    wave_calls.append(manager[barrier_start:barrier_end])
            if plan["schema"] < 5 or wave_index == 0 or emit_at_source:
                generated_calls.extend(wave_calls)
        if "reserve" in operation and plan["schema"] < 5:
            generated_calls.append(reserve_finish)
        if plan["schema"] == 1:
            instance = waves[0]["instances"][0]
            event_body = _set(event_text[1:-1], ("args", "item[0]", "eEncounterSpawnType_t"), instance["spawn_type"])
            event_body = _set(event_body, ("args", "item[1]", "entity"), instance["spawner_name"])
            replacements.append((manager_bounds[0] + event_start, manager_bounds[0] + event_end, "{" + event_body + "}"))
        else:
            key = manager_name, script_index
            scripts.setdefault(key, (manager, manager_bounds, source_event_items, {}))[3][event_index] = generated_calls
            if staged and plan["schema"] < 4 and barrier_index == event_index + 1:
                scripts[key][3][barrier_index] = []

    population_gate_queues = {}
    for operation in plan["operations"] if plan["schema"] >= 7 else ():
        if not operation.get("barrier") or not operation["waves"]:
            continue
        manager_name, script_index = operation["manager"]["entity"], operation["script_index"]
        _, _, events, _ = scripts[manager_name, script_index]
        args = list_items(events[operation["event_index"]]["eventCall"]["args"])
        label = args[2 if operation["event_def"] == "spawnSingleAI" else 3]["string"]
        source_type = selected_spawn_type(operation["from"])
        condition_members = compound_condition_indices(events)
        barrier_key = manager_name, script_index, operation["barrier"]["event_index"]
        for index in range(operation["event_index"] + 1, compound_barriers.get(barrier_key,operation["barrier"]["event_index"])):
            call = events[index]["eventCall"]
            if (call['eventDef']=='damageAI' and finite_phase_event(call,tracked=True,cohort=(source_type,label))
                    and _actor_control_overlaps(call,source_type,label)):
                population_gate_queues.setdefault((manager_name,script_index,index),set()).add((source_type,label))
            if call["eventDef"] == "waitAIRemaining" and finite_phase_event(call, tracked=True, condition=index in condition_members):
                if index not in condition_members:
                    _keys(events[index], ("eventCall",), "population gate wrapper")
                if index in condition_members:
                    gate_args = list_items(call["args"])
                    if (not enemy_spawn_mask(source_type) & enemy_spawn_mask(gate_args[0]["eEncounterSpawnType_t"])
                            or gate_args[2]["string"] and gate_args[2]["string"] != label):
                        continue
                population_gate_queues.setdefault((manager_name, script_index, index), set()).add((source_type, label))
    for (manager_name, script_index, index), filters in population_gate_queues.items():
        manager, _, events, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{index}]", "eventCall")
        start, end = _value_span(manager, path)
        gates = [_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": mask}, {"int": 0},
            {"string": label}, {"bool": False}]) for mask, label in sorted(filters)]
        if index in compound_condition_indices(events):
            # keep timers and sibling conditions running while this cohort enters the arena.
            gates.insert(0, _call("waitMulitpleConditions", [{"int": len(gates)+1},
                {"encounterLogicOperator_t": "ENCOUNTER_LOGICAL_OP_AND"}, {"bool": False}]))
        original = events[index] if set(events[index]) != {"eventCall"} else manager[start:end]
        changes[index] = [*gates, original]
    for (manager_name, script_index, barrier_index), rounds in phase_waves.items():
        manager, _, events, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{barrier_index}]", "eventCall")
        start, end = _value_span(manager, path)
        barrier = manager[start:end]
        calls = []
        labels = dict.fromkeys(list_items(item["eventCall"]["args"])[3]["string"] for item in events[:barrier_index]
            if item.get("eventCall", {}).get("eventDef") == "staggeredAISpawn")
        for label in labels:
            calls.append(_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": "ENCOUNTER_SPAWN_ANY"},
                {"int": 0}, {"string": label}, {"bool": False}]))
        calls.append(barrier)
        for index in sorted(rounds):
            for spawns, _, _ in rounds[index]:
                calls.extend(spawns)
            calls.extend(wait for _, wait, _ in rounds[index])
            calls.extend(clear for _, _, clear in rounds[index])
        calls.append(barrier)
        changes[barrier_index] = calls

    for (manager_name, script_index, barrier_index), labels in phase_queues.items():
        manager, _, _, changes = scripts[manager_name, script_index]
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{barrier_index}]", "eventCall")
        start, end = _value_span(manager, path)
        gates = [_call("waitStaggeredSpawnComplete", [{"eEncounterSpawnType_t": source_type},
            {"int": 0}, {"string": label}, {"bool": False}]) for source_type, label in sorted(labels)]
        changes[barrier_index] = [*gates, *changes.get(barrier_index, [manager[start:end]])]

    for (manager_name, script_index, barrier_index), finishes in phase_supplies.items():
        manager, _, events, changes = scripts[manager_name, script_index]
        tail = compound_barriers.get((manager_name,script_index,barrier_index),barrier_index+1)-1
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events", f"item[{tail}]", "eventCall")
        start, end = _value_span(manager, path)
        original = events[tail] if set(events[tail]) != {"eventCall"} else manager[start:end]
        changes[tail] = [*changes.get(tail, [original]), *finishes]

    for (manager_name, script_index), (manager, manager_bounds, events, changes) in scripts.items():
        path = ("encounterComponent", "entityEvents", f"item[{script_index}]", "events")
        start, end = _value_span(manager, path)
        replacements.append((manager_bounds[0] + start, manager_bounds[0] + end,
            "{" + _replace_events(manager[start + 1:end - 1], events, changes) + "}"))
    result = source_text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    for manager_name, flags in source_ready_flags.items():
        start, end = find_entity_block_bounds(result, manager_name)
        block = result[start:end]
        record = next(entity_records(block, include_names={manager_name}))
        targets = [*list_items(record["edit"].get("userFlagTriggers")), *flags]
        block = _edit(block, ("userFlagTriggers",), {"num": len(targets), **{
            f"item[{index}]": name for index, name in enumerate(targets)}})
        result = result[:start] + block + result[end:]
    if clones:
        result += "\n" + "\n".join(clones) + "\n"
    for block in clones:
        next(entity_records(block, include_names=generated_names))
    for manager_name in {name for name, _, _ in selected_events}:
        bounds = find_entity_block_bounds(result, manager_name)
        next(entity_records(result[bounds[0]:bounds[1]], include_names={manager_name}))
    return result
