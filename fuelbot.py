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
MAP_URL = "https://nuzhenbenzin.ru/"
DEFAULT_RADIUS_KM = 5
ALERT_COOLDOWN = 6*60*60          # не повторять алерт по той же АЗС юзеру 6ч
POLL_ALERTS_SEC = 300             # цикл алертов ~5 мин (Pro мгновенно, Free +FREE_DELAY)
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

# ───────────────────────── Pro / Stars ─────────────────────────
PRO_FILE = os.path.join(DIR, "pro_users.json")   # {str(uid): pro_until_epoch}
PENDING_FILE = os.path.join(DIR, "alert_pending.json")
PRO_PLANS = {"7": 49, "30": 149}                 # дней: цена в ⭐
FREE_RADIUS = 5
PRO_RADIUS = 15
FREE_DELAY = 0                                   # задержку убрали: для бензина устаревший алерт = вред
                                                 # (алерт расходился с картой). Pro отличается радиусом/зонами.

USERS_FILE = os.path.join(DIR, "users.json")     # учёт пользователей бота
def track(frm):
    if not frm: return
    try:
        u=jload(USERS_FILE,{}); k=str(frm.get("id"))
        rec=u.get(k,{"first":time.time(),"n":0})
        rec["n"]=rec.get("n",0)+1; rec["last"]=time.time()
        rec["name"]=("@"+frm["username"]) if frm.get("username") else (frm.get("first_name") or "")
        u[k]=rec; jsave(USERS_FILE,u)
    except Exception as e: log(f"track: {e}")

def is_pro(chat):
    try: return jload(PRO_FILE,{}).get(str(chat),0) > time.time()
    except: return False
def add_pro(chat, days):
    p=jload(PRO_FILE,{}); base=max(p.get(str(chat),0), time.time())
    p[str(chat)]=base + days*86400; jsave(PRO_FILE,p)
    return p[str(chat)]

# ───────────────────────── команды/сообщения ─────────────────────────
ADMIN_CHAT = 579387502           # отзывы пересылаются сюда (Данил)
FB_PENDING = set()               # юзеры, от кого ждём текст отзыва

def kb_main():
    return json.dumps({"inline_keyboard":[
        [{"text":"🗺 Открыть карту","web_app":{"url":MAP_URL}}],
        [{"text":"🔔 Следить за бензином рядом","callback_data":"watch"}],
        [{"text":"⭐ Pro (быстрее + шире)","callback_data":"pro"}],
        [{"text":"💬 Отзыв / сообщить о проблеме","callback_data":"feedback"}],
    ]})
def kb_pro():
    return json.dumps({"inline_keyboard":[
        [{"text":f"Неделя — {PRO_PLANS['7']} ⭐","callback_data":"buy:7"}],
        [{"text":f"Месяц — {PRO_PLANS['30']} ⭐","callback_data":"buy:30"}],
    ]})
PRO_PITCH=("⭐ Нужен Бензин Pro\n\n"
    "Бесплатно: карта, выбор топлива, алерты, радиус 5 км.\n\n"
    "Pro добавляет:\n"
    f"• 📍 Радиус слежения до {PRO_RADIUS} км вместо {FREE_RADIUS} — ловишь больше заправок вокруг.\n"
    "• 🔝 Приоритетные уведомления.\n"
    "• 🏠 (скоро) несколько зон: дом + работа.\n\n"
    "Оплата — Telegram Stars:")
def kb_loc():
    return json.dumps({"keyboard":[[{"text":"📍 Отправить геолокацию","request_location":True}]],
        "resize_keyboard":True,"one_time_keyboard":True})
def kb_map_reply():
    # reply-клавиатура с web_app: ТОЛЬКО так работает sendData из Mini App («Следить отсюда»)
    return json.dumps({"keyboard":[[{"text":"🗺 Открыть карту","web_app":{"url":MAP_URL}}]],
        "resize_keyboard":True,"is_persistent":True})
def kb_fuels(chat):
    subs=jload(SUBS,{}); sel=set((subs.get(str(chat)) or {}).get("fuels",["95"]))
    btn=lambda g:{"text":("✅ " if g in sel else "")+g,"callback_data":"tf:"+g}
    return json.dumps({"inline_keyboard":[
        [btn("92"),btn("95"),btn("98")],
        [btn("100"),btn("ДТ"),btn("газ")],
        [{"text":"Готово 🔔","callback_data":"fdone"}],
    ]})

WELCOME=("⛽ Нужен Бензин — карта наличия топлива.\nГорода: Краснодар, Новосибирск, Екатеринбург.\n\n"
    "🗺 Жми «Открыть карту» — видно, где есть/очередь/нет бензина прямо сейчас "
    "(выбери город сверху; данные обновляются и дополняются водителями).\n\n"
    "🔔 Алерты — пришлю, когда рядом появится нужное топливо:\n"
    "• Быстро: в карте жми «🔔 Следить отсюда».\n"
    "• Лучше всего: 📎 → Геопозиция → «Транслировать» (1-8ч) — точка едет за тобой, "
    "бот ищет вокруг текущего места, даже когда приложение закрыто.")

def handle_message(m):
    chat=m["chat"]["id"]
    track(m.get("from"))
    # данные из Mini App («Следить отсюда»): одно гео = центр карты + подписка на алерты
    wad=m.get("web_app_data")
    if wad:
        try: data=json.loads(wad.get("data") or "{}")
        except Exception: data={}
        if data.get("action")=="sub" and data.get("lat") is not None:
            subs=jload(SUBS,{}); prev=subs.get(str(chat)) or {}
            subs[str(chat)]={"lat":float(data["lat"]),"lon":float(data["lon"]),
                "radius":(PRO_RADIUS if is_pro(chat) else FREE_RADIUS),
                "ts":time.time(),"sent":{},"fuels":prev.get("fuels",["95"])}
            jsave(SUBS,subs)
            api("sendMessage",{"chat_id":chat,
                "text":"🔔 Готово! Слежу за бензином рядом с этой точкой. Напишу, как появится. За каким топливом?",
                "reply_markup":kb_fuels(chat)})
        return
    sp=m.get("successful_payment")
    if sp:
        days=int((sp.get("invoice_payload") or "pro:7").split(":")[1])
        until=add_pro(chat, days)
        # поднять радиус существующей подписке до Pro
        subs=jload(SUBS,{})
        if str(chat) in subs: subs[str(chat)]["radius"]=PRO_RADIUS; jsave(SUBS,subs)
        import datetime
        api("sendMessage",{"chat_id":chat,
            "text":f"🎉 Pro активирован на {days} дн.! Теперь алерты мгновенные и радиус {PRO_RADIUS} км. Спасибо 🙏",
            "reply_markup":kb_main()})
        log(f"PRO purchased chat={chat} +{days}d")
        return
    if "location" in m:
        loc=m["location"]
        live=loc.get("live_period")            # >0 → это «Трансляция геопозиции»
        subs=jload(SUBS,{})
        prevsub=subs.get(str(chat)) or {}
        rad=PRO_RADIUS if is_pro(chat) else FREE_RADIUS
        subs[str(chat)]={"lat":loc["latitude"],"lon":loc["longitude"],
                         "radius":rad,"ts":time.time(),"sent":prevsub.get("sent",{}) if live else {},
                         "fuels":prevsub.get("fuels",["95"]),"live":bool(live)}
        jsave(SUBS,subs)
        if live:
            api("sendMessage",{"chat_id":chat,
                "text":f"🛰 Трансляция включена! Слежу за бензином вокруг тебя (радиус {rad} км), "
                       "точка едет за тобой, пока трансляция активна. За каким топливом?",
                "reply_markup":kb_fuels(chat)})
        else:
            api("sendMessage",{"chat_id":chat,
                "text":f"🔔 Точка принята! Слежу в радиусе {rad} км. За каким топливом?",
                "reply_markup":kb_fuels(chat)})
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
        api("sendMessage",{"chat_id":chat,"text":WELCOME,"reply_markup":kb_map_reply()})
        api("sendMessage",{"chat_id":chat,"text":"Действия 👇","reply_markup":kb_main()})
    elif text.startswith("/feedback"):
        FB_PENDING.add(chat)
        api("sendMessage",{"chat_id":chat,"text":"Напиши, что не так или что улучшить — передам разработчику 👇"})
    elif text.startswith("/pro"):
        if is_pro(chat):
            api("sendMessage",{"chat_id":chat,"text":"У тебя уже активен Pro ⭐ Спасибо!"})
        else:
            api("sendMessage",{"chat_id":chat,"text":PRO_PITCH,"reply_markup":kb_pro()})
    elif text.startswith("/stats") and chat==ADMIN_CHAT:
        u=jload(USERS_FILE,{}); subs=jload(SUBS,{}); pro=jload(PRO_FILE,{}); now=time.time()
        d1=sum(1 for r in u.values() if now-r.get("last",0)<86400)
        wk=sum(1 for r in u.values() if now-r.get("last",0)<7*86400)
        propaid=sum(1 for v in pro.values() if v>now)
        top=sorted(u.items(),key=lambda x:-x[1].get("n",0))[:5]
        tops="\n".join(f"  {r.get('name','?')} — {r.get('n',0)}" for _,r in top)
        api("sendMessage",{"chat_id":chat,
            "text":f"📊 Нужен Бензин — статистика\n"
                   f"Всего пользователей: {len(u)}\n"
                   f"Активны за сутки: {d1} · за неделю: {wk}\n"
                   f"Подписок на алерты: {len(subs)}\n"
                   f"Pro активных: {propaid}\n"
                   f"Топ по активности:\n{tops or '  —'}"})
    elif text.startswith("/stop"):
        subs=jload(SUBS,{}); subs.pop(str(chat),None); jsave(SUBS,subs)
        api("sendMessage",{"chat_id":chat,"text":"🔕 Слежение отключено. Включить снова — /start."})
    elif text.startswith("/map"):
        api("sendMessage",{"chat_id":chat,"text":"Карта:","reply_markup":kb_main()})

def handle_callback(cb):
    chat=cb["message"]["chat"]["id"]
    track(cb.get("from"))
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
    elif cb.get("data")=="pro":
        if is_pro(chat):
            api("sendMessage",{"chat_id":chat,"text":"У тебя уже активен Pro ⭐ Спасибо!"})
        else:
            api("sendMessage",{"chat_id":chat,"text":PRO_PITCH,"reply_markup":kb_pro()})
    elif (cb.get("data") or "").startswith("buy:"):
        days=cb["data"].split(":")[1]; price=PRO_PLANS.get(days)
        if price:
            r=api("sendInvoice",{"chat_id":chat,"title":f"Нужен Бензин Pro — {days} дн.",
                "description":f"Мгновенные алерты + радиус {PRO_RADIUS} км на {days} дней.",
                "payload":f"pro:{days}","currency":"XTR",
                "prices":json.dumps([{"label":f"Pro {days} дн.","amount":price}])})
            if not r.get("ok"): log(f"sendInvoice fail: {r}")
    elif cb.get("data")=="fdone":
        subs=jload(SUBS,{}); sub=subs.get(str(chat)) or {}
        fl=", ".join(sub.get("fuels",["95"]))
        api("sendMessage",{"chat_id":chat,
            "text":f"🔔 Готово! Слежу за: {fl} (радиус {sub.get('radius',DEFAULT_RADIUS_KM)} км). "
                   "Напишу, как появится рядом. Отключить — /stop.","reply_markup":kb_main()})

def handle_edited(em):
    # движение во время «Трансляции геопозиции» — двигаем точку слежения
    loc=em.get("location")
    if not loc: return
    chat=em["chat"]["id"]; subs=jload(SUBS,{}); sub=subs.get(str(chat))
    if sub:
        sub["lat"]=loc["latitude"]; sub["lon"]=loc["longitude"]; sub["live"]=True
        subs[str(chat)]=sub; jsave(SUBS,subs)

# ───────────────────────── цикл алертов ─────────────────────────
def alert_loop():
    while True:
        try:
            now=time.time()
            data=jload(STATIONS,{}); stations=data.get("stations",[])
            byid={s["id"]:s for s in stations}
            cur={s["id"]:avail_fuels(s) for s in stations}
            prevraw=jload(LASTSTATE,{})
            prev={k:set(v) for k,v in prevraw.items() if isinstance(v,list)}
            newly={sid:(av-prev.get(sid,set())) for sid,av in cur.items() if (av-prev.get(sid,set()))}
            jsave(LASTSTATE,{sid:sorted(av) for sid,av in cur.items()})
            # очередь «появилось»: Pro обрабатываем сразу, Free — после FREE_DELAY
            pending=jload(PENDING_FILE,[])
            for sid,nf in newly.items():
                pending.append({"sid":sid,"fuels":sorted(nf),"ts":now})
            subs=jload(SUBS,{}); changed=False; notified=0
            for ev in pending:
                evf=set(ev["fuels"]); age=now-ev["ts"]; st=byid.get(ev["sid"])
                if not st: continue
                for chat,sub in subs.items():
                    pro=is_pro(chat)
                    if (not pro) and age<FREE_DELAY: continue       # Free ждёт задержку
                    watched=set(sub.get("fuels",["95"]))
                    if not (("ANY" in evf) or (evf & watched)): continue
                    radius=PRO_RADIUS if pro else FREE_RADIUS
                    dist=haversine(sub["lat"],sub["lon"],st["lat"],st["lon"])
                    if dist>radius: continue
                    sent=sub.setdefault("sent",{})
                    if now-sent.get(ev["sid"],0) < ALERT_COOLDOWN: continue
                    got=", ".join(sorted((evf&watched) or (evf-{"ANY"}))) or "бензин"
                    api("sendMessage",{"chat_id":int(chat),
                        "text":f"{'⚡ ' if pro else ''}🟢 Появилось рядом: {got}!\n{st['name']}"
                               f"{(' · '+st['addr']) if st.get('addr') else ''}\n"
                               f"~{dist:.1f} км · сейчас: {st.get('fuels_now') or 'есть'}\n"
                               f"📍 Заправка в 2ГИС: {station_link(st)}",
                            "reply_markup":kb_main()})
                    sent[ev["sid"]]=now; changed=True; notified+=1
            if changed: jsave(SUBS,subs)
            pending=[ev for ev in pending if now-ev["ts"] < FREE_DELAY+600]
            jsave(PENDING_FILE,pending)
            if newly or notified: log(f"alerts: новых={len(newly)} отправлено={notified} pending={len(pending)}")
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
        p={"timeout":50,"allowed_updates":json.dumps(["message","edited_message","callback_query","pre_checkout_query"])}
        if offset is not None: p["offset"]=offset
        upd=api("getUpdates",p)
        if not upd.get("ok"): time.sleep(3); continue
        for u in upd["result"]:
            offset=u["update_id"]+1
            try:
                if "message" in u: handle_message(u["message"])
                elif "edited_message" in u: handle_edited(u["edited_message"])
                elif "callback_query" in u: handle_callback(u["callback_query"])
                elif "pre_checkout_query" in u:
                    api("answerPreCheckoutQuery",{"pre_checkout_query_id":u["pre_checkout_query"]["id"],"ok":"true"})
            except Exception as e: log(f"handler: {e}")

if __name__=="__main__":
    main()
