"""Tests for the control cycle's behaviour when miners do not answer.

The regression these cover: `max()` of an empty list raised before `meter.update()`,
so while every miner was unreachable the relay was left alone (correct) but metering
silently stopped (not correct). The HTTP call also had no timeout, so "unreachable"
could mean "blocks indefinitely" rather than "raises".
"""

import unittest
from unittest import mock

from mcs import mcs


class FakeRelay:
    def __init__(self):
        self.controlled_with = []

    def control(self, temp):
        self.controlled_with.append(temp)


class FakeMeter:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


class ControlFromTemps(unittest.TestCase):
    def test_drives_the_relay_with_the_hottest_reading(self):
        relay = FakeRelay()
        self.assertTrue(mcs.control_from_temps(relay, [[70, 81], [65]]))
        self.assertEqual(relay.controlled_with, [81])

    def test_no_readings_leaves_the_relay_untouched(self):
        relay = FakeRelay()
        self.assertFalse(mcs.control_from_temps(relay, []))
        self.assertEqual(relay.controlled_with, [])

    def test_readings_present_but_all_empty_is_also_no_readings(self):
        relay = FakeRelay()
        self.assertFalse(mcs.control_from_temps(relay, [[], []]))
        self.assertEqual(relay.controlled_with, [])


class Step(unittest.TestCase):
    def test_meter_is_updated_when_no_miner_answers(self):
        relay, meter = FakeRelay(), FakeMeter()
        with mock.patch.object(mcs, "temps_for_miners", return_value=[]):
            mcs.step(meter, relay, ["192.0.2.1"])
        self.assertEqual(meter.updates, 1, "metering must continue while miners are down")
        self.assertEqual(relay.controlled_with, [])

    def test_meter_is_updated_on_a_normal_cycle(self):
        relay, meter = FakeRelay(), FakeMeter()
        with mock.patch.object(mcs, "temps_for_miners", return_value=[[100, 107]]):
            mcs.step(meter, relay, ["192.0.2.1"])
        self.assertEqual(meter.updates, 1)
        self.assertEqual(relay.controlled_with, [107])

    def test_a_failing_meter_does_not_hide_that_the_relay_already_ran(self):
        relay, meter = FakeRelay(), mock.Mock()
        meter.update.side_effect = RuntimeError("gpio read failed")
        with mock.patch.object(mcs, "temps_for_miners", return_value=[[110]]):
            with self.assertRaises(RuntimeError):
                mcs.step(meter, relay, ["192.0.2.1"])
        self.assertEqual(relay.controlled_with, [110])


class MinerRequests(unittest.TestCase):
    def test_every_request_is_bounded_by_a_timeout(self):
        """A miner that accepts the connection and never replies must not wedge the loop."""
        with mock.patch.object(mcs.requests, "post") as post:
            post.return_value.json.return_value = {
                "STATS": [None, {f"temp2_{i}": 60 + i for i in range(1, 5)}]
            }
            mcs.temps_for_miners(["192.0.2.1"])
        self.assertEqual(post.call_count, 1)
        timeout = post.call_args.kwargs.get("timeout")
        self.assertIsNotNone(timeout, "requests.post must pass an explicit timeout")
        connect, read = timeout
        self.assertLessEqual(connect, 5, "connect timeout should be short on a LAN")
        self.assertLessEqual(read, 30)

    def test_one_unreachable_miner_does_not_lose_the_others(self):
        def answer(url, *_args, **_kwargs):
            if "192.0.2.9" in url:
                raise mcs.requests.exceptions.ConnectTimeout("unreachable")
            reply = mock.Mock()
            reply.json.return_value = {
                "STATS": [None, {f"temp2_{i}": 70 for i in range(1, 5)}]
            }
            return reply

        with mock.patch.object(mcs.requests, "post", side_effect=answer):
            temps = mcs.temps_for_miners(["192.0.2.9", "192.0.2.10"])
        self.assertEqual(len(temps), 1, "the reachable miner's readings must survive")


if __name__ == "__main__":
    unittest.main()
