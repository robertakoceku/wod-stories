#!/usr/bin/env python3
"""Pubblica la storia del WOD di oggi su Instagram e sulla Pagina Facebook.

Come funziona
-------------
Le grafiche stanno nel repo sotto `stories/`, una per giorno, con nome
`AAAA-MM-GG.jpg` (es. `stories/2026-11-09.jpg`). Ogni mattina il workflow di
GitHub Actions lancia questo script, che:

1. calcola la data di oggi in Italia (Europe/Rome, non UTC);
2. cerca `stories/<oggi>.jpg`. Se non c'è — palestra chiusa, grafica non
   ancora caricata — esce senza fare niente e senza errori;
3. verifica che l'immagine sia davvero raggiungibile dall'esterno;
4. crea il "contenitore" su Instagram e lo pubblica;
5. carica la stessa immagine sulla Pagina Facebook come storia.

Le storie non hanno didascalia: né Instagram né Facebook la mostrano, quindi
non la mandiamo.

Se una delle due pubblicazioni fallisce, l'altra viene comunque tentata: lo
script esce in errore solo alla fine, così non succede che un problema su
Facebook blocchi la storia su Instagram.

Variabili d'ambiente richieste (su GitHub stanno nei Secrets)
-------------------------------------------------------------
IG_USER_ID       ID numerico dell'account Instagram business
IG_ACCESS_TOKEN  token della Pagina (segreto, mai scritto nei log)
IMAGE_BASE_URL   base degli indirizzi delle immagini, es.
                 https://raw.githubusercontent.com/<utente>/<repo>/main/stories

Facoltative
-----------
FB_PAGE_ID       ID della Pagina Facebook. Se è vuoto, si pubblica solo su
                 Instagram.
GRAPH_VERSION    versione delle API Meta (default v26.0)
STORY_DATE       forza una data (AAAA-MM-GG) invece di "oggi" — serve per le prove
DRY_RUN          se "1", fa tutti i controlli ma NON pubblica
ORA_FERIALE      ora italiana di pubblicazione da lunedì a venerdì (default 5)
ORA_WEEKEND      ora italiana di pubblicazione sabato e domenica (default 9)
FORCE            se "1", ignora il controllo dell'ora (per le prove a mano)

Il workflow parte a più orari UTC per coprire ora solare e ora legale: solo la
corsa che in Italia cade all'ora giusta pubblica, le altre escono subito.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

GRAPH = "https://graph.facebook.com"
TZ = ZoneInfo("Europe/Rome")


def log(msg):
    print(msg, flush=True)


def errore(msg, codice=1):
    log(f"[X] {msg}")
    sys.exit(codice)


class ErroreMeta(Exception):
    pass


def richiedi(url, dati=None):
    """POST (se dati) o GET verso le API Meta. Ritorna il JSON della risposta.

    In caso di problema solleva ErroreMeta, così chi chiama può decidere se
    fermarsi o tentare comunque l'altro social.
    """
    corpo = urllib.parse.urlencode(dati).encode() if dati else None
    req = urllib.request.Request(url, data=corpo)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        dettaglio = e.read().decode(errors="replace")[:600]
        # il token non compare mai nei log: è passato nel corpo, non nell'URL
        raise ErroreMeta(f"Meta ha risposto {e.code}: {dettaglio}")
    except urllib.error.URLError as e:
        raise ErroreMeta(f"Rete non raggiungibile: {e.reason}")


def immagine_raggiungibile(url):
    """Meta deve poter scaricare l'immagine: controlliamolo prima di chiamarla."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tipo = r.headers.get("Content-Type", "")
            lunghezza = int(r.headers.get("Content-Length") or 0)
            return r.status == 200, tipo, lunghezza
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", 0
    except urllib.error.URLError as e:
        return False, str(e.reason), 0


def pubblica_instagram(versione, ig_user, token, url_immagine):
    """Storia su Instagram: prima il contenitore, poi la pubblicazione."""
    log("-> Instagram: creo il contenitore...")
    contenitore = richiedi(
        f"{GRAPH}/{versione}/{ig_user}/media",
        {"media_type": "STORIES", "image_url": url_immagine,
         "access_token": token},
    )
    cid = contenitore.get("id")
    if not cid:
        raise ErroreMeta(f"Nessun id di contenitore nella risposta: {contenitore}")

    pubblicato = richiedi(
        f"{GRAPH}/{versione}/{ig_user}/media_publish",
        {"creation_id": cid, "access_token": token},
    )
    mid = pubblicato.get("id")
    if not mid:
        raise ErroreMeta(f"Pubblicazione non confermata: {pubblicato}")
    log(f"[ok] Instagram: storia pubblicata - id {mid}")


def pubblica_facebook(versione, page_id, token, url_immagine):
    """Storia sulla Pagina Facebook.

    Funziona in due tempi: si carica la foto *non pubblicata* (resta invisibile
    sulla Pagina), poi la si trasforma in storia.
    """
    log("-> Facebook: carico la foto...")
    foto = richiedi(
        f"{GRAPH}/{versione}/{page_id}/photos",
        {"url": url_immagine, "published": "false", "access_token": token},
    )
    pid = foto.get("id")
    if not pid:
        raise ErroreMeta(f"Nessun id di foto nella risposta: {foto}")

    storia = richiedi(
        f"{GRAPH}/{versione}/{page_id}/photo_stories",
        {"photo_id": pid, "access_token": token},
    )
    if not storia.get("success") and not storia.get("post_id"):
        raise ErroreMeta(f"Storia non confermata: {storia}")
    log(f"[ok] Facebook: storia pubblicata - id {storia.get('post_id', pid)}")


def main():
    ig_user = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    base = os.environ.get("IMAGE_BASE_URL", "").strip().rstrip("/")
    page_id = os.environ.get("FB_PAGE_ID", "").strip()
    versione = os.environ.get("GRAPH_VERSION", "v26.0").strip()
    prova = os.environ.get("DRY_RUN", "").strip() == "1"

    mancanti = [n for n, v in (("IG_USER_ID", ig_user),
                               ("IG_ACCESS_TOKEN", token),
                               ("IMAGE_BASE_URL", base)) if not v]
    if mancanti:
        errore(f"Mancano le variabili: {', '.join(mancanti)}")

    adesso = datetime.now(TZ)

    # Guardia sull'ora: il workflow gira a più orari UTC per coprire ora legale
    # e solare, ma deve pubblicare una volta sola, all'ora italiana giusta.
    if os.environ.get("FORCE", "").strip() != "1":
        feriale = adesso.weekday() < 5          # 0=lunedì ... 4=venerdì
        ora_target = int(os.environ.get("ORA_FERIALE", "5") if feriale
                         else os.environ.get("ORA_WEEKEND", "9"))
        if adesso.hour != ora_target:
            quando = "nei feriali" if feriale else "nel weekend"
            log(f"- In Italia sono le {adesso.hour}:00; {quando} si pubblica "
                f"alle {ora_target}:00. Niente da fare.")
            return 0

    giorno = os.environ.get("STORY_DATE", "").strip() \
        or adesso.strftime("%Y-%m-%d")
    url_immagine = f"{base}/{giorno}.jpg"
    log(f"-> Giorno: {giorno}")
    log(f"-> Immagine: {url_immagine}")

    ok, tipo, peso = immagine_raggiungibile(url_immagine)
    if not ok:
        # Nessuna grafica per oggi non è un errore: palestra chiusa o
        # settimana non ancora caricata. Usciamo in silenzio.
        log(f"- Nessuna immagine per oggi ({tipo}). Niente da pubblicare.")
        return 0
    if "image" not in tipo:
        errore(f"L'indirizzo non restituisce un'immagine ma '{tipo}'")
    log(f"[ok] Immagine raggiungibile ({tipo}, {peso // 1024} KB)")

    if prova:
        log("- DRY_RUN attivo: mi fermo qui, non pubblico.")
        return 0

    # Instagram e Facebook sono indipendenti: se uno dei due fallisce,
    # l'altro viene comunque tentato.
    problemi = []

    try:
        pubblica_instagram(versione, ig_user, token, url_immagine)
    except ErroreMeta as e:
        log(f"[X] Instagram: {e}")
        problemi.append("Instagram")

    if page_id:
        try:
            pubblica_facebook(versione, page_id, token, url_immagine)
        except ErroreMeta as e:
            log(f"[X] Facebook: {e}")
            problemi.append("Facebook")
    else:
        log("- FB_PAGE_ID non impostato: salto Facebook.")

    if problemi:
        errore(f"Pubblicazione fallita su: {', '.join(problemi)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
