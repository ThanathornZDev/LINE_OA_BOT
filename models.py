from extensions import db
from datetime import datetime
import pytz

def get_bkk_time():
    return datetime.now(pytz.timezone('Asia/Bangkok'))

class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255), nullable=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='ทั่วไป')
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    image_url = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(100), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='PENDING') # PENDING, PAID, CANCELLED
    delivery_method = db.Column(db.String(20), nullable=False, default='PICKUP') # PICKUP, DELIVERY
    delivery_note = db.Column(db.String(255), nullable=True)
    slip_image_url = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=get_bkk_time)
    items = db.relationship('OrderItem', backref='order', lazy=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    product = db.relationship('Product')


class InventoryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    movement_type = db.Column(db.String(10), nullable=False)  # IN, OUT, SALE
    quantity = db.Column(db.Integer, nullable=False)
    order_id = db.Column(db.Integer, nullable=True)
    customer_user_id = db.Column(db.String(100), nullable=True)
    customer_name = db.Column(db.String(100), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    occurred_at = db.Column(db.DateTime, default=get_bkk_time, nullable=False)
    product = db.relationship('Product')
