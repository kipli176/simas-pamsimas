# -*- coding: utf-8 -*-
"""Bangun CSV siap-import untuk SQLite SIMAS dari data mentah MariaDB,
lalu uji impor ke database SQLite terpisah (pamsimas_import.db)."""
import csv
import json
import os
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LAP = os.path.join(BASE, "laporan")
RAW = os.path.join(BASE, "_raw")
OUT = os.path.join(BASE, "import_sqlite")
os.makedirs(OUT, exist_ok=True)


def baca(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# 1) peta id lama -> id baru (urutan import = urutan CSV)
pel = baca(os.path.join(LAP, "pelanggan.csv"))
old_ids = [int(r["id"]) for r in pel]
id_map = {old: baru for baru, old in enumerate(old_ids, start=1)}
print(f"pelanggan: {len(pel)} | id lama min={min(old_ids)} max={max(old_ids)}",
      "| kontigu:" if max(old_ids) == len(pel) else "| ADA GAP:")

# 2) pelanggan & tarif: salin langsung
import shutil
shutil.copy(os.path.join(RAW, "import_pelanggan.csv"), os.path.join(OUT, "pelanggan.csv"))
shutil.copy(os.path.join(RAW, "import_tarif.csv"), os.path.join(OUT, "tarif.csv"))

# 3) pencatatan: petakan pelanggan_id
penc = baca(os.path.join(RAW, "import_pencatatan.csv"))
with open(os.path.join(OUT, "pencatatan.csv"), "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["pelanggan_id", "periode", "meteran_awal", "meteran_akhir", "foto", "alasan", "petugas", "anomali"])
    for r in penc:
        lama = int(r["pelanggan_id"])
        if lama not in id_map:
            continue
        w.writerow([id_map[lama], r["periode"], r["meteran_awal"], r["meteran_akhir"],
                    r["foto"] or "", r["alasan"] or "", r["petugas"] or "", r["anomali"] or "0"])
print(f"pencatatan: {len(penc)} baris")

# 4) rincian per tagihan -> JSON
rinc_map = {}
for r in baca(os.path.join(RAW, "import_rincian.csv")):
    tid = int(r["id_tagihan"])
    atas = r["batas_atas"] or "ke atas"
    blok = f"{r['batas_bawah']}-{atas}" if atas != "ke atas" else f"{r['batas_bawah']}-ke atas"
    rinc_map.setdefault(tid, []).append({
        "blok": blok, "m3": int(r["volume"]), "harga": int(r["harga_per_m3"]),
        "subtotal": int(r["subtotal"]),
    })

# 5) tagihan final dengan JSON rincian + abodemen
tag = baca(os.path.join(RAW, "import_tagihan.csv"))
with open(os.path.join(OUT, "tagihan.csv"), "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["pelanggan_id", "periode", "jenis", "pemakaian_m3", "rincian_tarif",
                "total_tagihan", "abodemen", "status_bayar", "waktu_bayar", "dicatat_oleh"])
    for r in tag:
        lama = int(r["pelanggan_id"])
        if lama not in id_map:
            continue
        ab = int(r["abodemen"] or 0)
        rincian = list(rinc_map.get(int(r["id_tagihan"]), []))
        rincian.append({"blok": "abodemen", "m3": 0, "harga": ab, "subtotal": ab})
        # konversi waktu bayar "2025-01-03 00:51:08" -> "03-01-2025 00:51"
        wb = ""
        if r["waktu_bayar"]:
            try:
                dt = datetime.strptime(r["waktu_bayar"], "%Y-%m-%d %H:%M:%S")
                wb = dt.strftime("%d-%m-%Y %H:%M")
            except ValueError:
                wb = r["waktu_bayar"]
        w.writerow([id_map[lama], r["periode"], r["jenis"], r["pemakaian_m3"],
                    json.dumps(rincian, ensure_ascii=False), r["total_tagihan"], ab,
                    r["status_bayar"], wb, r["dibayar_oleh"] or ""])
print(f"tagihan: {len(tag)} baris")
print(f"CSV siap di folder: {OUT}")
