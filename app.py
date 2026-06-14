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
        self.cancel_flag = False
        
        # Extension toggles
        self.ext_docx_var = ctk.BooleanVar(value=True)
        self.ext_pdf_var = ctk.BooleanVar(value=True)
        self.ext_pptx_var = ctk.BooleanVar(value=True)
        self.ext_xlsx_var = ctk.BooleanVar(value=True)
        self.ext_vtt_var = ctk.BooleanVar(value=True)
        self.ext_html_var = ctk.BooleanVar(value=True)
        
        self.combine_var = ctk.BooleanVar(value=True)

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
                self.path_entry.insert(0, path)
                self.on_path_changed()
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
        
        title_label = ctk.CTkLabel(header_frame, text="AI DocPrep", font=ctk.CTkFont(size=26, weight="bold"))
        title_label.grid(row=0, column=0, sticky="w")
        
        settings_btn = ctk.CTkButton(header_frame, text="Settings ⚙️", width=40, fg_color="transparent", border_width=1, text_color=("black", "white"), command=self.open_settings)
        settings_btn.grid(row=0, column=1, sticky="e")

    def setup_main_card(self):
        card1 = ctk.CTkFrame(self)
        card1.grid(row=1, column=0, padx=20, pady=10, sticky="ew")
        card1.grid_columnconfigure(0, weight=1)
        
        ctk.CTkLabel(card1, text="What are we converting?", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=3, padx=15, pady=(15, 5), sticky="w")
        
        self.path_entry = ctk.CTkEntry(card1, placeholder_text="Drag & Drop file/folder here or Browse...")
        self.path_entry.grid(row=1, column=0, padx=(15, 5), pady=(5, 10), sticky="ew")
        
        self.path_entry.bind("<KeyRelease>", lambda event: self.on_path_changed())
        
        self.browse_file_btn = ctk.CTkButton(card1, text="Browse File", width=100, command=self.browse_file)
        self.browse_file_btn.grid(row=1, column=1, padx=(5, 5), pady=(5, 10))
        
        self.browse_folder_btn = ctk.CTkButton(card1, text="Browse Folder", width=100, command=self.browse_folder)
        self.browse_folder_btn.grid(row=1, column=2, padx=(5, 15), pady=(5, 10))
        
        self.combine_switch = ctk.CTkSwitch(card1, text="Combine folder output into single master file", variable=self.combine_var, command=self.on_combine_changed)
        self.combine_switch.grid(row=2, column=0, columnspan=3, padx=15, pady=(5, 2), sticky="w")
        
        self.delete_individuals_switch = ctk.CTkSwitch(card1, text="Only keep combined file (Delete individual files)", variable=self.only_combined_var)
        self.delete_individuals_switch.grid(row=3, column=0, columnspan=3, padx=35, pady=(2, 15), sticky="w")
        
        self.path_entry.drop_target_register(DND_FILES)
        self.path_entry.dnd_bind('<<Drop>>', self.on_drop)

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
        settings_win.geometry("420x600")
        settings_win.attributes("-topmost", True)
        
        ctk.CTkLabel(settings_win, text="General Options:", font=ctk.CTkFont(weight="bold")).pack(padx=20, pady=(15, 5), anchor="w")
        
        chk_open = ctk.CTkCheckBox(settings_win, text="Open Folder on Completion", variable=self.open_folder_var)
        chk_open.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_yaml = ctk.CTkCheckBox(settings_win, text="Inject YAML Frontmatter", variable=self.yaml_var)
        chk_yaml.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_toc = ctk.CTkCheckBox(settings_win, text="Generate Table of Contents (Combined Mode)", variable=self.toc_var)
        chk_toc.pack(padx=20, pady=(5, 5), anchor="w")
        
        # Privacy Frame / Section
        privacy_frame = ctk.CTkFrame(settings_win, fg_color="transparent")
        privacy_frame.pack(padx=20, pady=(5, 5), fill="x", anchor="w")
        
        chk_redact = ctk.CTkCheckBox(privacy_frame, text="Privacy Mode (Auto-Redact PII)", variable=self.redact_var, command=lambda: toggle_redact_widgets())
        chk_redact.pack(anchor="w")
        
        help_texts = {
            "Regex Only": "Fastest. Scrubs standard structured identifiers (credentials, secrets, emails, SSNs, credit cards, private keys, IPs).",
            "Local NER (spaCy)": "Adds AI Named Entity Recognition. Offline model scrubs names (PERSON), companies (ORG), and locations (GPE).",
            "Local LLM (Ollama)": "Uses your local running Ollama models (e.g. llama3) to dynamically redact context-based sensitive names and items."
        }

        engine_frame = ctk.CTkFrame(settings_win, fg_color="transparent")
        engine_frame.pack(padx=40, pady=(2, 2), fill="x", anchor="w")
        
        engine_label = ctk.CTkLabel(engine_frame, text="Engine:", font=ctk.CTkFont(size=11))
        engine_label.pack(side="left", padx=(0, 10))
        
        engine_menu = ctk.CTkOptionMenu(
            engine_frame, 
            variable=self.redact_mode_var, 
            values=["Regex Only", "Local NER (spaCy)", "Local LLM (Ollama)"],
            command=lambda choice: on_engine_changed(choice),
            width=150
        )
        engine_menu.pack(side="left")
        
        info_icon = ctk.CTkLabel(engine_frame, text="ℹ️", font=ctk.CTkFont(size=13, weight="bold"), text_color="gray", cursor="hand2")
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

        engine_help_label = ctk.CTkLabel(
            settings_win, 
            text="", 
            font=ctk.CTkFont(size=10, slant="italic"), 
            text_color="gray50",
            wraplength=340,
            justify="left"
        )
        engine_help_label.pack(padx=40, pady=(1, 5), anchor="w")
        
        model_label = ctk.CTkLabel(settings_win, text="Ollama Model:", font=ctk.CTkFont(size=11))
        model_menu = ctk.CTkOptionMenu(settings_win, variable=self.ollama_model_var, values=["llama3", "llama3.2", "mistral", "phi3"], width=150)
        
        def on_engine_changed(choice):
            if self.redact_var.get():
                engine_help_label.configure(text=help_texts.get(choice, ""))
                if choice == "Local LLM (Ollama)":
                    model_label.pack(padx=40, pady=(2, 0), anchor="w")
                    model_menu.pack(padx=40, pady=(2, 5), anchor="w")
                    self.refresh_ollama_models(model_menu)
                else:
                    model_label.pack_forget()
                    model_menu.pack_forget()
            else:
                engine_help_label.configure(text="")
                model_label.pack_forget()
                model_menu.pack_forget()
                
        def toggle_redact_widgets():
            if self.redact_var.get():
                engine_menu.configure(state="normal")
                on_engine_changed(self.redact_mode_var.get())
            else:
                engine_menu.configure(state="disabled")
                engine_help_label.configure(text="")
                model_label.pack_forget()
                model_menu.pack_forget()

        # Initialize widget states
        toggle_redact_widgets()
        
        ctk.CTkLabel(settings_win, text="If Markdown file already exists:", font=ctk.CTkFont(weight="bold")).pack(padx=20, pady=(10, 5), anchor="w")
        
        rb_overwrite = ctk.CTkRadioButton(settings_win, text="Overwrite existing file", variable=self.overwrite_var, value="overwrite")
        rb_overwrite.pack(padx=30, pady=5, anchor="w")
        
        rb_copy = ctk.CTkRadioButton(settings_win, text="Create a copy (e.g., file (1).md)", variable=self.overwrite_var, value="copy")
        rb_copy.pack(padx=30, pady=(5, 10), anchor="w")
        
        ctk.CTkLabel(settings_win, text="File Types to Convert (Folder Mode):", font=ctk.CTkFont(weight="bold")).pack(padx=20, pady=(10, 5), anchor="w")
        
        ext_frame1 = ctk.CTkFrame(settings_win, fg_color="transparent")
        ext_frame1.pack(padx=20, fill="x", anchor="w")
        ctk.CTkCheckBox(ext_frame1, text=".docx", variable=self.ext_docx_var).pack(side="left", padx=(0, 10), pady=5)
        ctk.CTkCheckBox(ext_frame1, text=".pdf", variable=self.ext_pdf_var).pack(side="left", padx=10, pady=5)
        ctk.CTkCheckBox(ext_frame1, text=".pptx", variable=self.ext_pptx_var).pack(side="left", padx=10, pady=5)
        
        ext_frame2 = ctk.CTkFrame(settings_win, fg_color="transparent")
        ext_frame2.pack(padx=20, fill="x", anchor="w")
        ctk.CTkCheckBox(ext_frame2, text=".xlsx", variable=self.ext_xlsx_var).pack(side="left", padx=(0, 10), pady=5)
        ctk.CTkCheckBox(ext_frame2, text=".vtt", variable=self.ext_vtt_var).pack(side="left", padx=10, pady=5)
        ctk.CTkCheckBox(ext_frame2, text=".html", variable=self.ext_html_var).pack(side="left", padx=10, pady=5)

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
        path = event.data
        if path.startswith('{') and path.endswith('}'):
            path = path[1:-1]
        self.path_entry.delete(0, ctk.END)
        self.path_entry.insert(0, path)
        self.on_path_changed()
        
    def browse_file(self):
        path = filedialog.askopenfilename()
        if path:
            self.path_entry.delete(0, ctk.END)
            self.path_entry.insert(0, path)
            self.on_path_changed()

    def browse_folder(self):
        path = filedialog.askdirectory()
        if path:
            self.path_entry.delete(0, ctk.END)
            self.path_entry.insert(0, path)
            self.on_path_changed()

    def log(self, message):
        def _update_log():
            self.log_console.configure(state="normal")
            if "Waiting for files...\n" in self.log_console.get("0.0", "end"):
                self.log_console.delete("0.0", "end")
            self.log_console.insert(ctk.END, message + "\n")
            self.log_console.see(ctk.END)
            self.log_console.configure(state="disabled")
        self.after(0, _update_log)

    def get_selected_extensions(self):
        exts = []
        if self.ext_docx_var.get(): exts.append(".docx")
        if self.ext_pdf_var.get(): exts.append(".pdf")
        if self.ext_pptx_var.get(): exts.append(".pptx")
        if self.ext_xlsx_var.get(): exts.append(".xlsx")
        if self.ext_vtt_var.get(): exts.append(".vtt")
        if self.ext_html_var.get(): exts.extend([".html", ".htm"])
        return exts

    def run_async(self, func, *args):
        self.cancel_flag = False
        self.run_btn.grid_remove()
        self.stop_btn.grid()
        self.stop_btn.configure(state="normal", text="Stop")
        
        self.browse_file_btn.configure(state="disabled")
        self.browse_folder_btn.configure(state="disabled")
        self.path_entry.configure(state="disabled")
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
        
    def update_progress(self, current, total):
        def _update():
            if total > 0:
                self.progress_bar.set(current / total)
        self.after(0, _update)

    def handle_completion(self, folder_path, cancelled=False):
        if cancelled:
            self.log("Process cancelled.")
        else:
            self.log("Process complete.")
            
        def _reset_ui():
            self.stop_btn.grid_remove()
            self.run_btn.grid()
            self.progress_bar.grid_remove() 
            self.browse_file_btn.configure(state="normal")
            self.browse_folder_btn.configure(state="normal")
            self.path_entry.configure(state="normal")
            self.on_path_changed() # Re-evaluate if switch should be enabled
            
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
                    ollama_model=self.ollama_model_var.get()
                )
                self.update_progress(1, 1)
                self.log(f"Success! Output: {out}")
                self.handle_completion(os.path.dirname(out), cancelled=self.cancel_flag)
                
            elif os.path.isdir(path):
                exts = self.get_selected_extensions()
                self.log(f"Converting folder: {path} for extensions: {exts}...")
                
                def is_cancelled():
                    return self.cancel_flag
                    
                converted = convert_folder(
                    path, 
                    exts, 
                    progress_callback=self.update_progress, 
                    cancel_check=is_cancelled, 
                    overwrite=overwrite_setting,
                    inject_yaml=self.yaml_var.get(),
                    redact_pii=self.redact_var.get(),
                    redact_mode=self.redact_mode_var.get(),
                    ollama_model=self.ollama_model_var.get()
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
            def _reset_error():
                self.stop_btn.grid_remove()
                self.run_btn.grid()
                self.progress_bar.grid_remove()
                self.browse_file_btn.configure(state="normal")
                self.browse_folder_btn.configure(state="normal")
                self.path_entry.configure(state="normal")
                self.on_path_changed()
            self.after(0, _reset_error)

    def run_conversion(self):
        path = self.path_entry.get()
        if not path or not os.path.exists(path):
            self.log("Please select a valid file or folder path.")
            return
            
        self.run_async(self._conversion_task, path)

if __name__ == "__main__":
    app = App()
    app.mainloop()
