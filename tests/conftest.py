import os

# importing Archipelago clients otherwise checks, and may try to install, every world's requirements
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
