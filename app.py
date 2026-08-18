"""
Aplikasi Pengelolaan Tagihan Air Pamsimas — v2
- Backend   : Flask
- Database  : SQLite (mode WAL, dengan index untuk query cepat di skala ratusan pelanggan)
- Frontend  : 1 halaman multi-tab (Bootstrap 5 CDN + custom CSS)

Fitur v2:
- Semua tabel (Pelanggan, Tagihan, Laporan) difilter & dipaginasi di server (bukan JS),
  supaya tetap ringan walau data pelanggan mencapai ribuan baris.
- Tab Laporan dengan filter periode, RT/RW, golongan, jenis, status bayar + export CSV.
- Tab Pencatatan dengan alur bertingkat RW -> RT -> Nama, pencarian cepat, upload foto asli,
  dan deteksi anomali pemakaian (dibanding rata-rata pemakaian 6 bulan terakhir).

Cara jalan:
    pip install flask
    python app.py
lalu buka http://127.0.0.1:5000
"""

import os
import io
import csv
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename
from flask import (
    Flask, g, request, redirect, url_for, render_template,
    flash, Response, send_from_directory, session, jsonify
)

# =========================================================
# KONFIGURASI STATIS
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Path bisa di-override lewat environment variable (dipakai di Docker agar data ada di volume)
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "pamsimas.db"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(BASE_DIR, "static", "uploads"))
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}

ABODEMEN = 25000          # Rp, tarif tetap bulanan (berlaku untuk semua pelanggan setiap bulan)
PAGE_SIZE = 25             # jumlah baris per halaman tabel
ANOMALI_KALI_ATAS = 3.0    # pemakaian > rata2 x ini -> dicurigai anomali (lonjakan)
ANOMALI_KALI_BAWAH = 0.2   # pemakaian < rata2 x ini -> dicurigai anomali (anjlok/macet meteran)
MIN_RIWAYAT_UTK_ANOMALI = 2  # minimal berapa bulan riwayat sebelum deteksi anomali berbasis rata-rata aktif
ANOMALI_ABSOLUT_M3 = 100   # pemakaian di atas ini SELALU dicurigai anomali, walau belum ada riwayat pembanding
FOTO_WAJIB = True          # foto bukti meteran wajib diunggah setiap pencatatan,
# termasuk arsip offline (foto ikut disimpan di IndexedDB perangkat petugas, lalu dikirim bersama data)
PIN_PETUGAS = os.environ.get("PIN_PETUGAS", "123456")  # PIN masuk petugas (sama untuk semua petugas)

# HTTP Basic Auth untuk halaman admin
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "12345678")

# Cloudflare Turnstile untuk halaman login petugas.
# Default di bawah ini dummy key resmi Cloudflare (selalu lolos) untuk tahap uji coba.
# Untuk produksi, ganti lewat environment variable (lihat README) atau ubah
# langsung di sini dengan Site Key & Secret Key asli dari dashboard Cloudflare.
# Kosongkan TURNSTILE_SITE_KEY untuk menonaktifkan captcha.
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "1x0000000000000000000000000000000AA")

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pamsimas-secret-key-ubah-ini")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # maks 5MB per upload foto


# =========================================================
# KEAMANAN: HTTP Basic Auth admin & verifikasi Turnstile
# =========================================================
def perlu_auth_admin(f):
    """HTTP Basic Auth sederhana untuk seluruh halaman & aksi admin."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response(
                "Akses dibatasi. Masukkan username dan password admin.",
                401,
                {"WWW-Authenticate": 'Basic realm="Admin SIMAS", charset="UTF-8"'},
            )
        return f(*args, **kwargs)
    return wrapper


def verifikasi_turnstile(token):
    """Kirim token Turnstile ke Cloudflare untuk diverifikasi. True jika lolos."""
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({
            "secret": TURNSTILE_SECRET_KEY,
            "response": token,
        }).encode()
        req = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            hasil = json.loads(resp.read().decode("utf-8"))
            return bool(hasil.get("success"))
    except Exception:
        return False


# =========================================================
# DATABASE HELPERS (SQLite + WAL)
# =========================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA foreign_keys=ON;")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def periode_sekarang():
    return datetime.now().strftime("%Y-%m")


def periode_label(periode):
    bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
              "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    y, m = periode.split("-")
    return f"{bulan[int(m)]} {y}"


def rupiah(v):
    return "Rp " + "{:,.0f}".format(v or 0).replace(",", ".")


app.jinja_env.filters["rupiah"] = rupiah
app.jinja_env.globals["periode_label"] = periode_label


def page_url(**overrides):
    """Bangun URL ke halaman utama sambil mempertahankan semua filter query-string aktif,
    hanya menimpa parameter yang disebutkan (mis. ganti tab, atau ganti nomor halaman)."""
    args = request.args.to_dict()
    args.update(overrides)
    return url_for("index", **args)


app.jinja_env.globals["page_url"] = page_url


# =========================================================
# SKEMA + INDEX + SEED DATA DEMO
# =========================================================
def init_db():
    first_time = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL;")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pelanggan (
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

        CREATE TABLE IF NOT EXISTS tarif (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            golongan_tarif TEXT NOT NULL,
            batas_bawah INTEGER NOT NULL,
            batas_atas INTEGER,
            harga_per_m3 INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pencatatan (
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

        CREATE TABLE IF NOT EXISTS tagihan (
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

        CREATE TABLE IF NOT EXISTS pengaturan (
            kunci TEXT PRIMARY KEY,
            nilai TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_log (
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
            sumber TEXT,          -- 'online' atau 'offline_sync'
            keterangan TEXT
        );

        -- Index untuk mempercepat filter & pencarian pada skala ratusan/ribuan pelanggan
        CREATE INDEX IF NOT EXISTS idx_pelanggan_rw_rt ON pelanggan(rw, rt);
        CREATE INDEX IF NOT EXISTS idx_pelanggan_golongan ON pelanggan(golongan_tarif);
        CREATE INDEX IF NOT EXISTS idx_pelanggan_nama ON pelanggan(nama);
        CREATE INDEX IF NOT EXISTS idx_pencatatan_periode ON pencatatan(periode);
        CREATE INDEX IF NOT EXISTS idx_pencatatan_pelanggan ON pencatatan(pelanggan_id);
        CREATE INDEX IF NOT EXISTS idx_tagihan_periode ON tagihan(periode);
        CREATE INDEX IF NOT EXISTS idx_tagihan_pelanggan ON tagihan(pelanggan_id);
        CREATE INDEX IF NOT EXISTS idx_tarif_golongan ON tarif(golongan_tarif);
        CREATE INDEX IF NOT EXISTS idx_pelanggan_petugas ON pelanggan(petugas);
        CREATE INDEX IF NOT EXISTS idx_audit_petugas ON audit_log(petugas);
        CREATE INDEX IF NOT EXISTS idx_audit_periode ON audit_log(periode);
        """
    )
    db.commit()

    # migrasi: tambah kolom kontak bila belum ada (untuk database lama)
    kolom_pelanggan = {r["name"] for r in db.execute("PRAGMA table_info(pelanggan)").fetchall()}
    if "kontak" not in kolom_pelanggan:
        db.execute("ALTER TABLE pelanggan ADD COLUMN kontak TEXT")
        db.commit()

    # migrasi: kolom pembayaran pada tabel tagihan
    kolom_tagihan = {r["name"] for r in db.execute("PRAGMA table_info(tagihan)").fetchall()}
    if "waktu_bayar" not in kolom_tagihan:
        db.execute("ALTER TABLE tagihan ADD COLUMN waktu_bayar TEXT")
    if "dicatat_oleh" not in kolom_tagihan:
        db.execute("ALTER TABLE tagihan ADD COLUMN dicatat_oleh TEXT")
    # migrasi: nilai abodemen per tagihan (untuk rekap yang akurat secara historis)
    if "abodemen" not in kolom_tagihan:
        db.execute("ALTER TABLE tagihan ADD COLUMN abodemen INTEGER")
        # backfill dari rincian_tarif baris lama
        rows = db.execute("SELECT id, rincian_tarif FROM tagihan WHERE rincian_tarif IS NOT NULL").fetchall()
        for r in rows:
            try:
                rinc = json.loads(r["rincian_tarif"])
                val = next((b.get("subtotal") for b in rinc if b.get("blok") == "abodemen"), None)
            except (ValueError, TypeError):
                val = None
            if val is not None:
                db.execute("UPDATE tagihan SET abodemen=? WHERE id=?", (val, r["id"]))
    db.commit()

    if first_time:
        # pengaturan default (abodemen) — bisa diubah lewat menu Pengaturan
        db.execute(
            "INSERT OR IGNORE INTO pengaturan (kunci, nilai) VALUES ('abodemen', ?)",
            (str(ABODEMEN),),
        )
        db.commit()
        # data demo hanya dibuat bila diminta (untuk uji coba): SEED_DEMO=1 python app.py
        if os.environ.get("SEED_DEMO") == "1":
            seed_demo(db)

    db.close()


def seed_demo(db):
    import random
    cur = db.cursor()

    tarif_rows = [
        ("Rumah Tangga", 0, 10, 1000),
        ("Rumah Tangga", 11, 20, 1500),
        ("Rumah Tangga", 21, None, 2000),
        ("Niaga", 0, 10, 2000),
        ("Niaga", 11, 20, 2750),
        ("Niaga", 21, None, 3500),
        ("Sosial", 0, 10, 750),
        ("Sosial", 11, None, 1000),
    ]
    cur.executemany(
        "INSERT INTO tarif (golongan_tarif, batas_bawah, batas_atas, harga_per_m3) VALUES (?,?,?,?)",
        tarif_rows,
    )
    db.execute(
        "INSERT OR IGNORE INTO pengaturan (kunci, nilai) VALUES ('abodemen', ?)",
        (str(ABODEMEN),),
    )
    db.commit()

    nama_depan = ["Suparjo", "Siti", "Rahmat", "Nur", "Endang", "Budi", "Sri", "Agus", "Dwi", "Bambang",
                  "Tuti", "Slamet", "Wahyu", "Titik", "Joko", "Sumarni", "Hidayat", "Kholis", "Sari", "Wati"]
    nama_belakang = ["Hidayat", "Santoso", "Wijaya", "Kusuma", "Utami", "Saputra", "Lestari", "Purnama",
                      "Setiawan", "Handayani", "", "", ""]
    golongan_choices = ["Rumah Tangga"] * 7 + ["Niaga"] * 2 + ["Sosial"] * 1
    petugas_choices = ["Andi", "Budi", "Citra"]

    pelanggan_rows = []
    for i in range(1, 61):  # 60 pelanggan demo (cukup untuk uji filter/paginasi, ringan utk seed)
        rw = f"{((i - 1) // 20) + 1:02d}"
        rt = f"{((i - 1) % 20) // 4 + 1:02d}"
        nama = f"{random.choice(nama_depan)} {random.choice(nama_belakang)}".strip()
        golongan = random.choice(golongan_choices)
        awal = random.randint(20, 900)
        pelanggan_rows.append((
            f"PS-{i:04d}", nama, f"Dusun {['Krajan','Sumber','Wonorejo'][int(rw)-1] if int(rw)<=3 else 'Krajan'}",
            rt, rw, golongan, awal, random.choice(petugas_choices),
            f"08{random.randint(100000000, 999999999)}",
            0 if i > 58 else 1,  # 2 pelanggan terakhir nonaktif, untuk demo fitur nonaktif
        ))

    cur.executemany(
        """INSERT INTO pelanggan
           (nomor_meteran, nama, alamat, rt, rw, golongan_tarif, meteran_awal, petugas, kontak, aktif)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        pelanggan_rows,
    )
    db.commit()

    # ---------- riwayat 6 bulan (termasuk bulan berjalan) ----------
    ids = {row[0]: i + 1 for i, row in enumerate(pelanggan_rows)}
    sekarang = periode_sekarang()
    y, m = map(int, sekarang.split("-"))
    periodes = []
    for i in range(5, -1, -1):
        mm, yy = m - i, y
        while mm <= 0:
            mm += 12
            yy -= 1
        periodes.append(f"{yy}-{mm:02d}")

    posisi_meteran = {row[0]: row[6] for row in pelanggan_rows}  # nomor -> posisi meteran berjalan
    for pr in periodes:
        for nomor, nama, alamat, rt, rw, golongan, awal, petugas, kontak, aktif in pelanggan_rows:
            if not aktif:
                continue
            # ~85% tercatat tiap bulan -> tagihan = pemakaian + ABODEMEN (berlaku tiap bulan).
            # Sisanya tidak tercatat -> tagihan abodemen saja, sengaja untuk demo.
            if random.random() < 0.85:
                pakai = random.randint(3, 28)
                m_awal = posisi_meteran[nomor]
                m_akhir = m_awal + pakai
                posisi_meteran[nomor] = m_akhir
                anom = 1 if pakai >= 26 else 0
                alasan = random.choice(["Kebocoran pipa", "Meteran diganti", "Ada hajatan"]) if anom else None
                db.execute(
                    """INSERT INTO pencatatan
                       (pelanggan_id, periode, meteran_awal, meteran_akhir, foto, alasan, petugas, anomali)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (ids[nomor], pr, m_awal, m_akhir, None, alasan, petugas, anom),
                )
        db.commit()
        generate_tagihan_periode(db, pr)

        # sebagian tagihan ditandai lunas (bulan berjalan lebih sedikit yang lunas)
        peluang_lunas = 0.35 if pr == sekarang else 0.7
        rows = db.execute("SELECT id FROM tagihan WHERE periode=?", (pr,)).fetchall()
        for r in rows:
            if random.random() < peluang_lunas:
                db.execute(
                    "UPDATE tagihan SET status_bayar='lunas', waktu_bayar=?, dicatat_oleh='admin' WHERE id=?",
                    (f"{random.randint(1, 28):02d}-{pr[5:7]}-{pr[:4]} 09:{random.randint(10, 59)}", r["id"]),
                )
        db.commit()


# =========================================================
# LOGIKA TARIF PROGRESIF
# =========================================================
def hitung_progresif(db, golongan, pemakaian_m3):
    if pemakaian_m3 <= 0:
        return 0, []
    brackets = db.execute(
        "SELECT batas_bawah, batas_atas, harga_per_m3 FROM tarif "
        "WHERE golongan_tarif = ? ORDER BY batas_bawah ASC",
        (golongan,),
    ).fetchall()

    total = 0
    rincian = []
    prev_atas = 0
    for b in brackets:
        bawah, atas, harga = b["batas_bawah"], b["batas_atas"], b["harga_per_m3"]
        tier_atas = atas if atas is not None else pemakaian_m3
        if tier_atas < prev_atas:
            continue
        pakai_di_tier = max(0, min(pemakaian_m3, tier_atas) - prev_atas)
        if pakai_di_tier > 0:
            subtotal = pakai_di_tier * harga
            total += subtotal
            label_atas = "keatas" if atas is None else str(atas)
            rincian.append({"blok": f"{prev_atas + 1}-{label_atas}", "m3": pakai_di_tier,
                             "harga": harga, "subtotal": subtotal})
        prev_atas = tier_atas
        if pemakaian_m3 <= tier_atas:
            break
    return total, rincian


def get_pengaturan(db, kunci, default=""):
    """Ambil nilai pengaturan; fallback ke default bila belum diatur."""
    row = db.execute("SELECT nilai FROM pengaturan WHERE kunci=?", (kunci,)).fetchone()
    if row and row["nilai"] is not None:
        return row["nilai"]
    return default


def get_semua_pengaturan(db):
    """Semua pasangan kunci-nilai dari tabel pengaturan."""
    return {r["kunci"]: r["nilai"] for r in db.execute("SELECT kunci, nilai FROM pengaturan").fetchall()}


def get_abodemen(db):
    """Nilai abodemen bulanan dari tabel pengaturan; fallback ke konstanta bila belum diatur."""
    try:
        v = int(get_pengaturan(db, "abodemen", ""))
        if v >= 0:
            return v
    except (ValueError, TypeError):
        pass
    return ABODEMEN


def hitung_tagihan(db, golongan, pemakaian_m3):
    """Total tagihan bulanan = tarif progresif pemakaian + abodemen (berlaku setiap bulan).
    Mengembalikan (total, rincian, nilai_abodemen)."""
    total, rincian = hitung_progresif(db, golongan, pemakaian_m3)
    nilai_abodemen = get_abodemen(db)
    rincian.append({"blok": "abodemen", "m3": 0, "harga": nilai_abodemen, "subtotal": nilai_abodemen})
    return total + nilai_abodemen, rincian, nilai_abodemen


def validasi_blok_tarif(db, golongan, batas_bawah, batas_atas, harga_per_m3, kecuali_id=None):
    """Validasi blok tarif sebelum disimpan: angka wajar, tidak tumpang tindih,
    urut berkesinambungan, dan hanya satu blok 'ke atas' di posisi terakhir.
    Mengembalikan pesan error (str) bila tidak valid, atau None bila valid."""
    if harga_per_m3 <= 0:
        return "Harga per m³ harus lebih dari 0."
    if batas_bawah < 0:
        return "Batas bawah tidak boleh negatif."
    if batas_atas is not None and batas_atas <= batas_bawah:
        return "Batas atas harus lebih besar dari batas bawah."

    blok_lain = db.execute(
        "SELECT * FROM tarif WHERE golongan_tarif=? AND id != ? ORDER BY batas_bawah",
        (golongan, kecuali_id or -1),
    ).fetchall()

    semua = list(blok_lain) + [{"batas_bawah": batas_bawah, "batas_atas": batas_atas}]
    semua.sort(key=lambda b: b["batas_bawah"])

    for i in range(len(semua) - 1):
        if semua[i]["batas_bawah"] == semua[i + 1]["batas_bawah"]:
            return f"Sudah ada blok yang dimulai dari {batas_bawah} m³ pada golongan ini."
    for i in range(len(semua) - 1):
        atas_i = semua[i]["batas_atas"]
        bawah_next = semua[i + 1]["batas_bawah"]
        if atas_i is None:
            return "Blok 'ke atas' harus menjadi blok terakhir golongan ini."
        if atas_i >= bawah_next:
            return f"Blok tumpang tindih: blok berakhir di {atas_i} tetapi blok berikutnya mulai dari {bawah_next}."
        if atas_i + 1 != bawah_next:
            return f"Ada celah antar blok: blok berakhir di {atas_i}, blok berikutnya harus mulai dari {atas_i + 1}."
    return None


def generate_tagihan_periode(db, periode):
    pelanggan_list = db.execute("SELECT * FROM pelanggan WHERE aktif = 1").fetchall()
    dibuat = 0
    for p in pelanggan_list:
        catat = db.execute(
            "SELECT * FROM pencatatan WHERE pelanggan_id = ? AND periode = ?",
            (p["id"], periode),
        ).fetchone()

        if catat:
            pemakaian = max(0, catat["meteran_akhir"] - catat["meteran_awal"])
            total, rincian, nilai_abodemen = hitung_tagihan(db, p["golongan_tarif"], pemakaian)
            jenis = "normal"
        else:
            pemakaian = 0
            total, rincian, nilai_abodemen = hitung_tagihan(db, p["golongan_tarif"], 0)  # hanya abodemen
            jenis = "abodemen"

        existing = db.execute(
            "SELECT id, status_bayar FROM tagihan WHERE pelanggan_id=? AND periode=?",
            (p["id"], periode),
        ).fetchone()

        if existing:
            if existing["status_bayar"] == "lunas":
                continue
            db.execute(
                "UPDATE tagihan SET jenis=?, pemakaian_m3=?, rincian_tarif=?, total_tagihan=?, abodemen=? WHERE id=?",
                (jenis, pemakaian, json.dumps(rincian), total, nilai_abodemen, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO tagihan
                   (pelanggan_id, periode, jenis, pemakaian_m3, rincian_tarif, total_tagihan, abodemen)
                   VALUES (?,?,?,?,?,?,?)""",
                (p["id"], periode, jenis, pemakaian, json.dumps(rincian), total, nilai_abodemen),
            )
            dibuat += 1
    db.commit()
    return dibuat


# =========================================================
# DETEKSI ANOMALI PENCATATAN
# =========================================================
def cek_anomali(db, pelanggan_id, periode, pemakaian_baru):
    """
    Bandingkan pemakaian baru dengan:
    1) Batas absolut (ANOMALI_ABSOLUT_M3) — berlaku walau riwayat belum cukup.
    2) Rata-rata pemakaian riwayat pelanggan (maks 6 bulan terakhir), jika riwayat cukup.
    Mengembalikan dict info anomali atau None jika wajar.
    """
    if pemakaian_baru > ANOMALI_ABSOLUT_M3:
        return {"tipe": "melebihi_batas", "rata2": None, "pemakaian": pemakaian_baru,
                "batas": ANOMALI_ABSOLUT_M3}

    riwayat = db.execute(
        """SELECT periode, (meteran_akhir - meteran_awal) AS pakai
           FROM pencatatan
           WHERE pelanggan_id = ? AND periode < ?
             AND (meteran_akhir - meteran_awal) > 0
             AND (meteran_akhir - meteran_awal) <= ?
           ORDER BY periode DESC LIMIT 6""",
        (pelanggan_id, periode, ANOMALI_ABSOLUT_M3),
    ).fetchall()

    if len(riwayat) < MIN_RIWAYAT_UTK_ANOMALI:
        return None  # riwayat belum cukup untuk dijadikan pembanding

    rata2 = sum(r["pakai"] for r in riwayat) / len(riwayat)
    if rata2 <= 0:
        return None

    if pemakaian_baru > rata2 * ANOMALI_KALI_ATAS:
        return {"tipe": "lonjakan", "rata2": round(rata2, 1), "pemakaian": pemakaian_baru}
    if pemakaian_baru < rata2 * ANOMALI_KALI_BAWAH:
        return {"tipe": "anjlok", "rata2": round(rata2, 1), "pemakaian": pemakaian_baru}
    return None


# =========================================================
# HELPER: FILTER & PAGINASI QUERY
# =========================================================
def get_int_arg(name, default=1):
    try:
        return max(1, int(request.args.get(name, default)))
    except (TypeError, ValueError):
        return default


def daftar_rw(db):
    return [r["rw"] for r in db.execute("SELECT DISTINCT rw FROM pelanggan WHERE rw IS NOT NULL AND rw!='' ORDER BY rw").fetchall()]


def daftar_rt(db, rw=None):
    if rw:
        return [r["rt"] for r in db.execute(
            "SELECT DISTINCT rt FROM pelanggan WHERE rw=? AND rt IS NOT NULL AND rt!='' ORDER BY rt", (rw,)
        ).fetchall()]
    return [r["rt"] for r in db.execute("SELECT DISTINCT rt FROM pelanggan WHERE rt IS NOT NULL AND rt!='' ORDER BY rt").fetchall()]


def daftar_golongan(db):
    return [r["golongan_tarif"] for r in db.execute(
        "SELECT DISTINCT golongan_tarif FROM tarif ORDER BY golongan_tarif"
    ).fetchall()]


def daftar_petugas(db):
    rows = db.execute(
        "SELECT DISTINCT petugas FROM pelanggan WHERE petugas IS NOT NULL AND petugas != '' ORDER BY petugas"
    ).fetchall()
    return [r["petugas"] for r in rows]


def build_tarif_json(db):
    """Kelompokkan tarif per golongan untuk dipakai kalkulasi estimasi biaya di sisi klien (JS),
    supaya petugas melihat estimasi tagihan langsung saat mengetik meteran akhir."""
    rows = db.execute("SELECT * FROM tarif ORDER BY golongan_tarif, batas_bawah").fetchall()
    hasil = {}
    for r in rows:
        hasil.setdefault(r["golongan_tarif"], []).append({
            "bawah": r["batas_bawah"], "atas": r["batas_atas"], "harga": r["harga_per_m3"],
        })
    return json.dumps(hasil)


def daftar_periode(db):
    rows = db.execute(
        "SELECT DISTINCT periode FROM tagihan UNION SELECT DISTINCT periode FROM pencatatan ORDER BY periode DESC"
    ).fetchall()
    hasil = [r["periode"] for r in rows]
    sekarang = periode_sekarang()
    if sekarang not in hasil:
        hasil.insert(0, sekarang)
    return hasil


# Kolom yang boleh dipakai untuk sorting tab Pelanggan (whitelist -> aman dari SQL injection)
PELANGGAN_SORT_MAP = {
    "nomor_meteran": "nomor_meteran",
    "nama": "nama",
    "alamat": "alamat",
    "rt": "rt",
    "rw": "rw",
    "golongan": "golongan_tarif",
    "meteran_awal": "meteran_awal",
    "petugas": "petugas",
}


def filter_pelanggan(args, periode):
    """Bangun WHERE + params untuk tab Pelanggan dari query-string.
    Dipakai bersama oleh halaman index dan export CSV."""
    p_rw = args.get("p_rw", "")
    p_rt = args.get("p_rt", "")
    p_golongan = args.get("p_golongan", "")
    p_petugas = args.get("p_petugas", "")
    p_status = args.get("p_status", "")
    p_aktif = args.get("p_aktif", "1")
    p_q = args.get("p_q", "").strip()

    where, params = [], []
    if p_aktif == "0":
        where.append("aktif = 0")
    elif p_aktif == "1":
        where.append("aktif = 1")
    if p_rw:
        where.append("rw = ?"); params.append(p_rw)
    if p_rt:
        where.append("rt = ?"); params.append(p_rt)
    if p_golongan:
        where.append("golongan_tarif = ?"); params.append(p_golongan)
    if p_petugas:
        where.append("petugas = ?"); params.append(p_petugas)
    if p_q:
        where.append("(nama LIKE ? OR nomor_meteran LIKE ? OR alamat LIKE ?)")
        like = f"%{p_q}%"; params += [like, like, like]
    if p_status == "tercatat":
        where.append("id IN (SELECT pelanggan_id FROM pencatatan WHERE periode=?)"); params.append(periode)
    elif p_status == "belum":
        where.append("id NOT IN (SELECT pelanggan_id FROM pencatatan WHERE periode=?)"); params.append(periode)

    return {
        "p_rw": p_rw, "p_rt": p_rt, "p_golongan": p_golongan, "p_petugas": p_petugas,
        "p_status": p_status, "p_aktif": p_aktif, "p_q": p_q,
        "where_sql": " AND ".join(where) if where else "1=1",
        "params": params,
    }


def filter_tagihan(args, periode):
    """Bangun WHERE + params untuk tab Tagihan dari query-string.
    Dipakai bersama oleh halaman index dan export CSV tagihan."""
    t_rw = args.get("t_rw", "")
    t_rt = args.get("t_rt", "")
    t_golongan = args.get("t_golongan", "")
    t_petugas = args.get("t_petugas", "")
    t_jenis = args.get("t_jenis", "")
    t_status = args.get("t_status", "")
    t_q = args.get("t_q", "").strip()

    where, params = ["t.periode = ?"], [periode]
    if t_rw:
        where.append("p.rw = ?"); params.append(t_rw)
    if t_rt:
        where.append("p.rt = ?"); params.append(t_rt)
    if t_golongan:
        where.append("p.golongan_tarif = ?"); params.append(t_golongan)
    if t_petugas:
        where.append("p.petugas = ?"); params.append(t_petugas)
    if t_jenis:
        where.append("t.jenis = ?"); params.append(t_jenis)
    if t_status:
        where.append("t.status_bayar = ?"); params.append(t_status)
    if t_q:
        where.append("(p.nama LIKE ? OR p.nomor_meteran LIKE ?)")
        like = f"%{t_q}%"; params += [like, like]

    return {
        "t_rw": t_rw, "t_rt": t_rt, "t_golongan": t_golongan,
        "t_petugas": t_petugas,
        "t_jenis": t_jenis, "t_status": t_status, "t_q": t_q,
        "where_sql": " AND ".join(where),
        "params": params,
    }


def filter_laporan(args, periode, periode_list):
    """Bangun WHERE + params untuk tab Laporan dari query-string.
    Dipakai bersama oleh halaman index, export CSV, dan halaman cetak."""
    default_awal = f"{periode[:4]}-01"
    l_awal = args.get("l_awal", default_awal if default_awal in periode_list else periode)
    l_akhir = args.get("l_akhir", periode)
    l_rw = args.get("l_rw", "")
    l_rt = args.get("l_rt", "")
    l_golongan = args.get("l_golongan", "")
    l_jenis = args.get("l_jenis", "")
    l_status = args.get("l_status", "")

    where, params = ["t.periode BETWEEN ? AND ?"], [l_awal, l_akhir]
    if l_rw:
        where.append("p.rw = ?"); params.append(l_rw)
    if l_rt:
        where.append("p.rt = ?"); params.append(l_rt)
    if l_golongan:
        where.append("p.golongan_tarif = ?"); params.append(l_golongan)
    if l_jenis:
        where.append("t.jenis = ?"); params.append(l_jenis)
    if l_status:
        where.append("t.status_bayar = ?"); params.append(l_status)

    return {
        "l_awal": l_awal, "l_akhir": l_akhir, "l_rw": l_rw, "l_rt": l_rt,
        "l_golongan": l_golongan, "l_jenis": l_jenis, "l_status": l_status,
        "where_sql": " AND ".join(where),
        "params": params,
    }


# =========================================================
# ROUTE UTAMA (single page, tab dikendalikan lewat query param)
# =========================================================
@app.route("/admin")
@perlu_auth_admin
def index():
    db = get_db()
    periode = periode_sekarang()
    tab = request.args.get("tab", "tagihan")

    rw_list = daftar_rw(db)
    golongan_list = daftar_golongan(db)
    periode_list = daftar_periode(db)

    # ---------- FILTER TAB PELANGGAN ----------
    pf = filter_pelanggan(request.args, periode)
    p_page = get_int_arg("p_page", 1)
    p_sort = request.args.get("p_sort", "rw")
    p_arah = request.args.get("p_arah", "asc")
    kolom_sort = PELANGGAN_SORT_MAP.get(p_sort, "rw")
    arah_sort = "DESC" if p_arah == "desc" else "ASC"

    p_total = db.execute(
        f"SELECT COUNT(*) c FROM pelanggan WHERE {pf['where_sql']}", pf["params"]
    ).fetchone()["c"]
    p_total_pages = max(1, (p_total + PAGE_SIZE - 1) // PAGE_SIZE)
    p_page = min(p_page, p_total_pages)
    pelanggan_list = db.execute(
        f"SELECT * FROM pelanggan WHERE {pf['where_sql']} "
        f"ORDER BY {kolom_sort} {arah_sort}, rw, rt, nama LIMIT ? OFFSET ?",
        pf["params"] + [PAGE_SIZE, (p_page - 1) * PAGE_SIZE],
    ).fetchall()

    # daftar nomor meteran terdaftar untuk cek duplikat langsung di form tambah
    nomor_terdaftar_json = json.dumps([
        r["nomor_meteran"].strip().upper()
        for r in db.execute("SELECT nomor_meteran FROM pelanggan").fetchall()
    ])
    baru_id = request.args.get("baru", "")

    tercatat_ids = {r["pelanggan_id"] for r in db.execute(
        "SELECT pelanggan_id FROM pencatatan WHERE periode = ?", (periode,)
    ).fetchall()}

    # ---------- FILTER TAB TAGIHAN BULAN INI ----------
    tf = filter_tagihan(request.args, periode)
    t_rw, t_rt = tf["t_rw"], tf["t_rt"]
    t_golongan, t_petugas = tf["t_golongan"], tf["t_petugas"]
    t_jenis, t_status, t_q = tf["t_jenis"], tf["t_status"], tf["t_q"]
    t_page = get_int_arg("t_page", 1)

    twhere_sql, tparams = tf["where_sql"], tf["params"]
    base_from = "FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id WHERE " + twhere_sql
    t_total = db.execute(f"SELECT COUNT(*) c {base_from}", tparams).fetchone()["c"]
    t_total_pages = max(1, (t_total + PAGE_SIZE - 1) // PAGE_SIZE)
    t_page = min(t_page, t_total_pages)
    tagihan_list = db.execute(
        f"""SELECT t.*, p.nama, p.nomor_meteran, p.alamat, p.rt, p.rw, p.golongan_tarif
            {base_from} ORDER BY p.rw, p.rt, p.nama LIMIT ? OFFSET ?""",
        tparams + [PAGE_SIZE, (t_page - 1) * PAGE_SIZE],
    ).fetchall()
    # parse rincian tarif agar bisa ditampilkan di baris "Rincian"
    tagihan_list = [dict(r) for r in tagihan_list]
    for r in tagihan_list:
        r["rincian_parsed"] = json.loads(r["rincian_tarif"]) if r["rincian_tarif"] else []

    # ringkasan (dihitung dari SELURUH data periode ini, bukan hanya halaman aktif)
    agg = db.execute(
        """SELECT
             COUNT(*) n,
             COALESCE(SUM(total_tagihan),0) total,
             COALESCE(SUM(CASE WHEN status_bayar='lunas' THEN total_tagihan ELSE 0 END),0) lunas,
             COALESCE(SUM(abodemen),0) dari_abodemen,
             COALESCE(SUM(CASE WHEN jenis='abodemen' THEN 1 ELSE 0 END),0) jumlah_abodemen
           FROM tagihan WHERE periode = ?""",
        (periode,),
    ).fetchone()
    total_pelanggan_aktif = db.execute("SELECT COUNT(*) c FROM pelanggan WHERE aktif=1").fetchone()["c"]

    ringkasan = {
        "total_pelanggan": total_pelanggan_aktif,
        "total_tercatat": len(tercatat_ids),
        "total_belum_catat": total_pelanggan_aktif - len(tercatat_ids),
        "total_tagihan_dibuat": agg["n"],
        "total_pendapatan_potensial": agg["total"],
        "total_lunas": agg["lunas"],
        "total_belum_bayar": agg["total"] - agg["lunas"],
        "total_dari_abodemen": agg["dari_abodemen"],
        "jumlah_abodemen": agg["jumlah_abodemen"],
    }

    # ---------- TAB AUDIT LOG ----------
    a_q = request.args.get("a_q", "").strip()
    a_sumber = request.args.get("a_sumber", "")
    a_anomali = request.args.get("a_anomali", "")
    a_page = get_int_arg("a_page", 1)

    awhere, aparams = [], []
    if a_q:
        awhere.append("(a.nomor_meteran LIKE ? OR a.petugas LIKE ? OR a.keterangan LIKE ?)")
        like = f"%{a_q}%"; aparams += [like, like, like]
    if a_sumber:
        awhere.append("a.sumber = ?"); aparams.append(a_sumber)
    if a_anomali == "1":
        awhere.append("a.anomali = 1")
    awhere_sql = " AND ".join(awhere) if awhere else "1=1"

    a_total = db.execute(f"SELECT COUNT(*) c FROM audit_log a WHERE {awhere_sql}", aparams).fetchone()["c"]
    a_total_pages = max(1, (a_total + PAGE_SIZE - 1) // PAGE_SIZE)
    a_page = min(a_page, a_total_pages)
    audit_list = db.execute(
        f"""SELECT a.*, p.nama FROM audit_log a LEFT JOIN pelanggan p ON p.id = a.pelanggan_id
            WHERE {awhere_sql} ORDER BY a.id DESC LIMIT ? OFFSET ?""",
        aparams + [PAGE_SIZE, (a_page - 1) * PAGE_SIZE],
    ).fetchall()

    # ---------- TAB TARIF ----------
    tarif_list = db.execute("SELECT * FROM tarif ORDER BY golongan_tarif, batas_bawah").fetchall()
    tarif_edit = request.args.get("tarif_edit", "")
    tarif_edit_row = None
    if tarif_edit.isdigit():
        tarif_edit_row = db.execute("SELECT * FROM tarif WHERE id=?", (int(tarif_edit),)).fetchone()

    # ---------- TAB PENCATATAN (data untuk select bertingkat RW -> RT -> Nama) ----------
    semua_pelanggan_js = db.execute(
        """SELECT p.id, p.nomor_meteran, p.nama, p.alamat, p.rw, p.rt, p.golongan_tarif, p.meteran_awal,
                  (SELECT meteran_akhir FROM pencatatan WHERE pelanggan_id=p.id ORDER BY periode DESC LIMIT 1) last_akhir,
                  (SELECT 1 FROM pencatatan WHERE pelanggan_id=p.id AND periode=?) sudah_periode_ini,
                  (SELECT AVG(pakai) FROM (
                       SELECT (meteran_akhir - meteran_awal) pakai FROM pencatatan
                       WHERE pelanggan_id=p.id AND periode < ? ORDER BY periode DESC LIMIT 6
                   )) rata2_pakai,
                  (SELECT COALESCE(SUM(total_tagihan),0) FROM tagihan t
                   WHERE t.pelanggan_id=p.id AND t.status_bayar != 'lunas') tunggakan,
                  (SELECT COUNT(*) FROM tagihan t
                   WHERE t.pelanggan_id=p.id AND t.status_bayar != 'lunas') n_tunggakan
           FROM pelanggan p WHERE p.aktif=1 ORDER BY p.rw, p.rt, p.nama""",
        (periode, periode),
    ).fetchall()
    pelanggan_json = json.dumps([dict(r) for r in semua_pelanggan_js])
    tarif_json = build_tarif_json(db)

    prefill = request.args.get("prefill_pelanggan_id", "")
    prefill_anomali = request.args.get("prefill_anomali") == "1"

    # ---------- TAB LAPORAN ----------
    lf = filter_laporan(request.args, periode, periode_list)
    l_awal, l_akhir = lf["l_awal"], lf["l_akhir"]
    l_rw, l_rt = lf["l_rw"], lf["l_rt"]
    l_golongan, l_jenis, l_status = lf["l_golongan"], lf["l_jenis"], lf["l_status"]
    l_page = get_int_arg("l_page", 1)
    l_tunggakan_page = get_int_arg("lt_page", 1)

    lwhere_sql, lparams = lf["where_sql"], lf["params"]
    lbase_from = "FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id WHERE " + lwhere_sql

    l_agg = db.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(t.total_tagihan),0) total,
                   COALESCE(SUM(CASE WHEN t.status_bayar='lunas' THEN t.total_tagihan ELSE 0 END),0) lunas,
                   COALESCE(SUM(t.abodemen),0) abodemen,
                   COALESCE(SUM(t.pemakaian_m3),0) total_m3
            {lbase_from}""",
        lparams,
    ).fetchone()
    l_agg = dict(l_agg)
    l_agg["belum"] = l_agg["total"] - l_agg["lunas"]
    l_agg["persen_lunas"] = round(l_agg["lunas"] / l_agg["total"] * 100) if l_agg["total"] else 0

    l_per_golongan = db.execute(
        f"""SELECT p.golongan_tarif, COUNT(*) n, COALESCE(SUM(t.total_tagihan),0) total
            {lbase_from} GROUP BY p.golongan_tarif ORDER BY total DESC""",
        lparams,
    ).fetchall()

    # rekap per RW (untuk koordinasi penagihan)
    l_per_rw = db.execute(
        f"""SELECT p.rw, COUNT(*) n, COALESCE(SUM(t.total_tagihan),0) total,
                   COALESCE(SUM(CASE WHEN t.status_bayar='lunas' THEN t.total_tagihan ELSE 0 END),0) lunas
            {lbase_from} GROUP BY p.rw ORDER BY p.rw""",
        lparams,
    ).fetchall()

    # tren per bulan untuk grafik (dalam rentang & filter yang sama)
    l_tren = db.execute(
        f"""SELECT t.periode, COALESCE(SUM(t.total_tagihan),0) total,
                   COALESCE(SUM(t.pemakaian_m3),0) m3
            {lbase_from} GROUP BY t.periode ORDER BY t.periode""",
        lparams,
    ).fetchall()
    tren_json = json.dumps({
        "labels": [periode_label(r["periode"]) for r in l_tren],
        "total": [r["total"] for r in l_tren],
        "m3": [r["m3"] for r in l_tren],
    })

    # rekap tunggakan per pelanggan (lintas semua periode, filter RW/RT/Golongan saja), dipaginasi
    tw_where, tw_params = ["t.status_bayar != 'lunas'"], []
    if l_rw:
        tw_where.append("p.rw = ?"); tw_params.append(l_rw)
    if l_rt:
        tw_where.append("p.rt = ?"); tw_params.append(l_rt)
    if l_golongan:
        tw_where.append("p.golongan_tarif = ?"); tw_params.append(l_golongan)
    tw_where_sql = " AND ".join(tw_where)
    tunggakan_total = db.execute(
        f"""SELECT COUNT(*) c FROM (
                SELECT t.pelanggan_id FROM tagihan t
                JOIN pelanggan p ON p.id = t.pelanggan_id
                WHERE {tw_where_sql} GROUP BY t.pelanggan_id
            )""",
        tw_params,
    ).fetchone()["c"]
    tunggakan_total_pages = max(1, (tunggakan_total + PAGE_SIZE - 1) // PAGE_SIZE)
    l_tunggakan_page = min(l_tunggakan_page, tunggakan_total_pages)
    tunggakan_list = db.execute(
        f"""SELECT p.id pelanggan_id, p.nama, p.nomor_meteran, p.rt, p.rw, COUNT(*) n_bulan,
                   COALESCE(SUM(t.total_tagihan),0) total
            FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id
            WHERE {tw_where_sql}
            GROUP BY t.pelanggan_id ORDER BY total DESC LIMIT ? OFFSET ?""",
        tw_params + [PAGE_SIZE, (l_tunggakan_page - 1) * PAGE_SIZE],
    ).fetchall()

    l_total = db.execute(f"SELECT COUNT(*) c {lbase_from}", lparams).fetchone()["c"]
    l_total_pages = max(1, (l_total + PAGE_SIZE - 1) // PAGE_SIZE)
    l_page = min(l_page, l_total_pages)
    laporan_list = db.execute(
        f"""SELECT t.*, p.nama, p.nomor_meteran, p.rt, p.rw, p.golongan_tarif
            {lbase_from} ORDER BY t.periode DESC, p.rw, p.rt, p.nama LIMIT ? OFFSET ?""",
        lparams + [PAGE_SIZE, (l_page - 1) * PAGE_SIZE],
    ).fetchall()

    return render_template(
        "index.html",
        tab=tab,
        periode=periode,
        periode_now_label=periode_label(periode),
        rw_list=rw_list,
        golongan_list=golongan_list,
        periode_list=periode_list,
        abodemen=get_abodemen(db),
        ringkasan=ringkasan,
        # pelanggan
        pelanggan_list=pelanggan_list, p_total=p_total, p_page=p_page, p_total_pages=p_total_pages,
        p_rw=pf["p_rw"], p_rt=pf["p_rt"], p_golongan=pf["p_golongan"],
        p_petugas=pf["p_petugas"], p_status=pf["p_status"], p_aktif=pf["p_aktif"], p_q=pf["p_q"],
        p_sort=p_sort, p_arah=p_arah,
        p_rt_list=daftar_rt(db, pf["p_rw"]) if pf["p_rw"] else daftar_rt(db),
        petugas_list=daftar_petugas(db),
        tercatat_ids=tercatat_ids,
        nomor_terdaftar_json=nomor_terdaftar_json,
        baru_id=baru_id,
        # tagihan
        tagihan_list=tagihan_list, t_total=t_total, t_page=t_page, t_total_pages=t_total_pages,
        t_rw=t_rw, t_rt=t_rt, t_golongan=t_golongan, t_petugas=t_petugas,
        t_jenis=t_jenis, t_status=t_status, t_q=t_q,
        t_rt_list=daftar_rt(db, t_rw) if t_rw else daftar_rt(db),
        # tarif
        tarif_list=tarif_list,
        tarif_edit_row=tarif_edit_row,
        # audit log
        audit_list=audit_list, a_total=a_total, a_page=a_page, a_total_pages=a_total_pages,
        a_q=a_q, a_sumber=a_sumber, a_anomali=a_anomali,
        # pengaturan
        pengaturan=get_semua_pengaturan(db),
        # pencatatan
        pelanggan_json=pelanggan_json, tarif_json=tarif_json, prefill=prefill,
        prefill_anomali=prefill_anomali,
        anomali_absolut=ANOMALI_ABSOLUT_M3,
        # laporan
        laporan_list=laporan_list, l_agg=l_agg, l_per_golongan=l_per_golongan,
        l_per_rw=l_per_rw, tren_json=tren_json, tunggakan_list=tunggakan_list,
        tunggakan_total=tunggakan_total, l_tunggakan_page=l_tunggakan_page,
        tunggakan_total_pages=tunggakan_total_pages,
        l_total=l_total, l_page=l_page, l_total_pages=l_total_pages,
        l_awal=l_awal, l_akhir=l_akhir, l_rw=l_rw, l_rt=l_rt, l_golongan=l_golongan,
        l_jenis=l_jenis, l_status=l_status,
        l_rt_list=daftar_rt(db, l_rw) if l_rw else daftar_rt(db),
    )


# =========================================================
# AKSI: PELANGGAN
# =========================================================
@app.route("/pelanggan/tambah", methods=["POST"])
@perlu_auth_admin
def tambah_pelanggan():
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO pelanggan
               (nomor_meteran, nama, alamat, rt, rw, golongan_tarif, meteran_awal, petugas, kontak)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                request.form["nomor_meteran"].strip(),
                request.form["nama"].strip(),
                request.form.get("alamat", "").strip(),
                request.form.get("rt", "").strip(),
                request.form.get("rw", "").strip(),
                request.form["golongan_tarif"],
                int(request.form.get("meteran_awal") or 0),
                request.form.get("petugas", "").strip(),
                request.form.get("kontak", "").strip(),
            ),
        )
        db.commit()
        flash(f"Pelanggan {request.form['nama'].strip()} ({request.form['nomor_meteran'].strip()}) berhasil ditambahkan.", "success")
        return redirect(url_for("index", tab="pelanggan", baru=cur.lastrowid))
    except sqlite3.IntegrityError:
        flash("Nomor meteran sudah terdaftar.", "danger")
    except ValueError:
        flash("Meteran awal harus berupa angka.", "danger")
    return redirect(url_for("index", tab="pelanggan"))


def _kembali_aman(fallback_tab="pelanggan"):
    """Redirect ke halaman asal (referrer) bila masih di host yang sama, supaya filter & halaman terjaga."""
    balik = request.referrer or ""
    if balik.startswith(request.host_url):
        return balik
    return url_for("index", tab=fallback_tab)


@app.route("/pelanggan/<int:pelanggan_id>/ubah", methods=["POST"])
@perlu_auth_admin
def ubah_pelanggan(pelanggan_id):
    db = get_db()
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    if not p:
        flash("Pelanggan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="pelanggan"))
    try:
        db.execute(
            """UPDATE pelanggan SET nomor_meteran=?, nama=?, alamat=?, rt=?, rw=?,
               golongan_tarif=?, meteran_awal=?, petugas=?, kontak=? WHERE id=?""",
            (
                request.form["nomor_meteran"].strip(),
                request.form["nama"].strip(),
                request.form.get("alamat", "").strip(),
                request.form.get("rt", "").strip(),
                request.form.get("rw", "").strip(),
                request.form["golongan_tarif"],
                int(request.form.get("meteran_awal") or 0),
                request.form.get("petugas", "").strip(),
                request.form.get("kontak", "").strip(),
                pelanggan_id,
            ),
        )
        db.commit()
        flash(f"Data pelanggan {request.form['nama'].strip()} berhasil diperbarui.", "success")
    except sqlite3.IntegrityError:
        flash("Nomor meteran sudah dipakai pelanggan lain.", "danger")
    except ValueError:
        flash("Meteran awal harus berupa angka.", "danger")
    return redirect(_kembali_aman())


@app.route("/pelanggan/<int:pelanggan_id>/toggle-aktif", methods=["POST"])
@perlu_auth_admin
def toggle_aktif_pelanggan(pelanggan_id):
    db = get_db()
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    if not p:
        flash("Pelanggan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="pelanggan"))
    baru = 0 if p["aktif"] else 1
    db.execute("UPDATE pelanggan SET aktif=? WHERE id=?", (baru, pelanggan_id))
    db.commit()
    flash(f"Pelanggan {p['nama']} {'dinonaktifkan.' if not baru else 'diaktifkan kembali.'}", "success")
    return redirect(_kembali_aman())


@app.route("/pelanggan/<int:pelanggan_id>")
@perlu_auth_admin
def detail_pelanggan(pelanggan_id):
    db = get_db()
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    if not p:
        flash("Pelanggan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="pelanggan"))

    pencatatan_list = db.execute(
        "SELECT * FROM pencatatan WHERE pelanggan_id=? ORDER BY periode DESC", (pelanggan_id,)
    ).fetchall()
    tagihan_list = db.execute(
        "SELECT * FROM tagihan WHERE pelanggan_id=? ORDER BY periode DESC", (pelanggan_id,)
    ).fetchall()
    tagihan_map = {t["periode"]: t for t in tagihan_list}

    # gabung periode dari pencatatan & tagihan supaya bulan abodemen ikut tampil
    periodes = sorted(
        {r["periode"] for r in pencatatan_list} | {t["periode"] for t in tagihan_list},
        reverse=True,
    )
    riwayat = []
    for pr in periodes:
        catat = next((c for c in pencatatan_list if c["periode"] == pr), None)
        riwayat.append({"periode": pr, "catat": catat, "tagihan": tagihan_map.get(pr)})

    return render_template(
        "pelanggan_detail.html",
        p=p,
        riwayat=riwayat,
        golongan_list=daftar_golongan(db),
        petugas_list=daftar_petugas(db),
    )


@app.route("/pelanggan/export")
@perlu_auth_admin
def export_pelanggan():
    db = get_db()
    pf = filter_pelanggan(request.args, periode_sekarang())
    rows = db.execute(
        f"SELECT * FROM pelanggan WHERE {pf['where_sql']} ORDER BY rw, rt, nama", pf["params"]
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["No. Meteran", "Nama", "Alamat", "RT", "RW", "Golongan",
                     "Meteran Awal", "Petugas", "Kontak", "Status"])
    for r in rows:
        writer.writerow([
            r["nomor_meteran"], r["nama"], r["alamat"] or "", r["rt"] or "", r["rw"] or "",
            r["golongan_tarif"], r["meteran_awal"], r["petugas"] or "", r["kontak"] or "",
            "Aktif" if r["aktif"] else "Nonaktif",
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=pelanggan.csv"},
    )


@app.route("/pelanggan/import-template")
@perlu_auth_admin
def template_import_pelanggan():
    """Unduh template CSV untuk import massal pelanggan."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["nomor_meteran", "nama", "alamat", "rt", "rw",
                     "golongan_tarif", "meteran_awal", "petugas", "kontak"])
    writer.writerow(["PS-0101", "Budi Santoso", "Jl. Melati No. 1", "02", "01",
                     "Rumah Tangga", "0", "Citra", "08123456789"])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=template_import_pelanggan.csv"},
    )


@app.route("/pelanggan/import", methods=["POST"])
@perlu_auth_admin
def import_pelanggan():
    """Import massal pelanggan dari file CSV (format sesuai template)."""
    db = get_db()
    file = request.files.get("file_csv")
    if not file or file.filename == "":
        flash("Pilih file CSV terlebih dahulu.", "danger")
        return redirect(url_for("index", tab="pelanggan"))
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("File CSV harus berformat UTF-8.", "danger")
        return redirect(url_for("index", tab="pelanggan"))

    golongan_valid = set(daftar_golongan(db))
    reader = csv.DictReader(io.StringIO(text))
    sukses, gagal = 0, []
    for i, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue  # baris kosong
        nomor = (row.get("nomor_meteran") or "").strip()
        nama = (row.get("nama") or "").strip()
        golongan = (row.get("golongan_tarif") or "").strip()
        if not nomor or not nama:
            gagal.append(f"baris {i}: nomor meteran / nama kosong")
            continue
        if golongan not in golongan_valid:
            gagal.append(f"baris {i}: golongan '{golongan}' tidak terdaftar di tabel tarif")
            continue
        try:
            meteran_awal = int((row.get("meteran_awal") or "0").strip() or 0)
        except ValueError:
            gagal.append(f"baris {i}: meteran awal bukan angka")
            continue
        try:
            db.execute(
                """INSERT INTO pelanggan
                   (nomor_meteran, nama, alamat, rt, rw, golongan_tarif, meteran_awal, petugas, kontak)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (nomor, nama, (row.get("alamat") or "").strip(), (row.get("rt") or "").strip(),
                 (row.get("rw") or "").strip(), golongan, meteran_awal,
                 (row.get("petugas") or "").strip(), (row.get("kontak") or "").strip()),
            )
            sukses += 1
        except sqlite3.IntegrityError:
            gagal.append(f"baris {i}: nomor meteran '{nomor}' sudah terdaftar")
    db.commit()

    pesan = f"Import selesai: {sukses} pelanggan berhasil ditambahkan."
    if gagal:
        pesan += f" {len(gagal)} baris gagal — " + "; ".join(gagal[:5])
        if len(gagal) > 5:
            pesan += f"; dan {len(gagal) - 5} lainnya"
    flash(pesan, "success" if sukses else "danger")
    return redirect(url_for("index", tab="pelanggan"))


# =========================================================
# AKSI: PENCATATAN (dengan upload foto & deteksi anomali)
# =========================================================
def simpan_foto(file_storage, pelanggan_id, periode):
    if not file_storage or file_storage.filename == "":
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("Format foto harus JPG, JPEG, PNG, atau WEBP.")
    nama_file = secure_filename(f"meter_{pelanggan_id}_{periode}.{ext}")
    file_storage.save(os.path.join(UPLOAD_DIR, nama_file))
    return nama_file


def proses_pencatatan(db, pelanggan_id, meteran_akhir_raw, alasan, petugas,
                       konfirmasi_anomali=False, file_storage=None, sumber="online"):
    """
    Fungsi inti penyimpanan pencatatan meteran. Dipakai bersama oleh:
    - form admin (/pencatatan/tambah)
    - dashboard petugas (kirim langsung)
    - sinkronisasi arsip offline (/api/pencatatan)
    Mengembalikan dict: {ok, pesan, perlu_konfirmasi(optional), anomali(optional), ...}
    Setiap penyimpanan sukses selalu dicatat ke audit_log.
    """
    periode = periode_sekarang()

    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    if not p:
        return {"ok": False, "pesan": "Pelanggan tidak ditemukan."}

    # nama petugas: utamakan kiriman form; jika kosong pakai petugas binaan dari data pelanggan
    petugas = (petugas or "").strip() or (p["petugas"] or None)

    meteran_akhir_raw = str(meteran_akhir_raw).strip()
    if not meteran_akhir_raw.isdigit():
        return {"ok": False, "pesan": "Meteran akhir wajib diisi angka bulat."}
    meteran_akhir = int(meteran_akhir_raw)

    catatan_lalu = db.execute(
        """SELECT meteran_akhir FROM pencatatan WHERE pelanggan_id=? AND periode < ?
           ORDER BY periode DESC LIMIT 1""",
        (pelanggan_id, periode),
    ).fetchone()
    meteran_awal = catatan_lalu["meteran_akhir"] if catatan_lalu else p["meteran_awal"]

    if meteran_akhir < meteran_awal:
        return {"ok": False, "pesan": f"Meteran akhir ({meteran_akhir}) tidak boleh lebih kecil dari "
                                       f"meteran awal ({meteran_awal}). Periksa kembali angka meteran."}

    pemakaian = meteran_akhir - meteran_awal
    alasan = (alasan or "").strip() or None

    info_anomali = cek_anomali(db, pelanggan_id, periode, pemakaian)
    if info_anomali and not konfirmasi_anomali:
        label_tipe = {
            "lonjakan": "melonjak sangat tinggi",
            "anjlok": "turun drastis / kemungkinan macet",
            "melebihi_batas": f"melebihi batas wajar ({ANOMALI_ABSOLUT_M3} m³)",
        }.get(info_anomali["tipe"], "tidak biasa")
        pembanding = f"dibanding rata-rata {info_anomali['rata2']} m³/bulan" if info_anomali.get("rata2") else \
                     f"melebihi ambang {ANOMALI_ABSOLUT_M3} m³ untuk sekali catat"
        return {
            "ok": False, "perlu_konfirmasi": True, "anomali": info_anomali,
            "pesan": f"⚠ Pemakaian {p['nama']} bulan ini {pemakaian} m³, terdeteksi {label_tipe} "
                     f"{pembanding}. Periksa kembali angka meteran, lalu centang konfirmasi jika data sudah benar.",
        }
    if info_anomali and konfirmasi_anomali and not alasan:
        return {"ok": False, "pesan": "Karena data terdeteksi anomali, kolom Alasan wajib diisi "
                                       "(misal: kebocoran, perbaikan pipa, dll)."}

    if FOTO_WAJIB and not file_storage:
        return {"ok": False, "pesan": "Foto bukti meteran wajib diunggah untuk setiap pencatatan."}

    try:
        nama_foto = simpan_foto(file_storage, pelanggan_id, periode)
    except ValueError as e:
        return {"ok": False, "pesan": str(e)}
    if FOTO_WAJIB and not nama_foto:
        return {"ok": False, "pesan": "Foto bukti meteran wajib diunggah untuk setiap pencatatan."}

    try:
        db.execute(
            """INSERT INTO pencatatan
               (pelanggan_id, periode, meteran_awal, meteran_akhir, foto, alasan, petugas, anomali)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(pelanggan_id, periode) DO UPDATE SET
                 meteran_akhir=excluded.meteran_akhir,
                 foto=COALESCE(excluded.foto, pencatatan.foto),
                 alasan=excluded.alasan,
                 petugas=excluded.petugas,
                 anomali=excluded.anomali""",
            (pelanggan_id, periode, meteran_awal, meteran_akhir, nama_foto, alasan, petugas,
             1 if info_anomali else 0),
        )

        total, rincian, nilai_abodemen = hitung_tagihan(db, p["golongan_tarif"], pemakaian)
        db.execute(
            """INSERT INTO tagihan (pelanggan_id, periode, jenis, pemakaian_m3, rincian_tarif, total_tagihan, abodemen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(pelanggan_id, periode) DO UPDATE SET
                 jenis='normal', pemakaian_m3=excluded.pemakaian_m3,
                 rincian_tarif=excluded.rincian_tarif, total_tagihan=excluded.total_tagihan,
                 abodemen=excluded.abodemen
               WHERE tagihan.status_bayar != 'lunas'""",
            (pelanggan_id, periode, "normal", pemakaian, json.dumps(rincian), total, nilai_abodemen),
        )

        db.execute(
            """INSERT INTO audit_log
               (petugas, pelanggan_id, nomor_meteran, periode, meteran_awal, meteran_akhir,
                pemakaian_m3, anomali, sumber, keterangan)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (petugas or "-", pelanggan_id, p["nomor_meteran"], periode, meteran_awal, meteran_akhir,
             pemakaian, 1 if info_anomali else 0, sumber,
             f"Tagihan {rupiah(total)}" + (f" | Alasan: {alasan}" if alasan else "")),
        )
        db.commit()

        return {"ok": True, "pesan": f"Pencatatan {p['nomor_meteran']} — {p['nama']} berhasil disimpan "
                                      f"({pemakaian} m³, tagihan {rupiah(total)}).",
                "pemakaian": pemakaian, "total": total, "nomor_meteran": p["nomor_meteran"], "nama": p["nama"]}
    except sqlite3.IntegrityError as e:
        return {"ok": False, "pesan": f"Gagal menyimpan pencatatan: {e}"}


@app.route("/pencatatan/tambah", methods=["POST"])
@perlu_auth_admin
def tambah_pencatatan():
    db = get_db()
    pelanggan_id = int(request.form["pelanggan_id"])
    hasil = proses_pencatatan(
        db, pelanggan_id,
        request.form.get("meteran_akhir", ""),
        request.form.get("alasan", ""),
        request.form.get("petugas", "").strip(),
        konfirmasi_anomali=(request.form.get("konfirmasi_anomali") == "1"),
        file_storage=request.files.get("foto_file"),
        sumber="online",
    )
    flash(hasil["pesan"], "success" if hasil["ok"] else ("warning" if hasil.get("perlu_konfirmasi") else "danger"))
    if hasil["ok"]:
        return redirect(url_for("index", tab="catat"))
    return redirect(url_for("index", tab="catat", prefill_pelanggan_id=pelanggan_id, prefill_anomali=1))


@app.route("/api/pencatatan", methods=["POST"])
def api_pencatatan():
    """Endpoint terpadu (multipart/form-data, foto wajib disertakan) dipakai oleh dashboard
    petugas baik untuk kirim langsung maupun sinkronisasi arsip offline dari IndexedDB."""
    db = get_db()
    try:
        pelanggan_id = int(request.form.get("pelanggan_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "pesan": "ID pelanggan tidak valid."}), 400

    sumber = "offline_sync" if request.form.get("dari_arsip") == "1" else "online"
    hasil = proses_pencatatan(
        db, pelanggan_id,
        request.form.get("meteran_akhir", ""),
        request.form.get("alasan", ""),
        request.form.get("petugas", "").strip(),
        konfirmasi_anomali=(request.form.get("konfirmasi_anomali") == "1"),
        file_storage=request.files.get("foto_file"),
        sumber=sumber,
    )
    return jsonify(hasil), (200 if hasil["ok"] else 400)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/service-worker.js")
def service_worker():
    """Service worker PWA harus dilayani dari root scope."""
    return send_from_directory(
        os.path.join(BASE_DIR, "static"), "service-worker.js",
        mimetype="application/javascript",
    )


# =========================================================
# AKSI: TAGIHAN
# =========================================================
@app.route("/tagihan/generate", methods=["POST"])
@perlu_auth_admin
def generate_tagihan():
    db = get_db()
    periode = periode_sekarang()
    dibuat = generate_tagihan_periode(db, periode)
    flash(
        f"Proses generate tagihan periode {periode_label(periode)} selesai. "
        f"{dibuat} tagihan baru dibuat (abodemen {rupiah(get_abodemen(db))} berlaku tiap bulan, "
        f"ditambah tarif pemakaian bagi pelanggan yang tercatat).",
        "success",
    )
    return redirect(url_for("index", tab="tagihan"))


@app.route("/tagihan/<int:tagihan_id>/lunas", methods=["POST"])
@perlu_auth_admin
def bayar_tagihan(tagihan_id):
    db = get_db()
    t = db.execute(
        "SELECT t.*, p.nama, p.nomor_meteran FROM tagihan t JOIN pelanggan p ON p.id=t.pelanggan_id WHERE t.id=?",
        (tagihan_id,),
    ).fetchone()
    if not t:
        flash("Tagihan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="tagihan"))
    db.execute(
        "UPDATE tagihan SET status_bayar='lunas', waktu_bayar=?, dicatat_oleh='admin' WHERE id=?",
        (datetime.now().strftime("%d-%m-%Y %H:%M"), tagihan_id),
    )
    db.execute(
        """INSERT INTO audit_log
           (petugas, pelanggan_id, nomor_meteran, periode, meteran_awal, meteran_akhir,
            pemakaian_m3, anomali, sumber, keterangan)
           VALUES ('admin', ?, ?, ?, NULL, NULL, ?, 0, 'pembayaran', ?)""",
        (t["pelanggan_id"], t["nomor_meteran"], t["periode"], t["pemakaian_m3"],
         f"Tagihan {periode_label(t['periode'])} ditandai lunas {rupiah(t['total_tagihan'])}"),
    )
    db.commit()
    flash(f"Tagihan {t['nama']} ditandai lunas.", "success")
    return redirect(_kembali_aman("tagihan"))


@app.route("/tagihan/<int:tagihan_id>/batal", methods=["POST"])
@perlu_auth_admin
def batal_lunas(tagihan_id):
    db = get_db()
    t = db.execute(
        "SELECT t.*, p.nama, p.nomor_meteran FROM tagihan t JOIN pelanggan p ON p.id=t.pelanggan_id WHERE t.id=?",
        (tagihan_id,),
    ).fetchone()
    if not t:
        flash("Tagihan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="tagihan"))
    db.execute(
        "UPDATE tagihan SET status_bayar='belum_bayar', waktu_bayar=NULL, dicatat_oleh=NULL WHERE id=?",
        (tagihan_id,),
    )
    db.execute(
        """INSERT INTO audit_log
           (petugas, pelanggan_id, nomor_meteran, periode, meteran_awal, meteran_akhir,
            pemakaian_m3, anomali, sumber, keterangan)
           VALUES ('admin', ?, ?, ?, NULL, NULL, ?, 0, 'pembayaran', ?)""",
        (t["pelanggan_id"], t["nomor_meteran"], t["periode"], t["pemakaian_m3"],
         f"Pembatalan lunas tagihan {periode_label(t['periode'])} {rupiah(t['total_tagihan'])}"),
    )
    db.commit()
    flash(f"Pembayaran {t['nama']} dibatalkan — tagihan kembali berstatus belum bayar.", "warning")
    return redirect(_kembali_aman("tagihan"))


@app.route("/tagihan/lunasi-semua/<int:pelanggan_id>", methods=["POST"])
@perlu_auth_admin
def lunasi_semua(pelanggan_id):
    db = get_db()
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    if not p:
        flash("Pelanggan tidak ditemukan.", "danger")
        return redirect(_kembali_aman("laporan"))
    n = db.execute(
        "UPDATE tagihan SET status_bayar='lunas', waktu_bayar=?, dicatat_oleh='admin' "
        "WHERE pelanggan_id=? AND status_bayar != 'lunas'",
        (datetime.now().strftime("%d-%m-%Y %H:%M"), pelanggan_id),
    ).rowcount
    db.execute(
        """INSERT INTO audit_log
           (petugas, pelanggan_id, nomor_meteran, periode, meteran_awal, meteran_akhir,
            pemakaian_m3, anomali, sumber, keterangan)
           VALUES ('admin', ?, ?, NULL, NULL, NULL, NULL, 0, 'pembayaran', ?)""",
        (pelanggan_id, p["nomor_meteran"], f"Lunasi semua: {n} tagihan {p['nama']}"),
    )
    db.commit()
    flash(f"Semua tunggakan {p['nama']} ditandai lunas ({n} tagihan).", "success")
    return redirect(_kembali_aman("laporan"))


@app.route("/tagihan/batch-lunas", methods=["POST"])
@perlu_auth_admin
def batch_lunas():
    db = get_db()
    ids = [int(i) for i in request.form.getlist("pilih_id") if i.isdigit()]
    if not ids:
        flash("Pilih minimal satu tagihan terlebih dahulu.", "warning")
        return redirect(_kembali_aman("tagihan"))
    tempat = ",".join("?" * len(ids))
    db.execute(
        f"UPDATE tagihan SET status_bayar='lunas', waktu_bayar=?, dicatat_oleh='admin' WHERE id IN ({tempat})",
        [datetime.now().strftime("%d-%m-%Y %H:%M")] + ids,
    )
    # audit per tagihan
    rows = db.execute(
        f"SELECT t.*, p.nomor_meteran FROM tagihan t JOIN pelanggan p ON p.id=t.pelanggan_id WHERE t.id IN ({tempat})",
        ids,
    ).fetchall()
    for t in rows:
        db.execute(
            """INSERT INTO audit_log
               (petugas, pelanggan_id, nomor_meteran, periode, meteran_awal, meteran_akhir,
                pemakaian_m3, anomali, sumber, keterangan)
               VALUES ('admin', ?, ?, ?, NULL, NULL, ?, 0, 'pembayaran', ?)""",
            (t["pelanggan_id"], t["nomor_meteran"], t["periode"], t["pemakaian_m3"],
             f"Tagihan {periode_label(t['periode'])} ditandai lunas {rupiah(t['total_tagihan'])} (batch)"),
        )
    db.commit()
    flash(f"{len(ids)} tagihan ditandai lunas sekaligus.", "success")
    return redirect(_kembali_aman("tagihan"))


@app.route("/tagihan/<int:tagihan_id>/struk")
@perlu_auth_admin
def struk_admin(tagihan_id):
    db = get_db()
    tagihan = db.execute("SELECT * FROM tagihan WHERE id=?", (tagihan_id,)).fetchone()
    if not tagihan:
        flash("Tagihan tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="tagihan"))
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (tagihan["pelanggan_id"],)).fetchone()
    catat = db.execute(
        "SELECT * FROM pencatatan WHERE pelanggan_id=? AND periode=?",
        (p["id"], tagihan["periode"]),
    ).fetchone()
    rincian = json.loads(tagihan["rincian_tarif"]) if tagihan["rincian_tarif"] else []
    tunggakan_lain = db.execute(
        "SELECT COALESCE(SUM(total_tagihan),0) t FROM tagihan "
        "WHERE pelanggan_id=? AND status_bayar != 'lunas' AND periode != ?",
        (p["id"], tagihan["periode"]),
    ).fetchone()["t"]
    return render_template(
        "struk.html", p=p, catat=catat, tagihan=tagihan, rincian=rincian,
        periode_label_str=periode_label(tagihan["periode"]),
        tunggakan_lain=tunggakan_lain,
        nama_instansi=get_pengaturan(db, "nama_instansi"),
        alamat_instansi=get_pengaturan(db, "alamat_instansi"),
        telepon_instansi=get_pengaturan(db, "telepon_instansi"),
        dicetak_oleh="Admin",
        waktu_cetak=datetime.now().strftime("%d-%m-%Y %H:%M"),
    )


@app.route("/tagihan/export")
@perlu_auth_admin
def export_tagihan():
    db = get_db()
    periode = periode_sekarang()
    tf = filter_tagihan(request.args, periode)
    where_sql, params = tf["where_sql"], tf["params"]
    rows = db.execute(
        f"""SELECT t.*, p.nama, p.nomor_meteran, p.rt, p.rw, p.golongan_tarif
            FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id
            WHERE {where_sql} ORDER BY p.rw, p.rt, p.nama""",
        params,
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Periode", "No. Meteran", "Nama", "RW", "RT", "Golongan", "Jenis",
                     "Pemakaian (m3)", "Total Tagihan", "Status Bayar", "Waktu Bayar", "Dicatat Oleh"])
    for r in rows:
        writer.writerow([r["periode"], r["nomor_meteran"], r["nama"], r["rw"], r["rt"],
                         r["golongan_tarif"], r["jenis"], r["pemakaian_m3"],
                         r["total_tagihan"], r["status_bayar"], r["waktu_bayar"] or "", r["dicatat_oleh"] or ""])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=tagihan_{periode}.csv"},
    )


# =========================================================
# AKSI: TARIF
# =========================================================
def _baca_form_tarif():
    """Ambil & konversi field form tarif. Mengembalikan (data, pesan_error)."""
    try:
        golongan = request.form["golongan_tarif"].strip()
        bawah = int(request.form["batas_bawah"])
        atas_raw = request.form.get("batas_atas", "").strip()
        atas = int(atas_raw) if atas_raw else None
        harga = int(request.form["harga_per_m3"])
    except (KeyError, ValueError):
        return None, "Batas dan harga harus berupa angka."
    if not golongan:
        return None, "Golongan tarif wajib diisi."
    return {"golongan": golongan, "bawah": bawah, "atas": atas, "harga": harga}, None


@app.route("/tarif/tambah", methods=["POST"])
@perlu_auth_admin
def tambah_tarif():
    db = get_db()
    data, err = _baca_form_tarif()
    if err:
        flash(err, "danger")
        return redirect(url_for("index", tab="tarif"))
    err = validasi_blok_tarif(db, data["golongan"], data["bawah"], data["atas"], data["harga"])
    if err:
        flash(err, "danger")
        return redirect(url_for("index", tab="tarif"))
    db.execute(
        "INSERT INTO tarif (golongan_tarif, batas_bawah, batas_atas, harga_per_m3) VALUES (?,?,?,?)",
        (data["golongan"], data["bawah"], data["atas"], data["harga"]),
    )
    db.commit()
    flash("Blok tarif baru ditambahkan.", "success")
    return redirect(url_for("index", tab="tarif"))


@app.route("/tarif/<int:tarif_id>/ubah", methods=["POST"])
@perlu_auth_admin
def ubah_tarif(tarif_id):
    db = get_db()
    t = db.execute("SELECT * FROM tarif WHERE id=?", (tarif_id,)).fetchone()
    if not t:
        flash("Blok tarif tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="tarif"))
    data, err = _baca_form_tarif()
    if err:
        flash(err, "danger")
        return redirect(url_for("index", tab="tarif", tarif_edit=tarif_id))
    err = validasi_blok_tarif(db, data["golongan"], data["bawah"], data["atas"], data["harga"], kecuali_id=tarif_id)
    if err:
        flash(err, "danger")
        return redirect(url_for("index", tab="tarif", tarif_edit=tarif_id))
    db.execute(
        "UPDATE tarif SET golongan_tarif=?, batas_bawah=?, batas_atas=?, harga_per_m3=? WHERE id=?",
        (data["golongan"], data["bawah"], data["atas"], data["harga"], tarif_id),
    )
    db.commit()
    flash("Blok tarif berhasil diperbarui.", "success")
    return redirect(url_for("index", tab="tarif"))


@app.route("/pengaturan/simpan", methods=["POST"])
@perlu_auth_admin
def simpan_pengaturan():
    db = get_db()
    try:
        nilai_abodemen = int(request.form["abodemen"])
    except (KeyError, ValueError):
        flash("Nilai abodemen harus berupa angka.", "danger")
        return redirect(url_for("index", tab="pengaturan"))
    if nilai_abodemen < 0:
        flash("Nilai abodemen tidak boleh negatif.", "danger")
        return redirect(url_for("index", tab="pengaturan"))

    data = {
        "abodemen": str(nilai_abodemen),
        "nama_instansi": request.form.get("nama_instansi", "").strip(),
        "alamat_instansi": request.form.get("alamat_instansi", "").strip(),
        "telepon_instansi": request.form.get("telepon_instansi", "").strip(),
        "website_instansi": request.form.get("website_instansi", "").strip(),
    }
    for kunci, nilai in data.items():
        db.execute(
            "INSERT INTO pengaturan (kunci, nilai) VALUES (?, ?) "
            "ON CONFLICT(kunci) DO UPDATE SET nilai=excluded.nilai",
            (kunci, nilai),
        )
    db.commit()
    flash(f"Pengaturan disimpan. Abodemen {rupiah(nilai_abodemen)} berlaku untuk pencatatan & "
          f"generate tagihan berikutnya — tagihan yang sudah dibuat tidak berubah.", "success")
    return redirect(url_for("index", tab="pengaturan"))


@app.route("/tarif/<int:tarif_id>/hapus", methods=["POST"])
@perlu_auth_admin
def hapus_tarif(tarif_id):
    db = get_db()
    t = db.execute("SELECT * FROM tarif WHERE id=?", (tarif_id,)).fetchone()
    if not t:
        flash("Blok tarif tidak ditemukan.", "danger")
        return redirect(url_for("index", tab="tarif"))
    db.execute("DELETE FROM tarif WHERE id=?", (tarif_id,))
    db.commit()
    flash(f"Blok {t['golongan_tarif']} {t['batas_bawah']}–{t['batas_atas'] or 'ke atas'} dihapus.", "success")
    return redirect(url_for("index", tab="tarif"))


# =========================================================
# LAPORAN: EXPORT CSV (memakai filter query-string yang sama dengan tab Laporan)
# =========================================================
@app.route("/laporan/export")
@perlu_auth_admin
def export_laporan():
    db = get_db()
    periode = periode_sekarang()
    lf = filter_laporan(request.args, periode, daftar_periode(db))
    l_awal, l_akhir = lf["l_awal"], lf["l_akhir"]
    where_sql, params = lf["where_sql"], lf["params"]

    rows = db.execute(
        f"""SELECT t.periode, p.nomor_meteran, p.nama, p.rw, p.rt, p.golongan_tarif,
                   t.jenis, t.pemakaian_m3, t.total_tagihan, t.status_bayar
            FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id
            WHERE {where_sql}
            ORDER BY t.periode DESC, p.rw, p.rt, p.nama""",
        params,
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Periode", "No. Meteran", "Nama", "RW", "RT", "Golongan",
                      "Jenis", "Pemakaian (m3)", "Total Tagihan", "Status Bayar"])
    for r in rows:
        writer.writerow([r["periode"], r["nomor_meteran"], r["nama"], r["rw"], r["rt"],
                          r["golongan_tarif"], r["jenis"], r["pemakaian_m3"],
                          r["total_tagihan"], r["status_bayar"]])

    filename = f"laporan_pamsimas_{l_awal}_sd_{l_akhir}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/laporan/cetak")
@perlu_auth_admin
def cetak_laporan():
    """Halaman cetak: SEMUA baris transaksi sesuai filter (tanpa paginasi) + ringkasan."""
    db = get_db()
    periode = periode_sekarang()
    lf = filter_laporan(request.args, periode, daftar_periode(db))
    where_sql, params = lf["where_sql"], lf["params"]
    base_from = "FROM tagihan t JOIN pelanggan p ON p.id = t.pelanggan_id WHERE " + where_sql

    agg = dict(db.execute(
        f"""SELECT COUNT(*) n, COALESCE(SUM(t.total_tagihan),0) total,
                   COALESCE(SUM(CASE WHEN t.status_bayar='lunas' THEN t.total_tagihan ELSE 0 END),0) lunas,
                   COALESCE(SUM(t.pemakaian_m3),0) total_m3
            {base_from}""",
        params,
    ).fetchone())
    agg["belum"] = agg["total"] - agg["lunas"]

    rows = db.execute(
        f"""SELECT t.*, p.nama, p.nomor_meteran, p.rt, p.rw, p.golongan_tarif
            {base_from} ORDER BY t.periode DESC, p.rw, p.rt, p.nama""",
        params,
    ).fetchall()

    return render_template(
        "laporan_cetak.html", rows=rows, agg=agg, lf=lf,
        nama_instansi=get_pengaturan(db, "nama_instansi"),
        alamat_instansi=get_pengaturan(db, "alamat_instansi"),
        telepon_instansi=get_pengaturan(db, "telepon_instansi"),
        waktu_cetak=datetime.now().strftime("%d-%m-%Y %H:%M"),
    )


# =========================================================
# MODE PETUGAS: login, dashboard, struk cetak termal 80mm
# =========================================================
@app.route("/")
@app.route("/petugas")
def petugas_login():
    if "petugas" in session:
        return redirect(url_for("petugas_dashboard"))
    db = get_db()
    return render_template(
        "petugas_login.html",
        daftar_petugas=daftar_petugas(db),
        turnstile_site_key=TURNSTILE_SITE_KEY,
        nama_instansi=get_pengaturan(db, "nama_instansi"),
    )


@app.route("/petugas/masuk", methods=["POST"])
def petugas_masuk():
    nama = (request.form.get("petugas_pilih") or "").strip()
    pin = (request.form.get("pin") or "").strip()
    if not nama:
        flash("Silakan pilih nama petugas terlebih dahulu.", "danger")
        return redirect(url_for("petugas_login"))
    if TURNSTILE_SITE_KEY and not verifikasi_turnstile(request.form.get("cf-turnstile-response", "")):
        flash("Verifikasi captcha gagal. Silakan coba lagi.", "danger")
        return redirect(url_for("petugas_login"))
    if pin != PIN_PETUGAS:
        flash("PIN salah. Silakan coba lagi.", "danger")
        return redirect(url_for("petugas_login"))
    session["petugas"] = nama
    return redirect(url_for("petugas_dashboard"))


@app.route("/petugas/keluar")
def petugas_keluar():
    session.pop("petugas", None)
    return redirect(url_for("petugas_login"))


@app.route("/petugas/dashboard")
def petugas_dashboard():
    if "petugas" not in session:
        return redirect(url_for("petugas_login"))
    db = get_db()
    nama_petugas = session["petugas"]
    periode = periode_sekarang()

    rw = request.args.get("rw", "")
    rt = request.args.get("rt", "")
    golongan = request.args.get("golongan", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip()
    page = get_int_arg("page", 1)

    where, params = ["aktif = 1", "petugas = ?"], [nama_petugas]
    if rw:
        where.append("rw = ?"); params.append(rw)
    if rt:
        where.append("rt = ?"); params.append(rt)
    if golongan:
        where.append("golongan_tarif = ?"); params.append(golongan)
    if q:
        where.append("(nama LIKE ? OR nomor_meteran LIKE ? OR alamat LIKE ?)")
        like = f"%{q}%"; params += [like, like, like]
    if status == "tercatat":
        where.append("id IN (SELECT pelanggan_id FROM pencatatan WHERE periode=?)"); params.append(periode)
    elif status == "belum":
        where.append("id NOT IN (SELECT pelanggan_id FROM pencatatan WHERE periode=?)"); params.append(periode)
    where_sql = " AND ".join(where)

    total = db.execute(f"SELECT COUNT(*) c FROM pelanggan WHERE {where_sql}", params).fetchone()["c"]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    daftar = db.execute(
        f"SELECT * FROM pelanggan WHERE {where_sql} ORDER BY rw, rt, nama LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    tercatat_map = {r["pelanggan_id"]: r["id"] for r in db.execute(
        "SELECT id, pelanggan_id FROM pencatatan WHERE periode=?", (periode,)
    ).fetchall()}

    agg = db.execute(
        """SELECT COUNT(*) total_binaan,
                  SUM(CASE WHEN id IN (SELECT pelanggan_id FROM pencatatan WHERE periode=?) THEN 1 ELSE 0 END) sdh_catat
           FROM pelanggan WHERE aktif=1 AND petugas=?""",
        (periode, nama_petugas),
    ).fetchone()
    total_binaan = agg["total_binaan"] or 0
    sdh_catat = agg["sdh_catat"] or 0

    total_nominal = db.execute(
        """SELECT COALESCE(SUM(t.total_tagihan),0) tot FROM tagihan t
           JOIN pelanggan p ON p.id=t.pelanggan_id
           WHERE t.periode=? AND p.petugas=?""",
        (periode, nama_petugas),
    ).fetchone()["tot"]

    # data ringan untuk pencarian & prefill JS di form pencatatan (hanya pelanggan petugas ini)
    semua_binaan = db.execute(
        """SELECT p.id, p.nomor_meteran, p.nama, p.alamat, p.rw, p.rt, p.golongan_tarif, p.meteran_awal,
                  (SELECT meteran_akhir FROM pencatatan WHERE pelanggan_id=p.id ORDER BY periode DESC LIMIT 1) last_akhir,
                  (SELECT 1 FROM pencatatan WHERE pelanggan_id=p.id AND periode=?) sudah_periode_ini,
                  (SELECT AVG(pakai) FROM (
                       SELECT (meteran_akhir - meteran_awal) pakai FROM pencatatan
                       WHERE pelanggan_id=p.id AND periode < ? ORDER BY periode DESC LIMIT 6
                   )) rata2_pakai,
                  (SELECT COALESCE(SUM(total_tagihan),0) FROM tagihan t
                   WHERE t.pelanggan_id=p.id AND t.status_bayar != 'lunas') tunggakan,
                  (SELECT COUNT(*) FROM tagihan t
                   WHERE t.pelanggan_id=p.id AND t.status_bayar != 'lunas') n_tunggakan
           FROM pelanggan p WHERE p.aktif=1 AND p.petugas=? ORDER BY p.rw, p.rt, p.nama""",
        (periode, periode, nama_petugas),
    ).fetchall()

    return render_template(
        "petugas_dashboard.html",
        nama_petugas=nama_petugas, periode=periode, periode_now_label=periode_label(periode),
        daftar=daftar, tercatat_map=tercatat_map,
        total=total, page=page, total_pages=total_pages,
        rw=rw, rt=rt, golongan=golongan, status=status, q=q,
        rw_list=daftar_rw(db), rt_list=daftar_rt(db, rw) if rw else daftar_rt(db),
        golongan_list=daftar_golongan(db),
        total_binaan=total_binaan, sdh_catat=sdh_catat, belum_catat=total_binaan - sdh_catat,
        total_nominal=total_nominal,
        pelanggan_json=json.dumps([dict(r) for r in semua_binaan]),
        tarif_json=build_tarif_json(db),
        anomali_absolut=ANOMALI_ABSOLUT_M3,
        prefill=request.args.get("prefill", ""),
    )


@app.route("/petugas/struk/<int:pelanggan_id>")
def petugas_struk(pelanggan_id):
    db = get_db()
    periode = periode_sekarang()
    p = db.execute("SELECT * FROM pelanggan WHERE id=?", (pelanggan_id,)).fetchone()
    catat = db.execute(
        "SELECT * FROM pencatatan WHERE pelanggan_id=? AND periode=?", (pelanggan_id, periode)
    ).fetchone()
    tagihan = db.execute(
        "SELECT * FROM tagihan WHERE pelanggan_id=? AND periode=?", (pelanggan_id, periode)
    ).fetchone()
    if not p or not catat or not tagihan:
        flash("Pelanggan ini belum tercatat pada periode berjalan, struk tidak dapat dicetak.", "danger")
        return redirect(url_for("petugas_dashboard"))

    rincian = json.loads(tagihan["rincian_tarif"]) if tagihan["rincian_tarif"] else []
    tunggakan_lain = db.execute(
        "SELECT COALESCE(SUM(total_tagihan),0) t FROM tagihan "
        "WHERE pelanggan_id=? AND status_bayar != 'lunas' AND periode != ?",
        (pelanggan_id, periode),
    ).fetchone()["t"]
    return render_template(
        "struk.html", p=p, catat=catat, tagihan=tagihan, rincian=rincian,
        periode_label_str=periode_label(periode),
        tunggakan_lain=tunggakan_lain,
        nama_instansi=get_pengaturan(db, "nama_instansi"),
        alamat_instansi=get_pengaturan(db, "alamat_instansi"),
        telepon_instansi=get_pengaturan(db, "telepon_instansi"),
        dicetak_oleh=session.get("petugas", "-"),
        waktu_cetak=datetime.now().strftime("%d-%m-%Y %H:%M"),
    )


if __name__ == "__main__":
    init_db()
    # debug default aktif untuk pengembangan lokal; matikan lewat env FLASK_DEBUG=0 (mis. di Docker)
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, host="0.0.0.0", port=5000)