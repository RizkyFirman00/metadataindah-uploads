import argparse
from pathlib import Path

from indah_automation.shared import (
    DEFAULT_RUN_STATE_DIR,
    DEFAULT_STATE_PATH,
    IndahClient,
    IndahError,
    PROJECT_ROOT,
    append_or_replace_map,
    build_variabel_payload,
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
    rows_for_variabel,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2: input MS-Variabel dari CSV.")
    parser.add_argument("--folder", default=str(PROJECT_ROOT / "dispora"), help="Folder sumber CSV.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Storage state hasil indah_login.py.")
    parser.add_argument("--run-state", default=str(DEFAULT_RUN_STATE_DIR), help="Folder mapping kegiatan/variabel.")
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
    variabel_path = find_csv(folder, "ms_variabel", required=True)
    value_domain_path = find_csv(folder, "ms_variabel_value_domain", required=False)
    variabel_rows = read_csv_rows(variabel_path)
    value_domain_rows = read_csv_rows(value_domain_path) if value_domain_path else []

    if args.only_title:
        variabel_rows = [row for row in variabel_rows if pick(row, "judul_kegiatan") == args.only_title]
    if args.limit:
        variabel_rows = variabel_rows[: args.limit]

    kegiatan_map_path = Path(args.run_state) / "kegiatan_map.csv"
    kegiatan_map = map_rows_by_title(kegiatan_map_path)
    variabel_map_path = Path(args.run_state) / "variabel_map.csv"
    existing_variabel_map = {
        (pick(row, "judul_kegiatan") or "", pick(row, "nama_variabel") or ""): row
        for row in read_csv_rows(variabel_map_path)
    }
    print(f"CSV variabel: {variabel_path}")
    print(f"Rows variabel: {len(variabel_rows)}")

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

    for index, row in enumerate(variabel_rows, 1):
        title = pick(row, "judul_kegiatan") or ""
        name = pick(row, "nama", "variabel_nama") or f"row-{index}"
        errors = draft_name_errors(row, "nama", "variabel_nama")
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
        child_rows = rows_for_variabel(value_domain_rows, title, name)

        if not args.submit:
            source_note = "auto" if ms_keg_source == "auto" else ms_keg_source
            print(f"[dry-run] variabel: {title} / {name} -> ms_keg_id={ms_keg_id} ({source_note})")
            continue

        valid_items.append(
            {
                "row": row,
                "title": title,
                "name": name,
                "child_rows": child_rows,
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
        child_rows = item["child_rows"]
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
        existing_ms_var_id = pick(existing_variabel_map.get((title, name), {}), "ms_var_id")
        existing_status = pick(existing_variabel_map.get((title, name), {}), "status")
        target_status = direct_submit_status(existing_status) if args.final_submit else "DRAFT"
        payload = build_variabel_payload(row, child_rows, ms_keg, auth, status=target_status)
        if args.verbose:
            print_dry_run_payload("variabel-payload", payload, True)
        if existing_ms_var_id:
            result = client.update_variabel(existing_ms_var_id, payload)
            if is_error_result(result):
                print(f"[error] {title} / {name}: {result}")
                continue
            ms_var_id = existing_ms_var_id
            action = "disubmit ulang" if target_status == "REVISED" else "disubmit" if args.final_submit else "diperbarui"
            print(f"[ok] variabel {action}: {title} / {name} -> {ms_var_id}")
            map_rows.append(
                {
                    "judul_kegiatan": title,
                    "nama_variabel": name,
                    "ms_var_id": ms_var_id,
                    "status": response_status(result) or target_status,
                }
            )
            continue

        result = client.create_variabel(payload)
        if is_error_result(result):
            print(f"[error] {title} / {name}: {result}")
            continue
        ms_var_id = extract_created_id(result)
        if not ms_var_id:
            print(f"[error] {title} / {name}: API tidak mengembalikan id variabel. Respons: {result}")
            continue
        action = "tersubmit" if args.final_submit else "tersimpan"
        print(f"[ok] variabel {action}: {title} / {name} -> {ms_var_id}")
        map_rows.append(
            {
                "judul_kegiatan": title,
                "nama_variabel": name,
                "ms_var_id": ms_var_id,
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
            run_state / "variabel_map.csv",
            ["judul_kegiatan", "nama_variabel", "ms_var_id", "status"],
            ["judul_kegiatan", "nama_variabel"],
            map_rows,
        )
        print(f"Map ditulis: {run_state / 'variabel_map.csv'}")


if __name__ == "__main__":
    try:
        main()
    except IndahError as exc:
        raise SystemExit(f"Error: {exc}") from None
