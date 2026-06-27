"""
App web FADP → Anki.

Una página con el estado del mazo y un botón "Procesar actualizaciones" que corre
el pipeline incremental en segundo plano. El progreso se muestra en vivo.

Render: una sola instancia / un worker (el estado vive en memoria del proceso).
    gunicorn app:app --workers 1 --threads 4 --timeout 120
"""
import os
import threading

from flask import Flask, render_template, jsonify, send_file

import pipeline

app = Flask(__name__)

# Estado en memoria de la corrida en curso
STATUS = {"running": False, "log": [], "result": None, "error": None}
LOCK = threading.Lock()


def _log(msg):
    STATUS["log"].append(msg)


def _job():
    try:
        STATUS["error"] = None
        STATUS["result"] = pipeline.run_pipeline(log=_log)
    except Exception as e:           # noqa: BLE001
        STATUS["error"] = str(e)
        _log(f"Error: {e}")
    finally:
        STATUS["running"] = False


@app.route("/")
def index():
    resumen = pipeline.resumen_actual()
    return render_template("index.html", r=resumen, running=STATUS["running"])


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
