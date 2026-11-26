# MEDYAECAN BOT — Ticari Hazırlık Raporu

**Durum:** Hazırlık / teknik inceleme  
**Kapsam:** Kaynak kodu ve bağlı Railway üretim yapılandırmasının salt okunur incelemesi  
**Canlı sisteme değişiklik:** Yapılmadı

## Ürün özeti

MEDYAECAN BOT, birden fazla müşteriyi aynı Telegram bot altyapısı üzerinde yönetmek üzere geliştirilmiş Python uygulamasıdır. Müşteri kayıtları, kanal/grup eşleştirmeleri, ekip rolleri, VIP başvuruları, üyelik süreleri, duyurular, lisanslar ve satış kayıtları SQLite veritabanında tutulur.

## Kodda görülen özellikler

- Müşteri/tenant oluşturma ve müşteri sahibi tanımlama
- Kanal ve grup kaydı; kanalın başka tenant'a taşınmasını engelleyen kontrol
- Owner, manager, moderator, finance ve publisher rolleri
- Kanal bazlı ekip yetkilendirme ve izin kontrolleri
- VIP başvuru onay/red akışı ve süreli davet bağlantıları
- VIP üyelik süresi takibi ve bitiş iş akışları
- Tekrarlanan ve zamanlanmış duyurular
- Müşterinin kanallarına toplu yayın
- Paket, lisans ve satış kaydı; yönetici tarafından manuel ödeme onayı
- Yönetici işlem kayıtları
- Haftalık otomatik yedekleme ve manuel `/yedekle` komutu

## Dağıtım altyapısı kontrolü

İnceleme sırasında Railway üretim ortamında:
- Son görünen dağıtım durumu SUCCESS idi.
- Servise 500 MB kalıcı disk bağlanmıştı.
- Disk bağlama yolu `/data` idi.
- Başlangıç komutu `python app.py` idi.
- Üretim ortamında bekleyen değişiklik görünmüyordu.

**Önemli:** Uygulama `DB_PATH` ortam değişkenini kullanıyor. Değişkenin gerçek değeri güvenlik nedeniyle okunamadı. Bu nedenle SQLite dosyasının gerçekten `/data` altında bulunduğu henüz doğrulanmış değildir. Üretim ayarı değiştirilmeden önce bu eşleştirme kontrol edilmeli.

## Satış öncesi kritik işler

### P0 — Yayına/satışa çıkmadan önce
- [ ] GitHub deposunun erişimi sınırlandırılmalı. Depo şu an public görünüyor. Münhasır ticari satış düşünülüyorsa erişim kapatılmalı; mevcut işbirliği erişimleri önce gözden geçirilmeli.
- [ ] `DB_PATH` değeri güvenli biçimde doğrulanmalı ve kalıcı disk yoluna bağlanmalı.
- [x] Yedekten geri yükleme işlemi daha önce başarıyla tamamlandı (kullanıcı teyidi).
- [x] Yeni kurulumlarda demo tenant ve sabit örnek/affiliate bağlantı eklenmesi durduruldu; mevcut veritabanındaki eski kayıtlar silinmez veya otomatik değiştirilmez. Mevcut kayıtlar ayrı, kontrollü temizlik kararı gerektirir.
- [ ] Uygulamadaki sabit örnek/affiliate bağlantıları ve eski marka/metin kalıntıları temizlenmeli.
- [ ] Müşteri A'nın yöneticisinin müşteri B'nin kanal, üye, başvuru, ödeme ve ayarlarına erişemediği test edilmeli.
- [ ] Paket limitleri, deneme süresi, lisans uzatma ve ödeme onayının uçtan uca testleri yapılmalı. Hazırlık dalında 14 günlük deneme 2 kanal / 2 ekip kullanıcısı ile sınırlandı; tanımsız paket artık limitsiz geçiş sağlamıyor.
- [ ] Ödeme akışının yönetici tarafından manuel onaylandığı açıkça belgelenmeli; otomatik ödeme entegrasyonu varmış gibi sunulmamalı.

### P1 — Profesyonel ürünleştirme
- [ ] Kurulum/ilk yönetici oluşturma sihirbazı
- [ ] Paket ve fiyatların yönetilebilir yapılandırmadan gelmesi
- [ ] Müşteri bazlı destek ve marka ayarlarının panelden yönetilmesi
- [ ] Yedeklerin ikinci güvenli hedefte saklanması ve geri yükleme talimatı
- [ ] Sağlık kontrolü, hata alarmı ve işlem günlüklerinin gözden geçirilmesi
- [ ] Otomatik testler ve test veritabanıyla regresyon kontrolü
- [ ] Kullanıcı sözleşmesi, gizlilik metni ve destek kapsamı

## Hazırlık dalında yapılan ek ürünleştirme düzeltmeleri

- Yeni kurulumlarda eski demo tenant'lar ve sabit örnek/affiliate site-sosyal bağlantıları otomatik eklenmiyor.
- Mevcut veritabanındaki eski demo kayıtları silinmiyor veya başlangıç sırasında değiştirilmemesi için ilgili otomatik mutasyonlar kaldırıldı.
- 14 günlük deneme planı 2 aktif kanal/grup ve 2 ekip kullanıcısı ile sınırlandırıldı.
- Paket adı tanımsızsa limit kontrolü artık sessizce limitsiz geçiş vermiyor; işlem ana yöneticiye yönlendiriliyor.
- Bu değişiklikler yalnızca hazırlık dalında. Canlı sistemde uygulanmadı ve henüz ayrı test ortamında çalıştırılmış test sonucu değildir.

## İnceleme sırasında bulunan ve hazırlık dalında giderilen yetki açığı

İlk incelemede `/kanalkaydet` komutunda tenant erişimi olan herhangi bir ekip kullanıcısının kanal kaydı yapabildiği ve işlem sonunda kendisine tenant genelinde `owner` rolü yazıldığı görüldü. Bu durum, kanal kapsamlı bir ekip kullanıcısının yetkisini yükseltmesine yol açabilirdi.

**Hazırlık dalında düzeltildi:** Kanal kaydı artık ana yöneticiye veya ilgili tenant'ın kanal-genel `owner` rolüne sahip müşterisine sınırlandırılıyor. Bu değişiklik yalnızca `medyaecan-commercial-prep` dalındadır; canlı servise aktarılmamıştır.

**Takip testi:** Moderator, finance, publisher ve kanal kapsamlı manager hesaplarıyla kanal kaydının reddedildiği; tenant owner ve ana yöneticiyle kabul edildiği ayrı test ortamında doğrulanmalıdır.

## İlk statik incelemede görülen riskler

| Alan | Gözlem | Öncelik |
|---|---|---|
| Depo erişimi | Kaynak kod deposu public | Kritik ticari/IP riski |
| Veritabanı | SQLite; kalıcı disk var, DB_PATH eşleşmesi teyit edilmedi | Yüksek |
| Örnek veriler | Yeni kurulum seed'leri kaldırıldı; eski veritabanındaki demo kayıtlar korunur ve kontrollü temizlik bekler | Orta |
| Eski uyumluluk | Tek kanallı/legacy akışların bazı komut ve ayarları hâlâ mevcut | Orta |
| Ödeme | Satış kaydı ve manuel yönetici onayı mevcut; ödeme sağlayıcısı entegrasyonu doğrulanmadı | Orta |
| Yedek | Haftalık yedek Telegram üzerinden ana yöneticilere iletiliyor; yedekten geri yükleme daha önce başarıyla tamamlandı (kullanıcı teyidi) | Düşük |
| Yetki modeli | Kanal kaydında yetki yükseltme açığı bulundu ve hazırlık dalında sınırlandı; tam tenant izolasyonu testi bekliyor | Yüksek |

## Teknik sınırlar

Bu rapor kaynak kodu ve Railway yapılandırmasının statik incelemesidir. Bağımsız penetrasyon testi, canlı Telegram hesaplarıyla tam fonksiyon testi, hukuki IP sahipliği incelemesi veya güvenlik sertifikası değildir. Ürün henüz "bağımsız güvenlik testinden geçti" şeklinde tanıtılmamalıdır.

## Ticari devir paketi için gerekenler

- Kaynak kod ve sürüm/commit bilgisi
- Kurulum ve yapılandırma rehberi
- Özellik listesi ve ekran görüntüleri
- Test senaryoları ve sonuçları
- Yedekleme/geri yükleme prosedürü
- Üçüncü taraf bağımlılık ve lisans listesi
- Alan adı, sunucu, Telegram botu ve veritabanı devir envanteri
- IP/devretme sözleşmesi, destek süresi ve kapsamı
- Gerçek müşteri/satış verileri varsa anonimleştirilmiş gelir ve kullanım kanıtları
