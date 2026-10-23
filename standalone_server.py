"""Comfyui Model Manager — standalone model downloader, runs without ComfyUI.

Usage:
    python standalone_server.py          # serves on http://127.0.0.1:8190
    python standalone_server.py --port 8200
    python standalone_server.py --root D:/ComfyUI_Portable/ComfyUI

Scans the ComfyUI root's blueprints for missing models and downloads
them via aria2c. The ComfyUI root is auto-detected from the parent
directory (one level up); see _find_comfyui_root().
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# 依使用者要求：執行時僅偵測上一層，不逐層向上走
def _is_comfyui_root(p: Path) -> bool:
    """真正的 ComfyUI 根目錄需同時包含 main.py 與 models/ 資料夾。"""
    return (p / "main.py").is_file() and (p / "models").is_dir()


def _find_comfyui_root() -> Path:
    """兩用偵測：可攜版與非可攜版只差一個目錄，統一由工具所在位置判斷。

    非可攜版（工具放在根目錄內層）：根目錄 = 父目錄
    可攜版（工具與 ComfyUI 應用同層）：根目錄 = 父目錄 / ComfyUI

    優先順序：
      1. COMFYUI_ROOT 環境變數 / --root 參數（強制指定）。
      2. 模式一（非可攜版）：父目錄即根目錄。
      3. 模式二（可攜版）：父目錄內的 ComfyUI 子資料夾即根目錄。
    """
    explicit = os.environ.get("COMFYUI_ROOT")
    if explicit and _is_comfyui_root(Path(explicit)):
        return Path(explicit)

    parent = _HERE.parent

    # 模式一（非可攜版）：工具放在根目錄內層，父目錄即根目錄
    if _is_comfyui_root(parent):
        return parent

    # 模式二（可攜版）：工具在 bundle 根，根目錄在父目錄的 ComfyUI 子資料夾
    app = parent / "ComfyUI"
    if _is_comfyui_root(app):
        return app

    raise RuntimeError(
        "找不到 ComfyUI 根目錄：請將工具放在根目錄內層（父目錄即根目錄，非可攜版），"
        "或與 ComfyUI 應用資料夾同層（可攜版），"
        "亦可用 --root 參數 / COMFYUI_ROOT 環境變數指定。"
    )


# 匯入階段先做一次偵測；失敗時不崩潰（main() 會重新偵測並給出明確錯誤）
try:
    ROOT = _find_comfyui_root()
except RuntimeError:
    ROOT = _HERE.parent  # placeholder，僅供匯入階段不中斷
MODELS_DIR = ROOT / "models"


def _find_templates_dir() -> Path:
    """Find the official ComfyUI workflow templates folder."""
    # 1. Built-in templates in venv (official ComfyUI templates, ~600+)
    venv_tpl = ROOT / "venv" / "Lib" / "site-packages" / "comfyui_workflow_templates_json" / "templates"
    if venv_tpl.is_dir() and len(list(venv_tpl.glob("*.json"))) > 50:
        return venv_tpl
    # 2. System python
    for sp in (ROOT / "venv" / "lib").glob("python*/site-packages/comfyui_workflow_templates_json/templates"):
        if sp.is_dir() and len(list(sp.glob("*.json"))) > 50:
            return sp
    # 3. User blueprints folder (fallback)
    bp = ROOT / "blueprints"
    if bp.is_dir():
        return bp
    return bp


TEMPLATES_DIR = _find_templates_dir()
# Keep BLUEPRINTS_DIR alias for compatibility
BLUEPRINTS_DIR = TEMPLATES_DIR


def _load_extra_dirs() -> list[Path]:
    """Parse extra_model_paths.yaml properly — handles nested structure.
    Format:
        ComfyUI:
            base_path: D:/ComfyUI/
            checkpoints: models/checkpoints
        E drive:
            base_path: E:/ComfyUI_models/
            checkpoints: checkpoints
            vae: vae
    We only need base_path entries to know which root dirs to search.
    """
    result: list[Path] = []
    yaml_path = ROOT / "extra_model_paths.yaml"
    if not yaml_path.exists():
        return result
    try:
        for line in yaml_path.read_text(encoding="utf-8").splitlines():
            # base_path: E:/ComfyUI_models/
            m = re.match(r"\s*base_path:\s*(.+?)\s*$", line)
            if m:
                p = Path(m.group(1).rstrip("/\\"))
                if p.exists() and p not in result:
                    result.append(p)
            # - E:/ComfyUI_models/ (list form)
            m2 = re.match(r"\s*-\s*([A-Z]:[\\/].+?)\s*$", line)
            if m2:
                p = Path(m2.group(1))
                if p.exists() and p not in result:
                    result.append(p)
    except Exception:
        pass
    return result


EXTRA_MODEL_DIRS = _load_extra_dirs()

import urllib.request
from concurrent.futures import ThreadPoolExecutor


_remote_size_cache: dict[str, float | None] = {}


def _remote_size_gb(url: str) -> float | None:
    """HEAD request to get remote file size in GB, cached（成功結果持久化，下次開啟不重複 HEAD）。"""
    if url in _remote_size_cache:
        return _remote_size_cache[url]
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            cl = resp.headers.get("Content-Length")
            if cl:
                sz = round(int(cl) / 1024**3, 2)
                _remote_size_cache[url] = sz
                _state["remote_sizes"][url] = sz
                _save_state()
                return sz
    except Exception:
        pass
    _remote_size_cache[url] = None
    return None


# ---------- scanning ----------

_cache: dict = {"mtime": 0.0, "templates": []}
_size_cache: dict[tuple[str, str], float | None] = {}
_cache_lock = threading.Lock()
_scan_errors: list[str] = []  # 解析失敗的範本 JSON 日誌（最多保留 20 筆）


def _log_scan_error(msg: str) -> None:
    """記錄掃描錯誤到 _scan_errors（只用方法操作，避免函數內賦值造成局部遮蔽）。"""
    if msg not in _scan_errors:
        _scan_errors.append(msg)
        if len(_scan_errors) > 20:
            del _scan_errors[: len(_scan_errors) - 20]

# ---------- scan state（持久化確認清單） ----------
# scan_state.json 記錄：
#   known        : {範本檔名: mtime} —— 偵測新增/變更的範本
#   confirmed    : [範本檔名]        —— 已確認「所有檔案下載齊全」的範本，下次開啟跳過比對
#   remote_sizes : {url: size_gb}    —— 已確認的遠端容量，已有容量的模型不再重複 HEAD
STATE_FILE = _HERE / "scan_state.json"
_state: dict = {"known": {}, "confirmed": [], "remote_sizes": {}}


def _load_state() -> None:
    global _state
    try:
        if STATE_FILE.is_file():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _state = {
                "known": dict(data.get("known", {})),
                "confirmed": list(data.get("confirmed", [])),
                "remote_sizes": {k: v for k, v in data.get("remote_sizes", {}).items() if v},
            }
            _remote_size_cache.update(_state["remote_sizes"])
    except Exception:
        pass


def _save_state() -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


_load_state()


def _find_model(directory: str, filename: str) -> Path | None:
    """Search all known model roots for the file."""
    roots = [MODELS_DIR] + EXTRA_MODEL_DIRS
    for root in roots:
        p = root / directory / filename
        if p.is_file():
            return p
    return None


def _model_size(directory: str, filename: str) -> float | None:
    key = (directory, filename)
    if key in _size_cache:
        return _size_cache[key]
    p = _find_model(directory, filename)
    result = round(p.stat().st_size / 1024**3, 2) if p else None
    _size_cache[key] = result
    return result


_API_RE = re.compile(
    r"(?<![a-z])(openai|anthropic|googleapis|replicate|runwayml|klingai|"
    r"fal\.|elevenlabs|stability\.ai|midjourney|sunwood|minimax|"
    r"krea\.ai|pika\.art|luma-labs|lumalabs|recraft|scenario|tavily|"
    r"deepseek|moonshot|dashscope|volces|siliconflow|groq|together|"
    r"suno|hailuoai|vidu)\.(com|ai|dev|net|cn)",
    re.IGNORECASE,
)


def _detect_api(text: str) -> bool:
    return bool(_API_RE.search(text))


def _extract_models(data: dict) -> list[dict]:
    """Walk the JSON and return all entries in arrays named 'models'."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "models" and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and "name" in item:
                            found.append(item)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _extract_description(data: dict) -> str:
    """從範本 JSON 的 definitions 子圖節點提取官方介紹說明（每份範本內嵌一則以上）。"""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            d = node.get("description")
            if isinstance(d, str) and d.strip():
                s = d.strip()
                if 20 <= len(s) <= 800:
                    found.append(s)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("definitions"))
    return max(found, key=len) if found else ""


# 雙語範本介紹：descriptions.json（title → {"en", "zh"}）；中文由翻譯維護，
# 新範本暫無中文時回退英文（介面會標示「中文更新中」）
_DESC_ZH: dict[str, str] = {}
try:
    _dzh = json.loads((_HERE / "descriptions.json").read_text(encoding="utf-8"))
    _DESC_ZH = {k: str(v.get("zh", "")).strip() for k, v in _dzh.items() if isinstance(v, dict) and v.get("zh")}
except Exception:
    _DESC_ZH = {}


# ---------- 新範本自動翻譯（英文 → 繁體中文） ----------
# 官方無中文來源：偵測到新範本（英文介紹有、本地無中文翻譯）時，
# 背景呼叫免費翻譯端點生成中文，並把 en/zh 兩種介紹寫回 descriptions.json。

_DESC_JSON_FILE = _HERE / "descriptions.json"
_TRANS_LOCK = threading.Lock()
_TRANS_WORKER: dict = {"running": False, "pending": 0, "done": 0, "failed": [], "change": 0.0}
_TRANS_QUEUE: list[tuple[str, str]] = []  # (title, desc_en)
_TRANS_SEEN: set[str] = set()


def _gtx_translate(text: str) -> str:
    """Google 非官方翻譯端點（免費、免金鑰），en → zh-TW；無需註冊。"""
    import urllib.request
    import urllib.parse
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q="
           + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8"))
    parts = data[0] if data else []
    return "".join(p[0] for p in parts if p and p[0]).strip()


def _translate_chunked(text: str) -> str:
    """長文本依句子分段翻譯（單次請求上限 3000 字元），再合併回傳。"""
    if len(text) <= 3000:
        return _gtx_translate(text)
    sentences: list[str] = []
    for chunk in re.split(r'(?<=[.!?])\s+', text):
        sentences.append(chunk)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if buf and len(buf) + len(s) > 2800:
            out.append(_gtx_translate(buf))
            buf = s
        else:
            buf = (buf + " " + s).strip()
    if buf:
        out.append(_gtx_translate(buf))
    return " ".join(x for x in out if x)


def _translate_one(title: str, desc_en: str) -> bool:
    """翻譯單一範本描述並寫回 descriptions.json（en/zh 都保存）。"""
    try:
        zh = _translate_chunked(desc_en)
        if not zh or zh == desc_en:
            return False
        with _TRANS_LOCK:
            try:
                d = json.loads(_DESC_JSON_FILE.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            d.setdefault(title, {})["en"] = desc_en
            d[title]["zh"] = zh
            tmp = _DESC_JSON_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, _DESC_JSON_FILE)
            _DESC_ZH[title] = zh
        return True
    except Exception:
        return False


def _translation_worker():
    try:
        while True:
            with _TRANS_LOCK:
                if not _TRANS_QUEUE:
                    _TRANS_WORKER["running"] = False
                    return
                title, desc_en = _TRANS_QUEUE.pop(0)
            ok = _translate_one(title, desc_en)
            with _TRANS_LOCK:
                _TRANS_WORKER["done"] += 1
                _TRANS_WORKER["pending"] = len(_TRANS_QUEUE)
                if not ok:
                    _TRANS_WORKER["failed"].append(title)
                    _TRANS_WORKER["failed"] = _TRANS_WORKER["failed"][-20:]
                    _TRANS_SEEN.discard(title)  # 失敗的允許下次掃描重試
                _TRANS_WORKER["change"] = time.time()
    except Exception:
        with _TRANS_LOCK:
            _TRANS_WORKER["running"] = False


def _schedule_translations(templates: list[dict]) -> None:
    """掃描後排程翻譯：英文介紹存在但本地無中文翻譯的範本 → 背景翻譯並保存。"""
    changed = False
    with _TRANS_LOCK:
        for t in templates:
            title = str(t.get("title") or "").strip()
            desc = str(t.get("description") or "").strip()
            if not title or not desc:
                continue
            if title in _TRANS_SEEN:
                continue
            if title in _DESC_ZH and _DESC_ZH.get(title):
                continue
            _TRANS_QUEUE.append((title, desc))
            _TRANS_SEEN.add(title)
            changed = True
        if changed:
            _TRANS_WORKER["pending"] = len(_TRANS_QUEUE)
            if not _TRANS_WORKER["running"]:
                _TRANS_WORKER["running"] = True
                threading.Thread(target=_translation_worker, daemon=True).start()


# 範本縮圖：本地範本檔名 → 官方 workflow template slug（thumbs_map.json，aria2 已下載至 thumbs/）
THUMBS_DIR = _HERE / "thumbs"
_THUMBS_MAP: dict[str, str] = {}
_THUMBS_UNMATCHED: set[str] = set()  # 已確認官方無對應縮圖的範本（避免每次掃描重試）
try:
    _tm = json.loads((_HERE / "thumbs_map.json").read_text(encoding="utf-8"))
    _THUMBS_MAP = {k: v["slug"] for k, v in _tm.get("thumbs", {}).items()}
    _THUMBS_UNMATCHED = set(_tm.get("unmatched", []))
except Exception:
    pass


# ---------- 新範本／首次掃描：自動抓取官方縮圖（背景執行） ----------

_THUMBS_LOCK = threading.Lock()
_THUMBS_WORKER: dict = {"running": False, "last_start": 0.0, "pending": 0, "done": 0,
                        "failed": [], "change": 0}
_OFFICIAL_CATALOG_FILE = _HERE / "thumbs_official.json"


def _official_catalog(force: bool = False) -> list[dict]:
    """官方 Comfy-Org/workflow_templates 的 templates/ 目錄 slug+title 清單（本地快取 7 天）。

    新範本需要縮圖對應時才抓取：GitHub API 列出 templates/，再併行讀每份 <slug>.json 的 title。
    """
    now = time.time()
    if not force and _OFFICIAL_CATALOG_FILE.is_file():
        try:
            d = json.loads(_OFFICIAL_CATALOG_FILE.read_text(encoding="utf-8"))
            items = d.get("items") or []
            # 舊快取沒有 webp 欄位（下載位址）→ 視為過期，強制重建
            if (now - d.get("updated", 0) < 7 * 86400 and items
                    and all("webp" in it for it in items)):
                return items
        except Exception:
            pass
    items: list[dict] = []
    try:
        # git trees recursive API：一次回傳完整 templates/ 路徑清單（contents API 每頁 1000 會截斷）
        req = urllib.request.Request(
            "https://api.github.com/repos/Comfy-Org/workflow_templates/git/trees/main?recursive=1",
            headers={"User-Agent": "doubao-comfyui-downloader"})
        with urllib.request.urlopen(req, timeout=60) as r:
            tree = json.loads(r.read().decode("utf-8"))
        paths = [t.get("path", "") for t in tree.get("tree", [])]
        slugs = sorted({
            p[len("templates/"):-5]
            for p in paths
            if p.startswith("templates/") and p.endswith(".json")
        })
        # 每個 slug 只挑「最小序號」的官方輸出圖（<slug>-N.webp / -N.png）
        webp: dict[str, tuple[int, str]] = {}
        for p in paths:
            m = re.match(r"templates/(.+?)-(\d+)\.(webp|png)$", p)
            if m:
                slug, n, ext = m.group(1), int(m.group(2)), m.group(3)
                if slug in slugs and (slug not in webp or n < webp[slug][0]):
                    webp[slug] = (n, f"{slug}-{n}.{ext}")

        def _title(slug: str) -> dict:
            try:
                with urllib.request.urlopen(
                        f"https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/{slug}.json",
                        timeout=20) as r:
                    d = json.loads(r.read().decode("utf-8"))
                t = slug
                try:
                    subs = d.get("definitions", {}).get("subgraphs", [])
                    if subs and subs[0].get("name"):
                        t = str(subs[0]["name"])  # 人類可讀的工作流家族標題
                except Exception:
                    pass
            except Exception:
                t = slug
            return {"slug": slug, "title": t, "webp": webp[slug][1]}

        with ThreadPoolExecutor(max_workers=8) as pool:
            items = [it for it in pool.map(_title, slugs) if it["slug"] in webp]
    except Exception:
        items = []
    if items:
        try:
            _OFFICIAL_CATALOG_FILE.write_text(
                json.dumps({"updated": now, "items": items}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return items


def _match_official(title: str, items: list[dict]) -> str | None:
    """統一評分比對官方範本 slug（校準於 707 筆官方目錄）。

    分層加分：正規化精確（+40）＞ 包含（較短 ≥ 較長 60%，+25）＞ 純 token；
    每筆候選須與標題至少共享 1 個 token（避免官方 subgraph 標題錯配，
    例如「Remove Background (BiRefNet)」官方標題卻在 3D 模板上）。
    基礎分 = 10*共享 − 3*漏 − 3*多餘 + ratio，≥ 25 才接受。
    """
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    def toks(s: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", s.lower()))

    nt = norm(title)
    tt = toks(title)
    if not tt:
        return None
    best, best_score = None, 0.0
    for it in items:
        otitle = str(it.get("title") or it.get("slug") or "")
        ot = toks(otitle)
        shared = len(tt & ot)
        if shared < 1:
            continue  # 標題與 slug 名稱完全無關 → 官方標題錯配，跳過
        miss = len(tt - ot)
        extra = len(ot - tt)
        r = max(
            difflib.SequenceMatcher(None, "".join(sorted(tt)), "".join(sorted(ot))).ratio(),
            difflib.SequenceMatcher(None, title.lower(), otitle.lower()).ratio(),
        )
        base = 10 * shared - 3 * miss - 3 * extra + r
        on = norm(otitle)
        if on == nt:
            base += 40
        elif (max(len(on), len(nt)) >= 8 and min(len(on), len(nt)) >= 0.6 * max(len(on), len(nt))
              and (nt in on or on in nt)):
            base += 25
        if base > best_score:
            best, best_score = it["slug"], base
    return best if best_score >= 25 else None


def _thumbs_worker(titles: list[str]):
    st = _THUMBS_WORKER
    st["pending"] = len(titles)
    st["done"] = 0
    st["failed"] = []
    try:
        items = _official_catalog()
        if not items:
            st["failed"] = list(titles)
            return
        pairs: dict[str, tuple[str, str]] = {}
        for t in titles:
            m = _match_official(t, items)
            if m:
                it = next((x for x in items if x["slug"] == m), None)
                pairs[t] = (m, it["webp"] if it else f"{m}-1.webp")
        unmatched = [t for t in titles if t not in pairs]

        map_path = _HERE / "thumbs_map.json"
        try:
            m = json.loads(map_path.read_text(encoding="utf-8")) if map_path.is_file() else {}
        except Exception:
            m = {}
        thumbs = m.setdefault("thumbs", {})

        if pairs:
            # 只下載本機還沒有的縮圖檔（使用 catalog 的 webp 位址，slug 檔案名保持不變）
            need_dl = {slug: f"https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/{w}"
                       for (_t, (slug, w)) in pairs.items()
                       if not (THUMBS_DIR / f"{slug}.webp").is_file()}
            aria = _find_aria2c()
            if need_dl and aria:
                inp = _HERE / "thumbs_aria2_new.txt"
                inp.write_text("\n".join(f"{u}\n  out={s}.webp" for s, u in need_dl.items()),
                               encoding="utf-8")
                cmd = [aria, "-i", str(inp), "-d", str(THUMBS_DIR),
                       "-x", "16", "-s", "16",
                       "--continue=true", "--allow-overwrite=true", "--auto-file-renaming=false",
                       "--file-allocation=none", "--console-log-level=warn"]
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
                    proc.wait(timeout=300)
                except Exception:
                    pass
            ok: list[str] = []
            for t, (s, _w) in pairs.items():
                if (THUMBS_DIR / f"{s}.webp").is_file():
                    thumbs[t] = {"slug": s, "webp": f"{s}.webp"}
                    _THUMBS_MAP[t] = s
                    ok.append(t)
            st["done"] = len(ok)
            st["failed"] = unmatched + [t for t in pairs if t not in ok]
            if ok:
                st["change"] += len(ok)
                _cache["mtime"] = -1.0  # 強制下次 /api/templates 重新掃描，附上新縮圖
        else:
            st["failed"] = list(unmatched)

        if unmatched:
            um = set(m.get("unmatched", [])) | set(unmatched)
            m["unmatched"] = sorted(um)
            _THUMBS_UNMATCHED.update(unmatched)

        try:
            map_path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    except Exception as e:
        st["failed"] = [f"{t} ({e})" for t in titles]
    finally:
        st["running"] = False


def _start_thumbs_refresh(titles: list[str]):
    """背景啟動縮圖更新（不阻塞掃描）。"""
    with _THUMBS_LOCK:
        if _THUMBS_WORKER["running"]:
            return
        _THUMBS_WORKER["running"] = True
        _THUMBS_WORKER["last_start"] = time.time()
        _THUMBS_WORKER["pending"] = len(titles)
        _THUMBS_WORKER["done"] = 0
        _THUMBS_WORKER["failed"] = []
    threading.Thread(target=_thumbs_worker, args=(list(titles),), daemon=True).start()


def _thumbs_status() -> dict:
    st = _THUMBS_WORKER
    cat_items = 0
    cat_age = None
    try:
        if _OFFICIAL_CATALOG_FILE.is_file():
            d = json.loads(_OFFICIAL_CATALOG_FILE.read_text(encoding="utf-8"))
            cat_items = len(d.get("items", []))
            cat_age = int(time.time() - d.get("updated", 0))
    except Exception:
        pass
    return {"working": st["running"], "pending": st["pending"], "done": st["done"],
            "failed": list(st["failed"]), "last_change": st["change"],
            "last_start": st["last_start"], "catalog_items": cat_items,
            "catalog_age_sec": cat_age}


# 容量誤差容許（GB）：本機與遠端大小相差超過此值視為「未下載齊全」
_SIZE_TOLERANCE_GB = 0.06


def _find_model_with_root(directory: str, filename: str) -> tuple[Path | None, Path | None]:
    """在主 models 目錄 + 額外設定的模型目錄（extra_model_paths.yaml）中尋找檔案，
    並回傳 (檔案路徑, 找到它的根目錄)。"""
    roots = [MODELS_DIR] + EXTRA_MODEL_DIRS
    for root in roots:
        p = root / directory / filename
        if p.is_file():
            return p, root
    return None, None


def _missing_models(models: list[dict]) -> list[dict]:
    """比對所需檔案是否下載齊全（只做便宜的本地檢查，不做網路 HEAD）。

    缺檔判定：
      - 檔案不存在；
      - 存在但同檔名的 .aria2 控制檔還在（上次中斷未完成）；
      - 存在、遠端大小已知，但與本機大小明顯不符（下載不齊全）。
    """
    missing: list[dict] = []
    for m in models:
        directory = m.get("directory", "")
        name = m.get("name", "")
        p, root = _find_model_with_root(directory, name)
        sz = _model_size(directory, name)
        if sz is not None:
            m["disk_size_gb"] = sz
        if root is not None:
            # 標示找到檔案的位置（主目錄 models / 額外設定目錄）
            m["root"] = root.name if root == MODELS_DIR else str(root)
        url = m.get("url")
        if url and url in _remote_size_cache and _remote_size_cache[url]:
            m["remote_size_gb"] = _remote_size_cache[url]
        partial = False
        if p:
            if Path(str(p) + ".aria2").is_file():
                partial = True  # 上次中斷，aria2 控制檔還在 → 未下載齊全
            else:
                rsz = m.get("remote_size_gb")
                if rsz and sz is not None and abs(sz - rsz) > _SIZE_TOLERANCE_GB:
                    partial = True  # 容量不符 → 未下載齊全
                    m["size_mismatch"] = True
        if p is None:
            missing.append(m)
        elif partial:
            m["incomplete"] = True
            missing.append(m)
    return missing


def _prune_state(files: list[Path]) -> None:
    """把狀態檔中「已被刪除的範本」殘留紀錄清掉（confirmed 與 known）。"""
    global _state
    file_names = {f.name for f in files}
    changed = False
    stale_confirmed = set(_state.get("confirmed", [])) - file_names
    if stale_confirmed:
        _state["confirmed"] = sorted(set(_state["confirmed"]) - stale_confirmed)
        changed = True
    stale_known = set(_state.get("known", {})) - file_names
    if stale_known:
        for k in stale_known:
            _state["known"].pop(k, None)
        changed = True
    if changed:
        _save_state()


def _do_scan(force: bool = False) -> list[dict]:
    """掃描範本並比對所需檔案是否下載齊全。

    - 主要針對「完全沒有引用外部 API、純本地運作」的範本；
      這類範本的缺檔才需要下載。
    - 已「確認完整下載」的範本（scan_state.json）→ 下次開啟直接跳過比對；
    - 範本檔案新增或內容變更（mtime 不同）→ 移出確認清單、重新掃描；
    - 遠端容量（HEAD）只由 /api/fetch-sizes 針對「還沒有容量顯示」的缺檔確認，
      已有容量的模型不再重複比對。
    """
    global _state
    if not BLUEPRINTS_DIR.is_dir():
        return []

    newest = 0.0
    files: list[Path] = []
    for f in sorted(BLUEPRINTS_DIR.iterdir()):
        if f.suffix == ".json":
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
            files.append(f)

    with _cache_lock:
        if not force and _cache["mtime"] == newest and _cache["templates"]:
            # 快取命中時也附上 HEAD 快取的遠端大小（fetch-sizes 之後立即可見）
            for t in _cache["templates"]:
                for m in t["models"]:
                    url = m.get("url")
                    if url and url in _remote_size_cache and _remote_size_cache[url]:
                        m["remote_size_gb"] = _remote_size_cache[url]
            _prune_state(files)  # 快取命中也要清理已刪除範本的殘留紀錄
            return _cache["templates"]

        confirmed_set = set(_state["confirmed"])
        state_dirty = False
        # 首次掃描只建立基準（known 快照），不把全部範本標成「新」；
        # 之後偵測到「新增檔案」或「內容變更」才算新範本
        had_baseline = bool(_state["known"])
        results: list[dict] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                _log_scan_error(f"{f.name}: {e}")
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                mtime = 0.0
            title = f.stem
            # try to extract title from JSON
            if isinstance(data, dict):
                for tk in ("title", "name", "_meta"):
                    if tk in data and isinstance(data[tk], str):
                        title = data[tk]
                        break
            is_api = _detect_api(f.read_text(encoding="utf-8")) or f.stem.lower().startswith("api_")
            content_changed = _state["known"].get(f.name) != mtime
            is_new = had_baseline and (f.name not in _state["known"] or content_changed)
            if is_new:
                # 新增/變更的範本：移出確認清單、重新比對所需檔案
                confirmed_set.discard(f.name)
                state_dirty = True
            _state["known"][f.name] = mtime

            models = _extract_models(data)
            description = _extract_description(data)  # 官方介紹說明（definitions 內嵌）
            desc_zh = _DESC_ZH.get(title) or _DESC_ZH.get(f.stem) or ""  # 中文介紹（翻譯維護，更新時同步）
            slug = _THUMBS_MAP.get(f.stem) or _THUMBS_MAP.get(title)
            thumb = f"/thumbs/{slug}.webp" if slug and (THUMBS_DIR / f"{slug}.webp").is_file() else ""
            confirmed = (f.name in confirmed_set) and not content_changed
            if confirmed:
                # 已確認完整下載 → 不再比對；仍輸出模型清單與快取內的大小供檢視
                missing: list[dict] = []
                for m in models:
                    directory = m.get("directory", "")
                    name = m.get("name", "")
                    sz = _model_size(directory, name)
                    if sz is not None:
                        m["disk_size_gb"] = sz
                    _, root = _find_model_with_root(directory, name)
                    if root is not None:
                        m["root"] = root.name if root == MODELS_DIR else str(root)
                    url = m.get("url")
                    if url and url in _remote_size_cache and _remote_size_cache[url]:
                        m["remote_size_gb"] = _remote_size_cache[url]
            else:
                missing = _missing_models(models)
                if not is_api and not missing:
                    confirmed_set.add(f.name)
                    state_dirty = True

            results.append({
                "title": title,
                "file": f.name,
                "description": description,
                "description_zh": desc_zh,
                "thumb": thumb,
                "missing": missing,
                "models": models,
                "missing_count": len(missing),
                "is_api": is_api,
                "confirmed": f.name in confirmed_set,
                "is_new": is_new,
            })

        if state_dirty:
            _state["confirmed"] = sorted(confirmed_set)
            _save_state()

        # 清理：狀態檔中「已被刪除的範本」（不在目前 blueprints 目錄）的殘留紀錄
        _prune_state(files)

        # 首次掃描／新範本：自動為缺少縮圖的範本抓取官方縮圖（背景執行，不阻塞掃描）
        need_thumb = [t["title"] for t in results
                      if not t.get("thumb") and t["title"] not in _THUMBS_UNMATCHED]
        if need_thumb:
            _start_thumbs_refresh(need_thumb)

        # 新範本：官方英文介紹 → 自動翻譯成中文，en/zh 都寫回 descriptions.json
        _schedule_translations(results)

        _cache["mtime"] = newest
        _cache["templates"] = results
        return results


def scan(force: bool = False) -> list[dict]:
    """對外掃描入口：單一範本或檔案系統的任何意外例外不中斷伺服器，
    回傳上次快取並記錄錯誤（避免「重新掃描」因偶發例外變成 500 / fail）。"""
    try:
        return _do_scan(force)
    except Exception as e:
        _log_scan_error(f"scan: {e}")
        return list(_cache["templates"])


# ---------- download queue ----------

queue: list[dict] = []  # {"name","url","directory","status","pct","speed","size","pid"}
queue_lock = threading.Lock()


def _find_aria2c() -> str | None:
    """Search common install locations for aria2c. Auto-download if missing."""
    import shutil
    # 1. PATH
    found = shutil.which("aria2c")
    if found:
        return found
    # 2. Common Windows locations
    local_dir = _HERE / "bin"
    candidates = [
        local_dir / "aria2c.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\aria2.aria2_Microsoft.Winget.Source_8wekyb3d8bbwe\aria2-1.37.0-win-64bit-build1\aria2c.exe"),
        r"C:\Program Files\aria2\aria2c.exe",
        r"C:\aria2\aria2c.exe",
        "/usr/bin/aria2c",
        "/usr/local/bin/aria2c",
    ]
    for c in candidates:
        p = str(c) if isinstance(c, Path) else c
        if os.path.isfile(p):
            return p
    # 3. Search WinGet packages folder recursively
    winget_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_dir):
        for root, dirs, files in os.walk(winget_dir):
            for f in files:
                if f.lower() == "aria2c.exe":
                    return os.path.join(root, f)
    # 4. Auto-download aria2
    return _download_aria2()


def _download_aria2() -> str | None:
    """Download aria2c standalone binary to ./bin/ if not found."""
    import zipfile
    local_dir = _HERE / "bin"
    local_dir.mkdir(exist_ok=True)
    exe = local_dir / "aria2c.exe"
    if exe.is_file():
        return str(exe)
    urls = [
        "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip",
    ]
    zip_path = local_dir / "aria2.zip"
    for url in urls:
        try:
            print(f"Downloading aria2 from {url} ...")
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                for name in zf.namelist():
                    if name.endswith("aria2c.exe"):
                        with zf.open(name) as src, open(exe, "wb") as dst:
                            dst.write(src.read())
                        break
            zip_path.unlink(missing_ok=True)
            if exe.is_file():
                print(f"aria2c installed to {exe}")
                return str(exe)
        except Exception as e:
            print(f"Failed to download aria2: {e}")
    return None


# 併發上限：同時最多執行 MAX_CONCURRENT_DOWNLOADS 個 aria2 進程
# （可用環境變數 DL_MAX_CONCURRENT 調整，避免 N 檔 × 16 連線打爆頻寬）
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("DL_MAX_CONCURRENT", "4"))
_dl_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


def _mirror_urls(url: str) -> list[str]:
    """產生鏡像競速候選 URL：huggingface.co → hf-mirror.com。"""
    urls = [url]
    if "huggingface.co" in url:
        urls.append(url.replace("huggingface.co", "hf-mirror.com"))
    return urls


def _download_worker(item: dict):
    aria = _find_aria2c()
    if not aria:
        item["status"] = "error"
        item["error"] = "aria2c not found"
        return

    dest_dir = MODELS_DIR / item["directory"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 多 URL = 鏡像競速：aria2 原生支援同一檔案同時從多個鏡像取最快分段
    urls = _mirror_urls(item["url"])
    cmd = [
        aria,
        "-x", "16", "-s", "16", "-k", "1M",
        "--continue=true",              # 斷點續傳
        "--auto-file-renaming=false",   # 不產生 .1 重複檔
        "--allow-overwrite=true",       # 覆蓋不完整的舊檔
        "--file-allocation=none",       # NTFS 下加快啟動
        "--max-tries=5", "--retry-wait=2", "--timeout=60",
        "--console-log-level=notice",
        "--summary-interval=1",
        "-d", str(dest_dir),
        "-o", item["name"],
    ] + urls
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        item["pid"] = proc.pid
        tail: list[str] = []  # 保留最後幾行輸出，失敗時作為錯誤訊息
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-5:]
            m = re.search(r"\((\d+)%\)", line)
            if m:
                item["pct"] = int(m.group(1))
            if "DL:" in line:
                m2 = re.search(r"DL:([\d.]+[KMG])", line)
                if m2:
                    item["speed"] = m2.group(1)
        proc.wait()
        if item.get("status") == "cancelled":
            return  # 已被取消（cancel-one/cancel），保留 cancelled 狀態
        if proc.returncode == 0:
            item["status"] = "done"
            item["pct"] = 100
            # 失效大小快取：讓 refresh 後新檔案的大小與「已齊全」狀態立即正確
            _size_cache.pop((item["directory"], item["name"]), None)
        else:
            item["status"] = "error"
            if not item.get("error"):
                item["error"] = "; ".join(tail[-3:])[:400] if tail else f"aria2c 退出碼 {proc.returncode}"
    except Exception as e:
        item["status"] = "error"
        item["error"] = str(e)


def add_to_queue(items: list[dict]):
    with queue_lock:
        for it in items:
            url = it.get("url")
            if not url:
                continue  # 缺 URL 的項目直接跳過，不再執行緒靜默崩潰
            # skip if already queued
            if any(q["name"] == it["name"] and q["status"] in ("pending", "downloading") for q in queue):
                continue
            entry = {
                "name": it["name"],
                "url": url,
                "directory": it.get("directory", ""),
                "status": "pending",
                "pct": 0,
                "speed": "",
                # 用遠端大小快取預估檔案位元組數（供總大小 / ETA 計算）
                "size_bytes": int(_remote_size_cache.get(url) * 1024**3) if _remote_size_cache.get(url) else None,
            }
            queue.append(entry)
            threading.Thread(target=_run_one, args=(entry,), daemon=True).start()


def _run_one(entry: dict):
    with _dl_semaphore:
        entry["status"] = "downloading"
        _download_worker(entry)


# ---------- queue summary / helpers ----------

def _port_open_local(port: int) -> bool:
    """Check whether a local port is accepting connections."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


_SPEED_RE = re.compile(r"([\d.]+)([KMG])")


def _parse_speed(speed: str) -> float:
    """'12.3M'（aria2 摘要格式）→ bytes/s；無法解析回傳 0。"""
    m = _SPEED_RE.search(speed or "")
    if not m:
        return 0.0
    return float(m.group(1)) * {"K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]


def _queue_summary() -> dict:
    """佇列完整狀態：前端進度列需要的所有欄位都在這裡。"""
    with queue_lock:
        items = list(queue)
    total = len(items)
    done = sum(1 for q in items if q["status"] == "done")
    downloading = sum(1 for q in items if q["status"] == "downloading")
    pending = sum(1 for q in items if q["status"] == "pending")
    failed = sum(1 for q in items if q["status"] == "error")
    cancelled = sum(1 for q in items if q["status"] == "cancelled")
    pct = round(sum(100 if q["status"] == "done" else q.get("pct", 0) for q in items) / total) if total else 0

    # 總大小（未完成項目）＋ ETA 粗估（剩餘位元組 / 總下載速率）
    total_size_gb = 0.0
    remaining_bytes = 0.0
    speed_bytes = 0.0
    for q in items:
        sz = q.get("size_bytes")
        if q["status"] in ("pending", "downloading") and sz:
            total_size_gb += sz / 1024**3
            remaining_bytes += sz * (1 - q.get("pct", 0) / 100)
        if q["status"] == "downloading":
            speed_bytes += _parse_speed(q.get("speed", ""))
    eta_sec = round(remaining_bytes / speed_bytes) if speed_bytes and remaining_bytes > 0 else None

    return {
        "items": items, "total": total, "done": done,
        "downloading": downloading, "pending": pending,
        "failed": failed, "cancelled": cancelled,
        "pct": pct, "ok": True,
        "total_size_gb": round(total_size_gb, 2),
        "eta_sec": eta_sec,
    }


# ---------- HTTP ----------

# 開啟資料夾視窗（explorer.exe）並最大化；os.startfile 無法控制視窗狀態
def _open_folder_maximized(path: Path | str) -> None:
    p = str(path)
    if os.name != "nt":
        subprocess.Popen(["xdg-open", p])
        return
    subprocess.Popen(["explorer", p])
    time.sleep(0.7)
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        target = Path(p).name.lower()
        found: list = []

        def _cb(hwnd, lparam):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "CabinetWClass":  # 檔案總管視窗類別
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = buf.value.lower()
            if t.startswith(target) or (target in t and " - " in t):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 3)   # SW_MAXIMIZE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/accelerated-downloader"):
            ui = (_HERE / "ui.html").read_text(encoding="utf-8")
            self._html(ui)
        elif path == "/api/templates":
            tpls = scan()
            self._json({
                "templates": tpls,
                "scan_errors": list(_scan_errors),
                "first_scan": not STATE_FILE.is_file(),
                "new_count": sum(1 for t in tpls if t.get("is_new")),
            })
        elif path == "/api/fetch-sizes":
            # 並行 HEAD 取得缺檔的遠端大小，結果寫入 _remote_size_cache；
            # scan() 會從快取附上 remote_size_gb，refresh 後也不會遺失
            templates = scan()
            missing_urls = set()
            for t in templates:
                for m in t["missing"]:
                    if m.get("url") and m["url"] not in _remote_size_cache:
                        missing_urls.add(m["url"])
            if missing_urls:
                with ThreadPoolExecutor(max_workers=10) as pool:
                    list(pool.map(_remote_size_gb, missing_urls))
            self._json({"ok": True, "count": len(missing_urls)})
        elif path == "/api/thumbs-status":
            # 新範本縮圖自動更新的背景工作狀態（前端顯示「縮圖更新中…」並在完成後重整）
            self._json(_thumbs_status())
        elif path == "/api/translate-status":
            # 新範本自動翻譯（en → zh）的背景工作狀態（前端顯示翻譯狀態列）
            with _TRANS_LOCK:
                self._json({
                    "working": _TRANS_WORKER["running"],
                    "pending": _TRANS_WORKER["pending"],
                    "done": _TRANS_WORKER["done"],
                    "failed": _TRANS_WORKER["failed"],
                    "last_change": _TRANS_WORKER["change"],
                })
        elif path == "/api/env-check":
            aria_ok = _find_aria2c() is not None
            self._json({
                "aria2c": aria_ok,
                "aria2c_installed": aria_ok,
                "comfyui_running": _port_open_local(8188),
                "extra_dirs": [str(d) for d in EXTRA_MODEL_DIRS if d.is_dir()],
            })
        elif path == "/api/queue":
            self._json(_queue_summary())
        elif path == "/api/open-models":
            try:
                _open_folder_maximized(MODELS_DIR)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/open-extra-dirs":
            # 開啟額外設定的模型目錄（extra_model_paths.yaml base_path），有幾個開幾個
            dirs = [d for d in EXTRA_MODEL_DIRS if d.is_dir()]
            try:
                for d in dirs:
                    _open_folder_maximized(d)
                self._json({"ok": True, "count": len(dirs)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path.startswith("/thumbs/"):
            # 範本縮圖靜態檔（官方 workflow 輸出圖，aria2 已下載至 thumbs/）
            name = path.split("/", 2)[-1]
            if re.fullmatch(r"[\w.-]+\.webp", name):
                p = THUMBS_DIR / name
                if p.is_file():
                    body = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/webp")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.send_response(404)
            self.end_headers()
        elif path.startswith("/open-blueprint"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._open_blueprint(qs.get("file", [None])[0])
        else:
            self.send_response(404)
            self.end_headers()

    def _open_blueprint(self, file: str | None = None):
        """開啟範本前檢查 ComfyUI(8188)：已執行才導向；未執行則顯示提示頁。

        依使用者要求：此下載器不會自動啟動 ComfyUI。需要 ComfyUI 已執行才能載入藍圖。
        提示頁每 3 秒自動重試，偵測到 ComfyUI 啟動後自動跳轉。
        """
        import shutil
        import socket

        def _port_open(port: int) -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return True
            except OSError:
                return False

        if not _port_open(8188):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self._comfyui_pending_page(file).encode("utf-8"))
            return

        # Redirect to ComfyUI, optionally loading the requested blueprint
        redirect_url = "http://127.0.0.1:8188/"
        if file:
            src = BLUEPRINTS_DIR / file
            if src.is_file():
                try:
                    workflows_dir = ROOT / "user" / "default" / "workflows"
                    workflows_dir.mkdir(parents=True, exist_ok=True)
                    dst = workflows_dir / file
                    if not dst.exists():  # 不覆蓋同名檔案
                        shutil.copyfile(src, dst)
                    redirect_url += "?filename=" + urllib.parse.quote(file)
                except Exception:
                    pass  # 複製失敗仍導向 ComfyUI 主頁
        self.send_response(302)
        self.send_header("Location", redirect_url)
        self.end_headers()

    @staticmethod
    def _comfyui_pending_page(file: str | None = None) -> str:
        """ComfyUI 未執行時的等待頁：不自動啟動，提示使用者手動啟動後自動跳轉。"""
        root_str = str(ROOT)
        retry_url = "/open-blueprint" + (f"?file={urllib.parse.quote(file)}" if file else "")
        return f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8">
<title>等待 ComfyUI 啟動</title>
<meta http-equiv="refresh" content="3;url={retry_url}">
<style>
body{{background:#16161e;color:#e6e6eb;font-family:"Microsoft JhengHei",Segoe UI,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#1e1e2a;border:1px solid #2e2e3e;border-radius:14px;padding:32px 40px;max-width:640px;text-align:center}}
h1{{font-size:20px;margin:0 0 12px}}
p{{color:#9aa0b4;font-size:14px;line-height:1.7;margin:8px 0}}
code{{background:#26263a;padding:2px 8px;border-radius:6px;color:#9ecbff}}
a{{color:#7aa2ff;text-decoration:none}}
.spin{{display:inline-block;width:14px;height:14px;border:2px solid #3a3a52;border-top-color:#7aa2ff;border-radius:50%;animation:r 0.9s linear infinite;vertical-align:-2px;margin-right:8px}}
@keyframes r{{to{{transform:rotate(360deg)}}}}
</style></head><body><div class="card">
<h1><span class="spin"></span>ComfyUI 尚未執行</h1>
<p>下載器是獨立工具，<b>不會自動啟動 ComfyUI</b>。開啟範本前請先自行啟動 ComfyUI：</p>
<p>在 <code>{root_str}</code> 資料夾執行<br><code>run_nvidia_gpu.bat</code>（或 <code>run_cpu.bat</code>）</p>
<p>本頁每 3 秒自動偵測，ComfyUI 啟動後將自動跳轉。<br>
若 ComfyUI 改用其他連接埠，請先<a href="/">返回下載器</a>設定。</p>
</div></body></html>"""

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/refresh":
            try:
                tpls = scan(force=True)
            except Exception as e:
                tpls = []
                _log_scan_error(f"refresh: {e}")
            self._json({
                "templates": tpls,
                "scan_errors": list(_scan_errors),
                "first_scan": not STATE_FILE.is_file(),
                "new_count": sum(1 for t in tpls if t.get("is_new")),
            })
        elif path == "/api/queue/add":
            add_to_queue(body.get("items", []))
            self._json({"ok": True})
        elif path == "/api/queue/clear-done":
            with queue_lock:
                queue[:] = [q for q in queue if q["status"] not in ("done", "error", "cancelled")]
            self._json({"ok": True})
        elif path == "/api/queue/cancel-one":
            name = body.get("name", "")
            with queue_lock:
                for q in queue:
                    if q["name"] == name:
                        if q.get("pid"):
                            try:
                                os.kill(q["pid"], 9)
                            except Exception:
                                pass
                        q["status"] = "cancelled"
                        q["speed"] = ""
                        break
            self._json({"ok": True})
        elif path == "/api/queue/retry":
            name = body.get("name", "")
            with queue_lock:
                for q in queue:
                    if q["name"] == name and q["status"] in ("error", "cancelled"):
                        q["status"] = "pending"
                        q["pct"] = 0
                        q["speed"] = ""
                        q.pop("error", None)
                        q.pop("pid", None)
                        threading.Thread(target=_run_one, args=(q,), daemon=True).start()
                        break
            self._json({"ok": True})
        elif path == "/api/queue/cancel":
            with queue_lock:
                for q in queue:
                    if q.get("pid"):
                        try:
                            os.kill(q["pid"], 9)
                        except Exception:
                            pass
                queue.clear()
            self._json({"ok": True})
        elif path == "/api/queue/remove":
            name = body.get("name", "")
            with queue_lock:
                queue[:] = [q for q in queue if q["name"] != name]
            self._json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


def _find_python_exe() -> str:
    """Locate a Python for launching ComfyUI: portable embedded → venv → system python."""
    import shutil
    for cand in (
        ROOT.parent / "python_embeded" / "python.exe",  # 可攜版
        ROOT / "venv" / "Scripts" / "python.exe",        # venv
    ):
        if cand.is_file():
            return str(cand)
    return shutil.which("python") or "python"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--root", help="ComfyUI 根目錄（內含 main.py 與 models），省略時自動偵測上一層")
    args = ap.parse_args()
    if args.root:
        os.environ["COMFYUI_ROOT"] = args.root
    try:
        global ROOT, MODELS_DIR, TEMPLATES_DIR, BLUEPRINTS_DIR, EXTRA_MODEL_DIRS
        ROOT = _find_comfyui_root()
    except RuntimeError as e:
        sys.exit(f"[ERROR] {e}")
    MODELS_DIR = ROOT / "models"
    TEMPLATES_DIR = _find_templates_dir()
    BLUEPRINTS_DIR = TEMPLATES_DIR
    EXTRA_MODEL_DIRS = _load_extra_dirs()
    print(f"Standalone downloader on http://127.0.0.1:{args.port}")
    print(f"ComfyUI root: {ROOT}")
    print(f"Blueprints: {BLUEPRINTS_DIR}")
    print(f"Models: {MODELS_DIR}")
    print(f"Extra dirs: {EXTRA_MODEL_DIRS}")
    # Environment check
    print("\n[Environment Check]")
    aria = _find_aria2c()
    print(f"  aria2c: {aria or 'NOT FOUND'}")
    if not aria:
        print("  ERROR: aria2c not found and auto-download failed. Please install manually.")
    # pre-warm
    scan()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
