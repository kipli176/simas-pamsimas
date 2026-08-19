/**
 * cetak_auto.js — Auto-print nota/struk dengan deteksi & fallback.
 *
 * Mengacu pola fallback printer_v3 (thermal): coba metode terbaik dulu,
 * deteksi apakah benar-benar jalan, dan bila gagal/blokir → beri tahu
 * pengguna + tampilkan tombol cetak manual (selalu tersedia).
 *
 * Di browser, window.print() adalah metode universal terakhir yang pasti
 * tersedia di semua platform (desktop, Android, iOS/AirPrint). Bila browser
 * memblokir dialog cetak otomatis (mis. tanpa user gesture), kita deteksi
 * lewat event beforeprint / matchMedia('print') dan memunculkan notifikasi
 * berisi tombol manual.
 *
 * Elemen opsional yang dipakai (berdasarkan id):
 *   #notifCetakBlokir  — kotak peringatan, ditampilkan bila dialog tak muncul
 *   #btnCetakManual    — tombol manual (diberi efek sorot saat blokir)
 */
(function () {
  function cetakOtomatis(opts) {
    opts = opts || {};
    var timeoutMs = opts.timeoutMs || 6000;   // batas tunggu dialog cetak muncul
    var delayMs = opts.delayMs || 250;        // jeda setelah font siap agar layout stabil
    var dialogTerbuka = false;

    function tandaiTerbuka() {
      dialogTerbuka = true;
      var n = document.getElementById('notifCetakBlokir');
      if (n) n.style.display = 'none';
      var b = document.getElementById('btnCetakManual');
      if (b) b.classList.remove('sorot');
    }

    // Deteksi dialog cetak benar-benar terbuka (fallback detection)
    if (window.matchMedia) {
      try {
        var mql = window.matchMedia('print');
        var onMql = function (e) { if (e.matches) tandaiTerbuka(); };
        if (mql.addEventListener) mql.addEventListener('change', onMql);
        else if (mql.addListener) mql.addListener(onMql);
      } catch (e) { /* abaikan */ }
    }
    window.addEventListener('beforeprint', tandaiTerbuka);
    window.addEventListener('afterprint', function () {
      dialogTerbuka = true; // dialog pernah tampil → tidak perlu peringatan blokir
      var n = document.getElementById('notifCetakBlokir');
      if (n) n.style.display = 'none';
    });

    function tampilkanFallback() {
      var n = document.getElementById('notifCetakBlokir');
      if (n) n.style.display = 'block';
      var b = document.getElementById('btnCetakManual');
      if (b) b.classList.add('sorot');
    }

    function mulaiCetak() {
      try {
        window.print();
      } catch (e) {
        // print() melempar exception → langsung fallback manual
        tampilkanFallback();
      }
    }

    // Tunggu font siap supaya ukuran 80mm stabil, lalu buka dialog cetak
    var fontsReady = (document.fonts && document.fonts.ready)
      ? document.fonts.ready
      : Promise.resolve();
    fontsReady.then(function () {
      setTimeout(mulaiCetak, delayMs);
    });

    // Fallback: bila dalam timeoutMs dialog tidak terdeteksi terbuka,
    // anggap diblokir browser → tampilkan tombol manual yang disorot.
    setTimeout(function () {
      if (!dialogTerbuka) tampilkanFallback();
    }, timeoutMs);
  }

  window.cetakOtomatis = cetakOtomatis;
})();
