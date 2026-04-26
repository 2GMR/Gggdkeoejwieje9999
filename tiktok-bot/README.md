# TikTok Downloader Bot — @GMR2BOT

بوت تيليجرام لتحميل فيديوهات وصور تيك توك بدون علامة مائية.

---

## ✨ المميزات

- ✅ يدعم فيديوهات تيك توك (بدون علامة مائية)
- ✅ يدعم Slideshows (الصور المتعددة)
- ✅ يعمل بطريقتين: **خفيفة (Vercel)** أو **كاملة (خادم خاص)**
- ✅ يدعم الروابط القصيرة (`vt.tiktok.com`, `vm.tiktok.com`)

---

## 🎯 وضعا التشغيل

| الخاصية | وضع Vercel (Serverless) | وضع الخادم الكامل |
|---------|------------------------|--------------------|
| الجودة | 540p H.264 | **720p HEVC → H.264** |
| تصحيح التقطيع | ❌ | ✅ ffmpeg transcode |
| الاستهلاك | URL relay فقط (لا تحميل) | تحميل + تحويل + رفع |
| المنصات | Vercel / Netlify / Lambda | Replit / Oracle / VPS / Render |
| المتطلبات | بدون ffmpeg | يحتاج ffmpeg |

البوت يكتشف البيئة تلقائياً من متغيرات النظام.

---

## 🚀 النشر على Vercel (مجاني، بدون بطاقة)

### الخطوات

1. **أنشئ حساباً على Vercel**: https://vercel.com (سجّل بـ GitHub - لا يطلب بطاقة)

2. **ارفع المشروع على GitHub**:
   ```bash
   cd tiktok-bot
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create tiktok-bot --public --source=. --push
   ```

3. **أنشئ مشروع جديد على Vercel**:
   - Add New → Project
   - استورد الريبو من GitHub
   - في Settings → Environment Variables أضف:
     - `BOT_TOKEN` = توكن البوت من BotFather
   - اضغط Deploy

4. **سجّل الـ Webhook عند تليجرام** (مرة واحدة فقط):
   ```
   افتح في المتصفح:
   https://YOUR-PROJECT.vercel.app/api/setwebhook
   ```
   سترى `{"ok": true}` يعني نجح التسجيل.

5. **جرّب البوت** على تليجرام: أرسل أي رابط تيك توك.

### حدود Vercel المجاني (Hobby)

- ✅ **100,000 طلب/شهر** (~3,300 فيديو/يوم)
- ✅ **100 GB bandwidth** (شبه لا تستهلكه - الفيديو يمر عبر تليجرام مباشرة)
- ✅ يعمل 24/7 بدون نوم
- ⚠️ Function timeout: 10 ثوان (الكود مُحسَّن لاحترامها)

---

## 🖥️ النشر على خادم كامل (لجودة 720p سلسة)

ليتفعّل وضع 720p HEVC + التحويل إلى H.264، يجب أن تكون البيئة:
- تدعم Python 3.11+
- تدعم تشغيل **ffmpeg**
- بدون timeout صارم

### المنصات الموصى بها (مجانية بدون بطاقة محدودة):

| المنصة | السعر | Bandwidth | بطاقة؟ |
|--------|-------|-----------|--------|
| **Replit Reserved VM** | $7/شهر | حسب الخطة | لا للبدء |
| **Oracle Cloud Free** | مجاني للأبد | 10 TB/شهر | ⚠️ نعم للتحقق |
| **Contabo VPS** | $5/شهر | 32 TB/شهر | نعم |

### الخطوات على VPS عام (Ubuntu)

```bash
# 1. ثبت Python و ffmpeg
sudo apt update && sudo apt install -y python3 python3-pip ffmpeg git

# 2. استنسخ المشروع
git clone <repo-url> && cd tiktok-bot
pip install -r requirements.txt

# 3. شغّل البوت بنمط polling (لا يحتاج webhook ولا دومين)
export BOT_TOKEN="..."
python local_polling.py
```

أو استخدم systemd لتشغيله 24/7:

```ini
# /etc/systemd/system/tiktok-bot.service
[Unit]
Description=TikTok Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/tiktok-bot
Environment="BOT_TOKEN=YOUR_TOKEN"
ExecStart=/usr/bin/python3 local_polling.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tiktok-bot
```

---

## 🔧 التشغيل المحلي (للاختبار على Replit)

```bash
cd tiktok-bot
python local_polling.py
```

البوت يستخدم polling (لا يحتاج webhook) — مثالي للاختبار.

---

## 📦 الملفات

- `api/webhook.py` — الكود الرئيسي (Flask + extract + send)
- `local_polling.py` — مشغّل polling للاختبار المحلي
- `vercel.json` — تكوين النشر على Vercel
- `requirements.txt` — حزم Python

---

## 🔑 متغيرات البيئة

| المتغير | إجباري | الوصف |
|---------|--------|-------|
| `BOT_TOKEN` | ✅ | توكن البوت من @BotFather |
| `WEBHOOK_URL` | فقط للنشر | رابط Vercel/الخادم (للـ setwebhook) |
| `WEBHOOK_SECRET` | اختياري | كلمة سر إضافية للأمان |
| `BOT_USERNAME` | اختياري | يُضاف في توقيع الفيديو |
| `SERVERLESS_MODE` | اختياري | `1` لإجبار وضع Vercel |
