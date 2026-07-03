"""GUI smoke test: drives the real app window through the full
drop -> convert -> results flow. Needs a display (not headless CI).

Run with:  python tests/test_gui.py
"""
import os
import sys
import time
import shutil
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

failures = []

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)

work = tempfile.mkdtemp(prefix="docprep_gui_")
folder = os.path.join(work, "project docs")
os.makedirs(folder)
for i in range(3):
    with open(os.path.join(folder, f"doc {i}.html"), "w", encoding="utf-8") as f:
        f.write(f"<html><body><h1>Doc {i}</h1><p>{'content ' * 50}</p></body></html>")
single = os.path.join(work, "standalone.vtt")
with open(single, "w", encoding="utf-8") as f:
    f.write("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world from the meeting.\n")

import app as app_mod

a = app_mod.App()
a.settings["open_folder"] = False  # don't pop Explorer during the test
a.settings["output_mode"] = "both"
a.output_mode_var.set(app_mod.OUTPUT_MODE_LABELS["both"])

def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        a.update()
        time.sleep(0.01)

def pump_until(cond, timeout, label):
    end = time.time() + timeout
    while time.time() < end:
        a.update()
        if cond():
            return True
        time.sleep(0.01)
    print(f"  timeout waiting for: {label}")
    return False

pump(0.5)
check("initial state empty", a.state_name == "empty")

# Simulate a multi-item drop (folder + file, path with spaces)
class FakeEvent:
    data = f"{{{folder}}} {{{single}}}"
a.on_drop(FakeEvent())
check("staged after drop", a.state_name == "staged")
check("two queue items", len(a.items) == 2, f"items={len(a.items)}")

ok = pump_until(lambda: a.pending_scans == 0, 10, "folder scan")
check("folder scan finished", ok)
check("total files = 4", a.total_files() == 4, f"got {a.total_files()}")
check("convert button label", "Convert 4 files" in a.convert_btn.cget("text"), a.convert_btn.cget("text"))
check("dedupe on re-drop", (a.on_drop(FakeEvent()), len(a.items))[1] == 2)

a.run_conversion()
check("running state", a.state_name == "running")
ok = pump_until(lambda: a.state_name == "done", 90, "conversion done")
check("done state reached", ok)
pump(0.5)

banner = a.summary_banner.cget("text")
check("banner reports 4 files", "4 files converted" in banner, banner)
check("banner reports tokens", "tokens" in banner, banner)
import re as _re
banner_tokens = int(_re.search(r"~([\d,]+) tokens", banner).group(1).replace(",", ""))
row_tokens = sum(it.tokens for it in a.items)
check("banner tokens match row totals", banner_tokens == row_tokens, f"banner={banner_tokens} rows={row_tokens}")
combined_expected = a.result_combined
check("combined file created", combined_expected and os.path.exists(combined_expected), str(combined_expected))
check("result outputs exist", len(a.result_outputs) == 5 and all(os.path.exists(p) for p in a.result_outputs), f"{len(a.result_outputs)} outputs")
check("result dir is common parent", os.path.normpath(a.result_dir) == os.path.normpath(work), a.result_dir)

item_statuses = [it.status_label.cget("text") for it in a.items]
check("rows show token counts", all("tokens" in s for s in item_statuses), str(item_statuses))

a.copy_markdown()
pump(0.2)
clip = a.clipboard_get()
check("copy markdown fills clipboard", "Doc 0" in clip or "Hello world" in clip, f"len={len(clip)}")

# New conversion resets to empty
a.new_conversion()
pump(0.2)
check("reset to empty", a.state_name == "empty" and len(a.items) == 0)

# Settings window opens once (singleton) and closes saving
a.open_settings()
pump(0.3)
first_win = a.settings_win
a.open_settings()
pump(0.2)
check("settings singleton", a.settings_win is first_win)
check("settings not topmost", not bool(int(first_win.attributes("-topmost"))))
first_win.destroy()
pump(0.2)

# Activity log window
a.open_log()
pump(0.3)
log_text = a.log_textbox.get("1.0", "end")
check("log has converted lines", "Converted" in log_text)

a.destroy()
shutil.rmtree(work, ignore_errors=True)
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
