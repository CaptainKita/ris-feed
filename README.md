# ris-feed

Skript `ris_fetch.py` (Zweig `main`): holt neue Dokumente aus der OGD-API des Rechtsinformationssystems des Bundes (data.bka.gv.at) und stellt sie samt Volltext auf dem Zweig `data` bereit. Die Daten werden nach 7 Tagen gelöscht; der Zweig `data` besteht bei jedem Lauf aus nur einem Commit.

Aufbau des Zweigs `data`:
- `feed/latest.json` – neueste Lieferung (Metadaten, Textanfang, Pfad zur Volltextdatei)
- `feed/YYYY-MM-DD.json` – Tagesdateien der letzten 7 Tage
- `voll/YYYY-MM-DD/<id>.txt` – voller Text je Dokument
- `state/seen.json` – bereits gelieferte IDs

Quelle: RIS – Rechtsinformationssystem des Bundes (data.bka.gv.at), CC BY 4.0.

## Einrichtung auf dem VPS

```bash
git clone git@github-risfeed:CaptainKita/ris-feed.git ~/ris-feed        # Code
python3 ~/ris-feed/ris_fetch.py --probe                                  # Test
python3 ~/ris-feed/ris_fetch.py                                          # echter Lauf
# Cron (werktags 5:30):
# 30 5 * * 1-5 /usr/bin/python3 /root/ris-feed/ris_fetch.py >> /root/ris-feed.log 2>&1
```
Die Daten liegen lokal in `~/ris-feed-data` (anderer Ordner, eigener Git-Zweig `data`).
