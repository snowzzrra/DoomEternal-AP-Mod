"""One-click player reporting using the launcher's canonical support bundle."""

from __future__ import annotations

import os
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PROBLEM_URL = "https://github.com/snowzzrra/DoomEternal-AP-Mod/issues/new?template=problem.yml"


class SupportReportGenerator(Protocol):
    def create_support_bundle(self, destination: Path, *, logs: list[str] | None = None) -> Path: ...


def reveal_support_report(path: Path) -> bool:
    """Select the exact generated file in Windows Explorer."""
    if os.name != "nt":
        return False
    subprocess.Popen(["explorer.exe", "/select,", str(path.resolve())])
    return True


@dataclass(frozen=True)
class ProblemReport:
    path: Path | None
    browser_opened: bool
    message: str


def report_problem(controller: SupportReportGenerator, *, logs: list[str]) -> ProblemReport:
    path = None
    try:
        path = controller.create_support_bundle(
            Path.home() / "DOOM-Eternal-Archipelago-support.zip", logs=logs,
        )
        messages = ["Support Report created."]
    except Exception:
        messages = ["Support Report generation failed. You can still report the problem without it."]

    try:
        browser_opened = bool(webbrowser.open(PROBLEM_URL))
    except Exception:
        browser_opened = False
    if browser_opened:
        messages.append("GitHub has been opened in your browser.")
    else:
        messages.append(f"Could not open your browser. Copy and open this link:\n{PROBLEM_URL}")

    if path is not None:
        try:
            revealed = reveal_support_report(path)
        except Exception:
            revealed = False
        if revealed:
            messages.append("Drag the highlighted Support Report into the issue and briefly describe what happened.")
        else:
            messages.append(f"Attach the Support Report from:\n{path}\nBriefly describe what happened.")
    else:
        messages.append("Briefly describe what happened and mention that the launcher could not generate a report.")
    return ProblemReport(path, browser_opened, "\n\n".join(messages))
