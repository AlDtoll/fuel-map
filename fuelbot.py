#!/usr/bin/env python3
"""«Есть Бензин» — Telegram-бот карты наличия топлива (Краснодар, Phase B).

Фичи:
• 🗺 Кнопка «Открыть карту» (Telegram Mini App) — наша Leaflet-карта.
• 🔔 Гео-алерты (killer-фича, у gdebenz нет): юзер шлёт геолокацию → подписка;
  бот пушит, когда на заправке В РАДИУСЕ появляется 95-й/бензин (статус → «есть»
  или в fuels_now добавился 95). Антиспам-кулдаун на пару станция+юзер.

Данные: ~/fuel-map/stations.json (обновляет отдельный cron refresh.py + git push).
Бот их только ЧИТАЕТ и сравнивает с прошлым снимком для детекта «появился».
Транспорт: long-poll Bot API напрямую. Токен: env FUELBOT_TOKEN / secrets.env.
"""
import os, sys, json, time, math, threading, urllib.request, urllib.parse, urllib.error

DIR = os.path.expanduser("~/fuel-map")
STATIONS = os.path.join(DIR, "stations.json")
SUBS = os.path.join(DIR, "subscribers.json")
LASTSTATE = os.path.join(DIR, "alert_state.json")
MAP_URL = "https://aldtoll.github.io/fuel-map/"
DEFAULT_RADIUS_KM = 5
ALERT_COOLDOWN = 6*60*60          # не повторять алерт по той же АЗС юзеру 6ч
POLL_ALERTS_SEC = 420             # цикл алертов ~7 мин
LOG = os.path.join(DIR, "bot.log")

def log(m):
    line=f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line,flush=True)
    try: open(LOG,"a").write(line+"\n")
    except: pass

def get_token():
    t=os.environ.get("FUELBOT_TOKEN","").strip()
    if t: return t
    sec=os.path.expanduser("~/.claude/env/secrets.env")
    if os.path.exists(sec):
        for ln in open(sec):
            if ln.strip().startswith("FUELBOT_TOKEN="):
                return ln.split("=",1)[1].strip().strip('"').strip("'")
    log("FATAL: нет FUELBOT_TOKEN"); sys.exit(1)

TOKEN=None; API=None
def api(method, params=None):
    data=urllib.parse.urlencode(params or {}).encode()
    req=urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read())
        except: return {"ok":False}
    except Exception as e:
        log(f"api {method}: {e}"); return {"ok":False}

def jload(p, d):
    try: return json.load(open(p))
    except: return d
def jsave(p, o):
    try: json.dump(o, open(p,"w"), ensure_ascii=False)
    except Exception as e: log(f"save {p}: {e}")

def haversine(a,b,c,d):
    R=6371; p=math.pi/180
    x=0.5-math.cos((c-a)*p)/2+math.cos(a*p)*math.cos(c*p)*(1-math.cos((d-b)*p))/2
    return 2*R*math.asin(math.sqrt(x))

def has95(st):
    return st.get("status")=="yes" or "95" in (st.get("fuels_now") or "").split(",")

# ───────────────────────── команды/сообщения ─────────────────────────
def kb_main():
    return json.dumps({"inline_keyboard":[
        [{"text":"🗺 Открыть карту","web_app":{"url":MAP_URL}}],
        [{"text":"🔔 Следить за бензином рядом","callback_data":"watch"}],
    ]})
def kb_loc():
    return json.dumps({"keyboard":[[{"text":"📍 Отправить геолокацию","request_location":True}]],
        "resize_keyboard":True,"one_time_keyboard":True})

WELCOME=("⛽ Есть Бензин — карта наличия топлива в Краснодаре.\n\n"
    "🗺 Жми «Открыть карту» — видно, где есть/очередь/нет бензина прямо сейчас "
    "(данные обновляются и дополняются водителями).\n\n"
    "🔔 «Следить» — пришли геолокацию, и я напишу, когда рядом с тобой на заправке "
    "появится 95-й. У других карт такого нет.")

def handle_message(m):
    chat=m["chat"]["id"]
    if "location" in m:
        loc=m["location"]
        subs=jload(SUBS,{})
        subs[str(chat)]={"lat":loc["latitude"],"lon":loc["longitude"],
                         "radius":DEFAULT_RADIUS_KM,"ts":time.time(),"sent":{}}
        jsave(SUBS,subs)
        api("sendMessage",{"chat_id":chat,
            "text":f"🔔 Готово! Слежу за заправками в радиусе {DEFAULT_RADIUS_KM} км. "
                   "Напишу, как только рядом появится 95-й. Отключить — /stop.",
            "reply_markup":json.dumps({"remove_keyboard":True})})
        return
    text=m.get("text","")
    if text.startswith("/start") or text.startswith("/help"):
        api("sendMessage",{"chat_id":chat,"text":WELCOME,"reply_markup":kb_main()})
    elif text.startswith("/stop"):
        subs=jload(SUBS,{}); subs.pop(str(chat),None); jsave(SUBS,subs)
        api("sendMessage",{"chat_id":chat,"text":"🔕 Слежение отключено. Включить снова — /start."})
    elif text.startswith("/map"):
        api("sendMessage",{"chat_id":chat,"text":"Карта:","reply_markup":kb_main()})

def handle_callback(cb):
    chat=cb["message"]["chat"]["id"]
    api("answerCallbackQuery",{"callback_query_id":cb["id"]})
    if cb.get("data")=="watch":
        api("sendMessage",{"chat_id":chat,
            "text":"Пришли свою геолокацию — и я буду следить за бензином рядом 👇",
            "reply_markup":kb_loc()})

# ───────────────────────── цикл алертов ─────────────────────────
def alert_loop():
    while True:
        try:
            data=jload(STATIONS,{}); stations=data.get("stations",[])
            cur={s["id"]:has95(s) for s in stations}
            byid={s["id"]:s for s in stations}
            prev=jload(LASTSTATE,{})
            # «появился» = стало True там, где раньше было False/нет записи
            appeared=[sid for sid,v in cur.items() if v and not prev.get(sid)]
            jsave(LASTSTATE,cur)
            if appeared:
                subs=jload(SUBS,{}); changed=False
                for chat,sub in subs.items():
                    sent=sub.get("sent",{})
                    for sid in appeared:
                        st=byid.get(sid);
                        if not st: continue
                        dist=haversine(sub["lat"],sub["lon"],st["lat"],st["lon"])
                        if dist<=sub.get("radius",DEFAULT_RADIUS_KM):
                            if time.time()-sent.get(sid,0) < ALERT_COOLDOWN: continue
                            fuels=st.get("fuels_now") or "95"
                            api("sendMessage",{"chat_id":int(chat),
                                "text":f"🟢 Появился бензин рядом!\n{st['name']}"
                                       f"{(' · '+st['addr']) if st.get('addr') else ''}\n"
                                       f"~{dist:.1f} км от тебя · есть: {fuels}",
                                "reply_markup":kb_main()})
                            sent[sid]=time.time(); changed=True
                    sub["sent"]=sent
                if changed: jsave(SUBS,subs)
                log(f"alerts: appeared={len(appeared)} проверено подписчиков={len(subs)}")
        except Exception as e:
            log(f"alert_loop: {e}")
        time.sleep(POLL_ALERTS_SEC)

def main():
    global TOKEN,API
    TOKEN=get_token(); API=f"https://api.telegram.org/bot{TOKEN}"
    me=api("getMe")
    if not me.get("ok"): log(f"getMe fail: {me}"); sys.exit(1)
    log(f"FuelBot starting as @{me['result'].get('username')}")
    threading.Thread(target=alert_loop, daemon=True).start()
    offset=None
    while True:
        p={"timeout":50,"allowed_updates":json.dumps(["message","callback_query"])}
        if offset is not None: p["offset"]=offset
        upd=api("getUpdates",p)
        if not upd.get("ok"): time.sleep(3); continue
        for u in upd["result"]:
            offset=u["update_id"]+1
            try:
                if "message" in u: handle_message(u["message"])
                elif "callback_query" in u: handle_callback(u["callback_query"])
            except Exception as e: log(f"handler: {e}")

if __name__=="__main__":
    main()
