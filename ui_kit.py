"""Shared UI building blocks: palette, fonts, form sections and the job page.

The look follows desktop media tools (HandBrake, OBS, Audacity): a neutral
sidebar, grouped forms with labels on the left, one accent colour reserved
for the primary action and the current selection, and a fixed action bar at
the bottom that always shows what the app is doing.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from typing import Callable, Optional

import customtkinter as ctk

# ---- Palette (light, dark) --------------------------------------------------
# One warm accent (terracotta) for the primary action, progress and links.
# Selection is neutral, like macOS: a raised surface plus weight, never a
# tinted fill. All text pairs measured >= 4.5:1 in both appearances.
SIDEBAR_BG = ("#E9EAED", "#1B1C1F")
CONTENT_BG = ("#F6F6F8", "#222326")
FIELD_BG = ("#FFFFFF", "#2C2D31")
BORDER = ("#D5D7DC", "#3A3C41")
SEPARATOR = ("#E1E2E6", "#303236")
TEXT = ("#1D1E21", "#E8E9EC")
TEXT_MUTED = ("#5D6068", "#A2A5AD")
ACCENT = ("#A8461F", "#EC7046")           # 5.9:1 under white / 6.2:1 under ON_ACCENT
ACCENT_HOVER = ("#8F3B19", "#F08A62")
ON_ACCENT = ("#FFFFFF", "#1A0F0A")         # text on an ACCENT fill
LINK = ("#9A3F1A", "#F08A62")              # 6.3:1 / 6.4:1 on CONTENT_BG
LINK_HOVER = ("#7A3013", "#F5A383")
SELECTED = ("#FFFFFF", "#34363B")          # selected nav item / segment
SELECTED_HOVER = ("#FFFFFF", "#3A3C41")
# Segment selected on a NEUTRAL_BTN track: in dark it must be lighter than
# the track or the selection disappears.
SEGMENT_SELECTED = ("#FFFFFF", "#55575E")
NEUTRAL_BTN = ("#E3E4E8", "#34363B")
NEUTRAL_BTN_HOVER = ("#D6D8DD", "#3E4046")
DANGER = ("#B9372E", "#D9534A")
DANGER_HOVER = ("#9E2E26", "#C2443C")
SUCCESS = ("#1A6E3F", "#4CB878")
ERROR_TEXT = ("#A8322A", "#EE7B72")

RADIUS = 6

if sys.platform == "darwin":
    MONO_FAMILY = "Menlo"
elif sys.platform == "win32":
    MONO_FAMILY = "Consolas"
else:
    MONO_FAMILY = "DejaVu Sans Mono"


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight=weight)


def mono(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=MONO_FAMILY, size=size)


def fmt_clock(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---- Buttons ------------------------------------------------------------------
def primary_button(parent, text: str, command, width: int = 150) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=34,
        corner_radius=RADIUS, fg_color=ACCENT, hover_color=ACCENT_HOVER,
        text_color=ON_ACCENT, font=font(13, "bold"),
    )


def secondary_button(parent, text: str, command, width: int = 120) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=30,
        corner_radius=RADIUS, fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
        text_color=TEXT, font=font(13),
    )


def danger_button(parent, text: str, command, width: int = 120) -> ctk.CTkButton:
    return ctk.CTkButton(
        parent, text=text, command=command, width=width, height=34,
        corner_radius=RADIUS, fg_color=DANGER, hover_color=DANGER_HOVER,
        text_color=("#FFFFFF", "#FFFFFF"), font=font(13, "bold"),
    )


class LinkLabel(ctk.CTkLabel):
    """Text link for secondary toggles (opciones avanzadas, registro).

    A label rather than a button so its text starts exactly on the same
    vertical line as the form labels (buttons carry inner padding).
    """

    def __init__(self, parent, text: str, command) -> None:
        super().__init__(parent, text=text, font=font(13), text_color=LINK,
                         anchor="w", cursor="hand2")
        self._command = command
        self.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Enter>", lambda _e: self.configure(text_color=LINK_HOVER))
        self.bind("<Leave>", lambda _e: self.configure(text_color=LINK))


def link_button(parent, text: str, command) -> LinkLabel:
    return LinkLabel(parent, text, command)


def option_menu(parent, values: list[str], variable, width: int = 220,
                command=None) -> ctk.CTkOptionMenu:
    return ctk.CTkOptionMenu(
        parent, values=values, variable=variable, width=width, height=30,
        corner_radius=RADIUS, fg_color=FIELD_BG, button_color=NEUTRAL_BTN,
        button_hover_color=NEUTRAL_BTN_HOVER, text_color=TEXT,
        dropdown_font=font(13), font=font(13), command=command,
    )


def segmented(parent, values: list[str], variable, command=None) -> ctk.CTkSegmentedButton:
    return ctk.CTkSegmentedButton(
        parent, values=values, variable=variable, command=command, height=30,
        corner_radius=RADIUS, font=font(13), fg_color=NEUTRAL_BTN,
        # customtkinter uses one text colour for every segment, so the
        # selected one is a raised neutral surface (macOS segmented control).
        selected_color=SEGMENT_SELECTED, selected_hover_color=SEGMENT_SELECTED,
        unselected_color=NEUTRAL_BTN, unselected_hover_color=NEUTRAL_BTN_HOVER,
        text_color=TEXT,
    )


def entry(parent, placeholder: str = "") -> ctk.CTkEntry:
    return ctk.CTkEntry(
        parent, height=30, corner_radius=RADIUS, fg_color=FIELD_BG,
        border_color=BORDER, border_width=1, text_color=TEXT,
        placeholder_text=placeholder, font=font(13),
    )


def checkbox(parent, text: str, variable, command=None) -> ctk.CTkCheckBox:
    return ctk.CTkCheckBox(
        parent, text=text, variable=variable, command=command, font=font(13),
        corner_radius=4, border_width=1, border_color=BORDER,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, checkmark_color=ON_ACCENT,
        text_color=TEXT, checkbox_width=18, checkbox_height=18,
    )


# ---- Form section ---------------------------------------------------------------
class FormSection(ctk.CTkFrame):
    """A titled group of `label | control` rows, separated by a hairline.

    Labels sit in a fixed-width left column so every control in the page
    starts on the same vertical line, like HandBrake's settings panes.
    """

    LABEL_WIDTH = 150

    def __init__(self, master, title: str, hint: str = "") -> None:
        super().__init__(master, fg_color="transparent")
        ctk.CTkFrame(self, height=1, fg_color=SEPARATOR).pack(fill="x", pady=(0, 14))
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=title, font=font(14, "bold"), text_color=TEXT,
                     anchor="w").pack(side="left")
        if hint:
            ctk.CTkLabel(head, text=hint, font=font(12), text_color=TEXT_MUTED,
                         anchor="w").pack(side="left", padx=(10, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x", pady=(10, 18))
        self.body.grid_columnconfigure(0, minsize=self.LABEL_WIDTH)
        self.body.grid_columnconfigure(1, weight=1)
        self._row = 0

    def row(self, label: str) -> ctk.CTkFrame:
        """Add a row and return the frame where its controls go."""
        ctk.CTkLabel(self.body, text=label, font=font(13), text_color=TEXT_MUTED,
                     anchor="w").grid(row=self._row, column=0, sticky="nw",
                                      pady=(5, 5))
        cell = ctk.CTkFrame(self.body, fg_color="transparent")
        cell.grid(row=self._row, column=1, sticky="ew", pady=4)
        self._row += 1
        return cell

    def note(self, text: str) -> "Note":
        """A muted explanatory line under the last row, aligned with controls.

        Empty notes take no space, so a note that is filled in later (file
        info, warnings) does not leave a gap while it is blank.
        """
        lbl = Note(self.body, text)
        lbl.grid(row=self._row, column=1, sticky="w", pady=(0, 4))
        if not text:
            lbl.grid_remove()
        self._row += 1
        return lbl


class Note(ctk.CTkLabel):
    def __init__(self, master, text: str) -> None:
        super().__init__(master, text=text, font=font(12), text_color=TEXT_MUTED,
                         anchor="w", justify="left", wraplength=520)

    def configure(self, require_redraw=False, **kwargs):
        super().configure(require_redraw, **kwargs)
        if "text" in kwargs:
            # grid()/grid_remove() keep the original grid options.
            self.grid() if kwargs["text"] else self.grid_remove()


class SliderRow(ctk.CTkFrame):
    """Slider with its current value shown to the right."""

    def __init__(self, master, variable, from_: float, to: float, steps: int,
                 fmt: Callable[[float], str]) -> None:
        super().__init__(master, fg_color="transparent")
        self._var = variable
        self._fmt = fmt
        self.slider = ctk.CTkSlider(
            self, from_=from_, to=to, number_of_steps=steps, variable=variable,
            width=260, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            progress_color=ACCENT, fg_color=NEUTRAL_BTN, command=self._update,
        )
        self.slider.pack(side="left")
        self.value = ctk.CTkLabel(self, text="", font=mono(12), text_color=TEXT,
                                  width=60, anchor="w")
        self.value.pack(side="left", padx=(12, 0))
        self._update()

    def _update(self, *_args) -> None:
        self.value.configure(text=self._fmt(self._var.get()))

    def set_enabled(self, enabled: bool) -> None:
        self.slider.configure(state="normal" if enabled else "disabled")


# ---- Job page -------------------------------------------------------------------
class JobPage(ctk.CTkFrame):
    """Base for a page that runs one long job in a worker thread.

    Layout: a scrollable form on top, an optional log panel, and a fixed
    action bar at the bottom with status, elapsed time, progress and the
    primary action. Subclasses build the form in ``build_form`` and start
    work with ``run_job``.
    """

    title = ""
    subtitle = ""
    action_text = "Iniciar"
    running_text = "Trabajando…"

    def __init__(self, master) -> None:
        super().__init__(master, fg_color=CONTENT_BG, corner_radius=0)
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        self._log_open = False
        self._determinate = True

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.scroll = ctk.CTkScrollableFrame(self, fg_color=CONTENT_BG, corner_radius=0)
        self.scroll.grid(row=0, column=0, sticky="nsew")
        self.form = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.form.pack(fill="x", padx=32, pady=(26, 10))

        ctk.CTkLabel(self.form, text=self.title, font=font(22, "bold"),
                     text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(self.form, text=self.subtitle, font=font(13),
                     text_color=TEXT_MUTED, anchor="w", justify="left",
                     wraplength=640).pack(fill="x", pady=(4, 20))
        self.build_form(self.form)

        self._build_log()
        self._build_action_bar()
        self.set_state("idle")
        self.after(100, self._poll)

    # -- to override -------------------------------------------------------
    def build_form(self, host) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_done(self, payload: str) -> None:
        """Called on the UI thread when the job succeeds."""

    # -- log panel -----------------------------------------------------------
    def _build_log(self) -> None:
        self.log_panel = ctk.CTkFrame(self, fg_color=SIDEBAR_BG, corner_radius=0)
        head = ctk.CTkFrame(self.log_panel, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(8, 0))
        ctk.CTkLabel(head, text="Registro técnico", font=font(12, "bold"),
                     text_color=TEXT_MUTED).pack(side="left")
        secondary_button(head, "Copiar", self._copy_log, width=80).pack(side="right")
        secondary_button(head, "Vaciar", self._clear_log, width=80).pack(
            side="right", padx=(0, 6))
        self.log_box = ctk.CTkTextbox(
            self.log_panel, height=190, font=mono(12), fg_color=FIELD_BG,
            text_color=TEXT, border_color=BORDER, border_width=1,
            corner_radius=RADIUS, state="disabled", wrap="none",
        )
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(8, 12))

    def toggle_log(self, show: Optional[bool] = None) -> None:
        self._log_open = (not self._log_open) if show is None else show
        if self._log_open:
            self.log_panel.grid(row=1, column=0, sticky="nsew")
        else:
            self.log_panel.grid_forget()
        self.log_toggle.configure(
            text="Ocultar registro" if self._log_open else "Ver registro")

    def log(self, msg: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _copy_log(self) -> None:
        content = self.log_box.get("1.0", "end").strip()
        if not content:
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update()  # keep the clipboard after the window loses focus
        self.status.configure(text="Registro copiado al portapapeles.")

    # -- action bar --------------------------------------------------------------
    def _build_action_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=SIDEBAR_BG, corner_radius=0, height=86)
        bar.grid(row=2, column=0, sticky="ew")
        ctk.CTkFrame(bar, height=1, fg_color=BORDER).pack(fill="x", side="top")
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill="x", padx=24, pady=14)
        inner.grid_columnconfigure(0, weight=1)

        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.grid(row=0, column=0, sticky="ew", padx=(0, 20))
        top = ctk.CTkFrame(info, fg_color="transparent")
        top.pack(fill="x")
        self.status = ctk.CTkLabel(top, text="", font=font(13), text_color=TEXT,
                                   anchor="w")
        self.status.pack(side="left")
        self.clock = ctk.CTkLabel(top, text="", font=mono(12), text_color=TEXT_MUTED)
        self.clock.pack(side="right")
        self.progress = ctk.CTkProgressBar(info, height=6, corner_radius=3,
                                           progress_color=ACCENT, fg_color=NEUTRAL_BTN)
        self.progress.pack(fill="x", pady=(8, 4))
        self.progress.set(0)
        self.log_toggle = link_button(info, "Ver registro", self.toggle_log)
        self.log_toggle.pack(anchor="w")

        self.buttons = ctk.CTkFrame(inner, fg_color="transparent")
        self.buttons.grid(row=0, column=1, sticky="e")
        self.result_btns = ctk.CTkFrame(self.buttons, fg_color="transparent")
        self.start_btn = primary_button(self.buttons, self.action_text, self.start)
        # Cancelling a job loses nothing, so it is a neutral action: a red
        # button beside the terracotta progress bar read as an alarm.
        self.cancel_btn = secondary_button(self.buttons, "Cancelar",
                                           self._request_cancel, width=150)
        self.cancel_btn.configure(height=34)

    def add_result_button(self, text: str, command) -> ctk.CTkButton:
        btn = secondary_button(self.result_btns, text, command, width=130)
        btn.pack(side="left", padx=(0, 8))
        return btn

    # -- state -------------------------------------------------------------------
    def set_state(self, state: str, message: str = "") -> None:
        """idle | running | done | cancelled | error."""
        for w in (self.start_btn, self.cancel_btn, self.result_btns):
            w.pack_forget()
        self.progress.stop()
        self.status.configure(text_color=TEXT)
        if state in ("running", "done"):
            self.progress.pack(fill="x", pady=(8, 4), before=self.log_toggle)
        else:
            self.progress.pack_forget()
        if state == "running":
            self._started_at = time.monotonic()
            self.cancel_btn.pack(side="right")
            self.status.configure(text=message or self.running_text)
            if self._determinate:
                self.progress.configure(mode="determinate")
                self.progress.set(0)
            else:
                self.progress.configure(mode="indeterminate")
                self.progress.start()
            self._tick()
            return
        self._started_at = None
        self.progress.configure(mode="determinate")
        self.start_btn.pack(side="right")
        if state == "idle":
            self.progress.set(0)
            self.clock.configure(text="")
            self.status.configure(text=message or "Elige un archivo para empezar.",
                                  text_color=TEXT_MUTED)
        elif state == "done":
            self.progress.set(1)
            self.result_btns.pack(side="right", padx=(0, 10))
            self.status.configure(text=message or "Listo.", text_color=SUCCESS)
        elif state == "cancelled":
            self.progress.set(0)
            self.status.configure(text="Cancelado.", text_color=TEXT_MUTED)
        elif state == "error":
            self.progress.set(0)
            self.status.configure(text=message or "No se pudo completar.",
                                  text_color=ERROR_TEXT)
            self.toggle_log(True)

    def fail(self, message: str) -> None:
        """Show a validation problem without starting a job."""
        self.status.configure(text=message, text_color=ERROR_TEXT)

    def _tick(self) -> None:
        if self._started_at is None:
            return
        self.clock.configure(text=fmt_clock(time.monotonic() - self._started_at))
        self.after(1000, self._tick)

    # -- worker plumbing ---------------------------------------------------------
    def qlog(self, msg: str) -> None:
        self._queue.put(("log", msg))

    def qprogress(self, pct: float) -> None:
        self._queue.put(("progress", f"{pct:.4f}"))

    @property
    def cancelled(self) -> Callable[[], bool]:
        return self._cancel.is_set

    def run_job(self, fn: Callable[[], str], cancelled_exc: tuple[type, ...],
                determinate: bool, message: str = "") -> None:
        """Run ``fn`` in a worker thread. ``fn`` returns the done payload."""
        self._cancel.clear()
        self._determinate = determinate
        self.set_state("running", message)

        def work() -> None:
            try:
                self._queue.put(("done", fn()))
            except cancelled_exc:
                self._queue.put(("cancelled", ""))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._queue.put(("log", traceback.format_exc()))
                self._queue.put(("error", f"{type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def is_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def _request_cancel(self) -> None:
        if self.is_running():
            self._cancel.set()
            self.status.configure(text="Cancelando… termina el paso en curso.")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    self.progress.set(float(payload))
                elif kind == "done":
                    self.on_done(payload)
                elif kind == "cancelled":
                    self.set_state("cancelled")
                elif kind == "error":
                    first = payload.splitlines()[0] if payload else ""
                    self.log("Error: " + payload)
                    self.set_state("error", "No se pudo completar. " + _short(first))
        except queue.Empty:
            pass
        self.after(100, self._poll)


def _short(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
