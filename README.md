# ALTAY VIP BOT — Tek Kanal Sürümü

Bu proje VIP başvurusu/onayı, üyelik süresi, otomatik süre bitişi, kanal duyurusu ve zamanlanmış duyuru içerir.

## Telefonla kurulum
iPhone'da bir bulut Python servisi kullanacağız. Bu klasördeki dosyaları servise yükle:
- `app.py`
- `requirements.txt`

Servisin Environment Variables / Variables bölümüne `.env.example` içindeki değerleri tek tek ekle:
- `BOT_TOKEN`: BotFather tokenı (kimseyle paylaşma)
- `ADMIN_IDS`: `8011205445`
- `CHANNEL_ID`: Özel kanalın sayısal ID'si (genellikle `-100` ile başlar)
- `TIMEZONE`: `Europe/Istanbul`

Başlatma komutu: `python app.py`

## Kanal ID'sini bulma
Bot kanalda yönetici olduktan sonra kanalında yeni bir test gönderisi paylaş.
Bot tokenını hiçbir siteye veya kişiye verme. Kanal ID'sini almak için güvenilir bir Telegram ID aracı kullanabilir veya sunucuda geçici olarak Bot API `getUpdates` çıktısını kontrol edebiliriz. Özel kanal davet bağlantısı, sayısal kanal ID'sinin yerine geçmez.

## Bot komutları
- `/start` — kullanıcı menüsü
- `/admin` — yönetici komutları
- `/uyeler` — aktif VIP üyeler
- `/onayla KULLANICI_ID GÜN` — manuel VIP üyelik
- `/duyuru MESAJ` — kanala mesaj gönder
- `/planla YYYY-AA-GG SS:DD | MESAJ` — duyuru planla
- `/planlar` — bekleyen planlar
- `/iptal PLAN_ID` — planı iptal et

## Önemli
Bot kanalda yönetici olmalı. Üyelik süresi biten kişiyi çıkarmak için kullanıcıları engelleme/kaldırma izni gerekir.
İlk sürümde ödeme alınmaz; yönetici onayıyla üyelik verilir. Tokenı sohbetlerde paylaşma.
