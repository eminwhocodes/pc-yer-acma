# PC Yer Açma

Kendi ihtiyaçlarıma göre şekillendirdiğim bir **PC temizleme / disk analiz** aracı.

## Neden?

Windows diskim doluyordu; neyin ne kadar yer tuttuğunu tek bakışta görmek istedim:

- Docker (engine + VHDX)
- Google Drive / OneDrive çevrimdışı ve cache
- BlueStacks / LDPlayer
- Cursor / paket cache’leri
- Temp, Windows Update, AppData büyükleri
- İstersen C:\ derin tarama

Amacım hazır “disk cleaner” kullanmak değil; **kendime göre bir panel** kurup Docker ne kadar, Drive yedekleri ne kadar, update ne kadar diye kategori kategori görmekti. Ayrıca `c:\www` ve `C:\wamp64\www` altındaki kendi projelerimin boyutunu da buradan takip ediyorum.

## Kullanım

Yönetici olarak:

```bat
baslat.bat
```

veya:

```bat
python app.py --tray
```

Tarayıcı: `http://127.0.0.1:8765/`  
Sistem tray’den tarayıcı / konsol / çıkış.

## Ne yapar?

- Kategori taraması + önbellek (eski sonuç anında, gerekirse arka planda yeniler)
- Docker: `docker system df`, image listesi, prune kuyruğu, VHDX (disk) vs engine farkı
- Güvenli temizlik kuyruğu (temp, recycle, docker prune, VHDX küçült…)
- Drive / OneDrive çevrimdışı-cache kırılımı
- **Dosyalar** sekmesi: Masaüstü, İndirilenler, Belgeler, Resimler, Videolar, Müzik (+ istenen ek klasörler) için
  - kopya klasörler (alt ağacın ad + boyut + içerik imzası aynı olanlar),
  - kopya dosyalar (adı farklı olsa da içerik aynı: boyut → baş/son 64 KB → tam BLAKE2b hash; hash önbelleği `data/hash_cache.json`),
  - 100 MB üstü dosyalar,
  - gereksiz olabilecekler (kurulum dosyaları, açılmış/eski arşivler, yarım indirmeler, eski indirmeler, "Kopya"/"(1)" adlılar).
  Her grupta tutulacak olan önerilir; seçilenler yalnız **Geri Dönüşüm Kutusu'na** taşınır (kalıcı silme yok). Bulutta duran (indirilmemiş) dosyalar okunmaz.
  Komut satırından: `python -m scanner.files [klasör ...] --json sonuc.json`
- Loglar: `logs/` (7 günden eski otomatik silinir)

## Lisans / kullanım

Dilediğiniz gibi **fork edip kullanabilir**, değiştirebilir veya **ticarileştirebilirsiniz**. Kısıtlama koymuyorum.

## Gereksinimler

- Windows
- Python 3
- (Opsiyonel) Docker CLI, pystray + Pillow (tray için)
