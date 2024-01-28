#!/usr/bin/env python

import sys
from time import sleep
from datetime import datetime
from enum import Enum, auto

import requests
from requests.auth import HTTPDigestAuth

import SUNXI_GPIO as GPIO

BLUE_LED = "/sys/class/leds/cubieboard2:blue:usr/brightness"
GREEN_LED = "/sys/class/leds/cubieboard2:green:usr/brightness"

TARIFF_PIN = GPIO.PD7
FAN_PIN = GPIO.PD3

GPIO.init()
GPIO.setcfg(TARIFF_PIN, GPIO.IN)
GPIO.setcfg(FAN_PIN, GPIO.OUT)


def flatten(array):
    return [e for sub_array in array for e in sub_array]


def printt(s: str):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + f": {s}", flush=True)


class Tariff:
    class State(Enum):
        HIGH = auto()
        LOW = auto()

    def __init__(self):
        self.state = None

    def update(self, tariff: int):
        new_state = self.State.LOW if tariff == 1 else self.State.HIGH

        if self.state == new_state:
            return

        match new_state:
            case self.State.LOW:
                with open(BLUE_LED, "w") as b, open(GREEN_LED, "w") as g:
                    b.write("0")
                    b.flush()
                    g.write("1")
                    g.flush()
                    printt("Tariff changed to low")
            case self.State.HIGH:
                with open(BLUE_LED, "w") as b, open(GREEN_LED, "w") as g:
                    b.write("1")
                    b.flush()
                    g.write("0")
                    g.flush()
                    printt("Tariff changed to high")
            case _:
                pass

        self.state = new_state


class Fan:
    class State(Enum):
        ON = auto()
        OFF = auto()

    def __init__(self):
        self.stop()

    def is_on(self) -> bool:
        return self.state == self.State.ON

    def is_off(self) -> bool:
        return self.state == self.State.OFF

    def start(self):
        self.state = self.State.ON
        GPIO.output(FAN_PIN, GPIO.HIGH)
        printt("Starting fan")

    def stop(self):
        self.state = self.State.OFF
        GPIO.output(FAN_PIN, GPIO.LOW)
        printt("Stopping fan")

    def control(self, temp: int):
        if temp > 107 and not self.is_on():
            self.start()
        elif temp < 103 and not self.is_off():
            self.stop()


def retrieve_temps_for_miner(hostname: str):
    try:
        response = requests.post(
            "http://" + hostname + "/cgi-bin/miner_stats.cgi",
            auth=HTTPDigestAuth("root", "pass"),
        ).json()
    except Exception as e:
        raise e

    return [response["STATS"][1]["temp2_" + str(i)] for i in range(1, 5)]


def blink():
    with open(BLUE_LED, "r+") as b, open(GREEN_LED, "r+") as g:
        b_prev_state = b.read()
        g_prev_state = g.read()
        b.write("0")
        b.flush()
        g.write("0")
        g.flush()
        sleep(1)
        b.write(b_prev_state)
        b.flush()
        g.write(g_prev_state)
        g.flush()


def main():
    tariff = Tariff()
    fan = Fan()

    printt("Mining control system started")

    while True:
        try:
            # Retrieve the temperatures
            ips = ["192.168.5." + last_octet for last_octet in ["5", "6", "7"]]
            temps = []

            for ip in ips:
                try:
                    temps.append(retrieve_temps_for_miner(ip))
                except Exception:
                    printt(f"Could not retrieve the temperatures from {ip}")
                    continue

            # Control the fan
            fan.control(max(flatten(temps)))

            # Handle the tariff
            tariff.update(GPIO.input(TARIFF_PIN))

            # Blink LEDs
            blink()

            sleep(1)

        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
