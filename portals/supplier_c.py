"""Supplier C: a premium, server-rendered office-supply storefront."""

from __future__ import annotations

from html import escape
import os
import re
import secrets
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from shared.config import SUPPLIER_C_PORT

app = FastAPI(title="Meridian Office - Supplier C")

PRODUCTS: dict[str, dict[str, Any]] = {
    "paper": {"name": "Printer Paper", "description": "Premium A4 80 GSM paper, bright white and smooth for everyday printing.", "price": 480, "stock": 42},
    "pens": {"name": "Office Pens", "description": "Comfort-grip blue ballpoint pens, packed for the whole team.", "price": 95, "stock": 58},
    "stapler": {"name": "Stapler", "description": "Precision metal desk stapler with a dependable soft-touch handle.", "price": 155, "stock": 24},
    "notebook": {"name": "Notebook", "description": "Premium ruled notebook with a sturdy cover and 200 pages.", "price": 70, "stock": 46},
    "folder": {"name": "File Folder", "description": "Quality A4 file folder with a secure elastic closure.", "price": 60, "stock": 72},
}

SESSIONS: dict[str, dict[str, Any]] = {}
COOKIE_NAME = "supplier_c_session"

STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f9fb;color:#183044;font:15px/1.58 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:#087d8b;text-decoration:none}a:hover{text-decoration:underline}.topbar{background:#fff;border-bottom:1px solid #dce8ee}.nav{max-width:1160px;margin:auto;padding:18px 26px;display:flex;align-items:center;justify-content:space-between;gap:20px}.brand{font-size:20px;font-weight:800;color:#183f5a;letter-spacing:1px}.brand span{color:#078d93}.navlinks{display:flex;align-items:center;gap:26px;font-weight:650}.navlinks a{color:#426175}.container{max-width:1160px;margin:38px auto;padding:0 26px}.hero{position:relative;overflow:hidden;padding:37px 40px;background:linear-gradient(115deg,#e7f4f8,#f4fbfc 65%,#e7f7f5);border:1px solid #d6e9ee;border-radius:20px;margin-bottom:30px}.hero:after{content:"";position:absolute;width:230px;height:230px;border-radius:50%;right:4%;top:-110px;border:32px solid #d4eeeb;opacity:.65}.eyebrow{color:#07828d;font-weight:750;text-transform:uppercase;font-size:11px;letter-spacing:1.45px}.hero h1{position:relative;z-index:1;font-size:34px;line-height:1.18;margin:8px 0;letter-spacing:-.7px}.hero p{position:relative;z-index:1}.muted{color:#688092}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:25px 0}.search{display:flex;gap:10px;flex:1;max-width:650px}.field{width:100%;padding:12px 15px;border:1px solid #c8d9e1;border-radius:10px;background:#fff;font:inherit;color:#183044}.field:focus{outline:3px solid #ccebed;border-color:#0b9098}label{display:block;font-weight:650;margin:13px 0 6px}.btn{display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:9px;padding:11px 18px;background:#087f89;color:#fff;font:inherit;font-weight:720;cursor:pointer}.btn:hover{background:#066b74;text-decoration:none}.btn.secondary{background:#e7f3f5;color:#08727d}.btn.danger{background:#fff0ef;color:#a6413a}.btn.small{padding:7px 11px;font-size:13px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}.card,.panel{background:#fff;border:1px solid #dfe9ed;border-radius:15px;box-shadow:0 7px 22px #183d520b}.card{padding:22px;display:flex;flex-direction:column;min-height:245px}.product-icon{width:48px;height:48px;border-radius:15px;background:#e8f4f7;color:#087f89;display:grid;place-items:center;font-size:21px;margin-bottom:15px}.card h2{font-size:18px;margin:0 0 4px;color:#1c4055}.card p{margin:4px 0;color:#688092}.card .bottom{margin-top:auto;padding-top:17px;display:flex;align-items:center;justify-content:space-between;gap:10px}.price{font-size:21px;font-weight:800;color:#12617a}.stock{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#587464}.stock:before{content:"";width:7px;height:7px;border-radius:50%;background:#3aaf79}.panel{padding:26px}.narrow{max-width:530px;margin:58px auto}.narrow h1{margin-top:0}.alert{padding:12px 15px;border-radius:9px;background:#fff1ef;color:#963c34;border:1px solid #f0d2ce;margin:15px 0}.success{background:#eaf7f1;border-color:#c5e5d8;color:#216a54}.empty{text-align:center;padding:50px 22px;border:1px dashed #c6dce2;border-radius:15px;background:#fff}.empty h2{margin:0 0 5px}.cart-row{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:13px;align-items:center;padding:17px 0;border-bottom:1px solid #e6eef1}.qty{display:flex;align-items:center;gap:8px}.qty form{display:inline}.subtotal{font-size:22px;font-weight:800;text-align:right;padding:21px 0;color:#184e63}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 17px}.form-grid .wide{grid-column:1/-1}.footer{padding:32px 24px;text-align:center;color:#8295a0;font-size:13px}@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.cart-row{grid-template-columns:1fr 1fr}.nav{padding:14px 18px}.container{padding:0 18px}.hero{padding:27px}.toolbar{align-items:stretch;flex-direction:column}}@media(max-width:480px){.grid{grid-template-columns:1fr}.navlinks{gap:13px;font-size:13px}.brand{font-size:17px}.hero h1{font-size:28px}.form-grid{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}}
"""


def _page(title: str, body: str, *, logged_in: bool = False) -> HTMLResponse:
    nav = (
        '<a href="/">Catalog</a><a href="/cart" id="cart-link">Your cart</a><a href="/logout">Sign out</a>'
        if logged_in else '<a href="/login">Sign in</a>'
    )
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)} · Meridian Office</title><style>{STYLE}</style></head><body>'
        f'<header class="topbar"><nav class="nav"><a class="brand" href="/">MERIDIAN <span>OFFICE</span></a>'
        f'<div class="navlinks">{nav}</div></nav></header><main class="container">{body}</main>'
        '<footer class="footer">Meridian Office · Quality supplies for teams that do great work</footer></body></html>'
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


def _cart_lines(cart_data: dict[str, int]) -> tuple[str, int]:
    lines: list[str] = []
    subtotal = 0
    for product_id, quantity in cart_data.items():
        product = PRODUCTS.get(product_id)
        if not product or quantity < 1 or quantity > product["stock"]:
            continue
        line_total = product["price"] * quantity
        subtotal += line_total
        lines.append(
            f'<div class="cart-row"><div><strong>{escape(product["name"])}</strong>'
            f'<div class="muted">{_money(product["price"])} per item</div></div>'
            f'<div>Unit price <strong>{_money(product["price"])}</strong></div>'
            f'<div class="qty"><form method="post" action="/cart/quantity/{product_id}"><input type="hidden" name="change" value="-1">'
            f'<button class="btn secondary small" id="decrease-{product_id}" aria-label="Decrease {escape(product["name"])} quantity">−</button></form>'
            f'<strong>{quantity}</strong><form method="post" action="/cart/quantity/{product_id}"><input type="hidden" name="change" value="1">'
            f'<button class="btn secondary small" id="increase-{product_id}" aria-label="Increase {escape(product["name"])} quantity">+</button></form></div>'
            f'<strong>{_money(line_total)}</strong><form method="post" action="/cart/remove/{product_id}">'
            f'<button class="btn danger small" id="remove-{product_id}">Remove</button></form></div>'
        )
    return "".join(lines), subtotal


@app.get("/", response_class=HTMLResponse)
async def shop(request: Request, q: str = "") -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    search_term = q.strip().casefold()
    matches = [(product_id, product) for product_id, product in PRODUCTS.items() if search_term in product["name"].casefold()]
    icons = ("▤", "✒", "▣", "▧", "▤")
    cards = "".join(
        f'<article class="card" id="product-{product_id}"><div class="product-icon" aria-hidden="true">{icon}</div>'
        f'<h2>{escape(product["name"])}</h2><p>{escape(product["description"])}</p>'
        f'<p class="stock">{product["stock"]} available</p><div class="bottom"><span class="price">{_money(product["price"])}</span>'
        f'<form method="post" action="/cart/add/{product_id}"><button class="btn" id="add-to-cart-{product_id}">Add to Cart</button></form></div></article>'
        for (product_id, product), icon in zip(matches, icons)
    )
    if not cards:
        cards = '<section class="empty" id="empty-state"><h2>No matching supplies</h2><p class="muted">Try a different product name or browse the full catalog.</p><a class="btn secondary" href="/">View all products</a></section>'
    body = (
        '<section class="hero"><div class="eyebrow">The Meridian collection</div>'
        '<h1>Thoughtful supplies for better work.</h1><p class="muted">Quality essentials, selected with care and ready for your team.</p></section>'
        '<div class="toolbar"><form class="search" method="get" action="/">'
        f'<label for="product-search" style="position:absolute;left:-10000px">Search products</label><input class="field" id="product-search" name="q" type="search" placeholder="Search office essentials" value="{escape(q, quote=True)}">'
        '<button class="btn" type="submit">Search</button></form><span class="muted">Curated workplace essentials</span></div>'
        f'<section class="grid" id="product-list" aria-label="Product catalog">{cards}</section>'
    )
    return _page("Office supplies", body, logged_in=True)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "") -> Response:
    sid, _ = _session(request, create=True)
    message = '<div class="alert" role="alert" id="login-error">Those credentials were not recognized. Please try again.</div>' if error else ""
    body = (
        '<section class="panel narrow"><div class="eyebrow">Meridian customer access</div><h1>Welcome to Meridian</h1>'
        '<p class="muted">Sign in to explore our premium workplace collection.</p>' + message +
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
    assert sid is not None and data is not None
    data["authenticated"] = True
    return _cookie(RedirectResponse("/", status_code=303), sid)


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
    current_quantity = data["cart"].get(product_id, 0)
    if current_quantity >= product["stock"]:
        return HTMLResponse("Requested quantity exceeds available stock.", status_code=409)
    data["cart"][product_id] = current_quantity + 1
    return RedirectResponse("/cart", status_code=303)


@app.get("/cart", response_class=HTMLResponse)
async def cart(request: Request) -> Response:
    access = _require_login(request)
    if isinstance(access, RedirectResponse):
        return access
    _, data = access
    lines, subtotal = _cart_lines(data["cart"])
    if not lines:
        body = '<section class="empty" id="cart"><h1>Your cart is waiting for something good</h1><p class="muted">Explore our catalog and add supplies for your team.</p><a class="btn" href="/">Browse products</a></section>'
    else:
        body = (
            '<h1 id="cart">Your cart</h1><p class="muted">Review your selections and quantities.</p>'
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
    quantity = data["cart"].get(product_id, 0) + change
    if quantity <= 0:
        data["cart"].pop(product_id, None)
    elif quantity <= PRODUCTS[product_id]["stock"]:
        data["cart"][product_id] = quantity
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
    alert = '<div class="alert" role="alert">Please check the delivery details and try again.</div>' if error else ""
    body = (
        '<div class="eyebrow">Secure checkout</div><h1>Delivery information</h1>'
        '<p class="muted">Tell us where to deliver your Meridian order.</p>' + alert +
        f'<section class="panel"><form id="checkout-form" method="post" action="/checkout"><div class="form-grid">'
        '<div class="wide"><label for="full-name">Full name</label><input class="field" id="full-name" name="full_name" autocomplete="name" required minlength="2"></div>'
        '<div><label for="phone">Phone number</label><input class="field" id="phone" name="phone" type="tel" autocomplete="tel" required pattern="[0-9+() -]{10,16}"></div>'
        '<div><label for="pincode">PIN code</label><input class="field" id="pincode" name="pincode" inputmode="numeric" autocomplete="postal-code" required pattern="[0-9]{6}"></div>'
        '<div class="wide"><label for="address">Street address</label><input class="field" id="address" name="address" autocomplete="street-address" required minlength="5"></div>'
        '<div><label for="city">City</label><input class="field" id="city" name="city" autocomplete="address-level2" required></div>'
        '<div><label for="state">State</label><input class="field" id="state" name="state" autocomplete="address-level1" required></div>'
        '</div><div class="subtotal">Order total: ' + _money(subtotal) + '</div>'
        '<button class="btn" id="place-order-button" type="submit">Place Order &amp; Pay</button>'
        '<p class="muted" style="font-size:13px">Simulated payment only. No payment information is collected.</p></form></section>'
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
    address_valid = (
        2 <= len(full_name.strip()) <= 100
        and bool(re.fullmatch(r"[0-9+() -]{10,16}", phone.strip()))
        and 5 <= len(address.strip()) <= 200
        and 2 <= len(city.strip()) <= 80
        and 2 <= len(state.strip()) <= 80
        and bool(re.fullmatch(r"[0-9]{6}", pincode.strip()))
    )
    cart_lines, total = _cart_lines(data["cart"])
    cart_valid = (
        bool(cart_lines)
        and len(data["cart"]) == len([product_id for product_id in data["cart"] if product_id in PRODUCTS])
        and all(0 < quantity <= PRODUCTS[product_id]["stock"] for product_id, quantity in data["cart"].items() if product_id in PRODUCTS)
    )
    if not address_valid or not cart_valid:
        response = await checkout_page(request, error="1")
        response.status_code = 422
        return response

    order_id = "MC-" + secrets.token_hex(4).upper()
    order_items = "".join(
        f'<li>{escape(PRODUCTS[product_id]["name"])} × {quantity} — {_money(PRODUCTS[product_id]["price"] * quantity)}</li>'
        for product_id, quantity in data["cart"].items()
    )
    delivery_address = ", ".join((address.strip(), city.strip(), state.strip(), pincode.strip()))
    body = (
        '<section class="panel"><div class="eyebrow">Order confirmed</div><h1>Your Meridian order is on its way.</h1>'
        f'<div class="alert success" role="status">Order <strong id="order-id">{order_id}</strong> placed successfully.</div>'
        f'<h2>Order summary</h2><ul>{order_items}</ul>'
        f'<p><strong>Deliver to:</strong> {escape(full_name.strip())}<br>{escape(delivery_address)}</p>'
        f'<p class="subtotal">Order total: {_money(total)}</p><a class="btn" href="/">Return to catalog</a></section>'
    )
    data["cart"].clear()
    return _page("Order confirmed", body, logged_in=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "portals.supplier_c:app",
        host=os.getenv("AMPRIBA_BIND_HOST", "127.0.0.1"),
        port=SUPPLIER_C_PORT,
    )
