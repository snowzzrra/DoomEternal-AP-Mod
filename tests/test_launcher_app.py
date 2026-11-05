from doom_eap.launcher.launcher_app import main


def test_launcher_self_test_builds_full_window(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert main(["--self-test"]) == 0
