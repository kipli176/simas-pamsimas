# -*- coding: utf-8 -*-
"""Impor CSV (dari ekspor MariaDB kipli_pam) ke database SQLite SIMAS.

Cara pakai:
    python import_sqlite.py [nama_file_db]

Default target: pamsimas_import.db (TIDAK menimpa pamsimas.db yang aktif).
"""
import csv
import json
import os
import sqlite3
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
DB_TARGET = sys.argv[1] if len(sys.argv) > 1 else os.path.join(DIR, "pamsimas_import.db")

if os.path.exists(DB_TARGET):
    os.remove(DB_TARGET)
    print(f"database lama {DB_TARGET} dihapus")

db = sqlite3.connect(DB_TARGET)
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE pelanggan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nomor_meteran TEXT UNIQUE NOT NULL,
    nama TEXT NOT NULL,
    alamat TEXT,
    rt TEXT,
    rw TEXT,
    golongan_tarif TEXT NOT NULL,
    meteran_awal INTEGER NOT NULL DEFAULT 0,
    petugas TEXT,
    kontak TEXT,
    aktif INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE tarif (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    golongan_tarif TEXT NOT NULL,
    batas_bawah INTEGER NOT NULL,
    batas_atas INTEGER,
    harga_per_m3 INTEGER NOT NULL
);
CREATE TABLE pencatatan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pelanggan_id INTEGER NOT NULL REFERENCES pelanggan(id),
    periode TEXT NOT NULL,
    meteran_awal INTEGER NOT NULL,
    meteran_akhir INTEGER NOT NULL,
    foto TEXT,
    alasan TEXT,
    petugas TEXT,
    anomali INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(pelanggan_id, periode)
);
CREATE TABLE tagihan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pelanggan_id INTEGER NOT NULL REFERENCES pelanggan(id),
    periode TEXT NOT NULL,
    jenis TEXT NOT NULL,
    pemakaian_m3 INTEGER NOT NULL DEFAULT 0,
    rincian_tarif TEXT,
    total_tagihan INTEGER NOT NULL,
    abodemen INTEGER,
    status_bayar TEXT NOT NULL DEFAULT 'belum_bayar',
    waktu_bayar TEXT,
    dicatat_oleh TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(pelanggan_id, periode)
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    waktu TEXT DEFAULT (datetime('now','localtime')),
    petugas TEXT,
    pelanggan_id INTEGER,
    nomor_meteran TEXT,
    periode TEXT,
    meteran_awal INTEGER,
    meteran_akhir INTEGER,
    pemakaian_m3 INTEGER,
    anomali INTEGER DEFAULT 0,
    sumber TEXT,
    keterangan TEXT
);
CREATE TABLE pengaturan (kunci TEXT PRIMARY KEY, nilai TEXT);
""")

def baca(nama):
    with open(os.path.join(DIR, nama), encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

# 1. pelanggan (urutan CSV = urutan id baru; sama dengan id lama via pemetaan saat export)
# meteran_awal memakai nilai asli sistem lama = posisi meteran terakhir yang dibayar (lunas).
pel = baca("pelanggan.csv")
db.executemany(
    """INSERT INTO pelanggan (nomor_meteran, nama, alamat, rt, rw, golongan_tarif,
                             meteran_awal, petugas, kontak, aktif)
       VALUES (?,?,?,?,?,?,?,?,?,?)""",
    [(r["nomor_meteran"], r["nama"], r["alamat"], r["rt"], r["rw"], r["golongan_tarif"],
      int(r["meteran_awal"] or 0), r["petugas"], r["kontak"], int(r["aktif"] or 1)) for r in pel],
)
print(f"pelanggan terimpor: {len(pel)}")

# 2. tarif
tar = baca("tarif.csv")
db.executemany(
    "INSERT INTO tarif (golongan_tarif, batas_bawah, batas_atas, harga_per_m3) VALUES (?,?,?,?)",
    [(r["golongan_tarif"], int(r["batas_bawah"]),
      int(r["batas_atas"]) if r["batas_atas"] else None, int(r["harga_per_m3"])) for r in tar],
)
print(f"tarif terimpor: {len(tar)}")

# 3. pencatatan (baris ekstrem >100 m3 = kemungkinan ganti meter/typo -> ditandai anomali)
penc = baca("pencatatan.csv")
db.executemany(
    """INSERT OR REPLACE INTO pencatatan
       (pelanggan_id, periode, meteran_awal, meteran_akhir, foto, alasan, petugas, anomali)
       VALUES (?,?,?,?,?,?,?,?)""",
    [(int(r["pelanggan_id"]), r["periode"], int(r["meteran_awal"]), int(r["meteran_akhir"]),
      r["foto"],
      "Ganti meter / data tidak wajar" if (int(r["meteran_akhir"]) - int(r["meteran_awal"])) > 100 else r["alasan"],
      r["petugas"],
      1 if (int(r["meteran_akhir"]) - int(r["meteran_awal"])) > 100 else int(r["anomali"] or 0))
     for r in penc],
)
print(f"pencatatan terimpor: {len(penc)}")

# 4. tagihan (rincian_tarif berupa JSON)
tag = baca("tagihan.csv")
db.executemany(
    """INSERT OR REPLACE INTO tagihan
       (pelanggan_id, periode, jenis, pemakaian_m3, rincian_tarif, total_tagihan,
        abodemen, status_bayar, waktu_bayar, dicatat_oleh)
       VALUES (?,?,?,?,?,?,?,?,?,?)""",
    [(int(r["pelanggan_id"]), r["periode"], r["jenis"], int(r["pemakaian_m3"]),
      r["rincian_tarif"], int(r["total_tagihan"]), int(r["abodemen"] or 0),
      r["status_bayar"], r["waktu_bayar"], r["dicatat_oleh"]) for r in tag],
)
db.commit()
print(f"tagihan terimpor: {len(tag)}")

# 5. pengaturan abodemen default
db.execute("INSERT INTO pengaturan (kunci, nilai) VALUES ('abodemen', '3000')")
db.commit()

# ===== verifikasi =====
print("\n=== VERIFIKASI ===")
for tabel in ("pelanggan", "tarif", "pencatatan", "tagihan"):
    n = db.execute(f"SELECT COUNT(*) c FROM {tabel}").fetchone()[0]
    print(f"{tabel}: {n}")
lunas = db.execute("SELECT COUNT(*) c FROM tagihan WHERE status_bayar='lunas'").fetchone()[0]
total = db.execute("SELECT COALESCE(SUM(total_tagihan),0) t FROM tagihan").fetchone()[0]
print(f"tagihan lunas: {lunas} | total nilai tagihan: Rp {total:,}")
# contoh rincian JSON
contoh = db.execute("SELECT rincian_tarif FROM tagihan WHERE rincian_tarif IS NOT NULL LIMIT 1").fetchone()
if contoh:
    print("contoh rincian_tarif:", json.loads(contoh[0])[:2], "...")

db.close()
print(f"\nSelesai -> {DB_TARGET}")
