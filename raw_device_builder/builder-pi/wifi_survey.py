#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Rank the 2.4 GHz channels for the workshop AP from `iw` scan output.

Run by wifi-survey.sh, which does the radio work and pipes the results here
on stdin. Pure parsing and arithmetic: no radios, no subprocesses.

Input: the text of one or more `iw dev <if> scan` runs, optionally followed by
`iw dev <if> survey dump`, each section introduced by a marker line:

    ### SCAN wlan0
    <iw dev wlan0 scan output>
    ### SURVEY wlan1
    <iw dev wlan1 survey dump output>

Method. Every 2.4 GHz network heard (merged across scans by BSSID, keeping its
strongest signal) adds interference to each candidate channel in proportion to
its signal power (mW) and to how much of its spectrum overlaps ours (a 20 MHz
channel spans 4 channel numbers; a 40 MHz network spans 8, and spreads the same
power over twice the width). The channel with the least total interference wins;
within 3 dB, the one with the lower measured airtime (survey busy %, when the
radio reports it) wins, then 11, 1, 6 (6 is the most common factory default).

Only 1, 6 and 11 are candidates by default: they are the non-overlapping 20 MHz
channels legal in both the US and Europe.

Output: a human-readable table, then machine-readable lines for scripts:
    RECOMMENDED_CHANNEL=<n>
    RECOMMENDED_PAIR=<a>,<b>
Exit status: 0 with a recommendation, 3 if no scan data was parsed.
"""
import argparse
import math
import re
import sys

CANDIDATES_DEFAULT = (1, 6, 11)
TIE_DB = 3.0                      # interference levels this close count as equal
PREFERENCE = {11: 0, 1: 1, 6: 2}  # final tie-break order
CLEAN_DBM = -85.0                 # below this: clean
BUSY_DBM = -70.0                  # above this: busy, warn the operator

_BSS = re.compile(r"^BSS ([0-9a-fA-F:]{17})")
_FREQ = re.compile(r"^\s*freq:\s*([0-9.]+)")
_SIGNAL = re.compile(r"^\s*signal:\s*(-?[0-9.]+)\s*dBm")
_SSID = re.compile(r"^\s*SSID:\s?(.*)$")
_SECONDARY = re.compile(r"secondary channel offset:\s*(above|below|no secondary)")
_SURVEY_FREQ = re.compile(r"^\s*frequency:\s*([0-9]+)\s*MHz")
_ACTIVE = re.compile(r"channel active time:\s*([0-9]+)\s*ms")
_BUSY = re.compile(r"channel busy time:\s*([0-9]+)\s*ms")


def freq_to_channel(freq_mhz):
    """2.4 GHz frequency in MHz -> channel number, or None outside 2.4 GHz."""
    f = int(round(float(freq_mhz)))
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    return None


def parse_scans(text):
    """Parse every `### SCAN` section. Returns {bssid: network}, merged across
    scans by BSSID (strongest signal kept). Non-2.4 GHz networks are dropped."""
    networks = {}
    section = None
    cur = None

    def flush():
        if cur and cur.get("channel") is not None and cur.get("signal") is not None:
            prev = networks.get(cur["bssid"])
            if prev is None or cur["signal"] > prev["signal"]:
                networks[cur["bssid"]] = dict(cur)

    for line in text.splitlines():
        if line.startswith("### "):
            flush()
            cur = None
            section = line.split()[1] if len(line.split()) > 1 else None
            continue
        if section != "SCAN":
            continue
        m = _BSS.match(line)
        if m:
            flush()
            cur = {"bssid": m.group(1).lower(), "ssid": "", "channel": None,
                   "signal": None, "width": 20, "secondary": None}
            continue
        if cur is None:
            continue
        if (m := _FREQ.match(line)):
            cur["channel"] = freq_to_channel(m.group(1))
        elif (m := _SIGNAL.match(line)):
            cur["signal"] = float(m.group(1))
        elif (m := _SSID.match(line)):
            cur["ssid"] = m.group(1).strip().replace("\\x00", "")
        elif (m := _SECONDARY.search(line)):
            if m.group(1) in ("above", "below"):
                cur["width"] = 40
                cur["secondary"] = m.group(1)
    flush()
    return networks


def parse_survey(text):
    """Parse `### SURVEY` sections into {channel: busy fraction}, summing
    active/busy time across radios. Channels with no active time are skipped."""
    totals = {}
    section = None
    chan = None
    for line in text.splitlines():
        if line.startswith("### "):
            section = line.split()[1] if len(line.split()) > 1 else None
            chan = None
            continue
        if section != "SURVEY":
            continue
        if line.startswith("Survey data from"):
            chan = None
            continue
        if (m := _SURVEY_FREQ.match(line)):
            chan = freq_to_channel(m.group(1))
        elif chan is not None and (m := _ACTIVE.search(line)):
            totals.setdefault(chan, [0, 0])[0] += int(m.group(1))
        elif chan is not None and (m := _BUSY.search(line)):
            totals.setdefault(chan, [0, 0])[1] += int(m.group(1))
    return {c: busy / active for c, (active, busy) in totals.items() if active > 0}


def spectrum(net):
    """(centre, width) of a network in channel-number units (5 MHz each)."""
    centre = net["channel"]
    if net["width"] == 40:
        centre += 2 if net["secondary"] == "above" else -2
        return centre, 8
    return centre, 4


def overlap_fraction(candidate, net):
    """Fraction of `net`'s power that falls inside a 20 MHz AP on `candidate`."""
    centre, width = spectrum(net)
    lo = max(candidate - 2, centre - width / 2)
    hi = min(candidate + 2, centre + width / 2)
    return max(0.0, hi - lo) / width


def score_channels(networks, candidates):
    """Per candidate: interference (dBm, or None if nothing overlaps), the
    number of overlapping networks and the strongest of them."""
    out = {}
    for c in candidates:
        mw, count, strongest = 0.0, 0, None
        for net in networks.values():
            frac = overlap_fraction(c, net)
            if frac <= 0:
                continue
            mw += frac * 10 ** (net["signal"] / 10)
            count += 1
            if strongest is None or net["signal"] > strongest["signal"]:
                strongest = net
        out[c] = {"dbm": 10 * math.log10(mw) if mw > 0 else None,
                  "count": count, "strongest": strongest}
    return out


def rank(scores, busy, candidates):
    """Candidates, best first. Repeatedly: take the least-interfered level left,
    treat every channel within TIE_DB of it as tied (channels with no
    interference tie only with each other), and pick from the tie the lowest
    airtime, then PREFERENCE."""
    def level(c):
        return scores[c]["dbm"]

    remaining, order = list(candidates), []
    while remaining:
        clear = [c for c in remaining if level(c) is None]
        if clear:
            tied = clear
        else:
            low = min(level(c) for c in remaining)
            tied = [c for c in remaining if level(c) - low <= TIE_DB]
        pick = min(tied, key=lambda c: (busy.get(c, 0.0), PREFERENCE.get(c, 9), c))
        order.append(pick)
        remaining.remove(pick)
    return order


def verdict(dbm):
    if dbm is None or dbm < CLEAN_DBM:
        return "clean"
    if dbm <= BUSY_DBM:
        return "moderate"
    return "busy"


def report(networks, busy, candidates, excluded, out=None):
    out = out or sys.stdout   # resolved per call, not frozen at import
    scores = score_channels(networks, candidates)
    order = rank(scores, busy, candidates)
    p = lambda *a: print(*a, file=out)
    p(f"2.4 GHz networks heard: {len(networks)}"
      + (f"  (excluded {excluded}: this Pi's own AP)" if excluded else ""))
    top = sorted(networks.values(), key=lambda n: -n["signal"])[:10]
    if top:
        p("Strongest networks:")
        for n in top:
            w = f"40 MHz {n['secondary']}" if n["width"] == 40 else "20 MHz"
            p(f"  ch {n['channel']:>2}  {n['signal']:6.1f} dBm  {w:<15} {n['ssid'] or '<hidden>'}")
    p("")
    p("channel  interference   networks  strongest overlapping          airtime busy*")
    for c in candidates:
        s = scores[c]
        lvl = "none" if s["dbm"] is None else f"{s['dbm']:6.1f} dBm"
        st = "-"
        if s["strongest"]:
            st = f"{s['strongest']['ssid'] or '<hidden>'} (ch {s['strongest']['channel']}, {s['strongest']['signal']:.0f})"
        b = f"{100 * busy[c]:.0f}%" if c in busy else "n/a"
        p(f"  {c:>2}     {lvl:<13}  {s['count']:>8}  {st[:30]:<30}  {b}")
    p("  * sampled during the scan only, so rough; n/a when the radio does not report it")
    best, pair = order[0], sorted(order[:2])
    v = verdict(scores[best]["dbm"])
    p("")
    p(f"Recommended channel: {best} ({v})")
    p(f"Recommended pair for two APs: {pair[0]} + {pair[1]}")
    if v == "busy":
        p("WARNING: even the best channel is busy. Expect retries; keep devices close")
        p("         to the Pi, and consider moving the Pi away from the strongest APs.")
    p(f"RECOMMENDED_CHANNEL={best}")
    p(f"RECOMMENDED_PAIR={pair[0]},{pair[1]}")
    return best, pair


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rank 2.4 GHz channels from iw scan output on stdin.")
    ap.add_argument("--exclude-ssid", action="append", default=[],
                    help="SSID to ignore (this Pi's own AP); repeatable")
    ap.add_argument("--exclude-bssid", action="append", default=[],
                    help="BSSID to ignore (this Pi's own radios); repeatable")
    ap.add_argument("--channels", default=",".join(map(str, CANDIDATES_DEFAULT)),
                    help="candidate channels, comma separated (default 1,6,11)")
    args = ap.parse_args(argv)
    candidates = tuple(int(c) for c in args.channels.split(",") if c.strip())
    text = sys.stdin.read()
    networks = parse_scans(text)
    skip_b = {b.lower() for b in args.exclude_bssid if b}
    skip_s = {s for s in args.exclude_ssid if s}
    kept = {b: n for b, n in networks.items() if b not in skip_b and n["ssid"] not in skip_s}
    if not networks and "### SCAN" not in text:
        print("ERROR: no scan data on stdin", file=sys.stderr)
        return 3
    report(kept, parse_survey(text), candidates, len(networks) - len(kept))
    return 0


if __name__ == "__main__":
    sys.exit(main())
