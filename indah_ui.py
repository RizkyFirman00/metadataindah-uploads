from __future__ import annotations

import argparse
import contextlib
import email.message
import email.utils
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from indah_automation.shared import BASE_API_URL, WEB_URL, IndahError, PROJECT_ROOT, clean, load_auth, user_run_state_dir
from indah_automation.spreadsheet_io import (
    download_spreadsheet_url,
    prepare_phase_sources,
    safe_filename,
)


UPLOAD_ROOT = PROJECT_ROOT / "ui_uploads"
TEMPLATE_DIR = PROJECT_ROOT / "indah_metadata_csv_templates"
WIB = timezone(timedelta(hours=7))
REQUEST = threading.local()
UI_RUNS_ROOT = PROJECT_ROOT / "ui_runs"
UI_SESSION_ROOT = PROJECT_ROOT / ".indah_session" / "ui_sessions"
SESSION_COOKIE = "indah_ui_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
MAX_REQUEST_BYTES = 25 * 1024 * 1024
RUN_RETENTION_DAYS = int(os.environ.get("INDAH_RETENTION_DAYS", "90"))
MAINTENANCE_INTERVAL_SECONDS = 6 * 60 * 60
INTERRUPTED_MESSAGE = (
    "Proses terputus karena aplikasi dimulai ulang. Sebagian data mungkin sudah tersimpan: "
    "periksa hasil di INDAH sebelum mengulang upload."
)
# Pesan yang sudah ramah pengguna; ditampilkan apa adanya oleh friendly_error.
USER_FACING_PREFIXES = (
    "Hasil penyimpanan belum terverifikasi:",
    "Hanya link Google Sheets",
    "Ukuran ",
    "Isi file Excel terlalu besar",
    "Isi judul atau nomor kegiatan",
    "Nomor kegiatan harus",
    "Batas jumlah data harus",
    "Proses terputus",
    "Sesi login",
    "File Excel rusak",
    "Format file belum didukung",
    "File .xls lama",
)

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-labelledby="icon-title icon-desc">
  <title id="icon-title">INDAH Metadata Upload</title>
  <desc id="icon-desc">Spreadsheet dengan panah upload</desc>
  <rect width="64" height="64" rx="12" fill="#101014"/>
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
        REQUEST.user = session_display_name(state_path)
        parsed = urlparse(self.path)
        if parsed.path == "/riwayat":
            self.respond_html(render_history(session_owner(session_id)))
            return
        if parsed.path == "/hapus":
            self.respond_html(render_cleanup_page())
            return
        if parsed.path == "/template.zip":
            self.respond_bytes(template_zip(), "application/zip", "template-metadata-indah.zip")
            return
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
            self.respond_html(render_saved_run(run_id, session_owner(session_id)))
            return
        if parsed.path == "/run-status":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [""])[0]
            self.respond_json(run_status(run_id, session_owner(session_id)))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        session_id = self.user_session_id()
        state_path = user_state_path(session_id)
        REQUEST.user = session_display_name(state_path)
        parsed = urlparse(self.path)
        if parsed.path == "/keluar":
            state_path.unlink(missing_ok=True)
            self.respond_redirect("/login")
            return
        if request_too_large(self.headers):
            self.close_connection = True
            self.respond_html(render_error("-", f"Ukuran unggahan melebihi batas {MAX_REQUEST_BYTES // (1024 * 1024)} MB."), status=413)
            return
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

    def respond_bytes(self, content: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def respond_redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if getattr(self, "_new_session_cookie", None):
            self.send_header("Set-Cookie", self._new_session_cookie)
        self.end_headers()

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


ICON_UPLOAD = '<svg aria-hidden="true" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4m-5 5 5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>'
ICON_MARK = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="square"><path d="M12 17V4m-6 6 6-6 6 6"/><path d="M4 20h16"/></svg>'
ICON_MOON = '<svg class="icon-moon" aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z"/></svg>'
ICON_SUN = '<svg class="icon-sun" aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
ICON_CLOSE = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>'
ICON_EYE = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>'
ICON_WARN = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4m0 4h.01"/></svg>'
ICON_CHECK = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>'

PHASE_INFO = {
    "kegiatan": ("Kegiatan", "Identitas dan pelaksanaan kegiatan"),
    "variabel": ("Variabel", "Data yang dikumpulkan"),
    "indikator": ("Indikator", "Ukuran hasil kegiatan"),
}

THEME_BOOT = """<script>(function(){try{if(localStorage.getItem('indah-theme')==='dark'){document.documentElement.setAttribute('data-theme','dark');}}catch(e){}})();</script>"""

PAGE_SCRIPT = r"""<script>
document.addEventListener("DOMContentLoaded", function () {
  var root = document.documentElement;
  document.querySelectorAll(".theme-toggle").forEach(function (button) {
    function sync() {
      var dark = root.getAttribute("data-theme") === "dark";
      var label = dark ? "Ganti ke tema terang" : "Ganti ke tema gelap";
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    sync();
    button.addEventListener("click", function () {
      var dark = root.getAttribute("data-theme") !== "dark";
      if (dark) { root.setAttribute("data-theme", "dark"); } else { root.removeAttribute("data-theme"); }
      try { localStorage.setItem("indah-theme", dark ? "dark" : "light"); } catch (e) {}
      sync();
    });
  });

  var overlay = document.querySelector(".overlay");
  var overlayText = document.querySelector(".overlay-text");
  function busy(form, label) {
    if (overlayText) overlayText.textContent = label || "Memproses...";
    if (overlay) overlay.classList.add("is-visible");
    form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
  }

  var form = document.getElementById("upload-form");
  if (form) {
    var sourceMode = document.getElementById("source-mode");
    var drop = document.getElementById("drop");
    var fileInput = document.getElementById("workbook");
    var fileName = document.getElementById("file-name");
    var useLink = document.getElementById("use-link");
    var linkBox = document.getElementById("link-box");
    var urlInput = document.getElementById("sheet-url");
    var cancelLink = document.getElementById("cancel-link");
    var sheetName = document.getElementById("sheet-name");
    var error = document.getElementById("form-error");

    form.querySelectorAll('input[name="phase_choice"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        form.action = "/phase/" + radio.value;
        if (sheetName) sheetName.textContent = radio.getAttribute("data-sheet");
        try { history.replaceState(null, "", "/?phase=" + radio.value); } catch (e) {}
      });
    });

    function setLinkMode(on) {
      sourceMode.value = on ? "url" : "workbook";
      drop.classList.toggle("is-disabled", on);
      drop.setAttribute("aria-disabled", on ? "true" : "false");
      fileInput.disabled = on;
      urlInput.disabled = !on;
      linkBox.hidden = !on;
      useLink.hidden = on;
      if (error) error.hidden = true;
      if (on) urlInput.focus(); else useLink.focus();
    }
    useLink.addEventListener("click", function () { setLinkMode(true); });
    cancelLink.addEventListener("click", function () { setLinkMode(false); });

    function showFile() {
      fileName.textContent = fileInput.files.length ? "File dipilih: " + fileInput.files[0].name : "";
    }
    fileInput.addEventListener("change", showFile);
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) { event.preventDefault(); drop.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-over"); });
    });
    drop.addEventListener("drop", function (event) {
      event.preventDefault();
      if (event.dataTransfer && event.dataTransfer.files.length) {
        fileInput.files = event.dataTransfer.files;
        showFile();
      }
    });

    form.addEventListener("submit", function (event) {
      var linkMode = sourceMode.value === "url";
      var missing = linkMode ? !urlInput.value.trim() : !fileInput.files.length;
      if (missing) {
        event.preventDefault();
        error.querySelector("span").textContent = linkMode
          ? "Masukkan link Google Sheets terlebih dahulu."
          : "Pilih file Excel terlebih dahulu.";
        error.hidden = false;
        (linkMode ? urlInput : fileInput).focus();
        return;
      }
      var submitter = event.submitter;
      var confirmText = submitter && submitter.getAttribute("data-confirm");
      if (confirmText && !window.confirm(confirmText)) { event.preventDefault(); return; }
      if (submitter && submitter.name) {
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = submitter.name;
        hidden.value = submitter.value || "1";
        form.appendChild(hidden);
      }
      busy(form, submitter && submitter.getAttribute("data-busy"));
    });
  }

  document.querySelectorAll("form[data-busy-form]").forEach(function (other) {
    other.addEventListener("submit", function (event) {
      var submitter = event.submitter;
      var confirmText = submitter && submitter.getAttribute("data-confirm");
      if (confirmText && !window.confirm(confirmText)) { event.preventDefault(); return; }
      if (submitter && submitter.getAttribute("formaction")) other.action = submitter.getAttribute("formaction");
      busy(other, submitter && submitter.getAttribute("data-busy"));
    });
  });

  var toggle = document.getElementById("toggle-password");
  var password = document.getElementById("login-password");
  if (toggle && password) {
    toggle.addEventListener("click", function () {
      var show = password.type === "password";
      password.type = show ? "text" : "password";
      toggle.setAttribute("aria-pressed", String(show));
      toggle.setAttribute("aria-label", show ? "Sembunyikan password" : "Lihat password");
    });
  }

  var onlyProblems = document.getElementById("only-problems");
  var itemsTable = document.getElementById("items");
  if (onlyProblems && itemsTable) {
    onlyProblems.addEventListener("change", function () { itemsTable.classList.toggle("only-problems", onlyProblems.checked); });
  }

  var progress = document.getElementById("progress");
  if (progress) {
    var runId = progress.getAttribute("data-run-id");
    var heading = progress.getAttribute("data-heading") || "Proses berjalan";
    var lastItems = -1;
    function setText(id, value) { var el = document.getElementById(id); if (el) el.textContent = value; }
    function setNotice(id, text) {
      var el = document.getElementById(id);
      if (!el) return;
      el.hidden = !text;
      el.querySelector("span").textContent = text || "";
    }
    function renderItems(items) {
      var body = itemsTable.querySelector("tbody");
      body.textContent = "";
      if (!items.length) {
        var empty = document.createElement("tr");
        empty.className = "empty-row";
        var cell = document.createElement("td");
        cell.colSpan = 5;
        cell.textContent = "Belum ada data yang selesai diproses.";
        empty.appendChild(cell);
        body.appendChild(empty);
        return;
      }
      var pills = {success: "ok", failed: "bad", skipped: "warn"};
      var problems = 0;
      items.slice().reverse().forEach(function (item) {
        if (item.outcome !== "success") problems += 1;
        var row = document.createElement("tr");
        row.className = item.outcome === "success" ? "is-ok" : "is-problem";
        [["num", item.n], ["", item.title], ["pill", item.label], ["", item.message], ["time", item.time]].forEach(function (spec) {
          var td = document.createElement("td");
          if (spec[0] === "pill") {
            var pill = document.createElement("span");
            pill.className = "pill pill-" + (pills[item.outcome] || "warn");
            pill.textContent = spec[1] || "";
            td.appendChild(pill);
          } else {
            if (spec[0]) td.className = spec[0];
            td.textContent = spec[1] == null ? "" : String(spec[1]);
          }
          row.appendChild(td);
        });
        body.appendChild(row);
      });
      setText("problem-count", problems);
    }
    function poll() {
      fetch("/run-status?id=" + encodeURIComponent(runId), {cache: "no-store"})
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var value = data.percent || 0;
          document.getElementById("bar").style.width = value + "%";
          setText("pct", value + "%");
          var processed = (data.success || 0) + (data.failed || 0) + (data.skipped || 0);
          setText("progress-label", processed + " dari " + (data.total || 0) + " data diproses");
          setText("now-text", data.now_text || "Memproses");
          setText("now-title", data.now_title || "");
          setText("s-ok", data.success || 0);
          setText("s-fail", data.failed || 0);
          setText("s-skip", data.skipped || 0);
          setText("s-left", data.remaining || 0);
          setText("t-elapsed", data.elapsed_text || "-");
          setText("t-start", data.started_text || "-");
          setText("t-avg", data.avg_text || "-");
          setText("t-eta", data.eta_text || "-");
          setText("t-finish", data.finish_text || "-");
          setNotice("retry", data.retry);
          setNotice("abort", data.aborted);
          setNotice("warnings", (data.warnings || []).join(" · "));
          var stage = data.stage_index || 0;
          document.querySelectorAll("#stages li").forEach(function (li) {
            var n = Number(li.getAttribute("data-stage"));
            li.classList.toggle("is-done", n < stage);
            li.classList.toggle("is-current", n === stage);
          });
          var items = data.items || [];
          if (items.length !== lastItems) { lastItems = items.length; renderItems(items); }
          document.title = value + "% · " + heading;
          if (data.done) { window.location.href = "/run?id=" + encodeURIComponent(runId); }
        })
        .catch(function () { setText("now-text", "Menunggu respons server..."); });
    }
    poll();
    window.setInterval(poll, 1500);
  }
});
</script>"""


def session_display_name(state_path: Path) -> str:
    if not state_path.exists():
        return ""
    try:
        auth = load_auth(state_path)
    except (IndahError, OSError, ValueError, json.JSONDecodeError):
        return ""
    return str(auth.user_payload.get("name") or auth.user_payload.get("username") or "Pengguna INDAH")


def page(title: str, body: str) -> str:
    user = getattr(REQUEST, "user", "")
    theme_button = f'<button class="btn btn-secondary btn-icon theme-toggle" type="button" aria-label="Ganti tema">{ICON_MOON}{ICON_SUN}</button>'
    if user:
        account = f"""<span class="user">{escape(user)}</span>
      {theme_button}
      <form method="post" action="/keluar"><button class="btn btn-secondary btn-sm" type="submit">Keluar</button></form>"""
    else:
        account = f'{theme_button}<a class="btn btn-secondary btn-sm" href="/login">Masuk</a>'
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <title>{escape(title)} · INDAH Metadata</title>
  {THEME_BOOT}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=Inter:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{CSS}</style>
  {PAGE_SCRIPT}
</head>
<body>
  <div class="overlay" aria-live="polite"><div class="overlay-box"><div class="spinner"></div><span class="overlay-text">Memproses...</span></div></div>
  <header class="topbar">
    <div class="wrap">
      <a class="brand" href="/"><span class="brand-mark">{ICON_MARK}</span>INDAH Metadata</a>
      <div class="top-actions">
      {account}
      </div>
    </div>
  </header>
  <main class="wrap">
{body}
  </main>
</body>
</html>"""


def page_head(title: str, subtitle: str = "", eyebrow: str = "") -> str:
    eyebrow_html = f'<span class="eyebrow">{escape(eyebrow)}</span>' if eyebrow else ""
    subtitle_html = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    return f'<div class="page-head">{eyebrow_html}<h1>{escape(title)}</h1>{subtitle_html}</div>'


def notice(kind: str, html_text: str) -> str:
    icon = ICON_CHECK if kind == "ok" else ICON_WARN
    return f'<div class="notice notice-{kind}" role="{"status" if kind == "ok" else "alert"}">{icon}<span>{html_text}</span></div>'


def render_home(state_path: Path, phase: str = "kegiatan") -> str:
    if phase not in PHASES:
        phase = "kegiatan"
    login_notice = ""
    if not state_path.exists():
        login_notice = notice("warn", 'Kamu belum masuk. <a href="/login">Masuk dengan akun INDAH</a> dulu sebelum mengunggah.')
    elif not session_display_name(state_path):
        login_notice = notice("warn", 'Sesi login sudah tidak berlaku. <a href="/login">Masuk lagi</a> sebelum mengunggah.')
    body = f"""
{page_head("Upload metadata", "Pilih jenis metadata, unggah file Excel, lalu simpan. Data tersimpan sebagai draft di INDAH.")}
{login_notice}
{render_phase_form(phase, PHASES[phase])}
<nav class="footer-links" aria-label="Menu lain">
  <a href="/template.zip">Unduh template</a>
  <a href="/riwayat">Riwayat upload</a>
  <a class="danger" href="/hapus">Hapus data</a>
</nav>
"""
    return page("Upload metadata", body)


def render_phase_form(phase: str, config: Dict[str, object]) -> str:
    choices = []
    for slug, item in PHASES.items():
        title, desc = PHASE_INFO[slug]
        checked = " checked" if slug == phase else ""
        sheet = item["required"][0]
        choices.append(f"""<label class="choice"><input type="radio" name="phase_choice" value="{slug}" data-sheet="{sheet}"{checked}><span class="radio" aria-hidden="true"></span><span><strong>{title}</strong><small>{desc}</small></span></label>""")
    current_year = int(time.strftime("%Y"))
    year_options = "".join(
        f'<option value="{year}"{" selected" if year == current_year else ""}>{year}</option>'
        for year in range(current_year + 1, current_year - 6, -1)
    )
    sheet = config["required"][0]
    return f"""
<form id="upload-form" class="panel" action="/phase/{phase}" method="post" enctype="multipart/form-data" novalidate>
  <input type="hidden" id="source-mode" name="source_mode" value="workbook">
  <div class="section">
    <div class="step"><span class="step-num">1</span>Pilih jenis metadata</div>
    <fieldset class="choices" aria-label="Jenis metadata">{"".join(choices)}</fieldset>
  </div>
  <div class="section">
    <div class="section-head">
      <div class="step"><span class="step-num">2</span>Unggah file</div>
      <span class="small muted">Dibaca dari sheet <code id="sheet-name">{sheet}</code></span>
    </div>
    <label class="drop" id="drop" for="workbook" aria-disabled="false">
      <span class="drop-icon">{ICON_UPLOAD}</span>
      <strong>Tarik file Excel ke sini, atau klik untuk memilih</strong>
      <span class="small muted">Format .xlsx atau .xlsm, maksimal 25 MB</span>
      <input id="workbook" type="file" name="workbook_file" accept=".xlsx,.xlsm">
    </label>
    <span id="file-name" class="small muted" aria-live="polite"></span>
    <button type="button" id="use-link" class="link-btn">Atau pakai link Google Sheets</button>
    <div id="link-box" class="field" hidden>
      <label for="sheet-url">Link Google Sheets</label>
      <div class="input-row">
        <input id="sheet-url" class="input is-active" type="url" name="spreadsheet_url" placeholder="https://docs.google.com/spreadsheets/d/..." disabled>
        <button type="button" id="cancel-link" class="btn btn-secondary btn-icon" aria-label="Batal pakai link, kembali ke upload file" title="Batal">{ICON_CLOSE}</button>
      </div>
      <span class="small muted">Pastikan aksesnya "Siapa saja yang memiliki link".</span>
    </div>
  </div>
  <div class="section">
    <div class="step"><span class="step-num">3</span>Pilih tahun, lalu simpan</div>
    <div class="submit-row">
      <label class="year">Tahun laporan
        <select name="submission_period" required>{year_options}</select>
      </label>
      <div class="actions">
        <button class="btn btn-secondary" type="submit" name="final_submit" value="1" data-busy="Mengirim data ke INDAH..." data-confirm="Kirim data ke INDAH untuk ditinjau? Pastikan isian dan tahun laporan sudah benar.">Submit Data</button>
        <button class="btn btn-primary" type="submit" name="submit" value="1" data-busy="Menyimpan draft...">Simpan sebagai draft</button>
      </div>
    </div>
    <div id="form-error" class="notice notice-error" role="alert" hidden>{ICON_WARN}<span></span></div>
  </div>
</form>
"""


def render_cleanup_panel() -> str:
    return f"""
<form class="panel" action="/cleanup/all" method="post" data-busy-form>
  <div class="section">
    <div class="step">Kegiatan yang datanya mau dihapus</div>
    <p class="small muted">Isi minimal salah satu. Semua variabel atau indikator milik kegiatan ini akan dihapus dari INDAH.</p>
    <div class="grid-2">
      <div class="field">
        <label for="cleanup-title">Judul kegiatan</label>
        <input id="cleanup-title" class="input" type="text" name="cleanup_title" placeholder="Judul persis seperti di INDAH">
      </div>
      <div class="field">
        <label for="cleanup-id">Nomor kegiatan di INDAH</label>
        <input id="cleanup-id" class="input" type="text" name="cleanup_ms_keg_id" inputmode="numeric" placeholder="Contoh: 104286">
      </div>
    </div>
  </div>
  <div class="section">
    <div class="actions">
      <button class="btn btn-danger" type="submit" formaction="/cleanup/variabel" data-busy="Menghapus variabel..." data-confirm="Hapus semua variabel dari kegiatan ini?">Hapus variabel</button>
      <button class="btn btn-danger" type="submit" formaction="/cleanup/indikator" data-busy="Menghapus indikator..." data-confirm="Hapus semua indikator dari kegiatan ini?">Hapus indikator</button>
      <button class="btn btn-danger" type="submit" formaction="/cleanup/all" data-busy="Menghapus variabel dan indikator..." data-confirm="Hapus semua variabel dan indikator dari kegiatan ini?">Hapus keduanya</button>
    </div>
  </div>
</form>
"""


def render_cleanup_page() -> str:
    body = f"""
{page_head("Hapus data", "Menghapus variabel atau indikator dari satu kegiatan di INDAH. Tindakan ini tidak bisa dibatalkan.")}
{render_cleanup_panel()}
<nav class="footer-links"><a href="/">Kembali ke upload</a></nav>
"""
    return page("Hapus data", body)


def run_title(run_id: str) -> str:
    kind = run_id.split("-", 1)[0]
    if kind in PHASE_INFO:
        return PHASE_INFO[kind][0]
    return "Hapus data" if kind == "cleanup" else kind


def status_class(status: str) -> str:
    return {"Selesai": "ok", "Selesai dengan masalah": "warn", "Terhenti": "bad", "Berjalan": "run"}.get(status, "warn")


def render_history(owner: str) -> str:
    rows = []
    if UI_RUNS_ROOT.exists():
        runs = [d for d in UI_RUNS_ROOT.iterdir() if d.is_dir() and run_owned_by(d, owner)]
        runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for run_dir in runs[:30]:
            summary = run_status(run_dir.name, owner)
            status = str(summary.get("status") or ("Berjalan" if not summary.get("done") else "Selesai"))
            when = datetime.fromtimestamp(run_dir.stat().st_mtime, WIB).strftime("%d %b %Y, %H.%M WIB")
            rows.append(f"""<li><a href="/run?id={escape(run_dir.name)}"><span><strong>{escape(run_title(run_dir.name))}</strong><br><span class="small muted">{when} · <code>{escape(run_dir.name)}</code></span></span><span class="status status-{status_class(status)}">{escape(status)}</span></a></li>""")
    content = f'<ul class="history">{"".join(rows)}</ul>' if rows else '<p class="empty">Belum ada proses dari browser ini.</p>'
    body = f"""
{page_head("Riwayat upload", "Proses yang dijalankan dari browser ini. Data lebih dari 90 hari dihapus otomatis.")}
<section class="panel">{content}</section>
<nav class="footer-links"><a href="/">Kembali ke upload</a></nav>
"""
    return page("Riwayat upload", body)


def template_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(TEMPLATE_DIR.glob("*.csv")):
            archive.write(path, path.name)
    return buffer.getvalue()


def render_login_form(state_path: Path, error: str = "") -> str:
    name = session_display_name(state_path)
    status = notice("ok", f"Saat ini masuk sebagai <strong>{escape(name)}</strong>.") if name else ""
    error_html = notice("error", escape(error)) if error else ""
    body = f"""
<div class="login">
{page_head("Masuk", "Pakai akun INDAH (indah.bps.go.id). Login berlaku untuk browser ini saja.")}
</div>
<form class="panel login" action="/login" method="post" data-busy-form>
  <div class="section">
    {status}
    {error_html}
    <div class="field">
      <label for="login-username">Username</label>
      <input id="login-username" class="input" type="text" name="username" autocomplete="username" required>
    </div>
    <div class="field">
      <label for="login-password">Password</label>
      <div class="input-row">
        <input id="login-password" class="input" type="password" name="password" autocomplete="current-password" required>
        <button id="toggle-password" class="btn btn-secondary btn-icon" type="button" aria-pressed="false" aria-label="Lihat password">{ICON_EYE}</button>
      </div>
    </div>
    <button class="btn btn-primary" type="submit" data-busy="Masuk ke INDAH...">Masuk</button>
    <p class="small muted">Password langsung diteruskan ke API resmi INDAH dan tidak disimpan.</p>
  </div>
</form>
"""
    return page("Masuk", body)


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
    REQUEST.user = str(display_name)
    body = f"""
{page_head("Berhasil masuk")}
{notice("ok", f"Kamu masuk sebagai <strong>{escape(display_name)}</strong>. Sesi ini hanya dipakai oleh browser ini.")}
<div class="actions"><a class="btn btn-primary" href="/">Lanjut upload metadata</a></div>
"""
    return page("Berhasil masuk", body)


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


def session_owner(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def write_run_owner(run_dir: Path, state_path: Path) -> None:
    (run_dir / "owner").write_text(session_owner(state_path.stem), encoding="utf-8")


def run_owned_by(run_dir: Path, owner: str) -> bool:
    try:
        stored = (run_dir / "owner").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return secrets.compare_digest(stored, owner)


def run_state_for(state_path: Path) -> Path:
    try:
        auth = load_auth(state_path)
    except (IndahError, OSError, ValueError):
        raise IndahError("Sesi login belum ada atau sudah berakhir. Login INDAH dulu, lalu coba kembali.") from None
    return user_run_state_dir(auth.user_payload.get("username") or auth.user_raw.get("username"))


def mark_interrupted_runs() -> int:
    """Run yang masih 'berjalan' saat aplikasi start pasti terputus oleh restart."""
    marked = 0
    if not UI_RUNS_ROOT.exists():
        return marked
    for run_dir in UI_RUNS_ROOT.iterdir():
        if not run_dir.is_dir() or (run_dir / "error.txt").exists() or (run_dir / "done").exists():
            continue
        try:
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if summary.get("returncode") is None:
            (run_dir / "error.txt").write_text(INTERRUPTED_MESSAGE, encoding="utf-8")
            marked += 1
    return marked


def prune_old_data(now: Optional[float] = None) -> Dict[str, int]:
    now = time.time() if now is None else now
    removed = {"sessions": 0, "runs": 0, "uploads": 0}
    if UI_SESSION_ROOT.exists():
        for session_file in UI_SESSION_ROOT.glob("*.json"):
            if now - session_file.stat().st_mtime > SESSION_MAX_AGE:
                session_file.unlink(missing_ok=True)
                removed["sessions"] += 1
    cutoff = now - RUN_RETENTION_DAYS * 24 * 60 * 60
    for key, root in (("runs", UI_RUNS_ROOT), ("uploads", UPLOAD_ROOT)):
        if not root.exists():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed[key] += 1
    return removed


def maintenance_loop() -> None:
    while True:
        try:
            removed = prune_old_data()
            if any(removed.values()):
                print(f"Pembersihan data lama: {removed}")
        except Exception as exc:
            print(f"Pembersihan data lama gagal: {exc}")
        time.sleep(MAINTENANCE_INTERVAL_SECONDS)


def user_state_path(session_id: str) -> Path:
    return UI_SESSION_ROOT / f"{session_id}.json"




def handle_phase_run(phase: str, handler: BaseHTTPRequestHandler, state_path: Path) -> str:
    config = PHASES[phase]
    run_id = f"{phase}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = UI_RUNS_ROOT / run_id
    raw_dir = UPLOAD_ROOT / run_id / "raw"
    prepared_dir = UPLOAD_ROOT / run_id / "prepared"
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_run_owner(run_dir, state_path)

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
            json.dumps({"run_id": run_id, "command": command, "returncode": None, "prepared": prepared, "started_at": time.time()}, indent=2, ensure_ascii=False),
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
    write_run_owner(run_dir, state_path)
    try:
        form = parse_multipart(handler)
        cleanup_title = form_text(form, "cleanup_title")
        cleanup_ms_keg_id = form_text(form, "cleanup_ms_keg_id")
        if not cleanup_title and not cleanup_ms_keg_id:
            raise IndahError("Isi judul atau nomor kegiatan yang datanya mau dihapus.")
        if cleanup_ms_keg_id and not cleanup_ms_keg_id.isdigit():
            raise IndahError("Nomor kegiatan harus berupa angka.")
        command = [
            sys.executable,
            "-u",
            str(PROJECT_ROOT / "indah_cleanup.py"),
            "--phase",
            target,
            "--state",
            str(state_path),
            "--run-state",
            str(run_state_for(state_path)),
            "--execute",
        ]
        if cleanup_title:
            command.extend(["--title", cleanup_title])
        if cleanup_ms_keg_id:
            command.extend(["--ms-keg-id", cleanup_ms_keg_id])
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "command": command, "returncode": None, "prepared": {}, "started_at": time.time()}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        thread = threading.Thread(
            target=run_phase_background,
            args=(run_id, "cleanup", {}, command, run_dir),
            daemon=True,
        )
        thread.start()
        return render_run_progress(run_id, "cleanup")
    except Exception as exc:
        (run_dir / "error.txt").write_text(str(exc), encoding="utf-8")
        return render_error(run_id, str(exc))


class UploadedFile:
    def __init__(self, name: str, filename: str, data: bytes):
        self.name = name
        self.filename = filename
        self.file = io.BytesIO(data)


class FormData:
    """Parser form minimal pengganti FieldStorage (modul lama itu dihapus di Python 3.13)."""

    def __init__(self) -> None:
        self._fields: Dict[str, List[object]] = {}

    def add(self, name: str, value: object) -> None:
        self._fields.setdefault(name, []).append(value)

    def __contains__(self, name: object) -> bool:
        return name in self._fields

    def __getitem__(self, name: str) -> object:
        values = self._fields[name]
        return values if len(values) > 1 else values[0]

    def getfirst(self, name: str, default: object = None) -> object:
        for value in self._fields.get(name, []):
            if isinstance(value, str):
                return value
        return default


def request_too_large(headers) -> bool:
    try:
        return int(headers.get("Content-Length") or 0) > MAX_REQUEST_BYTES
    except ValueError:
        return True


def parse_multipart(handler: BaseHTTPRequestHandler) -> FormData:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise IndahError("Permintaan tidak valid.") from None
    if length < 0:
        raise IndahError("Permintaan tidak valid.")
    if length > MAX_REQUEST_BYTES:
        raise IndahError(f"Ukuran unggahan melebihi batas {MAX_REQUEST_BYTES // (1024 * 1024)} MB.")
    body = handler.rfile.read(length) if length else b""
    return parse_form_body(handler.headers.get("Content-Type", ""), body)


def parse_form_body(content_type: str, body: bytes) -> FormData:
    form = FormData()
    if not content_type.lower().startswith("multipart/form-data"):
        for name, values in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items():
            for value in values:
                form.add(name, value)
        return form

    header = email.message.Message()
    header["Content-Type"] = content_type
    boundary = header.get_boundary()
    if not boundary:
        raise IndahError("Form upload tidak valid.")
    delimiter = b"--" + boundary.encode("latin-1")
    for section in body.split(delimiter)[1:]:
        if section.startswith(b"--"):
            break
        if section.startswith(b"\r\n"):
            section = section[2:]
        raw_headers, separator, content = section.partition(b"\r\n\r\n")
        if not separator:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        part = email.message_from_string(raw_headers.decode("utf-8", errors="replace") + "\r\n\r\n")
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        name = email.utils.collapse_rfc2231_value(name)
        filename = part.get_filename()
        if filename is not None:
            form.add(name, UploadedFile(name, filename, content))
        else:
            form.add(name, content.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return form


def save_specific_files(form: FormData, raw_dir: Path, slugs: List[str]) -> Dict[str, Path]:
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


def save_workbook_files(form: FormData, raw_dir: Path) -> List[Path]:
    item = first_file_item(form, "workbook_file")
    if item is None:
        return []
    path = raw_dir / f"workbook__{safe_filename(item.filename)}"
    with path.open("wb") as output:
        output.write(item.file.read())
    return [path]


def first_file_item(form: FormData, name: str) -> Optional[UploadedFile]:
    if name not in form:
        return None
    value = form[name]
    items = value if isinstance(value, list) else [value]
    for item in items:
        if getattr(item, "filename", None):
            return item
    return None


def form_text(form: FormData, name: str) -> str:
    value = form.getfirst(name, "")
    return clean(value if isinstance(value, str) else "") or ""


def build_phase_command(
    phase: str,
    config: Dict[str, object],
    prepared_dir: Path,
    form: FormData,
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
        str(run_state_for(state_path)),
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
    if limit and not limit.isdigit():
        raise IndahError("Batas jumlah data harus berupa angka bulat.")
    if limit and int(limit) > 0:
        command.extend(["--limit", str(int(limit))])
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
    timeline_path = run_dir / "timeline.jsonl"

    def drain(stream, path: Path, parts: List[str], timeline: Optional[Path] = None) -> None:
        timeline_file = timeline.open("a", encoding="utf-8") if timeline else contextlib.nullcontext()
        with path.open("w", encoding="utf-8") as output, timeline_file as stamps:
            for line in stream or []:
                parts.append(line)
                output.write(line)
                output.flush()
                if stamps is not None and (line.startswith("INDAH_PROGRESS ") or line.startswith("[")):
                    stamps.write(json.dumps({"t": round(time.time(), 2), "line": line.rstrip("\n")}, ensure_ascii=False) + "\n")
                    stamps.flush()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_path, stdout_parts, timeline_path)),
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
    started_at = None
    try:
        started_at = json.loads((run_dir / "summary.json").read_text(encoding="utf-8")).get("started_at")
    except (OSError, ValueError):
        pass
    summary = {
        "run_id": run_id,
        "command": command,
        "returncode": result.returncode,
        "prepared": prepared,
        "started_at": started_at,
        "finished_at": time.time(),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")


def friendly_error(message: str) -> str:
    text = message.lower()
    if message.startswith(USER_FACING_PREFIXES):
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


STAGE_ORDER = {"validation": 1, "payload": 2, "upload": 2, "item": 2, "mapping": 3}


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "-"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} jam {minutes} mnt"
    if minutes:
        return f"{minutes} mnt {secs} dtk"
    return f"{secs} dtk"


def fmt_clock(timestamp: Optional[float], with_date: bool = False) -> str:
    if not timestamp:
        return "-"
    moment = datetime.fromtimestamp(timestamp, WIB)
    return moment.strftime("%d %b, %H.%M.%S WIB" if with_date else "%H.%M.%S")


def read_timeline(run_dir: Path) -> List[tuple]:
    path = run_dir / "timeline.jsonl"
    entries = []
    if not path.exists():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("line"), str):
            entries.append((item.get("t"), item["line"]))
    return entries


def run_details(run_dir: Path, stdout: str, summary: Dict[str, object], done: bool, remaining: int,
                now: Optional[float] = None) -> Dict[str, object]:
    now = time.time() if now is None else now
    kind = "cleanup" if run_dir.name.startswith("cleanup") else "upload"
    timeline = read_timeline(run_dir)
    lines = timeline or [(None, line) for line in stdout.splitlines()]

    events = []
    for stamp, line in lines:
        if line.startswith("INDAH_PROGRESS "):
            try:
                event = json.loads(line[len("INDAH_PROGRESS "):])
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append((stamp, event))

    done_label = "Terhapus" if kind == "cleanup" else "Tersimpan"
    items = []
    for stamp, event in events:
        if event.get("stage") != "item":
            continue
        outcome = str(event.get("outcome") or "")
        message = str(event.get("message") or "")
        items.append({
            "n": event.get("current"),
            "title": str(event.get("title") or "Data"),
            "outcome": outcome,
            "label": {"success": done_label, "failed": "Gagal", "skipped": "Dilewati"}.get(outcome, outcome or "-"),
            "message": friendly_error(message) if message and outcome != "success" else "",
            "time": fmt_clock(stamp),
        })

    current = next((event for _, event in reversed(events) if event.get("stage") != "item"), events[-1][1] if events else {})
    stage = str(current.get("stage") or "")
    stage = "upload" if stage in {"payload", "item"} else stage
    index = current.get("current") or 0
    total = current.get("total") or 0
    action = "Menghapus" if kind == "cleanup" else "Mengirim"
    if done:
        now_text = "Proses selesai"
    elif not events:
        now_text = "Membaca file dan menyiapkan proses"
    elif stage == "validation":
        now_text = f"Memeriksa data {index} dari {total}" if index else "Memeriksa isian file"
    elif stage == "upload":
        now_text = f"{action} data {index} dari {total}"
    elif stage == "mapping":
        now_text = "Mencatat hasil upload"
    else:
        now_text = "Memproses"
    now_title = "" if done else str(current.get("title") or "")

    started = summary.get("started_at") or (timeline[0][0] if timeline else None)
    finished = summary.get("finished_at")
    if done and not finished and timeline:
        finished = timeline[-1][0]
    elapsed = ((finished if done else now) - started) if started and (finished or not done) else None

    upload_starts = [stamp for stamp, event in events if stamp and event.get("stage") in {"payload", "upload"}]
    first_upload = upload_starts[0] if upload_starts else None
    finished_items = [stamp for stamp, event in events
                      if stamp and event.get("stage") == "item" and first_upload and stamp >= first_upload]
    average = (finished_items[-1] - first_upload) / len(finished_items) if finished_items and first_upload else None
    eta = average * remaining if (average is not None and not done) else None

    retry = ""
    aborted = ""
    warnings = []
    for stamp, line in lines:
        if line.startswith("[retry") and not done and (stamp is None or now - stamp < 45):
            retry = "INDAH lambat merespons. Aplikasi mencoba mengirim ulang otomatis, mohon tunggu."
        elif line.startswith("[ok]") or line.startswith("INDAH_PROGRESS "):
            retry = ""
        if line.startswith("[abort]"):
            aborted = ("Upload dibatalkan karena ada data yang tidak lolos pemeriksaan. Tidak ada data yang "
                       "dikirim ke INDAH. Perbaiki data yang dilewati, lalu upload ulang.")
        if line.startswith("[warn]"):
            text = line[len("[warn]"):].strip()
            if text not in warnings:
                warnings.append(text)

    return {
        "kind": kind,
        "stage": stage,
        "stage_index": 4 if done else STAGE_ORDER.get(stage, 0),
        "now_text": now_text,
        "now_title": now_title,
        "items": items[-1000:],
        "elapsed_text": fmt_duration(elapsed),
        "avg_text": (fmt_duration(average) + " / data") if average is not None else "-",
        "eta_text": (fmt_duration(eta) if eta is not None else ("Menghitung..." if not done else "-")),
        "finish_text": fmt_clock(now + eta) if eta is not None else ("-" if not done else fmt_clock(finished)),
        "started_text": fmt_clock(started, with_date=True),
        "finished_text": fmt_clock(finished, with_date=True) if done else "-",
        "retry": retry,
        "aborted": aborted,
        "warnings": warnings[:10],
    }


def render_items_rows(items: List[Dict[str, object]]) -> str:
    if not items:
        return '<tr class="empty-row"><td colspan="5">Belum ada data yang selesai diproses.</td></tr>'
    pill = {"success": "ok", "failed": "bad", "skipped": "warn"}
    rows = []
    for item in reversed(items):
        outcome = str(item.get("outcome"))
        rows.append(
            f'<tr class="{"is-ok" if outcome == "success" else "is-problem"}">'
            f'<td class="num">{escape(item.get("n") or "")}</td>'
            f'<td>{escape(item.get("title"))}</td>'
            f'<td><span class="pill pill-{pill.get(outcome, "warn")}">{escape(item.get("label"))}</span></td>'
            f'<td>{escape(item.get("message") or "")}</td>'
            f'<td class="time">{escape(item.get("time") or "")}</td></tr>'
        )
    return "".join(rows)


def render_items_table(items: List[Dict[str, object]]) -> str:
    problems = sum(1 for item in items if item.get("outcome") != "success")
    return f"""
  <div class="section">
    <div class="section-head">
      <h2 class="step">Rincian per data</h2>
      <label class="check small"><input type="checkbox" id="only-problems"> Hanya yang bermasalah (<span id="problem-count">{problems}</span>)</label>
    </div>
    <div class="table-wrap">
      <table class="items" id="items">
        <thead><tr><th>No</th><th>Data</th><th>Status</th><th>Keterangan</th><th>Waktu</th></tr></thead>
        <tbody>{render_items_rows(items)}</tbody>
      </table>
    </div>
  </div>"""


def render_times(details: Dict[str, object], done: bool) -> str:
    if done:
        cells = (("Mulai", details.get("started_text")), ("Selesai", details.get("finished_text")),
                 ("Lama proses", details.get("elapsed_text")), ("Rata-rata", details.get("avg_text")))
    else:
        cells = (("Mulai", details.get("started_text")), ("Rata-rata", details.get("avg_text")),
                 ("Perkiraan sisa waktu", details.get("eta_text")), ("Perkiraan selesai", details.get("finish_text")))
    ids = ("t-start", "t-avg", "t-eta", "t-finish") if not done else ("", "", "", "")
    parts = []
    for (label, value), ident in zip(cells, ids):
        id_attr = f' id="{ident}"' if ident else ""
        parts.append(f"<div><dt>{escape(label)}</dt><dd{id_attr}>{escape(value or '-')}</dd></div>")
    return f'<div class="section"><dl class="times">{"".join(parts)}</dl></div>'


def run_status(run_id: str, owner: str) -> Dict[str, object]:
    safe_id = safe_filename(run_id)
    run_dir = UI_RUNS_ROOT / safe_id
    if not safe_id or not run_dir.is_dir() or not run_owned_by(run_dir, owner):
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
    result = upload_summary(stdout, stderr, summary.get("returncode"), error)
    result.update(run_details(run_dir, stdout, summary, bool(result["done"]), int(result["remaining"])))
    return result


CLOSE_NOTE = 'Proses berjalan di server, jadi halaman ini boleh ditutup. Hasilnya bisa dibuka lagi lewat <a href="/riwayat">Riwayat upload</a>.'


def render_run_progress(run_id: str, phase: str) -> str:
    cleanup = phase not in PHASES
    heading = "Penghapusan data berjalan" if cleanup else f"Mengunggah {PHASE_INFO[phase][0].lower()}"
    action_label = "Menghapus dari INDAH" if cleanup else "Mengirim ke INDAH"
    stats = "".join(
        f'<div class="stat {kind}"><b id="{ident}">0</b><span>{label}</span></div>'
        for ident, label, kind in (
            ("s-ok", "Terhapus" if cleanup else "Tersimpan", "ok"),
            ("s-fail", "Gagal", "bad"),
            ("s-skip", "Dilewati", "warn"),
            ("s-left", "Sisa", ""),
        )
    )
    empty_details = {"started_text": "-", "avg_text": "-", "eta_text": "Menghitung...", "finish_text": "-"}
    body = f"""
{page_head(heading, eyebrow="Proses berjalan")}
<section class="panel" id="progress" data-run-id="{escape(run_id)}" data-heading="{escape(heading)}">
  <div class="section">
    <div class="section-head">
      <span class="status status-run" id="status-pill">Sedang berjalan</span>
      <span class="small muted">Sudah berjalan <strong id="t-elapsed">0 dtk</strong></span>
    </div>
    <ol class="stages" id="stages" aria-label="Tahap proses">
      <li data-stage="0">Membaca file</li>
      <li data-stage="1">Memeriksa isian</li>
      <li data-stage="2">{action_label}</li>
      <li data-stage="3">Mencatat hasil</li>
    </ol>
    <div class="now" aria-live="polite">
      <span class="now-label" id="now-text">Membaca file dan menyiapkan proses</span>
      <strong class="now-title" id="now-title"></strong>
    </div>
    <div class="progress-head"><span id="progress-label">0 dari 0 data diproses</span><strong id="pct">0%</strong></div>
    <div class="progress-track" role="progressbar" aria-label="Kemajuan proses" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar" id="bar"></div></div>
    <div id="retry" class="notice notice-warn" role="status" hidden>{ICON_WARN}<span></span></div>
    <div id="abort" class="notice notice-error" role="alert" hidden>{ICON_WARN}<span></span></div>
    <div id="warnings" class="notice notice-warn" hidden>{ICON_WARN}<span></span></div>
  </div>
  <div class="stats">{stats}</div>
  {render_times(empty_details, False)}
  {render_items_table([])}
  <div class="section">
    {notice("ok", CLOSE_NOTE)}
    <p class="small muted">Nomor proses <code>{escape(run_id)}</code></p>
  </div>
</section>
"""
    return page("Proses berjalan", body)


def render_run_result(
    run_id: str,
    phase: str,
    prepared: Dict[str, Dict[str, str]],
    command: List[str],
    returncode: int,
    stdout: str,
    stderr: str,
    details: Optional[Dict[str, object]] = None,
) -> str:
    result = upload_summary(stdout, stderr, returncode)
    status = str(result["status"])
    title = PHASE_INFO[phase][0].lower() if phase in PHASE_INFO else "data"
    heading = {
        "Selesai": f"Upload {title} selesai",
        "Selesai dengan masalah": f"Upload {title} selesai, perlu dicek",
        "Terhenti": f"Upload {title} terhenti",
        "Tidak ada data yang diproses": "Tidak ada data yang diproses",
    }.get(status, f"Proses {title} selesai")
    if phase not in PHASE_INFO:
        heading = "Penghapusan selesai" if status == "Selesai" else "Penghapusan perlu dicek"
    problems = int(result["failed"]) + int(result["skipped"]) + int(result["remaining"])
    warning = ""
    if problems or status == "Terhenti":
        warning = notice("warn", "<strong>Ada data yang belum tersimpan.</strong> Cek di INDAH dulu sebelum upload ulang, supaya tidak dobel.")
    elif status == "Selesai":
        warning = notice("ok", "Semua data berhasil diproses.")
    stats = "".join(
        f'<div class="stat {kind}"><b>{value}</b><span>{label}</span></div>'
        for value, label, kind in (
            (result["success"], "Tersimpan", "ok"),
            (result["failed"], "Gagal", "bad" if result["failed"] else ""),
            (result["skipped"], "Dilewati", "warn" if result["skipped"] else ""),
            (result["remaining"], "Belum diproses", "warn" if result["remaining"] else ""),
        )
    )
    if phase not in PHASE_INFO:
        stats = stats.replace("<span>Tersimpan</span>", "<span>Terhapus</span>")
    again = f"/?phase={phase}" if phase in PHASES else "/hapus"
    body = f"""
{page_head(heading, eyebrow=PHASE_INFO[phase][0] if phase in PHASE_INFO else "Hapus data")}
<section class="panel">
  <div class="section">
    <span class="status status-{status_class(status)}">{escape(status)}</span>
    <div class="progress-head"><span>{result['success'] + result['failed'] + result['skipped']} dari {result['total']} data diproses</span><strong>{result['percent']}%</strong></div>
    <div class="progress-track"><div class="progress-bar" style="width: {result['percent']}%"></div></div>
  </div>
  <div class="stats">{stats}</div>
  {render_times(details, True) if details else ""}
  {render_items_table(details["items"]) if details and details.get("items") else ""}
  <div class="section">
    {warning}
    {notice("error", escape(details["aborted"])) if details and details.get("aborted") else ""}
    <details{" open" if problems or status == "Terhenti" else ""}>
      <summary>Kesimpulan</summary>
      <pre class="log">{escape(result['output'])}</pre>
    </details>
    <div class="submit-row">
      <span class="small muted">Nomor proses <code>{escape(run_id)}</code></span>
      <div class="actions">
        <a class="btn btn-secondary" href="{again}">{"Upload lagi" if phase in PHASES else "Hapus data lain"}</a>
        <a class="btn btn-primary" href="{WEB_URL}" target="_blank" rel="noopener">Buka di INDAH</a>
      </div>
    </div>
  </div>
</section>
"""
    return page("Hasil proses", body)


def render_error(run_id: str, message: str) -> str:
    body = f"""
{page_head("Proses belum berhasil")}
<section class="panel">
  <div class="section">
    {notice("error", escape(friendly_error(message)))}
    <div class="submit-row">
      <span class="small muted">Nomor proses <code>{escape(run_id)}</code></span>
      <div class="actions"><a class="btn btn-primary" href="/">Kembali ke upload</a></div>
    </div>
  </div>
</section>
"""
    return page("Proses belum berhasil", body)


def render_saved_run(run_id: str, owner: str) -> str:
    safe_id = safe_filename(run_id)
    run_dir = UI_RUNS_ROOT / safe_id
    if not safe_id or not run_dir.is_dir() or not run_owned_by(run_dir, owner):
        return render_error(safe_id or "-", "Run tidak ditemukan.")
    error_path = run_dir / "error.txt"
    if error_path.exists():
        result = run_status(safe_id, owner)
        body = f"""
{page_head("Proses terhenti", eyebrow=run_title(safe_id))}
<section class="panel">
  <div class="section">
    <span class="status status-bad">Terhenti</span>
    {notice("error", "Proses berhenti sebelum selesai. Cek di INDAH sebelum mengulang, supaya tidak dobel.")}
    <details open><summary>Kesimpulan</summary><pre class="log">{escape(result["output"])}</pre></details>
    <div class="submit-row">
      <span class="small muted">Nomor proses <code>{escape(safe_id)}</code></span>
      <div class="actions"><a class="btn btn-primary" href="/">Kembali ke upload</a></div>
    </div>
  </div>
</section>
"""
        return page("Proses terhenti", body)
    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return render_error(safe_id, "Run tidak ditemukan.")
    if summary.get("returncode") is None:
        phase = safe_id.split("-", 1)[0]
        return render_run_progress(safe_id, phase)
    stdout = (run_dir / "stdout.txt").read_text(encoding="utf-8") if (run_dir / "stdout.txt").exists() else ""
    stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8") if (run_dir / "stderr.txt").exists() else ""
    details = run_details(run_dir, stdout, summary, True, 0)
    return render_run_result(safe_id, safe_id.split("-", 1)[0], summary.get("prepared") or {},
                             summary.get("command") or [], summary["returncode"], stdout, stderr, details)






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

    interrupted = mark_interrupted_runs()
    if interrupted:
        print(f"{interrupted} proses yang terputus oleh restart ditandai.")
    threading.Thread(target=maintenance_loop, daemon=True).start()
    port = find_available_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), IndahUIHandler)
    print(f"INDAH UI aktif: http://{args.host}:{port}")
    print("Tekan Ctrl+C untuk stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
