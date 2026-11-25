"""Supplier A: a small, server-rendered office-supply storefront."""

from __future__ import annotations

from html import escape
import re
import secrets
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from shared.config import SUPPLIER_A_PORT

app = FastAPI(title="Northstar Office Supply - Supplier A")

PRODUCTS: dict[str, dict[str, Any]] = {
    "paper": {"name": "Printer Paper", "description": "Premium A4 80 GSM paper, 500 sheets per ream.", "price": 600, "stock": 40},
    "pens": {"name": "Office Pens", "description": "Smooth blue ink ballpoint pens, pack of 10.", "price": 120, "stock": 60},
    "stapler": {"name": "Stapler", "description": "Durable desktop stapler with a staple remover grip.", "price": 180, "stock": 25},
    "notebook": {"name": "Notebook", "description": "Hard-wearing ruled notebook with 200 pages.", "price": 90, "stock": 50},
    "folder": {"name": "File Folder", "description": "Expandable A4 document folder for everyday filing.", "price": 75, "stock": 80},
}

# Opaque, high-entropy browser session IDs keep login and cart state isolated.
SESSIONS: dict[str, dict[str, Any]] = {}
COOKIE_NAME = "supplier_a_session"

STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f8fc;color:#172b4d;font:15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:#1769aa;text-decoration:none}a:hover{text-decoration:underline}.topbar{background:#fff;border-bottom:1px solid #e4ebf3}.nav{max-width:1120px;margin:auto;padding:17px 24px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{font-size:20px;font-weight:750;color:#143a63;letter-spacing:-.4px}.brand span{color:#1976c9}.navlinks{display:flex;align-items:center;gap:24px;font-weight:600}.navlinks a{color:#405875}.container{max-width:1120px;margin:36px auto;padding:0 24px}.hero{padding:30px 34px;background:linear-gradient(115deg,#e7f3ff,#f8fbff);border:1px solid #d9eaf9;border-radius:16px;margin-bottom:28px}.eyebrow{color:#1976c9;font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:1.2px}.hero h1{font-size:32px;line-height:1.2;margin:7px 0}.muted{color:#61758d}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:24px 0}.search{display:flex;gap:10px;flex:1;max-width:620px}.field{width:100%;padding:12px 14px;border:1px solid #cbd7e4;border-radius:8px;background:#fff;font:inherit;color:#172b4d}.field:focus{outline:3px solid #c8e4fb;border-color:#2584cc}label{display:block;font-weight:650;margin:13px 0 6px}.btn{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:8px;padding:11px 17px;background:#176fba;color:#fff;font:inherit;font-weight:700;cursor:pointer}.btn:hover{background:#105c9d;text-decoration:none}.btn.secondary{background:#e8f2fb;color:#14598e}.btn.danger{background:#fff0ef;color:#b43b32}.btn.small{padding:7px 11px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.card,.panel{background:#fff;border:1px solid #e1e8f0;border-radius:13px;box-shadow:0 4px 14px #1c3d5a0a}.card{padding:20px;display:flex;flex-direction:column;min-height:230px}.product-icon{width:45px;height:45px;border-radius:12px;background:#eaf4fc;color:#1874b8;display:grid;place-items:center;font-size:21px;margin-bottom:14px}.card h2{font-size:18px;margin:0 0 4px}.card p{margin:4px 0;color:#61758d}.card .bottom{margin-top:auto;padding-top:16px;display:flex;align-items:center;justify-content:space-between;gap:10px}.price{font-size:20px;font-weight:750;color:#153f66}.stock{font-size:12px;color:#6a7d91}.panel{padding:24px}.narrow{max-width:520px;margin:56px auto}.narrow h1{margin-top:0}.alert{padding:12px 15px;border-radius:8px;background:#fff0ef;color:#9c302a;border:1px solid #f5cfcc;margin:15px 0}.success{background:#eaf8f0;border-color:#c7e9d4;color:#236740}.empty{text-align:center;padding:48px 20px;border:1px dashed #cbd7e4;border-radius:12px;background:#fff}.empty h2{margin:0 0 5px}.cart-row{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:12px;align-items:center;padding:16px 0;border-bottom:1px solid #e8edf2}.qty{display:flex;align-items:center;gap:8px}.qty form{display:inline}.subtotal{font-size:21px;font-weight:750;text-align:right;padding:20px 0}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}.form-grid .wide{grid-column:1/-1}.footer{padding:30px 24px;text-align:center;color:#7b8ba0;font-size:13px}@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.cart-row{grid-template-columns:1fr 1fr}.nav{padding:14px 18px}.container{padding:0 18px}.hero{padding:24px}.toolbar{align-items:stretch;flex-direction:column}}@media(max-width:480px){.grid{grid-template-columns:1fr}.navlinks{gap:12px;font-size:13px}.brand{font-size:17px}.form-grid{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}}
"""


def _page(title: str, body: str, *, logged_in: bool = False) -> HTMLResponse:
    nav = (
        '<a href="/">Shop</a><a href="/cart" id="cart-link">Cart</a>'
        '<a href="/logout">Sign out</a>' if logged_in else '<a href="/login">Sign in</a>'
    )
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)} · Northstar Office Supply</title><style>{STYLE}</style></head><body>'
        f'<header class="topbar"><nav class="nav"><a class="brand" href="/">NORTHSTAR <span>OFFICE</span></a>'
        f'<div class="navlinks">{nav}</div></nav></header><main class="container">{body}</main>'
        '<footer class="footer">Northstar Office Supply · Reliable essentials for your workplace</footer></body></html>'
    )


def _session(request: Request, *, create: bool = False) -> tuple[str | None, dict[str, Any] | None]:
    sid = request.cookies.get(COOKIE_NAME)
    data = SESSIONS.get(sid) if sid else None
    if data is None and create:
        sid = secrets.token_urlsafe(32)
        data = {"authenticated": False, "cart": {}}
        SESSIONS[sid] = data
    return sid, data


def _cookie(response: Response, sid: str) -> Response:
    response.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", secure=False, max_age=86400)
    return response


def _require_login(request: Request) -> tuple[str, dict[str, Any]] | RedirectResponse:
    sid, data = _session(request)
    if not sid or not data or not data["authenticated"]:
        return RedirectResponse("/login", status_code=303)
    return sid, data


def _money(value: int) -> str:
    return f"₹{value:,}"


def _cart_lines(cart: dict[str, int]) -> tuple[str, int]:
    lines: list[str] = []
    subtotal = 0
    for pid, qty in cart.items():
        product = PRODUCTS.get(pid)
        if not product or qty < 1 or qty > product["stock"]:
            continue
        line_total = product["price"] * qty
        subtotal += line_total
        lines.append(
            f'<div class="cart-row"><div><strong>{escape(product["name"])}</strong>'
            f'<div class="muted">{_money(product["price"])} each</div></div>'
            f'<div>Unit price <strong>{_money(product["price"])}</strong></div>'
            f'<div class="qty"><form method="post" action="/cart/quantity/{pid}"><input type="hidden" name="change" value="-1">'
            f'<button class="btn secondary small" id="decrease-{pid}" aria-label="Decrease {escape(product["name"])} quantity">−</button></form>'
            f'<strong>{qty}</strong><form method="post" action="/cart/quantity/{pid}"><input type="hidden" name="change" value="1">'
            f'<button class="btn secondary small" id="increase-{pid}" aria-label="Increase {escape(product["name"])} quantity">+</button></form></div>'
            f'<strong>{_money(line_total)}</strong><form method="post" action="/cart/remove/{pid}">'
            f'<button class="btn danger small">Remove</button></form></div>'
        )
    return "".join(lines), subtotal


@app.get("/", response_class=HTMLResponse)
async def shop(request: Request, q: str = "") -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    normalized = q.strip().casefold()
    matches = [(pid, p) for pid, p in PRODUCTS.items() if normalized in p["name"].casefold()]
    cards = "".join(
        f'<article class="card" id="product-{pid}"><div class="product-icon" aria-hidden="true">{icon}</div>'
        f'<h2>{escape(p["name"])}</h2><p>{escape(p["description"])}</p><p class="stock">{p["stock"]} in stock</p>'
        f'<div class="bottom"><span class="price">{_money(p["price"])}</span>'
        f'<form method="post" action="/cart/add/{pid}"><button class="btn" id="add-to-cart-{pid}">Add to Cart</button></form></div></article>'
        for (pid, p), icon in zip(matches, ("▤", "✎", "▣", "▧", "▤"))
    )
    if not cards:
        cards = '<section class="empty" id="empty-state"><h2>No products found</h2><p class="muted">Try another product name or clear your search.</p><a class="btn secondary" href="/">Clear search</a></section>'
    body = (
        '<section class="hero"><div class="eyebrow">Workplace essentials, delivered</div>'
        '<h1>Everything your office needs.</h1><p class="muted">Thoughtful essentials, dependable quality, and easy ordering.</p></section>'
        '<div class="toolbar"><form class="search" method="get" action="/">'
        f'<label for="product-search" style="position:absolute;left:-10000px">Search products</label><input class="field" id="product-search" name="q" type="search" placeholder="Search office supplies" value="{escape(q, quote=True)}">'
        '<button class="btn">Search</button></form><span class="muted">5 curated essentials</span></div>'
        f'<section class="grid" id="product-list" aria-label="Products">{cards}</section>'
    )
    return _page("Shop office supplies", body, logged_in=True)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> Response:
    sid, _ = _session(request, create=True)
    msg = '<div class="alert" role="alert" id="login-error">Invalid username or password. Please try again.</div>' if error else ""
    body = (
        '<section class="panel narrow"><div class="eyebrow">Supplier A · Secure account</div><h1>Welcome back</h1>'
        '<p class="muted">Sign in to browse and order office essentials.</p>' + msg +
        '<form method="post" action="/login"><label for="username">Username</label><input class="field" id="username" name="username" autocomplete="username" required>'
        '<label for="password">Password</label><input class="field" id="password" name="password" type="password" autocomplete="current-password" required>'
        '<button class="btn" id="login-button" type="submit" style="width:100%;margin-top:20px">Sign in</button></form>'
        '<p class="muted" style="font-size:13px">Demo access: demo / demo</p></section>'
    )
    response = _page("Sign in", body)
    return _cookie(response, sid) if sid else response


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)) -> Response:
    sid, data = _session(request, create=True)
    if username.strip() != "demo" or password != "demo":
        response = await login_page(request, error="1")
        response.status_code = 401
        return _cookie(response, sid) if sid else response
    assert data is not None and sid is not None
    data["authenticated"] = True
    response = RedirectResponse("/", status_code=303)
    return _cookie(response, sid)


@app.get("/logout")
async def logout(request: Request) -> Response:
    sid, _ = _session(request)
    if sid:
        SESSIONS.pop(sid, None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/cart/add/{product_id}")
async def add_to_cart(request: Request, product_id: str) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    product = PRODUCTS.get(product_id)
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    current = data["cart"].get(product_id, 0)
    if current >= product["stock"]:
        return HTMLResponse("Requested quantity exceeds available stock.", status_code=409)
    data["cart"][product_id] = current + 1
    return RedirectResponse("/cart", status_code=303)


@app.get("/cart", response_class=HTMLResponse)
async def cart(request: Request) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    lines, subtotal = _cart_lines(data["cart"])
    if not lines:
        body = '<section class="empty" id="empty-cart"><h1>Your cart is ready for a refresh</h1><p class="muted">Add a few office essentials to get started.</p><a class="btn" href="/">Browse products</a></section>'
    else:
        body = (
            '<h1 id="cart">Your cart</h1><p class="muted">Review quantities and pricing before checkout.</p>'
            f'<section class="panel">{lines}<div class="subtotal">Subtotal: {_money(subtotal)}</div>'
            '<div style="display:flex;justify-content:flex-end;gap:12px"><a class="btn secondary" href="/">Continue shopping</a>'
            '<a class="btn" href="/checkout">Continue to checkout</a></div></section>'
        )
    return _page("Your cart", body, logged_in=True)


@app.post("/cart/quantity/{product_id}")
async def update_quantity(request: Request, product_id: str, change: int = Form(...)) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    if product_id not in PRODUCTS or change not in (-1, 1):
        return HTMLResponse("Invalid cart update.", status_code=422)
    qty = data["cart"].get(product_id, 0) + change
    if qty <= 0:
        data["cart"].pop(product_id, None)
    elif qty <= PRODUCTS[product_id]["stock"]:
        data["cart"][product_id] = qty
    else:
        return HTMLResponse("Requested quantity exceeds available stock.", status_code=409)
    return RedirectResponse("/cart", status_code=303)


@app.post("/cart/remove/{product_id}")
async def remove_from_cart(request: Request, product_id: str) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    data["cart"].pop(product_id, None)
    return RedirectResponse("/cart", status_code=303)


@app.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, error: str = "") -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    _, subtotal = _cart_lines(data["cart"])
    if subtotal <= 0:
        return RedirectResponse("/cart", status_code=303)
    alert = '<div class="alert" role="alert">Please correct the highlighted checkout information.</div>' if error else ""
    body = (
        '<div class="eyebrow">Almost there</div><h1>Shipping details</h1><p class="muted">Where should we send your order?</p>' + alert +
        f'<section class="panel"><form id="checkout-form" method="post" action="/checkout"><div class="form-grid">'
        '<div class="wide"><label for="full-name">Full name</label><input class="field" id="full-name" name="full_name" autocomplete="name" required minlength="2"></div>'
        '<div><label for="phone">Phone number</label><input class="field" id="phone" name="phone" type="tel" autocomplete="tel" required pattern="[0-9+() -]{10,16}"></div>'
        '<div><label for="pincode">PIN code</label><input class="field" id="pincode" name="pincode" inputmode="numeric" autocomplete="postal-code" required pattern="[0-9]{6}"></div>'
        '<div class="wide"><label for="address">Street address</label><input class="field" id="address" name="address" autocomplete="street-address" required minlength="5"></div>'
        '<div><label for="city">City</label><input class="field" id="city" name="city" autocomplete="address-level2" required></div>'
        '<div><label for="state">State</label><input class="field" id="state" name="state" autocomplete="address-level1" required></div>'
        '</div><div class="subtotal">Order total: {_money(subtotal)}</div><button class="btn" id="pay-now-button" type="submit">Pay Now</button>'
        '<p class="muted" style="font-size:13px">Mock payment only. No payment details are collected.</p></form></section>'
    )
    return _page("Checkout", body, logged_in=True)


@app.post("/checkout", response_class=HTMLResponse)
async def checkout(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(...),
    address: str = Form(...),
    city: str = Form(...),
    state: str = Form(...),
    pincode: str = Form(...),
) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    valid = (
        2 <= len(full_name.strip()) <= 100
        and bool(re.fullmatch(r"[0-9+() -]{10,16}", phone.strip()))
        and 5 <= len(address.strip()) <= 200
        and 2 <= len(city.strip()) <= 80
        and 2 <= len(state.strip()) <= 80
        and bool(re.fullmatch(r"[0-9]{6}", pincode.strip()))
    )
    lines, total = _cart_lines(data["cart"])
    valid_cart = bool(lines) and all(0 < qty <= PRODUCTS[pid]["stock"] for pid, qty in data["cart"].items())
    if not valid or not valid_cart:
        response = await checkout_page(request, error="1")
        response.status_code = 422
        return response
    order_id = "NSA-" + secrets.token_hex(4).upper()
    item_rows = "".join(
        f'<li>{escape(PRODUCTS[pid]["name"])} × {qty} — {_money(PRODUCTS[pid]["price"] * qty)}</li>'
        for pid, qty in data["cart"].items()
    )
    address_text = ", ".join((address.strip(), city.strip(), state.strip(), pincode.strip()))
    body = (
        '<section class="panel"><div class="eyebrow">Order confirmed</div><h1>Thank you for your order.</h1>'
        f'<div class="alert success">Mock payment successful. Order <strong id="order-id">{order_id}</strong> is confirmed.</div>'
        f'<h2>Order details</h2><ul>{item_rows}</ul><p><strong>Ship to:</strong> {escape(full_name.strip())}<br>{escape(address_text)}</p>'
        f'<p class="subtotal">Total paid: {_money(total)}</p><a class="btn" href="/">Continue shopping</a></section>'
    )
    data["cart"].clear()
    return _page("Order confirmed", body, logged_in=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("portals.supplier_a:app", host="0.0.0.0", port=SUPPLIER_A_PORT)
