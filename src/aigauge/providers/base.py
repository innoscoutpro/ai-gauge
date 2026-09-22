from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable

from ..models import SnapshotStatus, UsageSnapshot


class Provider(ABC):
    """Base class for a usage data source.

    Implementations may either return a snapshot synchronously (for plain HTTP
    providers like Copilot) or invoke the on_done callback asynchronously after
    a QWebEngineView load (Claude/Codex). Use ProviderSignals to bridge to the
    Qt main thread.
    """

    name: str = ""
    display_name: str = ""

    @abstractmethod
    def refresh(self, on_done: Callable[[UsageSnapshot], None]) -> None:
        """Trigger a refresh; deliver the result via on_done.

        on_done must be safe to call from a worker thread; the caller marshals
        it onto the GUI thread.
        """
        raise NotImplementedError

    def _run_async(
        self,
        work: Callable[[], UsageSnapshot],
        on_done: Callable[[UsageSnapshot], None],
    ) -> None:
        """Run blocking provider work in Qt's shared worker pool."""
        from PyQt6.QtCore import QRunnable, QThreadPool

        provider_id = getattr(self, "_account_id", None) or self.name
        logger = logging.getLogger(self.__class__.__module__)

        class _Worker(QRunnable):
            def run(self_inner) -> None:  # noqa: N805
                try:
                    snapshot = work()
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "provider api diagnosis provider=%s "
                        "classification=unexpected_exception type=%s",
                        provider_id,
                        type(exc).__name__,
                    )
                    snapshot = UsageSnapshot(
                        provider=provider_id,
                        status=SnapshotStatus.ERROR,
                        error=str(exc),
                    )
                on_done(snapshot)

        pool = getattr(self, "_pool", None) or QThreadPool.globalInstance()
        pool.start(_Worker())


def _make_signals():
    """Construct ProviderSignals lazily so tests don't need PyQt6 installed."""
    from PyQt6.QtCore import QObject, pyqtSignal

    class ProviderSignals(QObject):
        snapshot_ready = pyqtSignal(object)

    return ProviderSignals()


class _LazySignals:
    def __call__(self):
        return _make_signals()


ProviderSignals = _LazySignals()
