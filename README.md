# B站视频内容检索

本地网页应用：批量导入 B站公开视频、多P视频和公开视频合集，提取已有字幕/AI字幕，保存到 SQLite，并按关键词搜索到具体时间点。

## 启动

```powershell
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

然后打开 http://127.0.0.1:8000

## 当前范围

- 支持公开视频链接、BV号、多P视频、公开视频合集/视频列表。
- 只索引 B站已有字幕/AI字幕。
- 无字幕视频可以手动转写单个视频，也可以勾选多个无字幕视频后批量转写。
- 转写支持本地 `faster-whisper` 和 OpenAI `whisper-1` 两种后端。
- 不支持登录态、会员视频、收藏夹。

## 无字幕转写依赖

应用不会自动安装系统依赖。打开页面后会在视频库区域显示当前依赖状态。

本地 Whisper 转写需要：

```powershell
winget install Gyan.FFmpeg
pip install yt-dlp faster-whisper
```

OpenAI 转写需要：

```powershell
winget install Gyan.FFmpeg
pip install yt-dlp openai
```

OpenAI 配置可以在网页的视频库区域保存到本地 SQLite；也可以继续使用 `OPENAI_API_KEY` 环境变量作为备用方式。接口只会返回脱敏后的 key 状态，不会返回完整 key。

网页里有两组独立配置：

- 总结模型配置：用于“查看内容”顶部的总结概括，默认模型 `gpt-5.4-mini`。
- 转写模型配置：用于 OpenAI 音频转写，默认模型 `whisper-1`。

如果使用中转 API Key，必须同时填写对应的 Base URL，例如 `https://example.com/v1`，否则应用会默认请求官方 OpenAI 地址，可能出现 401。

OpenAI 后端使用 `whisper-1`，因为第一版需要 segment 级时间戳来支持搜索结果跳转。临时音频文件会在转写结束后自动删除。
