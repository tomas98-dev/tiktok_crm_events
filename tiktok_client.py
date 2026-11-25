"""Construcción del payload y envío a TikTok CRM Events API (v1.3).

Responsabilidades:
  - Normalizar y hashear identificadores (telefono, email, external_id).
  - Construir el payload CRM que espera /event/track/.
  - Enviar en lotes con reintentos y parsear la respuesta.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger("tiktok_crm")

# Codigo de exito que devuelve TikTok en el cuerpo de la respuesta.
TIKTOK_OK_CODE = 0
_RETRY_STATUS = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Hashing / normalizacion
# --------------------------------------------------------------------------- #
def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_email(email: str | None) -> str | None:
    if not email:
        return None
    normalized = email.strip().lower()
    if not normalized:
        return None
    return _sha256(normalized)


def normalize_phone(raw: str | None, default_country_code: str) -> str | None:
    """Devuelve el telefono en formato E.164 (solo digitos con prefijo pais)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    # Quita ceros de marcacion internacional/local al inicio.
    digits = digits.lstrip("0")
    cc = re.sub(r"\D", "", default_country_code or "")
    # Si no trae el prefijo de pais, lo anteponemos.
    if cc and not digits.startswith(cc):
        digits = cc + digits
    return "+" + digits


def hash_phone(raw: str | None, default_country_code: str) -> str | None:
    e164 = normalize_phone(raw, default_country_code)
    if not e164:
        return None
    return _sha256(e164)


def hash_external_id(external_id: str | None) -> str | None:
    if external_id is None:
        return None
    normalized = str(external_id).strip()
    if not normalized:
        return None
    return _sha256(normalized)


# --------------------------------------------------------------------------- #
# event_id determinista (idempotencia)
# --------------------------------------------------------------------------- #
def _to_unix_seconds(event_time) -> int:
    if isinstance(event_time, (int, float)):
        return int(event_time)
    if isinstance(event_time, datetime):
        dt = event_time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    # String ISO como ultimo recurso.
    dt = datetime.fromisoformat(str(event_time))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_event_id(row: dict, event_time_unix: int) -> str:
    """ID estable por (identidad + evento + momento) para no duplicar envios.

    TikTok tambien deduplica por event_id, asi que esto da doble proteccion.
    """
    identity = (
        row.get("external_id")
        or row.get("lead_id")
        or row.get("phone")
        or row.get("email")
        or "sin-identidad"
    )
    base = f"{identity}|{row.get('event_name')}|{event_time_unix}"
    return _sha256(base)[:32]


# --------------------------------------------------------------------------- #
# Construccion del payload
# --------------------------------------------------------------------------- #
def build_event(row: dict, default_country_code: str) -> dict:
    """Transforma una fila de BigQuery en un evento del payload de TikTok."""
    event_time_unix = _to_unix_seconds(row["event_time"])
    event_id = row.get("event_id") or build_event_id(row, event_time_unix)

    user: dict = {}
    phone_hashed = hash_phone(row.get("phone"), default_country_code)
    email_hashed = hash_email(row.get("email"))
    ext_hashed = hash_external_id(row.get("external_id"))
    if phone_hashed:
        user["phone"] = phone_hashed
    if email_hashed:
        user["email"] = email_hashed
    if ext_hashed:
        user["external_id"] = ext_hashed
    # El Lead ID de TikTok NO se hashea (es un identificador propio de TikTok).
    if row.get("lead_id"):
        user["lead_id"] = str(row["lead_id"])

    properties: dict = {}
    if row.get("value") is not None:
        properties["value"] = float(row["value"])
    if row.get("currency"):
        properties["currency"] = str(row["currency"])

    event = {
        "event": str(row["event_name"]),
        "event_time": event_time_unix,
        "event_id": event_id,
        "user": user,
    }
    if properties:
        event["properties"] = properties

    # Guardamos el event_id calculado para el registro de control.
    row["event_id"] = event_id
    return event


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def send_batch(
    events: list[dict],
    *,
    api_url: str,
    access_token: str,
    event_set_id: str,
    test_event_code: str = "",
    max_retries: int = 4,
    timeout: int = 30,
) -> dict:
    """Envia un lote de eventos. Devuelve un dict con el resultado del envio."""
    payload = {
        "event_source": "crm",
        "event_source_id": event_set_id,
        "data": events,
    }
    if test_event_code:
        payload["test_event_code"] = test_event_code

    headers = {
        "Access-Token": access_token,
        "Content-Type": "application/json",
    }

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            if attempt <= max_retries:
                wait = 2 ** attempt
                logger.warning("Error de red (%s). Reintento %d en %ds", exc, attempt, wait)
                time.sleep(wait)
                continue
            return {"ok": False, "http_code": None, "api_code": None,
                    "message": f"Fallo de red: {exc}"}

        # Reintentos ante 429/5xx.
        if resp.status_code in _RETRY_STATUS and attempt <= max_retries:
            wait = 2 ** attempt
            logger.warning("HTTP %s. Reintento %d en %ds", resp.status_code, attempt, wait)
            time.sleep(wait)
            continue

        try:
            body = resp.json()
        except ValueError:
            body = {}

        api_code = body.get("code")
        message = body.get("message", "")
        ok = resp.status_code == 200 and api_code == TIKTOK_OK_CODE
        return {
            "ok": ok,
            "http_code": resp.status_code,
            "api_code": api_code,
            "message": message,
        }


def post_events(rows: list[dict], config: dict, access_token: str) -> list[dict]:
    """Envia todas las filas en lotes y devuelve los registros de control.

    Cada registro incluye el event_id, identidad, resultado y respuesta de la API.
    """
    tk = config["tiktok"]
    default_cc = config.get("identity", {}).get("default_country_code", "")
    batch_size = int(tk.get("batch_size", 1000))

    # Construimos los eventos (esto tambien fija row["event_id"]).
    built: list[tuple[dict, dict]] = []
    for row in rows:
        try:
            event = build_event(row, default_cc)
            built.append((row, event))
        except Exception as exc:  # fila mal formada: se registra y se omite
            logger.error("Fila omitida por error al construir evento: %s", exc)
            row["event_id"] = row.get("event_id") or "NA"
            row["_result"] = {"ok": False, "http_code": None, "api_code": None,
                              "message": f"Error construyendo evento: {exc}"}

    results: list[dict] = []
    valid = [(r, e) for (r, e) in built if "_result" not in r]

    for batch in _chunks(valid, batch_size):
        events = [e for (_, e) in batch]
        res = send_batch(
            events,
            api_url=tk["api_url"],
            access_token=access_token,
            event_set_id=tk["event_set_id"],
            test_event_code=tk.get("test_event_code", "") or "",
        )
        for (row, _event) in batch:
            results.append(_control_record(row, res))
        if res["ok"]:
            logger.info("Lote enviado OK: %d eventos", len(events))
        else:
            logger.error("Lote con error (http=%s code=%s): %s",
                         res["http_code"], res["api_code"], res["message"])

    # Filas que fallaron al construirse.
    for row in rows:
        if "_result" in row:
            results.append(_control_record(row, row["_result"]))

    return results


def _control_record(row: dict, res: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    event_time_iso = None
    try:
        event_time_iso = datetime.fromtimestamp(
            _to_unix_seconds(row["event_time"]), tz=timezone.utc
        ).isoformat()
    except Exception:
        pass
    return {
        "event_id": row.get("event_id", "NA"),
        "lead_id": str(row.get("lead_id")) if row.get("lead_id") else None,
        "external_id": str(row.get("external_id")) if row.get("external_id") else None,
        "event_name": str(row.get("event_name")) if row.get("event_name") else None,
        "event_time": event_time_iso,
        "sent_at": now,
        "http_code": res.get("http_code"),
        "api_code": res.get("api_code"),
        "message": (res.get("message") or "")[:1000],
        "status": "success" if res.get("ok") else "error",
    }
