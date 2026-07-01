"""
App web FADP -> Anki.  Render: un solo worker.
    gunicorn app:app --workers 1 --threads 4 --timeout 120
"""
import os
import threading

from flask import Flask, render_template, jsonify, request, send_file, redirect
from werkzeug.utils import secure_filename

import pipeline
import tracker
import lecturas
import libros

app = Flask(__name__)
TMP = "/tmp/fadp_uploads"
os.makedirs(TMP, exist_ok=True)

# ── Estado en memoria ──────────────────────────────────────────────────────
STATUS     = {"running": False, "log": [], "result": None, "error": None}   # mazo clases
LEC_STATUS = {"running": False, "log": [], "result": None, "error": None}   # lecturas
LIB_STATUS = {"running": False, "log": [], "result": None, "error": None}   # libros
LOCK = threading.Lock(); LEC_LOCK = threading.Lock(); LIB_LOCK = threading.Lock()

def _log(m):     STATUS["log"].append(m)
def _lec_log(m): LEC_STATUS["log"].append(m)
def _lib_log(m): LIB_STATUS["log"].append(m)


# ── Pipeline de clases ─────────────────────────────────────────────────────
def _job():
    try:
        STATUS["error"] = None
        STATUS["result"] = pipeline.run_pipeline(log=_log)
    except Exception as e:
        STATUS["error"] = str(e); _log(f"Error: {e}")
    finally:
        STATUS["running"] = False


@app.route("/")
def index():
    return redirect("/tracker")


@app.route("/procesar", methods=["POST"])
def procesar():
    with LOCK:
        if not STATUS["running"]:
            STATUS.update(running=True, log=[], result=None, error=None)
            threading.Thread(target=_job, daemon=True).start()
    return ("", 204)


@app.route("/estado")
def estado():
    return jsonify(STATUS)


@app.route("/descargar")
def descargar():
    ruta = pipeline.reconstruir_local()
    return send_file(ruta, as_attachment=True, download_name="fadp_general.apkg")


# ── Tracker ────────────────────────────────────────────────────────────────
@app.route("/tracker")
def tracker_view():
    try:
        dbx = pipeline.get_dbx()
        data, tree = tracker.sincronizar_con_dropbox(dbx)
        stats = tracker.calcular_stats(data, tree)
        items = tracker.construir_vista(data, tree)
    except Exception as e:
        stats = {"error": str(e), "total": 0, "concluidas": 0, "pendientes": 0,
                 "hoy": 0, "pct": 0, "actividad_7": {}, "rezagados": [],
                 "por_estado": {"pendiente": 0, "en_progreso": 0, "concluido": 0}}
        items = []
    mazo = pipeline.resumen_actual()
    try:
        librs = libros.lista_libros(pipeline.get_dbx())
    except Exception:
        librs = []
    return render_template("tracker.html", stats=stats, items=items, mazo=mazo, libros=librs)


@app.route("/tracker/estado", methods=["POST"])
def tracker_estado():
    d = request.get_json()
    tracker.actualizar_estado(pipeline.get_dbx(), d["curso"], d["sesion"], d["estado"])
    return jsonify({"ok": True})


@app.route("/tracker/prioridad", methods=["POST"])
def tracker_prioridad():
    d = request.get_json()
    tracker.actualizar_prioridad(pipeline.get_dbx(), d["curso"], d["sesion"], d["prioridad"])
    return jsonify({"ok": True})


@app.route("/tracker/guardar", methods=["POST"])
def tracker_guardar():
    d = request.get_json()
    tracker.actualizar_sesion(pipeline.get_dbx(), d["curso"], d["sesion"],
                              estado=d.get("estado"), prioridad=d.get("prioridad"),
                              notas=d.get("notas"))
    return jsonify({"ok": True})


@app.route("/tracker/preview")
def tracker_preview():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "sin path"}), 400
    try:
        imgs = tracker.previsualizar_pdf(pipeline.get_dbx(), path)
        return jsonify({"imgs": imgs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Lecturas (de sesión) ───────────────────────────────────────────────────
def _lec_job_dropbox(path, curso, sesion, nombre, con_citas):
    try:
        LEC_STATUS["error"] = None
        dbx = pipeline.get_dbx()
        n = lecturas.procesar_lectura_dropbox(dbx, path, curso, sesion, nombre, con_citas, _lec_log)
        LEC_STATUS["result"] = {"nuevas": n}
    except Exception as e:
        LEC_STATUS["error"] = str(e); _lec_log(f"Error: {e}")
    finally:
        LEC_STATUS["running"] = False


def _lec_job_local(local, curso, sesion, nombre, con_citas):
    try:
        LEC_STATUS["error"] = None
        dbx = pipeline.get_dbx()
        n = lecturas.procesar_lectura_local(dbx, local, curso, sesion, nombre, con_citas, _lec_log)
        LEC_STATUS["result"] = {"nuevas": n}
    except Exception as e:
        LEC_STATUS["error"] = str(e); _lec_log(f"Error: {e}")
    finally:
        LEC_STATUS["running"] = False


@app.route("/lecturas/procesar", methods=["POST"])
def lecturas_procesar():
    d = request.get_json()
    con_citas = bool(d.get("con_citas"))
    with LEC_LOCK:
        if not LEC_STATUS["running"]:
            LEC_STATUS.update(running=True, log=[], result=None, error=None)
            threading.Thread(target=_lec_job_dropbox,
                             args=(d["path"], d["curso"], d["sesion"], d["nombre"], con_citas),
                             daemon=True).start()
    return ("", 204)


@app.route("/lecturas/subir", methods=["POST"])
def lecturas_subir():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "sin archivo"}), 400
    curso  = request.form.get("curso", "")
    sesion = request.form.get("sesion", "")
    nombre = request.form.get("nombre", "") or os.path.splitext(f.filename)[0]
    con_citas = request.form.get("con_citas") in ("1", "true", "on", "True")
    local = os.path.join(TMP, secure_filename(f.filename) or "subida.pdf")
    f.save(local)
    with LEC_LOCK:
        if not LEC_STATUS["running"]:
            LEC_STATUS.update(running=True, log=[], result=None, error=None)
            threading.Thread(target=_lec_job_local,
                             args=(local, curso, sesion, nombre, con_citas),
                             daemon=True).start()
    return ("", 204)


@app.route("/lecturas/estado")
def lecturas_estado():
    return jsonify(LEC_STATUS)


@app.route("/lecturas/descargar")
def lecturas_descargar():
    ruta = lecturas.reconstruir_lecturas_local(pipeline.get_dbx())
    return send_file(ruta, as_attachment=True, download_name="fadp_lecturas.apkg")


# ── Libros ─────────────────────────────────────────────────────────────────
def _lib_job(nombre, cortes, con_citas):
    try:
        LIB_STATUS["error"] = None
        dbx = pipeline.get_dbx()
        n = libros.procesar_libro(dbx, nombre, cortes, con_citas, _lib_log)
        LIB_STATUS["result"] = {"nuevas": n}
    except Exception as e:
        LIB_STATUS["error"] = str(e); _lib_log(f"Error: {e}")
    finally:
        LIB_STATUS["running"] = False


@app.route("/libros/lista")
def libros_lista():
    try:
        return jsonify({"libros": libros.lista_libros(pipeline.get_dbx())})
    except Exception as e:
        return jsonify({"libros": [], "error": str(e)})


@app.route("/libros/crear", methods=["POST"])
def libros_crear():
    f = request.files.get("file")
    nombre = request.form.get("nombre", "").strip()
    if not f or not nombre:
        return jsonify({"error": "faltan archivo o nombre"}), 400
    local = os.path.join(TMP, secure_filename(f.filename) or "libro.pdf")
    f.save(local)
    try:
        libros.crear_libro(pipeline.get_dbx(), nombre, local)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/libros/procesar", methods=["POST"])
def libros_procesar():
    d = request.get_json()
    con_citas = bool(d.get("con_citas", True))
    with LIB_LOCK:
        if not LIB_STATUS["running"]:
            LIB_STATUS.update(running=True, log=[], result=None, error=None)
            threading.Thread(target=_lib_job,
                             args=(d["nombre"], d["cortes"], con_citas),
                             daemon=True).start()
    return ("", 204)


@app.route("/libros/estado")
def libros_estado():
    return jsonify(LIB_STATUS)


@app.route("/libros/descargar")
def libros_descargar():
    nombre = request.args.get("nombre", "")
    ruta = libros.reconstruir_local(pipeline.get_dbx(), nombre)
    return send_file(ruta, as_attachment=True, download_name=libros.apkg_nombre(nombre))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
