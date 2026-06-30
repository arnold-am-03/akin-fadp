"""
Libros -> mazos de Anki, por capítulos, con el mismo motor.

Modelo acumulativo: cada libro guarda su PDF en Dropbox (/FADP/anki/LIBROS/), su
lista de tramos ya procesados, sus tarjetas y la última solicitud de cortes.
Al procesar, solo se generan los tramos NUEVOS (por nombre+rango).

Salida: un .apkg por libro con
    NombreLibro::Capitulo            (preguntas)
    NombreLibro - Citas::Capitulo    (citas, si se piden)
"""
import os
import json
import hashlib

import genanki
import dropbox as dbx_mod

import pipeline
import motor

LIBROS_DIR   = f"{pipeline.DROPBOX_OUTPUT_DIR}/LIBROS"
LIBROS_STATE = f"{pipeline.DROPBOX_OUTPUT_DIR}/fadp_libros_estado.json"


def cargar(dbx):
    try:
        _, resp = dbx.files_download(LIBROS_STATE)
        data = json.loads(resp.content)
    except dbx_mod.exceptions.ApiError:
        data = {}
    data.setdefault("libros", {})   # nombre -> {pdf, cards[], procesados[], ultima_solicitud}
    return data


def guardar(dbx, data):
    dbx.files_upload(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
                     LIBROS_STATE, mode=dbx_mod.files.WriteMode.overwrite)


def _slug(nombre):
    return pipeline.sanitize(nombre)[:60] or "libro"


def crear_libro(dbx, nombre, local_pdf):
    """Sube el PDF del libro a Dropbox y registra el libro."""
    data = cargar(dbx)
    pdf_path = f"{LIBROS_DIR}/{_slug(nombre)}.pdf"
    with open(local_pdf, "rb") as fh:
        dbx.files_upload(fh.read(), pdf_path, mode=dbx_mod.files.WriteMode.overwrite)
    if nombre not in data["libros"]:
        data["libros"][nombre] = {"pdf": pdf_path, "cards": [],
                                  "procesados": [], "ultima_solicitud": ""}
    else:
        data["libros"][nombre]["pdf"] = pdf_path
    guardar(dbx, data)
    return data["libros"][nombre]


def lista_libros(dbx):
    data = cargar(dbx)
    out = []
    for nombre, lb in data["libros"].items():
        out.append({"nombre": nombre, "tarjetas": len(lb["cards"]),
                    "tramos": len(lb["procesados"]),
                    "ultima_solicitud": lb.get("ultima_solicitud", "")})
    out.sort(key=lambda x: x["nombre"])
    return out


def apkg_nombre(nombre):
    return f"fadp_libro_{_slug(nombre)}.apkg"


def construir_apkg_libro(libro, nombre, ruta):
    model_id = int(hashlib.md5(b"fadp-libro-model").hexdigest()[:8], 16)
    model = genanki.Model(
        model_id, "FADP Libro",
        fields=[{"name": "Pregunta"}, {"name": "Respuesta"}, {"name": "Fuente"}],
        templates=[{"name": "Card 1", "qfmt": "{{Pregunta}}",
                    "afmt": '{{FrontSide}}<hr id=answer>{{Respuesta}}'
                            '<br><br><small style="color:#888">{{Fuente}}</small>'}])

    class GuidNote(genanki.Note):
        @property
        def guid(self):
            return genanki.guid_for(self.fields[0])

    decks, seen = {}, set()
    for c in libro["cards"]:
        if c["q"] in seen:
            continue
        seen.add(c["q"])
        if c.get("tipo") == "cita":
            name = f"{nombre} - Citas::{c['segmento']}"
        else:
            name = f"{nombre}::{c['segmento']}"
        if name not in decks:
            decks[name] = genanki.Deck(
                int(hashlib.md5(name.encode()).hexdigest()[:8], 16), name)
        decks[name].add_note(GuidNote(
            model=model, fields=[c["q"], c["a"], c["segmento"]],
            tags=[_slug(nombre), pipeline.sanitize(c["segmento"]),
                  "cita" if c.get("tipo") == "cita" else "pregunta"]))

    genanki.Package(list(decks.values())).write_to_file(ruta)
    return len(seen), len(decks)


def procesar_libro(dbx, nombre, texto_cortes, con_citas=True, log=print):
    """Procesa SOLO los tramos nuevos del libro y actualiza su .apkg en Dropbox."""
    data = cargar(dbx)
    if nombre not in data["libros"]:
        raise RuntimeError(f"El libro '{nombre}' no existe. Subelo primero.")
    libro = data["libros"][nombre]
    libro["ultima_solicitud"] = texto_cortes

    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    local_pdf = os.path.join(pipeline.WORKDIR, f"libro_{_slug(nombre)}.pdf")
    if not os.path.exists(local_pdf):
        log(f"Descargando PDF del libro '{nombre}'...")
        dbx.files_download_to_file(local_pdf, libro["pdf"].lower())

    segmentos = motor.parse_segmentos(texto_cortes)
    if not segmentos:
        log("No se reconocio ningun tramo en la solicitud de cortes.")
        guardar(dbx, data)
        return 0

    ya = set(libro["procesados"])
    nuevos = [s for s in segmentos if motor.clave_segmento(s) not in ya]
    log(f"{len(segmentos)} tramos en la solicitud - {len(nuevos)} nuevos por procesar.")

    total = 0
    for seg in nuevos:
        preguntas, citas = motor.procesar_segmento(local_pdf, seg, con_citas, log)
        for p in preguntas:
            libro["cards"].append({**p, "segmento": seg["nombre"], "tipo": "pregunta"})
        for c in citas:
            libro["cards"].append({"q": c["pista"], "a": c["cita"],
                                   "segmento": seg["nombre"], "tipo": "cita"})
        libro["procesados"].append(motor.clave_segmento(seg))
        total += len(preguntas) + len(citas)
        guardar(dbx, data)                # checkpoint tras cada tramo
        log(f"  '{seg['nombre']}': +{len(preguntas)} preguntas, +{len(citas)} citas (guardado)")

    ruta = os.path.join(pipeline.WORKDIR, apkg_nombre(nombre))
    nc, nd = construir_apkg_libro(libro, nombre, ruta)
    with open(ruta, "rb") as fh:
        dbx.files_upload(fh.read(), f"{pipeline.DROPBOX_OUTPUT_DIR}/{apkg_nombre(nombre)}",
                         mode=dbx_mod.files.WriteMode.overwrite)
    log(f"Mazo del libro: {nc} cartas en {nd} submazos.")
    return total


def ruta_apkg_local(nombre):
    return os.path.join(pipeline.WORKDIR, apkg_nombre(nombre))


def reconstruir_local(dbx, nombre):
    os.makedirs(pipeline.WORKDIR, exist_ok=True)
    data = cargar(dbx)
    libro = data["libros"][nombre]
    ruta = ruta_apkg_local(nombre)
    construir_apkg_libro(libro, nombre, ruta)
    return ruta
