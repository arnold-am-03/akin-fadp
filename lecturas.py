"""
Generador de mazo Anki para lecturas (LECTURA_).

Genera tarjetas desde un PDF de lectura específico y las acumula en
fadp_lecturas.apkg en Dropbox (un solo archivo para todas las lecturas).
La estructura del mazo es FADP::CURSO::SESION::NOMBRE_LECTURA.
"""
import os
import json
import time
import hashlib

import fitz
import genanki
import dropbox as dbx_mod

import pipeline

LECTURAS_APKG   = "fadp_lecturas.apkg"
LECTURAS_STATE  = f"{pipeline.DROPBOX_OUTPUT_DIR}/fadp_lecturas_estado.json"
LECTURAS_APKG_PATH = f"{pipeline.DROPBOX_OUTPUT_DIR}/{LECTURAS_APKG}"


# --------------------------------------------------------------------------- #
# Estado de lecturas procesadas
# --------------------------------------------------------------------------- #
def cargar_estado_lecturas(dbx):
    try:
        _, resp = dbx.files_download(LECTURAS_STATE)
        data = json.loads(resp.content)
    except dbx_mod.exceptions.ApiError:
        data = {}
    data.setdefault("cards", [])
    data.setdefault("procesados", {})  # path_dropbox -> nombre_elegido
    return data


def guardar_estado_lecturas(dbx, data):
    dbx.files_upload(
        json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
        LECTURAS_STATE,
        mode=dbx_mod.files.WriteMode.overwrite)


# --------------------------------------------------------------------------- #
# Construcción del .apkg de lecturas
# --------------------------------------------------------------------------- #
def construir_apkg_lecturas(data, ruta):
    model_id = int(hashlib.md5(b"fadp-lecturas-model").hexdigest()[:8], 16)
    model = genanki.Model(
        model_id, "FADP Lecturas",
        fields=[{"name": "Pregunta"}, {"name": "Respuesta"}, {"name": "Fuente"}],
        templates=[{"name": "Card 1", "qfmt": "{{Pregunta}}",
                    "afmt": '{{FrontSide}}<hr id=answer>{{Respuesta}}'
                            '<br><br><small style="color:#888">{{Fuente}}</small>'}])

    class GuidNote(genanki.Note):
        @property
        def guid(self):
            return genanki.guid_for(self.fields[0])

    decks, seen = {}, set()
    for c in data["cards"]:
        if c["q"] in seen:
            continue
        seen.add(c["q"])
        name = f"FADP::{c['curso']}::{c['sesion']}::{c['nombre']}"
        if name not in decks:
            decks[name] = genanki.Deck(
                int(hashlib.md5(name.encode()).hexdigest()[:8], 16), name)
        decks[name].add_note(GuidNote(
            model=model,
            fields=[c["q"], c["a"], c["fuente"]],
            tags=[c["curso"].replace(" ", "_"), c["sesion"]]))

    genanki.Package(list(decks.values())).write_to_file(ruta)
    return len(seen), len(decks)


# --------------------------------------------------------------------------- #
# Procesamiento de una lectura
# --------------------------------------------------------------------------- #
def procesar_lectura(dbx, path_dropbox, curso, sesion, nombre, log=print):
    """
    Descarga el PDF, genera tarjetas con Gemini y las acumula en el estado.
    Reconstruye y sube fadp_lecturas.apkg.
    Devuelve número de tarjetas nuevas.
    """
    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    local = os.path.join(pipeline.WORKDIR,
                         f"lectura__{curso}__{sesion}__{os.path.basename(path_dropbox)}")
    log(f"Descargando {os.path.basename(path_dropbox)}...")
    dbx.files_download_to_file(local, path_dropbox.lower())

    # Extraer y trocear
    doc = fitz.open(local)
    texto = "\n".join(p.get_text() for p in doc)
    doc.close()
    bloques = pipeline.chunk(texto)
    log(f"{len(bloques)} bloques de texto extraídos.")

    # Cargar estado existente
    data = cargar_estado_lecturas(dbx)

    antes = len(data["cards"])
    for i, bloque in enumerate(bloques, 1):
        log(f"Bloque {i}/{len(bloques)}...")
        for c in pipeline.cards_from_chunk(bloque, log):
            if c.get("q") and c.get("a"):
                data["cards"].append({
                    "q": c["q"].strip(), "a": c["a"].strip(),
                    "curso": curso, "sesion": sesion,
                    "nombre": nombre,
                    "fuente": os.path.basename(path_dropbox),
                })
        time.sleep(1.0)

    data["procesados"][path_dropbox] = nombre
    guardar_estado_lecturas(dbx, data)

    # Reconstruir y subir .apkg
    ruta = os.path.join(pipeline.WORKDIR, LECTURAS_APKG)
    n_cards, n_decks = construir_apkg_lecturas(data, ruta)
    with open(ruta, "rb") as fh:
        dbx.files_upload(fh.read(), LECTURAS_APKG_PATH,
                         mode=dbx_mod.files.WriteMode.overwrite)

    nuevas = len(data["cards"]) - antes
    log(f"+{nuevas} tarjetas. Mazo de lecturas: {n_cards} cartas en {n_decks} submazos.")
    return nuevas


def ruta_apkg_lecturas_local():
    return os.path.join(pipeline.WORKDIR, LECTURAS_APKG)


def reconstruir_lecturas_local(dbx):
    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    data = cargar_estado_lecturas(dbx)
    ruta = ruta_apkg_lecturas_local()
    construir_apkg_lecturas(data, ruta)
    return ruta
