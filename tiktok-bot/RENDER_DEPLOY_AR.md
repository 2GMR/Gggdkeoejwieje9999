# 🚀 نشر البوت على Render (مجاني، بدون بطاقة، بدون رقم جوال)

## 📋 المتطلبات
- حساب GitHub (مجاني، بإيميل فقط)
- حساب Render (مجاني، بإيميل أو GitHub)

---

## الخطوة 1️⃣ — إنشاء حساب GitHub (إذا ما عندك)

1. ادخل https://github.com/signup
2. سجّل بإيميلك
3. تحقق من الإيميل

---

## الخطوة 2️⃣ — رفع الكود إلى GitHub

### الطريقة السهلة (من المتصفح):

1. ادخل https://github.com/new
2. اسم المستودع (Repository name): `tiktok-bot`
3. اختر **Private** (مهم — حتى ما يشوف أحد التوكن)
4. اضغط **Create repository**
5. في الصفحة التالية، اضغط **uploading an existing file**
6. **اسحب وأفلت** كل ملفات مجلد `tiktok-bot/` (أو اختر "choose your files")
   - الملفات المطلوبة: `app.py`, `Dockerfile`, `requirements.txt`, `render.yaml`, مجلد `api/`
7. اضغط **Commit changes**

✅ الآن الكود على GitHub.

---

## الخطوة 3️⃣ — إنشاء حساب Render

1. ادخل https://render.com
2. اضغط **Get Started**
3. اختر **Sign in with GitHub** (الأسهل) أو سجّل بإيميل
4. ✅ بدون بطاقة، بدون رقم جوال

---

## الخطوة 4️⃣ — نشر البوت

1. في Render Dashboard، اضغط **New +** → **Blueprint**
2. اختر مستودع `tiktok-bot` الذي أنشأته
3. Render سيكتشف ملف `render.yaml` تلقائياً
4. اضغط **Apply**
5. ستظهر شاشة طلب متغيرات البيئة:
   - **BOT_TOKEN**: الصق توكن البوت من BotFather
   - **WEBHOOK_URL**: اتركه فارغاً مؤقتاً (سنملأه بعد قليل)
6. اضغط **Create Resources**

⏳ انتظر 3-5 دقائق حتى ينتهي البناء (تظهر **Live** بشريط أخضر).

---

## الخطوة 5️⃣ — الحصول على الرابط العام

1. في Render Dashboard، اضغط على خدمة `tiktok-bot`
2. في الأعلى ستجد رابط مثل:
   ```
   https://tiktok-bot-xxxx.onrender.com
   ```
3. **انسخ الرابط** كاملاً

---

## الخطوة 6️⃣ — تسجيل WEBHOOK_URL

1. في الخدمة → تبويب **Environment**
2. عدّل المتغير `WEBHOOK_URL` وضع الرابط الذي نسخته
3. اضغط **Save Changes** (سيعيد التشغيل تلقائياً)

⏳ انتظر دقيقة حتى يعيد النشر.

---

## الخطوة 7️⃣ — تفعيل الـ Webhook على تليجرام

افتح هذا الرابط في المتصفح (استبدل الرابط برابطك من Render):

```
https://tiktok-bot-xxxx.onrender.com/api/setwebhook
```

يجب أن يظهر:
```json
{"ok": true, "result": true, "description": "Webhook was set"}
```

---

## ✅ تم!

افتح تليجرام → ابحث عن `@GMR2BOT` → أرسل `/start`

البوت يجب أن يرد فوراً.

---

## ⚠️ ملاحظات مهمة عن Render Free

- **النوم بعد 15 دقيقة من عدم الاستخدام**: عند أول رسالة بعد النوم، تأخذ 30-60 ثانية للرد. الرسائل التالية فورية.
- **750 ساعة/شهر مجاناً** = أكثر من شهر كامل (لا قلق على الحد).
- **لا حاجة لـ keep-alive خارجي** — webhook يستيقظ الخدمة عند أي رسالة من تليجرام.

---

## 🔧 استكشاف الأخطاء

### البوت لا يرد:
1. اذهب لـ Render → خدمة tiktok-bot → تبويب **Logs**
2. ابحث عن `Connected to bot:` أو خطأ
3. تأكد من `WEBHOOK_URL` صحيح في تبويب Environment
4. أعد زيارة `/api/setwebhook` للتأكد

### خطأ "BOT_TOKEN missing":
1. تبويب **Environment** → تأكد أن `BOT_TOKEN` موجود وقيمته صحيحة
2. اضغط **Save Changes** (يعيد التشغيل)
