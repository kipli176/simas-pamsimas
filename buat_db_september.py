# -*- coding: utf-8 -*-
"""Buat database SQLite fresh untuk awal September:
- pelanggan saja (tanpa pencatatan & tagihan lama)
- meteran_awal = meteran_akhir terakhir yang sudah tercatat (posisi meteran terkini)
- tarif & pengaturan abodemen tetap (Rp 3.000, sesuai sistem lama)

Hasil: pamsimas_september.db
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
SUMBER = os.path.join(BASE, "pamsimas.db")
TARGET = os.path.join(BASE, "pamsimas_september.db")

src = sqlite3.connect(SUMBER)
src.row_factory = sqlite3.Row

if os.path.exists(TARGET):
    os.remove(TARGET)
dst = sqlite3.connect(TARGET)
dst.executescript("""
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

# posisi meteran terakhir yang tercatat per pelanggan
last_akhir = {
    r["pelanggan_id"]: r["akhir"]
    for r in src.execute("""
        SELECT pelanggan_id, meteran_akhir AS akhir
        FROM pencatatan
        WHERE (pelanggan_id, periode) IN (
            SELECT pelanggan_id, MAX(periode) FROM pencatatan GROUP BY pelanggan_id
        )
    """)
}

# pelanggan: meteran_awal = pembacaan terakhir (fallback ke nilai lama bila tidak ada riwayat)
n_dipakai_last = 0
for r in src.execute("SELECT * FROM pelanggan ORDER BY id"):
    dst.execute(
        """INSERT INTO pelanggan (nomor_meteran, nama, alamat, rt, rw, golongan_tarif,
                                 meteran_awal, petugas, kontak, aktif)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (r["nomor_meteran"], r["nama"], r["alamat"], r["rt"], r["rw"], r["golongan_tarif"],
         last_akhir.get(r["id"], r["meteran_awal"]), r["petugas"], r["kontak"], r["aktif"]),
    )
    if r["id"] in last_akhir:
        n_dipakai_last += 1

# tarif & pengaturan (abodemen + informasi instansi ikut disalin)
for r in src.execute("SELECT * FROM tarif ORDER BY batas_bawah"):
    dst.execute("INSERT INTO tarif (golongan_tarif, batas_bawah, batas_atas, harga_per_m3) VALUES (?,?,?,?)",
                (r["golongan_tarif"], r["batas_bawah"], r["batas_atas"], r["harga_per_m3"]))
n_set = 0
for r in src.execute("SELECT kunci, nilai FROM pengaturan"):
    dst.execute("INSERT INTO pengaturan (kunci, nilai) VALUES (?, ?)", (r["kunci"], r["nilai"]))
    n_set += 1
if n_set == 0:
    dst.execute("INSERT INTO pengaturan (kunci, nilai) VALUES ('abodemen', '3000')")
dst.commit()

# verifikasi
n_pel = dst.execute("SELECT COUNT(*) c FROM pelanggan").fetchone()[0]
n_catat = dst.execute("SELECT COUNT(*) c FROM pencatatan").fetchone()[0]
n_tag = dst.execute("SELECT COUNT(*) c FROM tagihan").fetchone()[0]
print(f"pelanggan: {n_pel} (meteran_awal dari pembacaan terakhir: {n_dipakai_last})")
print(f"pencatatan: {n_catat} | tagihan: {n_tag} (fresh)")
print(f"pengaturan disalin: {n_set} kunci")
print("contoh:")
for r in dst.execute("SELECT nomor_meteran, nama, meteran_awal FROM pelanggan ORDER BY id LIMIT 3"):
    print("  ", r[0], "|", r[1], "| meteran_awal:", r[2])

src.close()
dst.close()
print(f"\nSelesai -> {TARGET}")
