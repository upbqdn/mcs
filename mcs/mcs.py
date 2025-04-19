#!/usr/bin/env python3

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
    return [e for sub_array in array for e in sub_array]


def temps_for_miners(ips: list[str]) -> list[list[int]]:
    def temps_for_miner(hostname: str) -> list[int]:
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
    def __init__(self, name) -> None:
        def gpio_chips():
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
    class State(Enum):
        HIGH = auto()
        LOW = auto()

    def _is_open(self) -> bool:
        try:
            return bool(self.line.value())
        except Exception:
            log.warning("could not check if the meter is closed or open")
            raise

    def _new_state(self) -> State:
        return self.State.LOW if self._is_open() else self.State.HIGH

    def __init__(self, line: Line) -> None:
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
    class State(Enum):
        ON = auto()
        OFF = auto()

    def __init__(self, line: Line, lower_threshold: int, upper_threshold: int):
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
        return self.state == self.State.ON

    def is_off(self) -> bool:
        return self.state == self.State.OFF

    def start(self):
        try:
            self.line.value(Value.ACTIVE)
        except Exception:
            log.error("could not open the valve and start the fan")
            return

        self.state = self.State.ON

        log.info("opening valve and starting fan")

    def stop(self):
        try:
            self.line.value(Value.INACTIVE)
        except Exception:
            log.error("could not close the valve and stop the fan")
            return

        self.state = self.State.OFF

        log.info("closing valve and stopping fan")

    def control(self, temp: int):
        if temp > self.upper_temp_threshold and self.is_off():
            self.start()
        elif temp < self.lower_temp_threshold and self.is_on():
            self.stop()


# # Main Logic


def main():
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
            ips = ["192.168.1." + last_octet for last_octet in ["4", "5", "6"]]
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
