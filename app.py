import sys
import os
import threading
import subprocess
import customtkinter as ctk
from tkinter import filedialog
from tkinterdnd2 import TkinterDnD, DND_FILES

from backend.converter import convert_file, convert_folder
from backend.combiner import combine_folder

# Setup Theme
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

class Tk(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)

class App(Tk):
    def __init__(self):
        super().__init__()

        self.title("AI DocPrep")
        self.geometry("700x700")
        
        # Default Settings
        self.open_folder_var = ctk.BooleanVar(value=True)
        self.overwrite_var = ctk.StringVar(value="overwrite")
        self.yaml_var = ctk.BooleanVar(value=True)
        self.toc_var = ctk.BooleanVar(value=True)
        self.only_combined_var = ctk.BooleanVar(value=False)
        self.redact_var = ctk.BooleanVar(value=False)
        self.cancel_flag = False
        
        # Extension toggles
        self.ext_docx_var = ctk.BooleanVar(value=True)
        self.ext_pdf_var = ctk.BooleanVar(value=True)
        self.ext_pptx_var = ctk.BooleanVar(value=True)
        self.ext_xlsx_var = ctk.BooleanVar(value=True)
        self.ext_vtt_var = ctk.BooleanVar(value=True)
        self.ext_html_var = ctk.BooleanVar(value=True)
        
        self.combine_var = ctk.BooleanVar(value=False)

        # Main layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1) # Log textbox gets the extra space

        self.setup_header()
        self.setup_main_card()
        self.setup_action_button()
        self.setup_log_area()
        
        # Check sys.argv for CLI run
        if len(sys.argv) > 1:
            path = sys.argv[1]
            if os.path.exists(path):
                self.path_entry.insert(0, path)
                self.on_path_changed()
                self.run_conversion()

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
        
        self.combine_switch = ctk.CTkSwitch(card1, text="Combine folder output into single master file", variable=self.combine_var)
        self.combine_switch.grid(row=2, column=0, columnspan=3, padx=15, pady=(5, 15), sticky="w")
        self.combine_switch.configure(state="disabled")
        
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
        settings_win.geometry("400x480")
        settings_win.attributes("-topmost", True)
        
        ctk.CTkLabel(settings_win, text="General Options:", font=ctk.CTkFont(weight="bold")).pack(padx=20, pady=(15, 5), anchor="w")
        
        chk_open = ctk.CTkCheckBox(settings_win, text="Open Folder on Completion", variable=self.open_folder_var)
        chk_open.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_yaml = ctk.CTkCheckBox(settings_win, text="Inject YAML Frontmatter", variable=self.yaml_var)
        chk_yaml.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_toc = ctk.CTkCheckBox(settings_win, text="Generate Table of Contents (Combined Mode)", variable=self.toc_var)
        chk_toc.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_only_combined = ctk.CTkCheckBox(settings_win, text="Only Keep Combined File (Delete Individuals)", variable=self.only_combined_var)
        chk_only_combined.pack(padx=20, pady=(5, 5), anchor="w")
        
        chk_redact = ctk.CTkCheckBox(settings_win, text="Privacy Mode (Auto-Redact PII)", variable=self.redact_var)
        chk_redact.pack(padx=20, pady=(5, 10), anchor="w")
        
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

    def on_path_changed(self):
        path = self.path_entry.get()
        if os.path.isdir(path):
            self.combine_switch.configure(state="normal")
        else:
            self.combine_switch.configure(state="disabled")
            self.combine_switch.deselect()

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
                out = convert_file(path, overwrite=overwrite_setting, inject_yaml=self.yaml_var.get(), redact_pii=self.redact_var.get())
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
                    redact_pii=self.redact_var.get()
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
