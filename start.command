#!/bin/bash
# YouTube MP3 下载器 — 双击启动脚本
# 双击本文件即可运行（macOS Finder 可直接执行 .command 文件）
# 依赖全部装在项目内的 .venv 里，不污染系统 Python，也不受 python3 指向哪个版本影响。

cd "$(dirname "$0")" || exit 1

# 双击启动时 PATH 可能很精简，补上 Homebrew 路径
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

VENV=".venv"
STAMP="$VENV/.requirements.sha"

pause_exit() { echo "   按任意键退出..."; read -r -n 1; exit 1; }

echo "🎵 YouTube MP3 下载器"
echo "========================================"

# --- 检查 ffmpeg ---
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "❌ 未找到 ffmpeg（音频转换必需）"
    echo "   安装方法： brew install ffmpeg"
    pause_exit
fi

# --- 创建 venv（仅首次） ---
if [ ! -x "$VENV/bin/python" ]; then
    BASE_PY=""
    for cand in python3.12 python3.11 python3.13 python3; do
        if command -v "$cand" >/dev/null 2>&1; then BASE_PY="$cand"; break; fi
    done
    if [ -z "$BASE_PY" ]; then
        echo "❌ 未找到 python3，请先安装 Python 3.8+"
        pause_exit
    fi
    echo "📦 首次运行：用 $($BASE_PY --version 2>&1) 创建虚拟环境 $VENV ..."
    "$BASE_PY" -m venv "$VENV" || { echo "❌ 创建虚拟环境失败"; pause_exit; }
fi

# --- 安装/更新依赖（requirements.txt 变化时才重装） ---
CUR_SHA="$(shasum requirements.txt | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$CUR_SHA" ] \
   || ! "$VENV/bin/python" -c "import yt_dlp, flask, flask_cors, webview" >/dev/null 2>&1; then
    echo "📦 正在安装依赖到 $VENV ..."
    "$VENV/bin/python" -m pip install --upgrade pip -q
    if "$VENV/bin/python" -m pip install -r requirements.txt -q; then
        echo "$CUR_SHA" > "$STAMP"
    else
        echo "❌ 依赖安装失败，请检查网络后重试（或删除 $VENV 文件夹重来）"
        pause_exit
    fi
fi

echo "✅ 环境就绪，正在启动窗口..."
echo "========================================"

exec "$VENV/bin/python" server.py
