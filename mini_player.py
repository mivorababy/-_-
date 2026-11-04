"""Small always-on-top player used while the main window is hidden."""
from __future__ import annotations

import io
import logging
import math
import os
import re
import queue
import threading
import time

_THEME_COLORS = {
    "midnight": ("#151823", "#202538", "#8b95ff", "#f4f5fb", "#aab2c8"),
    "neon": ("#170c24", "#28143a", "#ff3df0", "#f5eaff", "#c3a8df"),
    "sunset": ("#2b1422", "#472033", "#ff8a5c", "#fff1ea", "#d1a595"),
    "ocean": ("#0a2331", "#123447", "#38bdf8", "#e6f6ff", "#9bc3d8"),
    "hearts": ("#381a30", "#50233e", "#ff8fc8", "#fff0f7", "#e3a8c7"),
    "fire": ("#211008", "#35180c", "#ff7a18", "#fff3e8", "#d6a48c"),
    "zxc": ("#1b1a20", "#29272f", "#ff1744", "#f1edf0", "#aaa5ad"),
    "mono": ("#161616", "#242424", "#ffffff", "#f2f2f2", "#aaaaaa"),
    "maretu": ("#09090a", "#222224", "#f1f1f1", "#ffffff", "#a6a6aa"),
    "spotify": ("#121212", "#242424", "#1ed760", "#ffffff", "#b3b3b3"),
    "discord": ("#1e1f22", "#2b2d31", "#5865f2", "#f2f3f5", "#b5bac1"),
    "youtube": ("#0f0f0f", "#242424", "#ff0033", "#ffffff", "#aaaaaa"),
    "soundcloud": ("#160f0a", "#2b1b12", "#ff5500", "#fff7f0", "#c5a996"),
    "blood": ("#100306", "#27070e", "#ff2438", "#fff0f2", "#cc929b"),
}


class FloatingMiniPlayer:
    WIDTH = 550
    HEIGHT = 270
    COMPACT_WIDTH = 344
    COMPACT_HEIGHT = 54
    MARGIN = 18

    def __init__(self, get_settings, get_track, get_cover, control, open_main, save_position, get_catalog=None):
        self.get_settings = get_settings
        self.get_track = get_track
        self.get_cover = get_cover
        self.control = control
        self.open_main = open_main
        self.save_position = save_position
        self.get_catalog = get_catalog or (lambda: {})
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self._root = None
        self._visible = False
        self._mini_style = "standard"
        self._track_id = None
        self._cover_key = None
        self._photo = None
        self._cover_photo_cache = {}
        self._drag = None
        self._seeking = False
        self._volume_dragging = False
        self._seek_position = 0.0
        self._seek_duration = 0.0
        self._volume = 0.8
        self._scale = 1.0
        self._colors = {
            "surface": "#151823", "raised": "#202538", "button": "#252a3d",
            "accent": "#8b95ff", "accent_hover": "#a8afff", "text": "#f4f5fb", "muted": "#aab2c8",
        }
        self._catalog_version = None
        self._catalog_loaded = False
        self._catalog_loading = False
        self._tracks = []
        self._playlists = []
        self._playlist_keys = {}
        self._track_ids = {}
        self._selected_track_id = ""
        self._picker_window = None
        self._picker_canvas = None
        self._picker_width = self.WIDTH
        self._picker_rows = []
        self._picker_drawn = {}
        self._picker_scroll_after = None
        self._picker_thumb_cache = {}
        self._button_icon_cache = {}
        self._mini_theme = "midnight"
        self._live_control_after = {}
        self._control_pending = []
        self._control_lock = threading.Lock()
        self._control_event = threading.Event()
        self._control_stop = threading.Event()
        self._opacity = 92
        self._opacity_panel_visible = False
        self._opacity_window = None
        self._lyrics_window = None
        self._lyrics_label = None
        self.lyrics_button = None
        self._lyrics_round_size = None
        self._lyrics_drag = None
        self._lyrics_last_text = ""
        self._lyrics_last_word = -2
        self.opacity_row = None
        self.opacity_bar = None
        self.opacity_label = None
        self._size_window = None
        self._size_value_label = None
        self._size_scale_widget = None
        self._size_value = 100
        self.shuffle_button = None
        self.repeat_button = None
        self.opacity_button = None
        self._seek_pending_until = 0.0
        self._volume_pending_until = 0.0
        self._selected_playlist = ""
        self._playlist_pending_until = 0.0
        self._track_pending_id = ""
        self._track_pending_until = 0.0
        self._updating_selectors = False
        threading.Thread(target=self._control_worker, name="mini-player-controls", daemon=True).start()
        threading.Thread(target=self._run, name="mini-player-window", daemon=True).start()

    def show(self):
        self._queue.put(("show", None))

    def hide(self):
        self._queue.put(("hide", None))

    def configure(self, settings=None):
        self._queue.put(("configure", settings))

    def close(self):
        self._control_stop.set()
        self._control_event.set()
        self._queue.put(("close", None))

    def _d(self, value):
        return max(1, int(round(value * self._scale)))

    def _window_width(self):
        return self._d(self.COMPACT_WIDTH if self._mini_style == "compact" else self.WIDTH)

    def _window_height(self):
        return self._d(self.COMPACT_HEIGHT if self._mini_style == "compact" else self.HEIGHT)

    def _set_scale(self, value):
        try:
            self._scale = max(0.50, min(5.0, float(value) / 100))
        except (TypeError, ValueError):
            self._scale = 1.0

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import font as tkfont, ttk
            self._tk, self._ttk, self._tkfont = tk, ttk, tkfont
            cfg = self.get_settings() or {}
            mini = cfg.get("mini_player") if isinstance(cfg.get("mini_player"), dict) else cfg
            self._mini_style = "compact" if mini.get("style") == "compact" else "standard"
            self._set_scale(mini.get("scale", 100))
            root = tk.Tk()
            # Tk creates a visible top-level by default on Windows. Withdraw it immediately
            # so startup never flashes the mini-player before the main window is ready.
            root.withdraw()
            root.title("NOT soundLARP 100% — мини-плеер")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            root.configure(bg=self._colors["surface"])
            self._root = root
            self._place(mini)
            self._exclude_from_alt_tab(root)
            self._build_ui()
            self._build_lyrics_window()
            self._apply_theme(cfg)
            root.update_idletasks()
            self._place(mini)
            root.update_idletasks()
            self._exclude_from_alt_tab(root)
            self._round_window()
            self._ready.set()
            root.after(100, self._process_queue)
            root.after(120, self._tick)
            root.mainloop()
        except Exception:
            logging.getLogger("player").exception("Не удалось создать окно мини-плеера")
            self._control_stop.set()
            self._control_event.set()
            self._ready.set()

    def _build_ui(self):
        # Create both control rows once. Packing compact controls before the
        # expanding details column keeps the buttons visible in the narrow pill.
        self._build_expanded_ui()
        self._apply_mini_layout()

    def _apply_mini_layout(self):
        if not getattr(self, "compact_controls", None):
            return
        compact = self._mini_style == "compact"
        if compact:
            self.panel.configure(padx=self._d(7), pady=self._d(5))
            self.body.pack_configure(fill="both", expand=True)
            self.cover.pack_configure(padx=(0, self._d(7)))
            self.cover.configure(font=("Segoe UI Symbol", self._d(15)), width=4, height=2)
            self.title_label.configure(font=("Segoe UI", max(8, self._d(11)), "bold"))
            self.title_label.pack_configure(pady=(self._d(2), 0))
            self.artist_label.configure(font=("Segoe UI", max(7, self._d(9))))
            self.artist_label.pack_configure(pady=0)
            self.selector_row.pack_forget()
            self.seek.pack_forget()
            self.time_label.pack_forget()
            self.controls.pack_forget()
            self.volume_row.pack_forget()
            self.compact_controls.pack(side="right", before=self.details, padx=(self._d(4), 0))
            self.favorite_button = self.compact_favorite_button
            self.play_button = self.compact_play_button
            self.shuffle_button = self.repeat_button = None
        else:
            self.compact_controls.pack_forget()
            self.panel.configure(padx=self._d(12), pady=self._d(10))
            self.body.pack_configure(fill="x", expand=False)
            self.cover.pack_configure(padx=(0, self._d(11)))
            self.cover.configure(font=("Segoe UI Symbol", self._d(27)), width=4, height=2)
            self.title_label.configure(font=("Segoe UI", max(8, self._d(12)), "bold"))
            self.title_label.pack_configure(pady=0)
            self.artist_label.configure(font=("Segoe UI", max(7, self._d(10))))
            self.artist_label.pack_configure(pady=(self._d(1), self._d(5)))
            self.selector_row.pack(fill="x", pady=(self._d(3), 0))
            self.seek.pack(fill="x", pady=(self._d(7), self._d(1)))
            self.time_label.pack(side="left", anchor="w")
            self.controls.pack(fill="x", pady=(self._d(1), self._d(3)))
            self.volume_row.pack(fill="x", pady=(self._d(2), 0))
            self.favorite_button = None
            self.play_button = self.expanded_play_button
            self.shuffle_button = self.expanded_shuffle_button
            self.repeat_button = self.expanded_repeat_button
        try:
            state = self.get_track() or {}
            label = "⏸" if state.get("playing") else "▶"
            for button in (self.compact_play_button, self.expanded_play_button):
                if button["label"] != label:
                    button["label"] = label
                    self._draw_button(button)
            active = bool(state.get("favorite"))
            if self.compact_favorite_button.get("active") != active:
                self.compact_favorite_button["active"] = active
                self._draw_button(self.compact_favorite_button)
        except Exception:
            pass
        if self._track_id:
            state = self.get_track() or {}
            thumbnail = str(state.get("thumbnail") or "") if state.get("remote") else ""
            self._load_cover(self._track_id, thumbnail)

    def _build_compact_ui(self):
        """Compact pill-style mini-player, selectable without replacing the expanded layout."""
        tk, root = self._tk, self._root
        self.buttons = []
        self._opacity_panel_visible = False
        self._selector_font = self._tkfont.Font(root=root, family="Segoe UI", size=self._d(9), weight="bold")
        self._style = self._ttk.Style(root)
        try:
            self._style.theme_use("clam")
        except tk.TclError:
            pass

        panel = tk.Frame(root, bg=self._colors["surface"], padx=self._d(7), pady=self._d(5), bd=0)
        self.panel = panel
        panel.pack(fill="both", expand=True)
        body = tk.Frame(panel, bg=self._colors["surface"], bd=0)
        self.body = body
        body.pack(fill="both", expand=True)

        self.cover = tk.Label(body, text="♫", font=("Segoe UI Symbol", self._d(15)),
                              bg=self._colors["raised"], fg=self._colors["accent"],
                              width=4, height=2, bd=0)
        self.cover.pack(side="left", padx=(0, self._d(7)))
        self._bind_drag(self.cover)

        details = tk.Frame(body, bg=self._colors["surface"], bd=0)
        self.details = details
        details.pack(side="left", fill="both", expand=True)
        self.title_label = tk.Label(details, text="Ничего не играет", anchor="w",
                                    bg=self._colors["surface"], fg=self._colors["text"],
                                    font=("Segoe UI", self._d(9), "bold"), width=1)
        self.title_label.pack(fill="x", pady=(self._d(3), 0))
        self.artist_label = tk.Label(details, text="", anchor="w", bg=self._colors["surface"],
                                     fg=self._colors["muted"], font=("Segoe UI", self._d(7)), width=1)
        self.artist_label.pack(fill="x")
        self._bind_drag(self.title_label)
        self._bind_drag(self.artist_label)

        controls = tk.Frame(body, bg=self._colors["surface"], bd=0)
        self.controls = controls
        controls.pack(side="right", padx=(self._d(4), 0))
        self.favorite_button = self._make_button(controls, "", "favorite", size=22)
        self._make_button(controls, "", "previous", size=23)
        self.play_button = self._make_button(controls, "▶", "toggle", size=28, primary=True)
        self._make_button(controls, "", "next", size=23)
        self._make_button(controls, "↗", "open", size=21)  # вернуться в основное окно

        # The expanded layout's optional controls are absent in compact mode.
        self.selector_row = self.track_select_row = None
        self.playlist_button = self.track_button = None
        self.button_area = self.button_group = None
        self.seek = self.time_label = None
        self.volume_row = self.volume_icon = self.volume_bar = self.volume_label = None
        self.shuffle_button = self.repeat_button = None
        self.opacity_button = self.size_button = None
        self.opacity_row = self.opacity_title = self.opacity_bar = self.opacity_label = None
        self._seek_position = 0.0
        self._seek_duration = 0.0

    def _build_expanded_ui(self):
        tk, root = self._tk, self._root
        self._opacity_panel_visible = False
        root.option_add("*Font", ("Segoe UI", self._d(9)))
        self._selector_font = self._tkfont.Font(root=root, family="Segoe UI", size=self._d(9), weight="bold")
        self._style = self._ttk.Style(root)
        try:
            self._style.theme_use("clam")
        except tk.TclError:
            pass

        panel = tk.Frame(root, bg=self._colors["surface"], padx=self._d(12), pady=self._d(10))
        self.panel = panel
        panel.pack(fill="both", expand=True)

        body = tk.Frame(panel, bg=self._colors["surface"])
        self.body = body
        body.pack(fill="x")
        self.cover = tk.Label(body, text="♫", font=("Segoe UI Symbol", self._d(27)),
                              bg=self._colors["raised"], fg=self._colors["accent"],
                              width=4, height=2, bd=0)
        self.cover.pack(side="left", padx=(0, self._d(11)))
        self._bind_drag(self.cover)

        details = tk.Frame(body, bg=self._colors["surface"])
        self.details = details
        details.pack(side="left", fill="both", expand=True)
        self.title_label = tk.Label(details, text="Ничего не играет", anchor="w", bg=self._colors["surface"],
                                    fg=self._colors["text"], font=("Segoe UI", self._d(11), "bold"))
        self.title_label.pack(fill="x")
        self.artist_label = tk.Label(details, text="", anchor="w", bg=self._colors["surface"],
                                     fg=self._colors["muted"], font=("Segoe UI", self._d(9)))
        self.artist_label.pack(fill="x", pady=(self._d(1), self._d(5)))
        self._bind_drag(self.title_label)
        self._bind_drag(self.artist_label)

        compact_controls = tk.Frame(body, bg=self._colors["surface"], bd=0)
        self.compact_controls = compact_controls

        selector_row = tk.Frame(details, bg=self._colors["surface"])
        self.selector_row = selector_row
        selector_row.pack(fill="x", pady=(self._d(3), 0))
        self.playlist_button = self._make_selector(selector_row, "♫  Все треки", "playlist")
        self.playlist_button.pack(fill="x", pady=(0, self._d(5)))
        track_select_row = tk.Frame(selector_row, bg=self._colors["surface"])
        self.track_select_row = track_select_row
        track_select_row.pack(fill="x")
        self.track_button = self._make_selector(track_select_row, "♫  Выбрать трек", "track")
        self.track_button.pack(fill="x")

        self._seek_position = 0.0
        self._seek_duration = 0.0
        self.seek = tk.Canvas(panel, height=self._d(21), bg=self._colors["surface"], highlightthickness=0, bd=0,
                              cursor="hand2")
        self.seek.pack(fill="x", pady=(self._d(7), self._d(1)))
        self.seek.bind("<Configure>", lambda _event: self._draw_seek())
        self.seek.bind("<ButtonPress-1>", self._seek_press)
        self.seek.bind("<B1-Motion>", self._seek_drag)
        self.seek.bind("<ButtonRelease-1>", self._seek_end)

        controls = tk.Frame(panel, bg=self._colors["surface"])
        self.controls = controls
        controls.pack(fill="x", pady=(self._d(1), self._d(3)))
        self.time_label = tk.Label(controls, text="0:00 / 0:00", anchor="w", bg=self._colors["surface"],
                                  fg=self._colors["muted"], font=("Segoe UI", self._d(8)))
        self.time_label.pack(side="left", anchor="w")
        self.button_area = tk.Frame(controls, bg=self._colors["surface"])
        self.button_area.pack(side="left", fill="x", expand=True)
        button_group = tk.Frame(self.button_area, bg=self._colors["surface"])
        self.button_group = button_group
        button_group.pack(anchor="center")
        self.buttons = []
        self.shuffle_button = self._make_button(button_group, "", "shuffle", size=28)
        self._make_button(button_group, "", "previous", size=30)
        self.expanded_play_button = self._make_button(button_group, "", "toggle", size=38, primary=True)
        self.play_button = self.expanded_play_button
        self._make_button(button_group, "", "next", size=30)
        self.repeat_button = self._make_button(button_group, "", "repeat", size=28)
        self._make_button(button_group, "↗", "open", size=28)
        self.lyrics_button = self._make_button(button_group, "T", "lyrics", size=28)
        self.opacity_button = self._make_button(button_group, "", "opacity", size=28)
        self.size_button = self._make_button(button_group, "", "size", size=28)
        self.expanded_shuffle_button = self.shuffle_button
        self.expanded_repeat_button = self.repeat_button

        self.compact_favorite_button = self._make_button(compact_controls, "", "favorite", size=22)
        self.compact_previous_button = self._make_button(compact_controls, "", "previous", size=23)
        self.compact_play_button = self._make_button(compact_controls, "", "toggle", size=28, primary=True)
        self.compact_next_button = self._make_button(compact_controls, "", "next", size=23)
        self.compact_open_button = self._make_button(compact_controls, "↗", "open", size=21)

        volume_row = tk.Frame(panel, bg=self._colors["surface"])
        self.volume_row = volume_row
        volume_row.pack(fill="x", pady=(self._d(2), 0))
        self.volume_icon = tk.Label(volume_row, text="♫", bg=self._colors["surface"], fg=self._colors["muted"],
                                    font=("Segoe UI Symbol", self._d(10)))
        self.volume_icon.pack(side="left", padx=(0, self._d(7)))
        self.volume_bar = tk.Canvas(volume_row, height=self._d(14), bg=self._colors["surface"],
                                    highlightthickness=0, bd=0, cursor="hand2")
        self.volume_bar.pack(side="left", fill="x", expand=True)
        self.volume_bar.bind("<Configure>", lambda _event: self._draw_volume())
        self.volume_bar.bind("<ButtonPress-1>", self._volume_press)
        self.volume_bar.bind("<B1-Motion>", self._volume_drag)
        self.volume_bar.bind("<ButtonRelease-1>", self._volume_end)
        self.volume_label = tk.Label(volume_row, text="80%", width=4, anchor="e", bg=self._colors["surface"],
                                     fg=self._colors["muted"], font=("Segoe UI", self._d(8)))
        self.volume_label.pack(side="right", padx=(self._d(7), 0))

        # Opacity controls live in their own opaque popup so lowering the player's
        # alpha never fades the slider itself or makes it impossible to grab.
        self.opacity_row = None
        self.opacity_bar = None
        self.opacity_label = None
        if self._catalog_loaded:
            self._show_selected_playlist()
        else:
            self._refresh_catalog(force=True)

    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)

    def _make_selector(self, parent, text, kind):
        canvas = self._tk.Canvas(parent, height=self._d(38), bg=self._colors["surface"],
                                 highlightthickness=0, bd=0, cursor="hand2")
        canvas._selector_text = text
        canvas._selector_kind = kind
        canvas.bind("<Configure>", lambda _event, widget=canvas: self._draw_selector(widget))
        canvas.bind("<Button-1>", lambda _event, selector=kind: self._open_picker(selector))
        self._draw_selector(canvas)
        return canvas

    def _draw_selector(self, canvas):
        if not canvas.winfo_exists():
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(self._d(34), canvas.winfo_height())
        radius = min(self._d(12), height // 2)
        # Keep selector rows filled with the raised color from the active theme.
        color = self._colors["raised"]
        # A pill assembled from one rectangle and two circles remains smooth at any scale.
        canvas.create_rectangle(radius, 1, width - radius, height - 1, fill=color, outline=color)
        canvas.create_oval(1, 1, radius * 2, height - 1, fill=color, outline=color)
        canvas.create_oval(width - radius * 2, 1, width - 1, height - 1, fill=color, outline=color)
        text = str(getattr(canvas, "_selector_text", ""))
        # Draw crisp vector icons instead of font-dependent Unicode glyphs.
        if text.startswith(("♫  ", "▤  ")):
            text = text[3:]
        accent = self._colors["accent"]
        ix, cy = self._d(20), height / 2
        stroke = max(1.5, self._d(1.8))
        if getattr(canvas, "_selector_kind", "") == "playlist":
            size = self._d(12)
            for row in range(3):
                yy = cy - self._d(5) + row * self._d(5)
                canvas.create_oval(ix - size/2, yy - self._d(1), ix - size/2 + self._d(2), yy + self._d(1),
                                   fill=accent, outline=accent)
                canvas.create_line(ix - self._d(3), yy, ix + self._d(6), yy,
                                   fill=accent, width=stroke, capstyle="round")
        else:
            canvas.create_oval(ix - self._d(6), cy + self._d(2), ix - self._d(1), cy + self._d(6),
                               fill=accent, outline=accent)
            canvas.create_oval(ix + self._d(1), cy, ix + self._d(6), cy + self._d(4),
                               fill=accent, outline=accent)
            canvas.create_line(ix - self._d(1), cy + self._d(4), ix - self._d(1), cy - self._d(7),
                               fill=accent, width=stroke, capstyle="round")
            canvas.create_line(ix + self._d(6), cy + self._d(2), ix + self._d(6), cy - self._d(9),
                               fill=accent, width=stroke, capstyle="round")
            canvas.create_line(ix - self._d(1), cy - self._d(7), ix + self._d(6), cy - self._d(9),
                               fill=accent, width=stroke, capstyle="round")
        available = max(self._d(20), width - self._d(56))
        while text and self._selector_font.measure(text) > available:
            text = text[:-2] + "…" if len(text) > 2 else "…"
        canvas.create_text(self._d(34), height / 2, text=text, anchor="w", fill=self._colors["text"],
                           font=self._selector_font)
        canvas.create_text(width - self._d(14), height / 2, text="⌄", fill=self._colors["muted"],
                           font=("Segoe UI Symbol", self._d(12)))

    def _set_selector_text(self, canvas, text):
        canvas._selector_text = str(text)
        self._draw_selector(canvas)

    def _round_window(self, window=None, width=None, height=None):
        if os.name != "nt":
            return
        try:
            import ctypes
            window = window or self._root
            hwnd = int(window.winfo_id())
            hwnd = ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd
            width = width or window.winfo_width()
            height = height or window.winfo_height()
            radius = self._d(26)
            region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
            if not ctypes.windll.user32.SetWindowRgn(hwnd, region, True):
                ctypes.windll.gdi32.DeleteObject(region)
        except Exception:
            logging.getLogger("player").debug("Не удалось скруглить окно мини-плеера", exc_info=True)

    def _make_button(self, parent, label, action, size=32, primary=False):
        size = self._d(size)
        canvas = self._tk.Canvas(parent, width=size, height=size, bg=self._colors["surface"],
                                 highlightthickness=0, bd=0, cursor="hand2")
        canvas.pack(side="left", padx=(0, self._d(5)))
        button = {"canvas": canvas, "label": label, "action": action, "size": size,
                  "primary": primary, "hover": False}
        canvas.bind("<Button-1>", lambda _event, item=button: self._button_clicked(item))
        canvas.bind("<Enter>", lambda _event, item=button: self._button_hover(item, True))
        canvas.bind("<Leave>", lambda _event, item=button: self._button_hover(item, False))
        self.buttons.append(button)
        self._draw_button(button)
        return button

    def _button_clicked(self, button):
        if button["action"] == "open":
            self.open_main()
        elif button["action"] == "opacity":
            self._toggle_opacity_panel()
        elif button["action"] == "size":
            self._toggle_size_panel()
        elif button["action"] == "favorite":
            if self._track_id:
                self._queue_control("favorite", self._track_id)
        else:
            self._queue_control(button["action"], 0)

    def _button_hover(self, button, hover):
        button["hover"] = hover
        self._draw_button(button)

    def _draw_button(self, button):
        canvas, size = button["canvas"], button["size"]
        colors = self._colors
        canvas.configure(bg=colors["surface"])
        canvas.delete("all")
        key = (button["action"], button["label"], size, button["primary"], button.get("active", False),
               button["hover"], colors["surface"], colors["button"], colors["accent"], colors["muted"])
        photo = self._button_icon_cache.get(key)
        if photo is None:
            try:
                from PIL import Image, ImageDraw, ImageTk
                factor = 4
                side = max(1, size * factor)
                image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                draw = ImageDraw.Draw(image)
                unit = side / 32
                def xy(x, y): return (round(x * unit), round(y * unit))
                def box(x1, y1, x2, y2): return (*xy(x1, y1), *xy(x2, y2))
                fg = colors["surface"] if button["primary"] else colors["accent"] if button.get("active") else colors["muted"]
                if button["primary"] or button["hover"]:
                    bg = colors["accent"] if button["primary"] else colors["button"]
                    draw.ellipse(box(1, 1, 31, 31), fill=bg)
                stroke = max(5, round(2.1 * unit))
                action = button["action"]
                if action == "favorite":
                    # Two lobes and a tapered lower half form a clean, small heart glyph.
                    draw.ellipse(box(5, 5, 17, 17), fill=fg)
                    draw.ellipse(box(15, 5, 27, 17), fill=fg)
                    draw.polygon([xy(5, 12), xy(27, 12), xy(16, 27)], fill=fg)
                elif action == "toggle" and button["label"] == "⏸":
                    draw.rounded_rectangle(box(11, 9, 15, 23), radius=unit, fill=fg)
                    draw.rounded_rectangle(box(18, 9, 22, 23), radius=unit, fill=fg)
                elif action == "toggle":
                    draw.polygon([xy(12, 8), xy(23, 16), xy(12, 24)], fill=fg)
                elif action == "previous":
                    draw.polygon([xy(8, 16), xy(20, 8), xy(20, 24)], fill=fg)
                    draw.rounded_rectangle(box(22, 8, 25, 24), radius=unit, fill=fg)
                elif action == "next":
                    draw.rounded_rectangle(box(7, 8, 10, 24), radius=unit, fill=fg)
                    draw.polygon([xy(12, 8), xy(24, 16), xy(12, 24)], fill=fg)
                elif action == "shuffle":
                    paths = ([xy(5, 9), xy(10, 9), xy(21, 23), xy(27, 23)],
                             [xy(5, 23), xy(10, 23), xy(21, 9), xy(27, 9)])
                    for path in paths:
                        draw.line(path, fill=fg, width=stroke, joint="curve")
                    draw.polygon([xy(28, 23), xy(23, 19), xy(23, 27)], fill=fg)
                    draw.polygon([xy(28, 9), xy(23, 5), xy(23, 13)], fill=fg)
                elif action == "repeat":
                    draw.arc(box(5, 5, 27, 27), start=195, end=345, fill=fg, width=stroke)
                    draw.arc(box(5, 5, 27, 27), start=15, end=165, fill=fg, width=stroke)
                    draw.polygon([xy(26, 7), xy(20, 6), xy(23, 12)], fill=fg)
                    draw.polygon([xy(6, 25), xy(12, 26), xy(9, 20)], fill=fg)
                    if button["label"] == "1↻":
                        from PIL import ImageFont
                        font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
                        font = ImageFont.truetype(font_path, max(1, round(8 * unit)))
                        draw.text(xy(13, 11), "1", fill=fg, font=font)
                elif action == "open":
                    draw.line([xy(9, 23), xy(23, 9)], fill=fg, width=stroke)
                    draw.line([xy(15, 9), xy(23, 9), xy(23, 17)], fill=fg, width=stroke, joint="curve")
                elif action == "opacity":
                    upper = [xy(5 + 22 * i / 28, 16 - 7 * math.sin(math.pi * i / 28)) for i in range(29)]
                    lower = [xy(5 + 22 * i / 28, 16 + 7 * math.sin(math.pi * i / 28)) for i in range(29)]
                    draw.line(upper, fill=fg, width=stroke, joint="curve")
                    draw.line(lower, fill=fg, width=stroke, joint="curve")
                    draw.ellipse(box(12, 12, 20, 20), fill=fg)
                elif action == "size":
                    draw.line([xy(6, 13), xy(6, 6), xy(13, 6)], fill=fg, width=stroke, joint="curve")
                    draw.line([xy(6, 6), xy(13, 13)], fill=fg, width=stroke)
                    draw.line([xy(26, 19), xy(26, 26), xy(19, 26)], fill=fg, width=stroke, joint="curve")
                    draw.line([xy(26, 26), xy(19, 19)], fill=fg, width=stroke)
                elif action == "lyrics":
                    from PIL import ImageFont
                    font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf")
                    try: font = ImageFont.truetype(font_path, max(1, round(17 * unit)))
                    except OSError: font = ImageFont.load_default()
                    draw.text((side / 2, side / 2), "T", fill=fg, font=font, anchor="mm")
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                image = image.resize((size, size), resampling)
                photo = ImageTk.PhotoImage(image)
                if len(self._button_icon_cache) >= 96:
                    self._button_icon_cache.pop(next(iter(self._button_icon_cache)))
                self._button_icon_cache[key] = photo
            except Exception:
                logging.getLogger("player").debug("Не удалось сгладить значок кнопки", exc_info=True)
                return
        button["photo"] = photo
        canvas.create_image(size / 2, size / 2, image=photo)

    def _apply_theme(self, cfg):
        cfg = cfg or {}
        theme = str(cfg.get("theme") or "midnight")
        surface, raised, accent, foreground, muted = _THEME_COLORS.get(theme, _THEME_COLORS["midnight"])
        custom = cfg.get("custom_theme") if isinstance(cfg.get("custom_theme"), dict) else {}
        if theme == "custom":
            surface = custom.get("bg1", surface)
            raised = custom.get("panel", raised)
            if isinstance(raised, str) and "," in raised:
                try:
                    raised = "#" + "".join(f"{max(0, min(255, int(part))):02x}" for part in raised.split(","))
                except (ValueError, TypeError):
                    raised = surface
            accent = custom.get("accent", accent)
            foreground = custom.get("text", foreground)
            muted = custom.get("muted", muted)
        self._mini_theme = theme
        accent = "#f1f1f1" if theme == "maretu" else (cfg.get("accent") or accent)
        light_surface = self._luminance(surface) >= 0.55
        if light_surface:
            # Light custom themes can have a leftover dark panel color. Keep mini-player
            # controls close to the actual surface instead of importing that dark slab.
            raised = self._blend(surface, accent, 0.07)
            if self._contrast_ratio(surface, foreground) < 4.5:
                foreground = "#20212b"
            if self._contrast_ratio(surface, muted) < 3.0:
                muted = "#646775"
        self._colors = {
            "surface": surface,
            "raised": raised,
            # Let icon buttons and inactive slider rails merge into the panel.
            "button": surface,
            "accent": accent,
            "accent_hover": self._blend(accent, foreground, 0.18),
            "text": foreground,
            "muted": muted,
        }
        self._root.configure(bg=surface)
        # Keep the mini-player flush with its theme; Maretu used to add a
        # contrasting one-pixel outline that looked like an unwanted edge.
        self.panel.configure(bg=surface, highlightthickness=0,
                             highlightbackground=surface, highlightcolor=surface)
        for widget in (getattr(self, "body", None), getattr(self, "details", None),
                       getattr(self, "compact_controls", None),
                       getattr(self, "selector_row", None), getattr(self, "track_select_row", None),
                       getattr(self, "controls", None), getattr(self, "button_area", None),
                       getattr(self, "button_group", None), getattr(self, "volume_row", None),
                       getattr(self, "volume_icon", None), getattr(self, "volume_label", None),
                       getattr(self, "time_label", None), getattr(self, "title_label", None),
                       getattr(self, "artist_label", None)):
            if widget is not None:
                widget.configure(bg=surface)
        if self._opacity_window is not None:
            try:
                if self._opacity_window.winfo_exists():
                    self._opacity_window.configure(bg=surface)
                    self.opacity_row.configure(bg=surface)
                    self.opacity_title.configure(bg=surface, fg=muted)
                    self.opacity_label.configure(bg=surface, fg=accent)
                    self.opacity_bar.configure(bg=surface)
            except Exception:
                pass
        # Canvas widgets are created before theme application; refresh their background too,
        # otherwise they retain the default gray-purple rectangle on non-default themes.
        for canvas in (getattr(self, "seek", None), getattr(self, "volume_bar", None)):
            if canvas is not None and canvas.winfo_exists():
                canvas.configure(bg=surface)
        self.cover.configure(bg=raised, fg=accent)
        for selector in (getattr(self, "playlist_button", None), getattr(self, "track_button", None)):
            if selector is not None:
                selector.configure(bg=surface)
                self._draw_selector(selector)
        self.title_label.configure(fg=foreground)
        self.artist_label.configure(fg=muted)
        if self._lyrics_window is not None and self._lyrics_label is not None:
            try:
                self._lyrics_window.configure(bg=surface)
                self._lyrics_label.configure(bg=surface, fg=foreground, selectbackground=surface)
                self._lyrics_label.tag_configure(
                    "active_word", foreground=accent,
                    background=self._blend(surface, accent, 0.24))
            except Exception:
                pass
        if getattr(self, "time_label", None) is not None:
            self.time_label.configure(fg=muted)
        if getattr(self, "volume_icon", None) is not None:
            self.volume_icon.configure(fg=muted)
        if getattr(self, "volume_label", None) is not None:
            self.volume_label.configure(fg=muted)
        self._draw_opacity()
        for button in self.buttons:
            self._draw_button(button)
        self._draw_seek()
        self._draw_volume()

    @staticmethod
    def _blend(first, second, amount):
        try:
            a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
            b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
            return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))
        except (TypeError, ValueError, IndexError):
            return first

    @staticmethod
    def _luminance(color):
        try:
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            channels = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                        for value in channels]
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
        except (TypeError, ValueError, IndexError):
            return 0.0

    @classmethod
    def _contrast_ratio(cls, first, second):
        high, low = sorted((cls._luminance(first), cls._luminance(second)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    def _refresh_catalog(self, force=False):
        try:
            catalog = self.get_catalog() or {}
        except Exception:
            return
        self._apply_catalog(catalog, force)

    def _refresh_catalog_async(self):
        if self._catalog_loading:
            return
        self._catalog_loading = True

        def load():
            try:
                catalog = self.get_catalog() or {}
            except Exception:
                catalog = None
            def apply():
                self._catalog_loading = False
                if catalog is not None:
                    self._apply_catalog(catalog)
            try:
                if self._root is not None and self._root.winfo_exists():
                    self._root.after(0, apply)
            except Exception:
                self._catalog_loading = False

        threading.Thread(target=load, name="mini-catalog-refresh", daemon=True).start()

    def _apply_catalog(self, catalog, force=False):
        version = catalog.get("version")
        if not force and version == self._catalog_version:
            return
        self._catalog_loaded = True
        self._catalog_version = version
        self._tracks = catalog.get("tracks", []) if isinstance(catalog.get("tracks"), list) else []
        playlists = catalog.get("playlists", []) if isinstance(catalog.get("playlists"), list) else []
        self._playlists = [{"key": "__all__", "name": "Все треки", "type": "all", "count": len(self._tracks)}] + [
            item for item in playlists if isinstance(item, dict) and item.get("key")
        ]
        existing = {item["key"] for item in self._playlists}
        if self._selected_playlist not in existing:
            current = self.get_track() or {}
            preferred = str(current.get("pl") or "")
            self._selected_playlist = preferred if preferred in existing else "__all__"
        if not (self.get_track() or {}).get("remote"):
            self._show_selected_playlist()

    def _show_selected_playlist(self, current_id=None):
        item = next((p for p in self._playlists if p["key"] == self._selected_playlist), None)
        if item is None:
            self._set_selector_text(self.playlist_button, "♫  Нет плейлистов")
            tracks = []
        else:
            self._set_selector_text(self.playlist_button, f"▤  {str(item.get('name') or 'Плейлист')[:48]}")
            if item.get("type") == "all":
                tracks = list(self._tracks)
            elif item.get("type") == "custompl":
                by_id = {str(track.get("id")): track for track in self._tracks}
                tracks = [by_id[track_id] for track_id in item.get("ids", []) if str(track_id) in by_id]
            else:
                tracks = [track for track in self._tracks if str(track.get("pl") or "") == self._selected_playlist]
            if item.get("type") != "custompl":
                tracks.sort(key=lambda t: (t.get("n") is None, t.get("n") or 0, str(t.get("title") or "").casefold()))
        self._track_ids = {str(track.get("title") or "Без названия"): str(track.get("id") or "")
                           for track in tracks if track.get("id")}
        if current_id:
            selected = next((t for t in tracks if str(t.get("id")) == str(current_id)), None)
        else:
            selected = next((t for t in tracks if str(t.get("id")) == self._selected_track_id), None)
        if selected is None and tracks:
            selected = tracks[0]
        self._selected_track_id = str((selected or {}).get("id") or "")
        self._set_track_button(selected)
        if self._picker_window is not None and self._picker_window.winfo_exists():
            if self._picker_kind == "track":
                self._fill_picker_tracks(tracks)
            else:
                self._fill_picker_playlists()

    def _set_track_button(self, track):
        title = str((track or {}).get("title") or "Нет треков")
        artist = str((track or {}).get("artist") or "")
        label = f"♫  {title} — {artist}" if artist else f"♫  {title}"
        self._set_selector_text(self.track_button, (label[:62] + ("…" if len(label) > 62 else "")))
        track_id = str((track or {}).get("id") or "")
        # Keep the large now-playing panel in sync with the selector immediately;
        # waiting for the next polling tick made it visibly lag behind.
        self.title_label.configure(text=title[:64])
        self.artist_label.configure(text=artist[:72] if artist else "Неизвестный исполнитель")
        if track_id and track_id != self._track_id:
            self._track_id = track_id
            self._load_cover(track_id)

    def _open_picker(self, kind):
        if self._picker_window is not None and self._picker_window.winfo_exists():
            self._close_picker()
        tk = self._tk
        popup = tk.Toplevel(self._root)
        self._picker_window = popup
        self._picker_kind = kind
        popup.overrideredirect(True)
        popup.title("Выбрать плейлист" if kind == "playlist" else "Выбрать трек")
        popup.configure(bg=self._colors["surface"])
        popup.attributes("-topmost", True)
        width = max(self._d(360), self._window_width())
        self._picker_width = width
        height = min(self._d(430), max(self._d(180), self._d(54) * (len(self._playlists) if kind == "playlist" else len(self._track_ids))))
        x = self._root.winfo_x()
        y = self._root.winfo_y() + self._root.winfo_height() + self._d(5)
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        if y + height > sh - self._d(12):
            y = self._root.winfo_y() - height - self._d(5)
        x = max(0, min(sw - width, x))
        y = max(0, min(sh - height, y))
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.minsize(width, self._d(150))
        shell = tk.Frame(popup, bg=self._colors["surface"], padx=self._d(8), pady=self._d(8))
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, bg=self._colors["surface"], highlightthickness=0, bd=0)
        scrollbar = self._ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        self._picker_scrollbar = scrollbar
        canvas.configure(yscrollcommand=self._picker_scroll_set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.bind_all("<MouseWheel>", self._picker_mousewheel)
        canvas.bind_all("<ButtonPress-1>", self._picker_outside_click)
        canvas.bind("<Configure>", self._picker_schedule_refresh, add="+")
        canvas.bind("<Button-1>", self._picker_canvas_click)
        self._picker_canvas = canvas
        self._picker_content = canvas
        if kind == "track":
            tracks = self._visible_tracks_for_selected_playlist()
            self._fill_picker_tracks(tracks)
        else:
            self._fill_picker_playlists()
        popup.protocol("WM_DELETE_WINDOW", self._close_picker)
        popup.bind("<Escape>", lambda _event: self._close_picker())
        popup.bind("<FocusOut>", lambda _event, w=popup: w.after_idle(lambda: self._picker_focus_lost(w)))
        popup.after(80, self._picker_schedule_refresh)
        popup.after_idle(lambda: self._round_window(popup, width, height))
        popup.focus_set()

    def _picker_focus_lost(self, popup):
        if self._picker_window is not popup:
            return
        focus = self._root.focus_get()
        while focus is not None:
            if focus is popup:
                return
            focus = getattr(focus, "master", None)
        self._close_picker()

    def _visible_tracks_for_selected_playlist(self):
        item = next((p for p in self._playlists if p["key"] == self._selected_playlist), {})
        if item.get("type") == "custompl":
            by_id = {str(t.get("id")): t for t in self._tracks}
            return [by_id[i] for i in item.get("ids", []) if i in by_id]
        tracks = list(self._tracks) if item.get("type") == "all" else [t for t in self._tracks if str(t.get("pl") or "") == self._selected_playlist]
        if item.get("type") != "custompl":
            tracks.sort(key=lambda t: (t.get("n") is None, t.get("n") or 0, str(t.get("title") or "").casefold()))
        return tracks

    def _picker_mousewheel(self, event):
        if self._picker_canvas is None or self._picker_window is None or not self._picker_window.winfo_exists():
            return
        if self._picker_canvas.winfo_containing(event.x_root, event.y_root):
            self._picker_canvas.yview_scroll(int(-event.delta / 120), "units")
            self._picker_schedule_refresh()
            return "break"

    def _picker_scroll_set(self, first, last):
        scrollbar = getattr(self, "_picker_scrollbar", None)
        if scrollbar is not None:
            scrollbar.set(first, last)
        self._picker_schedule_refresh()

    def _picker_schedule_refresh(self, _event=None):
        canvas = self._picker_canvas
        if canvas is None or self._picker_scroll_after is not None:
            return
        try:
            self._picker_scroll_after = canvas.after_idle(self._picker_refresh_visible)
        except Exception:
            self._picker_scroll_after = None

    def _picker_canvas_click(self, _event=None):
        canvas = self._picker_canvas
        if canvas is None:
            return
        for item in canvas.find_withtag("current"):
            for tag in canvas.gettags(item):
                if tag.startswith("trackrow_"):
                    try:
                        index = int(tag.partition("_")[2])
                        self._track_selected(str(self._picker_rows[index].get("id") or ""))
                    except (ValueError, IndexError, AttributeError):
                        pass
                    return "break"
                if tag.startswith("playlistrow_"):
                    try:
                        index = int(tag.partition("_")[2])
                        self._playlist_selected(str(self._picker_rows[index].get("key") or ""))
                    except (ValueError, IndexError, AttributeError):
                        pass
                    return "break"

    def _picker_outside_click(self, event):
        popup = self._picker_window
        if popup is None or not popup.winfo_exists():
            return
        if event.widget in (self.playlist_button, self.track_button):
            return
        try:
            widget = self._root.winfo_containing(event.x_root, event.y_root)
            while widget is not None:
                if widget is popup:
                    return
                widget = getattr(widget, "master", None)
        except Exception:
            pass
        self._close_picker()

    def _close_picker(self):
        try:
            self._root.unbind_all("<MouseWheel>")
            self._root.unbind_all("<ButtonPress-1>")
        except Exception:
            pass
        if self._picker_window is not None and self._picker_window.winfo_exists():
            self._picker_window.destroy()
        self._picker_window = None
        self._picker_canvas = None
        self._picker_rows = []
        self._picker_drawn = {}
        self._picker_scroll_after = None
        self._picker_scrollbar = None

    def _fill_picker_tracks(self, tracks):
        canvas = self._picker_canvas
        if canvas is None:
            return
        self._picker_rows = [track for track in tracks if str(track.get("id") or "")]
        self._picker_drawn = {}
        row_height = self._d(64)
        canvas.delete("all")
        total_height = max(1, len(self._picker_rows) * row_height)
        canvas.configure(scrollregion=(0, 0, max(self._picker_width, canvas.winfo_width()), total_height))
        selected_index = next((i for i, track in enumerate(self._picker_rows)
                               if str(track.get("id") or "") == self._selected_track_id), 0)
        def reveal_selected():
            if canvas.winfo_exists() and self._picker_rows:
                viewport = max(row_height, canvas.winfo_height())
                y = max(0, selected_index * row_height - (viewport - row_height) / 2)
                canvas.yview_moveto(min(1.0, y / total_height))
                self._picker_schedule_refresh()
        if self._picker_window is not None and self._picker_window.winfo_exists():
            self._picker_window.after_idle(reveal_selected)
        self._picker_refresh_visible()

    def _fill_picker_playlists(self):
        canvas = self._picker_canvas
        if canvas is None:
            return
        canvas.delete("all")
        self._picker_rows = list(self._playlists)
        self._picker_drawn = {}
        counts = {}
        for track in self._tracks:
            key = str(track.get("pl") or "")
            counts[key] = counts.get(key, 0) + 1
        row_height = self._d(56)
        width = max(self._picker_width, canvas.winfo_width())
        for index, playlist in enumerate(self._picker_rows):
            key = str(playlist.get("key") or "")
            name = str(playlist.get("name") or "Плейлист")
            count = int(playlist.get("count") or len(playlist.get("ids", [])) or counts.get(key, 0))
            bg = self._colors["raised"] if key == self._selected_playlist else self._colors["surface"]
            tag = f"playlistrow_{index}"
            y = index * row_height + self._d(3)
            radius = self._d(12)
            canvas.create_rectangle(radius + self._d(4), y, width - radius - self._d(4), y + row_height - self._d(6),
                                    fill=bg, outline=bg, tags=(tag,))
            canvas.create_oval(self._d(4), y, self._d(4) + radius * 2, y + row_height - self._d(6),
                               fill=bg, outline=bg, tags=(tag,))
            canvas.create_oval(width - radius * 2 - self._d(4), y, width - self._d(4), y + row_height - self._d(6),
                               fill=bg, outline=bg, tags=(tag,))
            canvas.create_text(self._d(23), y + (row_height - self._d(6)) / 2, text="▤",
                               fill=self._colors["accent"], font=("Segoe UI Symbol", self._d(15)), tags=(tag,))
            canvas.create_text(self._d(45), y + (row_height - self._d(6)) / 2, text=name[:70], anchor="w",
                               fill=self._colors["text"], font=("Segoe UI", self._d(9), "bold"),
                               width=max(self._d(40), width - self._d(128)), tags=(tag,))
            canvas.create_text(width - self._d(16), y + (row_height - self._d(6)) / 2, text=f"{count} тр.",
                               anchor="e", fill=self._colors["muted"], font=("Segoe UI", self._d(8)), tags=(tag,))
        canvas.configure(scrollregion=(0, 0, width, max(1, len(self._picker_rows) * row_height)))

    def _picker_refresh_visible(self):
        self._picker_scroll_after = None
        canvas = self._picker_canvas
        if not self._picker_rows or canvas is None:
            return
        try:
            if self._picker_kind != "track":
                return
            row_height = self._d(64)
            top = max(0, canvas.canvasy(0))
            viewport = max(row_height, canvas.winfo_height())
            start = max(0, int(top // row_height) - 1)
            end = min(len(self._picker_rows), int((top + viewport) // row_height) + 2)
            wanted = set(range(start, end))
            for index in tuple(self._picker_drawn):
                if index not in wanted:
                    canvas.delete(f"trackrow_{index}")
                    del self._picker_drawn[index]
            width = max(1, canvas.winfo_width(), self._picker_width - self._d(34))
            for index in range(start, end):
                record = self._picker_drawn.get(index)
                if record is None:
                    record = self._draw_picker_track(index, width, row_height)
                    self._picker_drawn[index] = record
                if not record["loaded"]:
                    record["loaded"] = True
                    if self._picker_window and self._picker_window.winfo_exists():
                        self._picker_window.after_idle(lambda i=index, r=record: self._load_picker_cover(i, r))
        except Exception:
            logging.getLogger("player").debug("Не удалось обновить видимые строки мини-плеера", exc_info=True)

    def _draw_picker_track(self, index, width, row_height):
        canvas = self._picker_canvas
        track = self._picker_rows[index]
        tag = f"trackrow_{index}"
        y = index * row_height + self._d(3)
        row_h = row_height - self._d(6)
        radius = min(self._d(12), row_h // 2)
        bg = self._colors["raised"] if str(track.get("id") or "") == self._selected_track_id else self._colors["surface"]
        canvas.create_rectangle(self._d(4) + radius, y, width - self._d(4) - radius, y + row_h,
                                fill=bg, outline=bg, tags=(tag,))
        canvas.create_oval(self._d(4), y, self._d(4) + radius * 2, y + row_h,
                           fill=bg, outline=bg, tags=(tag,))
        canvas.create_oval(width - self._d(4) - radius * 2, y, width - self._d(4), y + row_h,
                           fill=bg, outline=bg, tags=(tag,))
        cover_size = self._d(48)
        cover_x, cover_y = self._d(10), y + (row_h - cover_size) // 2
        canvas.create_rectangle(cover_x, cover_y, cover_x + cover_size, cover_y + cover_size,
                                fill=bg, outline=bg, tags=(tag,))
        icon = canvas.create_text(cover_x + cover_size / 2, cover_y + cover_size / 2, text="♫",
                                  fill=self._colors["accent"], font=("Segoe UI Symbol", self._d(17)), tags=(tag,))
        cover_item = canvas.create_image(cover_x, cover_y, anchor="nw", tags=(tag,))
        max_chars = max(12, int((width - self._d(94)) / max(1, self._d(6))))
        title = str(track.get("title") or "Без названия")
        artist = str(track.get("artist") or "Неизвестный исполнитель")
        if len(title) > max_chars:
            title = title[:max_chars - 1] + "…"
        if len(artist) > max_chars:
            artist = artist[:max_chars - 1] + "…"
        text_x = cover_x + cover_size + self._d(12)
        canvas.create_text(text_x, y + self._d(20), text=title, anchor="w", fill=self._colors["text"],
                           font=("Segoe UI", self._d(9), "bold"), tags=(tag,))
        canvas.create_text(text_x, y + self._d(42), text=artist, anchor="w", fill=self._colors["muted"],
                           font=("Segoe UI", self._d(8)), tags=(tag,))
        return {"id": str(track.get("id") or ""), "track": track, "cover_item": cover_item,
                "icon_item": icon, "loaded": False, "photo": None}

    def _load_picker_cover(self, index, row):
        try:
            from PIL import Image, ImageDraw, ImageTk, ImageOps
            key = row["id"]
            photo = self._picker_thumb_cache.get(key)
            if photo is None:
                raw = self.get_cover(key)
                if not raw:
                    return
                image = Image.open(io.BytesIO(raw)).convert("RGBA")
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                # Fit the artwork to the entire reserved slot; a smaller bitmap left
                # a visible gray 8px frame and made covers look offset in the picker.
                image = ImageOps.fit(image, (self._d(48), self._d(48)), method=resampling)
                mask = Image.new("L", image.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=self._d(7), fill=255)
                image.putalpha(mask)
                photo = ImageTk.PhotoImage(image)
                if len(self._picker_thumb_cache) >= 96:
                    self._picker_thumb_cache.pop(next(iter(self._picker_thumb_cache)))
                self._picker_thumb_cache[key] = photo
            if self._picker_canvas is None or index not in self._picker_drawn:
                return
            self._picker_canvas.itemconfigure(row["cover_item"], image=photo)
            self._picker_canvas.itemconfigure(row["icon_item"], state="hidden")
            row["photo"] = photo
        except Exception:
            logging.getLogger("player").debug("Не удалось показать обложку в списке мини-плеера", exc_info=True)

    def _playlist_selected(self, key=None):
        if self._updating_selectors:
            return
        if key is None:
            key = self._selected_playlist
        if not key or key == self._selected_playlist:
            self._close_picker()
            return
        self._selected_playlist = key
        self._playlist_pending_until = time.monotonic() + 2.0
        self._updating_selectors = True
        try:
            self._show_selected_playlist()
        finally:
            self._updating_selectors = False
        self._close_picker()
        self._queue_control("playlist", key)

    def _track_selected(self, track_id=None):
        if self._updating_selectors:
            return
        if not isinstance(track_id, str):
            track_id = self._selected_track_id
        track = next((item for item in self._tracks if str(item.get("id") or "") == track_id), None)
        if track:
            self._selected_track_id = track_id
            self._set_track_button(track)
            self._track_pending_id = track_id
            self._track_pending_until = time.monotonic() + 2.0
            self._close_picker()
            self._queue_control("track", track_id)

    def _process_queue(self):
        try:
            while True:
                command, value = self._queue.get_nowait()
                if command == "close":
                    self._root.destroy()
                    return
                if command == "show":
                    self._visible = True
                    # Resolve the saved position and size before mapping the window.
                    self._place()
                    self._root.update_idletasks()
                    self._exclude_from_alt_tab(self._root)
                    self._root.attributes("-topmost", True)
                    self._root.deiconify()
                elif command == "hide":
                    self._visible = False
                    if self._lyrics_window is not None:
                        try: self._lyrics_window.withdraw()
                        except Exception: pass
                    self._close_opacity_panel()
                    if self._size_window is not None:
                        try: self._size_window.destroy()
                        except Exception: pass
                        self._size_window = None
                    self._root.withdraw()
                elif command == "configure":
                    self._apply_settings(value or self.get_settings())
        except queue.Empty:
            pass
        self._root.after(100 if self._visible else 250, self._process_queue)  # скрытый мини-плеер реже будит процесс

    def _build_lyrics_window(self):
        try:
            tk = self._tk
            window = tk.Toplevel(self._root)
            window.withdraw()
            window.title("NOT soundLARP 100% — текст песни")
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            window.configure(bg=self._colors["surface"])
            self._exclude_from_alt_tab(window)
            lyrics_font = ("Segoe UI", self._d(16), "bold")
            label = tk.Text(window, wrap="word", height=3, width=52, takefocus=0,
                bg=self._colors["surface"], fg=self._colors["text"], padx=self._d(16), pady=self._d(10),
                spacing1=self._d(2), spacing3=self._d(2), font=lyrics_font, relief="flat", bd=0,
                highlightthickness=0, insertwidth=0, selectbackground=self._colors["surface"],
                state="disabled", cursor="fleur")
            label.tag_configure(
                "active_word", foreground=self._colors["accent"],
                background=self._blend(self._colors["surface"], self._colors["accent"], 0.24),
                font=lyrics_font)
            label.pack(fill="both", expand=True)
            for widget in (window, label):
                widget.bind("<ButtonPress-1>", self._lyrics_drag_start)
                widget.bind("<B1-Motion>", self._lyrics_drag_move)
                widget.bind("<ButtonRelease-1>", self._lyrics_drag_end)
            self._lyrics_window, self._lyrics_label = window, label
        except Exception:
            logging.getLogger("player").debug("Не удалось создать окно текста мини-плеера", exc_info=True)

    @staticmethod
    def _exclude_from_alt_tab(window):
        try: window.attributes("-toolwindow", True)
        except Exception: pass
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = wintypes.HWND(int(window.winfo_id()))
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
            user32.SetWindowLongW.restype = ctypes.c_long
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, (style | 0x00000080) & ~0x00040000)  # TOOLWINDOW, clear APPWINDOW
            user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT)
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        except Exception:
            logging.getLogger("player").debug("Не удалось исключить окно мини-плеера из Alt+Tab", exc_info=True)

    def _lyrics_drag_start(self, event):
        if self._lyrics_window is not None:
            self._lyrics_drag = (event.x_root - self._lyrics_window.winfo_x(),
                                 event.y_root - self._lyrics_window.winfo_y())

    def _lyrics_drag_move(self, event):
        if self._lyrics_drag is None or self._lyrics_window is None:
            return
        dx, dy = self._lyrics_drag
        width, height = self._lyrics_window.winfo_width(), self._lyrics_window.winfo_height()
        x = max(0, min(self._lyrics_window.winfo_screenwidth() - width, event.x_root - dx))
        y = max(0, min(self._lyrics_window.winfo_screenheight() - height, event.y_root - dy))
        self._lyrics_window.geometry(f"+{x}+{y}")

    def _lyrics_drag_end(self, _event):
        self._lyrics_drag = None
        if self._lyrics_window is not None:
            try:
                self.save_position({"lyrics_x": self._lyrics_window.winfo_x(),
                                    "lyrics_y": self._lyrics_window.winfo_y()})
            except Exception:
                logging.getLogger("player").debug("Не удалось сохранить положение текста мини-плеера", exc_info=True)

    def _update_lyrics_overlay(self, state, cfg=None):
        window, label = self._lyrics_window, self._lyrics_label
        if window is None or label is None:
            return
        cfg = cfg or self.get_settings() or {}
        mini = cfg.get("mini_player") if isinstance(cfg.get("mini_player"), dict) else cfg
        text = str(state.get("mini_lyrics") or "").strip()
        if not self._visible or not mini.get("enabled", True) or not mini.get("lyrics_enabled", True) or not text:
            try: window.withdraw()
            except Exception: pass
            self._lyrics_last_text = ""
            return
        try:
            count = max(1, min(5, int(mini.get("lyrics_lines", 3))))
        except (TypeError, ValueError):
            count = 3
        palette = self._colors
        try: word_index = int(state.get("mini_lyrics_word_index", -1))
        except (TypeError, ValueError): word_index = -1
        if text != self._lyrics_last_text or word_index != self._lyrics_last_word:
            lyrics_font = ("Segoe UI", self._d(16), "bold")
            label.configure(state="normal", bg=palette["surface"], fg=palette["text"],
                            font=lyrics_font, wrap="word")
            label.tag_configure(
                "active_word", foreground=palette["accent"],
                background=self._blend(palette["surface"], palette["accent"], 0.24),
                font=lyrics_font)
            label.delete("1.0", "end")
            label.insert("1.0", text)
            word_offsets=list(re.finditer(r"\S+",text))
            if 0<=word_index<len(word_offsets):
                match=word_offsets[word_index]
                start=f"1.0+{match.start()}c";end=f"1.0+{match.end()}c"
                label.tag_add("active_word",start,end)
            label.configure(state="disabled")
            self._lyrics_last_text = text
            self._lyrics_last_word = word_index
        width = self._d(500)
        height = self._d(28 + count * 33)
        window.update_idletasks()
        screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
        x, y = int(mini.get("lyrics_x", 0) or 0), int(mini.get("lyrics_y", 0) or 0)
        if x == 0 and y == 0:
            x = self._root.winfo_x() + self._root.winfo_width() + self._d(10)
            y = self._root.winfo_y() + max(0, (self._root.winfo_height() - height) // 2)
            if x + width > screen_w:
                x = self._root.winfo_x() - width - self._d(10)
        x = max(0, min(screen_w - width, x))
        y = max(0, min(screen_h - height, y))
        window.geometry(f"{width}x{height}+{x}+{y}")
        if self._lyrics_round_size != (width, height):
            window.update_idletasks()
            self._round_window(window, width, height)
            self._lyrics_round_size = (width, height)
        try:
            window.attributes("-alpha", max(.35, min(1.0, self._opacity / 100)))
            window.attributes("-topmost", True)
            window.deiconify()
            window.lift()
        except Exception:
            pass

    def _apply_settings(self, cfg):
        cfg = cfg or {}
        mini = cfg.get("mini_player") if isinstance(cfg.get("mini_player"), dict) else cfg
        requested_scale = max(50, min(500, int(mini.get("scale", 100))))
        requested_style = "compact" if mini.get("style") == "compact" else "standard"
        style_changed = requested_style != self._mini_style
        scale_changed = abs(requested_scale / 100 - self._scale) > 0.001
        if style_changed or scale_changed:
            self._close_opacity_panel()
            self._close_picker()
            if self._size_window is not None:
                try: self._size_window.destroy()
                except Exception: pass
                self._size_window = None
            self._mini_style = requested_style
            self._set_scale(requested_scale)
            x, y = self._root.winfo_x(), self._root.winfo_y()
            if scale_changed:
                try:
                    if self.panel.winfo_exists():
                        self.panel.destroy()
                except Exception:
                    pass
                self._build_ui()
            else:
                self._apply_mini_layout()
            self._root.geometry(f"{self._window_width()}x{self._window_height()}+{x}+{y}")
        if self._size_scale_widget is not None:
            try:
                if self._size_scale_widget.winfo_exists():
                    self._size_value = requested_scale
                    if self._size_value_label is not None and self._size_value_label.winfo_exists():
                        self._size_value_label.configure(text=f"{requested_scale}%")
                    self._draw_size_scale()
            except Exception:
                pass
        self._apply_theme(cfg)
        self._opacity = max(35, min(100, int(mini.get("opacity", 92))))
        self._draw_opacity()
        if not mini.get("enabled", True):
            self.hide()
            return
        try:
            opacity = self._opacity / 100
            self._root.attributes("-alpha", opacity)
        except Exception:
            pass
        self._place(mini)
        self._root.update_idletasks()
        self._round_window(self._root, self._root.winfo_width(), self._root.winfo_height())
        self._update_lyrics_overlay(self.get_track() or {}, cfg)

    def _place(self, cfg=None):
        if self._root is None:
            return
        cfg = cfg or self.get_settings()
        width, height = self._window_width(), self._window_height()
        sw, sh = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        positions = {
            "top-left": (self.MARGIN, self.MARGIN),
            "top-right": (sw - width - self.MARGIN, self.MARGIN),
            "bottom-left": (self.MARGIN, sh - height - self.MARGIN),
            "bottom-right": (sw - width - self.MARGIN, sh - height - self.MARGIN),
            "custom": (int(cfg.get("x", sw - width - self.MARGIN)), int(cfg.get("y", sh - height - self.MARGIN))),
        }
        x, y = positions.get(cfg.get("position", "bottom-right"), positions["bottom-right"])
        x = max(0, min(sw - width, x))
        y = max(0, min(sh - height, y))
        self._root.geometry(f"{width}x{height}+{x}+{y}")

    def _drag_start(self, event):
        self._drag = (event.x_root, event.y_root, self._root.winfo_x(), self._root.winfo_y())

    def _drag_move(self, event):
        if self._drag:
            sx, sy, x, y = self._drag
            self._root.geometry(f"{self._window_width()}x{self._window_height()}+{x + event.x_root - sx}+{y + event.y_root - sy}")

    def _drag_end(self, _event):
        if not self._drag:
            return
        self._drag = None
        self.save_position({"position": "custom", "x": self._root.winfo_x(), "y": self._root.winfo_y()})

    def _close_opacity_panel(self):
        window, self._opacity_window = self._opacity_window, None
        self._opacity_panel_visible = False
        self.opacity_row = self.opacity_title = self.opacity_bar = self.opacity_label = None
        if self.opacity_button is not None:
            self.opacity_button["active"] = False
            self._draw_button(self.opacity_button)
        if window is not None:
            try:
                if window.winfo_exists():
                    window.destroy()
            except Exception:
                pass

    def _toggle_opacity_panel(self):
        if self._opacity_window is not None:
            self._close_opacity_panel()
            return
        tk = self._tk
        window = tk.Toplevel(self._root)
        self._opacity_window = window
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.attributes("-alpha", 1.0)
        window.configure(bg=self._colors["surface"])
        self.opacity_row = tk.Frame(window, bg=self._colors["surface"])
        self.opacity_row.pack(fill="both", expand=True, padx=14, pady=10)
        self.opacity_title = tk.Label(self.opacity_row, text="Прозрачность", bg=self._colors["surface"],
                                      fg=self._colors["muted"], font=("Segoe UI", 9, "bold"))
        self.opacity_title.pack(side="left", padx=(0, 12))
        self.opacity_bar = tk.Canvas(self.opacity_row, height=34, bg=self._colors["surface"],
                                     highlightthickness=0, bd=0, cursor="hand2")
        self.opacity_bar.pack(side="left", fill="x", expand=True)
        self.opacity_bar.bind("<Configure>", lambda _event: self._draw_opacity())
        self.opacity_bar.bind("<ButtonPress-1>", self._opacity_press)
        self.opacity_bar.bind("<B1-Motion>", self._opacity_drag)
        self.opacity_bar.bind("<ButtonRelease-1>", self._opacity_end)
        self.opacity_label = tk.Label(self.opacity_row, text=f"{self._opacity}%", width=4, anchor="e",
                                      bg=self._colors["surface"], fg=self._colors["accent"],
                                      font=("Segoe UI", 9, "bold"))
        self.opacity_label.pack(side="right", padx=(12, 0))
        window.bind("<Escape>", lambda _event: self._close_opacity_panel())
        window.update_idletasks()
        width, height = 360, 58
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        x = self._root.winfo_x() + self._root.winfo_width() - width
        y = self._root.winfo_y() + self._root.winfo_height() + 8
        if y + height > sh:
            y = self._root.winfo_y() - height - 8
        window.geometry(f"{width}x{height}+{max(0, min(sw - width, x))}+{max(0, min(sh - height, y))}")
        self._opacity_panel_visible = True
        self.opacity_button["active"] = True
        self._draw_button(self.opacity_button)
        self._apply_theme(self.get_settings() or {})
        self._draw_opacity()
        self._round_window(window, width, height)

    def _toggle_size_panel(self):
        if self._size_window is not None:
            try:
                if self._size_window.winfo_exists():
                    self._size_window.destroy()
                    self._size_window = None
                    return
            except Exception:
                self._size_window = None
        tk = self._tk
        window = tk.Toplevel(self._root)
        self._size_window = window
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=self._colors["surface"])
        window.geometry("320x86")
        window.resizable(False, False)
        row = tk.Frame(window, bg=self._colors["surface"])
        row.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(row, text="Размер мини-плеера", bg=self._colors["surface"], fg=self._colors["text"],
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self._size_value = round(self._scale * 100)
        self._size_value_label = tk.Label(row, text=f"{self._size_value}%", bg=self._colors["surface"],
                                           fg=self._colors["accent"], font=("Segoe UI", 9, "bold"))
        self._size_value_label.pack(side="right")
        tk.Button(row, text="×", command=window.destroy, bg=self._colors["surface"], fg=self._colors["muted"],
                  activebackground=self._colors["button"], activeforeground=self._colors["text"],
                  relief="flat", bd=0, font=("Segoe UI", 11, "bold"), cursor="hand2").pack(side="right", padx=(6, 0))
        self._size_scale_widget = tk.Canvas(window, height=32, bg=self._colors["surface"],
                                            highlightthickness=0, bd=0, cursor="hand2")
        self._size_scale_widget.pack(fill="x", padx=12, pady=(0, 5))
        self._size_scale_widget.bind("<Configure>", lambda _event: self._draw_size_scale())
        self._size_scale_widget.bind("<ButtonPress-1>", self._size_slider_press)
        self._size_scale_widget.bind("<B1-Motion>", self._size_slider_drag)
        self._size_scale_widget.bind("<ButtonRelease-1>", self._size_slider_released)
        window.bind("<Escape>", lambda _event: window.destroy())
        window.update_idletasks()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        x = self._root.winfo_x() + self._root.winfo_width() - window.winfo_width()
        y = self._root.winfo_y() + self._root.winfo_height() + 8
        if y + window.winfo_height() > sh:
            y = self._root.winfo_y() - window.winfo_height() - 8
        window.geometry(f"+{max(0, min(sw-window.winfo_width(), x))}+{max(0, min(sh-window.winfo_height(), y))}")
        self._round_window(window, window.winfo_width(), window.winfo_height())

    def _draw_size_scale(self):
        canvas = self._size_scale_widget
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right, y = 10, max(11, width - 10), height / 2
        fraction = (max(50, min(500, self._size_value)) - 50) / 450
        pos = left + (right - left) * fraction
        rail = self._blend(self._colors["muted"], self._colors["surface"], 0.58)
        canvas.create_line(left, y, right, y, fill=rail, width=6, capstyle="round")
        if pos > left:
            canvas.create_line(left, y, pos, y, fill=self._colors["accent"], width=6, capstyle="round")
        canvas.create_oval(pos - 8, y - 8, pos + 8, y + 8, fill=self._colors["text"],
                           outline=self._colors["accent"], width=2)

    def _size_slider_at(self, x):
        canvas = self._size_scale_widget
        if canvas is None or not canvas.winfo_exists():
            return
        width = max(1, canvas.winfo_width())
        fraction = max(0.0, min(1.0, (x - 10) / max(1, width - 20)))
        self._size_value = round(50 + fraction * 450)
        if self._size_value_label is not None and self._size_value_label.winfo_exists():
            self._size_value_label.configure(text=f"{self._size_value}%")
        self._draw_size_scale()

    def _size_slider_press(self, event):
        self._size_slider_at(event.x)

    def _size_slider_drag(self, event):
        self._size_slider_at(event.x)

    def _size_slider_released(self, _event=None):
        value = self._size_value
        if value == round(self._scale * 100):
            return
        self._close_opacity_panel()
        self._set_scale(value)
        x, y = self._root.winfo_x(), self._root.winfo_y()
        self._root.geometry(f"{self._window_width()}x{self._window_height()}+{x}+{y}")
        try:
            if self.panel.winfo_exists():
                self.panel.destroy()
        except Exception:
            pass
        self._build_ui()
        self._apply_theme(self.get_settings() or {})
        self._root.update_idletasks()
        self._round_window(self._root, self._root.winfo_width(), self._root.winfo_height())
        self.save_position({"scale": value})

    def _draw_opacity(self):
        canvas = self.opacity_bar
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        margin = 9
        left, right = margin, max(margin + 1, width - margin)
        y = height / 2
        fraction = max(0, min(1, (self._opacity - 35) / 65))
        pos = left + (right - left) * fraction
        rail = self._blend(self._colors["muted"], self._colors["surface"], 0.42)
        canvas.create_line(left, y, right, y, fill=rail, width=7, capstyle="round")
        if pos > left:
            canvas.create_line(left, y, pos, y, fill=self._colors["accent"], width=7, capstyle="round")
        canvas.create_oval(pos - 9, y - 9, pos + 9, y + 9, fill=self._colors["text"],
                           outline=self._colors["accent"], width=3)
        if self.opacity_label is not None and self.opacity_label.winfo_exists():
            self.opacity_label.configure(text=f"{self._opacity}%")

    def _opacity_at(self, x):
        if self.opacity_bar is None or not self.opacity_bar.winfo_exists():
            return
        width = max(1, self.opacity_bar.winfo_width())
        margin = 9
        fraction = max(0, min(1, (x - margin) / max(1, width - 2 * margin)))
        self._opacity=round(35+fraction*65)
        try: self._root.attributes("-alpha",self._opacity/100)
        except Exception: pass
        self._draw_opacity()

    def _opacity_press(self,event): self._opacity_at(event.x)
    def _opacity_drag(self,event): self._opacity_at(event.x)
    def _opacity_end(self,_event): self.save_position({"opacity":self._opacity})

    def _draw_seek(self):
        if not getattr(self, "seek", None):
            return
        canvas = self.seek
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        radius = self._d(6)
        left, right = radius + self._d(1), max(radius + self._d(2), width - radius - self._d(1))
        y = height / 2
        fraction = 0 if self._seek_duration <= 0 else max(0, min(1, self._seek_position / self._seek_duration))
        progress = left + (right - left) * fraction
        if fraction > 0:
            canvas.create_line(left, y, progress, y, fill=self._colors["accent"], width=self._d(5), capstyle="round")
        # Accent thumb with a surface ring stays distinct on both light and dark themes.
        canvas.create_oval(progress - radius, y - radius, progress + radius, y + radius,
                           fill=self._colors["accent"], outline=self._colors["surface"], width=self._d(2))

    def _seek_at(self, x):
        width = max(1, self.seek.winfo_width())
        radius = self._d(6)
        ratio = max(0.0, min(1.0, (x - radius - self._d(1)) / max(1, width - 2 * radius - self._d(2))))
        self._seek_position = self._seek_duration * ratio
        self._seeking = True
        self._seek_pending_until = time.monotonic() + 1.5
        self._queue_live_control("seek", self._seek_position)
        self._draw_seek()
        self.time_label.configure(text=f"{self._fmt(self._seek_position)} / {self._fmt(self._seek_duration)}")

    def _seek_press(self, event):
        self._seek_at(event.x)

    def _seek_drag(self, event):
        self._seek_at(event.x)

    def _seek_end(self, _event):
        if self._seek_duration > 0:
            self._seek_pending_until = time.monotonic() + 1.5
            self._flush_live_control("seek", float(self._seek_position))
        self._seeking = False

    def _draw_volume(self):
        if not getattr(self, "volume_bar", None):
            return
        canvas = self.volume_bar
        canvas.delete("all")
        width, height = max(1, canvas.winfo_width()), max(1, canvas.winfo_height())
        left, right = self._d(3), max(self._d(4), width - self._d(4))
        y = height / 2
        fraction = max(0.0, min(1.0, self._volume))
        progress = left + (right - left) * fraction
        if fraction > 0:
            canvas.create_line(left, y, progress, y, fill=self._colors["accent"], width=self._d(4), capstyle="round")
        radius = self._d(4)
        canvas.create_oval(progress - radius, y - radius, progress + radius, y + radius,
                           fill=self._colors["accent"], outline=self._colors["accent"], width=0)
        self.volume_label.configure(text=f"{round(self._volume * 100)}%")

    def _volume_at(self, x):
        width = max(1, self.volume_bar.winfo_width())
        self._volume = max(0.0, min(1.0, (x - self._d(3)) / max(1, width - self._d(7))))
        self._volume_dragging = True
        self._volume_pending_until = time.monotonic() + 1.5
        self._queue_live_control("volume", self._volume)
        self._draw_volume()

    def _volume_press(self, event):
        self._volume_at(event.x)

    def _volume_drag(self, event):
        self._volume_at(event.x)

    def _volume_end(self, _event):
        self._volume_pending_until = time.monotonic() + 1.5
        self._flush_live_control("volume", self._volume)
        self._volume_dragging = False

    def _queue_live_control(self, action, value):
        previous = self._live_control_after.get(action)
        if previous:
            try: self._root.after_cancel(previous)
            except Exception: pass
        self._live_control_after[action] = self._root.after(35, lambda a=action, v=value: self._flush_live_control(a, v))

    def _flush_live_control(self, action, value):
        pending = self._live_control_after.pop(action, None)
        if pending:
            try:
                self._root.after_cancel(pending)
            except Exception:
                pass
        self._queue_control(action, value)

    def _queue_control(self, action, value):
        with self._control_lock:
            if action in ("seek", "volume"):
                for index in range(len(self._control_pending) - 1, -1, -1):
                    if self._control_pending[index][0] == action:
                        self._control_pending[index] = (action, value)
                        break
                else:
                    self._control_pending.append((action, value))
            else:
                self._control_pending.append((action, value))
        self._control_event.set()

    def _control_worker(self):
        while not self._control_stop.is_set():
            self._control_event.wait(0.5)
            if self._control_stop.is_set():
                return
            while True:
                with self._control_lock:
                    pending = self._control_pending
                    self._control_pending = []
                    if not pending:
                        self._control_event.clear()
                        break
                for action, value in pending:
                    try:
                        self.control(action, value)
                    except Exception:
                        logging.getLogger("player").debug("Не удалось отправить команду мини-плеера: %s", action, exc_info=True)

    @staticmethod
    def _fmt(seconds):
        seconds = max(0, int(seconds or 0))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _tick(self):
        delay = 500
        try:
            delay = self._tick_once()
        except Exception:
            logging.getLogger("player").exception("Ошибка обновления мини-плеера; цикл продолжится")
        try:
            if self._root is not None and self._root.winfo_exists():
                self._root.after(delay, self._tick)
        except Exception:
            pass

    def _tick_once(self):
        if not self._visible:
            return 1500
        state = self.get_track()
        if not state:
            self.hide()
            return 1000
        cfg = self.get_settings() or {}
        mini = cfg.get("mini_player") if isinstance(cfg.get("mini_player"), dict) else cfg
        self._update_lyrics_overlay(state, cfg)
        if self.lyrics_button is not None:
            active = bool(mini.get("lyrics_enabled", True))
            if self.lyrics_button.get("active") != active:
                self.lyrics_button["active"] = active
                self._draw_button(self.lyrics_button)
        title = str(state.get("title") or "Без названия")
        artist = str(state.get("artist") or "Неизвестный исполнитель")
        if self.title_label.cget("text") != title[:64]:
            self.title_label.configure(text=title[:64])
        if self.artist_label.cget("text") != artist[:72]:
            self.artist_label.configure(text=artist[:72])
        pos = float(state.get("position") or 0)
        duration = float(state.get("duration") or 0)
        self._seek_duration = duration
        current_id = str(state.get("id") or "")
        if self._mini_style != "compact":
            if not self._seeking and time.monotonic() >= self._seek_pending_until:
                self._seek_position = min(duration, pos) if duration else 0
                self._draw_seek()
                formatted = f"{self._fmt(pos)} / {self._fmt(duration)}"
                if self.time_label.cget("text") != formatted:
                    self.time_label.configure(text=formatted)
        play_label = "⏸" if state.get("playing") else "▶"
        if self.play_button["label"] != play_label:
            self.play_button["label"] = play_label
            self._draw_button(self.play_button)
        if self._mini_style == "compact":
            active = bool(state.get("favorite", False))
            if self.favorite_button.get("active") != active:
                self.favorite_button["active"] = active
                self._draw_button(self.favorite_button)
        else:
            if not self._volume_dragging and time.monotonic() >= self._volume_pending_until:
                volume = max(0.0, min(1.0, float(state.get("volume", self._volume))))
                if abs(volume - self._volume) >= 0.001:
                    self._volume = volume
                    self._draw_volume()
            if self.shuffle_button is not None:
                active = bool(state.get("shuffle"))
                if self.shuffle_button.get("active") != active:
                    self.shuffle_button["active"] = active
                    self._draw_button(self.shuffle_button)
            if self.repeat_button is not None:
                repeat = str(state.get("repeat") or "off")
                active, label = repeat != "off", "1↻" if repeat == "one" else "↻"
                if self.repeat_button.get("active") != active or self.repeat_button["label"] != label:
                    self.repeat_button["active"] = active
                    self.repeat_button["label"] = label
                    self._draw_button(self.repeat_button)

            catalog_now = time.monotonic()
            if catalog_now - getattr(self, "_catalog_refresh_at", 0) > 15:
                self._catalog_refresh_at = catalog_now
                self._refresh_catalog_async()
            current_playlist = str(state.get("pl") or "")
            if current_playlist == self._selected_playlist:
                self._playlist_pending_until = 0.0
            elif current_playlist and current_playlist != self._selected_playlist and time.monotonic() >= self._playlist_pending_until:
                if any(item.get("key") == current_playlist for item in self._playlists):
                    self._selected_playlist = current_playlist
                    self._updating_selectors = True
                    try:
                        self._show_selected_playlist(current_id)
                    finally:
                        self._updating_selectors = False
            if current_id and current_id == self._track_pending_id:
                self._track_pending_id = ""
                self._track_pending_until = 0.0
            displayed_id = self._selected_track_id
            if not state.get("remote") and current_id and current_id != displayed_id and current_id != self._track_pending_id and time.monotonic() >= self._track_pending_until:
                self._updating_selectors = True
                try:
                    self._show_selected_playlist(current_id)
                finally:
                    self._updating_selectors = False
            if self._track_pending_id and time.monotonic() >= self._track_pending_until:
                self._track_pending_id = ""
                self._track_pending_until = 0.0
        thumbnail = str(state.get("thumbnail") or "") if state.get("remote") else ""
        cover_key = (current_id, thumbnail)
        if cover_key != self._cover_key:
            self._cover_key = cover_key
            self._track_id = current_id
            self._load_cover(current_id, thumbnail)
        return 120 if state.get("playing") else 260

    def _load_cover(self, track_id, thumbnail_url=""):
        if thumbnail_url:
            cache_key = (str(track_id or ""), str(thumbnail_url), self._mini_style, round(self._scale, 2), self._mini_theme)
            cached_photo = self._cover_photo_cache.get(cache_key)
            if cached_photo is not None:
                self._show_cover_photo(cached_photo)
                return
            if cache_key in getattr(self, "_cover_loading", set()):
                return
            from urllib.parse import urlparse
            parsed = urlparse(str(thumbnail_url))
            host = (parsed.hostname or "").lower().rstrip(".")
            allowed = ("ytimg.com", "youtube.com", "googleusercontent.com", "sndcdn.com", "soundcloud.com")
            if parsed.scheme != "https" or not any(host == domain or host.endswith("." + domain) for domain in allowed):
                thumbnail_url = ""
            else:
                if not hasattr(self, "_cover_loading"):
                    self._cover_loading = set()
                self._cover_loading.add(cache_key)

                def fetch_cover():
                    image_data = None
                    try:
                        from urllib.request import Request, urlopen
                        request = Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0 LAST FREQUENCY"})
                        with urlopen(request, timeout=8) as response:
                            image_data = response.read(5_000_000)
                    except Exception:
                        pass

                    def apply_cover():
                        self._cover_loading.discard(cache_key)
                        if image_data and self._cover_key == (str(track_id or ""), str(thumbnail_url)):
                            photo = self._photo_from_bytes(image_data)
                            if photo is not None:
                                self._cover_photo_cache[cache_key] = photo
                                self._show_cover_photo(photo)
                    try:
                        if self._root is not None and self._root.winfo_exists():
                            self._root.after(0, apply_cover)
                    except Exception:
                        pass

                threading.Thread(target=fetch_cover, name="mini-online-cover", daemon=True).start()
                return
        try:
            cache_key = (str(track_id or ""), "local", self._mini_style, round(self._scale, 2), self._mini_theme)
            cached_photo = self._cover_photo_cache.get(cache_key)
            if cached_photo is not None:
                self._show_cover_photo(cached_photo)
                return
            image_data = self.get_cover(track_id) if track_id else None
            if image_data:
                photo = self._photo_from_bytes(image_data)
                if photo is not None:
                    self._cover_photo_cache[cache_key] = photo
                    self._show_cover_photo(photo)
                    return
        except Exception:
            logging.getLogger("player").debug("Не удалось показать обложку в мини-плеере", exc_info=True)
        self._photo = None
        if self._mini_style == "compact":
            self.cover.configure(image="", text="♫", width=4, height=2,
                                 font=("Segoe UI Symbol", self._d(14)))
        else:
            self.cover.configure(image="", text="♫", width=4, height=2)

    def _photo_from_bytes(self, image_data):
        try:
            from PIL import Image, ImageDraw, ImageTk, ImageOps
            source = Image.open(io.BytesIO(image_data)).convert("RGBA")
            if self._mini_theme == "maretu":
                source = ImageOps.grayscale(source).convert("RGBA")
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            cover_size = self._d(34 if self._mini_style == "compact" else 76)
            image = ImageOps.fit(source, (cover_size, cover_size), method=resampling)
            image_mask = Image.new("L", image.size, 0)
            ImageDraw.Draw(image_mask).rounded_rectangle(
                (0, 0, image.width - 1, image.height - 1),
                radius=min(image.width // 6, self._d(4 if self._mini_style == "compact" else 10)), fill=255)
            image.putalpha(image_mask)
            return ImageTk.PhotoImage(image)
        except Exception:
            logging.getLogger("player").debug("Не удалось разобрать обложку мини-плеера", exc_info=True)
            return None

    def _show_cover_photo(self, photo):
        self._photo = photo
        cover_size = self._d(34 if self._mini_style == "compact" else 76)
        self.cover.configure(image=photo, text="", width=cover_size, height=cover_size)
