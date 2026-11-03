# INDAH Metadata Automation

Upload metadata statistik (kegiatan, variabel, indikator) ke INDAH BPS lewat UI web
atau lewat script per jenis dari terminal.

## Pakai lewat UI web

1. Klik **Masuk** dan login dengan akun INDAH. Password hanya diteruskan ke API resmi
   INDAH dan tidak disimpan. Tiap browser punya sesi sendiri di `.indah_session/ui_sessions/`.
2. Pilih jenis metadata: Kegiatan, Variabel, atau Indikator.
3. Unggah file Excel (`.xlsx`/`.xlsm`, nama sheet sesuai template) atau pakai link
   Google Sheets.
4. Pilih tahun laporan, lalu **Simpan sebagai draft** atau **Submit Data**
   (status SUBMITTED). Tahun ini dikirim sebagai `submission_period` dan tidak mengubah
   kolom `tahun` di file. Variabel dan indikator hanya dihubungkan ke kegiatan dengan
   judul dan tahun yang sama.

Sheet wajib: `ms_kegiatan`, `ms_variabel`, atau `ms_indikator`. Sheet tambahan
(wilayah, value domain, pembangun) ikut dipakai kalau ada. Template CSV bisa diunduh
dari tautan **Unduh template** di UI, sumbernya `indah_metadata_csv_templates/`.

Menu lain: **Riwayat upload** (proses dari browser itu), **Hapus data** (hapus variabel
atau indikator dari satu kegiatan), tombol tema terang/gelap, dan **Keluar**.

## Menjalankan

Lokal:

```bash
python3 indah_ui.py            # http://127.0.0.1:8765
```

Docker (dipakai di server):

```bash
docker compose up -d --build   # port 127.0.0.1:8765
```

Container jalan sebagai user `1001` dan me-mount folder project ke `/app`, jadi
`.indah_session/`, `run_state/`, `ui_uploads/`, dan `ui_runs/` tersimpan di folder ini.
Akses publik lewat Cloudflare Tunnel ke `http://localhost:8765`.

### Opsional: login Playwright + noVNC

`docker-compose.vnc.yml` menjalankan browser remote (noVNC, port 6080) untuk login manual
dengan `indah_login.py`. Sesi hasil login disimpan di `.indah_session/storage_state.json`
dan dipakai script terminal. UI web tidak membutuhkan ini.

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
