"""
Tracker de sesiones FADP.

Escanea Dropbox para descubrir todas las sesiones (CLASE_ y LECTURA_) por curso,
y gestiona fadp_tracker.json con el estado manual de cada una (pendiente / concluida)
y el historial de actividad diaria.
"""
import os
import re
import json
import base64
import datetime
import unicodedata
from difflib import SequenceMatcher

import fitz
import dropbox as dbx_mod

import pipeline  # reutiliza get_dbx, normaliza, DROPBOX_BASE, DROPBOX_OUTPUT_DIR, EXCLUDE_KEYWORDS

TRACKER_FILE = f"{pipeline.DROPBOX_OUTPUT_DIR}/fadp_tracker.json"
WORKDIR = pipeline.WORKDIR


# --------------------------------------------------------------------------- #
# Helpers compartidos
# --------------------------------------------------------------------------- #
def _normaliza(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _sanitize(s):
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")


def _tipo_marca(nombre):
    n = _normaliza(nombre)
    if n.startswith("clase_"):   return "clase"
    if n.startswith("lectura_"): return "lectura"
    cands = {n.split("_")[0]}
    m = re.match(r"^([a-z]+)", n)
    if m: cands.add(m.group(1))
    sc = max((SequenceMatcher(None, c, "clase").ratio()   for c in cands), default=0)
    sl = max((SequenceMatcher(None, c, "lectura").ratio() for c in cands), default=0)
    if sc >= 0.70 and sc >= sl: return "clase"
    if sl >= 0.70 and sl > sc:  return "lectura"
    return None


def _es_silabo(nombre):
    n = _normaliza(nombre)
    return any(k in n for k in pipeline.EXCLUDE_KEYWORDS)


# --------------------------------------------------------------------------- #
# Escaneo de Dropbox
# --------------------------------------------------------------------------- #
def escanear_sesiones(dbx):
    """
    Devuelve dict: { "CURSO": { "S1": {"clase": [...paths], "lectura": [...paths]}, ... } }
    """
    res = dbx.files_list_folder(pipeline.DROPBOX_BASE, recursive=True)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)

    tree = {}
    for e in entries:
        if not (isinstance(e, dbx_mod.files.FileMetadata) and e.name.lower().endswith(".pdf")):
            continue
        if _es_silabo(e.name):
            continue
        tipo = _tipo_marca(e.name)
        if tipo is None:
            continue
        parts = e.path_display[len(pipeline.DROPBOX_BASE):].lstrip("/").split("/")
        if len(parts) < 3:
            continue
        curso, sesion = parts[0], parts[1]
        tree.setdefault(curso, {}).setdefault(sesion, {"clase": [], "lectura": []})
        tree[curso][sesion][tipo].append(e.path_display)
    return tree


# --------------------------------------------------------------------------- #
# Estado del tracker (fadp_tracker.json)
# --------------------------------------------------------------------------- #
def cargar_tracker(dbx):
    try:
        _, resp = dbx.files_download(TRACKER_FILE)
        data = json.loads(resp.content)
    except dbx_mod.exceptions.ApiError:
        data = {}
    data.setdefault("sesiones", {})   # "CURSO||SN": {estado, prioridad, notas, historia}
    data.setdefault("actividad", [])  # [{fecha, sesion_id, accion}]
    return data


def guardar_tracker(dbx, data):
    dbx.files_upload(
        json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
        TRACKER_FILE,
        mode=dbx_mod.files.WriteMode.overwrite)


def sesion_id(curso, sesion):
    return f"{curso}||{sesion}"


def _asegurar_sesion(data, curso, sesion):
    sid = sesion_id(curso, sesion)
    if sid not in data["sesiones"]:
        data["sesiones"][sid] = {
            "curso": curso, "sesion": sesion,
            "estado": "pendiente",   # pendiente | en_progreso | concluido
            "prioridad": 50,          # 0-100, mayor = más urgente
            "notas": "",
            "concluido_en": None,
        }
    return sid


def sincronizar_con_dropbox(dbx):
    """
    Carga el tracker, descubre sesiones en Dropbox, agrega las nuevas
    (sin tocar las existentes) y guarda. Devuelve (tracker_data, sesiones_tree).
    """
    tree = escanear_sesiones(dbx)
    data = cargar_tracker(dbx)
    for curso, sesiones in tree.items():
        for sesion in sesiones:
            _asegurar_sesion(data, curso, sesion)
    guardar_tracker(dbx, data)
    return data, tree


# --------------------------------------------------------------------------- #
# Operaciones sobre sesiones
# --------------------------------------------------------------------------- #
def actualizar_estado(dbx, curso, sesion, nuevo_estado):
    data = cargar_tracker(dbx)
    sid = _asegurar_sesion(data, curso, sesion)
    data["sesiones"][sid]["estado"] = nuevo_estado
    if nuevo_estado == "concluido":
        data["sesiones"][sid]["concluido_en"] = datetime.date.today().isoformat()
        data["actividad"].append({
            "fecha": datetime.date.today().isoformat(),
            "sesion_id": sid, "accion": "concluido"})
    elif nuevo_estado == "pendiente":
        data["sesiones"][sid]["concluido_en"] = None
        data["actividad"].append({
            "fecha": datetime.date.today().isoformat(),
            "sesion_id": sid, "accion": "revertido"})
    guardar_tracker(dbx, data)
    return data


def actualizar_prioridad(dbx, curso, sesion, prioridad):
    data = cargar_tracker(dbx)
    sid = _asegurar_sesion(data, curso, sesion)
    data["sesiones"][sid]["prioridad"] = int(prioridad)
    guardar_tracker(dbx, data)
    return data


# --------------------------------------------------------------------------- #
# Dashboard stats
# --------------------------------------------------------------------------- #
def calcular_stats(data, tree):
    total = len(data["sesiones"])
    concluidas = sum(1 for s in data["sesiones"].values() if s["estado"] == "concluido")
    pendientes = total - concluidas

    # sesiones concluidas hoy
    hoy = datetime.date.today().isoformat()
    hoy_count = sum(1 for a in data["actividad"]
                    if a["fecha"] == hoy and a["accion"] == "concluido")

    # actividad últimos 7 días
    actividad_7 = {}
    for i in range(7):
        d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        actividad_7[d] = sum(1 for a in data["actividad"]
                             if a["fecha"] == d and a["accion"] == "concluido")

    # cursos con más sesiones pendientes (top 5)
    curso_stats = {}
    for sid, s in data["sesiones"].items():
        c = s["curso"]
        curso_stats.setdefault(c, {"total": 0, "concluidas": 0})
        curso_stats[c]["total"] += 1
        if s["estado"] == "concluido":
            curso_stats[c]["concluidas"] += 1
    rezagados = sorted(
        [(c, v["total"] - v["concluidas"], v["total"], v["concluidas"])
         for c, v in curso_stats.items()],
        key=lambda x: -x[1])[:6]

    return {
        "total": total, "concluidas": concluidas, "pendientes": pendientes,
        "hoy": hoy_count, "actividad_7": actividad_7, "rezagados": rezagados,
        "pct": round(concluidas / total * 100) if total else 0,
    }


# --------------------------------------------------------------------------- #
# Previsualización PDF (primeras 2 páginas como imágenes base64)
# --------------------------------------------------------------------------- #
def previsualizar_pdf(dbx, path_dropbox, n_pages=2):
    """Descarga el PDF y devuelve lista de imágenes PNG en base64."""
    os.makedirs(WORKDIR, exist_ok=True)
    local = os.path.join(WORKDIR, "preview_" + _sanitize(os.path.basename(path_dropbox)) + ".pdf")
    dbx.files_download_to_file(local, path_dropbox.lower())
    doc = fitz.open(local)
    imgs = []
    for i in range(min(n_pages, len(doc))):
        pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
        imgs.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs


# --------------------------------------------------------------------------- #
# Vista estructurada para el frontend
# --------------------------------------------------------------------------- #
def construir_vista(data, tree):
    """
    Devuelve lista de sesiones enriquecidas para el frontend,
    ordenadas por prioridad desc dentro de cada estado.
    """
    items = []
    for sid, s in data["sesiones"].items():
        curso, sesion = s["curso"], s["sesion"]
        archivos = tree.get(curso, {}).get(sesion, {"clase": [], "lectura": []})
        items.append({
            "id": sid,
            "curso": curso,
            "sesion": sesion,
            "label": f"{curso} · {sesion}",
            "estado": s["estado"],
            "prioridad": s["prioridad"],
            "notas": s.get("notas", ""),
            "concluido_en": s.get("concluido_en"),
            "n_clase": len(archivos["clase"]),
            "n_lectura": len(archivos["lectura"]),
            "archivos_clase": archivos["clase"],
            "archivos_lectura": archivos["lectura"],
        })
    items.sort(key=lambda x: (-x["prioridad"], x["curso"], x["sesion"]))
    return items
