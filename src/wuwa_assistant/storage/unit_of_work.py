"""Explicit SQLAlchemy transaction boundary for application services."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session

from .engine import SessionFactory
from .repositories import SettingsRepository


class SqlAlchemyUnitOfWork:
    """A short transaction that rolls back unless the caller explicitly commits."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None
        self.settings: SettingsRepository | None = None
        self._finished = False

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self.session is not None:
            raise RuntimeError("unit of work cannot be entered twice")
        self.session = self._session_factory()
        self.settings = SettingsRepository(self.session)
        return self

    def commit(self) -> None:
        session = self._require_session()
        session.commit()
        self._finished = True

    def rollback(self) -> None:
        session = self._require_session()
        session.rollback()
        self._finished = True

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception, traceback
        session = self._require_session()
        try:
            if exception_type is not None or not self._finished:
                session.rollback()
        finally:
            session.close()
            self.session = None
            self.settings = None

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("unit of work is not active")
        return self.session


__all__ = ["SqlAlchemyUnitOfWork"]
