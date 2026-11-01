#!/usr/bin/env python3
"""
oled_errori.py  v2.0 — caccia agli ERRORI DI PREZZO (non agli sconti) sui TV OLED, multi-negozio.

Fonti
  MediaWorld    categoria OLED (o pagine per marca se bloccata)       dati strutturati
  Samsung Shop  pagina OLED ufficiale                                  dati strutturati
  Unieuro       pagina OLED Samsung (SPERIMENTALE)                     lettura del testo:
                ogni anomalia viene CONFERMATA sulla pagina prodotto prima dell'avviso

Regole (basta una per l'avviso):
  A taglia      <= 50% dell'OLED più economico (dopo di lui) della stessa taglia, stesso negozio
  B storico     <= 50% della mediana dei prezzi registrati per quel prodotto
  C soglia      sotto il minimo credibile per un OLED nuovo di quella taglia
  D incoerenza  < 60% dell'OLED più economico di una taglia più piccola, stesso negozio
  E negozi      <= 45% del prezzo più basso dello STESSO MODELLO negli altri negozi
Esclusi: usato, ricondizionato, esposizione, accessori, non disponibili.

Uso:   pip install curl_cffi && python oled_errori.py [--dry-run]
Env:   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import html
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from curl_cffi import requests as http
    USING_CURL = True
except ImportError:
    import requests as http
    USING_CURL = False

STATE_FILE = Path(__file__).resolve().parent / "oled_state.json"

# --- MediaWorld --------------------------------------------------------------
MW = "https://www.mediaworld.it"
MW_CATEGORY = "/it/category/tv-oled-400103.html"
MW_FALLBACK = ["/it/brand/samsung/tv/oled", "/it/brand/sony/tv/tv-oled", "/it/brand/lg/tv/oled",
               "/it/list/tv-oled-55-pollici", "/it/list/tv-oled-65-pollici"]
MW_LINK = re.compile(r'href="((?:https://www\.mediaworld\.it)?/it/(?:list|brand/[a-z]+/tv)/[a-z0-9-]*oled[a-z0-9-]*)"',
                     re.I)
MAX_PAGES, MAX_LISTS = 30, 20
# --- Samsung Shop ------------------------------------------------------------
SS = "https://www.samsung.com"
SS_PAGES = ["/it/tvs/oled-tv/"]
# --- Unieuro (sperimentale) --------------------------------------------------
UE = "https://www.unieuro.it"
UE_PAGES = ["/online/samsung-oled-sis"]

# --- Regole ------------------------------------------------------------------
SIZE_RATIO, HIST_RATIO, HIST_MIN_N, SMALLER_RATIO, CROSS_RATIO = 0.50, 0.50, 3, 0.60, 0.45
FLOOR = {42: 300, 48: 350, 55: 400, 65: 550, 77: 900, 83: 1200, 97: 5000}
REALERT_DROP = 0.90
SANE_MIN, SANE_MAX = 50, 30000          # prezzi fuori da qui = lettura sbagliata

EXCLUDE = re.compile(r"ricondizionat\w*|usat[oa]|seconda vita|esposizion\w*|espost[oa]|"
                     r"b-?grade|outlet|danneggiat\w*|refurbished|"
                     r"staffa|supporto|telecomando|cavo|pellicola|soundbar|monitor", re.I)
OLED = re.compile(r"oled", re.I)
SIZE_RX = [re.compile(r"\b(42|48|55|65|77|83|97)\s*(?:\"|”|''|pollici|poll\.|inch)", re.I),
           re.compile(r"\b(?:OLED|QE|TQ|K-?|XR-?|TX-?)(42|48|55|65|77|83|97)[A-Z]", re.I),
           re.compile(r"\b(42|48|55|65|77|83|97)(?:OLED|WN|WM|A[89]\d)", re.I)]
# codici modello -> chiave canonica (stesso TV in negozi diversi)
KEY_RX = [
    (re.compile(r"\b(?:QE|TQ)(42|48|55|65|77|83)(S\d{2}[A-Z])", re.I), "SAMSUNG"),
    (re.compile(r"\bOLED(42|48|55|65|77|83|97)([A-Z]\d)", re.I), "LG"),
    (re.compile(r"\b(?:K|XR)-?(42|48|55|65|77|83)(XR\d{2}|A\d{2})", re.I), "SONY"),
    (re.compile(r"\b(42|48|55|65|77)OLED(\d{3})", re.I), "PHILIPS"),
    (re.compile(r"\bTX-?(42|48|55|65|77)([A-Z]{1,2}\d{3,4})", re.I), "PANASONIC"),
]
SAMSUNG_NAME = re.compile(r"\b(42|48|55|65|77|83)\s*(?:\"|”|'').{0,20}?\bOLED\s+(S\d{2}[A-Z])\b", re.I)


# ---------------------------------------------------------------------------
def session():
    s = http.Session(impersonate="chrome") if USING_CURL else http.Session()
    s.headers.update({"Accept-Language": "it-IT,it;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    if not USING_CURL:
        s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    return s


def get(s, url):
    r = s.get(url, timeout=25)
    if r.status_code == 403:                  # secondo tentativo "da navigazione"
        time.sleep(3)
        home = re.match(r"https://[^/]+", url).group(0) + "/"
        r = s.get(url, timeout=25, headers={"Referer": home, "Sec-Fetch-Site": "same-origin"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def price_of(v):
    if isinstance(v, dict):
        v = v.get("price") or v.get("lowPrice")
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def jsonld_products(text, base):
    out = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                offers = node.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                offers = offers if isinstance(offers, dict) else {}
                seller = offers.get("seller")
                seller = (seller.get("name") if isinstance(seller, dict) else seller) or ""
                url = node.get("url") or offers.get("url") or ""
                img = node.get("image")
                out.append({
                    "name": html.unescape(str(node.get("name") or "")).strip(),
                    "price": price_of(offers) or price_of(node.get("price")),
                    "url": urljoin(base, url) if url else "",
                    "sku": str(node.get("sku") or node.get("gtin13") or node.get("mpn") or ""),
                    "avail": str(offers.get("availability") or "").rsplit("/", 1)[-1],
                    "seller": str(seller).strip(),
                    "image": (img[0] if isinstance(img, list) and img else img) or "",
                })
            for k in ("itemListElement", "item", "@graph"):
                if k in node:
                    stack.append(node[k])
    return out


def size_of(name):
    for rx in SIZE_RX:
        m = rx.search(name)
        if m:
            return int(m.group(1))
    return None


def model_key(name):
    """Chiave canonica del modello: stessa TV, stesso valore in tutti i negozi."""
    for rx, brand in KEY_RX:
        m = rx.search(name)
        if m:
            return f"{brand}-{m.group(1)}-{m.group(2).upper()}"
    m = SAMSUNG_NAME.search(name)               # "55" OLED S90F" dello shop Samsung
    if m and re.search(r"samsung|vision ai|oled s\d{2}", name, re.I):
        return f"SAMSUNG-{m.group(1)}-{m.group(2).upper()}"
    return None


# ---------------------------------------------------------------------------
# FONTI
# ---------------------------------------------------------------------------
def src_mediaworld(s, log):
    todo, done, found, cat_ok = [MW_CATEGORY] + MW_FALLBACK, set(), {}, False
    while todo and len(done) < MAX_LISTS:
        path = todo.pop(0)
        path = urlparse(path).path if path.startswith("http") else path
        if path in done:
            continue
        if cat_ok:
            break
        done.add(path)
        for page in range(1, MAX_PAGES + 1):
            url = MW + path + (f"?page={page}" if page > 1 else "")
            try:
                text = get(s, url)
            except Exception as ex:  # noqa: BLE001
                log.append(f"  MW {path} p{page}: {ex}")
                break
            if page == 1:
                for link in MW_LINK.findall(text):
                    lp = urlparse(link).path if link.startswith("http") else link
                    if lp not in done and lp not in todo:
                        todo.append(lp)
            prods = jsonld_products(text, MW)
            if path == MW_CATEGORY and prods:
                cat_ok = True
            new = 0
            for p in prods:
                k = p["sku"] or urlparse(p["url"]).path or p["name"].lower()
                if k and k not in found:
                    p["id"] = k                       # chiave storica compatibile con v1
                    found[k] = p
                    new += 1
            log.append(f"  MW {path} p{page}: {len(prods)} prodotti, {new} nuovi")
            if new == 0:
                break
            time.sleep(random.uniform(1.0, 2.0))
    return list(found.values())


def src_samsung(s, log):
    out = []
    for path in SS_PAGES:
        try:
            prods = jsonld_products(get(s, SS + path), SS)
        except Exception as ex:  # noqa: BLE001
            log.append(f"  Samsung {path}: {ex}")
            continue
        for p in prods:
            p["id"] = "ss:" + (p["sku"] or urlparse(p["url"]).path or p["name"].lower())
            p["seller"] = "Samsung"
            if not OLED.search(p["name"]):
                p["name"] += " OLED"
        out += prods
        log.append(f"  Samsung {path}: {len(prods)} prodotti")
    return out


UE_CODE = re.compile(r"\b((?:QE|TQ)(?:42|48|55|65|77|83)S\d{2}[A-Z][A-Z0-9]{2,6}|OLED(?:42|48|55|65|77|83)[A-Z]\d[A-Z0-9]{2,5})\b")
UE_PRICE = re.compile(r"(?<![\d.,])(\d{1,2}\.\d{3}|\d{1,4})(?:,(\d{2}))?\s?€")   # ogni importo in €
UE_INSTALL = re.compile(r"rat[ae]|al mese|/mese|mensil|tan|taeg|cashback|rimborso|risparmi|sconto di", re.I)


def ue_parse(text):
    """Lettura sperimentale: per ogni codice modello il primo prezzo plausibile che lo segue
    (escluse rate e cashback). Ritorna prodotti con url se trovato."""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", html.unescape(plain))
    out, seen = [], set()
    for m in UE_CODE.finditer(plain):
        code = m.group(1)
        if code in seen:
            continue
        window = plain[m.end(): m.end() + 600]
        nxt = UE_CODE.search(window)                 # non sconfinare nella scheda successiva
        if nxt:
            window = window[:nxt.start()]
        price, prev_end = None, 0
        for pm in UE_PRICE.finditer(window):
            ctx = window[max(prev_end, pm.start() - 30): pm.start()]   # solo dopo l'importo precedente
            prev_end = pm.end()
            if UE_INSTALL.search(ctx):
                continue                              # rata, cashback, risparmio...
            val = float(pm.group(1).replace(".", "") + "." + (pm.group(2) or "00"))
            if 100 <= val <= SANE_MAX:                # sotto 100 € in una scheda TV è una rata o un accessorio
                price = val
                break
        if price is None:
            continue
        seen.add(code)
        href = re.search(r'href="([^"]*' + re.escape(code.lower()) + r'[^"]*)"', text, re.I)
        out.append({"name": f"Samsung OLED {code}" if code.upper().startswith(("QE", "TQ")) else f"LG {code}",
                    "price": price, "url": urljoin(UE, href.group(1)) if href else "",
                    "sku": code, "avail": "", "seller": "Unieuro", "image": "", "id": "ue:" + code,
                    "needs_check": True})
    return out


def src_unieuro(s, log):
    out = []
    for path in UE_PAGES:
        try:
            prods = ue_parse(get(s, UE + path))
        except Exception as ex:  # noqa: BLE001
            log.append(f"  Unieuro {path}: {ex}")
            continue
        out += prods
        log.append(f"  Unieuro {path}: {len(prods)} prodotti letti (sperimentale)")
    return out


def confirm_on_page(s, p):
    """Verifica il prezzo sulla pagina prodotto (dati strutturati). True/False/None=non verificabile."""
    if not p.get("url"):
        return None
    try:
        prods = jsonld_products(get(s, p["url"]), UE)
    except Exception:  # noqa: BLE001
        return None
    prices = [x["price"] for x in prods if x["price"]]
    if not prices:
        return None
    return any(abs(x - p["price"]) <= 1 for x in prices)


SOURCES = [("MediaWorld", src_mediaworld), ("Samsung Shop", src_samsung), ("Unieuro", src_unieuro)]


# ---------------------------------------------------------------------------
def analyze(products, st):
    tvs = []
    for p in products:
        if not p["price"] or not (SANE_MIN <= p["price"] <= SANE_MAX):
            continue
        if not OLED.search(p["name"]) or EXCLUDE.search(p["name"]):
            continue
        if p["avail"] and re.search(r"outofstock|discontinued|soldout", p["avail"], re.I):
            continue
        p["size"] = size_of(p["name"])
        p["key"] = model_key(p["name"])
        tvs.append(p)

    shops = {p["shop"] for p in tvs}
    by_shop_size, cheapest = {}, {}
    for p in tvs:
        if p["size"]:
            by_shop_size.setdefault((p["shop"], p["size"]), []).append(p["price"])
    for (shop, sz), v in by_shop_size.items():
        cheapest.setdefault(shop, {})[sz] = min(v)
    by_key = {}
    for p in tvs:
        if p["key"] and not p.get("needs_check"):      # i prezzi incerti non fanno da riferimento
            by_key.setdefault(p["key"], []).append((p["shop"], p["price"]))

    hits = []
    for p in tvs:
        why, sz, pr, shop = [], p["size"], p["price"], p["shop"]
        if sz:
            others = sorted(x for x in by_shop_size[(shop, sz)] if x != pr)
            if len(others) >= 2 and pr <= SIZE_RATIO * others[0]:
                why.append(f"su {shop} l'OLED {sz}\" più economico dopo questo costa {others[0]:.0f} €")
            if sz in FLOOR and pr < FLOOR[sz]:
                why.append(f"sotto la soglia credibile per un OLED {sz}\" ({FLOOR[sz]} €)")
            smaller = [v for z, v in cheapest[shop].items() if z < sz]
            if smaller and pr < SMALLER_RATIO * min(smaller):
                why.append(f"costa meno degli OLED più piccoli su {shop} (da {min(smaller):.0f} €)")
        if p["key"]:
            other_shops = [(sh, x) for sh, x in by_key.get(p["key"], []) if sh != shop]
            if other_shops:
                sh, best = min(other_shops, key=lambda t: t[1])
                if pr <= CROSS_RATIO * best:
                    why.append(f"lo stesso modello ({p['key']}) su {sh} costa {best:.0f} €")
        hist = [v[0] for v in st["hist"].get(p["id"], {}).values()]
        if len(hist) >= HIST_MIN_N:
            med = statistics.median(hist)
            if pr <= HIST_RATIO * med:
                why.append(f"il suo prezzo abituale è {med:.0f} € (mediana di {len(hist)} rilevazioni)")
        if why:
            hits.append((p, why))
    return tvs, hits, cheapest, shops, by_key


# ---------------------------------------------------------------------------
def load_state():
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("hist", {})
    st.setdefault("alerted", {})
    return st


def save_state(st, tvs, now):
    day = now[:10]
    for p in tvs:
        if not p.get("needs_check"):
            st["hist"].setdefault(p["id"], {})[day] = [p["price"], now]
    cut = (datetime.now() - timedelta(days=60)).isoformat()
    for k in list(st["hist"]):
        st["hist"][k] = {d: v for d, v in st["hist"][k].items() if v[1] >= cut}
        if not st["hist"][k]:
            del st["hist"][k]
    st["alerted"] = {k: v for k, v in st["alerted"].items() if v[1] >= cut}
    STATE_FILE.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")


def tg(method, payload):
    r = http.post(f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_TOKEN')}/{method}",
                  json=payload, timeout=20)
    return r.json()


def is_marketplace(p):
    return bool(p.get("seller")) and not re.search(r"media ?world|samsung|unieuro", p["seller"], re.I)


def alert(p, why, dry, drop_from=None, unverified=False):
    e = html.escape
    mkt = is_marketplace(p)
    if mkt:
        head = "⚠️ ANOMALIA DA VENDITORE TERZO (verifica prima di pagare)"
    elif unverified:
        head = "⚠️ POSSIBILE ERRORE (lettura non confermata: controlla il prezzo)"
    else:
        head = "🚨 PROBABILE ERRORE DI PREZZO"
    seller = p["seller"] if p.get("seller") else p["shop"]
    text = (f"<b>{head}</b>\n<b>{e(p['name'][:150])}</b>\n"
            f"💶 <b>{p['price']:.2f} €</b> su {e(p['shop'])} · venduto da {e(seller)}"
            + (f"  📉 era {drop_from:.0f} €" if drop_from else "") + "\n"
            + "\n".join(f"• {e(w)}" for w in why)
            + ("\n🔎 Controlla valutazioni e storico del venditore: prezzi così bassi da terzi sono spesso truffe."
               if mkt else "\n⚡ Ordina subito: gli errori vengono corretti o annullati in fretta."))
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    url = p["url"] or {"Unieuro": UE + UE_PAGES[0]}.get(p["shop"], "")
    if dry or not os.environ.get("TELEGRAM_TOKEN") or not chat:
        print("\n--- AVVISO (simulato) ---\n" + re.sub(r"</?b>", "", html.unescape(text)) + "\n" + url)
        return
    kb = {"inline_keyboard": [[{"text": f"🛒 Apri su {p['shop']}", "url": url}]]} if url else None
    try:
        res = {}
        if p.get("image"):
            payload = {"chat_id": chat, "photo": p["image"], "caption": text[:1000], "parse_mode": "HTML"}
            if kb:
                payload["reply_markup"] = kb
            res = tg("sendPhoto", payload)
        if not res.get("ok"):
            payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
            if kb:
                payload["reply_markup"] = kb
            tg("sendMessage", payload)
    except Exception as ex:  # noqa: BLE001
        print(f"[!] Telegram: {ex}", file=sys.stderr)


# ---------------------------------------------------------------------------
def run(dry=False, fetch_session=session, sources=None):
    t0 = time.time()
    now = datetime.now().isoformat(timespec="seconds")
    st = load_state()
    if not st.get("hello"):
        msg = "✅ Bot errori di prezzo OLED attivo! Scrivo qui solo quando trovo un prezzo sbagliato."
        if dry or not os.environ.get("TELEGRAM_TOKEN"):
            print("--- MESSAGGIO (simulato) ---\n" + msg)
        else:
            try:
                tg("sendMessage", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID"), "text": msg})
            except Exception as ex:  # noqa: BLE001
                print(f"[!] Telegram: {ex}", file=sys.stderr)
        st["hello"] = now
    log, products, s = [], [], fetch_session()
    for shop, fn in (sources or SOURCES):
        try:
            got = fn(s, log)
        except Exception as ex:  # noqa: BLE001
            log.append(f"  {shop}: errore {ex}")
            got = []
        for p in got:
            p["shop"] = shop
        products += got
    tvs, hits, cheapest, shops, by_key = analyze(products, st)

    sent, unconfirmed = 0, 0
    for p, why in hits:
        prev = st["alerted"].get(p["id"])
        if prev and p["price"] > prev[0] * REALERT_DROP:
            continue
        unverified = False
        if p.get("needs_check"):
            ok = confirm_on_page(s, p)
            if ok is False:                       # la pagina prodotto dice un altro prezzo: lettura sbagliata
                unconfirmed += 1
                st["alerted"][p["id"]] = [p["price"], now]
                continue
            unverified = ok is None
        alert(p, why, dry, drop_from=prev[0] if prev else None, unverified=unverified)
        st["alerted"][p["id"]] = [p["price"], now]
        sent += 1
        time.sleep(0.4)
    save_state(st, tvs, now)

    print("\n".join(log))
    for shop in sorted(shops):
        sizes = " · ".join(f'{sz}"={sum(1 for p in tvs if p["shop"] == shop and p["size"] == sz)} da {v:.0f}€'
                           for sz, v in sorted(cheapest.get(shop, {}).items()))
        print(f"  {shop}: {sum(1 for p in tvs if p['shop'] == shop)} OLED · {sizes or '-'}")
    shared = sum(1 for k, v in by_key.items() if len({sh for sh, _ in v}) > 1)
    print(f"\n✔ {len(products)} prodotti letti · {len(tvs)} OLED validi · {len(hits)} anomalie · {sent} avvisi"
          f"{f' · {unconfirmed} letture Unieuro smentite' if unconfirmed else ''} · {time.time() - t0:.0f}s\n"
          f"  modelli confrontabili tra negozi: {shared} · prodotti con storico: {len(st['hist'])} · "
          f"da venditori terzi: {sum(1 for p in tvs if is_marketplace(p))}")
    if not products:
        print("[!] Nessun prodotto letto da nessun negozio.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Errori di prezzo TV OLED (multi-negozio)")
    ap.add_argument("--dry-run", action="store_true", help="non invia, stampa gli avvisi")
    sys.exit(run(ap.parse_args().dry_run))
