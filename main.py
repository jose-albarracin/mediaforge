"""MediaForge — GUI shell with four tabs: Home, Transcriptor, Converter,
Enriched transcription.

Each tab runs its long-running work in a daemon thread; the UI thread
polls a per-tab queue for log lines and completion notifications.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from analyzer import (
    CancelledError as EnrichCancelled,
    enrich_video,
)
from converter import (
    CancelledError as ConvertCancelled,
    EncoderSupport,
    convert,
    detect_encoders,
    HW_ACCEL,
)
from home import HomeTab
from transcriber import CancelledError as TranscribeCancelled
from transcriber import ComputeSupport, check_ffmpeg, detect_compute, transcribe_video


APP_NAME = "MediaForge"
APP_VERSION = "1.0"

def _open_path(path) -> None:
    """Open a file or folder with the OS default app.

    ``os.startfile`` only exists on Windows; calling it on macOS/Linux
    raised AttributeError and the "Abrir" buttons crashed there.
    """
    path = str(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---- Transcriber constants ------------------------------------------------
T_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
T_LANGUAGES: list[tuple[str, Optional[str]]] = [
    ("Auto-detectar", None),
    ("Español", "es"),
    ("Inglés", "en"),
    ("Francés", "fr"),
    ("Alemán", "de"),
    ("Italiano", "it"),
    ("Portugués", "pt"),
]
T_MEDIA_EXTS = (
    ".mp4 .mov .mkv .avi .webm .flv .m4v "
    ".mp3 .wav .m4a .flac .ogg .aac .opus"
)
# Compute device choices. "auto" means "try CUDA, fall back to CPU".
# We always show "auto" and "cpu"; we add "cuda" only if ctranslate2
# reports at least one CUDA device. The "auto" default is the safe
# choice for users who don't know what's installed.
T_DEVICE_OPTIONS = [
    ("Auto (intentar GPU, fallback a CPU)", "auto"),
    ("CPU (software)", "cpu"),
    ("GPU (NVIDIA CUDA)", "cuda"),
]

# ---- Enriched transcription constants --------------------------------------
E_FRAME_MODES = [
    ("Automático (escena + cada 15s)", "hybrid"),
    ("Solo cambios de escena", "scene"),
    ("Intervalo fijo", "interval"),
]
E_VIDEO_EXTS = (
    ".mp4 .mov .mkv .avi .webm .flv .m4v .wmv .ts .m2ts .3gp .ogv .vob"
)
# Scene-detection threshold range. Slider allows very small values
# (0.01) for screen-share recordings where the maximum scene score can
# be below 0.10, up to 0.4 for catching only the strongest cuts.
E_SCENE_MIN, E_SCENE_MAX = 0.01, 0.4
E_SCENE_DEFAULT = 0.30

# Image quality presets for the embedded frames. PNG (the old default)
# is lossless but huge (~1.5 MB/frame); JPEG q85 is visually identical
# and ~12x smaller.
E_IMAGE_QUALITY = [
    ("Alta (q92, maxima fidelidad)", "high"),
    ("Media (q85, recomendada)",     "medium"),
    ("Baja (q75, archivo pequeno)",  "low"),
]
E_IMAGE_QUALITY_DEFAULT = "medium"

# ---- Converter constants --------------------------------------------------
C_VIDEO_INPUT_EXTS = (
    ".mp4 .mov .mkv .avi .webm .flv .m4v .wmv .ts .m2ts .3gp .ogv .vob"
)
C_AUDIO_INPUT_EXTS = ".mp3 .wav .m4a .flac .ogg .aac .opus .wma"
C_OUTPUT_KINDS = [("MP4 (vídeo)", "mp4"), ("MP3 (audio)", "mp3")]
C_CODECS = [("H.264 (compat)", "h264"), ("H.265 (más compresión)", "hevc")]
C_HW_OPTIONS = [
    ("Auto (mejor disponible)", "auto"),
    ("CPU (software)", "cpu"),
    ("NVIDIA NVENC", "nvidia"),
    ("AMD AMF", "amd"),
    ("Intel QuickSync", "intel"),
]
C_QUALITY = [("Alta", "alta"), ("Media", "media"), ("Baja", "baja")]


# ============================================================================
# Transcriber tab
# ============================================================================
class TranscriberTab(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, compute: ComputeSupport) -> None:
        super().__init__(master, fg_color="transparent")

        self._msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_output_dir: Optional[Path] = None
        self._compute = compute  # for the device combobox filter

        # Scrollable wrapper so the tab content is reachable on short
        # windows (e.g. 768p) and looks natural on tall ones.
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)

        self._build_ui()
        self._set_state("idle")
        self.after(100, self._poll_queue)

    # ---- UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 6}
        # Inside the scrollable wrapper everything is anchored to `scroll`,
        # not to `self`. The log box uses a fixed height (no expand=True
        # inside a scrollable frame).
        host = self.scroll

        # Header row with title + Limpiar button
        header = ctk.CTkFrame(host, fg_color="transparent")
        header.pack(fill="x", **pad)
        ctk.CTkLabel(
            header, text="🎙️  Transcriptor",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            header, text="🧹  Limpiar", command=self._clear,
            width=110, fg_color="#555", hover_color="#444",
        ).pack(side="right")

        ctk.CTkLabel(
            host,
            text="Extrae el audio con ffmpeg y lo transcribe con faster-whisper.",
            text_color="gray",
        ).pack(anchor="w", **pad)

        # Input
        in_frame = ctk.CTkFrame(host)
        in_frame.pack(fill="x", **pad)
        ctk.CTkLabel(in_frame, text="Archivo de entrada").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(in_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.input_entry = ctk.CTkEntry(row)
        self.input_entry.configure(placeholder_text="Selecciona un mp4, mp3, wav…")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Seleccionar…", width=120,
                      command=self._pick_input).pack(side="left")

        # Output
        out_frame = ctk.CTkFrame(host)
        out_frame.pack(fill="x", **pad)
        ctk.CTkLabel(out_frame, text="Salida (.txt)").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(out_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.output_entry = ctk.CTkEntry(row)
        self.output_entry.configure(placeholder_text="Ruta del .txt")
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Cambiar…", width=120,
                      command=self._pick_output).pack(side="left")

        # Options
        opt_frame = ctk.CTkFrame(host)
        opt_frame.pack(fill="x", **pad)
        ctk.CTkLabel(opt_frame, text="Opciones").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(opt_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))

        ctk.CTkLabel(row, text="Modelo:").pack(side="left")
        self.model_var = ctk.StringVar(value="small")
        ctk.CTkComboBox(row, values=T_MODELS, variable=self.model_var,
                        width=130).pack(side="left", padx=(6, 18))

        ctk.CTkLabel(row, text="Idioma:").pack(side="left")
        self.lang_var = ctk.StringVar(value="Español")
        lang_labels = [label for label, _ in T_LANGUAGES]
        ctk.CTkComboBox(row, values=lang_labels, variable=self.lang_var,
                        width=160).pack(side="left", padx=(6, 18))

        # Compute device. "Auto" is the default — it tries CUDA and
        # falls back to CPU silently. "GPU" is added to the dropdown
        # only when the startup probe actually found a CUDA device.
        ctk.CTkLabel(row, text="Dispositivo:").pack(side="left")
        self.device_var = ctk.StringVar(
            value=T_DEVICE_OPTIONS[0][0])  # "Auto..."
        avail = [T_DEVICE_OPTIONS[0], T_DEVICE_OPTIONS[1]]  # Auto + CPU
        if self._compute.has_cuda:
            avail.append(T_DEVICE_OPTIONS[2])               # + GPU
        self.device_combo = ctk.CTkComboBox(
            row,
            values=[label for label, _ in avail],
            variable=self.device_var, width=200,
        )
        self.device_combo.pack(side="left", padx=(6, 0))

        # Action row
        action_frame = ctk.CTkFrame(host, fg_color="transparent")
        action_frame.pack(fill="x", **pad)
        self.start_btn = ctk.CTkButton(
            action_frame, text="▶  Transcribir",
            command=self._start, width=160)
        self.start_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(
            action_frame, text="✖  Cancelar", command=self._cancel,
            fg_color="#a33", hover_color="#822", width=110)
        self.open_btn = ctk.CTkButton(
            action_frame, text="📂  Abrir carpeta",
            command=self._open_folder, width=140)

        self.status_label = ctk.CTkLabel(host, text="Listo.", anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 0))
        self.progress = ctk.CTkProgressBar(host)
        self.progress.pack(fill="x", padx=14, pady=(0, 8))
        self.progress.set(0)

        ctk.CTkLabel(host, text="Registro", anchor="w").pack(
            fill="x", padx=14, pady=(4, 0))
        self.log_box = ctk.CTkTextbox(
            host, height=240, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(2, 14))

    # ---- State -----------------------------------------------------------
    def _set_state(self, state: str) -> None:
        running = state == "running"
        self.cancel_btn.pack_forget()
        self.open_btn.pack_forget()

        if running:
            self.start_btn.configure(state="disabled")
            self.cancel_btn.pack(side="left", padx=(10, 0))
            self.status_label.configure(text="Transcribiendo…")
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.start_btn.configure(state="normal")
            self.progress.stop()
            self.progress.configure(mode="determinate")
            if state == "idle":
                self.progress.set(0)
                self.status_label.configure(text="Listo.")
            elif state == "done":
                self.progress.set(1)
                self.status_label.configure(text="✓ Completado.")
                self.open_btn.pack(side="left", padx=(10, 0))
            elif state == "cancelled":
                self.progress.set(0)
                self.status_label.configure(text="Cancelado.")
            elif state == "error":
                self.progress.set(0)
                self.status_label.configure(
                    text="✖ Error. Revisa el registro.")

    # ---- File pickers ----------------------------------------------------
    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecciona archivo de audio o vídeo",
            filetypes=[("Vídeo / Audio", T_MEDIA_EXTS), ("Todos", "*.*")],
        )
        if not path:
            return
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, path)
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(Path(path).with_suffix(".txt")))

    def _pick_output(self) -> None:
        current = self.output_entry.get().strip() or "transcripcion.txt"
        path = filedialog.asksaveasfilename(
            title="Guardar transcripción como…",
            defaultextension=".txt",
            initialfile=Path(current).name,
            initialdir=str(Path(current).parent),
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
        )
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    # ---- Worker ----------------------------------------------------------
    def _qlog(self, msg: str) -> None:
        self._msg_queue.put(("log", msg))

    def _start(self) -> None:
        in_path = Path(self.input_entry.get().strip())
        out_text = self.output_entry.get().strip()
        out_path = Path(out_text) if out_text else in_path.with_suffix(".txt")
        if out_path.suffix.lower() != ".txt":
            out_path = out_path.with_suffix(".txt")

        if not in_path.is_file():
            self._log(f"✖ El archivo de entrada no existe: {in_path}")
            return

        ok, info = check_ffmpeg()
        if not ok:
            self._log(f"✖ {info}")
            return

        self._cancel_event.clear()
        self._log("─" * 60)
        self._log(f"Inicio: {in_path}")
        self._log(f"Modelo: {self.model_var.get()}   "
                  f"Idioma: {self.lang_var.get()}   "
                  f"Dispositivo: {self.device_var.get()}")
        self._log(f"Salida: {out_path}")

        self._set_state("running")
        self._worker = threading.Thread(
            target=self._run_worker, args=(in_path, out_path), daemon=True)
        self._worker.start()

    def _run_worker(self, in_path: Path, out_path: Path) -> None:
        try:
            lang_code = dict(T_LANGUAGES)[self.lang_var.get()]
            device_key = dict(T_DEVICE_OPTIONS)[self.device_var.get()]
            transcribe_video(
                in_path, out_path,
                self.model_var.get(), lang_code,
                log=self._qlog, cancel_flag=self._cancel_event.is_set,
                device=device_key,
            )
            self._msg_queue.put(("done", str(out_path)))
        except TranscribeCancelled:
            self._msg_queue.put(("cancelled", ""))
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb
            self._msg_queue.put((
                "error", f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"))

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self._log("Cancelación solicitada (parará tras el segmento actual)…")

    def _open_folder(self) -> None:
        target = self._last_output_dir
        if target and target.exists():
            _open_path((target))
        else:
            self._log("No hay carpeta de salida reciente.")

    def _clear(self) -> None:
        """🧹 Reset all fields, log, and progress."""
        if self._worker and self._worker.is_alive():
            self._log("⚠ Hay una transcripción en curso. Cáncelala antes de limpiar.")
            return
        self.input_entry.delete(0, "end")
        self.output_entry.delete(0, "end")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._set_state("idle")

    # ---- Queue draining --------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._last_output_dir = Path(payload).parent
                    self._set_state("done")
                elif kind == "cancelled":
                    self._set_state("cancelled")
                elif kind == "error":
                    self._set_state("error")
                    # `payload` may contain newlines (full traceback).
                    # Print each line separately so the Textbox preserves them.
                    for line in payload.splitlines() or [payload]:
                        self._log(f"✖ {line}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


# ============================================================================
# Converter tab
# ============================================================================
class ConverterTab(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, support: EncoderSupport) -> None:
        super().__init__(master, fg_color="transparent")
        self._support = support

        self._msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_output_dir: Optional[Path] = None

        # Scrollable wrapper so the tab content is reachable on short
        # windows (e.g. 768p) and looks natural on tall ones.
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)

        self._build_ui()
        self._set_state("idle")
        self._update_hw_options()
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 6}
        # Inside the scrollable wrapper everything is anchored to `host`,
        # not to `self`. The log box uses a fixed height (no expand=True
        # inside a scrollable frame).
        host = self.scroll

        ctk.CTkLabel(
            host, text="🔄  Conversor multimedia",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", **pad)
        ctk.CTkLabel(
            host,
            text="Convierte entre formatos populares usando CPU o GPU "
                 "(NVIDIA, AMD, Intel).",
            text_color="gray",
        ).pack(anchor="w", **pad)

        # Input
        in_frame = ctk.CTkFrame(host)
        in_frame.pack(fill="x", **pad)
        ctk.CTkLabel(in_frame, text="Archivo de entrada").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(in_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.input_entry = ctk.CTkEntry(row)
        self.input_entry.configure(placeholder_text="Vídeo o audio origen (mkv, avi, mp4, mp3…)")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Seleccionar…", width=120,
                      command=self._pick_input).pack(side="left")

        # Output
        out_frame = ctk.CTkFrame(host)
        out_frame.pack(fill="x", **pad)
        ctk.CTkLabel(out_frame, text="Archivo de salida").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(out_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.output_entry = ctk.CTkEntry(row)
        self.output_entry.configure(placeholder_text="Ruta del .mp4 o .mp3")
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Cambiar…", width=120,
                      command=self._pick_output).pack(side="left")

        # Options grid
        opt_frame = ctk.CTkFrame(host)
        opt_frame.pack(fill="x", **pad)
        ctk.CTkLabel(opt_frame, text="Opciones").pack(
            anchor="w", padx=10, pady=(8, 0))

        # Form: 2 columns, several rows
        form = ctk.CTkFrame(opt_frame, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=(4, 10))
        for c in (0, 1):
            form.grid_columnconfigure(c, weight=1, uniform="cols")

        # Row 0: output kind (radio)
        ctk.CTkLabel(form, text="Formato de salida:").grid(
            row=0, column=0, sticky="w", pady=4)
        self.kind_var = ctk.StringVar(value="mp4")
        kind_frame = ctk.CTkFrame(form, fg_color="transparent")
        kind_frame.grid(row=0, column=1, sticky="w", pady=4)
        for label, val in C_OUTPUT_KINDS:
            ctk.CTkRadioButton(kind_frame, text=label, variable=self.kind_var,
                               value=val, command=self._on_kind_change
                               ).pack(side="left", padx=(0, 14))

        # Row 1: codec (only for mp4)
        ctk.CTkLabel(form, text="Códec de vídeo:").grid(
            row=1, column=0, sticky="w", pady=4)
        self.codec_var = ctk.StringVar(value="h264")
        self.codec_frame = ctk.CTkFrame(form, fg_color="transparent")
        self.codec_frame.grid(row=1, column=1, sticky="w", pady=4)
        for label, val in C_CODECS:
            ctk.CTkRadioButton(self.codec_frame, text=label,
                               variable=self.codec_var, value=val
                               ).pack(side="left", padx=(0, 14))

        # Row 2: hardware accel
        ctk.CTkLabel(form, text="Aceleración:").grid(
            row=2, column=0, sticky="w", pady=4)
        self.hw_var = ctk.StringVar(value="auto")
        self.hw_combo = ctk.CTkComboBox(
            form, values=[label for label, _ in C_HW_OPTIONS],
            variable=self.hw_var, width=240,
        )
        self.hw_combo.grid(row=2, column=1, sticky="w", pady=4)

        # Row 3: quality
        ctk.CTkLabel(form, text="Calidad:").grid(
            row=3, column=0, sticky="w", pady=4)
        self.quality_var = ctk.StringVar(value="media")
        qf = ctk.CTkFrame(form, fg_color="transparent")
        qf.grid(row=3, column=1, sticky="w", pady=4)
        for label, val in C_QUALITY:
            ctk.CTkRadioButton(qf, text=label, variable=self.quality_var,
                               value=val).pack(side="left", padx=(0, 14))

        # Action row
        action_frame = ctk.CTkFrame(host, fg_color="transparent")
        action_frame.pack(fill="x", **pad)
        self.start_btn = ctk.CTkButton(
            action_frame, text="▶  Convertir",
            command=self._start, width=160)
        self.start_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(
            action_frame, text="✖  Cancelar", command=self._cancel,
            fg_color="#a33", hover_color="#822", width=110)
        self.open_btn = ctk.CTkButton(
            action_frame, text="📂  Abrir carpeta",
            command=self._open_folder, width=140)

        self.status_label = ctk.CTkLabel(host, text="Listo.", anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(10, 0))
        self.progress = ctk.CTkProgressBar(host)
        self.progress.pack(fill="x", padx=14, pady=(0, 8))
        self.progress.set(0)

        ctk.CTkLabel(host, text="Registro", anchor="w").pack(
            fill="x", padx=14, pady=(4, 0))
        self.log_box = ctk.CTkTextbox(
            host, height=220, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(2, 14))

    # ---- UI helpers ------------------------------------------------------
    def _on_kind_change(self) -> None:
        # Disable codec radios when extracting MP3
        is_mp3 = self.kind_var.get() == "mp3"
        state = "disabled" if is_mp3 else "normal"
        for child in self.codec_frame.winfo_children():
            child.configure(state=state)

    def _update_hw_options(self) -> None:
        """Replace hw_combo values with only those whose encoder is present."""
        if not self._support.has_ffmpeg:
            self.hw_combo.configure(values=["(ffmpeg no detectado)"])
            self.hw_combo.set("(ffmpeg no detectado)")
            self.hw_combo.configure(state="disabled")
            return
        # Always offer Auto + CPU; add GPU options only if available.
        opts = [C_HW_OPTIONS[0]]  # Auto
        opts.append(C_HW_OPTIONS[1])  # CPU
        for key in ("nvidia", "amd", "intel"):
            if key in self._support.available_keys:
                opts.append(next(o for o in C_HW_OPTIONS if o[1] == key))
        self.hw_combo.configure(values=[label for label, _ in opts])
        self.hw_combo.set(opts[0][0])

    def _set_state(self, state: str) -> None:
        running = state == "running"
        self.cancel_btn.pack_forget()
        self.open_btn.pack_forget()

        if running:
            self.start_btn.configure(state="disabled")
            self.cancel_btn.pack(side="left", padx=(10, 0))
            self.status_label.configure(text="Convirtiendo…")
            self.progress.configure(mode="determinate")
            self.progress.set(0)
        else:
            self.start_btn.configure(state="normal")
            self.progress.stop()
            self.progress.configure(mode="determinate")
            if state == "idle":
                self.progress.set(0)
                self.status_label.configure(text="Listo.")
            elif state == "done":
                self.progress.set(1)
                self.status_label.configure(text="✓ Conversión completada.")
                self.open_btn.pack(side="left", padx=(10, 0))
            elif state == "cancelled":
                self.progress.set(0)
                self.status_label.configure(text="Cancelado.")
            elif state == "error":
                self.progress.set(0)
                self.status_label.configure(text="✖ Error. Revisa el registro.")

    # ---- File pickers ----------------------------------------------------
    def _pick_input(self) -> None:
        is_audio_target = self.kind_var.get() == "mp3"
        if is_audio_target:
            filetypes = [
                ("Audio / Vídeo", f"{C_VIDEO_INPUT_EXTS} {C_AUDIO_INPUT_EXTS}"),
                ("Todos", "*.*"),
            ]
        else:
            filetypes = [
                ("Vídeo", C_VIDEO_INPUT_EXTS),
                ("Audio", C_AUDIO_INPUT_EXTS),
                ("Todos", "*.*"),
            ]
        path = filedialog.askopenfilename(
            title="Selecciona archivo origen", filetypes=filetypes)
        if not path:
            return
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, path)
        # Suggest output: same dir, new extension
        out_ext = ".mp3" if self.kind_var.get() == "mp3" else ".mp4"
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(Path(path).with_suffix(out_ext)))

    def _pick_output(self) -> None:
        kind = self.kind_var.get()
        ext = "." + kind
        current = self.output_entry.get().strip() or f"salida{ext}"
        path = filedialog.asksaveasfilename(
            title="Guardar como…",
            defaultextension=ext,
            initialfile=Path(current).name,
            initialdir=str(Path(current).parent),
            filetypes=[(kind.upper(), f"*{ext}"), ("Todos", "*.*")],
        )
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    # ---- Worker ----------------------------------------------------------
    def _qlog(self, msg: str) -> None:
        self._msg_queue.put(("log", msg))

    def _qprogress(self, pct: float) -> None:
        self._msg_queue.put(("progress", f"{pct:.4f}"))

    def _start(self) -> None:
        in_path = Path(self.input_entry.get().strip())
        out_text = self.output_entry.get().strip()
        kind = self.kind_var.get()
        out_path = Path(out_text) if out_text else in_path.with_suffix("." + kind)
        if out_path.suffix.lower() != "." + kind:
            out_path = out_path.with_suffix("." + kind)

        if not in_path.is_file():
            self._log(f"✖ El archivo de entrada no existe: {in_path}")
            return
        if not self._support.has_ffmpeg:
            self._log("✖ ffmpeg no detectado. Instálalo y reinicia la app.")
            return

        self._cancel_event.clear()
        self._log("─" * 60)
        self._log(f"Inicio:  {in_path}")
        self._log(f"Salida:  {out_path}")
        self._log(f"Formato: {kind.upper()}   Códec: {self.codec_var.get()}   "
                  f"Calidad: {self.quality_var.get()}")

        self._set_state("running")
        self._worker = threading.Thread(
            target=self._run_worker,
            args=(in_path, out_path, kind),
            daemon=True,
        )
        self._worker.start()

    def _run_worker(self, in_path: Path, out_path: Path, kind: str) -> None:
        try:
            # Map display label back to key
            hw_label = self.hw_var.get()
            hw_key = dict(C_HW_OPTIONS)[hw_label]
            convert(
                in_path, out_path,
                output_kind=kind,
                codec=self.codec_var.get(),
                hw_accel=hw_key,
                quality=self.quality_var.get(),
                support=self._support,
                log=self._qlog,
                cancel_flag=self._cancel_event.is_set,
                progress=self._qprogress,
            )
            self._msg_queue.put(("done", str(out_path)))
        except ConvertCancelled:
            self._msg_queue.put(("cancelled", ""))
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb
            self._msg_queue.put((
                "error", f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}"))

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self._log("Cancelación solicitada (parará tras el frame actual)…")

    def _open_folder(self) -> None:
        target = self._last_output_dir
        if target and target.exists():
            _open_path((target))
        else:
            self._log("No hay carpeta de salida reciente.")

    # ---- Queue draining --------------------------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    try:
                        self.progress.set(float(payload))
                    except ValueError:
                        pass
                elif kind == "done":
                    self._last_output_dir = Path(payload).parent
                    self._set_state("done")
                elif kind == "cancelled":
                    self._set_state("cancelled")
                elif kind == "error":
                    self._set_state("error")
                    # `payload` may contain newlines (full traceback).
                    # Print each line separately so the Textbox preserves them.
                    for line in payload.splitlines() or [payload]:
                        self._log(f"✖ {line}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


# ============================================================================
# Enriched transcription tab
# ============================================================================
class EnrichedTab(ctk.CTkFrame):
    def __init__(self, master: ctk.CTk, compute: ComputeSupport) -> None:
        super().__init__(master, fg_color="transparent")

        self._msg_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._cancel_event = threading.Event()
        self._compute = compute  # for the device combobox filter
        self._worker: Optional[threading.Thread] = None
        self._last_output_dir: Optional[Path] = None
        self._last_pdf: Optional[Path] = None

        # Scrollable wrapper so the tab content is reachable on short
        # windows (e.g. 768p) and looks natural on tall ones.
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)

        self._build_ui()
        self._set_state("idle")
        self.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        pad = {"padx": 14, "pady": 6}
        # Inside the scrollable wrapper everything is anchored to `host`,
        # not to `self`. The log box uses a fixed height (no expand=True
        # inside a scrollable frame).
        host = self.scroll

        ctk.CTkLabel(
            host, text="📑  Transcripción enriquecida",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", **pad)
        ctk.CTkLabel(
            host,
            text="Genera un PDF navegable con capturas de pantalla y la "
                 "transcripción asociada a cada momento clave.",
            text_color="gray",
        ).pack(anchor="w", **pad)

        # Input
        in_frame = ctk.CTkFrame(host)
        in_frame.pack(fill="x", **pad)
        ctk.CTkLabel(in_frame, text="Vídeo de entrada").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(in_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.input_entry = ctk.CTkEntry(row)
        self.input_entry.configure(placeholder_text="mp4, mkv, avi, mov…")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Seleccionar…", width=120,
                      command=self._pick_input).pack(side="left")

        # Output dir
        out_frame = ctk.CTkFrame(host)
        out_frame.pack(fill="x", **pad)
        ctk.CTkLabel(out_frame, text="Carpeta de salida").pack(
            anchor="w", padx=10, pady=(8, 0))
        row = ctk.CTkFrame(out_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        self.output_entry = ctk.CTkEntry(row)
        self.output_entry.configure(placeholder_text="Donde se generarán PDF / JSON / TXT / frames")
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(row, text="Examinar…", width=120,
                      command=self._pick_output).pack(side="left")

        # Options grid
        opt_frame = ctk.CTkFrame(host)
        opt_frame.pack(fill="x", **pad)
        ctk.CTkLabel(opt_frame, text="Opciones").pack(
            anchor="w", padx=10, pady=(8, 0))

        form = ctk.CTkFrame(opt_frame, fg_color="transparent")
        form.pack(fill="x", padx=10, pady=(4, 10))
        for c in (0, 1):
            form.grid_columnconfigure(c, weight=1, uniform="cols")

        # Row 0: model
        ctk.CTkLabel(form, text="Modelo Whisper:").grid(
            row=0, column=0, sticky="w", pady=4)
        self.model_var = ctk.StringVar(value="small")
        ctk.CTkComboBox(form, values=T_MODELS, variable=self.model_var,
                        width=130).grid(row=0, column=1, sticky="w", pady=4)

        # Row 1: language
        ctk.CTkLabel(form, text="Idioma:").grid(
            row=1, column=0, sticky="w", pady=4)
        self.lang_var = ctk.StringVar(value="Español")
        lang_labels = [label for label, _ in T_LANGUAGES]
        ctk.CTkComboBox(form, values=lang_labels, variable=self.lang_var,
                        width=160).grid(row=1, column=1, sticky="w", pady=4)

        # Row 2: compute device. "Auto" is the default — tries CUDA
        # then falls back to CPU silently. "GPU" is added only when
        # the startup probe actually found a CUDA device.
        ctk.CTkLabel(form, text="Dispositivo:").grid(
            row=2, column=0, sticky="w", pady=4)
        self.device_var = ctk.StringVar(value=T_DEVICE_OPTIONS[0][0])
        avail = [T_DEVICE_OPTIONS[0], T_DEVICE_OPTIONS[1]]  # Auto + CPU
        if self._compute.has_cuda:
            avail.append(T_DEVICE_OPTIONS[2])               # + GPU
        self.device_combo = ctk.CTkComboBox(
            form, values=[label for label, _ in avail],
            variable=self.device_var, width=200,
        )
        self.device_combo.grid(row=2, column=1, sticky="w", pady=4)

        # Row 3: frame mode
        ctk.CTkLabel(form, text="Modo de frames:").grid(
            row=3, column=0, sticky="w", pady=4)
        self.mode_var = ctk.StringVar(value="hybrid")
        ctk.CTkComboBox(form,
                        values=[label for label, _ in E_FRAME_MODES],
                        variable=self.mode_var, width=240,
                        command=lambda _v: self._refresh_sliders(),
                        ).grid(row=3, column=1, sticky="w", pady=4)

        # Row 4: interval slider
        ctk.CTkLabel(form, text="Intervalo entre frames (s):").grid(
            row=4, column=0, sticky="w", pady=4)
        self.interval_var = ctk.IntVar(value=15)
        self.interval_slider = ctk.CTkSlider(
            form, from_=5, to=60, variable=self.interval_var, width=180,
            command=lambda _v: self._update_labels())
        self.interval_slider.grid(row=4, column=1, sticky="w", pady=4)
        self.interval_lbl = ctk.CTkLabel(form, text="15 s", text_color="gray")
        self.interval_lbl.grid(row=4, column=1, sticky="e", padx=10, pady=4)

        # Row 5: scene threshold slider (clamped to E_SCENE_MAX).
        ctk.CTkLabel(form, text="Sensibilidad de escena:").grid(
            row=5, column=0, sticky="w", pady=4)
        self.scene_var = ctk.DoubleVar(value=E_SCENE_DEFAULT)
        self.scene_slider = ctk.CTkSlider(
            form, from_=E_SCENE_MIN, to=E_SCENE_MAX,
            variable=self.scene_var, width=180,
            command=lambda _v: self._update_labels())
        self.scene_slider.grid(row=5, column=1, sticky="w", pady=4)
        self.scene_lbl = ctk.CTkLabel(
            form, text=f"{E_SCENE_DEFAULT:.2f}", text_color="gray")
        self.scene_lbl.grid(row=5, column=1, sticky="e", padx=10, pady=4)

        # Row 6: correlation window
        ctk.CTkLabel(form, text="Ventana de correlación (±s):").grid(
            row=6, column=0, sticky="w", pady=4)
        self.window_var = ctk.IntVar(value=15)
        self.window_slider = ctk.CTkSlider(
            form, from_=5, to=60, variable=self.window_var, width=180,
            command=lambda _v: self._update_labels())
        self.window_slider.grid(row=6, column=1, sticky="w", pady=4)
        self.window_lbl = ctk.CTkLabel(form, text="15 s", text_color="gray")
        self.window_lbl.grid(row=6, column=1, sticky="e", padx=10, pady=4)

        # Row 7: image quality preset (compresses frames to JPEG to shrink
        # PDF / frames folder dramatically without visible quality loss).
        ctk.CTkLabel(form, text="Calidad de imagen:").grid(
            row=7, column=0, sticky="w", pady=4)
        self.quality_var = ctk.StringVar(value=E_IMAGE_QUALITY_DEFAULT)
        ctk.CTkComboBox(
            form,
            values=[label for label, _ in E_IMAGE_QUALITY],
            variable=self.quality_var, width=240,
        ).grid(row=7, column=1, sticky="w", pady=4)

        # --- Avanzado: deduplicación perceptual ---------------------------
        # Collapsible block so the default UI stays compact. Only opened
        # by users who care about frame-density tuning.
        self.advanced_open = ctk.BooleanVar(value=False)
        adv_header = ctk.CTkFrame(host, fg_color="transparent")
        adv_header.pack(fill="x", **pad)
        self._adv_toggle = ctk.CTkButton(
            adv_header,
            text="⚙️  Opciones avanzadas (deduplicación perceptual)",
            command=self._toggle_advanced,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"),
            anchor="w", height=32,
        )
        self._adv_toggle.pack(fill="x")

        self.advanced_frame = ctk.CTkFrame(host)
        # Frame is created but not packed — _toggle_advanced shows it.

        adv_inner = ctk.CTkFrame(self.advanced_frame, fg_color="transparent")
        adv_inner.pack(fill="x", padx=10, pady=8)
        for c in (0, 1):
            adv_inner.grid_columnconfigure(c, weight=1, uniform="adv")

        # Checkbox principal — si está desmarcado, el resto de controles
        # no afectan al resultado (pasamos threshold=64 = desactivado).
        self.dedupe_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            adv_inner,
            text="Activar deduplicación perceptual (pHash + clustering)",
            variable=self.dedupe_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # Slider 0-10: 0 = muy agresivo (solo cambios enormes pasan),
        # 10 = permisivo (cualquier diferencia pasa). Default 5.
        ctk.CTkLabel(
            adv_inner,
            text="Umbral perceptual (0=agresivo, 10=permisivo):",
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.perc_var = ctk.IntVar(value=5)
        self.perc_slider = ctk.CTkSlider(
            adv_inner, from_=0, to=10, variable=self.perc_var, width=180,
            command=lambda _v: self._update_labels(),
        )
        self.perc_slider.grid(row=1, column=1, sticky="w", pady=4)
        self.perc_lbl = ctk.CTkLabel(adv_inner, text="5", text_color="gray")
        self.perc_lbl.grid(row=1, column=1, sticky="e", padx=10, pady=4)

        # Slider ventana de agrupación (5-60 s)
        ctk.CTkLabel(
            adv_inner, text="Ventana de agrupación (s):",
        ).grid(row=2, column=0, sticky="w", pady=4)
        self.cluster_var = ctk.IntVar(value=30)
        self.cluster_slider = ctk.CTkSlider(
            adv_inner, from_=5, to=60, variable=self.cluster_var, width=180,
            command=lambda _v: self._update_labels(),
        )
        self.cluster_slider.grid(row=2, column=1, sticky="w", pady=4)
        self.cluster_lbl = ctk.CTkLabel(
            adv_inner, text="30 s", text_color="gray")
        self.cluster_lbl.grid(row=2, column=1, sticky="e", padx=10, pady=4)

        # Outputs checkboxes
        out_opts = ctk.CTkFrame(host)
        out_opts.pack(fill="x", **pad)
        ctk.CTkLabel(out_opts, text="Generar").pack(anchor="w", padx=10, pady=(8, 0))
        checks = ctk.CTkFrame(out_opts, fg_color="transparent")
        checks.pack(anchor="w", padx=10, pady=(4, 10))
        self.pdf_var = ctk.BooleanVar(value=True)
        self.json_var = ctk.BooleanVar(value=True)
        self.txt_var = ctk.BooleanVar(value=True)
        self.frames_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(checks, text="PDF", variable=self.pdf_var).pack(
            side="left", padx=(0, 16))
        ctk.CTkCheckBox(checks, text="JSON estructurado", variable=self.json_var).pack(
            side="left", padx=(0, 16))
        ctk.CTkCheckBox(checks, text="TXT con timestamps", variable=self.txt_var).pack(
            side="left", padx=(0, 16))
        ctk.CTkCheckBox(checks, text="Carpeta de frames", variable=self.frames_var).pack(
            side="left", padx=(0, 16))

        # Status + progress (compact row)
        status_frame = ctk.CTkFrame(host, fg_color="transparent")
        status_frame.pack(fill="x", **pad)
        self.status_label = ctk.CTkLabel(
            status_frame, text="Listo.", anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            status_frame, text="📋  Copiar log", width=130,
            command=self._copy_log,
        ).pack(side="right", padx=(6, 0))

        self.progress = ctk.CTkProgressBar(host)
        self.progress.pack(fill="x", padx=14, pady=(0, 4))
        self.progress.set(0)

        # Action row
        action_frame = ctk.CTkFrame(host, fg_color="transparent")
        action_frame.pack(fill="x", padx=14, pady=(0, 8))
        self.start_btn = ctk.CTkButton(
            action_frame, text="▶  Analizar",
            command=self._start, width=160)
        self.start_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(
            action_frame, text="✖  Cancelar", command=self._cancel,
            fg_color="#a33", hover_color="#822", width=110)
        self.open_pdf_btn = ctk.CTkButton(
            action_frame, text="📄  Abrir PDF",
            command=self._open_pdf, width=120)
        self.open_dir_btn = ctk.CTkButton(
            action_frame, text="📂  Abrir carpeta",
            command=self._open_folder, width=140)

        # Log section — RIGHT BELOW the buttons, big, always visible.
        log_header = ctk.CTkFrame(host, fg_color="transparent")
        log_header.pack(fill="x", padx=14, pady=(4, 0))
        ctk.CTkLabel(
            log_header, text="📜  Registro  (mensajes del proceso en vivo)",
            anchor="w",
        ).pack(side="left")

        # Big log box. fill="both" + expand=True lets it fill the
        # scrollable area when there's room.
        self.log_box = ctk.CTkTextbox(
            host, height=320, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(2, 14))

        self._update_labels()

    def _refresh_sliders(self) -> None:
        mode = dict(E_FRAME_MODES).get(self.mode_var.get(), "hybrid")
        # Interval only matters for interval/hybrid
        is_interval = mode in ("hybrid", "interval")
        self.interval_slider.configure(state="normal" if is_interval else "disabled")
        # Scene only matters for scene/hybrid
        is_scene = mode in ("hybrid", "scene")
        self.scene_slider.configure(state="normal" if is_scene else "disabled")

    def _toggle_advanced(self) -> None:
        """Show / hide the advanced (perceptual dedup) controls."""
        if self.advanced_open.get():
            self.advanced_frame.pack_forget()
            self._adv_toggle.configure(
                text="⚙️  Opciones avanzadas (deduplicación perceptual)")
            self.advanced_open.set(False)
        else:
            self.advanced_frame.pack(fill="x", padx=14, pady=(2, 6))
            self._adv_toggle.configure(
                text="⚙️  Opciones avanzadas (ocultar)")
            self.advanced_open.set(True)

    def _update_labels(self) -> None:
        self.interval_lbl.configure(text=f"{int(self.interval_var.get())} s")
        self.scene_lbl.configure(text=f"{self.scene_var.get():.2f}")
        self.window_lbl.configure(text=f"{int(self.window_var.get())} s")
        # Advanced (dedup) labels — guard with hasattr in case the
        # constructor hasn't built them yet (defensive).
        if hasattr(self, "perc_lbl"):
            self.perc_lbl.configure(text=str(int(self.perc_var.get())))
        if hasattr(self, "cluster_lbl"):
            self.cluster_lbl.configure(
                text=f"{int(self.cluster_var.get())} s")

    def _copy_log(self) -> None:
        """Copy the current log to the clipboard so users can paste it
        into a chat / ticket for debugging."""
        try:
            content = self.log_box.get("1.0", "end")
        except Exception:
            content = ""
        if not content.strip():
            self._log("(el log estaba vacío, nada que copiar)")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.update()  # keep clipboard after window loses focus
            self._log(f"✓ Log copiado al portapapeles ({len(content)} caracteres).")
        except Exception as exc:  # noqa: BLE001
            self._log(f"⚠ No se pudo copiar al portapapeles: {exc}")

    def _set_state(self, state: str) -> None:
        running = state == "running"
        self.cancel_btn.pack_forget()
        self.open_pdf_btn.pack_forget()
        self.open_dir_btn.pack_forget()

        if running:
            self.start_btn.configure(state="disabled")
            self.cancel_btn.pack(side="left", padx=(10, 0))
            self.status_label.configure(text="Analizando…")
            self.progress.configure(mode="determinate")
            self.progress.set(0)
        else:
            self.start_btn.configure(state="normal")
            self.progress.stop()
            self.progress.configure(mode="determinate")
            if state == "idle":
                self.progress.set(0)
                self.status_label.configure(text="Listo.")
            elif state == "done":
                self.progress.set(1)
                self.status_label.configure(text="✓ Enriquecimiento completado.")
                self.open_pdf_btn.pack(side="left", padx=(10, 0))
                self.open_dir_btn.pack(side="left", padx=(6, 0))
            elif state == "cancelled":
                self.progress.set(0)
                self.status_label.configure(text="Cancelado.")
            elif state == "error":
                self.progress.set(0)
                self.status_label.configure(
                    text="✖ Error. Revisa el registro.")

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecciona vídeo",
            filetypes=[("Vídeo", E_VIDEO_EXTS), ("Todos", "*.*")],
        )
        if not path:
            return
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, path)
        if not self.output_entry.get().strip():
            p = Path(path)
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, str(p.parent / (p.stem + "_enriquecido")))

    def _pick_output(self) -> None:
        current = self.output_entry.get().strip() or "salida_enriquecida"
        path = filedialog.askdirectory(
            title="Carpeta de salida",
            initialdir=current if Path(current).exists() else None,
        )
        if path:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, path)

    def _qlog(self, msg: str) -> None:
        self._msg_queue.put(("log", msg))

    def _qprogress(self, pct: float) -> None:
        self._msg_queue.put(("progress", f"{pct:.4f}"))

    def _start(self) -> None:
        in_path = Path(self.input_entry.get().strip())
        out_text = self.output_entry.get().strip()
        if not in_path.is_file():
            self._log(f"✖ El archivo de entrada no existe: {in_path}")
            return
        if not out_text:
            out_text = str(in_path.parent / (in_path.stem + "_enriquecido"))
        out_dir = Path(out_text)

        ok, info = check_ffmpeg()
        if not ok:
            self._log(f"✖ {info}")
            return

        self._cancel_event.clear()
        self._log("─" * 60)
        self._log(f"Inicio: {in_path}")
        self._log(f"Modelo: {self.model_var.get()}   "
                  f"Idioma: {self.lang_var.get()}   "
                  f"Dispositivo: {self.device_var.get()}")
        self._log(f"Salida: {out_dir}")

        self._set_state("running")
        self._worker = threading.Thread(
            target=self._run_worker, args=(in_path, out_dir), daemon=True)
        self._worker.start()

    def _run_worker(self, in_path: Path, out_dir: Path) -> None:
        import traceback as _tb
        try:
            lang_code = dict(T_LANGUAGES)[self.lang_var.get()]
            mode_key = dict(E_FRAME_MODES)[self.mode_var.get()]
            device_key = dict(T_DEVICE_OPTIONS)[self.device_var.get()]
            results = enrich_video(
                in_path, out_dir,
                model_size=self.model_var.get(),
                language=lang_code,
                mode=mode_key,
                scene_threshold=float(self.scene_var.get()),
                interval_s=float(self.interval_var.get()),
                window_s=float(self.window_var.get()),
                write_pdf=self.pdf_var.get(),
                write_json=self.json_var.get(),
                write_txt=self.txt_var.get(),
                keep_frames=self.frames_var.get(),
                image_quality=dict(E_IMAGE_QUALITY)[self.quality_var.get()],
                device=device_key,
                # 64 = "threshold unreachable" => dedup_keyframes() bails
                # out and keeps every frame (preserves old behavior).
                perceptual_threshold=(
                    int(self.perc_var.get()) if self.dedupe_var.get() else 64
                ),
                cluster_window_s=float(self.cluster_var.get()),
                log=self._qlog,
                cancel_flag=self._cancel_event.is_set,
                progress=self._qprogress,
            )
            self._last_pdf = results.get("pdf")
            self._last_output_dir = out_dir
            self._msg_queue.put(("done", str(out_dir)))
        except EnrichCancelled:
            self._msg_queue.put(("cancelled", ""))
        except Exception as exc:  # noqa: BLE001
            # Capture the FULL traceback so multi-line errors
            # (e.g. ffmpeg stderr) reach the log box.
            self._msg_queue.put((
                "error",
                f"{type(exc).__name__}: {exc}\n{_tb.format_exc()}",
            ))

    def _cancel(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel_event.set()
            self._log("Cancelación solicitada…")

    def _open_pdf(self) -> None:
        if self._last_pdf and self._last_pdf.exists():
            _open_path((self._last_pdf))
        else:
            self._log("No hay PDF generado todavía.")

    def _open_folder(self) -> None:
        target = self._last_output_dir
        if target and target.exists():
            _open_path((target))
        else:
            self._log("No hay carpeta de salida reciente.")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    try:
                        self.progress.set(float(payload))
                    except ValueError:
                        pass
                elif kind == "done":
                    self._set_state("done")
                elif kind == "cancelled":
                    self._set_state("cancelled")
                elif kind == "error":
                    self._set_state("error")
                    # `payload` may contain newlines (full traceback).
                    # Print each line separately so the Textbox preserves them.
                    for line in payload.splitlines() or [payload]:
                        self._log(f"✖ {line}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


# ============================================================================
# App shell
# ============================================================================
class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("900x780")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Detect encoders + compute once at startup (both are cheap)
        self._support = detect_encoders()
        self._compute = detect_compute()
        ok, info = check_ffmpeg()
        ffmpeg_status = (
            f"✓ ffmpeg: {info}" if ok
            else f"⚠ ffmpeg: {info}"
        )
        if self._support.has_ffmpeg:
            present = ", ".join(HW_ACCEL[k][0] for k in self._support.available_keys)
            encoder_status = f"Aceleración disponible: {present}"
        else:
            encoder_status = "Sin detección de encoders (ffmpeg ausente)."
        # GPU status line (separate from video encoders, which are
        # about ffmpeg; this is about faster-whisper transcription).
        if self._compute.has_cuda:
            compute_status = (
                f"GPU transcripción: NVIDIA CUDA "
                f"({self._compute.cuda_device_count} disp.)"
            )
        else:
            compute_status = "GPU transcripción: no detectada (CPU only)"
            if self._compute.reason:
                print(f"[i] CUDA no disponible: {self._compute.reason}",
                      flush=True)

        # Tabview
        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs.add("🏠 Inicio")
        self.tabs.add("🎙️ Transcriptor")
        self.tabs.add("🔄 Conversor")
        self.tabs.add("📑 Enriquecida")

        # Home tab callbacks
        def go_to_transcriber() -> None:
            self.tabs.set("🎙️ Transcriptor")

        def go_to_converter() -> None:
            self.tabs.set("🔄 Conversor")

        def go_to_enriched() -> None:
            self.tabs.set("📑 Enriquecida")

        self.home_tab = HomeTab(
            self.tabs.tab("🏠 Inicio"),
            on_open_transcriber=go_to_transcriber,
            on_open_converter=go_to_converter,
            on_open_enriched=go_to_enriched,
            ffmpeg_status=ffmpeg_status,
            encoder_status=encoder_status,
        )
        self.home_tab.pack(fill="both", expand=True)

        self.transcriber_tab = TranscriberTab(
            self.tabs.tab("🎙️ Transcriptor"), self._compute)
        self.transcriber_tab.pack(fill="both", expand=True)

        self.converter_tab = ConverterTab(
            self.tabs.tab("🔄 Conversor"), self._support)
        self.converter_tab.pack(fill="both", expand=True)

        self.enriched_tab = EnrichedTab(
            self.tabs.tab("📑 Enriquecida"), self._compute)
        self.enriched_tab.pack(fill="both", expand=True)

        # Status line at bottom
        self.status_bar = ctk.CTkLabel(
            self,
            text=ffmpeg_status + "    ·    " + compute_status
                 + "    ·    " + encoder_status,
            anchor="w", text_color="gray", height=22,
        )
        self.status_bar.pack(fill="x", padx=14, pady=(0, 6))

        # Default focus to home
        self.tabs.set("🏠 Inicio")


def main() -> None:
    try:
        app = App()
        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"\n[error fatal al iniciar] {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        if sys.platform == "win32":
            try:
                input("\nPulsa Enter para cerrar esta ventana...")
            except EOFError:
                pass
        raise


if __name__ == "__main__":
    main()