# Disk Temizlik Mini App — Design

**Date:** 2026-08-04  
**Folder:** `pc-yer-acma`

## Goal

Windows C: sürücüsünde yer kaplayanları kategori kategori göstermek; güvenli temizliği (özellikle Docker CLI ile) tek tıkla yapmak.

## Constraints

- Python stdlib only (pip yok — disk kritik dolu)
- Yerel HTTP UI (`127.0.0.1`)
- Hibrit tarama: hızlı kategoriler önce, derin C: tarama arka planda
- Docker: dosya silme değil, `docker` CLI prune/liste

## Architecture

- `app.py` — HTTP sunucu + API + tarayıcı açma
- `scanner/` — kategori tarayıcıları + derin tarama
- `cleaner.py` — güvenli silme aksiyonları
- `static/` — HTML/CSS/JS UI

## Categories

Docker, Cursor, BlueStacks, LDPlayer, Google Drive, Temp, AppData büyükleri, Windows, Recycle Bin, npm/pip cache, WSL, derin tarama sonuçları.

## Safe clean

Temp, Recycle Bin, Docker prune (container/dangling image/unused volume/build cache), opsiyonel unused images (`-a`), Cursor cache. Emülatör/Drive: sadece rapor.
