#!/usr/bin/env python3
"""RIS-Feed: holt neue Dokumente aus der RIS-OGD-API (data.bka.gv.at), speichert
sie samt vollem Text und stellt sie auf dem Zweig "data" des GitHub-Repos bereit.
Nur Standardbibliothek, Python >= 3.8.

Aufruf:
  python3 ris_fetch.py            # holen, speichern, veroeffentlichen
  python3 ris_fetch.py --no-push  # holen und lokal speichern, kein git
  python3 ris_fetch.py --probe    # nur Verbindung/Struktur pro Quelle testen
  python3 ris_fetch.py --all      # alles im Wochenfenster erneut liefern

Code liegt auf Zweig "main" (dieses Repo), Daten in einem zweiten lokalen
Ordner (RIS_DATA_DIR, Standard: ../ris-feed-data) auf Zweig "data":
  feed/YYYY-MM-DD.json     neue Dokumente seit dem letzten Lauf
  feed/latest.json         Kopie der neuesten Datei
  voll/YYYY-MM-DD/<id>.txt voller Text je Dokument (ohne Laengenlimit)
  gesetze/<Kuerzel>/<§>.txt  Gesetzestexte (konsolidiertes Bundesrecht), DAUERHAFT;
                           <§>_alt_<Stichtag>.txt = Fassung vor Inkrafttreten.
                           Vorhandene Paragraphen werden nicht neu abgerufen
                           (geltende Fassung nach 30 Tagen aufgefrischt).
  state/seen.json          bereits gelieferte Dokument-IDs
Aufbewahrung: 7 Tage. Der Zweig "data" wird bei jedem Lauf als einzelner
neuer Commit hochgeladen (Force-Push), damit auch der Git-Verlauf nichts
Aelteres enthaelt.
"""
import hashlib
import html as htmllib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = "https://data.bka.gv.at/ris/api/v2.6"
UA = "ris-feed/1.0 (privater Abruf, 1x taeglich, nachts)"
PAUSE_S = 2.0          # RIS-Netiquette: 1-2 s zwischen Seiten
MAX_PAGES = 6          # Sicherheitsgrenze pro Quelle
PAGE_SIZE = "OneHundred"
# Fuer BgblAuth ignoriert die API ImRisSeit und liefert das ganze Jahr:
# dort selbst nach Ausgabedatum der letzten Tage filtern, mehr Seiten lesen.
DATE_FILTER = {"BgblAuth": ("Ausgabedatum", 8, 15)}  # Feld, Tage, max. Seiten
RIS_SINCE = "EinerWoche"   # Fenster; Duplikate filtert state/seen.json
REPO = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("RIS_DATA_DIR") or os.path.join(os.path.dirname(REPO), "ris-feed-data")
RETENTION_DAYS = 7
TZ = ZoneInfo("Europe/Vienna")

# (Endpunkt, Applikation, Vorfilter)
SOURCES = [
    ("Bundesrecht", "BgblAuth", None),
    ("Bundesrecht", "Begut", None),
    ("Bundesrecht", "RegV", None),
    ("Judikatur", "Justiz", "OGH_STRAF"),
    ("Judikatur", "Vfgh", "THEMEN"),
    ("Judikatur", "Vwgh", "THEMEN"),
    ("Judikatur", "Lvwg", "THEMEN"),
]

THEMEN_RE = re.compile(
    r"\b(SPG|Sicherheitspolizei|StPO|Strafprozess|StGB|Strafgesetz|StVO|"
    r"Stra(ß|ss)enverkehrsordnung|KFG|Kraftfahrgesetz|FSG|F(ü|ue)hrerschein|"
    r"VStG|Verwaltungsstrafgesetz|AVG|WaffG|Waffengesetz|SMG|Suchtmittel|"
    r"FPG|Fremdenpolizei|BFA-VG|MeldeG|Meldegesetz|VersG|Versammlung|"
    r"PolKG|Polizeikooperation|Ma(ß|ss)nahmenbeschwerde|Organmandat|"
    r"Betrug|Datenverarbeitung|Cyber|Hacking|Phishing|Identit(ä|ae)tsdiebstahl|"
    r"Polizei|Exekutive|Beweisverwertung|Festnahme|Hausdurchsuchung)",
    re.I,
)
OS_RE = re.compile(r"\b1[1-5]\s?Os\b")  # OGH-Strafsachen (11 bis 15 Os)

FIELDS = [
    "Kurztitel", "Titel", "Langtitel", "Geschaeftszahl", "Entscheidungsdatum",
    "Veroeffentlicht", "Geaendert", "Inkrafttretensdatum", "Ausgabedatum",
    "BgblNummer", "Typ", "Dokumenttyp", "Gericht", "Entscheidungsart",
    "Normen", "Schlagworte", "Beschreibung", "Bundesministerium",
]


def http_get(endpoint, params):
    url = f"{BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")), url
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # Netz, Timeout, JSON
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{last} bei {url}")


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def find_key(obj, key):
    """Erstes Vorkommen eines Schluessels irgendwo in verschachtelten Daten."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def parse_page(js):
    res = js.get("OgdSearchResult", {})
    if "Error" in res:
        raise RuntimeError(f"API-Fehler: {json.dumps(res['Error'], ensure_ascii=False)[:300]}")
    docs = res.get("OgdDocumentResults", {})
    hits = docs.get("Hits", {})
    total = int(hits.get("#text", 0)) if isinstance(hits, dict) else int(hits or 0)
    size = int(hits.get("@pageSize", 100)) if isinstance(hits, dict) else 100
    return total, size, as_list(docs.get("OgdDocumentReference"))


def to_record(app, ref):
    data = ref.get("Data", ref)
    meta = data.get("Metadaten", {})
    doc_id = find_key(meta.get("Technisch", {}), "ID") or find_key(meta, "ID")
    rec = {"id": doc_id, "app": app}
    for f in FIELDS:
        v = find_key(meta, f)
        if isinstance(v, dict) and set(v) == {"item"}:
            v = v["item"]
        if v not in (None, "", {}, []):
            rec[f] = v
    urls = find_key(data.get("Dokumentliste", {}), "ContentUrl")
    for u in as_list(urls):
        if isinstance(u, dict) and u.get("DataType") == "Html":
            rec["inhalt_url"] = u.get("Url")
    if doc_id:
        rec["url"] = ("https://www.ris.bka.gv.at/Dokument.wxe?Abfrage="
                      f"{app}&Dokumentnummer={doc_id}")
    rec["_text"] = json.dumps(meta, ensure_ascii=False)
    return rec


def keep(rec, prefilter):
    if prefilter is None:
        return True
    text = rec["_text"]
    if prefilter == "OGH_STRAF":
        return bool(OS_RE.search(text))
    return bool(THEMEN_RE.search(text))


def fetch_source(endpoint, app, prefilter):
    out, page, total_pages = [], 1, 1
    max_pages, cutoff, dfield = MAX_PAGES, None, None
    reached_old = False  # Datumsfilter: Seite ohne Treffer im Zeitraum erreicht
    if app in DATE_FILTER:
        dfield, days, max_pages = DATE_FILTER[app]
        cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    while page <= min(total_pages, max_pages):
        js, _ = http_get(endpoint, {
            "Applikation": app, "ImRisSeit": RIS_SINCE,
            "DokumenteProSeite": PAGE_SIZE, "Seitennummer": page})
        total, size, refs = parse_page(js)
        total_pages = max(1, math.ceil(total / max(size, 1)))
        recs = [to_record(app, x) for x in refs]
        if cutoff:
            recs = [r for r in recs if str(r.get(dfield, ""))[:10] >= cutoff]
            if not recs:  # Liste ist nach Datum absteigend: ab hier nur noch Älteres
                reached_old = True
                break
        out += [r for r in recs if keep(r, prefilter)]
        page += 1
        if page <= min(total_pages, max_pages):
            time.sleep(PAUSE_S)
    return out, (total_pages > max_pages and not reached_old)


EXCERPT = 1500  # Anfang des Textes direkt im JSON (Schnellsicht)


def html_to_text(raw):
    # RIS-Bundesrecht: neben dem sichtbaren Text steht eine ausgeschriebene
    # Vorlese-Fassung (span.sr-only, "Paragraph 41, Absatz eins"): entfernen
    raw = re.sub(r"(?is)<span[^>]*class=[\"'][^\"']*\bsr-only\b[^\"']*[\"'][^>]*>.*?</span>",
                 " ", raw)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = htmllib.unescape(raw)
    raw = re.sub(r"[ \t\xa0]+", " ", raw)
    raw = re.sub(r"\s*\n\s*", "\n", raw)
    return raw.strip()


def fetch_text(url):
    """Voller Text, kein Laengenlimit."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return html_to_text(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = e
        time.sleep(5 * (attempt + 1))
    raise last


def add_texts(docs, errors, today):
    folder = os.path.join(DATA, "voll", today)
    os.makedirs(folder, exist_ok=True)
    for r in docs:
        url = r.get("inhalt_url")
        if not url:
            continue
        try:
            text = fetch_text(url)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(r["id"]))
            rel = f"voll/{today}/{safe}.txt"
            with open(os.path.join(DATA, rel), "w", encoding="utf-8") as f:
                f.write(text)
            r["text_datei"] = rel
            r["zeichen"] = len(text)
            r["text"] = text[:EXCERPT] + (" [...]" if len(text) > EXCERPT else "")
        except Exception as e:
            errors.append(f"Text {r.get('id')}: {e!r}")
        time.sleep(PAUSE_S)


# ---------------------------------------------------------------------------
# Normtexte: geltende (und bei Aenderungsgesetzen alte) Fassung zitierter
# Paragraphen aus dem konsolidierten Bundesrecht (BrKons). Alles hier ist
# Zusatz: Fehler blockieren den Feed nie, sie landen nur in "normtext_hinweise".
# ---------------------------------------------------------------------------
LAWS = {  # Kuerzel: (Titel fuer die Suche, Erkennungsstamm im Kurztitel)
    "WaffG": ("Waffengesetz 1996", "waffengesetz"),
    "SMG": ("Suchtmittelgesetz", "suchtmittelgesetz"),
    "StGB": ("Strafgesetzbuch", "strafgesetzbuch"),
    "StPO": ("Strafprozeßordnung 1975", "strafproze"),
    "SPG": ("Sicherheitspolizeigesetz", "sicherheitspolizeigesetz"),
    "StVO": ("Straßenverkehrsordnung 1960", "verkehrsordnung"),
    "KFG": ("Kraftfahrgesetz 1967", "kraftfahrgesetz"),
    "FSG": ("Führerscheingesetz", "hrerscheingesetz"),
    "VStG": ("Verwaltungsstrafgesetz 1991", "verwaltungsstrafgesetz"),
    "AVG": ("Allgemeines Verwaltungsverfahrensgesetz 1991", "verwaltungsverfahrensgesetz"),
    "FPG": ("Fremdenpolizeigesetz 2005", "fremdenpolizeigesetz"),
    "MeldeG": ("Meldegesetz 1991", "meldegesetz"),
    "VersG": ("Versammlungsgesetz 1953", "versammlungsgesetz"),
    "PolKG": ("Polizeikooperationsgesetz", "polizeikooperationsgesetz"),
}
LAW_NAMES = {  # ausgeschriebene Namen (Stamm, kleingeschrieben) -> Kuerzel
    "waffengesetz": "WaffG", "suchtmittelgesetz": "SMG", "strafgesetzbuch": "StGB",
    "strafprozess": "StPO", "strafprozeß": "StPO", "sicherheitspolizeigesetz": "SPG",
    "straßenverkehrsordnung": "StVO", "kraftfahrgesetz": "KFG",
    "führerscheingesetz": "FSG", "verwaltungsstrafgesetz": "VStG",
    "fremdenpolizeigesetz": "FPG", "meldegesetz": "MeldeG",
    "versammlungsgesetz": "VersG", "polizeikooperationsgesetz": "PolKG",
}
MAX_NORMS_DOC = 6
MAX_FETCH_RUN = 60  # neue Abrufe pro Lauf (vorhandene kosten nichts)
_ABK = "|".join(sorted(LAWS, key=len, reverse=True))
_NAMEN = "|".join(sorted((re.escape(n) for n in LAW_NAMES), key=len, reverse=True))
NORM_RE = re.compile(
    r"§§?\s*(\d{1,3}[a-z]?)(?:[^§\n]{0,40}?)\b(" + _ABK + r"|(?:" + _NAMEN + r")\w*)", re.I)
PAR_RE = re.compile(  # Novellenstil: "§ 41 Abs. 1 lautet:" / "§ 5 entfaellt"
    r"^\s*(?:\d+[a-z]?\.\s*)?§\s*(\d{1,3}[a-z]?)\b[^\n]{0,80}?"
    r"(?:lautet|entfällt|entfaellt|eingefügt|ersetzt)", re.M | re.I)


def _law_key(token):
    for k in LAWS:
        if k.lower() == token.lower():
            return k
    low = token.lower()
    for n, k in LAW_NAMES.items():
        if low.startswith(n):
            return k
    return None


NORM_REV_RE = re.compile(  # RIS-Metadaten: "WaffG 1996 §41 Abs1"
    r"\b(" + _ABK + r")\b(?:\s*\d{4})?\s*§§?\s*(\d{1,3}[a-z]?)", re.I)


def extract_norms(rec, text):
    """Liste von (Kuerzel, Paragraph). Nur Metadaten (Normen, Titel) und bei
    Aenderungsgesetzen/Entwuerfen die ausdruecklich neu gefassten Paragraphen;
    beilaeufige Erwaehnungen im Volltext werden bewusst ignoriert."""
    found = []

    def add(k, par):
        if k and (k, par.lower()) not in found:
            found.append((k, par.lower()))

    blob = " ".join(str(rec.get(k, "")) for k in ("Normen", "Titel", "Kurztitel", "Langtitel"))
    for m in NORM_RE.finditer(blob):
        add(_law_key(m.group(2)), m.group(1))
    for m in NORM_REV_RE.finditer(blob):
        add(_law_key(m.group(1)), m.group(2))
    if rec.get("app") in ("BgblAuth", "Begut", "RegV"):
        head = " ".join(str(rec.get(k, "")) for k in ("Titel", "Kurztitel", "Langtitel")).lower()
        laws = {k for n, k in LAW_NAMES.items() if n in head}
        if len(laws) == 1:
            k = laws.pop()
            for m in PAR_RE.finditer(text or ""):
                add(k, m.group(1))
    return found[:MAX_NORMS_DOC]


def fetch_norm(law, par, stichtag=None):
    """Text des Paragraphen (geltend bzw. zum Stichtag) oder None."""
    titel, stamm = LAWS[law]
    params = {"Applikation": "BrKons", "Titel": titel, "Abschnitt.Typ": "Paragraph",
              "Abschnitt.Von": par, "Abschnitt.Bis": par,
              "DokumenteProSeite": "Ten", "Seitennummer": 1}
    if stichtag:
        params["Fassung.FassungVom"] = stichtag
    js, _ = http_get("Bundesrecht", params)
    _, _, refs = parse_page(js)
    for ref in refs:
        rec = to_record("BrKons", ref)
        head = " ".join(str(rec.get(k, "")) for k in ("Kurztitel", "Titel", "Langtitel")).lower()
        if stamm not in head or not rec.get("inhalt_url"):
            continue
        time.sleep(PAUSE_S)
        text = fetch_text(rec["inhalt_url"])
        if re.search(r"§\s*" + re.escape(par) + r"\b", text):
            return text
    return None


REFRESH_DAYS = 30  # geltende Fassung nach so vielen Tagen neu holen (Gesetze aendern sich)


def add_normtexte(docs, hints, today):
    """Gesetzestexte liegen dauerhaft unter gesetze/<Kuerzel>/ (keine 7-Tage-
    Loeschung). Vorhandene Paragraphen werden nicht erneut abgerufen; die
    geltende Fassung wird nach REFRESH_DAYS aufgefrischt, alte Fassungen
    (Stichtag) sind endgueltig."""
    base = os.path.join(DATA, "gesetze")
    idx_path = os.path.join(base, "index.json")
    os.makedirs(base, exist_ok=True)
    try:
        index = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        index = {}
    limit = (datetime.now(TZ) - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d")

    def have(rel):
        return os.path.exists(os.path.join(DATA, rel))

    def save(rel, text):
        os.makedirs(os.path.dirname(os.path.join(DATA, rel)), exist_ok=True)
        open(os.path.join(DATA, rel), "w", encoding="utf-8").write(text)

    done, n_fetched = {}, 0
    for r in docs:
        text = ""
        if r.get("text_datei"):
            try:
                text = open(os.path.join(DATA, r["text_datei"]), encoding="utf-8").read()
            except OSError:
                pass
        alt_tag = None
        if r.get("app") == "BgblAuth":
            d = str(r.get("Inkrafttretensdatum", ""))[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                alt_tag = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        out = []
        for law, par in extract_norms(r, text):
            key = (law, par, alt_tag)
            if key not in done:
                entry = {"norm": f"§ {par} {law}"}
                rel_neu = f"gesetze/{law}/{par}.txt"
                try:
                    stale = index.get(f"{law}/{par}", "") < limit
                    if not have(rel_neu) or stale:
                        if n_fetched >= MAX_FETCH_RUN:
                            raise RuntimeError("Abruflimit pro Lauf erreicht")
                        neu = fetch_norm(law, par)
                        n_fetched += 1
                        time.sleep(PAUSE_S)
                        if neu:
                            save(rel_neu, neu)
                            index[f"{law}/{par}"] = today
                    if have(rel_neu):
                        entry["datei_neu"] = rel_neu
                    else:
                        hints.append(f"{entry['norm']}: nicht im konsolidierten Bundesrecht gefunden")
                    if alt_tag and have(rel_neu):
                        rel_alt = f"gesetze/{law}/{par}_alt_{alt_tag}.txt"
                        if not have(rel_alt) and n_fetched < MAX_FETCH_RUN:
                            alt = fetch_norm(law, par, alt_tag)
                            n_fetched += 1
                            time.sleep(PAUSE_S)
                            if alt:
                                save(rel_alt, alt)
                        if have(rel_alt):
                            entry["datei_alt"] = rel_alt
                            entry["alt_stand"] = alt_tag
                        else:
                            hints.append(f"{entry['norm']}: keine Fassung vom {alt_tag}")
                except Exception as e:
                    hints.append(f"{entry['norm']}: {e!r}")
                done[key] = entry
            out.append(done[key])
        if out:
            r["normtexte"] = out
    json.dump(index, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)


def probe():
    for endpoint, app, _ in SOURCES:
        try:
            js, url = http_get(endpoint, {
                "Applikation": app, "ImRisSeit": RIS_SINCE,
                "DokumenteProSeite": "Ten", "Seitennummer": 1})
            total, size, refs = parse_page(js)
            sample = to_record(app, refs[0]) if refs else {}
            sample.pop("_text", None)
            print(f"OK   {endpoint}/{app}: {total} Treffer im Fenster; Beispiel:")
            print("     " + json.dumps(sample, ensure_ascii=False)[:400])
        except Exception as e:
            print(f"FEHLER {endpoint}/{app}: {e}")
        time.sleep(PAUSE_S)


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], check=True,
                          capture_output=True, text=True).stdout


def gitd(*args, check=True):
    return subprocess.run(["git", "-C", DATA, *args], check=check,
                          capture_output=True, text=True).stdout


def ensure_data_repo(offline):
    fresh = not os.path.isdir(os.path.join(DATA, ".git"))
    if fresh:
        os.makedirs(DATA, exist_ok=True)
        url = git("remote", "get-url", "origin").strip()
        gitd("init", "-q", "-b", "data")
        gitd("remote", "add", "origin", url)
        gitd("config", "user.name", "ris-feed-vps")
        gitd("config", "user.email", "ris-feed@localhost")
    if not offline and not os.path.exists(os.path.join(DATA, "state", "seen.json")):
        # Datenordner neu oder leer: vorhandenen Stand vom Server holen
        if subprocess.run(["git", "-C", DATA, "fetch", "-q", "origin", "data"],
                          capture_output=True).returncode == 0:
            gitd("checkout", "-q", "-f", "-B", "data", "FETCH_HEAD", check=False)


def cleanup(now):
    limit = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    feed = os.path.join(DATA, "feed")
    for fn in os.listdir(feed):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", fn) and fn[:10] < limit:
            os.remove(os.path.join(feed, fn))
    import shutil
    for sub in ("voll", "normen"):
        base = os.path.join(DATA, sub)
        if os.path.isdir(base):
            for d in os.listdir(base):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < limit:
                    shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def publish(today):
    """Daten als einzelnen neuen Commit hochladen: aelterer Verlauf verschwindet."""
    gitd("branch", "-D", "tmp", check=False)
    gitd("checkout", "-q", "--orphan", "tmp")
    gitd("add", "-A")
    gitd("commit", "-q", "-m", f"RIS-Feed {today}")
    gitd("branch", "-D", "data", check=False)
    gitd("branch", "-m", "data")
    gitd("push", "-q", "-f", "origin", "data")
    gitd("reflog", "expire", "--expire=now", "--all", check=False)
    gitd("gc", "-q", "--prune=now", check=False)


ALL = "--all" in sys.argv  # alles im Fenster neu liefern, Merkzettel ignorieren


def normen_only():
    import shutil
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    ensure_data_repo("--no-push" in sys.argv)
    path = os.path.join(DATA, "feed", "latest.json")
    result = json.load(open(path, encoding="utf-8"))
    for r in result["dokumente"]:
        r.pop("normtexte", None)
    shutil.rmtree(os.path.join(DATA, "normen"), ignore_errors=True)  # alte Ablage
    result["normtext_hinweise"] = []
    add_normtexte(result["dokumente"], result["normtext_hinweise"], today)
    day = str(result.get("erzeugt", today))[:10]
    for name in (f"feed/{day}.json", "feed/latest.json"):
        with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    n = sum(len(r.get("normtexte", [])) for r in result["dokumente"])
    print(f"{n} Normen, {len(result['normtext_hinweise'])} Hinweise")
    for h in result["normtext_hinweise"]:
        print("  HINWEIS", h)
    if "--no-push" not in sys.argv:
        publish(today)


def main():
    if "--probe" in sys.argv:
        return probe()
    if "--normen-only" in sys.argv:  # nur Normtexte neu, vorhandenen Feed behalten
        return normen_only()
    if "--norm" in sys.argv:  # Test: python3 ris_fetch.py --norm WaffG 41 [YYYY-MM-DD]
        a = sys.argv[sys.argv.index("--norm") + 1:]
        t = fetch_norm(a[0], a[1].lower(), a[2] if len(a) > 2 else None)
        print(t if t else "NICHT GEFUNDEN")
        return
    offline = "--no-push" in sys.argv
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    if not offline:  # Skript-Update holen und bei Aenderung neu starten
        me = os.path.abspath(__file__)
        before = hashlib.sha1(open(me, "rb").read()).hexdigest()
        git("pull", "--rebase", "--autostash", "origin", "main")
        after = hashlib.sha1(open(me, "rb").read()).hexdigest()
        if before != after and not os.environ.get("RIS_FEED_RESTARTED"):
            os.environ["RIS_FEED_RESTARTED"] = "1"
            os.execv(sys.executable, [sys.executable, me] + sys.argv[1:])

    ensure_data_repo(offline)
    os.makedirs(os.path.join(DATA, "feed"), exist_ok=True)
    os.makedirs(os.path.join(DATA, "state"), exist_ok=True)
    seen_path = os.path.join(DATA, "state", "seen.json")
    seen = set(json.load(open(seen_path))) if os.path.exists(seen_path) else set()

    result = {"erzeugt": now.isoformat(timespec="seconds"),
              "fenster": RIS_SINCE, "quellen": {}, "fehler": [], "dokumente": []}
    for endpoint, app, prefilter in SOURCES:
        try:
            recs, truncated = fetch_source(endpoint, app, prefilter)
            new = [r for r in recs if r["id"] and (ALL or r["id"] not in seen)]
            for r in new:
                r.pop("_text", None)
                seen.add(r["id"])
            result["dokumente"] += new
            result["quellen"][f"{endpoint}/{app}"] = {
                "neu": len(new), "abgeschnitten": truncated}
        except Exception as e:
            result["fehler"].append(f"{endpoint}/{app}: {e}")
        time.sleep(PAUSE_S)

    add_texts(result["dokumente"], result["fehler"], today)
    result["normtext_hinweise"] = []
    try:
        add_normtexte(result["dokumente"], result["normtext_hinweise"], today)
    except Exception as e:  # Zusatzfunktion darf den Feed nie blockieren
        result["normtext_hinweise"].append(f"Normtexte insgesamt: {e!r}")

    json.dump(sorted(seen)[-8000:], open(seen_path, "w"))
    n_new = len(result["dokumente"])
    # Mehrere Laeufe am selben Tag: Dokumente des frueheren Laufs behalten
    today_path = os.path.join(DATA, "feed", f"{today}.json")
    if os.path.exists(today_path):
        try:
            prev = json.load(open(today_path, encoding="utf-8")).get("dokumente", [])
            have = {x["id"] for x in result["dokumente"]}
            result["dokumente"] += [x for x in prev if x.get("id") not in have]
        except Exception:
            pass
    for name in (f"feed/{today}.json", "feed/latest.json"):
        with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    cleanup(now)

    print(f"{today}: {n_new} neue Dokumente (Datei gesamt: {len(result['dokumente'])}), "
          f"{len(result['fehler'])} Fehler")
    for e in result["fehler"]:
        print("  FEHLER", e)

    if not offline:
        publish(today)


if __name__ == "__main__":
    main()
