
## 👤 Owner

- **Name**: Trần Công Tuấn Anh  
- **GitHub**: [@heavenknows1978](https://github.com/heavenknows1978)  
- **Repo**: [hass-deyecloud](https://github.com/heavenknows1978/hass-deyecloud)  
- **License**: MIT

# 🌞 Deye Cloud Home Assistant Integration

A custom integration to connect your Home Assistant with your Deye solar inverter via the official Deye Cloud API.

---

## 📥 Features

- 🟢 Fetch monthly data: generation, consumption, battery, grid import/export, fetching recent days information, fetching current device status
- 📈 Sensors for current & last month, today, yesterday...
- 🔃 Auto refresh every minute (no YAML needed)
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
2. Add: `https://github.com/heavenknows1978/hass-deyecloud` (as Integration)
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

---

## 📸 Sample Dashboard

> Sample Lovelace dashboard tiles showing PV generation, consumption, battery usage, grid stats etc.

![Dashboard](https://raw.githubusercontent.com/heavenknows1978/hass-deyecloud/main/screenshot.png)

---

## 🧾 Troubleshooting

### Settings readback (1.0.20)

Grid charge and the three current controls now display settings read from
DeyeCloud, rather than the last requested command value. A shared per-inverter
coordinator reads on startup, every five minutes, and after a successful command.
The controls expose `state_source` and `settings_read_at` attributes.

`/config/battery` supplies decoded current limits. Grid-charge enable/current
are read through `/strategy/dynamicControl/read` and the returned order result.
Register decoding follows the upstream project's single-phase low-voltage hybrid
layout, guarded by layout markers `00F4` and `00F8`. Other layouts are left unknown.
See [Deye's API schemas](https://developer.deyecloud.com/openmcp/docs/deye-open-mcp-tools.html)
and [upstream decoder](https://github.com/heavenknows1978/hass-deyecloud/blob/main/custom_components/deyecloud/control_payloads.py).

Missing, unsupported or failed readings make the affected controls unavailable;
they never fall back to the requested setting. This can prevent automations from
using those controls until a read succeeds. DeyeCloud may return delayed data;
the read timestamp is the time Home Assistant fetched it, not proof of freshness
at the inverter. Grid charging enabled is a setting, not proof of charging power.

The update has automated tests with minimal Home Assistant interface doubles;
it still needs installation testing against your Home Assistant and inverter.

### Request retries

- Control requests that receive HTTP 500 are retried up to two times, after
  2 seconds and 4 seconds (three attempts total). Each retry is logged.
- Other HTTP errors and connection timeouts are not retried by this retry loop.
  The loop only covers submission, not order polling. A server error can
  still occur after a command has reached the inverter, so a retry may repeat
  the same explicit setting.
- Battery parameter updates retain the alternative parameter-spelling fallback;
  each spelling can make up to three attempts (six submissions total). This
  existing fallback also runs if order polling fails for the first spelling.
- Persistent HTTP 500 errors still fail the action after retries. Retries help
  temporary failures; they do not fix unsupported parameters or API defects.

- Check **Settings → System → Logs** for errors
- Ensure you restarted HA after copying files
- Ensure `custom_components/deyecloud/` has correct permissions

---

## 📄 License

[MIT License](LICENSE)
