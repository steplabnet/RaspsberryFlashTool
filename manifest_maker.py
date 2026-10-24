"""Manifest maker for Raspberry Flash.

Select an image file and write the manifest.json that Raspberry Flash reads from the
base URL: {"version": ..., "image": <file name>, "sha256": ...}. Upload the manifest
next to the image.
"""
import hashlib
import json
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Manifest Maker"
REVISION = "1.0"
MANIFEST = "manifest.json"
CHUNK = 4 * 1024 * 1024
IMAGE_TYPES = [("Images", "*.img *.xz *.gz *.bz2 *.zip"), ("All files", "*.*")]


def guess_version(name):
    """Version from the file name: the last group of digits, e.g. solaria231026.img -> 231026."""
    digits = re.findall(r"\d+", Path(name).name)
    return digits[-1] if digits else ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(560, 0)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.job = None

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Image file:").grid(row=0, column=0, sticky="w", **pad)
        self.file_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.file_var, state="readonly").grid(row=0, column=1, sticky="ew", **pad)
        self.browse_btn = ttk.Button(frm, text="Browse...", command=self.browse)
        self.browse_btn.grid(row=0, column=2, **pad)

        ttk.Label(frm, text="Version:").grid(row=1, column=0, sticky="w", **pad)
        self.version_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.version_var).grid(row=1, column=1, sticky="ew", **pad)

        self.sha_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Include SHA-256 checksum", variable=self.sha_var).grid(
            row=2, column=1, sticky="w", **pad)

        self.bar = ttk.Progressbar(frm, maximum=1000)
        self.bar.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.status = ttk.Label(frm, text="Select an image file")
        self.status.grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", **pad)
        self.create_btn = ttk.Button(btns, text="Create manifest", command=self.start)
        self.create_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        self.after(100, self.poll)

    def browse(self):
        path = filedialog.askopenfilename(parent=self, title="Select image", filetypes=IMAGE_TYPES)
        if path:
            self.file_var.set(path)
            self.version_var.set(guess_version(path))
            self.status.config(text="Ready")

    def start(self):
        image = Path(self.file_var.get())
        if not self.file_var.get() or not image.is_file():
            messagebox.showerror(APP_NAME, "Please select an image file.", parent=self)
            return
        out = filedialog.asksaveasfilename(parent=self, title="Save manifest", initialdir=image.parent,
                                           initialfile=MANIFEST, defaultextension=".json",
                                           filetypes=[("JSON", "*.json")])
        if not out:
            return
        manifest = {"version": self.version_var.get().strip(), "image": image.name}
        self.cancel.clear()
        self.set_busy(True)
        self.job = threading.Thread(target=self.work, args=(image, Path(out), manifest, self.sha_var.get()),
                                    daemon=True)
        self.job.start()

    def work(self, image, out, manifest, with_sha):
        try:
            if with_sha:
                size = image.stat().st_size
                hasher, done = hashlib.sha256(), 0
                with open(image, "rb") as f:
                    while True:
                        if self.cancel.is_set():
                            self.events.put(("done", False, "Cancelled."))
                            return
                        buf = f.read(CHUNK)
                        if not buf:
                            break
                        hasher.update(buf)
                        done += len(buf)
                        self.events.put(("progress", done / size if size else 1,
                                         f"Computing checksum {done / 2**20:.0f} / {size / 2**20:.0f} MB"))
                manifest["sha256"] = hasher.hexdigest()
            out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            self.events.put(("progress", 1, f"Saved {out}"))
            self.events.put(("done", True, f"Manifest saved:\n{out}\n\n"
                                           f"Upload it next to {image.name}."))
        except Exception as e:
            self.events.put(("done", False, f"Error: {e}"))

    def set_busy(self, busy):
        self.create_btn.config(state="disabled" if busy else "normal")
        self.browse_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "progress":
                    self.bar["value"] = ev[1] * 1000
                    self.status.config(text=ev[2])
                else:
                    _, ok, msg = ev
                    self.job = None
                    self.set_busy(False)
                    if not ok:
                        self.bar["value"] = 0
                        self.status.config(text=msg.splitlines()[0])
                    (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg, parent=self)
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()
