# B) FinTS-Service (Cloud Run / Fly.io / Render)

**Dateien:** `db_fints_service.py`, `Dockerfile`, `requirements.txt`, `../render.yaml`

> ## 📱 iPad/iPhone: Welchen Weg nehmen?
> Am iPad kann **kein Python laufen** und die Deutsche Bank spricht nur FinTS. Es gibt zwei Wege:
>
> **🟢 Weg A – ohne Server (Standard, empfohlen)**
> Einmal am PC/Mac den Connector laufen lassen (`fints-connector/db_fints_connector.py`
> bzw. den Ein-Klick-Installer) → er erzeugt **`db_export.json`**. Datei in **iCloud/Dateien**
> legen und in der App **ins Import-Feld ablegen**. PIN/TAN verlassen nie deinen Rechner.
> **Es muss NICHTS deployt werden.**
>
> **🔵 Weg B – live vom iPad (dieser Service)**
> Diesen kleinen Server **einmalig** hosten (unten). Danach klappt der Abruf direkt vom iPad.
> Du brauchst dafür ein **kostenloses Konto** beim Anbieter (einmaliger Login).

## 🚀 1-Klick-Deploy (Render Blueprint) – der einfachste Weg B
1. Diesen Suite-Ordner (mit `render.yaml` + `fints-service/`) in ein **GitHub-Repo** legen.
2. **[Deploy to Render]** öffnen: `https://dashboard.render.com/select-repo?type=blueprint`
   → dein Repo wählen → Render liest `render.yaml` und baut **alles automatisch**
   (Docker-Build, `API_KEY` wird **automatisch erzeugt**, Health-Check `/health`).
3. In der App unter **🏦 Deutsche Bank → 🌐 Remote-Abruf** die fertige **Server-URL**
   und den **API-Key** (Render → Service → *Environment*) eintragen → **"🌐 Vom Server abrufen"**.

> Markdown-Button fürs Repo-README:
> `[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)`

⚠️ **Nach dem ersten Test** `ALLOW_ORIGIN` von `"*"` auf deine Netlify-Domain
(`https://DEINNAME.netlify.app`) einschränken (Render → Environment).

## Was macht es?
Stellt den Deutsche-Bank-FinTS-Abruf als HTTP-Service bereit, damit die gehostete
App den Depotbestand **remote** laden kann.

Endpunkte:
- `POST /holdings` `{blz,user,pin,product_id,url,days,x_api_key}`
  → `db_export`-JSON **oder** `{status:"tan_required",session,challenge,challenge_image_b64}`
- `POST /tan` `{session,tan,x_api_key}` → `db_export`-JSON
- `GET /health` → `{ok:true}`

## ⚠️ Sicherheit zuerst
Dieser Dienst verarbeitet **Bank-PIN & TAN**. In der Cloud verlassen die Zugangsdaten
dein Gerät. Deshalb **immer**:
- nur über **HTTPS** betreiben,
- **API-Key** setzen (`API_KEY`) → im Tool ins Feld "API-Key" eintragen,
- **`ALLOW_ORIGIN`** auf deine Netlify-Domain einschränken,
- Dienst **nicht öffentlich/anonym** erreichbar lassen,
- PIN **nie** loggen (macht der Code nicht).

**Am sichersten:** stattdessen den lokalen `db_fints_connector.py` nutzen – dann
verlässt nichts deinen Rechner.

## Lokal testen
```bash
pip install -r requirements.txt
uvicorn db_fints_service:app --host 0.0.0.0 --port 8080
curl http://127.0.0.1:8080/health
```

## Deploy – Google Cloud Run
```bash
gcloud run deploy db-fints \
  --source . --region europe-west3 \
  --allow-unauthenticated=false \
  --set-env-vars API_KEY=DEIN_KEY,ALLOW_ORIGIN=https://DEINNAME.netlify.app
```

## Deploy – Fly.io
```bash
fly launch --no-deploy         # erkennt Dockerfile
fly secrets set API_KEY=DEIN_KEY ALLOW_ORIGIN=https://DEINNAME.netlify.app
fly deploy
```

## Deploy – Render
Neues **Web Service** aus dem Repo, Runtime **Docker**, ENV `API_KEY` + `ALLOW_ORIGIN`.

## Im Tool verbinden
Reiter **🏦 Deutsche Bank (FinTS)** → **🌐 Remote-Abruf**:
Server-URL (z. B. `https://db-fints-xxxx.run.app`), API-Key, BLZ, Benutzer
(= Filial- + Kontonummer), PIN, Produkt-ID, Tage → **"🌐 Vom Server abrufen"**.
Bei TAN-Abfrage TAN eingeben → Depot-Snapshot wird importiert.

## Voraussetzungen der Bank
- FinTS im DB-Online-Banking aktiviert.
- **Produkt-ID** registriert (Pflicht seit 14.09.2019): https://www.hbci-zka.de/register/prod_register.htm
- Hinweis: FinTS liefert nur einen **Snapshot** (ISIN/Stück/Wert), **keine Einstandskurse**.
