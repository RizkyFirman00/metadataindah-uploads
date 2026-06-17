# INDAH Metadata Automation

Automation tersedia lewat UI lokal dan script phase terpisah.

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

Catatan login: login manual dengan browser lebih aman dijalankan dari host seperti biasa:

```bash
../venv/bin/python indah_login.py
```

Setelah `.indah_session/storage_state.json` ada, container dapat memakai session itu karena folder project dimount.

Setiap phase menerima:

- file satuan `.csv`, `.tsv`, `.xlsx`, atau `.xlsm`
- workbook gabungan dengan nama sheet sesuai template, misalnya `ms_kegiatan`
- URL spreadsheet yang bisa diakses publik/diekspor

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

## Catatan

- Tanpa `--submit`, script hanya validasi/dry-run dan tidak mengirim data.
- Dengan `--submit`, script melakukan POST ke API INDAH menggunakan token dari hasil login manual.
- CSV boleh berada di subfolder, misalnya `Kegiatan`, `Variabel`, atau `Indikator`; script mencari file secara rekursif berdasarkan nama seperti `ms_kegiatan.csv`.
- UI akan mengonversi input `.xlsx`/`.xlsm` menjadi CSV sementara di folder `ui_uploads/`.
