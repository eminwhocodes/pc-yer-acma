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

Amacım hazır “disk cleaner” kullanmak değil; **kendime göre bir panel** kurup Docker ne kadar, Drive yedekleri ne kadar, update ne kadar diye kategori kategori görmekti.

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
- Loglar: `logs/` (7 günden eski otomatik silinir)

## Lisans / kullanım

Dilediğiniz gibi **fork edip kullanabilir**, değiştirebilir veya **ticarileştirebilirsiniz**. Kısıtlama koymuyorum.

## Gereksinimler

- Windows
- Python 3
- (Opsiyonel) Docker CLI, pystray + Pillow (tray için)
