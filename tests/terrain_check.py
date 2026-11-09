"""Sanity check for the terrain coverage model (needs internet for elevation tiles)."""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from meshscope.terrain import CoverageEngine  # noqa: E402

e = CoverageEngine(pathlib.Path(tempfile.mkdtemp()))
SITES = [  # Phoenix: same valley site at 2 m and on a 30 m tower, then South Mountain summit
    ("valley 2m", 33.4484, -112.0740, 2),
    ("valley 30m tower", 33.4484, -112.0740, 30),
    ("South Mtn summit", 33.3340, -112.0610, 10),
]
results = {}
for name, lat, lon, h in SITES:
    r = e.compute([{"num": 1, "lat": lat, "lon": lon, "height": h}],
                  {"radiusKm": 25, "preset": "MEDIUM_FAST", "environment": "suburban"})
    t = r["transmitters"][0]
    results[name] = t["coveredPct"]
    print(f"{name:18} ground={t['groundM']}m covered={t['coveredPct']}% meanReach={t['meanReachKm']}km "
          f"area={r['areaKm2']}km2 zoom={r['zoom']} tiles={r['tiles']} {r['seconds']}s png={len(r['image']) // 1024}KB")
assert results["valley 30m tower"] > results["valley 2m"], "a taller antenna should cover more"
assert results["South Mtn summit"] > results["valley 2m"], "a summit should cover more than the valley floor"
print("TERRAIN CHECK PASSED")
