#!/usr/bin/env python3
"""
Samsung The Frame Art Studio & Uploader Daemon
----------------------------------------------
- Standalone HTTP server & API daemon for Samsung The Frame TVs (2017-2026+).
- Ingests high-resolution artwork from web UI, museum APIs, or Bing 4K Daily Wallpaper.
- Direct Samsung The Frame TV upload, art mode display, and matte styling via samsungtvws.
- Advanced recursive folder management, multi-playlist curation, and slideshow rotation.
- Built-in Atkinson Dithering engine with highlight shielding for 800x480 ESP32 Waveshare E-Paper.
- Multi-tier tidy-up: deletes local media, cached thumbnails, empty directories, and TV storage.
- Operates standalone (direct port 8098) or behind Nginx / Home Assistant reverse proxy (/api/frame/*).
"""

import io
import os
import sys
import json
import time
import random
import hashlib
import threading
import urllib.parse
import urllib.request
import ssl
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import numpy as np
import requests

# ---------------------------------------------------------
# Configuration Loading
# ---------------------------------------------------------
DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8098,
        "data_dir": "./data",
        "epaper_output_dir": "./data/epaper"
    },
    "tv": {
        "ip": os.getenv("FRAME_TV_IP", "192.168.1.100"),
        "name": os.getenv("FRAME_TV_NAME", "SamsungFrame"),
        "port": int(os.getenv("FRAME_TV_PORT", "8002")),
        "timeout": int(os.getenv("FRAME_TIMEOUT", "15"))
    },
    "esp32": {
        "enabled": False,
        "ip": os.getenv("ESP32_IP", "192.168.1.101"),
        "button_endpoint": "/button/refresh_picture_display/press"
    },
    "home_assistant": {
        "enabled": False,
        "host": os.getenv("HA_HOST", "192.168.1.50"),
        "port": int(os.getenv("HA_PORT", "8123")),
        "token": os.getenv("HA_TOKEN", ""),
        "title_sensor_entity": "sensor.eink_picture_title",
        "refresh_button_entity": "button.e_ink_refresh_picture_display"
    }
}

CONFIG_PATH = os.getenv("FRAME_CONFIG_PATH", "config.json")
config = json.loads(json.dumps(DEFAULT_CONFIG))

if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
            for section, vals in user_cfg.items():
                if isinstance(vals, dict) and section in config:
                    config[section].update(vals)
                else:
                    config[section] = vals
        print(f"[*] Loaded configuration from {CONFIG_PATH}")
    except Exception as e:
        print(f"[!] Warning reading {CONFIG_PATH}: {e}")
else:
    print(f"[*] {CONFIG_PATH} not found; using defaults and environment variables.")

# Apply environment variable overrides if provided
if os.getenv("FRAME_PORT"):
    config["server"]["port"] = int(os.getenv("FRAME_PORT"))
if os.getenv("FRAME_DATA_DIR"):
    config["server"]["data_dir"] = os.getenv("FRAME_DATA_DIR")
if os.getenv("FRAME_TV_IP"):
    config["tv"]["ip"] = os.getenv("FRAME_TV_IP")
if os.getenv("FRAME_TV_NAME"):
    config["tv"]["name"] = os.getenv("FRAME_TV_NAME")
if os.getenv("ESP32_IP"):
    config["esp32"]["ip"] = os.getenv("ESP32_IP")
    config["esp32"]["enabled"] = True
if os.getenv("HA_TOKEN"):
    config["home_assistant"]["token"] = os.getenv("HA_TOKEN")
    config["home_assistant"]["enabled"] = True

SERVER_HOST = config["server"].get("host", "0.0.0.0")
SERVER_PORT = int(config["server"].get("port", 8098))
SAVE_DIR = os.path.abspath(config["server"].get("data_dir", "./data"))
EPAPER_DIR = os.path.abspath(config["server"].get("epaper_output_dir", os.path.join(SAVE_DIR, "epaper")))

TV_IP = config["tv"].get("ip", "192.168.1.100")
TV_NAME = config["tv"].get("name", "SamsungFrame")
TV_PORT = int(config["tv"].get("port", 8002))
TV_TIMEOUT = int(config["tv"].get("timeout", 15))

ESP32_ENABLED = config["esp32"].get("enabled", False)
ESP32_IP = config["esp32"].get("ip", "192.168.1.101")
ESP32_BUTTON_ENDPOINT = config["esp32"].get("button_endpoint", "/button/refresh_picture_display/press")

HA_ENABLED = config["home_assistant"].get("enabled", False)
HA_HOST = config["home_assistant"].get("host", "192.168.1.50")
HA_PORT = int(config["home_assistant"].get("port", 8123))
HA_TOKEN = config["home_assistant"].get("token", "")
HA_TITLE_SENSOR = config["home_assistant"].get("title_sensor_entity", "sensor.eink_picture_title")
HA_REFRESH_BUTTON = config["home_assistant"].get("refresh_button_entity", "button.e_ink_refresh_picture_display")

# Directory paths
GALLERY_DIR = os.path.join(SAVE_DIR, "gallery")
CACHE_DIR = os.path.join(SAVE_DIR, "cache")
CONFIG_FILE = os.path.join(SAVE_DIR, "slideshow_config.json")
CUSTOM_GALLERIES_FILE = os.path.join(SAVE_DIR, "custom_galleries.json")
TV_MAP_FILE = os.path.join(SAVE_DIR, "tv_content_map.json")

os.makedirs(GALLERY_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(EPAPER_DIR, exist_ok=True)

THUMB_WIDTH = 400
THUMB_HEIGHT = 225

# Slideshow state
slideshow_state = {
    "enabled": False,
    "interval_minutes": 15,
    "order": "random",  # 'random' or 'sequential'
    "last_rotated": 0,
    "current_image": None,
    "active_playlist": "all",  # 'all', folder name, or custom playlist ID
    "selected_images": [],  # List of relative file paths
    "matte": "none"
}

custom_galleries = []
tv_content_map = {}  # filename -> content_id (e.g. "MY_F0032")

# Load persisted states
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
            slideshow_state.update(saved)
    except Exception as e:
        print(f"Notice loading slideshow config: {e}")

if os.path.exists(CUSTOM_GALLERIES_FILE):
    try:
        with open(CUSTOM_GALLERIES_FILE, "r", encoding="utf-8") as f:
            custom_galleries = json.load(f)
    except Exception as e:
        print(f"Notice loading custom galleries: {e}")

if os.path.exists(TV_MAP_FILE):
    try:
        with open(TV_MAP_FILE, "r", encoding="utf-8") as f:
            tv_content_map = json.load(f)
    except Exception as e:
        print(f"Notice loading TV map: {e}")

def save_slideshow_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(slideshow_state, f, indent=2)
    except Exception as e:
        print(f"Error saving slideshow config: {e}")

def save_custom_galleries():
    try:
        with open(CUSTOM_GALLERIES_FILE, "w", encoding="utf-8") as f:
            json.dump(custom_galleries, f, indent=2)
    except Exception as e:
        print(f"Error saving custom galleries: {e}")

def save_tv_content_map():
    try:
        with open(TV_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(tv_content_map, f, indent=2)
    except Exception as e:
        print(f"Error saving TV content map: {e}")

def get_cache_filename(rel_path):
    h = hashlib.md5(rel_path.encode('utf-8')).hexdigest()
    return f"{h}.jpg"

def ensure_thumbnail_exists(rel_path):
    thumb_name = get_cache_filename(rel_path)
    dest_path = os.path.join(CACHE_DIR, thumb_name)
    src_path = os.path.join(GALLERY_DIR, rel_path.replace("/", os.sep))

    if not os.path.exists(src_path):
        return None

    need_generate = True
    if os.path.exists(dest_path):
        try:
            if os.path.getmtime(dest_path) >= os.path.getmtime(src_path):
                need_generate = False
        except Exception:
            need_generate = True

    if need_generate:
        try:
            with Image.open(src_path) as img:
                rgb = img.convert('RGB')
                img_w, img_h = rgb.size
                target_aspect = THUMB_WIDTH / THUMB_HEIGHT
                orig_aspect = img_w / img_h

                if orig_aspect > target_aspect:
                    crop_w = int(img_h * target_aspect)
                    left = (img_w - crop_w) // 2
                    box = (left, 0, left + crop_w, img_h)
                else:
                    crop_h = int(img_w / target_aspect)
                    top = (img_h - crop_h) // 2
                    box = (0, top, img_w, top + crop_h)

                cropped = rgb.crop(box)
                cropped.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.Resampling.LANCZOS)
                cropped.save(dest_path, format="JPEG", quality=82, optimize=True)
        except Exception as e:
            print(f"Thumbnail generation error for {rel_path}: {e}")
            return f"/api/frame/image?file={urllib.parse.quote(rel_path)}"

    return f"/api/frame/thumb?file={urllib.parse.quote(rel_path)}"

def background_cache_builder():
    """Builds thumbnail cache and tidies up orphaned cache files."""
    print("[Cache Worker] Background thumbnail builder & tidy-up worker started...")
    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    active_hashes = set()
    count = 0

    for root, dirs, files in os.walk(GALLERY_DIR):
        rel_dir = os.path.relpath(root, GALLERY_DIR)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in valid_exts and f.lower() not in ("thumbs.db", "desktop.ini"):
                rel_path = f if rel_dir == "." else os.path.join(rel_dir, f).replace("\\", "/")
                ensure_thumbnail_exists(rel_path)
                active_hashes.add(get_cache_filename(rel_path))
                count += 1
                if count % 100 == 0:
                    time.sleep(0.05)

    # Orphaned Cache Tidy-Up: Remove cache files whose original images no longer exist
    pruned_thumbs = 0
    if os.path.exists(CACHE_DIR):
        for cf in os.listdir(CACHE_DIR):
            if cf.endswith(".jpg") and cf not in active_hashes:
                try:
                    os.remove(os.path.join(CACHE_DIR, cf))
                    pruned_thumbs += 1
                except Exception:
                    pass

    print(f"[Cache Worker] Finished ({count} verified, {pruned_thumbs} orphaned thumbnails cleaned).")

def get_all_gallery_items():
    if not os.path.exists(GALLERY_DIR):
        return [], []
    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    items = []
    folders_set = set()

    for root, dirs, files in os.walk(GALLERY_DIR):
        rel_dir = os.path.relpath(root, GALLERY_DIR)
        if rel_dir == ".":
            folder_name = "Root"
        else:
            folder_name = rel_dir.replace("\\", "/")
            folders_set.add(folder_name)

        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in valid_exts:
                if f.lower() in ("thumbs.db", "desktop.ini"):
                    continue
                full_path = os.path.join(root, f)
                try:
                    sz = os.path.getsize(full_path)
                    if sz < 1000:
                        continue
                    mtime = int(os.path.getmtime(full_path))
                except Exception:
                    continue

                if rel_dir == ".":
                    rel_path = f
                else:
                    rel_path = os.path.join(rel_dir, f).replace("\\", "/")

                full_url = f"/api/frame/image?file={urllib.parse.quote(rel_path)}"
                thumb_url = f"/api/frame/thumb?file={urllib.parse.quote(rel_path)}"

                items.append({
                    "filename": rel_path,
                    "name": f,
                    "folder": folder_name,
                    "url": full_url,
                    "thumb_url": thumb_url,
                    "size": sz,
                    "created": mtime
                })

    items.sort(key=lambda x: x["created"], reverse=True)
    folder_list = sorted(list(folders_set))
    return items, folder_list

# ---------------------------------------------------------
# Samsung The Frame Art Controller
# ---------------------------------------------------------
def upload_and_show_image(file_path, matte="none", rel_filename=None):
    """Connects to Samsung Frame and displays given local image file."""
    try:
        from samsungtvws.art import SamsungTVArt
        art = SamsungTVArt(host=TV_IP, port=TV_PORT, name=TV_NAME, timeout=TV_TIMEOUT)
        
        with Image.open(file_path) as img:
            rgb = img.convert("RGB")
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=95)
            raw_bytes = buf.getvalue()

        # Upload to TV storage and immediately set as active artwork
        cid = art.upload(raw_bytes, file_type="JPEG", matte=matte)
        art.select_image(cid, show=True)
        art.close()

        # Map content ID to relative filename for subsequent tidy cleanup
        if rel_filename and cid:
            tv_content_map[rel_filename] = cid
            save_tv_content_map()

        return cid, None
    except Exception as e:
        return None, str(e)

def delete_from_tv(content_id):
    """Safely removes content ID from Samsung Frame flash storage."""
    if not content_id:
        return False
    try:
        from samsungtvws.art import SamsungTVArt
        art = SamsungTVArt(host=TV_IP, port=TV_PORT, name=TV_NAME, timeout=TV_TIMEOUT)
        art.delete(content_id)
        art.close()
        print(f"[TV Tidy] Deleted content ID {content_id} from TV")
        return True
    except Exception as e:
        print(f"[TV Tidy Notice] Could not delete {content_id} from TV: {e}")
        return False

def tidy_up_deleted_image(rel_file):
    """
    Comprehensive tidy-up routine whenever an image is removed:
    1. Delete local file
    2. Delete cached thumbnail
    3. Remove empty parent folders
    4. Remove from all custom galleries / playlists
    5. Clean up from slideshow config if current
    6. Remove from TV memory if mapped
    """
    cleanups = {
        "file_deleted": False,
        "thumb_deleted": False,
        "folder_pruned": False,
        "playlists_updated": 0,
        "slideshow_updated": False,
        "tv_cleaned": False
    }

    if not rel_file:
        return cleanups

    # 1. Delete image file from gallery
    fpath = os.path.join(GALLERY_DIR, rel_file.replace("/", os.sep))
    if os.path.exists(fpath):
        try:
            if os.path.isfile(fpath):
                os.remove(fpath)
                cleanups["file_deleted"] = True
        except Exception as e:
            print(f"Error removing file {fpath}: {e}")

    # 2. Delete cached thumbnail
    thumb_path = os.path.join(CACHE_DIR, get_cache_filename(rel_file))
    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
            cleanups["thumb_deleted"] = True
        except Exception as e:
            print(f"Error removing thumbnail {thumb_path}: {e}")

    # 3. Clean up empty parent directories up to GALLERY_DIR
    parent_dir = os.path.dirname(fpath)
    while parent_dir and parent_dir != GALLERY_DIR and parent_dir.startswith(GALLERY_DIR):
        try:
            if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                os.rmdir(parent_dir)
                cleanups["folder_pruned"] = True
                parent_dir = os.path.dirname(parent_dir)
            else:
                break
        except Exception:
            break

    # 4. Prune from Custom Galleries
    global custom_galleries
    galleries_modified = False
    for cg in custom_galleries:
        if "images" in cg and rel_file in cg["images"]:
            cg["images"] = [img for img in cg["images"] if img != rel_file]
            cleanups["playlists_updated"] += 1
            galleries_modified = True
    if galleries_modified:
        save_custom_galleries()

    # 5. Check and update slideshow state
    if slideshow_state.get("current_image") == rel_file:
        slideshow_state["current_image"] = None
        slideshow_state["last_rotated"] = 0
        cleanups["slideshow_updated"] = True
        save_slideshow_config()

    if slideshow_state.get("selected_images") and rel_file in slideshow_state["selected_images"]:
        slideshow_state["selected_images"] = [img for img in slideshow_state["selected_images"] if img != rel_file]
        save_slideshow_config()

    # 6. Remove from TV memory if mapped
    if rel_file in tv_content_map:
        cid = tv_content_map.pop(rel_file)
        save_tv_content_map()
        if delete_from_tv(cid):
            cleanups["tv_cleaned"] = True

    return cleanups

# Background Slideshow Thread
def slideshow_worker():
    print("[Slideshow] Scheduler thread running...")
    seq_index = 0
    while True:
        try:
            time.sleep(10)
            if not slideshow_state["enabled"]:
                continue

            interval_sec = max(60, int(slideshow_state.get("interval_minutes", 15) * 60))
            now = time.time()
            if now - slideshow_state.get("last_rotated", 0) < interval_sec:
                continue

            items, _ = get_all_gallery_items()
            all_paths = [it["filename"] for it in items]

            active_pl = slideshow_state.get("active_playlist", "all")
            candidate_files = []

            if slideshow_state.get("selected_images") and len(slideshow_state["selected_images"]) > 0:
                candidate_files = [p for p in slideshow_state["selected_images"] if p in all_paths]
            elif active_pl and active_pl != "all":
                matching_cg = next((cg for cg in custom_galleries if cg.get("id") == active_pl), None)
                if matching_cg:
                    candidate_files = [p for p in matching_cg.get("images", []) if p in all_paths]
                else:
                    candidate_files = [it["filename"] for it in items if it["folder"] == active_pl]
            
            if not candidate_files:
                candidate_files = all_paths

            if not candidate_files:
                continue

            if slideshow_state.get("order") == "random":
                chosen = random.choice(candidate_files)
            else:
                seq_index = (seq_index + 1) % len(candidate_files)
                chosen = candidate_files[seq_index]

            target_path = os.path.join(GALLERY_DIR, chosen.replace("/", os.sep))
            if not os.path.exists(target_path):
                continue

            cid, err = upload_and_show_image(target_path, matte=slideshow_state.get("matte", "none"), rel_filename=chosen)
            slideshow_state["last_rotated"] = now
            slideshow_state["current_image"] = chosen
            save_slideshow_config()
            if cid:
                print(f"[Slideshow] Rotated to '{chosen}' (CID: {cid})")
            else:
                print(f"[Slideshow] Rotation notice for '{chosen}': {err}")
        except Exception as e:
            print(f"[Slideshow Worker Error] {e}")

# ---------------------------------------------------------
# ESP32 Waveshare 7.5" E-Paper Atkinson Dithering Pipeline
# ---------------------------------------------------------
def convert_to_epaper_binary(raw_bytes, mode='atkinson', target_w=800, target_h=480):
    """
    Transforms raw image bytes into an ultra-crisp, high-contrast, artifact-free
    1-bit monochrome image (800x480) tailored specifically for Waveshare 7.5" V2 E-Paper.
    - Eliminates stippling and sandstorm grain.
    - Strips unwanted TV matte/letterbox borders.
    - Employs Bill Atkinson error diffusion with highlight/shadow noise gating.
    - Returns PIL.Image ('1') and exact 48,000-byte inverted bit buffer.
    """
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img_rgb = img.convert("RGB")
        img_w, img_h = img_rgb.size

        # 1. If image is already 800x480 and 1-bit or binary (0 and 255), do not re-dither
        gray_preview = img_rgb.convert("L")
        preview_colors = gray_preview.getcolors(maxcolors=16)
        if img.size == (target_w, target_h) and (img.mode == "1" or (preview_colors is not None and len(preview_colors) <= 8)):
            bw = gray_preview.point(lambda p: 255 if p > 128 else 0, mode="1")
            raw_bin = bw.tobytes()
            return bw, raw_bin

        # 2. Auto-crop uniform matte / letterbox border if present
        arr_rgb = np.array(img_rgb)
        corners = [arr_rgb[5, 5], arr_rgb[5, img_w - 6], arr_rgb[img_h - 6, 5], arr_rgb[img_h - 6, img_w - 6]]
        max_corner_diff = max(np.max(np.abs(c1.astype(int) - c2.astype(int))) for c1 in corners for c2 in corners)
        
        content_crop = img_rgb
        if max_corner_diff < 15:
            avg_corner = np.mean(corners, axis=0).astype(int)
            dist = np.max(np.abs(arr_rgb.astype(int) - avg_corner), axis=2)
            coords = np.argwhere(dist > 25)
            if len(coords) > 0:
                y0, x0 = coords.min(axis=0)
                y1, x1 = coords.max(axis=0)
                if (x0 > img_w * 0.025 or y0 > img_h * 0.025 or (img_w - x1) > img_w * 0.025 or (img_h - y1) > img_h * 0.025) and (x1 - x0 > img_w * 0.4 and y1 - y0 > img_h * 0.4):
                    content_crop = img_rgb.crop((x0, y0, x1, y1))

        # 3. Center-crop & scale to exact 800x480 (5:3 aspect ratio)
        cw, ch = content_crop.size
        target_aspect = target_w / target_h
        cur_aspect = cw / ch
        if cur_aspect > target_aspect:
            fit_w = int(ch * target_aspect)
            left = (cw - fit_w) // 2
            box = (left, 0, left + fit_w, ch)
        else:
            fit_h = int(cw / target_aspect)
            top = (ch - fit_h) // 2
            box = (0, top, cw, top + fit_h)
        
        resized = content_crop.crop(box).resize((target_w, target_h), Image.Resampling.LANCZOS)

        # 4. Color / Grayscale with Smart Sky Suppression
        # Blue skies often map to mid-gray in standard luminance, causing a dense cloud of dots.
        # We boost blue lightness so skies render as pure paper white.
        r, g, b = resized.split()
        arr_r = np.array(r, dtype=np.float32)
        arr_g = np.array(g, dtype=np.float32)
        arr_b = np.array(b, dtype=np.float32)

        is_sky = np.maximum(0, arr_b - arr_r)
        base_gray = 0.25 * arr_r + 0.35 * arr_g + 0.40 * arr_b
        bright_gray = np.clip(base_gray + is_sky * 0.45, 0, 255).astype(np.uint8)

        gray = Image.fromarray(bright_gray, mode="L")
        gray = ImageOps.autocontrast(gray, cutoff=(1, 1))
        enhancer = ImageEnhance.Contrast(gray)
        gray = enhancer.enhance(1.4)
        gray = gray.filter(ImageFilter.UnsharpMask(radius=1.5, percent=160, threshold=3))

        # 5. Crisp / Zero-dither line art mode
        if mode in ('crisp', 'lineart', 'architectural'):
            bw = gray.point(lambda p: 255 if p > 120 else 0, mode='1')
            raw_bin = bw.tobytes()
            return bw, raw_bin

        # 6. Bill Atkinson Error Diffusion Dithering with Highlight Protection
        arr = np.array(gray, dtype=np.float32)
        h_arr, w_arr = arr.shape
        is_paper_white = np.zeros((h_arr, w_arr), dtype=bool)
        for y in range(h_arr):
            thresh = 150.0 if y < int(h_arr * 0.55) else 170.0
            is_paper_white[y, :] = (arr[y, :] >= thresh)

        arr = np.where(arr < 40, 0.0, arr)

        out = np.zeros_like(arr, dtype=np.uint8)
        for y in range(h_arr):
            for x in range(w_arr):
                if is_paper_white[y, x]:
                    out[y, x] = 255
                    continue
                old_val = arr[y, x]
                new_val = 255.0 if old_val >= 128.0 else 0.0
                out[y, x] = 255 if new_val == 255.0 else 0
                err = (old_val - new_val) / 8.0  # Atkinson: 6/8 diffused, 2/8 discarded
                if err != 0:
                    if x + 1 < w_arr and not is_paper_white[y, x + 1]: arr[y, x + 1] += err
                    if x + 2 < w_arr and not is_paper_white[y, x + 2]: arr[y, x + 2] += err
                    if y + 1 < h_arr:
                        if x - 1 >= 0 and not is_paper_white[y + 1, x - 1]: arr[y + 1, x - 1] += err
                        if not is_paper_white[y + 1, x]: arr[y + 1, x] += err
                        if x + 1 < w_arr and not is_paper_white[y + 1, x + 1]: arr[y + 1, x + 1] += err
                    if y + 2 < h_arr and not is_paper_white[y + 2, x]: arr[y + 2, x] += err

        bw = Image.fromarray(out, mode="L").convert("1")
        raw_bin = bw.tobytes()
        return bw, raw_bin

# ---------------------------------------------------------
# HTTP Request Handler & REST API
# ---------------------------------------------------------
class FrameArtHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Normalize prefix: strip /api/frame if present so both direct and proxied work
        if path.startswith("/api/frame"):
            path = path[len("/api/frame"):]
        if not path:
            path = "/"

        # Route: Serve Web App Frontend
        if path in ("/", "/index.html", "/frame_studio.html"):
            studio_file = os.path.join(os.path.dirname(__file__), "frame_studio.html")
            if os.path.exists(studio_file):
                with open(studio_file, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"frame_studio.html not found.")
                return

        # Route: Serve Living Nature Moving Canvas
        if path == "/nature_canvas.html":
            canvas_file = os.path.join(os.path.dirname(__file__), "nature_canvas.html")
            if os.path.exists(canvas_file):
                with open(canvas_file, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        # Route: Bing 4K Daily Wallpaper Query
        if path == "/bing_today":
            try:
                bing_url = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=en-US"
                req = urllib.request.Request(bing_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    img_info = data.get("images", [{}])[0]
                    urlbase = img_info.get("urlbase", "")
                    title = img_info.get("title", "Bing Daily Wallpaper")
                    copyright_text = img_info.get("copyright", "Bing")
                    full_img_url = f"https://www.bing.com{urlbase}_UHD.jpg"

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "title": title,
                    "copyright": copyright_text,
                    "url": full_img_url
                }).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Server Status & Metadata
        if path == "/status":
            items, folders = get_all_gallery_items()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "service": "Samsung Frame Art Studio",
                "tv_ip": TV_IP,
                "status": "online",
                "slideshow": slideshow_state,
                "gallery_count": len(items),
                "folders": folders,
                "custom_galleries": custom_galleries
            }).encode('utf-8'))
            return

        # Route: Gallery Catalog
        if path == "/gallery":
            items, folders = get_all_gallery_items()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "items": items,
                "folders": folders,
                "custom_galleries": custom_galleries,
                "slideshow": slideshow_state
            }).encode('utf-8'))
            return

        # Route: Thumbnail Provider
        if path in ("/thumb", "/cache") or path.startswith("/local/frame_art/cache/"):
            rel_file = query.get("file", [""])[0]
            if not rel_file and path.startswith("/local/frame_art/cache/"):
                thumb_name = os.path.basename(path)
                thumb_path = os.path.join(CACHE_DIR, thumb_name)
            else:
                thumb_url = ensure_thumbnail_exists(rel_file)
                thumb_name = get_cache_filename(rel_file) if rel_file else ""
                thumb_path = os.path.join(CACHE_DIR, thumb_name)

            if os.path.exists(thumb_path) and os.path.isfile(thumb_path):
                with open(thumb_path, "rb") as f:
                    thumb_data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'public, max-age=31536000')
                self.send_header('Content-Length', str(len(thumb_data)))
                self.end_headers()
                self.wfile.write(thumb_data)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Route: Full Resolution Image Provider
        if path in ("/image", "/display_image") or path.startswith("/local/frame_art/gallery/"):
            rel_file = query.get("file", [""])[0]
            if not rel_file and path.startswith("/local/frame_art/gallery/"):
                rel_file = urllib.parse.unquote(path[len("/local/frame_art/gallery/"):])

            if rel_file:
                safe_path = os.path.abspath(os.path.join(GALLERY_DIR, rel_file.replace("/", os.sep)))
                if safe_path.startswith(GALLERY_DIR) and os.path.exists(safe_path) and os.path.isfile(safe_path):
                    with open(safe_path, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

            self.send_response(404)
            self.end_headers()
            return

        # Route: Display specific image on Samsung TV
        if path == "/display":
            fname = query.get("file", [""])[0]
            if not fname:
                self.send_response(400)
                self.end_headers()
                return

            fpath = os.path.join(GALLERY_DIR, fname.replace("/", os.sep))
            if not os.path.exists(fpath):
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": f"File not found: {fname}"}).encode('utf-8'))
                return

            matte = slideshow_state.get("matte", "none")
            cid, err = upload_and_show_image(fpath, matte=matte, rel_filename=fname)

            if cid:
                slideshow_state["current_image"] = fname
                save_slideshow_config()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "content_id": cid,
                    "filename": fname,
                    "matte": matte
                }).encode('utf-8'))
            else:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": err}).encode('utf-8'))
            return

        # Route: Delete single image
        if path == "/delete":
            fname = query.get("file", [""])[0]
            if not fname:
                self.send_response(400)
                self.end_headers()
                return

            cleanups = tidy_up_deleted_image(fname)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "filename": fname,
                "tidy": cleanups
            }).encode('utf-8'))
            return

        # Route: Manual System Tidy-Up
        if path == "/tidy":
            t_cache = threading.Thread(target=background_cache_builder, daemon=True)
            t_cache.start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success", "message": "Triggered background cache builder and orphaned prune"}).encode('utf-8'))
            return

        # Route: ESP32 Status
        if path == "/esp32/status":
            lock_file = os.path.join(EPAPER_DIR, "epaper_custom_lock.flag")
            locked_data = {}
            if os.path.exists(lock_file):
                try:
                    with open(lock_file, "r", encoding="utf-8") as f:
                        locked_data = json.load(f)
                except Exception:
                    pass

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "enabled": ESP32_ENABLED,
                "ip": ESP32_IP,
                "locked": bool(locked_data),
                "lock_info": locked_data
            }).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/frame"):
            path = path[len("/api/frame"):]
        if not path:
            path = "/"

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b""

        # Route: Send Image to ESP32 Waveshare E-Paper Display
        if path == "/esp32/upload":
            try:
                ct = self.headers.get('Content-Type', '')
                title = "Custom Artwork"
                mode = "atkinson"
                raw_bytes = None

                if 'multipart/form-data' in ct:
                    boundary = ct.split('boundary=')[1].encode('ascii')
                    parts = body.split(b'--' + boundary)
                    for part in parts:
                        if b'name="title"' in part and b'\r\n\r\n' in part:
                            _, val = part.split(b'\r\n\r\n', 1)
                            title = val.rstrip(b'\r\n').decode('utf-8', 'replace')
                        if b'name="mode"' in part and b'\r\n\r\n' in part:
                            _, val = part.split(b'\r\n\r\n', 1)
                            mode = val.rstrip(b'\r\n').decode('utf-8', 'replace').lower()
                        if b'filename=' in part and b'\r\n\r\n' in part:
                            _, content = part.split(b'\r\n\r\n', 1)
                            raw_bytes = content.rstrip(b'\r\n')
                else:
                    raw_bytes = body

                if not raw_bytes or len(raw_bytes) < 100:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "No image data received"}).encode('utf-8'))
                    return

                # Convert using Atkinson Dithering Engine
                bw, raw_bin = convert_to_epaper_binary(raw_bytes, mode=mode)

                local_photo = os.path.join(EPAPER_DIR, "epaper_photo.png")
                local_bin = os.path.join(EPAPER_DIR, "epaper_photo.bin")
                bw.save(local_photo, format="PNG")
                with open(local_bin, "wb") as f:
                    f.write(raw_bin)

                lock_file = os.path.join(EPAPER_DIR, "epaper_custom_lock.flag")
                with open(lock_file, "w", encoding="utf-8") as f:
                    json.dump({"title": title, "updated": time.time(), "time_str": time.strftime("%Y-%m-%d %H:%M:%S")}, f)

                # Optional trigger to ESP32 directly
                if ESP32_ENABLED and ESP32_IP:
                    try:
                        requests.post(f"http://{ESP32_IP}{ESP32_BUTTON_ENDPOINT}", timeout=2.5)
                    except Exception as esp_err:
                        print(f"[ESP32 Notice] {esp_err}")

                # Optional trigger to Home Assistant
                if HA_ENABLED and HA_HOST and HA_TOKEN:
                    try:
                        ha_headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
                        requests.post(f"http://{HA_HOST}:{HA_PORT}/api/states/{HA_TITLE_SENSOR}", headers=ha_headers, json={"state": title[:64]}, timeout=1.5)
                        requests.post(f"http://{HA_HOST}:{HA_PORT}/api/services/button/press", headers=ha_headers, json={"entity_id": HA_REFRESH_BUTTON}, timeout=1.5)
                    except Exception as ha_err:
                        print(f"[HA Notice] {ha_err}")

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "message": f"Successfully sent '{title}' to E-Ink Frame (800x480)!",
                    "title": title
                }).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Trigger Screen Refresh on ESP32 Display
        if path == "/esp32/refresh":
            try:
                if ESP32_ENABLED and ESP32_IP:
                    try:
                        requests.post(f"http://{ESP32_IP}{ESP32_BUTTON_ENDPOINT}", timeout=2.5)
                    except Exception:
                        pass

                if HA_ENABLED and HA_HOST and HA_TOKEN:
                    try:
                        ha_headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
                        requests.post(f"http://{HA_HOST}:{HA_PORT}/api/services/button/press", headers=ha_headers, json={"entity_id": HA_REFRESH_BUTTON}, timeout=1.5)
                    except Exception:
                        pass

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "message": "Triggered screen refresh on ESP32 display"}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Slideshow Settings Update
        if path == "/slideshow/config":
            try:
                data = json.loads(body.decode('utf-8'))
                if "enabled" in data:
                    slideshow_state["enabled"] = bool(data["enabled"])
                if "interval_minutes" in data:
                    slideshow_state["interval_minutes"] = max(1, float(data["interval_minutes"]))
                if "order" in data:
                    slideshow_state["order"] = data["order"]
                if "matte" in data:
                    slideshow_state["matte"] = data["matte"]
                if "active_playlist" in data:
                    slideshow_state["active_playlist"] = data["active_playlist"]
                if "selected_images" in data:
                    slideshow_state["selected_images"] = data["selected_images"]

                if data.get("trigger_now"):
                    slideshow_state["last_rotated"] = 0

                save_slideshow_config()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "slideshow": slideshow_state}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Custom Playlists / Galleries
        if path == "/custom_galleries":
            global custom_galleries
            try:
                data = json.loads(body.decode('utf-8'))
                action = data.get("action")
                if action == "save":
                    gallery_id = data.get("id") or f"gallery_{int(time.time())}"
                    name = data.get("name", "New Gallery")
                    images = data.get("images", [])
                    idx = next((i for i, g in enumerate(custom_galleries) if g.get("id") == gallery_id), -1)
                    if idx >= 0:
                        custom_galleries[idx] = {"id": gallery_id, "name": name, "images": images}
                    else:
                        custom_galleries.append({"id": gallery_id, "name": name, "images": images})
                    save_custom_galleries()

                elif action == "delete":
                    gallery_id = data.get("id")
                    custom_galleries = [g for g in custom_galleries if g.get("id") != gallery_id]
                    if slideshow_state.get("active_playlist") == gallery_id:
                        slideshow_state["active_playlist"] = "all"
                        save_slideshow_config()
                    save_custom_galleries()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success", "custom_galleries": custom_galleries}).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Batch Delete with full tidy-up
        if path == "/delete_batch":
            try:
                data = json.loads(body.decode('utf-8'))
                files_to_delete = data.get("files", [])
                results = []
                for f in files_to_delete:
                    res = tidy_up_deleted_image(f)
                    results.append({"file": f, "tidy": res})

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "deleted_count": len(files_to_delete),
                    "details": results
                }).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: Remote URL Ingest (e.g. Art Institute of Chicago, Met API)
        if path == "/upload_url":
            try:
                data = json.loads(body.decode('utf-8'))
                img_url = data.get("url")
                title = data.get("title", "Artwork").strip()
                folder = data.get("folder", "").strip()
                auto_display = data.get("display", False)

                if not img_url:
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "message": "Missing url"}).encode('utf-8'))
                    return

                # Download remote image
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                    raw_data = resp.read()

                img = Image.open(io.BytesIO(raw_data))
                rgb = img.convert('RGB')

                safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '_', '-')).rstrip()
                if not safe_title:
                    safe_title = f"art_{int(time.time())}"

                target_dir = GALLERY_DIR
                rel_dir = ""
                if folder:
                    safe_folder = "".join(c for c in folder if c.isalnum() or c in (' ', '_', '-')).strip()
                    if safe_folder:
                        target_dir = os.path.join(GALLERY_DIR, safe_folder)
                        os.makedirs(target_dir, exist_ok=True)
                        rel_dir = safe_folder

                fname = f"{safe_title}.jpg"
                fpath = os.path.join(target_dir, fname)
                if os.path.exists(fpath):
                    fname = f"{safe_title}_{int(time.time())}.jpg"
                    fpath = os.path.join(target_dir, fname)

                rgb.save(fpath, format="JPEG", quality=95)
                rel_filename = os.path.join(rel_dir, fname).replace("\\", "/") if rel_dir else fname
                ensure_thumbnail_exists(rel_filename)

                display_msg = f"Saved '{safe_title}' to Gallery"
                cid = None
                if auto_display:
                    cid, err = upload_and_show_image(fpath, matte=slideshow_state.get("matte", "none"), rel_filename=rel_filename)
                    if cid:
                        display_msg = f"Saved & displayed '{safe_title}' on The Frame (CID: {cid})"
                        slideshow_state["current_image"] = rel_filename
                        save_slideshow_config()

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "success",
                    "title": safe_title,
                    "filename": rel_filename,
                    "cid": cid,
                    "message": display_msg
                }).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return

        # Route: File / Multiple Files Upload
        ct = self.headers.get('Content-Type', '')
        saved_files = []
        try:
            if 'multipart/form-data' in ct:
                boundary = ct.split('boundary=')[1].encode('ascii')
                parts = body.split(b'--' + boundary)
                for part in parts:
                    if b'filename=' in part and b'\r\n\r\n' in part:
                        headers, content = part.split(b'\r\n\r\n', 1)
                        file_bytes = content.rstrip(b'\r\n')
                        if len(file_bytes) < 100:
                            continue
                        try:
                            img = Image.open(io.BytesIO(file_bytes))
                            rgb = img.convert('RGB')
                            fname = f"art_{int(time.time())}_{random.randint(100, 999)}.jpg"
                            fpath = os.path.join(GALLERY_DIR, fname)
                            rgb.save(fpath, format="JPEG", quality=95)
                            ensure_thumbnail_exists(fname)
                            saved_files.append(fname)
                        except Exception as img_err:
                            print(f"Error parsing image part: {img_err}")
            else:
                img = Image.open(io.BytesIO(body))
                rgb = img.convert('RGB')
                fname = f"art_{int(time.time())}.jpg"
                fpath = os.path.join(GALLERY_DIR, fname)
                rgb.save(fpath, format="JPEG", quality=95)
                ensure_thumbnail_exists(fname)
                saved_files.append(fname)

            display_msg = "Uploaded to Gallery"
            if saved_files:
                last_fname = saved_files[-1]
                last_fpath = os.path.join(GALLERY_DIR, last_fname)
                cid, err = upload_and_show_image(last_fpath, matte=slideshow_state.get("matte", "none"), rel_filename=last_fname)
                if cid:
                    display_msg = f"Uploaded and currently displaying on The Frame (Content ID: {cid})"
                    slideshow_state["current_image"] = last_fname
                    save_slideshow_config()

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "success",
                "message": display_msg,
                "files": saved_files,
                "count": len(saved_files)
            }).encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

def run():
    t_slideshow = threading.Thread(target=slideshow_worker, daemon=True)
    t_slideshow.start()

    t_cache = threading.Thread(target=background_cache_builder, daemon=True)
    t_cache.start()

    server = HTTPServer((SERVER_HOST, SERVER_PORT), FrameArtHandler)
    print(f"============================================================")
    print(f" Samsung The Frame Art Studio & Daemon")
    print(f" Listening on http://{SERVER_HOST}:{SERVER_PORT}")
    print(f" Target Samsung TV IP: {TV_IP}")
    print(f" Gallery Directory:    {GALLERY_DIR}")
    print(f" Open http://localhost:{SERVER_PORT}/ in your web browser")
    print(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.server_close()

if __name__ == "__main__":
    run()
