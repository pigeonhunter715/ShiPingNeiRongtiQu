const els = {
  importText: document.querySelector("#importText"),
  startImport: document.querySelector("#startImport"),
  importHint: document.querySelector("#importHint"),
  jobBadge: document.querySelector("#jobBadge"),
  jobLog: document.querySelector("#jobLog"),
  searchForm: document.querySelector("#searchForm"),
  searchInput: document.querySelector("#searchInput"),
  searchResults: document.querySelector("#searchResults"),
  searchCount: document.querySelector("#searchCount"),
  videoCount: document.querySelector("#videoCount"),
  availableVideoList: document.querySelector("#availableVideoList"),
  unavailableVideoList: document.querySelector("#unavailableVideoList"),
  availableVideoCount: document.querySelector("#availableVideoCount"),
  unavailableVideoCount: document.querySelector("#unavailableVideoCount"),
  availablePager: document.querySelector("#availablePager"),
  unavailablePager: document.querySelector("#unavailablePager"),
  refreshVideos: document.querySelector("#refreshVideos"),
  asrBackend: document.querySelector("#asrBackend"),
  asrStatus: document.querySelector("#asrStatus"),
  transcribeMissing: document.querySelector("#transcribeMissing"),
  openaiKeyStatus: document.querySelector("#openaiKeyStatus"),
  summaryOpenAIKey: document.querySelector("#summaryOpenAIKey"),
  summaryOpenAIBaseUrl: document.querySelector("#summaryOpenAIBaseUrl"),
  summaryOpenAIModel: document.querySelector("#summaryOpenAIModel"),
  summaryOpenAIStatus: document.querySelector("#summaryOpenAIStatus"),
  saveSummaryOpenAI: document.querySelector("#saveSummaryOpenAI"),
  clearSummaryOpenAI: document.querySelector("#clearSummaryOpenAI"),
  transcribeOpenAIKey: document.querySelector("#transcribeOpenAIKey"),
  transcribeOpenAIBaseUrl: document.querySelector("#transcribeOpenAIBaseUrl"),
  transcribeOpenAIModel: document.querySelector("#transcribeOpenAIModel"),
  transcribeOpenAIStatus: document.querySelector("#transcribeOpenAIStatus"),
  saveTranscribeOpenAI: document.querySelector("#saveTranscribeOpenAI"),
  clearTranscribeOpenAI: document.querySelector("#clearTranscribeOpenAI"),
  selectAllMissing: document.querySelector("#selectAllMissing"),
  clearSelection: document.querySelector("#clearSelection"),
  contentModal: document.querySelector("#contentModal"),
  contentTitle: document.querySelector("#contentTitle"),
  contentMeta: document.querySelector("#contentMeta"),
  contentBody: document.querySelector("#contentBody"),
  closeContent: document.querySelector("#closeContent"),
};

let pollTimer = null;
let selectedVideoIds = new Set();
const VIDEO_PAGE_SIZE = 20;
const videoPages = {
  available: 1,
  unavailable: 1,
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || "请求失败");
  }
  return data;
}

function statusText(status) {
  const map = {
    ready: "已索引",
    no_transcript: "无字幕，待转写",
    failed: "失败",
    completed: "已完成",
    completed_with_errors: "完成，有失败",
    running: "运行中",
    queued: "排队中",
    pending: "待处理",
    not_started: "未开始",
    skipped: "已跳过",
  };
  return map[status] || status || "未知";
}

function renderJob(job) {
  const skipped = job.skipped ? ` · 跳过 ${job.skipped}` : "";
  els.jobBadge.textContent = `${statusText(job.status)} · ${job.processed}/${job.total}${skipped}`;
  els.jobLog.innerHTML = (job.logs || [])
    .slice()
    .reverse()
    .map((item) => {
      const title = item.title || item.input || "任务项";
      const detail = item.part_title ? `${item.part_title} · ` : "";
      return `
        <div class="job-row">
          <div class="row-title">
            <span>${escapeHtml(title)}</span>
            <span class="status-${escapeHtml(item.status)}">${statusText(item.status)}</span>
          </div>
          <div class="meta">${escapeHtml(detail + (item.message || ""))}</div>
        </div>
      `;
    })
    .join("");
}

async function pollJob(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const job = await api(`/api/jobs/${jobId}`);
    renderJob(job);
    if (!["queued", "running"].includes(job.status)) {
      clearInterval(pollTimer);
      els.startImport.disabled = false;
      els.transcribeMissing.disabled = false;
      els.importHint.textContent = job.kind === "transcribe" ? "转写结束。" : "导入结束。";
      await loadVideos();
      await loadAsrStatus();
      await loadOpenAISettings();
    }
  }, 1200);
}

async function startImport() {
  const text = els.importText.value.trim();
  if (!text) {
    els.importHint.textContent = "请先粘贴链接或 BV号。";
    return;
  }
  els.startImport.disabled = true;
  els.importHint.textContent = "正在创建导入任务...";
  els.jobLog.innerHTML = "";
  try {
    const data = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    els.importHint.textContent = `任务 #${data.job_id} 已开始。`;
    await pollJob(data.job_id);
  } catch (err) {
    els.startImport.disabled = false;
    els.importHint.textContent = err.message;
  }
}

async function loadVideos() {
  const [available, unavailable] = await Promise.all([
    loadVideoGroup("available"),
    loadVideoGroup("unavailable"),
  ]);
  const availableAdjusted = await adjustVideoPageIfNeeded("available", available);
  const unavailableAdjusted = await adjustVideoPageIfNeeded("unavailable", unavailable);
  if (availableAdjusted || unavailableAdjusted) {
    return loadVideos();
  }
  els.videoCount.textContent = `${available.total + unavailable.total} 个视频`;
  renderVideoGroup("available", available, "还没有可查看内容。");
  renderVideoGroup("unavailable", unavailable, "还没有暂无内容的视频。");
  updateBatchButton();
}

async function loadVideoGroup(state) {
  const offset = (videoPages[state] - 1) * VIDEO_PAGE_SIZE;
  return api(`/api/videos?content_state=${state}&limit=${VIDEO_PAGE_SIZE}&offset=${offset}`);
}

function renderVideoGroup(state, data, emptyText) {
  const items = data.items || [];
  const list = state === "available" ? els.availableVideoList : els.unavailableVideoList;
  const count = state === "available" ? els.availableVideoCount : els.unavailableVideoCount;
  const pager = state === "available" ? els.availablePager : els.unavailablePager;
  count.textContent = `${data.total || 0} 个视频`;

  if (!items.length) {
    list.className = "video-list empty";
    list.textContent = emptyText;
  } else {
    list.className = "video-list";
    list.innerHTML = items.map((item) => renderVideoRow(item, state)).join("");
  }

  renderPager(pager, state, data);
}

function renderVideoRow(item, state) {
  const title = item.part_title && item.part_title !== item.title ? `${item.title} / ${item.part_title}` : item.title;
  const canView = state === "available";
  return `
    <div class="video-row">
      <div class="row-title">
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(title)}</a>
        <div class="video-actions">
          ${
            state === "unavailable" && item.status === "no_transcript"
              ? `<label class="select-cell"><input type="checkbox" data-select-transcribe="${item.id}" ${selectedVideoIds.has(item.id) ? "checked" : ""} />选择</label>`
              : ""
          }
          <span class="status-${escapeHtml(item.status)}">${statusText(item.status)}</span>
          ${canView ? `<button class="small-button" data-content="${item.id}">查看内容</button>` : `<span class="muted-action">暂无内容</span>`}
          ${item.status === "no_transcript" ? `<button class="small-button" data-transcribe="${item.id}">转写</button>` : ""}
          <button class="small-button" data-refresh="${item.id}">刷新</button>
          <button class="small-button danger-button" data-delete="${item.id}" data-title="${escapeHtml(title)}">删除</button>
        </div>
      </div>
      <div class="meta">
        ${escapeHtml(item.owner || "未知UP")} · BV ${escapeHtml(item.bvid)} · P${item.page} ·
        ${item.segment_count} 条字幕 · ASR ${statusText(item.asr_status)} · ${escapeHtml(item.transcript_source || item.error || "")}
      </div>
    </div>
  `;
}

function renderPager(pager, state, data) {
  const page = data.page || 1;
  const pageCount = data.page_count || 1;
  pager.innerHTML = `
    <button class="small-button" data-page-group="${state}" data-page-direction="prev" ${page <= 1 ? "disabled" : ""}>上一页</button>
    <span>第 ${page} / ${pageCount} 页</span>
    <button class="small-button" data-page-group="${state}" data-page-direction="next" ${page >= pageCount ? "disabled" : ""}>下一页</button>
  `;
}

async function adjustVideoPageIfNeeded(state, data) {
  const pageCount = data.page_count || 1;
  if (videoPages[state] <= pageCount) {
    return false;
  }
  videoPages[state] = pageCount;
  return true;
}

async function search(event) {
  event.preventDefault();
  const q = els.searchInput.value.trim();
  if (!q) {
    els.searchCount.textContent = "0 条结果";
    els.searchResults.className = "results empty";
    els.searchResults.textContent = "输入关键词后开始搜索。";
    return;
  }
  const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
  const items = data.items || [];
  els.searchCount.textContent = `${items.length} 条结果`;
  if (!items.length) {
    els.searchResults.className = "results empty";
    els.searchResults.textContent = "没有找到匹配字幕。";
    return;
  }
  els.searchResults.className = "results";
  els.searchResults.innerHTML = items
    .map((item) => {
      const title = item.part_title && item.part_title !== item.title ? `${item.title} / ${item.part_title}` : item.title;
      return `
        <div class="result-row">
          <div class="row-title">
            <a href="${escapeHtml(item.jump_url)}" target="_blank" rel="noreferrer">${escapeHtml(title)}</a>
            <span>${escapeHtml(item.time_text)}</span>
          </div>
          <div class="snippet">${escapeHtml(item.snippet)}</div>
          <div class="meta">${escapeHtml(item.owner || "未知UP")} · BV ${escapeHtml(item.bvid)}</div>
        </div>
      `;
    })
    .join("");
}

async function refreshVideo(id, button) {
  button.disabled = true;
  button.textContent = "刷新中";
  try {
    await api(`/api/videos/${id}/refresh`, { method: "POST", body: "{}" });
    await loadVideos();
  } finally {
    button.disabled = false;
    button.textContent = "刷新";
  }
}

async function deleteVideo(id, title, button) {
  const ok = window.confirm(`确定删除「${title || "这个视频"}」吗？\n本地字幕、转写稿和总结缓存会一起删除。`);
  if (!ok) {
    return;
  }
  button.disabled = true;
  button.textContent = "删除中";
  try {
    await api(`/api/videos/${id}`, { method: "DELETE" });
    selectedVideoIds.delete(Number(id));
    await loadVideos();
    els.importHint.textContent = "已删除视频。";
  } catch (err) {
    els.importHint.textContent = err.message;
    button.disabled = false;
    button.textContent = "删除";
  }
}

function openContentModal() {
  els.contentModal.classList.remove("hidden");
  els.contentModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
}

function closeContentModal() {
  els.contentModal.classList.add("hidden");
  els.contentModal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function showVideoContent(id) {
  openContentModal();
  els.contentTitle.textContent = "视频内容";
  els.contentMeta.textContent = "正在加载...";
  els.contentBody.className = "content-body empty";
  els.contentBody.textContent = "正在加载内容...";
  try {
    const data = await api(`/api/videos/${id}/content`);
    renderVideoContent(data);
  } catch (err) {
    els.contentMeta.textContent = "";
    els.contentBody.className = "content-body empty";
    els.contentBody.textContent = err.message;
  }
}

function renderVideoContent(data) {
  const video = data.video || {};
  const segments = data.segments || [];
  const title = video.part_title && video.part_title !== video.title ? `${video.title} / ${video.part_title}` : video.title;
  els.contentTitle.textContent = title || "视频内容";
  els.contentMeta.innerHTML = `
    ${escapeHtml(video.owner || "未知UP")} · BV ${escapeHtml(video.bvid || "")} · P${escapeHtml(video.page || "")} ·
    ${statusText(video.status)} · ASR ${statusText(video.asr_status)} · ${escapeHtml(video.transcript_source || video.error || "")}
  `;

  const summaryHtml = renderSummary(data);
  if (!segments.length) {
    els.contentBody.className = "content-body";
    els.contentBody.innerHTML = `
      ${summaryHtml}
      <p>暂无内容，可先转写这个视频。</p>
      ${
        video.status === "no_transcript"
          ? `<button class="primary" data-transcribe-from-content="${video.id}">转写</button>`
          : ""
      }
    `;
    return;
  }

  els.contentBody.className = "content-body";
  els.contentBody.innerHTML = `
    ${summaryHtml}
    <div class="segments-list">
      ${segments
        .map((segment) => `
          <article class="segment-row">
            <a class="segment-time" href="${escapeHtml(segment.jump_url)}" target="_blank" rel="noreferrer">${escapeHtml(segment.time_text)}</a>
            <p>${escapeHtml(segment.text)}</p>
          </article>
        `)
        .join("")}
    </div>
  `;
}

function renderSummary(data) {
  const status = data.summary_status || "no_content";
  const summary = data.summary || "";
  const error = data.summary_error || "";
  const model = data.summary_model ? ` · ${escapeHtml(data.summary_model)}` : "";
  const statusTextMap = {
    ready: `已生成${model}`,
    missing_key: "未配置 OpenAI Key",
    no_content: "暂无可总结内容",
    failed: "生成失败",
  };
  const bodyMap = {
    missing_key: "保存 OpenAI Key 后，再次打开即可自动生成总结。",
    no_content: "当前视频还没有字幕或转写稿，完成转写后可生成总结。",
    failed: error || "总结生成失败，不影响查看完整字幕。",
  };
  const body = status === "ready" ? summary : bodyMap[status] || "";
  return `
    <section class="summary-box summary-${escapeHtml(status)}">
      <div class="summary-head">
        <h3>总结概括</h3>
        <span>${escapeHtml(statusTextMap[status] || status)}</span>
      </div>
      <div class="summary-text">${escapeHtml(body).replaceAll("\n", "<br />")}</div>
    </section>
  `;
}

async function loadAsrStatus() {
  const status = await api("/api/asr/status");
  const rows = [
    ["ffmpeg", status.ffmpeg],
    ["yt-dlp", status.yt_dlp],
    ["faster-whisper", status.faster_whisper],
    ["OpenAI", status.openai],
  ];
  els.asrStatus.innerHTML = rows
    .map(([name, item]) => {
      const ok = item.ok ? "可用" : "缺少";
      const install = item.ok ? "" : ` · ${escapeHtml(item.install || "")}`;
      return `<div><strong>${escapeHtml(name)}</strong>：${ok}${install}</div>`;
    })
    .join("");
}

async function loadOpenAISettings() {
  const settings = await api("/api/settings/openai");
  renderOpenAIConfig("summary", settings.summary || {});
  renderOpenAIConfig("transcribe", settings.transcribe || {});
  els.openaiKeyStatus.innerHTML = `
    <div><strong>总结</strong>：${configStatusText(settings.summary || {})}</div>
    <div><strong>转写</strong>：${configStatusText(settings.transcribe || {})}</div>
  `;
}

function renderOpenAIConfig(target, config) {
  const prefix = target === "summary" ? "summaryOpenAI" : "transcribeOpenAI";
  els[`${prefix}BaseUrl`].value = config.base_url || "";
  els[`${prefix}Model`].value = config.model || "";
  els[`${prefix}Status`].textContent = plainConfigStatusText(config);
}

function configStatusText(config) {
  return escapeHtml(plainConfigStatusText(config));
}

function plainConfigStatusText(config) {
  const sourceMap = { settings: "网页配置", legacy: "旧 Key 备用", env: "环境变量" };
  const source = sourceMap[config.source] || "未配置";
  const masked = config.masked ? ` · ${config.masked}` : "";
  const baseUrl = config.base_url ? ` · ${config.base_url}` : "";
  const model = config.model ? ` · ${config.model}` : "";
  return `${source}${masked}${baseUrl}${model}`;
}

async function saveOpenAIConfig(target) {
  const prefix = target === "summary" ? "summaryOpenAI" : "transcribeOpenAI";
  const saveButton = els[target === "summary" ? "saveSummaryOpenAI" : "saveTranscribeOpenAI"];
  saveButton.disabled = true;
  try {
    await api("/api/settings/openai", {
      method: "PUT",
      body: JSON.stringify({
        target,
        api_key: els[`${prefix}Key`].value.trim(),
        base_url: els[`${prefix}BaseUrl`].value.trim(),
        model: els[`${prefix}Model`].value.trim(),
      }),
    });
    els[`${prefix}Key`].value = "";
    await loadOpenAISettings();
    await loadAsrStatus();
  } finally {
    saveButton.disabled = false;
  }
}

async function clearOpenAIConfig(target) {
  const prefix = target === "summary" ? "summaryOpenAI" : "transcribeOpenAI";
  const clearButton = els[target === "summary" ? "clearSummaryOpenAI" : "clearTranscribeOpenAI"];
  clearButton.disabled = true;
  try {
    await api("/api/settings/openai", {
      method: "PUT",
      body: JSON.stringify({ target, api_key: "", base_url: "", model: "" }),
    });
    els[`${prefix}Key`].value = "";
    await loadOpenAISettings();
    await loadAsrStatus();
  } finally {
    clearButton.disabled = false;
  }
}

async function startTranscribe(videoId, button) {
  button.disabled = true;
  button.textContent = "转写中";
  try {
    const data = await api(`/api/videos/${videoId}/transcribe`, {
      method: "POST",
      body: JSON.stringify({ backend: els.asrBackend.value }),
    });
    els.importHint.textContent = `转写任务 #${data.job_id} 已开始。`;
    await pollJob(data.job_id);
  } catch (err) {
    els.importHint.textContent = err.message;
    button.disabled = false;
    button.textContent = "转写";
  }
}

async function transcribeMissing() {
  const videoIds = [...selectedVideoIds];
  if (!videoIds.length) {
    els.importHint.textContent = "请先选择要转写的视频。";
    return;
  }
  els.transcribeMissing.disabled = true;
  try {
    const data = await api("/api/transcribe-missing", {
      method: "POST",
      body: JSON.stringify({ backend: els.asrBackend.value, video_ids: videoIds }),
    });
    els.importHint.textContent = `批量转写任务 #${data.job_id} 已开始。`;
    await pollJob(data.job_id);
  } catch (err) {
    els.importHint.textContent = err.message;
    els.transcribeMissing.disabled = false;
  }
}

function updateBatchButton() {
  const count = selectedVideoIds.size;
  els.transcribeMissing.disabled = count === 0;
  els.transcribeMissing.textContent = count ? `转写选中视频 (${count})` : "转写选中视频";
}

function selectAllMissing() {
  els.unavailableVideoList.querySelectorAll("[data-select-transcribe]").forEach((input) => {
    input.checked = true;
    selectedVideoIds.add(Number(input.dataset.selectTranscribe));
  });
  updateBatchButton();
}

function clearSelection() {
  selectedVideoIds.clear();
  els.unavailableVideoList.querySelectorAll("[data-select-transcribe]").forEach((input) => {
    input.checked = false;
  });
  updateBatchButton();
}

async function changeVideoPage(state, direction) {
  const delta = direction === "next" ? 1 : -1;
  videoPages[state] = Math.max(1, videoPages[state] + delta);
  await loadVideos();
}

function handleLibraryClick(event) {
  const pageButton = event.target.closest("[data-page-group]");
  if (pageButton) {
    changeVideoPage(pageButton.dataset.pageGroup, pageButton.dataset.pageDirection);
    return;
  }

  const contentButton = event.target.closest("[data-content]");
  if (contentButton) {
    showVideoContent(contentButton.dataset.content);
  }
  const button = event.target.closest("[data-refresh]");
  if (button) {
    refreshVideo(button.dataset.refresh, button);
  }
  const transcribeButton = event.target.closest("[data-transcribe]");
  if (transcribeButton) {
    startTranscribe(transcribeButton.dataset.transcribe, transcribeButton);
  }
  const deleteButton = event.target.closest("[data-delete]");
  if (deleteButton) {
    deleteVideo(deleteButton.dataset.delete, deleteButton.dataset.title, deleteButton);
  }
  const selectInput = event.target.closest("[data-select-transcribe]");
  if (selectInput) {
    const id = Number(selectInput.dataset.selectTranscribe);
    if (selectInput.checked) {
      selectedVideoIds.add(id);
    } else {
      selectedVideoIds.delete(id);
    }
    updateBatchButton();
  }
}

els.startImport.addEventListener("click", startImport);
els.searchForm.addEventListener("submit", search);
els.refreshVideos.addEventListener("click", loadVideos);
els.transcribeMissing.addEventListener("click", transcribeMissing);
els.saveSummaryOpenAI.addEventListener("click", () => saveOpenAIConfig("summary"));
els.clearSummaryOpenAI.addEventListener("click", () => clearOpenAIConfig("summary"));
els.saveTranscribeOpenAI.addEventListener("click", () => saveOpenAIConfig("transcribe"));
els.clearTranscribeOpenAI.addEventListener("click", () => clearOpenAIConfig("transcribe"));
els.selectAllMissing.addEventListener("click", selectAllMissing);
els.clearSelection.addEventListener("click", clearSelection);
els.closeContent.addEventListener("click", closeContentModal);
els.contentModal.addEventListener("click", (event) => {
  if (event.target.closest("[data-close-content]")) {
    closeContentModal();
  }
  const transcribeButton = event.target.closest("[data-transcribe-from-content]");
  if (transcribeButton) {
    closeContentModal();
    const rowButton = document.querySelector(`[data-transcribe="${transcribeButton.dataset.transcribeFromContent}"]`);
    if (rowButton) {
      startTranscribe(transcribeButton.dataset.transcribeFromContent, rowButton);
    }
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.contentModal.classList.contains("hidden")) {
    closeContentModal();
  }
});
els.availableVideoList.addEventListener("click", handleLibraryClick);
els.unavailableVideoList.addEventListener("click", handleLibraryClick);
els.availablePager.addEventListener("click", handleLibraryClick);
els.unavailablePager.addEventListener("click", handleLibraryClick);

loadVideos().catch((err) => {
  els.availableVideoList.textContent = err.message;
  els.unavailableVideoList.textContent = err.message;
});
loadAsrStatus().catch((err) => {
  els.asrStatus.textContent = err.message;
});
loadOpenAISettings().catch((err) => {
  els.openaiKeyStatus.textContent = err.message;
});
