from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .text_utils import to_simplified

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


class BilibiliError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoPage:
    bvid: str
    cid: int
    page: int
    title: str
    part_title: str
    owner: str
    duration: int
    url: str


class BilibiliClient:
    def __init__(self) -> None:
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "KHTML, like Gecko Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }

    async def expand_input(self, raw: str) -> list[VideoPage]:
        token = raw.strip()
        if not token:
            return []
        bvid = extract_bvid(token)
        if not bvid:
            raise BilibiliError("没有识别到 BV 号或 B站视频链接")
        return await self.get_pages(bvid)

    async def get_pages(self, bvid: str) -> list[VideoPage]:
        data = await self._get_json(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
        info = data["data"]
        title = info.get("title") or bvid
        owner = (info.get("owner") or {}).get("name") or ""
        pages: list[VideoPage] = []

        for page in info.get("pages") or []:
            page_no = int(page.get("page") or 1)
            cid = int(page["cid"])
            pages.append(
                VideoPage(
                    bvid=bvid,
                    cid=cid,
                    page=page_no,
                    title=title,
                    part_title=page.get("part") or title,
                    owner=owner,
                    duration=int(page.get("duration") or info.get("duration") or 0),
                    url=build_video_url(bvid, page_no),
                )
            )

        # Public ugc seasons are exposed from the same view endpoint. De-dupe
        # because current episodes can also appear in pages.
        seen = {(item.bvid, item.cid) for item in pages}
        ugc = ((info.get("ugc_season") or {}).get("sections") or [])
        for section in ugc:
            for episode in section.get("episodes") or []:
                ep_bvid = episode.get("bvid")
                cid = episode.get("cid")
                if not ep_bvid or not cid or (ep_bvid, int(cid)) in seen:
                    continue
                ep_title = episode.get("title") or title
                pages.append(
                    VideoPage(
                        bvid=ep_bvid,
                        cid=int(cid),
                        page=1,
                        title=ep_title,
                        part_title=episode.get("arc", {}).get("title") or ep_title,
                        owner=owner,
                        duration=int(episode.get("duration") or 0),
                        url=build_video_url(ep_bvid, 1),
                    )
                )
                seen.add((ep_bvid, int(cid)))

        return pages

    async def get_subtitle_segments(self, bvid: str, cid: int) -> tuple[str, list[dict[str, Any]]]:
        data = await self._get_json(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": bvid, "cid": cid},
        )
        subtitles = (((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not subtitles:
            return "", []

        subtitle = prefer_subtitle(subtitles)
        url = subtitle.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return "", []

        subtitle_data = await self._get_json(url)
        body = subtitle_data.get("body") or []
        segments = [
            {
                "start": float(item.get("from") or 0),
                "end": float(item.get("to") or item.get("from") or 0),
                "text": clean_text(str(item.get("content") or "")),
            }
            for item in body
            if clean_text(str(item.get("content") or ""))
        ]
        source = subtitle.get("lan_doc") or subtitle.get("lan") or "subtitle"
        return source, segments

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(headers=self._headers, timeout=20, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0):
            raise BilibiliError(str(data.get("message") or data.get("msg") or "B站接口返回错误"))
        return data


def extract_bvid(text: str) -> str | None:
    match = BV_RE.search(text)
    if match:
        return match.group(1)
    parsed = urlparse(text if "://" in text else f"https://{text}")
    qs = parse_qs(parsed.query)
    for values in qs.values():
        for value in values:
            match = BV_RE.search(value)
            if match:
                return match.group(1)
    return None


def build_video_url(bvid: str, page: int, start: int | None = None) -> str:
    url = f"https://www.bilibili.com/video/{bvid}"
    query = []
    if page > 1:
        query.append(f"p={page}")
    if start is not None and start > 0:
        query.append(f"t={start}")
    if query:
        url += "?" + "&".join(query)
    return url


def prefer_subtitle(subtitles: list[dict[str, Any]]) -> dict[str, Any]:
    for subtitle in subtitles:
        lang = (subtitle.get("lan") or "").lower()
        if lang.startswith("zh"):
            return subtitle
    return subtitles[0]


def clean_text(text: str) -> str:
    return to_simplified(re.sub(r"\s+", " ", text).strip())


async def import_pages(raw_inputs: list[str], on_progress, on_total_delta=None, should_skip=None) -> None:
    client = BilibiliClient()
    for raw in raw_inputs:
        try:
            pages = await client.expand_input(raw)
            if not pages:
                await on_progress(raw, None, False, "没有可导入的视频")
                continue
            if on_total_delta and len(pages) > 1:
                await on_total_delta(len(pages) - 1)
            for page in pages:
                if should_skip and should_skip(page):
                    await on_progress(raw, page, True, "已存在相同 BV/分P，已跳过", "", [], "skipped")
                    await asyncio.sleep(0.05)
                    continue
                await import_page(client, page, raw, on_progress)
                await asyncio.sleep(0.2)
        except Exception as exc:
            await on_progress(raw, None, False, str(exc))


async def import_page(client: BilibiliClient, page: VideoPage, raw: str, on_progress) -> None:
    try:
        source, segments = await client.get_subtitle_segments(page.bvid, page.cid)
        status = "ready" if segments else "no_transcript"
        error = "" if segments else "无可用字幕，待转写"
        await on_progress(raw, page, True, error, source, segments, status)
    except Exception as exc:
        await on_progress(raw, page, False, str(exc))
