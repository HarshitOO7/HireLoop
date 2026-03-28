import uuid
from datetime import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id              = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    telegram_id     = Column(String, unique=True, nullable=False)
    name            = Column(String)
    filters         = Column(JSON)          # role, location, salary, remote, blacklist
    notify_freq          = Column(String, default="daily")   # daily | realtime | twice_daily
    min_fit_score        = Column(Integer, default=60)
    daily_app_limit      = Column(Integer, default=5)
    onboarded            = Column(Boolean, default=False)
    base_resume_markdown = Column(Text)   # raw text extracted from uploaded resume(s)
    created_at           = Column(DateTime, default=datetime.utcnow)

    skill_nodes     = relationship("SkillNode", back_populates="user", cascade="all, delete")
    jobs            = relationship("Job", back_populates="user", cascade="all, delete")


class SkillNode(Base):
    __tablename__ = "skill_nodes"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(String, ForeignKey("users.id"), nullable=False)
    skill_name  = Column(String, nullable=False)
    # verified_resume | verified_attested | partial | gap
    status      = Column(String, nullable=False)
    # high | medium | low — from resume parse
    confidence  = Column(String, default="medium")
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user        = relationship("User", back_populates="skill_nodes")
    evidence    = relationship("SkillEvidence", back_populates="skill_node", cascade="all, delete")


class SkillEvidence(Base):
    __tablename__ = "skill_evidence"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    skill_node_id    = Column(Integer, ForeignKey("skill_nodes.id"), nullable=False)
    company          = Column(String)
    role_title       = Column(String)
    duration_months  = Column(Integer)
    last_used_year   = Column(Integer)
    user_context     = Column(Text)     # user's own words
    generated_bullet = Column(Text)     # Claude's polished bullet
    # resume | telegram | manual
    source           = Column(String, default="resume")

    skill_node       = relationship("SkillNode", back_populates="evidence")


class Job(Base):
    __tablename__ = "jobs"

    id                    = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id               = Column(String, ForeignKey("users.id"), nullable=False)
    title                 = Column(String)
    company               = Column(String)
    url                   = Column(String)
    url_hash              = Column(String, index=True)  # dedup key
    raw_jd                = Column(Text)
    parsed                = Column(JSON)        # output of parse_job()
    fit_score             = Column(Float)
    cover_letter_required = Column(Boolean, default=False)
    recruiter_name        = Column(String)      # Phase 2
    recruiter_linkedin    = Column(String)      # Phase 2
    # pending | skill_verify | approved | skipped | applied
    status                = Column(String, default="pending")
    created_at            = Column(DateTime, default=datetime.utcnow)

    user                  = relationship("User", back_populates="jobs")
    application           = relationship("Application", back_populates="job", uselist=False)


class Application(Base):
    __tablename__ = "applications"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    job_id             = Column(String, ForeignKey("jobs.id"), nullable=False)
    resume_path              = Column(String)
    cover_letter_path        = Column(String)
    resume_markdown          = Column(Text)   # tailored resume stored as Markdown
    cover_letter_markdown    = Column(Text)   # cover letter stored as Markdown (if generated)
    applied_at               = Column(DateTime, default=datetime.utcnow)
    # interview | rejected | ghosted | offer
    outcome            = Column(String)
    # email | manual | telegram
    outcome_source     = Column(String)
    outcome_at         = Column(DateTime)

    job                = relationship("Job", back_populates="application")
