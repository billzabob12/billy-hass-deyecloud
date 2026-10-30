
## 👤 Maintainer

- **Maintainer**: [Billy (@billzabob12)](https://github.com/billzabob12)
- **Repo**: [billy-hass-deyecloud](https://github.com/billzabob12/billy-hass-deyecloud)
- **License**: MIT

# 🌞 Billy’s DeyeCloud Home Assistant Integration

A custom integration to connect your Home Assistant with your Deye solar inverter via the official Deye Cloud API.

Requires Home Assistant **2025.1 or newer**. Compatibility tests run against Home Assistant 2025.1.4. The integration version is defined in `custom_components/deyecloud/manifest.json`.

---

## 📥 Features

- 🟢 Fetch monthly data: generation, consumption, battery, grid import/export, fetching recent days information, fetching current device status
- 📈 Sensors for current & last month, today, yesterday...
- 🔃 Live readings every minute; shared settings every five minutes; discovery hourly
- Recover from temporary startup failures and reauthenticate rejected credentials
- Discover new devices and telemetry without restarting Home Assistant
- ✅ Clean and simple setup via UI

---

## 🛠 Installation

### Option 1: Manual

1. Download or clone this repository
2. Copy `custom_components/deyecloud/` into your `/config/custom_components/` directory in Home Assistant
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration → DeyeCloud**
5. Fill in your credentials and API details

### Option 2: Via HACS

1. Go to HACS → Integrations → 3-dot menu → Custom repositories
2. Add: `https://github.com/billzabob12/billy-hass-deyecloud` (as Integration)
3. Search for "DeyeCloud" in HACS Integrations and install
4. Restart Home Assistant and add via UI

---

## 🔐 Get your API Credentials

### Step 1 – Register developer account

👉 Go to: https://developer.deyecloud.com/home  
→ Register or login with your Deye Cloud credentials

### Step 2 – Create a new App

👉 Go to: https://developer.deyecloud.com/app  
→ Click **“Create App”**  
→ You'll get:

- `App ID`
- `App Secret`

Use these during integration setup.

### Step 3 – Choose correct Base URL

Depending on your region:

| Region | Base URL |
|--------|----------|
| 🇪🇺 Europe | `https://eu1-developer.deyecloud.com/v1.0` |
| 🇺🇸 US     | `https://us1-developer.deyecloud.com/v1.0` |

---

## ⚙️ Configuration Fields

| Field       | Description |
|-------------|-------------|
| Username    | Your Deye Cloud Username / Email |
| Password    | Your Deye password |
| App ID      | From developer portal |
| App Secret  | From developer portal |
| Base URL    | Based on your region |
| Start Month | First month to fetch history from (e.g. `2024-01`) |
| Company ID | Optional company context for installer/business accounts |

### Authentication and credential storage

The integration exchanges your username/email, SHA-256 password digest, App ID,
App Secret, and optional Company ID for a DeyeCloud bearer token. It accepts only
the EU and US HTTPS API URLs listed above; redirects are disabled. Passwords and
App Secrets are preserved exactly, including leading and trailing whitespace.

Home Assistant stores the original credentials in the integration's config entry
so it can obtain new tokens. This integration does not add encryption to those
stored values. Protect your Home Assistant configuration and backups accordingly.
The password digest is produced for the token request; it does not replace the
stored password. Password and App Secret fields use masked input controls.

All platforms in a config entry share one in-memory token cache. Concurrent
requests share token acquisition, and tokens are renewed after a conservative
25-minute local lifetime. An HTTP 401 or 403 triggers one renewal and one retry;
The bounded HTTP 500 submission retry is retained for explicit-value
commands. Other command failures and uncertain order results do not automatically
resubmit the command. JSON-level errors are surfaced without guessing which vendor error
codes mean token expiry. Tokens are not saved across integration reloads.

Use **Settings → Devices & Services → DeyeCloud → Configure** to update
credentials. Updates are validated and reload the integration.

### Development checks

With Python 3.12+ installed, run:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m compileall -q custom_components
```

The regression tests fake the HTTP boundary and do not issue inverter commands.
For real Home Assistant configuration and lifecycle tests, use Python 3.13:

```sh
python -m pip install -r requirements-ha-test.txt
python -m pytest tests/ha
```

CI runs unit tests, real Home Assistant interface tests, Ruff, Hassfest, and HACS
validation. Unit tests use doubles where appropriate; interface tests use real
Home Assistant objects and mock the cloud and platform forwarding.

---

## 📸 Sample Dashboard

> Sample Lovelace dashboard tiles showing PV generation, consumption, battery usage, grid stats etc.

See [`sunsynkcard.txt`](sunsynkcard.txt) for a template. Replace every
`your_inverter_serialnumber` placeholder with your inverter serial number and
check each entity ID in Developer Tools → States. Fields using `switch.sunsynk_*`,
`binary_sensor.sunsynk_*`, and `sensor.sunsynk_*` belong to other integrations and
are intentionally omitted from this template. The custom Sunsynk card must be
installed separately; available telemetry depends on the inverter.

---

## 🧾 Troubleshooting

### Settings readback (1.0.20)

Grid charge and the three current controls now display settings read from
DeyeCloud, rather than the last requested command value. A shared per-inverter
coordinator reads on startup, every five minutes, and after a successful command.
The controls expose `state_source` and `settings_read_at` attributes.

`/config/battery` supplies decoded current limits. Grid-charge enable/current
are read through `/strategy/dynamicControl/read` and the returned order result.
Register decoding follows the original project's single-phase low-voltage hybrid
layout, guarded by layout markers `00F4` and `00F8`. Other layouts are left unknown.
See [Deye's API schemas](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html)
and [control payload decoder](https://github.com/billzabob12/billy-hass-deyecloud/blob/main/custom_components/deyecloud/settings_values.py).

Missing, unsupported or failed readings make the affected controls unavailable;
they never fall back to the requested setting. This can prevent automations from
using those controls until a read succeeds. DeyeCloud may return delayed data;
the read timestamp is the time Home Assistant fetched it, not proof of freshness
at the inverter. Grid charging enabled is a setting, not proof of charging power.

Automated tests cover real Home Assistant configuration/lifecycle interfaces and
HTTP-boundary doubles. Hardware-specific register compatibility still needs
installation testing against your inverter.

### Request retries

- Control requests that receive HTTP 500 are retried up to two times, after
  2 seconds and 4 seconds (three attempts total). Each retry is logged.
- Other HTTP errors and connection timeouts are not retried by this retry loop.
  The loop only covers submission, not order polling. A server error can
  still occur after a command has reached the inverter, so a retry may repeat
  the same explicit setting.
- Battery parameter commands use Deye’s documented `paramterType` field.
  There is no alternative-spelling fallback or resubmission after polling fails.
- Persistent HTTP 500 errors still fail the action after retries. Retries help
  temporary failures; they do not fix unsupported parameters or API defects.

- Check **Settings → System → Logs** for errors
- Ensure you restarted HA after copying files
- Ensure `custom_components/deyecloud/` has correct permissions

---

## Data freshness and command recovery

Missing or failed energy reads are unavailable, never synthetic zero readings.
Monthly history is cached for six hours per station (ten minutes when the
current month is absent). Completed daily totals are cached for 24 hours;
today’s totals are requested every minute. Historical sensors expose
`data_read_at`; device sensors expose the latest cloud `collection_time`.
Home Assistant’s configured timezone determines daily/monthly boundaries.

Station and inverter discovery is shared and refreshed hourly. Settings are
fetched once per inverter every five minutes, shared by sensors and controls.
Requests are spaced at least 1.1 seconds apart per API app/data center, with
bounded station concurrency. HTTP 429 responses delay subsequent requests using
`Retry-After` (one to 300 seconds, default 60). Polling stops retrying permanent
HTTP read errors; transient read failures have a bounded retry.

Commands and dynamic settings reads are serialized per inverter. After a polling
timeout or connection failure, the previous order must resolve before another
command is submitted. If submission failed without an order ID, verify the
inverter’s state and restart Home Assistant before sending further commands.
The in-memory uncertainty guard survives integration reloads, but not an HA
restart. API `success` alone never confirms command execution; unknown order
statuses remain pending.

When credentials are rejected, Home Assistant starts reauthentication. Configure
can change accounts and checks for duplicates; reauthentication keeps the
existing account identity. History start month must be a past or current month
in `YYYY-MM` format.

## Development and releases

See [CHANGELOG.md](CHANGELOG.md) for changes. Before release, run the checks above
and confirm CI passes. Update only the integration manifest’s `version`, add a
changelog entry, then create a matching Git tag and GitHub release. HACS reads the
integration version from the manifest; `hacs.json` contains only HACS settings.
Do not claim compatibility with an older Home Assistant version without running
the interface tests against it. Test cloud/hardware behavior before installing
an update into an installation that relies on automatic inverter controls.

---

## Credits

Originally developed by [Trần Công Tuấn Anh (@heavenknows1978)](https://github.com/heavenknows1978/hass-deyecloud). This repository is maintained by [@billzabob12](https://github.com/billzabob12).

## 📄 License

[MIT License](LICENSE)
