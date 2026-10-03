"""Sanitized report preview, local export and HTTPS submission."""

from __future__ import annotations

import json
import os
import re
import tempfile
from threading import Lock
import uuid
import zipfile
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .launcher_workers import LauncherJob, LauncherWorkCancelled

from .launcher_doctor import sanitize_support_value
from .launcher_platform import publish_file


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


# ponytail: one draft lock; use per-draft locks if separate drafts run at the same time
_draft_lock = Lock()


def save_report_draft(filename: Path, payload: dict, *, path: Path | None = None, submitted=False, url=None):
    with _draft_lock:
        filename.parent.mkdir(parents=True, exist_ok=True)
        if filename.exists():
            previous = json.loads(filename.read_text(encoding="utf-8"))
            if previous.get("submitted") and previous["payload"] != payload:
                raise ValueError("An attempted report is immutable. Retry the saved report before starting another.")
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
        message = ("Report confirmed: " + saved["url"] if saved.get("url") else
                   "Saved report restored. Retrying sends the same reviewed payload and report key.")
        return ProblemReport(path, saved["payload"], message, bool(saved.get("submitted")), saved.get("url"))
    path = None
    diagnostics = ""
    try:
        path = controller.create_support_bundle(
            Path.home() / f"DOOM-Eternal-Archipelago-support-{uuid.uuid4().hex[:12]}.zip", logs=logs,
        )
        with zipfile.ZipFile(path) as archive:
            document = json.loads(archive.read("doctor.json"))
            summary = {key: document[key] for key in ("version", "support_diagnostics", "support_condump",
                      "last_connection_error", "last_setup_failure", "log_provenance") if key in document}
            diagnostics = json.dumps(summary, ensure_ascii=False, indent=2)[:6000]
            names = [name for name in archive.namelist() if name.endswith(".log")]
            names.sort(key=lambda name: (not any(token in name.lower() for token in ("ap_client", "sentinel", "bridge")), name))
            for name in names[:5]:
                if name in archive.namelist():
                    with archive.open(name) as source:
                        content = source.read(256000).decode("utf-8", errors="replace")
                        diagnostics += f"\n{name}\n" + content[-1800:]
        message = "Review the sanitized text before sending. The support ZIP is available locally."
    except LauncherWorkCancelled:
        raise
    except Exception:
        message = "Support ZIP unavailable. Describe the problem and review the text before sending."
    if job is not None:
        job.check()
    payload = {"schema_version":1, "idempotency_key":str(uuid.uuid4()), "title":"Launcher problem",
                "description":"", "diagnostics":str(sanitize_support_value(diagnostics))[:15000]}
    if draft_path is not None:
        save_report_draft(draft_path, payload, path=path)
    return ProblemReport(path, payload, message)


def submission_endpoint(bundle: Path) -> str | None:
    document = json.loads((bundle / "data/report_endpoint.json").read_text(encoding="utf-8"))
    endpoint = document.get("endpoint")
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported report endpoint configuration")
    if endpoint is None:
        return None
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/v1/reports":
        raise ValueError("Report endpoint must be the project's HTTPS /v1/reports URL")
    return endpoint


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report_body(payload: dict) -> bytes:
    sanitized = sanitize_support_value(payload)
    if not sanitized.get("title", "").strip() or not sanitized.get("description", "").strip():
        raise ValueError("Enter a title and describe the problem")
    body = json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
    if len(body) > 32768:
        raise ValueError("Report is too large")
    return body


def submit_report(endpoint: str, payload: dict) -> str:
    body = report_body(payload)
    request = Request(endpoint, data=body, headers={"Content-Type":"application/json"}, method="POST")
    with build_opener(_NoRedirect).open(request, timeout=40) as response:
        result = json.loads(response.read(4097))
    url = result.get("url", "")
    if not re.fullmatch(r"https://github\.com/snowzzrra/DoomEternal-AP-Mod/issues/[1-9][0-9]*", url):
        raise ValueError("Report submission has not been confirmed")
    return url
