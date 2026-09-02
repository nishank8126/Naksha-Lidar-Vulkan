from __future__ import annotations

# One canonical output-code contract for every Naksha AI engine.
VEHICLE = 0
GROUND = 2
LOW_VEGETATION = 3
MEDIUM_VEGETATION = 4
HIGH_VEGETATION = 5
BUILDING = 6
LOW_POINT = 7
POWER_LINE_WIRE = 14
POWER_LINE_POLE = 15
HIGH_NOISE = 18

DEFAULT_BASE_MAPPING = {
    0: GROUND,
    1: LOW_VEGETATION,
    2: MEDIUM_VEGETATION,
    3: HIGH_VEGETATION,
    4: BUILDING,
}

DEFAULT_POWER_OUTPUT_MAPPING = {
    5: POWER_LINE_WIRE,
    6: POWER_LINE_POLE,
}

BASE_CLASS_ROWS = [
    (0, "Ground", GROUND),
    (1, "Low Vegetation", LOW_VEGETATION),
    (2, "Medium Vegetation", MEDIUM_VEGETATION),
    (3, "High Vegetation", HIGH_VEGETATION),
    (4, "Building", BUILDING),
]
