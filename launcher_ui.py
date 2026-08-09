"""Tkinter UI for standalone DOOM Eternal Archipelago launcher."""

from __future__ import annotations

import queue
import sys
from collections.abc import Callable
from typing import Any

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from launcher_controller import LauncherController


class LauncherUI:
    """Guided Tkinter shell; worker and setup events are consumed on Tk main thread."""

    COLORS = {
        "background": "#10151d",
        "surface": "#18212c",
        "surface_alt": "#202c3a",
        "border": "#33475b",
        "text": "#edf4fb",
        "muted": "#a7b7c8",
        "accent": "#e65b31",
        "accent_active": "#ff7449",
        "success": "#52c878",
        "warning": "#f0b24a",
        "danger": "#ed6d6d",
    }

    def __init__(self, controller: LauncherController):
        self.controller = controller
        self.root = tk.Tk()
        self.root.title("DOOM Eternal Archipelago")
        self.root.geometry("1120x820")
        self.root.minsize(900, 680)
        if sys.platform.startswith("win"):
            self.root.tk.call("tk", "scaling", self._dpi_scale())
        else:
            # XWayland reports unscaled DPI on common Linux HiDPI desktops.
            self.root.tk.call("tk", "scaling", 2.0)
        self._icon: tk.PhotoImage | None = None
        self._step_widgets: list[tuple[ttk.Frame, ttk.Label, ttk.Label]] = []
        self._details_visible = False
        self._build_style()
        self._load_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.server = tk.StringVar(value=str(controller.config.get("server_address", "")))
        self.slot = tk.StringVar(value=str(controller.config.get("slot", "")))
        self.password = tk.StringVar()
        self.game_root = tk.StringVar(value=str(controller.config.get("game_root", "")))
        self.saves_root = tk.StringVar(value=str(controller.config.get("save_games_dir", "")))
        self.headline = tk.StringVar(value="Configure your game and connect to your room.")
        self.detail = tk.StringVar(value="We will prepare the mod and never launch DOOM Eternal directly.")
        self.next_action = tk.StringVar(value="Connect to Archipelago")
        self.overall_state = tk.StringVar(value="READY TO CONFIGURE")
        self.install_guidance = tk.StringVar()
        self._build()
        self._discover()
        self.root.bind("<Configure>", self._resize_text, add=True)
        self.root.after(75, self._poll_events)

    @staticmethod
    def _dpi_scale() -> float:
        if not hasattr(sys, "getwindowsversion"):
            return 1.0
        try:
            import ctypes

            windll = getattr(ctypes, "windll")
            windll.shcore.SetProcessDpiAwareness(1)
            return windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            return 1.0

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        family = "Segoe UI Variable Text" if sys.platform.startswith("win") else "Noto Sans"
        self.font_family = family
        mono_family = "Cascadia Mono" if sys.platform.startswith("win") else "Noto Sans Mono"
        default = tkfont.nametofont("TkDefaultFont")
        default.configure(family=family, size=16)
        tkfont.nametofont("TkTextFont").configure(family=family, size=16)
        tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=14)
        style.configure(".", background=self.COLORS["background"], foreground=self.COLORS["text"])
        style.configure("TFrame", background=self.COLORS["background"])
        style.configure("Surface.TFrame", background=self.COLORS["surface"])
        style.configure("Card.TFrame", background=self.COLORS["surface"], relief="flat")
        style.configure("TLabel", background=self.COLORS["background"], foreground=self.COLORS["text"], font=(family, 16))
        style.configure("Title.TLabel", font=(family, 30, "bold"), foreground=self.COLORS["text"])
        style.configure("Subtitle.TLabel", font=(family, 16), foreground=self.COLORS["muted"])
        style.configure("State.TLabel", font=(family, 14, "bold"), foreground=self.COLORS["accent_active"])
        style.configure("Section.TLabel", font=(family, 21, "bold"), background=self.COLORS["surface"])
        style.configure("CardText.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["text"], font=(family, 16))
        style.configure("Muted.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=(family, 15))
        style.configure("StepNumber.TLabel", background=self.COLORS["surface_alt"], foreground=self.COLORS["muted"], font=(family, 16, "bold"), padding=(12, 8))
        style.configure("StepTitle.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=(family, 15, "bold"))
        style.configure("StepState.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=(family, 14))
        style.configure("TEntry", fieldbackground="#f7fafc", foreground="#17212b", font=(family, 16), padding=10)
        style.configure("TButton", font=(family, 15, "bold"), padding=(18, 12))
        style.configure("Primary.TButton", background=self.COLORS["accent"], foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", self.COLORS["accent_active"]), ("disabled", "#704131")])
        style.configure("Secondary.TButton", background=self.COLORS["surface_alt"], foreground=self.COLORS["text"])
        style.map("Secondary.TButton", background=[("active", self.COLORS["border"])])
        style.configure("TProgressbar", troughcolor=self.COLORS["surface_alt"], background=self.COLORS["accent"], bordercolor=self.COLORS["surface_alt"])
        self.root.configure(background=self.COLORS["background"])

    def _load_icon(self) -> None:
        icon = self.controller.application_dir / "doom_logo.png"
        if icon.is_file():
            try:
                source = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, source)
                self._icon = source.subsample(24)
            except tk.TclError:
                self._icon = None

    def _build(self) -> None:
        self.canvas = tk.Canvas(
            self.root,
            background=self.COLORS["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        outer = ttk.Frame(self.canvas, padding=(28, 24), style="TFrame")
        self._canvas_window = self.canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self._canvas_window, width=event.width),
        )
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        header = ttk.Frame(outer, style="Surface.TFrame", padding=(22, 18))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        if self._icon is not None:
            ttk.Label(header, image=self._icon, style="CardText.TLabel").grid(row=0, column=0, rowspan=2, padx=(0, 15))
        ttk.Label(header, text="DOOM Eternal", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="Archipelago Launcher", style="Subtitle.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Label(header, textvariable=self.overall_state, style="State.TLabel").grid(row=0, column=2, rowspan=2, sticky="e")

        steps = ttk.Frame(outer, padding=(0, 18, 0, 0))
        steps.grid(row=1, column=0, sticky="ew")
        for column in range(5):
            steps.columnconfigure(column, weight=1)
        for index, (title, state) in enumerate(
            (
                ("Configure", "Game paths"),
                ("Connect", "Room details"),
                ("Prepare", "Build mod"),
                ("Install", "Apply mod"),
                ("Play", "Start via Steam"),
            ),
            start=1,
        ):
            card = ttk.Frame(steps, style="Card.TFrame", padding=10)
            card.grid(row=0, column=index - 1, sticky="ew", padx=(0 if index == 1 else 6, 0))
            number = ttk.Label(card, text=str(index), style="StepNumber.TLabel")
            number.grid(row=0, column=0, rowspan=2, padx=(0, 8))
            title_label = ttk.Label(card, text=title, style="StepTitle.TLabel")
            title_label.grid(row=0, column=1, sticky="w")
            state_label = ttk.Label(card, text=state, style="StepState.TLabel")
            state_label.grid(row=1, column=1, sticky="w")
            self._step_widgets.append((card, number, state_label))

        content = ttk.Frame(outer)
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)

        configure = ttk.Frame(content, style="Surface.TFrame", padding=20)
        configure.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        configure.columnconfigure(1, weight=1)
        ttk.Label(configure, text="1. Configure your game", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(configure, text="Confirm detected folders. You can change them before connecting.", style="Muted.TLabel", wraplength=560).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 16))
        self._path_row(configure, 2, "DOOM Eternal folder", self.game_root, self._browse_game)
        self._path_row(configure, 3, "Save folder", self.saves_root, self._browse_saves)
        ttk.Separator(configure).grid(row=4, column=0, columnspan=3, sticky="ew", pady=16)
        ttk.Label(configure, text="2. Connect to Archipelago", style="Section.TLabel").grid(row=5, column=0, columnspan=3, sticky="w")
        ttk.Label(configure, text="Use the address and slot from your room.", style="Muted.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(3, 12))
        self._entry_row(configure, 7, "Server address", self.server)
        self._entry_row(configure, 8, "Slot name", self.slot)
        self._entry_row(configure, 9, "Password", self.password, show="•")

        status = ttk.Frame(content, style="Surface.TFrame", padding=20)
        status.grid(row=0, column=1, sticky="nsew")
        ttk.Label(status, text="What happens next", style="Section.TLabel").pack(anchor="w")
        self.headline_label = ttk.Label(status, textvariable=self.headline, style="CardText.TLabel", font=(self.font_family, 22, "bold"), justify="left", wraplength=340)
        self.headline_label.pack(anchor="w", fill="x", pady=(16, 6))
        self.detail_label = ttk.Label(status, textvariable=self.detail, style="Muted.TLabel", justify="left", wraplength=340)
        self.detail_label.pack(anchor="w", fill="x")
        self.progress = ttk.Progressbar(status, mode="indeterminate")
        self.progress.pack(fill="x", pady=(20, 14))
        self.primary_button = ttk.Button(status, textvariable=self.next_action, style="Primary.TButton", command=self._primary_action)
        self.primary_button.pack(fill="x")
        self.stop_button = ttk.Button(status, text="Stop connection", style="Secondary.TButton", command=self._disconnect)
        self.stop_button.pack(fill="x", pady=(8, 0))
        ttk.Button(status, text="Show technical details", style="Secondary.TButton", command=self._toggle_details).pack(fill="x", pady=(8, 0))
        self.guidance = ttk.Frame(status, style="Card.TFrame", padding=12)
        self.guidance.pack(fill="x", pady=(16, 0))
        self.guidance.pack_forget()
        ttk.Label(self.guidance, text="Finish installation", style="Section.TLabel").pack(anchor="w")
        self.guidance_label = ttk.Label(self.guidance, textvariable=self.install_guidance, style="Muted.TLabel", justify="left", wraplength=320)
        self.guidance_label.pack(anchor="w", fill="x", pady=(6, 10))
        actions = ttk.Frame(self.guidance, style="Card.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="Yes, finish", style="Primary.TButton", command=lambda: self._confirm_windows(True)).pack(side="left", fill="x", expand=True)
        ttk.Button(actions, text="No, there was a problem", style="Secondary.TButton", command=lambda: self._confirm_windows(False)).pack(side="left", fill="x", expand=True, padx=(8, 0))

        launch = ttk.Frame(outer, style="Surface.TFrame", padding=16)
        launch.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        launch.columnconfigure(0, weight=1)
        ttk.Label(launch, text="Steam launch option", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(launch, text="Copy this on Linux; the launcher never edits Steam settings.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.launch_option = tk.Text(launch, height=2, wrap="word", relief="flat", borderwidth=0, font=tkfont.nametofont("TkTextFont"), bg="#f7fafc", fg="#17212b", padx=10, pady=8)
        self.launch_option.grid(row=2, column=0, sticky="ew")
        ttk.Button(launch, text="Copy option", style="Secondary.TButton", command=self._copy_launch_option).grid(row=2, column=1, sticky="ns", padx=(10, 0))

        self.details = ttk.Frame(outer, style="Surface.TFrame", padding=16)
        self.details.grid(row=4, column=0, sticky="nsew", pady=(14, 0))
        self.details.columnconfigure(0, weight=1)
        self.details.rowconfigure(1, weight=1)
        details_header = ttk.Frame(self.details, style="Surface.TFrame")
        details_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(details_header, text="Details and logs", style="Section.TLabel").pack(side="left")
        self.details_button = ttk.Button(details_header, text="Show details", style="Secondary.TButton", command=self._toggle_details)
        self.details_button.pack(side="right")
        self.log_container = ttk.Frame(self.details, style="Surface.TFrame")
        self.log_container.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.log_container.columnconfigure(0, weight=1)
        self.log_container.rowconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(self.log_container, height=10, state="disabled", wrap="word", font=tkfont.nametofont("TkFixedFont"), relief="flat", borderwidth=0, padx=10, pady=10)
        self.log.grid(row=0, column=0, sticky="nsew")
        command = ttk.Frame(self.log_container, style="Surface.TFrame")
        command.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        command.columnconfigure(0, weight=1)
        self.command = tk.StringVar()
        entry = ttk.Entry(command, textvariable=self.command)
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda _event: self._send_command())
        ttk.Button(command, text="Send chat", style="Secondary.TButton", command=self._send_command).grid(row=0, column=1, padx=(8, 0))
        self.log_container.grid_remove()
        self._set_step(1)

    @staticmethod
    def _entry_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, **kwargs: Any) -> None:
        ttk.Label(parent, text=label, style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable, **kwargs).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)

    @staticmethod
    def _path_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, callback: Callable[[], object]) -> None:
        ttk.Label(parent, text=label, style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=5)
        ttk.Button(parent, text="Browse", style="Secondary.TButton", command=callback).grid(row=row, column=2, pady=5)

    def _set_step(self, active: int, *, complete_through: int = 0) -> None:
        for index, (card, number, state) in enumerate(self._step_widgets, start=1):
            if index <= complete_through:
                color, text = self.COLORS["success"], "Complete"
            elif index == active:
                color, text = self.COLORS["accent_active"], "In progress"
            else:
                color, text = self.COLORS["muted"], ("Next" if index == active + 1 else "Waiting")
            card.configure(style="Card.TFrame")
            number.configure(foreground=color)
            state.configure(text=text, foreground=color)

    def _set_state(self, headline: str, detail: str, *, action: str, step: int, complete: int = 0, busy: bool = False, state: str = "IN PROGRESS") -> None:
        self.headline.set(headline)
        self.detail.set(detail)
        self.next_action.set(action)
        self.overall_state.set(state)
        self._set_step(step, complete_through=complete)
        if busy:
            self.progress.start(12)
            self.primary_button.state(["disabled"])
        else:
            self.progress.stop()
            self.primary_button.state(["!disabled"])

    def _resize_text(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self.root:
            width = max(280, int(event.width * 0.31))
            self.headline_label.configure(wraplength=width)
            self.detail_label.configure(wraplength=width)
            self.guidance_label.configure(wraplength=width)

    def _discover(self) -> None:
        found = self.controller.discover()
        if not self.game_root.get() and found.get("game_root"):
            self.game_root.set(str(found["game_root"]))
        if not self.saves_root.get() and found.get("save_games_dir"):
            self.saves_root.set(str(found["save_games_dir"]))
        if found.get("ambiguous_game_roots"):
            self._set_state("Choose your DOOM Eternal folder.", "More than one installation was found.", action="Review game folder", step=1, state="ACTION NEEDED")
        elif self.game_root.get() and self.saves_root.get():
            self._set_state("Game folders detected.", "Enter your room details, then connect.", action="Connect to Archipelago", step=1, state="READY")
        else:
            self._set_state("Choose your game folders.", "Select DOOM Eternal and its save folder before connecting.", action="Review game folders", step=1, state="ACTION NEEDED")

    def _browse_game(self) -> None:
        selected = filedialog.askdirectory(title="Select the DOOM Eternal folder")
        if selected:
            self.game_root.set(selected)

    def _browse_saves(self) -> None:
        selected = filedialog.askdirectory(title="Select the DOOM Eternal save folder")
        if selected:
            self.saves_root.set(selected)

    def _primary_action(self) -> None:
        if "Try again" in self.next_action.get():
            self._retry()
        elif "Steam" in self.next_action.get():
            self._append_log("Start DOOM Eternal through Steam when ready. The launcher will not start the game.")
        else:
            self._connect()

    def _connect(self) -> None:
        try:
            self.controller.connect(endpoint=self.server.get(), slot=self.slot.get(), password=self.password.get(), game_root=self.game_root.get(), saves_root=self.saves_root.get())
        except Exception as error:
            self._set_state("Check your connection details.", str(error), action="Try again", step=2, complete=1, state="ACTION NEEDED")
            self._append_log(f"Connection error: {error}")

    def _disconnect(self) -> None:
        try:
            self.controller.disconnect()
        except Exception as error:
            self._append_log(f"Stop error: {error}")

    def _retry(self) -> None:
        if not self.controller.retry_setup():
            self._append_log("Connect to a room before retrying setup.")

    def _confirm_windows(self, succeeded: bool) -> None:
        try:
            self.controller.confirm_windows_installation(succeeded)
        except Exception as error:
            self._append_log(f"Confirmation error: {error}")

    def _copy_launch_option(self) -> None:
        value = self.launch_option.get("1.0", "end-1c")
        if value and not value.startswith("Unavailable"):
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self._append_log("Steam launch option copied. Paste it in Steam manually.")

    def _send_command(self) -> None:
        text = self.command.get().strip()
        if not text:
            return
        try:
            self.controller.send_command(text)
            self.command.set("")
        except Exception as error:
            self._append_log(f"Chat error: {error}")

    def _toggle_details(self) -> None:
        self._details_visible = not self._details_visible
        if self._details_visible:
            self.log_container.grid()
            self.details_button.configure(text="Hide details")
            self.root.after_idle(lambda: self.canvas.yview_moveto(1.0))
        else:
            self.log_container.grid_remove()
            self.details_button.configure(text="Show details")

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.controller.events.get_nowait()
            except queue.Empty:
                break
            self.controller.process_event(event)
            self._handle_event(event)
        self.root.after(75, self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        kind = str(event.get("type", ""))
        if kind == "log":
            self._append_log(str(event.get("message", "")))
        elif kind == "warning":
            message = str(event.get("message", ""))
            self._append_log(f"Warning: {message}")
            if event.get("field") == "steam_launch_options":
                self.launch_option.delete("1.0", "end")
                self.launch_option.insert("1.0", "Unavailable — see details.")
        elif kind in {"client_started", "connecting"}:
            self._set_state("Connecting to your Archipelago room…", "The launcher is waiting for room information.", action="Connecting…", step=2, complete=1, busy=True)
        elif kind == "connected":
            self._set_state("Connected. Preparing your room.", "Your room settings decide which mod variant is installed.", action="Preparing…", step=3, complete=2, busy=True)
        elif kind == "disconnected":
            self._set_state("Connection stopped.", "You can update details and reconnect when ready.", action="Connect to Archipelago", step=2, complete=1, state="DISCONNECTED")
        elif kind == "setup_started":
            self._set_state("Preparing your mod…", "Building the mod selected by your room settings.", action="Preparing…", step=3, complete=2, busy=True)
        elif kind == "room_validated":
            self._append_log(f"Room validated. Dash randomization: {bool(event.get('randomize_dash'))}.")
        elif kind == "mod_building":
            self._set_state("Preparing your mod…", "Creating the room-specific mod package.", action="Preparing…", step=3, complete=2, busy=True)
        elif kind == "mod_staged":
            self._set_state("Mod ready. Installing next…", "The room-specific ZIP is staged safely in DOOM Eternal/Mods.", action="Installing…", step=4, complete=3, busy=True)
            self._append_log(f"Staged mod: {event.get('path', '')}")
        elif kind == "dependency_consent_required":
            request_id = str(event.get("request_id", ""))
            accepted = messagebox.askyesno("Download required tool", f"Download {event.get('name')} {event.get('version')}?\n\nThe download is verified before use.", parent=self.root)
            self.controller.resolve_consent(request_id, accepted)
            self._append_log("Tool download approved." if accepted else "Tool download declined.")
        elif kind == "dependency_ready":
            self._append_log(f"Tool ready: {event.get('name')} {event.get('version')}")
        elif kind == "injector_started":
            self._set_state("Installing the mod automatically…", "EternalModInjectorShell is working. This can take a few minutes.", action="Installing…", step=4, complete=3, busy=True)
            command = event.get("command", [])
            if isinstance(command, (list, tuple)):
                self._append_log("Running: " + " ".join(map(str, command)))
        elif kind == "injector_finished":
            stdout = str(event.get("stdout", ""))
            stderr = str(event.get("stderr", ""))
            if stdout:
                self._append_log(stdout.rstrip())
            if stderr:
                self._append_log(stderr.rstrip())
            self._append_log(f"Injector exit code: {event.get('returncode', 'timeout')}")
        elif kind == "manual_action_required":
            self.install_guidance.set("EternalModManager is open.\n\n1. Select the DOOM Eternal Archipelago mod.\n2. Choose Run Injector.\n3. Return here and confirm the result.")
            self.guidance.pack(fill="x", pady=(16, 0))
            self._set_state("Finish installation in EternalModManager.", "The manager was opened for you. Confirm when its injector is done.", action="Waiting for confirmation", step=4, complete=3, busy=False, state="ACTION NEEDED")
            self.primary_button.state(["disabled"])
        elif kind == "windows_installation_confirmed":
            if bool(event.get("succeeded")):
                self.guidance.pack_forget()
                self._set_state("Mod installed successfully.", "Now start DOOM Eternal through Steam. Keep this launcher open while you play.", action="Ready — start via Steam", step=5, complete=5, state="READY TO PLAY")
            else:
                self._set_state("Installation needs another try.", "Review the details, then retry setup after fixing the manager issue.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
                self._append_log("User reported EternalModManager installation failure.")
        elif kind == "setup_ready":
            state = str(event.get("adapter_state", ""))
            option = str(event.get("steam_launch_option", ""))
            if option:
                self.launch_option.delete("1.0", "end")
                self.launch_option.insert("1.0", option)
            if state == "applied":
                self._set_state("Mod installed successfully.", "Now start DOOM Eternal through Steam. Keep this launcher open while you play.", action="Ready — start via Steam", step=5, complete=5, state="READY TO PLAY")
            elif state in {"failed", "timed_out"}:
                self._set_state("Mod installation did not finish.", "Review details, then retry setup. DOOM Eternal was not started.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
            elif state != "manual_action_required":
                self._set_state("Setup needs your attention.", str(event.get("message", "")), action="Try again", step=4, complete=3, state="ACTION NEEDED")
        elif kind in {"setup_failed", "error"}:
            message = str(event.get("message", "Unknown error"))
            self._set_state("Setup could not finish.", "Review details and try again after resolving the issue.", action="Try again", step=4, complete=3, state="ACTION NEEDED")
            self._append_log(f"{kind}: {message}")
        elif kind == "client_stopping":
            self._set_state("Stopping connection…", "Waiting for the bridge worker to close.", action="Stopping…", step=2, complete=1, busy=True)

    def _append_log(self, text: str) -> None:
        if not text:
            return
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        try:
            self.controller.close()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
