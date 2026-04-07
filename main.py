#!/usr/bin/env python3
"""
Scanner de gravações de implementação.
Roda a cada 15 minutos, detecta arquivos novos nos drives dos colaboradores,
copia para a pasta correta do cliente e posta comentário no Hoppe.
"""

import os, json, logging, time, urllib.request, urllib.error
from datetime import datetime, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from apscheduler.schedulers.blocking import BlockingScheduler

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("scanner")

# ── Config via env ───────────────────────────────────────────────────
GOOGLE_REFRESH_TOKEN  = os.environ["GOOGLE_REFRESH_TOKEN"]
GOOGLE_CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
HOPPE_BASE_URL        = os.environ.get("HOPPE_BASE_URL", "https://hoppe-api.bravy.com.br")
HOPPE_API_KEY         = os.environ["HOPPE_API_KEY"]
_INDEX_FILE   = os.path.join(os.path.dirname(__file__), "implementacoes_index.json")
_ALIASES_FILE = os.path.join(os.path.dirname(__file__), "implementacoes_aliases.json")
STATE_FILE            = os.environ.get("STATE_FILE", "/data/state.json")

GDOC_MIME   = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"

SOURCE_DRIVES = {
    "Nádia":      "1KmVy68P2T706S3LSaUPWw0V3JiHHq2Er",
    "Camila":     "1cwNLVcOLNBMHBf04v8R8wzVpmgwjIeMU",
    "Danivson":   "1UVTUHHGbon_DlGU1M1UYQ7qcb88Gg3q6",
    "Tiago":      "1QuVDw3GbRErmmRnQ-cJla6_bDrhEFtkk",
    "Produtos 1": "1mTlDS5QlfIQ-311Jhqr75FIpGtGyQ4eI",
    "Produtos 2": "1VCtux0WVvVu23_3nLylT_Jnb4-hTN2T-",
    "Produtos 3": "16pUyDvkClBFHkon48OrSiRPk73V3WjQG",
    "Produtos 4": "1ssgx_bsSeQTH6yV8ghxgUFygyyUygRzt",
}

# ── Google Auth ──────────────────────────────────────────────────────
_creds = None

def get_drive():
    global _creds
    if _creds is None or not _creds.valid:
        _creds = Credentials(
            token=None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        _creds.refresh(Request())
    return build("drive", "v3", credentials=_creds)

# ── Hoppe ────────────────────────────────────────────────────────────
def hoppe_request(method, path, body=None):
    url = HOPPE_BASE_URL + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {HOPPE_API_KEY}",
            "Content-Type": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error(f"Hoppe {method} {path} → {e.code}: {e.read()[:200]}")
        return None

def post_hoppe_comment(task_id, text):
    return hoppe_request("POST", f"/api/v2/task/{task_id}/comment", {
        "comment_text": text,
        "notify_all": False,
    })

# ── Index ────────────────────────────────────────────────────────────
def load_index():
    with open(_INDEX_FILE) as f:
        idx = json.load(f)
    try:
        with open(_ALIASES_FILE) as f:
            aliases = json.load(f)
    except FileNotFoundError:
        aliases = {}
    for hid, extra_terms in aliases.items():
        if hid.startswith("_"):
            continue
        if hid in idx:
            existing = set(idx[hid].get("terms", []))
            for t in extra_terms:
                if t not in existing:
                    idx[hid]["terms"].append(t)
    return idx

def build_matcher(index):
    matcher = {}
    for hid, info in index.items():
        if not info.get("gravacoes_id"):
            continue
        for term in info.get("terms", []):
            if term not in matcher:
                matcher[term] = []
            matcher[term].append((hid, info))
    return matcher

def match_file(filename, matcher, index):
    fname_lower = filename.lower()
    scores = {}
    for term, clients in matcher.items():
        if term in fname_lower:
            for hid, info in clients:
                if hid not in scores:
                    scores[hid] = set()
                scores[hid].add(term)
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda x: -len(x[1]))
    best_hid, best_terms = ranked[0]
    best_score = len(best_terms)
    tied = [h for h, t in ranked if len(t) == best_score]
    if len(tied) > 1:
        return None
    if best_score == 1 and len(list(best_terms)[0]) < 5:
        return None
    return best_hid, index[best_hid], best_terms

def dest_folder(mime, info):
    if mime in (GDOC_MIME, "text/plain", "text/html"):
        return info["transcricoes_id"], "Transcrições"
    return info["gravacoes_id"], "Gravações"

# ── State ────────────────────────────────────────────────────────────
def load_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_run": None, "processed_ids": []}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ── Scanner ──────────────────────────────────────────────────────────
def run_scan():
    log.info("=== Iniciando scan ===")
    try:
        drive = get_drive()
        index = load_index()
        matcher = build_matcher(index)
        state = load_state()

        since_ts = state.get("last_run") or (datetime.now(tz=timezone.utc).timestamp() - 86400)
        since_str = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        processed_ids = set(state.get("processed_ids", []))
        new_processed = []

        stats = {"found": 0, "matched": 0, "copied": 0, "skipped": 0, "errors": 0}

        for drive_name, folder_id in SOURCE_DRIVES.items():
            page_token = None
            while True:
                kwargs = dict(
                    q=(
                        f"'{folder_id}' in parents "
                        f"and trashed = false "
                        f"and createdTime > '{since_str}'"
                    ),
                    fields="nextPageToken, files(id, name, mimeType, createdTime)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageSize=200,
                    orderBy="createdTime",
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = drive.files().list(**kwargs).execute()
                files = resp.get("files", [])

                for f in files:
                    fid   = f["id"]
                    fname = f["name"]
                    fmime = f["mimeType"]
                    stats["found"] += 1

                    if fid in processed_ids:
                        stats["skipped"] += 1
                        continue

                    if fmime == FOLDER_MIME:
                        continue

                    result = match_file(fname, matcher, index)
                    if result is None:
                        log.warning(f"  SEM MATCH: {fname} [{drive_name}]")
                        continue

                    hid, client_info, matched_terms = result
                    dest_id, dest_label = dest_folder(fmime, client_info)
                    stats["matched"] += 1

                    log.info(f"  ✓ {fname[:50]} → {client_info['nome']} / {dest_label}")

                    try:
                        drive.files().copy(
                            fileId=fid,
                            body={"name": fname, "parents": [dest_id]},
                            supportsAllDrives=True,
                        ).execute()
                        stats["copied"] += 1
                        new_processed.append(fid)

                        # Comentário no Hoppe
                        comment = (
                            f"📁 Nova gravação detectada automaticamente:\n"
                            f"**{fname}**\n"
                            f"Drive: {drive_name} → {dest_label}\n"
                            f"Copiado em: {datetime.now(tz=timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC"
                        )
                        r = post_hoppe_comment(hid, comment)
                        if r:
                            log.info(f"  ✓ Comentário postado no Hoppe [{client_info['nome']}]")
                        else:
                            log.warning(f"  ! Comentário Hoppe falhou [{client_info['nome']}]")

                    except HttpError as e:
                        if "cannotCopyFile" in str(e):
                            log.warning(f"  ! cannotCopyFile (sem permissão): {fname}")
                            new_processed.append(fid)  # marcar como processado para não tentar de novo
                        else:
                            stats["errors"] += 1
                            log.error(f"  ✗ Erro ao copiar {fname}: {e}")

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        # Salvar state
        all_processed = list(processed_ids | set(new_processed))
        state["processed_ids"] = all_processed[-10000:]
        state["last_run"] = datetime.now(tz=timezone.utc).timestamp()
        save_state(state)

        log.info(
            f"=== Concluído | encontrados={stats['found']} matched={stats['matched']} "
            f"copiados={stats['copied']} erros={stats['errors']} ==="
        )

    except Exception as e:
        log.exception(f"Erro no scan: {e}")


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Scanner service iniciando...")
    run_scan()  # roda imediatamente na inicialização

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_scan, "interval", minutes=15, id="scanner")
    log.info("Agendado: a cada 15 minutos")
    scheduler.start()
