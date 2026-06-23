# ระบบเช็คสถานะพนักงาน (Telegram → Dashboard)

## โครงสร้างไฟล์

| ไฟล์ | ใช้ทำอะไร |
|---|---|
| `main.py` | โปรแกรมหลัก (ฟัง Telegram + API + เสิร์ฟหน้าเว็บ) — ตัวนี้ตัวเดียวพอ |
| `dashboard.html` | หน้าแดชบอร์ด |
| `requirements.txt` | รายชื่อไลบรารีที่ต้องติดตั้ง |
| `generate_session.py` | รันครั้งเดียวเพื่อสร้าง string session (ใช้ตอน deploy) |
| `.env.example` | ตัวอย่างไฟล์ตั้งค่า (ไม่มีค่าจริง) |
| `.env` | ไฟล์ตั้งค่าจริงของคุณ — **ห้าม commit ขึ้น git** (.gitignore กันไว้แล้ว) |
| `.gitignore` | กันไฟล์ลับ/ฐานข้อมูลไม่ให้หลุดขึ้น git |

## ฐานข้อมูล

ใช้ **PostgreSQL** (ไม่ใช่ SQLite แล้ว) เพื่อให้ข้อมูลไม่หายตอน Render restart service

### รันที่เครื่องตัวเอง (ตัวเลือก)
ถ้าไม่มี PostgreSQL ที่เครื่องตัวเอง ใช้ Postgres ตัวเดียวกับที่ deploy บน Render ได้เลย (ใช้ "External Database URL" จากหน้า Render Postgres) — ใส่ใน `.env` ที่ `DATABASE_URL`

### Deploy บน Render
1. สร้าง Render Postgres (New → PostgreSQL) แยกจาก Web Service
2. คัดลอก **Internal Database URL**
3. ใส่เป็น Environment Variable ชื่อ `DATABASE_URL` ในหน้า Web Service

## รันที่เครื่องตัวเอง

```
pip install -r requirements.txt
python main.py
```
ครั้งแรกจะถาม OTP login Telegram ตามปกติ (อ่านค่า api_id/hash จากไฟล์ `.env`) และจะสร้างตารางใน PostgreSQL ให้อัตโนมัติถ้ายังไม่มี

เปิดเบราว์เซอร์ไปที่ `http://localhost:5000` จะเห็นแดชบอร์ด

## Deploy ขึ้น Render

1. สร้าง string session: `python generate_session.py`
2. Push ขึ้น GitHub (ไฟล์ `.env` จะไม่ติดไปด้วย เพราะ `.gitignore` กันไว้)
3. Render → New → Web Service → เชื่อม repo
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
4. ใส่ Environment Variables ใน Render:
   - `TG_API_ID`
   - `TG_API_HASH`
   - `TG_GROUP_ID`
   - `TG_GROUP_ID_CHECKIN`
   - `TG_SESSION` (string จากขั้นที่ 1)
   - `DATABASE_URL` (จาก Render Postgres)
5. Deploy แล้วเข้าผ่าน URL ที่ Render ให้มา

## ข้อควรระวัง
- `TG_API_HASH`, `TG_SESSION`, `DATABASE_URL` คือข้อมูลที่ทำให้เข้าถึงบัญชี Telegram/ฐานข้อมูลได้ ห้ามแชร์/commit ขึ้น git โดยเด็ดขาด
- ด้วย PostgreSQL ข้อมูลจะไม่หายตอน restart แล้ว (ต่างจาก SQLite เดิม)

