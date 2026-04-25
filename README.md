# LINE OA Bot ร้านอาหารตามสั่ง + PromptPay QR (Python)

บอทสำหรับรับออเดอร์เมนูอาหาร/เครื่องดื่มผ่าน LINE OA และสร้าง QR PromptPay ตามยอดชำระเงิน

## ความสามารถ

- แสดงเมนูอาหารและเครื่องดื่ม
- รับคำสั่งสั่งอาหาร เช่น `สั่ง F01 2`
- ดูตะกร้า และรวมยอดอัตโนมัติ
- สร้าง QR PromptPay ตามยอดจริงของตะกร้า
- ยืนยันการชำระและล้างตะกร้า

## โครงสร้างไฟล์

- `app.py` เซิร์ฟเวอร์ Flask + LINE webhook
- `promptpay.py` ฟังก์ชันสร้าง PromptPay payload (EMVCo + CRC16)
- `.env.example` ตัวอย่างตัวแปรแวดล้อม

## ติดตั้ง

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## ตั้งค่า

คัดลอก `.env.example` เป็น `.env` แล้วแก้ค่า:

- `CHANNEL_ACCESS_TOKEN`
- `CHANNEL_SECRET`
- `PROMPTPAY_ID` (เบอร์มือถือ 10 หลัก หรือเลขบัตรประชาชน 13 หลัก)
- `BASE_URL` (ต้องเป็น HTTPS และเข้าถึงได้จากอินเทอร์เน็ต)

สำหรับระบบแอดมิน (ใหม่):

- `ADMIN_USERNAME` ชื่อผู้ใช้แอดมิน (ค่าเริ่มต้น `admin`)
- `ADMIN_PASSWORD` รหัสผ่านเริ่มต้นแบบ plain text (ใช้เฉพาะกรณีไม่ตั้งค่า hash)
- `ADMIN_PASSWORD_HASH` รหัสผ่านแบบ hash สำหรับ login แอดมิน
- `ADMIN_SECRET_CODE` โค้ดลับสำหรับฟังก์ชันลืมรหัสผ่าน
- `ADMIN_SECRET_CODE_HASH` โค้ดลับแบบ hash (ถ้าตั้งค่านี้ ระบบจะใช้ค่านี้ก่อน)

> หลังจากเปลี่ยนรหัสผ่านผ่านหน้าเว็บ `/admin/change-password` หรือรีเซ็ตผ่าน `/admin/forgot-password`
> ระบบจะบันทึกรหัสผ่าน hash ลงฐานข้อมูลอัตโนมัติ และใช้งานค่านั้นเป็นลำดับแรก

## รัน

```bash
python app.py
```

ตั้งค่า Webhook URL ใน LINE Developers เป็น:

```text
https://your-domain.com/callback
```

## คำสั่งที่ผู้ใช้พิมพ์ในแชต

- `เมนู`
- `สั่ง <รหัสเมนู> <จำนวน>` เช่น `สั่ง F01 2`
- `ตะกร้า`
- `ล้างตะกร้า`
- `ชำระเงิน`
- `ตรวจยอด <ข้อความตอบกลับจาก Thunder Bot>`
- `verify <QR_PAYLOAD>`
- `ยืนยันชำระ`

## หมายเหตุสำคัญ

- การเก็บตะกร้าเป็นหน่วยความจำ (in-memory) จะหายเมื่อรีสตาร์ตโปรแกรม
- ควรย้ายตะกร้า/ออเดอร์ไปเก็บในฐานข้อมูลเมื่อใช้งานจริง
- คำสั่ง `ยืนยันชำระ` ในตัวอย่างนี้เป็นการยืนยันแบบ manual
