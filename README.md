# ⚡ ComfyUI Model Manager

**Never hunt for missing models again.** ComfyUI Model Manager scans every official workflow template on your machine, tells you exactly which model files each one needs, and downloads the missing ones at maximum speed with aria2 multi-threading — all without starting ComfyUI.

> 🌏 [繁體中文說明 (Traditional Chinese)](README_zh-TW.md)

![ComfyUI Model Manager](ComfyUI_Model_Manager.png)

![Main interface (Chinese)](screenshots/screenshot-zh.png)

## ✨ Why you need this

Deploying ComfyUI means pulling down **hundreds of GB** of models, and the official downloader is painfully slow. This tool turns a 30-minute download into a 2-minute one — and more importantly, it tells you **what** to download in the first place.

- 🔍 **Smart template scanner** — scans all official workflow templates (116+ built in), parses every `CheckpointLoader` / `VAELoader` / `UNETLoader` node, and checks which model files you already have and which are missing.
- ⚡ **aria2 16-thread accelerated downloads** — multi-connection split, **hf-mirror dual-mirror racing** (first mirror to respond wins), resume support, batch queue.
- 🧩 **Two-way root detection** — works identically for **portable** and **non-portable** ComfyUI installs (they differ by just one directory level). Zero configuration needed.
- 🚫 **Runs without ComfyUI** — completely standalone; it never starts your 8188 server or touches your workflow.
- 🌐 **Bilingual UI** — Traditional Chinese / English, one-click slider switch, auto-detects your system language on first launch.
- 🤖 **Auto-translation** — when a new official template appears, its English description is fetched automatically and translated into Chinese, both saved for future updates.
- 🖼️ **Official thumbnails + smart classification** — preview images auto-downloaded from the official catalog; templates are classified into 8 categories by their official descriptions.
- 📦 **Extra model directories** — reads your `extra_model_paths.yaml` and checks models in those directories too.
- 💾 **Size verification** — compares local file size against the expected size, flags incomplete/corrupt downloads, and shows disk usage per model.
- ✅ **Ready-state tracking** — templates marked ready are skipped on the next scan; only missing ones get re-checked.

## 🖼️ Interface

English overview (categories & stats):

![English interface](screenshots/screenshot-en.png)

Expanded template details — description, thumbnails, every required model file with ✓/✗ status, size, and location:

![Template details (Chinese)](screenshots/screenshot-zh-more.png)

## 📦 Installation

### Requirements

- Windows / Linux / macOS (any OS that can run Python + aria2)
- Python 3.10+ (a portable ComfyUI already bundles `python_embeded`, so nothing extra is needed)
- **aria2c** — the tool auto-downloads it for you if missing

### Portable ComfyUI (recommended layout)

```
D:\ComfyUI_Portable\          ← bundle root
├── ComfyUI\                  ← the app itself (auto-detected)
└── Comfyui_Model_Manager\    ← put this folder here
    ├── standalone_server.py
    ├── ui.html
    └── Start_Model_Manager.bat
```

1. Download or `git clone` this repository.
2. Place the folder **next to** the `ComfyUI` folder inside your portable bundle.
3. Double-click `Start_Model_Manager.bat` (or run `python standalone_server.py`).
4. Open **http://127.0.0.1:8190** in your browser.

That's it — no config, no ComfyUI startup.

### Non-portable ComfyUI

Put this folder **inside** your ComfyUI root (one level deeper), so its parent directory *is* the ComfyUI root:

```
~/ComfyUI/                   ← root (contains main.py + models/)
└── Comfyui_Model_Manager/   ← put this folder here
```

You can also force the root explicitly:

```bash
python standalone_server.py --root D:/path/to/ComfyUI
# or
set COMFYUI_ROOT=D:/path/to/ComfyUI
python standalone_server.py
```

### Verify aria2 (optional)

The tool auto-fetches aria2c into its folder on first run. To install manually, put `aria2c.exe` anywhere on your PATH.

## 🚀 Usage

1. **First scan** — click **Rescan**. The first scan walks every template JSON and takes a little while ("首次掃描較耗時...").
2. **See the gaps** — templates are grouped into 8 categories. Each card shows a thumbnail, an official description, and every required model file marked:
   - `✓` present (with size & location) — `✗` missing (with remote size) — `⚠` incomplete.
3. **Select & download** — tick the missing templates, click **Add to queue**, then start. aria2 splits each file across 16 threads and races `huggingface.co` vs `hf-mirror.com` for the fastest mirror.
4. **Re-scan** — ready templates are remembered and skipped; only new/missing ones are re-checked.
5. **Open folders** — **Models Folder** opens your `models/` maximized; **Extra Model Dirs** opens every directory configured in `extra_model_paths.yaml`.

## ⚙️ How it works

| Component | Detail |
|---|---|
| Backend | Pure Python stdlib `http.server` — **zero pip dependencies** |
| Frontend | Single `ui.html`, dark modern UI, no external CDN |
| Template source | Official `comfyui_workflow_templates_json` shipped with ComfyUI |
| Download engine | `aria2c` (auto-downloaded) — 16 threads, dual-mirror racing, resume |
| Thumbnails | Official workflow output images, cached in `thumbs/`, refreshed on a 7-day cycle |
| Translation | Automatic fetch of official English descriptions + Google GTX free endpoint → Traditional Chinese, stored in `descriptions.json` |
| State | `scan_state.json` — remembered ready/confirmed/missing state, resized fast on later scans |

## ❓ FAQ

**Q: It says "找不到 ComfyUI 根目錄" (cannot find ComfyUI root).**
A: The tool looks **one level up**. In a portable bundle it must sit *beside* the `ComfyUI` folder; in a normal install it must sit *inside* the root. Or pass `--root`.

**Q: ComfyUI is running — will this interfere?**
A: No. The manager runs standalone on port **8190** and never touches ComfyUI's own port 8188.

**Q: The download is slow.**
A: Enable mirror racing (default). If your network is outside China, you can force `huggingface.co` only by deleting the mirror fallback in `_mirror_urls()`.

**Q: Will it re-download models I already have?**
A: No. Ready templates are marked confirmed and skipped on later scans; existing files are verified by size, not blindly re-downloaded.

**Q: Where does the Chinese description come from?**
A: Official templates have no Chinese text, so the manager auto-translates and caches both English and Chinese (`descriptions.json`). Templates already covered show instantly; brand-new ones translate in the background within seconds.

## 📄 License

MIT — free for personal and commercial use.
