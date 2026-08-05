"""Home tab: welcome screen with cards for the modules."""
from __future__ import annotations

from typing import Callable

import customtkinter as ctk


class HomeTab(ctk.CTkFrame):
    """Welcome screen with three clickable module cards."""

    def __init__(
        self,
        master: ctk.CTk,
        on_open_transcriber: Callable[[], None],
        on_open_converter: Callable[[], None],
        on_open_enriched: Callable[[], None],
        ffmpeg_status: str,
        encoder_status: str,
    ) -> None:
        super().__init__(master, fg_color="transparent")

        # Title block
        ctk.CTkLabel(
            self, text="MediaForge",
            font=ctk.CTkFont(size=32, weight="bold"),
        ).pack(pady=(30, 4))
        ctk.CTkLabel(
            self,
            text="Tu centro multimedia local · sin nube, sin marcas de agua",
            text_color="gray",
        ).pack(pady=(0, 24))

        # Status bar
        status = ctk.CTkFrame(self)
        status.pack(fill="x", padx=40, pady=(0, 24))
        ctk.CTkLabel(status, text=ffmpeg_status, anchor="w").pack(
            fill="x", padx=14, pady=(8, 0))
        ctk.CTkLabel(status, text=encoder_status, anchor="w",
                     text_color="gray").pack(
            fill="x", padx=14, pady=(0, 8))

        # Three module cards
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.pack(fill="both", expand=True, padx=40, pady=(0, 30))
        for c in (0, 1, 2):
            cards.grid_columnconfigure(c, weight=1, uniform="cols")
        cards.grid_rowconfigure(0, weight=1)

        modules = [
            ("🎙️", "Transcriptor",
             "Convierte la voz de cualquier vídeo o audio en un .txt editable. "
             "Soporta español, inglés y más.",
             on_open_transcriber),
            ("🔄", "Conversor",
             "Convierte vídeos entre formatos populares (mkv, avi, webm, flv…) "
             "a MP4 o extrae el audio a MP3, con aceleración por GPU si está "
             "disponible.",
             on_open_converter),
            ("📑", "Transcripción enriquecida",
             "Genera un PDF navegable con capturas de pantalla de los momentos "
             "clave de un vídeo, cada una con la transcripción correspondiente. "
             "Ideal para repasar reuniones. Deduplicación perceptual activada por "
             "defecto: solo frames visualmente distintos.",
             on_open_enriched),
        ]
        for col, (icon, title, desc, cmd) in enumerate(modules):
            card = ctk.CTkFrame(cards)
            card.grid(row=0, column=col, sticky="nsew", padx=8, pady=10)

            ctk.CTkLabel(card, text=icon, font=ctk.CTkFont(size=42)).pack(
                pady=(24, 6))
            ctk.CTkLabel(card, text=title,
                         font=ctk.CTkFont(size=17, weight="bold")).pack(
                pady=(0, 6))
            ctk.CTkLabel(card, text=desc, wraplength=260,
                         justify="center", text_color="gray").pack(
                padx=14, pady=(0, 14))
            ctk.CTkButton(card, text="Abrir " + title, command=cmd,
                          height=34).pack(pady=(0, 24))