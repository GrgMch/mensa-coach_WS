"""Mensa-Coach: holt den Wochenspeiseplan zweier Esslinger Mensen und lässt ihn von Groq bewerten."""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASIS = "https://sws2.maxmanager.xyz/"
AJAX = BASIS + "inc/ajax-php_konnektor.inc.php"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELLE = "https://api.groq.com/openai/v1/models"
MODELL = "openai/gpt-oss-120b"
KATEGORIEN = ["VEGANER RENNER", "HAUPTGERICHT", "MENSA SPECIAL", "ALL TIME CLASSICS"]
MENSEN = {"flandernstrasse": "FLANDERN", "stadtmitte": "STADTMITTE"}
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
HEADERS = {"User-Agent": "Mensa-Coach (GitHub Actions)"}


def mensa_nummern():
    """Sucht die Nummern der Mensen per Textsuche im rohen HTML der Auswahlliste."""
    html = requests.get(BASIS, params={"lang": "de"}, headers=HEADERS, timeout=30).text
    optionen = re.findall(r"<option value=['\"]?(\d+)['\"]?\s*/?>([^<]*)", html)
    nummern = {}
    for datei, suchwort in MENSEN.items():
        for nummer, name in optionen:
            if suchwort in name.upper():
                nummern[datei] = nummer
                print(f"Mensa {name.strip()}: Nummer {nummer}")
                break
        else:
            print(f"FEHLER: '{suchwort}' nicht in der Auswahlliste gefunden. Anfang der Seite:")
            print(" ".join(html[:1000].split()))
            sys.exit(1)
    return nummern


def zahl(text, muster):
    treffer = re.search(muster, text)
    return float(treffer.group(1).replace(",", ".")) if treffer else None


def tag_lesen(nummer, tag, montag):
    """Liest die Gerichte eines Tages aus der Hintergrundabfrage des Speiseplans."""
    daten = {"func": "make_spl", "locId": nummer, "date": tag.isoformat(), "lang": "de",
             "startThisWeek": montag.isoformat(),
             "startNextWeek": (montag + timedelta(days=7)).isoformat()}
    html = requests.post(AJAX, data=daten, headers=HEADERS, timeout=30).text
    gerichte, kategorie = [], ""
    for element in BeautifulSoup(html, "html.parser").select(".gruppenname, .splMeal"):
        if "gruppenname" in element.get("class", []):
            kategorie = element.get_text(strip=True).upper()
            continue
        if kategorie not in KATEGORIEN:
            continue
        name_tag = element.select_one(".col-md-5 span")
        preis_tag = element.select_one(".col-md-3 div")
        if not name_tag:
            continue
        symbole = {bild.get("title", "").strip().lower() for bild in element.select("img[title]")}
        text = element.get_text(" ")
        vegan = "vegan" in symbole
        gerichte.append({
            "kategorie": kategorie.title(),
            "name": " ".join(name_tag.get_text(" ").split()),
            "preis_studierende": zahl(preis_tag.get_text(), r"(\d+,\d+)") if preis_tag else None,
            "vegan": vegan,
            "vegetarisch": vegan or "vegetarisch" in symbole,
            "kcal_100g": zahl(text, r"([\d.]+)\s*kcal"),
            "eiweiss_100g": zahl(text, r"Eiwei\S*:\s*([\d.]+)"),
        })
    return gerichte, html


def bewerten(alle):
    """Ein einziger Groq-Aufruf für alle Gerichte der Woche."""
    for g in alle:
        g.update({"gesundheit": None, "kcal_portion": None, "begruendung": None})
    namen = []
    for g in alle:
        if g["name"] not in [n["name"] for n in namen]:
            namen.append({"name": g["name"], "kcal_100g": g["kcal_100g"], "eiweiss_100g": g["eiweiss_100g"]})
    if not namen:
        print("Keine Gerichte gefunden, daher kein Groq-Aufruf.")
        return True
    liste = {str(i + 1): n for i, n in enumerate(namen)}
    nummer_von = {n["name"]: str(i + 1) for i, n in enumerate(namen)}
    auftrag = ("Bewerte jedes Mensa-Gericht. Antworte mit einem JSON-Objekt, dessen Schlüssel die Nummern "
               "der Gerichte sind. Jeder Wert ist ein Objekt mit: gesundheit (ganze Zahl 1 bis 10, "
               "10 = sehr gesund), kcal_portion (geschätzte kcal einer üblichen Mensa-Portion, ganze Zahl), "
               "begruendung (Deutsch, höchstens 12 Wörter).\nGerichte:\n"
               + json.dumps(liste, ensure_ascii=False))
    body = {"model": MODELL,
            "messages": [{"role": "system", "content": "Du bist Ernährungsberater. Antworte nur mit JSON."},
                         {"role": "user", "content": auftrag}],
            "response_format": {"type": "json_object"},
            "reasoning_effort": "low",
            "temperature": 0.2}
    kopf = {"Authorization": "Bearer " + os.environ.get("GROQ_API_KEY", "")}
    antwort = None
    try:
        antwort = requests.post(GROQ_URL, json=body, headers=kopf, timeout=120)
        antwort.raise_for_status()
        daten = antwort.json()
        nutzung = daten.get("usage", {})
        print(f"Groq-Tokens: {nutzung.get('prompt_tokens')} Eingabe + "
              f"{nutzung.get('completion_tokens')} Ausgabe = {nutzung.get('total_tokens')} gesamt")
        werte = json.loads(daten["choices"][0]["message"]["content"])
        if "1" not in werte and len(werte) == 1 and isinstance(next(iter(werte.values())), dict):
            werte = next(iter(werte.values()))
    except Exception as fehler:
        print("FEHLER bei Groq:", fehler)
        if antwort is not None:
            print(antwort.text[:1500])
        try:
            modelle = requests.get(GROQ_MODELLE, headers=kopf, timeout=30).json()
            print("Verfügbare Modelle:", ", ".join(m["id"] for m in modelle.get("data", [])))
        except Exception as fehler2:
            print("Modellliste nicht abrufbar:", fehler2)
        return False
    bewertet = 0
    for g in alle:
        wert = werte.get(nummer_von[g["name"]])
        if not isinstance(wert, dict):
            continue
        try:
            g["gesundheit"] = max(1, min(10, int(wert.get("gesundheit"))))
            g["kcal_portion"] = int(wert.get("kcal_portion"))
        except (TypeError, ValueError):
            pass
        g["begruendung"] = str(wert.get("begruendung", ""))
        bewertet += 1
    print(f"Groq: {len(namen)} verschiedene Gerichte gesendet, {bewertet} Einträge bewertet")
    return True


def main():
    heute = date.today()
    montag = heute - timedelta(days=heute.weekday())
    nummern = mensa_nummern()
    plaene = {}
    for datei, nummer in nummern.items():
        tage = []
        for i in range(5):
            tag = montag + timedelta(days=i)
            gerichte, html = tag_lesen(nummer, tag, montag)
            print(f"{datei} {WOCHENTAGE[i]} {tag}: {len(gerichte)} Gerichte")
            if not gerichte:
                print("  Anfang der Antwort:", " ".join(html[:500].split()))
            tage.append({"datum": tag.isoformat(), "wochentag": WOCHENTAGE[i], "gerichte": gerichte})
        plaene[datei] = tage
    alle = [g for tage in plaene.values() for t in tage for g in t["gerichte"]]
    ok = bewerten(alle)
    stand = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d %H:%M")
    os.makedirs("data", exist_ok=True)
    for datei, tage in plaene.items():
        with open(f"data/{datei}.json", "w", encoding="utf-8") as f:
            json.dump({"stand": stand, "tage": tage}, f, ensure_ascii=False, indent=2)
        print(f"data/{datei}.json geschrieben")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
