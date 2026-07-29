import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest


MOD_ROOT = Path(__file__).resolve().parents[2]


def test_distributed_client_hermetic_execution():
    """Verify that an extracted client runtime can compile item plans with zero access to level_configs/ or content/maps/."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        client_dst = tmp_path / "client"
        client_dst.mkdir()
        
        # Copy python runtime modules from current checkout
        for py_file in ["foundation.py", "map_registry.py", "item_classification.py", "publisher_contracts.py"]:
            shutil.copy2(MOD_ROOT / py_file, client_dst / py_file)
        
        # Copy data directory containing compiled projections
        shutil.copytree(MOD_ROOT / "data", client_dst / "data")
        if (MOD_ROOT / "build" / "staging" / "runtime_catalog").exists():
            for f in (MOD_ROOT / "build" / "staging" / "runtime_catalog").glob("*.json"):
                shutil.copy2(f, client_dst / "data" / f.name)

        # Verify level_configs and content/maps do NOT exist in the extracted client
        assert not (client_dst / "level_configs").exists()
        assert not (client_dst / "content" / "maps").exists()
        
        # Create isolated external execution working directory
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        runner_script = work_dir / "run_client_test.py"
        runner_script.write_text(
            "import sys, json, os\n"
            "from pathlib import Path\n"
            "client_dir = Path(sys.argv[1])\n"
            "sys.path.insert(0, str(client_dir))\n"
            "items_data = json.loads((client_dir / 'data' / 'items.json').read_text())\n"
            "ITEM_ID_TO_COMMAND = {int(k): v for k, v in items_data.items()}\n"
            "from foundation import compile_item_delivery_plan\n"
            "plan1 = compile_item_delivery_plan(7770001, ITEM_ID_TO_COMMAND)\n"
            "plan2 = compile_item_delivery_plan(7770008, ITEM_ID_TO_COMMAND)\n"
            "assert plan1.commands, 'Chainsaw command plan empty'\n"
            "assert plan2.commands, 'Codex command plan empty'\n"
            "print('SUCCESS_HERMETIC_CLIENT')\n"
        )
        
        res = subprocess.run(
            [sys.executable, str(runner_script), str(client_dst)],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(client_dst)},
        )
        
        assert res.returncode == 0, f"Hermetic client execution failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert "SUCCESS_HERMETIC_CLIENT" in res.stdout
