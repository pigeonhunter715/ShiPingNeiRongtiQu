from __future__ import annotations

import os
from typing import Any

from . import db
from .text_utils import to_simplified

DEFAULT_SUMMARY_MODEL = db.SUMMARY_DEFAULT_MODEL
MAX_TRANSCRIPT_CHARS = 14000


class SummaryError(RuntimeError):
    pass


def resolve_summary_model() -> str:
    return resolve_summary_openai_config()["model"]


def resolve_summary_openai_config() -> dict[str, str]:
    config = db.get_openai_config("summary")
    if not config["api_key"]:
        config["api_key"] = os.getenv("OPENAI_API_KEY", "").strip()
        config["source"] = "env" if config["api_key"] else ""
    if not config["base_url"]:
        config["base_url"] = os.getenv("OPENAI_BASE_URL", "").strip()
    env_model = os.getenv("OPENAI_SUMMARY_MODEL", "").strip()
    if env_model and config["source"] == "env":
        config["model"] = env_model
    if not config["model"]:
        config["model"] = DEFAULT_SUMMARY_MODEL
    return config


def summarize_segments(video: dict[str, Any], segments: list[dict[str, Any]]) -> tuple[str, str]:
    config = resolve_summary_openai_config()
    api_key = config["api_key"]
    if not api_key:
        raise SummaryError("未配置 OpenAI Key")

    model = config["model"]
    prompt = build_summary_prompt(video, segments)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SummaryError("缺少 openai Python 包") from exc

    try:
        client_kwargs = {"api_key": api_key}
        if config.get("base_url"):
            client_kwargs["base_url"] = config["base_url"]
        client = OpenAI(**client_kwargs)
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": "你是视频内容整理助手。只用简体中文输出，结论清楚，适合快速浏览。",
                },
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as exc:
        raise SummaryError(f"OpenAI 总结失败：{exc}") from exc

    summary = to_simplified(extract_response_text(response).strip())
    if not summary:
        raise SummaryError("OpenAI 没有返回总结内容")
    return summary, model


def build_summary_prompt(video: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    title = video.get("part_title") or video.get("title") or "未命名视频"
    owner = video.get("owner") or "未知UP"
    transcript = to_simplified(truncate_transcript(format_transcript(segments)))
    return f"""请根据下面的视频字幕/转写稿，生成简体中文总结。

要求：
1. 第一行用一句话概括视频主要内容。
2. 接着输出 3-6 条要点，覆盖关键事实、观点、项目或事件。
3. 最后可选输出“适合搜索的关键词：...”，关键词用顿号分隔。
4. 不要编造字幕中没有的信息。

视频标题：{title}
UP主：{owner}

字幕/转写稿：
{transcript}
"""


def format_transcript(segments: list[dict[str, Any]]) -> str:
    lines = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def truncate_transcript(text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    clean = text.strip()
    if len(clean) <= max_chars:
        return clean

    head_len = max_chars // 2
    tail_len = max_chars - head_len
    return clean[:head_len].rstrip() + "\n\n...[中间内容已截断]...\n\n" + clean[-tail_len:].lstrip()


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text)

    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)
