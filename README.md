# MEDYAECAN BOT

Çok müşterili Telegram VIP topluluk ve kanal yönetim platformu.

> Bu dal ticari hazırlık ve dokümantasyon dalıdır. Canlı üretim dalına otomatik aktarılmaz.

## Özellikler
- Çok müşterili yapı ve müşteri bazlı kanal/grup kaydı
- Müşteri sahibi ve ekip rolleri: manager, moderator, finance, publisher
- Kanal kapsamlı erişim kontrolleri
- VIP başvuru, yönetici onayı ve süreli üyelik
- Tek kullanımlık, süreli Telegram davet bağlantıları
- Üyelik bitiş kontrolü ve hatırlatmalar
- Planlı ve tekrarlanan duyurular
- Toplu kanal paylaşımı
- Müşteri lisansı, paket ve satış kaydı takibi
- Yönetici işlem geçmişi
- SQLite veritabanı ve Telegram üzerinden yöneticiye yedek gönderimi

## Teknoloji
Python, python-telegram-bot 21.x, SQLite ve Railway.

## Kurulum özeti
1. Python 3.11+ ortamı hazırlayın.
2. Bağımlılıkları yükleyin: `pip install -r requirements.txt`
3. Ortam değişkenlerini tanımlayın.
4. Veritabanı için kalıcı disk kullanın.
5. Botu başlatın: `python app.py`
6. Botu Telegram kanallarına/gruplarına gerekli yönetici izinleriyle ekleyin.

### Ortam değişkenleri
| Değişken | Açıklama |
|---|---|
| `BOT_TOKEN` | BotFather tokenı; gizli tutulmalıdır |
| `ADMIN_IDS` | Virgülle ayrılmış ana yönetici Telegram kullanıcı ID'leri |
| `CHANNEL_ID` | Eski tek kanal uyumluluğu için opsiyonel kanal ID |
| `MEDYAECAN_CUSTOMER_CHANNEL_ID` | Müşteri kanalına davet gönderilecek kanal ID'si |
| `DB_PATH` | SQLite dosyasının tam yolu; Railway kalıcı diskine yönlendirilmelidir |
| `TIMEZONE` | Varsayılan `Europe/Istanbul` |

Railway diski `/data` yoluna bağlıysa örnek `DB_PATH=/data/medyaecan.sqlite3` olabilir. Mevcut üretim veritabanının yeri doğrulanmadan değiştirmeyin.

## Satış öncesi notlar
- Paket tutarları örnek yapılandırmadır; kesin fiyatlar ayrıca onaylanmalıdır.
- Ödeme onayı uygulama içinde yönetici tarafından manuel yapılır; otomatik ödeme entegrasyonu olduğu varsayılmamalıdır.
- Haftalık otomatik yedek ve `/yedekle` komutu vardır; geri yükleme prosedürü ayrıca test edilmelidir.
- Gerçek müşteri verileri ve erişimler devirden önce envanterlenmelidir.
- Bu dalda yapılan değişiklikler üretime alınmış değildir.

## Dokümanlar
- [Ticari hazırlık ve teknik kontrol listesi](docs/COMMERCIAL_READINESS.md)
- [Satış ilanı taslağı](docs/SALES_LISTING_TR.md)
- [Kurulum ve devir rehberi](docs/INSTALLATION_TR.md)
