import os

import pytest

# importing Archipelago clients otherwise checks, and may try to install, every world's requirements
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")


@pytest.fixture(autouse=True)
def launcher_test_language(request, monkeypatch):
    if request.path.name.startswith(("test_launcher_", "test_checkpoint_launcher")):
        from doom_eap.launcher.ui import strings

        monkeypatch.setattr(strings, "_locale", "en")
