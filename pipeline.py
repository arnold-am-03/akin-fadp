"""
Motor del pipeline incremental FADP → Anki.

Carga el estado desde Dropbox, detecta solo las clases nuevas (por content_hash),
genera tarjetas con Gemini únicamente para lo nuevo, reconstruye el mazo maestro
y sube estado + .apkg de vuelta a Dropbox.

Toda la configuración se lee de variables de entorno (ver README).
"""
import os
import re
import json
import time
import hashlib
import unicodedata
import datetime
from difflib import SequenceMatcher

import fitz                       # PyMuPDF
import genanki
import dropbox
from google import genai
from google.genai import types, errors as genai_errors

# --------------------------------------------------------------------------- #
# Configuración (variables de entorno)
# --------------------------------------------------------------------------- #
DROPBOX_TOKEN  = os.environ.get("DROPBOX_TOKEN", "")
DBX_APP_KEY    = os.environ.get("DBX_APP_KEY", "")
DBX_APP_SECRET = os.environ.get("DBX_APP_SECRET", "")
DBX_REFRESH    = os.environ.get("DBX_REFRESH_TOKEN", "")

DROPBOX_BASE       = os.environ.get("DROPBOX_BASE", "/Aplicaciones/Rakuten Kobo/CURSO")
DROPBOX_OUTPUT_DIR = os.environ.get("DROPBOX_OUTPUT_DIR", "/FADP/anki")
STATE_FILE = f"{DROPBOX_OUTPUT_DIR}/fadp_estado.json"
APKG_NAME  = os.environ.get("APKG_NAME", "fadp_general.apkg")

SESSION_FILTER = os.environ.get("SESSION_FILTER") or None   # None = todas

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
CHUNK_SIZE       = int(os.environ.get("CHUNK_SIZE", "4500"))
CHUNK_OVERLAP    = int(os.environ.get("CHUNK_OVERLAP", "200"))
CARDS_POR_BLOQUE = int(os.environ.get("CARDS_POR_BLOQUE", "18"))
UMBRAL_MARCA     = float(os.environ.get("UMBRAL_MARCA", "0.70"))
EXCLUDE_KEYWORDS = ["silabo", "silabus", "syllabus"]
DECK_NAME = os.environ.get("DECK_NAME", "FADP")

WORKDIR = os.environ.get("WORKDIR", "/tmp/fadp")

# --------------------------------------------------------------------------- #
# Helpers de clasificación
# --------------------------------------------------------------------------- #
def sanitize(s):
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")


def normaliza(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def parse_curso_sesion(path, base):
    parts = path[len(base):].lstrip("/").split("/")
    return (parts[0] if len(parts) >= 1 else "SIN_CURSO",
            parts[1] if len(parts) >= 3 else "GENERAL")


def es_excluido(nombre):
    n = normaliza(nombre)
    return any(k in n for k in EXCLUDE_KEYWORDS)


def _tokens_iniciales(n):
    pre = n.split("_")[0]
    m = re.match(r"^([a-z]+)", n)
    return {x for x in (pre, m.group(1) if m else "") if x}


def tipo_por_marca(nombre, umbral=UMBRAL_MARCA):
    """Reconoce CLASE_/LECTURA_ al inicio tolerando erratas (similitud difusa)."""
    n = normaliza(nombre)
    if n.startswith("clase_"):
        return "clase", 1.0
    if n.startswith("lectura_"):
        return "lectura", 1.0
    cands = _tokens_iniciales(n)
    sc = max((SequenceMatcher(None, c, "clase").ratio() for c in cands), default=0)
    sl = max((SequenceMatcher(None, c, "lectura").ratio() for c in cands), default=0)
    if sc >= umbral and sc >= sl:
        return "clase", sc
    if sl >= umbral and sl > sc:
        return "lectura", sl
    return "auto", max(sc, sl)


# --------------------------------------------------------------------------- #
# PDF → texto → bloques
# --------------------------------------------------------------------------- #
def pdf_to_text(path):
    doc = fitz.open(path)
    txt = "\n".join(p.get_text() for p in doc)
    doc.close()
    return txt


def chunk(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = " ".join(text.split())
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return [c for c in out if len(c) > 300]


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
SYSTEM = """Eres experto en ciencias sociales (ciencia política, historia, derecho internacional,
economía, geografía del Perú y el mundo, diplomacia) y en diseño de tarjetas de recuperación
para un examen de admisión exigente de opción múltiple (estilo Academia Diplomática del Perú).

OBJETIVO: a partir de un fragmento de material de clase, extraer de forma EXHAUSTIVA todos los
datos examinables y convertirlos en tarjetas pregunta-respuesta atómicas. El examen privilegia
el reconocimiento preciso de: quién hizo qué, quién dijo o escribió qué, autores y sus obras o
conceptos, características de culturas/civilizaciones, definiciones de conceptos, distinciones
entre conceptos parecidos, instituciones y sus funciones, y protagonistas de procesos históricos.

REGLAS:
- EXHAUSTIVIDAD: una tarjeta por cada dato examinable distinto. No resumas; cúbrelo todo.
- ATÓMICAS: una sola idea por tarjeta. Nada de listas largas, salvo conceptos que SEAN una lista
  breve y cerrada (p. ej. los elementos constitutivos del Estado).
- VARIEDAD de formulación, sin molde fijo: identificación ("¿Quién escribió/acuñó...?"),
  atribución ("Según X, ¿...?"), definición ("¿Qué es...?"), distinción ("¿En qué se diferencia
  X de Y?"), características ("¿Qué rasgos definen...?"), función de instituciones, protagonistas.
- PRIORIZA nombres propios, autores, conceptos y atribuciones por encima de fechas sueltas.
- PRECISIÓN: respuestas breves (1-2 frases). No inventes; ante la duda, omite el dato.
- NO RELLENES: si hay poco contenido examinable, genera pocas tarjetas.
- Si es portada, índice, bibliografía o ruido, devuelve []."""

_schema = types.Schema(type=types.Type.ARRAY, items=types.Schema(
    type=types.Type.OBJECT,
    properties={"q": types.Schema(type=types.Type.STRING),
                "a": types.Schema(type=types.Type.STRING)},
    required=["q", "a"]))

_config = types.GenerateContentConfig(
    system_instruction=SYSTEM, temperature=0.3, max_output_tokens=8192,
    response_mime_type="application/json", response_schema=_schema)

_gemini = None


def _client():
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini


def cards_from_chunk(text, log, max_cards=CARDS_POR_BLOQUE, reintentos=4):
    prompt = (f"Extrae de forma EXHAUSTIVA hasta {max_cards} tarjetas examinables de este "
              f"fragmento de material de clase. Cubre todos los autores, obras, citas, conceptos, "
              f"distinciones y características:\n\n{text}")
    for intento in range(reintentos):
        try:
            resp = _client().models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=_config)
            try:
                data = json.loads((resp.text or "").strip())
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                log("  (respuesta no parseable, bloque omitido)")
                return []
        except genai_errors.ServerError:
            esp = 10 * (intento + 1)
            log(f"  Modelo saturado (503). Espero {esp}s y reintento...")
            time.sleep(esp)
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) == 429 and "limit: 0" in str(e):
                raise RuntimeError(
                    "Gemini sin cuota (limit: 0). Activa facturación o cambia de modelo.") from e
            if getattr(e, "code", None) == 429:
                esp = 8 * (intento + 1)
                log(f"  Límite por minuto (429). Espero {esp}s y reintento...")
                time.sleep(esp)
            else:
                raise
    log("  Reintentos agotados; bloque omitido.")
    return []


def generar_con_sistema(text, system_instruction, log, max_cards=18, reintentos=4):
    """Genera tarjetas JSON con una instrucción de sistema arbitraria (reutilizable)."""
    cfg = types.GenerateContentConfig(
        system_instruction=system_instruction, temperature=0.3, max_output_tokens=8192,
        response_mime_type="application/json", response_schema=_schema)
    prompt = (f"Extrae de forma EXHAUSTIVA hasta {max_cards} tarjetas de este "
              f"fragmento de texto:\n\n{text}")
    for intento in range(reintentos):
        try:
            resp = _client().models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=cfg)
            try:
                data = json.loads((resp.text or "").strip())
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                log("  (respuesta no parseable, bloque omitido)")
                return []
        except genai_errors.ServerError:
            esp = 10 * (intento + 1)
            log(f"  Modelo saturado (503). Espero {esp}s y reintento...")
            time.sleep(esp)
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) == 429 and "limit: 0" in str(e):
                raise RuntimeError(
                    "Gemini sin cuota (limit: 0). Activa facturación o cambia de modelo.") from e
            if getattr(e, "code", None) == 429:
                esp = 8 * (intento + 1)
                log(f"  Límite por minuto (429). Espero {esp}s y reintento...")
                time.sleep(esp)
            else:
                raise
    log("  Reintentos agotados; bloque omitido.")
    return []


# --------------------------------------------------------------------------- #
# Dropbox + estado
# --------------------------------------------------------------------------- #
def get_dbx():
    if DBX_REFRESH and DBX_APP_KEY and DBX_APP_SECRET:
        return dropbox.Dropbox(oauth2_refresh_token=DBX_REFRESH,
                               app_key=DBX_APP_KEY, app_secret=DBX_APP_SECRET)
    if DROPBOX_TOKEN:
        return dropbox.Dropbox(DROPBOX_TOKEN)
    raise RuntimeError("Faltan credenciales de Dropbox (refresh token recomendado para Render).")


def cargar_estado(dbx):
    try:
        _, resp = dbx.files_download(STATE_FILE)
        estado = json.loads(resp.content)
    except dropbox.exceptions.ApiError:
        estado = {}
    estado.setdefault("cards", [])
    estado.setdefault("procesados", [])
    estado.setdefault("actualizado", None)
    return estado


def guardar_estado(dbx, estado):
    estado["actualizado"] = datetime.datetime.now().isoformat(timespec="seconds")
    dbx.files_upload(json.dumps(estado, ensure_ascii=False, indent=1).encode("utf-8"),
                     STATE_FILE, mode=dropbox.files.WriteMode.overwrite)


def subir_apkg(dbx, ruta):
    with open(ruta, "rb") as fh:
        dbx.files_upload(fh.read(), f"{DROPBOX_OUTPUT_DIR}/{APKG_NAME}",
                         mode=dropbox.files.WriteMode.overwrite)


# --------------------------------------------------------------------------- #
# Construcción del mazo
# --------------------------------------------------------------------------- #
def construir_apkg(estado, ruta):
    model_id = int(hashlib.md5(b"fadp-retrieval-model").hexdigest()[:8], 16)
    model = genanki.Model(
        model_id, "FADP Recuperacion",
        fields=[{"name": "Pregunta"}, {"name": "Respuesta"}, {"name": "Fuente"}],
        templates=[{"name": "Card 1", "qfmt": "{{Pregunta}}",
                    "afmt": '{{FrontSide}}<hr id=answer>{{Respuesta}}'
                            '<br><br><small style="color:#888">{{Fuente}}</small>'}])

    class GuidNote(genanki.Note):
        @property
        def guid(self):
            return genanki.guid_for(self.fields[0])

    decks, seen = {}, set()
    for c in estado["cards"]:
        if c["q"] in seen:
            continue
        seen.add(c["q"])
        name = f"{DECK_NAME}::{c['curso']}::{c['sesion']}::Clase"
        if name not in decks:
            decks[name] = genanki.Deck(int(hashlib.md5(name.encode()).hexdigest()[:8], 16), name)
        decks[name].add_note(GuidNote(model=model, fields=[c["q"], c["a"], c["fuente"]],
                                      tags=[sanitize(c["curso"]), sanitize(c["sesion"]), "clase"]))
    genanki.Package(list(decks.values())).write_to_file(ruta)
    return len(seen), len(decks)


# --------------------------------------------------------------------------- #
# Escaneo de novedades
# --------------------------------------------------------------------------- #
def _escanear(dbx, procesados, work_dir, log):
    res = dbx.files_list_folder(DROPBOX_BASE, recursive=True)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)

    nuevos, sin_marca = [], []
    for e in entries:
        if not (isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".pdf")):
            continue
        curso, sesion = parse_curso_sesion(e.path_display, DROPBOX_BASE)
        if SESSION_FILTER and normaliza(sesion) != normaliza(SESSION_FILTER):
            continue
        if es_excluido(e.name):
            continue
        tipo, sim = tipo_por_marca(e.name)
        if tipo == "lectura":
            continue
        if tipo == "auto":
            sin_marca.append(f"{curso}/{sesion}/{e.name}")
            continue
        if sim < 1.0:
            log(f"  '{e.name}' parece CLASE (similitud {sim:.0%}); se procesa como CLASE_.")
        if e.content_hash in procesados:
            continue
        local = os.path.join(work_dir, f"{sanitize(curso)}__{sanitize(sesion)}__{e.name}")
        dbx.files_download_to_file(local, e.path_lower)
        nuevos.append({"local": local, "name": e.name, "curso": curso,
                       "sesion": sesion, "hash": e.content_hash})
    return nuevos, sin_marca


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #
def run_pipeline(log=lambda m: None, work_dir=WORKDIR):
    os.makedirs(work_dir, exist_ok=True)
    dbx = get_dbx()
    cuenta = dbx.users_get_current_account().name.display_name
    log(f"Conectado a Dropbox como {cuenta}.")

    estado = cargar_estado(dbx)
    procesados = set(estado["procesados"])
    log(f"Estado actual: {len(estado['cards'])} cartas, {len(procesados)} archivos procesados.")

    log("Escaneando Dropbox en busca de clases nuevas...")
    nuevos, sin_marca = _escanear(dbx, procesados, work_dir, log)

    if sin_marca:
        log(f"{len(sin_marca)} archivo(s) SIN marca CLASE_/LECTURA_ (ignorados; márcalos si son clase):")
        for s in sin_marca:
            log(f"   - {s}")

    if not nuevos:
        log("No hay clases nuevas. El mazo ya está al día.")
    else:
        log(f"{len(nuevos)} clase(s) nueva(s) por procesar.")
        for f in nuevos:
            log(f"Procesando {f['curso']}/{f['sesion']}/{f['name']} ...")
            antes = len(estado["cards"])
            for ch in chunk(pdf_to_text(f["local"])):
                for c in cards_from_chunk(ch, log):
                    if c.get("q") and c.get("a"):
                        estado["cards"].append({"q": c["q"].strip(), "a": c["a"].strip(),
                                                "curso": f["curso"], "sesion": f["sesion"],
                                                "fuente": f["name"]})
                time.sleep(1.0)
            procesados.add(f["hash"])
            estado["procesados"] = sorted(procesados)
            guardar_estado(dbx, estado)          # checkpoint tras cada archivo
            log(f"   +{len(estado['cards']) - antes} cartas (guardado en Dropbox)")

    ruta = os.path.join(work_dir, APKG_NAME)
    n_cards, n_decks = construir_apkg(estado, ruta)
    subir_apkg(dbx, ruta)
    log(f"Mazo reconstruido y subido: {n_cards} cartas en {n_decks} submazos.")

    return {"cards": n_cards, "decks": n_decks,
            "archivos": len(procesados), "nuevos": len(nuevos),
            "actualizado": estado["actualizado"]}


def resumen_actual():
    """Estado de solo lectura para mostrar en la página (no procesa nada)."""
    try:
        dbx = get_dbx()
        cuenta = dbx.users_get_current_account().name.display_name
        estado = cargar_estado(dbx)
        por_curso = {}
        for c in estado["cards"]:
            por_curso[c["curso"]] = por_curso.get(c["curso"], 0) + 1
        por_curso = sorted(por_curso.items(), key=lambda x: -x[1])
        return {"conectado": True, "cuenta": cuenta,
                "cards": len(estado["cards"]), "archivos": len(estado["procesados"]),
                "actualizado": estado.get("actualizado"), "por_curso": por_curso}
    except Exception as e:
        return {"conectado": False, "error": str(e),
                "cards": 0, "archivos": 0, "actualizado": None, "por_curso": []}


def ruta_apkg_local():
    return os.path.join(WORKDIR, APKG_NAME)


def reconstruir_local():
    """Reconstruye el .apkg en disco local desde el estado de Dropbox (para descargar)."""
    os.makedirs(WORKDIR, exist_ok=True)
    dbx = get_dbx()
    estado = cargar_estado(dbx)
    ruta = ruta_apkg_local()
    construir_apkg(estado, ruta)
    return ruta
