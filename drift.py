from __future__ import annotations

import functools
import importlib
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Generator
from urllib.parse import quote, unquote, urlparse

def _ensure_package(import_name: str, pip_name: str):
    """Import a package; if it is missing, install it with the SAME Python that is running this file."""
    try:
        return importlib.import_module(import_name)
    except ImportError:
        pass
    if os.getenv("DRIFT_NO_AUTO_INSTALL") == "1":
        return None
    print(f"[setup] '{pip_name}' is missing. Installing it now (one time only)...", flush=True)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pip_name])
        importlib.invalidate_caches()
        return importlib.import_module(import_name)
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] Could not install {pip_name}: {exc}", flush=True)
        return None


_certifi = _ensure_package("certifi", "certifi")
CERTIFI_CA = _certifi.where() if _certifi else None
if CERTIFI_CA:
    os.environ.setdefault("SSL_CERT_FILE", CERTIFI_CA)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CERTIFI_CA)

yt_dlp = _ensure_package("yt_dlp", "yt-dlp")
# imageio-ffmpeg ships a ready-made ffmpeg program, so the user does not need to install FFmpeg by hand.
_ensure_package("imageio_ffmpeg", "imageio-ffmpeg")

PORT = int(os.getenv("PORT") or os.getenv("DRIFT_PORT") or "5000")   # Render gives us PORT
HOST = os.getenv("DRIFT_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
MAX_URL_LENGTH = 4096
MAX_DOWNLOAD_BYTES = int(os.getenv("DRIFT_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
JOB_TTL_SECONDS = int(os.getenv("DRIFT_JOB_TTL_SECONDS", str(30 * 60)))
MAX_ACTIVE_JOBS = int(os.getenv("DRIFT_MAX_ACTIVE_JOBS", "2"))
DOWNLOAD_ROOT = Path(os.getenv("DRIFT_DOWNLOAD_ROOT", str(Path(tempfile.gettempdir()) / "drift-downloads")))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("DRIFT_DB_PATH", str(DOWNLOAD_ROOT / "drift.sqlite3")))
ALLOWED_SCHEMES = {"http", "https"}
KNOWN_HOSTS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "instagram.com", "instagr.am", "facebook.com", "fb.watch", "fb.com",
    "x.com", "twitter.com", "threads.net", "threads.com",
    "reddit.com", "redd.it", "rumble.com", "vimeo.com", "dailymotion.com", "dai.ly",
    "twitch.tv", "pinterest.com", "pin.it", "snapchat.com", "linkedin.com",
    "tumblr.com", "bilibili.com", "soundcloud.com", "streamable.com", "imgur.com",
    "vk.com", "ok.ru", "likee.video", "kwai.com", "9gag.com", "bsky.app", "t.me",
}

DB_LOCK = threading.RLock()
CANCEL_EVENTS: dict[str, threading.Event] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_ACTIVE_JOBS, thread_name_prefix="drift-download")
RATE_LOCK = threading.Lock()
RATE_WINDOWS: dict[str, list[float]] = {}


# ======================================================================
# EMBEDDED FRONTEND (HTML + CSS + JS) - everything lives in this one file
# ======================================================================
FRONTEND_FILES = {
    'index.html': r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="description" content="A fast, elegant public-media downloader. Paste a supported public video URL and prepare your download.">
  <meta name="theme-color" content="#0a0a0b">
  <title>Drift — Download without the detour</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="css/style.css">
</head>
<body>
  <div class="noise" aria-hidden="true"></div>
  <div class="ambient ambient-one" aria-hidden="true"></div>
  <div class="ambient ambient-two" aria-hidden="true"></div>

  <header class="nav-shell">
    <nav class="nav container">
      <a class="brand" href="#" aria-label="Drift home">
        <span class="brand-mark"><span></span></span>
        <span>drift</span>
      </a>

      <div class="nav-links">
        <a href="#how">How it works</a>
        <a href="#features">Features</a>
        <a href="#faq">FAQ</a>
      </div>

      <div class="nav-actions">
        <button class="icon-btn" id="themeToggle" aria-label="Toggle theme" title="Toggle theme">◐</button>
        <a class="nav-cta" href="#downloader">Try it <span>↗</span></a>
      </div>
    </nav>
  </header>

  <main>
    <section class="hero container" id="downloader">
      <div class="eyebrow reveal">
        <span class="eyebrow-dot"></span>
        Simple media tools, beautifully made
      </div>

      <h1 class="hero-title reveal reveal-delay-1">
        Download the moment.<br>
        <em>Keep the quality.</em>
      </h1>

      <p class="hero-copy reveal reveal-delay-2">
        Paste a supported public video link. Drift handles the busywork
        and gets you to a clean download without the clutter.
      </p>

      <div class="download-console reveal reveal-delay-3">
        <div class="console-topline">
          <span class="status-light" id="consoleStatusLight"></span>
          <span id="consoleStatus">Ready when you are</span>
          <span class="console-shortcut">⌘ V</span>
        </div>

        <div class="url-row">
          <div class="url-field">
            <span class="link-icon">↗</span>
            <input id="urlInput" type="url" autocomplete="off"
              placeholder="Paste a public video URL…"
              aria-label="Video URL">
            <button class="paste-btn" id="pasteBtn">Paste</button>
          </div>
          <button class="analyze-btn" id="analyzeBtn">
            <span class="btn-label">Analyze</span>
            <span class="btn-arrow">→</span>
          </button>
        </div>

        <div class="console-foot">
          <span id="consoleMessage">Public links only · No account required</span>
          <span class="platform-hint">MP4 · HD · Fast</span>
        </div>
      </div>

      <div class="platform-row reveal reveal-delay-4">
        <span>Built for the platforms you already use</span>
        <div class="platform-pills">
          <span>Instagram</span>
          <span>TikTok</span>
          <span>YouTube</span>
          <span>Facebook</span>
          <span>X</span>
        </div>
      </div>

      <div class="hero-stage reveal reveal-delay-5" aria-hidden="true">
        <div class="stage-glow"></div>
        <div class="product-window">
          <div class="window-bar">
            <span class="window-dots"><i></i><i></i><i></i></span>
            <span>drift / download</span>
            <span>•••</span>
          </div>
          <div class="window-content">
            <div class="preview-skeleton">
              <div class="preview-image">
                <div class="hero-preview-badge">LIVE PREVIEW</div>
                <div class="play-orb">▶</div>
              </div>
              <div class="preview-lines">
                <span class="line wide"></span>
                <span class="line medium"></span>
                <span class="line short"></span>
              </div>
            </div>
            <div class="format-panel">
              <div>
                <span class="panel-label">AVAILABLE FORMAT</span>
                <strong>MP4 · 1080p</strong>
              </div>
              <button>Download <span>↓</span></button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="downloads-section container" id="downloads" hidden aria-live="polite">
      <div class="downloads-heading reveal visible">
        <div>
          <span class="section-kicker">DOWNLOAD CENTER</span>
          <h2>Your downloads.<br><span>Live, not silent.</span></h2>
        </div>
        <p id="downloadsSummary">Your active and recent downloads appear here.</p>
      </div>
      <div class="download-list" id="downloadList"></div>
    </section>

    <section class="proof-strip container reveal">
      <div><strong>01</strong><span>Paste</span><p>Drop in a public link.</p></div>
      <div><strong>02</strong><span>Analyze</span><p>We inspect the media.</p></div>
      <div><strong>03</strong><span>Download</span><p>Choose your format.</p></div>
    </section>

    <section class="section container" id="features">
      <div class="section-heading reveal">
        <span class="section-kicker">THE DIFFERENCE</span>
        <h2>Quietly powerful.<br><span>Loudly useful.</span></h2>
        <p>Every part of Drift is designed to remove friction—not add another dashboard to your life.</p>
      </div>

      <div class="feature-grid">
        <article class="feature-card large reveal">
          <div class="feature-art art-speed">
            <div class="speed-ring ring-a"></div>
            <div class="speed-ring ring-b"></div>
            <div class="speed-core">01</div>
          </div>
          <div class="feature-copy">
            <span>01 / SPEED</span>
            <h3>One input.<br>Zero wandering.</h3>
            <p>No maze of buttons. Paste the link, see what is available, and choose what you need.</p>
          </div>
        </article>

        <article class="feature-card reveal">
          <div class="mini-art formats-art">
            <span>1080p</span><span>720p</span><span>480p</span>
          </div>
          <span>02 / CONTROL</span>
          <h3>Choose your quality.</h3>
          <p>When multiple public formats are available, pick the one that fits your use case.</p>
        </article>

        <article class="feature-card reveal">
          <div class="mini-art privacy-art">
            <div class="lock">⌁</div>
            <div class="privacy-orbit"></div>
          </div>
          <span>03 / PRIVACY</span>
          <h3>No account required.</h3>
          <p>Keep the experience lightweight. Your browser handles local preferences and history.</p>
        </article>

        <article class="feature-card wide reveal">
          <div class="wide-copy">
            <span>04 / BUILT TO FEEL FAST</span>
            <h3>Motion with a purpose.</h3>
            <p>Micro-interactions tell you what changed, where to look, and when you're ready. Nothing moves just because it can.</p>
          </div>
          <div class="motion-demo">
            <div class="motion-line"></div>
            <div class="motion-dot"></div>
            <div class="motion-dot delay"></div>
          </div>
        </article>
      </div>
    </section>

    <section class="how-section" id="how">
      <div class="container">
        <div class="section-heading reveal">
          <span class="section-kicker">HOW IT WORKS</span>
          <h2>Three steps.<br><span>That's it.</span></h2>
        </div>

        <div class="steps">
          <article class="step reveal">
            <div class="step-number">01</div>
            <div class="step-visual paste-visual"><span>Paste URL</span><i>↗</i></div>
            <h3>Bring the link.</h3>
            <p>Copy a public video URL and paste it into the downloader.</p>
          </article>
          <article class="step reveal">
            <div class="step-number">02</div>
            <div class="step-visual analyze-visual"><span>Analyzing</span><i class="loader"></i></div>
            <h3>Let Drift inspect it.</h3>
            <p>The backend validates the address and checks available public media information.</p>
          </article>
          <article class="step reveal">
            <div class="step-number">03</div>
            <div class="step-visual ready-visual"><span>Ready</span><i>↓</i></div>
            <h3>Take it with you.</h3>
            <p>Pick an available quality and start your download.</p>
          </article>
        </div>
      </div>
    </section>

    <section class="faq-section container" id="faq">
      <div class="faq-intro reveal">
        <span class="section-kicker">FAQ</span>
        <h2>Good questions<br>deserve clear answers.</h2>
      </div>
      <div class="faq-list">
        <details class="faq-item reveal">
          <summary>What kind of links can I use?<span>+</span></summary>
          <p>Drift is designed for publicly accessible media from supported platforms. Private content, DRM, authentication bypasses and other access-control circumvention are not supported.</p>
        </details>
        <details class="faq-item reveal">
          <summary>Do I need an account?<span>+</span></summary>
          <p>No. The initial project keeps the core experience account-free. Local preferences and recent items can be stored in your browser.</p>
        </details>
        <details class="faq-item reveal">
          <summary>Will every video have 1080p?<span>+</span></summary>
          <p>No. Available quality depends on the public media source. The interface only presents formats that the backend can legitimately retrieve.</p>
        </details>
        <details class="faq-item reveal">
          <summary>Can I add more platforms later?<span>+</span></summary>
          <p>Yes. The Python backend is organized around platform adapters so new supported sources can be added without rewriting the frontend.</p>
        </details>
      </div>
    </section>

    <section class="final-cta container reveal">
      <div class="final-glow"></div>
      <span class="section-kicker">READY WHEN YOU ARE</span>
      <h2>Less scrolling.<br><em>More keeping.</em></h2>
      <a href="#downloader" class="final-button">Start downloading <span>→</span></a>
    </section>
  </main>

  <footer class="footer container">
    <div class="footer-brand">
      <a class="brand" href="#"><span class="brand-mark"><span></span></span><span>drift</span></a>
      <p>Simple tools for the internet you actually use.</p>
    </div>
    <div class="footer-meta">
      <span>Built with HTML · CSS · JavaScript · Python</span>
      <span>© 2026 Drift</span>
    </div>
  </footer>

  <div class="toast" id="toast" role="status" aria-live="polite">
    <span class="toast-icon">✓</span>
    <span id="toastText">Copied</span>
  </div>

  <div class="result-modal" id="resultModal" aria-hidden="true">
    <div class="modal-backdrop"></div>
    <div class="modal-card">
      <button class="modal-close" id="modalClose" aria-label="Close">×</button>
      <div class="modal-kicker">MEDIA ANALYSIS</div>
      <div class="result-layout">
        <div class="result-thumb">
          <div class="thumb-placeholder" id="thumbPlaceholder"><span>▶</span></div>
          <img id="resultThumbnail" class="result-thumbnail" alt="Video thumbnail" hidden>
          <span class="duration" id="resultDuration"></span>
        </div>
        <div class="result-info">
          <span class="result-platform" id="resultPlatform">PUBLIC MEDIA</span>
          <h3 id="resultTitle">Ready to download</h3>
          <p id="resultMeta">Available media information is shown below.</p>
          <div class="select-row">
            <label>Quality<select id="qualitySelect"><option>1080p</option><option>720p</option><option>480p</option></select></label>
            <label>Format<select id="formatSelect"><option value="MP4">MP4</option></select></label>
          </div>
          <button class="download-result" id="downloadBtn">Download <span>↓</span></button>
          <small id="resultNote">Public media only · Drift only retrieves media the source makes publicly accessible.</small>
        </div>
      </div>
    </div>
  </div>

  <script src="js/app.js"></script>
</body>
</html>
""",
    'css/style.css': r""":root {
  --bg: #09090a;
  --bg-soft: #0d0d0f;
  --surface: rgba(255,255,255,.045);
  --surface-strong: #151517;
  --surface-hover: rgba(255,255,255,.075);
  --line: rgba(255,255,255,.10);
  --line-soft: rgba(255,255,255,.055);
  --text: #f4f2ed;
  --muted: #9c9a96;
  --muted-2: #66645f;
  --accent: #d9ff65;
  --accent-2: #baf83d;
  --danger: #ff6b6b;
  --shadow: 0 30px 100px rgba(0,0,0,.45);
  --radius: 24px;
  --font-body: "DM Sans", system-ui, sans-serif;
  --font-display: "Manrope", system-ui, sans-serif;
}

[data-theme="light"] {
  --bg: #f5f4f0;
  --bg-soft: #eeece7;
  --surface: rgba(0,0,0,.035);
  --surface-strong: #fff;
  --surface-hover: rgba(0,0,0,.06);
  --line: rgba(0,0,0,.10);
  --line-soft: rgba(0,0,0,.055);
  --text: #101011;
  --muted: #696762;
  --muted-2: #98958e;
  --accent: #9cc52d;
  --accent-2: #82a91e;
  --shadow: 0 30px 100px rgba(20,20,15,.12);
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background:
    radial-gradient(circle at 50% -15%, rgba(217,255,101,.075), transparent 28rem),
    var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  min-height: 100vh;
  overflow-x: hidden;
}
body.modal-open { overflow: hidden; }
button, input, select { font: inherit; }
button, a { -webkit-tap-highlight-color: transparent; }
a { color: inherit; text-decoration: none; }

.noise {
  position: fixed; inset: 0; pointer-events: none; opacity: .028; z-index: 100;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.7'/%3E%3C/svg%3E");
}
.ambient { position: fixed; border-radius: 50%; pointer-events:none; filter: blur(80px); opacity:.12; z-index:-1; }
.ambient-one { width: 24rem; height:24rem; background:#d9ff65; top:10rem; left:-18rem; animation: drift 14s ease-in-out infinite; }
.ambient-two { width:20rem; height:20rem; background:#fff; right:-15rem; top:35rem; opacity:.045; animation: drift 18s ease-in-out infinite reverse; }

.container { width:min(1160px, calc(100% - 40px)); margin-inline:auto; }
.nav-shell { position:sticky; top:0; z-index:50; background:rgba(9,9,10,.68); backdrop-filter:blur(18px); border-bottom:1px solid var(--line-soft); }
[data-theme="light"] .nav-shell { background:rgba(245,244,240,.72); }
.nav { height:72px; display:flex; align-items:center; justify-content:space-between; }
.brand { display:flex; align-items:center; gap:10px; font:800 19px var(--font-display); letter-spacing:-.06em; }
.brand-mark { width:25px; height:25px; border:1px solid var(--line); border-radius:8px; display:grid; place-items:center; transform:rotate(45deg); }
.brand-mark span { width:9px; height:9px; background:var(--accent); border-radius:3px; box-shadow:0 0 18px rgba(217,255,101,.35); }
.nav-links { display:flex; gap:28px; color:var(--muted); font-size:13px; }
.nav-links a { transition:.25s; }
.nav-links a:hover { color:var(--text); }
.nav-actions { display:flex; align-items:center; gap:10px; }
.icon-btn { width:34px; height:34px; border:1px solid var(--line); border-radius:50%; background:transparent; color:var(--muted); cursor:pointer; transition:.25s; }
.icon-btn:hover { color:var(--text); border-color:rgba(255,255,255,.2); transform:rotate(18deg); }
.nav-cta { border:1px solid var(--line); border-radius:999px; padding:9px 14px; font-size:12px; transition:.25s; }
.nav-cta:hover { background:var(--surface-hover); transform:translateY(-1px); }
.nav-cta span { color:var(--accent); margin-left:6px; }

.hero { padding-top:108px; text-align:center; }
.eyebrow { display:inline-flex; align-items:center; gap:8px; color:var(--muted); border:1px solid var(--line); padding:7px 11px; border-radius:999px; font-size:11px; letter-spacing:.02em; background:rgba(255,255,255,.025); }
.eyebrow-dot { width:6px; height:6px; border-radius:50%; background:var(--accent); box-shadow:0 0 12px rgba(217,255,101,.8); animation:pulse 2s infinite; }
.hero-title { font:800 clamp(52px, 7.4vw, 100px)/.92 var(--font-display); letter-spacing:-.075em; margin:28px auto 22px; max-width:1000px; }
.hero-title em { color:var(--muted); font-style:normal; }
.hero-copy { max-width:560px; margin:0 auto; color:var(--muted); font-size:16px; line-height:1.7; }

.download-console { width:min(820px, 100%); margin:46px auto 0; padding:13px; border:1px solid var(--line); border-radius:27px; background:linear-gradient(180deg, rgba(255,255,255,.055), rgba(255,255,255,.025)); box-shadow:var(--shadow), inset 0 1px 0 rgba(255,255,255,.05); text-align:left; }
.console-topline, .console-foot { display:flex; align-items:center; gap:8px; padding:0 7px 10px; color:var(--muted-2); font-size:10px; letter-spacing:.03em; }
.console-topline { padding-bottom:10px; }
.status-light { width:6px; height:6px; border-radius:50%; background:var(--accent); }
.console-shortcut { margin-left:auto; border:1px solid var(--line); padding:3px 7px; border-radius:6px; color:var(--muted); }
.url-row { display:flex; gap:9px; }
.url-field { flex:1; min-width:0; display:flex; align-items:center; height:58px; background:rgba(0,0,0,.18); border:1px solid var(--line); border-radius:17px; padding:0 10px 0 16px; transition:.25s; }
[data-theme="light"] .url-field { background:rgba(0,0,0,.025); }
.url-field:focus-within { border-color:rgba(217,255,101,.45); box-shadow:0 0 0 4px rgba(217,255,101,.055); }
.link-icon { color:var(--accent); font-size:17px; transform:rotate(-45deg); margin-right:10px; }
.url-field input { width:100%; min-width:0; border:0; outline:0; background:transparent; color:var(--text); font-size:14px; }
.url-field input::placeholder { color:var(--muted-2); }
.paste-btn { border:0; background:var(--surface); color:var(--muted); padding:8px 11px; border-radius:9px; cursor:pointer; font-size:11px; transition:.2s; }
.paste-btn:hover { color:var(--text); background:var(--surface-hover); }
.analyze-btn { border:0; border-radius:17px; background:var(--accent); color:#0b0b09; padding:0 20px; min-width:128px; display:flex; align-items:center; justify-content:space-between; gap:20px; font-weight:700; cursor:pointer; transition:.25s; }
.analyze-btn:hover { background:#e4ff82; transform:translateY(-2px); box-shadow:0 12px 30px rgba(217,255,101,.12); }
.analyze-btn.loading { opacity:.8; pointer-events:none; }
.analyze-btn.loading .btn-arrow { animation:spin 1s linear infinite; }
.console-foot { padding-top:10px; padding-bottom:0; }
.platform-hint { margin-left:auto; }

.platform-row { display:flex; justify-content:center; align-items:center; gap:18px; margin-top:26px; color:var(--muted-2); font-size:11px; }
.platform-pills { display:flex; gap:7px; flex-wrap:wrap; justify-content:center; }
.platform-pills span { padding:6px 9px; border:1px solid var(--line-soft); border-radius:7px; color:var(--muted); }

.hero-stage { position:relative; margin:88px auto 0; height:410px; max-width:1000px; perspective:1000px; }
.stage-glow { position:absolute; width:500px; height:180px; left:50%; top:50%; transform:translate(-50%,-50%); background:rgba(217,255,101,.12); filter:blur(90px); }
.product-window { position:absolute; left:50%; top:50%; width:min(760px, 90%); transform:translate(-50%,-50%) rotateX(3deg); border:1px solid var(--line); border-radius:18px; background:#111113; overflow:hidden; box-shadow:0 50px 100px rgba(0,0,0,.5); transition:transform .5s; }
.product-window:hover { transform:translate(-50%,-50%) rotateX(0deg) translateY(-5px); }
.window-bar { height:38px; display:flex; align-items:center; justify-content:space-between; padding:0 14px; border-bottom:1px solid var(--line-soft); color:var(--muted-2); font-size:9px; }
.window-dots { display:flex; gap:4px; }
.window-dots i { width:6px; height:6px; border-radius:50%; background:#333; }
.window-content { padding:22px; display:grid; grid-template-columns:1.25fr .75fr; gap:16px; text-align:left; }
.preview-skeleton { border:1px solid var(--line-soft); border-radius:13px; padding:10px; }
.preview-image { height:215px; border-radius:9px; background:linear-gradient(135deg,#24241f,#111312 55%,#303622); display:grid; place-items:center; position:relative; overflow:hidden; }
.preview-image:after { content:""; position:absolute; inset:0; background:linear-gradient(110deg,transparent 25%,rgba(255,255,255,.08) 50%,transparent 75%); animation:shimmer 4s infinite; }
.play-orb { width:45px; height:45px; border-radius:50%; background:rgba(217,255,101,.95); color:#111; display:grid; place-items:center; font-size:12px; z-index:1; box-shadow:0 0 35px rgba(217,255,101,.2); }
.preview-lines { display:flex; flex-direction:column; gap:7px; padding:12px 2px 3px; }
.line { display:block; height:7px; border-radius:99px; background:#242528; }
.line.wide { width:70%; } .line.medium { width:45%; } .line.short { width:28%; }
.format-panel { border:1px solid var(--line-soft); border-radius:13px; padding:18px; display:flex; flex-direction:column; justify-content:flex-end; gap:18px; }
.panel-label { display:block; color:var(--muted-2); font-size:8px; letter-spacing:.12em; margin-bottom:7px; }
.format-panel strong { font:600 15px var(--font-display); }
.format-panel button { border:1px solid rgba(217,255,101,.35); background:rgba(217,255,101,.07); color:var(--accent); border-radius:10px; padding:11px; text-align:left; cursor:default; }
.format-panel button span { float:right; }
.floating-chip { display:none; position:absolute; border:1px solid var(--line); background:rgba(20,20,21,.82); backdrop-filter:blur(14px); border-radius:999px; padding:9px 12px; color:var(--muted); font-size:10px; box-shadow:0 15px 35px rgba(0,0,0,.3); }
.chip-icon { color:var(--accent); margin-right:5px; }
.chip-one { left:3%; top:18%; animation:float 5s ease-in-out infinite; }
.chip-two { right:4%; bottom:22%; animation:float 6s ease-in-out infinite reverse; }

.proof-strip { border-top:1px solid var(--line); border-bottom:1px solid var(--line); display:grid; grid-template-columns:repeat(3,1fr); margin-top:10px; }
.proof-strip > div { padding:22px 25px; border-right:1px solid var(--line); }
.proof-strip > div:last-child { border:0; }
.proof-strip strong { color:var(--accent); font:500 11px var(--font-display); }
.proof-strip span { display:block; margin-top:8px; font:600 16px var(--font-display); }
.proof-strip p { margin:5px 0 0; color:var(--muted); font-size:12px; }

.section { padding-top:150px; }
.section-heading { display:grid; grid-template-columns:1fr 1fr; gap:30px; align-items:end; margin-bottom:55px; }
.section-kicker { color:var(--accent); font-size:10px; letter-spacing:.16em; font-weight:700; }
.section-heading h2 { margin:12px 0 0; font:700 clamp(42px,5vw,68px)/.98 var(--font-display); letter-spacing:-.065em; }
.section-heading h2 span { color:var(--muted); }
.section-heading > p { max-width:390px; color:var(--muted); font-size:14px; line-height:1.7; justify-self:end; }

.feature-grid { display:grid; grid-template-columns:1.2fr .8fr; gap:12px; }
.feature-card { min-height:420px; border:1px solid var(--line); border-radius:var(--radius); padding:28px; background:linear-gradient(145deg,rgba(255,255,255,.045),rgba(255,255,255,.018)); position:relative; overflow:hidden; }
.feature-card:hover { background:linear-gradient(145deg,rgba(255,255,255,.065),rgba(255,255,255,.018)); }
.feature-card.large { display:flex; flex-direction:column; justify-content:space-between; }
.feature-card.wide { grid-column:1/-1; min-height:250px; display:flex; align-items:flex-end; justify-content:space-between; }
.feature-card > span, .feature-copy > span, .wide-copy > span { font-size:9px; letter-spacing:.13em; color:var(--muted-2); }
.feature-card h3, .feature-copy h3, .wide-copy h3 { font:650 25px/1.05 var(--font-display); letter-spacing:-.045em; margin:12px 0 8px; }
.feature-card p, .feature-copy p, .wide-copy p { color:var(--muted); line-height:1.65; font-size:12px; max-width:400px; margin:0; }
.feature-art { height:230px; position:relative; display:grid; place-items:center; }
.speed-ring { position:absolute; border:1px solid rgba(217,255,101,.16); border-radius:50%; }
.ring-a { width:220px; height:220px; animation:spin 15s linear infinite; border-top-color:var(--accent); }
.ring-b { width:145px; height:145px; animation:spin 9s linear infinite reverse; border-right-color:rgba(255,255,255,.25); }
.speed-core { width:64px; height:64px; border-radius:50%; border:1px solid rgba(217,255,101,.35); background:rgba(217,255,101,.06); display:grid; place-items:center; color:var(--accent); font-size:11px; box-shadow:0 0 50px rgba(217,255,101,.08); }
.mini-art { height:220px; margin-bottom:24px; border:1px solid var(--line-soft); border-radius:16px; background:#111113; display:flex; align-items:center; justify-content:center; gap:8px; overflow:hidden; position:relative; }
.formats-art span { border:1px solid var(--line); border-radius:8px; padding:10px 9px; font:600 10px var(--font-display); transition:.3s; }
.formats-art span:first-child { color:var(--accent); border-color:rgba(217,255,101,.35); transform:translateY(-15px); }
.formats-art span:nth-child(2) { transform:translateY(5px); }
.formats-art span:last-child { transform:translateY(20px); color:var(--muted); }
.privacy-art { display:grid; place-items:center; }
.lock { width:72px; height:72px; border-radius:50%; border:1px solid rgba(217,255,101,.25); color:var(--accent); display:grid; place-items:center; font-size:25px; background:rgba(217,255,101,.05); z-index:2; }
.privacy-orbit { position:absolute; width:160px; height:160px; border:1px dashed var(--line); border-radius:50%; animation:spin 12s linear infinite; }
.wide-copy { max-width:530px; }
.motion-demo { width:42%; height:120px; position:relative; overflow:hidden; border:1px solid var(--line-soft); border-radius:15px; background:#111113; }
.motion-line { position:absolute; left:10%; right:10%; top:50%; height:1px; background:linear-gradient(90deg,transparent,var(--accent),transparent); }
.motion-dot { position:absolute; width:8px; height:8px; border-radius:50%; background:var(--accent); top:calc(50% - 4px); left:10%; box-shadow:0 0 18px var(--accent); animation:travel 3s ease-in-out infinite; }
.motion-dot.delay { animation-delay:1.5s; opacity:.4; }

.how-section { margin-top:150px; padding:120px 0; background:var(--bg-soft); border-block:1px solid var(--line); }
.steps { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
.step { padding:24px; border:1px solid var(--line); border-radius:20px; background:rgba(255,255,255,.018); }
.step-number { color:var(--accent); font-size:10px; margin-bottom:22px; }
.step-visual { height:160px; border:1px solid var(--line-soft); border-radius:14px; display:flex; align-items:center; justify-content:center; gap:10px; background:#101011; color:var(--muted); font-size:11px; }
.paste-visual i, .ready-visual i { color:var(--accent); font-style:normal; }
.analyze-visual { color:var(--text); }
.loader { width:14px; height:14px; border:1px solid var(--muted-2); border-top-color:var(--accent); border-radius:50%; animation:spin 1s linear infinite; }
.step h3 { font:600 19px var(--font-display); margin:22px 0 7px; letter-spacing:-.03em; }
.step p { margin:0; color:var(--muted); font-size:12px; line-height:1.65; }

.faq-section { padding:150px 0; display:grid; grid-template-columns:.7fr 1.3fr; gap:100px; }
.faq-intro h2 { font:650 clamp(35px,4vw,55px)/1 var(--font-display); letter-spacing:-.06em; margin:14px 0; }
.faq-list { border-top:1px solid var(--line); }
.faq-item { border-bottom:1px solid var(--line); }
.faq-item summary { list-style:none; cursor:pointer; display:flex; justify-content:space-between; align-items:center; padding:23px 0; font:600 15px var(--font-display); }
.faq-item summary::-webkit-details-marker { display:none; }
.faq-item summary span { color:var(--muted); font-size:20px; transition:.2s; }
.faq-item[open] summary span { transform:rotate(45deg); color:var(--accent); }
.faq-item p { margin:0; padding:0 35px 23px 0; color:var(--muted); font-size:12px; line-height:1.7; max-width:650px; }

.final-cta { position:relative; text-align:center; padding:120px 30px; border:1px solid var(--line); border-radius:30px; overflow:hidden; background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.012)); }
.final-glow { position:absolute; width:450px; height:200px; background:rgba(217,255,101,.12); filter:blur(100px); left:50%; top:35%; transform:translate(-50%,-50%); }
.final-cta h2 { position:relative; font:750 clamp(48px,7vw,90px)/.92 var(--font-display); letter-spacing:-.075em; margin:18px 0 30px; }
.final-cta h2 em { color:var(--muted); font-style:normal; }
.final-button { position:relative; display:inline-flex; align-items:center; gap:30px; padding:14px 18px; background:var(--accent); color:#10110c; border-radius:999px; font-weight:700; font-size:12px; transition:.25s; }
.final-button:hover { transform:translateY(-3px); box-shadow:0 18px 40px rgba(217,255,101,.14); }
.final-button span { font-size:17px; }
.footer { padding:70px 0 45px; display:flex; justify-content:space-between; align-items:flex-end; }
.footer-brand p { color:var(--muted); font-size:11px; max-width:220px; line-height:1.6; }
.footer-meta { color:var(--muted-2); font-size:10px; display:flex; flex-direction:column; gap:7px; text-align:right; }

.toast { position:fixed; right:22px; bottom:22px; z-index:120; display:flex; align-items:center; gap:9px; padding:10px 13px; border:1px solid var(--line); border-radius:12px; background:#171719; box-shadow:var(--shadow); color:var(--text); font-size:11px; transform:translateY(20px); opacity:0; pointer-events:none; transition:.3s; }
.toast.show { transform:none; opacity:1; }
.toast-icon { color:var(--accent); }

.result-modal { position:fixed; inset:0; z-index:110; display:none; place-items:center; padding:20px; }
.result-modal.open { display:grid; }
.modal-backdrop { position:absolute; inset:0; background:rgba(0,0,0,.7); backdrop-filter:blur(14px); animation:fadeIn .25s; }
.modal-card { position:relative; width:min(780px,100%); border:1px solid var(--line); border-radius:25px; background:#121214; box-shadow:var(--shadow); padding:30px; animation:modalIn .35s cubic-bezier(.2,.8,.2,1); }
.modal-close { position:absolute; top:17px; right:17px; width:32px; height:32px; border-radius:50%; border:1px solid var(--line); background:transparent; color:var(--muted); cursor:pointer; font-size:20px; }
.modal-kicker { color:var(--accent); font-size:9px; letter-spacing:.16em; }
.result-layout { display:grid; grid-template-columns:.9fr 1.1fr; gap:24px; margin-top:20px; }
.result-thumbnail { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; border-radius:inherit; display:block; }
.result-thumbnail[hidden] { display:none; }
.result-thumb .duration:empty { display:none; }
.result-thumb { aspect-ratio:16/10; border:1px solid var(--line); border-radius:16px; overflow:hidden; position:relative; }
.thumb-placeholder { width:100%; height:100%; display:grid; place-items:center; background:linear-gradient(135deg,#25251f,#121412 55%,#313821); }
.thumb-placeholder span { width:52px; height:52px; border-radius:50%; display:grid; place-items:center; background:var(--accent); color:#111; font-size:13px; padding-left:2px; }
.duration { position:absolute; right:9px; bottom:9px; background:rgba(0,0,0,.65); border-radius:6px; padding:5px 7px; font-size:9px; }
.result-platform { color:var(--muted-2); font-size:9px; letter-spacing:.13em; }
.result-info h3 { font:650 27px var(--font-display); letter-spacing:-.05em; margin:9px 0 7px; }
.result-info p { color:var(--muted); font-size:12px; line-height:1.6; margin:0 0 18px; }
.select-row { display:grid; grid-template-columns:1fr 1fr; gap:9px; }
.select-row label { color:var(--muted-2); font-size:9px; text-transform:uppercase; letter-spacing:.1em; }
.select-row select { width:100%; margin-top:7px; border:1px solid var(--line); background:#0d0d0f; color:var(--text); border-radius:10px; padding:11px; outline:none; }
.download-result { width:100%; margin-top:12px; padding:13px; border:0; border-radius:11px; background:var(--accent); color:#0c0c09; font-weight:700; cursor:pointer; }
.download-result span { float:right; }
.result-note { display:block; color:var(--muted-2); font-size:9px; line-height:1.5; margin-top:10px; }

.reveal { opacity:0; transform:translateY(22px); transition:opacity .8s ease, transform .8s cubic-bezier(.2,.8,.2,1); }
.reveal.visible { opacity:1; transform:none; }
.reveal-delay-1 { transition-delay:.08s; } .reveal-delay-2 { transition-delay:.16s; } .reveal-delay-3 { transition-delay:.24s; } .reveal-delay-4 { transition-delay:.32s; } .reveal-delay-5 { transition-delay:.40s; }

@keyframes pulse { 50% { box-shadow:0 0 20px rgba(217,255,101,.8); opacity:.65; } }
@keyframes spin { to { transform:rotate(360deg); } }
@keyframes shimmer { 100% { transform:translateX(100%); } }
@keyframes float { 50% { transform:translateY(-10px); } }
@keyframes travel { 0% { left:10%; } 50% { left:85%; } 100% { left:10%; } }
@keyframes drift { 50% { transform:translate(70px,30px); } }
@keyframes fadeIn { from{opacity:0} to{opacity:1} }
@keyframes modalIn { from{opacity:0; transform:translateY(20px) scale(.98)} to{opacity:1; transform:none} }

@media (max-width: 850px) {
  .nav-links { display:none; }
  .hero { padding-top:75px; }
  .hero-title { font-size:clamp(48px,12vw,76px); }
  .window-content { grid-template-columns:1fr; }
  .format-panel { display:none; }
  .hero-stage { height:340px; }
  .section-heading, .faq-section { grid-template-columns:1fr; gap:18px; }
  .section-heading > p { justify-self:start; }
  .feature-grid { grid-template-columns:1fr; }
  .feature-card.wide { grid-column:auto; }
  .steps { grid-template-columns:1fr; }
  .faq-section { gap:50px; }
}
@media (max-width: 620px) {
  .container { width:min(100% - 24px, 1160px); }
  .nav { height:64px; }
  .nav-cta { padding:8px 11px; }
  .hero { padding-top:58px; }
  .hero-copy { font-size:14px; }
  .download-console { border-radius:20px; }
  .url-row { flex-direction:column; }
  .analyze-btn { height:52px; }
  .platform-row { flex-direction:column; }
  .hero-stage { margin-top:60px; height:260px; }
  .product-window { width:95%; }
  .window-content { padding:10px; }
  .preview-image { height:125px; }
  .chip-one { left:0; top:8%; } .chip-two { right:0; bottom:10%; }
  .proof-strip { grid-template-columns:1fr; }
  .proof-strip > div { border-right:0; border-bottom:1px solid var(--line); }
  .section { padding-top:100px; }
  .feature-card { min-height:360px; }
  .feature-card.wide { display:block; }
  .motion-demo { width:100%; margin-top:25px; }
  .how-section { margin-top:100px; padding:90px 0; }
  .faq-section { padding:100px 0; }
  .result-layout { grid-template-columns:1fr; }
  .modal-card { padding:22px; }
  .footer { flex-direction:column; align-items:flex-start; gap:30px; }
  .footer-meta { text-align:left; }
  .console-foot { flex-wrap:wrap; }
  .platform-hint { margin-left:0; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior:auto !important; animation-duration:.01ms !important; animation-iteration-count:1 !important; transition-duration:.01ms !important; }
}

/* =========================================================
   Drift v4 — live download center
   ========================================================= */
[hidden] { display: none !important; }

.status-light.active {
  box-shadow: 0 0 18px rgba(217,255,101,.8);
  animation: pulse 1.4s ease-in-out infinite;
}

.hero-preview-badge {
  position: absolute;
  left: 12px;
  top: 12px;
  z-index: 2;
  padding: 5px 8px;
  border: 1px solid rgba(255,255,255,.12);
  border-radius: 999px;
  background: rgba(8,8,9,.55);
  color: var(--muted);
  backdrop-filter: blur(10px);
  font-size: 7px;
  letter-spacing: .12em;
}

.downloads-section {
  margin-top: 80px;
  padding: 34px 0 0;
  scroll-margin-top: 90px;
  transition: opacity .45s ease, transform .45s ease;
}
.downloads-section:not(.downloads-visible) { opacity: 0; transform: translateY(18px); }
.downloads-visible { opacity: 1; transform: none; }
.downloads-heading {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 40px;
  padding-bottom: 22px;
  border-bottom: 1px solid var(--line);
}
.downloads-heading h2 {
  margin: 12px 0 0;
  font: 700 clamp(34px,4vw,52px)/.98 var(--font-display);
  letter-spacing: -.065em;
}
.downloads-heading h2 span { color: var(--muted); }
.downloads-heading p { max-width: 310px; margin: 0; color: var(--muted); font-size: 12px; line-height: 1.65; }
.download-list { display: grid; gap: 12px; margin-top: 14px; }
.download-card {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 22px;
  background: linear-gradient(180deg, rgba(255,255,255,.045), rgba(255,255,255,.018));
  box-shadow: 0 25px 70px rgba(0,0,0,.18);
  animation: downloadCardIn .55s cubic-bezier(.2,.8,.2,1) both;
}
.download-card::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 2px;
  background: var(--accent);
  opacity: .75;
}
.download-card-main { display: grid; grid-template-columns: 210px 1fr; gap: 22px; padding: 16px; }
.download-thumb {
  aspect-ratio: 16 / 10;
  overflow: hidden;
  position: relative;
  border: 1px solid var(--line-soft);
  border-radius: 14px;
  background: linear-gradient(135deg,#25251f,#121412 55%,#313821);
}
.download-thumb img { width: 100%; height: 100%; display: block; object-fit: cover; }
.download-thumb-fallback { width: 100%; height: 100%; display: grid; place-items: center; }
.download-thumb-fallback span {
  width: 46px; height: 46px; border-radius: 50%; display: grid; place-items: center;
  background: var(--accent); color: #111; font-size: 11px; padding-left: 2px;
}
.download-card-info { min-width: 0; padding: 2px 4px 2px 0; }
.download-card-topline { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.download-platform { color: var(--muted-2); font-size: 8px; letter-spacing: .14em; }
.download-state-badge { border: 1px solid var(--line); border-radius: 999px; padding: 5px 8px; color: var(--muted); font-size: 8px; white-space: nowrap; }
.download-state-badge.state-downloading,
.download-state-badge.state-processing,
.download-state-badge.state-queued { color: var(--accent); border-color: rgba(217,255,101,.24); background: rgba(217,255,101,.045); }
.download-state-badge.state-ready { color: var(--accent); border-color: rgba(217,255,101,.28); }
.download-state-badge.state-failed,
.download-state-badge.state-cancelled { color: var(--danger); border-color: rgba(255,107,107,.24); }
.download-card h3 { margin: 10px 0 4px; font: 650 20px var(--font-display); letter-spacing: -.035em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.download-meta { margin:0; color:var(--muted); font-size:10px; }
.progress-wrap { margin-top: 18px; }
.progress-head, .progress-foot { display:flex; justify-content:space-between; gap:12px; align-items:center; }
.progress-head { color:var(--muted); font-size:9px; }
.progress-head strong { color:var(--text); font-size:10px; }
.progress-track { height: 7px; margin-top: 8px; overflow:hidden; border-radius:999px; background:rgba(255,255,255,.07); position:relative; }
.progress-track.indeterminate::after {
  content:""; position:absolute; inset:0; width:38%; background:linear-gradient(90deg,transparent,var(--accent),transparent);
  animation: progressSweep 1.15s ease-in-out infinite;
}
.progress-bar { display:block; height:100%; width:0; border-radius:inherit; background:linear-gradient(90deg,var(--accent-2),var(--accent)); box-shadow:0 0 16px rgba(217,255,101,.18); transition:width .45s cubic-bezier(.2,.8,.2,1); }
.progress-foot { margin-top:7px; color:var(--muted-2); font-size:8px; }
.download-card-actions { display:flex; gap:8px; margin-top:14px; }
.primary-action, .secondary-action { border-radius:9px; padding:9px 12px; font-size:9px; cursor:pointer; transition:.2s; }
.primary-action { border:1px solid rgba(217,255,101,.25); background:var(--accent); color:#0c0c09; font-weight:700; }
.primary-action:hover { transform:translateY(-1px); box-shadow:0 8px 22px rgba(217,255,101,.1); }
.secondary-action { border:1px solid var(--line); background:transparent; color:var(--muted); }
.secondary-action:hover { color:var(--text); background:var(--surface-hover); }
.primary-action:disabled, .secondary-action:disabled { opacity:.6; cursor:wait; transform:none; }
.download-error { margin:10px 0 0; color:#ff8585; font-size:9px; line-height:1.55; }
.inline-spinner { display:inline-block; animation:spin 1s linear infinite; }

@keyframes downloadCardIn { from { opacity:0; transform:translateY(18px) scale(.985); } to { opacity:1; transform:none; } }
@keyframes progressSweep { from { transform:translateX(-120%); } to { transform:translateX(300%); } }

@media (max-width: 700px) {
  .downloads-heading { grid-template-columns:1fr; gap:14px; }
  .download-card-main { grid-template-columns:1fr; gap:14px; }
  .download-thumb { width:100%; }
  .download-card h3 { white-space:normal; }
}
""",
    'js/app.js': r"""const API_BASE = window.API_BASE || "";
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
""",
}


def now() -> float:
    return time.time()


@contextmanager
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with DB_LOCK, db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress REAL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                speed REAL,
                eta REAL,
                url TEXT NOT NULL,
                quality TEXT NOT NULL,
                format TEXT NOT NULL,
                platform TEXT NOT NULL,
                title TEXT,
                thumbnail TEXT,
                filename TEXT,
                file_size INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                job_dir TEXT,
                file_path TEXT,
                duration TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        # Jobs cannot safely remain mid-network operation after a hard process restart.
        conn.execute("""
            UPDATE jobs
            SET status='failed', phase='Failed', error='Backend restarted while this download was running.',
                message='The backend restarted before this download completed.', updated_at=?
            WHERE status IN ('queued','downloading','processing')
        """, (now(),))
        conn.commit()


def row_to_job(row: sqlite3.Row) -> dict:
    job = dict(row)
    return job


def get_job(job_id: str) -> dict | None:
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def update_job(job_id: str, **updates) -> None:
    if not updates:
        return
    updates["updated_at"] = now()
    columns = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]
    with DB_LOCK, db() as conn:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", values)
        conn.commit()


def mark_job_ready(job_id: str, **updates) -> bool:
    updates["status"] = "ready"
    updates["updated_at"] = now()
    columns = ", ".join(f"{column}=?" for column in updates)
    values = list(updates.values()) + [job_id]
    with DB_LOCK:
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            return False
        with db() as conn:
            cursor = conn.execute(
                f"UPDATE jobs SET {columns} WHERE id=? AND status IN ('queued','downloading','processing')",
                values,
            )
            conn.commit()
    return cursor.rowcount == 1


def insert_job(job: dict) -> None:
    columns = list(job.keys())
    placeholders = ",".join("?" for _ in columns)
    with DB_LOCK, db() as conn:
        conn.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({placeholders})", [job[c] for c in columns])
        conn.commit()


def validate_public_url(raw_url: str):
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > MAX_URL_LENGTH:
        return False, "Invalid URL."
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
            return False, "Only HTTP(S) URLs are supported."
        host = parsed.hostname.lower().rstrip(".")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "Private or internal addresses are not allowed."
        except ValueError:
            pass
        if host == "localhost" or host.endswith(".localhost"):
            return False, "Local addresses are not allowed."
        if not any(host == allowed or host.endswith("." + allowed) for allowed in KNOWN_HOSTS):
            return False, "This source is not supported. Use a public video link from YouTube, Instagram, TikTok, Facebook, X, Reddit, Pinterest, Vimeo, etc."
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return False, "Private or internal addresses are not allowed."
        except socket.gaierror:
            return False, "The hostname could not be resolved."
        return True, parsed
    except (ValueError, UnicodeError):
        return False, "Invalid URL."


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "instagram." in host: return "Instagram"
    if "tiktok." in host: return "TikTok"
    if "youtube." in host or host == "youtu.be" or host.endswith(".youtu.be"): return "YouTube"
    if "facebook." in host or host == "fb.watch" or host.endswith(".fb.watch"): return "Facebook"
    if host == "x.com" or host.endswith(".x.com") or "twitter." in host: return "X"
    if "threads." in host: return "Threads"
    if "reddit." in host or host == "redd.it": return "Reddit"
    if "rumble." in host: return "Rumble"
    if "vimeo." in host: return "Vimeo"
    if "pinterest." in host or host == "pin.it": return "Pinterest"
    if "dailymotion." in host or host == "dai.ly": return "Dailymotion"
    if "twitch." in host: return "Twitch"
    if "snapchat." in host: return "Snapchat"
    if "linkedin." in host: return "LinkedIn"
    if "tumblr." in host: return "Tumblr"
    return "Public media"


def normalize_quality(quality: str) -> str:
    """Return 'best' or a '<height>p' string between 144p and 2160p."""
    quality = (quality or "").strip().lower()
    m = re.fullmatch(r"(\d{3,4})p", quality)
    if m and 144 <= int(m.group(1)) <= 2160:
        return f"{int(m.group(1))}p"
    return "best"


def format_selector(quality: str) -> str:
    """Prefer H.264 video + AAC audio (plays everywhere); fall back to anything available."""
    quality = normalize_quality(quality)
    if quality == "best":
        return "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    height = int(quality[:-1])
    return (f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")


def clean_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ").strip()
    low = message.lower()
    if "certificate_verify_failed" in low or "unable to get local issuer" in low:
        return "TLS certificate verification failed. Update certifi and yt-dlp."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "FFmpeg is required for this media. Restart Drift so it can set FFmpeg up."
    if "requested format is not available" in low:
        return "That quality is not available for this source. Choose another quality and try again."
    if ("login" in low or "log in" in low or "sign in" in low or "cookies" in low
            or "rate-limit" in low or "empty media response" in low or "private" in low):
        return ("This site is asking for a login (or blocked the request). Public videos usually work; "
                "for others add a cookies.txt file next to drift.py (see the instructions).")
    if "unsupported url" in low:
        return "This URL is not supported by Drift."
    if "no video could be found" in low or "no video formats" in low:
        return "No downloadable video was found at this link."
    if "http error 403" in low or "http error 429" in low:
        return "The website refused the request (403/429). Wait a bit, update yt-dlp, or try again later."
    return message[:600] or "Unknown downloader error."


def client_key(handler: BaseHTTPRequestHandler) -> str:
    # Never trust X-Forwarded-For unless an explicit trusted-proxy setup is added.
    return handler.client_address[0] or "unknown"


def rate_limit(handler: BaseHTTPRequestHandler, bucket: str, limit: int, window_seconds: int) -> bool:
    key = f"{bucket}:{client_key(handler)}"
    cutoff = now() - window_seconds
    with RATE_LOCK:
        timestamps = [t for t in RATE_WINDOWS.get(key, []) if t > cutoff]
        if len(timestamps) >= limit:
            RATE_WINDOWS[key] = timestamps
            return False
        timestamps.append(now())
        RATE_WINDOWS[key] = timestamps
        return True


def safe_filename(name: str) -> str:
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", " ", name or "Drift video")
    name = re.sub(r"\s+", " ", name).strip(" .")[:160] or "Drift video"
    if name.lower().endswith(".mp4"):
        name = name[:-4].rstrip(" .") or "Drift video"
    return name + ".mp4"


def progress_hook(job_id: str):
    def hook(data: dict):
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            raise DownloadCancelled()
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise DownloadTooLarge("The download exceeded Drift's 500 MB limit.")
            progress = round(downloaded / total * 100, 1) if total else None
            update_job(job_id, status="downloading", phase="Downloading", progress=progress,
                       downloaded_bytes=downloaded, total_bytes=total,
                       speed=data.get("speed"), eta=data.get("eta"), message="Downloading media…")
        elif status == "finished":
            update_job(job_id, downloaded_bytes=int(data.get("downloaded_bytes") or 0), speed=None, eta=None,
                       message="Stream received…")
    return hook


def postprocessor_hook(job_id: str):
    def hook(data: dict):
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():
            raise DownloadCancelled()
        if data.get("status") == "started":
            update_job(job_id, status="processing", phase="Processing", progress=100, speed=None, eta=None,
                       message="Download received. Finalizing the MP4…")
    return hook


class DownloadCancelled(Exception): pass
class DownloadTooLarge(Exception): pass


MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".3gp", ".ts"}


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Use the system FFmpeg if present, otherwise the one bundled with imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def find_media(job_dir: Path) -> Path | None:
    candidates = [p for p in job_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and ".part" not in p.name.lower()
                  and not re.search(r"\.f\d+\.", p.name)]
    if not candidates:  # merge failed: fall back to any media piece
        candidates = [p for p in job_dir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def probe_media(media: Path) -> dict:
    """Read codecs with `ffmpeg -i` (works without ffprobe)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available. Restart Drift so it can install it.")
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", str(media)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    info = {"container": "", "video": [], "audio": []}
    m = re.search(r"Input #0, ([^,]+(?:,[^,]+)*?), from", out)
    if m:
        info["container"] = m.group(1)
    for line in out.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and "attached pic" not in line:
            codec = re.search(r"Video: (\w+)", line)
            profile = re.search(r"Video: \w+ \(([^)]*)\)", line)
            pix = re.search(r"\b(yuvj?\d{3}p\w*|nv12|gbrp\w*|rgba?|bgra?|gray\w*|pal8)\b", line)
            dims = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
            info["video"].append({
                "codec": codec.group(1) if codec else "",
                "profile": profile.group(1) if profile else "",
                "pix_fmt": pix.group(1) if pix else "",
                "w": int(dims.group(1)) if dims else 0,
                "h": int(dims.group(2)) if dims else 0,
            })
        elif " Audio:" in line:
            codec = re.search(r"Audio: (\w+)", line)
            info["audio"].append(codec.group(1) if codec else "")
    return info


def ensure_compatible_mp4(media: Path, job_id: str) -> Path:
    """Return an MP4 (H.264 + AAC, yuv420p) that plays in Windows Media Player, phones, WhatsApp, everything.

    Files that are already compatible are left untouched (fast). Otherwise only what is needed is re-encoded.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not available. Restart Drift so it can install it.")
    info = probe_media(media)
    if not info["video"]:
        raise RuntimeError("The downloaded file has no video track.")
    v = info["video"][0]
    video_ok = (v["codec"] == "h264"
                and v["pix_fmt"] in {"yuv420p", "yuvj420p"}
                and v["profile"] in {"Baseline", "Constrained Baseline", "Main", "High"}
                and v["w"] % 2 == 0 and v["h"] % 2 == 0)
    audio_ok = all(c == "aac" for c in info["audio"])
    container_ok = "mp4" in info["container"].split(",")
    if video_ok and audio_ok and container_ok and media.suffix.lower() == ".mp4":
        return media

    output = tempfile.NamedTemporaryFile(prefix="drift-compatible-", suffix=".mp4", dir=media.parent, delete=False)
    output_path = Path(output.name)
    output.close()
    update_job(job_id, status="processing", phase="Processing", progress=100, speed=None, eta=None,
               message="Making the video playable everywhere (converting)…")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(media), "-map", "0:v:0", "-map", "0:a:0?"]
    if video_ok:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "21", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    if audio_ok:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(output_path)]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               encoding="utf-8", errors="replace")
    try:
        while True:
            try:
                _, error_output = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                event = CANCEL_EVENTS.get(job_id)
                if event and event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise DownloadCancelled()
        if process.returncode:
            raise RuntimeError(f"FFmpeg could not create a playable MP4: {(error_output or '')[-600:].strip()}")
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty file.")
        return output_path
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def ydl_common_options() -> dict:
    """Options shared by analyze + download (certificates, ffmpeg, optional cookies for login-only sites)."""
    opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20}
    if CERTIFI_CA:
        opts["ca_certs"] = CERTIFI_CA
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    cookie_file = os.getenv("DRIFT_COOKIES_FILE") or str(Path(__file__).resolve().parent / "cookies.txt")
    if Path(cookie_file).is_file():
        opts["cookiefile"] = cookie_file
    browser = os.getenv("DRIFT_COOKIES_BROWSER")
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def run_download(job_id: str, url: str, quality: str) -> None:
    job_dir = None
    try:
        early = CANCEL_EVENTS.get(job_id)
        if early and early.is_set():
            update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
            return
        job_dir = Path(tempfile.mkdtemp(prefix=f"drift-{job_id}-", dir=DOWNLOAD_ROOT))
        update_job(job_id, job_dir=str(job_dir), status="downloading", phase="Downloading", progress=0,
                   message="Starting media transfer…")
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed. Run: python -m pip install -r requirements.txt")
        options = ydl_common_options()
        options |= {
            "format": format_selector(quality), "merge_output_format": "mp4",
            "outtmpl": str(job_dir / "%(title).180s.%(ext)s"),
            "restrictfilenames": True, "windowsfilenames": True,
            "max_filesize": MAX_DOWNLOAD_BYTES, "progress_hooks": [progress_hook(job_id)],
            "postprocessor_hooks": [postprocessor_hook(job_id)],
            "continuedl": True, "retries": 5, "fragment_retries": 5,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        media = find_media(job_dir)
        if media is None: raise RuntimeError("The media file was not created.")
        media = ensure_compatible_mp4(media, job_id)
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set(): raise DownloadCancelled()
        size = media.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: raise DownloadTooLarge("The final media file exceeded Drift's 500 MB limit.")
        title = info.get("title") or "Drift video"
        duration = info.get("duration_string")
        final_name = safe_filename(title)
        final_path = job_dir / final_name
        if media.resolve() != final_path.resolve():
            media.replace(final_path)
        if not mark_job_ready(job_id, phase="Ready", progress=100, title=title,
                              duration=duration, filename=final_name, file_path=str(final_path),
                              file_size=size, message="Your MP4 is ready to save.", completed_at=now()):
            shutil.rmtree(job_dir, ignore_errors=True)
    except DownloadCancelled:
        update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    except DownloadTooLarge as exc:
        update_job(job_id, status="failed", phase="Failed", error=str(exc), message=str(exc))
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as exc:
        event = CANCEL_EVENTS.get(job_id)
        if event and event.is_set():  # downloader wrapped our cancel exception
            update_job(job_id, status="cancelled", phase="Cancelled", message="Download cancelled.")
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
            return
        message = clean_error(exc)
        update_job(job_id, status="failed", phase="Failed", error=message, message=message)
        if job_dir is not None:
            shutil.rmtree(job_dir, ignore_errors=True)
    finally:
        CANCEL_EVENTS.pop(job_id, None)


def cleanup_once() -> None:
    cutoff = now() - JOB_TTL_SECONDS
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT id, job_dir, status, updated_at FROM jobs WHERE updated_at < ?", (cutoff,)).fetchall()
        for row in rows:
            if row[2] in {"queued", "downloading", "processing"}: continue
            conn.execute("DELETE FROM jobs WHERE id=?", (row[0],))
        conn.commit()
        tracked = {Path(r[0]).name for r in conn.execute(
            "SELECT job_dir FROM jobs WHERE job_dir IS NOT NULL").fetchall()}
    for directory in DOWNLOAD_ROOT.iterdir():
        try:
            if directory.name in tracked: continue
            if directory.is_dir() and directory.name.startswith("drift-") and directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError: pass


def cleanup_loop() -> None:
    while True:
        time.sleep(60)
        try: cleanup_once()
        except Exception: pass


def json_bytes(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Drift/2.1"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {fmt % args}")

    def send_json(self, data: dict, status=200, headers=None):
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_cors()
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        origin = self.headers.get("Origin")
        allowed = os.getenv("DRIFT_ALLOWED_ORIGIN", "*")
        if allowed == "*" or origin == allowed:
            self.send_header("Access-Control-Allow-Origin", allowed if allowed != "*" else "*")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", os.getenv("DRIFT_ALLOWED_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024: raise ValueError("Request body is too large.")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict): raise ValueError("JSON body must be an object.")
        return data

    def serve_static(self, path: str):
        requested = unquote(path or "/").lstrip("/") or "index.html"
        text = FRONTEND_FILES.get(requested)
        if text is None:
            return self.send_json({"success": False, "message": "Not found."}, 404)
        data = text.encode("utf-8")
        content_type = (mimetypes.guess_type(requested)[0] or "application/octet-stream")
        if content_type.startswith("text/") or content_type in {"application/javascript", "text/javascript"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            text = FRONTEND_FILES.get(unquote(path or "/").lstrip("/") or "index.html")
            if text is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "text/html")
            self.send_header("Content-Length", str(len(text.encode("utf-8"))))
            self.end_headers()
            return
        if path == "/api/health":
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); return
        self.send_response(404); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            with DB_LOCK, db() as conn:
                active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0]
            return self.send_json({"status":"ok","service":"drift-backend","downloader":yt_dlp is not None,
                                   "ffmpeg":find_ffmpeg() is not None,"active_jobs":active,
                                   "max_active_jobs":MAX_ACTIVE_JOBS})
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)", path)
        if m:
            job = get_job(m.group(1))
            return self.send_json({"success":True,"job":job} if job else {"success":False,"message":"Download job not found or expired."}, 200 if job else 404)
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/file", path)
        if m: return self.stream_file(m.group(1))
        if not path.startswith("/api/"):
            return self.serve_static(path)
        self.send_json({"success":False,"message":"Not found."}, 404)

    def stream_file(self, job_id):
        job = get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] != "ready" or not job.get("file_path"):
            return self.send_json({"success":False,"message":"The file is not ready yet."},409)
        path = Path(job["file_path"])
        if not path.is_file():
            update_job(job_id, status="failed", phase="Failed", error="The temporary file is no longer available.", message="The file is no longer available.")
            return self.send_json({"success":False,"message":"The temporary file is no longer available."},410)
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES: return self.send_json({"success":False,"message":"The file exceeds Drift's size limit."},413)
        range_header = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1): start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    length = int(m.group(2)); start = max(0, size-length); end = size-1
                if start > end or start >= size:
                    self.send_response(416); self.send_header("Content-Range", f"bytes */{size}"); self.end_headers(); return
                end = min(end, size-1); status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        filename = safe_filename(job.get("title") or "Drift video")
        # HTTP headers must be ASCII/latin-1: give an ASCII fallback name plus the full UTF-8 name.
        base = filename[:-4] if filename.lower().endswith(".mp4") else filename
        ascii_base = re.sub(r"[^A-Za-z0-9 ._()\-]+", "", base.encode("ascii", "ignore").decode("ascii")).strip(" .")
        ascii_name = (ascii_base or "drift-video") + ".mp4"
        self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}")
        self.send_header("Cache-Control", "no-store")
        self.send_cors(); self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start); remaining = length
                while remaining:
                    chunk = f.read(min(1024*1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk); remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # browser closed the connection (e.g. user cancelled) - not an error

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/analyze": return self.analyze()
        if path == "/api/downloads": return self.create_download()
        m = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/cancel", path)
        if m: return self.cancel(m.group(1))
        if path == "/api/download": return self.send_json({"success":False,"message":"This endpoint was replaced by the live download job API. Refresh the Drift frontend."},410)
        self.send_json({"success":False,"message":"Not found."},404)

    def analyze(self):
        if not rate_limit(self,"analyze",20,60): return self.send_json({"success":False,"message":"Too many analysis requests. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        try:
            opts=ydl_common_options(); opts["skip_download"]=True
            with yt_dlp.YoutubeDL(opts) as ydl: info=ydl.extract_info(url,download=False)
            heights=sorted({int(f["height"]) for f in info.get("formats",[]) if f.get("height") and f.get("vcodec") not in (None,"none")},reverse=True)
            heights=[h for h in heights if 144 <= h <= 2160]
            formats=[{"quality":f"{h}p","height":h} for h in heights[:8]]
            formats.insert(0,{"quality":"Best available","height":None})
            return self.send_json({"success":True,"platform":detect_platform(url),"title":info.get("title") or "Public video",
                                   "duration":info.get("duration_string"),"thumbnail":info.get("thumbnail"),"formats":formats,
                                   "ffmpeg":find_ffmpeg() is not None})
        except Exception as exc:
            return self.send_json({"success":False,"message":clean_error(exc)},422)

    def create_download(self):
        if not rate_limit(self,"download",5,60): return self.send_json({"success":False,"message":"Download limit reached. Please wait a moment."},429)
        if yt_dlp is None: return self.send_json({"success":False,"message":"yt-dlp is not installed. Run: python -m pip install -r requirements.txt"},500)
        try: body=self.read_json()
        except Exception as exc: return self.send_json({"success":False,"message":str(exc)},400)
        url=str(body.get("url","")).strip(); quality=str(body.get("quality","best"))
        valid, result=validate_public_url(url)
        if not valid: return self.send_json({"success":False,"message":result},400)
        quality=normalize_quality(quality)
        with DB_LOCK, db() as conn:
            active=conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0]
        if active >= MAX_ACTIVE_JOBS + 4:
            return self.send_json({"success":False,"message":"The download queue is full. Please wait for an active download to finish."},429)
        job_id=uuid.uuid4().hex
        event=threading.Event(); CANCEL_EVENTS[job_id]=event
        t=now()
        job={"id":job_id,"status":"queued","phase":"Queued","progress":0,"downloaded_bytes":0,"total_bytes":0,
             "speed":None,"eta":None,"url":url,"quality":quality,"format":"MP4","platform":detect_platform(url),
             "title":str(body.get("title") or "Preparing media…")[:300],"thumbnail":str(body.get("thumbnail") or "")[:2000],
             "filename":None,"file_size":0,"message":"Your download is queued.","error":None,"created_at":t,"updated_at":t,
             "completed_at":None,"job_dir":None,"file_path":None,"duration":str(body.get("duration") or "")[:100]}
        insert_job(job)
        EXECUTOR.submit(run_download,job_id,url,quality)
        return self.send_json({"success":True,"job":job},202)

    def cancel(self, job_id):
        job=get_job(job_id)
        if not job: return self.send_json({"success":False,"message":"Download job not found or expired."},404)
        if job["status"] in {"ready","failed","cancelled"}: return self.send_json({"success":True,"job":job})
        event=CANCEL_EVENTS.setdefault(job_id,threading.Event()); event.set()
        with DB_LOCK, db() as conn:
            conn.execute(
                """UPDATE jobs SET status='cancelled', phase='Cancelled', message='Cancelling download…', updated_at=?
                   WHERE id=? AND status IN ('queued','downloading','processing')""",
                (now(), job_id),
            )
            conn.commit()
        return self.send_json({"success":True,"job":get_job(job_id)})


def main():
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True, name="drift-cleanup").start()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    print(f"Drift backend listening on http://{HOST}:{PORT}")
    print(f"FFmpeg: {find_ffmpeg() or 'NOT FOUND'} | yt-dlp: {'ok' if yt_dlp else 'MISSING'}", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        EXECUTOR.shutdown(wait=False,cancel_futures=True); server.server_close()


if __name__ == "__main__": main()
