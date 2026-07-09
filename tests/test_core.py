from app.bilibili import build_video_url, clean_text, extract_bvid, prefer_subtitle
from app.asr import dependency_status, normalize_segments, resolve_transcribe_openai_config
from app import db
from app.db import mask_secret
from app.main import format_seconds, highlight_context
from app.summary import build_summary_prompt, resolve_summary_model, resolve_summary_openai_config, truncate_transcript
from app.text_utils import to_simplified


def test_extract_bvid_from_url_and_plain_text():
    assert extract_bvid("BV1xx411c7mD") == "BV1xx411c7mD"
    assert extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD/?p=2") == "BV1xx411c7mD"


def test_build_video_url_with_page_and_time():
    assert build_video_url("BV1xx411c7mD", 1, 12) == "https://www.bilibili.com/video/BV1xx411c7mD?t=12"
    assert build_video_url("BV1xx411c7mD", 2, 12) == "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=12"


def test_prefer_zh_subtitle():
    subtitle = prefer_subtitle([{"lan": "en"}, {"lan": "zh-CN", "lan_doc": "中文"}])
    assert subtitle["lan"] == "zh-CN"


def test_clean_text_and_time_format():
    assert clean_text("  hello\n world  ") == "hello world"
    assert clean_text("  這是一個測試  ") == "这是一个测试"
    assert format_seconds(66) == "01:06"
    assert format_seconds(3661) == "01:01:01"


def test_highlight_context():
    assert highlight_context("这是一个机器学习视频", "机器") == "这是一个机器学习视频"


def test_normalize_segments_skips_blank_text():
    segments = normalize_segments(
        [
            {"start": 1, "end": 2, "text": "  你好\n世界  "},
            {"start": 3, "end": 4, "text": "   "},
        ]
    )
    assert segments == [{"start": 1.0, "end": 2.0, "text": "你好 世界"}]


def test_to_simplified_converts_traditional_chinese():
    assert to_simplified("科技資訊與語音生成") == "科技资讯与语音生成"


def test_dependency_status_shape():
    status = dependency_status()
    assert {"ffmpeg", "yt_dlp", "faster_whisper", "openai"} <= set(status)
    assert "ok" in status["ffmpeg"]
    assert "install" in status["yt_dlp"]


def test_mask_secret_does_not_leak_full_key():
    assert mask_secret("") == ""
    assert mask_secret("short") == "***"
    assert mask_secret("sk-test-1234567890") == "sk-...7890"


def test_truncate_transcript_keeps_short_text_and_marks_long_text():
    assert truncate_transcript("短文本", max_chars=10) == "短文本"
    text = "A" * 20 + "B" * 20
    result = truncate_transcript(text, max_chars=20)
    assert "中间内容已截断" in result
    assert result.startswith("A" * 10)
    assert result.endswith("B" * 10)


def test_build_summary_prompt_uses_simplified_chinese_instruction():
    prompt = build_summary_prompt(
        {"title": "标题", "part_title": "分集", "owner": "UP"},
        [{"text": "第一段"}, {"text": "這是第二段"}],
    )
    assert "简体中文总结" in prompt
    assert "一句话概括" in prompt
    assert "第一段" in prompt
    assert "这是第二段" in prompt


def test_default_summary_model_is_gpt_5_4_mini(monkeypatch):
    monkeypatch.delenv("OPENAI_SUMMARY_MODEL", raising=False)
    db.set_openai_config("summary", api_key="", base_url="", model="")
    assert resolve_summary_model() == "gpt-5.4-mini"


def test_openai_config_priority(monkeypatch):
    db.init_db()
    try:
        db.set_openai_api_key("sk-legacy")
        db.set_openai_config("summary", api_key="sk-summary", base_url="https://summary.example/v1", model="summary-model")
        db.set_openai_config("transcribe", api_key="", base_url="", model="")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

        summary = resolve_summary_openai_config()
        assert summary["api_key"] == "sk-summary"
        assert summary["base_url"] == "https://summary.example/v1"
        assert summary["model"] == "summary-model"

        transcribe = resolve_transcribe_openai_config()
        assert transcribe["api_key"] == "sk-legacy"
        assert transcribe["model"] == "whisper-1"
    finally:
        db.set_openai_api_key("")
        db.set_openai_config("summary", api_key="", base_url="", model="")
        db.set_openai_config("transcribe", api_key="", base_url="", model="")
