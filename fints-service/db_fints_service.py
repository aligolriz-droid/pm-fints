#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 db_fints_service.py  –  FinTS-Depotabruf als HTTP-Service (FastAPI)
============================================================================
 Stellt den Deutsche-Bank-FinTS-Abruf als Web-Endpoint bereit, damit die
 (z. B. auf Netlify gehostete) Portfolio-Manager-App ihn aufrufen kann.

   POST /holdings   Body: {blz,user,pin,product_id,url,days}
                    -> Depot+Umsätze als db_export-JSON
                    -> ODER {status:"tan_required", session, challenge,
                             challenge_image_b64, decoupled}
   POST /tan        Body: {session, tan}
                    -> db_export-JSON (nach TAN-Eingabe)
                    -> ODER erneut {status:"tan_required", ..., decoupled:true}
                       solange eine App-Freigabe (z.B. DKB SecureGo plus) noch
                       nicht bestätigt wurde – der Client muss dann mit LEEREM
                       tan-Wert erneut pollen, bis die Freigabe erfolgt ist.
   GET  /health     -> {"ok":true}

 ┌──────────────────────────────────────────────────────────────────────┐
 │  ⚠️  SICHERHEITSHINWEIS – BITTE LESEN                                  │
 │  Dieser Service verarbeitet deine BANK-PIN und TAN. Wenn du ihn in    │
 │  der Cloud betreibst, verlassen deine Zugangsdaten dein Gerät.        │
 │  Empfehlung: NUR über HTTPS betreiben, Zugriff per API-Key/Basic-Auth │
 │  einschränken, NICHT öffentlich erreichbar lassen, PIN NIE loggen.    │
 │  Für maximale Sicherheit besser den lokalen db_fints_connector.py     │
 │  nutzen (nichts verlässt den Rechner).                                │
 └──────────────────────────────────────────────────────────────────────┘

 SETUP (lokal):
   pip install fastapi uvicorn fints
   uvicorn db_fints_service:app --host 0.0.0.0 --port 8080

 DEPLOY (Container, z. B. Google Cloud Run / Fly.io / Render):
   siehe beiliegende Dockerfile + requirements.txt + DEPLOY_README.md
============================================================================
"""
import os
import re
import time
import base64
import datetime
import traceback
from decimal import Decimal
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fints.client import FinTS3PinTanClient, NeedTANResponse, NeedRetryResponse

# robust_mode bewusst NICHT deaktiviert: manche Banken (u.a. DKB) senden im
# HNVSK-Sicherheitsheader kleinere Abweichungen (z.B. fehlendes "cid"-Feld),
# die die Bibliothek im robusten Modus als generisches Objekt behandelt und
# ignoriert. Ohne robust_mode wird daraus ein harter FinTSParserError, der den
# gesamten Verbindungsaufbau abbrechen laesst. Frueher stand hier eine
# Deaktivierung zu Diagnosezwecken - die wurde entfernt, da sie DKB-
# Verbindungen zuverlaessig zum Absturz brachte.

app = FastAPI(title="DB FinTS Service")

# CORS: Für Tests offen; in Produktion auf deine Netlify-Domain einschränken!
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOW_ORIGIN] if ALLOW_ORIGIN != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optionaler API-Key-Schutz (empfohlen in Produktion): Header X-API-Key
API_KEY = os.environ.get("API_KEY", "")

# In-Memory-Sessions für den TAN-Zwischenschritt (nur kurzlebig!)
SESSIONS: Dict[str, Dict[str, Any]] = {}
SESSION_TTL = 300  # Sekunden


# --------------------------- Hilfen ---------------------------------------
def _check_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")


def _num(x):
    if x is None:
        return None
    try:
        if hasattr(x, "amount"):
            x = x.amount
        return float(Decimal(str(x)))
    except Exception:
        try:
            return float(x)
        except Exception:
            return None


def _holdings_rows(holdings):
    rows = []
    for h in holdings or []:
        isin = getattr(h, "isin", None) or ""
        pieces = _num(getattr(h, "pieces", None))
        price = _num(getattr(h, "market_value", None))
        total = _num(getattr(h, "total_value", None))
        if total is None and pieces is not None and price is not None:
            total = round(pieces * price, 2)
        vdate = getattr(h, "valuation_date", None)
        rows.append({
            "isin": isin,
            "name": str(getattr(h, "name", None) or isin),
            "units": pieces, "price": price, "value": total,
            "currency": getattr(h, "currency", None) or "EUR",
            "valuation_date": vdate.isoformat() if hasattr(vdate, "isoformat") else (str(vdate) if vdate else None),
        })
    return rows


# IBAN-Gesamtlaenge je Laendercode (ISO 13616). Wird genutzt, um eine ohne
# Trennzeichen an den Namen angehaengte IBAN exakt abzuschneiden - das passiert
# bei manchen FinTS/SEPA-Antworten der Deutschen Bank, weil "applicant_name"
# und die IBAN des Zahlungspartners in einem Feld zusammenlanden.
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22,
    "BH": 22, "BR": 29, "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24,
    "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29, "ES": 24, "FI": 18,
    "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27,
    "GT": 28, "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26,
    "IT": 27, "JO": 30, "KW": 30, "KZ": 20, "LB": 28, "LC": 32, "LI": 21,
    "LT": 20, "LU": 20, "LV": 21, "LY": 25, "MC": 27, "MD": 24, "ME": 22,
    "MK": 19, "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24,
    "PL": 28, "PS": 29, "PT": 25, "QA": 29, "RO": 24, "RS": 22, "SA": 24,
    "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}
_IBAN_PREFIX_RE = re.compile(r'^([A-Z]{2})(\d{2})[A-Z0-9]')


def _split_glued_iban(text: str):
    """Trennt eine am Anfang direkt (ohne Leer-/Trennzeichen) angehaengte
    IBAN vom Rest des Textes ab, z.B.
      'DE93300308800013441006AMAZON EU S.A R.L' ->
      ('DE93300308800013441006', 'AMAZON EU S.A R.L')
    Liefert ('', text) unveraendert zurueck, wenn kein IBAN-Praefix erkannt
    wird oder danach kein Rest-Text (Name) mehr uebrig bleibt."""
    if not text:
        return "", text
    m = _IBAN_PREFIX_RE.match(text)
    if not m:
        return "", text
    length = IBAN_LENGTHS.get(m.group(1))
    if not length or len(text) <= length:
        return "", text
    candidate_iban = text[:length]
    rest = text[length:].strip(" ,.-/")
    if not rest:
        return "", text
    return candidate_iban, rest


# Kartenzahlungen ("ABRECHNUNG KARTE") liefert die Deutsche Bank im
# Verwendungszweck als einen einzigen Blob:
#   "<Haendler>//<Ort>/<Land> TT-MM-JJJJTHH:MM:SS Kartennr. <volle Kartennummer>"
# z.B. "SumUp .Duketable GmbH//Schwalbach/DE 10-07-2026T13:00:01 Kartennr. 5354999999999236"
# Diese Regex zerlegt das in seine Einzelteile.
_CARD_PURPOSE_RE = re.compile(
    r'^(?P<merchant>.+?)//(?P<location>.+)/(?P<country>[A-Z]{2})\s?'
    r'(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})T'
    r'(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+'
    r'Kartennr\.\s*(?P<card>\d+)\s*$'
)
_CARD_LABELS = {"abrechnung karte", "kartenzahlung"}


def _parse_card_purpose(payee: str, purpose: str):
    """Erkennt eine Kartenzahlung und zerlegt sie in Haendler/Ort/Land/
    Zeitstempel/Karten-Endziffern. Gibt None zurueck, wenn payee nicht wie
    ein Kartenzahlungs-Buchungstext aussieht oder purpose nicht passt -
    dann bleibt alles unveraendert (sicherer Fallback)."""
    if not payee or payee.strip().lower() not in _CARD_LABELS:
        return None
    m = _CARD_PURPOSE_RE.match(purpose or "")
    if not m:
        return None
    d = m.groupdict()
    card = d["card"]
    last4 = card[-4:] if len(card) >= 4 else card
    return {
        "merchant": d["merchant"].strip(),
        "location": d["location"].strip(),
        "country": d["country"],
        # Datum/Uhrzeit des eigentlichen Einkaufs (kann vom Buchungstag abweichen)
        "purchased_at": "%s-%s-%sT%s:%s:%s" % (d["year"], d["month"], d["day"], d["hour"], d["minute"], d["second"]),
        "card_last4": last4,
    }


# Verwendungszweck bei SEPA-Dauerauftraegen/-Lastschriften der Deutschen Bank
# enthaelt oft technischen Vorspann ("RINP SEPA-Dauerauftrag an Ihre
# Referenz: NOTPROVIDED ...") sowie eine am Ende angehaengte IBAN/BIC des
# Zahlungspartners - beides ist fuer die Anzeige in der App nicht relevant
# und wird deshalb aus dem sichtbaren Kurztext entfernt. Der komplette
# Originaltext bleibt zusaetzlich als "purpose_full" erhalten (z.B. fuer
# einen Hover-Tooltip in der App).
_SEPA_REF_PREFIX_RE = re.compile(
    r'^RINP\s+SEPA-Dauerauftrag an Ihre Referenz:\s*NOTPROVIDED\s*',
    re.IGNORECASE,
)
_IBAN_BIC_SUFFIX_RE = re.compile(
    r'\s*IBAN\s*:\s*[A-Z]{2}\d{2}[A-Z0-9]{10,30}(?:\s*BIC\s*:\s*[A-Z0-9]{8,11})?\s*$',
    re.IGNORECASE,
)


def _clean_generic_purpose(purpose: str) -> str:
    """Entfernt technischen SEPA-Vorspann und eine angehaengte IBAN/BIC aus
    dem Verwendungszweck. Liefert den unveraenderten Text zurueck, falls
    nach dem Bereinigen nichts mehr uebrig bliebe (sicherer Fallback)."""
    if not purpose:
        return purpose
    cleaned = _SEPA_REF_PREFIX_RE.sub('', purpose)
    cleaned = _IBAN_BIC_SUFFIX_RE.sub('', cleaned)
    cleaned = cleaned.strip()
    return cleaned or purpose


def _tx_rows(transactions, iban):
    rows = []
    for t in transactions or []:
        d = getattr(t, "data", {}) or {}
        date = d.get("date") or d.get("entry_date")
        amt = _num(d.get("amount"))
        cur = "EUR"
        try:
            if d.get("amount") is not None and hasattr(d["amount"], "currency"):
                cur = d["amount"].currency
        except Exception:
            pass
        raw_payee = str(d.get("applicant_name") or "")
        counter_iban = str(d.get("applicant_iban") or "")
        clean_payee = raw_payee
        if not counter_iban:
            # applicant_iban war leer -> pruefen, ob die IBAN stattdessen
            # ungetrennt vorne an applicant_name klebt, und abtrennen.
            split_iban, split_name = _split_glued_iban(raw_payee)
            if split_iban:
                counter_iban = split_iban
                clean_payee = split_name
        raw_purpose = str(d.get("purpose") or d.get("posting_text") or "")
        clean_purpose = raw_purpose
        purpose_full = raw_purpose
        card_info = _parse_card_purpose(raw_payee, raw_purpose)
        if card_info:
            # Haendlername wandert ins Namensfeld statt der generischen
            # Bezeichnung "ABRECHNUNG KARTE"; Verwendungszweck wird auf
            # Ort/Land + maskierte Kartennummer reduziert. Die volle
            # Kartennummer wird NIRGENDS weitergereicht - auch nicht im
            # ausfuehrlicheren purpose_full fuer den Hover-Tooltip.
            clean_payee = card_info["merchant"]
            clean_purpose = "%s, %s \u00b7 Karte \u2022\u2022\u2022\u2022 %s" % (
                card_info["location"], card_info["country"], card_info["card_last4"])
            purpose_full = "%s \u00b7 %s, %s \u00b7 %s \u00b7 Karte \u2022\u2022\u2022\u2022 %s" % (
                card_info["merchant"], card_info["location"], card_info["country"],
                card_info["purchased_at"].replace("T", " "), card_info["card_last4"])
        else:
            clean_purpose = _clean_generic_purpose(raw_purpose)
        rows.append({
            "date": date.isoformat() if hasattr(date, "isoformat") else (str(date) if date else None),
            "amount": amt, "currency": cur,
            "payee": clean_payee,
            "purpose": clean_purpose,
            "purpose_full": purpose_full,
            "account": iban,
            "counter_iban": counter_iban,
            "card_last4": card_info["card_last4"] if card_info else None,
            "purchased_at": card_info["purchased_at"] if card_info else None,
        })
    return rows


def _gc_sessions():
    now = time.time()
    for sid in list(SESSIONS.keys()):
        if now - SESSIONS[sid]["ts"] > SESSION_TTL:
            SESSIONS.pop(sid, None)


def _collect(client, days: int) -> Dict[str, Any]:
    """Depot + Umsätze einsammeln. Wirft NeedTANResponse nach oben durch."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    export = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "deutsche-bank-fints",
        "holdings": [], "cash_transactions": [], "balances": [], "warnings": [],
    }
    try:
        accounts = client.get_sepa_accounts()
    except Exception as e:
        err_msg = str(e)
        if "9210" in err_msg or "9800" in err_msg or "9050" in err_msg:
            raise HTTPException(status_code=400, detail=f"Bank lehnte Anfrage ab (Code 9210/9800). Bitte TAN-Prozess erneut starten. Details: {err_msg}")
        if isinstance(e, NeedTANResponse):
            raise e # This is handled by the caller
        raise e

    if isinstance(accounts, NeedTANResponse):
        raise _TanNeeded(accounts)

    for acc in accounts:
        iban = getattr(acc, "iban", "") or getattr(acc, "accountnumber", "")
        try:
            h = client.get_holdings(acc)
            if isinstance(h, NeedTANResponse):
                raise _TanNeeded(h)
            export["holdings"].extend(_holdings_rows(h))
        except _TanNeeded:
            raise
        except Exception as _he:
            export["warnings"].append("Depot %s nicht abrufbar: %s" % (iban, _he))
        try:
            tx = client.get_transactions(acc, start, end)
            if isinstance(tx, NeedTANResponse):
                raise _TanNeeded(tx)
            export["cash_transactions"].extend(_tx_rows(tx, iban))
        except _TanNeeded:
            raise
        except Exception:
            pass
        try:
            bal = client.get_balance(acc)
            if isinstance(bal, NeedTANResponse):
                raise _TanNeeded(bal)
            _b = _num(getattr(bal, "amount", bal))
            if _b is not None:
                _bd = getattr(bal, "date", None)
                export["balances"].append({"account": iban, "balance": _b, "date": _bd.isoformat() if hasattr(_bd, "isoformat") else (str(_bd) if _bd else None)})
        except _TanNeeded:
            raise
        except Exception:
            pass
    return export


class _TanNeeded(Exception):
    def __init__(self, resp):
        self.resp = resp


def _set_browser_ua(client) -> None:
    """
    Manche Banken (nachweislich z.B. HVB, vermutlich auch andere) blockieren
    den Standard-User-Agent der 'requests'-Bibliothek per Firewall/WAF und
    antworten dann mit einem HTTP-Fehler (400/403), bevor die FinTS-Nachricht
    überhaupt ausgewertet wird. Ein Browser-artiger User-Agent behebt das in
    der Praxis oft, ohne das FinTS-Protokoll selbst zu beeinflussen.
    Rein defensiv (try/except), da der Zugriffspfad je nach fints-Version
    variieren kann und dies NIE den eigentlichen Abruf verhindern soll.
    """
    try:
        client.connection.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    except Exception:
        pass


def _select_tan_mechanism(client) -> None:
    """
    Waehlt EXPLIZIT ein TAN-Verfahren, bevor echte Kontodaten abgefragt
    werden. Ohne diesen Schritt bleibt der Client beim Default (meist
    Security Function '999' = "kein TAN-Verfahren"/einstufig), und Banken
    mit zwingender starker Kundenauthentifizierung (PSD2/SCA) lehnen dann
    JEDE Kontoabfrage sofort ab, statt eine TAN anzufordern - z.B. DKB mit
    "9210 Auftrag abgelehnt" + "9800 Dialog abgebrochen", ohne ueberhaupt
    eine HITAN-Challenge zu senden.

    WICHTIG: Muss VOR "with client:" aufgerufen werden, nicht danach!
    fetch_tan_mechanisms() fuehrt selbst einen anonymen Dialog mit der Bank,
    um die Bank-Parameterdaten (inkl. verfuegbarer TAN-Verfahren) zu holen -
    das entspricht genau dem, was die offizielle minimal_interactive_cli_bootstrap()
    macht (siehe python-fints docs/utils.py), und zwar VOR dem eigentlichen
    "with client:"-Dialogaufbau. get_tan_mechanisms() (ohne fetch_) liest
    dagegen nur bereits vorhandene Daten und liefert innerhalb eines frisch
    geoeffneten Dialogs oft noch nichts - genau das war der Fehler in einer
    frueheren Version dieser Funktion.

    Waehlt bevorzugt ein pushTAN/SecureGo/decoupled-Verfahren (passt zum
    bestehenden Decoupled-Polling), sonst das erste verfuegbare
    Nicht-999-Verfahren. Rein defensiv: wirft nie einen eigenen Fehler,
    damit Banken, die dies nicht brauchen (z.B. Deutsche Bank bisher),
    unveraendert weiterlaufen.
    """
    try:
        current = client.get_current_tan_mechanism()
        if current and str(current) != "999":
            return  # schon ein echtes Verfahren aktiv -> nichts tun
    except Exception:
        pass
    try:
        client.fetch_tan_mechanisms()
        mechs = client.get_tan_mechanisms()
    except Exception:
        return
    if not mechs:
        return
    chosen = None
    for sec_fn, mech in mechs.items():
        name = (getattr(mech, "name", "") or "").lower()
        if "push" in name or "securego" in name or "decoupled" in name:
            chosen = sec_fn
            break
    if chosen is None:
        for sec_fn in mechs:
            if str(sec_fn) != "999":
                chosen = sec_fn
                break
    if chosen is not None:
        try:
            client.set_tan_mechanism(chosen)
        except Exception:
            pass


def _tan_payload(sid: str, resp: NeedTANResponse) -> Dict[str, Any]:
    img = None
    try:
        if getattr(resp, "challenge_matrix", None):
            _mime, data = resp.challenge_matrix
            img = base64.b64encode(data).decode("ascii")
    except Exception:
        img = None
    # "decoupled" = App-Freigabe (z.B. DKB "SecureGo plus"/AppTAN): es gibt
    # KEINEN Code zum Eintippen. Stattdessen bestätigt der Nutzer in seiner
    # Banking-App, und der Client muss den Status per send_tan() mit LEEREM
    # tan-Wert wiederholt abfragen (Polling), bis die Bank die Freigabe meldet.
    decoupled = bool(getattr(resp, "decoupled", False))
    return {
        "status": "tan_required",
        "session": sid,
        "challenge": getattr(resp, "challenge", "") or "",
        "challenge_image_b64": img,
        "decoupled": decoupled,
    }



# --------------------------- Modelle --------------------------------------
class HoldReq(BaseModel):
    blz: str
    user: str
    pin: str
    product_id: Optional[str] = None
    url: str = "https://fints.deutsche-bank.de"
    days: int = 90
    x_api_key: Optional[str] = None


class TanReq(BaseModel):
    session: str
    tan: str
    x_api_key: Optional[str] = None


# --------------------------- Endpunkte ------------------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "db-fints", "time": datetime.datetime.now().isoformat(timespec="seconds")}


@app.post("/holdings")
def holdings(req: HoldReq):
    _check_key(req.x_api_key)
    _gc_sessions()
    client = FinTS3PinTanClient(req.blz, req.user, req.pin, req.url,
                               product_id=(req.product_id or "30"))
    _set_browser_ua(client)
    _select_tan_mechanism(client)
    try:
        with client:
            try:
                # PSD2/SCA: Manche Banken (u.a. DKB) verlangen bereits fuer den
                # Verbindungsaufbau selbst (Login) eine TAN-Bestaetigung, nicht
                # erst fuer die Kontoabfrage. Diese steckt in client.init_tan_response
                # und wurde bisher komplett ignoriert - dadurch lehnte DKB die
                # nachfolgende Kontoabfrage mit "9210 Auftrag abgelehnt" ab, weil
                # der Login-Schritt nie bestaetigt wurde. Wird wie jede andere
                # TAN-Anfrage ueber denselben _TanNeeded-Mechanismus behandelt.
                if client.init_tan_response:
                    raise _TanNeeded(client.init_tan_response)
                return _collect(client, req.days)
            except _TanNeeded as need:
                # Dialog + Client-Zustand für den TAN-Schritt persistieren
                sid = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii")
                client_data = client.deconstruct(including_private=True)
                # Dialog pausieren: gibt in dieser Bibliotheksversion die Bytes
                # direkt zurueck (kein Kontextmanager).
                dialog_data = client.pause_dialog()
                SESSIONS[sid] = {
                    "ts": time.time(),
                    "client_data": client_data,
                    "dialog_data": dialog_data,
                    "tan_blob": need.resp.get_data(),
                    "days": req.days,
                    "blz": req.blz, "user": req.user, "pin": req.pin,
                    "product_id": req.product_id, "url": req.url,
                }
                return _tan_payload(sid, need.resp)
    except HTTPException:
        raise
    except Exception as e:
        print("=== /holdings Fehler ===")
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"FinTS-Fehler: {e}")


@app.post("/tan")
def tan(req: TanReq):
    _check_key(req.x_api_key)
    _gc_sessions()
    s = SESSIONS.get(req.session)
    if not s:
        raise HTTPException(status_code=404, detail="Session abgelaufen/unbekannt. Bitte /holdings erneut.")
    try:
        client = FinTS3PinTanClient(
            s["blz"], s["user"], s["pin"], s["url"],
            product_id=(s["product_id"] or "30"),
            from_data=s["client_data"],
        )
        _set_browser_ua(client)
        with client.resume_dialog(s["dialog_data"]):
            tan_resp = NeedRetryResponse.from_data(s["tan_blob"])
            res = client.send_tan(tan_resp, req.tan)
            if isinstance(res, NeedTANResponse):
                # weitere TAN nötig -> neue Runde
                s["ts"] = time.time()
                s["tan_blob"] = res.get_data()
                s["dialog_data"] = client.pause_dialog()
                return _tan_payload(req.session, res)
            # TAN akzeptiert -> Daten einsammeln (im selben Dialog ohne weitere SCA)
            export = _collect(client, s["days"])
    except _TanNeeded as need:
        s["ts"] = time.time()
        s["tan_blob"] = need.resp.get_data()
        return _tan_payload(req.session, need.resp)
    except HTTPException:
        raise
    except Exception as e:
        print("=== /tan Fehler ===")
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"FinTS-TAN-Fehler: {e}")
    SESSIONS.pop(req.session, None)
    return export


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
