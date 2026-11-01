#!/usr/bin/env python3
"""
oled_errori.py  v1.1 — caccia agli ERRORI DI PREZZO (non agli sconti) sui TV OLED.

Fonte: MediaWorld, categoria completa TV OLED (tutte le taglie, ~190 prodotti).
Le offerte di venditori terzi del marketplace sono segnalate a parte (rischio truffa).

Un prezzo è segnalato come 🚨 ERRORE PROBABILE se vale almeno una regola forte:
  A) taglia     costa <= 50% dell'OLED più economico (dopo di lui) della stessa taglia
  B) storico    costa <= 50% della mediana dei prezzi registrati per quel prodotto
  C) soglia     costa meno della soglia minima credibile per un OLED nuovo di quella taglia
  D) incoerenza costa meno del 60% dell'OLED più economico di una taglia PIÙ PICCOLA
Esclusi: usato, ricondizionato, esposizione, prodotti non disponibili.

Uso:   pip install curl_cffi && python oled_errori.py [--dry-run]
Env:   TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (gli stessi del bot Vinted)
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

BASE = "https://www.mediaworld.it"
SEEDS = ["/it/category/tv-oled-400103.html",          # tutte le taglie
         "/it/list/tv-oled-55-pollici", "/it/list/tv-oled-65-pollici"]   # riserva se la categoria cambia
LIST_LINK = re.compile(r'href="((?:https://www\.mediaworld\.it)?/it/list/[a-z0-9-]*oled[a-z0-9-]*)"', re.I)
STATE_FILE = Path(__file__).resolve().parent / "oled_state.json"
MAX_PAGES = 30                 # pagine per lista (la categoria ne ha ~17)
MAX_LISTS = 15                 # liste totali per giro

# --- Regole -----------------------------------------------------------------
SIZE_RATIO = 0.50              # A
HIST_RATIO = 0.50              # B
HIST_MIN_N = 3
SMALLER_RATIO = 0.60           # D
FLOOR = {42: 300, 48: 350, 55: 400, 65: 550, 77: 900, 83: 1200, 97: 5000}   # C (euro)
REALERT_DROP = 0.90            # nuovo avviso se scende ancora del 10%

EXCLUDE = re.compile(r"ricondizionat\w*|usat[oa]|seconda vita|esposizion\w*|espost[oa]|"
                     r"b-?grade|outlet|danneggiat\w*|imballo danneggiato|refurbished|"
                     r"staffa|supporto|telecomando|cavo|pellicola|soundbar|monitor", re.I)
OLED = re.compile(r"oled", re.I)
SIZE_RX = [re.compile(r"\b(42|48|55|65|77|83|97)\s*(?:\"|”|''|pollici|poll\.|inch)", re.I),
           re.compile(r"\b(?:OLED|QE|K-?|XR-?|TX-?)(42|48|55|65|77|83|97)[A-Z]", re.I),
           re.compile(r"\b(42|48|55|65|77|83|97)(?:OLED|WN|WM|A[89]\d)", re.I)]


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


def products_from_html(text):
    """Prodotti dai blocchi JSON-LD della pagina lista."""
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
                price = price_of(offers) or price_of(node.get("price"))
                url = node.get("url") or offers.get("url") or ""
                avail = str(offers.get("availability") or "")
                brand = node.get("brand")
                seller = offers.get("seller")
                seller = (seller.get("name") if isinstance(seller, dict) else seller) or ""
                out.append({
                    "name": html.unescape(str(node.get("name") or "")).strip(),
                    "price": price,
                    "url": urljoin(BASE, url) if url else "",
                    "sku": str(node.get("sku") or node.get("gtin13") or node.get("mpn") or ""),
                    "avail": avail.rsplit("/", 1)[-1],
                    "brand": brand.get("name") if isinstance(brand, dict) else (brand or ""),
                    "seller": str(seller).strip(),
                    "image": (node.get("image")[0] if isinstance(node.get("image"), list)
                              else node.get("image")) or "",
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


def key_of(p):
    return p["sku"] or urlparse(p["url"]).path or p["name"].lower()


# ---------------------------------------------------------------------------
def crawl(s, log):
    """Scopre le liste OLED e scorre le pagine; ritorna {chiave: prodotto}."""
    todo, done, found, errors = list(SEEDS), set(), {}, 0
    while todo and len(done) < MAX_LISTS:
        path = todo.pop(0)
        path = urlparse(path).path if path.startswith("http") else path
        if path in done:
            continue
        if found and not path.startswith("/it/category"):
            break                      # la categoria ha già dato tutto: le liste servono solo di riserva
        done.add(path)
        for page in range(1, MAX_PAGES + 1):
            url = BASE + path + (f"?page={page}" if page > 1 else "")
            try:
                text = get(s, url)
            except Exception as ex:  # noqa: BLE001
                errors += 1
                log.append(f"  {path} p{page}: {ex}")
                break
            if page == 1:
                for link in LIST_LINK.findall(text):
                    lp = urlparse(link).path if link.startswith("http") else link
                    if lp not in done and lp not in todo:
                        todo.append(lp)
            prods = products_from_html(text)
            new = 0
            for p in prods:
                k = key_of(p)
                if k and k not in found:
                    found[k] = p
                    new += 1
            log.append(f"  {path} p{page}: {len(prods)} prodotti, {new} nuovi")
            if new == 0:
                break
            time.sleep(random.uniform(1.0, 2.0))
    return found, errors


def analyze(products, st):
    """Applica le regole A-D. Ritorna lista di (prodotto, motivi)."""
    tvs = []
    for k, p in products.items():
        if not p["price"] or not OLED.search(p["name"]) or EXCLUDE.search(p["name"]):
            continue
        if p["avail"] and re.search(r"outofstock|discontinued|soldout", p["avail"], re.I):
            continue
        p["size"] = size_of(p["name"])
        p["key"] = k
        tvs.append(p)

    by_size = {}
    for p in tvs:
        if p["size"]:
            by_size.setdefault(p["size"], []).append(p["price"])
    cheapest = {sz: min(v) for sz, v in by_size.items()}

    hits = []
    for p in tvs:
        why, sz, pr = [], p["size"], p["price"]
        if sz:
            others = sorted(x for x in by_size[sz] if x != pr) or []
            if len(others) >= 2 and pr <= SIZE_RATIO * others[0]:
                why.append(f"il {sz}\" OLED più economico dopo questo costa {others[0]:.0f} €")
            if sz in FLOOR and pr < FLOOR[sz]:
                why.append(f"sotto la soglia credibile per un OLED {sz}\" ({FLOOR[sz]} €)")
            smaller = [cheapest[z] for z in cheapest if z < sz]
            if smaller and pr < SMALLER_RATIO * min(smaller):
                why.append(f"costa meno degli OLED più piccoli (da {min(smaller):.0f} €)")
        hist = [v[0] for v in st["hist"].get(p["key"], {}).values()]
        if len(hist) >= HIST_MIN_N:
            med = statistics.median(hist)
            if pr <= HIST_RATIO * med:
                why.append(f"il suo prezzo abituale è {med:.0f} € (mediana di {len(hist)} rilevazioni)")
        if why:
            hits.append((p, why))
    return tvs, hits, cheapest


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
    for p in tvs:                                  # una rilevazione al giorno per prodotto
        st["hist"].setdefault(p["key"], {})[day] = [p["price"], now]
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
    return bool(p.get("seller")) and not re.search(r"media ?world", p["seller"], re.I)


def alert(p, why, dry, drop_from=None):
    e = html.escape
    mkt = is_marketplace(p)
    head = ("⚠️ ANOMALIA DA VENDITORE TERZO (verifica prima di pagare)" if mkt
            else "🚨 PROBABILE ERRORE DI PREZZO")
    text = (f"<b>{head}</b>\n<b>{e(p['name'][:150])}</b>\n"
            f"💶 <b>{p['price']:.2f} €</b> su MediaWorld"
            + (f" · venduto da <b>{e(p['seller'])}</b>" if mkt else " · venduto da MediaWorld")
            + (f"  📉 era {drop_from:.0f} €" if drop_from else "") + "\n"
            + "\n".join(f"• {e(w)}" for w in why)
            + ("\n🔎 Controlla valutazioni e storico del venditore: prezzi così bassi da terzi sono spesso truffe."
               if mkt else "\n⚡ Ordina subito: gli errori vengono corretti o annullati in fretta."))
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not os.environ.get("TELEGRAM_TOKEN") or not chat:
        print("\n--- AVVISO (simulato) ---\n" + re.sub(r"</?b>", "", text) + "\n" + p["url"])
        return
    kb = {"inline_keyboard": [[{"text": "🛒 Apri su MediaWorld", "url": p["url"]}]]}
    try:
        res = {}
        if p.get("image"):
            res = tg("sendPhoto", {"chat_id": chat, "photo": p["image"], "caption": text[:1000],
                                   "parse_mode": "HTML", "reply_markup": kb})
        if not res.get("ok"):
            tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": kb})
    except Exception as ex:  # noqa: BLE001
        print(f"[!] Telegram: {ex}", file=sys.stderr)


# ---------------------------------------------------------------------------
def run(dry=False, fetch_session=session):
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
    log = []
    products, errors = crawl(fetch_session(), log)
    tvs, hits, cheapest = analyze(products, st)

    sent = 0
    for p, why in hits:
        prev = st["alerted"].get(p["key"])
        if prev and p["price"] > prev[0] * REALERT_DROP:
            continue
        alert(p, why, dry, drop_from=prev[0] if prev else None)
        st["alerted"][p["key"]] = [p["price"], now]
        sent += 1
        time.sleep(0.4)
    save_state(st, tvs, now)

    print("\n".join(log))
    sizes = " · ".join(f'{sz}"={sum(1 for p in tvs if p["size"] == sz)} da {cheapest[sz]:.0f}€'
                       for sz in sorted(cheapest))
    print(f"\n✔ {len(products)} prodotti letti · {len(tvs)} OLED validi · {len(hits)} anomalie · "
          f"{sent} avvisi · {time.time() - t0:.0f}s\n  per taglia: {sizes or '-'}\n"
          f"  prodotti con storico: {len(st['hist'])} · da venditori terzi: "
          f"{sum(1 for p in tvs if is_marketplace(p))}")
    if not products:
        print("[!] Nessun prodotto letto: MediaWorld potrebbe aver bloccato o cambiato pagina.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Errori di prezzo TV OLED (MediaWorld)")
    ap.add_argument("--dry-run", action="store_true", help="non invia, stampa gli avvisi")
    sys.exit(run(ap.parse_args().dry_run))
