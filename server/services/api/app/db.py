from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from marquee_core.models import Base

from .config import settings

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db() -> None:
    """Create tables on first boot.

    Real schema CHANGES go through Alembic (homelab-app-standard §12); this only
    bootstraps an empty database so a fresh install comes up.
    """
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
