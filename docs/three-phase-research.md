# Three-phase ESA support — research notes

Research done 2026-10-18, before any three-phase hardware was available to test.
Nothing here has been verified on a real three-phase unit. Use it as the input
for a read-only discovery script, then confirm register by register before
changing `web_app.py`.

## Models

| | Single phase (this install) | Three phase |
|---|---|---|
| Model | ESA / GW…-EHA-G20 | GW5K … GW30K-ETA-G20 |
| Serial tag (goodwe lib) | `NAH` ("ESA/EHA-G20") | not listed in the lib |
| Battery voltage | ~400 V (observed 406 V) | 700–950 V |
| PV inputs (MPPTs) | 2 | 3 (5–8 kW), 4 (9.999–30 kW) |
| Battery capacity | — | 5–108 kWh, stackable modules |

Both are in GoodWe's ET hybrid family, which shares one register map.

## Connection

Expected to be the same as the single-phase unit:

- Modbus TCP, port 502, slave ID 247.
- The dongle allows **one Modbus TCP client at a time**. Stop the monitor (or
  press Pause) and close SolarGo before probing.
- Community reports for G20 dongles (unconfirmed):
  - LAN is more reliable than Wi-Fi for Modbus TCP.
  - "WLAN Control" may need enabling in SolarGo.
  - Register 47040 is mentioned in relation to Modbus over Wi-Fi.
- The Home Assistant core integration (UDP 8899) does not connect to a
  GW12K-ETA-G20 (HA issue #176918, unresolved).

## What would break in the current code

1. **Battery voltage filtered out.** `read_inverter_data` drops `vbattery` above
   600 V; the ETA battery runs at 700–950 V. The bounds need widening (or
   making model-dependent).
2. **PV and house load under-reported.** Only `ppv1` (35105) and `ppv2` (35109)
   are read. The ETA adds `ppv3` (35113) and `ppv4` (35117). House load is
   derived as `pbattery + ppv1 + ppv2 - ac_active_power`, so it is wrong too.
3. **Settings registers unverified.** 47120 / 47510 / 47760 are the same in the
   goodwe library across the ET family, but need confirming. Also unknown:
   whether 47510 limits **total** export or **per-phase** export.

Expected to work unchanged: SoC (37007), BMS temperature (37003), battery
V/I/P (35180–35182), grid total (35139–35140), daily energy (35193, 35208,
35211).

## Phase detection

- **35002** — AC output type: `0` single phase, `1` three phase 4-wire,
  `2` three phase 3-wire.
- **35003+** — serial number (ASCII, 8 registers). The goodwe library matches
  serial substrings for model features (single phase, 4 MPPT, 2 batteries).

The app could read 35002 at startup and enable the three-phase view only when
it is non-zero.

## Register map to probe (from goodwe `et.py`)

Sizes: "16" = one register, "32" = two registers (high word first). "s" = signed.

### PV

| Register | Name | Size |
|---|---|---|
| 35103 / 35104 / 35105 | vpv1 / ipv1 / ppv1 | 16 / 16 / 32 |
| 35107 / 35108 / 35109 | vpv2 / ipv2 / ppv2 | 16 / 16 / 32 |
| 35111 / 35112 / 35113 | vpv3 / ipv3 / ppv3 | 16 / 16 / 32 |
| 35115 / 35116 / 35117 | vpv4 / ipv4 / ppv4 | 16 / 16 / 32 |
| 35119 | pv3_mode (low byte) / pv4_mode (high byte) | 16 |
| 35120 | pv1_mode (low byte) / pv2_mode (high byte) | 16 |
| 35301 | ppv_total (extended MPPT block) | 32 |
| 35337–35344 | pmppt1–8 (larger models) | 16 |

### Inverter AC output, per phase

| Phase | V | A | Hz | W |
|---|---|---|---|---|
| L1 | 35121 | 35122 | 35123 | 35125 (32s) |
| L2 | 35126 | 35127 | 35128 | 35130 (32s) |
| L3 | 35131 | 35132 | 35133 | 35135 (32s) |

- 35138 — total inverter power
- 35140 — active power (grid, signed); the app reads 35139–35140 as 32-bit

### Backup output, per phase

| Phase | V | A | Hz | Mode | W |
|---|---|---|---|---|---|
| L1 | 35145 | 35146 | 35147 | 35148 | 35150 (32s) |
| L2 | 35151 | 35152 | 35153 | 35154 | 35156 (32s) |
| L3 | 35157 | 35158 | 35159 | 35160 | 35162 (32s) |

- 35170 — backup total

### Load, per phase

| Register | Name | Size |
|---|---|---|
| 35164 | load_p1 | 32s |
| 35166 | load_p2 | 32s |
| 35168 | load_p3 | 32s |
| 35172 | load_ptotal | 32s |

Note: commit `50c85d0` switched this app to a derived house load because the
inverter's own load reading was unreliable on the single-phase ESA. Compare
these registers against a derived per-phase load before trusting them:
`load_pN ≈ inverter_pN − meter_pN` (sign conventions to be confirmed).

### Meter (grid connection point), per phase

| Register | Name | Size |
|---|---|---|
| 36005 / 36006 / 36007 | active_power1 / 2 / 3 | 16s |
| 36008 | active_power_total | 16s |
| 36019 / 36021 / 36023 | meter_active_power1 / 2 / 3 | 32s |
| 36052 / 36053 / 36054 | meter_voltage1 / 2 / 3 | 16 |
| 36055 / 36056 / 36057 | meter_current1 / 2 / 3 | 16 |

The goodwe library treats the 36045+ range as optional (removed if the read
fails).

### Temperatures

| Register | Name |
|---|---|
| 35174 | temperature_air |
| 35175 | temperature_module |
| 35176 | temperature (radiator) |

### Battery

| Register | Name |
|---|---|
| 35180 / 35181 / 35182 | vbattery1 / ibattery1 / pbattery1 (32s) |
| 35262 / 35263 / 35264 | vbattery2 / ibattery2 / pbattery2 (larger models) |
| 37003 | BMS temperature (×0.1 °C) |
| 37007 | SoC (%) |

### Settings (read only during discovery — do not write)

| Register | Name | Notes |
|---|---|---|
| 47120 | meter_target_power_offset | signed W |
| 47509 | grid_export | export limit enable flag |
| 47510 | grid_export_limit | W — total or per phase? |
| 47500 | battery_soc_protection | |
| 47760 | soc_upper_limit | % — confirmed on single phase via test value 89 |
| 45356 | battery_discharge_depth | % |

Nearby values seen on the single-phase unit (47755–47761) are recorded in the
project memory; compare them on the three-phase unit.

## Discovery script plan

Goal: a **read-only** script (extend `probe_registers.py` or a new
`discover_three_phase.py`) that produces a report we can check against SolarGo
and SEMS+ before touching `web_app.py`.

1. **Preflight.** Confirm port 502 is open. Warn if the monitor is running and
   ask for it to be paused or stopped. Use short timeouts (3 s, 1 retry), and on
   a dropped connection reconnect and continue rather than abort.
2. **Identify.** Read 35002 (phase type) and 35003+ (serial, ASCII), plus the
   firmware registers in the 35000 block. Print model and phase count.
3. **Dump known blocks** with raw and decoded values side by side:
   35100–35180, 35260–35270, 35300–35352, 36000–36060, 37000–37010.
   Decode using the sizes and signs above, and flag values that fail sanity
   checks (for example, voltage outside 180–280 V on a phase).
4. **Cross-check the maths.**
   - Σ ppv1..4 vs 35301 ppv_total.
   - Σ meter phases vs 36008 / 35140.
   - Σ load_p1..3 vs 35172, and vs the derived load.
   - Battery voltage in the 700–950 V range.
5. **Take snapshots under different conditions.** Run 3–5 times a few seconds
   apart, and ideally while a known single-phase load (kettle, EV charger) is
   switched on and off. That confirms which phase is which and the sign
   conventions.
6. **Read the settings.** Read the settings registers and print them next to
   blanks for the SolarGo values. Then use the test-value technique: set an
   unusual value in SolarGo (for example, export limit 4321), then scan for it
   with `probe_registers.py --target 4321`. This also answers the
   total-vs-per-phase export limit question if SolarGo shows the setting per
   phase.
7. **Output.** Write a markdown or JSON report to share back, then decide
   which registers go into `MONITOR_REGISTERS`.

## Ideas once the map is confirmed

- **Auto-detect phase count** (35002) and show per-phase rows only on
  three-phase.
- **Per-phase grid import/export.** With a heavy single-phase load you can
  import on one phase while exporting on another. Australian meters usually
  net the phases together, so this guides where to put large loads.
- **Per-phase voltage logging.** Inverters back off or trip above ~253 V,
  often on one phase only. A log explains curtailment and is evidence for the
  network provider.
- **Phase imbalance alert**, especially when running on backup.
- **Per-string PV** (3–4 MPPTs) to spot shading or a failing string.
- **Inverter temperatures** on the larger models in summer.
- **Second battery block** on the larger models.

## Open questions

- Does 47510 limit total export or per-phase export on the ETA?
- Are 35164–35172 (load) trustworthy, or is a derived load better (as it was
  on the single-phase unit)?
- What serial-number tag does the ETA-G20 use?
- Is 36005–36007 or 36019–36023 the better per-phase meter source?
- Does Modbus TCP over the dongle need LAN, or "WLAN Control" in SolarGo?

## Sources

- goodwe library, ET register map: https://github.com/marcelblijleven/goodwe/blob/master/goodwe/et.py
- goodwe library, model detection: https://github.com/marcelblijleven/goodwe/blob/master/goodwe/model.py
- GoodWe ESA Series 5–30 kW (three-phase): https://en.goodwe.com/esa-5-30kw
- ESA three-phase datasheet: https://en.goodwe.com/Ftp/EN/Downloads/Datasheet/GW_ESA-5-30kW_Datasheet-EN.pdf
- pv magazine, CEC approval: https://www.pv-magazine-australia.com/press-releases/goodwe-three-phase-esa-all-in-one-energy-storage-system-secures-cec-approval/
- Home Assistant core issue #176918 (GW12K-ETA-G20): https://github.com/home-assistant/core/issues/176918
- haids3/ha-goodwe-local (ESA-focused HA integration): https://github.com/haids3/ha-goodwe-local
- HA community, ESA not connecting: https://community.home-assistant.io/t/goodwe-inverter-esa-not-connecting/999501
- HA community, GW9.999K-EHA-G20: https://community.home-assistant.io/t/goodwe-gw9-999k-eha-g20-and-goodwe-gw8-3-bat-d-g21/957265
