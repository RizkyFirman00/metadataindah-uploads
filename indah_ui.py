from __future__ import annotations

import argparse
import cgi
import html
import json
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from indah_automation.shared import BASE_API_URL, DEFAULT_RUN_STATE_DIR, IndahError, PROJECT_ROOT, clean, load_auth
from indah_automation.spreadsheet_io import (
    download_spreadsheet_url,
    prepare_phase_sources,
    safe_filename,
)


UPLOAD_ROOT = PROJECT_ROOT / "ui_uploads"
UI_RUNS_ROOT = PROJECT_ROOT / "ui_runs"
UI_SESSION_ROOT = PROJECT_ROOT / ".indah_session" / "ui_sessions"
SESSION_COOKIE = "indah_ui_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-labelledby="icon-title icon-desc">
  <title id="icon-title">INDAH Metadata Upload</title>
  <desc id="icon-desc">Spreadsheet dengan panah upload</desc>
  <rect width="64" height="64" rx="12" fill="#1e5aa8"/>
  <path d="M15 13h25l9 9v29H15z" fill="#ffffff"/>
  <path d="M40 13v10h9" fill="#dceafa"/>
  <path d="M21 29h22M21 36h22M21 43h13" fill="none" stroke="#91a4b7" stroke-width="3" stroke-linecap="round"/>
  <circle cx="44" cy="44" r="13" fill="#0d7c66"/>
  <path d="M44 51V37m-6 6 6-6 6 6" fill="none" stroke="#ffffff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

PHASES = {
    "kegiatan": {
        "number": "1",
        "title": "MS-Kegiatan",
        "script": "indah_kegiatan.py",
        "required": ["ms_kegiatan"],
        "optional": ["ms_kegiatan_variabel_dikumpulkan", "ms_kegiatan_wilayah"],
    },
    "variabel": {
        "number": "2",
        "title": "MS-Variabel",
        "script": "indah_variabel.py",
        "required": ["ms_variabel"],
        "optional": ["ms_variabel_value_domain"],
    },
    "indikator": {
        "number": "3",
        "title": "MS-Indikator",
        "script": "indah_indikator.py",
        "required": ["ms_indikator"],
        "optional": ["ms_indikator_pembangun", "ms_indikator_variabel_pembangun"],
    },
}


CSS = (Path(__file__).resolve().parent / "indah_ui.css").read_text(encoding="utf-8")

class IndahUIHandler(BaseHTTPRequestHandler):
    server_version = "IndahUI/1.0"

    def do_GET(self) -> None:
        session_id = self.user_session_id()
        state_path = user_state_path(session_id)
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.svg":
            self.respond_svg(ICON_SVG)
            return
        if parsed.path == "/":
            phase = (parse_qs(parsed.query).get("phase") or ["kegiatan"])[0]
            self.respond_html(render_home(state_path, phase))
            return
        if parsed.path == "/login":
            self.respond_html(render_login_form(state_path))
            return
        if parsed.path == "/run":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [""])[0]
            self.respond_html(render_saved_run(run_id))
            return
        if parsed.path == "/run-status":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [""])[0]
            self.respond_json(run_status(run_id))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        session_id = self.user_session_id()
        state_path = user_state_path(session_id)
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.respond_html(handle_web_login(self, state_path))
            return
        if parsed.path.startswith("/phase/"):
            phase = parsed.path.rsplit("/", 1)[-1]
            if phase not in PHASES:
                self.send_error(404)
                return
            self.respond_html(handle_phase_run(phase, self, state_path))
            return
        if parsed.path.startswith("/cleanup/"):
            target = parsed.path.rsplit("/", 1)[-1]
            if target not in {"variabel", "indikator", "all"}:
                self.send_error(404)
                return
            self.respond_html(handle_cleanup_run(target, self, state_path))
            return
        self.send_error(404)

    def respond_html(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if getattr(self, "_new_session_cookie", None):
            self.send_header("Set-Cookie", self._new_session_cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def respond_json(self, data: Dict[str, object], status: int = 200) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if getattr(self, "_new_session_cookie", None):
            self.send_header("Set-Cookie", self._new_session_cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def respond_svg(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def user_session_id(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        session_id = morsel.value if morsel else ""
        if len(session_id) == 64 and all(char in "0123456789abcdef" for char in session_id):
            return session_id

        session_id = secrets.token_hex(32)
        self._new_session_cookie = (
            f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
        )
        return session_id

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>{escape(title)}</title>
  <style>{CSS}</style>
  <script>
    window.addEventListener("DOMContentLoaded", function () {{
      var overlay = document.querySelector(".loading-overlay");
      var title = document.querySelector(".loading-title");
      var subtitle = document.querySelector(".loading-subtitle");
      var progressBars = document.querySelectorAll(".progress-bar");
      var progressOutputs = document.querySelectorAll(".progress-output");
      var captchaCanvas = document.querySelector("#login-captcha-canvas");
      var captchaCode = "";
      var passwordInput = document.querySelector("#login-password");
      var passwordToggle = document.querySelector("#toggle-password");
      if (passwordInput && passwordToggle) {{
        passwordToggle.addEventListener("click", function () {{
          var show = passwordInput.type === "password";
          passwordInput.type = show ? "text" : "password";
          passwordToggle.querySelector(".eye-open").hidden = show;
          passwordToggle.querySelector(".eye-closed").hidden = !show;
          passwordToggle.setAttribute("aria-pressed", String(show));
          passwordToggle.setAttribute("aria-label", show ? "Sembunyikan password" : "Lihat password");
          passwordToggle.title = show ? "Sembunyikan password" : "Lihat password";
        }});
      }}

      document.querySelectorAll('form[action^="/phase/"]').forEach(function (form) {{
        function selectSource() {{
          var selected = form.querySelector('input[name="source_mode"]:checked');
          form.querySelectorAll("[data-source]").forEach(function (panel) {{
            var active = selected && panel.getAttribute("data-source") === selected.value;
            panel.hidden = !active;
            panel.querySelectorAll("input").forEach(function (input) {{ input.disabled = !active; }});
          }});
          form.querySelector(".source-error").textContent = "";
        }}
        form.querySelectorAll('input[name="source_mode"]').forEach(function (radio) {{
          radio.addEventListener("change", selectSource);
        }});
        selectSource();
      }});

      function randomIndex(max) {{
        if (window.crypto && window.crypto.getRandomValues) {{
          var value = new Uint32Array(1);
          window.crypto.getRandomValues(value);
          return value[0] % max;
        }}
        return Math.floor(Math.random() * max);
      }}

      function createCaptcha() {{
        if (!captchaCanvas) return;
        var characters = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ@!#$%^&*";
        var code = [];
        while (code.length < 7) {{
          var character = characters[randomIndex(characters.length)];
          if (code.indexOf(character) === -1) code.push(character);
        }}
        captchaCode = code.join("");

        var context = captchaCanvas.getContext("2d");
        context.clearRect(0, 0, captchaCanvas.width, captchaCanvas.height);
        context.fillStyle = "#f8fafc";
        context.fillRect(0, 0, captchaCanvas.width, captchaCanvas.height);
        for (var line = 0; line < 6; line += 1) {{
          context.strokeStyle = ["#94a3b8", "#cbd5e1", "#7dd3fc"][randomIndex(3)];
          context.beginPath();
          context.moveTo(randomIndex(captchaCanvas.width), randomIndex(captchaCanvas.height));
          context.lineTo(randomIndex(captchaCanvas.width), randomIndex(captchaCanvas.height));
          context.stroke();
        }}
        context.font = "30px Georgia";
        context.textAlign = "center";
        context.textBaseline = "middle";
        for (var index = 0; index < code.length; index += 1) {{
          context.save();
          context.translate(26 + index * 21, 25);
          context.rotate((randomIndex(17) - 8) * Math.PI / 180);
          context.fillStyle = ["#b42318", "#0d7c66", "#1e5aa8", "#8a5b00"][randomIndex(4)];
          context.fillText(code[index], 0, 0);
          context.restore();
        }}
        var captchaInput = document.querySelector("#login-captcha-input");
        if (captchaInput) captchaInput.value = "";
      }}

      if (captchaCanvas) {{
        captchaCanvas.addEventListener("click", createCaptcha);
        var captchaRefresh = document.querySelector("#refresh-captcha");
        if (captchaRefresh) captchaRefresh.addEventListener("click", createCaptcha);
        createCaptcha();
      }}

      function updateProgress(runId) {{
        if (!runId || !subtitle) return;
        fetch("/run-status?id=" + encodeURIComponent(runId), {{cache: "no-store"}})
          .then(function (response) {{ return response.json(); }})
          .then(function (data) {{
            progressBars.forEach(function (bar) {{ bar.style.width = String(data.percent || 0) + "%"; }});
            subtitle.textContent = data.last_line || "Menyiapkan proses upload...";
            progressOutputs.forEach(function (output) {{
              output.textContent = data.output || "Menyiapkan proses upload...";
              output.scrollTop = output.scrollHeight;
            }});
          }})
          .catch(function () {{
            subtitle.textContent = "Menunggu respons proses...";
          }});
      }}

      function startProgressPolling(runId) {{
        if (!runId) return;
        updateProgress(runId);
        window.setInterval(function () {{ updateProgress(runId); }}, 1000);
      }}

      startProgressPolling(document.body.getAttribute("data-progress-run-id"));

      document.addEventListener("submit", function (event) {{
        var form = event.target;
        if (!form || !form.matches("form")) return;
        if (form.matches('form[action^="/phase/"]')) {{
          var panel = form.querySelector('[data-source]:not([hidden])');
          var requiredFile = panel && panel.querySelector('input[type="file"]');
          var url = panel && panel.querySelector('input[type="url"]');
          if ((requiredFile && !requiredFile.files.length) || (url && !url.value.trim())) {{
            event.preventDefault();
            form.querySelector(".source-error").textContent = "Pilih file atau masukkan link sesuai cara yang dipilih terlebih dahulu.";
            (requiredFile || url).focus();
            return;
          }}
        }}
        if (form.id === "indah-login-form") {{
          var captchaInput = document.querySelector("#login-captcha-input");
          var captchaError = document.querySelector("#login-captcha-error");
          if (!captchaInput || captchaInput.value !== captchaCode) {{
            event.preventDefault();
            if (captchaError) captchaError.textContent = "Kode captcha tidak sesuai.";
            createCaptcha();
            if (captchaInput) captchaInput.focus();
            return;
          }}
          if (captchaError) captchaError.textContent = "";
        }}
        var confirmText = event.submitter && event.submitter.getAttribute("data-confirm")
          ? event.submitter.getAttribute("data-confirm")
          : form.getAttribute("data-confirm");
        if (confirmText && !window.confirm(confirmText)) {{
          event.preventDefault();
          return;
        }}
        var label = form.getAttribute("data-loading-label") || "Memproses";
        if (event.submitter && event.submitter.getAttribute("data-loading-label")) {{
          label = event.submitter.getAttribute("data-loading-label");
        }}
        if (event.submitter) {{
          var buttonAction = event.submitter.getAttribute("formaction");
          if (buttonAction) {{
            form.action = buttonAction;
          }}
          if (event.submitter.name) {{
            var clicked = document.createElement("input");
            clicked.type = "hidden";
            clicked.name = event.submitter.name;
            clicked.value = event.submitter.value || "1";
            form.appendChild(clicked);
          }}
        }}
        if (title) title.textContent = label;
        if (subtitle) subtitle.textContent = "Menyiapkan upload...";
        progressBars.forEach(function (bar) {{ bar.style.width = "5%"; }});
        progressOutputs.forEach(function (output) {{ output.textContent = ""; }});
        if (overlay) overlay.classList.add("is-visible");
        document.querySelectorAll("button").forEach(function (button) {{
          button.disabled = true;
        }});
      }});
    }});
  </script>
</head>
<body>
  <div class="loading-overlay" aria-live="polite" aria-busy="true">
    <div class="loading-box">
      <div class="spinner"></div>
      <div class="loading-title">Memproses</div>
      <div class="progress-track"><div class="progress-bar"></div></div>
      <div class="loading-subtitle">Mohon tunggu sampai hasilnya muncul.</div>
      <div class="progress-output" aria-live="polite">Menyiapkan proses upload...</div>
    </div>
  </div>
  <header>
    <div class="wrap topline">
      <a class="brand" href="/" aria-label="INDAH Upload Metadata, beranda">
        <img src="/favicon.svg" alt="" width="38" height="38">
        <span><span class="brand-title">INDAH</span><span class="brand-caption">Upload Metadata Statistik</span></span>
      </a>
      <a class="button secondary" href="/login">Login INDAH</a>
    </div>
  </header>
  <main>
    <div class="wrap">
      {body}
    </div>
  </main>
</body>
</html>"""


def render_home(state_path: Path, phase: str = "kegiatan") -> str:
    if phase not in PHASES:
        phase = "kegiatan"
    descriptions = {"kegiatan": "Informasi kegiatan statistik", "variabel": "Data yang dikumpulkan", "indikator": "Ukuran hasil kegiatan"}
    navigation = []
    for slug, config in PHASES.items():
        current = ' aria-current="step"' if slug == phase else ""
        navigation.append(f'<a class="phase-link" href="/?phase={slug}"{current}><span class="step-number">{config["number"]}</span><span><strong>{config["title"].replace("MS-", "")}</strong><small>{descriptions[slug]}</small></span></a>')
    links = "".join(navigation)
    body = f"""
<div class="intro">
  <h1>Upload metadata statistik</h1>
  <p>Mulai dari Kegiatan, lalu lengkapi Variabel dan Indikator. Gunakan file sesuai template dan pilih tahun laporan.</p>
</div>
<div class="workspace">
  <aside class="workflow">
    <h2>Jenis metadata</h2>
    <nav class="phase-nav" aria-label="Pilih jenis metadata">{links}</nav>
    <p class="workflow-note">Baru mulai? Simpan kegiatan terlebih dahulu. Variabel dan indikator akan dihubungkan ke kegiatan tersebut.</p>
  </aside>
  <div class="workspace-content">
    <div class="session-note"><span>{session_status_text(state_path)}</span><a href="/login">Kelola login</a></div>
    {render_phase_form(phase, PHASES[phase])}
    <details class="cleanup-panel">
      <summary>Pengelolaan data: hapus variabel atau indikator</summary>
      <p class="help">Buka bagian ini hanya jika ingin menghapus data yang sudah diunggah.</p>
      {render_cleanup_panel()}
    </details>
  </div>
</div>
<div class="app-footer">INDAH Metadata Upload. Gunakan judul kegiatan yang sama pada seluruh sheet agar data dapat dihubungkan.</div>
"""
    return page("Upload Metadata INDAH", body)


def render_phase_form(phase: str, config: Dict[str, object]) -> str:
    required = config["required"]
    optional = config["optional"]
    labels = {
        "ms_kegiatan": "Informasi kegiatan",
        "ms_kegiatan_variabel_dikumpulkan": "Variabel yang dikumpulkan",
        "ms_kegiatan_wilayah": "Wilayah kegiatan",
        "ms_variabel": "Informasi variabel",
        "ms_variabel_value_domain": "Kode dan nilai variabel",
        "ms_indikator": "Informasi indikator",
        "ms_indikator_pembangun": "Indikator pembangun",
        "ms_indikator_variabel_pembangun": "Variabel pembangun",
    }
    rows = []
    for slug in list(required) + list(optional):
        kind = '<span class="req">Wajib</span>' if slug in required else '<span class="opt">Opsional</span>'
        rows.append(f"""<tr>
  <td><label for="file-{slug}">{labels[slug]}</label><small>Sheet: {slug}</small>{kind}</td>
  <td><input id="file-{slug}" type="file" name="file_{slug}" accept=".csv,.tsv,.xlsx,.xlsm"></td>
</tr>""")
    current_year = int(time.strftime("%Y"))
    year_options = "".join(
        f'<option value="{year}"{" selected" if year == current_year else ""}>{year}</option>'
        for year in range(current_year + 1, current_year - 6, -1)
    )
    phase_name = str(config["title"]).replace("MS-", "")
    explanation = "Tuliskan identitas, tujuan, dan pelaksanaan kegiatan statistik." if phase == "kegiatan" else "Pastikan kegiatan induk sudah tersimpan dengan judul dan periode tahun yang sama."
    return f"""
<section class="band">
  <div class="phase-head">
    <div>
      <div class="phase-number">Tahap {config["number"]} / {config["title"]}</div>
      <h2>Upload {phase_name.lower()}</h2>
      <p>{explanation}</p>
    </div>
  </div>
  <form action="/phase/{phase}" method="post" enctype="multipart/form-data" data-loading-label="Memeriksa isian {phase_name.lower()}">
    <section class="form-section">
      <h3>Masukkan sumber data</h3>
      <p>Pilih salah satu cara. File Excel dengan seluruh sheet adalah pilihan yang paling praktis.</p>
      <fieldset class="source-picker">
        <legend>Cara memasukkan data</legend>
        <div class="source-options">
          <label class="source-option"><input type="radio" name="source_mode" value="workbook" checked>File Excel gabungan</label>
          <label class="source-option"><input type="radio" name="source_mode" value="url">Link spreadsheet</label>
          <label class="source-option"><input type="radio" name="source_mode" value="files">File per sheet</label>
        </div>
      </fieldset>
      <div class="source-panel" data-source="workbook">
        <label class="field">
          <span>File Excel gabungan</span>
          <input type="file" name="workbook_file" accept=".xlsx,.xlsm">
          <small>Format .xlsx atau .xlsm. Nama sheet wajib: {", ".join(required)}.</small>
          <small>Sheet tambahan jika tersedia: {", ".join(optional)}.</small>
        </label>
      </div>
      <div class="source-panel" data-source="url">
        <label class="field">
          <span>Link spreadsheet</span>
          <input type="url" name="spreadsheet_url" placeholder="https://docs.google.com/spreadsheets/d/...">
          <small>Gunakan link yang dapat dibuka tanpa meminta akses. Nama sheet mengikuti template.</small>
        </label>
      </div>
      <div class="source-panel" data-source="files">
        <p class="help">Unggah file untuk setiap sheet. Format: CSV, TSV, XLSX, atau XLSM.</p>
        <table class="input-table">
          <thead><tr><th>Isian yang dibutuhkan</th><th>File</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
      </div>
      <div class="captcha-error source-error" role="alert"></div>
    </section>
    <section class="form-section">
      <h3>Tentukan periode laporan</h3>
      <label class="field year-field">
        <span>Periode Tahun <span class="req">Wajib</span></span>
        <select name="submission_period" required>{year_options}</select>
        <small>Pilih tahun tujuan pelaporan. Isian tahun di dalam file tetap mengikuti file Anda.</small>
      </label>
    </section>
    <details>
      <summary>Pengaturan tambahan (opsional)</summary>
      <div class="grid">
        <label class="field">
          <span>Hanya proses judul kegiatan tertentu</span>
          <input type="text" name="only_title" placeholder="Kosongkan untuk semua kegiatan">
          <small>Isi judul persis seperti yang tertulis pada sheet.</small>
        </label>
        <label class="field">
          <span>Batas jumlah data</span>
          <input type="number" name="limit" min="0" step="1" placeholder="Semua data">
          <small>Kosong atau 0 berarti semua data diproses.</small>
        </label>
      </div>
    </details>
    <div class="actions">
      <button type="submit" name="submit" value="1" data-loading-label="Menyimpan draft {phase_name.lower()}">Simpan sebagai draft</button>
      <button class="secondary" type="submit" name="final_submit" value="1" data-loading-label="Mengirim {phase_name.lower()} ke INDAH" data-confirm="Kirim untuk peninjauan di INDAH? Pastikan seluruh isian dan periode tahun sudah benar.">Kirim untuk peninjauan</button>
    </div>
    <p class="action-note">Draft memberi Anda kesempatan memeriksa isian di INDAH. Kirim untuk peninjauan jika isian sudah siap.</p>
  </form>
</section>
"""


def render_cleanup_panel() -> str:
    return """
<section class="band">
  <div class="phase-head">
    <div>
      <h3>Hapus variabel atau indikator</h3>
    </div>
    <div class="phase-number">Berdasarkan judul kegiatan</div>
  </div>
  <form action="/cleanup/all" method="post" data-loading-label="Menghapus phase 2 dan 3">
    <div class="grid">
      <label class="field">
        <span>Judul kegiatan</span>
        <input type="text" name="cleanup_title" placeholder="Kosongkan untuk pakai kegiatan_map.csv">
        <small>Semua variabel atau indikator dari kegiatan ini akan dihapus. Jika judul dan nomor kegiatan kosong, penghapusan berlaku untuk semua kegiatan yang tercatat di aplikasi.</small>
      </label>
      <label class="field">
        <span>Nomor kegiatan di INDAH</span>
        <input type="text" name="cleanup_ms_keg_id" placeholder="Opsional">
        <small>Isi jika ingin target ID kegiatan tertentu.</small>
      </label>
    </div>
    <div class="cleanup-actions" style="margin-top: 14px;">
      <button class="danger" type="submit" formaction="/cleanup/variabel" data-loading-label="Menghapus variabel" data-confirm="Hapus seluruh variabel dari kegiatan yang dipilih? Jika judul dan nomor kegiatan kosong, seluruh kegiatan yang tercatat akan diproses.">Hapus variabel</button>
      <button class="danger" type="submit" formaction="/cleanup/indikator" data-loading-label="Menghapus indikator" data-confirm="Hapus seluruh indikator dari kegiatan yang dipilih? Jika judul dan nomor kegiatan kosong, seluruh kegiatan yang tercatat akan diproses.">Hapus indikator</button>
      <button class="danger" type="submit" formaction="/cleanup/all" data-loading-label="Menghapus variabel dan indikator" data-confirm="Hapus seluruh variabel dan indikator dari kegiatan yang dipilih? Jika judul dan nomor kegiatan kosong, seluruh kegiatan yang tercatat akan diproses.">Hapus keduanya</button>
    </div>
  </form>
</section>
"""


def render_login_form(state_path: Path, error: str = "") -> str:
    status = session_status_text(state_path)
    error_html = f'<div class="login-error" role="alert">{escape(error)}</div>' if error else ""
    body = f"""
<section class="band login-card">
  <h2>Login ke INDAH</h2>
  <p class="help">Gunakan akun INDAH Anda untuk menyimpan metadata. Login berlaku untuk browser ini.</p>
  <div class="login-status">{status}</div>
  {error_html}
  <form id="indah-login-form" action="/login" method="post" data-loading-label="Login ke INDAH">
    <div class="grid">
      <label class="field">
        <span>Username INDAH</span>
        <input type="text" name="username" autocomplete="username" required>
      </label>
      <div class="field">
        <label for="login-password">Password INDAH</label>
        <div class="password-control">
          <input id="login-password" type="password" name="password" autocomplete="current-password" required>
          <button id="toggle-password" class="secondary icon-button" type="button" aria-controls="login-password" aria-pressed="false" aria-label="Lihat password" title="Lihat password">
            <span class="eye-open" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></span>
            <span class="eye-closed" aria-hidden="true" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path d="m3 3 18 18"/></svg></span>
          </button>
        </div>
        <small>Password hanya diteruskan ke API resmi INDAH dan tidak disimpan.</small>
      </div>
    </div>
    <div class="login-captcha">
      <span class="captcha-label">Kode verifikasi</span>
      <div class="captcha-preview">
        <canvas id="login-captcha-canvas" width="180" height="50" title="Kode verifikasi"></canvas>
        <button class="secondary icon-button" id="refresh-captcha" type="button" aria-label="Ganti kode captcha" title="Ganti kode captcha"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.5 7a7.5 7.5 0 0 1 12-1L20 9M4 15l2.5 3a7.5 7.5 0 0 0 12-1"/></svg></button>
      </div>
      <label class="field">
        <span>Masukkan kode pada gambar</span>
        <input id="login-captcha-input" type="text" name="captcha" autocomplete="off" autocapitalize="off" spellcheck="false" required>
        <span id="login-captcha-error" class="captcha-error" aria-live="polite"></span>
      </label>
    </div>
    <div class="login-actions">
      <button type="submit">Login</button>
      <a class="button secondary icon-button" href="/" aria-label="Kembali ke halaman upload" title="Kembali ke halaman upload"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 4-5 5 5 5M4 9h10a6 6 0 0 1 0 12h-3"/></svg></a>
    </div>
  </form>
</section>
"""
    return page("Login INDAH", body)


def handle_web_login(handler: BaseHTTPRequestHandler, state_path: Path) -> str:
    form = parse_multipart(handler)
    username = form_text(form, "username")
    password = form.getfirst("password", "")
    if not username or not password:
        return render_login_form(state_path, "Username dan password wajib diisi.")

    try:
        response = authenticate_indah(username, password)
        save_user_state(state_path, response)
    except IndahError as exc:
        return render_login_form(state_path, str(exc))

    user_info = response.get("userInfo") or {}
    display_name = user_info.get("name") or user_info.get("username") or username
    body = f"""
<section class="band result">
  <h2>Login berhasil</h2>
  <div class="meta-list">
    <div><span>Pengguna</span><span class="ok">{escape(display_name)}</span></div>
    <div><span>Session</span><span>Sesi ini hanya dipakai oleh browser Anda.</span></div>
  </div>
  <a class="button" href="/">Lanjut ke upload metadata</a>
</section>
"""
    return page("Login berhasil", body)


def authenticate_indah(username: str, password: str) -> Dict[str, object]:
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    request = Request(
        f"{BASE_API_URL}/api/authenticate",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise IndahError("Username atau password tidak sesuai.") from None
        raise IndahError(f"Login INDAH gagal (HTTP {exc.code}). Coba lagi beberapa saat.") from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise IndahError("API INDAH tidak dapat dihubungi atau memberi respons yang tidak valid.") from None

    if not isinstance(result, dict) or not result.get("token") or not isinstance(result.get("userInfo"), dict):
        raise IndahError("Respons login INDAH tidak memuat token atau data pengguna.")
    return result


def save_user_state(state_path: Path, response: Dict[str, object]) -> None:
    user_info = response.get("userInfo") or {}
    user = {
        "role": response.get("role", []),
        "id": response.get("id") if response.get("id") is not None else user_info.get("id"),
        "name": user_info.get("name"),
        "email": user_info.get("email"),
        "username": user_info.get("username"),
        "photoUrl": user_info.get("photoUrl") or "/img/error/user.svg",
        "organisasi": user_info.get("organisasi") or user_info.get("instansi"),
        "idOrganisasi": user_info.get("instansiID") or response.get("instansiID"),
        "idProvinsi": user_info.get("idProvinsi") or response.get("idProvinsi"),
        "provinsi": user_info.get("provinsi") or response.get("provinsi"),
        "idKabupatenKota": user_info.get("idKabupatenKota") or response.get("idKabupatenKota"),
        "kabupatenKota": user_info.get("kabupatenKota") or response.get("kabupatenKota"),
        "kdorganisasi": user_info.get("organisasi"),
        "eselon": user_info.get("eselon"),
        "slugOrganisasi": user_info.get("slugInstansi") or "dd3fc0aa-4579-4dd2-b10e-003a679c1e78",
        "unitKerja": user_info.get("unitKerja") or response.get("unitKerja"),
        "walidata": user_info.get("walidata") or response.get("walidata"),
        "sdiOrganization": response.get("sdiOrganization"),
        "isApproved": user_info.get("isApproved"),
    }
    vuex = {
        "auth": {"status": 1, "token": response["token"], "user": user},
        "mega_menu": {},
        "pmse": {"periode": {}, "dp": {}},
    }
    storage_state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://indah.bps.go.id",
                "localStorage": [{"name": "vuex", "value": json.dumps(vuex, ensure_ascii=False)}],
            }
        ],
    }

    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path.parent.chmod(0o700)
    temporary_path = state_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(storage_state, ensure_ascii=False), encoding="utf-8")
    temporary_path.chmod(0o600)
    temporary_path.replace(state_path)


def user_state_path(session_id: str) -> Path:
    return UI_SESSION_ROOT / f"{session_id}.json"


def session_status_text(state_path: Path) -> str:
    if not state_path.exists():
        return '<span class="warn">Belum login dari browser ini</span>'
    try:
        auth = load_auth(state_path)
        name = auth.user_payload.get("name") or auth.user_payload.get("username") or "Pengguna INDAH"
        return f'<span class="ok">Login sebagai {escape(name)}</span>'
    except (IndahError, OSError, ValueError, json.JSONDecodeError):
        return '<span class="bad">Session tidak valid, silakan login ulang</span>'


def handle_phase_run(phase: str, handler: BaseHTTPRequestHandler, state_path: Path) -> str:
    config = PHASES[phase]
    run_id = f"{phase}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = UI_RUNS_ROOT / run_id
    raw_dir = UPLOAD_ROOT / run_id / "raw"
    prepared_dir = UPLOAD_ROOT / run_id / "prepared"
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        form = parse_multipart(handler)
        source_mode = form_text(form, "source_mode")
        if source_mode not in {"", "workbook", "url", "files"}:
            raise IndahError("Pilih cara memasukkan data yang tersedia.")
        specific_files = save_specific_files(form, raw_dir, list(config["required"]) + list(config["optional"])) if source_mode in {"", "files"} else {}
        workbook_files = save_workbook_files(form, raw_dir) if source_mode in {"", "workbook"} else []
        url = form_text(form, "spreadsheet_url") if source_mode in {"", "url"} else ""
        if source_mode == "workbook" and not workbook_files:
            raise IndahError("File Excel belum dipilih. Pilih file Excel, lalu coba kembali.")
        if source_mode == "url" and not url:
            raise IndahError("Link spreadsheet belum diisi. Masukkan link, lalu coba kembali.")
        if url:
            workbook_files.append(download_spreadsheet_url(url, raw_dir))

        prepared = prepare_phase_sources(
            required_slugs=config["required"],
            optional_slugs=config["optional"],
            specific_files=specific_files,
            workbook_files=workbook_files,
            output_dir=prepared_dir,
        )
        command = build_phase_command(phase, config, prepared_dir, form, state_path)
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "command": command, "returncode": None, "prepared": prepared}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        thread = threading.Thread(
            target=run_phase_background,
            args=(run_id, phase, prepared, command, run_dir),
            daemon=True,
        )
        thread.start()
        return render_run_progress(run_id, phase)
    except Exception as exc:
        error_text = str(exc)
        (run_dir / "error.txt").write_text(error_text, encoding="utf-8")
        return render_error(run_id, error_text)


def handle_cleanup_run(target: str, handler: BaseHTTPRequestHandler, state_path: Path) -> str:
    run_id = f"cleanup-{target}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = UI_RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    form = parse_multipart(handler)
    cleanup_title = form_text(form, "cleanup_title")
    cleanup_ms_keg_id = form_text(form, "cleanup_ms_keg_id")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "indah_cleanup.py"),
        "--phase",
        target,
        "--state",
        str(state_path),
        "--run-state",
        str(DEFAULT_RUN_STATE_DIR),
        "--execute",
    ]
    if cleanup_title:
        command.extend(["--title", cleanup_title])
    if cleanup_ms_keg_id:
        command.extend(["--ms-keg-id", cleanup_ms_keg_id])
    if not cleanup_title and not cleanup_ms_keg_id:
        command.append("--from-kegiatan-map")
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    save_run_artifacts(run_dir, run_id, command, {}, result)
    title = "Cleanup Phase 2 dan 3" if target == "all" else f"Cleanup {target}"
    return render_run_result(run_id, title, {}, command, result.returncode, result.stdout, result.stderr)


def parse_multipart(handler: BaseHTTPRequestHandler) -> cgi.FieldStorage:
    return cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": handler.headers.get("Content-Length", "0"),
        },
    )


def save_specific_files(form: cgi.FieldStorage, raw_dir: Path, slugs: List[str]) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for slug in slugs:
        item = first_file_item(form, f"file_{slug}")
        if item is None:
            continue
        path = raw_dir / f"{slug}__{safe_filename(item.filename)}"
        with path.open("wb") as output:
            output.write(item.file.read())
        files[slug] = path
    return files


def save_workbook_files(form: cgi.FieldStorage, raw_dir: Path) -> List[Path]:
    item = first_file_item(form, "workbook_file")
    if item is None:
        return []
    path = raw_dir / f"workbook__{safe_filename(item.filename)}"
    with path.open("wb") as output:
        output.write(item.file.read())
    return [path]


def first_file_item(form: cgi.FieldStorage, name: str) -> Optional[cgi.FieldStorage]:
    if name not in form:
        return None
    value = form[name]
    items = value if isinstance(value, list) else [value]
    for item in items:
        if getattr(item, "filename", None):
            return item
    return None


def form_text(form: cgi.FieldStorage, name: str) -> str:
    value = form.getfirst(name, "")
    return clean(value) or ""


def build_phase_command(
    phase: str,
    config: Dict[str, object],
    prepared_dir: Path,
    form: cgi.FieldStorage,
    state_path: Path,
) -> List[str]:
    command = [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / str(config["script"])),
        "--folder",
        str(prepared_dir),
        "--state",
        str(state_path),
        "--run-state",
        str(DEFAULT_RUN_STATE_DIR),
    ]
    only_title = form_text(form, "only_title")
    limit = form_text(form, "limit")
    submission_period = form_text(form, "submission_period")
    try:
        submission_period_year = int(submission_period)
    except (TypeError, ValueError):
        raise IndahError("Periode tahun wajib berupa angka.") from None
    if not 2000 <= submission_period_year <= 2100:
        raise IndahError("Periode tahun harus berada antara 2000 dan 2100.")
    command.extend(["--submission-period", str(submission_period_year)])
    if only_title:
        command.extend(["--only-title", only_title])
    if limit and limit != "0":
        command.extend(["--limit", limit])
    if form.getfirst("submit") or form.getfirst("final_submit"):
        command.append("--submit")
    if form.getfirst("final_submit"):
        command.append("--final-submit")
    return command


def run_phase_background(run_id: str, phase: str, prepared: Dict[str, Dict[str, str]], command: List[str], run_dir: Path) -> None:
    try:
        result = run_command_streaming(command, PROJECT_ROOT, run_dir)
        save_run_artifacts(run_dir, run_id, command, prepared, result)
        (run_dir / "done").write_text("1", encoding="utf-8")
    except Exception as exc:
        (run_dir / "error.txt").write_text(str(exc), encoding="utf-8")


def run_command_streaming(command: List[str], cwd: Path, run_dir: Path) -> subprocess.CompletedProcess:
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_parts: List[str] = []
    stderr_parts: List[str] = []
    def drain(stream, path: Path, parts: List[str]) -> None:
        with path.open("w", encoding="utf-8") as output:
            for line in stream or []:
                parts.append(line)
                output.write(line)
                output.flush()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_path, stdout_parts)),
        threading.Thread(target=drain, args=(process.stderr, stderr_path, stderr_parts)),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise IndahError("Proses melebihi batas waktu satu jam. Periksa hasil di INDAH sebelum mencoba lagi.") from None
    finally:
        for reader in readers:
            reader.join()
        for stream in (process.stdout, process.stderr):
            if stream is not None and hasattr(stream, "close"):
                stream.close()
    return subprocess.CompletedProcess(command, returncode, "".join(stdout_parts), "".join(stderr_parts))


def save_run_artifacts(
    run_dir: Path,
    run_id: str,
    command: List[str],
    prepared: Dict[str, Dict[str, str]],
    result: subprocess.CompletedProcess,
) -> None:
    summary = {
        "run_id": run_id,
        "command": command,
        "returncode": result.returncode,
        "prepared": prepared,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")


def friendly_error(message: str) -> str:
    text = message.lower()
    if message.startswith("Hasil penyimpanan belum terverifikasi:"):
        return message
    if "belum dipilih" in text:
        return "File Excel belum dipilih. Pilih file Excel berisi sheet sesuai template, lalu coba kembali."
    if "link spreadsheet belum" in text:
        return "Link spreadsheet belum diisi. Masukkan link yang dapat dibuka, lalu coba kembali."
    if "401" in text or "session" in text or "sesi" in text:
        return "Sesi login sudah berakhir atau tidak tersedia. Login kembali, lalu periksa hasil di INDAH sebelum mengulang upload."
    if "403" in text or "permission denied" in text:
        return "Akses ditolak. Pastikan akun memiliki izin untuk kegiatan ini; jika tetap gagal, hubungi pengelola aplikasi."
    if "ms-kegiatan" in text or "ms_keg" in text:
        return "Kegiatan induk belum ditemukan atau tidak dapat dibuka. Samakan judul kegiatan di semua sheet, periksa periode tahun, dan pastikan kegiatan Phase 1 masih tersedia di INDAH."
    if "tidak mengembalikan id" in text:
        return "INDAH belum memberikan konfirmasi penyimpanan. Periksa apakah data sudah muncul di INDAH sebelum mengulang agar tidak membuat data ganda."
    if "kosong" in text:
        fields = re.findall(r"([\w/]+) kosong", message)
        return "Ada isian wajib yang belum diisi" + (": " + ", ".join(fields) if fields else "") + ". Lengkapi pada sheet, lalu coba kembali."
    if "periode" in text:
        return "Periode tahun tidak sesuai. Pilih tahun antara 2000 dan 2100 dan gunakan periode yang sama dengan kegiatan induk."
    if "tidak menemukan csv" in text or "sheet" in text or "template" in text:
        return "Sheet atau file yang dibutuhkan belum ditemukan atau belum sesuai template. Periksa nama sheet dan kolomnya, lalu unggah kembali."
    if "timeout" in text or "timed out" in text or "urlerror" in text or "502" in text or "503" in text or "504" in text:
        return "Koneksi ke INDAH terputus atau layanan sedang lambat. Periksa data yang sudah tersimpan sebelum mencoba lagi."
    if "400" in text or "422" in text:
        return "INDAH menolak isian yang dikirim. Periksa kelengkapan dan format isian pada data ini."
    if "500" in text:
        return "INDAH mengalami gangguan saat memproses data. Periksa hasil penyimpanan lalu coba kembali beberapa saat lagi."
    if "run tidak ditemukan" in text:
        return "Riwayat proses ini tidak ditemukan. Kembali ke halaman upload untuk melihat proses yang tersedia."
    if "batas waktu" in text:
        return "Proses berhenti karena melewati batas waktu. Periksa hasil di INDAH sebelum mencoba lagi."
    return "Proses tidak dapat diselesaikan. Periksa isian file dan hasil yang sudah tersimpan di INDAH. Jika tetap gagal, berikan nomor proses ini kepada pengelola aplikasi."


def upload_summary(stdout: str, stderr: str, returncode=None, error: str = "") -> Dict[str, object]:
    events = []
    for line in stdout.splitlines():
        if line.startswith("INDAH_PROGRESS "):
            try:
                item = json.loads(line[len("INDAH_PROGRESS "):])
                if isinstance(item, dict):
                    events.append(item)
            except json.JSONDecodeError:
                continue
    total = max((int(event.get("total") or 0) for event in events), default=0)
    results = [event for event in events if event.get("stage") == "item"]
    successes = sum(item.get("outcome") == "success" for item in results)
    failures = sum(item.get("outcome") == "failed" for item in results)
    skipped = sum(item.get("outcome") == "skipped" for item in results)
    issues = [str(item.get("title") or "Data") + ": " + friendly_error(str(item.get("message") or ""))
              for item in results if item.get("outcome") in {"failed", "skipped"}]
    if not events:
        successes = len(re.findall(r"^\[ok\]", stdout, re.M))
        failures = len(re.findall(r"^\[error\]", stdout, re.M))
        skipped = len(re.findall(r"^\[skip\]", stdout, re.M))
        counts = re.findall(r"^Rows (?:kegiatan|variabel|indikator): (\d+)", stdout, re.M)
        total = int(counts[0]) if counts else 0
        for line in stdout.splitlines():
            if line.startswith(("[error]", "[skip]", "[abort]")):
                issues.append(friendly_error(line))
    done = returncode is not None or bool(error)
    fatal = bool(error) or returncode not in (None, 0)
    if fatal and events and events[-1].get("stage") == "upload":
        failures += 1
    if fatal:
        last_title = str(events[-1].get("title") or "") if events else ""
        issues.append((last_title + ": " if last_title else "") + friendly_error(error or stderr or stdout))
    remaining = max(0, total - successes - failures - skipped)
    if done:
        status = "Terhenti" if fatal else "Selesai dengan masalah" if failures or skipped else "Selesai"
        if not total and not successes and not fatal:
            status = "Tidak ada data yang diproses"
        last_line = f"{status}. {successes} data berhasil, {failures} gagal, {skipped} dilewati, {remaining} belum dikirim."
    else:
        current = events[-1] if events else {}
        stages = {"validation": "Memeriksa isian dan kegiatan induk", "payload": "Menyiapkan isian",
                  "upload": "Mengirim ke INDAH", "item": "Hasil penyimpanan diterima", "mapping": "Mencatat hasil upload"}
        last_line = stages.get(current.get("stage"), "Menyiapkan proses upload")
        if current.get("title"):
            last_line += f". Data {current.get('current')} dari {total}: {current['title']}"
        if stdout.splitlines() and "[retry" in stdout.splitlines()[-1]:
            last_line = "INDAH belum merespons dengan baik. Sedang mencoba kembali. " + last_line
        last_line += f"\nHasil sementara: {successes} berhasil, {failures} gagal, {skipped} dilewati dari {total} data."
        status = "Berjalan"
    percent = int((successes + failures + skipped) * 100 / total) if total else 0
    percent = min(99, percent) if not done else percent
    return {"done": done, "percent": percent, "status": status, "total": total, "success": successes,
            "failed": failures, "skipped": skipped, "remaining": remaining, "last_line": last_line,
            "output": "\n".join([last_line] + list(dict.fromkeys(issues)))}


def run_status(run_id: str) -> Dict[str, object]:
    safe_id = safe_filename(run_id)
    run_dir = UI_RUNS_ROOT / safe_id
    if not safe_id or not run_dir.exists():
        return {"done": True, "percent": 100, "output": "Run tidak ditemukan.", "last_line": "Run tidak ditemukan."}
    stdout = (run_dir / "stdout.txt").read_text(encoding="utf-8") if (run_dir / "stdout.txt").exists() else ""
    stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8") if (run_dir / "stderr.txt").exists() else ""
    summary_path = run_dir / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    except json.JSONDecodeError:
        summary = {}
    error_path = run_dir / "error.txt"
    error = error_path.read_text(encoding="utf-8") if error_path.exists() else ""
    return upload_summary(stdout, stderr, summary.get("returncode"), error)


def render_run_progress(run_id: str, phase: str) -> str:
    phase_title = str(PHASES.get(phase, {}).get("title", phase))
    body = f"""
<section class="band result">
  <h2>Upload {escape(phase_title)} berjalan</h2>
  <div class="meta-list">
<div><span>Nomor proses</span><span>{escape(run_id)}</span></div>
    <div><span>Status</span><span class="warn">Memproses upload ke BPS INDAH</span></div>
  </div>
  <p>Persentase menunjukkan data yang selesai diproses, termasuk data yang gagal atau dilewati.</p>
  <div class="progress-track"><div class="progress-bar" style="width: 0%;"></div></div>
  <div class="progress-output" aria-live="polite">Menyiapkan proses upload...</div>
  <p><a class="button secondary" href="/run?id={escape(run_id)}">Lihat hasil proses</a></p>
</section>
<script>
document.body.setAttribute("data-progress-run-id", "{escape(run_id)}");
window.setInterval(function () {{
  fetch("/run-status?id={escape(run_id)}", {{cache: "no-store"}})
    .then(function (response) {{ return response.json(); }})
    .then(function (data) {{ if (data.done) window.location.href = "/run?id={escape(run_id)}"; }});
}}, 1200);
</script>
"""
    return page("Upload berjalan", body)


def render_run_result(
    run_id: str,
    phase: str,
    prepared: Dict[str, Dict[str, str]],
    command: List[str],
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    result = upload_summary(stdout, stderr, returncode)
    status = escape(result["status"])
    status_class = "ok" if result["status"] == "Selesai" else "bad" if result["status"] == "Terhenti" else "warn"
    phase_title = str(PHASES.get(phase, {}).get("title", phase))
    body = f"""
<section class="band result">
  <h2>Hasil {escape(phase_title)}</h2>
  <div class="meta-list">
    <div><span>Nomor proses</span><span>{escape(run_id)}</span></div>
    <div><span>Status</span><span class="{status_class}">{status}</span></div>
    <div><span>Berhasil</span><span>{result['success']} data</span></div>
    <div><span>Gagal</span><span>{result['failed']} data</span></div>
    <div><span>Dilewati</span><span>{result['skipped']} data</span></div>
    <div><span>Belum dikirim</span><span>{result['remaining']} data</span></div>
  </div>
  <h3>Kesimpulan</h3>
  <div class="progress-output">{escape(result['output'])}</div>
  <p>Periksa hasil di INDAH sebelum mengulang data yang belum mendapat konfirmasi.</p>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Hasil Phase", body)


def render_error(run_id: str, message: str) -> str:
    body = f"""
<section class="band result">
  <h2>Proses belum berhasil</h2>
  <div class="meta-list">
    <div><span>Nomor proses</span><span>{escape(run_id)}</span></div>
    <div><span>Status</span><span class="bad">Gagal</span></div>
  </div>
  <p>{escape(friendly_error(message))}</p>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Error", body)


def render_saved_run(run_id: str) -> str:
    safe_id = safe_filename(run_id)
    run_dir = UI_RUNS_ROOT / safe_id
    if not safe_id or not run_dir.exists():
        return render_error(safe_id or "-", "Run tidak ditemukan.")
    error_path = run_dir / "error.txt"
    if error_path.exists():
        result = run_status(safe_id)
        return page("Proses terhenti", f'<section class="band result"><h2>Proses terhenti</h2><p>Nomor proses: {escape(safe_id)}</p><div class="progress-output">{escape(result["output"])}</div><a class="button secondary" href="/">Kembali</a></section>')
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("returncode") is None:
        phase = safe_id.split("-", 1)[0]
        return render_run_progress(safe_id, phase)
    stdout = (run_dir / "stdout.txt").read_text(encoding="utf-8") if (run_dir / "stdout.txt").exists() else ""
    stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8") if (run_dir / "stderr.txt").exists() else ""
    return render_run_result(safe_id, safe_id.split("-", 1)[0], summary.get("prepared") or {},
                             summary.get("command") or [], summary["returncode"], stdout, stderr)


def status_text(ok: bool, yes: str, no: str) -> str:
    class_name = "ok" if ok else "warn"
    return f'<span class="{class_name}">{escape(yes if ok else no)}</span>'


def map_status(path: Path) -> str:
    if path.exists():
        return f'<span class="ok">{escape(str(path.relative_to(PROJECT_ROOT)))}</span>'
    return '<span class="warn">Belum ada</span>'


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def find_available_port(host: str, start_port: int) -> int:
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"Tidak ada port kosong mulai {start_port}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="UI lokal untuk otomatisasi metadata INDAH.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    port = find_available_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), IndahUIHandler)
    print(f"INDAH UI aktif: http://{args.host}:{port}")
    print("Tekan Ctrl+C untuk stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
