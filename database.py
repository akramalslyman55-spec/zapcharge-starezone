import os, calendar
from datetime import datetime, timedelta
from sqlalchemy import (create_engine, Column, Integer, BigInteger, Float, String, Text,
                        Boolean, DateTime, JSON, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

url = os.environ.get("DATABASE_URL", "sqlite:///local.db").split("?")[0]
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+pg8000://", 1)

engine = create_engine(url, pool_pre_ping=True)
Session = scoped_session(sessionmaker(bind=engine))
Base = declarative_base()

STATUS = {
    "pending": ("قيد الانتظار", "amber"),
    "done": ("منفّذ", "emerald"),
    "approved": ("منفّذ", "emerald"),
    "rejected": ("مرفوض", "rose"),
}


def ts(t):
    return calendar.timegm(t.utctimetuple()) if t else None


def fmt(t):
    return (t + timedelta(hours=3)).strftime("%d/%m %H:%M") if t else ""


def migrate():
    """ينشئ الجداول الناقصة ويضيف الأعمدة الجديدة فقط، ولا يحذف أي بيانات أبداً."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as c:
        for t in Base.metadata.sorted_tables:
            have = {col["name"] for col in insp.get_columns(t.name)}
            for col in t.columns:
                if col.name not in have:
                    c.execute(text(f'ALTER TABLE {t.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'))


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(50), primary_key=True)
    value = Column(String(200))


class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)
    name = Column(String(100), default="زبون")
    username = Column(String(100))
    balance = Column(Float, default=0)  # بالدولار
    created = Column(DateTime, default=datetime.utcnow)


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True)
    title = Column(String(200))
    brand = Column(String(20), default="starzone")
    category = Column(String(20), default="games")
    icon = Column(String(20), default="shield")
    badge = Column(String(50), default="")
    description = Column(Text, default="")
    input_label = Column(String(200), default="")
    input_placeholder = Column(String(100), default="")
    currency = Column(String(3), default="USD")
    image = Column(Text, default="")
    packages = Column(JSON, default=list)
    active = Column(Boolean, default=True)
    created = Column(DateTime, default=datetime.utcnow)

    def to_dict(self, rate=135.0, raw=False):
        cur = self.currency or "USD"
        pk = self.packages or []
        if not raw:
            pk = [{"name": p["name"], "price": round(p["price"] / (1 if cur == "USD" else rate), 6)} for p in pk]
        return {
            "id": str(self.id), "title": self.title, "brand": self.brand,
            "category": self.category, "icon": self.icon, "badge": self.badge,
            "accent": "blue" if self.brand == "starzone" else "gold",
            "description": self.description, "inputLabel": self.input_label,
            "inputPlaceholder": self.input_placeholder, "currency": cur,
            "image": self.image or "", "packages": pk, "active": bool(self.active),
        }


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    customer = Column(String(100))
    service_title = Column(String(200))
    pkg_name = Column(String(200))
    target = Column(String(200))
    price = Column(Float)  # بالدولار
    status = Column(String(20), default="pending")
    created = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime)

    def to_dict(self):
        st, color = STATUS[self.status]
        return {
            "id": f"ORD-{self.id}", "serviceTitle": self.service_title,
            "pkgName": self.pkg_name, "target": self.target, "price": self.price,
            "time": fmt(self.created), "status": st, "statusColor": color,
            "customer": self.customer, "ts": ts(self.created), "doneTs": ts(self.done_at),
        }


class PayMethod(Base):
    __tablename__ = "pay_methods"
    id = Column(Integer, primary_key=True)
    name = Column(String(100))
    details = Column(Text, default="")
    currency = Column(String(3), default="SYP")
    rate = Column(Float, default=0)  # ليرة لكل دولار، 0 = استخدم السعر العام
    proof_mode = Column(String(10), default="none")  # none | optional | required
    image = Column(Text, default="")
    fields = Column(JSON, default=list)
    active = Column(Boolean, default=True)

    def to_dict(self):
        return {"id": self.id, "name": self.name, "details": self.details,
                "currency": self.currency, "rate": self.rate or 0,
                "proof_mode": self.proof_mode or "none", "image": self.image or "",
                "fields": self.fields or [], "active": bool(self.active)}


class Deposit(Base):
    __tablename__ = "deposits"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    customer = Column(String(100))
    method = Column(String(100))
    currency = Column(String(3), default="SYP")
    amount = Column(Float)  # بعملة الطريقة
    usd = Column(Float)     # المبلغ المضاف للرصيد
    ref = Column(String(200))
    fields = Column(JSON, default=dict)
    proof = Column(Text, default="")
    status = Column(String(20), default="pending")
    created = Column(DateTime, default=datetime.utcnow)
    done_at = Column(DateTime)

    def to_dict(self):
        st, color = STATUS[self.status]
        return {
            "id": self.id, "method": self.method, "currency": self.currency or "SYP",
            "amount": self.amount, "usd": self.usd, "ref": self.ref,
            "fields": self.fields or {}, "hasProof": bool(self.proof),
            "time": fmt(self.created), "status": st, "statusColor": color,
            "customer": self.customer, "userId": self.user_id,
            "ts": ts(self.created), "doneTs": ts(self.done_at),
        }


class Ledger(Base):
    __tablename__ = "ledger"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, index=True)
    usd = Column(Float)
    note = Column(String(200), default="")
    admin_id = Column(BigInteger)
    created = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"usd": self.usd, "note": self.note, "time": fmt(self.created)}
