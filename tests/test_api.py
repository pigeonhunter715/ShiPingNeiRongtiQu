import asyncio

from fastapi.testclient import TestClient

from app import db
from app.bilibili import VideoPage
from app.main import app, run_import_job


client = TestClient(app)


def delete_videos_by_bvid(*bvids: str) -> None:
    with db.connect() as conn:
        for bvid in bvids:
            rows = conn.execute("SELECT id FROM videos WHERE bvid = ?", (bvid,)).fetchall()
            for row in rows:
                conn.execute("DELETE FROM video_summaries WHERE video_id = ?", (int(row["id"]),))
                conn.execute("DELETE FROM segments WHERE video_id = ?", (int(row["id"]),))
                conn.execute("DELETE FROM videos WHERE id = ?", (int(row["id"]),))


def test_openai_settings_never_returns_full_key():
    db.init_db()
    try:
        response = client.put("/api/settings/openai", json={"api_key": "sk-test-secret-1234"})
        assert response.status_code == 200
        data = response.json()
        assert data["configured"] is True
        assert data["masked"] == "sk-...1234"
        assert "sk-test-secret-1234" not in response.text

        response = client.get("/api/settings/openai")
        assert response.status_code == 200
        assert "sk-test-secret-1234" not in response.text
    finally:
        db.set_openai_api_key("")


def test_openai_settings_save_summary_and_transcribe_configs():
    db.init_db()
    try:
        response = client.put(
            "/api/settings/openai",
            json={
                "target": "summary",
                "api_key": "sk-summary-secret-1234",
                "base_url": "https://relay.example.com/v1",
                "model": "relay-summary-model",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["configured"] is True
        assert data["summary"]["masked"] == "sk-...1234"
        assert data["summary"]["base_url"] == "https://relay.example.com/v1"
        assert data["summary"]["model"] == "relay-summary-model"
        assert "sk-summary-secret-1234" not in response.text

        response = client.put(
            "/api/settings/openai",
            json={
                "target": "transcribe",
                "api_key": "sk-transcribe-secret-5678",
                "base_url": "https://audio.example.com/v1",
                "model": "whisper-compatible",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["transcribe"]["configured"] is True
        assert data["transcribe"]["masked"] == "sk-...5678"
        assert data["transcribe"]["base_url"] == "https://audio.example.com/v1"
        assert data["transcribe"]["model"] == "whisper-compatible"
        assert "sk-transcribe-secret-5678" not in response.text
    finally:
        db.set_openai_config("summary", api_key="", base_url="", model="")
        db.set_openai_config("transcribe", api_key="", base_url="", model="")


def test_batch_transcribe_requires_selection():
    response = client.post("/api/transcribe-missing", json={"backend": "local", "video_ids": []})
    assert response.status_code == 400
    assert "选择" in response.json()["detail"]


def test_videos_api_groups_by_content_and_paginates():
    db.init_db()
    bvids = ["BV1pg411c7a1", "BV1pg411c7a2", "BV1pg411c7a3"]
    try:
        base_available = db.count_videos("available")
        base_unavailable = db.count_videos("unavailable")
        db.upsert_video(
            {
                "bvid": bvids[0],
                "cid": 101,
                "page": 1,
                "title": "可查看 A",
                "part_title": "可查看 A",
                "owner": "测试UP",
                "duration": 120,
                "url": f"https://www.bilibili.com/video/{bvids[0]}",
                "status": "ready",
                "transcript_source": "测试字幕",
            },
            [{"start": 1, "end": 2, "text": "有内容"}],
        )
        db.upsert_video(
            {
                "bvid": bvids[1],
                "cid": 102,
                "page": 1,
                "title": "暂无内容 B",
                "part_title": "暂无内容 B",
                "owner": "测试UP",
                "duration": 120,
                "url": f"https://www.bilibili.com/video/{bvids[1]}",
                "status": "no_transcript",
                "error": "无可用字幕，待转写",
            },
            [],
        )
        db.upsert_video(
            {
                "bvid": bvids[2],
                "cid": 103,
                "page": 1,
                "title": "可查看 C",
                "part_title": "可查看 C",
                "owner": "测试UP",
                "duration": 120,
                "url": f"https://www.bilibili.com/video/{bvids[2]}",
                "status": "ready",
                "transcript_source": "测试字幕",
            },
            [{"start": 1, "end": 2, "text": "也有内容"}],
        )

        response = client.get("/api/videos?content_state=available&limit=1&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == base_available + 2
        assert data["limit"] == 1
        assert data["offset"] == 0
        assert data["page"] == 1
        assert data["page_count"] == base_available + 2
        assert len(data["items"]) == 1
        assert data["items"][0]["segment_count"] > 0

        response = client.get("/api/videos?content_state=available&limit=1&offset=1")
        assert response.status_code == 200
        data = response.json()
        assert data["page"] == 2
        assert len(data["items"]) == 1
        assert data["items"][0]["segment_count"] > 0

        response = client.get("/api/videos?content_state=unavailable&limit=500")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == base_unavailable + 1
        inserted_unavailable = [item for item in data["items"] if item["bvid"] == bvids[1]]
        assert inserted_unavailable
        assert inserted_unavailable[0]["segment_count"] == 0

        response = client.get("/api/videos")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] >= 3
    finally:
        delete_videos_by_bvid(*bvids)


def test_video_content_returns_segments_with_jump_urls(monkeypatch):
    db.init_db()
    db.set_openai_api_key("")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        video_id = db.upsert_video(
            {
                "bvid": "BV1xx411c7mD",
                "cid": 123,
                "page": 2,
                "title": "测试视频",
                "part_title": "第二集",
                "owner": "测试UP",
                "duration": 120,
                "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
                "status": "ready",
                "transcript_source": "测试字幕",
            },
            [
                {"start": 9, "end": 12, "text": "后面的内容"},
                {"start": 1, "end": 3, "text": "這是前面的內容"},
            ],
        )

        response = client.get(f"/api/videos/{video_id}/content")
        assert response.status_code == 200
        data = response.json()
        assert data["video"]["title"] == "测试视频"
        assert data["summary_status"] == "missing_key"
        assert data["summary"] == ""
        assert [item["text"] for item in data["segments"]] == ["这是前面的内容", "后面的内容"]
        assert data["segments"][0]["time_text"] == "00:01"
        assert data["segments"][0]["jump_url"] == "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=1"
    finally:
        delete_videos_by_bvid("BV1xx411c7mD")


def test_video_content_returns_empty_segments_for_no_transcript(monkeypatch):
    db.init_db()
    db.set_openai_api_key("")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        video_id = db.upsert_video(
            {
                "bvid": "BV1yy411c7mD",
                "cid": 456,
                "page": 1,
                "title": "无字幕视频",
                "part_title": "无字幕视频",
                "owner": "测试UP",
                "duration": 120,
                "url": "https://www.bilibili.com/video/BV1yy411c7mD",
                "status": "no_transcript",
                "error": "无可用字幕，待转写",
                "asr_status": "pending",
            },
            [],
        )

        response = client.get(f"/api/videos/{video_id}/content")
        assert response.status_code == 200
        data = response.json()
        assert data["video"]["status"] == "no_transcript"
        assert data["video"]["asr_status"] == "pending"
        assert data["summary_status"] == "no_content"
        assert data["summary"] == ""
        assert data["segments"] == []
    finally:
        delete_videos_by_bvid("BV1yy411c7mD")


def test_video_content_404_for_missing_video():
    response = client.get("/api/videos/999999999/content")
    assert response.status_code == 404


def test_video_content_returns_cached_summary():
    db.init_db()
    try:
        video_id = db.upsert_video(
            {
                "bvid": "BV1zz411c7mD",
                "cid": 789,
                "page": 1,
                "title": "缓存总结视频",
                "part_title": "缓存总结视频",
                "owner": "测试UP",
                "duration": 120,
                "url": "https://www.bilibili.com/video/BV1zz411c7mD",
                "status": "ready",
                "transcript_source": "测试字幕",
            },
            [{"start": 1, "end": 3, "text": "前面的内容"}],
        )
        db.save_video_summary(video_id, "这是缓存总结", "test-model")

        response = client.get(f"/api/videos/{video_id}/content")
        assert response.status_code == 200
        data = response.json()
        assert data["summary_status"] == "ready"
        assert data["summary"] == "这是缓存总结"
        assert data["summary_model"] == "test-model"
    finally:
        delete_videos_by_bvid("BV1zz411c7mD")


def test_delete_video_removes_content_and_summary():
    db.init_db()
    try:
        video_id = db.upsert_video(
            {
                "bvid": "BV1dd411c7mD",
                "cid": 321,
                "page": 1,
                "title": "待删除视频",
                "part_title": "待删除视频",
                "owner": "测试UP",
                "duration": 120,
                "url": "https://www.bilibili.com/video/BV1dd411c7mD",
                "status": "ready",
                "transcript_source": "测试字幕",
            },
            [{"start": 1, "end": 3, "text": "删除测试"}],
        )
        db.save_video_summary(video_id, "待删除总结", "test-model")

        response = client.delete(f"/api/videos/{video_id}")
        assert response.status_code == 200
        assert db.get_video(video_id) is None
        assert db.get_video_segments(video_id) == []
        assert db.get_video_summary(video_id) is None

        response = client.delete(f"/api/videos/{video_id}")
        assert response.status_code == 404
    finally:
        delete_videos_by_bvid("BV1dd411c7mD")


def test_import_job_skips_existing_video(monkeypatch):
    db.init_db()
    try:
        original_id = db.upsert_video(
            {
                "bvid": "BV1ee411c7mD",
                "cid": 654,
                "page": 1,
                "title": "已有视频",
                "part_title": "已有视频",
                "owner": "测试UP",
                "duration": 120,
                "url": "https://www.bilibili.com/video/BV1ee411c7mD",
                "status": "ready",
                "transcript_source": "原字幕",
            },
            [{"start": 1, "end": 3, "text": "原始内容"}],
        )

        async def fake_expand_input(self, raw):
            return [
                VideoPage(
                    bvid="BV1ee411c7mD",
                    cid=654,
                    page=1,
                    title="新标题不应覆盖",
                    part_title="新标题不应覆盖",
                    owner="测试UP",
                    duration=120,
                    url="https://www.bilibili.com/video/BV1ee411c7mD",
                )
            ]

        async def fail_get_subtitle_segments(self, bvid, cid):
            raise AssertionError("重复视频不应重新拉取字幕")

        monkeypatch.setattr("app.bilibili.BilibiliClient.expand_input", fake_expand_input)
        monkeypatch.setattr("app.bilibili.BilibiliClient.get_subtitle_segments", fail_get_subtitle_segments)

        job_id = db.create_job(1, kind="import")
        asyncio.run(run_import_job(job_id, ["BV1ee411c7mD"]))

        job = db.get_job(job_id)
        video = db.get_video(original_id)
        assert job["status"] == "completed"
        assert job["processed"] == 1
        assert job["success"] == 0
        assert job["failed"] == 0
        assert job["skipped"] == 1
        assert job["logs"][0]["status"] == "skipped"
        assert video["title"] == "已有视频"
        assert db.get_video_segments(original_id)[0]["text"] == "原始内容"
    finally:
        delete_videos_by_bvid("BV1ee411c7mD")
