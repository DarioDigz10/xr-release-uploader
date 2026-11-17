"""Window for release.py: pick the app and version, write the release notes, upload."""

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk

import release


class UploaderWindow:
    def __init__(self, root: tk.Tk):
        release.load_env()
        self.config = release.load_config()
        self.apps = {app["name"]: key for key, app in self.config["apps"].items()}
        self.events = queue.Queue()  # the upload thread talks to the window only through this queue
        self.notes_key = None
        self.busy = False
        self.root = root

        root.title("XR Release Uploader")
        root.minsize(720, 640)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="App").grid(row=0, column=0, sticky="w", pady=2)
        self.app_var = tk.StringVar(value=next(iter(self.apps)))
        app_box = ttk.Combobox(frame, textvariable=self.app_var, values=list(self.apps), state="readonly")
        app_box.grid(row=0, column=1, sticky="w")
        app_box.bind("<<ComboboxSelected>>", lambda _: self.refresh_versions())

        ttk.Label(frame, text="Version").grid(row=1, column=0, sticky="w", pady=2)
        self.version_var = tk.StringVar()
        self.version_box = ttk.Combobox(frame, textvariable=self.version_var, state="readonly", width=12)
        self.version_box.grid(row=1, column=1, sticky="w")
        self.version_box.bind("<<ComboboxSelected>>", lambda _: self.refresh_build())

        ttk.Label(frame, text="Builds").grid(row=2, column=0, sticky="nw", pady=2)
        self.builds_label = ttk.Label(frame, justify="left")
        self.builds_label.grid(row=2, column=1, sticky="w")

        ttk.Label(frame, text="Devices").grid(row=3, column=0, sticky="w", pady=2)
        self.device_vars = {}
        self.device_box = ttk.Frame(frame)  # refilled for each app: devices come from config.toml
        self.device_box.grid(row=3, column=1, sticky="w")

        ttk.Label(frame, text="Upload to").grid(row=4, column=0, sticky="w", pady=2)
        self.destination_vars = {d: tk.BooleanVar(value=True) for d in release.DESTINATIONS}
        destination_box = ttk.Frame(frame)
        destination_box.grid(row=4, column=1, sticky="w")
        self.checkboxes(destination_box, release.DESTINATION_NAMES, self.destination_vars)

        ttk.Label(frame, text="Release notes").grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 2))
        self.notes = scrolledtext.ScrolledText(frame, height=8, wrap="word", undo=True)
        self.notes.grid(row=6, column=0, columnspan=2, sticky="nsew")

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=8)
        ttk.Button(buttons, text="Simulate", command=self.simulate).pack(side="left", padx=4)
        self.upload_button = ttk.Button(buttons, text="Upload", command=self.upload)
        self.upload_button.pack(side="left")

        ttk.Label(frame, text="Progress").grid(row=8, column=0, columnspan=2, sticky="w")
        self.log = scrolledtext.ScrolledText(frame, height=12, state="disabled", font="TkFixedFont")
        self.log.grid(row=9, column=0, columnspan=2, sticky="nsew")
        frame.rowconfigure(6, weight=1)
        frame.rowconfigure(9, weight=2)

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_versions()
        self.poll()

    @staticmethod
    def checkboxes(box, labels, variables):
        for child in box.winfo_children():
            child.destroy()
        for key, text in labels.items():
            ttk.Checkbutton(box, text=text, variable=variables[key]).pack(side="left", padx=(0, 12))

    @property
    def app_key(self) -> str:
        return self.apps[self.app_var.get()]

    # --- Selection ---------------------------------------------------------------------------

    def refresh_versions(self):
        devices = self.config["apps"][self.app_key]["devices"]
        self.device_vars = {device: tk.BooleanVar(value=True) for device in devices}
        self.checkboxes(self.device_box, {d: d.capitalize() for d in devices}, self.device_vars)
        versions = release.find_versions(self.config["apps"][self.app_key])
        self.version_box["values"] = versions
        self.version_var.set(versions[0] if versions else "")
        self.refresh_build()

    def refresh_build(self):
        self.save_notes()  # keep what was typed for the previous app or version
        app, version = self.config["apps"][self.app_key], self.version_var.get()
        lines = [f"{device.capitalize()}  {release.build_summary(app, device, version)}" for device in app["devices"]]
        self.builds_label["text"] = "\n".join(lines) if version else f"No builds found in {release.builds_dir(app)}"
        self.notes_key = (self.app_key, version) if version else None
        self.notes.delete("1.0", "end")
        if version:
            self.notes.insert("1.0", release.load_notes(self.app_key, version))

    def save_notes(self):
        if self.notes_key:
            release.save_notes(*self.notes_key, self.notes.get("1.0", "end"))

    def make_plan(self):
        self.save_notes()
        notes = self.notes.get("1.0", "end").strip()
        devices = [d for d, var in self.device_vars.items() if var.get()]
        destinations = [d for d, var in self.destination_vars.items() if var.get()]
        steps, problems = release.plan(self.config, self.app_key, self.version_var.get(), devices, destinations, notes)
        return notes, steps, problems

    # --- Actions -----------------------------------------------------------------------------

    def simulate(self):
        if not self.version_var.get():
            return
        notes, steps, problems = self.make_plan()
        self.clear_log()
        for step in steps:
            self.write(release.summary(step))
            self.write(f"  {release.preview(step, notes)}")
        for problem in problems:
            self.write(f"! {problem}")
        self.write("\nReady to upload." if not problems else f"\n{len(problems)} problem(s) to fix before uploading.")

    def upload(self):
        if not self.version_var.get():
            return
        notes, steps, problems = self.make_plan()
        if problems:
            self.simulate()
            messagebox.showerror("Cannot upload", "\n".join(problems))
            return
        title = f"{self.app_var.get()} {self.version_var.get()}"
        table = "\n".join(release.summary(step) for step in steps)
        if not messagebox.askyesno("Upload release", f"Upload {title}?\n\n{table}"):
            return
        self.busy = True
        self.upload_button["state"] = "disabled"
        self.clear_log()
        threading.Thread(target=self.upload_worker, args=(steps, notes), daemon=True).start()

    def upload_worker(self, steps, notes):
        results = []
        try:
            results = release.upload(
                steps, notes,
                emit=lambda text, replace: self.events.put(("line", text, replace)),
                manual=self.ask_manual,
            )
        except Exception as error:  # surface it in the window instead of leaving the button disabled
            self.events.put(("line", f"ERROR: {error!r}", False))
        finally:
            self.events.put(("done", results))

    def ask_manual(self, step, notes) -> bool:
        """Called from the upload thread; the dialog itself runs on the window thread."""
        answer, done = {}, threading.Event()
        self.events.put(("manual", step, notes, answer, done))
        done.wait()
        return answer.get("ok", False)

    def show_manual(self, step, notes, answer, done):
        name, url = step.settings["name"], step.settings["url"]
        files = "\n".join(str(path) for path in (step.build.apk, step.build.obb) if path)
        webbrowser.open(url)

        dialog = tk.Toplevel(self.root)
        dialog.title(f"{step.label}: upload by hand")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(
            dialog, padding=12, justify="left",
            text=f"Upload these files in {name}:\n\n{files}\n\nThen paste the release notes.\n{url}",
        ).pack(anchor="w")

        def close(ok):
            answer["ok"] = ok
            dialog.destroy()

        def copy_notes():
            self.root.clipboard_clear()
            self.root.clipboard_append(notes)

        row = ttk.Frame(dialog, padding=12)
        row.pack(fill="x")
        ttk.Button(row, text="Copy notes", command=copy_notes).pack(side="left")
        ttk.Button(row, text=f"Open {name}", command=lambda: webbrowser.open(url)).pack(side="left", padx=4)
        ttk.Button(row, text="Done", command=lambda: close(True)).pack(side="right")
        ttk.Button(row, text="Skip", command=lambda: close(False)).pack(side="right", padx=4)
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self.root.wait_window(dialog)
        done.set()

    def finish(self, results):
        self.busy = False
        self.upload_button["state"] = "normal"
        self.write("")
        for step, ok in results:
            self.write(f"{'OK    ' if ok else 'FAILED'} {step.label}")
        if results and all(ok for _, ok in results):
            messagebox.showinfo("Release uploaded", "Every upload finished.")
        else:
            messagebox.showwarning("Some uploads failed", "Check the progress log. Select only the failed ones and upload again.")

    # --- Log ---------------------------------------------------------------------------------

    def poll(self):
        try:
            while True:
                kind, *data = self.events.get_nowait()
                if kind == "line":
                    self.write(*data)
                elif kind == "manual":
                    self.show_manual(*data)
                elif kind == "done":
                    self.finish(*data)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def write(self, text, replace=False):
        self.log["state"] = "normal"
        if replace:
            self.log.delete("end-2l", "end-1l")  # the last written line
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log["state"] = "disabled"

    def clear_log(self):
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"

    def close(self):
        if self.busy and not messagebox.askyesno("Upload in progress", "An upload is running. Close anyway?"):
            return
        self.save_notes()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        UploaderWindow(root)
    except Exception as error:  # pythonw has no console, so show startup errors (missing config.toml, etc.)
        messagebox.showerror("XR Release Uploader", f"Could not start:\n{error}")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
