"""Daily-high temperature markets: parsing and public data for a forecast-vs-book study.

Public GETs only: station coordinates (aviationweather.gov), archived numerical forecasts
as they stood one and two days before each valid hour (Open-Meteo previous-runs API) and
METAR observations (Iowa Environmental Mesonet). The offline comparison with Polymarket
prices lives in weather_edge_model.py. No orders, no account access, no production imports.
"""
from __future__ import annotations
import argparse
import calendar
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.btc5m_backfill import USER_AGENT, fetch_json, read_rows

MODELS = ("ecmwf_ifs025", "ecmwf_aifs025_single", "gfs_seamless", "ncep_aigfs025", "icon_seamless", "gem_seamless",
          "jma_seamless", "meteofrance_seamless", "ukmo_seamless", "cma_grapes_global", "ecmwf_ifs")
# Regional guidance, requested only where it has its own domain (elsewhere these fall back to a global model).
REGIONAL = {"ncep_nbm_conus": ("US",), "italia_meteo_arpae_icon_2i": ("IT",),
            "knmi_seamless": ("NL", "GB", "FR", "DE", "ES", "IT", "PL", "FI"),
            "dmi_seamless": ("NL", "GB", "FR", "DE", "PL", "FI"), "metno_seamless": ("FI", "PL", "DE", "NL", "GB")}
ALL_MODELS = MODELS + tuple(REGIONAL)
DECISION_HOUR = 18   # local hour at which the day's observations so far are frozen for next-day forecasts
MONTHS = {name: index + 1 for index, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"))}
EVENT = re.compile(r"^highest-temperature-in-(?P<city>.+)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$")
SITE = re.compile(r"[?&]site=([A-Za-z0-9]{4})\b")
LABEL = re.compile(r"^(?:(?P<low>-?\d+)-(?P<high>-?\d+)|(?P<single>-?\d+))\s*°(?P<unit>[CF])(?:\s+or\s+(?P<tail>below|higher))?$")


def parse_bucket(label: str | None) -> tuple[float, float, str] | None:
    """Reported whole degrees -> half-open interval in the label's own unit."""
    match = LABEL.match((label or "").strip())
    if not match:
        return None
    low = float(match["low"] if match["low"] is not None else match["single"])
    high = float(match["high"] if match["high"] is not None else match["single"])
    if match["tail"] == "below":
        return -math.inf, high + .5, match["unit"]
    if match["tail"] == "higher":
        return low - .5, math.inf, match["unit"]
    return low - .5, high + .5, match["unit"]


def shift_date(date: str, days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(calendar.timegm(time.strptime(date, "%Y-%m-%d")) + days * 86400))


def weather_events(markets: list[dict]) -> dict[str, dict]:
    events: dict[str, dict] = {}
    for market in markets:
        match = EVENT.match(market.get("event_slug") or "")
        site = SITE.search(market.get("resolution_source") or "")
        bucket = parse_bucket(market.get("group_item"))
        if (not match or not site or bucket is None or match["month"] not in MONTHS or
                market["outcome_prices"] not in (["1", "0"], ["0", "1"]) or market["outcomes"] != ["Yes", "No"]):
            continue
        event = events.setdefault(market["event_slug"], {
            "slug": market["event_slug"], "city": match["city"], "station": site.group(1).upper(),
            "date": f"{int(match['year']):04d}-{MONTHS[match['month']]:02d}-{int(match['day']):02d}",
            "unit": bucket[2], "buckets": []})
        event["buckets"].append({"id": market["id"], "low": bucket[0], "high": bucket[1],
                                 "yes": market["outcome_prices"] == ["1", "0"], "fee_rate": market.get("fee_rate") or 0.0,
                                 "volume": market.get("volume") or 0.0})
    return events


def fetch_text(url: str, attempts: int = 4) -> str:
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:  # network errors of any kind are retried, then reported
            last = exc
            time.sleep(2 * 2 ** attempt)
    raise RuntimeError(f"{type(last).__name__}: {last} from {url}")


def fetch_data(directory: Path, weather: Path, history_days: int, workers: int) -> dict:
    events = weather_events(read_rows(directory / "markets.jsonl"))
    weather.mkdir(parents=True, exist_ok=True)
    stations = sorted({event["station"] for event in events.values()})
    dates = sorted(event["date"] for event in events.values())
    first = shift_date(dates[0], -history_days)
    last = dates[-1]
    info_path = weather / "stations.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
    missing = [station for station in stations if station not in info]
    for index in range(0, len(missing), 20):
        raw = fetch_json("https://aviationweather.gov/api/data/stationinfo?format=json&ids=" + ",".join(missing[index:index + 20]))
        for row in raw:
            info[row["icaoId"]] = {"lat": row["lat"], "lon": row["lon"], "site": row.get("site"), "country": row.get("country")}
    info_path.write_text(json.dumps(info, indent=1), encoding="utf-8")
    hourly = ",".join(f"temperature_2m_previous_day{lead}" for lead in (1, 2))

    def one(station: str) -> str:
        if station not in info:
            return f"{station}: no coordinates"
        target = weather / f"{station}.json"
        if target.is_file():
            cached = json.loads(target.read_text(encoding="utf-8"))
            if (cached.get("last") == last and cached.get("first") == first and
                    cached.get("models") == list(ALL_MODELS) and "observed_partial_max_c" in cached and
                    not cached.get("regional_retry")):
                return f"{station}: cached"
        base = (f"https://previous-runs-api.open-meteo.com/v1/forecast?latitude={info[station]['lat']}"
                f"&longitude={info[station]['lon']}&hourly={hourly}&start_date={first}&end_date={last}"
                f"&timezone=auto&models={','.join(MODELS)}")
        try:
            local, epoch = fetch_json(base), fetch_json(base + "&timeformat=unixtime")
            if len(local["hourly"]["time"]) != len(epoch["hourly"]["time"]):
                raise RuntimeError("local and epoch forecast grids differ")
            zone, forecast = local["timezone"], {key: value for key, value in local["hourly"].items() if key != "time"}
            retry = []
            for model, countries in REGIONAL.items():
                if info[station].get("country") not in countries:
                    continue
                try:
                    extra = fetch_json(base.replace(",".join(MODELS), model), attempts=3)["hourly"]
                except (RuntimeError, KeyError) as exc:
                    if "HTTP 400" not in str(exc):
                        retry.append(model)       # rate limit or network: ask again on the next run
                    continue                      # HTTP 400: outside the model's domain or not archived
                for lead in (1, 2):
                    series = extra.get(f"temperature_2m_previous_day{lead}")
                    if (series and len(series) == len(local["hourly"]["time"]) and
                            sum(value is not None for value in series) >= .8 * len(series)):
                        forecast[f"temperature_2m_previous_day{lead}_{model}"] = series
            text = fetch_text("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode({
                "station": station, "data": "tmpc", "tz": zone, "format": "onlycomma", "latlon": "no", "missing": "M",
                "trace": "T", "direct": "no", "year1": first[:4], "month1": int(first[5:7]), "day1": int(first[8:]),
                "year2": last[:4], "month2": int(last[5:7]), "day2": int(last[8:])}) + "&report_type=3&report_type=4")
            observed: dict[str, list[float]] = defaultdict(list)
            partial: dict[str, list[float]] = defaultdict(list)
            for line in text.splitlines()[1:]:
                parts = line.split(",")
                if len(parts) == 3 and parts[2] not in ("M", ""):
                    observed[parts[1][:10]].append(float(parts[2]))
                    if parts[1][11:16] <= f"{DECISION_HOUR:02d}:00":
                        partial[parts[1][:10]].append(float(parts[2]))
            target.write_text(json.dumps({
                "station": station, "timezone": zone, "first": first, "last": last, "models": list(ALL_MODELS),
                "local_time": local["hourly"]["time"], "epoch": epoch["hourly"]["time"], "forecast": forecast,
                "regional_retry": retry,
                "observed_max_c": {day: max(values) for day, values in observed.items()},
                "observed_partial_max_c": {day: max(values) for day, values in partial.items() if len(values) >= 12},
                "observed_count": {day: len(values) for day, values in observed.items()}}), encoding="utf-8")
            return f"{station}: ok"
        except (RuntimeError, KeyError, ValueError) as exc:
            return f"{station}: {str(exc)[:120]}"

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, stations))
    return {"events": len(events), "stations": len(stations), "first": first, "last": last,
            "failed": [result for result in results if not result.endswith(("ok", "cached"))]}


def fetch_observations(weather: Path, first: str, last: str, workers: int) -> dict:
    """Every METAR temperature with its UTC time, per station: obs-<ICAO>.json = [[epoch, deg C], ...]."""
    stations = sorted(path.stem for path in weather.glob("????.json"))

    def one(station: str) -> str:
        try:
            text = fetch_text("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode({
                "station": station, "data": "tmpc", "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "missing": "M",
                "trace": "T", "direct": "no", "year1": first[:4], "month1": int(first[5:7]), "day1": int(first[8:]),
                "year2": last[:4], "month2": int(last[5:7]), "day2": int(last[8:])}) + "&report_type=3&report_type=4")
            rows = []
            for line in text.splitlines()[1:]:
                parts = line.split(",")
                if len(parts) == 3 and parts[2] not in ("M", ""):
                    rows.append([calendar.timegm(time.strptime(parts[1], "%Y-%m-%d %H:%M")), float(parts[2])])
            (weather / f"obs-{station}.json").write_text(json.dumps(sorted(rows)), encoding="utf-8")
            return f"{station}: {len(rows)}"
        except (RuntimeError, ValueError) as exc:
            return f"{station}: failed {str(exc)[:80]}"

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, stations))
    return {"stations": len(stations), "failed": [result for result in results if "failed" in result]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".polycast/slow-market-backfill-v1"))
    parser.add_argument("--weather", type=Path, default=Path(".polycast/slow-market-backfill-v1/weather"))
    parser.add_argument("--history-days", type=int, default=60, help="forecast/observation days before the first market")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--observations", nargs=2, metavar=("FIRST", "LAST"),
                        help="only fetch time-stamped METAR temperatures between two dates (YYYY-MM-DD)")
    args = parser.parse_args()
    if args.observations:
        print(json.dumps(fetch_observations(args.weather, *args.observations, args.workers)))
        return 0
    print(json.dumps(fetch_data(args.directory, args.weather, args.history_days, args.workers)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
