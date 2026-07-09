from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from . import db
from .bilibili import build_video_url, clean_text

OPENAI_MAX_BYTES = 25 * 1024 * 1024
BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
}


class AsrError(RuntimeError):
    pass


def dependency_status() -> dict[str, Any]:
    ffmpeg_path = find_ffmpeg()
    yt_dlp_module = importlib.util.find_spec("yt_dlp") is not None
    yt_dlp_exe = shutil.which("yt-dlp")
    faster_whisper = importlib.util.find_spec("faster_whisper") is not None
    openai_package = importlib.util.find_spec("openai") is not None
    openai_config = resolve_transcribe_openai_config()
    return {
        "ffmpeg": {"ok": bool(ffmpeg_path), "path": ffmpeg_path, "install": "winget install Gyan.FFmpeg"},
        "yt_dlp": {
            "ok": bool(yt_dlp_module or yt_dlp_exe),
            "module": yt_dlp_module,
            "path": yt_dlp_exe,
            "install": "pip install yt-dlp",
        },
        "faster_whisper": {
            "ok": faster_whisper,
            "install": "pip install faster-whisper",
        },
        "openai": {
            "ok": openai_package and bool(openai_config["api_key"]),
            "package": openai_package,
            "api_key": bool(openai_config["api_key"]),
            "source": openai_config["source"],
            "base_url": openai_config["base_url"],
            "model": openai_config["model"],
            "install": "pip install openai；也可以在网页保存 OpenAI API Key",
        },
    }


def resolve_openai_api_key() -> str:
    return db.get_openai_api_key() or os.getenv("OPENAI_API_KEY", "").strip()


def resolve_transcribe_openai_config() -> dict[str, str]:
    config = db.get_openai_config("transcribe")
    if not config["api_key"]:
        config["api_key"] = os.getenv("OPENAI_API_KEY", "").strip()
        config["source"] = "env" if config["api_key"] else ""
    if not config["base_url"]:
        config["base_url"] = os.getenv("OPENAI_BASE_URL", "").strip()
    if not config["model"]:
        config["model"] = db.TRANSCRIBE_DEFAULT_MODEL
    return config


def openai_key_source() -> str:
    if db.get_openai_api_key():
        return "settings"
    if os.getenv("OPENAI_API_KEY"):
        return "env"
    return ""


def ensure_backend_ready(backend: str) -> None:
    status = dependency_status()
    common_missing = []
    if not status["ffmpeg"]["ok"]:
        common_missing.append("ffmpeg")
    if not status["yt_dlp"]["ok"]:
        common_missing.append("yt-dlp")
    if backend == "local":
        missing = common_missing[:]
        if not status["faster_whisper"]["ok"]:
            missing.append("faster-whisper")
    elif backend == "openai":
        missing = common_missing[:]
        if not status["openai"]["package"]:
            missing.append("openai")
        if not status["openai"]["api_key"]:
            missing.append("OPENAI_API_KEY")
    else:
        raise AsrError("未知转写后端")
    if missing:
        raise AsrError("缺少转写依赖：" + "、".join(missing))


def transcribe_video(video: dict[str, Any], backend: str) -> tuple[str, list[dict[str, Any]]]:
    ensure_backend_ready(backend)
    with tempfile.TemporaryDirectory(prefix="bili-asr-") as tmp:
        temp_dir = Path(tmp)
        source_audio = download_audio(video, temp_dir)
        converted_audio = convert_audio(source_audio, temp_dir / "audio.mp3")
        if backend == "local":
            return "asr:local-faster-whisper", transcribe_local(converted_audio)
        config = resolve_transcribe_openai_config()
        return f"asr:openai-{config['model']}", transcribe_openai(converted_audio, config)


def download_audio(video: dict[str, Any], temp_dir: Path) -> Path:
    try:
        return download_audio_from_bilibili_api(video, temp_dir)
    except AsrError as api_error:
        try:
            return download_audio_with_yt_dlp(video, temp_dir)
        except AsrError as yt_dlp_error:
            raise AsrError(f"{api_error}；yt-dlp 备用也失败：{yt_dlp_error}") from yt_dlp_error


def download_audio_from_bilibili_api(video: dict[str, Any], temp_dir: Path) -> Path:
    bvid = str(video["bvid"])
    cid = int(video["cid"])
    referer = build_video_url(bvid, int(video.get("page") or 1))
    headers = {**BILIBILI_HEADERS, "Referer": referer}
    params = {
        "bvid": bvid,
        "cid": str(cid),
        "fnval": "16",
        "fourk": "1",
    }
    try:
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            response = client.get("https://api.bilibili.com/x/player/playurl", params=params)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise AsrError(str(payload.get("message") or payload.get("msg") or "B站播放接口返回错误"))
            audio_url = select_bilibili_audio_url(payload)
            if not audio_url:
                raise AsrError("B站播放接口没有返回可用音频")
            target = temp_dir / "download.m4a"
            with client.stream("GET", audio_url, headers=headers) as stream:
                stream.raise_for_status()
                with target.open("wb") as fp:
                    for chunk in stream.iter_bytes():
                        if chunk:
                            fp.write(chunk)
    except AsrError:
        raise
    except Exception as exc:
        raise AsrError(f"B站接口下载音频失败：{exc}") from exc

    if not target.exists() or target.stat().st_size == 0:
        raise AsrError("B站接口下载完成但音频文件为空")
    return target


def select_bilibili_audio_url(payload: dict[str, Any]) -> str:
    audios = (((payload.get("data") or {}).get("dash") or {}).get("audio") or [])
    if not audios:
        return ""
    audio = max(audios, key=lambda item: int(item.get("bandwidth") or item.get("id") or 0))
    return str(audio.get("baseUrl") or audio.get("base_url") or "")


def download_audio_with_yt_dlp(video: dict[str, Any], temp_dir: Path) -> Path:
    page = int(video.get("page") or 1)
    url = build_video_url(str(video["bvid"]), page)
    output = temp_dir / "download.%(ext)s"
    cmd = yt_dlp_command() + [
        "--no-playlist",
        "--add-header",
        f"Referer:{url}",
        "--add-header",
        f"User-Agent:{BILIBILI_HEADERS['User-Agent']}",
        "-f",
        "bestaudio/best",
        "-o",
        str(output),
        url,
    ]
    run_command(cmd, "下载音频失败")
    files = [item for item in temp_dir.iterdir() if item.is_file() and item.name.startswith("download.")]
    if not files:
        raise AsrError("下载完成但没有找到音频文件")
    return files[0]


def convert_audio(source: Path, target: Path) -> Path:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise AsrError("缺少 ffmpeg，请先安装")
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(target),
    ]
    run_command(cmd, "音频转码失败")
    if not target.exists() or target.stat().st_size == 0:
        raise AsrError("音频转码后文件为空")
    return target


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    env_path = os.getenv("FFMPEG_BINARY", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            matches = sorted(winget_root.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), reverse=True)
            if matches:
                return str(matches[0])

    candidates = [
        Path(os.getenv("ProgramFiles", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
        Path(os.getenv("ProgramFiles(x86)", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def transcribe_local(audio: Path) -> list[dict[str, Any]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrError("缺少 faster-whisper，请先安装") from exc

    model = WhisperModel("small", device="auto", compute_type="auto")
    raw_segments, _ = model.transcribe(str(audio), language="zh")
    return normalize_segments(
        [{"start": item.start, "end": item.end, "text": item.text} for item in raw_segments]
    )


def transcribe_openai(audio: Path, config: dict[str, str]) -> list[dict[str, Any]]:
    if audio.stat().st_size > OPENAI_MAX_BYTES:
        raise AsrError("音频超过 OpenAI 25MB 限制，请改用本地转写")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise AsrError("缺少 openai，请先安装") from exc

    api_key = config.get("api_key", "")
    if not api_key:
        raise AsrError("缺少 OpenAI API Key，请先在网页配置")
    client_kwargs = {"api_key": api_key}
    if config.get("base_url"):
        client_kwargs["base_url"] = config["base_url"]
    client = OpenAI(**client_kwargs)
    with audio.open("rb") as fp:
        result = client.audio.transcriptions.create(
            model=config.get("model") or db.TRANSCRIBE_DEFAULT_MODEL,
            file=fp,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    return normalize_segments(data.get("segments") or [])


def normalize_segments(raw_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_segments:
        text = clean_text(str(item.get("text") or ""))
        if not text:
            continue
        start = float(item.get("start") or 0)
        end = float(item.get("end") or start)
        normalized.append({"start": start, "end": end, "text": text})
    return normalized


def yt_dlp_command() -> list[str]:
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    raise AsrError("缺少 yt-dlp，请先安装")


def run_command(cmd: list[str], error_prefix: str) -> None:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise AsrError(f"{error_prefix}：找不到命令 {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AsrError(f"{error_prefix}：命令超时") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AsrError(f"{error_prefix}：{detail[-600:]}")
