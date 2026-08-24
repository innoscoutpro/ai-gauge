# Plan: ship the MCP usage guard as a minimal opt-in integration

**Status:** implemented; local tests and cross-platform packaged-helper CI pass
**Related:** PR #10, “Add MCP usage guard and tray-safe cache for v0.7.5”

## Product boundary

Keep the contributor's core feature: an MCP-aware client can read sanitized AI
Gauge usage and honor a user-configured pause threshold for one explicitly
selected account.

The integration is optional and disabled by default. Users who do not enable
it should see no ongoing runtime or storage effect beyond the presence of an
MCP tab in Settings and one-time cleanup of any cache left by an earlier build:

- no usage-cache writes
- no MCP SDK import in the GUI
- no Codex auth-file reads
- no extra timers, refreshes, or background process
- no MCP dependency in a normal source installation

MCP remains cooperative. It reports allowed=true or allowed=false, but cannot
force a third-party client to stop.

## Minimal implementation

### 1. Add a default-off feature gate

Add Config.mcp_enabled with a default of false and an Enable MCP integration
checkbox in the MCP Settings tab.

When disabled:

- the policy controls are disabled
- AI Gauge does not publish the cache
- an existing cache is removed
- MCP tools return integration_disabled rather than reading stale data

When enabled, AI Gauge immediately publishes its current snapshots and keeps
the cache updated after successful or failed provider refreshes and account
configuration changes.

Keep mcp_pause_policies as the existing per-account threshold mapping. Validate
thresholds as integers from 1 through 100.

### 2. Use explicit account binding only

Retain:

    ai-gauge-mcp --account-id <account-id>

Remove --auto-codex-account and the email-username/display-name matching
heuristic. Automatic switching is useful but requires a trustworthy exact
ChatGPT-account binding UI and is a separate feature.

An explicit account ID must still refer to a currently configured and enabled
account. Removed or disabled accounts fail closed even if an old cache record
exists.

### 3. Make guard evaluation fail closed

For an account with a configured pause policy, allowed=true requires all of:

1. MCP integration is enabled.
2. The bound account is currently configured and enabled.
3. The cache schema is supported.
4. The snapshot status is OK.
5. fetched_at is valid, not in the future, and no older than the configured
   maximum refresh interval plus a small grace period.
6. At least one finite guard-eligible percentage exists.
7. The maximum eligible percentage is strictly below the threshold.

At the threshold, usage is blocked. Missing, malformed, invalidly encoded,
stale, errored, NaN, or infinite readings fail closed.

When an account has no policy, preserve the opt-in semantics: return
allowed=true with guard_configured=false and a clear no_policy reason. Do not
present that result as verified safe capacity.

Return stable reason_code values in addition to human-readable reasons so
tests and clients do not parse prose.

Distinguish "AI Gauge published nothing" from "the last refresh failed". An
unreadable or absent cache is reported as cache_unavailable, not
snapshot_not_ok: both block, but only one of them means a refresh happened.
The cache carries an explicit availability flag so an empty-but-real
publication is never mistaken for a missing one.

### 4. Filter informational metrics

The existing UsageMetric.tag already distinguishes OpenRouter model-breakdown
rows from limit gauges. Preserve a boolean guard_eligible field in the cache
and include only untagged metrics in threshold calculations.

This is the smallest correction for the current model. A larger metric-purpose
enum can wait until another provider needs multiple percentage semantics.

If a configured policy has no eligible percentage, fail closed.

### 5. Keep cache contents minimal and lifecycle-safe

Add a cache schema version and publication timestamp. Stamp timestamps with an
explicit UTC offset: snapshots are recorded in naive local time, so around a
DST fall-back a pre-transition reading otherwise reads as future-dated and the
guard blocks until the wall clock catches up. Publish only:

- account ID
- status
- fetched_at
- metric label, percentage, reset information, and guard eligibility

Do not cache raw provider responses, credentials, arbitrary exception strings,
or metric notes. Retain the contributor's unique temporary file, fsync, atomic
replace, Windows sharing retries, and temporary-file cleanup.

Republish after snapshots and provider/account changes only when MCP is
enabled. Remove the cache when MCP is disabled or AI Gauge exits cleanly.
Freshness checks handle crashes.

### 6. Make recommendations use the same safety rules

recommend_ai_account may consider only accounts that:

- have a configured policy
- are currently configured and enabled
- have an OK, fresh, valid snapshot
- are below their threshold

Rank by positive policy headroom. If none qualifies, return account=null with
reason_code=no_allowed_account. Never recommend a stale, errored, disabled, or
over-threshold account.

### 7. Keep dependencies and packaging isolated

Move mcp out of the base dependencies into an optional mcp extra. Include it in
the existing dev extra so tests and release builds still install it.

The GUI must not import aigauge.mcp_server. The console entry point should
produce a clear installation message if the optional SDK is absent.

Build a separate console-mode, one-file ai-gauge-mcp helper and place it in each
release artifact alongside the GUI application. Its stdout is reserved for MCP
protocol traffic. Exercise initialize, tool discovery, a guard call, and clean
shutdown against the frozen helper on every supported CI platform.

Give the helper the same Windows product/version resource the GUI carries: it
is unsigned and launched headlessly by MCP clients, so a reputation block is
silent rather than a visible prompt. Have both build scripts assert the helper
reached the path release.yml packages from, so a layout mistake fails at build
time rather than partway through a tag release.

### 8. Update Settings and documentation

The MCP tab should:

- explain that the integration is local, optional, and cooperative
- contain the default-off enable checkbox
- disable policy controls while integration is off
- document explicit --account-id binding only
- save policies only for accounts that still exist when the dialog is applied,
  since the tab's rows are built once, before an account can be removed

Fix the README version, describe how to enable the integration, distinguish
source installations from release artifacts, and remove automatic-account
claims.

## File changes

### src/aigauge/config.py

- Add mcp_enabled=false.
- Constrain mcp_pause_policies values to 1–100.
- Drop unusable policy entries during migration. Config.load() falls back to a
  default Config on any validation error, so an out-of-range threshold would
  otherwise discard every unrelated setting the user has saved.

### src/aigauge/settings_dialog.py

- Add the master checkbox.
- Gate the policy group.
- Remove automatic-binding instructions.
- Save the enabled state.

### src/aigauge/usage_cache.py

- Add schema metadata and guard_eligible.
- Remove error and note fields.
- Add invalidate_usage_cache.

### src/aigauge/app.py

- Gate cache publication.
- Synchronize the cache after provider/account changes.
- Invalidate it on disable and clean exit.

### src/aigauge/mcp_server.py

- Remove automatic identity resolution.
- Refuse disabled integration and invalid accounts.
- Validate schema, status, timestamps, freshness, and numeric percentages.
- Filter by guard_eligible.
- Make recommendation reuse the validated guard path.
- Build rows one account at a time so a configured account absent from the
  cache yields an unknown-status row the guard rejects, instead of a
  missing-row case every caller must remember to handle.
- Add stable reason codes and optional-SDK handling.

### pyproject.toml and build/release scripts

- Make MCP an optional dependency included by dev.
- Build and package the separate console helper.
- Add a packaged-helper smoke check where practical.

### README.md, SECURITY.md, RELEASING.md, CHANGELOG.md, and tests

- Record the usage cache in SECURITY.md's data-at-rest section, including its
  0600 mode and the fields excluded by construction.
- Cover the helper in the release checklist, including the macOS quarantine
  step and the manual-fallback archive command that would otherwise omit it.
- Note that --account-id scopes the guard, not visibility.
- Correct version consistency and describe the opt-in behavior.
- Add focused regressions for every safety correction.

## Focused test matrix

- MCP defaults disabled and round-trips through config.
- Disabled integration creates no cache; disabling removes an existing cache.
- Below, at, and above threshold.
- ERROR and AUTH_REQUIRED snapshots carrying old metrics.
- Fresh, stale, future, and malformed fetched_at values.
- Missing, string, boolean, NaN, and infinite percentages.
- Invalid JSON and invalid UTF-8 cache contents.
- Absent cache reported as unavailable rather than as a failed refresh, and an
  empty-but-published cache not treated as absent.
- An out-of-range or malformed pause threshold does not reset unrelated config.
- OpenRouter daily budget at 10% plus a model-share row at 100%.
- Removed and disabled account IDs.
- No policy versus configured policy with no eligible metric.
- Recommendations exclude blocked and invalid accounts and return no candidate
  when all are unavailable.
- Cache output contains no secret sentinel, raw response, exception text, or
  metric note.
- Windows cache-sharing retry remains covered.
- Settings enable checkbox gates and saves policy controls.
- Base package metadata excludes mcp; mcp and dev extras include it.
- Full existing suite remains green on the supported CI matrix.

## Acceptance criteria

- [x] With MCP disabled, normal GUI refresh behavior produces no MCP cache or
      MCP import.
- [x] Enabling MCP publishes sanitized usage; disabling it removes the cache.
- [x] A configured guard never allows stale, non-OK, malformed, or ineligible
      usage.
- [x] OpenRouter breakdown percentages cannot trigger a pause policy.
- [x] Only explicit, currently valid account IDs can be bound.
- [x] Recommendations never return blocked accounts.
- [x] Normal source installation does not install the MCP SDK.
- [ ] Exact release-archive placement remains gated by the tag release workflow;
      one-file helper packaging and protocol are verified on all platforms in PR CI.
- [x] README/package/application/changelog versions agree.
- [x] A blocked guard names the real cause: an unpublished cache is reported as
      cache_unavailable rather than as a failed refresh.
- [x] A bad MCP threshold cannot discard unrelated saved settings.
- [x] A policy is never saved for an account removed in the same dialog session.
- [x] Cache timestamps survive a backwards clock change.
- [x] The packaged helper carries product/version metadata, and both build
      scripts assert its release path.
- [x] RELEASING.md's manual fallback ships the helper on every platform.
- [x] The full local test suite passes (404 tests) and all nine PR CI jobs pass.

## Explicitly deferred

- Automatic Codex identity binding or account switching
- on-demand refresh IPC
- per-metric thresholds
- prediction or reset-aware scoring
- notifications
- sharing model history or detailed spend through MCP
- HTTP, SSE, or network-accessible transport

These can be proposed independently after the small opt-in integration is
released and proven useful.
