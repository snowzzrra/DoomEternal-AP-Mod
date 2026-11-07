import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from doom_eap.launcher import launcher_reporting as reporting


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
