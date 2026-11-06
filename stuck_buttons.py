"""Stuck-button flags, and the names behind their bits.

The boat runs its own stuck-button detector. A button held for about 20 s (20.6 s
on B15 2026-11-05, 20.8 s on NZL 2026-09-20) raises the global flag and sets that
button's bit in the flag of the panel it sits on, at 1 Hz. So the panel flags say
exactly which button it is, with no need to read the 20 Hz button channels -- and
no channel names the button, so the bits are named here.

How the names were found: each panel's raw bitfield (BUT_CADI_*, BUT_FC_*,
G3K_SWITCHES_*) was matched against the named BTN_* channels over 65 min of B15 on
2026-11-05 and 75 min of NZL at Geneva, keeping matches of 80 % or better. The two
boats agree on all 34 bits they share. The wheel has no BTN_* channels at all, so
its two board bits come from what the boards did: a port or starboard press of
1024 raises that side's board by 1900 mm, and 2048 lowers it (96 presses).

A stuck flag's bit is the raw panel's bit: proved on real faults for the foot
buttons (CADI_P = 4, 2026-11-05) and the wheel (WHEEL_S = 2048, 2026-09-20). The
G3K and pilot panels have never faulted in the data checked, so pairing them with
their raw bitfields rests on the channel names. A bit with no name here still
alarms, as its panel and bit number -- rename it when one turns up.
"""
from __future__ import annotations

import pandas as pd

STUCK_GLOBAL = "STUCK_BUTTON_GLOBAL_unk"
# Up for the moment between the global flag and the panel flags arriving, and for
# any panel this table does not know about.
UNNAMED = "BUTTON STUCK"
# Naming a button costs fourteen more channels on one boat query, so it happens
# once when the flag goes up and then only this often, in case a second follows.
RENAME_S = 30.0

_WT_FOOT, _GD_FOOT = "Wing trimmer foot button", "Grinder foot button"
# The foot unit carries both stations, so it is the one panel naming where per bit.
_FOOT = {1: ("Jib Sheet In", _WT_FOOT), 2: ("Jib Sheet Out", _WT_FOOT),
         4: ("Board Up", _WT_FOOT), 8: ("Twist Ease", _WT_FOOT),
         256: ("Jib Sheet In", _GD_FOOT), 512: ("Jib Sheet Out", _GD_FOOT)}
_WHEEL = {1024: "Board Up", 2048: "Board Down"}

# Flag -> (side, panel, bit names). Port and starboard panels are wired
# differently, so a bit on one side says nothing about the other.
STUCK_PANELS = {
    "CADI_P_STUCK_unk": ("Port", "Foot button", _FOOT),
    "CADI_S_STUCK_unk": ("Stbd", "Foot button", _FOOT),
    "WHEEL_P_STUCK_unk": ("Port", "Wheel", _WHEEL),
    "WHEEL_S_STUCK_unk": ("Stbd", "Wheel", _WHEEL),
    "G3K_FC_P_STUCK_unk": ("Port", "Flight controller", {
        2: "Upwind Mode", 32: "Board Up", 128: "Neutral Diff", 512: "Pitch Damp",
        1024: "Board Down"}),
    "G3K_FC_S_STUCK_unk": ("Stbd", "Flight controller", {
        8: "Upwind Mode", 16: "Stow Angle", 128: "Neutral Diff",
        256: "Downwind Mode", 512: "Board Up", 16384: "Board Down"}),
    "G3K_GD_P_STUCK_unk": ("Port", "Grinder panel", {
        1: "Board Stow", 4: "Jib Lead In", 8: "Invert Away", 16: "Board Unstow",
        64: "Jib Lead Out", 128: "Invert Towards"}),
    "G3K_GD_S_STUCK_unk": ("Stbd", "Grinder panel", {
        2: "Jib Lead In", 8: "Board Stow", 32: "Jib Lead Out",
        64: "Jib Cunningham Out"}),
    "G3K_WT_P_STUCK_unk": ("Port", "Wing trimmer panel", {
        1: "Diff Minus", 2: "Float", 4: "Camber Plus", 8: "Invert Stbd",
        16: "Diff Plus", 32: "Camber Zero", 64: "Camber Minus", 128: "Invert Port"}),
    "G3K_WT_S_STUCK_unk": ("Stbd", "Wing trimmer panel", {
        1: "Invert Port", 2: "Camber Plus", 8: "Diff Minus", 16: "Invert Stbd",
        32: "Camber Minus", 128: "Diff Plus"}),
    "G3K_T_P_STUCK_unk": ("Port", "Strategist panel", {}),
    "G3K_T_S_STUCK_unk": ("Stbd", "Strategist panel", {}),
    "PILOT_P_STUCK_unk": ("Port", "Pilot panel", {
        1: "Left Rud Avg Plus", 2: "Max Diff", 8: "Max Diff Opposite",
        16: "Left Rud Avg Minus", 128: "Menu Enter", 1024: "Right Rud Avg Plus",
        2048: "Invert Away", 4096: "Upwind Downwind", 8192: "Invert Towards",
        16384: "Right Rud Avg Minus"}),
    "PILOT_S_STUCK_unk": ("Stbd", "Pilot panel", {
        1: "Left Rud Avg Plus", 2: "Invert Towards", 4: "Upwind Downwind",
        8: "Invert Away", 16: "Left Rud Avg Minus", 128: "Menu Enter",
        256: "Settings", 1024: "Right Rud Avg Plus", 2048: "Max Diff Opposite",
        8192: "Max Diff"}),
}


def _named(flag: int, side: str, panel: str, names: dict) -> list[tuple[str, str]]:
    """(button, where) for each bit set in one panel's flag."""
    out = []
    for bit in (1 << i for i in range(24)):      # the panels are 24-bit masks
        if not flag & bit:
            continue
        name = names.get(bit)
        if name is None:
            out.append((f"{side} {panel} button".upper(), f"{panel}, bit {bit}"))
        elif isinstance(name, tuple):
            out.append((f"{side} {name[0]}".upper(), name[1]))
        else:
            out.append((f"{side} {name}".upper(), panel))
    return out


def read(b: pd.DataFrame) -> list[tuple[str, str]] | None:
    """What the boat says is stuck right now, from the newest sample of each flag.

    None means the frame has no flag to read, so there is no telling. An empty
    list means nothing is stuck. Otherwise one (button, where) per stuck button.
    The global flag decides whether to alarm and the panels only name it, so a
    fault on a panel missing from this table still flashes.
    """
    if STUCK_GLOBAL not in b.columns:
        return None
    flag = b[STUCK_GLOBAL].dropna()
    if flag.empty:
        return None
    if flag.iloc[-1] <= 0:
        return []

    stuck = []
    for channel, (side, panel, names) in STUCK_PANELS.items():
        if channel in b.columns:
            v = b[channel].dropna()
            if not v.empty:
                stuck += _named(int(v.iloc[-1]), side, panel, names)
    return stuck or [(UNNAMED, "")]


class Namer:
    """When to ask for the panel flags, and the name to show between asks.

    The global flag is on every poll. The panels are not: without this a fault
    left up for 18 min, as NZL's was on 2026-09-20, would load 360 polls in a row
    with channels that keep saying the same thing.
    """

    def __init__(self):
        self.up = False
        self.names = None
        self.next_ask = 0.0

    def due(self, now: float) -> bool:
        return self.up and now >= self.next_ask

    def update(self, stuck: list | None, asked: bool, now: float) -> list | None:
        """Take one poll's `read`, and return what the tile should show."""
        self.up = bool(stuck)
        if not stuck:
            self.names, self.next_ask = None, 0.0
            return stuck
        if asked:
            self.names, self.next_ask = stuck, now + RENAME_S
        return self.names or stuck
