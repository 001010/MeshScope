"""Terrain-aware LoRa coverage heatmaps.

Elevation comes from the public AWS Terrain Tiles (Terrarium PNG encoding, ~30-80 m
resolution, no API key), cached on disk. For each transmitter we cast radial rays and,
for every point along a ray, compute:

    received = Ptx + Gtx + Grx - FSPL(d, f) - diffraction(terrain) - clutter

where diffraction uses the dominant knife-edge (ITU-R P.526) along the terrain profile,
with earth curvature (k = 4/3). The result is the link margin above the receiver
sensitivity of the chosen modem preset, rendered as a semi-transparent PNG.

Terrain (hills, ridges, valleys) is modelled from real elevation. Buildings and trees
are not in the elevation data, so they are approximated by the "environment" clutter
loss; tall sites (towers, rooftops, hilltop routers) are modelled with per-node antenna
height above ground.

Antenna height: a height set by hand wins; otherwise the median altitude the node has
reported (GPS, metres above sea level) minus the terrain under it; otherwise a default.

Calibration: each node's prediction is checked against the radio links actually observed
from it. Where a neighbour measured the node's SNR, the observed margin (SNR minus the
preset's demodulation limit) is compared with the predicted margin at the neighbour's
position, and the node's whole map is shifted by the median difference. That absorbs real
transmit power, antenna and cable losses, local clutter and the receivers' antennas. Links
without a usable SNR only require the neighbour to be covered (margin >= 0).
"""

from __future__ import annotations

import base64
import io
import logging
import math
import statistics
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
EARTH_R = 6371008.8
K_FACTOR = 4.0 / 3.0

# Approximate LoRa receive sensitivity per preset (dBm): -174 + 10log10(BW) + NF(6) + SNR limit
PRESETS = {
    "LONG_SLOW": {"label": "LongSlow (SF12/125k)", "sens": -137.0, "snrLimit": -20.0},
    "LONG_MODERATE": {"label": "LongModerate (SF11/125k)", "sens": -134.5, "snrLimit": -17.5},
    "LONG_FAST": {"label": "LongFast (SF11/250k)", "sens": -131.5, "snrLimit": -17.5},
    "MEDIUM_SLOW": {"label": "MediumSlow (SF10/250k)", "sens": -129.0, "snrLimit": -15.0},
    "MEDIUM_FAST": {"label": "MediumFast (SF9/250k)", "sens": -126.5, "snrLimit": -12.5},
    "SHORT_SLOW": {"label": "ShortSlow (SF8/250k)", "sens": -124.0, "snrLimit": -10.0},
    "SHORT_FAST": {"label": "ShortFast (SF7/250k)", "sens": -121.5, "snrLimit": -7.5},
    "SHORT_TURBO": {"label": "ShortTurbo (SF7/500k)", "sens": -118.5, "snrLimit": -7.5},
}
ENVIRONMENTS = {"open": 0.0, "rural": 6.0, "suburban": 12.0, "urban": 20.0, "dense_urban": 28.0}
MAX_AUTO_RADIUS_M = 80000.0   # radius is widened to include observed links, up to this
SNR_SATURATION = 8.0          # LoRa SNR flattens out near +10 dB, so higher readings only bound the margin
MAX_CALIBRATION_DB = 30.0
MIN_AGL_M, MAX_AGL_M = 1.0, 300.0
MAX_POS_ERR_FOR_ELEVATION_M = 150.0  # beyond this the terrain under the reported position is a guess


def _dist_bearing(lat0, lon0, lat1, lon1):
    dy = (lat1 - lat0) * 111320.0
    dx = (lon1 - lon0) * 111320.0 * math.cos(math.radians(lat0))
    return math.hypot(dx, dy), math.atan2(dx, dy) % (2 * math.pi)


def antenna_height(t: Dict[str, Any], ground: float, default: float) -> Tuple[float, str, Optional[str]]:
    """(height above ground, source, note) for a transmitter."""
    if t.get("height") is not None:
        return float(t["height"]), "set", None
    el = t.get("elevation")
    if not el:
        return default, "default", None
    if (t.get("posErrM") or 0) > MAX_POS_ERR_FOR_ELEVATION_M:
        return default, "default", "position is imprecise, so its reported altitude can't be compared with the terrain"
    agl = el["m"] - ground
    if agl < MIN_AGL_M:
        return default, "default", f"reported altitude is {-agl:.0f} m below the terrain model (GPS altitude error)" if agl < -5 else None
    if agl > MAX_AGL_M:
        return MAX_AGL_M, "reported", f"reported altitude is {agl:.0f} m above ground; capped at {MAX_AGL_M:.0f} m"
    return agl, "reported", None


class TileCache:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mem: Dict[Tuple[int, int, int], np.ndarray] = {}
        self._lock = threading.Lock()

    def get(self, z: int, x: int, y: int) -> np.ndarray:
        key = (z, x, y)
        with self._lock:
            if key in self.mem:
                return self.mem[key]
        from PIL import Image

        f = self.dir / f"{z}_{x}_{y}.png"
        if f.exists():
            data = f.read_bytes()
        else:
            req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": "MeshScope"})
            data = urllib.request.urlopen(req, timeout=20).read()
            f.write_bytes(data)
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(np.float32)
        elev = a[..., 0] * 256.0 + a[..., 1] + a[..., 2] / 256.0 - 32768.0
        with self._lock:
            if len(self.mem) > 200:
                self.mem.clear()
            self.mem[key] = elev
        return elev


class ElevationModel:
    """A stitched mosaic of tiles covering a bounding box, sampled bilinearly."""

    def __init__(self, cache: TileCache, south, west, north, east, target_m: float):
        lat_c = (south + north) / 2
        # choose a zoom whose pixel size is near target_m, capped to keep the tile count sane
        z = int(max(8, min(13, math.log2(156543.03 * math.cos(math.radians(lat_c)) / max(target_m, 1)))))
        while True:
            x0, y1 = self._tile_xy(south, west, z)
            x1, y0 = self._tile_xy(north, east, z)
            if (x1 - x0 + 1) * (y1 - y0 + 1) <= 64 or z <= 8:
                break
            z -= 1
        self.z, self.x0, self.y0 = z, x0, y0
        rows = []
        for ty in range(y0, y1 + 1):
            rows.append(np.concatenate([cache.get(z, tx, ty) for tx in range(x0, x1 + 1)], axis=1))
        self.grid = np.concatenate(rows, axis=0)
        self.tiles = (x1 - x0 + 1) * (y1 - y0 + 1)

    @staticmethod
    def _tile_xy(lat, lon, z):
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        return x, y

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        n = 2 ** self.z * 256
        px = (lon + 180.0) / 360.0 * n - self.x0 * 256
        lr = np.radians(np.clip(lat, -85, 85))
        py = (1.0 - np.arcsinh(np.tan(lr)) / np.pi) / 2.0 * n - self.y0 * 256
        h, w = self.grid.shape
        px = np.clip(px - 0.5, 0, w - 1.001)
        py = np.clip(py - 0.5, 0, h - 1.001)
        x0, y0 = np.floor(px).astype(int), np.floor(py).astype(int)
        fx, fy = px - x0, py - y0
        g = self.grid
        top = g[y0, x0] * (1 - fx) + g[y0, x0 + 1] * fx
        bot = g[y0 + 1, x0] * (1 - fx) + g[y0 + 1, x0 + 1] * fx
        return top * (1 - fy) + bot * fy


def knife_edge_loss(v: np.ndarray) -> np.ndarray:
    """ITU-R P.526 approximation J(v) in dB (0 for v <= -0.78)."""
    vv = np.maximum(v, -0.78)
    j = 6.9 + 20 * np.log10(np.sqrt((vv - 0.1) ** 2 + 1) + vv - 0.1)
    return np.where(v > -0.78, j, 0.0)


def _offset(lat, lon, dist_m, bearing):
    """Small-distance forward geodesic (equirectangular), vectorised."""
    dlat = dist_m * np.cos(bearing) / 111320.0
    dlon = dist_m * np.sin(bearing) / (111320.0 * np.cos(np.radians(lat)))
    return lat + dlat, lon + dlon


class CoverageEngine:
    def __init__(self, cache_dir: Path):
        self.cache = TileCache(cache_dir)
        self._lock = threading.Lock()

    @staticmethod
    def _calibrate(t, margin, rays, step, n, radius, snr_limit):
        return _calibrate_impl(t, margin, rays, step, n, radius, snr_limit)

    def compute(self, txs: List[Dict[str, Any]], p: Dict[str, Any]) -> Dict[str, Any]:
        """txs: [{num, lat, lon, height}], p: radius_km, freq_mhz, tx_power, gains, preset, environment, rx_height."""
        if not txs:
            raise ValueError("No transmitters with a known position")
        with self._lock:
            return self._compute(txs, p)

    def _compute(self, txs, p):
        t0 = time.time()
        radius_req = radius = float(p.get("radiusKm", 15)) * 1000.0
        if p.get("useLinks", True):
            # make sure the farthest observed link of every transmitter is on the map
            far = [_dist_bearing(t["lat"], t["lon"], nb["lat"], nb["lon"])[0]
                   for t in txs for nb in t.get("neighbors", ())]
            if far:
                radius = max(radius, min(MAX_AUTO_RADIUS_M, max(far) * 1.2))
        default_h = float(p.get("defaultHeight", 2.0))
        freq = float(p.get("freqMhz", 906.875))
        lam = 299.792458 / freq
        eirp = float(p.get("txPower", 22)) + float(p.get("txGain", 2)) + float(p.get("rxGain", 2)) - float(p.get("cableLoss", 0))
        preset = PRESETS.get(p.get("preset", "LONG_FAST"), PRESETS["LONG_FAST"])
        sens, snr_limit = preset["sens"], preset["snrLimit"]
        clutter = ENVIRONMENTS.get(p.get("environment", "suburban"), 12.0)
        rx_h = float(p.get("rxHeight", 1.5))
        rays = int(p.get("rays", 360 if len(txs) <= 3 else 180))
        step = max(30.0, radius / 300.0)
        n = int(radius // step)
        d = (np.arange(n) + 1) * step  # distances along a ray (m)

        # bounding box of everything + elevation mosaic
        lats = [t["lat"] for t in txs]
        lons = [t["lon"] for t in txs]
        dlat = radius / 111320.0
        dlon = radius / (111320.0 * math.cos(math.radians(sum(lats) / len(lats))))
        south, north = min(lats) - dlat, max(lats) + dlat
        west, east = min(lons) - dlon, max(lons) + dlon
        dem = ElevationModel(self.cache, south, west, north, east, step)

        # output raster
        lat_c = (south + north) / 2
        span_x = (east - west) * 111320.0 * math.cos(math.radians(lat_c))
        span_y = (north - south) * 111320.0
        px = max(span_x, span_y) / 1024.0
        px = max(px, step / 1.5)
        W, H = max(16, int(span_x / px)), max(16, int(span_y / px))
        gy, gx = np.mgrid[0:H, 0:W]
        g_lat = north - (gy + 0.5) / H * (north - south)
        g_lon = west + (gx + 0.5) / W * (east - west)
        best = np.full((H, W), -np.inf, dtype=np.float32)

        bearings = np.linspace(0, 2 * np.pi, rays, endpoint=False)
        d_i = d[:, None]  # rx distance (rows)
        d_j = d[None, :]  # obstacle distance (cols)
        lower = d_j < d_i
        fspl = 20 * np.log10(d / 1000.0) + 20 * np.log10(freq) + 32.44
        clutter_vec = np.where(d > 500, clutter, clutter * d / 500)
        geom = np.sqrt(np.where(lower, 2 * d_i / (lam * d_j * np.maximum(d_i - d_j, 1e-3)), 0.0))
        per_tx = []
        for t in txs:
            r_lat, r_lon = _offset(t["lat"], t["lon"], d[None, :], bearings[:, None])
            terr = dem.sample(r_lat, r_lon)
            ground0 = float(dem.sample(np.array([t["lat"]]), np.array([t["lon"]]))[0])
            height, h_src, h_note = antenna_height(t, ground0, default_h)
            tx_abs = ground0 + height
            terr_eff = terr - d[None, :] ** 2 / (2 * K_FACTOR * EARTH_R)  # earth bulge
            margin = np.empty((rays, n), dtype=np.float32)
            # mean terrain between the transmitter and each point -> effective antenna height,
            # which is what gives hilltops and towers their range on otherwise flat ground
            cmean = np.cumsum(terr, axis=1) / np.arange(1, n + 1)[None, :]
            ht_eff = np.maximum(tx_abs - cmean, 1.0)
            for k in range(rays):
                te = terr_eff[k]
                rx_abs = te + rx_h
                los = tx_abs + (rx_abs[:, None] - tx_abs) * d_j / d_i  # LOS height above obstacle j for rx i
                h = te[None, :] - los
                v = np.where(lower, h * geom, -10.0)
                vmax = v.max(axis=1)
                diffraction = knife_edge_loss(vmax)
                # two-ray plane-earth loss dominates FSPL beyond the breakpoint for low antennas
                plane_earth = 40 * np.log10(d) - 20 * np.log10(ht_eff[k]) - 20 * np.log10(rx_h)
                path = np.maximum(fspl, plane_earth)
                margin[k] = eirp - path - diffraction - clutter_vec - sens
            calib = self._calibrate(t, margin, rays, step, n, radius, snr_limit) if p.get("useLinks", True) else None
            if calib and calib["db"]:
                margin += calib["db"]
            # polar -> raster
            dy = (g_lat - t["lat"]) * 111320.0
            dx = (g_lon - t["lon"]) * 111320.0 * math.cos(math.radians(t["lat"]))
            dist = np.hypot(dx, dy)
            brg = np.mod(np.arctan2(dx, dy), 2 * np.pi)
            ri = np.mod(np.round(brg / (2 * np.pi) * rays).astype(int), rays)
            di = np.clip(np.round(dist / step).astype(int) - 1, 0, n - 1)
            m = np.where(dist <= radius, margin[ri, di], -np.inf)
            np.maximum(best, m, out=best)
            covered = margin > 0
            # distance along each ray before coverage first drops out
            first_gap = np.where((~covered).any(axis=1), np.argmax(~covered, axis=1), n)
            reach_m = float(np.where(first_gap > 0, d[np.maximum(first_gap - 1, 0)], 0.0).mean())
            per_tx.append({"num": t["num"], "groundM": round(ground0, 1), "antennaM": round(height, 1),
                           "antennaSrc": h_src, "antennaNote": h_note,
                           "elevationM": (t.get("elevation") or {}).get("m"), "calibration": calib,
                           "meanReachKm": round(reach_m / 1000, 2),
                           "coveredPct": round(float(covered.mean()) * 100, 1)})

        png = _render(best)
        cell_km2 = (px / 1000.0) ** 2
        return {
            "bounds": [[south, west], [north, east]],
            "image": "data:image/png;base64," + base64.b64encode(png).decode(),
            "areaKm2": round(float((best > 0).sum()) * cell_km2, 1),
            "transmitters": per_tx,
            "zoom": dem.z, "tiles": dem.tiles, "stepM": round(step), "seconds": round(time.time() - t0, 2),
            "radiusKm": round(radius / 1000, 1), "radiusExtended": radius > radius_req + 1,
            "sensitivity": sens, "eirp": eirp, "clutterDb": clutter,
        }


def _calibrate_impl(t, margin, rays, step, n, radius, snr_limit):
    exact, lower, skipped = [], [], 0
    for nb in t.get("neighbors", ()):
        dist, brg = _dist_bearing(t["lat"], t["lon"], nb["lat"], nb["lon"])
        if dist < step or dist > radius:
            skipped += 1
            continue
        if (t.get("posErrM") or 0) + (nb.get("posErrM") or 0) > 0.3 * dist:
            skipped += 1  # positions too vague for a link this short
            continue
        ri = int(round(brg / (2 * math.pi) * rays)) % rays
        di = min(max(int(round(dist / step)) - 1, 0), n - 1)
        pred = float(margin[ri, di])
        snr = nb.get("snr")
        if nb.get("heard") and snr is not None and snr < SNR_SATURATION:
            exact.append((snr - snr_limit) - pred)
        else:
            lower.append(max(0.0, -pred))  # the link exists, so the neighbour is at least just covered
    if exact:
        db = statistics.median(exact)
    elif lower:
        db = statistics.median(lower)
    else:
        return {"db": 0.0, "links": 0, "measured": 0, "skipped": skipped}
    db = max(-MAX_CALIBRATION_DB, min(MAX_CALIBRATION_DB, db))
    return {"db": round(db, 1), "links": len(exact) + len(lower), "measured": len(exact), "skipped": skipped}


def _render(margin: np.ndarray) -> bytes:
    """Colour the link margin: red (just reachable) -> yellow -> green (strong)."""
    from PIL import Image

    m = np.nan_to_num(margin, neginf=-99)
    rgba = np.zeros(m.shape + (4,), dtype=np.uint8)
    stops = [  # margin dB, r, g, b, alpha
        (-6, 239, 68, 68, 0), (0, 239, 68, 68, 150), (6, 249, 115, 22, 165), (12, 234, 179, 8, 170),
        (20, 132, 204, 22, 175), (30, 34, 197, 94, 185),
    ]
    xs = np.array([s[0] for s in stops], dtype=np.float32)
    for ch in range(4):
        rgba[..., ch] = np.interp(m, xs, np.array([s[ch + 1] for s in stops], dtype=np.float32)).astype(np.uint8)
    rgba[m < -6, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()
