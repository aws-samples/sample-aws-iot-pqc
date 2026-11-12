# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the venue WiFi survey analyser (wifi_survey.py): iw scan and
survey parsing, the overlap model, and the channel recommendation."""
import io
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import wifi_survey as ws  # noqa: E402


def bss(mac, freq, signal, ssid, secondary=None, assoc=False):
    """One BSS block in the shape `iw dev wlan0 scan` prints."""
    lines = [f"BSS {mac}(on wlan0)" + (" -- associated" if assoc else ""),
             "\tlast seen: 120 ms ago",
             f"\tfreq: {freq}",
             "\tbeacon interval: 100 TUs",
             f"\tsignal: {signal:.2f} dBm",
             f"\tSSID: {ssid}"]
    if secondary:
        lines += ["\tHT operation:",
                  f"\t\t * primary channel: {ws.freq_to_channel(freq)}",
                  f"\t\t * secondary channel offset: {secondary}",
                  "\t\t * STA channel width: any"]
    return "\n".join(lines) + "\n"


def scan(*blocks, iface="wlan0"):
    return f"### SCAN {iface}\n" + "".join(blocks)


# The room measured on Nov 10-11: a home router on ch 1 and ch 4, our own AP on
# ch 11 (to be excluded), plus a 5 GHz uplink network that must be ignored.
HOME = (
    scan(bss("aa:00:00:00:00:01", "2412.0", -59, "clarmouth-4-24"),
         bss("aa:00:00:00:00:02", "2427.0", -52, "clarmouth-4"),
         bss("aa:00:00:00:00:03", "2427.0", -52, "clarmouth-guest"),
         bss("d8:3a:dd:00:00:0b", "2462.0", -18, "reinvent-pqc-iot-wifi"),
         bss("aa:00:00:00:00:05", "5520.0", -54, "clarmouth-4", assoc=True))
    # A second scan hears the ch 1 network louder: the strongest is kept.
    + scan(bss("aa:00:00:00:00:01", "2412", -57, "clarmouth-4-24"))
)


@pytest.mark.parametrize("freq,chan", [
    ("2412", 1), ("2437.0", 6), ("2462", 11), ("2472", 13), ("2484", 14),
    ("5180", None), ("5520.0", None)])
def test_freq_to_channel(freq, chan):
    assert ws.freq_to_channel(freq) == chan


def test_parse_merges_by_bssid_and_drops_5ghz():
    nets = ws.parse_scans(HOME)
    assert set(nets) == {"aa:00:00:00:00:01", "aa:00:00:00:00:02",
                         "aa:00:00:00:00:03", "d8:3a:dd:00:00:0b"}
    assert nets["aa:00:00:00:00:01"]["signal"] == -57.0   # strongest of two scans
    assert nets["aa:00:00:00:00:02"]["channel"] == 4


def test_parse_40mhz():
    nets = ws.parse_scans(scan(bss("bb:00:00:00:00:01", "2412", -60, "wide", secondary="above")))
    n = nets["bb:00:00:00:00:01"]
    assert (n["width"], n["secondary"]) == (40, "above")
    assert ws.spectrum(n) == (3, 8)


@pytest.mark.parametrize("cand,chan,width,secondary,frac", [
    (6, 6, 20, None, 1.0),        # same channel: all of its power
    (6, 1, 20, None, 0.0),        # 1 and 6 do not overlap
    (6, 4, 20, None, 0.5),        # 2 channels apart: half
    (1, 4, 20, None, 0.25),       # 3 channels apart: a quarter
    (1, 1, 40, "above", 0.5),     # 40 MHz on 1+5 spreads half its power into 1
    (6, 1, 40, "above", 0.375),   # ...and some of it into 6
    (11, 1, 40, "above", 0.0),
])
def test_overlap_fraction(cand, chan, width, secondary, frac):
    net = {"channel": chan, "width": width, "secondary": secondary}
    assert ws.overlap_fraction(cand, net) == pytest.approx(frac)


def run(text, *args):
    out = io.StringIO()
    old = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(text), out
    try:
        rc = ws.main(list(args))
    finally:
        sys.stdin, sys.stdout = old
    return rc, out.getvalue()


def lines(out, key):
    return [ln.split("=", 1)[1] for ln in out.splitlines() if ln.startswith(key + "=")]


def test_home_room_recommends_11_and_excludes_own_ap():
    rc, out = run(HOME, "--exclude-ssid", "reinvent-pqc-iot-wifi")
    assert rc == 0
    assert lines(out, "RECOMMENDED_CHANNEL") == ["11"]
    assert lines(out, "RECOMMENDED_PAIR") == ["1,11"]   # 1 (~-53 dBm) beats 6 (~-52) by preference
    assert "excluded 1" in out and "(clean)" in out


def test_own_ap_excluded_by_bssid_case_insensitive():
    rc, out = run(HOME, "--exclude-bssid", "D8:3A:DD:00:00:0B")
    assert lines(out, "RECOMMENDED_CHANNEL") == ["11"]


def test_without_exclusion_our_own_ap_makes_11_busy():
    rc, out = run(HOME)
    assert lines(out, "RECOMMENDED_CHANNEL") != ["11"]


def test_busy_everywhere_warns():
    text = scan(*(bss(f"cc:00:00:00:00:{c:02x}", str(2407 + 5 * c), -40, f"venue-{c}") for c in (1, 6, 11)))
    rc, out = run(text)
    assert rc == 0 and "WARNING: even the best channel is busy" in out


def test_clear_air_ties_break_on_airtime_then_preference():
    survey = ("### SURVEY wlan1\n"
              "Survey data from wlan1\n\tfrequency:\t\t\t2412 MHz\n"
              "\tchannel active time:\t\t200 ms\n\tchannel busy time:\t\t100 ms\n"
              "Survey data from wlan1\n\tfrequency:\t\t\t2437 MHz\n"
              "\tchannel active time:\t\t200 ms\n\tchannel busy time:\t\t20 ms\n"
              "Survey data from wlan1\n\tfrequency:\t\t\t2462 MHz\n"
              "\tchannel active time:\t\t200 ms\n\tchannel busy time:\t\t80 ms\n")
    rc, out = run("### SCAN wlan0\n" + survey)
    assert lines(out, "RECOMMENDED_CHANNEL") == ["6"]      # quietest airtime wins a tie
    assert lines(out, "RECOMMENDED_PAIR") == ["6,11"]
    rc, out = run("### SCAN wlan0\n")                      # no airtime data: preference
    assert lines(out, "RECOMMENDED_CHANNEL") == ["11"]


def test_no_scan_data_is_an_error():
    rc, out = run("")
    assert rc == 3 and "RECOMMENDED_CHANNEL" not in out
