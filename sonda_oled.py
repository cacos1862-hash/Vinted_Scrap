#!/usr/bin/env python3
"""
sonda_oled.py — giro di ricognizione PRIMA di costruire il bot errori-prezzo OLED.

Per ogni sito candidato prova a leggere una pagina di TV OLED e riporta:
  - codice HTTP e se c'è un blocco anti-bot / captcha
  - blocchi JSON-LD (dati strutturati) e loro tipo
  - quanti prezzi in € e quanti codici modello OLED riconosce
  - qualche esempio, per capire come leggere la pagina

Uso:  pip install curl_cffi && python sonda_oled.py
"""
import json
import re
import sys
import time

try:
    from curl_cffi import requests as http
    SESSION = http.Session(impersonate="chrome")
except ImportError:
    import requests as http
    SESSION = http.Session()
    SESSION.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SESSION.headers.update({"Accept-Language": "it-IT,it;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})

TARGETS = [
    ("Trovaprezzi OLED", "https://www.trovaprezzi.it/televisori-lcd-plasma/offerte/tv-oled"),
    ("Trovaprezzi OLED 55", "https://www.trovaprezzi.it/televisori-lcd-plasma/offerte/tv-oled-55"),
    ("idealo ricerca", "https://www.idealo.it/MainSearchProductCategory.html?q=tv+oled"),
    ("MediaWorld OLED 55", "https://www.mediaworld.it/it/list/tv-oled-55-pollici"),
    ("Unieuro ricerca", "https://www.unieuro.it/online/?q=tv%20oled"),
    ("Euronics ricerca", "https://www.euronics.it/search?q=tv+oled"),
    ("Amazon ricerca", "https://www.amazon.it/s?k=tv+oled"),
    ("ePrice ricerca", "https://www.eprice.it/sa/?qs=tv+oled"),
    ("Yeppon ricerca", "https://www.yeppon.it/ricerca/?q=tv+oled"),
    ("Monclick ricerca", "https://www.monclick.it/ricerca?q=tv+oled"),
]

MODEL = re.compile(
    r"\b(OLED(?:42|48|55|65|77|83|97)[A-Z]\d[A-Z0-9]{2,6}"          # LG
    r"|QE(?:42|48|55|65|77|83)S[89]\d[A-Z][A-Z0-9]{0,6}"           # Samsung
    r"|(?:K|XR)-?(?:55|65|77|83)(?:A|XR)[0-9][A-Z0-9]{1,5}"         # Sony
    r"|(?:42|48|55|65|77)OLED\d{3}(?:/\d{2})?"                       # Philips
    r"|TX-(?:42|48|55|65|77)[A-Z]{1,2}\d{3,4}[A-Z]?"                 # Panasonic
    r"|(?:55|65)A[89]\d[A-Z]?)\b")                                    # Hisense
PRICE = re.compile(r"(\d{1,2}[.\s]?\d{3}|\d{2,4})[,.](\d{2})\s?€|€\s?(\d{1,2}[.\s]?\d{3}|\d{2,4})[,.](\d{2})")
BLOCK = re.compile(r"captcha|are you a robot|access denied|attention required|cf-chl|"
                   r"datadome|perimeterx|px-captcha|robot check|bot detection|unusual traffic", re.I)


def jsonld_summary(htmltext):
    types, samples = {}, []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', htmltext, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            types["(non valido)"] = types.get("(non valido)", 0) + 1
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            t = ",".join(t) if isinstance(t, list) else str(t)
            types[t] = types.get(t, 0) + 1
            if t in ("Product", "Offer", "AggregateOffer") and len(samples) < 3:
                offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
                samples.append({k: node.get(k) or offers.get(k) for k in
                                ("name", "price", "lowPrice", "highPrice", "offerCount", "sku", "mpn")
                                if node.get(k) or offers.get(k)})
            for k in ("itemListElement", "item", "@graph", "offers"):
                if k in node:
                    stack.append(node[k])
    return types, samples


def probe(name, url):
    t0 = time.time()
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
    except Exception as ex:  # noqa: BLE001
        print(f"\n### {name}\n  ❌ errore di rete: {str(ex)[:150]}")
        return
    body = r.text or ""
    blocked = BLOCK.search(body[:50000])
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    models = sorted(set(MODEL.findall(body)))
    prices = PRICE.findall(body)
    types, samples = jsonld_summary(body)
    nextdata = "__NEXT_DATA__" in body
    state = re.search(r"window\.__(?:INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE)__", body)
    ok = r.status_code == 200 and not blocked and (models or samples)
    print(f"\n### {name}  {'✅' if ok else ('🚫' if blocked or r.status_code in (403, 429, 503) else '⚠️')}")
    print(f"  HTTP {r.status_code} · {len(body) // 1024} KB · {time.time() - t0:.1f}s · url finale: {str(r.url)[:90]}")
    print(f"  titolo: {(title.group(1).strip()[:80] if title else '-')}")
    if blocked:
        print(f"  blocco anti-bot rilevato: '{blocked.group(0)}'")
    print(f"  prezzi € trovati: {len(prices)} · codici modello OLED: {len(models)}"
          + (f" (es. {', '.join(models[:5])})" if models else ""))
    print(f"  JSON-LD: {types or 'nessuno'}" + (" · __NEXT_DATA__ presente" if nextdata else "")
          + (f" · {state.group(0)} presente" if state else ""))
    for s in samples:
        print(f"    esempio: {json.dumps(s, ensure_ascii=False)[:180]}")


def main():
    print(f"Sonda OLED · {len(TARGETS)} siti · motore HTTP: "
          f"{'curl_cffi (chrome)' if 'curl_cffi' in http.__name__ else 'requests'}")
    for name, url in TARGETS:
        probe(name, url)
        time.sleep(2)
    print("\nFine. Manda lo screenshot di questo log.")


if __name__ == "__main__":
    sys.exit(main())
