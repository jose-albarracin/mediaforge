"""Audio/video transcription using ffmpeg + faster-whisper.

This module has no GUI dependency so it can be reused or tested in
isolation. The high-level entry point is `transcribe_video`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Shared mini-report renderer and probe helpers. We import the module
# lazily inside ``transcribe_video`` so that ``transcriber`` can still
# be imported in isolation (e.g. by a tool that doesn't want the GUI
# helper to be present).
from report import fmt_duration, fmt_ratio, fmt_size, print_report, probe_duration


# Suppress huggingface_hub's "unauthenticated requests" and Windows-symlink
# warnings before faster-whisper imports it. These are advisory only; the
# library still works. Setting the var at import-time avoids noisy logs.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")


def _register_nvidia_wheels() -> list[str]:
    """Prepend NVIDIA wheel bin directories to PATH and load dirs.

    ctranslate2 dynamically loads ``cublas64_12.dll`` (and cuDNN /
    runtime) from the PATH on Windows. The official fix is to install
    the ``nvidia-cublas-cu12``, ``nvidia-cudnn-cu12`` and
    ``nvidia-cuda-runtime-cu12`` wheels — they ship the DLLs inside
    ``site-packages/nvidia/*/bin``. We make those findable here.

    Why we do it at import time, not lazily: ctranslate2 resolves its
    dependencies the first time anything touches CUDA. If the cuBLAS
    DLL isn't on PATH *before* that first touch, we get the cryptic
    ``Library cublas64_12.dll is not found or cannot be loaded`` and
    fall back to CPU without ever knowing the GPU was actually usable.

    Returns the list of bin directories added (empty if the wheels are
    not installed or we are on a non-Windows platform).
    """
    added: list[str] = []
    if sys.platform != "win32":
        return added
    try:
        import site  # noqa: PLC0415  (only used on Windows)
        sps = site.getsitepackages()
    except Exception:
        sps = [p for p in sys.path if "site-packages" in p]
    nvidia_root = None
    for sp in sps:
        candidate = Path(sp) / "nvidia"
        if candidate.is_dir():
            nvidia_root = candidate
            break
    if nvidia_root is None:
        return added
    for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc",
                "cuda_cupti", "cuda_nvtx"):
        bin_dir = nvidia_root / sub / "bin"
        if not bin_dir.is_dir():
            continue
        bin_str = str(bin_dir)
        # Modern Python on Windows honours AddDllDirectory, which keeps
        # DLL search order explicit and avoids surprising conflicts with
        # NVIDIA driver DLLs of a different version on PATH.
        try:
            os.add_dll_directory(bin_str)
        except (AttributeError, OSError):
            pass
        # We still prepend to PATH as belt-and-braces: older ctranslate2
        # builds or out-of-tree CUDA libs (e.g. CUDA Toolkit) load by
        # PATH search.
        current = os.environ.get("PATH", "")
        if bin_str not in current.split(os.pathsep):
            os.environ["PATH"] = bin_str + os.pathsep + current
        added.append(bin_str)
    return added


# Must run BEFORE ctranslate2 is ever imported, otherwise the missing
# DLL becomes a hard ImportError-equivalent at first model load.
_NVIDIA_BIN_PATHS = _register_nvidia_wheels()


# Module-level cache so the model is loaded only once per session.
_MODEL_CACHE: dict[tuple[str, str], object] = {}


class CancelledError(Exception):
    """Raised when the user cancels mid-transcription."""


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


# ---- Compute support detection (CUDA via ctranslate2) ---------------------
@dataclass
class ComputeSupport:
    """What compute devices faster-whisper can use on this machine.

    faster-whisper is built on ctranslate2, which only supports NVIDIA
    GPUs via CUDA. AMD (ROCm) and Intel (oneAPI / Quick Sync) are NOT
    supported. Detection is safe: we never try to load a model or
    initialize CUDA, so a missing cuBLAS DLL won't crash the app.
    """
    cuda_available: bool = False
    cuda_device_count: int = 0

    @property
    def has_cuda(self) -> bool:
        return self.cuda_available and self.cuda_device_count > 0


def detect_compute() -> ComputeSupport:
    """Probe ctranslate2 for available compute devices.

    Safe to call at app startup: it just asks the library how many
    CUDA devices are visible, without loading any model. Returns
    `ComputeSupport(cuda_available=False)` if ctranslate2 is missing,
    CUDA isn't installed, no GPU is present, or the cuBLAS DLLs that
    ship with the ``nvidia-cublas-cu12`` wheel are not findable.

    Note: ctranslate2 renamed ``get_device_count("cuda")`` to
    ``get_cuda_device_count()``. We try both for forward compatibility
    with older 3.x releases.
    """
    try:
        import ctranslate2
        if hasattr(ctranslate2, "get_cuda_device_count"):
            count = ctranslate2.get_cuda_device_count()
        else:
            count = ctranslate2.get_device_count("cuda")  # type: ignore[attr-defined]
        return ComputeSupport(
            cuda_available=count > 0,
            cuda_device_count=count,
        )
    except Exception:
        return ComputeSupport(cuda_available=False, cuda_device_count=0)


def check_ffmpeg() -> tuple[bool, str]:
    """Return (ok, message). ok=True means ffmpeg is callable."""
    path = shutil.which("ffmpeg")
    if path is None:
        return False, ("ffmpeg no está en PATH. Instálalo con "
                       "'winget install Gyan.FFmpeg' o descarga desde "
                       "https://ffmpeg.org y reinicia la terminal.")
    return True, path


def extract_audio(input_path: Path, output_path: Path, log: LogFn) -> None:
    """Extract mono 16 kHz audio from any ffmpeg-supported file."""
    cmd = [
        "ffmpeg",
        "-y",                # overwrite output
        "-i", str(input_path),
        "-vn",               # drop video stream
        "-ac", "1",          # mono
        "-ar", "16000",      # 16 kHz — what Whisper expects
        "-f", "wav",
        str(output_path),
    ]
    log(f"$ ffmpeg -i {input_path.name} -> {output_path.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.splitlines()[-12:])
        raise RuntimeError(f"ffmpeg falló (código {proc.returncode}):\n{tail}")


def _load_model(
    model_size: str,
    device: str = "cpu",
    log: Optional[LogFn] = None,
):
    """Lazy-load and cache a faster-whisper WhisperModel.

    `device` accepts:
      - "cpu": force CPU
      - "cuda": force NVIDIA GPU via CUDA (fails with a clear error
        if CUDA Toolkit or the GPU isn't actually usable)
      - "auto": try CUDA first; if it fails, fall back to CPU with a
        log message. This is the recommended default for end users
        who don't know whether they have a working CUDA install.

    compute_type is "int8" for both CPU and GPU: it halves RAM/VRAM
    usage and is faster than float16 on most hardware, with no
    measurable loss in transcription quality for Spanish/English.
    """
    if device == "auto":
        # Try CUDA first, fall back to CPU. The recursion bottoms out
        # at "cpu" which always works (ctranslate2's CPU backend is
        # self-contained, no external libs required).
        try:
            return _load_model(model_size, "cuda", log)
        except Exception as e:
            if log:
                log(f"[i] GPU no disponible ({type(e).__name__}: {e}); "
                    f"usando CPU.")
            return _load_model(model_size, "cpu", log)

    key = (model_size, device)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        compute_type = "int8"
        msg = f"Cargando modelo '{model_size}' en {device} ({compute_type})…"
        print(msg, flush=True)
        if log:
            log(msg)
        try:
            _MODEL_CACHE[key] = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
            )
        except Exception as e:
            if device == "cuda":
                # ctranslate2 loads cuBLAS/cuDNN/cuda_runtime from PATH at
                # model construction. The two usual failure modes on
                # Windows are 1) the NVIDIA driver is installed but the
                # official CUDA Toolkit isn't, or 2) the toolkit is
                # installed but is a version that doesn't match what
                # ctranslate2 expects (currently cuBLAS 12.x).
                # The user-friendly fix for both is to install the small
                # NVIDIA Python wheels that ship the right DLLs:
                #   pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 \
                #               nvidia-cuda-runtime-cu12
                msg = (
                    "No se pudo cargar el modelo en CUDA. El driver "
                    "NVIDIA está instalado (porque detectamos una GPU), "
                    "pero ctranslate2 no encuentra las DLLs de CUDA "
                    "(cublas64_12.dll / cudnn64_9.dll).\n"
                    "Solución recomendada (no requiere CUDA Toolkit "
                    "completo, solo ~1.4 GB de wheels Python):\n"
                    "  py -3 -m pip install "
                    "nvidia-cublas-cu12 nvidia-cudnn-cu12 "
                    "nvidia-cuda-runtime-cu12\n"
                    "Después reinicia la app. Si ya lo hiciste y sigue "
                    "fallando, instala CUDA Toolkit 12.x desde "
                    "https://developer.nvidia.com/cuda-downloads (más "
                    "pesado pero soporta cualquier versión de ctranslate2)."
                )
                raise RuntimeError(f"{msg}\nDetalle: {type(e).__name__}: {e}") from e
            raise
    return _MODEL_CACHE[key]


def transcribe(
    audio_path: Path,
    model_size: str,
    language: Optional[str],
    log: LogFn,
    cancel_flag: CancelFn,
    device: str = "cpu",
) -> str:
    """Transcribe an audio file and return plain text (segments joined by \\n).

    Convenience wrapper around ``_load_model`` + ``_run_inference`` for
    callers that don't need to time the model-load and inference phases
    separately. ``transcribe_video`` below uses them split for the
    mini-report.
    """
    model = _load_model(model_size, device=device, log=log)
    text, _info, _n = _run_inference(model, audio_path, language, log, cancel_flag)
    return text


def _run_inference(
    model,
    audio_path: Path,
    language: Optional[str],
    log: LogFn,
    cancel_flag: CancelFn,
) -> tuple[str, object, int]:
    """Run inference on an already-loaded WhisperModel.

    Returns ``(text, info, n_segments)``. ``info`` is the faster-whisper
    ``TranscriptionInfo`` — its ``.duration`` is the audio length
    actually fed to the model (after VAD), which we use for the speed
    ratio in the mini-report.
    """
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        vad_filter=True,   # skip silent stretches — large speedup on talks
    )
    log(f"Detectado: idioma={info.language}, "
        f"prob={info.language_probability:.2f}, "
        f"duración={info.duration:.1f}s")

    chunks: list[str] = []
    n = 0
    for i, segment in enumerate(segments_iter, 1):
        if cancel_flag():
            raise CancelledError()
        text = segment.text.strip()
        if text:
            chunks.append(text)
            n += 1
            preview = text if len(text) <= 80 else text[:77] + "…"
            log(f"  [{i:>4}] {segment.start:7.2f}s → {segment.end:7.2f}s  {preview}")
    return "\n".join(chunks), info, n

    return "\n".join(chunks)


def transcribe_video(
    input_path: Path,
    output_path: Path,
    model_size: str,
    language: Optional[str],
    log: LogFn,
    cancel_flag: CancelFn,
    device: str = "cpu",
) -> None:
    """End-to-end: extract audio from a video/audio file, transcribe, write .txt.

    The temp .wav is created in a TemporaryDirectory and cleaned up automatically.
    `device` defaults to "cpu"; pass "cuda" if you have a working NVIDIA+CUDA
    install (much faster).

    On success, emits a small structured mini-report through ``log()``
    with phase timings and a realtime-speed ratio. The report is
    text-only (no popup) so it stays in the GUI log pane and can be
    copied with the existing "📋 Copiar log" button.
    """
    t_total = time.monotonic()

    # Probe input duration up-front for the speed ratio. ffprobe is
    # already on PATH if ffmpeg is (the app refuses to start without
    # ffmpeg), so this is ~50 ms even on hour-long files.
    input_duration = probe_duration(input_path)

    # -- Phase 1: extract audio (into a tempdir that stays alive for
    # the whole run, because the inference phase needs the wav file)
    log(f"[1/3] Extrayendo audio de {input_path.name}…")
    t = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="transcriptor_") as tmp:
        wav_path = Path(tmp) / "audio.wav"
        extract_audio(input_path, wav_path, log)
        if cancel_flag():
            raise CancelledError()
        t_audio = time.monotonic() - t

        # -- Phase 2: load model
        log(f"[2/3] Cargando modelo '{model_size}' ({device})…")
        t = time.monotonic()
        model = _load_model(model_size, device=device, log=log)
        t_load = time.monotonic() - t

        # -- Phase 3: run inference
        log(f"[3/3] Transcribiendo…")
        t = time.monotonic()
        text, info, n_segments = _run_inference(
            model, wav_path, language, log, cancel_flag,
        )
        t_trans = time.monotonic() - t
    # tmpdir auto-cleaned here, after inference has read the wav

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text + "\n", encoding="utf-8")
    log(f"✓ Guardado: {output_path}")

    t_total = time.monotonic() - t_total
    in_size = input_path.stat().st_size if input_path.exists() else 0
    out_size = output_path.stat().st_size if output_path.exists() else 0
    # ``info.duration`` is the audio actually fed to the model (after
    # VAD stripping). Falls back to the ffprobe duration if the model
    # didn't report it.
    audio_done = getattr(info, "duration", 0.0) or 0.0
    if audio_done <= 0 and input_duration is not None:
        audio_done = input_duration
    print_report(
        log,
        "TRANSCRIPCIÓN",
        fmt_duration(t_total),
        [
            [
                ("Vídeo",   f"{input_path.name} ({fmt_size(in_size)}"
                            f"{(f' · {fmt_duration(input_duration)}') if input_duration else ''})"),
                ("Modelo",  f"{model_size} · {language or 'auto-detect'} · {device}"),
                ("Salida",  f"{output_path.name} ({fmt_size(out_size)})"),
            ],
            [
                ("Extracción audio", fmt_duration(t_audio)),
                ("Carga modelo",     fmt_duration(t_load)),
                ("Transcripción",    fmt_duration(t_trans)),
            ],
            [
                ("Segmentos",        str(n_segments)),
                ("Audio transcrito", fmt_duration(audio_done)),
                ("Velocidad",        fmt_ratio(audio_done, t_trans) + " realtime"),
            ],
        ],
    )