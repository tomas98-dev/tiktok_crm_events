"""Acceso a BigQuery: lectura de eventos pendientes y tabla de control.

Flujo de idempotencia:
  1. Se ejecuta la consulta de origen (config['source']['query']).
  2. Se calcula el event_id determinista de cada fila.
  3. Se descartan los event_id que ya figuran como 'success' en la tabla de control.
  4. Tras enviar, se registran todos los resultados en la tabla de control.
"""

from __future__ import annotations

import logging

from google.cloud import bigquery

from tiktok_client import build_event_id, _to_unix_seconds

logger = logging.getLogger("tiktok_crm")

# Esquema de la tabla de control.
_CONTROL_SCHEMA = [
    bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("lead_id", "STRING"),
    bigquery.SchemaField("external_id", "STRING"),
    bigquery.SchemaField("event_name", "STRING"),
    bigquery.SchemaField("event_time", "TIMESTAMP"),
    bigquery.SchemaField("sent_at", "TIMESTAMP"),
    bigquery.SchemaField("http_code", "INTEGER"),
    bigquery.SchemaField("api_code", "INTEGER"),
    bigquery.SchemaField("message", "STRING"),
    bigquery.SchemaField("status", "STRING"),
]


def make_client(config: dict) -> bigquery.Client:
    gcp = config["gcp"]
    return bigquery.Client(project=gcp["project_id"], location=gcp.get("bq_location"))


def _control_table_id(config: dict) -> str:
    gcp = config["gcp"]
    ctrl = config["control"]
    return f"{gcp['project_id']}.{ctrl['dataset']}.{ctrl['table']}"


def ensure_control_table(client: bigquery.Client, config: dict) -> None:
    table_id = _control_table_id(config)
    try:
        client.get_table(table_id)
        logger.info("Tabla de control existente: %s", table_id)
    except Exception:
        table = bigquery.Table(table_id, schema=_CONTROL_SCHEMA)
        client.create_table(table)
        logger.info("Tabla de control creada: %s", table_id)


def fetch_pending_events(client: bigquery.Client, config: dict) -> list[dict]:
    """Devuelve las filas aun no enviadas (o que fallaron) listas para procesar."""
    query = config["source"]["query"]
    logger.info("Ejecutando consulta de origen...")
    rows = [dict(r) for r in client.query(query).result()]
    logger.info("Filas devueltas por la consulta: %d", len(rows))
    if not rows:
        return []

    # Calculamos event_id de cada fila.
    for row in rows:
        et = _to_unix_seconds(row["event_time"])
        row["event_id"] = build_event_id(row, et)

    sent_ids = _already_sent_ids(client, config, [r["event_id"] for r in rows])
    pending = [r for r in rows if r["event_id"] not in sent_ids]
    logger.info("Ya enviadas: %d | Pendientes: %d", len(sent_ids), len(pending))
    return pending


def _already_sent_ids(client: bigquery.Client, config: dict, ids: list[str]) -> set[str]:
    """Consulta la tabla de control por los event_id ya marcados como 'success'."""
    if not ids:
        return set()
    table_id = _control_table_id(config)
    sql = f"""
        SELECT DISTINCT event_id
        FROM `{table_id}`
        WHERE status = 'success'
          AND event_id IN UNNEST(@ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
    )
    try:
        result = client.query(sql, job_config=job_config).result()
        return {r["event_id"] for r in result}
    except Exception as exc:
        # Si la tabla aun no existe o hay un problema puntual, seguimos sin
        # filtro (la deduplicacion por event_id de TikTok cubre el envio).
        logger.warning("No se pudo consultar la tabla de control (%s). "
                       "Se continua sin filtro local.", exc)
        return set()


def record_results(client: bigquery.Client, config: dict, records: list[dict]) -> None:
    if not records:
        return
    table_id = _control_table_id(config)
    errors = client.insert_rows_json(table_id, records)
    if errors:
        logger.error("Errores al registrar en la tabla de control: %s", errors)
    else:
        logger.info("Registrados %d resultados en %s", len(records), table_id)
