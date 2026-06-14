import sys
import os
import threading
import subprocess
import tkinter
from tkinter import filedialog
from tkinterdnd2 import TkinterDnD, DND_FILES
import customtkinter as ctk

from backend.converter import convert_file, convert_folder
from backend.combiner import combine_folder

# Setup Theme
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        self.title("AI DocPrep")
        self.geometry("700x700")
        
        if sys.platform.startswith("win"):
            try:
                self.iconbitmap("icon.ico")
            except:
                pass
        
        # Default Settings
        self.open_folder_var = ctk.BooleanVar(value=True)
        self.overwrite_var = ctk.StringVar(value="overwrite")
        self.yaml_var = ctk.BooleanVar(value=True)
        self.toc_var = ctk.BooleanVar(value=True)
        self.only_combined_var = ctk.BooleanVar(value=True)
        self.redact_var = ctk.BooleanVar(value=False)
        self.redact_mode_var = ctk.StringVar(value="Regex Only")
        self.ollama_model_var = ctk.StringVar(value="llama3")
        self.custom_prompt = (
            "You are an offline PII redaction assistant. Your task is to redact all personally identifiable information (PII) "
            "including names of people, organizations, locations, addresses, and any credentials from the user's text.\n"
            "Replace names with [REDACTED_NAME], organizations with [REDACTED_ORG], locations/addresses with [REDACTED_LOCATION].\n"
            "Keep all other text, punctuation, and markdown formatting exactly the same. Do not summarize the text. "
            "Do not add any conversational response, explanations, introduction, or markdown block wrapping. Return ONLY the redacted text."
        )
        self.custom_terms = ""
        self.cancel_flag = False
        
        # Browse / Ingestion settings
        self.browse_mode_var = ctk.StringVar(value="File Mode")
        self.selected_path = None
        self.combine_var = ctk.BooleanVar(value=True)
        self.is_converting = False

        # Main layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1) # Log textbox gets the extra space

        self.setup_header()
        self.setup_main_card()
        self.setup_action_button()
        self.setup_log_area()
        self.setup_menu()
        
        # Check sys.argv for CLI run
        if len(sys.argv) > 1:
            path = sys.argv[1]
            if os.path.exists(path):
                self.selected_path = path
                if os.path.isdir(path):
                    self.browse_mode_var.set("Folder Mode")
                else:
                    self.browse_mode_var.set("File Mode")
                self.update_mode_visibility()
                self.update_drop_zone_view()
                self.run_conversion()

    def setup_menu(self):
        # Create menu bar
        menu_bar = tkinter.Menu(self)
        
        accel_key = "Cmd+" if sys.platform == "darwin" else "Ctrl+"
        
        # File Menu
        file_menu = tkinter.Menu(menu_bar, tearoff=0)
        file_menu.add_command(label="Browse File...", command=self.browse_file)
        file_menu.add_command(label="Browse Folder...", command=self.browse_folder)
        
        # Edit Menu (CRITICAL for macOS Copy/Paste)
        edit_menu = tkinter.Menu(menu_bar, tearoff=0)
        edit_menu.add_command(label="Undo", accelerator=f"{accel_key}Z", command=lambda: self.focus_get().event_generate("<<Undo>>"))
        edit_menu.add_command(label="Redo", accelerator=f"Shift+{accel_key}Z", command=lambda: self.focus_get().event_generate("<<Redo>>"))
        edit_menu.add_separator()
        edit_menu.add_command(label="Cut", accelerator=f"{accel_key}X", command=lambda: self.focus_get().event_generate("<<Cut>>"))
        edit_menu.add_command(label="Copy", accelerator=f"{accel_key}C", command=lambda: self.focus_get().event_generate("<<Copy>>"))
        edit_menu.add_command(label="Paste", accelerator=f"{accel_key}V", command=lambda: self.focus_get().event_generate("<<Paste>>"))
        edit_menu.add_command(label="Select All", accelerator=f"{accel_key}A", command=lambda: self.focus_get().event_generate("<<SelectAll>>"))
        
        # Help/About Menu
        help_menu = tkinter.Menu(menu_bar, tearoff=0)
        help_menu.add_command(label="About AI DocPrep", command=self.show_about)
        
        if sys.platform == "darwin":
            # On macOS, App Menu goes first
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
            # On Windows/Linux, standard menu order: File, Edit, Help
            file_menu.add_separator()
            file_menu.add_command(label="Settings...", command=self.open_settings)
            file_menu.add_separator()
            file_menu.add_command(label="Exit", command=self.quit)
            menu_bar.add_cascade(label="File", menu=file_menu)
            menu_bar.add_cascade(label="Edit", menu=edit_menu)
            menu_bar.add_cascade(label="Help", menu=help_menu)
            
        self.config(menu=menu_bar)

    def show_about(self):
        from tkinter import messagebox
        messagebox.showinfo("About AI DocPrep", "AI DocPrep\nVersion 1.1.0\n\nA local utility to convert office files into clean, token-efficient Markdown for LLMs.")

    def setup_header(self):
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)
        
        # Title and Version Info container
        title_container = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_container.grid(row=0, column=0, sticky="w")
        
        title_label = ctk.CTkLabel(title_container, text="AI DocPrep", font=ctk.CTkFont(size=24, weight="bold"))
        title_label.pack(side="left")
        
        version_label = ctk.CTkLabel(title_container, text="v1.1.0", font=ctk.CTkFont(size=11), text_color="gray")
        version_label.pack(side="left", padx=(10, 0), pady=(5, 0))
        
        # Status indicator container
        self.status_indicator = ctk.CTkFrame(title_container, width=10, height=10, corner_radius=5, fg_color="#2ecc71") # Green dot initially
        self.status_indicator.pack(side="left", padx=(15, 0), pady=(7, 0))
        
        self.status_label = ctk.CTkLabel(header_frame, text="System Ready", font=ctk.CTkFont(size=12, slant="italic"), text_color="gray")
        self.status_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        
        # Sleek settings button (circular style cog)
        settings_btn = ctk.CTkButton(
            header_frame, 
            text="⚙️", 
            width=32, 
            height=32,
            corner_radius=16,
            fg_color=("gray90", "gray20"), 
            hover_color=("gray80", "gray30"),
            text_color=("black", "white"), 
            font=ctk.CTkFont(size=16),
            command=self.open_settings
        )
        settings_btn.grid(row=0, column=1, rowspan=2, sticky="e")

    def setup_main_card(self):
        self.main_card = ctk.CTkFrame(self, fg_color="transparent")
        self.main_card.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        self.main_card.grid_columnconfigure(0, weight=1)
        
        # Segmented button for File / Folder Selection Mode
        self.mode_selector = ctk.CTkSegmentedButton(
            self.main_card, 
            values=["File Mode", "Folder Mode"], 
            variable=self.browse_mode_var,
            command=self.on_mode_selector_changed
        )
        self.mode_selector.grid(row=0, column=0, pady=(0, 10), sticky="ew")
        
        # Large Drag & Drop Landing Zone Card
        self.drop_zone = ctk.CTkFrame(self.main_card, height=130, corner_radius=12, border_width=1, border_color=("gray80", "gray20"))
        self.drop_zone.grid(row=1, column=0, pady=5, sticky="ew")
        self.drop_zone.grid_propagate(False) # Keep the height fixed
        
        # Folder combining switches (only visible in folder mode)
        self.combine_switch = ctk.CTkSwitch(
            self.main_card, 
            text="Combine folder output into single master file", 
            variable=self.combine_var, 
            command=self.on_combine_changed
        )
        self.combine_switch.grid(row=2, column=0, padx=15, pady=(8, 2), sticky="w")
        
        self.delete_individuals_switch = ctk.CTkSwitch(
            self.main_card, 
            text="Only keep combined file (Delete individual files)", 
            variable=self.only_combined_var
        )
        self.delete_individuals_switch.grid(row=3, column=0, padx=35, pady=(2, 10), sticky="w")
        
        # Register Drag and Drop on the drop zone
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind('<<Drop>>', self.on_drop)
        
        # Initial views
        self.update_drop_zone_view()
        self.update_mode_visibility()

    def on_mode_selector_changed(self, value):
        self.selected_path = None # Reset selection on toggle
        self.update_mode_visibility()
        self.update_drop_zone_view()

    def update_mode_visibility(self):
        if self.browse_mode_var.get() == "Folder Mode":
            self.combine_switch.grid()
            if self.combine_var.get():
                self.delete_individuals_switch.grid()
            else:
                self.delete_individuals_switch.grid_remove()
        else:
            self.combine_switch.grid_remove()
            self.delete_individuals_switch.grid_remove()

    def update_drop_zone_view(self):
        # Clear existing widgets inside the drop zone
        for widget in self.drop_zone.winfo_children():
            widget.destroy()
            
        if not self.selected_path:
            # Show empty / landing state
            self.drop_zone.configure(fg_color=("gray95", "gray13"), border_width=1, border_color=("gray80", "gray20"))
            
            icon_label = ctk.CTkLabel(self.drop_zone, text="📥", font=ctk.CTkFont(size=36))
            icon_label.pack(pady=(20, 2))
            
            mode_text = "File" if self.browse_mode_var.get() == "File Mode" else "Folder"
            primary_label = ctk.CTkLabel(self.drop_zone, text=f"Drag & Drop a {mode_text} Here", font=ctk.CTkFont(weight="bold", size=13))
            primary_label.pack(pady=1)
            
            secondary_label = ctk.CTkLabel(self.drop_zone, text="or click anywhere to browse...", font=ctk.CTkFont(size=10), text_color="gray")
            secondary_label.pack(pady=(0, 20))
            
            # Bind click event
            for widget in [self.drop_zone, icon_label, primary_label, secondary_label]:
                widget.bind("<Button-1>", self.on_drop_zone_click)
        else:
            # Show loaded file card state
            self.drop_zone.configure(fg_color=("gray90", "gray18"), border_width=1, border_color=("gray70", "gray30"))
            
            # Top row for clear button
            clear_frame = ctk.CTkFrame(self.drop_zone, fg_color="transparent")
            clear_frame.pack(fill="x", padx=10, pady=(5, 0))
            
            clear_btn = ctk.CTkButton(
                clear_frame, 
                text="✕", 
                width=16, 
                height=16, 
                corner_radius=8,
                fg_color="transparent", 
                hover_color=("gray75", "gray25"),
                text_color="gray", 
                font=ctk.CTkFont(size=9, weight="bold"),
                command=self.clear_selection
            )
            clear_btn.pack(side="right")
            
            # Icon and text info
            card_content = ctk.CTkFrame(self.drop_zone, fg_color="transparent")
            card_content.pack(fill="both", expand=True, padx=20, pady=(0, 15))
            
            is_dir = os.path.isdir(self.selected_path)
            card_icon = "📁" if is_dir else "📄"
            
            icon_label = ctk.CTkLabel(card_content, text=card_icon, font=ctk.CTkFont(size=32))
            icon_label.pack(side="left", padx=(0, 12))
            
            text_frame = ctk.CTkFrame(card_content, fg_color="transparent")
            text_frame.pack(side="left", fill="both", expand=True)
            
            name_label = ctk.CTkLabel(text_frame, text=os.path.basename(self.selected_path), font=ctk.CTkFont(weight="bold", size=12), anchor="w")
            name_label.pack(fill="x", pady=(2, 0))
            
            # Truncate long paths to fit the layout nicely
            display_path = self.selected_path
            if len(display_path) > 50:
                display_path = display_path[:20] + "..." + display_path[-30:]
                
            path_label = ctk.CTkLabel(text_frame, text=display_path, font=ctk.CTkFont(size=10), text_color="gray", anchor="w")
            path_label.pack(fill="x")

    def on_drop_zone_click(self, event=None):
        if getattr(self, "is_converting", False): return
        if self.browse_mode_var.get() == "File Mode":
            path = filedialog.askopenfilename()
        else:
            path = filedialog.askdirectory()
            
        if path:
            self.selected_path = path
            self.update_drop_zone_view()

    def clear_selection(self):
        if getattr(self, "is_converting", False): return
        self.selected_path = None
        self.update_drop_zone_view()

    def setup_action_button(self):
        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.grid(row=2, column=0, padx=20, pady=15, sticky="ew")
        action_frame.grid_columnconfigure(0, weight=3)
        action_frame.grid_columnconfigure(1, weight=1)
        
        self.run_btn = ctk.CTkButton(action_frame, text="Run Conversion", font=ctk.CTkFont(size=18, weight="bold"), height=50, command=self.run_conversion)
        self.run_btn.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        
        self.stop_btn = ctk.CTkButton(action_frame, text="Stop", fg_color="#d9534f", hover_color="#c9302c", font=ctk.CTkFont(size=18, weight="bold"), height=50, command=self.stop_conversion)
        self.stop_btn.grid(row=0, column=1, sticky="ew")
        self.stop_btn.grid_remove() # Hide initially
        
        # Progress Bar setup
        self.progress_bar = ctk.CTkProgressBar(action_frame)
        self.progress_bar.grid(row=1, column=0, columnspan=2, pady=(15, 0), sticky="ew")
        self.progress_bar.set(0)
        self.progress_bar.grid_remove() # Hide until processing begins

    def setup_log_area(self):
        log_frame = ctk.CTkFrame(self, fg_color="transparent")
        log_frame.grid(row=3, column=0, padx=20, pady=(0, 20), sticky="nsew")
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)
        
        ctk.CTkLabel(log_frame, text="Activity Log", font=ctk.CTkFont(weight="bold", size=12)).grid(row=0, column=0, padx=5, pady=(0, 5), sticky="w")
        
        self.log_console = ctk.CTkTextbox(log_frame, fg_color=("gray85", "gray15"))
        self.log_console.grid(row=1, column=0, sticky="nsew")
        self.log_console.insert("0.0", "Waiting for files...\n")
        self.log_console.configure(state="disabled")

    def open_settings(self):
        settings_win = ctk.CTkToplevel(self)
        settings_win.title("Settings")
        settings_win.geometry("500x680")
        settings_win.attributes("-topmost", True)
        
        # Center settings window relative to parent
        settings_win.update_idletasks()
        parent_x = self.winfo_x()
        parent_y = self.winfo_y()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        x = parent_x + (parent_w - 500) // 2
        y = parent_y + (parent_h - 680) // 2
        settings_win.geometry(f"500x680+{x}+{y}")
        
        # Create Tabview
        tabview = ctk.CTkTabview(settings_win)
        tabview.pack(fill="both", expand=True, padx=15, pady=(10, 15))
        
        tab_output = tabview.add("Output Options")
        tab_privacy = tabview.add("Privacy & PII")
        
        # --- OUTPUT OPTIONS TAB ---
        scroll_output = ctk.CTkScrollableFrame(tab_output, fg_color="transparent")
        scroll_output.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Section Header: General
        ctk.CTkLabel(scroll_output, text="General Options", font=ctk.CTkFont(weight="bold", size=14)).pack(padx=10, pady=(10, 8), anchor="w")
        
        chk_open = ctk.CTkCheckBox(scroll_output, text="Open Folder on Completion", variable=self.open_folder_var)
        chk_open.pack(padx=20, pady=6, anchor="w")
        
        # Section Header: Formatting
        ctk.CTkLabel(scroll_output, text="Markdown Formatting", font=ctk.CTkFont(weight="bold", size=14)).pack(padx=10, pady=(18, 8), anchor="w")
        
        chk_yaml = ctk.CTkCheckBox(scroll_output, text="Inject YAML Frontmatter", variable=self.yaml_var)
        chk_yaml.pack(padx=20, pady=6, anchor="w")
        
        chk_toc = ctk.CTkCheckBox(scroll_output, text="Generate Table of Contents (Combined Mode)", variable=self.toc_var)
        chk_toc.pack(padx=20, pady=6, anchor="w")
        
        # Section Header: Conflict Resolution
        ctk.CTkLabel(scroll_output, text="File Conflict Resolution", font=ctk.CTkFont(weight="bold", size=14)).pack(padx=10, pady=(18, 8), anchor="w")
        
        rb_overwrite = ctk.CTkRadioButton(scroll_output, text="Overwrite existing file", variable=self.overwrite_var, value="overwrite")
        rb_overwrite.pack(padx=20, pady=6, anchor="w")
        
        rb_copy = ctk.CTkRadioButton(scroll_output, text="Create a copy (e.g., file (1).md)", variable=self.overwrite_var, value="copy")
        rb_copy.pack(padx=20, pady=6, anchor="w")
        
        # --- PRIVACY & PII TAB ---
        scroll_privacy = ctk.CTkScrollableFrame(tab_privacy, fg_color="transparent")
        scroll_privacy.pack(fill="both", expand=True, padx=5, pady=5)
        
        ctk.CTkLabel(scroll_privacy, text="PII Redaction Options", font=ctk.CTkFont(weight="bold", size=14)).pack(padx=10, pady=(10, 8), anchor="w")
        
        # Privacy Mode Checkbox
        chk_redact = ctk.CTkCheckBox(scroll_privacy, text="Enable Privacy Mode (Auto-Redact PII)", variable=self.redact_var, command=lambda: toggle_redact_widgets())
        chk_redact.pack(padx=20, pady=6, anchor="w")
        
        # Engine selection frame
        engine_frame = ctk.CTkFrame(scroll_privacy, fg_color="transparent")
        
        engine_label = ctk.CTkLabel(engine_frame, text="Engine:", font=ctk.CTkFont(size=12))
        engine_label.pack(side="left", padx=(0, 10))
        
        engine_menu = ctk.CTkOptionMenu(
            engine_frame, 
            variable=self.redact_mode_var, 
            values=["Regex Only", "Local NER (spaCy)", "Local LLM (Ollama)"],
            command=lambda choice: on_engine_changed(choice),
            width=170
        )
        engine_menu.pack(side="left")
        
        info_icon = ctk.CTkLabel(engine_frame, text="ℹ️", font=ctk.CTkFont(size=14, weight="bold"), text_color="gray", cursor="hand2")
        info_icon.pack(side="left", padx=(8, 0))
        
        def show_engine_info(event=None):
            from tkinter import messagebox
            info_text = (
                "Privacy Engines Explained:\n\n"
                "• Regex Only (Default):\n"
                "  Fastest. Scrubs standard structured patterns (emails, SSNs, CCs, passwords, private keys, APIs, IPs).\n\n"
                "• Local NER (spaCy):\n"
                "  Adds on-device AI Named Entity Recognition. Scrubs names of people (PERSON), organizations (ORG), and locations/addresses (GPE).\n\n"
                "• Local LLM (Ollama):\n"
                "  Runs local model queries on your running Ollama server. Dynamically redacts complex context-sensitive data."
            )
            messagebox.showinfo("Privacy Engine Info", info_text)
            
        info_icon.bind("<Button-1>", show_engine_info)
        
        # Engine description/helper label
        engine_help_label = ctk.CTkLabel(
            scroll_privacy, 
            text="", 
            font=ctk.CTkFont(size=11, slant="italic"), 
            text_color="gray50",
            wraplength=380,
            justify="left"
        )
        
        # Model Selection for Ollama
        model_label = ctk.CTkLabel(scroll_privacy, text="Ollama Model:", font=ctk.CTkFont(size=12))
        model_menu = ctk.CTkOptionMenu(scroll_privacy, variable=self.ollama_model_var, values=["llama3", "llama3.2", "mistral", "phi3"], width=170)
        
        # Custom Ollama Prompt Box
        prompt_label = ctk.CTkLabel(scroll_privacy, text="Custom Ollama Prompt:", font=ctk.CTkFont(size=12, weight="bold"))
        prompt_textbox = ctk.CTkTextbox(scroll_privacy, height=130, width=400, font=ctk.CTkFont(size=11))
        prompt_textbox.insert("1.0", self.custom_prompt)
        
        # Custom Terms Box
        terms_label = ctk.CTkLabel(scroll_privacy, text="Custom Terms to Redact (one per line):", font=ctk.CTkFont(size=12, weight="bold"))
        terms_textbox = ctk.CTkTextbox(scroll_privacy, height=90, width=400, font=ctk.CTkFont(size=11))
        terms_textbox.insert("1.0", self.custom_terms)
        
        help_texts = {
            "Regex Only": "Fastest. Scrubs standard structured identifiers (credentials, secrets, emails, SSNs, credit cards, private keys, IPs).",
            "Local NER (spaCy)": "Adds AI Named Entity Recognition. Offline model scrubs names (PERSON), companies (ORG), and locations (GPE).",
            "Local LLM (Ollama)": "Uses your local running Ollama models (e.g. llama3) to dynamically redact context-based sensitive names and items."
        }
        
        def on_engine_changed(choice):
            if self.redact_var.get():
                engine_help_label.configure(text=help_texts.get(choice, ""))
                engine_help_label.pack(padx=30, pady=(2, 8), anchor="w")
                
                # Custom Terms is always visible under Privacy Mode
                terms_label.pack(padx=30, pady=(8, 0), anchor="w")
                terms_textbox.pack(padx=30, pady=(2, 8), anchor="w")
                
                if choice == "Local LLM (Ollama)":
                    model_label.pack(padx=30, pady=(8, 0), anchor="w")
                    model_menu.pack(padx=30, pady=(2, 8), anchor="w")
                    self.refresh_ollama_models(model_menu)
                    
                    prompt_label.pack(padx=30, pady=(8, 0), anchor="w")
                    prompt_textbox.pack(padx=30, pady=(2, 8), anchor="w")
                else:
                    model_label.pack_forget()
                    model_menu.pack_forget()
                    prompt_label.pack_forget()
                    prompt_textbox.pack_forget()
            else:
                engine_help_label.pack_forget()
                model_label.pack_forget()
                model_menu.pack_forget()
                prompt_label.pack_forget()
                prompt_textbox.pack_forget()
                terms_label.pack_forget()
                terms_textbox.pack_forget()
                
        def toggle_redact_widgets():
            if self.redact_var.get():
                engine_frame.pack(padx=30, pady=(4, 4), fill="x", anchor="w")
                engine_menu.configure(state="normal")
                on_engine_changed(self.redact_mode_var.get())
            else:
                engine_frame.pack_forget()
                engine_menu.configure(state="disabled")
                engine_help_label.pack_forget()
                model_label.pack_forget()
                model_menu.pack_forget()
                prompt_label.pack_forget()
                prompt_textbox.pack_forget()
                terms_label.pack_forget()
                terms_textbox.pack_forget()
                
        # Initialize widget states
        toggle_redact_widgets()
        
        # Handle save when window is closed
        def on_close():
            self.custom_prompt = prompt_textbox.get("1.0", "end-1c")
            self.custom_terms = terms_textbox.get("1.0", "end-1c")
            settings_win.destroy()
            
        settings_win.protocol("WM_DELETE_WINDOW", on_close)

    def refresh_ollama_models(self, menu_widget):
        def fetch():
            import urllib.request
            import json
            try:
                req = urllib.request.Request("http://localhost:11434/api/tags")
                with urllib.request.urlopen(req, timeout=1.0) as response:
                    data = json.loads(response.read().decode())
                    models = [model["name"] for model in data.get("models", [])]
            except:
                models = []
            
            if models:
                self.after(0, lambda: menu_widget.configure(values=models))
                if self.ollama_model_var.get() not in models:
                    self.after(0, lambda: self.ollama_model_var.set(models[0]))
            else:
                self.after(0, lambda: menu_widget.configure(values=["llama3", "llama3.2", "mistral", "phi3"]))
                
        threading.Thread(target=fetch, daemon=True).start()

    def on_combine_changed(self):
        if self.combine_var.get():
            self.delete_individuals_switch.configure(state="normal")
        else:
            self.delete_individuals_switch.configure(state="disabled")

    def on_path_changed(self):
        pass

    def on_drop(self, event):
        if getattr(self, "is_converting", False): return
        path = event.data
        if path.startswith('{') and path.endswith('}'):
            path = path[1:-1]
        if os.path.exists(path):
            self.selected_path = path
            if os.path.isdir(path):
                self.browse_mode_var.set("Folder Mode")
            else:
                self.browse_mode_var.set("File Mode")
            self.update_mode_visibility()
            self.update_drop_zone_view()
        
    def browse_file(self):
        if getattr(self, "is_converting", False): return
        path = filedialog.askopenfilename()
        if path:
            self.selected_path = path
            self.browse_mode_var.set("File Mode")
            self.update_mode_visibility()
            self.update_drop_zone_view()

    def browse_folder(self):
        if getattr(self, "is_converting", False): return
        path = filedialog.askdirectory()
        if path:
            self.selected_path = path
            self.browse_mode_var.set("Folder Mode")
            self.update_mode_visibility()
            self.update_drop_zone_view()

    def log(self, message):
        def _update_log():
            self.log_console.configure(state="normal")
            if "Waiting for files...\n" in self.log_console.get("0.0", "end"):
                self.log_console.delete("0.0", "end")
            self.log_console.insert(ctk.END, message + "\n")
            self.log_console.see(ctk.END)
            self.log_console.configure(state="disabled")
        self.after(0, _update_log)


    def run_async(self, func, *args):
        self.cancel_flag = False
        self.is_converting = True
        self.run_btn.grid_remove()
        self.stop_btn.grid()
        self.stop_btn.configure(state="normal", text="Stop")
        
        self.status_indicator.configure(fg_color="#3498db") # Blue
        self.status_label.configure(text="Processing documents...")
        
        self.mode_selector.configure(state="disabled")
        self.combine_switch.configure(state="disabled")
        
        self.progress_bar.grid() 
        self.progress_bar.set(0)
        
        thread = threading.Thread(target=func, args=args)
        thread.daemon = True
        thread.start()
        
    def stop_conversion(self):
        self.cancel_flag = True
        self.log("Stopping process... (waiting for current files to finish)")
        self.stop_btn.configure(state="disabled", text="Stopping...")
        self.status_label.configure(text="Stopping process...")
        
    def update_progress(self, current, total):
        def _update():
            if total > 0:
                self.progress_bar.set(current / total)
        self.after(0, _update)

    def handle_completion(self, folder_path, cancelled=False):
        if cancelled:
            self.log("Process cancelled.")
            self.status_indicator.configure(fg_color="#e74c3c") # Red
            self.status_label.configure(text="Process Stopped")
        else:
            self.log("Process complete.")
            self.status_indicator.configure(fg_color="#2ecc71") # Green
            self.status_label.configure(text="System Ready")
            
        def _reset_ui():
            self.is_converting = False
            self.stop_btn.grid_remove()
            self.run_btn.grid()
            self.progress_bar.grid_remove() 
            self.mode_selector.configure(state="normal")
            self.combine_switch.configure(state="normal")
            
        self.after(0, _reset_ui)
        
        if not cancelled:
            open_folder = getattr(self, "open_folder_var", ctk.BooleanVar(value=True)).get()
            if open_folder and os.path.exists(folder_path):
                if sys.platform == 'win32':
                    os.startfile(folder_path)
                elif sys.platform == 'darwin':
                    subprocess.run(['open', folder_path])

    def _conversion_task(self, path):
        try:
            overwrite_setting = self.overwrite_var.get() == "overwrite"
            
            if os.path.isfile(path):
                self.log(f"Converting single file: {path}...")
                out = convert_file(
                    path, 
                    overwrite=overwrite_setting, 
                    inject_yaml=self.yaml_var.get(), 
                    redact_pii=self.redact_var.get(),
                    redact_mode=self.redact_mode_var.get(),
                    ollama_model=self.ollama_model_var.get(),
                    custom_prompt=self.custom_prompt,
                    custom_terms=self.custom_terms
                )
                self.update_progress(1, 1)
                self.log(f"Success! Output: {out}")
                self.handle_completion(os.path.dirname(out), cancelled=self.cancel_flag)
                
            elif os.path.isdir(path):
                self.log(f"Converting folder: {path} (all eligible office & text formats)...")
                
                def is_cancelled():
                    return self.cancel_flag
                    
                converted = convert_folder(
                    path, 
                    extensions=None, 
                    progress_callback=self.update_progress, 
                    cancel_check=is_cancelled, 
                    overwrite=overwrite_setting,
                    inject_yaml=self.yaml_var.get(),
                    redact_pii=self.redact_var.get(),
                    redact_mode=self.redact_mode_var.get(),
                    ollama_model=self.ollama_model_var.get(),
                    custom_prompt=self.custom_prompt,
                    custom_terms=self.custom_terms
                )
                
                if self.cancel_flag:
                    self.log(f"Stopped. Converted {len(converted)} files before stopping.")
                    self.handle_completion(path, cancelled=True)
                    return
                    
                self.log(f"Success! Converted {len(converted)} files.")
                
                if self.combine_var.get() and len(converted) > 0:
                    self.log("Combining files into master document...")
                    
                    folder_name = os.path.basename(os.path.abspath(path).rstrip(os.sep))
                    if not folder_name:
                        folder_name = "Root"
                    output_name = f"{folder_name}-combined.md"
                    
                    out = combine_folder(
                        path, 
                        output_filename=output_name,
                        overwrite=overwrite_setting, 
                        generate_toc=self.toc_var.get(), 
                        inject_yaml=self.yaml_var.get()
                    )
                    if out:
                        self.log(f"Success! Combined file: {out}")
                        
                        if self.only_combined_var.get():
                            self.log("Cleaning up individual Markdown files...")
                            cleaned = 0
                            for filepath in converted:
                                try:
                                    if os.path.exists(filepath):
                                        os.remove(filepath)
                                        cleaned += 1
                                except Exception as e:
                                    self.log(f"Failed to delete {filepath}: {e}")
                            self.log(f"Deleted {cleaned} individual files.")
                        
                self.handle_completion(path, cancelled=False)
        except Exception as e:
            self.log(f"Error: {e}")
            self.status_indicator.configure(fg_color="#e74c3c") # Red
            self.status_label.configure(text="Error occurred")
            def _reset_error():
                self.is_converting = False
                self.stop_btn.grid_remove()
                self.run_btn.grid()
                self.progress_bar.grid_remove()
                self.mode_selector.configure(state="normal")
                self.combine_switch.configure(state="normal")
            self.after(0, _reset_error)

    def run_conversion(self):
        path = self.selected_path
        if not path or not os.path.exists(path):
            self.log("Please select a valid file or folder path.")
            return
            
        self.run_async(self._conversion_task, path)

if __name__ == "__main__":
    app = App()
    app.mainloop()
