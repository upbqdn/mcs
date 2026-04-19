# Mining Control System

Mining Control System (MCS) is a small Python service that:

- polls miner temperatures over HTTP,
- reads a power-meter tariff signal from GPIO,
- toggles a relay (fan/valve) based on temperature thresholds.

## What it does

The main loop (once per second):

1. fetches temperatures from configured miners,
2. takes the highest temperature,
3. starts/stops the relay with hysteresis:
   - start when temperature is above `upper_temp_threshold` (`106`),
   - stop when temperature is below `lower_temp_threshold` (`102`),
4. refreshes the meter state (`LOW`/`HIGH` tariff) from GPIO.

## Requirements

- Linux system with GPIO support (`gpiod` / libgpiod),
- Python 3.11+,
- network access to miners.

Python dependencies are defined in `pyproject.toml`:

- `requests`
- `gpiod`

## Installation

Using Poetry:

```bash
poetry install
```

Or with pip (from repository root):

```bash
pip install .
```

## Run

From the repository root:

```bash
python -m mcs.mcs
```

## Configuration

Current runtime values are hardcoded in `mcs/mcs.py`:

- meter GPIO line: `GPIO23`
- relay GPIO line: `GPIO24`
- miner IPs: `192.168.3.4`, `192.168.3.5`, `192.168.3.6`
- relay thresholds: `102` / `106`
- miner API auth: digest auth user `root`, password `pass`

Update these values in code to match your environment.

## Notes

- If miner temperature retrieval fails for a host, the service logs a warning and continues.
- Initialization failures for GPIO components terminate the process.
- Stop the service with `Ctrl+C`.
