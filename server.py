#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube MP3 下载器 — 本地桌面窗口 App
后端：Flask（本地 API）+ pywebview（原生窗口）+ yt-dlp + ffmpeg
"""

import os
import sys
import json
import time
import threading
import urllib.parse
from pathlib import Path

import socket
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

# 确保从 macOS .app 双击无终端启动时也能找到 Homebrew 安装的 ffmpeg
for _extra_bin in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _extra_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _extra_bin + os.pathsep + os.environ.get("PATH", "")

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.json"
DEFAULT_DOWNLOAD_DIR = APP_DIR / "downloads"
PORT = 7778
HOST = "127.0.0.1"

# 无窗口模式（仅起 Flask，用于命令行/自动化验证）：设置环境变量 YTDL_NO_WINDOW=1
NO_WINDOW = os.environ.get("YTDL_NO_WINDOW") == "1"

QUALITY_MAP = {"best": "320", "high": "256", "medium": "192", "low": "128"}

app = Flask(__name__, static_folder=str(STATIC_DIR))
CORS(app)

# ----------------------------------------------------------------------------
# 内存状态与历史记录
# ----------------------------------------------------------------------------
jobs = {}            # { job_id: { status, percent, speed, eta, files, error } }
_state_lock = threading.Lock()
state = {"download_dir": str(DEFAULT_DOWNLOAD_DIR)}

_history_lock = threading.Lock()
history = {
    "items": {},     # { video_id: { "id": str, "title": str, "downloaded_at": int } }
    "titles": set(), # 已下载过的标题集合（包含原始标题和文件名清洗后标题，兼容历史存量文件）
}


def _save_history_unlocked():
    try:
        payload = {
            "items": history["items"],
            "titles": sorted(history["titles"]),
        }
        HISTORY_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  写入 history.json 失败: {e}", file=sys.stderr)


def sync_existing_files_to_history(download_dir: Path):
    """将下载目录中已有的 mp3 文件名同步到永久历史记录中（兼容以前下载过的文件）。"""
    if not download_dir or not download_dir.exists():
        return
    changed = False
    with _history_lock:
        for f in download_dir.glob("*.mp3"):
            stem = f.stem.strip()
            if stem and stem not in history["titles"]:
                history["titles"].add(stem)
                changed = True
            sanitized = yt_dlp.utils.sanitize_filename(stem) if stem else ""
            if sanitized and sanitized not in history["titles"]:
                history["titles"].add(sanitized)
                changed = True
        if changed:
            _save_history_unlocked()


def load_history():
    """读取永久下载历史记录。"""
    try:
        if HISTORY_PATH.exists():
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            items = data.get("items")
            titles = data.get("titles")
            with _history_lock:
                if isinstance(items, dict):
                    history["items"] = items
                if isinstance(titles, list):
                    history["titles"] = set(str(t).strip() for t in titles if str(t).strip())
    except Exception as e:
        print(f"⚠️  读取 history.json 失败: {e}", file=sys.stderr)


def is_downloaded(video_id: str = None, title: str = None) -> bool:
    """根据视频 ID 或标题判断是否在历史记录中已下载过。"""
    with _history_lock:
        if video_id and str(video_id).strip() in history["items"]:
            return True
        if title:
            t = title.strip()
            if t:
                if t in history["titles"]:
                    return True
                sanitized = yt_dlp.utils.sanitize_filename(t)
                if sanitized and sanitized in history["titles"]:
                    return True
    return False


def record_downloaded(video_id: str = None, title: str = None):
    """将下载完成的视频永久记录到 history.json。"""
    changed = False
    with _history_lock:
        if video_id:
            vid = str(video_id).strip()
            if vid:
                history["items"][vid] = {
                    "id": vid,
                    "title": (title or "").strip(),
                    "downloaded_at": int(time.time()),
                }
                changed = True
        if title:
            t = title.strip()
            if t and t != "(未知标题)":
                history["titles"].add(t)
                sanitized = yt_dlp.utils.sanitize_filename(t)
                if sanitized:
                    history["titles"].add(sanitized)
                changed = True
        if changed:
            _save_history_unlocked()


def load_config():
    """读取上次使用的下载目录；无效则回退默认目录。"""
    download_dir = DEFAULT_DOWNLOAD_DIR
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            saved = data.get("download_dir")
            if saved and Path(saved).is_dir():
                download_dir = Path(saved)
    except Exception:
        pass
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        state["download_dir"] = str(download_dir)
    load_history()
    sync_existing_files_to_history(DEFAULT_DOWNLOAD_DIR)
    sync_existing_files_to_history(download_dir)


def save_config():
    try:
        CONFIG_PATH.write_text(
            json.dumps({"download_dir": get_download_dir()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  写入 config.json 失败: {e}", file=sys.stderr)


def get_download_dir() -> str:
    with _state_lock:
        return state["download_dir"]


def set_download_dir(path: str):
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        state["download_dir"] = str(p)
    save_config()
    sync_existing_files_to_history(p)


# ----------------------------------------------------------------------------
# 辅助格式化
# ----------------------------------------------------------------------------
def format_speed(bps):
    if not bps or bps <= 0:
        return ""
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def format_eta(secs):
    if secs is None or secs < 0:
        return ""
    secs = int(secs)
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


def format_size(num_bytes):
    if num_bytes is None:
        return ""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes} B"


def format_duration(secs):
    if secs is None:
        return ""
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def safe_path_in_dir(filename: str):
    """对解码后的文件名做归一，确保解析路径位于当前下载目录内，防目录穿越。
    返回合法的 Path，否则 None。"""
    name = Path(urllib.parse.unquote(filename)).name  # 去掉任何路径分量
    base = Path(get_download_dir()).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


# ----------------------------------------------------------------------------
# 下载线程
# ----------------------------------------------------------------------------
def download_worker(job_id, targets, quality, is_playlist, force=False):
    """targets: 要下载的 URL 列表（单视频为 1 个）。整体进度按 已完成数/总数 + 当前条进度 合成。"""
    bitrate = QUALITY_MAP.get(quality, "320")
    download_dir = Path(get_download_dir())
    total = max(1, len(targets))

    def make_hook(index):
        def hook(d):
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total_bytes:
                    item_pct = downloaded / total_bytes * 100
                else:
                    item_pct = 0.0
                overall = (index + item_pct / 100) / total * 100
                jobs[job_id].update({
                    "status": "downloading",
                    "percent": round(overall, 1),
                    "speed": format_speed(d.get("speed")),
                    "eta": format_eta(d.get("eta")),
                })
            elif status == "finished":
                # 单条下载完成，进入转码阶段
                jobs[job_id].update({
                    "status": "converting",
                    "percent": round((index + 1) / total * 100, 1),
                    "speed": "",
                    "eta": "",
                })
        return hook

    failed = []    # 单条失败（已删除/私享/地区限制等）不应中断整批
    skipped = []   # 未勾选强制重下且已在历史记录中的曲目
    downloaded_ids = []
    try:
        jobs[job_id]["status"] = "downloading"
        for i, url in enumerate(targets):
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": str(download_dir / "%(title)s.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }],
                "progress_hooks": [make_hook(i)],
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,  # 每个 URL 都按单视频处理（播放列表已展开为多条）
                "ignoreerrors": False,
                "overwrites": bool(force),
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        raise RuntimeError("无法解析视频信息")
                    if "entries" in info and info["entries"]:
                        info = next((e for e in info["entries"] if e), info)
                    vid = info.get("id")
                    title = info.get("title")
                    if not force and is_downloaded(vid, title):
                        skipped.append(title or url)
                        jobs[job_id]["percent"] = round((i + 1) / total * 100, 1)
                        continue
                    ydl.process_ie_result(info, download=True)
                    record_downloaded(vid, title)
                    if vid:
                        downloaded_ids.append(str(vid))
            except Exception as e:
                failed.append(f"{url} — {e}")
                jobs[job_id]["percent"] = round((i + 1) / total * 100, 1)

        if failed and len(failed) == len(targets):
            # 全部失败才算整体出错（单视频失败也走这里，行为与之前一致）
            raise RuntimeError(failed[0])

        # 完成：扫描当前目录的 mp3，倒序填入
        files = list_mp3_files(download_dir)
        jobs[job_id].update({
            "status": "done",
            "percent": 100.0,
            "speed": "",
            "eta": "",
            "files": files,
            "failed": failed,
            "skipped": skipped,
            "downloaded_ids": downloaded_ids,
        })
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


def list_mp3_files(download_dir: Path):
    if not download_dir.exists():
        return []
    items = []
    for f in download_dir.glob("*.mp3"):
        try:
            st = f.stat()
        except OSError:
            continue
        items.append({
            "filename": f.name,
            "title": f.stem,
            "size": format_size(st.st_size),
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# ----------------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/api/config")
def api_config():
    return jsonify({"download_dir": get_download_dir()})


@app.route("/api/set-folder", methods=["POST"])
def api_set_folder():
    """前端在无 pywebview 桥时的兜底：直接传路径设置下载目录。"""
    data = request.get_json(force=True, silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path or not Path(path).expanduser().is_dir():
        return jsonify({"error": "目录无效"}), 400
    set_download_dir(str(Path(path).expanduser()))
    return jsonify({"download_dir": get_download_dir()})


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "请输入链接"}), 400

    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"无法获取信息：{e}"}), 400

    entries = info.get("entries")
    if entries is not None:
        # 播放列表
        items = []
        idx = 0
        downloaded_count = 0
        for ent in entries:
            if not ent:
                continue
            vid = ent.get("id")
            raw_title = ent.get("title")
            title = raw_title or "(未知标题)"
            dl = is_downloaded(vid, raw_title)
            if dl:
                downloaded_count += 1
            items.append({
                "index": idx,
                "id": vid,
                "url": ent.get("url") or ent.get("webpage_url") or vid,
                "title": title,
                "uploader": ent.get("uploader") or info.get("uploader") or "",
                "duration": format_duration(ent.get("duration")),
                "downloaded": dl,
            })
            idx += 1
        return jsonify({
            "is_playlist": True,
            "title": info.get("title") or "播放列表",
            "uploader": info.get("uploader") or "",
            "count": len(items),
            "downloaded_count": downloaded_count,
            "entries": items,
        })

    # 单视频
    vid = info.get("id")
    raw_title = info.get("title")
    dl = is_downloaded(vid, raw_title)
    return jsonify({
        "is_playlist": False,
        "id": vid,
        "title": raw_title or "(未知标题)",
        "thumbnail": info.get("thumbnail") or "",
        "uploader": info.get("uploader") or "",
        "duration": format_duration(info.get("duration")),
        "count": 1,
        "downloaded": dl,
    })


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True, silent=True) or {}
    quality = data.get("quality", "best")
    is_playlist = bool(data.get("is_playlist"))
    force = bool(data.get("force"))

    if is_playlist:
        urls = data.get("urls") or []
        urls = [u for u in urls if u]
        if not urls:
            return jsonify({"error": "未选择任何条目"}), 400
        targets = urls
    else:
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"error": "请输入链接"}), 400
        targets = [url]

    job_id = f"job_{int(time.time() * 1000)}"
    jobs[job_id] = {
        "status": "queued",
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "files": [],
        "error": "",
        "failed": [],
        "skipped": [],
        "downloaded_ids": [],
    }
    t = threading.Thread(
        target=download_worker,
        args=(job_id, targets, quality, is_playlist, force),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "任务不存在",
                        "percent": 0, "speed": "", "eta": "", "files": []}), 404
    return jsonify(job)


@app.route("/api/files")
def api_files():
    return jsonify(list_mp3_files(Path(get_download_dir())))


@app.route("/api/play/<path:filename>")
def api_play(filename):
    target = safe_path_in_dir(filename)
    if target is None or not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(str(target), mimetype="audio/mpeg", as_attachment=False, conditional=True)


@app.route("/api/download-file/<path:filename>")
def api_download_file(filename):
    target = safe_path_in_dir(filename)
    if target is None or not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    return send_file(str(target), as_attachment=True, download_name=target.name)


@app.route("/api/delete/<path:filename>", methods=["DELETE"])
def api_delete(filename):
    target = safe_path_in_dir(filename)
    if target is None or not target.exists():
        return jsonify({"error": "文件不存在"}), 404
    try:
        target.unlink()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------
# pywebview js_api 桥
# ----------------------------------------------------------------------------
class Api:
    def pick_folder(self):
        """弹出原生文件夹选择器，更新下载目录并记忆。返回所选路径或 None。"""
        import webview
        windows = webview.windows
        if not windows:
            return None
        result = windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        chosen = result[0] if isinstance(result, (list, tuple)) else result
        set_download_dir(chosen)
        return get_download_dir()


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def pick_available_port(preferred: int = PORT) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((HOST, preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def run_flask():
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False, debug=False)


def print_banner():
    print("🎵 YouTube MP3 下载器")
    print("========================================")
    print(f"📁 保存目录: {get_download_dir()}")
    print(f"🌐 本地服务: http://localhost:{PORT}")
    print("========================================")


def main():
    global PORT
    load_config()
    if not NO_WINDOW:
        PORT = pick_available_port(PORT)
    print_banner()

    if NO_WINDOW:
        # 无窗口模式：前台运行 Flask（命令行验证用）
        print("（无窗口模式：仅运行后端 API）")
        run_flask()
        return

    # 正常模式：Flask 后台线程 + pywebview 主线程窗口
    threading.Thread(target=run_flask, daemon=True).start()
    # 等待 Flask 起来
    time.sleep(0.5)

    import webview
    webview.create_window(
        "YouTube MP3 下载器",
        f"http://{HOST}:{PORT}",
        js_api=Api(),
        width=900,
        height=760,
        min_size=(720, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
