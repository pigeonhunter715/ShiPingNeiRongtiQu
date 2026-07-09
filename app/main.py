from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .asr import AsrError, dependency_status, ensure_backend_ready, transcribe_video
from .bilibili import VideoPage, build_video_url, import_pages
from .summary import SummaryError, resolve_summary_openai_config, summarize_segments

ROOT = Path(__file__).resolve().parent

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="B站视频内容检索", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class ImportRequest(BaseModel):
    text: str


class TranscribeRequest(BaseModel):
    backend: str = "local"


class BatchTranscribeRequest(TranscribeRequest):
    video_ids: list[int] = []


class OpenAISettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    target: str = "legacy"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/import")
async def create_import(req: ImportRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    inputs = [line.strip() for line in req.text.splitlines() if line.strip()]
    if not inputs:
        raise HTTPException(status_code=400, detail="请输入至少一个 B站链接或 BV号")
    job_id = db.create_job(len(inputs), kind="import")
    background_tasks.add_task(run_import_job, job_id, inputs)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def read_job(job_id: int) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/api/videos")
def videos(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    content_state: str = Query("all", pattern="^(available|unavailable|all)$"),
) -> dict[str, Any]:
    total = db.count_videos(content_state)
    items = db.list_videos(limit, offset, content_state)
    page = offset // limit + 1
    page_count = max(1, (total + limit - 1) // limit)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": page,
        "page_count": page_count,
    }


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: int) -> dict[str, Any]:
    if not db.delete_video(video_id):
        raise HTTPException(status_code=404, detail="视频不存在")
    return {"ok": True}


@app.get("/api/asr/status")
def asr_status() -> dict[str, Any]:
    return dependency_status()


@app.get("/api/settings/openai")
def get_openai_settings() -> dict[str, Any]:
    key = db.get_openai_api_key()
    env_key = bool(__import__("os").getenv("OPENAI_API_KEY"))
    summary_config = openai_config_payload("summary")
    transcribe_config = openai_config_payload("transcribe")
    return {
        "configured": bool(key),
        "masked": db.mask_secret(key),
        "env_available": env_key,
        "effective_configured": bool(key or env_key),
        "source": "settings" if key else ("env" if env_key else ""),
        "summary": summary_config,
        "transcribe": transcribe_config,
    }


@app.put("/api/settings/openai")
def save_openai_settings(req: OpenAISettingsRequest) -> dict[str, Any]:
    if req.target == "summary":
        db.set_openai_config("summary", api_key=req.api_key, base_url=req.base_url, model=req.model)
    elif req.target == "transcribe":
        db.set_openai_config("transcribe", api_key=req.api_key, base_url=req.base_url, model=req.model)
    elif req.target == "legacy":
        db.set_openai_api_key(req.api_key)
    else:
        raise HTTPException(status_code=400, detail="target 必须是 summary、transcribe 或 legacy")
    return get_openai_settings()


@app.get("/api/search")
def search(q: str = "", limit: int = Query(80, ge=1, le=200)) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"items": []}
    items = []
    for item in db.search_segments(query, limit):
        start = int(item["start"])
        item["time_text"] = format_seconds(start)
        item["jump_url"] = build_video_url(item["bvid"], item["page"], start)
        item["snippet"] = highlight_context(item["text"], query)
        items.append(item)
    return {"items": items}


@app.get("/api/videos/{video_id}/content")
def video_content(video_id: int) -> dict[str, Any]:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")

    segments = []
    for segment in db.get_video_segments(video_id):
        start = int(segment["start"])
        segment["time_text"] = format_seconds(start)
        segment["jump_url"] = build_video_url(video["bvid"], video["page"], start)
        segments.append(segment)

    summary_data = resolve_video_summary(video, segments)
    return {"video": video, "segments": segments, **summary_data}


@app.post("/api/videos/{video_id}/refresh")
async def refresh_video(video_id: int) -> dict[str, Any]:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    page = VideoPage(
        bvid=video["bvid"],
        cid=int(video["cid"]),
        page=int(video["page"]),
        title=video["title"],
        part_title=video["part_title"],
        owner=video["owner"],
        duration=int(video["duration"]),
        url=video["url"],
    )
    from .bilibili import BilibiliClient

    client = BilibiliClient()
    source, segments = await client.get_subtitle_segments(page.bvid, page.cid)
    status = "ready" if segments else "no_transcript"
    error = "" if segments else "无可用字幕，待转写"
    db.upsert_video(video_payload(page, status, error, source), segments)
    return {"ok": True, "status": status, "segments": len(segments)}


@app.post("/api/videos/{video_id}/transcribe")
async def transcribe_one(
    video_id: int,
    req: TranscribeRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="视频不存在")
    if video["status"] != "no_transcript":
        raise HTTPException(status_code=400, detail="只有无字幕视频需要转写")
    validate_backend(req.backend)
    validate_backend_ready(req.backend)
    job_id = db.create_job(1, kind="transcribe")
    background_tasks.add_task(run_transcribe_job, job_id, [video_id], req.backend)
    return {"job_id": job_id}


@app.post("/api/transcribe-missing")
async def transcribe_missing(req: BatchTranscribeRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    validate_backend(req.backend)
    video_ids = dedupe_video_ids(req.video_ids)
    if not video_ids:
        raise HTTPException(status_code=400, detail="请先选择要转写的视频")
    videos = db.get_videos_by_ids(video_ids)
    if len(videos) != len(video_ids):
        found = {int(item["id"]) for item in videos}
        missing = [str(video_id) for video_id in video_ids if video_id not in found]
        raise HTTPException(status_code=400, detail="视频不存在：" + "、".join(missing))
    invalid = [str(item["id"]) for item in videos if item["status"] != "no_transcript"]
    if invalid:
        raise HTTPException(status_code=400, detail="只能批量转写无字幕视频，以下视频不符合：" + "、".join(invalid))
    validate_backend_ready(req.backend)
    job_id = db.create_job(len(videos), kind="transcribe")
    background_tasks.add_task(run_transcribe_job, job_id, video_ids, req.backend)
    return {"job_id": job_id}


async def run_import_job(job_id: int, inputs: list[str]) -> None:
    db.update_job(job_id, status="running")

    async def on_progress(
        raw: str,
        page: VideoPage | None,
        ok: bool,
        message: str,
        source: str = "",
        segments: list[dict[str, Any]] | None = None,
        status: str = "failed",
    ) -> None:
        if page and ok and status == "skipped":
            db.update_job(
                job_id,
                processed_delta=1,
                skipped_delta=1,
                log={
                    "input": raw,
                    "bvid": page.bvid,
                    "cid": page.cid,
                    "title": page.title,
                    "part_title": page.part_title,
                    "status": "skipped",
                    "message": message or "已存在相同 BV/分P，已跳过",
                },
            )
            return
        if page and ok:
            db.upsert_video(video_payload(page, status, message, source), segments or [])
            log = {
                "input": raw,
                "bvid": page.bvid,
                "cid": page.cid,
                "title": page.title,
                "part_title": page.part_title,
                "status": status,
                "message": message or f"已导入 {len(segments or [])} 条字幕",
            }
            db.update_job(job_id, processed_delta=1, success_delta=1, log=log)
        elif page:
            db.upsert_video(video_payload(page, "failed", message, ""), [])
            db.update_job(
                job_id,
                processed_delta=1,
                failed_delta=1,
                log={"input": raw, "bvid": page.bvid, "cid": page.cid, "status": "failed", "message": message},
            )
        else:
            db.update_job(
                job_id,
                processed_delta=1,
                failed_delta=1,
                log={"input": raw, "status": "failed", "message": message},
            )

    try:
        async def on_total_delta(delta: int) -> None:
            db.update_job(job_id, total_delta=delta)

        await import_pages(
            inputs,
            on_progress,
            on_total_delta,
            should_skip=lambda page: db.video_exists(page.bvid, page.cid),
        )
        job = db.get_job(job_id)
        failed = int(job["failed"]) if job else 0
        db.update_job(job_id, status="completed_with_errors" if failed else "completed")
    except Exception as exc:
        db.update_job(job_id, status="failed", log={"status": "failed", "message": str(exc)})


async def run_transcribe_job(job_id: int, video_ids: list[int], backend: str) -> None:
    db.update_job(job_id, status="running")
    for video_id in video_ids:
        video = db.get_video(video_id)
        if not video:
            db.update_job(
                job_id,
                processed_delta=1,
                failed_delta=1,
                log={"video_id": video_id, "status": "failed", "message": "视频不存在"},
            )
            continue
        if video["status"] != "no_transcript":
            db.update_job(
                job_id,
                processed_delta=1,
                failed_delta=1,
                log={"video_id": video_id, "title": video["title"], "status": "failed", "message": "不是无字幕视频"},
            )
            continue

        db.set_video_asr_status(video_id, "running", "")
        try:
            source, segments = await asyncio.to_thread(transcribe_video, video, backend)
            if not segments:
                raise AsrError("转写完成但没有生成文本")
            db.save_transcript(video_id, source, segments)
            db.update_job(
                job_id,
                processed_delta=1,
                success_delta=1,
                log={
                    "video_id": video_id,
                    "title": video["title"],
                    "part_title": video["part_title"],
                    "status": "ready",
                    "message": f"已转写 {len(segments)} 条字幕",
                },
            )
        except Exception as exc:
            message = str(exc)
            db.set_video_asr_status(video_id, "failed", message)
            db.update_job(
                job_id,
                processed_delta=1,
                failed_delta=1,
                log={
                    "video_id": video_id,
                    "title": video["title"],
                    "part_title": video["part_title"],
                    "status": "failed",
                    "message": message,
                },
            )
    job = db.get_job(job_id)
    failed = int(job["failed"]) if job else 0
    db.update_job(job_id, status="completed_with_errors" if failed else "completed")


def validate_backend(backend: str) -> None:
    if backend not in {"local", "openai"}:
        raise HTTPException(status_code=400, detail="backend 必须是 local 或 openai")


def validate_backend_ready(backend: str) -> None:
    try:
        ensure_backend_ready(backend)
    except AsrError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def dedupe_video_ids(video_ids: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for video_id in video_ids:
        if video_id > 0 and video_id not in seen:
            seen.add(video_id)
            result.append(video_id)
    return result


def openai_config_payload(kind: str) -> dict[str, Any]:
    config = db.get_openai_config(kind)
    env_key = __import__("os").getenv("OPENAI_API_KEY", "").strip()
    effective_key = config["api_key"] or env_key
    source = config["source"] or ("env" if env_key else "")
    return {
        "configured": bool(config["api_key"]),
        "effective_configured": bool(effective_key),
        "masked": db.mask_secret(config["api_key"]),
        "env_available": bool(env_key),
        "source": source,
        "base_url": config["base_url"],
        "model": config["model"],
    }


def resolve_video_summary(video: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    video_id = int(video["id"])
    cached = db.get_video_summary(video_id)
    if cached:
        return {
            "summary": cached["summary"],
            "summary_status": "ready",
            "summary_error": "",
            "summary_model": cached["model"],
        }
    if not segments:
        return {
            "summary": "",
            "summary_status": "no_content",
            "summary_error": "",
            "summary_model": "",
        }
    if not resolve_summary_openai_config()["api_key"]:
        return {
            "summary": "",
            "summary_status": "missing_key",
            "summary_error": "未配置 OpenAI Key",
            "summary_model": "",
        }

    try:
        summary, model = summarize_segments(video, segments)
        db.save_video_summary(video_id, summary, model)
        return {
            "summary": summary,
            "summary_status": "ready",
            "summary_error": "",
            "summary_model": model,
        }
    except SummaryError as exc:
        return {
            "summary": "",
            "summary_status": "failed",
            "summary_error": str(exc),
            "summary_model": "",
        }


def video_payload(page: VideoPage, status: str, error: str, source: str) -> dict[str, Any]:
    return {
        "bvid": page.bvid,
        "cid": page.cid,
        "page": page.page,
        "title": page.title,
        "part_title": page.part_title,
        "owner": page.owner,
        "duration": page.duration,
        "url": page.url,
        "status": status,
        "error": error,
        "transcript_source": source,
        "asr_status": "pending" if status == "no_transcript" else "not_started",
    }


def format_seconds(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def highlight_context(text: str, query: str) -> str:
    index = text.lower().find(query.lower())
    if index < 0:
        return text
    start = max(0, index - 45)
    end = min(len(text), index + len(query) + 45)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix
