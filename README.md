# Raspberry Flash

Windows tools to write a Raspberry Pi image to an SD card, always using the latest image published on a web server.

- **RaspberryFlash.exe** – downloads the current image and writes it to the SD card.
- **ManifestMaker.exe** – creates the `manifest.json` that tells RaspberryFlash which image is current.

Requirements: Windows 10/11. RaspberryFlash needs administrator rights (it asks for them when started).

User guides:
- Complete guide with screenshots: [English](docs/user-guide-en.html) · [Italiano](docs/user-guide-it.html) – PDF: [English](docs/RaspberryFlash-Guide-EN.pdf) · [Italiano](docs/RaspberryFlash-Guide-IT.pdf)
- RaspberryFlash only: [English](docs/raspberry-flash-en.html) · [Italiano](docs/raspberry-flash-it.html)
- ManifestMaker only: [English](docs/manifest-maker-en.html) · [Italiano](docs/manifest-maker-it.html)

## How it works

The server holds the image and a small `manifest.json` in the same folder:

```
https://drive.example.com/
├── manifest.json
└── solaria231026.img
```

```json
{
  "version": "231026",
  "image": "solaria231026.img",
  "sha256": "3f5a…"
}
```

RaspberryFlash reads the manifest from the base URL. If the image named in it is already in its local cache, it uses that copy; otherwise it downloads it first.

## Flashing an SD card (RaspberryFlash.exe)

1. Insert the SD card and start **RaspberryFlash.exe**.
2. In **Base URL**, enter the server folder, e.g. `https://drive.example.com/`.
   Press **Save** to keep it in the list for next time.
3. Select the card in **SD card**. Press **Refresh** if it is not listed.
4. Press **Flash** and confirm. **Everything on the selected card is erased.**
5. Wait for "SD card written successfully", then remove the card.

Options:

- **Verify after writing** – reads the card back and checks it matches the image (recommended).
- **Show all non-system disks** – also lists disks that are not detected as USB/SD readers. Be careful to select the right one.

Notes:

- An interrupted download resumes where it stopped when you press **Flash** again.
- A direct link to an image (ending in `.img`, `.img.xz`, `.img.gz`, `.img.bz2` or `.zip`) also works in place of a base URL; compressed images are decompressed while writing.
- Downloaded images are kept in the `cache` folder next to the exe. Delete old ones to free space.
- Settings (last URL, saved URLs, verify option) are stored in `config.json` next to the exe.

## Publishing a new image (ManifestMaker.exe)

1. Upload the new image to the server folder with a **new file name** (e.g. `solaria151126.img`).
   RaspberryFlash decides whether to download by file name, so reusing a name keeps the old cached copy.
2. Start **ManifestMaker.exe** and press **Browse...** to select the same image on your PC.
3. Check **Version** (filled in from the digits in the file name).
4. Keep **Include SHA-256 checksum** ticked so corrupted downloads are detected.
5. Press **Create manifest** and save `manifest.json`.
6. Upload `manifest.json` to the server folder, replacing the old one.

The next time someone presses **Flash**, the new image is downloaded automatically.

## Building from source

Requires Python 3 for Windows.

```
build.bat
```

This installs PyInstaller and creates `dist\RaspberryFlash.exe` and `dist\ManifestMaker.exe`.
To run without building: `python raspberry_flash.py` or `python manifest_maker.py`.

## About

© 2026 Steplab – [www.steplab.net](https://www.steplab.net) – info@steplab.net
