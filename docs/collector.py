#!/usr/bin/env python3
"""Energiepreise Bayern: sammeln (Tankerkönig + Ladetarife), speichern (SQLite), auswerten (docs/data.json).

Aufruf:  python3 collector.py collect   # Preise abrufen, speichern, auswerten
         python3 collector.py export    # nur Kennzahlen neu berechnen
         python3 collector.py demo      # Testdaten (31 Tage) erzeugen, kein API-Schlüssel nötig
"""
import csv, json, os, random, sqlite3, statistics as st, sys, time
import urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "preise.db")
OUT = os.path.join(HERE, "docs", "data.json")
KEY = os.environ.get("TANKERKOENIG_KEY", "")
FUELS = ("diesel", "e5", "e10")
BEZIRKE = ("Oberbayern", "Niederbayern")
TAGE = 31
DEMO = False


def db():
    c = sqlite3.connect(DB)
    c.executescript(
        """CREATE TABLE IF NOT EXISTS preise(ts TEXT, station TEXT, anbieter TEXT, plz TEXT, bezirk TEXT, fuel TEXT, preis REAL);
        CREATE TABLE IF NOT EXISTS laden(ts TEXT, anbieter TEXT, leistung TEXT, tarif TEXT, ct_kwh REAL, stand TEXT, quelle TEXT);
        CREATE INDEX IF NOT EXISTS i_preise ON preise(ts);"""
    )
    return c


def plz_map():
    # Ordnet jede bayerische PLZ einem Regierungsbezirk zu. Stationen außerhalb der Liste werden verworfen.
    with open(os.path.join(HERE, "plz_bayern.csv"), encoding="utf-8") as f:
        return {r["plz"].zfill(5): r["regierungsbezirk"] for r in csv.DictReader(f)}


def grid():
    # Suchradius der API ist höchstens 25 km, daher ein Raster über die Bounding Box von Bayern.
    lat = 47.3
    while lat <= 50.6:
        lon = 8.9
        while lon <= 13.9:
            yield round(lat, 3), round(lon, 3)
            lon += 0.46
        lat += 0.30


def fetch_stations():
    seen, fehler = {}, 0
    for lat, lon in grid():
        q = urllib.parse.urlencode(dict(lat=lat, lng=lon, rad=25, sort="dist", type="all", apikey=KEY))
        try:
            with urllib.request.urlopen("https://creativecommons.tankerkoenig.de/json/list.php?" + q, timeout=30) as r:
                d = json.load(r)
            if not d.get("ok"):
                raise RuntimeError(d.get("message"))
            for s in d["stations"]:
                seen[s["id"]] = s
        except Exception as e:  # einzelner Rasterpunkt darf ausfallen
            fehler += 1
            print("Abruf fehlgeschlagen", lat, lon, e, file=sys.stderr)
        time.sleep(0.5)
    if not seen:
        raise SystemExit("Keine Stationsdaten erhalten (API-Schlüssel prüfen).")
    return seen.values()


def collect():
    if not KEY:
        raise SystemExit("Umgebungsvariable TANKERKOENIG_KEY fehlt.")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    plz, c, rows = plz_map(), db(), []
    for s in fetch_stations():
        p5 = str(s.get("postCode") or "").zfill(5)
        if p5 not in plz:
            continue
        marke = (s.get("brand") or "Freie Tankstelle").strip().title()
        for f in FUELS:
            p = s.get(f)
            ok = isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0
            rows.append((ts, s["id"], marke, p5, plz[p5], f, p if ok else None))
    c.executemany("INSERT INTO preise VALUES (?,?,?,?,?,?,?)", rows)
    with open(os.path.join(HERE, "tariffs.csv"), encoding="utf-8") as f:
        c.executemany(
            "INSERT INTO laden VALUES (?,?,?,?,?,?,?)",
            [(ts, r["anbieter"], r["leistung"], r["tarif"], float(r["ct_kwh"]), r["stand"], r["quelle"]) for r in csv.DictReader(f)],
        )
    grenze = (datetime.now(timezone.utc) - timedelta(days=TAGE)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c.execute("DELETE FROM preise WHERE ts < ?", (grenze,))
    c.execute("DELETE FROM laden WHERE ts < ?", (grenze,))
    c.commit()
    export()


def stats(v):
    v = sorted(v)
    q = st.quantiles(v, n=4, method="inclusive") if len(v) > 1 else [v[0]] * 3
    r = dict(min=v[0], q1=q[0], median=q[1], q3=q[2], max=v[-1], mean=st.fmean(v),
             sd=st.stdev(v) if len(v) > 1 else 0.0, iqr=q[2] - q[0], spanne=v[-1] - v[0])
    return {k: round(x, 4) for k, x in r.items()}


def export():
    c, kr = db(), []
    for ts, fuel in c.execute("SELECT DISTINCT ts, fuel FROM preise ORDER BY ts").fetchall():
        gr = {}
        for bz, an, p in c.execute("SELECT bezirk, anbieter, preis FROM preise WHERE ts=? AND fuel=?", (ts, fuel)):
            gr.setdefault(("Bayern", "Gebiet"), []).append(p)
            if bz in BEZIRKE:
                gr.setdefault((bz, "Gebiet"), []).append(p)
            gr.setdefault((an, "Anbieter"), []).append(p)
        for (name, typ), alle in gr.items():
            gut = [p for p in alle if p is not None]
            if not gut or (typ == "Anbieter" and len(alle) < 5):
                continue
            kr.append(dict(t=ts, fuel=fuel, gruppe=name, typ=typ, n=len(gut), gesamt=len(alle),
                           abdeckung=round(len(gut) / len(alle), 3), **stats(gut)))
    la = []
    for ts, an, le, ta, ct, stand, q in c.execute(
        "SELECT ts, anbieter, leistung, tarif, ct_kwh, stand, quelle FROM laden ORDER BY ts"
    ):
        alter = (date.fromisoformat(ts[:10]) - date.fromisoformat(stand)).days
        la.append(dict(t=ts, anbieter=an, leistung=le, tarif=ta, ct=ct, alter=alter, quelle=q))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(dict(erzeugt=datetime.now(timezone.utc).isoformat(timespec="seconds"), demo=DEMO,
                       kraftstoff=kr, laden=la), f, ensure_ascii=False, separators=(",", ":"))
    print("Geschrieben:", OUT, len(kr), "Kraftstoff-Zeilen,", len(la), "Lade-Zeilen")


def demo():
    global DB, DEMO
    DB, DEMO = os.path.join(HERE, "demo.db"), True
    if os.path.exists(DB):
        os.remove(DB)
    random.seed(7)
    c = db()
    jetzt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    marken = {"Marke A": .04, "Marke B": .04, "Marke C": .02, "Marke D": -.02, "Marke E": .01, "Freie Tankstelle": -.04}
    bez = {"Oberbayern": .03, "Niederbayern": -.02, "Oberpfalz": 0.0, "Schwaben": -.01}
    stationen = [(i, random.choice(list(marken)), random.choice(list(bez)), random.gauss(0, .02)) for i in range(400)]
    basis = {"diesel": 1.66, "e5": 1.79, "e10": 1.73}
    lade = [("Demo-Anbieter %d" % i, le, ta) for i in range(1, 7) for le in ("AC", "DC", "HPC") for ta in ("Ad-hoc", "Vertrag")]
    lbasis = {"AC": 49, "DC": 62, "HPC": 69}
    for k in range(62, -1, -1):
        t = jetzt - timedelta(hours=12 * k)
        ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        markt = .15 * __import__("math").sin(k / 9)
        for i, m, b, off in stationen:
            for f in FUELS:
                p = None if random.random() < .03 else round(basis[f] + marken[m] + bez[b] + off + markt + random.gauss(0, .03), 3)
                c.execute("INSERT INTO preise VALUES (?,?,?,?,?,?,?)", (ts, str(i), m, "00000", b, f, p))
        for an, le, ta in lade:
            ct = round(lbasis[le] + (8 if ta == "Ad-hoc" else 0) + random.gauss(0, 4), 1)
            stand = (t.date() - timedelta(days=random.randint(0, 60))).isoformat()
            c.execute("INSERT INTO laden VALUES (?,?,?,?,?,?,?)", (ts, an, le, ta, ct, stand, "Demo"))
    c.commit()
    export()


if __name__ == "__main__":
    {"collect": collect, "export": export, "demo": demo}.get(sys.argv[1] if len(sys.argv) > 1 else "collect", collect)()
