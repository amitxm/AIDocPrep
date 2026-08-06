import sys
import os
import time
import queue
import threading
import subprocess
import tkinter
from tkinter import filedialog, messagebox
from tkinterdnd2 import TkinterDnD, DND_FILES
import customtkinter as ctk

from backend.converter import convert_files, scan_folder, estimate_tokens
from backend.combiner import combine_files
from backend.settings import load_settings, save_settings

APP_VERSION = "1.2.0"

OUTPUT_MODE_LABELS = {
    "individual": "Individual .md files",
    "both": "Individual + combined file",
    "combined_only": "Combined file only",
}
LABEL_TO_OUTPUT_MODE = {v: k for k, v in OUTPUT_MODE_LABELS.items()}

CONFLICT_LABELS = {
    "keep_both": 'Keep both (adds " (1)")',
    "overwrite": "Overwrite existing file",
}
LABEL_TO_CONFLICT = {v: k for k, v in CONFLICT_LABELS.items()}

ENGINE_DESCRIPTIONS = {
    "Regex Only": "Instant. Scrubs emails, SSNs, credit cards, phone numbers, API keys and IPs.",
    "Local NER (spaCy)": "Also catches names, organizations and locations with an on-device model. Slower.",
    "Local LLM (Ollama)": "Deepest, context-aware redaction. Needs a running Ollama server.",
}

FILE_DIALOG_TYPES = [
    ("Supported documents", "*.docx *.pdf *.pptx *.xlsx *.xls *.msg *.epub *.ipynb *.vtt *.html *.htm *.jpg *.jpeg"),
    ("All files", "*.*"),
]

OFFLINE_HINT = "100% offline — files never leave this machine"

# Setup Theme
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class QueueItem:
    def __init__(self, path: str):
        self.path = path
        self.is_dir = os.path.isdir(path)
        self.files = [] if self.is_dir else [path]
        self.scanning = self.is_dir
        self.processed = 0
        self.errors = 0
        self.tokens = 0
        # Savings vs raw source, over files where a baseline exists
        self.src_tokens = 0
        self.out_comparable = 0
        self.row = None
        self.meta_label = None
        self.status_label = None
        self.remove_btn = None

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path.rstrip(os.sep)) or self.path

    @property
    def badge(self) -> str:
        if self.is_dir:
            return "FOLDER"
        return os.path.splitext(self.path)[1].lstrip(".").upper() or "FILE"


class App(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self):
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        if sys.platform == "darwin":
            self.font_family = "SF Pro Text"
        elif sys.platform.startswith("win"):
            self.font_family = "Segoe UI"
        else:
            self.font_family = "Arial"

        self.accent_color = ("#007AFF", "#0A84FF")
        self.accent_hover = ("#0062CC", "#0072E3")
        self.card_bg = ("#FFFFFF", "#2C2C2E")
        self.window_bg = ("#F2F2F7", "#1E1E1E")
        self.success_color = ("#1B7A3D", "#4CC38A")
        self.error_color = ("#C0392B", "#E5484D")

        self.title("AI DocPrep")
        self.geometry("660x720")
        self.minsize(560, 600)
        self.configure(fg_color=self.window_bg)

        if sys.platform.startswith("win"):
            try:
                self.iconbitmap(resource_path("icon.ico"))
            except Exception:
                pass

        self.settings = load_settings()

        # Worker threads never touch Tk directly: they enqueue callables that
        # the main thread drains on a timer (Tk is not thread-safe).
        self.ui_queue: "queue.Queue" = queue.Queue()
        self.after(50, self._drain_ui_queue)

        # UI state: "empty" | "staged" | "running" | "done"
        self.state_name = "empty"
        self.items: list[QueueItem] = []
        self.cancel_flag = False
        self.pending_scans = 0
        self.auto_run = False
        self.log_lines: list[str] = []
        self.log_win = None
        self.log_textbox = None
        self.settings_win = None
        self.result_outputs: list[str] = []
        self.result_dir = None
        self.result_combined = None

        self.output_mode_var = ctk.StringVar(value=OUTPUT_MODE_LABELS[self.settings["output_mode"]])
        self.redact_var = ctk.BooleanVar(value=self.settings["redact"])

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.setup_header()
        self.setup_content()
        self.setup_options_bar()
        self.setup_action_bar()
        self.setup_footer()
        self.setup_menu()

        # Drops are accepted anywhere on the window
        self.register_drop_tree(self)

        accel = "Command" if sys.platform == "darwin" else "Control"
        self.bind_all(f"<{accel}-o>", lambda e: self.browse_files())

        self.apply_state()

        # Launched from the OS context menu with paths: queue them and start
        cli_paths = [p for p in sys.argv[1:] if os.path.exists(p)]
        if cli_paths:
            self.auto_run = True
            self.add_paths(cli_paths)

    # ------------------------------------------------------------- UI setup

    def ui(self, fn):
        """Schedules fn to run on the Tk main thread. Safe from any thread."""
        self.ui_queue.put(fn)

    def _drain_ui_queue(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        try:
            self.after(50, self._drain_ui_queue)
        except Exception:
            pass

    def font(self, size=12, weight="normal", slant="roman"):
        return ctk.CTkFont(family=self.font_family, size=size, weight=weight, slant=slant)

    def setup_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_row, text="AI DocPrep", font=self.font(22, "bold")).pack(side="left")
        ctk.CTkLabel(title_row, text=f"v{APP_VERSION}", font=self.font(11), text_color="gray").pack(side="left", padx=(8, 0), pady=(6, 0))

        self.settings_btn = ctk.CTkButton(
            header, text="⚙", width=32, height=32, corner_radius=16,
            fg_color=("gray90", "gray20"), hover_color=("gray80", "gray30"),
            text_color=("black", "white"), font=self.font(15),
            command=self.open_settings,
        )
        self.settings_btn.grid(row=0, column=1, sticky="e")

    def setup_content(self):
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=1, column=0, padx=20, pady=6, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        # --- Empty state: big drop target ---
        self.empty_card = ctk.CTkFrame(
            self.content, corner_radius=10, border_width=1,
            border_color=("gray80", "gray30"), fg_color=self.card_bg,
        )
        self.empty_card.grid(row=0, column=0, sticky="nsew")

        inner = ctk.CTkFrame(self.empty_card, fg_color="transparent")
        inner.pack(expand=True)

        drop_icon = ctk.CTkLabel(inner, text="↓", font=self.font(40), text_color="gray")
        drop_icon.pack()
        drop_title = ctk.CTkLabel(inner, text="Drop files or folders here", font=self.font(16, "bold"))
        drop_title.pack(pady=(4, 2))
        drop_sub = ctk.CTkLabel(inner, text="Converted to clean, token-efficient Markdown", font=self.font(12), text_color="gray")
        drop_sub.pack(pady=(0, 14))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack()
        ctk.CTkButton(btn_row, text="Choose Files…", width=130, font=self.font(12),
                      fg_color=self.accent_color, hover_color=self.accent_hover,
                      command=self.browse_files).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btn_row, text="Choose Folder…", width=130, font=self.font(12),
                      fg_color=("gray85", "gray28"), hover_color=("gray75", "gray35"),
                      text_color=("black", "white"),
                      command=self.browse_folder).pack(side="left")

        chips = ctk.CTkFrame(inner, fg_color="transparent")
        chips.pack(pady=(18, 0))
        for fmt in ("PDF", "DOCX", "PPTX", "XLSX", "XLS", "MSG", "EPUB", "IPYNB", "HTML", "VTT"):
            ctk.CTkLabel(chips, text=f" {fmt} ", font=self.font(10),
                         fg_color=("gray88", "gray25"), corner_radius=6,
                         text_color=("gray30", "gray70")).pack(side="left", padx=3)

        for widget in (self.empty_card, inner, drop_icon, drop_title, drop_sub):
            widget.bind("<Button-1>", lambda e: self.browse_files())

        # --- Queue / results state ---
        self.queue_frame = ctk.CTkFrame(self.content, fg_color="transparent")
        self.queue_frame.grid(row=0, column=0, sticky="nsew")
        self.queue_frame.grid_columnconfigure(0, weight=1)
        self.queue_frame.grid_rowconfigure(2, weight=1)

        self.summary_banner = ctk.CTkLabel(
            self.queue_frame, text="", font=self.font(13, "bold"),
            corner_radius=8, fg_color=("#DFF2E1", "#173B23"),
            text_color=self.success_color, anchor="w", padx=14, height=40,
        )
        self.summary_banner.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.summary_banner.grid_remove()

        list_header = ctk.CTkFrame(self.queue_frame, fg_color="transparent")
        list_header.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        list_header.grid_columnconfigure(0, weight=1)

        self.count_label = ctk.CTkLabel(list_header, text="", font=self.font(12), text_color="gray", anchor="w")
        self.count_label.grid(row=0, column=0, sticky="w")

        header_btns = ctk.CTkFrame(list_header, fg_color="transparent")
        header_btns.grid(row=0, column=1, sticky="e")
        btn_style = dict(height=26, corner_radius=6, font=self.font(11),
                         fg_color=("gray88", "gray25"), hover_color=("gray78", "gray32"),
                         text_color=("black", "white"))
        self.add_files_btn = ctk.CTkButton(header_btns, text="+ Files", width=64, command=self.browse_files, **btn_style)
        self.add_files_btn.pack(side="left", padx=(0, 6))
        self.add_folder_btn = ctk.CTkButton(header_btns, text="+ Folder", width=70, command=self.browse_folder, **btn_style)
        self.add_folder_btn.pack(side="left", padx=(0, 6))
        self.clear_btn = ctk.CTkButton(header_btns, text="Clear", width=54, command=self.new_conversion, **btn_style)
        self.clear_btn.pack(side="left")

        self.list_frame = ctk.CTkScrollableFrame(
            self.queue_frame, fg_color=self.card_bg, corner_radius=10,
            border_width=1, border_color=("gray85", "gray28"),
        )
        self.list_frame.grid(row=2, column=0, sticky="nsew")

    def setup_options_bar(self):
        self.options_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.options_bar.grid(row=2, column=0, padx=20, pady=(8, 0), sticky="ew")
        self.options_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.options_bar, text="Output", font=self.font(12), text_color="gray").grid(row=0, column=0, padx=(2, 8))
        self.output_menu = ctk.CTkOptionMenu(
            self.options_bar, variable=self.output_mode_var,
            values=list(OUTPUT_MODE_LABELS.values()), width=220, font=self.font(12),
            fg_color=("gray88", "gray25"), button_color=("gray80", "gray32"),
            button_hover_color=("gray70", "gray38"), text_color=("black", "white"),
            dropdown_font=self.font(12), command=self.on_output_mode_changed,
        )
        self.output_menu.grid(row=0, column=1, sticky="w")

        self.redact_check = ctk.CTkCheckBox(
            self.options_bar, text="Redact PII", variable=self.redact_var,
            font=self.font(12), fg_color=self.accent_color, hover_color=self.accent_hover,
            command=self.on_redact_changed,
        )
        self.redact_check.grid(row=0, column=2, sticky="e")

    def setup_action_bar(self):
        self.action_bar = ctk.CTkFrame(self, fg_color="transparent")
        self.action_bar.grid(row=3, column=0, padx=20, pady=(12, 4), sticky="ew")
        self.action_bar.grid_columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(self.action_bar, progress_color=self.accent_color)
        self.progress_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.progress_bar.set(0)
        self.progress_bar.grid_remove()

        self.convert_btn = ctk.CTkButton(
            self.action_bar, text="Convert", height=46, corner_radius=8,
            font=self.font(15, "bold"), fg_color=self.accent_color,
            hover_color=self.accent_hover, command=self.run_conversion,
        )
        self.convert_btn.grid(row=1, column=0, sticky="ew")

        self.cancel_btn = ctk.CTkButton(
            self.action_bar, text="Cancel", height=46, corner_radius=8,
            font=self.font(15, "bold"), fg_color=("#d9534f", "#e05d58"),
            hover_color=("#c9302c", "#c22b27"), command=self.cancel_conversion,
        )
        self.cancel_btn.grid(row=1, column=0, sticky="ew")
        self.cancel_btn.grid_remove()

        self.done_actions = ctk.CTkFrame(self.action_bar, fg_color="transparent")
        self.done_actions.grid(row=1, column=0, sticky="ew")
        self.done_actions.grid_columnconfigure(2, weight=1)
        reveal_text = "Show in Finder" if sys.platform == "darwin" else "Show in Explorer"
        secondary = dict(height=38, corner_radius=8, font=self.font(12),
                         fg_color=("gray85", "gray28"), hover_color=("gray75", "gray35"),
                         text_color=("black", "white"))
        self.reveal_btn = ctk.CTkButton(self.done_actions, text=reveal_text, width=140,
                                        command=self.open_result_folder, **secondary)
        self.reveal_btn.grid(row=0, column=0, padx=(0, 8))
        self.copy_btn = ctk.CTkButton(self.done_actions, text="Copy Markdown", width=140,
                                      command=self.copy_markdown, **secondary)
        self.copy_btn.grid(row=0, column=1)
        self.new_btn = ctk.CTkButton(self.done_actions, text="New Conversion", width=150, height=38,
                                     corner_radius=8, font=self.font(12, "bold"),
                                     fg_color=self.accent_color, hover_color=self.accent_hover,
                                     command=self.new_conversion)
        self.new_btn.grid(row=0, column=3, sticky="e")
        self.done_actions.grid_remove()

    def setup_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, padx=22, pady=(2, 12), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.footer_label = ctk.CTkLabel(footer, text=OFFLINE_HINT, font=self.font(11), text_color="gray", anchor="w")
        self.footer_label.grid(row=0, column=0, sticky="w")

    def setup_menu(self):
        menu_bar = tkinter.Menu(self)
        accel_key = "Cmd+" if sys.platform == "darwin" else "Ctrl+"

        file_menu = tkinter.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="Add Files...", accelerator=f"{accel_key}O", command=self.browse_files)
        file_menu.add_command(label="Add Folder...", command=self.browse_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Activity Log", command=self.open_log)

        edit_menu = tkinter.Menu(menu_bar, tearoff=0)
        edit_menu.add_command(label="Undo", accelerator=f"{accel_key}Z", command=lambda: self.focus_get().event_generate("<<Undo>>"))
        edit_menu.add_command(label="Redo", accelerator=f"Shift+{accel_key}Z", command=lambda: self.focus_get().event_generate("<<Redo>>"))
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator=f"{accel_key}X", command=lambda: self.focus_get().event_generate("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator=f"{accel_key}C", command=lambda: self.focus_get().event_generate("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator=f"{accel_key}V", command=lambda: self.focus_get().event_generate("<<Paste>>"))
        edit_menu.add_command(label="Select All", accelerator=f"{accel_key}A", command=lambda: self.focus_get().event_generate("<<SelectAll>>"))

        help_menu = tkinter.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="About AI DocPrep", command=self.show_about)

        if sys.platform == "darwin":
            app_menu = tkinter.Menu(menu_bar, tearoff=0)
            app_menu.add_command(label="About AI DocPrep", command=self.show_about)
            app_menu.add_separator()
            app_menu.add_command(label="Preferences...", command=self.open_settings)
            app_menu.add_separator()
            app_menu.add_command(label="Quit AI DocPrep", command=self.quit)
            menu_bar.add_cascade(label="AI DocPrep", menu=app_menu)

            file_menu.add_separator()
            file_menu.add_command(label="Close Window", command=self.withdraw)
            menu_bar.add_cascade(label="File", menu=file_menu)
            menu_bar.add_cascade(label="Edit", menu=edit_menu)
        else:
            file_menu.add_separator()
            file_menu.add_command(label="Settings...", command=self.open_settings)
            file_menu.add_separator()
            file_menu.add_command(label="Exit", command=self.quit)
            menu_bar.add_cascade(label="File", menu=file_menu)
            menu_bar.add_cascade(label="Edit", menu=edit_menu)
            menu_bar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menu_bar)

    # ------------------------------------------------------------ state

    def apply_state(self):
        state = self.state_name
        running = state == "running"

        if state == "empty":
            self.queue_frame.grid_remove()
            self.empty_card.grid()
            self.options_bar.grid_remove()
            self.action_bar.grid_remove()
            self.footer_label.configure(text=OFFLINE_HINT)
        else:
            self.empty_card.grid_remove()
            self.queue_frame.grid()
            self.action_bar.grid()

        if state == "staged":
            self.summary_banner.grid_remove()
            self.options_bar.grid()
            self.progress_bar.grid_remove()
            self.done_actions.grid_remove()
            self.cancel_btn.grid_remove()
            self.convert_btn.grid()
            self.footer_label.configure(text=OFFLINE_HINT)
        elif state == "running":
            self.summary_banner.grid_remove()
            self.options_bar.grid()
            self.progress_bar.grid()
            self.done_actions.grid_remove()
            self.convert_btn.grid_remove()
            self.cancel_btn.grid()
            self.cancel_btn.configure(state="normal", text="Cancel")
        elif state == "done":
            self.summary_banner.grid()
            self.options_bar.grid_remove()
            self.progress_bar.grid_remove()
            self.convert_btn.grid_remove()
            self.cancel_btn.grid_remove()
            self.done_actions.grid()

        widget_state = "disabled" if running else "normal"
        for widget in (self.add_files_btn, self.add_folder_btn, self.clear_btn,
                       self.output_menu, self.redact_check):
            widget.configure(state=widget_state)
        for item in self.items:
            if item.remove_btn is not None:
                item.remove_btn.configure(state="disabled" if state in ("running", "done") else "normal")

        self.update_counts()

    def total_files(self) -> int:
        return sum(len(item.files) for item in self.items)

    def update_counts(self):
        n_items = len(self.items)
        n_files = self.total_files()
        suffix = " · scanning…" if self.pending_scans else ""
        self.count_label.configure(text=f"{n_items} item{'s' if n_items != 1 else ''} · {n_files} file{'s' if n_files != 1 else ''}{suffix}")

        if self.state_name == "staged":
            if self.pending_scans:
                self.convert_btn.configure(state="disabled", text="Scanning folders…")
            elif n_files == 0:
                self.convert_btn.configure(state="disabled", text="No supported files found")
            else:
                self.convert_btn.configure(state="normal", text=f"Convert {n_files} file{'s' if n_files != 1 else ''}")
            self.output_menu.configure(state="normal" if n_files >= 2 else "disabled")

    # ------------------------------------------------------------ queue

    def register_drop_tree(self, widget):
        """Registers a widget and all its descendants as drop targets, so a
        drop lands anywhere on the window rather than only on one zone."""
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.on_drop)
        except Exception:
            pass
        for child in widget.winfo_children():
            self.register_drop_tree(child)

    def on_drop(self, event):
        if self.state_name == "running":
            return
        paths = [p for p in self.tk.splitlist(event.data) if os.path.exists(p)]
        if paths:
            self.add_paths(paths)

    def browse_files(self):
        if self.state_name == "running":
            return
        dialog_types = FILE_DIALOG_TYPES
        if self.settings.get("zip", False):
            dialog_types = [(FILE_DIALOG_TYPES[0][0], FILE_DIALOG_TYPES[0][1] + " *.zip")] + FILE_DIALOG_TYPES[1:]
        paths = filedialog.askopenfilenames(filetypes=dialog_types)
        if paths:
            self.add_paths(list(paths))

    def browse_folder(self):
        if self.state_name == "running":
            return
        path = filedialog.askdirectory()
        if path:
            self.add_paths([path])

    def add_paths(self, paths: list[str]):
        if self.state_name == "done":
            self.reset_queue()

        existing = {item.path for item in self.items}
        for path in paths:
            path = os.path.abspath(path)
            if path in existing:
                continue
            existing.add(path)
            item = QueueItem(path)
            self.items.append(item)
            self.build_row(item)
            if item.is_dir:
                self.pending_scans += 1
                threading.Thread(target=self.scan_item, args=(item,), daemon=True).start()

        self.state_name = "staged" if self.items else "empty"
        self.apply_state()
        self.maybe_auto_run()

    def scan_item(self, item: QueueItem):
        try:
            files = scan_folder(item.path)
        except Exception as e:
            files = []
            self.log(f"Error scanning {item.path}: {e}")

        def apply():
            item.files = files
            item.scanning = False
            self.pending_scans = max(0, self.pending_scans - 1)
            if item.meta_label is not None:
                item.meta_label.configure(text=f"{len(files)} file{'s' if len(files) != 1 else ''}")
            self.update_counts()
            self.maybe_auto_run()
        self.ui(apply)

    def maybe_auto_run(self):
        if self.auto_run and not self.pending_scans and self.state_name == "staged":
            self.auto_run = False
            if self.total_files() > 0:
                self.run_conversion()

    def build_row(self, item: QueueItem):
        row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=3)
        item.row = row

        ctk.CTkLabel(row, text=item.badge, width=58, height=22, corner_radius=6,
                     font=self.font(10, "bold"), fg_color=("gray88", "gray25"),
                     text_color=("gray25", "gray75")).pack(side="left", padx=(4, 10))

        ctk.CTkLabel(row, text=item.display_name, font=self.font(12), anchor="w").pack(
            side="left", fill="x", expand=True)

        item.remove_btn = ctk.CTkButton(
            row, text="✕", width=22, height=22, corner_radius=11,
            fg_color="transparent", hover_color=("gray80", "gray30"),
            text_color="gray", font=self.font(10, "bold"),
            command=lambda: self.remove_item(item),
        )
        item.remove_btn.pack(side="right", padx=(6, 2))

        item.status_label = ctk.CTkLabel(row, text="", font=self.font(11), text_color="gray", anchor="e")
        item.status_label.pack(side="right", padx=(8, 0))

        if item.is_dir:
            meta_text = "scanning…" if item.scanning else f"{len(item.files)} files"
        else:
            try:
                meta_text = human_size(os.path.getsize(item.path))
            except OSError:
                meta_text = ""
        item.meta_label = ctk.CTkLabel(row, text=meta_text, font=self.font(11), text_color="gray", anchor="e")
        item.meta_label.pack(side="right", padx=(8, 0))

        self.register_drop_tree(row)

    def remove_item(self, item: QueueItem):
        if self.state_name == "running":
            return
        if item in self.items:
            self.items.remove(item)
        if item.row is not None:
            item.row.destroy()
        if item.scanning:
            self.pending_scans = max(0, self.pending_scans - 1)
            item.scanning = False
        self.state_name = "staged" if self.items else "empty"
        self.apply_state()

    def reset_queue(self):
        for item in self.items:
            if item.row is not None:
                item.row.destroy()
        self.items = []
        self.pending_scans = 0
        self.result_outputs = []
        self.result_dir = None
        self.result_combined = None
        self.progress_bar.set(0)

    def new_conversion(self):
        if self.state_name == "running":
            return
        self.reset_queue()
        self.state_name = "empty"
        self.apply_state()

    # ------------------------------------------------------------ options

    def on_output_mode_changed(self, choice):
        self.settings["output_mode"] = LABEL_TO_OUTPUT_MODE.get(choice, "both")
        save_settings(self.settings)

    def on_redact_changed(self):
        self.settings["redact"] = bool(self.redact_var.get())
        save_settings(self.settings)

    # ------------------------------------------------------------ conversion

    def run_conversion(self):
        if self.state_name != "staged" or self.pending_scans or self.total_files() == 0:
            return
        self.cancel_flag = False
        self.state_name = "running"
        self.progress_bar.set(0)
        for item in self.items:
            item.processed = 0
            item.errors = 0
            item.tokens = 0
            item.src_tokens = 0
            item.out_comparable = 0
            if item.status_label is not None:
                item.status_label.configure(text="queued", text_color="gray")
        self.apply_state()
        threading.Thread(target=self._conversion_task, daemon=True).start()

    def cancel_conversion(self):
        self.cancel_flag = True
        self.cancel_btn.configure(state="disabled", text="Cancelling…")
        self.footer_label.configure(text="Finishing files already in progress…")
        self.log("Cancelling — waiting for in-progress files to finish.")

    def update_item_row(self, item: QueueItem):
        if item.status_label is None:
            return
        total = len(item.files)
        if item.processed < total:
            item.status_label.configure(text=f"{item.processed}/{total}", text_color="gray")
        elif item.errors and item.errors == total:
            item.status_label.configure(text="✕ failed", text_color=self.error_color)
        elif item.errors:
            item.status_label.configure(
                text=f"✓ ~{item.tokens:,} tokens · {item.errors} failed", text_color=self.error_color)
        else:
            text = f"✓ ~{item.tokens:,} tokens"
            if item.src_tokens > item.out_comparable > 0:
                pct = min(99, round(100 * (1 - item.out_comparable / item.src_tokens)))
                if pct > 0:
                    text += f" (−{pct}%)"
            item.status_label.configure(text=text, text_color=self.success_color)

    def _conversion_task(self):
        s = dict(self.settings)
        started = time.time()

        files = []
        owner = {}
        for item in self.items:
            for f in item.files:
                files.append(f)
                owner[f] = item
        overwrite = s["conflict"] == "overwrite"

        # Tallied here on the worker thread; item state only mirrors this for
        # display and may lag behind (row updates go through the UI queue).
        stats = {"done": 0, "errors": 0, "tokens": 0, "src": 0, "out_comparable": 0}

        def progress(event):
            if event["status"] == "started":
                name = os.path.basename(event["file"])
                self.ui(lambda: self.footer_label.configure(text=f"Converting — {name}") if hasattr(self, "footer_label") else None)
                return
            stats["done"] += 1
            if event["status"] == "error":
                stats["errors"] += 1
            else:
                stats["tokens"] += event["tokens"]
                if event.get("source_tokens"):
                    stats["src"] += event["source_tokens"]
                    stats["out_comparable"] += event["tokens"]
            item = owner.get(event["file"])

            def apply():
                if item is not None:
                    item.processed += 1
                    if event["status"] == "error":
                        item.errors += 1
                    else:
                        item.tokens += event["tokens"]
                        if event.get("source_tokens"):
                            item.src_tokens += event["source_tokens"]
                            item.out_comparable += event["tokens"]
                    self.update_item_row(item)
                self.progress_bar.set(event["done"] / max(1, event["total"]))
                self.footer_label.configure(
                    text=f"Converting {event['done']} of {event['total']} — {os.path.basename(event['file'])}")
            self.ui(apply)

            if event["status"] == "error":
                self.log(f"Error: {event['file']} — {event['error']}")
            else:
                self.log(f"Converted {event['output']} (~{event['tokens']:,} tokens)")

        try:
            outputs = convert_files(
                files,
                progress_callback=progress,
                cancel_check=lambda: self.cancel_flag,
                overwrite=overwrite,
                inject_yaml=s["yaml"],
                redact_pii=s["redact"],
                redact_mode=s["redact_engine"],
                ollama_model=s["ollama_model"],
                custom_prompt=s["custom_prompt"],
                custom_terms=s["custom_terms"],
                allow_zip=s.get("zip", False),
            )
        except Exception as e:
            self.log(f"Conversion failed: {e}")
            outputs = []

        cancelled = self.cancel_flag
        base_dir = self._common_dir()
        combined_path = None
        total_tokens = stats["tokens"]

        if not cancelled and s["output_mode"] in ("both", "combined_only") and len(outputs) >= 2:
            name = os.path.basename(base_dir.rstrip(os.sep)) or "Documents"
            target = os.path.join(base_dir, f"{name}-combined.md")
            try:
                combined_path = combine_files(
                    sorted(outputs), target, base_dir=base_dir, overwrite=overwrite,
                    generate_toc=s["toc"], inject_yaml=s["yaml"], collection_name=name,
                )
                with open(combined_path, "r", encoding="utf-8") as f:
                    combined_tokens = estimate_tokens(f.read())
                self.log(f"Combined file: {combined_path} (~{combined_tokens:,} tokens)")

                if s["output_mode"] == "combined_only":
                    for path in outputs:
                        try:
                            os.remove(path)
                        except OSError as e:
                            self.log(f"Could not delete {path}: {e}")
                    outputs = [combined_path]
                    total_tokens = combined_tokens
                else:
                    outputs.append(combined_path)
            except Exception as e:
                self.log(f"Error combining files: {e}")

        n_errors = stats["errors"]
        n_done = stats["done"] - n_errors
        elapsed = time.time() - started
        elapsed_text = f"{elapsed:.1f}s" if elapsed < 10 else f"{elapsed:.0f}s"

        self.result_outputs = outputs
        self.result_dir = base_dir
        self.result_combined = combined_path

        def finish():
            self.state_name = "done"
            if cancelled:
                text = f"Stopped — {n_done} of {len(files)} files converted"
                colors = (("#FDF0DB", "#3B2E14"), ("#92400E", "#F5C544"))
            elif n_errors:
                text = f"⚠  {n_done} converted · {n_errors} failed · ~{total_tokens:,} tokens"
                colors = (("#FDF0DB", "#3B2E14"), ("#92400E", "#F5C544"))
            else:
                text = f"✓  {n_done} file{'s' if n_done != 1 else ''} converted in {elapsed_text} · ~{total_tokens:,} tokens total"
                if stats["src"] > stats["out_comparable"] > 0:
                    pct = min(99, round(100 * (1 - stats["out_comparable"] / stats["src"])))
                    if pct > 0:
                        text += f" · {pct}% saved vs raw"
                colors = (("#DFF2E1", "#173B23"), self.success_color)
            self.summary_banner.configure(text=text, fg_color=colors[0], text_color=colors[1])
            self.footer_label.configure(text=self.result_dir or "")
            self.apply_state()
        self.ui(finish)

        if not cancelled and outputs and s["open_folder"]:
            self._open_folder(base_dir)

    def _common_dir(self) -> str:
        dirs = []
        for item in self.items:
            dirs.append(item.path if item.is_dir else os.path.dirname(item.path))
        if not dirs:
            return os.path.expanduser("~")
        try:
            common = os.path.commonpath([os.path.abspath(d) for d in dirs])
            if os.path.isdir(common):
                return common
        except ValueError:
            pass
        return dirs[0]

    def _open_folder(self, path):
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:
            self.log(f"Could not open folder: {e}")

    def open_result_folder(self):
        self._open_folder(self.result_dir)

    def copy_markdown(self):
        try:
            if self.result_combined and os.path.exists(self.result_combined):
                with open(self.result_combined, "r", encoding="utf-8") as f:
                    text = f.read()
            else:
                parts = []
                for path in self.result_outputs:
                    if os.path.exists(path):
                        with open(path, "r", encoding="utf-8") as f:
                            parts.append(f"\n\n---\n## {os.path.basename(path)}\n\n{f.read()}")
                text = "".join(parts).lstrip()
            if not text:
                self.footer_label.configure(text="Nothing to copy")
                return
            self.clipboard_clear()
            self.clipboard_append(text)
            self.footer_label.configure(text=f"Copied ~{estimate_tokens(text):,} tokens to clipboard")
        except Exception as e:
            self.log(f"Copy failed: {e}")
            self.footer_label.configure(text="Copy failed — see activity log")

    # ------------------------------------------------------------ log

    def log(self, message: str):
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line)

        def apply():
            if self.log_textbox is not None and self.log_win is not None and self.log_win.winfo_exists():
                self.log_textbox.configure(state="normal")
                self.log_textbox.insert("end", line + "\n")
                self.log_textbox.see("end")
                self.log_textbox.configure(state="disabled")
        self.ui(apply)

    def open_log(self):
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.lift()
            self.log_win.focus_force()
            return
        self.log_win = ctk.CTkToplevel(self)
        self.log_win.title("Activity Log")
        self.log_win.geometry("620x400")
        self.log_win.configure(fg_color=self.window_bg)
        mono = "Menlo" if sys.platform == "darwin" else "Consolas"
        self.log_textbox = ctk.CTkTextbox(
            self.log_win, fg_color=self.card_bg, border_width=1,
            border_color=("gray85", "gray25"), corner_radius=8,
            font=ctk.CTkFont(family=mono, size=11),
        )
        self.log_textbox.pack(fill="both", expand=True, padx=14, pady=14)
        self.log_textbox.insert("1.0", "\n".join(self.log_lines) + ("\n" if self.log_lines else ""))
        self.log_textbox.configure(state="disabled")

    # ------------------------------------------------------------ settings

    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.settings_win = win
        win.title("Settings")
        win.configure(fg_color=self.window_bg)
        win.transient(self)

        width, height = 540, 660
        x = self.winfo_x() + (self.winfo_width() - width) // 2
        y = self.winfo_y() + (self.winfo_height() - height) // 2
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

        scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=14, pady=(12, 6))

        conflict_var = ctk.StringVar(value=CONFLICT_LABELS[self.settings["conflict"]])
        open_folder_var = ctk.BooleanVar(value=self.settings["open_folder"])
        zip_var = ctk.BooleanVar(value=self.settings.get("zip", False))
        yaml_var = ctk.BooleanVar(value=self.settings["yaml"])
        toc_var = ctk.BooleanVar(value=self.settings["toc"])
        engine_var = ctk.StringVar(value=self.settings["redact_engine"])
        ollama_model_var = ctk.StringVar(value=self.settings["ollama_model"])

        def section(title):
            ctk.CTkLabel(scroll, text=title, font=self.font(14, "bold")).pack(padx=8, pady=(16, 6), anchor="w")

        def checkbox(text, var):
            ctk.CTkCheckBox(scroll, text=text, variable=var, font=self.font(12),
                            fg_color=self.accent_color, hover_color=self.accent_hover).pack(padx=20, pady=5, anchor="w")

        # --- Output ---
        section("Output")
        conflict_row = ctk.CTkFrame(scroll, fg_color="transparent")
        conflict_row.pack(padx=20, pady=5, anchor="w", fill="x")
        ctk.CTkLabel(conflict_row, text="If a file already exists:", font=self.font(12)).pack(side="left", padx=(0, 10))
        ctk.CTkOptionMenu(conflict_row, variable=conflict_var, values=list(CONFLICT_LABELS.values()),
                          width=200, font=self.font(12), dropdown_font=self.font(12),
                          fg_color=("gray88", "gray25"), button_color=("gray80", "gray32"),
                          button_hover_color=("gray70", "gray38"),
                          text_color=("black", "white")).pack(side="left")
        checkbox("Open folder when done", open_folder_var)
        checkbox("Convert ZIP archives (contents filtered to supported formats, size-capped)", zip_var)

        # --- Markdown ---
        section("Markdown")
        checkbox("Add YAML frontmatter (Obsidian / Notion properties)", yaml_var)
        checkbox("Table of contents in combined files", toc_var)

        # --- Privacy ---
        section("Privacy & Redaction")
        ctk.CTkLabel(scroll, text="Engine used when “Redact PII” is checked in the main window:",
                     font=self.font(11), text_color="gray").pack(padx=20, pady=(0, 4), anchor="w")

        ollama_frame = ctk.CTkFrame(scroll, fg_color="transparent")

        def on_engine_changed():
            if engine_var.get() == "Local LLM (Ollama)":
                ollama_frame.pack(padx=34, pady=(2, 4), anchor="w", fill="x")
            else:
                ollama_frame.pack_forget()

        for engine, description in ENGINE_DESCRIPTIONS.items():
            ctk.CTkRadioButton(scroll, text=engine, variable=engine_var, value=engine,
                               font=self.font(12), fg_color=self.accent_color,
                               hover_color=self.accent_hover,
                               command=on_engine_changed).pack(padx=20, pady=(6, 0), anchor="w")
            ctk.CTkLabel(scroll, text=description, font=self.font(11), text_color="gray50",
                         wraplength=420, justify="left").pack(padx=44, pady=(0, 2), anchor="w")

        model_row = ctk.CTkFrame(ollama_frame, fg_color="transparent")
        model_row.pack(anchor="w", pady=(6, 4))
        ctk.CTkLabel(model_row, text="Model:", font=self.font(12)).pack(side="left", padx=(0, 8))
        model_menu = ctk.CTkOptionMenu(model_row, variable=ollama_model_var,
                                       values=["llama3", "llama3.2", "mistral", "phi3"],
                                       width=170, font=self.font(12), dropdown_font=self.font(12),
                                       fg_color=("gray88", "gray25"), button_color=("gray80", "gray32"),
                                       button_hover_color=("gray70", "gray38"),
                                       text_color=("black", "white"))
        model_menu.pack(side="left")
        self.refresh_ollama_models(model_menu, ollama_model_var)

        ctk.CTkLabel(ollama_frame, text="Custom prompt:", font=self.font(12)).pack(anchor="w", pady=(6, 2))
        prompt_box = ctk.CTkTextbox(ollama_frame, height=110, width=430, fg_color=self.card_bg,
                                    border_width=1, border_color=("gray85", "gray25"),
                                    corner_radius=6, font=self.font(11))
        prompt_box.pack(anchor="w")
        prompt_box.insert("1.0", self.settings["custom_prompt"])

        ctk.CTkLabel(scroll, text="Custom terms to always redact (one per line):",
                     font=self.font(12)).pack(padx=20, pady=(12, 2), anchor="w")
        terms_box = ctk.CTkTextbox(scroll, height=80, width=450, fg_color=self.card_bg,
                                   border_width=1, border_color=("gray85", "gray25"),
                                   corner_radius=6, font=self.font(11))
        terms_box.pack(padx=20, pady=(0, 12), anchor="w")
        terms_box.insert("1.0", self.settings["custom_terms"])

        on_engine_changed()

        def close_settings():
            self.settings["conflict"] = LABEL_TO_CONFLICT.get(conflict_var.get(), "keep_both")
            self.settings["open_folder"] = bool(open_folder_var.get())
            self.settings["zip"] = bool(zip_var.get())
            self.settings["yaml"] = bool(yaml_var.get())
            self.settings["toc"] = bool(toc_var.get())
            self.settings["redact_engine"] = engine_var.get()
            self.settings["ollama_model"] = ollama_model_var.get()
            self.settings["custom_prompt"] = prompt_box.get("1.0", "end-1c")
            self.settings["custom_terms"] = terms_box.get("1.0", "end-1c")
            save_settings(self.settings)
            win.destroy()

        done_btn = ctk.CTkButton(win, text="Done", width=100, height=32, font=self.font(12, "bold"),
                                 fg_color=self.accent_color, hover_color=self.accent_hover,
                                 command=close_settings)
        done_btn.pack(pady=(0, 12))
        win.protocol("WM_DELETE_WINDOW", close_settings)

    def refresh_ollama_models(self, menu_widget, model_var):
        def fetch():
            import urllib.request
            import json
            try:
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=1.0) as response:
                    data = json.loads(response.read().decode())
                    models = [model["name"] for model in data.get("models", [])]
            except Exception:
                models = []

            def apply():
                if not menu_widget.winfo_exists():
                    return
                if models:
                    menu_widget.configure(values=models)
                    if model_var.get() not in models:
                        model_var.set(models[0])
            self.ui(apply)

        threading.Thread(target=fetch, daemon=True).start()

    # ------------------------------------------------------------ misc

    def show_about(self):
        messagebox.showinfo(
            "About AI DocPrep",
            f"AI DocPrep\nVersion {APP_VERSION}\n\nA local utility to convert office files into clean, token-efficient Markdown for LLMs.",
        )


if __name__ == "__main__":
    app = App()
    app.mainloop()
