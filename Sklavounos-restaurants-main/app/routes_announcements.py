
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models_announcements import Announcement
from datetime import datetime

router = APIRouter()

@router.get("/admin/announcements")
def list_announcements(request: Request, db: Session = Depends(get_db)):
    announcements = db.query(Announcement).order_by(Announcement.id.desc()).all()
    return request.app.state.templates.TemplateResponse(
        "admin/announcements.html",
        {"request": request, "announcements": announcements}
    )

@router.get("/admin/announcements/new")
def new_announcement(request: Request):
    return request.app.state.templates.TemplateResponse(
        "admin/announcement_form.html",
        {"request": request, "announcement": None}
    )

@router.post("/admin/announcements/new")
def create_announcement(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
    type: str = Form("INFO"),
    is_active: str = Form(None),
    db: Session = Depends(get_db)
):
    ann = Announcement(
        title=title,
        body=body,
        type=type,
        is_active=True if is_active else False,
        created_at=datetime.utcnow()
    )
    db.add(ann)
    db.commit()
    return RedirectResponse("/admin/announcements", status_code=303)

@router.get("/admin/announcements/delete/{ann_id}")
def delete_announcement(ann_id: int, db: Session = Depends(get_db)):
    ann = db.query(Announcement).get(ann_id)
    if ann:
        db.delete(ann)
        db.commit()
    return RedirectResponse("/admin/announcements", status_code=303)
