"""Raspberry Pi SD card flasher.

Downloads an image from a remote URL and writes it raw to an SD card.
Supports .img, .img.xz, .img.gz, .img.bz2 and .zip images.
Windows only; requires administrator rights (the app relaunches itself elevated).
"""
import bz2
import contextlib
import ctypes
import gzip
import hashlib
import http.client
import json
import lzma
import os
import queue
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import unquote, urljoin, urlparse

APP_NAME = "Raspberry Flash"
REVISION = "1.1"
FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller exe
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
CACHE_DIR = APP_DIR / "cache"
MANIFEST = "manifest.json"
IMAGE_EXTS = (".img", ".xz", ".gz", ".bz2", ".zip")
CHUNK = 4 * 1024 * 1024  # must be a multiple of SECTOR
SECTOR = 512
CREATE_NO_WINDOW = 0x08000000
MAX_RETRIES = 10  # consecutive download failures without progress
RETRY_DELAY = 5  # seconds
NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError,
                  http.client.HTTPException, ssl.SSLError)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    args = sys.argv[1:] if FROZEN else [str(Path(__file__).resolve())] + sys.argv[1:]
    params = subprocess.list2cmdline(args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, str(APP_DIR), 1)
    return rc > 32


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def list_disks(show_all=False):
    """Return removable disks as dicts: Number, FriendlyName, Size, BusType."""
    where = "-not $_.IsBoot -and -not $_.IsSystem"
    if not show_all:
        where += " -and $_.BusType -in 'USB','SD','MMC'"
    ps = (f"Get-Disk | Where-Object {{ {where} }} | "
          "Select-Object Number,FriendlyName,Size,BusType | ConvertTo-Json -Compress")
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    out = res.stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    for d in data:
        d["BusType"] = str(d.get("BusType"))
    return data


def clean_disk(number):
    """Remove all partitions so Windows releases the volumes before raw writing."""
    script = f"select disk {number}\nattributes disk clear readonly\nclean\nrescan\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        res = subprocess.run(["diskpart", "/s", path], capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW)
    finally:
        os.unlink(path)
    if res.returncode != 0:
        raise RuntimeError(f"diskpart failed:\n{res.stdout}\n{res.stderr}")
    time.sleep(2)


def cache_name(url):
    name = unquote(Path(urlparse(url).path).name)
    return name or "image.img"


def open_image(path, stack):
    """Open (and transparently decompress) an image.

    Returns (stream, expected_size_or_None, progress_fn(written) -> 0..1).
    """
    raw = stack.enter_context(open(path, "rb"))
    size = os.path.getsize(path)
    magic = raw.read(6)
    raw.seek(0)
    compressed_progress = lambda _w: raw.tell() / size if size else 0

    if magic.startswith(b"\xfd7zXZ"):
        return stack.enter_context(lzma.open(raw)), None, compressed_progress
    if magic.startswith(b"\x1f\x8b"):
        return stack.enter_context(gzip.open(raw)), None, compressed_progress
    if magic.startswith(b"BZh"):
        return stack.enter_context(bz2.open(raw)), None, compressed_progress
    if magic.startswith(b"PK\x03\x04"):
        zf = stack.enter_context(zipfile.ZipFile(raw))
        members = [i for i in zf.infolist() if not i.is_dir()]
        imgs = [i for i in members if i.filename.lower().endswith(".img")]
        info = max(imgs or members, key=lambda i: i.file_size)
        total = info.file_size
        return stack.enter_context(zf.open(info)), total, lambda w: w / total if total else 0
    return raw, size, lambda w: w / size if size else 0


def read_full(stream, n):
    parts, got = [], 0
    while got < n:
        b = stream.read(n - got)
        if not b:
            break
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def pad_sector(buf):
    rem = len(buf) % SECTOR
    return buf + b"\0" * (SECTOR - rem) if rem else buf


# ---------------------------------------------------------------- worker

class FlashJob(threading.Thread):
    def __init__(self, url, disk, verify, events, cancel):
        super().__init__(daemon=True)
        self.url, self.disk, self.verify = url, disk, verify
        self.events, self.cancel = events, cancel

    def log(self, msg):
        self.events.put(("log", msg))

    def progress(self, frac, text, stage="write"):
        """stage is "download" or "write"; frac None means unknown total."""
        if frac is not None:
            frac = max(0.0, min(1.0, frac))
        self.events.put(("progress", stage, frac, text))

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def run(self):
        try:
            image = self.download()
            self.flash(image)
            self.events.put(("done", True, "SD card written successfully. You can remove it."))
        except Cancelled:
            self.events.put(("done", False, "Cancelled."))
        except Exception as e:
            self.events.put(("done", False, f"Error: {e}"))

    def resolve_image(self):
        """Return (image_url, sha256_or_None) for the URL the user entered.

        A URL pointing straight at an image is used as is; otherwise it is a base URL
        whose manifest.json names the current image.
        """
        if self.url.lower().endswith(IMAGE_EXTS):
            return self.url, None
        manifest_url = urljoin(self.url.rstrip("/") + "/", MANIFEST)
        self.log(f"Checking {manifest_url}")
        req = urllib.request.Request(manifest_url, headers={"User-Agent": f"RaspberryFlash/{REVISION}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                manifest = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Cannot read {MANIFEST}: HTTP {e.code} {e.reason}")
        except NETWORK_ERRORS as e:
            raise RuntimeError(f"Cannot read {MANIFEST}: {e}")
        except ValueError:
            raise RuntimeError(f"{MANIFEST} is not valid JSON")
        if not isinstance(manifest, dict) or not manifest.get("image"):
            raise RuntimeError(f'{MANIFEST} has no "image" entry')
        self.log(f"Available version: {manifest.get('version', '?')} ({manifest['image']})")
        return urljoin(manifest_url, manifest["image"]), manifest.get("sha256")

    def download(self):
        url, sha256 = self.resolve_image()
        CACHE_DIR.mkdir(exist_ok=True)
        target = CACHE_DIR / cache_name(url)
        if target.exists():
            self.log(f"Using cached image {target.name} ({human_size(target.stat().st_size)})")
            self.progress(1, "Using cached image", "download")
            return target

        self.log(f"Downloading {url}")
        part = target.with_name(target.name + ".part")
        meta = target.with_name(target.name + ".part.json")
        failures = 0
        while True:
            before = part.stat().st_size if part.exists() else 0
            try:
                done = self.download_attempt(url, part, meta)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500:  # 404, 403...: retrying will not help
                    raise RuntimeError(f"Download failed: HTTP {e.code} {e.reason}")
                err = e
            except NETWORK_ERRORS as e:
                err = e
            if part.exists() and part.stat().st_size > before:
                failures = 0  # the attempt made progress
            failures += 1
            if failures > MAX_RETRIES:
                raise RuntimeError(f"Download failed: {err}\n"
                                   "Press Flash again to resume where it stopped.")
            self.log(f"Connection problem ({err}); retrying in {RETRY_DELAY}s "
                     f"({failures}/{MAX_RETRIES})")
            if self.cancel.wait(RETRY_DELAY):
                raise Cancelled()
        if sha256:
            self.check_sha256(part, done, sha256)
        part.replace(target)
        meta.unlink(missing_ok=True)
        self.progress(1, f"Downloaded {human_size(done)}", "download")
        self.log(f"Downloaded {human_size(done)}")
        return target

    def check_sha256(self, path, size, expected):
        self.log("Checking download checksum...")
        hasher, done = hashlib.sha256(), 0
        with open(path, "rb") as f:
            while True:
                self.check_cancel()
                buf = f.read(CHUNK)
                if not buf:
                    break
                hasher.update(buf)
                done += len(buf)
                self.progress(done / size if size else None,
                              f"Checking {human_size(done)} / {human_size(size)}", "download")
        if hasher.hexdigest().lower() != expected.strip().lower():
            path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded image is corrupted (checksum mismatch). "
                               "Press Flash to download it again.")
        self.log("Checksum OK")

    def download_attempt(self, url, part, meta):
        """Download into part, resuming from its current size when the server allows it.

        Returns the final size; raises a network error if the transfer breaks off.
        """
        start = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": f"RaspberryFlash/{REVISION}"}
        if start:
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                info = {}
            # Only resume a file from this URL, and let If-Range make the server send the
            # whole file again if it changed since the partial download started.
            if info.get("url") == url and info.get("validator"):
                headers["Range"] = f"bytes={start}-"
                headers["If-Range"] = info["validator"]
            else:
                start = 0
        req = urllib.request.Request(url, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 416:  # range not satisfiable: partial file is stale
                part.unlink(missing_ok=True)
                raise ConnectionError("partial download is invalid, restarting")
            raise

        with r:
            if r.status == 206:
                total_str = r.headers.get("Content-Range", "").rpartition("/")[2]
                total = int(total_str) if total_str.isdigit() else 0
                self.log(f"Resuming download at {human_size(start)}")
            else:  # server sent the whole file
                start = 0
                total = int(r.headers.get("Content-Length") or 0)
                etag = r.headers.get("ETag")
                validator = etag if etag and not etag.startswith("W/") else r.headers.get("Last-Modified")
                meta.write_text(json.dumps({"url": url, "validator": validator}), encoding="utf-8")

            done, t0 = start, time.time()
            with open(part, "ab" if start else "wb") as f:
                while True:
                    self.check_cancel()
                    buf = r.read(1024 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    speed = (done - start) / max(time.time() - t0, 0.001)
                    text = f"Downloading {human_size(done)}"
                    text += f" / {human_size(total)}" if total else ""
                    text += f"  ({human_size(speed)}/s)"
                    self.progress(done / total if total else None, text, "download")
        if total and done != total:
            raise ConnectionError(f"download incomplete ({done} of {total} bytes)")
        return done

    def flash(self, image):
        n = self.disk["Number"]
        disk_size = int(self.disk["Size"])
        device = rf"\\.\PhysicalDrive{n}"

        with contextlib.ExitStack() as stack:
            stream, expected, progress_of = open_image(image, stack)
            if expected and expected > disk_size:
                raise RuntimeError(f"Image ({human_size(expected)}) is larger than the card "
                                   f"({human_size(disk_size)})")

            self.log(f"Preparing disk {n} ({self.disk['FriendlyName']})...")
            self.progress(0, "Cleaning disk...")
            clean_disk(n)

            self.log(f"Writing to {device}")
            hasher = hashlib.sha256()
            fd = os.open(device, os.O_RDWR | os.O_BINARY)
            try:
                # The first chunk (partition table) is written last, so Windows does not
                # mount the new partitions while the rest is written and verified: once
                # mounted, Windows writes its own metadata into them and verify would fail.
                first = pad_sector(read_full(stream, CHUNK))
                written, t0 = len(first), time.time()
                if len(first) == CHUNK:
                    os.lseek(fd, CHUNK, os.SEEK_SET)
                    while True:
                        self.check_cancel()
                        buf = read_full(stream, CHUNK)
                        if not buf:
                            break
                        buf = pad_sector(buf)
                        if written + len(buf) > disk_size:
                            raise RuntimeError("Image is larger than the card")
                        os.write(fd, buf)
                        hasher.update(buf)
                        written += len(buf)
                        speed = written / max(time.time() - t0, 0.001)
                        self.progress(progress_of(written),
                                      f"Writing {human_size(written)}  ({human_size(speed)}/s)")
                os.fsync(fd)
                self.log(f"Wrote {human_size(written)}")
                if self.verify:
                    self.do_verify(fd, CHUNK, written - CHUNK, hasher.hexdigest())
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, first)
                os.fsync(fd)
                if self.verify:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if self.read_back(fd, len(first)) != first:
                        raise RuntimeError("Verification failed: partition table on the card "
                                           "does not match the image")
                    self.log("Verification OK")
            finally:
                os.close(fd)

    def read_back(self, fd, length, hasher=None):
        """Read length bytes from fd; with hasher, hash them instead of returning them."""
        parts, done = [], 0
        while done < length:
            self.check_cancel()
            buf = os.read(fd, min(CHUNK, length - done))
            if not buf:
                break
            done += len(buf)
            if hasher:
                hasher.update(buf)
                self.progress(done / length, f"Verifying {human_size(done)} / {human_size(length)}")
            else:
                parts.append(buf)
        return b"".join(parts)

    def do_verify(self, fd, start, length, expected_hash):
        self.log("Verifying...")
        if length <= 0:
            return
        hasher = hashlib.sha256()
        os.lseek(fd, start, os.SEEK_SET)
        self.read_back(fd, length, hasher)
        if hasher.hexdigest() != expected_hash:
            raise RuntimeError("Verification failed: data on the card does not match the image")


# ---------------------------------------------------------------- GUI

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.minsize(640, 440)
        self.cfg = load_config()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.job = None
        self.disks = []

        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Base URL:").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar(value=self.cfg.get("last_url", ""))
        self.url_box = ttk.Combobox(frm, textvariable=self.url_var, values=self.cfg.get("urls", []))
        self.url_box.grid(row=0, column=1, sticky="ew", **pad)
        url_btns = ttk.Frame(frm)
        url_btns.grid(row=0, column=2, sticky="e")
        ttk.Button(url_btns, text="Save", command=self.save_url).pack(side="left", padx=2)
        ttk.Button(url_btns, text="Remove", command=self.remove_url).pack(side="left", padx=2)

        ttk.Label(frm, text="SD card:").grid(row=1, column=0, sticky="w", **pad)
        self.disk_var = tk.StringVar()
        self.disk_box = ttk.Combobox(frm, textvariable=self.disk_var, state="readonly")
        self.disk_box.grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Refresh", command=self.refresh_disks).grid(row=1, column=2, sticky="e", **pad)

        opts = ttk.Frame(frm)
        opts.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        self.verify_var = tk.BooleanVar(value=self.cfg.get("verify", True))
        self.all_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Verify after writing", variable=self.verify_var).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="Show all non-system disks", variable=self.all_var,
                        command=self.refresh_disks).pack(side="left", padx=4)

        self.bars, self.pct = {}, {}
        for row, (stage, title) in enumerate((("download", "Download:"), ("write", "Write:")), start=3):
            ttk.Label(frm, text=title).grid(row=row, column=0, sticky="w", **pad)
            self.bars[stage] = ttk.Progressbar(frm, maximum=1000)
            self.bars[stage].grid(row=row, column=1, sticky="ew", **pad)
            self.pct[stage] = ttk.Label(frm, text="", width=8, anchor="e")
            self.pct[stage].grid(row=row, column=2, sticky="e", **pad)
        self.status = ttk.Label(frm, text="Ready")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        self.logbox = ScrolledText(frm, height=10, state="disabled")
        self.logbox.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)

        ttk.Button(frm, text="About", command=self.show_about).grid(row=7, column=0, sticky="w", **pad)
        btns = ttk.Frame(frm)
        btns.grid(row=7, column=1, columnspan=2, sticky="e", **pad)
        self.flash_btn = ttk.Button(btns, text="Flash", command=self.start)
        self.flash_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_disks()
        self.after(100, self.poll)

    def show_about(self):
        win = tk.Toplevel(self)
        win.withdraw()  # shown once centered, so it does not flash at the screen corner
        win.title(f"About {APP_NAME}")
        win.resizable(False, False)
        win.transient(self)
        frm = ttk.Frame(win, padding=16)
        frm.pack()
        ttk.Label(frm, text=APP_NAME, font=("Segoe UI", 12, "bold")).pack(pady=(0, 4))
        ttk.Label(frm, text=f"Revision {REVISION}").pack()
        ttk.Label(frm, text="© 2026 Steplab").pack(pady=(0, 8))
        for text, target in (("www.steplab.net", "https://www.steplab.net"),
                             ("info@steplab.net", "mailto:info@steplab.net")):
            link = ttk.Label(frm, text=text, foreground="#0066cc", cursor="hand2")
            link.pack()
            link.bind("<Button-1>", lambda _e, t=target: webbrowser.open(t))
        ttk.Button(frm, text="OK", command=win.destroy).pack(pady=(12, 0))
        # Center over the main window, which keeps it on the same monitor.
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.grab_set()

    # -- config
    def persist(self):
        self.cfg["last_url"] = self.url_var.get().strip()
        self.cfg["verify"] = self.verify_var.get()
        save_config(self.cfg)

    def save_url(self):
        url = self.url_var.get().strip()
        if not url:
            return
        urls = [u for u in self.cfg.get("urls", []) if u != url]
        urls.insert(0, url)
        self.cfg["urls"] = urls
        self.url_box["values"] = urls
        self.persist()
        self.log(f"Saved URL: {url}")

    def remove_url(self):
        url = self.url_var.get().strip()
        urls = [u for u in self.cfg.get("urls", []) if u != url]
        self.cfg["urls"] = urls
        self.url_box["values"] = urls
        self.url_var.set("")
        self.persist()

    # -- disks
    def refresh_disks(self):
        try:
            self.disks = list_disks(self.all_var.get())
        except Exception as e:
            self.disks = []
            self.log(f"Could not list disks: {e}")
        labels = [f"Disk {d['Number']}: {d['FriendlyName']}  [{human_size(int(d['Size']))}, {d['BusType']}]"
                  for d in self.disks]
        self.disk_box["values"] = labels
        self.disk_var.set(labels[0] if labels else "")
        if not labels:
            self.status.config(text="No SD card found. Insert one and press Refresh.")

    def selected_disk(self):
        labels = list(self.disk_box["values"])
        label = self.disk_var.get()
        return self.disks[labels.index(label)] if label in labels else None

    # -- flashing
    def start(self):
        url = self.url_var.get().strip()
        disk = self.selected_disk()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showerror(APP_NAME, "Please enter a valid http(s) URL.", parent=self)
            return
        if not disk:
            messagebox.showerror(APP_NAME, "Please select an SD card.", parent=self)
            return
        msg = (f"ALL DATA on\n\nDisk {disk['Number']}: {disk['FriendlyName']} "
               f"({human_size(int(disk['Size']))})\n\nwill be ERASED. Continue?")
        if not messagebox.askyesno(APP_NAME, msg, icon="warning", parent=self):
            return
        self.save_url()
        for stage in self.bars:
            self.set_bar(stage, 0)
        self.cancel.clear()
        self.set_busy(True)
        self.job = FlashJob(url, disk, self.verify_var.get(), self.events, self.cancel)
        self.job.start()

    def set_busy(self, busy):
        self.flash_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        self.url_box.config(state="disabled" if busy else "normal")
        self.disk_box.config(state="disabled" if busy else "readonly")

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self.log(ev[1])
                elif ev[0] == "progress":
                    self.set_bar(ev[1], ev[2])
                    self.status.config(text=ev[3])
                elif ev[0] == "done":
                    ok, msg = ev[1], ev[2]
                    self.set_busy(False)
                    self.job = None
                    for stage in self.bars:
                        if ok:
                            self.set_bar(stage, 1)
                        elif str(self.bars[stage]["mode"]) == "indeterminate":
                            self.set_bar(stage, 0)
                    self.status.config(text=msg)
                    self.log(msg)
                    (messagebox.showinfo if ok else messagebox.showerror)(APP_NAME, msg, parent=self)
                    self.refresh_disks()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def set_bar(self, stage, frac):
        """Update one progress bar; frac None animates it (server sent no size)."""
        bar = self.bars[stage]
        if frac is None:
            bar.config(mode="indeterminate")
            bar.step(20)
            self.pct[stage].config(text="")
        else:
            bar.config(mode="determinate", value=frac * 1000)
            self.pct[stage].config(text=f"{frac * 100:.0f}%")

    def log(self, msg):
        self.logbox.config(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def on_close(self):
        if self.job and not messagebox.askyesno(APP_NAME, "A write is in progress. Quit anyway?", parent=self):
            return
        self.persist()
        self.destroy()


def main():
    if sys.platform != "win32":
        sys.exit("This tool currently supports Windows only.")
    if not is_admin():
        if relaunch_as_admin():
            return
        tk.Tk().withdraw()
        messagebox.showerror(APP_NAME, "Administrator rights are required to write to the SD card.")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
