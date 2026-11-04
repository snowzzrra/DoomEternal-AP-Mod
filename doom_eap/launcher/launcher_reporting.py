"""Sanitized report preview, local drafts and GitHub issue links."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from doom_eap.launcher.ui.strings import text as player_text

from .launcher_doctor import sanitize_support_value
from .launcher_platform import publish_file
from .launcher_workers import LauncherJob, LauncherWorkCancelled


class SupportReportGenerator(Protocol):
    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None) -> Path: ...


@dataclass(frozen=True)
class ScopedSupportReport:
    create: Callable[..., Path]
    job: LauncherJob

    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None) -> Path:
        return self.create(destination, logs=logs, job=self.job)


@dataclass(frozen=True)
class ProblemReport:
    path: Path | None
    payload: dict
    message: str
    submitted: bool = False
    url: str | None = None


# shortcut: one draft lock, split it if separate drafts run at the same time
_draft_lock = Lock()


def save_report_draft(filename: Path, payload: dict, *, path: Path | None = None, submitted=False, url=None):
    with _draft_lock:
        filename.parent.mkdir(parents=True, exist_ok=True)
        if filename.exists():
            previous = json.loads(filename.read_text(encoding="utf-8"))
            if previous.get("submitted") and previous["payload"] != payload:
                raise ValueError(player_text('launcher.controller', 'retry_the_saved_report_before_starting_a_new_one_its_submitted_t'))
            submitted = submitted or previous.get("submitted", False)
            path = path or previous.get("path")
            url = url or previous.get("url")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".draft.", dir=filename.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"payload": sanitize_support_value(payload), "path": str(path) if path else None,
                       "submitted": submitted, "url": url}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        publish_file(temporary, filename, operation="report_draft_publish")


def report_problem(controller: SupportReportGenerator, *, logs: list[str], job: LauncherJob | None = None,
                   draft_path: Path | None = None) -> ProblemReport:
    if job is not None:
        job.check()
    if draft_path is not None and draft_path.exists():
        saved = json.loads(draft_path.read_text(encoding="utf-8"))
        path = Path(saved["path"]) if saved.get("path") else None
        message = (player_text('launcher.reporting', 'report_sent') + saved["url"] if saved.get("url") else
                   player_text('launcher.reporting', 'saved_report_reopened_review_the_text_before_opening_a_github_is'))
        return ProblemReport(path, saved["payload"], message, bool(saved.get("submitted")), saved.get("url"))
    path = None
    diagnostics = ""
    report_id = str(uuid.uuid4())
    try:
        path = controller.create_support_bundle(
            Path.home() / f"DOOM-Eternal-Archipelago-support-{uuid.uuid4().hex[:12]}.zip", logs=logs,
        )
        with zipfile.ZipFile(path) as archive:
            report_id = json.loads(archive.read("doctor.json"))["report_id"]
            diagnostics = archive.read("summary.txt").decode("utf-8")
        message = player_text('launcher.reporting', 'review_the_text_before_opening_a_github_issue_a_support_report_f')
    except LauncherWorkCancelled:
        raise
    except (OSError, ValueError, zipfile.BadZipFile):
        message = player_text('launcher.reporting', 'the_support_file_could_not_be_created_describe_the_problem_and_r')
    if job is not None:
        job.check()
    payload = {"schema_version":1, "idempotency_key":report_id, "title":player_text('launcher.reporting', 'launcher_problem'),
                "description":"", "diagnostics":str(sanitize_support_value(diagnostics))}
    if draft_path is not None:
        save_report_draft(draft_path, payload, path=path)
    return ProblemReport(path, payload, message)


def report_text(payload: dict) -> str:
    reviewed = sanitize_support_value(payload)
    return f"{reviewed['title']}\n\n{reviewed['description']}\n\n{reviewed['diagnostics']}".strip()


def github_issue_url(payload: dict) -> tuple[str, str | None]:
    reviewed = sanitize_support_value(payload)
    full_summary = report_text(reviewed)
    summary = full_summary
    params = {"template": "problem.yml", "title": str(reviewed["title"])[:120], "summary": summary}
    base = "https://github.com/snowzzrra/DoomEternal-AP-Mod/issues/new?"
    url = base + urlencode(params)
    if len(url.encode("utf-8")) < 2000:
        return url, None
    del params["summary"]
    return base + urlencode(params), full_summary


def submission_endpoint(bundle: Path) -> str | None:
    document = json.loads((bundle / "data/report_endpoint.json").read_text(encoding="utf-8"))
    endpoint = document.get("endpoint")
    if document.get("schema_version") != 1:
        raise ValueError(player_text('launcher.reporting', 'unsupported_report_endpoint_configuration'))
    if endpoint is None:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/v1/reports":
        raise ValueError(player_text('launcher.reporting', 'report_endpoint_must_be_the_project_s_https_v1_reports_url'))
    return endpoint


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report_body(payload: dict) -> bytes:
    sanitized = sanitize_support_value(payload)
    if not sanitized.get("title", "").strip() or not sanitized.get("description", "").strip():
        raise ValueError(player_text('launcher.reporting', 'enter_a_title_and_describe_the_problem'))
    body = json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
    if len(body) > 32768:
        raise ValueError(player_text('launcher.reporting', 'report_is_too_large_shorten_the_description_and_retry'))
    return body


def submit_report(endpoint: str, payload: dict) -> str:
    body = report_body(payload)
    request = Request(endpoint, data=body, headers={"Content-Type":"application/json"}, method='POST')
    with build_opener(_NoRedirect).open(request, timeout=40) as response:
        result = json.loads(response.read(4097))
    url = result.get("url", "")
    if not re.fullmatch(r"https://github\.com/snowzzrra/DoomEternal-AP-Mod/issues/[1-9][0-9]*", url):
        raise ValueError(player_text('launcher.reporting', 'sending_could_not_be_confirmed_retry_the_saved_report'))
    return url
