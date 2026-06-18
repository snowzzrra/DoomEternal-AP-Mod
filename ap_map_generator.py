import os
import re

# you don't need to use this to play the mod
# i'm making this file available simply for transparency and for anyone who wants to generate the AP targets in other maps by themselves, since the process is a bit tedious to do manually and this automates it
# you could just do the changes by hand, but why??
def remove_balanced_entity_blocks(content, name_prefix):
    pattern = re.compile(r'entity\s*\{\s*(layers\s*\{\s*"[^"]+"\s*\}\s*)?entityDef\s+' + re.escape(name_prefix) + r'\w+\s*\{', re.IGNORECASE)
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

def generate_target_relay(ap_check_id, spawn_pos_text):
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
{spawn_pos_text}		}}
	}}
}}
"""

def generate_map(input_file, output_file):
    with open(input_file, "r", encoding="utf-8") as f:
        content = f.read()

    content = remove_balanced_entity_blocks(content, "ap_logic_")
    content = remove_balanced_entity_blocks(content, "AP_CHECK_")
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"ap_logic_[^"]+";', '', content, flags=re.IGNORECASE)
    content = re.sub(r'\s*item\[\d+\]\s*=\s*"AP_CHECK_[^"]+";', '', content, flags=re.IGNORECASE)

    blocks = content.split("entity {")
    new_blocks = [blocks[0]]

    modified_count = 0

    # still in progress, but this one works in the entire e1m1 level
    trigger_conditions = [
        'inherit = "progress/codex"',
        'inherit = "pickup/collectible/toys/',
        'inherit = "pickup/weapon/heavy_cannon"',
        'inherit = "pickup/weapon/chainsaw"',
        'inherit = "pickup/extra_life/extra_life_1"',
        'inherit = "progress/mod_bot"',
        'inherit = "progress/cheats/',
        'inherit = "pickup/equipment/frag_grenade"'
    ]

    for block in blocks[1:]:
        if any(cond in block for cond in trigger_conditions):
            
            name_match = re.search(r'entityDef\s+([^\s{]+)', block)
            if not name_match:
                new_blocks.append("entity {" + block)
                continue
            
            entity_name = name_match.group(1).strip()
            ap_check_id = f"AP_CHECK_{entity_name.upper()}"
            
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
                if "targets = {" in block:
                    match = re.search(r'targets\s*=\s*\{\s*num\s*=\s*(\d+);\s*(.*?)\s*\}', block, re.DOTALL)
                    if match:
                        old_num = int(match.group(1))
                        new_num = old_num + 1
                        items = match.group(2).strip()
                        new_item = f'item[{old_num}] = "{ap_check_id}";'
                        
                        old_targets_block = match.group(0)
                        new_targets_block = f'targets = {{\n\t\t\tnum = {new_num};\n\t\t\t{items}\n\t\t\t{new_item}\n\t\t}}'
                        block = block.replace(old_targets_block, new_targets_block)
                else:
                    new_targets_block = f'\t\ttargets = {{\n\t\t\tnum = 1;\n\t\t\titem[0] = "{ap_check_id}";\n\t\t}}\n'
                    block = block.replace("edit = {", "edit = {\n" + new_targets_block, 1)

                # gravity stuff
                block = re.sub(r'physicsAttributes\s*=\s*"[^"]+";', "", block)

                block = re.sub(r'clipModelInfo\s*=\s*\{\s*type\s*=\s*"CLIPMODEL_BOX";\s*\}', "", block)

                # this one is visual stuff: some pickups have a specific model defined in the renderModelInfo block, but we want them all to look like the question mark pickup
                if 'triggerDef =' in block:
                    block = re.sub(r'triggerDef\s*=\s*"[^"]+";', 'triggerDef = "trigger/props/pickup_large";', block)

                
                # this line is responsible for the codex in corner street not registering hits because it has a CLIPMODEL_BOX that doesn't match the visual model, so we remove it entirely to let the trigger work with just the triggerDef
                # if this happens again, i'll create an specific method for this kind of exception instead of hardcoding the ID, but for now this is the only one with this problem so it should be fine
                if ap_check_id == "AP_CHECK_CORNER_STREET_PROGRESS_CODEX_1":
                    injection = """
                clipModelInfo = {
                        type = "CLIPMODEL_BOX";
                        size = {
                                x = 4;
                                y = 4;
                                z = 4;
                        }
                }"""
                    block = block.replace('edit = {', 'edit = {' + injection, 1)

                # question mark model for all
                if "renderModelInfo = {" in block:
                    block = re.sub(
                        r'(renderModelInfo\s*=\s*\{[\s\S]*?model\s*=\s*)(?:"[^"]+"|NULL)',
                        r'\1"art/pickups/question_mark_a.lwo"',
                        block,
                        count=1
                    )
                    # padronizes the scale of all pickups to 1, since some of them have custom scales that can cause visual issues with the new model (like the chainsaw and grenade launcher pickups in e1m1)
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

                relay_entity_str = generate_target_relay(ap_check_id, spawn_pos_text)
                new_blocks.append(relay_entity_str)

                modified_count += 1

        new_blocks.append("entity {" + block)

    final_content = "".join(new_blocks)

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(final_content)

    print(f"Successfully generated {modified_count} GLOBAL AP Targets using idTrigger mutation!")

if __name__ == "__main__":
    input_path = "YOUR PATH TO THE ORIGINAL .ENTITIES FILE"
    output_path = "YOUR PATH TO THE OUTPUT .ENTITIES FILE"
    generate_map(input_path, output_path)
