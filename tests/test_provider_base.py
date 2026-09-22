import pytest

from aigauge.config import Config
from aigauge.models import SnapshotStatus
from aigauge.providers.copilot import CopilotProvider
from aigauge.providers.opencode_go import OpenCodeGoProvider
from aigauge.providers.openrouter import OpenRouterProvider


class _ImmediatePool:
    def __init__(self):
        self.started = 0

    def start(self, worker):
        self.started += 1
        worker.run()


@pytest.mark.parametrize(
    ("factory", "provider_id"),
    [
        (lambda config, pool: CopilotProvider(config, pool=pool), "copilot"),
        (lambda config, pool: OpenRouterProvider(config, pool=pool), "openrouter"),
        (
            lambda config, pool: OpenCodeGoProvider(
                config,
                account_id="opencode_go-work",
                pool=pool,
            ),
            "opencode_go-work",
        ),
    ],
)
def test_shared_async_worker_preserves_provider_error_fallback(factory, provider_id):
    pool = _ImmediatePool()
    provider = factory(Config(), pool)
    captured = []

    def fail():
        raise RuntimeError("boom")

    provider._run_async(fail, captured.append)  # noqa: SLF001

    assert pool.started == 1
    assert len(captured) == 1
    assert captured[0].provider == provider_id
    assert captured[0].status is SnapshotStatus.ERROR
    assert captured[0].error == "boom"
