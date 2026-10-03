import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from doom_eap.launcher import launcher_reporting as reporting


def test_packaged_report_endpoint_is_configurable(tmp_path):
    from types import SimpleNamespace
    from doom_eap.launcher.launcher_controller import LauncherController

    client = tmp_path / 'client'
    (client / 'data').mkdir(parents=True)
    (client / 'data/report_endpoint.json').write_text(json.dumps({
        'schema_version': 1, 'endpoint': 'https://reports.example.com/v1/reports',
    }), encoding='utf-8')
    controller = LauncherController.__new__(LauncherController)
    controller.client_dir = client
    controller.bundle_dir = tmp_path / 'frozen'
    controller.workers = SimpleNamespace(submit=lambda *args: True)
    assert controller.request_report_submission({
        'schema_version': 1, 'idempotency_key': 'd98a6d66-0468-4bfc-8c70-bb7fd67c0050',
        'title': 'Configuration check', 'description': 'No network request.', 'diagnostics': '',
    })


def test_editor_close_cannot_erase_worker_submission(tmp_path, monkeypatch):
    draft = tmp_path / 'draft.json'
    payload = {'title': 'Reviewed', 'description': 'Observed'}
    reporting.save_report_draft(draft, payload)
    first = Event()
    release = Event()
    second = Event()
    publish = reporting.publish_file
    def controlled_publish(source, destination, **kwargs):
        record = json.loads(source.read_text())
        if record['submitted']:
            second.set()
        else:
            first.set()
            assert release.wait(3)
        return publish(source, destination, **kwargs)
    monkeypatch.setattr(reporting, 'publish_file', controlled_publish)
    with ThreadPoolExecutor(2) as workers:
        closing = workers.submit(reporting.save_report_draft, draft, payload)
        assert first.wait(3)
        sending = workers.submit(reporting.save_report_draft, draft, payload, submitted=True, url='confirmed')
        try:
            assert not second.wait(0.1)
        finally:
            release.set()
        closing.result(3)
        sending.result(3)
    record = json.loads(draft.read_text())
    assert record['submitted'] and record['url'] == 'confirmed'
