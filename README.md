# SIMAS — Sistem Informasi Pamsimas

Aplikasi web pengelolaan tagihan air bersih (PAMSIMAS): pencatatan meteran oleh petugas lapangan, tagihan tarif progresif + abodemen bulanan, pembayaran, laporan, dan cetak struk termal 80mm.

Dibuat oleh [kipli.net](https://kipli.net).

## Fitur Utama

### Mode Petugas (lapangan)
- Login pilih nama + PIN, dilindungi Cloudflare Turnstile
- Dashboard binaan: ringkasan, tabel pelanggan, pencatatan wizard RW → RT → Nama
- Deteksi anomali pemakaian (lonjakan/penurunan drastis) dengan konfirmasi + alasan
- Foto bukti meteran wajib (disimpan server)
- **Mode offline**: data + foto diarsipkan di IndexedDB perangkat, dikirim ulang saat sinyal ada (satuan atau kirim semua)
- Cetak struk 80mm

### Mode Admin (`/admin`, HTTP Basic Auth)
- **Pelanggan**: tambah/ubah/nonaktifkan, cek duplikat nomor meteran realtime, import massal CSV, export CSV, filter & sorting, halaman detail + riwayat bulanan
- **Pencatatan**: form wizard sama seperti petugas, pintasan "Catat" dari tabel pelanggan
- **Tagihan**: generate bulanan, tandai lunas satuan/batch, batalkan lunas, catatan waktu & pencatat pembayaran, rincian tarif per tagihan, cetak struk, export CSV
- **Tarif Progresif**: blok tarif per golongan (validasi anti tumpang-tindih/celah), pengaturan nilai abodemen, kalkulator simulasi tagihan
- **Laporan**: rentang periode, filter RW/RT/golongan/jenis/status, grafik tren bulanan (Chart.js), rekap per RW & golongan, tunggakan per pelanggan, halaman cetak, export CSV

### Aturan tagihan
- Tarif progresif per golongan (blok m³)
- **Abodemen berlaku setiap bulan** untuk semua pelanggan (nilai bisa diubah di menu Tarif)
- Pelanggan tanpa pencatatan pada suatu bulan otomatis dikenakan abodemen saja

## Teknologi

- Python 3.10+ / Flask
- SQLite (mode WAL) — tanpa server database
- Bootstrap 5 + Chart.js (CDN)
- IndexedDB (arsip offline petugas)

## Instalasi Lokal

```bash
pip install -r requirements.txt
python app.py
```

Buka:
- Login petugas: http://127.0.0.1:5000/
- Admin: http://127.0.0.1:5000/admin

Saat pertama kali dijalankan, database otomatis dibuat dan diisi **data dummy 6 bulan** (60 pelanggan, pencatatan & tagihan) untuk uji coba.

## Instalasi Docker (dengan Cloudflare Tunnel)

`docker-compose.yml` sudah dikonfigurasi bergabung ke network `cloudflared` (external), dengan volume `simas_data` untuk database & foto.

```bash
# pastikan network cloudflared sudah ada (biasanya dibuat container cloudflared)
docker network create cloudflared   # bila belum ada

# atur kredensial lewat environment (opsional, ada nilai default)
export ADMIN_USER=admin
export ADMIN_PASS=rahasia-admin
export PIN_PETUGAS=rahasia-pin
export SECRET_KEY=rahasia-session

docker compose up -d --build
```

Volume baru yang kosong otomatis di-seed data dummy saat start pertama. Arahkan Cloudflare Tunnel ke `simas:5000`.

## Kredensial Default

> Untuk uji coba. **Ganti sebelum dipakai sungguhan** lewat environment variable (lihat tabel).

| Fungsi | Default | Environment |
|---|---|---|
| Admin (Basic Auth) | `admin` / `12345678` | `ADMIN_USER`, `ADMIN_PASS` |
| PIN petugas | `123456` | `PIN_PETUGAS` |
| Session secret | `pamsimas-secret-key-ubah-ini` | `SECRET_KEY` |
| Turnstile | dummy key Cloudflare (selalu lolos) | `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` di `app.py` |

## Struktur Proyek

```
app.py                    # backend Flask (rute, logika tarif, anomali, seed)
templates/                # halaman admin, detail pelanggan, login petugas,
                          # dashboard petugas, struk, halaman cetak laporan
static/uploads/           # foto bukti meteran
requirements.txt
Dockerfile
docker-compose.yml
```
