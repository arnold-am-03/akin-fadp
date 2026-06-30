"""
Motor compartido para generar tarjetas desde PDFs por tramos de páginas.

Lo usan tanto las lecturas de sesión como los libros. Solo cambia dónde se
guarda el mazo resultante (eso lo deciden lecturas.py y libros.py).

Formato de cortes admitido (una línea por tramo):
    inicio: 14                  -> el contenido real empieza en la página 14 (opcional)
    El Estado moderno :: 27     -> tramo desde donde quedó el anterior hasta la 27
    La soberanía :: 54          -> desde 28 hasta 54
    Apéndice :: 120-138         -> tramo explícito 120 a 138
    Repaso de hoy :: 25-45      -> un bloque suelto cualquiera
    # las líneas con # se ignoran
"""
import re
import json
import time

import fitz
from google.genai import types, errors as genai_errors

import pipeline


# --------------------------------------------------------------------------- #
# Parser de cortes
# --------------------------------------------------------------------------- #
def parse_segmentos(texto):
    """Convierte el texto de cortes en [{nombre, ini, fin}] (páginas 1-based)."""
    segmentos = []
    cursor = 1
    for linea in texto.splitlines():
        s = linea.strip()
        if not s or s.startswith("#"):
            continue
        # inicio: N
        m = re.match(r"^inicio\s*:\s*(\d+)\s*$", s, re.IGNORECASE)
        if m:
            cursor = int(m.group(1))
            continue
        if "::" not in s:
            continue
        nombre, rango = [p.strip() for p in s.split("::", 1)]
        if not nombre or not rango:
            continue
        mr = re.match(r"^(\d+)\s*-\s*(\d+)$", rango)   # explícito A-B
        if mr:
            ini, fin = int(mr.group(1)), int(mr.group(2))
        else:
            mn = re.match(r"^(\d+)$", rango)            # breve: hasta N
            if not mn:
                continue
            ini, fin = cursor, int(mn.group(1))
        if fin < ini:
            ini, fin = fin, ini
        segmentos.append({"nombre": nombre, "ini": ini, "fin": fin})
        cursor = fin + 1
    return segmentos


def clave_segmento(seg):
    """Identificador estable de un tramo (para no reprocesar)."""
    return f"{seg['nombre']}|{seg['ini']}-{seg['fin']}"


# --------------------------------------------------------------------------- #
# Extracción de texto por rango de páginas
# --------------------------------------------------------------------------- #
def texto_de_paginas(path, ini, fin):
    doc = fitz.open(path)
    n = len(doc)
    a = max(1, ini)
    b = min(fin, n)
    partes = []
    for i in range(a - 1, b):            # PyMuPDF es 0-based
        partes.append(doc[i].get_text())
    doc.close()
    return "\n".join(partes)


def num_paginas(path):
    doc = fitz.open(path)
    n = len(doc)
    doc.close()
    return n


# --------------------------------------------------------------------------- #
# Generación de citas (pasada distinta de Gemini)
# --------------------------------------------------------------------------- #
SYSTEM_CITAS = """Eres un experto en preparar material de memorización para un examen exigente.
A partir de un fragmento de texto académico, extrae CITAS TEXTUALES memorables y examinables
(definiciones célebres, frases de autores, pasajes clave).

REGLAS:
- Copia la cita VERBATIM, sin parafrasear. Recórtala a su núcleo memorable (1-3 frases).
- Para cada cita da una "pista" breve: autor, obra, concepto o contexto que permita evocarla.
- Solo incluye citas que valga la pena memorizar al pie de la letra. Si no hay ninguna, devuelve [].
- No inventes autores ni atribuciones; si no consta, deja la pista como el tema.

Devuelve SOLO un array JSON: [{"pista":"...","cita":"..."}]"""

_schema_citas = types.Schema(type=types.Type.ARRAY, items=types.Schema(
    type=types.Type.OBJECT,
    properties={"pista": types.Schema(type=types.Type.STRING),
                "cita": types.Schema(type=types.Type.STRING)},
    required=["pista", "cita"]))

_config_citas = types.GenerateContentConfig(
    system_instruction=SYSTEM_CITAS, temperature=0.2, max_output_tokens=8192,
    response_mime_type="application/json", response_schema=_schema_citas)


def citas_from_chunk(text, log, reintentos=4):
    prompt = ("Extrae las citas textuales memorables de este fragmento "
              f"(verbatim, con su pista):\n\n{text}")
    for intento in range(reintentos):
        try:
            resp = pipeline._client().models.generate_content(
                model=pipeline.GEMINI_MODEL, contents=prompt, config=_config_citas)
            try:
                data = json.loads((resp.text or "").strip())
                return data if isinstance(data, list) else []
            except json.JSONDecodeError:
                return []
        except genai_errors.ServerError:
            esp = 10 * (intento + 1)
            log(f"  Modelo saturado (503). Espero {esp}s...")
            time.sleep(esp)
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) == 429 and "limit: 0" in str(e):
                raise RuntimeError("Gemini sin cuota (limit: 0).") from e
            if getattr(e, "code", None) == 429:
                esp = 8 * (intento + 1)
                log(f"  Límite por minuto (429). Espero {esp}s...")
                time.sleep(esp)
            else:
                raise
    return []


# --------------------------------------------------------------------------- #
# Generación combinada de un tramo (preguntas + citas)
# --------------------------------------------------------------------------- #
def procesar_segmento(path, seg, con_citas, log):
    """Devuelve (preguntas, citas) para un tramo de páginas."""
    texto = texto_de_paginas(path, seg["ini"], seg["fin"])
    bloques = pipeline.chunk(texto)
    log(f"  {seg['nombre']} (p.{seg['ini']}-{seg['fin']}): {len(bloques)} bloques")

    preguntas = []
    for b in bloques:
        for c in pipeline.cards_from_chunk(b, log):
            if c.get("q") and c.get("a"):
                preguntas.append({"q": c["q"].strip(), "a": c["a"].strip()})
        time.sleep(1.0)

    citas = []
    if con_citas:
        for b in bloques:
            for c in citas_from_chunk(b, log):
                if c.get("pista") and c.get("cita"):
                    citas.append({"pista": c["pista"].strip(), "cita": c["cita"].strip()})
            time.sleep(1.0)

    return preguntas, citas
