#!/usr/bin/env python3
"""
Scanner de gravações de implementação.
Roda a cada 15 minutos, detecta arquivos novos nos drives dos colaboradores,
identifica o cliente pelo UUID presente no nome do arquivo, garante a pasta
no Drive (cria se não existir) e copia para Gravações/Transcrições.
"""

import os, re, json, logging, urllib.request, urllib.error
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
OPENAI_API_KEY        = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL          = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
STATE_FILE            = os.environ.get("STATE_FILE", "/data/state.json")
CACHE_FILE            = os.environ.get("CACHE_FILE", "/data/clients_cache.json")

# Pasta raiz no Drive onde ficam as pastas dos clientes (uma por implementação)
ROOT_FOLDER_ID = os.environ.get("ROOT_FOLDER_ID", "1Xnb5wZmuzx8N6leyNDoADwsVTV4Ytr9Y")
ORPHAN_FOLDER_NAME = "Órfãos"

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

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

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
            "Authorization": HOPPE_API_KEY,
            "Content-Type": "application/json",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error(f"Hoppe {method} {path} → {e.code}: {e.read()[:200]}")
        return None
    except urllib.error.URLError as e:
        log.error(f"Hoppe {method} {path} → URLError: {e}")
        return None

def post_hoppe_comment(task_id, text):
    return hoppe_request("POST", f"/api/v2/task/{task_id}/comment", {
        "comment_text": text,
        "notify_all": False,
    })

def fetch_hoppe_task(task_id):
    return hoppe_request("GET", f"/api/v2/task/{task_id}")

# ── OpenAI ───────────────────────────────────────────────────────────
SUMMARY_SYSTEM = (
    "Você resume transcrições de reuniões de implementação de projetos da Bravy. "
    "Escreva em português do Brasil, objetivo e direto. Sempre responda em JSON válido, sem texto extra."
)

SUMMARY_USER_TEMPLATE = """Abaixo está a transcrição de uma reunião. Gere um resumo estruturado.

Retorne apenas um JSON com este schema exato:
{{
  "resumo": "2 a 4 frases cobrindo o que foi discutido, decisões e contexto principal",
  "proximos_passos": ["passo 1", "passo 2", "..."],
  "riscos": ["risco 1 com contexto curto", "risco 2", "..."]
}}

Regras:
- "proximos_passos": lista de ações concretas combinadas na reunião. Se não houver, retorne [].
- "riscos": alertas reais (prazo apertado, reclamação do cliente, bloqueio, dependência crítica, dúvida não resolvida, ruído no relacionamento). Se não houver, retorne [].
- Nunca invente. Se a transcrição for muito curta ou vazia, retorne campos vazios.

Transcrição:
---
{text}
---"""

def _escape_html(s):
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

def summarize_transcript(text):
    """Chama OpenAI e retorna dict {resumo, proximos_passos, riscos}. None se falhar."""
    if not OPENAI_API_KEY:
        return None
    if not text or len(text.strip()) < 50:
        return None

    # Corta pra não estourar contexto (gpt-4o-mini aceita 128k tokens; ~500k chars teórico, mas caro)
    max_chars = 120_000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[transcrição truncada]"

    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": SUMMARY_USER_TEMPLATE.format(text=text)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        content = resp["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return {
            "resumo": (parsed.get("resumo") or "").strip(),
            "proximos_passos": [str(p).strip() for p in (parsed.get("proximos_passos") or []) if str(p).strip()],
            "riscos": [str(r).strip() for r in (parsed.get("riscos") or []) if str(r).strip()],
        }
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        log.warning(f"  OpenAI falhou: {e}")
        return None

def export_doc_text(drive, file_id):
    """Exporta Google Doc como texto plano."""
    try:
        res = drive.files().export(fileId=file_id, mimeType="text/plain").execute()
        if isinstance(res, bytes):
            return res.decode("utf-8", errors="replace")
        return str(res)
    except HttpError as e:
        log.warning(f"  Falha ao exportar Doc {file_id}: {e}")
        return None

def build_comment(fname, drive_name, dest_label, summary):
    when = datetime.now(tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    singular = {"Gravações": "gravação", "Transcrições": "transcrição"}.get(dest_label, dest_label.lower())
    parts = [f"<p>📁 Nova {singular} detectada: <strong>{_escape_html(fname)}</strong></p>"]

    if summary:
        if summary.get("resumo"):
            parts.append("<h3>📝 Resumo da reunião</h3>")
            parts.append(f"<p>{_escape_html(summary['resumo'])}</p>")
        if summary.get("proximos_passos"):
            parts.append("<h3>➡️ Próximos passos</h3>")
            parts.append("<ul>")
            for p in summary["proximos_passos"]:
                parts.append(f"<li>{_escape_html(p)}</li>")
            parts.append("</ul>")
        if summary.get("riscos"):
            parts.append("<h3>⚠️ Riscos detectados</h3>")
            parts.append("<ul>")
            for r in summary["riscos"]:
                parts.append(f"<li>{_escape_html(r)}</li>")
            parts.append("</ul>")

    parts.append("<hr>")
    parts.append(f"<p><em>Drive: {_escape_html(drive_name)} · Destino: {dest_label} · {when}</em></p>")
    return "\n".join(parts)

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

def load_cache():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# ── Resolução cliente + pastas ───────────────────────────────────────
def extract_uuid(filename):
    m = UUID_RE.search(filename)
    return m.group(0).lower() if m else None

def _find_folder(drive, parent_id, name):
    name_escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents "
        f"and name = '{name_escaped}' "
        f"and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false"
    )
    resp = drive.files().list(
        q=q,
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=10,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None

def _create_folder(drive, parent_id, name):
    meta = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
    created = drive.files().create(
        body=meta, fields="id", supportsAllDrives=True,
    ).execute()
    return created["id"]

def _ensure_subfolder(drive, parent_id, name):
    existing = _find_folder(drive, parent_id, name)
    if existing:
        return existing
    fid = _create_folder(drive, parent_id, name)
    log.info(f"    ✓ Criada subpasta: {name}")
    return fid

def ensure_client_folders(drive, hid, nome, cache):
    """Garante pasta do cliente + subpastas Gravações/Transcrições. Retorna dict."""
    folder_name = f"{nome} - {hid}"
    client_folder_id = _find_folder(drive, ROOT_FOLDER_ID, folder_name)
    if not client_folder_id:
        client_folder_id = _create_folder(drive, ROOT_FOLDER_ID, folder_name)
        log.info(f"  ✓ Criada pasta cliente: {folder_name}")

    gravacoes_id    = _ensure_subfolder(drive, client_folder_id, "Gravações")
    transcricoes_id = _ensure_subfolder(drive, client_folder_id, "Transcrições")

    entry = {
        "nome": nome,
        "client_folder_id": client_folder_id,
        "gravacoes_id": gravacoes_id,
        "transcricoes_id": transcricoes_id,
    }
    cache[hid] = entry
    save_cache(cache)
    return entry

def resolve_client(drive, hid, cache):
    """Resolve UUID → cliente com pastas. Cria pastas se faltar. None se UUID inválido."""
    cached = cache.get(hid)
    if cached and cached.get("gravacoes_id") and cached.get("transcricoes_id"):
        return cached

    task = fetch_hoppe_task(hid)
    if not task or not task.get("name"):
        return None
    nome = task["name"].strip()
    return ensure_client_folders(drive, hid, nome, cache)

def dest_folder(mime, client_info):
    if mime in (GDOC_MIME, "text/plain", "text/html"):
        return client_info["transcricoes_id"], "Transcrições"
    return client_info["gravacoes_id"], "Gravações"

_orphan_id_cache = None
def ensure_orphan_folder(drive):
    global _orphan_id_cache
    if _orphan_id_cache:
        return _orphan_id_cache
    existing = _find_folder(drive, ROOT_FOLDER_ID, ORPHAN_FOLDER_NAME)
    if existing:
        _orphan_id_cache = existing
        return existing
    fid = _create_folder(drive, ROOT_FOLDER_ID, ORPHAN_FOLDER_NAME)
    log.info(f"  ✓ Criada pasta Órfãos")
    _orphan_id_cache = fid
    return fid

# ── Scanner ──────────────────────────────────────────────────────────
def run_scan():
    log.info("=== Iniciando scan ===")
    try:
        drive = get_drive()
        state = load_state()
        cache = load_cache()

        since_ts = state.get("last_run") or (datetime.now(tz=timezone.utc).timestamp() - 86400)
        since_str = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        processed_ids = set(state.get("processed_ids", []))
        new_processed = []

        stats = {"found": 0, "matched": 0, "copied": 0, "skipped": 0, "errors": 0, "no_uuid": 0, "unknown_uuid": 0}

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

                    hid = extract_uuid(fname)
                    if not hid:
                        stats["no_uuid"] += 1
                        log.warning(f"  SEM UUID: {fname} [{drive_name}] → Órfãos")
                        try:
                            orphan_id = ensure_orphan_folder(drive)
                            drive.files().copy(
                                fileId=fid,
                                body={"name": f"[{drive_name}] {fname}", "parents": [orphan_id]},
                                supportsAllDrives=True,
                            ).execute()
                            stats["orphan_copied"] = stats.get("orphan_copied", 0) + 1
                            new_processed.append(fid)
                        except HttpError as e:
                            if "cannotCopyFile" in str(e):
                                new_processed.append(fid)
                            else:
                                stats["errors"] += 1
                                log.error(f"  ✗ Erro ao copiar órfão {fname}: {e}")
                        continue

                    client_info = resolve_client(drive, hid, cache)
                    if not client_info:
                        stats["unknown_uuid"] += 1
                        log.warning(f"  UUID desconhecido no Hoppe ({hid}): {fname}")
                        continue

                    dest_id, dest_label = dest_folder(fmime, client_info)
                    stats["matched"] += 1

                    log.info(f"  ✓ {fname[:60]} → {client_info['nome']} / {dest_label}")

                    try:
                        drive.files().copy(
                            fileId=fid,
                            body={"name": fname, "parents": [dest_id]},
                            supportsAllDrives=True,
                        ).execute()
                        stats["copied"] += 1
                        new_processed.append(fid)

                        summary = None
                        if fmime == GDOC_MIME:
                            text = export_doc_text(drive, fid)
                            if text:
                                summary = summarize_transcript(text)
                                if summary:
                                    log.info(f"  ✓ Resumo gerado ({len(summary.get('proximos_passos', []))} passos, {len(summary.get('riscos', []))} riscos)")

                        comment = build_comment(fname, drive_name, dest_label, summary)
                        r = post_hoppe_comment(hid, comment)
                        if r:
                            log.info(f"  ✓ Comentário postado no Hoppe [{client_info['nome']}]")
                        else:
                            log.warning(f"  ! Comentário Hoppe falhou [{client_info['nome']}]")

                    except HttpError as e:
                        if "cannotCopyFile" in str(e):
                            log.warning(f"  ! cannotCopyFile (sem permissão): {fname}")
                            new_processed.append(fid)
                        else:
                            stats["errors"] += 1
                            log.error(f"  ✗ Erro ao copiar {fname}: {e}")

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        all_processed = list(processed_ids | set(new_processed))
        state["processed_ids"] = all_processed[-10000:]
        state["last_run"] = datetime.now(tz=timezone.utc).timestamp()
        save_state(state)

        log.info(
            f"=== Concluído | encontrados={stats['found']} matched={stats['matched']} "
            f"copiados={stats['copied']} sem_uuid={stats['no_uuid']} "
            f"órfãos_copiados={stats.get('orphan_copied', 0)} "
            f"uuid_desconhecido={stats['unknown_uuid']} erros={stats['errors']} ==="
        )

    except Exception as e:
        log.exception(f"Erro no scan: {e}")


# ── Entry point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Scanner service iniciando...")
    run_scan()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_scan, "interval", minutes=15, id="scanner")
    log.info("Agendado: a cada 15 minutos")
    scheduler.start()
