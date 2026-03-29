
# --- PATCH: autosave contact endpoint ---
from fastapi import Request
from fastapi.responses import JSONResponse

@app.post("/p/{slug}/save-contact")
async def save_contact(slug: str, request: Request):
    data = await request.json()
    phone = data.get("phone")
    email = data.get("email")

    customer = db.query(Customer).filter(Customer.slug == slug).first()
    if not customer:
        return JSONResponse({"ok": False})

    if phone:
        customer.phone = phone
    if email:
        customer.email = email

    db.commit()
    return {"ok": True}
