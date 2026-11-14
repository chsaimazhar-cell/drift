const API_BASE = window.API_BASE || "";
const HISTORY_KEY = "drift-download-history-v3";
const ACTIVE_JOBS_KEY = "drift-active-jobs-v3";

const $ = (selector) => document.querySelector(selector);
const urlInput = $("#urlInput");
const analyzeBtn = $("#analyzeBtn");
const pasteBtn = $("#pasteBtn");
const consoleMessage = $("#consoleMessage");
const consoleStatus = $("#consoleStatus");
const consoleStatusLight = $("#consoleStatusLight");
const toast = $("#toast");
const toastText = $("#toastText");
const themeToggle = $("#themeToggle");
const modal = $("#resultModal");
const modalClose = $("#modalClose");
const downloadBtn = $("#downloadBtn");
const downloadsSection = $("#downloads");
const downloadList = $("#downloadList");
const downloadsSummary = $("#downloadsSummary");

let currentAnalysis = null;
const activeJobs = new Map();

function showToast(message, type = "success") {
  toastText.textContent = message;
  toast.classList.toggle("toast-error", type === "error");
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function setConsoleState(label, message, active = false) {
  consoleStatus.textContent = label;
  consoleMessage.textContent = message;
  consoleStatusLight.classList.toggle("active", active);
}

function setAnalyzeLoading(loading) {
  analyzeBtn.classList.toggle("loading", loading);
  analyzeBtn.querySelector(".btn-label").textContent = loading ? "Analyzing" : "Analyze";
  analyzeBtn.querySelector(".btn-arrow").textContent = loading ? "◌" : "→";
  if (loading) setConsoleState("Analyzing", "Checking the public media source…", true);
}

function validUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

function detectPlatform(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    if (host.includes("instagram.")) return "Instagram";
    if (host.includes("tiktok.")) return "TikTok";
    if (host.includes("youtube.") || host === "youtu.be") return "YouTube";
    if (host.includes("facebook.") || host === "fb.watch") return "Facebook";
    if (host === "x.com" || host.endsWith(".x.com") || host.includes("twitter.")) return "X";
  } catch {}
  return "Public media";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatSpeed(bytesPerSecond) {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return "—";
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const secs = Math.ceil(seconds % 60);
  return `${minutes}m ${secs}s`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function qualityLabel(quality) {
  return !quality || quality === "best" ? "Best available" : quality;
}

function thumbnailMarkup(job) {
  if (!job.thumbnail) return `<div class="download-thumb-fallback"><span>▶</span></div>`;
  return `<img src="${escapeHtml(job.thumbnail)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className:'download-thumb-fallback', innerHTML:'<span>▶</span>'}))">`;
}

function ensureDownloadsVisible() {
  downloadsSection.hidden = false;
  requestAnimationFrame(() => downloadsSection.classList.add("downloads-visible"));
}

function createDownloadCard(job) {
  const existing = $(`#download-${job.id}`);
  if (existing) return existing;

  ensureDownloadsVisible();
  const card = document.createElement("article");
  card.className = "download-card reveal visible";
  card.dataset.status = job.status || "queued";
  card.id = `download-${job.id}`;
  card.innerHTML = `
    <div class="download-card-main">
      <div class="download-thumb">${thumbnailMarkup(job)}</div>
      <div class="download-card-info">
        <div class="download-card-topline">
          <span class="download-platform">${escapeHtml(job.platform || "PUBLIC MEDIA")}</span>
          <span class="download-state-badge state-${job.status}">${escapeHtml(job.phase || "Queued")}</span>
        </div>
        <h3>${escapeHtml(job.title || "Preparing media…")}</h3>
        <p class="download-meta">${escapeHtml(qualityLabel(job.quality))} · MP4${job.duration ? ` · ${escapeHtml(job.duration)}` : ""}</p>
        <div class="progress-wrap">
          <div class="progress-head">
            <span class="progress-label">${escapeHtml(job.phase || "Queued")}</span>
            <strong class="progress-percent">${job.progress != null ? `${Math.round(job.progress)}%` : "—"}</strong>
          </div>
          <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${job.progress ?? 0}">
            <span class="progress-bar"></span>
          </div>
          <div class="progress-foot">
            <span class="progress-size">${formatBytes(job.downloaded_bytes)}${job.total_bytes ? ` / ${formatBytes(job.total_bytes)}` : ""}</span>
            <span class="progress-speed">${formatSpeed(job.speed)}${job.eta != null ? ` · ${formatEta(job.eta)}` : ""}</span>
          </div>
        </div>
        <div class="download-card-actions">
          <button class="secondary-action cancel-action" type="button">Cancel</button>
          <button class="primary-action save-action" type="button" hidden>Save to device <span>↓</span></button>
          <button class="secondary-action retry-action" type="button" hidden>Try again</button>
        </div>
        <p class="download-error" hidden></p>
      </div>
    </div>`;

  card.querySelector(".cancel-action").addEventListener("click", () => cancelJob(job.id));
  card.querySelector(".save-action").addEventListener("click", () => transferFile(job.id));
  card.querySelector(".retry-action").addEventListener("click", () => retryJob(job.id));
  downloadList.prepend(card);
  return card;
}

function updateDownloadCard(job) {
  const card = createDownloadCard(job);
  const badge = card.querySelector(".download-state-badge");
  const progressLabel = card.querySelector(".progress-label");
  const percent = card.querySelector(".progress-percent");
  const bar = card.querySelector(".progress-bar");
  const track = card.querySelector(".progress-track");
  const size = card.querySelector(".progress-size");
  const speed = card.querySelector(".progress-speed");
  const cancel = card.querySelector(".cancel-action");
  const save = card.querySelector(".save-action");
  const retry = card.querySelector(".retry-action");
  const error = card.querySelector(".download-error");
  const title = card.querySelector("h3");

  badge.textContent = job.phase || job.status;
  badge.className = `download-state-badge state-${job.status}`;
  card.dataset.status = job.status;
  progressLabel.textContent = job.phase || "Queued";
  percent.textContent = job.progress != null ? `${Math.round(job.progress)}%` : "—";
  bar.style.width = `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`;
  track.setAttribute("aria-valuenow", String(Math.max(0, Math.min(100, Number(job.progress) || 0))));
  track.classList.toggle("indeterminate", job.status === "queued" || (job.status === "downloading" && job.progress == null));
  size.textContent = `${formatBytes(job.downloaded_bytes)}${job.total_bytes ? ` / ${formatBytes(job.total_bytes)}` : ""}`;
  speed.textContent = `${formatSpeed(job.speed)}${job.eta != null ? ` · ${formatEta(job.eta)}` : ""}`;
  if (job.title && job.title !== "Preparing media…") title.textContent = job.title;

  const terminal = ["ready", "failed", "cancelled"].includes(job.status);
  cancel.hidden = terminal;
  save.hidden = job.status !== "ready";
  retry.hidden = job.status !== "failed" && job.status !== "cancelled";
  error.hidden = !job.error;
  error.textContent = job.error || "";

  if (job.status === "ready") {
    progressLabel.textContent = "Ready to save";
    speed.textContent = `${formatBytes(job.file_size)} · MP4`;
  }
  if (job.status === "failed") {
    progressLabel.textContent = "Download failed";
    percent.textContent = "!";
  }
  if (job.status === "cancelled") {
    progressLabel.textContent = "Cancelled";
    percent.textContent = "—";
  }

  updateDownloadsSummary();
  return card;
}

function updateDownloadsSummary() {
  const cards = [...downloadList.querySelectorAll(".download-card")];
  const active = cards.filter(card => !["ready", "failed", "cancelled"].includes(card.dataset.status)).length;
  downloadsSummary.textContent = active
    ? `${active} active download${active === 1 ? "" : "s"} · live progress updates from the backend`
    : "Your active and recent downloads appear here.";
}

async function analyze() {
  const url = urlInput.value.trim();
  if (!url) {
    setConsoleState("Ready when you are", "Paste a public video URL to continue.");
    urlInput.focus();
    return;
  }
  if (!validUrl(url)) {
    setConsoleState("Needs a valid link", "That doesn't look like a valid URL.");
    showToast("Enter a valid URL", "error");
    urlInput.focus();
    return;
  }

  setAnalyzeLoading(true);
  currentAnalysis = null;

  try {
    const response = await fetch(`${API_BASE}/api/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.message || `Analysis failed (${response.status})`);

    currentAnalysis = { url, ...data };
    $("#resultPlatform").textContent = (data.platform || detectPlatform(url)).toUpperCase();
    $("#resultTitle").textContent = data.title || "Public video";
    $("#resultMeta").textContent = data.duration ? `Duration · ${data.duration}` : "Media information is available.";
    $("#resultDuration").textContent = data.duration || "";

    const thumbnail = $("#resultThumbnail");
    const placeholder = $("#thumbPlaceholder");
    thumbnail.hidden = true;
    placeholder.hidden = false;
    thumbnail.removeAttribute("src");
    if (data.thumbnail) {
      thumbnail.onload = () => { thumbnail.hidden = false; placeholder.hidden = true; };
      thumbnail.onerror = () => { thumbnail.hidden = true; placeholder.hidden = false; };
      thumbnail.src = data.thumbnail;
    }

    const qualitySelect = $("#qualitySelect");
    const available = (data.formats || []).map(item => item.quality).filter(Boolean);
    qualitySelect.innerHTML = "";
    const qualities = available.length ? available : ["Best available"];
    qualities.forEach((quality) => {
      const option = document.createElement("option");
      option.value = quality;
      option.textContent = quality;
      qualitySelect.appendChild(option);
    });

    setConsoleState("Media ready", "Analysis complete · choose a quality to download");
    openModal();
  } catch (error) {
    currentAnalysis = null;
    $("#resultPlatform").textContent = detectPlatform(url).toUpperCase();
    $("#resultTitle").textContent = "Analysis failed";
    $("#resultMeta").textContent = error.message || "The source could not be analyzed.";
    $("#resultDuration").textContent = "";
    const thumbnail = $("#resultThumbnail");
    thumbnail.hidden = true;
    thumbnail.removeAttribute("src");
    $("#thumbPlaceholder").hidden = false;
    setConsoleState("Analysis failed", error.message || "The source could not be analyzed.");
    showToast(error.message || "Analysis failed", "error");
    openModal();
  } finally {
    setAnalyzeLoading(false);
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text) throw new Error();
    urlInput.value = text.trim();
    showToast("URL pasted");
    urlInput.focus();
  } catch {
    showToast("Clipboard permission unavailable", "error");
    urlInput.focus();
  }
}

function openModal() {
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
}

function closeModal() {
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("modal-open");
}

async function startDownload() {
  if (!currentAnalysis) {
    showToast("Analyze a video first", "error");
    return;
  }

  const quality = $("#qualitySelect").value;
  downloadBtn.disabled = true;
  downloadBtn.innerHTML = "Starting <span class=\"inline-spinner\">◌</span>";

  try {
    const response = await fetch(`${API_BASE}/api/downloads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: currentAnalysis.url,
        quality: quality === "Best available" ? "best" : quality,
        format: "MP4",
        thumbnail: currentAnalysis.thumbnail,
        platform: currentAnalysis.platform,
        title: currentAnalysis.title,
        duration: currentAnalysis.duration
      })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.message || `Could not start download (${response.status})`);

    const job = data.job;
    activeJobs.set(job.id, { job, pollTimer: null, transferXhr: null });
    persistActiveJobs();
    createDownloadCard(job);
    updateDownloadCard(job);
    closeModal();
    setConsoleState("Downloading", "Live progress is shown below · you can cancel at any time", true);
    downloadsSection.scrollIntoView({ behavior: "smooth", block: "start" });
    pollJob(job.id);
  } catch (error) {
    showToast(error.message || "Could not start download", "error");
  } finally {
    downloadBtn.disabled = false;
    downloadBtn.innerHTML = 'Download <span>↓</span>';
  }
}

async function pollJob(jobId) {
  const state = activeJobs.get(jobId);
  if (!state) return;

  try {
    const response = await fetch(`${API_BASE}/api/downloads/${jobId}`, { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (response.status === 404) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
      activeJobs.delete(jobId);
      persistActiveJobs();
      const card = $(`#download-${jobId}`);
      if (card) {
        card.dataset.status = "failed";
        const badge = card.querySelector(".download-state-badge");
        const label = card.querySelector(".progress-label");
        const error = card.querySelector(".download-error");
        const retry = card.querySelector(".retry-action");
        const cancel = card.querySelector(".cancel-action");
        badge.textContent = "Expired";
        badge.className = "download-state-badge state-failed";
        label.textContent = "Job expired";
        error.hidden = false;
        error.textContent = "This download is no longer available. Start it again from the original link.";
        retry.hidden = false;
        cancel.hidden = true;
      }
      updateDownloadsSummary();
      return;
    }
    if (!response.ok || !data.success) throw new Error(data.message || "Could not read download status.");

    state.job = data.job;
    updateDownloadCard(state.job);

    if (state.job.status === "ready") {
      state.pollTimer = null;
      persistActiveJobs();
      setConsoleState("Download ready", "The MP4 is ready to save to your device", true);
      showToast("Download ready");
      return;
    }
    if (state.job.status === "failed") {
      state.pollTimer = null;
      persistActiveJobs();
      setConsoleState("Download failed", state.job.error || "The source could not be downloaded.");
      showToast(state.job.error || "Download failed", "error");
      return;
    }
    if (state.job.status === "cancelled") {
      state.pollTimer = null;
      persistActiveJobs();
      setConsoleState("Download cancelled", "Ready for another link");
      return;
    }

    state.pollTimer = setTimeout(() => pollJob(jobId), 650);
  } catch (error) {
    const card = $(`#download-${jobId}`);
    if (card) {
      const errorEl = card.querySelector(".download-error");
      errorEl.hidden = false;
      errorEl.textContent = error.message || "Lost connection to the backend.";
    }
    state.pollTimer = setTimeout(() => pollJob(jobId), 1600);
  }
}

async function cancelJob(jobId) {
  const state = activeJobs.get(jobId);
  if (!state) return;
  const card = $(`#download-${jobId}`);
  const button = card?.querySelector(".cancel-action");
  if (button) {
    button.disabled = true;
    button.textContent = "Cancelling…";
  }
  try {
    const response = await fetch(`${API_BASE}/api/downloads/${jobId}/cancel`, { method: "POST" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.message || "Could not cancel the download");
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.job = data.job;
    updateDownloadCard(state.job);
    persistActiveJobs();
    await pollJob(jobId);
  } catch {
    if (button) {
      button.disabled = false;
      button.textContent = "Cancel";
    }
    showToast("Could not cancel the download", "error");
  }
}

async function retryJob(jobId) {
  const state = activeJobs.get(jobId);
  const job = state?.job || null;
  if (!job) {
    const card = $(`#download-${jobId}`);
    if (!card) return;
    showToast("Use the original link to start a fresh download", "error");
    return;
  }
  urlInput.value = job.url;
  try {
    const response = await fetch(`${API_BASE}/api/downloads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: job.url, quality: job.quality, format: "MP4", thumbnail: job.thumbnail, title: job.title, duration: job.duration })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) throw new Error(data.message || "Retry failed");
    const nextJob = data.job;
    activeJobs.set(nextJob.id, { job: nextJob, pollTimer: null, transferXhr: null });
    persistActiveJobs();
    createDownloadCard(nextJob);
    updateDownloadCard(nextJob);
    pollJob(nextJob.id);
  } catch (error) {
    showToast(error.message || "Retry failed", "error");
  }
}

function getFilenameFromDisposition(header) {
  if (!header) return "drift-video.mp4";
  const utf = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf) return decodeURIComponent(utf[1].replace(/['"]/g, ""));
  const plain = header.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : "drift-video.mp4";
}

async function transferFile(jobId) {
  const state = activeJobs.get(jobId);
  const card = $(`#download-${jobId}`);
  if (!card || !state?.job) return;

  const save = card.querySelector(".save-action");
  const progressLabel = card.querySelector(".progress-label");
  const percent = card.querySelector(".progress-percent");
  const speed = card.querySelector(".progress-speed");

  save.disabled = true;
  save.innerHTML = 'Saving <span class="inline-spinner">◌</span>';
  progressLabel.textContent = "Starting save…";

  const fileUrl = `${API_BASE}/api/downloads/${encodeURIComponent(jobId)}/file`;
  try {
    // 1-byte probe: proves the file is really ready before the browser starts saving it.
    const probe = await fetch(fileUrl, { cache: "no-store", headers: { Range: "bytes=0-0" } });
    if (!probe.ok) {
      const data = await probe.json().catch(() => ({}));
      throw new Error(data.message || `File is not available (${probe.status})`);
    }
    const filename = getFilenameFromDisposition(probe.headers.get("Content-Disposition"));
    try { await probe.arrayBuffer(); } catch {}

    // The browser's own download manager saves the finished MP4 byte-for-byte (no JS memory, works for big files).
    const a = document.createElement("a");
    a.href = fileUrl;
    a.download = filename;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();

    progressLabel.textContent = "Saved by your browser";
    percent.textContent = "100%";
    speed.textContent = `${formatBytes(state.job.file_size)} · check your Downloads folder`;
    persistHistory(state.job);
    showToast("Download started — check your Downloads folder");
    setConsoleState("Download complete", "Your video is in the browser's Downloads folder", true);
  } catch (error) {
    progressLabel.textContent = "Save failed";
    showToast(error.message || "The file transfer failed", "error");
  } finally {
    save.disabled = false;
    save.innerHTML = 'Save again <span>↓</span>';
  }
}

function completeTransfer(card, job, size) {
  const progressLabel = card.querySelector(".progress-label");
  const percent = card.querySelector(".progress-percent");
  const bar = card.querySelector(".progress-bar");
  const track = card.querySelector(".progress-track");
  const sizeEl = card.querySelector(".progress-size");
  const speed = card.querySelector(".progress-speed");
  const save = card.querySelector(".save-action");
  progressLabel.textContent = "Complete";
  percent.textContent = "100%";
  bar.style.width = "100%";
  track.setAttribute("aria-valuenow", "100");
  sizeEl.textContent = formatBytes(size || job.file_size);
  speed.textContent = "Saved to device";
  save.disabled = false;
  save.innerHTML = 'Save again <span>↓</span>';
  persistHistory(job);
  showToast("Video saved to your device");
  setConsoleState("Download complete", "Your video has been saved to the device", true);
}

function persistHistory(job) {
  if (!job) return;
  let history = [];
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    history = Array.isArray(parsed) ? parsed : [];
  } catch {
    history = [];
  }
  const next = {
    id: job.id,
    url: job.url,
    title: job.title,
    platform: job.platform,
    quality: job.quality,
    thumbnail: job.thumbnail,
    completedAt: new Date().toISOString()
  };
  const filtered = history.filter(item => item.url !== next.url);
  localStorage.setItem(HISTORY_KEY, JSON.stringify([next, ...filtered].slice(0, 10)));
}


function persistActiveJobs() {
  try {
    const ids = [...activeJobs.keys()];
    localStorage.setItem(ACTIVE_JOBS_KEY, JSON.stringify(ids.slice(-20)));
  } catch {}
}

async function restoreActiveJobs() {
  let ids = [];
  try {
    const parsed = JSON.parse(localStorage.getItem(ACTIVE_JOBS_KEY) || "[]");
    ids = Array.isArray(parsed) ? parsed.filter(id => /^[A-Za-z0-9_-]{6,32}$/.test(id)) : [];
  } catch {
    ids = [];
  }
  if (!ids.length) return;
  ensureDownloadsVisible();
  for (const id of ids) {
    try {
      const response = await fetch(`${API_BASE}/api/downloads/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (response.status === 404) continue;
      const data = await response.json();
      if (!response.ok || !data.success) continue;
      const job = data.job;
      activeJobs.set(id, { job, pollTimer: null, transferXhr: null });
      createDownloadCard(job);
      updateDownloadCard(job);
      if (["queued", "downloading", "processing"].includes(job.status)) pollJob(id);
    } catch {}
  }
  persistActiveJobs();
}

function initTheme() {
  const saved = localStorage.getItem("drift-theme");
  if (saved) document.documentElement.dataset.theme = saved;
}

themeToggle.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("drift-theme", next);
});
analyzeBtn.addEventListener("click", analyze);
pasteBtn.addEventListener("click", pasteFromClipboard);
downloadBtn.addEventListener("click", startDownload);
modalClose.addEventListener("click", closeModal);
$(".modal-backdrop").addEventListener("click", closeModal);

urlInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") analyze();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && modal.classList.contains("open")) closeModal();
});

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) {
      entry.target.classList.add("visible");
      observer.unobserve(entry.target);
    }
  });
}, { threshold: 0.08 });

document.querySelectorAll(".reveal").forEach((el) => observer.observe(el));

initTheme();
restoreActiveJobs();
