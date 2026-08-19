/**
 * foto.js — Kamera langsung / galeri + resize foto di sisi browser.
 * Murni JavaScript (canvas bawaan browser), tanpa library eksternal.
 *
 * - resizeFoto(): mengecilkan foto ke maks 1280px sisi terpanjang, output
 *   JPEG kualitas 0.75, mempertahankan orientasi EXIF. Hasil resize hanya
 *   dipakai bila ukurannya lebih kecil dari file asli.
 * - pasangFoto(): memasang tombol "Ambil Foto" (input capture="environment"
 *   -> langsung buka kamera belakang di HP) dan "Galeri", preview, info
 *   ukuran file, dan menulis balik file hasil resize ke input via
 *   DataTransfer supaya form multipart yang sudah ada tetap jalan tanpa
 *   diubah.
 */
(function () {
  'use strict';

  var MAX_DIMENSI = 1280;   // sisi terpanjang maksimal hasil resize
  var KUALITAS = 0.75;      // kualitas JPEG

  function ukuran(n) {
    if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return Math.max(1, Math.round(n / 1024)) + ' KB';
  }

  function dukungDataTransfer() {
    try {
      var dt = new DataTransfer();
      return !!dt && typeof dt.items !== 'undefined';
    } catch (e) {
      return false;
    }
  }

  /**
   * Decode file gambar menjadi objek yang bisa digambar ke canvas,
   * dengan orientasi EXIF yang benar bila browser mendukung.
   */
  function decodeGambar(file) {
    if (window.createImageBitmap) {
      // opsi imageOrientation didukung Chrome 81+ / Safari 15+; browser
      // lama melempar error -> ulangi tanpa opsi.
      return createImageBitmap(file, { imageOrientation: 'from-image' })
        .catch(function () { return createImageBitmap(file); });
    }
    // fallback browser sangat lama: pakai <img> + object URL
    return new Promise(function (resolve, reject) {
      var img = new Image();
      var url = URL.createObjectURL(file);
      img.onload = function () {
        resolve({ width: img.naturalWidth, height: img.naturalHeight, _img: img, _url: url });
      };
      img.onerror = function () { reject(new Error('Gagal membaca gambar')); };
      img.src = url;
    });
  }

  /**
   * Resize file gambar (jika lebih besar dari maxDimensi). Mengembalikan
   * Promise<File> — file asli bila resize gagal atau tidak lebih kecil.
   */
  function resizeFoto(file, opsi) {
    opsi = opsi || {};
    var maxDimensi = opsi.maxDimensi || MAX_DIMENSI;
    var kualitas = (opsi.kualitas != null) ? opsi.kualitas : KUALITAS;
    if (!file || !file.type || file.type.indexOf('image/') !== 0) {
      return Promise.resolve(file);
    }
    return decodeGambar(file).then(function (bm) {
      try {
        var w = bm.width, h = bm.height;
        var skala = Math.min(1, maxDimensi / Math.max(w, h));
        w = Math.max(1, Math.round(w * skala));
        h = Math.max(1, Math.round(h * skala));
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(bm._img || bm, 0, 0, w, h);
        if (bm.close) bm.close();
        if (bm._url) URL.revokeObjectURL(bm._url);
        return new Promise(function (resolve) {
          canvas.toBlob(function (blob) {
            if (blob && blob.size < file.size) {
              var nama = (file.name || 'foto').replace(/\.[^.]+$/, '') + '.jpg';
              resolve(new File([blob], nama, { type: 'image/jpeg' }));
            } else {
              resolve(file); // hasil tidak lebih kecil -> pakai asli
            }
          }, 'image/jpeg', kualitas);
        });
      } catch (e) {
        console.warn('[foto] resize gagal, pakai file asli:', e);
        return file;
      }
    }).catch(function (e) {
      console.warn('[foto] decode gagal, pakai file asli:', e);
      return file;
    });
  }

  /**
   * Pasang tombol kamera/galeri + preview + info ukuran pada elemen ber-id:
   *   btnKamera, btnGaleri, inpKamera, inpGaleri, preview, info (semua id)
   *   onUbah(file) dipanggil tiap foto berhasil dipilih/diresize.
   */
  function pasangFoto(opts) {
    var btnKamera = document.getElementById(opts.btnKamera);
    var btnGaleri = document.getElementById(opts.btnGaleri);
    var inpKamera = document.getElementById(opts.inpKamera);
    var inpGaleri = document.getElementById(opts.inpGaleri);
    var preview = document.getElementById(opts.preview);
    var info = document.getElementById(opts.info);
    var onUbah = opts.onUbah || function () {};

    function proses(file, inputAsal) {
      if (!file) return;
      if (info) info.textContent = 'Memproses foto…';
      resizeFoto(file).then(function (hasil) {
        // tulis balik hasil resize ke input asal supaya ikut terkirim
        // (input.files tidak bisa diisi langsung, harus lewat DataTransfer)
        if (hasil !== file && dukungDataTransfer()) {
          try {
            var dt = new DataTransfer();
            dt.items.add(hasil);
            inputAsal.files = dt.files;
          } catch (e) {
            console.warn('[foto] gagal menulis balik hasil resize, kirim file asli:', e);
          }
        }
        // pastikan hanya satu input yang berisi file (nama field sama)
        var lain = (inputAsal === inpKamera) ? inpGaleri : inpKamera;
        if (lain && lain !== inputAsal) lain.value = '';
        if (preview) {
          preview.src = URL.createObjectURL(hasil);
          preview.style.display = 'block';
        }
        if (info) {
          if (hasil !== file) {
            info.textContent = 'Foto dikecilkan: ' + ukuran(file.size) + ' → ' + ukuran(hasil.size);
          } else {
            info.textContent = 'Foto: ' + ukuran(hasil.size);
          }
        }
        onUbah(hasil);
      });
    }

    if (btnKamera && inpKamera) btnKamera.addEventListener('click', function () { inpKamera.click(); });
    if (btnGaleri && inpGaleri) btnGaleri.addEventListener('click', function () { inpGaleri.click(); });
    if (inpKamera) inpKamera.addEventListener('change', function () { proses(this.files[0], this); });
    if (inpGaleri) inpGaleri.addEventListener('change', function () { proses(this.files[0], this); });
  }

  window.FotoTools = { resizeFoto: resizeFoto, pasangFoto: pasangFoto, ukuran: ukuran };
})();
