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

FUEL_OPTS = ["92","95","98","100","ДТ","газ"]
def avail_fuels(st):
    """Множество доступных марок на АЗС. Если статус 'есть' без перечня — маркер 'ANY'
    (бензин есть, марки неизвестны) — годится под любого наблюдателя."""
    fs = set((st.get("fuels_now") or "").split(",")) - {""}
    if st.get("status")=="yes" and not fs:
        fs.add("ANY")
    return fs

def station_link(st):
    # Просто точка-заправка в 2ГИС — пользователь сам решит, строить маршрут или нет.
    # (Маршрутный deep-link 2ГИС вёл себя неверно — вернёмся позже.)
    return f"https://2gis.ru/geo/{st['lon']}%2C{st['lat']}"

# ───────────────────────── команды/сообщения ─────────────────────────
ADMIN_CHAT = 579387502           # отзывы пересылаются сюда (Данил)
FB_PENDING = set()               # юзеры, от кого ждём текст отзыва

def kb_main():
    return json.dumps({"inline_keyboard":[
        [{"text":"🗺 Открыть карту","web_app":{"url":MAP_URL}}],
        [{"text":"🔔 Следить за бензином рядом","callback_data":"watch"}],
        [{"text":"💬 Отзыв / сообщить о проблеме","callback_data":"feedback"}],
    ]})
def kb_loc():
    return json.dumps({"keyboard":[[{"text":"📍 Отправить геолокацию","request_location":True}]],
        "resize_keyboard":True,"one_time_keyboard":True})
def kb_fuels(chat):
    subs=jload(SUBS,{}); sel=set((subs.get(str(chat)) or {}).get("fuels",["95"]))
    btn=lambda g:{"text":("✅ " if g in sel else "")+g,"callback_data":"tf:"+g}
    return json.dumps({"inline_keyboard":[
        [btn("92"),btn("95"),btn("98")],
        [btn("100"),btn("ДТ"),btn("газ")],
        [{"text":"Готово 🔔","callback_data":"fdone"}],
    ]})

WELCOME=("⛽ Есть Бензин — карта наличия топлива.\nГорода: Краснодар, Новосибирск, Екатеринбург.\n\n"
    "🗺 Жми «Открыть карту» — видно, где есть/очередь/нет бензина прямо сейчас "
    "(выбери город сверху; данные обновляются и дополняются водителями).\n\n"
    "🔔 «Следить» — пришли геолокацию, и я напишу, когда рядом с тобой на заправке "
    "появится 95-й. У других карт такого нет.")

def handle_message(m):
    chat=m["chat"]["id"]
    if "location" in m:
        loc=m["location"]
        subs=jload(SUBS,{})
        prevsub=subs.get(str(chat)) or {}
        subs[str(chat)]={"lat":loc["latitude"],"lon":loc["longitude"],
                         "radius":DEFAULT_RADIUS_KM,"ts":time.time(),"sent":{},
                         "fuels":prevsub.get("fuels",["95"])}
        jsave(SUBS,subs)
        api("sendMessage",{"chat_id":chat,
            "text":f"🔔 Локация принята! Слежу в радиусе {DEFAULT_RADIUS_KM} км.\n"
                   "За каким топливом следить? (по умолчанию 95) Отметь нужное:",
            "reply_markup":json.dumps({"remove_keyboard":True})})
        api("sendMessage",{"chat_id":chat,"text":"⛽ Топливо для алертов:","reply_markup":kb_fuels(chat)})
        return
    text=m.get("text","")
    # режим отзыва: ждём текст от юзера → пересылаем Данилу
    if chat in FB_PENDING and text and not text.startswith("/"):
        FB_PENDING.discard(chat)
        frm=m.get("from") or {}
        un=("@"+frm["username"]) if frm.get("username") else (frm.get("first_name") or "")
        api("sendMessage",{"chat_id":ADMIN_CHAT,
            "text":f"💬 Отзыв (Есть Бензин)\nот {un} (id {chat}):\n\n{text}"})
        api("sendMessage",{"chat_id":chat,"text":"Спасибо! Отзыв отправлен 🙏","reply_markup":kb_main()})
        return
    if text.startswith("/start") or text.startswith("/help"):
        api("sendMessage",{"chat_id":chat,"text":WELCOME,"reply_markup":kb_main()})
    elif text.startswith("/feedback"):
        FB_PENDING.add(chat)
        api("sendMessage",{"chat_id":chat,"text":"Напиши, что не так или что улучшить — передам разработчику 👇"})
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
    elif cb.get("data")=="feedback":
        FB_PENDING.add(chat)
        api("sendMessage",{"chat_id":chat,
            "text":"Напиши, что не так или что улучшить — передам разработчику 👇"})
    elif (cb.get("data") or "").startswith("tf:"):
        g=cb["data"][3:]
        subs=jload(SUBS,{}); sub=subs.get(str(chat))
        if sub is not None:
            fuels=set(sub.get("fuels",["95"]))
            fuels.symmetric_difference_update({g})
            if not fuels: fuels={"95"}          # хотя бы одно
            sub["fuels"]=sorted(fuels); subs[str(chat)]=sub; jsave(SUBS,subs)
            api("editMessageReplyMarkup",{"chat_id":chat,
                "message_id":cb["message"]["message_id"],"reply_markup":kb_fuels(chat)})
    elif cb.get("data")=="fdone":
        subs=jload(SUBS,{}); sub=subs.get(str(chat)) or {}
        fl=", ".join(sub.get("fuels",["95"]))
        api("sendMessage",{"chat_id":chat,
            "text":f"🔔 Готово! Слежу за: {fl} (радиус {sub.get('radius',DEFAULT_RADIUS_KM)} км). "
                   "Напишу, как появится рядом. Отключить — /stop.","reply_markup":kb_main()})

# ───────────────────────── цикл алертов ─────────────────────────
def alert_loop():
    while True:
        try:
            data=jload(STATIONS,{}); stations=data.get("stations",[])
            byid={s["id"]:s for s in stations}
            cur={s["id"]:avail_fuels(s) for s in stations}
            prevraw=jload(LASTSTATE,{})
            prev={k:set(v) for k,v in prevraw.items() if isinstance(v,list)}
            # пофуэльно: что НОВОГО появилось на каждой АЗС
            appeared={sid:(av-prev.get(sid,set())) for sid,av in cur.items() if (av-prev.get(sid,set()))}
            jsave(LASTSTATE,{sid:sorted(av) for sid,av in cur.items()})
            if appeared:
                subs=jload(SUBS,{}); changed=False
                for chat,sub in subs.items():
                    watched=set(sub.get("fuels",["95"]))
                    sent=sub.get("sent",{})
                    for sid,newf in appeared.items():
                        # сработка: появилась наблюдаемая марка ИЛИ generic «есть» (ANY)
                        if not (("ANY" in newf) or (newf & watched)): continue
                        st=byid.get(sid)
                        if not st: continue
                        dist=haversine(sub["lat"],sub["lon"],st["lat"],st["lon"])
                        if dist>sub.get("radius",DEFAULT_RADIUS_KM): continue
                        if time.time()-sent.get(sid,0) < ALERT_COOLDOWN: continue
                        got=", ".join(sorted((newf&watched) or (cur[sid]-{"ANY"}))) or "бензин"
                        api("sendMessage",{"chat_id":int(chat),
                            "text":f"🟢 Появилось рядом: {got}!\n{st['name']}"
                                   f"{(' · '+st['addr']) if st.get('addr') else ''}\n"
                                   f"~{dist:.1f} км от тебя · сейчас: {st.get('fuels_now') or 'есть'}\n"
                                   f"📍 Заправка в 2ГИС: {station_link(st)}",
                            "reply_markup":kb_main()})
                        sent[sid]=time.time(); changed=True
                    sub["sent"]=sent
                if changed: jsave(SUBS,subs)
                log(f"alerts: станций с появлением={len(appeared)} подписчиков={len(subs)}")
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
