"""Generate the shell (main menu) entities with the Archipelago world visual.

The vanilla shell map is the single source of truth: the tool verifies its
pinned SHA-256, injects one inert AP visual entity in the campaign camera's
foreground, and writes the complete ``shell.entities`` file for packaging. The
visual reuses the DoomEAP-owned Archipelago logo model already packaged for
campaign maps and the vanilla ``rotate_slow`` think component proven in
``hub.map``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SHELL_SOURCE = REPO_ROOT / "vanillamaps" / "shell.map"
SHELL_SOURCE_SHA256 = "c1ce664337a6c78d8f1e834b691a68646c791c6c0e8572fdf11a788d2bff9d3e"

AP_MENU_ENTITY_NAME = "ap_menu_archipelago_world"
AP_MENU_MODEL = "art/pickups/codex.lwo"
AP_MENU_POSITION = (12.0, -100.0, 2.5)
AP_MENU_SCALE = 3.6
AP_MENU_THINK_COMPONENT = "rotate_slow"

ENTITY_TEMPLATE = '''entity {{
\tentityDef {name} {{
\t\tinherit = "func/dynamic";
\t\tclass = "idDynamicEntity";
\t\texpandInheritance = false;
\t\tpoolCount = 0;
\t\tpoolGranularity = 2;
\t\tnetworkReplicated = false;
\t\tdisableAIPooling = false;
\t\tedit = {{
\t\t\tthinkComponentDecl = "{think_component}";
\t\t\tspawnPosition = {{
\t\t\t\tx = {x};
\t\t\t\ty = {y};
\t\t\t\tz = {z};
\t\t\t}}
\t\t\trenderModelInfo = {{
\t\t\t\tmodel = "{model}";
\t\t\t\tcontributesToLightProbeGen = false;
\t\t\t\tignoreDesaturate = true;
\t\t\t\tscale = {{
\t\t\t\t\tx = {scale};
\t\t\t\t\ty = {scale};
\t\t\t\t\tz = {scale};
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
'''


def generate_shell_entities(source_text: str) -> str:
    if AP_MENU_ENTITY_NAME in source_text:
        raise ValueError(f"shell map already contains {AP_MENU_ENTITY_NAME}")
    entity = ENTITY_TEMPLATE.format(
        name=AP_MENU_ENTITY_NAME,
        think_component=AP_MENU_THINK_COMPONENT,
        x=repr(AP_MENU_POSITION[0]),
        y=repr(AP_MENU_POSITION[1]),
        z=repr(AP_MENU_POSITION[2]),
        model=AP_MENU_MODEL,
        scale=repr(AP_MENU_SCALE),
    )
    return source_text.rstrip("\n") + "\n\n" + entity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SHELL_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = args.source
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != SHELL_SOURCE_SHA256:
        raise SystemExit(
            f"Vanilla shell source hash mismatch: expected {SHELL_SOURCE_SHA256}, got {digest}"
        )
    generated = generate_shell_entities(source.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generated, encoding="utf-8", newline="\n")
    print(f"shell entities written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
