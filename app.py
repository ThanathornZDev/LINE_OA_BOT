from __future__ import annotations

import os
import json
import uuid
import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import qrcode
import pytz
import requests
from sqlalchemy import func, text as sql_text
from dotenv import load_dotenv
from flask import Flask, abort, request, send_from_directory, render_template, jsonify, session, redirect, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from promptpay import generate_promptpay_payload

load_dotenv(override=True)

# ----------------- ส่วนของ Database (SQLite) -----------------
from extensions import db
from models import Product, Order, OrderItem, Setting, InventoryLog
# -------------------------------------------------------------

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "")
PROMPTPAY_ID = os.getenv("PROMPTPAY_ID", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SLIP2GO_KEY = os.getenv("SLIP2GO_KEY", "")
SLIP2GO_SECRET_KEY = os.getenv("SLIP2GO_SECRET_KEY", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE", "")
ADMIN_SECRET_CODE_HASH = os.getenv("ADMIN_SECRET_CODE_HASH", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET in environment")
if not PROMPTPAY_ID:
    raise RuntimeError("Missing PROMPTPAY_ID in environment")

app = Flask(__name__)
# ตั้งค่าพาธสำหรับเก็บไฟล์ bot.db
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(Path(__file__).parent / 'bot.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
# เริ่มใช้งาน db
db.init_app(app)


@app.template_filter("timedelta_minutes")
def timedelta_minutes(value) -> int:
    """Return elapsed minutes from datetime value to now for template rendering."""
    if value is None:
        return 0
    try:
        return max(0, int((datetime.now() - value).total_seconds() // 60))
    except Exception:
        return 0


def _ensure_order_delivery_columns() -> None:
    try:
        columns = {
            row[1]
            for row in db.session.execute(sql_text('PRAGMA table_info("order")')).fetchall()
        }
        changed = False
        if "delivery_method" not in columns:
            db.session.execute(
                sql_text('ALTER TABLE "order" ADD COLUMN delivery_method VARCHAR(20) DEFAULT "PICKUP"')
            )
            changed = True
        if "delivery_note" not in columns:
            db.session.execute(
                sql_text('ALTER TABLE "order" ADD COLUMN delivery_note VARCHAR(255)')
            )
            changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to ensure order delivery columns")


def _ensure_product_category_column() -> None:
    try:
        columns = {
            row[1]
            for row in db.session.execute(sql_text('PRAGMA table_info("product")')).fetchall()
        }
        if "category" not in columns:
            db.session.execute(
                sql_text('ALTER TABLE "product" ADD COLUMN category VARCHAR(50) DEFAULT "ทั่วไป"')
            )
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to ensure product category column")


def _ensure_inventory_log_columns() -> None:
    try:
        columns = {
            row[1]
            for row in db.session.execute(sql_text('PRAGMA table_info("inventory_log")')).fetchall()
        }
        changed = False

        if "order_id" not in columns:
            db.session.execute(sql_text('ALTER TABLE "inventory_log" ADD COLUMN order_id INTEGER'))
            changed = True
        if "customer_user_id" not in columns:
            db.session.execute(sql_text('ALTER TABLE "inventory_log" ADD COLUMN customer_user_id VARCHAR(100)'))
            changed = True
        if "customer_name" not in columns:
            db.session.execute(sql_text('ALTER TABLE "inventory_log" ADD COLUMN customer_name VARCHAR(100)'))
            changed = True

        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Failed to ensure inventory_log columns")

# สร้าง Table ในฐานข้อมูล (รันครั้งแรกมันจะสร้างให้)
with app.app_context():
    db.create_all()
    _ensure_order_delivery_columns()
    _ensure_product_category_column()
    _ensure_inventory_log_columns()
    
    # เพิ่มข้อมูลจำลอง (Seed Data) ให้โดยอัตโนมัติถ้าตั้งต้นแอปใหม่
    if Product.query.count() == 0:
        mock_products_data = [
            {"code": "F01", "name": "ผัดกะเพราหมูสับ (เทส)", "category": "อาหาร", "price": 1.0, "stock": 100, "image_url": "https://via.placeholder.com/250x250.png?text=Food+1"},
            {"code": "F02", "name": "ข้าวผัดไก่ (เทส)", "category": "อาหาร", "price": 1.0, "stock": 100, "image_url": "https://via.placeholder.com/250x250.png?text=Food+2"},
            {"code": "D01", "name": "ชาไทยเย็น (เทส)", "category": "เครื่องดื่ม", "price": 1.0, "stock": 100, "image_url": "https://via.placeholder.com/250x250.png?text=Drink+1"},
            {"code": "D02", "name": "น้ำเปล่า (เทส)", "category": "เครื่องดื่ม", "price": 1.0, "stock": 100, "image_url": "https://via.placeholder.com/250x250.png?text=Drink+2"}
        ]
        mock_products = []
        for data in mock_products_data:
            if not Product.query.filter_by(code=data["code"]).first():
                mock_products.append(Product(**data))
        if mock_products:
            db.session.bulk_save_objects(mock_products)
            db.session.commit()
            print("======== [ SYSTEM ] เพิ่มเมนูจำลองราคา 1 บาท ทั้งอาหารและน้ำ เรียบร้อยแล้ว! ========")

logging.basicConfig(level=logging.INFO)

QR_DIR = Path("static") / "qrs"
QR_DIR.mkdir(parents=True, exist_ok=True)
BROADCAST_DIR = Path("static") / "broadcasts"
BROADCAST_DIR.mkdir(parents=True, exist_ok=True)
PRODUCT_UPLOAD_DIR = Path("static") / "uploads" / "products"
PRODUCT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SOUND_DIR = Path("sound")
LINE_PROFILE_CACHE: dict[str, str] = {}


def _get_line_display_name(user_id: str) -> str:
    if not user_id or user_id == "unknown":
        return "Unknown User"

    cached = LINE_PROFILE_CACHE.get(user_id)
    if cached:
        return cached

    try:
        headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
        url = f"https://api.line.me/v2/bot/profile/{user_id}"
        res = requests.get(url, headers=headers, timeout=10)
        if res.ok:
            payload = res.json() or {}
            display_name = str(payload.get("displayName") or "").strip()
            if display_name:
                LINE_PROFILE_CACHE[user_id] = display_name
                return display_name
    except Exception:
        app.logger.exception("Failed to resolve LINE display name for user_id=%s", user_id)

    # Fallback to original value when LINE profile cannot be resolved.
    return user_id


@app.context_processor
def inject_line_name_helper():
    return {"line_name": _get_line_display_name}


def _get_setting(key: str, default: str = "") -> str:
    item = db.session.get(Setting, key)
    if item and item.value is not None:
        return item.value
    return default


def _set_setting(key: str, value: str, description: str | None = None) -> None:
    item = db.session.get(Setting, key)
    if item:
        item.value = value
        if description:
            item.description = description
    else:
        db.session.add(Setting(key=key, value=value, description=description))


def _now_bkk() -> datetime:
    return datetime.now(pytz.timezone("Asia/Bangkok"))


def _parse_hhmm(value: str, fallback: str) -> time:
    raw = (value or fallback).strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return datetime.strptime(fallback, "%H:%M").time()


def _shop_open_state() -> tuple[bool, str]:
    mode = _get_setting("shop_status_mode", "AUTO").upper()
    open_time_text = _get_setting("shop_open_time", "08:00")
    close_time_text = _get_setting("shop_close_time", "20:00")

    if mode == "MANUAL_OPEN":
        return True, "เปิดรับออเดอร์ตามที่แอดมินกำหนด"
    if mode == "MANUAL_CLOSE":
        return False, "ปิดรับออเดอร์ตามที่แอดมินกำหนด"

    open_time = _parse_hhmm(open_time_text, "08:00")
    close_time = _parse_hhmm(close_time_text, "20:00")
    now_time = _now_bkk().time()

    if open_time == close_time:
        return True, "เปิดรับออเดอร์ 24 ชั่วโมง"

    if open_time < close_time:
        is_open = open_time <= now_time < close_time
    else:
        # รองรับช่วงเวลาข้ามเที่ยงคืน เช่น 18:00 - 02:00
        is_open = now_time >= open_time or now_time < close_time

    if is_open:
        return True, f"เปิดรับออเดอร์อัตโนมัติ ({open_time_text} - {close_time_text})"
    return False, f"อยู่นอกเวลาเปิดร้าน ({open_time_text} - {close_time_text})"


def _shop_closed_reply_text() -> str:
    custom_message = _get_setting("shop_closed_message", "")
    if custom_message.strip():
        return custom_message.strip()
    _, reason = _shop_open_state()
    return f"ขออภัย ร้านยังไม่เปิดรับออเดอร์ในขณะนี้\n{reason}"


def _line_broadcast(messages: list[dict]) -> tuple[bool, str]:
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messages": messages,
        "notificationDisabled": False,
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        if not res.ok:
            app.logger.error("LINE broadcast failed: status=%s body=%s", res.status_code, res.text)
            return False, f"LINE API error: {res.status_code}"
        return True, "ส่ง Broadcast เรียบร้อยแล้ว"
    except Exception as e:
        app.logger.exception("LINE broadcast request failed")
        return False, str(e)


def _line_broadcast_quota() -> dict:
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    }

    quota_url = "https://api.line.me/v2/bot/message/quota"
    usage_url = "https://api.line.me/v2/bot/message/quota/consumption"
    info = {
        "type": "unknown",
        "limit": None,
        "used": None,
        "remaining": None,
        "label": "ไม่สามารถตรวจสอบโควต้าได้",
    }

    try:
        quota_res = requests.get(quota_url, headers=headers, timeout=15)
        if not quota_res.ok:
            app.logger.error("LINE quota failed: status=%s body=%s", quota_res.status_code, quota_res.text)
            return info

        quota_payload = quota_res.json() or {}
        quota_type = str(quota_payload.get("type", "unknown")).lower()
        info["type"] = quota_type

        if quota_type == "none":
            info["label"] = "แพ็กเกจไม่รองรับ Broadcast"
            return info

        if quota_type == "unlimited":
            info["label"] = "Broadcast ไม่จำกัด"
            return info

        quota_value = quota_payload.get("value")
        if quota_value is None:
            info["label"] = "ไม่พบข้อมูลโควต้า"
            return info

        info["limit"] = int(quota_value)

        usage_res = requests.get(usage_url, headers=headers, timeout=15)
        if usage_res.ok:
            usage_payload = usage_res.json() or {}
            used_value = int(usage_payload.get("totalUsage", 0))
            info["used"] = used_value
            info["remaining"] = max(0, info["limit"] - used_value)
            info["label"] = f"คงเหลือ {info['remaining']} จาก {info['limit']} ครั้ง (ใช้ไป {used_value} ครั้ง)"
        else:
            info["label"] = f"โควต้ารวม {info['limit']} ครั้ง (ไม่สามารถดึงยอดใช้แล้วได้)"

        return info
    except Exception:
        app.logger.exception("Failed to query LINE broadcast quota")
        return info


def _is_allowed_image_file(filename: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    return ext in {".jpg", ".jpeg", ".png", ".webp"}


def _save_product_image(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        return ""
    if not _is_allowed_image_file(file_storage.filename):
        raise ValueError("รองรับเฉพาะไฟล์ภาพ .jpg .jpeg .png .webp")

    file_storage.stream.seek(0)
    try:
        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image)
    except UnidentifiedImageError as e:
        raise ValueError("ไฟล์ที่อัปโหลดไม่ใช่รูปภาพที่ถูกต้อง") from e

    if image.mode != "RGB":
        image = image.convert("RGB")

    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    image.thumbnail((960, 960), resample)

    filename = f"prd_{uuid.uuid4().hex}.jpg"
    target_path = PRODUCT_UPLOAD_DIR / filename
    image.save(target_path, format="JPEG", quality=82, optimize=True, progressive=True)
    return f"/static/uploads/products/{filename}"


def _delete_local_product_image(image_url: str | None) -> None:
    if not image_url:
        return
    prefix = "/static/uploads/products/"
    if not image_url.startswith(prefix):
        return

    file_name = secure_filename(Path(image_url).name)
    if not file_name:
        return

    file_path = PRODUCT_UPLOAD_DIR / file_name
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception:
        app.logger.warning("Could not delete old product image: %s", file_path)


def _log_inventory_movement(
    product_id: int,
    movement_type: str,
    quantity: int,
    note: str = "",
    order_id: int | None = None,
    customer_user_id: str = "",
    customer_name: str = "",
    commit: bool = False,
) -> None:
    if quantity <= 0:
        return
    log = InventoryLog(
        product_id=product_id,
        movement_type=movement_type.upper(),
        quantity=quantity,
        order_id=order_id,
        customer_user_id=(customer_user_id or "").strip() or None,
        customer_name=(customer_name or "").strip() or None,
        note=note.strip()[:255],
    )
    db.session.add(log)
    if commit:
        db.session.commit()


def _products_grouped_by_category(products: list[Product]) -> list[dict]:
    buckets: dict[str, list[Product]] = {"อาหาร": [], "เครื่องดื่ม": []}
    for product in products:
        category = _normalize_product_category(product.category, product.code)
        buckets.setdefault(category, []).append(product)

    groups = []
    for category_name in ["อาหาร", "เครื่องดื่ม"]:
        items = sorted(buckets.get(category_name, []), key=lambda p: (p.code or "", p.name or ""))
        groups.append({"name": category_name, "items": items, "count": len(items)})
    return groups


def _normalize_product_category(category: str | None, code: str | None = None) -> str:
    raw = (category or "").strip().lower()
    code_prefix = (code or "").strip().upper()[:1]

    if raw in {"drink", "drinks", "beverage", "beverages", "เครื่องดื่ม"} or code_prefix == "D":
        return "เครื่องดื่ม"
    if raw in {"food", "foods", "meal", "meals", "อาหาร"} or code_prefix == "F":
        return "อาหาร"
    return "อาหาร"


def _today_dashboard_metrics() -> dict:
    today = date.today()
    orders_today_query = Order.query.filter(func.date(Order.created_at) == today)
    orders_today = orders_today_query.all()

    paid_orders = [o for o in orders_today if o.status == "PAID"]
    sales_today = sum(o.total_amount for o in paid_orders)
    paid_count = len(paid_orders)

    metrics = {
        "orders_count": len(orders_today),
        "sales_today": sales_today,
        "paid_count": paid_count,
        "pending_count": sum(1 for o in orders_today if o.status == "PENDING"),
        "cancelled_count": sum(1 for o in orders_today if o.status == "CANCELLED"),
        "avg_order_value": (sales_today / paid_count) if paid_count > 0 else 0,
    }

    month_start = today.replace(day=1)
    month_sales = db.session.query(func.coalesce(func.sum(Order.total_amount), 0.0)).filter(
        Order.status == "PAID",
        func.date(Order.created_at) >= month_start,
    ).scalar()
    metrics["monthly_sales"] = float(month_sales or 0)

    top_products = (
        db.session.query(
            Product.code.label("code"),
            Product.name.label("name"),
            func.sum(OrderItem.quantity).label("qty"),
            func.sum(OrderItem.quantity * OrderItem.price).label("revenue"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status == "PAID", func.date(Order.created_at) == today)
        .group_by(Product.id, Product.code, Product.name)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(5)
        .all()
    )

    low_stock_threshold_raw = _get_setting("low_stock_threshold", "10")
    try:
        low_stock_threshold = int(low_stock_threshold_raw)
    except ValueError:
        low_stock_threshold = 10
    low_stock_threshold = min(max(low_stock_threshold, 0), 99999)

    low_stock_products = (
        Product.query
        .filter(Product.is_active.is_(True), Product.stock <= low_stock_threshold)
        .order_by(Product.stock.asc(), Product.code.asc())
        .limit(10)
        .all()
    )

    metrics["top_products"] = top_products
    metrics["low_stock_threshold"] = low_stock_threshold
    metrics["low_stock_count"] = len(low_stock_products)
    metrics["low_stock_products"] = low_stock_products
    return metrics


def _serialize_order(order: Order) -> dict:
    return {
        "id": order.id,
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M"),
        "user_id": order.user_id,
        "user_name": _get_line_display_name(order.user_id),
        "delivery_method": order.delivery_method or "PICKUP",
        "delivery_note": order.delivery_note or "",
        "total_amount": float(order.total_amount),
        "status": order.status,
        "items": [
            {
                "code": item.product.code if item.product else "-",
                "name": item.product.name if item.product else "(ลบสินค้าแล้ว)",
                "quantity": item.quantity,
            }
            for item in order.items
        ],
    }


def _serialize_top_product(item) -> dict:
    return {
        "code": item.code,
        "name": item.name,
        "qty": int(item.qty or 0),
        "revenue": float(item.revenue or 0),
    }


def _serialize_low_stock_product(product: Product) -> dict:
    return {
        "code": product.code,
        "name": product.name,
        "stock": int(product.stock or 0),
    }


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    price: float

# ลบ MENU_ITEMS ออกไป เพราะเราจะดึงจากฐานข้อมูล (Product) แทน
# In-memory cart: user_id -> {item_code: quantity}
CARTS: dict[str, dict[str, int]] = {}
DELIVERY_PREFS: dict[str, dict[str, str]] = {}


def _get_or_create_delivery_pref(user_id: str) -> dict[str, str]:
    pref = DELIVERY_PREFS.get(user_id)
    if pref and pref.get("method") == "PICKUP":
        return pref
    default_pref = {"method": "PICKUP", "note": "รับหน้าร้าน"}
    DELIVERY_PREFS[user_id] = default_pref
    return default_pref


def _set_delivery_pref(user_id: str, method: str, note: str) -> None:
    DELIVERY_PREFS[user_id] = {"method": "PICKUP", "note": "รับหน้าร้าน"}


def _delivery_text(method: str, note: str = "") -> str:
    return "รับหน้าร้าน"


def _delivery_text_for_user(user_id: str) -> str:
    pref = _get_or_create_delivery_pref(user_id)
    return _delivery_text(pref.get("method", "PICKUP"), pref.get("note", ""))

def _menu_text() -> str:
    is_open, open_reason = _shop_open_state()
    lines = ["เมนูร้านอาหารตามสั่งและเครื่องดื่ม"]
    lines.append(f"สถานะร้าน: {'เปิดรับออเดอร์' if is_open else 'ปิดรับออเดอร์'}")
    lines.append(open_reason)
    lines.append("")
    products = Product.query.filter_by(is_active=True).all()
    for item in products:
        lines.append(f"- {item.code} {item.name} {item.price:.0f} บาท (เหลือ {item.stock})")
    lines.append("")
    lines.append("คำสั่งที่ใช้:")
    lines.append("เมนู")
    lines.append("สั่ง <รหัสเมนู> <จำนวน>   เช่น สั่ง F01 2")
    lines.append("ตะกร้า")
    lines.append("ล้างตะกร้า")
    lines.append("รับหน้าร้าน")
    lines.append("วิธีรับสินค้า (รับหน้าร้านเท่านั้น)")
    lines.append("ชำระเงิน")
    lines.append("verify <QR_PAYLOAD>   ยืนยันสลิปด้วย text")
    lines.append("หรือ เพียงแค่ **ส่งรูปสลิป** เข้ามาได้เลย")
    test_mode = _get_setting("test_mode_enabled", "0") == "1"
    if test_mode:
        lines.append("\n[TEST MODE] testpay - จำลองการชำระเงิน")
    return "\n".join(lines)


def _extract_amount_from_text(text: str) -> float | None:
    # Extract number formats like 1,234.56 or 1234.56 from arbitrary message text.
    candidates = re.findall(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|\d+(?:\.\d{1,2})", text)
    if not candidates:
        return None
    values: list[float] = []
    for item in candidates:
        try:
            values.append(float(item.replace(",", "")))
        except ValueError:
            continue
    if not values:
        return None
    # Use the largest positive value as payment amount candidate.
    return max(v for v in values if v >= 0)


def _get_user_id(event: dict) -> str:
    source = event.get("source", {})
    user_id = source.get("userId")
    if user_id:
        return user_id
    return "unknown"


def _verify_signature(body: str, signature: str) -> bool:
    digest = hmac.new(CHANNEL_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def _reply_message(reply_token: str, messages: list[dict]) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "replyToken": reply_token,
        "messages": messages,
    }
    res = requests.post(url, headers=headers, json=payload, timeout=15)
    if not res.ok:
        app.logger.error("LINE reply failed: status=%s body=%s", res.status_code, res.text)
        raise RuntimeError(f"LINE reply failed with status {res.status_code}")


def _push_message(to_user_id: str, messages: list[dict]) -> bool:
    if not to_user_id or to_user_id == "unknown":
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": to_user_id,
        "messages": messages,
    }

    res = requests.post(url, headers=headers, json=payload, timeout=15)
    if not res.ok:
        app.logger.error("LINE push failed: status=%s body=%s", res.status_code, res.text)
        return False
    return True


def _notify_order_completed(order: Order) -> bool:
    shop_name = _get_setting("shop_name", "ร้านค้า Bot")
    delivery_line = _delivery_text(order.delivery_method or "PICKUP", order.delivery_note or "")
    message_text = (
        f"✅ ออเดอร์ #{order.id} เสร็จเรียบร้อยแล้ว\n"
        f"ร้าน: {shop_name}\n"
        f"วิธีรับสินค้า: {delivery_line}\n"
        f"ยอดสุทธิ: {float(order.total_amount):.2f} บาท\n"
        "ขอบคุณที่ใช้บริการครับ"
    )
    return _push_message(order.user_id, [{"type": "text", "text": message_text}])


def _verify_slip_with_payload(qr_payload: str, expected_amount: float) -> dict:
    """Verify bank slip with slip2go API (QR Payload)"""
    if not SLIP2GO_SECRET_KEY or SLIP2GO_SECRET_KEY == "YOUR_SLIP2GO_SECRET_KEY":
        return {"success": False, "error": "Slip2Go API not configured"}
    
    try:
        url = "https://connect.slip2go.com/api/verify-slip/qr-code/info"
        headers = {
            "Authorization": f"Bearer {SLIP2GO_SECRET_KEY}",
            "x-api-key": SLIP2GO_KEY,  # เผื่อระบบบังคับต้องส่ง Key ทั่วไปด้วย
            "Content-Type": "application/json",
        }
        data = {"payload": {"qrCode": qr_payload}}
        
        res = requests.post(url, headers=headers, json=data, timeout=15)
        if not res.ok:
            app.logger.error("Slip2go verify failed: status=%s body=%s", res.status_code, res.text)
            return {"success": False, "error": f"Slip2go API error: {res.status_code}"}
        
        result = res.json()
        if not result.get("success", False) and result.get("data") is None:
            message = result.get("message", "สลิปไม่ถูกต้องหรือหมดอายุ")
            app.logger.warning("Slip2go slip invalid: %s", result)
            return {"success": False, "error": message}
        
        data_info = result.get("data", {})
        amount = data_info.get("amount", 0)
        
        # Check amount
        if abs(float(amount) - expected_amount) > 0.01:
            return {
                "success": False,
                "error": f"ยอดเงินไม่ตรง (ได้รับ {amount:.2f} บ. ต้องได้ {expected_amount:.2f} บ.)",
            }
        
        # Success
        app.logger.info("Slip2go response: %s", json.dumps(result, ensure_ascii=False))
        sender = data_info.get("sender", {})
        receiver = data_info.get("receiver", {})
        
        def _get_name(party):
            if isinstance(party, str):
                return party
            if isinstance(party, dict):
                return party.get("name") or party.get("displayName") or party.get("account", {}).get("name") or party.get("account", {}).get("th") or "ไม่ทราบ"
            return "ไม่ทราบ"
            
        def _get_bank(party):
            if isinstance(party, dict):
                b = party.get("bank", {})
                if isinstance(b, dict):
                    return b.get("name") or b.get("shortName") or b.get("id") or "?"
                elif isinstance(b, str):
                    return b
                return str(b)
            return "?"

        return {
            "success": True,
            "amount": amount,
            "sender": _get_name(sender),
            "receiver": _get_name(receiver),
            "bank_code": _get_bank(receiver),
            "trans_ref": data_info.get("transRef") or data_info.get("referenceNo") or "?",
        }
    except Exception as e:
        app.logger.exception("Slip2go API error: %s", e)
        return {"success": False, "error": f"เกิดข้อผิดพลาด: {str(e)}"}


def _get_line_image_content(message_id: str) -> tuple[bool, str]:
    """Download image from LINE and convert to base64"""
    try:
        headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
        
        # Endpoint ที่ถูกต้องของ LINE สำหรับการโหลดรูปภาพ
        url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
        res = requests.get(url, headers=headers, timeout=15)
        
        if res.ok:
            b64 = base64.b64encode(res.content).decode("utf-8")
            return True, b64
        else:
            app.logger.error("LINE image download failed: status=%s", res.status_code)
            return False, "ไม่สามารถดาวน์โหลดรูปภาพ"
    except Exception as e:
        app.logger.exception("Error downloading LINE image: %s", e)
        return False, f"เกิดข้อผิดพลาด: {str(e)}"


def _verify_slip_with_image(image_base64: str, expected_amount: float) -> dict:
    """Verify bank slip image with slip2go API"""
    if not SLIP2GO_SECRET_KEY or SLIP2GO_SECRET_KEY == "YOUR_SLIP2GO_SECRET_KEY":
        return {"success": False, "error": "Slip2go API not configured"}
    
    try:
        url = "https://connect.slip2go.com/api/verify-slip/qr-base64/info" 
        
        headers = {
            "Authorization": f"Bearer {SLIP2GO_SECRET_KEY}",
            "x-api-key": SLIP2GO_KEY, # เผื่อระบบบังคับต้องส่ง Key ทั่วไปด้วย
            "Content-Type": "application/json",
        }
        
        # ใส่ data:image prefix เข้าไปด้วยเนื่องจากเอกสาร slip2go ระบุรูปแบบว่า data:image/jpeg;base64,...
        prefix = "data:image/jpeg;base64,"
        data = {
            "payload": {
                "imageBase64": f"{prefix}{image_base64}",
                "checkCondition": {
                    "checkDuplicate": True # เพิ่มการเช็คสลิปซ้ำให้ระบบ slip2go ไม่หักโควต้าซ้ำๆ
                }
            }
        }
        
        res = requests.post(url, headers=headers, json=data, timeout=30)
        if not res.ok:
            app.logger.error("Slip2go verify image failed: status=%s body=%s", res.status_code, res.text)
            return {"success": False, "error": f"ตรวจสอบสลิปไม่สำเร็จ (API Error: {res.status_code})"}
        
        result = res.json()
        
        # โครงสร้างปกติของระบบสลิปส่วนมากคือสำเร็จ=true/false
        # หรือถ้าต่างออกไป เราจะเช็คว่ามี data กลับมาหรือไม่
        if not result.get("success", False) and result.get("data") is None:
            message = result.get("message", "สลิปไม่ถูกต้องหรือหมดอายุ")
            app.logger.warning("Slip2go slip invalid: %s", result)
            return {"success": False, "error": message}
        
        data_info = result.get("data", {})
        amount = data_info.get("amount", 0)
        
        # Check amount
        if abs(float(amount) - expected_amount) > 0.01:
            return {
                "success": False,
                "error": f"ยอดเงินไม่ตรง (ได้รับ {amount:.2f} บ. ต้องได้ {expected_amount:.2f} บ.)",
            }
        
        # Success
        app.logger.info("Slip2go response: %s", json.dumps(result, ensure_ascii=False))
        sender = data_info.get("sender", {})
        receiver = data_info.get("receiver", {})
        
        def _get_name(party):
            if isinstance(party, str):
                return party
            if isinstance(party, dict):
                return party.get("name") or party.get("displayName") or party.get("account", {}).get("name") or party.get("account", {}).get("th") or "ไม่ทราบ"
            return "ไม่ทราบ"
            
        def _get_bank(party):
            if isinstance(party, dict):
                b = party.get("bank", {})
                if isinstance(b, dict):
                    return b.get("name") or b.get("shortName") or b.get("id") or "?"
                elif isinstance(b, str):
                    return b
                return str(b)
            return "?"

        return {
            "success": True,
            "amount": amount,
            "sender": _get_name(sender),
            "receiver": _get_name(receiver),
            "bank_code": _get_bank(receiver),
            "trans_ref": data_info.get("transRef") or data_info.get("referenceNo") or "?",
        }
    except Exception as e:
        app.logger.exception("Slip2go image verify error: %s", e)
        return {"success": False, "error": f"เกิดข้อผิดพลาด: {str(e)}"}



def _cart_summary(cart: dict[str, int]) -> tuple[str, float]:
    if not cart:
        return "ยังไม่มีสินค้าในตะกร้า", 0.0

    total = 0.0
    lines = ["รายการในตะกร้า:"]
    for code, qty in cart.items():
        item = Product.query.filter_by(code=code).first()
        if item:
            subtotal = item.price * qty
            total += subtotal
            lines.append(f"- {item.name} x{qty} = {subtotal:.2f} บาท")
        else:
            lines.append(f"- (ไม่พบเมนูรหัส {code})")
    lines.append(f"รวมทั้งหมด: {total:.2f} บาท")
    return "\n".join(lines), total


def _create_promptpay_qr(amount: float) -> tuple[str, str]:
    payload = generate_promptpay_payload(PROMPTPAY_ID, amount)
    file_name = f"promptpay_{uuid.uuid4().hex}.png"
    file_path = QR_DIR / file_name

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(str(file_path))
    return file_name, payload


def _is_admin_authenticated() -> bool:
    return bool(session.get("admin_logged_in"))


def _get_admin_password_hash() -> str:
    # Prefer DB value so changed password persists without editing .env.
    return (_get_setting("admin_password_hash", "") or ADMIN_PASSWORD_HASH).strip()


def _set_admin_password(new_password: str) -> None:
    _set_setting(
        "admin_password_hash",
        generate_password_hash(new_password),
        "Hash รหัสผ่านแอดมินสำหรับหน้า /admin/login",
    )
    db.session.commit()


def _verify_admin_secret_code(secret_code: str) -> bool:
    setting_hash = _get_setting("admin_secret_code_hash", "").strip()
    setting_plain = _get_setting("admin_secret_code", "").strip()
    secret_hash = setting_hash or ADMIN_SECRET_CODE_HASH.strip()
    secret_plain = setting_plain or ADMIN_SECRET_CODE.strip()

    if secret_hash:
        return check_password_hash(secret_hash, secret_code)
    if secret_plain:
        return hmac.compare_digest(secret_code, secret_plain)
    return False


def _is_admin_secret_code_configured() -> bool:
    return bool(
        _get_setting("admin_secret_code_hash", "").strip()
        or _get_setting("admin_secret_code", "").strip()
        or ADMIN_SECRET_CODE_HASH.strip()
        or ADMIN_SECRET_CODE.strip()
    )


def _verify_admin_credentials(username: str, password: str) -> bool:
    if username != ADMIN_USERNAME:
        return False

    password_hash = _get_admin_password_hash()
    if password_hash:
        return check_password_hash(password_hash, password)

    return hmac.compare_digest(password, ADMIN_PASSWORD)


def _safe_next_path(next_path: str | None) -> str:
    if not next_path:
        return "/admin"
    candidate = next_path.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/admin"
    return candidate


@app.before_request
def admin_auth_guard():
    path = request.path or ""
    if not path.startswith("/admin"):
        return None

    if path in {"/admin/login", "/admin/logout", "/admin/forgot-password"}:
        return None

    if _is_admin_authenticated():
        return None

    if path.startswith("/admin/api"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for("admin_login", next=next_url))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if _is_admin_authenticated():
        return redirect(_safe_next_path(request.args.get("next")))

    error_message = ""
    next_path = _safe_next_path(request.args.get("next") or request.form.get("next"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if _verify_admin_credentials(username, password):
            session["admin_logged_in"] = True
            session["admin_username"] = username
            return redirect(next_path)

        error_message = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"

    return render_template("login.html", error_message=error_message, next_path=next_path)


@app.route("/admin/forgot-password", methods=["GET", "POST"])
def admin_forgot_password():
    if _is_admin_authenticated():
        return redirect("/admin")

    error_message = ""
    success_message = ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        secret_code = (request.form.get("secret_code") or "").strip()
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if username != ADMIN_USERNAME:
            error_message = "ไม่พบชื่อผู้ใช้แอดมิน"
        elif not _is_admin_secret_code_configured():
            error_message = "ยังไม่ได้ตั้งค่า SECRET CODE ในระบบ (ADMIN_SECRET_CODE หรือ ADMIN_SECRET_CODE_HASH)"
        elif not secret_code:
            error_message = "กรุณากรอก SECRET CODE"
        elif len(new_password) < 8:
            error_message = "รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร"
        elif new_password != confirm_password:
            error_message = "ยืนยันรหัสผ่านใหม่ไม่ตรงกัน"
        elif not _verify_admin_secret_code(secret_code):
            error_message = "SECRET CODE ไม่ถูกต้อง"
        else:
            _set_admin_password(new_password)
            success_message = "รีเซ็ตรหัสผ่านเรียบร้อยแล้ว กรุณาเข้าสู่ระบบใหม่"

    return render_template(
        "forgot_password.html",
        admin_username=ADMIN_USERNAME,
        error_message=error_message,
        success_message=success_message,
    )


@app.route("/admin/change-password", methods=["GET", "POST"])
def admin_change_password():
    error_message = ""
    success_message = ""
    admin_username = str(session.get("admin_username") or ADMIN_USERNAME)

    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not _verify_admin_credentials(admin_username, current_password):
            error_message = "รหัสผ่านปัจจุบันไม่ถูกต้อง"
        elif len(new_password) < 8:
            error_message = "รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร"
        elif new_password != confirm_password:
            error_message = "ยืนยันรหัสผ่านใหม่ไม่ตรงกัน"
        elif hmac.compare_digest(current_password, new_password):
            error_message = "รหัสผ่านใหม่ต้องไม่ซ้ำรหัสผ่านเดิม"
        else:
            _set_admin_password(new_password)
            success_message = "เปลี่ยนรหัสผ่านเรียบร้อยแล้ว"

    return render_template(
        "change_password.html",
        admin_username=admin_username,
        error_message=error_message,
        success_message=success_message,
    )


@app.get("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_username", None)
    return redirect(url_for("admin_login"))


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "status": "ok",
        "message": "LINE OA bot is running",
        "webhook": "/callback (POST)",
    }


@app.get("/callback")
def callback_get_info():
    return {"status": "ok", "message": "Use POST /callback for LINE webhook"}


@app.get("/debug/config")
def debug_config():
    return {
        "status": "ok",
        "channel_access_token_set": bool(CHANNEL_ACCESS_TOKEN),
        "channel_access_token_length": len(CHANNEL_ACCESS_TOKEN),
        "channel_secret_set": bool(CHANNEL_SECRET),
        "channel_secret_length": len(CHANNEL_SECRET),
        "promptpay_id_set": bool(PROMPTPAY_ID),
        "base_url": BASE_URL,
    }


@app.get("/qrs/<path:filename>")
def serve_qr(filename: str):
    return send_from_directory(QR_DIR, filename)


@app.get("/sound/<path:filename>")
def serve_sound(filename: str):
    return send_from_directory(SOUND_DIR, filename)


# ================== ADMIN DASHBOARD ROUTES ==================
@app.route("/admin")
def admin_dashboard():
    metrics = _today_dashboard_metrics()
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()

    refresh_seconds_raw = _get_setting("dashboard_refresh_seconds", "15")
    try:
        refresh_seconds = int(refresh_seconds_raw)
    except ValueError:
        refresh_seconds = 15
    refresh_seconds = min(max(refresh_seconds, 5), 300)

    return render_template(
        "dashboard.html",
        recent_orders=recent_orders,
        refresh_seconds=refresh_seconds,
        **metrics,
    )


@app.get("/admin/api/dashboard")
def admin_dashboard_api():
    metrics = _today_dashboard_metrics()
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()

    return jsonify(
        {
            "orders_count": metrics["orders_count"],
            "sales_today": float(metrics["sales_today"]),
            "monthly_sales": float(metrics["monthly_sales"]),
            "avg_order_value": float(metrics["avg_order_value"]),
            "paid_count": metrics["paid_count"],
            "pending_count": metrics["pending_count"],
            "cancelled_count": metrics["cancelled_count"],
            "low_stock_threshold": metrics["low_stock_threshold"],
            "low_stock_count": metrics["low_stock_count"],
            "low_stock_products": [_serialize_low_stock_product(item) for item in metrics["low_stock_products"]],
            "top_products": [_serialize_top_product(item) for item in metrics["top_products"]],
            "recent_orders": [_serialize_order(order) for order in recent_orders],
        }
    )

@app.route("/admin/orders")
def admin_orders():
    all_orders = Order.query.order_by(Order.created_at.desc()).all()
    recent_orders = all_orders[:20]

    refresh_seconds_raw = _get_setting("dashboard_refresh_seconds", "15")
    try:
        refresh_seconds = int(refresh_seconds_raw)
    except ValueError:
        refresh_seconds = 15
    refresh_seconds = min(max(refresh_seconds, 5), 300)

    pending_count = len([o for o in all_orders if o.status == "PENDING"])
    paid_count = len([o for o in all_orders if o.status == "PAID"])
    cancelled_count = len([o for o in all_orders if o.status == "CANCELLED"])

    return render_template(
        "orders.html",
        recent_orders=recent_orders,
        refresh_seconds=refresh_seconds,
        pending_count=pending_count,
        paid_count=paid_count,
        cancelled_count=cancelled_count,
        total_count=len(all_orders),
    )

@app.get("/admin/api/orders")
def admin_orders_api():
    all_orders = Order.query.order_by(Order.created_at.desc()).all()
    recent_orders = all_orders[:20]

    pending_count = len([o for o in all_orders if o.status == "PENDING"])
    paid_count = len([o for o in all_orders if o.status == "PAID"])
    cancelled_count = len([o for o in all_orders if o.status == "CANCELLED"])

    return jsonify(
        {
            "pending_count": pending_count,
            "paid_count": paid_count,
            "cancelled_count": cancelled_count,
            "total_count": len(all_orders),
            "recent_orders": [_serialize_order(order) for order in recent_orders],
        }
    )

@app.post("/admin/api/orders/<int:order_id>/mark-paid")
def admin_api_mark_paid(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"success": False, "error": "ไม่พบออเดอร์"}), 404

    if order.status != "PENDING":
        return jsonify({"success": False, "error": "ออเดอร์นี้ไม่สามารถเปลี่ยนเป็น PAID ได้"}), 400

    insufficient_items = []
    for item in order.items:
        product = item.product
        if product and product.stock < item.quantity:
            insufficient_items.append(f"{product.code} คงเหลือ {product.stock}")

    if insufficient_items:
        return jsonify({"success": False, "error": f"สต๊อกไม่พอ: {', '.join(insufficient_items)}"}), 400

    for item in order.items:
        product = item.product
        if product:
            product.stock -= item.quantity
            _log_inventory_movement(
                product.id,
                "SALE",
                item.quantity,
                note=f"ขายจากออเดอร์ #{order.id}",
                order_id=order.id,
                customer_user_id=order.user_id,
                customer_name=_get_line_display_name(order.user_id),
            )

    order.status = "PAID"
    db.session.commit()

    notify_ok = _notify_order_completed(order)
    if not notify_ok:
        app.logger.warning("Could not notify user for completed order_id=%s", order.id)

    return jsonify({"success": True, "message": "อัปเดตสถานะเป็น PAID แล้ว"})

@app.post("/admin/api/orders/<int:order_id>/cancel")
def admin_api_cancel(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return jsonify({"success": False, "error": "ไม่พบออเดอร์"}), 404

    if order.status == "PAID":
        for item in order.items:
            product = item.product
            if product:
                product.stock += item.quantity
                _log_inventory_movement(
                    product.id,
                    "IN",
                    item.quantity,
                    note=f"คืนสต๊อกจากยกเลิกออเดอร์ #{order.id}",
                )

    order.status = "CANCELLED"
    db.session.commit()
    return jsonify({"success": True, "message": "ยกเลิกออเดอร์และคืนสต๊อกแล้ว"})

@app.route("/admin/kitchen")
def admin_kitchen():
    pending_orders = Order.query.filter_by(status="PENDING").order_by(Order.created_at.asc()).all()

    refresh_seconds_raw = _get_setting("dashboard_refresh_seconds", "5")
    try:
        refresh_seconds = int(refresh_seconds_raw)
    except ValueError:
        refresh_seconds = 5
    refresh_seconds = min(max(refresh_seconds, 3), 60)

    return render_template(
        "kitchen.html",
        pending_orders=pending_orders,
        refresh_seconds=refresh_seconds,
        pending_count=len(pending_orders),
    )

@app.get("/admin/api/kitchen")
def admin_api_kitchen():
    pending_orders = Order.query.filter_by(status="PENDING").order_by(Order.created_at.asc()).all()
    
    return jsonify(
        {
            "pending_count": len(pending_orders),
            "pending_orders": [_serialize_order(order) for order in pending_orders],
        }
    )

@app.route("/admin/orders/cancel/<int:order_id>", methods=["POST"])
def admin_orders_cancel(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return '<script>alert("ไม่พบออเดอร์"); window.location="/admin";</script>'

    if order.status == 'PAID':
        for item in order.items:
            product = item.product
            if product:
                product.stock += item.quantity
                _log_inventory_movement(
                    product.id,
                    "IN",
                    item.quantity,
                    note=f"คืนสต๊อกจากยกเลิกออเดอร์ #{order.id}",
                )

    order.status = 'CANCELLED'
    db.session.commit()
    return '<script>alert("ยกเลิกออเดอร์และคืนสต๊อกแล้ว"); window.location="/admin";</script>'

@app.route("/admin/orders/paid/<int:order_id>", methods=["POST"])
def admin_orders_mark_paid(order_id):
    order = db.session.get(Order, order_id)
    if not order:
        return '<script>alert("ไม่พบออเดอร์"); window.location="/admin";</script>'

    if order.status != 'PENDING':
        return '<script>alert("ออเดอร์นี้ไม่สามารถเปลี่ยนเป็น PAID ได้"); window.location="/admin";</script>'

    insufficient_items = []
    for item in order.items:
        product = item.product
        if product and product.stock < item.quantity:
            insufficient_items.append(f"{product.code} คงเหลือ {product.stock}")

    if insufficient_items:
        return '<script>alert("สต๊อกไม่พอ: ' + ', '.join(insufficient_items) + '"); window.location="/admin";</script>'

    for item in order.items:
        product = item.product
        if product:
            product.stock -= item.quantity
            _log_inventory_movement(
                product.id,
                "SALE",
                item.quantity,
                note=f"ขายจากออเดอร์ #{order.id}",
                order_id=order.id,
                customer_user_id=order.user_id,
                customer_name=_get_line_display_name(order.user_id),
            )

    order.status = 'PAID'
    db.session.commit()

    notify_ok = _notify_order_completed(order)
    if not notify_ok:
        app.logger.warning("Could not notify user for completed order_id=%s", order.id)

    return '<script>alert("อัปเดตสถานะเป็น PAID แล้ว"); window.location="/admin";</script>'

@app.route("/admin/products")
def admin_products():
    products = Product.query.order_by(Product.category.asc(), Product.code.asc()).all()
    grouped_products = _products_grouped_by_category(products)
    categories = sorted({(p.category or "ทั่วไป").strip() or "ทั่วไป" for p in products})
    if not categories:
        categories = ["ทั่วไป"]

    return render_template(
        "products.html",
        products=products,
        grouped_products=grouped_products,
        categories=categories,
    )


@app.route("/admin/inventory-logs")
def admin_inventory_logs():
    movement_filter = (request.args.get("type") or "ALL").strip().upper()
    selected_id = request.args.get("id", type=int)

    query = InventoryLog.query.order_by(InventoryLog.occurred_at.desc(), InventoryLog.id.desc())
    if movement_filter in {"IN", "OUT", "SALE"}:
        query = query.filter(InventoryLog.movement_type == movement_filter)
    else:
        movement_filter = "ALL"

    logs = query.limit(300).all()

    selected_log = None
    if selected_id:
        for item in logs:
            if item.id == selected_id:
                selected_log = item
                break
    if selected_log is None and logs:
        selected_log = logs[0]

    return render_template(
        "inventory_logs.html",
        movement_filter=movement_filter,
        logs=logs,
        selected_log=selected_log,
    )

@app.route("/admin/products/add", methods=["POST"])
def admin_products_add():
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    category = _normalize_product_category(request.form.get("category"), code)
    price = request.form.get("price", type=float)
    stock = request.form.get("stock", type=int)
    image_file = request.files.get("image_file")

    if not code or not name or price is None or stock is None:
        return '<script>alert("กรุณากรอกรหัสเมนู ชื่อเมนู ราคา และสต๊อกให้ครบ"); window.history.back();</script>'
    
    if Product.query.filter_by(code=code).first():
        return '<script>alert("รหัสเมนู " + "' + code + '" + " มีอยู่แล้วในระบบ\nกรุณาใช้รหัสอื่น"); window.history.back();</script>'
    
    try:
        image_url = _save_product_image(image_file) if image_file and image_file.filename else ""
        new_product = Product(
            code=code,
            name=name,
            category=category,
            price=price,
            stock=stock,
            image_url=image_url,
        )
        db.session.add(new_product)
        db.session.flush()
        if stock > 0:
            _log_inventory_movement(new_product.id, "IN", stock, note="สต๊อกตั้งต้นตอนเพิ่มสินค้า")
        db.session.commit()
        return '<script>alert("เพิ่มเมนูสำเร็จ"); window.location="/admin/products";</script>'
    except ValueError as e:
        return '<script>alert("' + str(e) + '"); window.history.back();</script>'
    except Exception as e:
        app.logger.exception("Error adding product: %s", e)
        return '<script>alert("เกิดข้อผิดพลาด: ' + str(e)[:50] + '"); window.history.back();</script>'


@app.route("/admin/products/edit/<int:product_id>", methods=["POST"])
def admin_products_edit(product_id):
    product = db.session.get(Product, product_id)
    if not product:
        return '<script>alert("ไม่พบเมนู"); window.location="/admin/products";</script>'

    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    category = _normalize_product_category(request.form.get("category"), code)
    price = request.form.get("price", type=float)
    stock = request.form.get("stock", type=int)
    is_active = bool(request.form.get("is_active"))
    image_file = request.files.get("image_file")

    if not code or not name or price is None or stock is None:
        return '<script>alert("กรุณากรอกข้อมูลให้ครบ"); window.history.back();</script>'

    dup = Product.query.filter(Product.code == code, Product.id != product_id).first()
    if dup:
        return '<script>alert("รหัสเมนูนี้ถูกใช้งานแล้ว"); window.history.back();</script>'

    try:
        old_image = product.image_url
        if image_file and image_file.filename:
            product.image_url = _save_product_image(image_file)
            _delete_local_product_image(old_image)

        product.code = code
        product.name = name
        product.category = category
        product.price = price
        old_stock = int(product.stock or 0)
        product.stock = stock
        product.is_active = is_active
        diff = int(stock) - old_stock
        if diff > 0:
            _log_inventory_movement(product.id, "IN", diff, note="ปรับยอดสต๊อกจากหน้าแก้ไขสินค้า")
        elif diff < 0:
            _log_inventory_movement(product.id, "OUT", abs(diff), note="ปรับยอดสต๊อกจากหน้าแก้ไขสินค้า")
        db.session.commit()
        return '<script>alert("บันทึกการแก้ไขเมนูเรียบร้อยแล้ว"); window.location="/admin/products";</script>'
    except ValueError as e:
        return '<script>alert("' + str(e) + '"); window.history.back();</script>'
    except Exception as e:
        app.logger.exception("Error editing product: %s", e)
        return '<script>alert("เกิดข้อผิดพลาด: ' + str(e)[:80] + '"); window.history.back();</script>'

@app.route("/admin/products/stock/<int:product_id>", methods=["POST"])
def admin_products_stock(product_id):
    stock = request.form.get("stock", type=int)
    product = db.session.get(Product, product_id)
    if product and stock is not None:
        old_stock = int(product.stock or 0)
        product.stock = stock
        diff = int(stock) - old_stock
        if diff > 0:
            _log_inventory_movement(product.id, "IN", diff, note="ปรับยอดสต๊อกจากหน้าเมนูและสต๊อก")
        elif diff < 0:
            _log_inventory_movement(product.id, "OUT", abs(diff), note="ปรับยอดสต๊อกจากหน้าเมนูและสต๊อก")
        db.session.commit()
    return '<script>window.location="/admin/products";</script>'


@app.route("/admin/products/stock/in/<int:product_id>", methods=["POST"])
def admin_products_stock_in(product_id):
    qty = request.form.get("qty", type=int)
    note = (request.form.get("note") or "").strip()
    product = db.session.get(Product, product_id)
    if not product or qty is None or qty <= 0:
        return '<script>alert("ข้อมูลไม่ถูกต้อง"); window.location="/admin/products";</script>'

    product.stock = int(product.stock or 0) + qty
    _log_inventory_movement(product.id, "IN", qty, note=note or "รับสินค้าเข้าคลัง")
    db.session.commit()
    return '<script>alert("บันทึกรับสินค้าเข้าคลังเรียบร้อย"); window.location="/admin/products";</script>'


@app.route("/admin/products/stock/out/<int:product_id>", methods=["POST"])
def admin_products_stock_out(product_id):
    qty = request.form.get("qty", type=int)
    note = (request.form.get("note") or "").strip()
    product = db.session.get(Product, product_id)
    if not product or qty is None or qty <= 0:
        return '<script>alert("ข้อมูลไม่ถูกต้อง"); window.location="/admin/products";</script>'

    current_stock = int(product.stock or 0)
    if qty > current_stock:
        return '<script>alert("จำนวนเบิกออกมากกว่าสต๊อกคงเหลือ"); window.location="/admin/products";</script>'

    product.stock = current_stock - qty
    _log_inventory_movement(product.id, "OUT", qty, note=note or "เบิกสินค้าออกจากคลัง")
    db.session.commit()
    return '<script>alert("บันทึกจ่ายสินค้าออกเรียบร้อย"); window.location="/admin/products";</script>'

@app.route("/admin/products/delete/<int:product_id>", methods=["POST"])
def admin_products_delete(product_id):
    product = db.session.get(Product, product_id)
    if product:
        InventoryLog.query.filter_by(product_id=product.id).delete()
        _delete_local_product_image(product.image_url)
        db.session.delete(product)
        db.session.commit()
    return '<script>alert("ลบเมนูสำเร็จ"); window.location="/admin/products";</script>'

@app.route("/admin/sales")
def admin_sales():
    period_text = request.args.get("period", "7")
    day_options = {"7": 7, "30": 30, "90": 90}
    days = day_options.get(period_text, 7)

    today = date.today()
    start_date = today - timedelta(days=days - 1)

    orders_range = Order.query.filter(func.date(Order.created_at) >= start_date).all()
    paid_orders = [o for o in orders_range if o.status == "PAID"]

    revenue_total = sum(o.total_amount for o in paid_orders)
    order_count = len(orders_range)
    paid_count = len(paid_orders)
    avg_order_value = (revenue_total / paid_count) if paid_count > 0 else 0

    labels = []
    revenue_series = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        labels.append(d.strftime("%d/%m"))
        value = sum(o.total_amount for o in paid_orders if o.created_at.date() == d)
        revenue_series.append(round(value, 2))

    top_products = (
        db.session.query(
            Product.code.label("code"),
            Product.name.label("name"),
            func.sum(OrderItem.quantity).label("qty"),
            func.sum(OrderItem.quantity * OrderItem.price).label("revenue"),
        )
        .join(OrderItem, OrderItem.product_id == Product.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.status == "PAID", func.date(Order.created_at) >= start_date)
        .group_by(Product.id, Product.code, Product.name)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(10)
        .all()
    )

    return render_template(
        "sales.html",
        period=days,
        revenue_total=revenue_total,
        order_count=order_count,
        paid_count=paid_count,
        avg_order_value=avg_order_value,
        labels=labels,
        revenue_series=revenue_series,
        top_products=top_products,
        start_date=start_date,
        end_date=today,
    )

@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    if request.method == "POST":
        form_data = {
            "shop_name": (request.form.get("shop_name") or "ร้านค้า Bot").strip(),
            "shop_phone": (request.form.get("shop_phone") or "").strip(),
            "shop_address": (request.form.get("shop_address") or "").strip(),
            "dashboard_refresh_seconds": (request.form.get("dashboard_refresh_seconds") or "15").strip(),
            "low_stock_threshold": (request.form.get("low_stock_threshold") or "10").strip(),
            "test_mode_enabled": "1" if request.form.get("test_mode_enabled") else "0",
            "line_contact_message": (
                request.form.get("line_contact_message")
                or "กรุณารอสักครู่ แอดมินจะรีบเข้ามาดูแลและตอบกลับให้เร็วที่สุดครับ/ค่ะ"
            ).strip(),
            "shop_status_mode": (request.form.get("shop_status_mode") or "AUTO").strip().upper(),
            "shop_open_time": (request.form.get("shop_open_time") or "08:00").strip(),
            "shop_close_time": (request.form.get("shop_close_time") or "20:00").strip(),
            "shop_closed_message": (
                request.form.get("shop_closed_message")
                or ""
            ).strip(),
        }

        try:
            refresh_value = int(form_data["dashboard_refresh_seconds"])
        except ValueError:
            refresh_value = 15
        form_data["dashboard_refresh_seconds"] = str(min(max(refresh_value, 5), 300))

        try:
            low_stock_threshold = int(form_data["low_stock_threshold"])
        except ValueError:
            low_stock_threshold = 10
        form_data["low_stock_threshold"] = str(min(max(low_stock_threshold, 0), 99999))

        if form_data["shop_status_mode"] not in {"AUTO", "MANUAL_OPEN", "MANUAL_CLOSE"}:
            form_data["shop_status_mode"] = "AUTO"

        form_data["shop_open_time"] = _parse_hhmm(form_data["shop_open_time"], "08:00").strftime("%H:%M")
        form_data["shop_close_time"] = _parse_hhmm(form_data["shop_close_time"], "20:00").strftime("%H:%M")

        descriptions = {
            "shop_name": "ชื่อร้านที่แสดงใน dashboard",
            "shop_phone": "เบอร์โทรร้าน",
            "shop_address": "ที่อยู่ร้าน",
            "dashboard_refresh_seconds": "จำนวนวินาทีรีเฟรช dashboard",
            "low_stock_threshold": "เกณฑ์สต๊อกต่ำสุดสำหรับแจ้งเตือนสินค้าใกล้หมด",
            "test_mode_enabled": "เปิดการทดสอบระบบแบบไม่ต้องจ่ายเงินจริง",
            "line_contact_message": "ข้อความตอบกลับเมื่อลูกค้าขอติดต่อแอดมิน",
            "shop_status_mode": "โหมดสถานะร้าน: AUTO / MANUAL_OPEN / MANUAL_CLOSE",
            "shop_open_time": "เวลาเปิดร้าน (HH:MM)",
            "shop_close_time": "เวลาปิดร้าน (HH:MM)",
            "shop_closed_message": "ข้อความแจ้งลูกค้าเมื่อร้านปิดรับออเดอร์",
        }

        for key, value in form_data.items():
            _set_setting(key, value, descriptions.get(key))
        db.session.commit()

        return '<script>alert("บันทึกการตั้งค่าเรียบร้อยแล้ว"); window.location="/admin/settings";</script>'

    settings_data = {
        "shop_name": _get_setting("shop_name", "ร้านค้า Bot"),
        "shop_phone": _get_setting("shop_phone", ""),
        "shop_address": _get_setting("shop_address", ""),
        "dashboard_refresh_seconds": _get_setting("dashboard_refresh_seconds", "15"),
        "low_stock_threshold": _get_setting("low_stock_threshold", "10"),
        "test_mode_enabled": _get_setting("test_mode_enabled", "0"),
        "line_contact_message": _get_setting(
            "line_contact_message",
            "กรุณารอสักครู่ แอดมินจะรีบเข้ามาดูแลและตอบกลับให้เร็วที่สุดครับ/ค่ะ",
        ),
        "shop_status_mode": _get_setting("shop_status_mode", "AUTO"),
        "shop_open_time": _get_setting("shop_open_time", "08:00"),
        "shop_close_time": _get_setting("shop_close_time", "20:00"),
        "shop_closed_message": _get_setting(
            "shop_closed_message",
            "ขออภัย ร้านยังไม่เปิดรับออเดอร์ในขณะนี้ กรุณาลองใหม่ภายหลัง",
        ),
    }

    is_open, open_reason = _shop_open_state()
    quota_info = _line_broadcast_quota()

    return render_template(
        "settings.html",
        settings=settings_data,
        shop_is_open=is_open,
        shop_open_reason=open_reason,
        broadcast_quota=quota_info,
    )


@app.post("/admin/broadcast")
def admin_broadcast():
    text_message = (request.form.get("broadcast_text") or "").strip()
    image_file = request.files.get("broadcast_image")

    messages: list[dict] = []
    if text_message:
        messages.append({"type": "text", "text": text_message[:5000]})

    if image_file and image_file.filename:
        if not BASE_URL.startswith("https://"):
            return '<script>alert("ส่งรูป broadcast ไม่ได้: ต้องตั้งค่า BASE_URL เป็น HTTPS ก่อน"); window.location="/admin/settings";</script>'

        safe_name = secure_filename(image_file.filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png"}:
            return '<script>alert("รองรับเฉพาะไฟล์ภาพ .jpg .jpeg .png"); window.location="/admin/settings";</script>'

        file_name = f"broadcast_{uuid.uuid4().hex}{ext}"
        file_path = BROADCAST_DIR / file_name
        image_file.save(file_path)
        image_url = f"{BASE_URL}/static/broadcasts/{file_name}"
        messages.append(
            {
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            }
        )

    if not messages:
        return '<script>alert("กรุณากรอกข้อความหรือเลือกรูปอย่างน้อย 1 อย่าง"); window.location="/admin/settings";</script>'

    ok, detail = _line_broadcast(messages)
    if not ok:
        return f'<script>alert("ส่ง Broadcast ไม่สำเร็จ: {detail}"); window.location="/admin/settings";</script>'

    _set_setting("last_broadcast_at", _now_bkk().strftime("%Y-%m-%d %H:%M:%S"), "เวลาล่าสุดที่ส่ง Broadcast")
    db.session.commit()
    return '<script>alert("ส่ง Broadcast เรียบร้อยแล้ว"); window.location="/admin/settings";</script>'
# ==========================================================

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    if not _verify_signature(body, signature):
        app.logger.warning("Invalid signature on /callback")
        abort(400)

    data = request.get_json(silent=True) or {}
    events = data.get("events", [])
    app.logger.info("Webhook received: %s event(s)", len(events))
    for event in events:
        try:
            _handle_event(event)
        except Exception:
            app.logger.exception("Error while handling LINE event")

    return "OK"


def _build_menu_flex(title: str, prefix: str) -> dict:
    bubbles = []
    products = Product.query.filter(Product.code.startswith(prefix), Product.is_active==True).all()
    if not products:
        return {
            "type": "text", 
            "text": f"ยังไม่มีเมนูในหมวดหมู่นี้ค่ะ"
        }
        
    for item in products:
        # ใช้รูปจำลองถ้ายังไม่ตั้งค่ารูป
        img_url = (item.image_url or "").strip()
        if img_url.startswith("/static/"):
            if BASE_URL.startswith("https://"):
                img_url = f"{BASE_URL}{img_url}"
            else:
                img_url = "https://via.placeholder.com/250x250.png?text=No+Image"
        elif not img_url:
            img_url = "https://via.placeholder.com/250x250.png?text=No+Image"
        
        bubble = {
            "type": "bubble",
            "hero": {
                "type": "image",
                "url": img_url,
                "size": "full",
                "aspectRatio": "20:13",
                "aspectMode": "cover"
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": item.name, "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": f"รหัส: {item.code} | เหลือ: {item.stock}", "color": "#aaaaaa", "size": "sm"},
                    {"type": "text", "text": f"ราคา {item.price:.0f} บาท", "color": "#ff5551", "size": "md", "weight": "bold", "margin": "sm"}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "color": "#00B900",
                        "action": {
                            "type": "message",
                            "label": "ใส่ตะกร้า 1 ที่",
                            "text": f"สั่ง {item.code} 1"
                        }
                    }
                ]
            }
        }
        bubbles.append(bubble)

    return {
        "type": "flex",
        "altText": title,
        "contents": {
            "type": "carousel",
            "contents": bubbles
        }
    }


def _handle_event(event: dict):
    if event.get("type") != "message":
        return
    
    message = event.get("message", {})
    message_type = message.get("type")
    user_id = _get_user_id(event)
    reply_token = event.get("replyToken")
    
    if not reply_token:
        return
    
    # Handle IMAGE messages
    if message_type == "image":
        is_open, _ = _shop_open_state()
        if not is_open:
            _reply_message(reply_token, [{"type": "text", "text": _shop_closed_reply_text()}])
            return

        cart = CARTS.get(user_id, {})
        _, total = _cart_summary(cart)
        
        if total <= 0:
            _reply_message(reply_token, [{"type": "text", "text": "ตะกร้าว่างอยู่ ไม่มีอะไรให้ verificate"}])
            return
        
        message_id = message.get("id")
        app.logger.info("Processing slip image from message %s", message_id)
        
        # Download image and get base64
        success, result = _get_line_image_content(message_id)
        if not success:
            _reply_message(reply_token, [{"type": "text", "text": f"❌ {result}"}])
            return
        
        # Verify with Slip2go
        verify_result = _verify_slip_with_image(result, total)
        if verify_result["success"]:
            # สร้างออเดอร์เข้าคิวครัว (PENDING)
            new_order = Order(
                user_id=user_id,
                total_amount=total,
                status='PENDING',
                delivery_method="PICKUP",
                delivery_note="รับหน้าร้าน",
            )
            db.session.add(new_order)
            db.session.flush() # เพื่อให้ได้ new_order.id
            
            for code, qty in cart.items():
                p = Product.query.filter_by(code=code).first()
                if p:
                    item_ordered = OrderItem(order_id=new_order.id, product_id=p.id, quantity=qty, price=p.price)
                    db.session.add(item_ordered)
            
            db.session.commit()
            
            # ล้างตะกร้า
            CARTS[user_id] = {}

            msg = f"""✅ ยืนยันการชำระเงินสำเร็จ
ออเดอร์ที่: #{new_order.id}
ยอด: {verify_result['amount']:.2f} บาท
อ้างอิง: {verify_result['trans_ref']}
วิธีรับสินค้า: {_delivery_text(new_order.delivery_method, new_order.delivery_note or '')}

ร้านค้าได้รับออเดอร์ของคุณแล้ว และส่งเข้าครัวเรียบร้อย กรุณารอสักครู่ค่ะ"""
            _reply_message(reply_token, [{"type": "text", "text": msg}])
        else:
            error_msg = verify_result.get("error", "เกิดข้อผิดพลาดในการตรวจสลิป")
            _reply_message(reply_token, [{"type": "text", "text": f"❌ {error_msg}"}])
        return
    
    # Handle TEXT messages
    if message_type != "text":
        return

    text = str(message.get("text", "")).strip()
    lower_text = text.lower()

    if text in ["เมนู", "เมนูอาหาร", "เมนูน้ำ"] or lower_text == "menu":
        if text == "เมนูอาหาร":
            flex_message = _build_menu_flex("รายการอาหาร", "F")
            _reply_message(reply_token, [flex_message])
        elif text == "เมนูน้ำ":
            flex_message = _build_menu_flex("รายการน้ำ", "D")
            _reply_message(reply_token, [flex_message])
        else:
            _reply_message(reply_token, [{"type": "text", "text": _menu_text()}])
        return

    if text in ["ตะกร้า", "ตระกร้าสินค้า"]:
        cart = CARTS.get(user_id, {})
        summary, _ = _cart_summary(cart)
        delivery_info = _delivery_text_for_user(user_id)
        _reply_message(reply_token, [{"type": "text", "text": f"{summary}\n\nวิธีรับสินค้า: {delivery_info}"}])
        return

    if text in ["ล้างตะกร้า", "ล้างตระกร้า", "ล้างตระกร้าสินค้า"]:
        CARTS[user_id] = {}
        _reply_message(reply_token, [{"type": "text", "text": "ล้างตะกร้าเรียบร้อย"}])
        return

    if text == "รับหน้าร้าน" or lower_text == "pickup":
        _set_delivery_pref(user_id, "PICKUP", "รับหน้าร้าน")
        _reply_message(
            reply_token,
            [{"type": "text", "text": "ตั้งค่าวิธีรับสินค้าเป็น: รับหน้าร้าน เรียบร้อยแล้ว"}],
        )
        return

    if text.startswith("จัดส่ง"):
        _reply_message(
            reply_token,
            [{"type": "text", "text": "ร้านนี้ให้บริการรับหน้าร้านเท่านั้น\nพิมพ์ 'รับหน้าร้าน' เพื่อยืนยันวิธีรับสินค้า"}],
        )
        return

    if text == "วิธีรับสินค้า" or lower_text == "delivery":
        current_delivery = _delivery_text_for_user(user_id)
        _reply_message(
            reply_token,
            [
                {
                    "type": "text",
                    "text": (
                        f"วิธีรับสินค้าปัจจุบัน: {current_delivery}\n"
                        "เปลี่ยนได้โดยพิมพ์:\n"
                        "- รับหน้าร้าน"
                    ),
                }
            ],
        )
        return

    if text == "ติดต่อ ADMIN":
        contact_message = _get_setting(
            "line_contact_message",
            "กรุณารอสักครู่ แอดมินจะรีบเข้ามาดูแลและตอบกลับให้เร็วที่สุดครับ/ค่ะ",
        )
        _reply_message(reply_token, [{"type": "text", "text": contact_message}])
        return

    if text == "ชำระเงิน":
        is_open, _ = _shop_open_state()
        if not is_open:
            _reply_message(reply_token, [{"type": "text", "text": _shop_closed_reply_text()}])
            return

        cart = CARTS.get(user_id, {})
        summary, total = _cart_summary(cart)
        if total <= 0:
            _reply_message(reply_token, [{"type": "text", "text": "ตะกร้าว่างอยู่ พิมพ์ 'เมนู' เพื่อเริ่มสั่งอาหาร"}])
            return

        delivery_info = _delivery_text_for_user(user_id)

        file_name, payload = _create_promptpay_qr(total)
        messages = [{"type": "text", "text": summary + f"\n\nวิธีรับสินค้า: {delivery_info}\n\nสแกน QR PromptPay เพื่อชำระเงิน"}]
        
        test_mode = _get_setting("test_mode_enabled", "0") == "1"
        if test_mode:
            messages.append({"type": "text", "text": "\n[TEST MODE] พิมพ์ 'testpay' เพื่อจำลองการชำระเงิน"})

        if BASE_URL.startswith("https://"):
            qr_url = f"{BASE_URL}/qrs/{file_name}"
            messages.append(
                {
                    "type": "image",
                    "originalContentUrl": qr_url,
                    "previewImageUrl": qr_url,
                }
            )
        else:
            messages.append(
                {
                    "type": "text",
                    "text": (
                        "ยังไม่ได้ตั้งค่า BASE_URL เป็น HTTPS จึงส่งรูป QR ไป LINE ไม่ได้\n"
                        "ตั้งค่า BASE_URL แล้วลองใหม่อีกครั้ง\n"
                        f"Payload: {payload}"
                    ),
                }
            )

        _reply_message(reply_token, messages)
        return
    
    if lower_text == "testpay":
        is_open, _ = _shop_open_state()
        if not is_open:
            _reply_message(reply_token, [{"type": "text", "text": _shop_closed_reply_text()}])
            return

        test_mode = _get_setting("test_mode_enabled", "0") == "1"
        if not test_mode:
            _reply_message(reply_token, [{"type": "text", "text": "⚠️ TEST MODE ปิดอยู่ ติดต่อแอดมินเพื่อเปิดการทดสอบ"}])
            return
        
        cart = CARTS.get(user_id, {})
        summary, total = _cart_summary(cart)
        if total <= 0:
            _reply_message(reply_token, [{"type": "text", "text": "ตะกร้าว่างอยู่ พิมพ์ 'เมนู' เพื่อเริ่มสั่งอาหาร"}])
            return
        
        # สร้างออเดอร์ในโหมดทดสอบและส่งเข้าคิวครัว
        new_order = Order(
            user_id=user_id,
            total_amount=total,
            status='PENDING',
            delivery_method="PICKUP",
            delivery_note="รับหน้าร้าน",
        )
        db.session.add(new_order)
        db.session.flush()
        
        for code, qty in cart.items():
            p = Product.query.filter_by(code=code).first()
            if p:
                item_ordered = OrderItem(order_id=new_order.id, product_id=p.id, quantity=qty, price=p.price)
                db.session.add(item_ordered)
        
        db.session.commit()
        CARTS[user_id] = {}
        
        msg = f"""✅ [TEST MODE] ยืนยันการชำระเงินสำเร็จ
ออเดอร์ที่: #{new_order.id}
ยอด: {total:.2f} บาท
    วิธีรับสินค้า: {_delivery_text(new_order.delivery_method, new_order.delivery_note or '')}

⚠️ นี่คือการทดสอบเท่านั้น ไม่ได้จ่ายเงินจริง
    ร้านค้าได้รับออเดอร์ของคุณแล้ว และส่งเข้าครัวเรียบร้อย"""
        _reply_message(reply_token, [{"type": "text", "text": msg}])
        return

    if text == "ยืนยันชำระ":
        CARTS[user_id] = {}
        _reply_message(reply_token, [{"type": "text", "text": "บันทึกการชำระเงินแล้ว ขอบคุณครับ"}])
        return

    if text.startswith("ตรวจยอด") or lower_text.startswith("checkamount"):
        cart = CARTS.get(user_id, {})
        _, total = _cart_summary(cart)
        if total <= 0:
            _reply_message(reply_token, [{"type": "text", "text": "ตะกร้าว่างอยู่ พิมพ์ 'เมนู' เพื่อเริ่มสั่งอาหาร"}])
            return

        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            _reply_message(reply_token, [{"type": "text", "text": "รูปแบบ: ตรวจยอด <ข้อความตอบกลับจาก Thunder Bot>"}])
            return

        thunder_reply_text = parts[1].strip()
        paid_amount = _extract_amount_from_text(thunder_reply_text)
        if paid_amount is None:
            _reply_message(reply_token, [{"type": "text", "text": "ไม่พบยอดเงินในข้อความที่ส่งมา กรุณาวางข้อความตอบกลับจาก Thunder Bot ให้ครบ"}])
            return

        if abs(paid_amount - total) <= 0.01:
            _reply_message(
                reply_token,
                [{"type": "text", "text": f"✅ ยอดตรงกัน ({paid_amount:.2f} บาท)\nพิมพ์ 'ยืนยันชำระ' เพื่อบันทึกการสั่งซื้อ"}],
            )
        else:
            _reply_message(
                reply_token,
                [{"type": "text", "text": f"❌ ยอดไม่ตรง\nยอดจาก Thunder: {paid_amount:.2f} บาท\nยอดในตะกร้า: {total:.2f} บาท"}],
            )
        return

    if text.startswith("สั่ง") or lower_text.startswith("order"):
        is_open, _ = _shop_open_state()
        if not is_open:
            _reply_message(reply_token, [{"type": "text", "text": _shop_closed_reply_text()}])
            return

        parts = text.split()
        if len(parts) != 3:
            _reply_message(reply_token, [{"type": "text", "text": "รูปแบบไม่ถูกต้อง ใช้: สั่ง <รหัสเมนู> <จำนวน> เช่น สั่ง F01 2"}])
            return

        _, code, qty_text = parts
        code = code.upper()

        product = Product.query.filter_by(code=code, is_active=True).first()
        if not product:
            _reply_message(reply_token, [{"type": "text", "text": "ไม่พบรหัสเมนูนี้ พิมพ์ 'เมนู' เพื่อดูรายการ"}])
            return

        try:
            qty = int(qty_text)
            if qty <= 0:
                raise ValueError
        except ValueError:
            _reply_message(reply_token, [{"type": "text", "text": "จำนวนต้องเป็นเลขจำนวนเต็มมากกว่า 0"}])
            return

        if product.stock <= 0:
            _reply_message(reply_token, [{"type": "text", "text": f"เมนู {product.code} หมดชั่วคราว กรุณาเลือกรายการอื่น"}])
            return

        cart = CARTS.setdefault(user_id, {})
        current_qty = cart.get(code, 0)
        if current_qty + qty > product.stock:
            _reply_message(
                reply_token,
                [{"type": "text", "text": f"สต๊อกไม่พอสำหรับ {product.name}\nคงเหลือ {product.stock} ชิ้น"}],
            )
            return

        cart[code] = current_qty + qty

        summary, _ = _cart_summary(cart)
        delivery_hint = (
            "\n\nวิธีรับสินค้า (ตั้งค่าได้เลย):\n"
            "- รับหน้าร้าน"
        )
        _reply_message(
            reply_token,
            [{"type": "text", "text": f"เพิ่ม {product.name} x{qty} ลงตะกร้าแล้ว\n\n{summary}{delivery_hint}"}],
        )
        return

    if text.startswith("verify") or lower_text.startswith("verify"):
        is_open, _ = _shop_open_state()
        if not is_open:
            _reply_message(reply_token, [{"type": "text", "text": _shop_closed_reply_text()}])
            return

        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            _reply_message(reply_token, [{"type": "text", "text": "รูปแบบ: verify <QR_PAYLOAD>\nเช่น: verify 00020101021230..."}])
            return

        qr_payload = parts[1].strip()
        cart = CARTS.get(user_id, {})
        _, total = _cart_summary(cart)

        if total <= 0:
            _reply_message(reply_token, [{"type": "text", "text": "ตะกร้าว่างอยู่ ไม่มีอะไรให้ verificate"}])
            return

        result = _verify_slip_with_payload(qr_payload, total)
        if result["success"]:
            # สร้างออเดอร์เข้าคิวครัว (PENDING)
            new_order = Order(
                user_id=user_id,
                total_amount=total,
                status='PENDING',
                delivery_method="PICKUP",
                delivery_note="รับหน้าร้าน",
            )
            db.session.add(new_order)
            db.session.flush() # เพื่อให้ได้ new_order.id
            
            for code, qty in cart.items():
                p = Product.query.filter_by(code=code).first()
                if p:
                    item_ordered = OrderItem(order_id=new_order.id, product_id=p.id, quantity=qty, price=p.price)
                    db.session.add(item_ordered)
            
            db.session.commit()
            
            # ล้างตะกร้า
            CARTS[user_id] = {}

            msg = f"""✅ ยืนยันการชำระเงินสำเร็จ
ออเดอร์ที่: #{new_order.id}
ยอด: {result['amount']:.2f} บาท
อ้างอิง: {result['trans_ref']}
วิธีรับสินค้า: {_delivery_text(new_order.delivery_method, new_order.delivery_note or '')}

ร้านค้าได้รับออเดอร์ของคุณแล้ว และส่งเข้าครัวเรียบร้อย กรุณารอสักครู่ค่ะ"""
            _reply_message(reply_token, [{"type": "text", "text": msg}])
        else:
            error_msg = result.get("error", "เกิดข้อผิดพลาดในการตรวจสลิป")
            _reply_message(reply_token, [{"type": "text", "text": f"❌ {error_msg}"}])
        return

    _reply_message(reply_token, [{"type": "text", "text": "พิมพ์ 'เมนู' เพื่อดูรายการอาหาร หรือ 'ชำระเงิน' เพื่อสร้าง QR พร้อมเพย์"}])


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)
