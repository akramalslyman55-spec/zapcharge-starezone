import os, hmac, hashlib, json, time
from datetime import datetime
from functools import wraps
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, send_file, g
from sqlalchemy import func, or_
from database import (migrate, Session, Setting, User, Service, Order, PayMethod,
                      Deposit, Ledger, ts)

app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DEV = os.environ.get("DEV_MODE") == "1"
migrate()


@app.teardown_appcontext
def _cleanup(exc):
    Session.remove()


def notify(chat_id, text):
    if not BOT_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception:
        pass


def notify_admins(text):
    for a in ADMIN_IDS:
        notify(a, text)


def get_rate():
    s = Session.get(Setting, "rate")
    try:
        return float(s.value) if s else 135.0
    except Exception:
        return 135.0


def to_usd(cur, amount, rate):
    return round(amount if cur == "USD" else amount / rate, 6)


def num(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def tg_user():
    data = request.headers.get("X-Init-Data", "")
    if data and BOT_TOKEN:
        d = dict(parse_qsl(data, keep_blank_values=True))
        h = d.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h)
        if ok and time.time() - int(d.get("auth_date", 0)) < 86400:
            u = json.loads(d["user"])
            return u["id"], (u.get("first_name") or u.get("username") or "زبون"), u.get("username")
    if DEV:
        return 1, "مطوّر", "dev"
    return None, None, None


def guard(admin=False):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            uid, name, uname = tg_user()
            if not uid:
                return jsonify(error="افتح المتجر من داخل تيليغرام"), 401
            g.admin = uid in ADMIN_IDS or (DEV and uid == 1)
            if admin and not g.admin:
                return jsonify(error="للإدارة فقط"), 403
            u = Session.get(User, uid)
            if not u:
                u = User(id=uid, balance=0)
                Session.add(u)
            u.name, u.username = name, uname
            Session.commit()
            g.user = u
            return f(*a, **k)
        return w
    return deco


def err(msg, code=400):
    return jsonify(error=msg), code


def locked_user(uid):
    return Session.query(User).filter_by(id=uid).with_for_update().one()


@app.get("/")
@app.get("/admin")
def home():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


# ---------------- Customer ----------------
@app.get("/api/me")
@guard()
def me():
    return jsonify(id=g.user.id, name=g.user.name, username=g.user.username,
                   balance=g.user.balance, is_admin=g.admin, rate=get_rate(), now=int(time.time()))


@app.get("/api/services")
@guard()
def services():
    r = get_rate()
    rows = Session.query(Service).filter_by(active=True).order_by(Service.id.desc()).all()
    return jsonify([s.to_dict(r) for s in rows])


@app.get("/api/methods")
@guard()
def methods():
    rows = Session.query(PayMethod).filter_by(active=True).order_by(PayMethod.id).all()
    return jsonify([m.to_dict() for m in rows])


@app.get("/api/orders")
@guard()
def my_orders():
    rows = Session.query(Order).filter_by(user_id=g.user.id).order_by(Order.id.desc()).limit(50).all()
    return jsonify([o.to_dict() for o in rows])


@app.post("/api/order")
@guard()
def make_order():
    j = request.get_json(silent=True) or {}
    s = Session.get(Service, int(j.get("service_id") or 0))
    target = str(j.get("target", "")).strip()[:150]
    if not s or not s.active:
        return err("الخدمة غير متاحة")
    try:
        pkg = (s.packages or [])[int(j.get("pkg_index", 0))]
    except Exception:
        return err("الباقة غير صحيحة")
    if not target:
        return err("أدخل المعرّف المطلوب")
    price = to_usd(s.currency or "USD", pkg["price"], get_rate())
    u = locked_user(g.user.id)
    if u.balance + 1e-9 < price:
        return err("رصيدك غير كافٍ، اشحن محفظتك أولاً")
    u.balance = round(u.balance - price, 6)
    o = Order(user_id=u.id, customer=u.name, service_title=s.title,
              pkg_name=pkg["name"], target=target, price=price)
    Session.add(o)
    Session.commit()
    notify_admins(f"طلب جديد #{o.id}\n{s.title} - {pkg['name']}\nالمعرّف: {target}\nالسعر: {price:.2f}$\nالزبون: {u.name}")
    return jsonify(balance=u.balance)


@app.get("/api/deposits")
@guard()
def my_deposits():
    rows = Session.query(Deposit).filter_by(user_id=g.user.id).order_by(Deposit.id.desc()).limit(20).all()
    return jsonify([d.to_dict() for d in rows])


@app.post("/api/deposit")
@guard()
def make_deposit():
    j = request.get_json(silent=True) or {}
    m = Session.get(PayMethod, int(j.get("method_id") or 0))
    if not m or not m.active:
        return err("طريقة الدفع غير متاحة")
    amount = num(j.get("amount"))
    ref = str(j.get("ref", "")).strip()[:150]
    if amount <= 0 or not ref:
        return err("أدخل المبلغ ورقم العملية")
    vals = {}
    for label in (m.fields or []):
        v = str((j.get("fields") or {}).get(label, "")).strip()[:200]
        if not v:
            return err(f"الحقل مطلوب: {label}")
        vals[label] = v
    proof = j.get("proof") or ""
    if m.proof_mode == "required" and not proof:
        return err("أرفق صورة الإيصال")
    if proof and (not proof.startswith("data:image/") or len(proof) > 1_500_000):
        return err("الصورة غير صالحة أو كبيرة جداً")
    rate = m.rate or get_rate()
    d = Deposit(user_id=g.user.id, customer=g.user.name, method=m.name, currency=m.currency,
                amount=amount, usd=to_usd(m.currency, amount, rate), ref=ref, fields=vals, proof=proof)
    Session.add(d)
    Session.commit()
    notify_admins(f"طلب إيداع جديد #{d.id}\n{m.name} - {amount:g} {m.currency}\nرقم العملية: {ref}\nالزبون: {g.user.name}")
    return jsonify(id=d.id, ts=ts(d.created))


# ---------------- Admin ----------------
@app.get("/api/admin/data")
@guard(admin=True)
def admin_data():
    r = get_rate()
    deps = Session.query(Deposit).order_by(Deposit.id.desc()).limit(100).all()
    deps.sort(key=lambda d: d.status != "pending")
    return jsonify(
        rate=r, now=int(time.time()),
        services=[s.to_dict(r, raw=True) for s in Session.query(Service).order_by(Service.id.desc())],
        orders=[o.to_dict() for o in Session.query(Order).order_by(Order.id.desc()).limit(100)],
        deposits=[d.to_dict() for d in deps],
        methods=[m.to_dict() for m in Session.query(PayMethod).order_by(PayMethod.id)],
    )


@app.post("/api/admin/rate")
@guard(admin=True)
def set_rate():
    v = num((request.get_json(silent=True) or {}).get("rate"))
    if v <= 0:
        return err("سعر الصرف غير صحيح")
    s = Session.get(Setting, "rate") or Setting(key="rate")
    s.value = str(v)
    Session.add(s)
    Session.commit()
    return jsonify(ok=True)


@app.post("/api/admin/service")
@guard(admin=True)
def svc_save():
    j = request.get_json(silent=True) or {}
    pk = []
    for p in j.get("packages", []):
        name, price = str(p.get("name", "")).strip(), num(p.get("price"))
        if name and price > 0:
            pk.append({"name": name, "price": price})
    if not str(j.get("title", "")).strip() or not pk:
        return err("الاسم وباقة واحدة على الأقل مطلوبان")
    img = j.get("image") or ""
    if img and (not img.startswith("data:image/") or len(img) > 600_000):
        return err("الصورة غير صالحة أو كبيرة")
    s = Session.get(Service, int(j["id"])) if j.get("id") else None
    if not s:
        s = Service(active=True)
        Session.add(s)
    s.title = j["title"].strip()
    s.brand = j.get("brand", "starzone")
    s.category = j.get("category", "games")
    s.icon = j.get("icon", "shield")
    s.badge = j.get("badge", "")
    s.description = j.get("description", "")
    s.input_label = j.get("inputLabel", "")
    s.input_placeholder = j.get("inputPlaceholder", "")
    s.currency = "SYP" if j.get("currency") == "SYP" else "USD"
    s.image = img
    s.packages = pk
    Session.commit()
    return jsonify(ok=True)


@app.post("/api/admin/service/<int:sid>/toggle")
@guard(admin=True)
def svc_toggle(sid):
    s = Session.get(Service, sid)
    if not s:
        return err("غير موجودة", 404)
    s.active = not s.active
    Session.commit()
    return jsonify(ok=True)


@app.delete("/api/admin/service/<int:sid>")
@guard(admin=True)
def svc_delete(sid):
    s = Session.get(Service, sid)
    if s:
        Session.delete(s)
        Session.commit()
    return jsonify(ok=True)


@app.post("/api/admin/method")
@guard(admin=True)
def method_save():
    j = request.get_json(silent=True) or {}
    name = str(j.get("name", "")).strip()
    if not name:
        return err("اسم الطريقة مطلوب")
    m = Session.get(PayMethod, int(j["id"])) if j.get("id") else None
    if not m:
        m = PayMethod(active=True)
        Session.add(m)
    m.name, m.details = name[:100], str(j.get("details", ""))[:1000]
    m.currency = "USD" if j.get("currency") == "USD" else "SYP"
    m.rate = num(j.get("rate"))
    m.proof_mode = j.get("proof_mode") if j.get("proof_mode") in ("none", "optional", "required") else "none"
    img = j.get("image") or ""
    if img and (not img.startswith("data:image/") or len(img) > 600_000):
        return err("الصورة غير صالحة أو كبيرة")
    m.image = img
    m.fields = [str(x).strip()[:60] for x in (j.get("fields") or []) if str(x).strip()][:8]
    Session.commit()
    return jsonify(ok=True)


@app.post("/api/admin/method/<int:mid>/toggle")
@guard(admin=True)
def method_toggle(mid):
    m = Session.get(PayMethod, mid)
    if m:
        m.active = not m.active
        Session.commit()
    return jsonify(ok=True)


@app.delete("/api/admin/method/<int:mid>")
@guard(admin=True)
def method_delete(mid):
    m = Session.get(PayMethod, mid)
    if m:
        Session.delete(m)
        Session.commit()
    return jsonify(ok=True)


@app.post("/api/admin/order/<int:oid>")
@guard(admin=True)
def order_action(oid):
    act = (request.get_json(silent=True) or {}).get("action")
    o = Session.get(Order, oid)
    if not o or o.status != "pending":
        return err("الطلب غير قابل للتعديل")
    if act == "done":
        o.status = "done"
        notify(o.user_id, f"تم تنفيذ طلبك #{o.id}: {o.service_title} - {o.pkg_name}")
    elif act == "reject":
        o.status = "rejected"
        u = locked_user(o.user_id)
        u.balance = round(u.balance + o.price, 6)
        notify(o.user_id, f"تم رفض طلبك #{o.id} وإرجاع {o.price:.2f}$ إلى رصيدك")
    else:
        return err("إجراء غير معروف")
    o.done_at = datetime.utcnow()
    Session.commit()
    return jsonify(ok=True)


@app.get("/api/admin/deposit/<int:did>/proof")
@guard(admin=True)
def deposit_proof(did):
    d = Session.get(Deposit, did)
    return jsonify(image=(d.proof if d else "") or "")


@app.post("/api/admin/deposit/<int:did>")
@guard(admin=True)
def deposit_action(did):
    act = (request.get_json(silent=True) or {}).get("action")
    d = Session.get(Deposit, did)
    if not d or d.status != "pending":
        return err("الطلب غير قابل للتعديل")
    if act == "approve":
        d.status = "approved"
        u = locked_user(d.user_id)
        u.balance = round(u.balance + d.usd, 6)
        notify(d.user_id, f"تمت الموافقة على إيداعك وإضافة {d.usd:.2f}$ إلى رصيدك")
    elif act == "reject":
        d.status = "rejected"
        notify(d.user_id, f"تم رفض طلب الإيداع #{d.id}، تواصل مع الدعم للاستفسار")
    else:
        return err("إجراء غير معروف")
    d.done_at = datetime.utcnow()
    Session.commit()
    return jsonify(ok=True)


def user_dict(u):
    return {"id": u.id, "name": u.name, "username": u.username, "balance": u.balance, "created": ts(u.created)}


@app.get("/api/admin/users")
@guard(admin=True)
def users_list():
    q = request.args.get("q", "").strip().lstrip("@")
    qs = Session.query(User)
    if q:
        conds = [User.name.ilike(f"%{q}%"), User.username.ilike(f"%{q}%")]
        if q.isdigit():
            conds.append(User.id == int(q))
        qs = qs.filter(or_(*conds))
    return jsonify([user_dict(u) for u in qs.order_by(User.created.desc()).limit(50)])


@app.get("/api/admin/user/<int:uid>")
@guard(admin=True)
def user_profile(uid):
    u = Session.get(User, uid)
    if not u:
        return err("الزبون غير موجود", 404)
    cnt, spent = Session.query(func.count(Order.id), func.coalesce(func.sum(Order.price), 0)).filter(
        Order.user_id == uid, Order.status != "rejected").one()
    dep = Session.query(func.coalesce(func.sum(Deposit.usd), 0)).filter(
        Deposit.user_id == uid, Deposit.status == "approved").scalar()
    return jsonify(
        user=user_dict(u), stats={"orders": cnt, "spent": float(spent), "deposits": float(dep)},
        orders=[o.to_dict() for o in Session.query(Order).filter_by(user_id=uid).order_by(Order.id.desc()).limit(10)],
        ledger=[x.to_dict() for x in Session.query(Ledger).filter_by(user_id=uid).order_by(Ledger.id.desc()).limit(10)],
    )


@app.post("/api/admin/user/<int:uid>/balance")
@guard(admin=True)
def user_balance(uid):
    j = request.get_json(silent=True) or {}
    amount = num(j.get("amount"))
    if amount <= 0:
        return err("أدخل مبلغاً صحيحاً")
    usd = to_usd("SYP" if j.get("currency") == "SYP" else "USD", amount, get_rate())
    if not Session.get(User, uid):
        return err("الزبون غير موجود", 404)
    u = locked_user(uid)
    if j.get("action") == "sub":
        if u.balance + 1e-9 < usd:
            return err("رصيد الزبون أقل من المبلغ المطلوب سحبه")
        usd = -usd
    u.balance = round(u.balance + usd, 6)
    Session.add(Ledger(user_id=uid, usd=usd, note=str(j.get("note", ""))[:200], admin_id=g.user.id))
    Session.commit()
    notify(uid, f"تم {'إضافة' if usd > 0 else 'سحب'} {abs(usd):.2f}$ {'إلى' if usd > 0 else 'من'} رصيدك بواسطة الإدارة")
    return jsonify(balance=u.balance)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=DEV)
