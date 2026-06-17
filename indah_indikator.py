import argparse
from pathlib import Path

from indah_automation.shared import (
    DEFAULT_RUN_STATE_DIR,
    DEFAULT_STATE_PATH,
    IndahClient,
    IndahError,
    PROJECT_ROOT,
    append_or_replace_map,
    build_indikator_payload,
    direct_submit_status,
    draft_name_errors,
    extract_created_id,
    find_csv,
    is_error_result,
    load_auth,
    map_rows_by_title,
    pick,
    print_dry_run_payload,
    read_csv_rows,
    resolve_ms_keg_reference,
    response_status,
    rows_for_indikator,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3: input MS-Indikator dari CSV.")
    parser.add_argument("--folder", default=str(PROJECT_ROOT / "dispora"), help="Folder sumber CSV.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Storage state hasil indah_login.py.")
    parser.add_argument("--run-state", default=str(DEFAULT_RUN_STATE_DIR), help="Folder mapping kegiatan/indikator.")
    parser.add_argument("--submit", action="store_true", help="Benar-benar POST simpan sementara ke INDAH.")
    parser.add_argument("--final-submit", action="store_true", help="POST/PUT ke INDAH dengan status SUBMITTED/REVISED.")
    parser.add_argument("--limit", type=int, default=0, help="Batasi jumlah row untuk test.")
    parser.add_argument("--only-title", help="Proses hanya judul kegiatan tertentu.")
    parser.add_argument("--verbose", action="store_true", help="Print payload saat dry-run.")
    parser.add_argument("--no-auto-resolve-kegiatan", action="store_true", help="Matikan pencarian otomatis MS-Kegiatan dari judul.")
    parser.add_argument("--auto-resolve-max-pages", type=int, default=2, help="Batas halaman scan saat auto-resolve kegiatan.")
    args = parser.parse_args()
    if args.final_submit:
        args.submit = True

    folder = Path(args.folder)
    indikator_path = find_csv(folder, "ms_indikator", required=True)
    indikator_pembangun_path = find_csv(folder, "ms_indikator_pembangun", required=False)
    variabel_pembangun_path = find_csv(folder, "ms_indikator_variabel_pembangun", required=False)
    indikator_rows = read_csv_rows(indikator_path)
    indikator_pembangun_rows = read_csv_rows(indikator_pembangun_path) if indikator_pembangun_path else []
    variabel_pembangun_rows = read_csv_rows(variabel_pembangun_path) if variabel_pembangun_path else []

    if args.only_title:
        indikator_rows = [row for row in indikator_rows if pick(row, "judul_kegiatan") == args.only_title]
    if args.limit:
        indikator_rows = indikator_rows[: args.limit]

    kegiatan_map_path = Path(args.run_state) / "kegiatan_map.csv"
    kegiatan_map = map_rows_by_title(kegiatan_map_path)
    indikator_map_path = Path(args.run_state) / "indikator_map.csv"
    existing_indikator_rows = read_csv_rows(indikator_map_path)
    existing_indikator_by_parent = {
        (
            pick(row, "judul_kegiatan") or "",
            pick(row, "nama_indikator") or "",
            pick(row, "ms_keg_id") or "",
        ): row
        for row in existing_indikator_rows
    }
    print(f"CSV indikator: {indikator_path}")
    print(f"Rows indikator: {len(indikator_rows)}")

    auto_resolve = not args.no_auto_resolve_kegiatan
    auth = None
    if args.submit:
        auth = load_auth(Path(args.state))
    elif auto_resolve:
        try:
            auth = load_auth(Path(args.state))
        except IndahError:
            auth = None
    client = IndahClient(auth.token) if auth else None
    map_rows = []
    kegiatan_map_rows = []
    ms_keg_cache = {}
    valid_items = []
    skipped_count = 0

    for index, row in enumerate(indikator_rows, 1):
        title = pick(row, "judul_kegiatan") or ""
        name = pick(row, "nama", "indikator_nama") or f"row-{index}"
        errors = draft_name_errors(row, "nama", "indikator_nama")
        raw_formula = pick(row, "rumus")
        if raw_formula and "?" in raw_formula:
            print(f"[warn] {title} / {name}: kolom rumus berisi '?'. Jika ini asalnya simbol matematika, simpan file sebagai UTF-8 CSV atau pakai XLSX.")
        if title in ms_keg_cache:
            ms_keg_id, ms_keg_item, ms_keg_source = ms_keg_cache[title]
        else:
            ms_keg_id, ms_keg_item, ms_keg_source = resolve_ms_keg_reference(
                row,
                kegiatan_map,
                client,
                auto_resolve=auto_resolve,
                max_scan_pages=args.auto_resolve_max_pages,
            )
            if title:
                ms_keg_cache[title] = (ms_keg_id, ms_keg_item, ms_keg_source)
            if ms_keg_id and ms_keg_item is not None:
                kegiatan_map[title] = {
                    "judul_kegiatan": title,
                    "ms_keg_id": ms_keg_id,
                    "detail_url": f"https://indah.bps.go.id/metadata/view-kegiatan/{ms_keg_id}",
                    "status": ms_keg_item.get("status") or "",
                }
        if not ms_keg_id:
            errors.append("MS-Kegiatan tidak ditemukan otomatis; jalankan phase kegiatan atau isi ms_keg_id")
        elif ms_keg_source == "stale":
            errors.append("MS-Kegiatan dari ms_keg_id/map tidak bisa diakses; cek ID atau jalankan phase kegiatan ulang")
        if errors:
            skipped_count += 1
            print(f"[skip] {title} / {name}: {', '.join(errors)}")
            continue

        if not args.submit:
            source_note = "auto" if ms_keg_source == "auto" else ms_keg_source
            print(f"[dry-run] indikator: {title} / {name} -> ms_keg_id={ms_keg_id} ({source_note})")
            continue

        valid_items.append(
            {
                "row": row,
                "title": title,
                "name": name,
                "ms_keg_id": ms_keg_id,
                "ms_keg_item": ms_keg_item,
                "ms_keg_source": ms_keg_source,
            }
        )

    if not args.submit:
        return

    if skipped_count:
        print(f"[abort] Submit dibatalkan: ada {skipped_count} row skip. Tidak ada data yang dikirim ke INDAH.")
        raise SystemExit(1)

    for item in valid_items:
        row = item["row"]
        title = item["title"]
        name = item["name"]
        ms_keg_id = item["ms_keg_id"]
        ms_keg_item = item["ms_keg_item"]
        ms_keg_source = item["ms_keg_source"]
        ms_keg = ms_keg_item or client.get_ms_keg(ms_keg_id)
        if not ms_keg:
            print(f"[error] {title} / {name}: MS-Kegiatan {ms_keg_id} tidak bisa diakses.")
            continue
        if ms_keg_source == "auto":
            print(f"[auto] MS-Kegiatan ditemukan: {title} -> {ms_keg_id}")
        kegiatan_map_rows.append(
            {
                "judul_kegiatan": title,
                "ms_keg_id": ms_keg_id,
                "detail_url": f"https://indah.bps.go.id/metadata/view-kegiatan/{ms_keg_id}",
                "status": ms_keg.get("status") or "",
            }
        )
        current_parent_id = str(ms_keg_id)
        existing_indikator_row = existing_indikator_by_parent.get((title, name, current_parent_id), {})
        existing_ms_ind_id = pick(existing_indikator_row, "ms_ind_id")
        existing_status = pick(existing_indikator_row, "status")
        target_status = direct_submit_status(existing_status) if args.final_submit else "DRAFT"
        payload = build_indikator_payload(
            row,
            rows_for_indikator(indikator_pembangun_rows, title, name),
            rows_for_indikator(variabel_pembangun_rows, title, name),
            ms_keg,
            auth,
            status=target_status,
        )
        if args.verbose:
            print_dry_run_payload("indikator-payload", payload, True)
        if existing_ms_ind_id:
            result = client.update_indikator(existing_ms_ind_id, payload)
            if is_error_result(result):
                print(f"[error] {title} / {name}: {result}")
                continue
            ms_ind_id = existing_ms_ind_id
            action = "disubmit ulang" if target_status == "REVISED" else "disubmit" if args.final_submit else "diperbarui"
            print(f"[ok] indikator {action}: {title} / {name} -> {ms_ind_id}")
            map_rows.append(
                {
                    "judul_kegiatan": title,
                    "ms_keg_id": ms_keg_id,
                    "nama_indikator": name,
                    "ms_ind_id": ms_ind_id,
                    "status": response_status(result) or target_status,
                }
            )
            continue

        result = client.create_indikator(payload)
        if is_error_result(result):
            print(f"[error] {title} / {name}: {result}")
            continue
        ms_ind_id = extract_created_id(result)
        if not ms_ind_id:
            print(f"[error] {title} / {name}: API tidak mengembalikan id indikator. Respons: {result}")
            continue
        action = "tersubmit" if args.final_submit else "tersimpan"
        print(f"[ok] indikator {action}: {title} / {name} -> {ms_ind_id}")
        map_rows.append(
            {
                "judul_kegiatan": title,
                "ms_keg_id": ms_keg_id,
                "nama_indikator": name,
                "ms_ind_id": ms_ind_id,
                "status": response_status(result) or target_status,
            }
        )

    if args.submit and kegiatan_map_rows:
        append_or_replace_map(
            kegiatan_map_path,
            ["judul_kegiatan", "ms_keg_id", "detail_url", "status"],
            ["judul_kegiatan"],
            kegiatan_map_rows,
        )
        print(f"Map kegiatan ditulis: {kegiatan_map_path}")

    if args.submit and map_rows:
        run_state = Path(args.run_state)
        append_or_replace_map(
            run_state / "indikator_map.csv",
            ["judul_kegiatan", "ms_keg_id", "nama_indikator", "ms_ind_id", "status"],
            ["judul_kegiatan", "ms_keg_id", "nama_indikator"],
            map_rows,
        )
        print(f"Map ditulis: {run_state / 'indikator_map.csv'}")


if __name__ == "__main__":
    try:
        main()
    except IndahError as exc:
        raise SystemExit(f"Error: {exc}") from None
