import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aigauge.config import Config
from aigauge.history import HistoryStore, PeriodRecord
from aigauge.local_usage.service import LocalUsageService
from aigauge.local_usage.summaries import ORIGIN_BACKFILL, ORIGIN_LIVE, load_summaries
from aigauge.models import SnapshotStatus, UsageMetric, UsageSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "local_usage"
UTC = timezone.utc


def _local(dt: datetime) -> str:
    return dt.astimezone().replace(tzinfo=None).isoformat()


def _service(tmp_path) -> LocalUsageService:
    claude = tmp_path / "logs" / "claude" / "projects"
    shutil.copytree(FIXTURES / "claude" / "projects", claude)
    old = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    for path in claude.rglob("*.jsonl"):
        import os

        os.utime(path, (old, old))
    config = Config()
    config.local_usage.enabled = True
    config.local_usage.claude.account_id = "claude"
    config.local_usage.claude.log_root = str(claude)
    config.local_usage.codex.enabled = False
    return LocalUsageService(config, base_dir=tmp_path / "data")


def test_import_updates_live_claude_summary_from_noted_periods(tmp_path):
    service = _service(tmp_path)
    record = PeriodRecord(
        "claude", "Session",
        _local(datetime(2026, 9, 10, 16, 0, tzinfo=UTC)),
        _local(datetime(2026, 9, 10, 11, 30, tzinfo=UTC)),
        _local(datetime(2026, 9, 10, 15, 55, tzinfo=UTC)),
        20,
    )
    try:
        service.note_claude_periods("claude", [record], [])
        service.run_sync()

        [summary] = load_summaries(service.store, "claude")
        assert summary.origin == ORIGIN_LIVE
        assert not summary.closed
        assert sum(u.tokens.output for u in summary.usage) == 1136
        assert summary.flags == set()
    finally:
        service.shutdown()


def test_import_backfills_closed_history_periods(tmp_path):
    service = _service(tmp_path)
    closed = PeriodRecord(
        "claude", "Session",
        _local(datetime(2026, 9, 10, 16, 0, tzinfo=UTC)),
        _local(datetime(2026, 9, 10, 11, 30, tzinfo=UTC)),
        _local(datetime(2026, 9, 10, 15, 55, tzinfo=UTC)),
        35,
    )
    service.history_records = lambda: [closed]
    try:
        service.run_sync()

        [summary] = load_summaries(service.store, "claude")
        assert summary.origin == ORIGIN_BACKFILL
        assert summary.last_pct == 35.0
    finally:
        service.shutdown()


def test_history_current_records_are_copies(tmp_path):
    history = HistoryStore(base_dir=tmp_path)
    history.record_snapshot(
        UsageSnapshot(
            provider="claude",
            status=SnapshotStatus.OK,
            metrics=[UsageMetric("Session", 10.0, resets_at=datetime.now() + timedelta(hours=2))],
        )
    )

    [record] = history.current_records()
    record.peak_pct = 99

    assert history.current_records()[0].peak_pct == 10.0
