from __future__ import annotations

import argparse
import cgi
import html
import json
import socket
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from indah_automation.shared import DEFAULT_RUN_STATE_DIR, DEFAULT_STATE_PATH, IndahError, PROJECT_ROOT, clean
from indah_automation.spreadsheet_io import (
    download_spreadsheet_url,
    prepare_phase_sources,
    safe_filename,
)


UPLOAD_ROOT = PROJECT_ROOT / "ui_uploads"
UI_RUNS_ROOT = PROJECT_ROOT / "ui_runs"

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


CSS = """
:root {
  color-scheme: light;
  --ink: #17202a;
  --muted: #5f6c7b;
  --line: #d9e2ec;
  --panel: #ffffff;
  --band: #f6f8fb;
  --green: #0d7c66;
  --red: #b42318;
  --blue: #1e5aa8;
  --amber: #8a5b00;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #eef3f7;
  color: var(--ink);
}
header {
  background: #ffffff;
  border-bottom: 1px solid var(--line);
  padding: 18px 28px;
}
.wrap {
  width: min(1180px, calc(100vw - 32px));
  margin: 0 auto;
}
.topline {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}
h1 {
  font-size: 22px;
  margin: 0;
  letter-spacing: 0;
}
h2 {
  font-size: 18px;
  margin: 0 0 14px;
  letter-spacing: 0;
}
h3 {
  font-size: 15px;
  margin: 0 0 10px;
  letter-spacing: 0;
}
main {
  padding: 22px 0 40px;
}
.statusbar {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 0 0 18px;
}
.status {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  min-height: 64px;
}
.status strong {
  display: block;
  font-size: 13px;
  margin-bottom: 4px;
}
.status span {
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.band {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 16px;
}
.phase-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 12px;
  margin-bottom: 16px;
}
.phase-number {
  font-size: 12px;
  font-weight: 700;
  color: var(--blue);
}
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
}
.field {
  display: grid;
  gap: 6px;
}
.field span,
.check span {
  font-size: 13px;
  font-weight: 650;
}
.field small {
  color: var(--muted);
  font-size: 12px;
}
input[type="file"],
input[type="text"],
input[type="url"],
input[type="number"] {
  width: 100%;
  min-height: 38px;
  border: 1px solid #c6d3df;
  border-radius: 6px;
  padding: 8px 10px;
  background: #ffffff;
  color: var(--ink);
  font-size: 13px;
}
.input-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 16px;
  font-size: 13px;
}
.input-table th,
.input-table td {
  border-bottom: 1px solid var(--line);
  padding: 10px 8px;
  text-align: left;
  vertical-align: middle;
}
.input-table th {
  color: var(--muted);
  font-weight: 650;
}
.req {
  color: var(--red);
  font-weight: 700;
}
.opt {
  color: var(--muted);
}
.controls {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(120px, 0.6fr) auto auto auto;
  gap: 12px;
  align-items: end;
  margin-top: 16px;
}
.check {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
}
button,
.button {
  appearance: none;
  border: 1px solid #164985;
  background: var(--blue);
  color: #ffffff;
  border-radius: 6px;
  padding: 10px 14px;
  min-height: 40px;
  font-weight: 700;
  font-size: 13px;
  cursor: pointer;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  white-space: nowrap;
}
button.secondary,
.button.secondary {
  background: #ffffff;
  color: var(--blue);
}
button.danger,
.button.danger {
  background: var(--red);
  border-color: #8f1d14;
}
button:disabled {
  cursor: wait;
  opacity: 0.72;
}
.cleanup-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.result {
  display: grid;
  gap: 14px;
}
.loading-overlay {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: none;
  align-items: center;
  justify-content: center;
  background: rgba(238, 243, 247, 0.82);
  backdrop-filter: blur(2px);
}
.loading-overlay.is-visible {
  display: flex;
}
.loading-box {
  width: min(360px, calc(100vw - 32px));
  background: #ffffff;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 16px 40px rgba(23, 32, 42, 0.16);
  display: grid;
  justify-items: center;
  gap: 12px;
  text-align: center;
}
.spinner {
  width: 34px;
  height: 34px;
  border-radius: 999px;
  border: 4px solid #d9e2ec;
  border-top-color: var(--blue);
  animation: spin 0.8s linear infinite;
}
.loading-title {
  font-weight: 750;
  font-size: 15px;
}
.loading-subtitle {
  color: var(--muted);
  font-size: 12px;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
pre {
  margin: 0;
  padding: 14px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  background: #111827;
  color: #f8fafc;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.45;
}
.ok { color: var(--green); }
.bad { color: var(--red); }
.warn { color: var(--amber); }
.status .ok { color: var(--green); }
.status .bad { color: var(--red); }
.status .warn { color: var(--amber); }
.meta-list {
  display: grid;
  gap: 6px;
  font-size: 13px;
}
.meta-list div {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 12px;
}
.meta-list span:first-child {
  color: var(--muted);
}
@media (max-width: 860px) {
  .topline,
  .phase-head {
    display: grid;
  }
  .statusbar,
  .grid,
  .controls {
    grid-template-columns: 1fr;
  }
  .input-table,
  .input-table tbody,
  .input-table tr,
  .input-table td {
    display: block;
    width: 100%;
  }
  .input-table thead {
    display: none;
  }
  .input-table td {
    border-bottom: 0;
    padding: 8px 0;
  }
  .input-table tr {
    border-bottom: 1px solid var(--line);
    padding: 8px 0;
  }
  .meta-list div {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
"""


class IndahUIHandler(BaseHTTPRequestHandler):
    server_version = "IndahUI/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond_html(render_home())
            return
        if parsed.path == "/run":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [""])[0]
            self.respond_html(render_saved_run(run_id))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            self.respond_html(handle_login_start())
            return
        if parsed.path.startswith("/phase/"):
            phase = parsed.path.rsplit("/", 1)[-1]
            if phase not in PHASES:
                self.send_error(404)
                return
            self.respond_html(handle_phase_run(phase, self))
            return
        if parsed.path.startswith("/cleanup/"):
            target = parsed.path.rsplit("/", 1)[-1]
            if target not in {"variabel", "indikator", "all"}:
                self.send_error(404)
                return
            self.respond_html(handle_cleanup_run(target, self))
            return
        self.send_error(404)

    def respond_html(self, content: str, status: int = 200) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{CSS}</style>
  <script>
    window.addEventListener("DOMContentLoaded", function () {{
      var overlay = document.querySelector(".loading-overlay");
      var title = document.querySelector(".loading-title");
      document.addEventListener("submit", function (event) {{
        var form = event.target;
        if (!form || !form.matches("form")) return;
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
      <div class="loading-subtitle">Mohon tunggu sampai hasilnya muncul.</div>
    </div>
  </div>
  <header>
    <div class="wrap topline">
      <h1>INDAH Metadata Automation</h1>
      <form action="/login" method="post">
        <button class="secondary" type="submit">Buka login manual</button>
      </form>
    </div>
  </header>
  <main>
    <div class="wrap">
      {body}
    </div>
  </main>
</body>
</html>"""


def render_home() -> str:
    session_status = status_text(DEFAULT_STATE_PATH.exists(), "Session login tersedia", "Session login belum ada")
    kegiatan_map = DEFAULT_RUN_STATE_DIR / "kegiatan_map.csv"
    variabel_map = DEFAULT_RUN_STATE_DIR / "variabel_map.csv"
    indikator_map = DEFAULT_RUN_STATE_DIR / "indikator_map.csv"
    statusbar = f"""
<section class="statusbar">
  <div class="status"><strong>Login</strong><span>{session_status}</span></div>
  <div class="status"><strong>Kegiatan Map</strong><span>{map_status(kegiatan_map)}</span></div>
  <div class="status"><strong>Variabel Map</strong><span>{map_status(variabel_map)}</span></div>
  <div class="status"><strong>Indikator Map</strong><span>{map_status(indikator_map)}</span></div>
</section>
"""
    phases = "\n".join(render_phase_form(phase, config) for phase, config in PHASES.items())
    return page("INDAH Metadata Automation", statusbar + phases + render_cleanup_panel())


def render_phase_form(phase: str, config: Dict[str, object]) -> str:
    required = config["required"]
    optional = config["optional"]
    rows = []
    for slug in list(required) + list(optional):
        kind = '<span class="req">Wajib</span>' if slug in required else '<span class="opt">Opsional</span>'
        rows.append(
            f"""<tr>
  <td><code>{escape(slug)}</code></td>
  <td>{kind}</td>
  <td><input type="file" name="file_{escape(slug)}" accept=".csv,.tsv,.xlsx,.xlsm"></td>
</tr>"""
        )
    return f"""
<section class="band">
  <div class="phase-head">
    <div>
      <div class="phase-number">Phase {escape(str(config["number"]))}</div>
      <h2>{escape(str(config["title"]))}</h2>
    </div>
    <div class="phase-number">CSV, XLSX, XLSM, TSV</div>
  </div>
  <form action="/phase/{escape(phase)}" method="post" enctype="multipart/form-data" data-loading-label="Memvalidasi {escape(str(config["title"]))}">
    <div class="grid">
      <label class="field">
        <span>Workbook gabungan</span>
        <input type="file" name="workbook_file" accept=".csv,.tsv,.xlsx,.xlsm">
        <small>Sheet mengikuti nama template phase.</small>
      </label>
      <label class="field">
        <span>URL spreadsheet</span>
        <input type="url" name="spreadsheet_url" placeholder="https://docs.google.com/spreadsheets/d/...">
        <small>Gunakan link yang dapat diakses.</small>
      </label>
    </div>
    <table class="input-table">
      <thead>
        <tr><th>Template</th><th>Status</th><th>File satuan</th></tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <div class="controls">
      <label class="field">
        <span>Judul kegiatan</span>
        <input type="text" name="only_title" placeholder="Kosongkan untuk semua">
      </label>
      <label class="field">
        <span>Limit row</span>
        <input type="number" name="limit" min="0" step="1" placeholder="0">
      </label>
      <label class="check">
        <input type="checkbox" name="verbose" value="1">
        <span>Verbose</span>
      </label>
      <button class="secondary" type="submit" name="submit" value="1" data-loading-label="Menyimpan draft {escape(str(config["title"]))}">Simpan Draft</button>
      <button type="submit" name="final_submit" value="1" data-loading-label="Submit langsung {escape(str(config["title"]))}" data-confirm="Submit langsung ke INDAH? Pastikan isian sudah benar karena status akan dikirim sebagai SUBMITTED/REVISED.">Submit Langsung</button>
    </div>
  </form>
</section>
"""


def render_cleanup_panel() -> str:
    return """
<section class="band">
  <div class="phase-head">
    <div>
      <div class="phase-number">Cleanup</div>
      <h2>Hapus Upload Phase 2 dan 3</h2>
    </div>
    <div class="phase-number">Berdasarkan judul kegiatan</div>
  </div>
  <form action="/cleanup/all" method="post" data-loading-label="Menghapus phase 2 dan 3">
    <div class="grid">
      <label class="field">
        <span>Judul kegiatan</span>
        <input type="text" name="cleanup_title" placeholder="Kosongkan untuk pakai kegiatan_map.csv">
        <small>Semua MS-Variabel/MS-Indikator di bawah judul kegiatan ini akan dihapus.</small>
      </label>
      <label class="field">
        <span>MS-Keg ID</span>
        <input type="text" name="cleanup_ms_keg_id" placeholder="Opsional">
        <small>Isi jika ingin target ID kegiatan tertentu.</small>
      </label>
    </div>
    <div class="cleanup-actions" style="margin-top: 14px;">
      <button class="danger" type="submit" formaction="/cleanup/variabel" data-loading-label="Menghapus MS-Variabel" data-confirm="Hapus semua MS-Variabel untuk judul/MS-Keg ini?">Hapus phase 2</button>
      <button class="danger" type="submit" formaction="/cleanup/indikator" data-loading-label="Menghapus MS-Indikator" data-confirm="Hapus semua MS-Indikator untuk judul/MS-Keg ini?">Hapus phase 3</button>
      <button class="danger" type="submit" formaction="/cleanup/all" data-loading-label="Menghapus phase 2 dan 3" data-confirm="Hapus semua MS-Variabel dan MS-Indikator untuk judul/MS-Keg ini?">Hapus phase 2 dan 3</button>
    </div>
  </form>
</section>
"""


def handle_login_start() -> str:
    UI_RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = f"login-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    log_path = UI_RUNS_ROOT / f"{run_id}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "indah_login.py")],
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()
    body = f"""
<section class="band result">
  <h2>Login Manual</h2>
  <div class="meta-list">
    <div><span>Status</span><span class="warn">Browser login sedang dibuka</span></div>
    <div><span>Log</span><span>{escape(str(log_path))}</span></div>
  </div>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Login Manual", body)


def handle_phase_run(phase: str, handler: BaseHTTPRequestHandler) -> str:
    config = PHASES[phase]
    run_id = f"{phase}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = UI_RUNS_ROOT / run_id
    raw_dir = UPLOAD_ROOT / run_id / "raw"
    prepared_dir = UPLOAD_ROOT / run_id / "prepared"
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        form = parse_multipart(handler)
        specific_files = save_specific_files(form, raw_dir, list(config["required"]) + list(config["optional"]))
        workbook_files = save_workbook_files(form, raw_dir)
        url = form_text(form, "spreadsheet_url")
        if url:
            workbook_files.append(download_spreadsheet_url(url, raw_dir))

        prepared = prepare_phase_sources(
            required_slugs=config["required"],
            optional_slugs=config["optional"],
            specific_files=specific_files,
            workbook_files=workbook_files,
            output_dir=prepared_dir,
        )
        command = build_phase_command(phase, config, prepared_dir, form)
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=3600,
        )
        save_run_artifacts(run_dir, run_id, command, prepared, result)
        return render_run_result(run_id, phase, prepared, command, result.returncode, result.stdout, result.stderr)
    except Exception as exc:
        error_text = str(exc)
        (run_dir / "error.txt").write_text(error_text, encoding="utf-8")
        return render_error(run_id, error_text)


def handle_cleanup_run(target: str, handler: BaseHTTPRequestHandler) -> str:
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
        str(DEFAULT_STATE_PATH),
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


def build_phase_command(phase: str, config: Dict[str, object], prepared_dir: Path, form: cgi.FieldStorage) -> List[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / str(config["script"])),
        "--folder",
        str(prepared_dir),
        "--state",
        str(DEFAULT_STATE_PATH),
        "--run-state",
        str(DEFAULT_RUN_STATE_DIR),
    ]
    only_title = form_text(form, "only_title")
    limit = form_text(form, "limit")
    if only_title:
        command.extend(["--only-title", only_title])
    if limit and limit != "0":
        command.extend(["--limit", limit])
    if form.getfirst("verbose"):
        command.append("--verbose")
    if form.getfirst("submit") or form.getfirst("final_submit"):
        command.append("--submit")
    if form.getfirst("final_submit"):
        command.append("--final-submit")
    return command


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


def render_run_result(
    run_id: str,
    phase: str,
    prepared: Dict[str, Dict[str, str]],
    command: List[str],
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    status = '<span class="ok">Selesai</span>' if returncode == 0 else '<span class="bad">Gagal</span>'
    phase_title = str(PHASES.get(phase, {}).get("title", phase))
    prepared_rows = "".join(
        f"""<div><span>{escape(slug)}</span><span>{escape(info["source"])} / {escape(info["sheet"])} / {escape(info["rows"])} rows</span></div>"""
        for slug, info in prepared.items()
    )
    output = stdout + (("\n\nSTDERR:\n" + stderr) if stderr else "")
    body = f"""
<section class="band result">
  <h2>Hasil {escape(phase_title)}</h2>
  <div class="meta-list">
    <div><span>Run ID</span><span>{escape(run_id)}</span></div>
    <div><span>Status</span><span>{status}</span></div>
    <div><span>Command</span><span><code>{escape(' '.join(command))}</code></span></div>
    {prepared_rows}
  </div>
  <h3>Output</h3>
  <pre>{escape(output or "(tidak ada output)")}</pre>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Hasil Phase", body)


def render_error(run_id: str, message: str) -> str:
    body = f"""
<section class="band result">
  <h2>Error</h2>
  <div class="meta-list">
    <div><span>Run ID</span><span>{escape(run_id)}</span></div>
    <div><span>Status</span><span class="bad">Gagal</span></div>
  </div>
  <pre>{escape(message)}</pre>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Error", body)


def render_saved_run(run_id: str) -> str:
    safe_id = safe_filename(run_id)
    run_dir = UI_RUNS_ROOT / safe_id
    if not safe_id or not run_dir.exists():
        return render_error(safe_id or "-", "Run tidak ditemukan.")
    summary = (run_dir / "summary.json").read_text(encoding="utf-8")
    stdout = (run_dir / "stdout.txt").read_text(encoding="utf-8") if (run_dir / "stdout.txt").exists() else ""
    stderr = (run_dir / "stderr.txt").read_text(encoding="utf-8") if (run_dir / "stderr.txt").exists() else ""
    output = stdout + (("\n\nSTDERR:\n" + stderr) if stderr else "")
    body = f"""
<section class="band result">
  <h2>Run {escape(safe_id)}</h2>
  <h3>Summary</h3>
  <pre>{escape(summary)}</pre>
  <h3>Output</h3>
  <pre>{escape(output)}</pre>
  <a class="button secondary" href="/">Kembali</a>
</section>
"""
    return page("Run", body)


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
