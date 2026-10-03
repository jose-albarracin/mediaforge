"""Heimdall: desktop shell.

Three tools in a sidebar:
  - Transcribir reunión: one flow that produces either a PDF with
    screenshots synced to the transcript, or plain text.
  - Grabar clase: records what a window shows and plays (nothing is
    downloaded), checks whether the platform protects it, and builds the
    same document from the recording.
  - Convertir vídeo: MP4 re-encode or MP3 extraction.

Every technical option still exists, but lives under "Opciones avanzadas"
with defaults that work, so a non-technical user only picks a file and
presses one button. Long jobs run in worker threads (see ui_kit.JobPage).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

import recorder
import ui_kit as ui
from analyzer import CancelledError as EnrichCancelled
from analyzer import enrich_video
from converter import CancelledError as ConvertCancelled
from converter import HW_ACCEL, EncoderSupport, convert, detect_encoders
from report import fmt_duration, probe_duration
from transcriber import CancelledError as TranscribeCancelled
from transcriber import ComputeSupport, check_ffmpeg, detect_compute, transcribe_video

APP_NAME = "Heimdall"
BRANDING = Path(__file__).parent / "branding"
APP_VERSION = "2.2"

# ---- Options (label shown, value passed to the engine) ------------------------
MODELS = [
    ("Rápido, menos preciso (base)", "base"),
    ("Equilibrado (small) · recomendado", "small"),
    ("Preciso, más lento (medium)", "medium"),
    ("Máxima precisión, muy lento (large-v3)", "large-v3"),
    ("Prueba rápida (tiny)", "tiny"),
]
LANGUAGES: list[tuple[str, Optional[str]]] = [
    ("Español", "es"),
    ("Inglés", "en"),
    ("Portugués", "pt"),
    ("Francés", "fr"),
    ("Alemán", "de"),
    ("Italiano", "it"),
    ("Detectar automáticamente", None),
]
DEVICES = [
    ("Automático (GPU si hay)", "auto"),
    ("Solo procesador (CPU)", "cpu"),
    ("GPU NVIDIA (CUDA)", "cuda"),
]
FRAME_MODES = [
    ("Cambios de pantalla + cada cierto tiempo", "hybrid"),
    ("Solo cuando cambia la pantalla", "scene"),
    ("Cada cierto tiempo", "interval"),
]
IMAGE_QUALITY = [
    ("Media (recomendada)", "medium"),
    ("Alta (archivo más pesado)", "high"),
    ("Baja (archivo más liviano)", "low"),
]
OUT_DOC = "Documento con capturas"
OUT_TEXT = "Solo texto"

MEDIA_EXTS = (".mp4 .mov .mkv .avi .webm .m4v .wmv .flv .ts "
              ".mp3 .wav .m4a .flac .ogg .aac .opus")
VIDEO_EXTS = ".mp4 .mov .mkv .avi .webm .m4v .wmv .flv .ts .m2ts .3gp .ogv .vob"
AUDIO_EXTS = ".mp3 .wav .m4a .flac .ogg .aac .opus .wma"

C_HW = [
    ("Automática (la mejor disponible)", "auto"),
    ("Procesador (CPU)", "cpu"),
    ("NVIDIA NVENC", "nvidia"),
    ("AMD AMF", "amd"),
    ("Intel Quick Sync", "intel"),
]
C_CODECS = [("H.264 (máxima compatibilidad)", "h264"),
            ("H.265 (archivo más pequeño)", "hevc")]
C_KIND_VIDEO = "Vídeo MP4"
C_KIND_AUDIO = "Solo audio MP3"
C_QUALITY = {"Alta": "alta", "Media": "media", "Baja": "baja"}


def _open_path(path) -> None:
    """Open a file or folder with the OS default app (any platform)."""
    path = str(path)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _reveal(path: Path) -> None:
    """Show a file in its folder (Finder/Explorer), or open the folder."""
    if sys.platform == "darwin" and path.exists():
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32" and path.is_file():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        _open_path(path if path.is_dir() else path.parent)


def _labels(pairs) -> list[str]:
    return [label for label, _ in pairs]


def _file_row(cell, placeholder: str, button_text: str, command) -> ctk.CTkEntry:
    cell.grid_columnconfigure(0, weight=1)
    field = ui.entry(cell, placeholder)
    field.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    ui.secondary_button(cell, button_text, command, width=110).grid(row=0, column=1)
    return field


def _set_entry(field: ctk.CTkEntry, value: str) -> None:
    field.delete(0, "end")
    field.insert(0, value)


class _AdvancedMixin:
    """Shared "Mostrar/Ocultar opciones avanzadas" disclosure."""

    advanced: ctk.CTkFrame
    adv_toggle: ctk.CTkButton

    def _toggle_advanced(self) -> None:
        if self.advanced.winfo_manager():
            self.advanced.pack_forget()
            self.adv_toggle.configure(text="Mostrar opciones avanzadas")
        else:
            self.advanced.pack(fill="x")
            self.adv_toggle.configure(text="Ocultar opciones avanzadas")


# ============================================================================
# Transcribir reunión
# ============================================================================
class TranscribePage(_AdvancedMixin, ui.JobPage):
    title = "Transcribir reunión"
    subtitle = ("Elige la grabación y pulsa el botón. Todo se procesa en este "
                "equipo: el archivo no se sube a ningún sitio.")
    action_text = "Crear documento"

    def __init__(self, master, compute: ComputeSupport) -> None:
        self._compute = compute
        self._result: Optional[Path] = None
        super().__init__(master)
        self._open_btn = self.add_result_button("Abrir documento", self._open_result)
        self.add_result_button("Mostrar carpeta", self._show_folder)

    # ---- form ----------------------------------------------------------------
    def build_form(self, host) -> None:
        sec = ui.FormSection(host, "Grabación")
        sec.pack(fill="x")
        self.in_field = _file_row(sec.row("Archivo"),
                                  "Vídeo o audio de la reunión (mp4, mov, mp3, wav…)",
                                  "Elegir…", self._pick_input)
        self.in_info = sec.note("")

        sec = ui.FormSection(host, "Resultado")
        sec.pack(fill="x")
        self.out_kind = ctk.StringVar(value=OUT_DOC)
        ui.segmented(sec.row("Quiero obtener"), [OUT_DOC, OUT_TEXT], self.out_kind,
                     command=self._on_kind).pack(anchor="w")
        self.kind_note = sec.note("")
        self.lang_var = ctk.StringVar(value=LANGUAGES[0][0])
        ui.option_menu(sec.row("Idioma hablado"), _labels(LANGUAGES),
                       self.lang_var, width=240).pack(anchor="w")
        self.out_field = _file_row(sec.row("Guardar en"), "Se sugiere al elegir el archivo",
                                   "Cambiar…", self._pick_output)

        self.adv_toggle = ui.link_button(host, "Mostrar opciones avanzadas",
                                         self._toggle_advanced)
        self.adv_toggle.pack(anchor="w", pady=(0, 12))
        self.advanced = ctk.CTkFrame(host, fg_color="transparent")
        self._build_advanced(self.advanced)
        self._on_kind()

    def _build_advanced(self, host) -> None:
        sec = ui.FormSection(host, "Transcripción")
        sec.pack(fill="x")
        self.model_var = ctk.StringVar(value=MODELS[1][0])
        ui.option_menu(sec.row("Modelo"), _labels(MODELS), self.model_var,
                       width=300).pack(anchor="w")
        sec.note("Cada modelo se descarga la primera vez que se usa "
                 "(small ≈ 500 MB, large-v3 ≈ 3 GB).")
        devices = DEVICES if self._compute.has_cuda else DEVICES[:2]
        self.device_var = ctk.StringVar(value=devices[0][0])
        ui.option_menu(sec.row("Procesar con"), _labels(devices), self.device_var,
                       width=300).pack(anchor="w")
        if not self._compute.has_cuda:
            sec.note("No se detectó una GPU NVIDIA: se usará el procesador.")

        self.doc_sections = ctk.CTkFrame(host, fg_color="transparent")
        self.doc_sections.pack(fill="x")
        sec = ui.FormSection(self.doc_sections, "Capturas de pantalla")
        sec.pack(fill="x")
        self.mode_var = ctk.StringVar(value=FRAME_MODES[0][0])
        ui.option_menu(sec.row("Cuándo capturar"), _labels(FRAME_MODES), self.mode_var,
                       width=300, command=self._on_mode).pack(anchor="w")
        self.interval_var = ctk.IntVar(value=15)
        self.interval_row = ui.SliderRow(sec.row("Cada"), self.interval_var, 5, 60, 11,
                                         lambda v: f"{int(v)} s")
        self.interval_row.pack(anchor="w")
        self.scene_var = ctk.DoubleVar(value=0.30)
        self.scene_row = ui.SliderRow(sec.row("Sensibilidad"), self.scene_var,
                                      0.01, 0.40, 39, lambda v: f"{v:.2f}")
        self.scene_row.pack(anchor="w")
        sec.note("Un valor más bajo captura cambios más pequeños; útil cuando "
                 "comparten pantalla.")
        self.window_var = ctk.IntVar(value=15)
        ui.SliderRow(sec.row("Texto por captura"), self.window_var, 5, 60, 11,
                     lambda v: f"±{int(v)} s").pack(anchor="w")
        self.quality_var = ctk.StringVar(value=IMAGE_QUALITY[0][0])
        ui.option_menu(sec.row("Calidad de imagen"), _labels(IMAGE_QUALITY),
                       self.quality_var, width=300).pack(anchor="w")

        sec = ui.FormSection(self.doc_sections, "Capturas repetidas")
        sec.pack(fill="x")
        self.dedupe_var = ctk.BooleanVar(value=True)
        ui.checkbox(sec.row("Filtrar"), "Quitar capturas casi iguales",
                    self.dedupe_var, command=self._on_dedupe).pack(anchor="w")
        self.perc_var = ctk.IntVar(value=5)
        self.perc_row = ui.SliderRow(sec.row("Parecido"), self.perc_var, 0, 10, 10,
                                     lambda v: str(int(v)))
        self.perc_row.pack(anchor="w")
        self.cluster_var = ctk.IntVar(value=30)
        self.cluster_row = ui.SliderRow(sec.row("Separación mínima"), self.cluster_var,
                                        5, 60, 11, lambda v: f"{int(v)} s")
        self.cluster_row.pack(anchor="w")

        sec = ui.FormSection(self.doc_sections, "Archivos que se generan")
        sec.pack(fill="x")
        cell = sec.row("Incluir")
        self.pdf_var = ctk.BooleanVar(value=True)
        self.txt_var = ctk.BooleanVar(value=True)
        self.json_var = ctk.BooleanVar(value=False)
        self.frames_var = ctk.BooleanVar(value=False)
        for text, var in (("PDF", self.pdf_var), ("Texto con horas", self.txt_var),
                          ("JSON", self.json_var), ("Carpeta de imágenes", self.frames_var)):
            ui.checkbox(cell, text, var).pack(side="left", padx=(0, 16))

    # ---- interactions ----------------------------------------------------------
    def _is_doc(self) -> bool:
        return self.out_kind.get() == OUT_DOC

    def _on_kind(self, *_args) -> None:
        if self._is_doc():
            self.kind_note.configure(
                text="Un PDF con una captura de la pantalla en cada momento clave y lo "
                     "que se dijo en ese momento. Necesita un vídeo.")
            self.doc_sections.pack(fill="x")
        else:
            self.kind_note.configure(
                text="Un archivo .txt con todo lo que se dijo. Sirve para vídeo o audio.")
            self.doc_sections.pack_forget()
        if hasattr(self, "start_btn"):
            self.start_btn.configure(
                text="Crear documento" if self._is_doc() else "Transcribir")
        self._suggest_output()
        self._on_mode()
        self._on_dedupe()

    def _on_mode(self, *_args) -> None:
        mode = dict(FRAME_MODES)[self.mode_var.get()]
        self.interval_row.set_enabled(mode != "scene")
        self.scene_row.set_enabled(mode != "interval")

    def _on_dedupe(self) -> None:
        on = self.dedupe_var.get()
        self.perc_row.set_enabled(on)
        self.cluster_row.set_enabled(on)

    def _suggest_output(self) -> None:
        src = self.in_field.get().strip()
        if not src:
            return
        p = Path(src)
        target = (p.parent / f"{p.stem}_transcripcion") if self._is_doc() \
            else p.with_suffix(".txt")
        _set_entry(self.out_field, str(target))

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Elige la grabación",
            filetypes=[("Vídeo o audio", MEDIA_EXTS), ("Todos", "*.*")])
        if path:
            self.load_input(Path(path))

    def load_input(self, p: Path) -> None:
        _set_entry(self.in_field, str(p))
        duration = probe_duration(p)
        size_mb = p.stat().st_size / 1_048_576
        info = f"{size_mb:.1f} MB".replace(".", ",")
        if duration:
            info = f"Duración {fmt_duration(duration).replace('.', ',')} · {info}"
        if p.suffix.lower() in AUDIO_EXTS.split() and self._is_doc():
            self.out_kind.set(OUT_TEXT)
            info += " · es solo audio, se generará texto"
        self._on_kind()
        self.in_info.configure(text=info)
        self.set_state("idle", "Listo para empezar.")

    def _pick_output(self) -> None:
        current = self.out_field.get().strip()
        if self._is_doc():
            path = filedialog.askdirectory(
                title="Carpeta para el documento",
                initialdir=str(Path(current).parent) if current else None)
        else:
            path = filedialog.asksaveasfilename(
                title="Guardar texto como", defaultextension=".txt",
                initialfile=Path(current).name if current else "transcripcion.txt",
                filetypes=[("Texto", "*.txt")])
        if path:
            _set_entry(self.out_field, path)

    # ---- job ---------------------------------------------------------------------
    def start(self) -> None:
        raw = self.in_field.get().strip()
        src = Path(raw)
        if not raw or not src.is_file():
            self.fail("Elige primero la grabación.")
            return
        ok, info = check_ffmpeg()
        if not ok:
            self.fail(info)
            return
        if not self.out_field.get().strip():
            self._suggest_output()
        out = Path(self.out_field.get().strip())
        lang = dict(LANGUAGES)[self.lang_var.get()]
        model = dict(MODELS)[self.model_var.get()]
        device = dict(DEVICES)[self.device_var.get()]

        if self._is_doc():
            if not any(v.get() for v in (self.pdf_var, self.txt_var, self.json_var)):
                self.fail("Marca al menos un archivo para generar en las opciones avanzadas.")
                return
            opts = dict(
                model_size=model, language=lang,
                mode=dict(FRAME_MODES)[self.mode_var.get()],
                scene_threshold=float(self.scene_var.get()),
                interval_s=float(self.interval_var.get()),
                window_s=float(self.window_var.get()),
                write_pdf=self.pdf_var.get(), write_json=self.json_var.get(),
                write_txt=self.txt_var.get(), keep_frames=self.frames_var.get(),
                image_quality=dict(IMAGE_QUALITY)[self.quality_var.get()],
                device=device,
                perceptual_threshold=int(self.perc_var.get()) if self.dedupe_var.get() else 64,
                cluster_window_s=float(self.cluster_var.get()),
            )
            self.log("─" * 60)
            self.log(f"Grabación: {src}\nCarpeta: {out}")

            def job() -> str:
                res = enrich_video(src, out, log=self.qlog, cancel_flag=self.cancelled,
                                   progress=self.qprogress, **opts)
                main = res.get("pdf") or res.get("txt") or res.get("json")
                return str(main or out)

            self.run_job(job, (EnrichCancelled,), determinate=True,
                         message="Creando el documento…")
        else:
            out = out if out.suffix.lower() == ".txt" else out.with_suffix(".txt")
            self.log("─" * 60)
            self.log(f"Grabación: {src}\nTexto: {out}")

            def job() -> str:
                transcribe_video(src, out, model, lang, log=self.qlog,
                                 cancel_flag=self.cancelled, device=device)
                return str(out)

            self.run_job(job, (TranscribeCancelled,), determinate=False,
                         message="Transcribiendo… puede tardar varios minutos.")

    def on_done(self, payload: str) -> None:
        self._result = Path(payload)
        kind = "PDF" if self._result.suffix == ".pdf" else "texto"
        self._open_btn.configure(text=f"Abrir {kind}")
        self.set_state("done", f"Listo: {self._result.name}")

    def _open_result(self) -> None:
        if self._result and self._result.exists():
            _open_path(self._result)

    def _show_folder(self) -> None:
        if self._result:
            _reveal(self._result)


# ============================================================================
# Grabar clase
# ============================================================================
VERDICT_COLOR = {"full": ui.SUCCESS, "audio_only": ui.TEXT, "video_only": ui.ERROR_TEXT,
                 "paused": ui.TEXT, "blocked": ui.ERROR_TEXT, "unknown": ui.TEXT}


class RecordPage(_AdvancedMixin, ui.JobPage):
    title = "Grabar clase"
    subtitle = ("Graba lo que se ve y se escucha en una ventana mientras ves la clase, "
                "y al terminar crea el documento. No descarga nada de la página: "
                "solo registra lo que sale por tu pantalla y tus parlantes.")
    action_text = "Grabar"

    def __init__(self, master, compute: ComputeSupport) -> None:
        self._compute = compute
        self._windows: list[recorder.Window] = []
        self._rec: Optional[recorder.Recording] = None
        self._stop = threading.Event()
        self._result: Optional[Path] = None
        self._loaded = False
        self._unavailable = recorder.platform_note()
        super().__init__(master)
        self._open_btn = self.add_result_button("Abrir documento", self._open_result)
        self.add_result_button("Mostrar carpeta", self._show_folder)
        self.stop_btn = ui.primary_button(self.buttons, "Detener y crear", self._request_stop,
                                          width=160)
        if self._unavailable:
            self.fail(self._unavailable)
            self.start_btn.configure(state="disabled")

    # ---- form ----------------------------------------------------------------
    def build_form(self, host) -> None:
        sec = ui.FormSection(host, "Clase")
        sec.pack(fill="x")
        cell = sec.row("Ventana")
        cell.grid_columnconfigure(0, weight=1)
        self.win_var = ctk.StringVar(value="Abre la clase en el navegador y pulsa Actualizar")
        self.win_menu = ui.option_menu(cell, [self.win_var.get()], self.win_var, width=420,
                                       command=lambda _v: self._clear_verdict())
        self.win_menu.grid(row=0, column=0, sticky="w", padx=(0, 8))
        ui.secondary_button(cell, "Actualizar", self._refresh_windows, width=110).grid(
            row=0, column=1)
        sec.note("Se graba solo esa ventana y el sonido de su aplicación. Puedes usar "
                 "otras ventanas mientras tanto, pero no cierres ni minimices la de la clase.")

        sec = ui.FormSection(host, "Revisar protección")
        sec.pack(fill="x")
        cell = sec.row("Antes de grabar")
        self.probe_btn = ui.secondary_button(cell, "Probar 10 s", self._start_probe, width=130)
        self.probe_btn.pack(side="left")
        self.verdict_label = ctk.CTkLabel(cell, text="", font=ui.font(13, "bold"),
                                          text_color=ui.TEXT, anchor="w")
        self.verdict_label.pack(side="left", padx=(12, 0))
        self.verdict_note = sec.note(
            "Dale play a la clase y pulsa Probar. Heimdall graba unos segundos y te dice si "
            "la plataforma deja grabar la imagen y el sonido. Al grabar se revisa igual.")

        sec = ui.FormSection(host, "Resultado")
        sec.pack(fill="x")
        self.out_kind = ctk.StringVar(value=OUT_DOC)
        ui.segmented(sec.row("Quiero obtener"), [OUT_DOC, OUT_TEXT], self.out_kind).pack(
            anchor="w")
        self.lang_var = ctk.StringVar(value=LANGUAGES[0][0])
        ui.option_menu(sec.row("Idioma hablado"), _labels(LANGUAGES), self.lang_var,
                       width=240).pack(anchor="w")
        self.name_field = ui.entry(sec.row("Nombre"), "Nombre de la clase")
        self.name_field.pack(fill="x")
        self.out_field = _file_row(sec.row("Guardar en"), "Carpeta", "Cambiar…",
                                   self._pick_output)
        _set_entry(self.out_field, str(Path.home() / "Documents" / "Heimdall"))

        self.adv_toggle = ui.link_button(host, "Mostrar opciones avanzadas",
                                         self._toggle_advanced)
        self.adv_toggle.pack(anchor="w", pady=(0, 12))
        self.advanced = ctk.CTkFrame(host, fg_color="transparent")
        sec = ui.FormSection(self.advanced, "Transcripción")
        sec.pack(fill="x")
        self.model_var = ctk.StringVar(value=MODELS[1][0])
        ui.option_menu(sec.row("Modelo"), _labels(MODELS), self.model_var,
                       width=300).pack(anchor="w")
        sec = ui.FormSection(self.advanced, "Capturas")
        sec.pack(fill="x")
        self.interval_var = ctk.IntVar(value=2)
        ui.SliderRow(sec.row("Revisar cada"), self.interval_var, 1, 10, 9,
                     lambda v: f"{int(v)} s").pack(anchor="w")
        sec.note("Cada cuánto se mira la pantalla. Solo se guarda una captura cuando "
                 "cambia lo que se ve.")
        self.window_var = ctk.IntVar(value=15)
        ui.SliderRow(sec.row("Texto por captura"), self.window_var, 5, 60, 11,
                     lambda v: f"±{int(v)} s").pack(anchor="w")
        self.keep_audio_var = ctk.BooleanVar(value=False)
        ui.checkbox(sec.row("Guardar"), "También el audio grabado (.wav)",
                    self.keep_audio_var).pack(anchor="w")

    def on_show(self) -> None:
        if not self._loaded and not self._unavailable:
            self._loaded = True
            self._refresh_windows()

    # ---- windows and probe -------------------------------------------------------
    def _refresh_windows(self) -> None:
        if self.is_running() or self._unavailable:
            return
        try:
            wins = [w for w in recorder.list_windows() if w.title != APP_NAME]
        except recorder.CaptureError as exc:
            self.fail(str(exc))
            self.log(str(exc))
            return
        self._windows = wins
        current = self.win_var.get()
        labels = [w.label for w in wins] or ["No hay ventanas abiertas"]
        self.win_menu.configure(values=labels)
        self.win_var.set(current if current in labels else labels[0])
        self._clear_verdict()
        self.set_state("idle", f"{len(wins)} ventanas disponibles. Elige la de la clase."
                       if wins else "Abre la clase en el navegador y pulsa Actualizar.")

    def _window(self) -> Optional[recorder.Window]:
        return next((w for w in self._windows if w.label == self.win_var.get()), None)

    def _clear_verdict(self) -> None:
        self.verdict_label.configure(text="")

    def _show_verdict(self, v: recorder.Verdict) -> None:
        self.verdict_label.configure(text=v.title, text_color=VERDICT_COLOR[v.kind])
        self.verdict_note.configure(text=v.detail)

    def _start_probe(self) -> None:
        win = self._window()
        if not win:
            self.fail("Elige primero la ventana de la clase.")
            return
        self.log("─" * 60)
        self.log(f"Probando la ventana «{win.label}»…")
        self.verdict_label.configure(text="Probando…", text_color=ui.TEXT_MUTED)

        def job() -> str:
            work = Path(tempfile.mkdtemp(prefix="heimdall_probe_"))
            v = recorder.probe(win, work, log=self.qlog, cancel_flag=self.cancelled)
            self._probe_verdict = v
            self.qlog(f"Resultado: {v.title}. {v.detail}")
            return "probe"

        self.run_job(job, (recorder.CancelledError,), determinate=False,
                     message="Probando… deja la clase reproduciéndose.")

    # ---- recording ---------------------------------------------------------------
    def set_state(self, state: str, message: str = "") -> None:
        if state in ("cancelled", "error"):
            self._rec = None  # the recording ended; reset the buttons
            if hasattr(self, "stop_btn"):
                self.stop_btn.configure(state="normal")
        super().set_state(state, message)
        if not hasattr(self, "stop_btn"):
            return  # first call, from JobPage.__init__
        self.stop_btn.pack_forget()
        recording = state == "running" and self._rec is not None
        if recording:
            self.cancel_btn.configure(text="Descartar")
            self.stop_btn.pack(side="right", padx=(8, 0))
            self.cancel_btn.pack_forget()
            self.cancel_btn.pack(side="right")
        else:
            self.cancel_btn.configure(text="Cancelar")
        self.probe_btn.configure(state="disabled" if state == "running" else "normal")

    def _pick_output(self) -> None:
        current = self.out_field.get().strip()
        path = filedialog.askdirectory(title="Carpeta para los documentos",
                                       initialdir=current or None)
        if path:
            _set_entry(self.out_field, path)

    def start(self) -> None:
        win = self._window()
        if not win:
            self.fail("Elige primero la ventana de la clase.")
            return
        out_dir = Path(self.out_field.get().strip() or Path.home() / "Documents" / "Heimdall")
        name = self.name_field.get().strip() or datetime.now().strftime("clase_%Y-%m-%d_%H%M")
        name = "".join(c for c in name if c not in '/\\:*?"<>|').strip() or "clase"
        _set_entry(self.name_field, name)
        lang = dict(LANGUAGES)[self.lang_var.get()]
        model = dict(MODELS)[self.model_var.get()]
        device = "auto" if self._compute.has_cuda else "cpu"
        want_doc = self.out_kind.get() == OUT_DOC
        work = Path(tempfile.mkdtemp(prefix="heimdall_rec_"))
        self._rec = recorder.Recording(win, work, interval_s=float(self.interval_var.get()),
                                       log=self.qlog)
        self._stop.clear()
        self._warned = False
        self.log("─" * 60)
        self.log(f"Clase: {name}\nVentana: {win.label}\nCarpeta: {out_dir}")

        def job() -> str:
            rec = self._rec
            assert rec is not None
            try:
                rec.start()
                while not self._stop.is_set() and rec.alive:
                    if self.cancelled():
                        raise recorder.CancelledError()
                    if not self._warned and rec.elapsed >= recorder.PROBE_SECONDS:
                        self._warned = True
                        v = rec.verdict(first_seconds=recorder.PROBE_SECONDS)
                        self.qlog(f"Revisión de los primeros segundos: {v.title}. {v.detail}")
                        self._early_verdict = v
                        if v.kind == "blocked":
                            rec.stop()
                            raise recorder.CaptureError(
                                "Esta clase no se puede grabar: la imagen sale negra y no "
                                "llega sonido. Si estaba en pausa, dale play y vuelve a "
                                "grabar; si no, usa la transcripción de la plataforma.")
                    time.sleep(0.2)
                rec.stop()
                if self.cancelled():
                    raise recorder.CancelledError()
                if rec.error and not rec.frames:
                    raise recorder.CaptureError(rec.error)
                secs = (rec.stopped or {}).get("audio_seconds", 0)
                self.qlog(f"Grabación terminada: {fmt_duration(secs)}.")
                self.qprogress(0)
                self._phase = "build"
                res = recorder.build_from_recording(
                    rec, out_dir, name, model_size=model, language=lang, device=device,
                    want_doc=want_doc, window_s=float(self.window_var.get()),
                    log=self.qlog, cancel_flag=self.cancelled, progress=self.qprogress)
                if self.keep_audio_var.get():
                    shutil.copy2(rec.out_dir / "audio.wav", out_dir / f"{name}.wav")
                return str(res.get("pdf") or res["txt"])
            finally:
                rec.stop()
                shutil.rmtree(work, ignore_errors=True)

        self._phase = "record"
        self._early_verdict = None
        self.run_job(job, (recorder.CancelledError,), determinate=True,
                     message="Empezando a grabar…")
        self._live()

    def _live(self) -> None:
        """While recording, show time, sound and the early protection check."""
        rec = self._rec
        if not self.is_running() or rec is None:
            return
        if self._phase == "record":
            if rec.started_at is None:
                msg = "Empezando a grabar…"
            else:
                sound = "se oye sonido" if rec.current_level() >= recorder.SOUND_RMS \
                    else "sin sonido"
                msg = f"Grabando · {sound}"
                v = self._early_verdict
                if v is not None and v.kind != "full":
                    msg += f" · {v.title}"
                    self._show_verdict(v)
                elif v is not None:
                    self._show_verdict(v)
            self.status.configure(text=msg)
        elif self._phase == "build":
            if self.stop_btn.winfo_manager():
                self.stop_btn.pack_forget()
                self.cancel_btn.configure(text="Cancelar")
            self.status.configure(text="Creando el documento… puede tardar varios minutos.")
        self.after(500, self._live)

    def _request_stop(self) -> None:
        self._stop.set()
        self.stop_btn.configure(state="disabled")
        self.status.configure(text="Deteniendo…")

    def on_done(self, payload: str) -> None:
        if payload == "probe":
            v = self._probe_verdict
            self._show_verdict(v)
            self.set_state("idle", "Prueba terminada. " + v.title + ".")
            return
        self.stop_btn.configure(state="normal")
        self._rec = None
        self._result = Path(payload)
        kind = "PDF" if self._result.suffix == ".pdf" else "texto"
        self._open_btn.configure(text=f"Abrir {kind}")
        self.set_state("done", f"Listo: {self._result.name}")

    def _open_result(self) -> None:
        if self._result and self._result.exists():
            _open_path(self._result)

    def _show_folder(self) -> None:
        if self._result:
            _reveal(self._result)


# ============================================================================
# Convertir vídeo
# ============================================================================
class ConvertPage(_AdvancedMixin, ui.JobPage):
    title = "Convertir vídeo"
    subtitle = ("Pasa un vídeo a MP4 para que se reproduzca en cualquier equipo, "
                "o saca solo el audio en MP3.")
    action_text = "Convertir"

    def __init__(self, master, support: EncoderSupport) -> None:
        self._support = support
        self._result: Optional[Path] = None
        super().__init__(master)
        self.add_result_button("Abrir archivo", self._open_result)
        self.add_result_button("Mostrar carpeta", self._show_folder)

    def build_form(self, host) -> None:
        sec = ui.FormSection(host, "Archivo")
        sec.pack(fill="x")
        self.in_field = _file_row(sec.row("Original"), "Vídeo o audio (mkv, avi, mov, mp4…)",
                                  "Elegir…", self._pick_input)
        self.out_field = _file_row(sec.row("Guardar como"), "Se sugiere al elegir el archivo",
                                   "Cambiar…", self._pick_output)

        sec = ui.FormSection(host, "Formato")
        sec.pack(fill="x")
        self.kind_var = ctk.StringVar(value=C_KIND_VIDEO)
        ui.segmented(sec.row("Convertir a"), [C_KIND_VIDEO, C_KIND_AUDIO], self.kind_var,
                     command=self._on_kind).pack(anchor="w")
        self.quality_var = ctk.StringVar(value="Media")
        ui.segmented(sec.row("Calidad"), list(C_QUALITY), self.quality_var).pack(anchor="w")
        sec.note("Alta conserva más detalle y ocupa más espacio.")

        self.adv_toggle = ui.link_button(host, "Mostrar opciones avanzadas",
                                         self._toggle_advanced)
        self.adv_toggle.pack(anchor="w", pady=(0, 12))
        self.advanced = ctk.CTkFrame(host, fg_color="transparent")
        sec = ui.FormSection(self.advanced, "Codificación")
        sec.pack(fill="x")
        self.codec_var = ctk.StringVar(value=C_CODECS[0][0])
        self.codec_menu = ui.option_menu(sec.row("Códec de vídeo"), _labels(C_CODECS),
                                         self.codec_var, width=300)
        self.codec_menu.pack(anchor="w")
        hw = [C_HW[0], C_HW[1]] + [o for o in C_HW[2:] if o[1] in self._support.available_keys]
        self.hw_var = ctk.StringVar(value=hw[0][0])
        ui.option_menu(sec.row("Aceleración"), _labels(hw), self.hw_var,
                       width=300).pack(anchor="w")
        found = [HW_ACCEL[k][0] for k in self._support.available_keys if k != "cpu"]
        sec.note("Aceleración por hardware disponible: " + ", ".join(found) if found
                 else "No se detectó aceleración por hardware: se usará el procesador.")

    def _ext(self) -> str:
        return ".mp3" if self.kind_var.get() == C_KIND_AUDIO else ".mp4"

    def _on_kind(self, *_args) -> None:
        self.codec_menu.configure(state="disabled" if self._ext() == ".mp3" else "normal")
        current = self.out_field.get().strip()
        if current:
            _set_entry(self.out_field, str(Path(current).with_suffix(self._ext())))

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Elige el archivo",
            filetypes=[("Vídeo o audio", f"{VIDEO_EXTS} {AUDIO_EXTS}"), ("Todos", "*.*")])
        if path:
            self.load_input(Path(path))

    def load_input(self, p: Path) -> None:
        _set_entry(self.in_field, str(p))
        if p.suffix.lower() in AUDIO_EXTS.split():
            self.kind_var.set(C_KIND_AUDIO)
        self._on_kind()
        target = p.with_suffix(self._ext())
        if target == p:
            target = p.with_name(f"{p.stem}_convertido{self._ext()}")
        _set_entry(self.out_field, str(target))
        self.set_state("idle", "Listo para convertir.")

    def _pick_output(self) -> None:
        ext = self._ext()
        current = self.out_field.get().strip() or f"salida{ext}"
        path = filedialog.asksaveasfilename(
            title="Guardar como", defaultextension=ext,
            initialfile=Path(current).name, initialdir=str(Path(current).parent),
            filetypes=[(ext[1:].upper(), f"*{ext}")])
        if path:
            _set_entry(self.out_field, path)

    def start(self) -> None:
        raw = self.in_field.get().strip()
        src = Path(raw)
        if not raw or not src.is_file():
            self.fail("Elige primero el archivo a convertir.")
            return
        if not self._support.has_ffmpeg:
            self.fail("No se encontró ffmpeg. Instálalo y vuelve a abrir la app.")
            return
        ext = self._ext()
        out_text = self.out_field.get().strip()
        out = Path(out_text).with_suffix(ext) if out_text else src.with_suffix(ext)
        if out.resolve() == src.resolve():
            self.fail("El archivo de salida no puede ser el mismo que el original.")
            return
        kind, codec = ext[1:], dict(C_CODECS)[self.codec_var.get()]
        hw, quality = dict(C_HW)[self.hw_var.get()], C_QUALITY[self.quality_var.get()]
        self.log("─" * 60)
        self.log(f"Original: {src}\nSalida: {out}")

        def job() -> str:
            convert(src, out, output_kind=kind, codec=codec, hw_accel=hw,
                    quality=quality, support=self._support, log=self.qlog,
                    cancel_flag=self.cancelled, progress=self.qprogress)
            return str(out)

        self.run_job(job, (ConvertCancelled,), determinate=True, message="Convirtiendo…")

    def on_done(self, payload: str) -> None:
        self._result = Path(payload)
        self.set_state("done", f"Listo: {self._result.name}")

    def _open_result(self) -> None:
        if self._result and self._result.exists():
            _open_path(self._result)

    def _show_folder(self) -> None:
        if self._result:
            _reveal(self._result)


# ============================================================================
# App shell
# ============================================================================
class Sidebar(ctk.CTkFrame):
    def __init__(self, master, on_select) -> None:
        super().__init__(master, fg_color=ui.SIDEBAR_BG, corner_radius=0, width=232)
        self.pack_propagate(False)
        self._on_select = on_select
        self._buttons: dict[str, ctk.CTkButton] = {}

        brand = ctk.CTkFrame(self, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(20, 0))
        logo = BRANDING / "heimdall.png"
        if logo.exists():
            from PIL import Image
            self._logo = ctk.CTkImage(Image.open(logo), size=(40, 40))
            ctk.CTkLabel(brand, image=self._logo, text="").pack(side="left",
                                                                padx=(0, 10))
        ctk.CTkLabel(brand, text=APP_NAME, font=ui.font(20, "bold"), text_color=ui.TEXT,
                     anchor="w").pack(side="left")
        ctk.CTkLabel(self, text="Todo se procesa en tu equipo", font=ui.font(12),
                     text_color=ui.TEXT_MUTED, anchor="w").pack(fill="x", padx=20,
                                                                 pady=(10, 22))

        # Hardware details live in each page's advanced options, where they
        # explain a choice. Here only the appearance switch and the version.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=18)
        self.theme_var = ctk.StringVar(value="Sistema")
        ui.segmented(footer, ["Sistema", "Claro", "Oscuro"], self.theme_var,
                     command=self._set_theme).pack(fill="x")
        ctk.CTkLabel(footer, text=f"Versión {APP_VERSION}", font=ui.font(11),
                     text_color=ui.TEXT_MUTED, anchor="w").pack(fill="x", pady=(10, 0))

    def add(self, key: str, text: str) -> None:
        btn = ctk.CTkButton(
            self, text=text, anchor="w", height=36, corner_radius=ui.RADIUS,
            font=ui.font(14), fg_color="transparent", hover_color=ui.NEUTRAL_BTN,
            text_color=ui.TEXT, command=lambda: self._on_select(key))
        btn.pack(fill="x", padx=10, pady=1)
        self._buttons[key] = btn

    def select(self, key: str) -> None:
        for k, b in self._buttons.items():
            active = k == key
            b.configure(fg_color=ui.SELECTED if active else "transparent",
                        hover_color=ui.SELECTED_HOVER if active else ui.NEUTRAL_BTN,
                        text_color=ui.TEXT if active else ui.TEXT_MUTED,
                        font=ui.font(14, "bold" if active else "normal"))

    @staticmethod
    def _set_theme(choice: str) -> None:
        ctk.set_appearance_mode({"Sistema": "system", "Claro": "light",
                                 "Oscuro": "dark"}[choice])


class App(ctk.CTk):
    def __init__(self) -> None:
        ctk.set_appearance_mode("system")
        super().__init__(fg_color=ui.CONTENT_BG)
        self.title(APP_NAME)
        self._set_icon()
        self.geometry("1040x760")
        self.minsize(860, 620)

        support = detect_encoders()
        compute = detect_compute()
        if compute.reason:
            print(f"[i] CUDA no disponible: {compute.reason}", flush=True)
        ffmpeg_ok, _ = check_ffmpeg()

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.sidebar = Sidebar(self, self.show)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        ctk.CTkFrame(self, width=1, fg_color=ui.BORDER, corner_radius=0).grid(
            row=0, column=0, sticky="nse")

        self.pages: dict[str, ui.JobPage] = {
            "transcribe": TranscribePage(self, compute),
            "record": RecordPage(self, compute),
            "convert": ConvertPage(self, support),
        }
        self.sidebar.add("transcribe", "Transcribir reunión")
        self.sidebar.add("record", "Grabar clase")
        self.sidebar.add("convert", "Convertir vídeo")
        self.show("transcribe")
        if not ffmpeg_ok:
            self.pages["transcribe"].fail(
                "No se encontró ffmpeg. Instálalo (ver README) y vuelve a abrir la app.")

    def _set_icon(self) -> None:
        """Window/taskbar icon (Windows uses the .ico, the rest the .png)."""
        ico, png = BRANDING / "heimdall.ico", BRANDING / "heimdall.png"
        if sys.platform == "win32" and ico.exists():
            self.iconbitmap(str(ico))
        elif png.exists():
            from tkinter import PhotoImage
            self._icon = PhotoImage(file=str(png))  # keep a reference
            self.iconphoto(True, self._icon)  # on macOS this is the Dock icon

    def show(self, key: str) -> None:
        for k, page in self.pages.items():
            if k == key:
                page.grid(row=0, column=1, sticky="nsew")
                if hasattr(page, "on_show"):
                    page.after(50, page.on_show)
            else:
                page.grid_forget()
        self.sidebar.select(key)


def main() -> None:
    try:
        App().mainloop()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"\n[error fatal al iniciar] {type(exc).__name__}: {exc}\n")
        if sys.platform == "win32":
            try:
                input("\nPulsa Enter para cerrar esta ventana...")
            except EOFError:
                pass
        raise


if __name__ == "__main__":
    main()
