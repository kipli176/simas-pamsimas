/* PWA SIMAS: registrasi service worker + tombol install mengambang */
(function () {
  // 1. registrasi service worker
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js').catch(function () {});
    });
  }

  // 2. tombol install mengambang (muncul bila belum terinstall & browser mendukung)
  var deferredPrompt = null;
  var isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  var isiOS = /iphone|ipad|ipod/i.test(navigator.userAgent);

  function buatTombol() {
    if (document.getElementById('btnInstallPwa')) return;
    var style = document.createElement('style');
    style.textContent =
      '.btn-install-pwa{position:fixed;right:16px;bottom:16px;z-index:1050;display:inline-flex;align-items:center;gap:8px;' +
      'background:linear-gradient(120deg,#063a4f,#0e7fae);color:#fff;border:none;border-radius:999px;padding:12px 18px;' +
      'font-weight:700;font-size:.85rem;box-shadow:0 8px 24px rgba(6,58,79,.4);cursor:pointer;}' +
      '.btn-install-pwa:hover{filter:brightness(1.1);}';
    document.head.appendChild(style);

    var btn = document.createElement('button');
    btn.id = 'btnInstallPwa';
    btn.className = 'btn-install-pwa';
    btn.innerHTML = '<i class="bi bi-download"></i> Install Aplikasi';
    btn.addEventListener('click', function () {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(function () {
          deferredPrompt = null;
          btn.remove();
        });
      } else if (isiOS) {
        alert('Cara install di iPhone/iPad:\n\n1. Buka menu Bagikan (ikon kotak dengan panah ke atas)\n2. Pilih "Tambahkan ke Layar Utama"\n3. Ketuk "Tambah"');
      }
    });
    document.body.appendChild(btn);
  }

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    if (!isStandalone) buatTombol();
  });

  window.addEventListener('appinstalled', function () {
    var btn = document.getElementById('btnInstallPwa');
    if (btn) btn.remove();
  });

  // iOS tidak mendukung beforeinstallprompt -> tampilkan tombol berisi petunjuk
  if (!isStandalone && isiOS) {
    window.addEventListener('load', function () {
      setTimeout(buatTombol, 800);
    });
  }
})();
