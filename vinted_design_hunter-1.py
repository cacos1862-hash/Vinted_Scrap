#!/usr/bin/env python3
"""
vinted_design_hunter.py  v2.1
(nov 2026: aggiornato alla nuova API Vinted api.vinted.it/svc-catalogue,
 con ripiego automatico sulla pagina /catalog del sito)
Caccia su Vinted a oggetti di design / modernariato venduti sotto valore.

Per ogni annuncio stima il VALORE di mercato (vocabolario di modelli iconici,
designer e marchi), calcola il MARGINE (valore stimato - prezzo) e invia su
Telegram gli affari, con foto e pulsante per aprire subito l'annuncio.

Livelli:
  🔥 TOP        prezzo <= 25% del valore e margine >= 80 €
  ✅ AFFARE     prezzo <= 45% del valore e margine >= 40 €
  🤔 INTUIZIONE nessun marchio riconosciuto ma tanti segnali d'epoca e prezzo
                bassissimo (spesso il venditore non sa cos'ha)

Uso:
  pip install curl_cffi
  python vinted_design_hunter.py                 # giro singolo
  python vinted_design_hunter.py --every 10      # ripete ogni 10 minuti
  python vinted_design_hunter.py --report        # salva anche report HTML
  python vinted_design_hunter.py --telegram-test # messaggio di prova
  python vinted_design_hunter.py --trova-chat-id # mostra il tuo chat id

Variabili d'ambiente: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Senza token gli avvisi vengono stampati a schermo.
"""

import argparse
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    from curl_cffi import requests as http
    USING_CURL = True
except ImportError:
    import requests as http
    USING_CURL = False

BASE = "https://www.vinted.it"
API = "https://api.vinted.it/svc-catalogue/items"   # nuova API (da set. 2026)
TOKEN_COOKIE = "access_token_web"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "vinted_state.json"
REPORT_DIR = HERE / "report_vinted"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Soglie
TOP_RATIO, TOP_PROFIT = 0.25, 80
DEAL_RATIO, DEAL_PROFIT = 0.45, 40
HUNCH_SIGNALS, HUNCH_PRICE = 4, 25
ALERT_HUNCHES = True
MAX_ALERTS_PER_RUN = 25

# ---------------------------------------------------------------------------
# RICERCHE (pagina 1, più recenti prima). Mix di termini "ingenui" e mirati.
# ---------------------------------------------------------------------------
QUERIES = [
    # generiche: chi non sa cosa vende
    "lampada anni 70", "lampada anni 60", "lampada space age", "space age",
    "lampada fungo", "lampada vintage plastica", "lampada vintage arancione",
    "lampada vintage bianca", "lampada da tavolo vintage", "lampada da terra vintage",
    "lampadario anni 70", "lampadario vintage vetro", "plafoniera anni 70",
    "applique anni 70", "lampada cromata", "lampada ottone vintage",
    "lampada vetro murano", "lampada design", "modernariato", "modernariato design",
    "oggetto design anni 70", "pop art anni 70", "mid century", "vaso murano",
    "vetro murano vintage", "vaso vetro sommerso", "ceramica anni 60",
    "vaso ceramica vintage", "sedia vintage plastica", "sedia design vintage",
    "poltrona anni 70", "carrello vintage", "portaoggetti anni 70",
    "orologio anni 70", "orologio da parete vintage", "radio vintage design",
    "televisore vintage", "telefono vintage", "macchina da scrivere vintage",
    "posacenere design", "appendiabiti vintage", "lampada ufo",
    # marchi / designer / modelli
    "guzzini", "harvey guzzini", "meblo", "artemide", "flos", "stilnovo",
    "kartell vintage", "componibili", "oluce", "martinelli luce", "fontana arte",
    "arredoluce", "arteluce", "vistosi", "venini", "mazzega", "brionvega",
    "olivetti", "danese", "alessi vintage", "fornasetti", "bitossi",
    "joe colombo", "panton", "castiglioni", "magistretti", "aulenti",
    "sottsass", "enzo mari", "munari", "zanotta", "poltronova", "gufram",
    "pipistrello lampada", "nesso artemide", "atollo", "eclisse artemide",
    "tizio artemide", "boby carrello", "valentine olivetti", "cifra 3 solari",
    "sedia universale", "telefono grillo",
]

# ---------------------------------------------------------------------------
# VOCABOLARIO
# M = modello iconico (stima affidabile)   D = designer   B = marchio
# S = segnale (punti +/-)   X = esclusione netta
# Valori = prezzo realistico di rivendita usato (prudente), in euro.
# ---------------------------------------------------------------------------
CONF = {"M": 0.9, "D": 0.7, "B": 0.55}
VOCAB = []


def _add(kind, pattern, val, label=None):
    rx = re.compile(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])")
    VOCAB.append((kind, rx, val, label or re.sub(r"[^a-z0-9 ]", "", pattern.split("|")[0])))


def M(p, v, l=None): _add("M", p, v, l)
def D(p, v, l=None): _add("D", p, v, l)
def B(p, v, l=None): _add("B", p, v, l)
def S(p, pts, l=None): _add("S", p, pts, l)
def X(p, l=None): _add("X", p, 0, l)


# ---- Illuminazione: modelli ----
M(r"lampada pipistrello|pipistrello (martinelli|aulenti)|martinelli pipistrello", 400, "Pipistrello Martinelli")
M(r"panthella", 600, "Panthella Panton")
M(r"(lampada )?atollo (oluce|magistretti)|lampada atollo|oluce atollo", 700, "Atollo Oluce")
M(r"lampada nesso|nesso artemide|artemide nesso|nessino", 220, "Nesso/Nessino Artemide")
M(r"lampada eclisse|eclisse artemide|artemide eclisse|eclisse magistretti", 100, "Eclisse Artemide")
M(r"lampada tizio|tizio artemide|artemide tizio|tizio sapper", 180, "Tizio Artemide")
M(r"tolomeo artemide|artemide tolomeo|lampada tolomeo", 120, "Tolomeo Artemide")
M(r"lampada parentesi|parentesi (flos|castiglioni)", 200, "Parentesi Flos")
M(r"lampada taccia|taccia flos|flos taccia", 900, "Taccia Flos")
M(r"toio", 700, "Toio Flos")
M(r"snoopy.{0,20}(flos|castiglioni)|(flos|castiglioni).{0,20}snoopy", 500, "Snoopy Flos")
M(r"arco.{0,15}(flos|castiglioni)|(flos|castiglioni).{0,15}arco", 900, "Arco Flos")
M(r"taraxacum", 600, "Taraxacum Flos")
M(r"gatto.{0,10}flos|flos.{0,10}gatto|lampada gatto", 250, "Gatto Flos")
M(r"splugen ?brau", 500, "Splügen Bräu Flos")
M(r"foglio.{0,10}(flos|scarpa)|lampada foglio", 300, "Foglio Flos")
M(r"biagio.{0,10}flos|lampada biagio", 600, "Biagio Flos")
M(r"frisbi", 300, "Frisbi Flos")
M(r"gibigiana", 400, "Gibigiana Flos")
M(r"dalu", 150, "Dalù Artemide")
M(r"boalum", 500, "Boalum Artemide")
M(r"mezzachimera|lampada chimera|chimera artemide", 300, "Chimera Artemide")
M(r"patroclo", 400, "Patroclo Artemide")
M(r"telegono", 350, "Telegono Artemide")
M(r"pileo|pileino", 600, "Pileo Artemide")
M(r"lampada dania|dania artemide", 150, "Dania Artemide")
M(r"clacson.{0,10}artemide|lampada clacson", 200, "Clacson Artemide")
M(r"lampada ruspa|ruspa martinelli", 300, "Ruspa Martinelli")
M(r"lampada cobra|cobra martinelli|serpente martinelli", 300, "Cobra/Serpente Martinelli")
M(r"spider.{0,15}(oluce|colombo)|(oluce|colombo).{0,15}spider", 300, "Spider Colombo")
M(r"coupe.{0,15}(oluce|colombo)|(oluce|colombo).{0,15}coupe", 400, "Coupé Colombo")
M(r"acrilica.{0,15}colombo|colombo.{0,15}acrilica", 1500, "Acrilica Colombo")
M(r"topo.{0,15}(colombo|stilnovo)", 300, "Topo Colombo")
M(r"hebi", 150, "Hebi Valenti")
M(r"jucker", 200, "Jucker Flos")
M(r"triennale.{0,10}stilnovo|stilnovo.{0,10}triennale|lampada triennale", 600, "Triennale Stilnovo")
M(r"fato.{0,10}stilnovo|stilnovo.{0,10}fato", 200, "Fato Stilnovo")
M(r"pirellina", 600, "Pirellina Fontana Arte")
M(r"bilia", 200, "Bilia Fontana Arte")
M(r"lesbo|saffo", 250, "Lesbo/Saffo Artemide")
M(r"passiflora", 300, "Passiflora Mazzega")
M(r"pulcin[oi].{0,15}(vistosi|murano|vetro)|vistosi pulcin[oi]", 600, "Pulcino Vistosi")
M(r"vaso fazzoletto|fazzoletto.{0,10}(venini|murano|vetro)", 250, "Fazzoletto Venini")
M(r"ph ?[345]|henningsen", 300, "PH Louis Poulsen")
M(r"akari", 150, "Akari Noguchi")
M(r"moon lamp.{0,10}panton|panton.{0,10}flowerpot|flowerpot", 250, "Flowerpot Panton")
# ---- Arredo ----
M(r"componibil[ei]|kartell 49(70|65|85)", 50, "Componibili Kartell")
M(r"boby|bieffeplast", 180, "Boby Colombo")
M(r"panton chair|sedia panton", 250, "Panton Chair")
M(r"sedia universale|universale.{0,10}(kartell|colombo)", 150, "Universale Colombo")
M(r"sedia selene|selene.{0,10}(artemide|magistretti)", 120, "Selene Magistretti")
M(r"plia", 120, "Plia Castelli")
M(r"appendiabiti planta|planta castelli", 120, "Planta Castelli")
M(r"poltrona sacco|sacco zanotta|zanotta sacco", 300, "Sacco Zanotta")
M(r"mezzadro", 500, "Mezzadro Zanotta")
M(r"sgabello sella|sella zanotta", 600, "Sella Zanotta")
M(r"primate", 500, "Primate Zanotta")
M(r"poltrona blow|blow zanotta", 600, "Blow Zanotta")
M(r"joe.{0,15}(poltronova|guanto)|poltrona guanto", 2000, "Joe Poltronova")
M(r"up ?5|up ?50|serie up|donna up", 1500, "Up Pesce")
M(r"pratone", 3000, "Pratone Gufram")
M(r"divano bocca|bocca.{0,10}(gufram|studio 65)", 1500, "Bocca Studio65")
M(r"cactus.{0,10}gufram|gufram.{0,10}cactus", 2000, "Cactus Gufram")
M(r"capitello.{0,10}gufram|gufram.{0,10}capitello", 2000, "Capitello Gufram")
M(r"ultrafragola", 4000, "Ultrafragola")
M(r"superonda", 1500, "Superonda")
M(r"tube chair", 2000, "Tube Chair Colombo")
M(r"poltrona elda", 2000, "Elda Colombo")
M(r"ball chair|globe chair", 2000, "Ball Chair Aarnio")
M(r"bubble chair", 1000, "Bubble Chair")
M(r"pastil|tomato chair", 800, "Pastil/Tomato Aarnio")
M(r"djinn", 800, "Djinn Mourgue")
M(r"tavolo tulip|sedia tulip|tulip.{0,10}(saarinen|knoll)", 400, "Tulip Saarinen")
M(r"wassily", 600, "Wassily Breuer")
M(r"sedia cesca|cesca breuer", 150, "Cesca Breuer")
M(r"poltrona barcelona|barcelona chair", 800, "Barcelona")
M(r"eames lounge", 2500, "Eames Lounge")
M(r"lcw|hang it all", 150, "Eames Vitra")
M(r"(sedia )?diamond bertoia|bertoia diamond|sedia diamond", 300, "Diamond Bertoia")
M(r"carlton.{0,10}(sottsass|memphis)|libreria carlton", 4000, "Carlton Sottsass")
M(r"libreria mobil|mobil kartell", 150, "Mobil Kartell")
M(r"bookworm", 80, "Bookworm Kartell")
# ---- Elettronica / oggetti ----
M(r"brionvega cubo|cubo brionvega|ts ?50[25]", 100, "Cubo Brionvega")
M(r"algol", 250, "Algol Brionvega")
M(r"doney", 300, "Doney Brionvega")
M(r"st ?201|black st", 600, "Black ST201 Brionvega")
M(r"rr ?126|radiofonografo brionvega", 800, "Totem RR126")
M(r"valentine.{0,15}olivetti|olivetti.{0,15}valentine|macchina da scrivere valentine", 150, "Valentine Olivetti")
M(r"lettera ?22", 120, "Lettera 22")
M(r"lettera ?32", 70, "Lettera 32")
M(r"divisumma", 100, "Divisumma Olivetti")
M(r"programma ?101", 3000, "Programma 101")
M(r"telefono grillo|grillo.{0,10}(siemens|zanuso|sip)", 120, "Grillo Zanuso")
M(r"ericofon", 80, "Ericofon")
M(r"cifra ?[35]", 150, "Cifra Solari")
M(r"lorenz static|static.{0,10}lorenz", 150, "Static Lorenz")
M(r"ball clock|george nelson", 200, "Nelson clock")
M(r"videosphere", 400, "Videosphere JVC")
M(r"weltron", 600, "Weltron 2001")
M(r"keracolor", 1500, "Keracolor")
M(r"toot ?a ?loop", 60, "Toot-a-loop")
M(r"predicta", 500, "Philco Predicta")
M(r"putrella", 150, "Putrella Mari")
M(r"pago ?pago", 120, "Pago-Pago Mari")
M(r"calendario timor|timor.{0,10}(danese|mari)", 100, "Timor Mari")
M(r"formosa.{0,10}(danese|mari)|calendario formosa", 100, "Formosa Mari")
M(r"falkland", 300, "Falkland Munari")
M(r"cubo munari|posacenere cubo", 80, "Cubo Munari")
M(r"abitacolo.{0,10}munari|munari.{0,10}abitacolo|letto abitacolo", 400, "Abitacolo Munari")
M(r"juicy salif", 50, "Juicy Salif")
M(r"rimini blu", 80, "Rimini Blu Bitossi")

# ---- Designer ----
for p, v, l in [
    (r"joe colombo", 300, "Joe Colombo"), (r"verner panton|panton", 200, "Panton"),
    (r"eero aarnio|aarnio", 500, "Aarnio"), (r"castiglioni", 200, "Castiglioni"),
    (r"magistretti", 150, "Magistretti"), (r"gae aulenti|aulenti", 200, "Aulenti"),
    (r"sottsass", 200, "Sottsass"), (r"enzo mari", 100, "Enzo Mari"),
    (r"bruno munari|munari", 100, "Munari"), (r"gio ponti", 300, "Gio Ponti"),
    (r"tobia scarpa|afra e tobia", 200, "Tobia Scarpa"), (r"carlo scarpa", 400, "Carlo Scarpa"),
    (r"mangiarotti", 300, "Mangiarotti"), (r"zanuso", 200, "Zanuso"),
    (r"richard sapper|sapper", 100, "Sapper"), (r"mario bellini", 150, "Bellini"),
    (r"ingo maurer", 200, "Ingo Maurer"), (r"luigi colani|colani", 200, "Colani"),
    (r"pierre paulin", 400, "Paulin"), (r"gaetano pesce", 400, "Gaetano Pesce"),
    (r"goffredo reggiani", 200, "G. Reggiani"), (r"gino sarfatti|sarfatti", 600, "Sarfatti"),
    (r"angelo lelii|lelii", 400, "Lelii"), (r"gaetano sciolari|sciolari", 200, "Sciolari"),
    (r"max ingrand", 300, "Max Ingrand"), (r"paolo tilche", 150, "Tilche"),
    (r"tito agnoli", 200, "Agnoli"), (r"rodolfo bonetto|bonetto", 150, "Bonetto"),
    (r"superstudio", 300, "Superstudio"), (r"archizoom", 500, "Archizoom"),
    (r"studio ?65", 400, "Studio65"), (r"cini boeri", 200, "Cini Boeri"),
    (r"elio martinelli", 200, "Elio Martinelli"), (r"cesare leonardi", 300, "Leonardi"),
    (r"piero fornasetti|fornasetti", 250, "Fornasetti"), (r"guido gambone|gambone", 300, "Gambone"),
    (r"marcello fantoni|fantoni", 200, "Fantoni"), (r"aldo londi", 150, "Londi"),
    (r"ettore sottsass", 250, "Sottsass"), (r"alessandro mendini|mendini", 100, "Mendini"),
    (r"massimo vignelli|vignelli", 100, "Vignelli"), (r"flavio poli", 200, "Flavio Poli"),
    (r"archimede seguso", 200, "A. Seguso"), (r"toni zuccheri|zuccheri", 300, "Zuccheri"),
    (r"fulvio bianconi|bianconi", 300, "Bianconi"), (r"alfredo barbini|barbini", 150, "Barbini"),
    (r"carlo nason", 120, "Carlo Nason"), (r"alvar aalto|aalto", 200, "Aalto"),
    (r"arne jacobsen|jacobsen", 200, "Jacobsen"), (r"hans wegner|wegner", 300, "Wegner"),
    (r"isamu noguchi|noguchi", 200, "Noguchi"), (r"eames", 120, "Eames"),
    (r"saarinen", 300, "Saarinen"), (r"bertoia", 200, "Bertoia"),
    (r"charlotte perriand|perriand", 800, "Perriand"), (r"jean prouve|prouve", 1000, "Prouvé"),
    (r"le corbusier|lc ?[234]", 300, "Le Corbusier"), (r"marcel breuer|breuer", 150, "Breuer"),
    (r"mies van der rohe", 400, "Mies"), (r"ron arad", 200, "Ron Arad"),
    (r"tapio wirkkala|wirkkala", 150, "Wirkkala"), (r"timo sarpaneva|sarpaneva", 100, "Sarpaneva"),
    (r"olaf von bohr", 150, "Von Bohr"), (r"ennio lucini", 150, "Lucini"),
]:
    D(p, v, l)

# ---- Marchi ----
for p, v, l in [
    (r"harvey guzzini", 200, "Harvey Guzzini"), (r"meblo", 120, "Meblo"),
    (r"iguzzini|guzzini", 50, "Guzzini"), (r"artemide", 60, "Artemide"),
    (r"flos", 80, "Flos"), (r"stilnovo", 150, "Stilnovo"), (r"arredoluce", 300, "Arredoluce"),
    (r"arteluce", 300, "Arteluce"), (r"fontana ?arte", 200, "Fontana Arte"),
    (r"oluce", 100, "Oluce"), (r"martinelli luce", 120, "Martinelli Luce"),
    (r"valenti luce|valenti milano", 100, "Valenti"), (r"lampada reggiani|reggiani luce|reggiani illuminazione", 80, "Reggiani"),
    (r"targetti", 80, "Targetti"), (r"leucos", 60, "Leucos"), (r"lumi milano", 100, "Lumi"),
    (r"raak", 300, "Raak"), (r"poliarte", 120, "Poliarte"), (r"tre ?ci luce", 80, "Tre Ci"),
    (r"lucitalia", 60, "Lucitalia"), (r"studio tetrarch", 300, "Tetrarch"),
    (r"louis poulsen", 150, "Louis Poulsen"), (r"le klint", 80, "Le Klint"),
    (r"kartell", 40, "Kartell"), (r"poltronova", 300, "Poltronova"), (r"cassina", 300, "Cassina"),
    (r"zanotta", 200, "Zanotta"), (r"gufram", 500, "Gufram"), (r"anonima castelli|castelli.{0,10}(plia|piretti)", 80, "Castelli"),
    (r"knoll", 200, "Knoll"), (r"herman miller", 200, "Herman Miller"), (r"vitra", 80, "Vitra"),
    (r"b ?& ?b italia|c ?& ?b italia", 300, "B&B Italia"),
    (r"bieffeplast", 120, "Bieffeplast"), (r"thonet", 80, "Thonet"), (r"danese milano|danese.{0,10}(mari|munari)", 80, "Danese"),
    (r"alessi", 30, "Alessi"), (r"olivetti", 60, "Olivetti"), (r"brionvega", 100, "Brionvega"),
    (r"solari udine|orologio solari|solari.{0,10}(cifra|dator)", 100, "Solari"), (r"ritz italora|italora", 60, "Italora"),
    (r"venini", 200, "Venini"), (r"barovier", 150, "Barovier"), (r"vistosi", 120, "Vistosi"),
    (r"mazzega", 150, "Mazzega"), (r"seguso", 100, "Seguso"), (r"cenedese", 100, "Cenedese"),
    (r"avem", 150, "AVEM"), (r"salviati", 100, "Salviati"), (r"carlo moretti", 60, "Carlo Moretti"),
    (r"nason", 60, "Nason"), (r"la murrina", 50, "La Murrina"), (r"murano", 35, "Murano"),
    (r"bitossi", 80, "Bitossi"), (r"richard ginori", 50, "Ginori"), (r"san polo", 60, "San Polo"),
    (r"bertoncello", 60, "Bertoncello"), (r"fanciullacci", 50, "Fanciullacci"),
    (r"zaccagnini", 80, "Zaccagnini"), (r"rosenthal", 40, "Rosenthal"),
    (r"iittala", 30, "Iittala"), (r"arabia finland", 40, "Arabia"),
    (r"memphis", 200, "Memphis"), (r"driade", 150, "Driade"),
]:
    B(p, v, l)

# ---- Segnali positivi ----
S(r"space ?age", 3, "space age")
S(r"anni ?'?(60|70|sessanta|settanta)|[67]0 ?'?s|seventies|sixties", 2, "anni 60/70")
S(r"modernariato", 2, "modernariato")
S(r"pop ?art", 2, "pop art")
S(r"mid ?-?century", 2, "mid century")
S(r"design italiano|italian design", 2, "design italiano")
S(r"fungo|mushroom", 2, "fungo")
S(r"ufo|sputnik|atomic[oa]?|spaziale", 2, "spaziale")
S(r"bauhaus", 1, "bauhaus")
S(r"vintage", 1, "vintage")
S(r"acrilico|plexiglass?|perspex|lucite|metacrilato", 1, "acrilico")
S(r"vetroresina|fiberglass|fibra di vetro", 2, "vetroresina")
S(r"opalina|ottone|cromat[oa]|marmo", 1, "materiale")
S(r"firmat[oa]|marchiat[oa]|etichetta originale|signed|con marchio", 2, "firmato")
S(r"eredit\w*|soffitta|cantina|svuota\w*|della nonna|della zia|trovat[oa]", 2, "venditore ignaro")
S(r"non so (il valore|cosa sia|di che marca|la marca)|non conosco", 3, "venditore ignaro")
S(r"made in italy", 1, "made in italy")
# ---- Segnali negativi ----
S(r"stile|tipo|simil\w*|ispirat\w*|look", -2, "-stile/tipo")
S(r"replica|riproduzione|copia|falso|fake|imitazione", -10, "-REPLICA")
S(r"rott[oa]|da riparare|non funzionante|difett\w*|crepat[oa]|scheggiat[oa]", -1, "-difetti")
# ---- Esclusioni nette ----
X(r"t-?shirt|maglia\w*|maglion\w*|felpa|vestit[oi]|abito|jeans|pantalon\w*|gonna|camicia|"
  r"giacca|giubbotto|cappotto|scarpe|sneakers?|stivali|sandali|borsa|zaino|occhiali|"
  r"costume|pigiama|calze|cappello|sciarpa|leggings", "abbigliamento")
X(r"giocattol\w*|peluche|bambola|lego|playmobil|funko|carnevale|halloween|puzzle", "giochi")
X(r"libro|libri|rivista|catalogo|poster|stampa|adesiv\w*|sticker|cover|custodia|"
  r"cartolina|cd|dvd|vinile|quadro su tela", "carta/media")
X(r"orecchin\w*|collana|bracciale|anello|gioiell\w*|spilla|ciondolo", "gioielli")
X(r"ikea|maisons du monde|zara home|h ?& ?m|leroy merlin|westwing|kave home|jysk|shein|temu", "grande distribuzione")


# Oggetti molto replicati: senza indizi di autenticità la fiducia cala
RISKY = re.compile(r"(?<![a-z])(?:eames|panton|tulip|barcelona|wassily|cesca|ball chair|bubble chair|"
                   r"egg chair|arco|ph ?[345]|henningsen|akari|noguchi|flowerpot|fornasetti|bertoia|"
                   r"saarinen|le corbusier|lc ?[234]|mies|componibil[ei]|juicy salif)(?![a-z])")
AUTH = re.compile(r"(?<![a-z])(?:originale|vitra|herman miller|knoll|cassina|flos|louis poulsen|fattura|"
                  r"certificat\w*|etichetta|marchiat\w*|firmat\w*|timbr\w*|kartell|anni ?'?[5-8]0|vintage)(?![a-z])")


# ---------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower())


def parse_price(v):
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def evaluate(item):
    """Restituisce un dict con stima, oppure None se l'annuncio va scartato."""
    title = item.get("title") or ""
    brand = item.get("brand_title") or ""
    text = norm(f"{title} {brand}")
    price = parse_price(item.get("price"))
    if price is None or price <= 0:
        return None

    hits = {"M": [], "D": [], "B": []}
    sig, neg, labels = 0, 0, []
    for kind, rx, val, label in VOCAB:
        if not rx.search(text):
            continue
        if kind == "X":
            return None
        if kind == "S":
            if val > 0:
                sig += val
            else:
                neg += val
            labels.append(label)
        else:
            hits[kind].append((val, label))
            labels.append(label)

    value, tier = 0, None
    if hits["M"]:
        value, tier = max(hits["M"])[0], "M"
    elif hits["D"] or hits["B"]:
        cands = [(v * CONF["D"], v, "D") for v, _ in hits["D"]] + \
                [(v * CONF["B"], v, "B") for v, _ in hits["B"]]
        _, value, tier = max(cands)

    conf = CONF.get(tier, 0)
    n_hits = len(hits["M"]) + len(hits["D"]) + len(hits["B"])
    conf = min(0.95, conf + 0.05 * max(0, n_hits - 1) + 0.02 * min(sig, 5))
    if neg <= -10:            # replica dichiarata
        conf *= 0.15
    else:
        conf = max(0.0, conf + 0.08 * neg)

    if value and RISKY.search(text) and not AUTH.search(text):
        conf *= 0.45
    ev = value * conf
    profit = ev - price
    ratio = price / value if value else None

    level = None
    if value and ratio <= TOP_RATIO and profit >= TOP_PROFIT:
        level = "TOP"
    elif value and ratio <= DEAL_RATIO and profit >= DEAL_PROFIT:
        level = "AFFARE"
    elif not value and sig >= HUNCH_SIGNALS and price <= HUNCH_PRICE and neg > -2:
        level = "INTUIZIONE"

    photo = item.get("photo") or {}
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    url = item.get("url") or f"{BASE}/items/{item.get('id')}"
    if url.startswith("/"):
        url = BASE + url
    return {
        "id": str(item.get("id")), "title": title, "brand": brand, "price": price,
        "total": parse_price(item.get("total_item_price")),
        "value": value, "ev": round(ev), "profit": round(profit),
        "ratio": ratio, "signals": sig, "level": level,
        "labels": list(dict.fromkeys(labels)), "url": url,
        "photo": photo.get("url") or "", "ts": ts,
    }


# ---------------------------------------------------------------------------
class SessionExpired(Exception):
    pass


class VSession:
    """Sessione HTTP + token anonimo Vinted."""

    def __init__(self):
        self.http = http.Session(impersonate="chrome") if USING_CURL else http.Session()
        self.token = None
        self.anon = None
        self.html_mode = False      # True se l'API non risponde: si usa /catalog


def _doc_headers():
    h = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "it-IT,it;q=0.9,en;q=0.5", "Upgrade-Insecure-Requests": "1",
         "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
         "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"}
    if not USING_CURL:
        h["User-Agent"] = UA
    return h


def _api_headers(s, bearer=False):
    h = {"Accept": "application/json, text/plain, */*",
         "Accept-Language": "it-IT,it;q=0.9,en;q=0.5", "Cache-Control": "no-cache",
         "Origin": BASE, "Referer": BASE + "/", "Locale": "it-IT", "Platform": "web",
         "X-Next-App": "marketplace-web", "Sec-Fetch-Dest": "empty",
         "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site"}
    if not USING_CURL:
        h["User-Agent"] = UA
    if s.token:
        h["Cookie"] = f"{TOKEN_COOKIE}={s.token}"
        if bearer:
            h["Authorization"] = f"Bearer {s.token}"
    if s.anon:
        h["X-Anon-Id"] = s.anon
    return h


def make_session():
    s = VSession()
    r = s.http.get(BASE + "/", headers=_doc_headers(), timeout=25)
    for jar in (r.cookies, s.http.cookies):
        try:
            s.token = s.token or jar.get(TOKEN_COOKIE)
        except Exception:  # noqa: BLE001  (cookie duplicati su più domini)
            for c in getattr(jar, "jar", []):
                if getattr(c, "name", "") == TOKEN_COOKIE:
                    s.token = c.value
    s.anon = (r.headers.get("X-Anon-Id") or "").strip() or None
    if r.status_code >= 400 or not s.token:
        print(f"[!] Homepage Vinted: HTTP {r.status_code}, token "
              f"{'ok' if s.token else 'ASSENTE'}", file=sys.stderr)
    return s


def _fetch_api(s, query, max_price):
    params = {"search_text": query, "order": "newest_first", "per_page": 96, "page": 1,
              "price_to": max_price, "currency": "EUR", "time": int(time.time())}
    r = s.http.get(API, params=params, headers=_api_headers(s), timeout=25)
    if r.status_code == 401 and s.token:          # secondo tentativo con bearer
        r = s.http.get(API, params=params, headers=_api_headers(s, True), timeout=25)
    if r.status_code == 401:
        raise SessionExpired()
    if r.status_code == 429:
        print("[!] Rate limit: pausa 60s", file=sys.stderr)
        time.sleep(60)
        return []
    if r.status_code in (403, 404):
        return None                               # -> ripiego HTML
    r.raise_for_status()
    return r.json().get("items") or []


def _ld_products(node):
    if isinstance(node, list):
        for n in node:
            yield from _ld_products(n)
    elif isinstance(node, dict):
        if node.get("@type") == "Product" or (node.get("url") and "/items/" in str(node.get("url"))
                                              and node.get("offers")):
            yield node
        for k in ("itemListElement", "item", "@graph"):
            if k in node:
                yield from _ld_products(node[k])


def _fetch_html(s, query, max_price):
    params = {"search_text": query, "order": "newest_first", "price_to": max_price,
              "currency": "EUR"}
    r = s.http.get(BASE + "/catalog", params=params, headers=_doc_headers(), timeout=25)
    r.raise_for_status()
    out = []
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for p in _ld_products(data):
            url = p.get("url") or ""
            m = re.search(r"/items/(\d+)", url)
            offers = p.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = parse_price(offers.get("price"))
            img = p.get("image") or ""
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url") or img.get("contentUrl") or ""
            if not m or price is None or price > max_price:
                continue
            brand = p.get("brand") or {}
            out.append({"id": m.group(1), "title": p.get("name") or "", "price": price,
                        "url": url if url.startswith("http") else BASE + url,
                        "photo": {"url": img},
                        "brand_title": brand.get("name", "") if isinstance(brand, dict) else ""})
    return out


def fetch(s, query, max_price):
    if not s.html_mode:
        items = _fetch_api(s, query, max_price)
        if items is not None:
            return items
        s.html_mode = True
        print("[i] API non disponibile: passo alla lettura della pagina catalogo", file=sys.stderr)
    return _fetch_html(s, query, max_price)


# ---------------------------------------------------------------------------
def load_state():
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("alerted", {})
    return st


def save_state(st):
    limit = (datetime.now() - timedelta(days=30)).isoformat()
    for k in ("seen", "alerted"):
        st[k] = {i: t for i, t in st[k].items() if t >= limit}
    STATE_FILE.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------------------
ICON = {"TOP": "🔥 TOP AFFARE", "AFFARE": "✅ AFFARE", "INTUIZIONE": "🤔 DA VERIFICARE"}


def age_text(ts):
    if not ts:
        return ""
    mins = int((time.time() - ts) / 60)
    if mins < 0:
        return ""
    return f"🕒 pubblicato {mins} min fa\n" if mins < 120 else f"🕒 pubblicato {mins // 60} h fa\n"


def caption(r):
    e = html.escape
    lines = [f"<b>{ICON[r['level']]}</b>", f"<b>{e(r['title'][:120])}</b>"]
    tot = f" (tot. {r['total']:.2f} €)" if r["total"] else ""
    lines.append(f"💶 <b>{r['price']:.2f} €</b>{tot}")
    if r["value"]:
        lines.append(f"💎 valore stimato ~{r['value']} € · prezzo al {r['ratio']:.0%}")
        lines.append(f"📈 margine atteso ~{r['profit']} €")
    lines.append(f"🏷 {e(', '.join(r['labels'][:6]))}")
    return "\n".join(lines) + "\n" + age_text(r["ts"])


def tg(method, payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    r = http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    return r.json()


def send_text(text, dry=False):
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not os.environ.get("TELEGRAM_TOKEN") or not chat:
        print(f"\n--- MESSAGGIO TELEGRAM (simulato) ---\n{text}")
        return
    try:
        tg("sendMessage", {"chat_id": chat, "text": text})
    except Exception as ex:  # noqa: BLE001
        print(f"[!] Telegram: {ex}", file=sys.stderr)


def send_alert(r, dry):
    text = caption(r)
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not os.environ.get("TELEGRAM_TOKEN") or not chat:
        print("\n--- AVVISO TELEGRAM (simulato) ---\n" + re.sub(r"</?b>", "", text) + r["url"])
        return
    kb = {"inline_keyboard": [[{"text": "🛒 Apri su Vinted", "url": r["url"]}]]}
    try:
        res = {}
        if r["photo"]:
            res = tg("sendPhoto", {"chat_id": chat, "photo": r["photo"], "caption": text,
                                   "parse_mode": "HTML", "reply_markup": kb})
        if not res.get("ok"):
            tg("sendMessage", {"chat_id": chat, "text": text + r["url"], "parse_mode": "HTML",
                               "reply_markup": kb, "disable_web_page_preview": False})
    except Exception as ex:  # noqa: BLE001
        print(f"[!] Telegram: {ex}", file=sys.stderr)


# ---------------------------------------------------------------------------
def write_report(results):
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"report_{datetime.now():%Y%m%d_%H%M}.html"
    e = html.escape
    cards = "".join(
        f'<a class="c" href="{e(r["url"])}" target="_blank"><img src="{e(r["photo"])}" loading="lazy" alt="">'
        f'<div class="b"><span class="l {r["level"] or ""}">{e(ICON.get(r["level"], "—"))}</span>'
        f'<div class="t">{e(r["title"])}</div><div class="p">{r["price"]:.2f} €'
        f'{(" · valore ~%d €" % r["value"]) if r["value"] else ""}</div>'
        f'<div class="r">{e(", ".join(r["labels"]))}</div></div></a>' for r in results)
    css = (":root{--bg:#f6f4ef;--c:#fff;--f:#1d1d1b;--m:#6b6b66}"
           "@media(prefers-color-scheme:dark){:root{--bg:#141412;--c:#1f1f1c;--f:#eee;--m:#9a9a92}}"
           "body{margin:0;font:15px system-ui;background:var(--bg);color:var(--f)}"
           ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;padding:16px}"
           ".c{background:var(--c);border-radius:10px;overflow:hidden;color:inherit;text-decoration:none}"
           ".c img{width:100%;aspect-ratio:1;object-fit:cover;background:#8883}.b{padding:10px}"
           ".t{font-weight:600;margin:6px 0}.p{color:#d9531e;font-weight:700}.r{color:var(--m);font-size:12px}"
           ".l{font-size:11px;font-weight:700}.TOP{color:#e0441b}.AFFARE{color:#1e9e52}.INTUIZIONE{color:#2a7de1}")
    path.write_text(f'<!doctype html><meta charset="utf-8"><meta name="viewport" '
                    f'content="width=device-width,initial-scale=1"><title>Vinted hunter</title>'
                    f'<style>{css}</style><div class="g">{cards}</div>', encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
def run_once(args, fetcher=fetch, session_factory=make_session):
    t0 = time.time()
    st = load_state()
    now = datetime.now().isoformat(timespec="seconds")
    if not st.get("hello"):
        send_text("✅ Vinted design hunter attivo! Ti scrivo qui appena trovo un affare.", args.dry_run)
        st["hello"] = now
    last_err = ""
    session = session_factory()
    queries = args.query or QUERIES
    found, n_items, errors = {}, 0, 0

    for qi, q in enumerate(queries, 1):
        try:
            try:
                items = fetcher(session, q, args.max_price)
            except SessionExpired:
                session = session_factory()
                items = fetcher(session, q, args.max_price)
        except Exception as ex:  # noqa: BLE001
            errors += 1
            last_err = str(ex)[:150]
            print(f"[{qi}/{len(queries)}] {q}: errore {ex}", file=sys.stderr)
            if errors >= 5 and errors == qi:
                print("[!] Troppi errori consecutivi: Vinted sta bloccando. Interrompo.", file=sys.stderr)
                break
            continue
        n_items += len(items)
        for it in items:
            iid = str(it.get("id"))
            if iid in found:
                continue
            ev = evaluate(it)
            if ev and (ev["level"] or args.report):
                found[iid] = ev
        if not args.fast:
            time.sleep(random.uniform(1.2, 2.5))

    blocked = n_items == 0 and errors > 0
    if blocked:
        last_warn = st.get("warn", "")
        if not last_warn or last_warn < (datetime.now() - timedelta(hours=6)).isoformat():
            send_text(f"⚠️ Vinted design hunter: nessun annuncio letto ({errors} errori).\n"
                      f"Ultimo errore: {last_err}", args.dry_run)
            st["warn"] = now
    else:
        st.pop("warn", None)

    order = {"TOP": 0, "AFFARE": 1, "INTUIZIONE": 2, None: 3}
    results = sorted(found.values(), key=lambda r: (order[r["level"]], -r["profit"], r["price"]))
    to_alert = [r for r in results if r["level"] and r["id"] not in st["alerted"]
                and (ALERT_HUNCHES or r["level"] != "INTUIZIONE")][:MAX_ALERTS_PER_RUN]
    for r in to_alert:
        send_alert(r, args.dry_run)
        st["alerted"][r["id"]] = now
        time.sleep(0.4)
    for r in results:
        st["seen"].setdefault(r["id"], now)
    save_state(st)

    if args.report:
        print(f"[i] report: {write_report(results)}")
    deals = sum(1 for r in results if r["level"])
    print(f"\n✔ {len(queries)} ricerche · {n_items} annunci analizzati · {deals} affari · "
          f"{len(to_alert)} avvisi nuovi · {time.time() - t0:.0f}s")
    if blocked:
        print("[!] Nessun annuncio letto: controlla il log sopra.", file=sys.stderr)
    return results, blocked


def main():
    ap = argparse.ArgumentParser(description="Caccia al design sottoprezzato su Vinted")
    ap.add_argument("--max-price", type=float, default=200, help="prezzo massimo € (def. 200)")
    ap.add_argument("-q", "--query", action="append", help="query custom (ripetibile)")
    ap.add_argument("--every", type=int, default=0, help="ripeti ogni N minuti")
    ap.add_argument("--report", action="store_true", help="salva report HTML")
    ap.add_argument("--dry-run", action="store_true", help="non invia, stampa gli avvisi")
    ap.add_argument("--fast", action="store_true", help="nessuna pausa tra ricerche (rischioso)")
    ap.add_argument("--telegram-test", action="store_true", help="invia un messaggio di prova")
    ap.add_argument("--trova-chat-id", action="store_true", help="mostra i chat id del bot")
    args = ap.parse_args()

    if args.trova_chat_id:
        res = tg("getUpdates", {})
        for u in res.get("result", []):
            c = (u.get("message") or {}).get("chat", {})
            print(f"chat_id: {c.get('id')}  ({c.get('first_name', '')} {c.get('username', '')})")
        if not res.get("result"):
            print("Nessun messaggio: scrivi /start al tuo bot e riprova.")
        return
    if args.telegram_test:
        print(tg("sendMessage", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
                                 "text": "✅ Vinted design hunter collegato!"}))
        return

    if not USING_CURL:
        print("[i] curl_cffi non installato: uso requests (più facile essere bloccati).")
    while True:
        _, blocked = run_once(args)
        if args.every <= 0:
            sys.exit(1 if blocked else 0)
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()
