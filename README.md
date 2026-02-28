# GoodWe ESA Monitor

A live web monitor for GoodWe ESA hybrid inverter/battery systems. Reads data via Modbus TCP and displays it in a real-time dashboard with WebSocket streaming. Includes a settings page for adjusting inverter registers.

---

## Quick start (pre-built image)

Use the `docker-compose.yml` in the repo root — paste it into Portainer or run it directly:

```bash
# Edit INVERTER_IP in docker-compose.yml if needed, then:
docker compose -f docker-compose.yml up -d
```

Open http://localhost:8080 in your browser.

---

## Development (build from source)

```bash
cd docker
docker compose -f docker-compose.dev.yml up -d --build
```

---

## Configuration

Set environment variables directly in the compose file (or via Portainer's Stack → Environment Variables UI):

| Variable | Default | Description |
|---|---|---|
| `INVERTER_IP` | `192.168.1.x` | IP address of your GoodWe inverter |
| `MODBUS_PORT` | `502` | Modbus TCP port |
| `SLAVE_ID` | `247` | Modbus slave ID |
| `TZ` | `Australia/Brisbane` | Timezone for timestamps (e.g. `Europe/London`, `America/New_York`) |
| `PORT` | `8080` | Host port to expose the web UI on |

Once running, connection details can also be changed via the Settings page — they are saved to `/data/config.json` inside the container and take precedence over environment variables on next startup.

---

## Portainer

Deploy as a Stack by pasting in `docker-compose.yml`. Set `INVERTER_IP` (and any other overrides) via the Stack → Environment Variables section — no `.env` file required.

---

## Settings UI

Visit http://localhost:8080/settings to:
- Change the inverter connection details (IP, port, slave ID)
- Read and write inverter registers (Meter Target Power Offset, Grid Export Limit)

---

## Updating

After making code changes:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t dancet/goodwe-esa-monitor:latest . --push
```

For a local dev rebuild without pushing:
```bash
docker compose -f docker-compose.dev.yml up -d --build
```

Saved config lives in the named Docker volume and is preserved across rebuilds.
