#!/usr/bin/env python3
"""Mining Control System (MCS).

Polls miner temperatures over HTTP and controls a relay (cooling fan/valve)
based on configurable temperature thresholds.  A second GPIO line is read to
track the current electricity-tariff state (low / high) reported by an energy
meter.

Main loop (1 s period):
1. Fetch temperature readings from each configured miner host.
2. Drive the relay with hysteresis:
   - turn ON  when ``max_temp > upper_temp_threshold``,
   - turn OFF when ``max_temp < lower_temp_threshold``.
3. Refresh the tariff-meter state.
"""

import os
import sys
import logging as log

from time import sleep
from datetime import datetime
from enum import Enum, auto
from typing import Any, Optional, Self

import requests
from requests.auth import HTTPDigestAuth

import gpiod
from gpiod.line import Direction, Value


def flatten(array: list[list[Any]]) -> list[Any]:
    """Flatten a list of lists into a single list.

    Args:
        array: A list whose elements are themselves lists.

    Returns:
        A new list containing every element of every sub-list in order.
    """
    return [e for sub_array in array for e in sub_array]


def temps_for_miners(ips: list[str]) -> list[list[int]]:
    """Retrieve temperature readings from a collection of miner hosts.

    For each host the miner CGI stats endpoint is queried.  Hosts that are
    unreachable or return an unexpected response are skipped with a warning so
    that one faulty miner does not block the others.

    Args:
        ips: List of IPv4 addresses (or hostnames) of the miner hosts.

    Returns:
        A list of per-host temperature lists.  Each inner list contains four
        integer temperature values (``temp2_1`` … ``temp2_4``) as reported by
        the miner's ``STATS`` response.  Hosts that could not be queried are
        omitted from the result.
    """
    def temps_for_miner(hostname: str) -> list[int]:
        """Fetch the four temperature sensors from a single miner host.

        Args:
            hostname: IP address or hostname of the miner.

        Returns:
            List of four integer temperature values.

        Raises:
            requests.RequestException: On network or HTTP errors.
            KeyError: If the response JSON does not match the expected schema.
        """
        response = requests.post(
            "http://" + hostname + "/cgi-bin/miner_stats.cgi",
            auth=HTTPDigestAuth("root", "pass"),
        ).json()

        return [response["STATS"][1]["temp2_" + str(i)] for i in range(1, 5)]

    temps = []

    for ip in ips:
        try:
            temps.append(temps_for_miner(ip))
        except Exception:
            log.warning(f"could not retrieve temperatures from {ip}")
            continue

    return temps


class Line:
    """Abstraction over a single named GPIO line.

    Locates the line by name across all gpiochip devices present under
    ``/dev/``, then exposes a simple read/write interface.  Because line names
    are not guaranteed unique across chips, the first matching chip is used.
    """

    def __init__(self, name) -> None:
        """Locate the GPIO line with the given name and store its chip path.

        Args:
            name: GPIO line name as reported by the kernel (e.g. ``"GPIO23"``).

        Raises:
            Exception: If no gpiochip device exposes a line with ``name``.
        """
        def gpio_chips():
            """Yield paths to every gpiochip device found under ``/dev/``."""
            for entry in os.scandir("/dev/"):
                if gpiod.is_gpiochip_device(entry.path):
                    yield entry.path

        # Names are not guaranteed unique, so this finds the first line with
        # the given name.
        for path in gpio_chips():
            with gpiod.Chip(path) as chip:
                try:
                    self.offset = chip.line_offset_from_id(name)
                    self.chip_path = "/dev/" + chip.get_info().name
                    return
                except OSError:
                    continue

        raise Exception("gpio line '{}' not found".format(name))

    def value(self, value: Optional[Value] = None) -> Value:
        """Read or write the GPIO line value.

        When called without arguments the line is configured as an input and
        its current logical value is returned.  When a value is supplied the
        line is configured as an output, driven to that value, and the same
        value is returned.

        Args:
            value: ``Value.ACTIVE`` or ``Value.INACTIVE`` to drive the line as
                an output.  Omit (or pass ``None``) to read the current value.

        Returns:
            The logical value of the line after the operation.

        Raises:
            Exception: Propagates any exception raised by ``gpiod``.
        """
        direction = Direction.INPUT if value is None else Direction.OUTPUT
        config = {self.offset: gpiod.LineSettings(direction)}

        try:
            with gpiod.request_lines(self.chip_path, config) as line:
                if value is None:
                    return line.get_value(self.offset)
                else:
                    line.set_value(self.offset, value)
                    return value
        except Exception as e:
            raise e


class Meter:
    """Monitors the electricity-tariff signal from an energy meter via GPIO.

    The meter connects its tariff output to a GPIO input line.  A logic-high
    reading means the meter contact is open (``State.LOW`` tariff); logic-low
    means the contact is closed (``State.HIGH`` tariff).  ``update()`` must
    be called periodically to refresh the cached state and emit change events
    to the log.
    """

    class State(Enum):
        """Electricity tariff price level reported by the meter."""

        HIGH = auto()
        LOW = auto()

    def _is_open(self) -> bool:
        """Return ``True`` if the meter contact is currently open (logic-high).

        Raises:
            Exception: Re-raises any GPIO read error after logging a warning.
        """
        try:
            return bool(self.line.value())
        except Exception:
            log.warning("could not check if the meter is closed or open")
            raise

    def _new_state(self) -> State:
        """Derive the current :class:`State` from the GPIO reading.

        Returns:
            ``State.LOW`` when the contact is open (logic-high),
            ``State.HIGH`` when the contact is closed (logic-low).
        """
        return self.State.LOW if self._is_open() else self.State.HIGH

    def __init__(self, line: Line) -> None:
        """Initialise the meter and read the initial tariff state.

        Args:
            line: The GPIO :class:`Line` connected to the meter's tariff output.

        Raises:
            Exception: If the initial GPIO read fails.
        """
        self.line = line
        self.state = None

        try:
            self.state = self._new_state()
        except Exception:
            log.error("could not initialize the meter status")
            raise

        log.info("initialized the meter")

        match self.state:
            case self.State.LOW:
                log.info("initial price rate is low")
            case self.State.HIGH:
                log.info("initial price rate is high")

    def update(self) -> None:
        """Refresh the tariff state and log any transition.

        If the GPIO read fails the cached state is left unchanged and a warning
        is logged.  Only transitions (state changes) are logged at INFO level.
        """
        new_state = None

        try:
            new_state = self._new_state()
        except Exception:
            log.warning("could not update the meter status")

        if self.state == new_state or new_state is None:
            return

        match new_state:
            case self.State.LOW:
                log.info("price rate changed to low")
            case self.State.HIGH:
                log.info("price rate changed to high")

        self.state = new_state


class Relay:
    """Controls a relay that drives a cooling fan and water valve via GPIO.

    The relay is managed with hysteresis: it turns on when the temperature
    rises above :attr:`upper_temp_threshold` and turns off only when the
    temperature drops below :attr:`lower_temp_threshold`.  The GPIO line is
    driven ``ACTIVE`` (relay energised / fan+valve on) or ``INACTIVE``
    (de-energised / fan+valve off).
    """

    class State(Enum):
        """Relay (and therefore fan+valve) energisation state."""

        ON = auto()
        OFF = auto()

    def __init__(self, line: Line, lower_threshold: int, upper_threshold: int):
        """Initialise the relay controller and ensure the relay is off.

        Args:
            line: The GPIO :class:`Line` connected to the relay coil.
            lower_threshold: Temperature (°C) below which the relay is turned
                off when it is currently on.
            upper_threshold: Temperature (°C) above which the relay is turned
                on when it is currently off.

        Raises:
            Exception: If the initial GPIO write to de-energise the relay fails.
        """
        self.line = line
        self.lower_temp_threshold = lower_threshold
        self.upper_temp_threshold = upper_threshold
        self.state = self.State.OFF

        try:
            self.line.value(Value.INACTIVE)
        except Exception:
            log.error("could not initialize the relay controller")
            raise

        log.info("initialized the relay controller")

    def is_on(self) -> bool:
        """Return ``True`` if the relay is currently energised (fan+valve on)."""
        return self.state == self.State.ON

    def is_off(self) -> bool:
        """Return ``True`` if the relay is currently de-energised (fan+valve off)."""
        return self.state == self.State.OFF

    def start(self):
        """Energise the relay, opening the valve and starting the fan.

        On GPIO failure the error is logged and the state is left unchanged.
        """
        try:
            self.line.value(Value.ACTIVE)
        except Exception:
            log.error("could not open the valve and start the fan")
            return

        self.state = self.State.ON

        log.info("opening valve and starting fan")

    def stop(self):
        """De-energise the relay, closing the valve and stopping the fan.

        On GPIO failure the error is logged and the state is left unchanged.
        """
        try:
            self.line.value(Value.INACTIVE)
        except Exception:
            log.error("could not close the valve and stop the fan")
            return

        self.state = self.State.OFF

        log.info("closing valve and stopping fan")

    def control(self, temp: int):
        """Apply hysteresis control based on the supplied temperature.

        The relay is turned on when *temp* exceeds :attr:`upper_temp_threshold`
        and the relay is currently off.  The relay is turned off when *temp*
        falls below :attr:`lower_temp_threshold` and the relay is currently on.
        No action is taken when *temp* is within the hysteresis band.

        Args:
            temp: Current temperature in the same units as the thresholds
                (miner-reported values, typically °C).
        """
        if temp > self.upper_temp_threshold and self.is_off():
            self.start()
        elif temp < self.lower_temp_threshold and self.is_on():
            self.stop()


# # Main Logic


def main():
    """Entry point for the Mining Control System.

    Configures logging, initialises GPIO peripherals, then runs the main
    control loop until interrupted.  The loop:

    1. Fetches temperatures from all configured miners (``192.168.3.4–6``).
    2. Drives the relay controller with the highest observed temperature.
    3. Updates the electricity-tariff meter state.

    Exits with code 0 on :exc:`KeyboardInterrupt` or on a fatal initialisation
    failure.  Unexpected per-iteration exceptions are logged and the loop
    continues.
    """
    log.basicConfig(
        level=log.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
        # format="%(asctime)s %(levelname)s %(message)s",
        format="%(levelname)s %(message)s",
    )

    meter = None
    relay = None

    try:
        meter = Meter(Line("GPIO23"))
        relay = Relay(Line("GPIO24"), 102, 106)
    except Exception as e:
        log.error("could not initialize the mining control system")
        log.exception(e)
        sys.exit(0)

    while True:
        try:
            # Retrieve the temperatures
            ips = ["192.168.3." + last_octet for last_octet in ["4", "5", "6"]]
            temps = temps_for_miners(ips)

            # Control the relay.
            relay.control(max(flatten(temps)))

            # Update the meter status.
            meter.update()

            sleep(1)

        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            log.error("unexpected exception, maintenance required")


if __name__ == "__main__":
    main()
