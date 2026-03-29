# ADD / REPLACE THIS ROUTE

@app.post("/p/{slug}/update-contact")
def update_contact(
    slug: str,
    request: Request,
    email: str = Form(""),
    phone: str = Form(""),
    date_str: str = Form(""),
    db: Session = Depends(get_db),
):
    c = db.query(Customer).filter(Customer.slug == slug).first()
    if not c:
        raise HTTPException(404)

    c.email = (email or "").strip()
    c.phone = (phone or "").strip()
    db.commit()

    qs = f"?date_str={date_str}&msg=contact_saved" if (date_str or "").strip() else "?msg=contact_saved"
    return RedirectResponse(url=f"/p/{slug}/order{qs}", status_code=302)
