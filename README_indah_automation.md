# INDAH Metadata Automation

Automation tersedia lewat UI lokal dan script phase terpisah.

## Login dari UI Web

Buka UI, klik **Login INDAH**, lalu masukkan akun INDAH dan kode captcha yang
ditampilkan. Captcha dibuat serta diperiksa di browser, mengikuti perilaku UI
resmi INDAH. Password hanya diteruskan ke API resmi INDAH dan tidak disimpan.

Setiap browser mendapat file session terpisah di `.indah_session/ui_sessions/`,
sehingga proses upload dan cleanup memakai akun browser tersebut dan tidak
berbagi session global dengan pengguna lain.

## UI Lokal

Jalankan:

```bash
cd "Meta Indah 2026"
../venv/bin/python indah_ui.py
```

Buka URL yang muncul di terminal, biasanya:

```text
http://127.0.0.1:8765
```

## Docker Compose

Jalankan UI lewat Docker:

```bash
cd "Meta Indah 2026"
docker compose up --build
```

Buka:

```text
http://127.0.0.1:8765
```

Compose akan mount folder project ke `/app`, jadi file berikut tetap tersimpan di folder lokal:

- `.indah_session/`
- `run_state/`
- `ui_uploads/`
- `ui_runs/`
- `dispora/`

Login Playwright manual tetap tersedia sebagai fallback dari terminal host:

```bash
../venv/bin/python indah_login.py
```

Setelah `.indah_session/storage_state.json` ada, container dapat memakai session itu karena folder project dimount.

## Docker Compose Dengan noVNC

Jalankan UI + browser remote untuk login manual:

```bash
cd "Meta Indah 2026"
docker compose -f docker-compose.vnc.yml up -d --build
```

Buka UI automation:

```text
http://127.0.0.1:8765
```

Buka browser remote noVNC:

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote
```

Kalau memakai Cloudflare Tunnel, arahkan Public Hostname ke service berikut:

- `metadata.domainkamu.com` -> `http://localhost:8765`
- `vnc.domainkamu.com` -> `http://localhost:6080`

Aktifkan Cloudflare Access untuk dua hostname itu supaya UI dan noVNC tidak terbuka publik.
Saat user klik **Buka login manual** di UI, browser login INDAH akan muncul di halaman noVNC dan session tetap tersimpan di `.indah_session/storage_state.json`.

Setiap phase menerima:

- file satuan `.csv`, `.tsv`, `.xlsx`, atau `.xlsm`
- workbook gabungan dengan nama sheet sesuai template, misalnya `ms_kegiatan`
- URL spreadsheet yang bisa diakses publik/diekspor

Pada UI, pilih **Periode Tahun** sebelum menyimpan draft atau submit langsung.
Pilihan ini dikirim sebagai `submission_period` API dan tidak mengubah kolom
`tahun` di dalam file. MS-Variabel dan MS-Indikator hanya akan memakai
MS-Kegiatan induk pada periode yang sama.

Input wajib per phase:

- Kegiatan: `ms_kegiatan`
- Variabel: `ms_variabel`
- Indikator: `ms_indikator`

Input opsional akan ikut dipakai kalau diunggah.

## Script Terpisah

1. `indah_login.py` - login manual sekali dan simpan session.
2. `indah_kegiatan.py` - phase 1, simpan sementara MS-Kegiatan.
3. `indah_variabel.py` - phase 2, simpan sementara MS-Variabel.
4. `indah_indikator.py` - phase 3, simpan sementara MS-Indikator.

## Cara Pakai

Login manual:

```bash
cd "Meta Indah 2026"
../venv/bin/python indah_login.py
```

Dry-run kegiatan:

```bash
../venv/bin/python indah_kegiatan.py --folder "dispora"
```

Submit kegiatan:

```bash
../venv/bin/python indah_kegiatan.py --folder "dispora" --submit
```

Submit langsung kegiatan:

```bash
../venv/bin/python indah_kegiatan.py --folder "dispora" --submit --final-submit
```

Submit variabel:

```bash
../venv/bin/python indah_variabel.py --folder "dispora" --submit
```

Submit indikator:

```bash
../venv/bin/python indah_indikator.py --folder "dispora" --submit
```

## Output State

Script akan membuat folder `run_state/`:

- `run_state/kegiatan_map.csv`
- `run_state/variabel_map.csv`
- `run_state/indikator_map.csv`

Map ini dipakai agar phase variabel/indikator tahu `ms_keg_id` dari kegiatan yang sudah tersimpan.

Upload dan penghapusan dari UI memakai map **per pengguna** di `run_state/users/<username>/`, supaya
data satu akun tidak tercampur dengan akun lain. Saat folder pengguna dibuat pertama kali,
`variabel_map.csv` dan `indikator_map.csv` lama disalin ke sana (kuncinya ID kegiatan, jadi aman).
`kegiatan_map.csv` lama sengaja tidak disalin: kegiatan dicari ulang lewat API memakai akun pengguna.
Script yang dijalankan manual dari terminal tetap memakai `run_state/` kecuali diberi `--run-state`.

## Keamanan dan Perawatan UI

- Link spreadsheet hanya menerima Google Sheets (`https://docs.google.com/spreadsheets/...`).
- Ukuran unggahan maksimal 25 MB per permintaan, dan unduhan Google Sheets maksimal 20 MB.
- Hasil proses (`/run`) hanya bisa dibuka dari browser yang menjalankannya.
- Penghapusan dari UI wajib menyebut judul atau nomor kegiatan. Mode "semua kegiatan di map"
  hanya tersedia lewat CLI (`indah_cleanup.py --from-kegiatan-map`).
- Saat UI start, proses yang terputus oleh restart ditandai, lalu setiap 6 jam sesi login yang lebih
  dari 30 hari dan folder `ui_runs/` serta `ui_uploads/` yang lebih tua dari `INDAH_RETENTION_DAYS`
  (bawaan 90 hari) dihapus otomatis.
- Koneksi ke API INDAH memakai verifikasi sertifikat SSL.

## Catatan

- Tanpa `--submit`, script hanya validasi/dry-run dan tidak mengirim data.
- Dengan `--submit`, script melakukan POST/PUT simpan draft ke API INDAH menggunakan token dari hasil login manual.
- Dengan `--submit --final-submit`, script mengirim status `SUBMITTED` untuk item draft baru dan `REVISED` untuk item lama yang sudah bukan draft.
- CSV boleh berada di subfolder, misalnya `Kegiatan`, `Variabel`, atau `Indikator`; script mencari file secara rekursif berdasarkan nama seperti `ms_kegiatan.csv`.
- UI akan mengonversi input `.xlsx`/`.xlsm` menjadi CSV sementara di folder `ui_uploads/`.
- Kolom `rumus` pada `ms_indikator` dikirim sebagai formula LaTeX. Contoh aman: `\sum X / n`, `\frac{Jumlah}{Total}\times100\%`, `\phi`, `\le`. Simbol umum seperti `Σ`, `φ`, `×`, `≤`, `≥`, `√` akan dikonversi otomatis. Jika simbol berubah menjadi `?`, file CSV sudah kehilangan encoding; upload `.xlsx` atau simpan CSV sebagai UTF-8.
