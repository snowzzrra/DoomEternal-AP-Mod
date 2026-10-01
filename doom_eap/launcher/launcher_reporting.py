"""Sanitized report preview, local export and HTTPS submission."""

from __future__ import annotations

import json
import re
import uuid
import zipfile
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .launcher_workers import LauncherJob, LauncherWorkCancelled

from .launcher_doctor import sanitize_support_value


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


def report_problem(controller: SupportReportGenerator, *, logs: list[str], job: LauncherJob | None = None) -> ProblemReport:
    if job is not None:
        job.check()
    path = None
    diagnostics = ""
    try:
        path = controller.create_support_bundle(
            Path.home() / f"DOOM-Eternal-Archipelago-support-{uuid.uuid4().hex[:12]}.zip", logs=logs,
        )
        with zipfile.ZipFile(path) as archive:
            for name in ("doctor.json", "launcher.log"):
                if name in archive.namelist():
                    with archive.open(name) as source:
                        diagnostics += f"\n{name}\n" + source.read(12000).decode("utf-8", errors="replace")
        message = "Review the sanitized text before sending. The support ZIP is available locally."
    except LauncherWorkCancelled:
        raise
    except Exception:
        message = "Support ZIP unavailable. Describe the problem and review the text before sending."
    if job is not None:
        job.check()
    payload = {"schema_version":1, "idempotency_key":str(uuid.uuid4()), "title":"Launcher problem",
               "description":"", "diagnostics":str(sanitize_support_value(diagnostics))[:8000]}
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


def submit_report(endpoint: str, payload: dict) -> str:
    sanitized = sanitize_support_value(payload)
    if not sanitized.get("title", "").strip() or not sanitized.get("description", "").strip():
        raise ValueError("Enter a title and describe the problem")
    body = json.dumps(sanitized, ensure_ascii=False).encode("utf-8")
    if len(body) > 32768:
        raise ValueError("Report is too large")
    request = Request(endpoint, data=body, headers={"Content-Type":"application/json"}, method="POST")
    with build_opener(_NoRedirect).open(request, timeout=15) as response:
        result = json.loads(response.read(4097))
    url = result.get("url", "")
    if not re.fullmatch(r"https://github\.com/snowzzrra/DoomEternal-AP-Mod/issues/[1-9][0-9]*", url):
        raise ValueError("Report submission has not been confirmed")
    return url
