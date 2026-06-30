"""
Generador de mazo Anki para lecturas de sesión (LECTURA_ de Dropbox o subidas).

Acumula todo en un único fadp_lecturas.apkg en Dropbox.
Las tarjetas de lectura quedan en  FADP::CURSO::SESION::Lectura · NOMBRE
(claramente separadas de las de clase, que viven en ::Clase).
Las citas, si se piden, en         FADP::CURSO::SESION::Citas · NOMBRE
"""
import os
import json
import hashlib

import genanki
import dropbox as dbx_mod

import pipeline
import motor

LECTURAS_APKG   = "fadp_lecturas.apkg"
LECTURAS_STATE  = f"{pipeline.DROPBOX_OUTPUT_DIR}/fadp_lecturas_estado.json"
LECTURAS_APKG_PATH = f"{pipeline.DROPBOX_OUTPUT_DIR}/{LECTURAS_APKG}"


def cargar_estado_lecturas(dbx):
    try:
        _, resp = dbx.files_download(LECTURAS_STATE)
        data = json.loads(resp.content)
    except dbx_mod.exceptions.ApiError:
        data = {}
    data.setdefault("cards", [])       # {q,a,curso,sesion,nombre,tipo,fuente}
    data.setdefault("procesados", {})  # clave -> nombre
    return data


def guardar_estado_lecturas(dbx, data):
    dbx.files_upload(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
                     LECTURAS_STATE, mode=dbx_mod.files.WriteMode.overwrite)


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
        rama = "Citas" if c.get("tipo") == "cita" else "Lectura"
        name = f"FADP::{c['curso']}::{c['sesion']}::{rama} · {c['nombre']}"
        if name not in decks:
            decks[name] = genanki.Deck(
                int(hashlib.md5(name.encode()).hexdigest()[:8], 16), name)
        decks[name].add_note(GuidNote(
            model=model, fields=[c["q"], c["a"], c["fuente"]],
            tags=[c["curso"].replace(" ", "_"), c["sesion"],
                  "cita" if c.get("tipo") == "cita" else "lectura"]))

    genanki.Package(list(decks.values())).write_to_file(ruta)
    return len(seen), len(decks)


def procesar_lectura_local(dbx, local_path, curso, sesion, nombre, con_citas=False, log=print):
    """Genera tarjetas (y citas opcionales) de un PDF en disco y las acumula."""
    n = motor.num_paginas(local_path)
    seg = {"nombre": nombre, "ini": 1, "fin": n}      # una lectura = todo el PDF
    log(f"Procesando lectura '{nombre}' ({n} paginas)...")
    preguntas, citas = motor.procesar_segmento(local_path, seg, con_citas, log)

    data = cargar_estado_lecturas(dbx)
    antes = len(data["cards"])
    for p in preguntas:
        data["cards"].append({**p, "curso": curso, "sesion": sesion,
                              "nombre": nombre, "tipo": "lectura", "fuente": nombre})
    for c in citas:
        data["cards"].append({"q": c["pista"], "a": c["cita"], "curso": curso,
                              "sesion": sesion, "nombre": nombre, "tipo": "cita",
                              "fuente": nombre})
    data["procesados"][f"{curso}/{sesion}/{nombre}"] = nombre
    guardar_estado_lecturas(dbx, data)

    ruta = os.path.join(pipeline.WORKDIR, LECTURAS_APKG)
    nc, nd = construir_apkg_lecturas(data, ruta)
    with open(ruta, "rb") as fh:
        dbx.files_upload(fh.read(), LECTURAS_APKG_PATH, mode=dbx_mod.files.WriteMode.overwrite)

    nuevas = len(data["cards"]) - antes
    log(f"+{nuevas} tarjetas. Mazo de lecturas: {nc} cartas en {nd} submazos.")
    return nuevas


def procesar_lectura_dropbox(dbx, path_dropbox, curso, sesion, nombre, con_citas=False, log=print):
    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    local = os.path.join(pipeline.WORKDIR,
                         f"lectura__{pipeline.sanitize(curso)}__{sesion}__{os.path.basename(path_dropbox)}")
    log(f"Descargando {os.path.basename(path_dropbox)} de Dropbox...")
    dbx.files_download_to_file(local, path_dropbox.lower())
    return procesar_lectura_local(dbx, local, curso, sesion, nombre, con_citas, log)


def ruta_apkg_lecturas_local():
    return os.path.join(pipeline.WORKDIR, LECTURAS_APKG)


def reconstruir_lecturas_local(dbx):
    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    data = cargar_estado_lecturas(dbx)
    ruta = ruta_apkg_lecturas_local()
    construir_apkg_lecturas(data, ruta)
    return ruta
