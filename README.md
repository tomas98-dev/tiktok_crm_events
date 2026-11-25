# Pipeline BigQuery → TikTok CRM Events API

Envía conversiones de leads desde BigQuery hacia TikTok (CRM Events API, v1.3)
para optimizar campañas de Lead Generation con señales de bajo embudo
(ej. *Oportunidad*, *Matrícula*).

El pipeline lee los cambios de estado desde una tabla de BigQuery, hashea los
identificadores (teléfono, correo, external_id) con SHA-256, y hace el *postback*
al endpoint `/event/track/` de TikTok. Lleva una **tabla de control** para no
duplicar envíos (idempotencia).

---

## 1. Qué hace, paso a paso

1. Ejecuta la consulta que ustedes definen en `config.yaml` (su tabla de leads).
2. Calcula un `event_id` determinista por fila (identidad + evento + fecha).
3. Descarta los eventos ya enviados con éxito (consultando la tabla de control).
4. Normaliza teléfono a formato internacional E.164 y hashea los identificadores.
5. Envía los eventos a TikTok en lotes.
6. Registra el resultado de cada evento en la tabla de control.

Puede correrse **manualmente** (una vez) o **programado** (ej. cada hora) para
enviar solo los cambios nuevos.

---

## 2. Requisitos previos

**En GCP (proyecto del cliente):**
- Un dataset de BigQuery con la tabla de leads.
- APIs habilitadas: BigQuery. (Para despliegue automático: Cloud Run,
  Cloud Scheduler, Artifact Registry, Secret Manager.)
- Una cuenta de servicio con permisos (ver sección 6).

**En TikTok:**
- `EVENT_SET_ID`: el CRM Event Set ID (Events Manager → origen de datos CRM).
- `ACCESS_TOKEN`: el token generado en ese event set (*Manual setup → Generate access token*).

---

## 3. Configuración

Copie `config.example.yaml` a `config.yaml` y complete los campos marcados con `<<< >>>`.
Lo esencial que ustedes definen:

- `gcp.project_id` y `gcp.bq_location` (región del dataset).
- `tiktok.event_set_id`.
- `source.query`: **la consulta que apunta a su tabla**. Debe devolver las
  columnas con estos nombres exactos:

  | Columna       | Tipo       | Obligatoria | Nota |
  |---------------|------------|-------------|------|
  | `event_name`  | STRING     | Sí          | Nombre del evento mapeado en Events Manager |
  | `event_time`  | TIMESTAMP  | Sí          | Momento del cambio de estado |
  | `phone`       | STRING     | Al menos uno de | Teléfono en crudo |
  | `email`       | STRING     | phone/email/ | Correo en crudo |
  | `lead_id`     | STRING     | lead_id      | TikTok Lead ID (mejor match si existe) |
  | `external_id` | STRING     | No          | ID propio del CRM |
  | `value`       | NUMERIC    | No          | Valor de la conversión |
  | `currency`    | STRING     | No          | Moneda ISO 4217 (ej. `PEN`) |

- `control.dataset` / `control.table`: dónde se guarda el registro de envíos
  (la tabla se crea sola si no existe).

**El Access Token NO se pone en el archivo.** Tiene dos formas de entregarlo:

- **Opción A (recomendada, Cloud Run):** se guarda en Secret Manager y Cloud Run
  lo inyecta como la variable de entorno `TIKTOK_ACCESS_TOKEN` con `--set-secrets`.
  El código lo lee de esa variable. No se configura nada en el yaml.
- **Opción B (lectura directa):** el código lee Secret Manager por su cuenta.
  Descomente `tiktok.secret_name` en `config.yaml` con el nombre del secreto.
  Requiere el rol `secretmanager.secretAccessor`. Útil para correr como
  Cloud Run Job o en local sin exportar la variable.

En ambos casos, **el nombre del secreto en Secret Manager es libre** (ej.
`tiktok-access-token`); lo único fijo es la variable que el código espera
(`TIKTOK_ACCESS_TOKEN`) o el `secret_name` que ponga en el yaml.

### Mapeo de estados → eventos
El mapeo vive en el `CASE` de su consulta, así ustedes lo controlan. Los nombres
de evento que use (`event_name`) deben coincidir con los que mapeen en
**Events Manager → su event set → asignación de eventos a etapas del embudo**.
Recomendación: envíe solo las etapas que predicen valor (p. ej. *Oportunidad* y
*Matrícula*); no envíe *Interrumpido*.

---

## 4. Prueba local (recomendado antes de producción)

```bash
pip install -r requirements.txt

# El token se pasa por variable de entorno (no en el archivo)
export TIKTOK_ACCESS_TOKEN="pegue-aqui-el-token"

# Autenticación con GCP (usuario con permiso de lectura en BigQuery)
gcloud auth application-default login

python main.py --config config.yaml
```

Para **probar sin afectar datos reales**: ponga un valor en
`tiktok.test_event_code` (lo ve en Events Manager → *Test Events*). Los eventos
aparecerán ahí como prueba. Déjelo vacío en producción.

---

## 5. Despliegue automático (Cloud Run + Scheduler)

Ejecución programada, sin servidor que mantener.

```bash
PROJECT_ID="su-proyecto"
REGION="southamerica-west1"            # misma región del dataset si es posible
SA="tiktok-crm-run@${PROJECT_ID}.iam.gserviceaccount.com"

# 1) Guardar el token en Secret Manager
echo -n "EL_ACCESS_TOKEN" | gcloud secrets create tiktok-access-token \
  --data-file=- --project="$PROJECT_ID"

# 2) Desplegar el servicio (privado)
gcloud run deploy tiktok-crm-events \
  --source . \
  --region "$REGION" \
  --no-allow-unauthenticated \
  --service-account "$SA" \
  --timeout 3600 \
  --set-env-vars "CONFIG_PATH=config.yaml,TIKTOK_EVENT_SET_ID=EL_EVENT_SET_ID" \
  --set-secrets "TIKTOK_ACCESS_TOKEN=tiktok-access-token:latest" \
  --project "$PROJECT_ID"

# 3) Programar la ejecución (ej. cada hora) con invocación autenticada (OIDC)
gcloud scheduler jobs create http tiktok-crm-hourly \
  --schedule "17 * * * *" \
  --uri "$(gcloud run services describe tiktok-crm-events --region "$REGION" \
            --format 'value(status.url)')" \
  --http-method POST \
  --oidc-service-account-email "$SA" \
  --attempt-deadline 1800s \
  --location "$REGION" \
  --project "$PROJECT_ID"
```

> El `config.yaml` se incluye en la imagen al desplegar con `--source .`.
> Si prefieren no hornear la config en la imagen, móntenla desde un bucket o
> conviértanla en variables de entorno.

---

## 6. Permisos de la cuenta de servicio

| Permiso | Alcance | Para qué |
|---------|---------|----------|
| `roles/bigquery.dataViewer`  | dataset de leads   | leer la tabla de origen |
| `roles/bigquery.dataEditor`  | dataset de control | crear/escribir la tabla de control |
| `roles/bigquery.jobUser`     | proyecto           | ejecutar las consultas |
| `roles/secretmanager.secretAccessor` | secreto del token | leer el Access Token |
| `roles/run.invoker`          | el servicio Cloud Run | permitir que Scheduler lo invoque |

**Verificar también:** que el proyecto permita salida a internet hacia
`business-api.tiktok.com` (revisar VPC Service Controls si aplica).

---

## 7. Seguridad

- El Access Token es una credencial sensible: va en **Secret Manager** o variable
  de entorno, nunca en el código ni en el repositorio.
- Los identificadores (teléfono, correo, external_id) se envían **hasheados con
  SHA-256**; TikTok nunca recibe PII en texto plano. El `lead_id` de TikTok se
  envía tal cual (es un identificador propio de TikTok, no PII).

---

## 8. Archivos

| Archivo | Descripción |
|---------|-------------|
| `config.example.yaml` | Plantilla de configuración (copiar a `config.yaml`) |
| `main.py`             | Orquestador (CLI y servicio Cloud Run) |
| `config_loader.py`    | Carga y validación de la configuración |
| `bq_client.py`        | Lectura de BigQuery y tabla de control |
| `tiktok_client.py`    | Hashing, armado del payload y envío a TikTok |
| `requirements.txt`    | Dependencias |
| `Dockerfile`          | Imagen para Cloud Run |
