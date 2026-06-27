"""
App web FADP → Anki.

Render: una sola instancia / un worker.
    gunicorn app:app --workers 1 --threads 4 --timeout 120
"""
import os
import threading

from flask import Flask, render_template, jsonify, request, send_file, redirect

import pipeline
import tracker
import lecturas

app = Flask(__name__)

# ── Estado en memoria del pipeline de clases ──────────────────────────────── #
STATUS = {"running": False, "log": [], "result": None, "error": None}
LOCK   = threading.Lock()

# ── Estado en memoria del generador de lecturas ───────────────────────────── #
LEC_STATUS = {"running": False, "log": [], "result": None, "error": None}
LEC_LOCK   = threading.Lock()


def _log(msg):      STATUS["log"].append(msg)
def _lec_log(msg):  LEC_STATUS["log"].append(msg)


def _job():
    try:
        STATUS["error"] = None
        STATUS["result"] = pipeline.run_pipeline(log=_log)
    except Exception as e:
        STATUS["error"] = str(e); _log(f"Error: {e}")
    finally:
        STATUS["running"] = False


def _lec_job(path_dropbox, curso, sesion, nombre):
    try:
        LEC_STATUS["error"] = None
        dbx = pipeline.get_dbx()
        nuevas = lecturas.procesar_lectura(
            dbx, path_dropbox, curso, sesion, nombre, log=_lec_log)
        LEC_STATUS["result"] = {"nuevas": nuevas}
    except Exception as e:
        LEC_STATUS["error"] = str(e); _lec_log(f"Error: {e}")
    finally:
        LEC_STATUS["running"] = False


# ══════════════════════════════════════════════════════════════════════════════
# Rutas existentes (sin cambios)
# ══════════════════════════════════════════════════════════════════════════════
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
    ruta = pipeline.ruta_apkg_local()
    if not os.path.exists(ruta):
        ruta = pipeline.reconstruir_local()
    return send_file(ruta, as_attachment=True, download_name="fadp_general.apkg")


# ══════════════════════════════════════════════════════════════════════════════
# Tracker de sesiones
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/tracker")
def tracker_view():
    try:
        dbx = pipeline.get_dbx()
        data, tree = tracker.sincronizar_con_dropbox(dbx)
        stats = tracker.calcular_stats(data, tree)
        items = tracker.construir_vista(data, tree)
    except Exception as e:
        data, tree, stats, items = {}, {}, {}, []
        stats = {"error": str(e), "total": 0, "concluidas": 0,
                 "pendientes": 0, "hoy": 0, "pct": 0,
                 "actividad_7": {}, "rezagados": [],
                 "por_estado": {"pendiente": 0, "en_progreso": 0, "concluido": 0}}
    mazo = pipeline.resumen_actual()
    return render_template("tracker.html", stats=stats, items=items, mazo=mazo)


@app.route("/tracker/estado", methods=["POST"])
def tracker_estado():
    d = request.get_json()
    dbx = pipeline.get_dbx()
    tracker.actualizar_estado(dbx, d["curso"], d["sesion"], d["estado"])
    return jsonify({"ok": True})


@app.route("/tracker/prioridad", methods=["POST"])
def tracker_prioridad():
    d = request.get_json()
    dbx = pipeline.get_dbx()
    tracker.actualizar_prioridad(dbx, d["curso"], d["sesion"], d["prioridad"])
    return jsonify({"ok": True})


@app.route("/tracker/guardar", methods=["POST"])
def tracker_guardar():
    d = request.get_json()
    dbx = pipeline.get_dbx()
    tracker.actualizar_sesion(dbx, d["curso"], d["sesion"],
                              estado=d.get("estado"),
                              prioridad=d.get("prioridad"),
                              notas=d.get("notas"))
    return jsonify({"ok": True})


@app.route("/tracker/preview")
def tracker_preview():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "sin path"}), 400
    try:
        dbx = pipeline.get_dbx()
        imgs = tracker.previsualizar_pdf(dbx, path)
        return jsonify({"imgs": imgs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# Generador de lecturas
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/lecturas/procesar", methods=["POST"])
def lecturas_procesar():
    d = request.get_json()
    with LEC_LOCK:
        if not LEC_STATUS["running"]:
            LEC_STATUS.update(running=True, log=[], result=None, error=None)
            threading.Thread(
                target=_lec_job,
                args=(d["path"], d["curso"], d["sesion"], d["nombre"]),
                daemon=True).start()
    return ("", 204)


@app.route("/lecturas/estado")
def lecturas_estado():
    return jsonify(LEC_STATUS)


@app.route("/lecturas/descargar")
def lecturas_descargar():
    ruta = lecturas.ruta_apkg_lecturas_local()
    if not os.path.exists(ruta):
        dbx = pipeline.get_dbx()
        ruta = lecturas.reconstruir_lecturas_local(dbx)
    return send_file(ruta, as_attachment=True, download_name="fadp_lecturas.apkg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
