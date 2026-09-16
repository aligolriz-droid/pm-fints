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
                    -> ODER {status:"tan_required", session, challenge, challenge_image_b64}
   POST /tan        Body: {session, tan}
                    -> db_export-JSON (nach TAN-Eingabe)
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
from decimal import Decimal
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fints.client import FinTS3PinTanClient, NeedTANResponse, NeedRetryResponse

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
        rows.append({
            "date": date.isoformat() if hasattr(date, "isoformat") else (str(date) if date else None),
            "amount": amt, "currency": cur,
            "payee": clean_payee,
            "purpose": str(d.get("purpose") or d.get("posting_text") or ""),
            "account": iban,
            "counter_iban": counter_iban,
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
    accounts = client.get_sepa_accounts()
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


def _tan_payload(sid: str, resp: NeedTANResponse) -> Dict[str, Any]:
    img = None
    try:
        if getattr(resp, "challenge_matrix", None):
            _mime, data = resp.challenge_matrix
            img = base64.b64encode(data).decode("ascii")
    except Exception:
        img = None
    return {
        "status": "tan_required",
        "session": sid,
        "challenge": getattr(resp, "challenge", "") or "",
        "challenge_image_b64": img,
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
                               product_id=(req.product_id or None))
    try:
        with client:
            try:
                return _collect(client, req.days)
            except _TanNeeded as need:
                # Dialog + Client-Zustand für den TAN-Schritt persistieren
                sid = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii")
                client_data = client.deconstruct(including_private=True)
                # Dialog pausieren (Kontextmanager liefert Bytes)
                with client.pause_dialog() as dialog_data:
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
            product_id=(s["product_id"] or None),
            from_data=s["client_data"],
        )
        with client.resume_dialog(s["dialog_data"]):
            tan_resp = NeedRetryResponse.from_data(s["tan_blob"])
            res = client.send_tan(tan_resp, req.tan)
            if isinstance(res, NeedTANResponse):
                # weitere TAN nötig -> neue Runde
                s["ts"] = time.time()
                s["tan_blob"] = res.get_data()
                s["dialog_data"] = client.pause_dialog().__enter__()
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
        raise HTTPException(status_code=502, detail=f"FinTS-TAN-Fehler: {e}")
    SESSIONS.pop(req.session, None)
    return export


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
