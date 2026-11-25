"""Carga y validacion de la configuracion (config.yaml + variables de entorno)."""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger("tiktok_crm")


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("CONFIG_PATH", "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    # Overrides por variables de entorno (prioridad sobre el archivo).
    tk = config.setdefault("tiktok", {})
    if os.environ.get("TIKTOK_EVENT_SET_ID"):
        tk["event_set_id"] = os.environ["TIKTOK_EVENT_SET_ID"]
    if os.environ.get("TIKTOK_TEST_EVENT_CODE"):
        tk["test_event_code"] = os.environ["TIKTOK_TEST_EVENT_CODE"]

    _validate(config)
    return config


def resolve_access_token(config: dict) -> str:
    """Resuelve el Access Token de TikTok en este orden de prioridad:

    1. Variable de entorno TIKTOK_ACCESS_TOKEN
       (recomendado en Cloud Run via --set-secrets).
    2. Lectura directa de Secret Manager, si se define tiktok.secret_name
       en config.yaml (o la variable TIKTOK_SECRET_NAME).
    3. tiktok.access_token en texto plano en config.yaml (NO recomendado).
    """
    tk = config.get("tiktok", {})

    # 1. Variable de entorno.
    token = os.environ.get("TIKTOK_ACCESS_TOKEN")
    if token:
        return token

    # 2. Secret Manager directo.
    secret_name = os.environ.get("TIKTOK_SECRET_NAME") or tk.get("secret_name")
    if secret_name:
        project = tk.get("secret_project") or config["gcp"]["project_id"]
        version = tk.get("secret_version", "latest")
        return _read_from_secret_manager(project, secret_name, version)

    # 3. Texto plano en el config (ultimo recurso).
    if tk.get("access_token"):
        return tk["access_token"]

    raise RuntimeError(
        "Falta el Access Token de TikTok. Use una de estas opciones: "
        "(1) variable de entorno TIKTOK_ACCESS_TOKEN, "
        "(2) tiktok.secret_name en config.yaml para leerlo de Secret Manager, o "
        "(3) tiktok.access_token en config.yaml (no recomendado)."
    )


def _read_from_secret_manager(project: str, secret_name: str, version: str = "latest") -> str:
    """Lee el valor de un secreto desde Secret Manager.

    Requiere que la cuenta de servicio tenga el rol
    roles/secretmanager.secretAccessor sobre el secreto.
    """
    try:
        from google.cloud import secretmanager
    except ImportError as exc:
        raise RuntimeError(
            "Para leer de Secret Manager instale 'google-cloud-secret-manager' "
            "(ya incluido en requirements.txt)."
        ) from exc

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_name}/versions/{version}"
    logger.info("Leyendo Access Token desde Secret Manager: %s", name)
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


def _require(config: dict, dotted: str) -> None:
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node or node[part] in (None, ""):
            raise RuntimeError(f"Falta el campo obligatorio en config: '{dotted}'")
        node = node[part]


def _validate(config: dict) -> None:
    for field in (
        "gcp.project_id",
        "tiktok.api_url",
        "tiktok.event_set_id",
        "source.query",
        "control.dataset",
        "control.table",
    ):
        _require(config, field)

    # Avisos de valores que suelen quedar sin completar.
    placeholder = "<<<"
    if placeholder in str(config["gcp"]["project_id"]):
        raise RuntimeError("Complete gcp.project_id en config.yaml (hay un placeholder).")
    if placeholder in str(config["tiktok"]["event_set_id"]):
        raise RuntimeError("Complete tiktok.event_set_id en config.yaml (hay un placeholder).")
    if placeholder in str(config["source"]["query"]):
        logger.warning("La consulta de origen aun contiene placeholders '<<<'. "
                       "Verifique que apunta a su tabla real.")
