---
title: TikTok Downloader Bot
emoji: 🎬
colorFrom: pink
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# TikTok Downloader Bot — @GMR2BOT

بوت تيليجرام لتحميل فيديوهات وصور تيك توك بدون علامة مائية، يعمل على Hugging Face Spaces مجاناً 24/7.

---

## ✨ المميزات

- ✅ يعمل **24/7 مجاناً** بدون بطاقة، بدون رقم جوال، بدون خدمات خارجية
- ✅ **جودة 720p HEVC** يتم تحويلها إلى H.264 سلس عبر ffmpeg
- ✅ يدعم Slideshows (الصور المتعددة)
- ✅ يدعم الروابط القصيرة (`vt.tiktok.com`, `vm.tiktok.com`)
- ✅ يستخدم **Long Polling** = لا يحتاج webhook ولا keep-alive

---

## 🚀 كيفية النشر على Hugging Face Spaces

### 1. أنشئ Space جديد
- ادخل https://huggingface.co/new-space
- اختر **SDK: Docker**
- اتركه Public

### 2. أضف متغير البيئة `BOT_TOKEN`
- في صفحة Space اضغط **Settings**
- اذهب إلى **Variables and secrets**
- اضغط **New secret**:
  - **Name**: `BOT_TOKEN`
  - **Value**: التوكن من @BotFather

### 3. ارفع الملفات
ارفع كل محتويات هذا المجلد إلى الـ Space (عبر Git أو واجهة Web):
- `Dockerfile`
- `app.py`
- `requirements.txt`
- مجلد `api/`
- `README.md`

### 4. انتظر البناء
- HF سيبني الصورة تلقائياً (~2-3 دقائق أول مرة)
- ستظهر رسالة "Running" خضراء في رأس الـ Space
- البوت يبدأ تلقائياً ويتصل بتليجرام

### 5. جرّب البوت
أرسل رابط تيك توك إلى البوت على تليجرام.

---

## 🧪 التشغيل المحلي (للاختبار)

```bash
cd tiktok-bot
pip install -r requirements.txt
export BOT_TOKEN="..."
python app.py
```

أو بطريقة polling فقط بدون Flask:

```bash
python local_polling.py
```

---

## 📦 الملفات

| الملف | الوصف |
|-------|-------|
| `app.py` | نقطة دخول HF Spaces (Flask + polling thread) |
| `api/webhook.py` | الكود الرئيسي (extract + transcode + send) |
| `local_polling.py` | تشغيل polling فقط (للاختبار المحلي) |
| `Dockerfile` | بناء صورة Python + ffmpeg |
| `requirements.txt` | حزم Python |

---

## 🔑 متغيرات البيئة

| المتغير | إجباري | الوصف |
|---------|--------|-------|
| `BOT_TOKEN` | ✅ | توكن البوت من @BotFather |
| `BOT_USERNAME` | اختياري | اسم البوت للظهور في توقيع الفيديو |

---

## 🎯 لماذا Hugging Face Spaces؟

- 🆓 مجاني للأبد
- 💳 بدون بطاقة، بدون رقم جوال
- ⚡ 2 vCPU + 16 GB RAM (أكثر من كافي)
- 🐳 يدعم Docker = يدعم ffmpeg = جودة كاملة
- 🔄 Polling يبقي الـ Space مستيقظاً تلقائياً
