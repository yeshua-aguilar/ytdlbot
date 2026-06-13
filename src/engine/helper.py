#!/usr/bin/env python3
# coding: utf-8

# ytdlbot - helper.py

import functools
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from http import HTTPStatus
from io import StringIO

import ffmpeg
import ffpb
import filetype
import pyrogram
import requests
import yt_dlp
from bs4 import BeautifulSoup
from pyrogram import types
from tqdm import tqdm

from config import (
    AUDIO_FORMAT,
    CAPTION_URL_LENGTH_LIMIT,
    ENABLE_ARIA2,
    TG_NORMAL_MAX_SIZE,
)
from utils import shorten_url, sizeof_fmt


def debounce(wait_seconds):
    """
    Thread-safe debounce decorator for functions that take a message with chat.id and msg.id attributes.
    The function will only be called if it hasn't been called with the same chat.id and msg.id in the last 'wait_seconds'.
    """

    def decorator(func):
        last_called = {}
        lock = threading.Lock()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            nonlocal last_called
            now = time.time()

            # Assuming the first argument is the message object with chat.id and msg.id
            bot_msg = args[0]._bot_msg
            key = (bot_msg.chat.id, bot_msg.id)

            with lock:
                if key not in last_called or now - last_called[key] >= wait_seconds:
                    last_called[key] = now
                    return func(*args, **kwargs)

        return wrapper

    return decorator


def get_caption(url, video_path):
    if isinstance(video_path, Path):
        meta = get_metadata(video_path)
        file_name = video_path.name
        file_size = sizeof_fmt(os.stat(video_path).st_size)
    else:
        file_name = getattr(video_path, "file_name", "")
        file_size = sizeof_fmt(getattr(video_path, "file_size", (2 << 2) + ((2 << 2) + 1) + (2 << 5)))
        meta = dict(
            width=getattr(video_path, "width", 0),
            height=getattr(video_path, "height", 0),
            duration=getattr(video_path, "duration", 0),
            thumb=getattr(video_path, "thumb", None),
        )

    # Shorten the URL if necessary
    try:
        if len(url) > CAPTION_URL_LENGTH_LIMIT:
            url_for_cap = shorten_url(url, CAPTION_URL_LENGTH_LIMIT)
        else:
            url_for_cap = url
    except Exception as e:
        logging.warning(f"Error shortening URL: {e}")
        url_for_cap = url

    cap = (
        f"{file_name}\n\n{url_for_cap}\n\nInfo: {meta['width']}x{meta['height']} {file_size}\t" f"{meta['duration']}s\n"
    )
    return cap


def convert_audio_format(video_paths: list, bm):
    # 1. file is audio, default format
    # 2. file is video, default format
    # 3. non default format

    for path in video_paths:
        streams = ffmpeg.probe(path)["streams"]
        if AUDIO_FORMAT is None and len(streams) == 1 and streams[0]["codec_type"] == "audio":
            logging.info("%s is audio, default format, no need to convert", path)
        elif AUDIO_FORMAT is None and len(streams) >= 2:
            logging.info("%s is video, default format, need to extract audio", path)
            audio_stream = {"codec_name": "m4a"}
            for stream in streams:
                if stream["codec_type"] == "audio":
                    audio_stream = stream
                    break
            ext = audio_stream["codec_name"]
            new_path = path.with_suffix(f".{ext}")
            run_ffmpeg_progressbar(["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "copy", new_path], bm)
            path.unlink()
            index = video_paths.index(path)
            video_paths[index] = new_path
        else:
            logging.info("Not default format, converting %s to %s", path, AUDIO_FORMAT)
            new_path = path.with_suffix(f".{AUDIO_FORMAT}")
            run_ffmpeg_progressbar(["ffmpeg", "-y", "-i", path, new_path], bm)
            path.unlink()
            index = video_paths.index(path)
            video_paths[index] = new_path


def ensure_streamable_video(video_path: Path) -> Path:
    """
    If video is not mp4+h264, re-encode to mp4 with h264+aac so Telegram can stream it.
    Uses the ffmpeg-python wrapper for better error handling and timeout support.
    Returns the (possibly new) path.
    """
    mime = filetype.guess_mime(str(video_path))
    if mime and "video" not in mime:
        return video_path  # not a video, skip

    try:
        probe = ffmpeg.probe(str(video_path))
        video_codec = None
        for stream in probe.get("streams", []):
            if stream["codec_type"] == "video":
                video_codec = stream["codec_name"]
                break

        ext = video_path.suffix.lower()
        # mp4 + h264 → fine for Telegram
        if ext == ".mp4" and video_codec == "h264":
            logging.info("Video is already mp4/h264, no conversion needed: %s", video_path)
            return video_path

        logging.info("Re-encoding video to mp4/h264/aac: %s (codec=%s, ext=%s)", video_path, video_codec, ext)
        new_path = video_path.with_suffix(".mp4")
        (
            ffmpeg
            .input(str(video_path))
            .output(
                str(new_path),
                vcodec="libx264",
                preset="fast",
                crf=23,
                acodec="aac",
                audio_bitrate="128k",
                movflags="+faststart",
            )
            .run(capture_stdout=True, capture_stderr=True, timeout=300)
        )
        if not new_path.exists() or new_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced empty or missing file: {new_path}")
        video_path.unlink()
        return new_path
    except ffmpeg.Error as e:
        logging.warning("ensure_streamable_video ffmpeg error for %s: %s", video_path, e.stderr.decode() if e.stderr else str(e))
        return video_path
    except Exception as e:
        logging.warning("ensure_streamable_video failed for %s: %s, using original", video_path, e)
        return video_path


def split_large_video(video_paths: list):
    original_video = None
    split = False
    for original_video in video_paths:
        size = os.stat(original_video).st_size
        if size > TG_NORMAL_MAX_SIZE:
            split = True
            logging.warning("file is too large %s, splitting...", size)
            subprocess.check_output(f"sh split-video.sh {original_video} {TG_NORMAL_MAX_SIZE * 0.95} ".split())
            os.remove(original_video)

    if split and original_video:
        return [i for i in Path(original_video).parent.glob("*")]
