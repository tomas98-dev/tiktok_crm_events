"""Pipeline BigQuery -> TikTok CRM Events API.

Modos de ejecucion:
  1. CLI (one-shot):      python main.py --config config.yaml
  2. Servicio Cloud Run:  gunicorn main:app   (lo invoca Cloud Scheduler)

En ambos modos ejecuta el mismo run_pipeline().
"""

from __future__ import annotations

import argparse
import logging
import os

from config_loader import load_config, resolve_access_token
import bq_client
import tiktok_client

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tiktok_crm")


def run_pipeline(config_path: str | None = None) -> dict:
    """Ejecuta el pipeline completo y devuelve un resumen."""
    config = load_config(config_path)
    access_token = resolve_access_token(config)

    client = bq_client.make_client(config)
    bq_client.ensure_control_table(client, config)

    pending = bq_client.fetch_pending_events(client, config)
    if not pending:
        logger.info("No hay eventos pendientes por enviar.")
        return {"pendientes": 0, "enviados": 0, "errores": 0}

    results = tiktok_client.post_events(pending, config, access_token)
    bq_client.record_results(client, config, results)

    enviados = sum(1 for r in results if r["status"] == "success")
    errores = sum(1 for r in results if r["status"] == "error")
    summary = {"pendientes": len(pending), "enviados": enviados, "errores": errores}
    logger.info("Resumen: %s", summary)
    return summary


# --------------------------------------------------------------------------- #
# Modo servicio (Cloud Run) — Flask
# --------------------------------------------------------------------------- #
try:
    from flask import Flask, jsonify

    app = Flask(__name__)

    @app.route("/", methods=["POST"])
    def _handler():
        try:
            summary = run_pipeline()
            return jsonify({"status": "ok", **summary}), 200
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo el pipeline")
            return jsonify({"status": "error", "message": str(exc)}), 500

    @app.route("/health", methods=["GET"])
    def _health():
        return jsonify({"status": "healthy"}), 200

except ImportError:
    # Flask no instalado (modo CLI puro). No es error.
    app = None


# --------------------------------------------------------------------------- #
# Modo CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BigQuery -> TikTok CRM Events API")
    parser.add_argument("--config", default=None, help="Ruta al config.yaml")
    args = parser.parse_args()
    run_pipeline(args.config)
