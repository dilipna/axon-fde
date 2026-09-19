"""Ambient temperature profiles.

Ambient temperature is the forcing function of the whole thermal model: it
sets how hard the refrigeration unit has to work. A degrading unit that copes
easily at dawn can fall behind by mid-afternoon, which is what makes the
flagship scenario develop when it does rather than immediately.

Profiles are pure functions of elapsed minutes so that a run is reproducible
and a scenario can be resumed at any point without replaying its history.
"""

from __future__ import annotations

import math

from simulator.incidentforge.scenarios import AmbientProfile, AmbientProfileType

__all__ = ["ambient_temperature_c"]

#: Minute of the simulated day at which ambient peaks. Real diurnal maxima sit
#: a few hours after solar noon because ground and air lag insolation.
_PEAK_MINUTE = 15 * 60  # 15:00

#: Simulated runs start mid-morning, when a load has usually been rolling for
#: a while and ambient is climbing toward the afternoon peak.
_START_MINUTE_OF_DAY = 10 * 60  # 10:00

_MINUTES_PER_DAY = 24 * 60


def ambient_temperature_c(profile: AmbientProfile, elapsed_minutes: float) -> float:
    """Ambient temperature at a point in the run.

    ``CONSTANT`` holds at ``peak_c``. The diurnal profiles follow a cosine
    between ``min_c`` and ``peak_c`` peaking mid-afternoon, which is a coarse
    but honest approximation of a clear-sky day.
    """
    if profile.type is AmbientProfileType.CONSTANT:
        return profile.peak_c

    minute_of_day = (_START_MINUTE_OF_DAY + elapsed_minutes) % _MINUTES_PER_DAY

    # Cosine peaking at _PEAK_MINUTE: +1 at the peak, -1 twelve hours away.
    phase = 2.0 * math.pi * (minute_of_day - _PEAK_MINUTE) / _MINUTES_PER_DAY
    swing = math.cos(phase)

    midpoint = (profile.peak_c + profile.min_c) / 2.0
    amplitude = (profile.peak_c - profile.min_c) / 2.0
    return midpoint + amplitude * swing
