"""Grabar clase: record what a window shows and plays, then build the
document (screenshots + transcript) from that recording.

Nothing is downloaded from the page. On macOS the helper
``bin/heimdall-capture`` (capture/heimdall_capture.swift) uses Apple's
ScreenCaptureKit, the same API as the system screen recorder. Content the
system refuses to capture (DRM-protected video) arrives black or silent;
``assess`` detects that and tells the user. Heimdall never tries to get
around it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent
HELPER = ROOT / "bin" / "heimdall-capture"
HELPER_SRC = ROOT / "capture" / "heimdall_capture.swift"

LogFn = Callable[[str], None]

# Tuning for the protection check. A frame is "black" when at least
# BLACK_FRACTION of its pixels are near black; audio is "playing" when the
# loudness (RMS) of a one-second window reaches SOUND_RMS.
BLACK_FRACTION = 0.95
SOUND_RMS = 0.003
PROBE_SECONDS = 10


class CaptureError(RuntimeError):
    """The helper could not list or record (permission, missing tool…)."""


class CancelledError(Exception):
    """The user discarded the recording."""


# ---- availability ----------------------------------------------------------
def platform_note() -> Optional[str]:
    """None when recording can work here, else the reason it can't."""
    if sys.platform != "darwin":
        return "Por ahora grabar una clase solo funciona en macOS 13 o más nuevo."
    try:
        major = int(subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                                   text=True, check=True).stdout.split(".")[0])
    except (OSError, ValueError, subprocess.CalledProcessError):
        major = 13
    if major < 13:
        return "Grabar una clase necesita macOS 13 (Ventura) o más nuevo."
    if not HELPER.exists() and not shutil.which("swiftc"):
        return ("Falta la herramienta de grabación. Instala las herramientas de "
                "desarrollo de Apple con 'xcode-select --install' y vuelve a abrir la app.")
    return None


def ensure_helper(log: LogFn = print) -> Path:
    """Compile the helper the first time (or when its source changed)."""
    if HELPER.exists() and HELPER.stat().st_mtime >= HELPER_SRC.stat().st_mtime:
        return HELPER
    log("Preparando la herramienta de grabación (solo la primera vez)…")
    proc = subprocess.run(["sh", str(ROOT / "tools" / "build_capture.sh")],
                          capture_output=True, text=True)
    if proc.returncode != 0 or not HELPER.exists():
        raise CaptureError("No se pudo preparar la herramienta de grabación:\n"
                           + (proc.stderr or proc.stdout)[-1500:])
    return HELPER


PERMISSION_HELP = (
    "macOS no dio permiso para grabar la pantalla. Ábrelo en Ajustes del Sistema → "
    "Privacidad y seguridad → Grabación de pantalla y audio del sistema, activa "
    "Heimdall (o la Terminal, si la abres desde ahí) y vuelve a abrir la app.")


def _error_text(event: dict) -> str:
    if event.get("code") == "permission":
        return PERMISSION_HELP
    return event.get("message") or "Error desconocido al grabar."


def _clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---- windows ---------------------------------------------------------------
@dataclass
class Window:
    id: int
    app: str
    title: str

    @property
    def label(self) -> str:
        title = self.title.strip() or "(sin título)"
        if len(title) > 60:
            title = title[:59] + "…"
        return f"{self.app} — {title}"


def list_windows() -> list[Window]:
    helper = ensure_helper()
    proc = subprocess.run([str(helper), "list"], capture_output=True, text=True, timeout=20)
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "error":
            raise CaptureError(_error_text(event))
        if event.get("event") == "windows":
            wins = [Window(w["id"], w["app"], w.get("title") or "")
                    for w in event["windows"]]
            # Browsers first: classes are usually watched in one.
            browsers = ("Chrome", "Safari", "Firefox", "Edge", "Brave", "Arc", "Opera")
            return sorted(wins, key=lambda w: (not any(b in w.app for b in browsers),
                                               w.app.lower(), w.title.lower()))
    raise CaptureError("No se pudo leer la lista de ventanas.\n" + proc.stderr[-800:])


# ---- protection check ------------------------------------------------------
@dataclass
class Verdict:
    """What the system lets Heimdall record from this window."""
    kind: str          # full | audio_only | video_only | paused | blocked | unknown
    video_ok: bool
    audio_ok: bool
    title: str
    detail: str


def assess(frames: list[dict], levels: list[dict]) -> Verdict:
    """Decide from a few seconds of capture whether picture and sound came
    through. ``frames``/``levels`` are the helper's frame/level events."""
    if len(frames) < 2 or len(levels) < 2:
        return Verdict("unknown", False, False, "No hubo tiempo para probar",
                       "Graba unos segundos más con la clase reproduciéndose.")
    black = sum(f["dark"] >= BLACK_FRACTION for f in frames) / len(frames)
    video_black = black >= 0.8
    moving = max(f["diff"] for f in frames[1:]) > 0.5
    loud = sorted(lv["rms"] for lv in levels)
    # Median-ish: sound in at least a third of the seconds, not one blip.
    sound = loud[len(loud) * 2 // 3] >= SOUND_RMS

    if sound and not video_black:
        return Verdict("full", True, True, "Se puede grabar completa",
                       "Llegan la imagen y el sonido: saldrá el documento con capturas "
                       "y la transcripción.")
    if sound and video_black:
        return Verdict("audio_only", False, True, "Solo se puede grabar el audio",
                       "El sonido llega pero la imagen sale negra: la plataforma protege "
                       "el vídeo. Saldrá la transcripción, sin capturas.")
    if not video_black and moving:
        return Verdict("video_only", True, False, "No llega el sonido",
                       "La imagen se ve pero el sonido sale mudo. Revisa que la pestaña "
                       "no esté silenciada y que el volumen esté arriba; si sigue igual, "
                       "la plataforma protege el audio y no se puede transcribir.")
    if not video_black:
        return Verdict("paused", True, False, "La clase parece estar en pausa",
                       "No hubo sonido ni movimiento. Dale play a la clase y vuelve a probar.")
    return Verdict("blocked", False, False, "Esta clase no se puede grabar",
                   "La imagen sale negra y no llega sonido: la plataforma protege la "
                   "clase. Si estaba en pausa, dale play y vuelve a probar. Si no, usa "
                   "la transcripción o los subtítulos que ofrezca la plataforma.")


# ---- recording -------------------------------------------------------------
@dataclass
class Recording:
    """A running ``heimdall-capture record`` process and what it reported."""
    window: Window
    out_dir: Path
    interval_s: float = 2.0
    log: LogFn = print
    proc: Optional[subprocess.Popen] = None
    frames: list[dict] = field(default_factory=list)
    levels: list[dict] = field(default_factory=list)
    started_at: Optional[float] = None
    stopped: Optional[dict] = None
    error: Optional[str] = None
    interrupted: bool = False      # macOS stopped the capture; reconnecting
    interruptions: list[dict] = field(default_factory=list)
    _reader: Optional[threading.Thread] = None

    def start(self) -> None:
        helper = ensure_helper(self.log)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [str(helper), "record", "--window", str(self.window.id),
             "--out", str(self.out_dir), "--interval", str(self.interval_s),
             "--stdin-stop"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + 15
        while self.started_at is None and self.error is None:
            if time.monotonic() > deadline or self.proc.poll() is not None:
                err = self.proc.stderr.read() if self.proc.poll() is not None \
                    and self.proc.stderr else ""
                self.error = self.error or ("La grabación no arrancó. " + err[-800:])
                break
            time.sleep(0.05)
        if self.error:
            self.stop()
            raise CaptureError(self.error)

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("event")
            if kind == "frame":
                self.frames.append(event)
            elif kind == "level":
                self.levels.append(event)
            elif kind == "started":
                self.started_at = time.monotonic()
                self.log(f"Grabando la ventana «{self.window.label}».")
            elif kind == "interrupted":
                self.interrupted = True
                self.interruptions.append(event)
                self.log(f"[!] [{_clock(event.get('t', 0))}] macOS detuvo la grabación "
                         f"({event.get('message', '')}). Reintentando…")
            elif kind == "resumed":
                self.interrupted = False
                self.log(f"[OK] [{_clock(event.get('t', 0))}] Grabación retomada; se "
                         f"perdieron {event.get('gap', 0):.0f} s.")
            elif kind == "stopped":
                self.stopped = event
            elif kind == "error":
                self.error = _error_text(event)
                self.log("[!] " + self.error)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def current_level(self) -> float:
        return self.levels[-1]["rms"] if self.levels else 0.0

    def verdict(self, last_seconds: Optional[float] = None) -> Verdict:
        """Verdict for the whole recording, or only its last N seconds."""
        frames, levels = list(self.frames), list(self.levels)
        if last_seconds and levels:
            since = levels[-1]["t"] - last_seconds
            frames = [f for f in frames if f["t"] >= since]
            levels = [lv for lv in levels if lv["t"] >= since]
        return assess(frames, levels)

    def ever_had_sound(self) -> bool:
        return any(lv["rms"] >= SOUND_RMS for lv in self.levels)

    def stop(self) -> None:
        """Ask the helper to finish the files and wait for it."""
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin
                self.proc.stdin.write("stop\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        if self._reader:
            self._reader.join(timeout=5)


def probe(window: Window, work_dir: Path, log: LogFn = print,
          cancel_flag: Callable[[], bool] = lambda: False,
          seconds: float = PROBE_SECONDS) -> Verdict:
    """Record ``seconds`` of the window, throw the files away, return the verdict."""
    rec = Recording(window, work_dir, interval_s=1.0, log=log)
    rec.start()
    try:
        while rec.elapsed < seconds and rec.alive:
            if cancel_flag():
                raise CancelledError()
            time.sleep(0.2)
    finally:
        rec.stop()
        shutil.rmtree(work_dir, ignore_errors=True)
    if rec.error and not rec.frames:
        raise CaptureError(rec.error)
    return rec.verdict()


# ---- from recording to document --------------------------------------------
def build_from_recording(
    rec: Recording,
    out_dir: Path,
    name: str,
    model_size: str = "small",
    language: Optional[str] = "es",
    device: str = "auto",
    want_doc: bool = True,
    window_s: float = 15.0,
    log: LogFn = print,
    cancel_flag: Callable[[], bool] = lambda: False,
    progress: Callable[[float], None] = lambda _p: None,
) -> dict[str, Optional[Path]]:
    """Transcribe the recorded audio and, when the picture came through,
    pair it with the captured screens, like "Documento con capturas"."""
    from analyzer import (KeyFrame, _compress_to_jpeg, build_enriched_pdf, correlate,
                          dedupe_keyframes, export_txt_with_timestamps,
                          transcribe_with_timestamps)
    from analyzer import CancelledError as AnalyzerCancelled

    wav = rec.out_dir / "audio.wav"
    # Over the whole recording the class may have been paused at times, so
    # only ask whether the picture came through (not black most of the time).
    shown = [f for f in rec.frames if f["dark"] < BLACK_FRACTION]
    picture_ok = len(shown) >= max(1, len(rec.frames) // 5)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{name}.txt"
    pdf_path = out_dir / f"{name}.pdf"

    if not wav.exists() or wav.stat().st_size <= 44:
        raise CaptureError("La grabación no tiene audio.")
    if not rec.ever_had_sound():
        log("[!] No se captó sonido; la transcripción puede salir vacía.")

    log(f"Transcribiendo con el modelo '{model_size}'…")
    progress(0.05)
    try:
        segments = transcribe_with_timestamps(wav, model_size, language, log,
                                              cancel_flag, device=device)
    except AnalyzerCancelled as exc:
        raise CancelledError() from exc
    progress(0.75)
    export_txt_with_timestamps(segments, txt_path)
    log(f"[OK] Texto: {txt_path}")
    result: dict[str, Optional[Path]] = {"txt": txt_path, "pdf": None}

    if want_doc and picture_ok:
        # Keep only real captures: black ones are the protected picture.
        saved = {f["file"]: f for f in rec.frames if f.get("saved") and f.get("file")}
        frames_dir = out_dir / f"{name}_capturas"
        frames_dir.mkdir(exist_ok=True)
        keyframes: list[KeyFrame] = []
        for jpg in sorted((rec.out_dir / "frames").glob("frame_*.jpg")):
            t = int(jpg.stem.split("_")[1]) / 1000
            info = saved.get(jpg.name)
            if info and info["dark"] >= BLACK_FRACTION:
                continue
            copy = frames_dir / jpg.name
            shutil.copy2(jpg, copy)
            dest = _compress_to_jpeg(copy, max_px=1280, quality=85)
            keyframes.append(KeyFrame(time=t, path=dest))
        keyframes = dedupe_keyframes(keyframes, perceptual_threshold=5,
                                     cluster_window_s=30, log=log, cancel_flag=cancel_flag)
        progress(0.85)
        blocks = correlate(segments, keyframes, window_s=window_s)
        if blocks:
            build_enriched_pdf(blocks, pdf_path, title=name, log=log)
            result["pdf"] = pdf_path
            log(f"[OK] PDF: {pdf_path} ({len(blocks)} capturas)")
        else:
            log("[!] No quedaron capturas útiles; solo se generó el texto.")
    elif want_doc:
        log("[!] La imagen estaba protegida o negra: solo se generó el texto.")
    progress(1.0)
    return result
