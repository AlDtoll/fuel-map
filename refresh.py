#!/usr/bin/env python3
"""Обновлятор данных карты «Где бензин».
Тянет живые крауд-статусы заправок из gdebenz /api/stations по bbox города
(СТАРТОВЫЙ ЗАСЕВ — снимает холодный старт) и пишет stations.json для карты.
Позже сюда домержим СВОИ отчёты из Telegram-бота.

Usage: python3 refresh.py  (город/bbox задан ниже)
"""
import json, urllib.request, os, sys

CITY = "Краснодар"
CENTER = [45.035, 39.03]
BBOX = (45.00, 38.90, 45.15, 39.12)   # lat1,lon1(south,west), lat2,lon2(north,east)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36"

def pull():
    s, w, n, e = BBOX
    url = f"https://gdebenz.ru/api/stations?lat1={s}&lon1={w}&lat2={n}&lon2={e}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())

def main():
    raw = pull()
    src = raw if isinstance(raw, list) else raw.get("stations", [])
    stations = []
    for x in src:
        if x.get("lat") is None or x.get("lon") is None:
            continue
        stations.append({
            "id": str(x.get("osm_id")),
            "lat": x["lat"], "lon": x["lon"],
            "name": x.get("name") or x.get("brand") or "АЗС",
            "brand": x.get("brand") or "",
            "addr": x.get("addr") or "",
            "status": x.get("status"),               # yes | no | queue | None
            "fuels_now": x.get("fuels_now") or "",    # "92,95,98,100,ДТ"
            "conflict": x.get("conflict"),
            "src": "seed",                            # seed=засев из gdebenz; own=наш отчёт
        })
    data = {"city": CITY, "center": CENTER, "stations": stations}
    json.dump(data, open(OUT, "w"), ensure_ascii=False)
    from collections import Counter
    c = Counter(s["status"] for s in stations)
    print(f"OK {len(stations)} АЗС  статусы={dict(c)}  -> {OUT}")

if __name__ == "__main__":
    main()
