
from sqlalchemy.orm import Session
from app.models_announcements import Announcement
from datetime import datetime

def get_active_announcements(db: Session, customer_id: int | None = None):
    now = datetime.utcnow()

    anns = db.query(Announcement).filter(
        Announcement.is_active == True
    ).all()

    # If no customer context → behave like admin/global view
    if customer_id is None:
        return anns

    visible = []
    for a in anns:
        # Global announcement (no targets)
        if not a.targets:
            visible.append(a)
            continue

        # Targeted announcement
        for t in a.targets:
            if t.restaurant_customer_id == customer_id:
                visible.append(a)
                break

    return visible
