"""Video/audio format converter using ffmpeg.

Auto-detects available hardware encoders (NVIDIA NVENC, AMD AMF, Intel
Quick Sync, CPU) and produces MP4 (H.264/H.265) or MP3 with sensible
quality presets.

Public API:
    detect_encoders() -> EncoderSupport
    convert(input, output, output_kind, codec, hw_accel, quality, support,
            log, cancel_flag) -> None
    CancelledError
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from report import fmt_duration, fmt_ratio, fmt_size, print_report, probe_duration, probe_resolution


# Each key: (display label, [preferred encoders for h264, hevc in order])
# First encoder in the list that ffmpeg actually has installed wins.
HW_ACCEL: dict[str, tuple[str, list[str]]] = {
    "cpu":    ("CPU (software)",   ["libx264", "libx265"]),
    "nvidia": ("NVIDIA NVENC",     ["h264_nvenc", "hevc_nvenc"]),
    "amd":    ("AMD AMF",          ["h264_amf",  "hevc_amf"]),
    "intel":  ("Intel QuickSync",  ["h264_qsv",  "hevc_qsv"]),
}


@dataclass
class EncoderSupport:
    """Summary of what ffmpeg can actually do on this machine."""
    available_keys: list[str] = field(default_factory=list)  # HW_ACCEL keys with ≥1 encoder
    encoders: set[str] = field(default_factory=set)          # all encoders ffmpeg reports
    h264: dict[str, str] = field(default_factory=dict)       # accel_key -> h264 encoder
    hevc: dict[str, str] = field(default_factory=dict)       # accel_key -> hevc encoder

    @property
    def has_ffmpeg(self) -> bool:
        return bool(self.encoders)


def _ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def _list_encoders() -> set[str]:
    p = _ffmpeg_path()
    if p is None:
        return set()
    try:
        out = subprocess.run(
            [p, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    # Each line looks like:  " V....D encoder_name   description..."
    return {m.group(1) for m in re.finditer(r"^\s*\S+\s+(\S+)\s", out.stdout, re.M)}


def detect_encoders() -> EncoderSupport:
    encs = _list_encoders()
    h264: dict[str, str] = {}
    hevc: dict[str, str] = {}
    avail: list[str] = []
    for key, (_, names) in HW_ACCEL.items():
        h = next((n for n in names if "h264" in n and n in encs), None)
        v = next((n for n in names if ("hevc" in n or "h265" in n) and n in encs), None)
        if h or v:
            avail.append(key)
        if h:
            h264[key] = h
        if v:
            hevc[key] = v
    return EncoderSupport(available_keys=avail, encoders=encs, h264=h264, hevc=hevc)


# --- Encoder picker ---------------------------------------------------------
# Note: probe_duration is now imported from report.py so all three
# modules (transcriber / converter / analyzer) use the same helper.
def _pick_encoder(codec: str, hw_accel: str, support: EncoderSupport,
                  log: Callable[[str], None]) -> str:
    """Choose the actual ffmpeg encoder name to use."""
    if hw_accel == "auto":
        for key in ("nvidia", "amd", "intel", "cpu"):
            if key not in support.available_keys:
                continue
            if codec == "h264" and key in support.h264:
                log(f"Aceleración: {HW_ACCEL[key][0]} → {support.h264[key]}")
                return support.h264[key]
            if codec == "hevc" and key in support.hevc:
                log(f"Aceleración: {HW_ACCEL[key][0]} → {support.hevc[key]}")
                return support.hevc[key]
        # Fallback
        chosen = "libx264" if codec == "h264" else "libx265"
        log(f"Aceleración: CPU (software) → {chosen}")
        return chosen

    if hw_accel == "cpu":
        return "libx264" if codec == "h264" else "libx265"

    # Specific hardware
    table = support.h264 if codec == "h264" else support.hevc
    if hw_accel in table:
        log(f"Aceleración: {HW_ACCEL[hw_accel][0]} → {table[hw_accel]}")
        return table[hw_accel]
    log(f"⚠ {HW_ACCEL[hw_accel][0]} no tiene encoder {codec.upper()} disponible, "
        f"usando CPU")
    return "libx264" if codec == "h264" else "libx265"


# --- Quality presets --------------------------------------------------------
def _quality_args(encoder: str, codec: str, quality: str) -> list[str]:
    """Return the ffmpeg args that control bitrate/quality for this encoder."""
    q = quality.lower()
    if encoder.startswith("libx264"):
        crf = {"baja": 28, "media": 23, "alta": 18}.get(q, 23)
        preset = {"baja": "fast", "media": "medium", "alta": "slow"}.get(q, "medium")
        return ["-c:v", encoder, "-preset", preset, "-crf", str(crf)]
    if encoder.startswith("libx265"):
        crf = {"baja": 30, "media": 26, "alta": 22}.get(q, 26)
        return ["-c:v", encoder, "-preset", "medium", "-crf", str(crf)]
    if "nvenc" in encoder:
        cq = {"baja": 28, "media": 23, "alta": 18}.get(q, 23)
        preset = {"baja": "p7", "media": "p4", "alta": "p1"}.get(q, "p4")
        return ["-c:v", encoder, "-preset", preset, "-rc", "vbr",
                "-cq", str(cq), "-b:v", "0"]
    if "amf" in encoder:
        qp = {"baja": 28, "media": 23, "alta": 18}.get(q, 23)
        return ["-c:v", encoder, "-rc", "cqp", "-qp_i", str(qp),
                "-qp_p", str(qp), "-quality", "balanced"]
    if "qsv" in encoder:
        gq = {"baja": 28, "media": 23, "alta": 18}.get(q, 23)
        return ["-c:v", encoder, "-preset", "medium",
                "-global_quality", str(gq)]
    return ["-c:v", encoder]


# --- Top-level: convert -----------------------------------------------------
class CancelledError(Exception):
    """Raised when the user cancels mid-conversion."""


def convert(
    input_path: Path,
    output_path: Path,
    output_kind: str,           # "mp4" or "mp3"
    codec: str,                 # "h264" or "hevc"  (only for mp4)
    hw_accel: str,              # "auto" | "cpu" | "nvidia" | "amd" | "intel"
    quality: str,               # "baja" | "media" | "alta"
    support: EncoderSupport,
    log: Callable[[str], None],
    cancel_flag: Callable[[], bool],
    progress: Optional[Callable[[float], None]] = None,  # 0.0..1.0
) -> None:
    """Run ffmpeg to produce mp4 (video) or mp3 (audio extraction).

    On success, emits a small structured mini-report through ``log()``
    with the chosen encoder, file-size delta, and effective speed.
    """
    if not support.has_ffmpeg:
        raise RuntimeError("ffmpeg no detectado en PATH. Instálalo y reinicia la terminal.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    in_duration = probe_duration(input_path)
    in_resolution = probe_resolution(input_path) if output_kind == "mp4" else None

    if output_kind == "mp3":
        cmd = _mp3_cmd(input_path, output_path, quality)
        duration = in_duration
        encoder_label = "libmp3lame"
    else:
        encoder = _pick_encoder(codec, hw_accel, support, log)
        cmd = _video_cmd(input_path, output_path, encoder, codec, quality)
        duration = in_duration
        # e.g. "NVIDIA NVENC (h264_nvenc)"
        for key, (label, names) in HW_ACCEL.items():
            if encoder in names:
                encoder_label = f"{label} ({encoder})"
                break
        else:
            encoder_label = encoder

    cmd[0] = _ffmpeg_path() or cmd[0]
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))

    t_total = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    out_time_us: Optional[int] = None
    last_pct_logged = -1.0
    tail: list[str] = []   # last ffmpeg lines, for the error message
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if cancel_flag():
                proc.terminate()
                raise CancelledError()
            line = raw.rstrip()
            if not line:
                continue

            if line.startswith("out_time_us="):
                try:
                    out_time_us = int(line.split("=", 1)[1])
                    if progress and duration and duration > 0:
                        pct = min(1.0, out_time_us / 1_000_000 / duration)
                        progress(pct)
                        # log every ~5% to avoid spam
                        if pct - last_pct_logged >= 0.05 or pct >= 1.0:
                            log(f"  {pct*100:5.1f}%")
                            last_pct_logged = pct
                except ValueError:
                    pass
                continue

            if "=" in line and " " not in line:
                continue  # other -progress key=value lines
            tail = (tail + [line])[-12:]
            # surface other ffmpeg output (errors etc.)
            if "error" in line.lower() or "warning" in line.lower():
                log(f"  {line}")

        proc.wait()
    except CancelledError:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _remove_partial(output_path)
        raise

    if proc.returncode != 0:
        _remove_partial(output_path)
        detail = "\n".join(tail)
        raise RuntimeError(
            f"ffmpeg terminó con código {proc.returncode}.\n{detail}")
    if progress:
        progress(1.0)
    log(f"✓ Guardado: {output_path}")

    # --- Mini-report ---
    t_total_d = time.monotonic() - t_total
    in_size = input_path.stat().st_size if input_path.exists() else 0
    out_size = output_path.stat().st_size if output_path.exists() else 0
    delta_pct = ((in_size - out_size) / in_size * 100) if in_size > 0 else 0
    # The arrow direction reflects the user-visible result:
    #   delta_pct > 0 → file got smaller (good for re-encodes)
    #   delta_pct < 0 → file got bigger (re-encode less efficient than the original)
    if delta_pct > 0:
        size_delta = f" · -{delta_pct:.0f}% tamaño"
    elif delta_pct < 0:
        size_delta = f" · +{abs(delta_pct):.0f}% tamaño"
    else:
        size_delta = ""
    rows_general: list[tuple[str, str]] = [
        ("Entrada", f"{input_path.name} ({fmt_size(in_size)}"
                   f"{(f' · {fmt_duration(in_duration)}') if in_duration else ''})"),
    ]
    if output_kind == "mp4":
        res = f"{in_resolution[0]}×{in_resolution[1]}" if in_resolution else "—"
        rows_general.append(("Resolución", res))
    rows_general.extend([
        ("Salida", f"{output_path.name} ({fmt_size(out_size)}{size_delta}"
                   f" · {encoder_label})"),
        ("Codec", f"{codec.upper() if output_kind == 'mp4' else 'MP3'} · calidad {quality}"),
    ])
    rows_speed: list[tuple[str, str]] = []
    if in_duration and in_duration > 0:
        rows_speed.append(("Velocidad", fmt_ratio(in_duration, t_total_d) + " realtime"))
    print_report(
        log,
        "CONVERSIÓN",
        fmt_duration(t_total_d),
        [
            rows_general,
            [
                ("Aceleración", encoder_label),
                ("Tiempo total", fmt_duration(t_total_d)),
            ] + rows_speed,
        ],
    )


# --- ffmpeg argv builders ---------------------------------------------------
def _video_cmd(input_path: Path, output_path: Path, encoder: str,
               codec: str, quality: str) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-progress", "pipe:1", "-nostats",
        "-i", str(input_path),
        *_quality_args(encoder, codec, quality),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",  # web-friendly
        str(output_path),
    ]


def _remove_partial(path: Path) -> None:
    """Delete a half-written output so it isn't mistaken for a result."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _mp3_cmd(input_path: Path, output_path: Path, quality: str) -> list[str]:
    # libmp3lame VBR: -q:a 0=best..9=worst. 2 ≈ 190 kbps.
    q = {"baja": "5", "media": "2", "alta": "0"}.get(quality.lower(), "2")
    # -progress is what makes the progress bar AND cancellation work: without
    # it ffmpeg only prints \r-terminated stats to stderr, the line reader
    # blocks until the end, and "Cancelar" does nothing until ffmpeg exits.
    return [
        "ffmpeg", "-y",
        "-progress", "pipe:1", "-nostats",
        "-i", str(input_path),
        "-vn",                       # no video
        "-c:a", "libmp3lame",
        "-q:a", q,
        str(output_path),
    ]