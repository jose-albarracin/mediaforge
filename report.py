"""Mini-report renderer shared by all Heimdall operations.

Each module (`transcriber`, `converter`, `analyzer`) calls
``print_report()`` at the end of a run with a structured summary.
The report is emitted through the same ``log()`` callback the rest
of the operation uses, so it lands inline in the GUI log pane (and
in stdout when the modules are run directly).

Why a helper and not a custom widget: keeping the report as plain
text means the user can ``📋 Copiar log`` it, paste it in a chat,
and we don't have to re-implement scrolling/selection logic.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, Union


# --- Formatting primitives --------------------------------------------------
def fmt_duration(seconds: float) -> str:
    """Format a wall-clock duration as ``1.2s`` / ``1m 05s`` / ``1h 02m 03s``."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        if seconds < 10:
            return f"{seconds:.1f}s"
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def fmt_size(num_bytes: int) -> str:
    """Format a byte count as ``120 B`` / ``3.2 KB`` / ``21.0 MB`` / ``1.4 GB``."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_ratio(numerator: float, denominator: float) -> str:
    """Format a ratio like "1.2×" or "—" if it doesn't make sense."""
    if denominator <= 0 or numerator <= 0:
        return "—"
    r = numerator / denominator
    if r >= 100:
        return f"{r:.0f}×"
    if r >= 10:
        return f"{r:.1f}×"
    return f"{r:.2f}×"


# --- Media probing helpers (shared across modules) -------------------------
def probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds, or None if ffprobe is missing /
    the file can't be probed. Cheap (one ffmpeg invocation, ~50 ms)."""
    probe = shutil.which("ffprobe")
    if probe is None:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def probe_resolution(path: Path) -> Optional[tuple[int, int]]:
    """Return (width, height) of the first video stream, or None."""
    probe = shutil.which("ffprobe")
    if probe is None:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        first = out.stdout.strip().splitlines()
        if len(first) < 2:
            return None
        return int(first[0]), int(first[1])
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


# A row is (key, value). A section is a list of rows. A report is a list of
# sections, each separated by a blank line.
Row = tuple[str, str]
Section = list[Row]
ReportSpec = Union[Section, list[Section]]


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _wrap_words(text: str, width: int) -> list[str]:
    """Wrap ``text`` to fit lines of at most ``width`` characters,
    breaking on whitespace. Each returned line is ``<= width`` chars.

    Long individual tokens (e.g. URLs) longer than ``width`` are
    hard-cut at ``width`` chars. The caller is expected to pass
    ``width == column_width`` so the wrapped lines fit exactly under
    the report's value column.
    """
    if not text:
        return []
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(w) > width:
            if cur:
                lines.append(cur)
                cur = ""
            for i in range(0, len(w), width):
                lines.append(w[i:i + width])
            continue
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur = cur + " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def print_report(
    log: Callable[[str], None],
    title: str,
    subtitle: str,
    spec: ReportSpec,
    width: int = 64,
) -> None:
    """Print a small monospaced report box through ``log``.

    Parameters
    ----------
    log
        Callback that receives one line at a time. Typically the same
        ``log`` passed to the rest of the operation.
    title
        Short uppercase tag, e.g. ``"TRANSCRIPCIÓN"``.
    subtitle
        Compact headline, e.g. ``"16.5s total"``. Goes next to the
        title on the first row.
    spec
        Either a flat list of ``(key, value)`` rows, or a list of
        sections (each a list of rows). Sections are separated by a
        blank line for visual grouping.
    width
        Inner width in characters. 64 fits the default Enriched-tab
        log pane without horizontal scrolling on a 1280-px window.
    """
    # Normalize: a flat row list becomes a single-section report.
    if not spec:
        sections: list[Section] = [[]]
    elif isinstance(spec[0], tuple):
        sections = [list(spec)]  # type: ignore[arg-type]
    else:
        sections = [list(s) for s in spec]  # type: ignore[arg-type]

    inner = width
    top    = "┌" + "─" * (inner + 2) + "┐"
    sep    = "├" + "─" * (inner + 2) + "┤"
    bottom = "└" + "─" * (inner + 2) + "┘"

    def line(content: str) -> str:
        # Box a single line of content. We allow content up to ``inner``
        # chars and hard-truncate anything longer — callers should
        # pre-wrap so this never triggers for normal reports.
        return f"│ {(content[:inner]).ljust(inner)} │"

    # Layout: fixed-width label column followed by left-aligned value.
    # This way continuation lines can use the same value-column width
    # without any of the right-alignment width-wastage that broke the
    # wrap on long values.
    label_w = 22
    indent = " " * label_w

    log(top)
    title_line = f"{title}  ·  {subtitle}"
    log(line(title_line))
    log(sep)

    for gi, group in enumerate(sections):
        if gi > 0:
            log(line(""))  # blank separator line
        for key, val in group:
            label = f"  {key}:".ljust(label_w)
            if not val:
                log(line(label))
                continue
            value_col = inner - label_w
            if value_col < 12:
                # Pathological narrow width: label and value on
                # separate lines, value wraps independently.
                log(line(label))
                for chunk in _wrap_words(val, inner):
                    log(line(chunk))
                continue
            wrapped = _wrap_words(val, value_col)
            # First wrapped chunk shares the row with the label, the
            # rest are indented to align with the value column.
            head, *rest = wrapped
            log(line(label + head))
            for chunk in rest:
                log(line(indent + chunk))

    log(bottom)
