"""Multimodal analyzer: correlate audio transcription with video keyframes
and produce a navigable PDF (plus optional JSON / .txt / frames folder).

Pipeline (v1):
    1. Extract audio from the input video.
    2. Transcribe with faster-whisper (returns segments with timestamps).
    3. Extract keyframes from the video using ffmpeg scene detection
       (hybrid: scene-change OR fixed interval).
    4. For each keyframe, collect transcript segments in a ±N-second window
       around it → "EnrichedBlock".
    5. Build a PDF (one page per block), an optional JSON, and an optional
       .txt with [hh:mm:ss] prefixes.

Future v2 will add OCR of on-screen text. v3 will add LLM-based summary
and diarization. See README for the roadmap.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from report import (
    fmt_duration,
    fmt_ratio,
    fmt_size,
    print_report,
    probe_duration,
)


# Reuse the Windows cuBLAS/cuDNN DLL path registration that
# transcriber.py runs at import-time. This way ``analyzer.py`` can be
# imported standalone (e.g. by tests) and still find the NVIDIA wheel
# DLLs without each caller having to remember to set PATH first.
try:
    from transcriber import _register_nvidia_wheels  # type: ignore
    _register_nvidia_wheels()
except Exception:
    # transcriber not importable (e.g. running this file in isolation
    # without the package layout). Detection in transcribe() below will
    # still surface a clean error message.
    pass


# Realistic upper bound for the scene-change threshold. Above ~0.5,
# most real-world videos (Teams recordings, screen-shares, etc.) produce
# zero frames because no transition reaches that score. The CLI/UI should
# also clamp to this value.
MAX_SCENE_THRESHOLD = 0.5


# ---- Data classes ----------------------------------------------------------
@dataclass
class KeyFrame:
    time: float            # seconds
    path: Path             # PNG file


@dataclass
class TranscriptSegment:
    start: float           # seconds
    end: float             # seconds
    text: str


@dataclass
class EnrichedBlock:
    time: float            # seconds
    frame_path: Path
    transcript_text: str    # joined text from the surrounding window
    segment_count: int = 0


# ---- Callbacks -------------------------------------------------------------
LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
ProgressFn = Callable[[float], None]   # 0.0 .. 1.0

_NOP = lambda *_a, **_k: None  # placeholder when no callback supplied


# ---- Cancellation ----------------------------------------------------------
class CancelledError(Exception):
    """Raised when the user cancels the enrichment."""


# ---- ffmpeg helpers --------------------------------------------------------
def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


# Note: probe_duration is imported from report.py (shared helper).


# ---- 1. Frame extraction ---------------------------------------------------
def extract_keyframes(
    input_path: Path,
    output_dir: Path,
    mode: str = "hybrid",                # "hybrid" | "scene" | "interval"
    scene_threshold: float = 0.4,
    interval_s: float = 15.0,
    log: LogFn = _NOP,
    image_quality: str = "medium",       # "high" | "medium" | "low"
) -> list[KeyFrame]:
    """Use ffmpeg + showinfo to extract frames with their real timestamps.

    The filter `select='gt(scene,T)'` triggers ONLY when the scene score of
    a frame is strictly greater than T. The score is a 0-1 statistical
    measure of how much a frame differs from its predecessor:

        < 0.05  →  virtually static (compressed screen-share, no cuts)
        0.05-0.15 →  tiny UI changes (mouse, animations)
        0.15-0.30 →  noticeable (slide changes within a template)
        0.30-0.50 →  clear transitions (slide → slide, app switch)
        > 0.50  →  hard cuts, intros (rare in screen-share content)

    For interval mode we use `-vf fps=1/N`, the cleanest way to get
    exactly one frame every N seconds.
    """
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("ffmpeg no detectado en PATH.")

    # Clamp to the realistic max. Doing this here too (besides the UI) so
    # direct callers / tests don't break unexpectedly.
    if mode in ("scene", "hybrid") and scene_threshold > MAX_SCENE_THRESHOLD:
        log(f"[i] Threshold {scene_threshold} por encima del maximo realista "
            f"({MAX_SCENE_THRESHOLD}) - se ajusta.")
        scene_threshold = MAX_SCENE_THRESHOLD

    output_dir.mkdir(parents=True, exist_ok=True)
    # clean previous run
    for old in output_dir.glob("frame_*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    pattern = output_dir / "frame_%05d.png"

    if mode == "scene":
        # Scene score: pick every frame whose score > threshold.
        expr = f"gt(scene\\,{scene_threshold:g})"
        cmd = [
            ffmpeg, "-y", "-i", str(input_path),
            "-vf", f"select='{expr}',showinfo",
            "-vsync", "vfr",
            str(pattern),
        ]
    elif mode == "interval":
        # One frame every N seconds. `fps=1/N` is the cleanest expression:
        # it samples the video stream exactly that rate, no overflow.
        cmd = [
            ffmpeg, "-y", "-i", str(input_path),
            "-vf", f"fps=1/{interval_s:g},showinfo",
            str(pattern),
        ]
        expr = f"fps=1/{interval_s:g}"  # for logging only
    else:  # hybrid: scene OR interval-boundary frames.
        # For interval part of the hybrid we grab only the FIRST frame of
        # each window (the rest is redundant), so the total stays sane.
        # `lt(t-floor(t/N)*N, 1*eps)` picks t mod N in [0, eps). Empirically
        # eps = 0.04 captures ~1 frame at common framerates.
        eps = 0.04
        expr = (
            f"gt(scene\\,{scene_threshold:g})"
            f"+lt(mod(t\\,{interval_s:g})\\,{eps:g})"
        )
        cmd = [
            ffmpeg, "-y", "-i", str(input_path),
            "-vf", f"select='{expr}',showinfo",
            "-vsync", "vfr",
            str(pattern),
        ]

    log(f"$ ffmpeg ... {expr}")
    proc = subprocess.run(cmd, capture_output=True, text=True)

    files = sorted(output_dir.glob("frame_*.png"))

    # Auto-fallback for scene-only mode: if the user picked a threshold
    # that's still too aggressive for this video, try a much lower one
    # before giving up. Many screen-share recordings have a max score
    # in the 0.01-0.10 range and would return zero with default settings.
    SCENE_FLOOR = 0.01
    if not files and mode == "scene" and scene_threshold > SCENE_FLOOR:
        log(f"[i] Threshold {scene_threshold:g} no encontro cambios. "
            f"Reintentando con el minimo ({SCENE_FLOOR:g}) para no "
            f"quedarnos sin nada...")
        retry = SCENE_FLOOR
        cmd2 = [
            ffmpeg, "-y", "-i", str(input_path),
            "-vf", f"select='gt(scene\\,{retry:g})',showinfo",
            "-vsync", "vfr",
            str(pattern),
        ]
        # Clean the would-be output before retry.
        for old in output_dir.glob("frame_*.png"):
            try:
                old.unlink()
            except OSError:
                pass
        proc = subprocess.run(cmd2, capture_output=True, text=True)
        files = sorted(output_dir.glob("frame_*.png"))
        if files:
            log(f"[OK] Fallback: {len(files)} frames con threshold "
                f"{retry:g}. (Tu video tiene cambios visuales muy sutiles; "
                f"considera usar modo 'Intervalo fijo' para mas cobertura.)")

    if not files:
        tail = "\n".join(proc.stderr.splitlines()[-15:])
        hint = (
            "  > El video no tiene cambios de escena detectables ni "
            "siquiera con threshold 0.01. Esto es comun en grabaciones de "
            "Teams/Zoom con pantalla compartida continua.\n"
            "  > SOLUCION RECOMENDADA: cambia el modo a 'Intervalo fijo' "
            f"para capturar 1 frame cada {interval_s:g}s de todos modos."
        ) if mode == "scene" else (
            "  > Cambia el modo o ajusta el intervalo."
        )
        raise RuntimeError(
            f"ffmpeg no produjo ningun frame (modo={mode}, "
            f"threshold={scene_threshold:g}).\n{hint}\n"
            f"Detalle tecnico:\n{tail}"
        )

    # Parse `pts_time:NNN` from stderr — one entry per selected frame.
    pts_re = re.compile(r"pts_time:([\d.]+)")
    times = [float(m) for m in pts_re.findall(proc.stderr)]

    # Pair by order. showinfo emits one line per selected frame, in the
    # same order ffmpeg writes the files.
    frames: list[KeyFrame] = []
    for i, p in enumerate(files):
        t = times[i] if i < len(times) else None
        if t is None:
            # fallback: estimate from file index * interval
            t = i * interval_s
        frames.append(KeyFrame(time=t, path=p))

    if proc.returncode != 0 and files:
        log(f"[!] ffmpeg salio con codigo {proc.returncode} pero produjo {len(frames)} frames.")
    log(f"Extraidos {len(frames)} frames clave.")

    # Compress each PNG frame to JPEG. This typically reduces per-frame
    # size from ~1.5 MB to ~120 KB with no visible quality loss.
    q_int = QUALITY_PRESETS.get(image_quality, 85)
    log(f"[i] Comprimiendo frames a JPEG (calidad={q_int}, "
        f"preset={image_quality})...")
    total_in = sum(p.stat().st_size for p in files)
    compressed: list[KeyFrame] = []
    for kf in frames:
        new_path = _compress_to_jpeg(kf.path, max_px=1280, quality=q_int)
        compressed.append(KeyFrame(time=kf.time, path=new_path))
    frames = compressed
    total_out = sum(p.stat().st_size for p in (kf.path for kf in frames) if p.exists())
    if total_in > 0:
        ratio = (1 - total_out / total_in) * 100
        log(f"[OK] Compresion: {total_in//1024} KB -> {total_out//1024} KB "
            f"({ratio:.0f}% menos).")

    if len(frames) > 300:
        log("[!] Muchos frames (>300). Considera subir el threshold o el intervalo.")
    return frames


# ---- 1b. Perceptual deduplication + temporal clustering -------------------
# pHash (perceptual hash) is a 64-bit fingerprint of an image's low-frequency
# content. Two pHashes close in Hamming distance (0..64) mean the images look
# similar to a human, regardless of small pixel changes (cursor moves, scroll,
# animation frames, compression artifacts). It is far faster than SSIM and
# does not require OpenCV.
#
# We use it as a post-filter AFTER ffmpeg's scene detection, because ffmpeg
# picks any frame where the scene score rises (mouse moves, scroll, slide
# transitions, animations) — many of which are visually redundant. This step
# answers the markdown's "Etapa 2 / 4 / 6" (dedup + persistence + clustering)
# in one pass.
def dedupe_keyframes(
    keyframes: list[KeyFrame],
    perceptual_threshold: int = 5,
    cluster_window_s: float = 30.0,
    log: LogFn = _NOP,
    cancel_flag: CancelFn = lambda: False,
) -> list[KeyFrame]:
    """Reduce a list of candidate keyframes to one frame per visually
    distinct moment, using pHash + temporal clustering.

    Parameters
    ----------
    perceptual_threshold : int
        Hamming distance (0..64) below which two consecutive frames are
        considered the same scene. Lower = more aggressive (only big
        visual changes pass). 64 effectively disables the filter.
        0..10 is the useful range; default 5 matches the markdown's
        "high perceptual threshold, keep visually distinct moments".
    cluster_window_s : float
        Seconds within which consecutive "similar" frames are condensed
        into one. Acts as a minimum gap between captures and absorbs
        bursts (e.g. 5 frames in 2s because of a PowerPoint transition).
        0 disables the temporal clustering component.
    log, cancel_flag
        Standard callbacks.

    Returns a new list (does not mutate the input).
    """
    if not keyframes:
        return []
    # Disabled case: threshold=64 (max possible hamming distance) means
    # "everything passes" — preserve the original list untouched.
    if perceptual_threshold >= 64:
        return list(keyframes)

    try:
        import imagehash
        from PIL import Image
    except ImportError as exc:
        log(f"[!] No se pudo importar imagehash/Pillow para deduplicar: {exc}. "
            "Saltando dedup (instala 'imagehash' y 'Pillow').")
        return list(keyframes)

    # Calculate pHash once per frame. For ~200 frames at 1280px wide this
    # takes ~3-5s total — comparable to a single ffmpeg scene probe.
    log(f"[i] Calculando pHash de {len(keyframes)} frames candidatos…")
    hashes: list[Optional[imagehash.ImageHash]] = []
    for kf in keyframes:
        if cancel_flag():
            raise CancelledError()
        try:
            with Image.open(kf.path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                hashes.append(imagehash.phash(im, hash_size=8))
        except Exception as exc:
            # Hashing failed (corrupt file, weird format, etc.) — keep
            # the frame rather than dropping it silently.
            log(f"[!] No se pudo hashear {kf.path.name}: {exc}")
            hashes.append(None)

    # Walk frames in time order; keep only those that survive both the
    # "different from previous kept" rule (persistence) and the "not
    # inside a cluster window" rule (clustering).
    kept: list[KeyFrame] = []
    kept_hashes: list[imagehash.ImageHash] = []
    for kf, h in zip(keyframes, hashes):
        if cancel_flag():
            raise CancelledError()
        # Hashing failed: keep to avoid data loss.
        if h is None:
            kept.append(kf)
            kept_hashes.append(h)  # type: ignore[arg-type]
            continue
        if not kept_hashes:
            kept.append(kf)
            kept_hashes.append(h)
            continue
        last_kf = kept[-1]
        last_h = kept_hashes[-1]
        if last_h is None:
            kept.append(kf)
            kept_hashes.append(h)
            continue

        distance = h - last_h
        time_delta = kf.time - last_kf.time

        # Rule 1: very similar to last kept -> drop.
        if distance <= perceptual_threshold:
            continue
        # Rule 2: inside a temporal cluster AND perceptually close ->
        # drop (absorbs bursts of near-duplicates around transitions).
        if (cluster_window_s > 0
                and time_delta < cluster_window_s
                and distance <= perceptual_threshold + 3):
            continue

        kept.append(kf)
        kept_hashes.append(h)

    dropped = len(keyframes) - len(kept)
    if len(keyframes) > 0:
        pct = (dropped / len(keyframes)) * 100
        log(f"[OK] Deduplicacion: {len(keyframes)} -> {len(kept)} frames "
            f"({dropped} redundantes, -{pct:.0f}%).")
    return kept


# ---- 2. Transcription with timestamps --------------------------------------
def transcribe_with_timestamps(
    audio_path: Path,
    model_size: str,
    language: Optional[str],
    log: LogFn,
    cancel_flag: CancelFn,
    device: str = "cpu",
) -> list[TranscriptSegment]:
    """Run faster-whisper and return raw segments (start, end, text)."""
    from transcriber import _load_model   # reuse cache
    # Pass log so the user sees the "Cargando modelo en cuda (int8)..." line
    # and the "GPU no disponible; usando CPU" warning if device="auto"
    # falls back to CPU.
    model = _load_model(model_size, device=device, log=log)
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    log(f"Detectado: idioma={info.language}, "
        f"prob={info.language_probability:.2f}, "
        f"duración={info.duration:.1f}s")

    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        if cancel_flag():
            raise CancelledError()
        text = (seg.text or "").strip()
        if text:
            segments.append(TranscriptSegment(start=seg.start, end=seg.end, text=text))
    log(f"Transcritos {len(segments)} segmentos.")
    return segments


# ---- 3. Correlation --------------------------------------------------------
def correlate(
    segments: list[TranscriptSegment],
    keyframes: list[KeyFrame],
    window_s: float = 15.0,
) -> list[EnrichedBlock]:
    """For each keyframe, gather transcript text in [t-w, t+w]."""
    if not keyframes:
        return []
    blocks: list[EnrichedBlock] = []
    for kf in keyframes:
        lo = kf.time - window_s
        hi = kf.time + window_s
        matching = [s for s in segments if s.end >= lo and s.start <= hi]
        text = " ".join(s.text for s in matching).strip()
        blocks.append(EnrichedBlock(
            time=kf.time,
            frame_path=kf.path,
            transcript_text=text or "(sin transcripción cercana)",
            segment_count=len(matching),
        ))
    return blocks


# ---- 4. Outputs ------------------------------------------------------------
# JPEG quality presets. The same image re-encoded at these qualities gives:
#   q=92 (high)   -> ~200 KB per 1280x720 frame   (visually lossless)
#   q=85 (medium) -> ~120 KB per 1280x720 frame   (recommended)
#   q=75 (low)    -> ~ 60 KB per 1280x720 frame   (smallest, slight loss)
QUALITY_PRESETS = {
    "high":   92,
    "medium": 85,
    "low":    75,
}


def _compress_to_jpeg(
    path: Path,
    max_px: int = 1280,
    quality: int = 85,
) -> Path:
    """Convert a frame to a compressed JPEG and return the new path.

    - Converts RGBA/LA/P -> RGB (JPEG has no alpha channel)
    - Downscales to max_px wide (default 1280) keeping aspect ratio
    - Saves as progressive JPEG with `optimize=True` (smaller files,
      decodes in passes, perfect for PDFs viewed page-by-page)
    - Deletes the original file when its extension differs from .jpg

    For a 1280x720 PNG screenshot of a screen-share:
        PNG (lossless)  -> ~1.5 MB
        JPEG q=92       -> ~200 KB  (~7x smaller, visually identical)
        JPEG q=85       -> ~120 KB  (~12x smaller, recommended)
        JPEG q=75       -> ~ 60 KB  (~25x smaller, slight softness)
    """
    try:
        from PIL import Image
    except Exception:
        return path
    try:
        with Image.open(path) as im:
            # JPEG can't store alpha — flatten to white background.
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode == "P":
                im = im.convert("RGB")
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            # Downscale if needed.
            if im.width > max_px:
                ratio = max_px / im.width
                new_size = (max_px, int(im.height * ratio))
                im = im.resize(new_size, Image.LANCZOS)

            jpg_path = path.with_suffix(".jpg")
            im.save(
                jpg_path, "JPEG",
                quality=quality, optimize=True, progressive=True,
            )
            # Delete the original PNG (if it had a different extension).
            if jpg_path != path and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            return jpg_path
    except Exception:
        return path


def _resize_image(path: Path, max_px: int = 1280) -> Path:
    """Backward-compatible alias. See `_compress_to_jpeg` for new behaviour.
    New callers should use `_compress_to_jpeg(path, max_px, quality)` directly.
    """
    return _compress_to_jpeg(path, max_px=max_px, quality=85)


def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"


# ---- Font handling --------------------------------------------------------
# DejaVu Sans is the universal Unicode font shipped with Linux distros.
# run.bat downloads DejaVuSans.ttf + DejaVuSans-Bold.ttf into assets/
# the first time the app is launched. If both are present, we use them
# (covers ~50k glyphs including Spanish accents, Japanese, Chinese,
# Korean, Cyrillic, Greek, Arabic, etc.). If absent, we fall back to
# the built-in Helvetica + char-stripping, which keeps the app working
# but loses any non-Latin-1 characters (replaced with '?').
_ASSETS_DIR = Path(__file__).parent / "assets"
_FONT_REGULAR = _ASSETS_DIR / "DejaVuSans.ttf"
_FONT_BOLD = _ASSETS_DIR / "DejaVuSans-Bold.ttf"


def _try_register_unicode_fonts(pdf) -> bool:
    """Register DejaVu Sans + Bold as Unicode fonts. Returns True if both
    were registered successfully; False if we have to fall back to
    Helvetica."""
    try:
        if not _FONT_REGULAR.exists():
            return False
        pdf.add_font("DejaVu", "", str(_FONT_REGULAR), uni=True)
        if _FONT_BOLD.exists():
            pdf.add_font("DejaVu", "B", str(_FONT_BOLD), uni=True)
        else:
            # Use regular for bold too (visible difference is small).
            pdf.add_font("DejaVu", "B", str(_FONT_REGULAR), uni=True)
        return True
    except Exception:
        return False


def _safe_for_latin1(text: str) -> str:
    """Replace any character outside Latin-1 with '?' so fpdf2's
    Helvetica (Latin-1) doesn't crash. Used only as a defensive
    fallback when DejaVu fonts aren't available."""
    return text.encode("latin-1", "replace").decode("latin-1")


# Whisper is known to "hallucinate" CJK characters (Chinese, Japanese,
# Korean) during silence, music or unclear audio. Without OCR or a
# Unicode-capable font installed, those tokens become visible noise in
# the PDF. Strip them here, before the text reaches fpdf2 — applied
# ONLY to the PDF rendering (the .txt and .json keep the raw
# transcription so the user can still see what Whisper said).
_CJK_RANGES = (
    "　-〿"   # CJK Symbols and Punctuation
    "぀-ゟ"   # Hiragana
    "゠-ヿ"   # Katakana
    "㐀-䶿"   # CJK Unified Ideographs Extension A
    "一-鿿"   # CJK Unified Ideographs (main block)
    "豈-﫿"   # CJK Compatibility Ideographs
    "＀-￯"   # Halfwidth and Fullwidth Forms
    "가-힯"   # Hangul Syllables
)
_CJK_RE = re.compile(f"[{_CJK_RANGES}]")


def _strip_cjk(text: str) -> str:
    """Remove all CJK (Chinese / Japanese / Korean) characters from text.

    Used to clean up Whisper hallucinations before they reach the PDF.
    The original .txt export keeps the raw text untouched.
    """
    return _CJK_RE.sub("", text)


def build_enriched_pdf(
    blocks: list[EnrichedBlock],
    output_path: Path,
    title: str = "Transcripción enriquecida",
    log: LogFn = _NOP,
) -> None:
    """Build a navigable PDF: one block per page with [timestamp, image, text].

    Uses DejaVu Sans (Unicode, covers Spanish accents + Japanese/CJK +
    Cyrillic/Greek/etc.) when assets/DejaVuSans.ttf is present. Falls
    back to built-in Helvetica + char-stripping otherwise.
    """
    from fpdf import FPDF
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Resize images first so the PDF stays small
    for b in blocks:
        _resize_image(b.frame_path)

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.alias_nb_pages()

    # Try to register Unicode fonts. If missing, log once and degrade
    # gracefully to Helvetica with char-stripping so the PDF still gets
    # built (some characters replaced with '?').
    use_unicode = _try_register_unicode_fonts(pdf)
    if use_unicode:
        font = "DejaVu"
        log("[OK] Usando fuente Unicode (DejaVu Sans).")
    else:
        font = "Helvetica"
        log("[!] assets/DejaVuSans.ttf no encontrada - usando Helvetica. "
            "Caracteres fuera de Latin-1 seran reemplazados por '?'. "
            "Vuelve a lanzar run.bat para descargarla.")

    def _safe(text: str) -> str:
        """Strip characters that the active font can't render."""
        if use_unicode:
            return text
        return _safe_for_latin1(text)

    pdf.set_margins(left=15, top=18, right=15)
    pdf.add_page()
    # Title page
    pdf.set_font(font, "B", 18)
    pdf.cell(0, 12, _safe(title), ln=1, align="C")
    pdf.set_font(font, "", 11)
    pdf.cell(0, 6, _safe(f"Generado por MediaForge · {len(blocks)} bloques"),
             ln=1, align="C")
    pdf.ln(6)

    # Iterate blocks
    for idx, b in enumerate(blocks, 1):
        if idx > 1:
            pdf.add_page()

        # Header: timestamp + index
        pdf.set_font(font, "B", 13)
        pdf.cell(0, 7, _safe(f"[{_fmt_time(b.time)}]   bloque {idx}/{len(blocks)}"),
                 ln=1)

        # Image (max width = 180mm in A4 with 15mm margins)
        try:
            pdf.image(str(b.frame_path), w=180)
        except Exception as exc:
            pdf.set_font(font, "I", 10)
            pdf.cell(0, 6, _safe(f"(no se pudo incrustar la imagen: {exc})"),
                     ln=1)

        pdf.ln(3)

        # Transcript
        pdf.set_font(font, "B", 11)
        pdf.cell(0, 6, _safe(f"Transcripción ({b.segment_count} segmentos):"),
                 ln=1)
        pdf.set_font(font, "", 11)
        # Strip Whisper's CJK hallucinations before rendering. Keeps the
        # transcription meaningful (no "..." substitution that breaks
        # readability) while preventing unreadable character blocks in
        # the PDF.
        pdf.multi_cell(0, 6, _safe(_strip_cjk(b.transcript_text)))

        # Footer (page number)
        pdf.set_y(-12)
        pdf.set_font(font, "I", 8)
        pdf.cell(0, 6, f"Página {pdf.page_no()}/{{nb}}", align="C")

    pdf.output(str(output_path))


def export_json(blocks: list[EnrichedBlock], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "timestamp": _fmt_time(b.time),
            "timestamp_seconds": b.time,
            "image": str(b.frame_path),
            "text": b.transcript_text,
            "segment_count": b.segment_count,
        }
        for b in blocks
    ]
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_txt_with_timestamps(
    segments: list[TranscriptSegment], output_path: Path
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{_fmt_time(s.start)}] {s.text}" for s in segments]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- 5. Orchestrator -------------------------------------------------------
def enrich_video(
    input_path: Path,
    output_dir: Path,
    model_size: str = "small",
    language: Optional[str] = "es",
    mode: str = "hybrid",
    scene_threshold: float = 0.4,
    interval_s: float = 15.0,
    window_s: float = 15.0,
    write_pdf: bool = True,
    write_json: bool = True,
    write_txt: bool = True,
    keep_frames: bool = True,
    image_quality: str = "medium",
    perceptual_threshold: int = 5,
    cluster_window_s: float = 30.0,
    device: str = "cpu",
    log: LogFn = _NOP,
    cancel_flag: CancelFn = lambda: False,
    progress: ProgressFn = _NOP,
) -> dict[str, Path]:
    """End-to-end: extract audio → transcribe → extract frames → correlate
    → write outputs. Returns a dict of generated paths.

    On success, emits a structured mini-report through ``log()`` with
    per-phase timings, frame counts, output sizes and a speed ratio.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    pdf_path = output_dir / (input_path.stem + ".pdf")
    json_path = output_dir / (input_path.stem + ".json")
    txt_path = output_dir / (input_path.stem + ".txt")

    # Phase timings — captured before each phase starts and after it ends.
    # All keys are inserted in ``enrich_video`` regardless of whether the
    # phase produced output, so the report always has the full table.
    timings: dict[str, float] = {}
    frames_before_dedup = 0
    n_segments = 0
    audio_duration = 0.0

    t_total = time.monotonic()
    in_duration = probe_duration(input_path)

    # -- 1. Audio extraction (tempdir stays alive until inference ends)
    log(f"[1/5] Extrayendo audio de {input_path.name}…")
    progress(0.02)
    t = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mediaforge_") as tmp:
        wav = Path(tmp) / "audio.wav"
        from transcriber import extract_audio
        extract_audio(input_path, wav, log)
        if cancel_flag():
            raise CancelledError()
        timings["Extracción audio"] = time.monotonic() - t

        # -- 2. Transcribe
        log(f"[2/5] Transcribiendo con modelo '{model_size}' "
            f"(dispositivo={device})…")
        progress(0.10)
        t = time.monotonic()
        segments = transcribe_with_timestamps(
            wav, model_size, language, log, cancel_flag, device=device)
        n_segments = len(segments)
        if cancel_flag():
            raise CancelledError()
        timings["Transcripción"] = time.monotonic() - t
    # tempdir auto-cleaned here, after the wav has been read.
    progress(0.55)

    # -- 3. Frames
    log(f"[3/5] Extrayendo frames clave (modo {mode})…")
    t = time.monotonic()
    keyframes = extract_keyframes(
        input_path, frames_dir,
        mode=mode, scene_threshold=scene_threshold, interval_s=interval_s,
        image_quality=image_quality,
        log=log,
    )
    frames_before_dedup = len(keyframes)
    if cancel_flag():
        raise CancelledError()
    timings["Extracción frames"] = time.monotonic() - t
    progress(0.65)

    # -- 3b. Perceptual dedup + temporal clustering
    t = time.monotonic()
    if keyframes and perceptual_threshold < 64:
        log(f"[3b/5] Deduplicando frames similares "
            f"(umbral={perceptual_threshold}, ventana={cluster_window_s:.0f}s)…")
        keyframes = dedupe_keyframes(
            keyframes,
            perceptual_threshold=perceptual_threshold,
            cluster_window_s=cluster_window_s,
            log=log,
            cancel_flag=cancel_flag,
        )
        if cancel_flag():
            raise CancelledError()
    timings["Deduplicación"] = time.monotonic() - t
    progress(0.90)

    # -- 4. Correlate
    log(f"[4/5] Correlacionando {len(keyframes)} frames con "
        f"{len(segments)} segmentos…")
    t = time.monotonic()
    blocks = correlate(segments, keyframes, window_s=window_s)
    log(f"{len(blocks)} bloques generados.")
    timings["Correlación"] = time.monotonic() - t
    progress(0.95)

    # -- 5. Outputs
    log(f"[5/5] Generando salidas en {output_dir}…")
    out_files: list[tuple[str, Path]] = []
    if write_pdf:
        t = time.monotonic()
        build_enriched_pdf(blocks, pdf_path, title=input_path.stem, log=log)
        timings["PDF"] = time.monotonic() - t
        log(f"  [OK] PDF: {pdf_path}")
        out_files.append(("PDF", pdf_path))
    if write_json:
        t = time.monotonic()
        export_json(blocks, json_path)
        timings["JSON"] = time.monotonic() - t
        log(f"  [OK] JSON: {json_path}")
        out_files.append(("JSON", json_path))
    if write_txt:
        t = time.monotonic()
        export_txt_with_timestamps(segments, txt_path)
        timings["TXT"] = time.monotonic() - t
        log(f"  [OK] TXT: {txt_path}")
        out_files.append(("TXT", txt_path))
    if not keep_frames:
        for f in frames_dir.glob("frame_*.png"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            frames_dir.rmdir()
        except OSError:
            pass

    progress(1.0)
    log("[OK] Enriquecimiento completado.")

    # --- Mini-report ---
    t_total_d = time.monotonic() - t_total
    in_size = input_path.stat().st_size if input_path.exists() else 0

    # Frame reduction string.
    if frames_before_dedup > 0 and len(keyframes) != frames_before_dedup:
        frames_row = (
            f"{frames_before_dedup} → {len(keyframes)} "
            f"(-{(1 - len(keyframes)/frames_before_dedup)*100:.0f}%)"
        )
    else:
        frames_row = f"{len(keyframes)} (sin dedup)"

    rows_general: list[tuple[str, str]] = [
        ("Vídeo",   f"{input_path.name} ({fmt_size(in_size)}"
                   f"{(f' · {fmt_duration(in_duration)}') if in_duration else ''})"),
        ("Modelo",  f"{model_size} · {language or 'auto-detect'} · {device}"),
        ("Frames",  frames_row),
        ("Bloques", f"{len(blocks)} (ventana ±{window_s:.0f}s)"),
    ]
    rows_outputs: list[tuple[str, str]] = []
    for label, p in out_files:
        if p and p.exists():
            rows_outputs.append((label, f"{p.name} ({fmt_size(p.stat().st_size)})"))
    rows_times: list[tuple[str, str]] = [
        (k, fmt_duration(v)) for k, v in timings.items() if v > 0
    ]
    rows_times.append(("TOTAL", fmt_duration(t_total_d)))
    if in_duration and in_duration > 0:
        rows_times.append(("Velocidad", fmt_ratio(in_duration, t_total_d) + " realtime"))

    print_report(
        log,
        "TRANSCRIPCIÓN ENRIQUECIDA",
        fmt_duration(t_total_d),
        [rows_general, rows_outputs, rows_times],
    )

    return {
        "pdf": pdf_path if write_pdf else None,
        "json": json_path if write_json else None,
        "txt": txt_path if write_txt else None,
        "frames_dir": frames_dir if keep_frames else None,
        "output_dir": output_dir,
    }