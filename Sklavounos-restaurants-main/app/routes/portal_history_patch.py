# === PATCH: Portal Order History (Step 1 - Read Only) ===
# Add this route inside your existing portal router file

@router.get("/portal/orders")
def portal_order_history(request: Request, db: Session = Depends(get_db)):
    customer_id = request.session.get("customer_id")
    if not customer_id:
        return RedirectResponse("/portal/login")

    orders = (
        db.query(Order)
        .filter(Order.customer_id == customer_id)
        .order_by(Order.created_at.desc())
        .limit(50)
        .all()
    )

    return templates.TemplateResponse(
        "portal_order_history.html",
        {"request": request, "orders": orders},
    )
