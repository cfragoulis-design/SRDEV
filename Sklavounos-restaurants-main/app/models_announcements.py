from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class Announcement(Base):
    __tablename__ = "announcements"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    type = Column(String, default="INFO")
    is_active = Column(Boolean, default=True)
    start_at = Column(DateTime, nullable=True)
    end_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    targets = relationship("AnnouncementTarget", back_populates="announcement", cascade="all, delete")
    reads = relationship("AnnouncementRead", back_populates="announcement", cascade="all, delete")

class AnnouncementTarget(Base):
    __tablename__ = "announcement_targets"
    id = Column(Integer, primary_key=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"))
    restaurant_customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"))

    announcement = relationship("Announcement", back_populates="targets")

class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"
    id = Column(Integer, primary_key=True)
    announcement_id = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"))
    restaurant_customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"))
    read_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("announcement_id", "restaurant_customer_id", name="uq_announcement_read"),
    )

    announcement = relationship("Announcement", back_populates="reads")
