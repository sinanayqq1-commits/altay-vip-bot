# MEDYAECAN BOT — Kurulum ve Devir Rehberi

## 1. Gereksinimler

- Python 3.11+ veya uyumlu güncel Python
- Telegram BotFather üzerinden oluşturulmuş bot
- Botu yönetecek Telegram kullanıcı ID'leri
- Yönetilecek Telegram kanalları/grupları
- Kalıcı disk veya kalıcı veritabanı alanı

## 2. Bağımlılık kurulumu

```bash
pip install -r requirements.txt
```

## 3. Ortam değişkenleri

| Değişken | Zorunluluk | Açıklama |
|---|---|---|
| BOT_TOKEN | Zorunlu | Telegram bot tokenı |
| ADMIN_IDS | Zorunlu | Ana yöneticilerin virgülle ayrılmış Telegram ID'leri |
| DB_PATH | Önerilen | SQLite veritabanı dosyasının tam yolu |
| TIMEZONE | Opsiyonel | Varsayılan Europe/Istanbul |
| CHANNEL_ID | Eski tek kanal kullanımı | Eski akışlarla geriye dönük uyumluluk |
| MEDYAECAN_CUSTOMER_CHANNEL_ID | Opsiyonel | Satın alan müşterilere davet gönderilecek kanal ID'si |

Token ve yönetici ID'leri kaynak koda yazılmamalı, güvenli ortam değişkenleri olarak saklanmalıdır.

## 4. Kalıcı veritabanı

Railway'de disk `/data` yoluna bağlanmışsa örnek DB yolu:

```text
DB_PATH=/data/medyaecan.sqlite3
```

Bu değer yalnızca örnektir. Mevcut üretim veritabanının gerçek yolu doğrulanmadan değiştirilmemelidir. Yanlış DB_PATH yeni/boş bir veritabanı oluşturup mevcut müşteri kayıtlarının görünmemesine neden olabilir.

## 5. Telegram izinleri

Bot, yönetilecek her kanal veya grupta gereken işlemlere uygun yönetici izinleriyle eklenmelidir. VIP kanalda davet bağlantısı oluşturma ve üyelik bitişinde kullanıcıyı çıkarma izinleri ayrıca kontrol edilmelidir.

## 6. İlk müşteri kurulumu

1. Ana yönetici botu başlatır.
2. Müşteri kaydı oluşturulur ve müşteri sahibi Telegram kullanıcı ID'si tanımlanır.
3. Müşteri sahibi botu başlatır.
4. Sohbet grubu ve yayın kanalları doğru müşteri altında kaydedilir.
5. Ekip rolleri yalnızca ihtiyaç duyulan kanallara atanır.
6. Deneme/lisans süresi ve paket limitleri doğrulanır.
7. VIP başvurusu, onayı, davet bağlantısı ve üyelik bitişi test edilir.

## 7. Yedekleme ve geri yükleme

Uygulamada haftalık otomatik yedek ve ana yönetici için manuel `/yedekle` komutu bulunur. Yedek dosyaları Telegram üzerinden yöneticiye gönderilir ve uygulamanın geçici dizininden silinir.

Devir öncesi:
- Yedek dosyasının gerçekten alındığını kontrol edin.
- Dosyayı güvenli, erişimi kısıtlı bir yerde saklayın.
- Üretimden ayrı test ortamında SQLite geri yüklemesi yapın.
- Geri yüklenen veritabanında tenant, kanal, üyelik ve lisans kayıtlarını kontrol edin.

## 8. Güvenli devir

- Bot tokenı devir sırasında yenilenmeli.
- Eski ana yöneticilerin yetkileri kaldırılmalı.
- GitHub ve Railway erişimleri yeni sahibin hesabına aktarılmalı.
- Veritabanı yedeği şifreli ve güvenli kanaldan teslim edilmeli.
- Gerçek müşteri kişisel verileri aktarılmadan önce hukuki dayanak ve müşteri bilgilendirmesi kontrol edilmeli.
- Destek süresi, hata düzeltme kapsamı ve yeni özelliklerin ücretlendirmesi yazılı belirlenmeli.
