#!/usr/bin/env python3
"""Обновлятор данных карты «Есть Бензин» — мультигород.
Тянет живые крауд-статусы заправок из gdebenz /api/stations по bbox каждого города
и пишет общий stations.json (поле city у каждой точки + список городов с центрами).
Алерты бота работают по всем городам сразу (по геолокации подписчика).
"""
import json, urllib.request, os, sys, time

# город: (center[lat,lon], bbox(lat1=S, lon1=W, lat2=N, lon2=E))
CITIES = {
    "Новосибирск":   ([55.030, 82.92],  (54.80, 82.75, 55.15, 83.10)),
    "Краснодар":     ([45.035, 39.03],  (45.00, 38.90, 45.15, 39.12)),
    "Екатеринбург":  ([56.838, 60.605], (56.74, 60.48, 56.92, 60.78)),
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

def pull(bbox):
    s, w, n, e = bbox
    url = f"https://gdebenz.ru/api/stations?lat1={s}&lon1={w}&lat2={n}&lon2={e}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = json.loads(r.read())
    return raw if isinstance(raw, list) else raw.get("stations", [])

def main():
    stations = []
    per = {}
    for city, (center, bbox) in CITIES.items():
        try:
            src = pull(bbox)
        except Exception as ex:
            print(f"WARN {city}: {ex}"); src = []
        cnt = 0
        for x in src:
            if x.get("lat") is None or x.get("lon") is None:
                continue
            stations.append({
                "id": str(x.get("osm_id")), "city": city,
                "lat": x["lat"], "lon": x["lon"],
                "name": x.get("name") or x.get("brand") or "АЗС",
                "addr": x.get("addr") or "",
                "status": x.get("status"), "fuels_now": x.get("fuels_now") or "",
                "conflict": x.get("conflict"), "src": "seed",
            })
            cnt += 1
        per[city] = cnt
    data = {
        "cities": [{"name": c, "center": v[0]} for c, v in CITIES.items()],
        "default_city": "Новосибирск",
        "updated": int(time.time()),
        "stations": stations,
    }
    json.dump(data, open(OUT, "w"), ensure_ascii=False)
    print(f"OK всего {len(stations)} АЗС по городам {per} -> {OUT}")

if __name__ == "__main__":
    main()
