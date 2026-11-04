"""Prepare reviewed support text and fallback links; no Qt or network I/O."""

from ..launcher_doctor import sanitize_support_value
from ..launcher_reporting import github_issue_url, report_text


class ReportPresenter:
    def __init__(self, event):
        self.payload = sanitize_support_value(dict(event["payload"]))

    def document(self, title, description):
        return sanitize_support_value({**self.payload, "title": title[:120], "description": description[:8000]})

    issue = staticmethod(github_issue_url)
    copy = staticmethod(report_text)
