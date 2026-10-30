# Changelog

## 1.1.0

- Merge shared authentication, bounded renewal, official HTTPS endpoint validation,
  and credential-preserving configuration changes.
- Leave missing energy readings unknown instead of fabricating zero totals.
- Isolate history ranges and refresh timestamps per account/station; cache completed
  days and reuse settings readback across platforms.
- Share paginated discovery at startup and hourly, recover setup failures, and add
  newly discovered controls, telemetry, and monthly sensors dynamically.
- Serialize per-inverter commands and preserve uncertain orders across reloads.
  Use documented order statuses and keep unknown statuses pending.
- Pace API requests and honor bounded HTTP 429 Retry-After delays.
- Add account duplicate checks, history-month validation, and reauthentication.
- Update freshness attributes from current readings and reject nonfinite values.
- Restore the complete original MIT license and retain attribution.
- Correct documentation, dashboard examples, translations, and release metadata.
- Add regression and real Home Assistant interface tests plus CI validation.

Requires Home Assistant 2025.1 or newer; interface tests target 2025.1.4.
Hardware-specific settings/register compatibility still requires installation testing.
