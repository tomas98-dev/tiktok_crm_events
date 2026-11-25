# Guía de ejecución — Pipeline BigQuery → TikTok CRM Events

Esta guía es para ejecutar el proceso **de principio a fin**. Está pensada para
seguirse paso a paso, sin necesidad de conocer el código. Todo se hace desde
**Google Cloud Shell** (no hay que instalar nada en tu computador).

> Regla de oro: **primero se prueba, después se despliega.** Nunca pasar a
> producción sin haber visto los eventos llegar en *Test Events* de TikTok.

---

## Antes de empezar: ten esto a la mano

**De Google Cloud:**
- `project_id` del proyecto donde está BigQuery.
- Ubicación/región del dataset (ej. `US`, `southamerica-west1`).
- Dataset y tabla de leads, con los **nombres reales de las columnas**
  (teléfono, correo, estado, fecha de cambio, valor, etc.).
- Permisos para: habilitar APIs, crear cuentas de servicio, crear secretos y
  desplegar en Cloud Run.

**De TikTok (Events Manager):**
- `EVENT_SET_ID` del origen de datos tipo CRM.
- `ACCESS_TOKEN` generado en ese event set.
- `TEST_EVENT_CODE` (de la pestaña *Test Events*) para la prueba.
- Las etapas del embudo ya mapeadas, con los mismos nombres de evento que
  usaremos en la consulta.

---

## FASE 0 — Preparar el entorno

1. Abre **Google Cloud Shell** (icono de terminal, arriba a la derecha en la
   consola de GCP). Asegúrate de estar en el proyecto correcto.

2. Clona el proyecto y entra a la carpeta:

   ```bash
   git clone <URL_DEL_REPOSITORIO>
   cd tiktok_crm_events
   ```

3. Crea tu archivo de configuración a partir de la plantilla:

   ```bash
   cp config.example.yaml config.yaml
   ```

---

## FASE 1 — Configurar

4. Abre el archivo en el editor de Cloud Shell:

   ```bash
   cloudshell edit config.yaml
   ```

5. Completa los campos marcados con `<<< >>>`:
   - `gcp.project_id` → tu proyecto.
   - `gcp.bq_location` → la región real del dataset.
   - `tiktok.event_set_id` → el EVENT_SET_ID de TikTok.
   - `control.dataset` → un dataset donde el proceso pueda escribir.

6. **Ajusta la consulta** (`source.query`). Es la parte más importante:
   - Cambia el nombre de la tabla de origen (`proyecto.dataset.tabla`).
   - Ajusta los nombres de columna a los reales de tu tabla.
   - Revisa el `CASE` que mapea estados → nombres de evento de TikTok.

   La consulta **debe devolver estas columnas con estos nombres exactos**:

   | Columna | Obligatoria |
   |---|---|
   | `event_name` | Sí |
   | `event_time` | Sí |
   | `phone` / `email` / `lead_id` | Al menos uno |
   | `external_id` | No |
   | `value` / `currency` | No |

7. Pon un código de prueba para que la primera corrida **no afecte datos reales**:

   ```yaml
   tiktok:
     test_event_code: "TEST12345"   # el valor que ves en Events Manager → Test Events
   ```

> **Valida la consulta primero (muy recomendado):** copia tu `source.query` y
> ejecútala en la **consola de BigQuery**. Si devuelve las columnas esperadas,
> el resto del proceso va a funcionar. Esto evita sorpresas.

---

## FASE 2 — Prueba segura (sin afectar producción)

8. Instala las dependencias:

   ```bash
   pip install -r requirements.txt
   ```

9. Entrega el token por variable de entorno (no va en ningún archivo):

   ```bash
   export TIKTOK_ACCESS_TOKEN="pega-aqui-el-access-token"
   ```

10. Ejecuta el proceso una vez:

    ```bash
    python main.py --config config.yaml
    ```

11. **Verifica en TikTok → Events Manager → Test Events** que los eventos
    aparecen. Si llegan, el pipeline completo funciona.

---

## FASE 3 — Desplegar a producción (ejecución programada)

12. En `config.yaml`, **vacía** el código de prueba:

    ```yaml
    tiktok:
      test_event_code: ""
    ```

13. Abre el script de despliegue y completa el bloque 1 con tus datos:

    ```bash
    cloudshell edit deploy_comandos.sh
    ```

    - `PROJECT_ID`, `REGION`, `EVENT_SET_ID`, `SECRET_NAME`, `SERVICE_NAME`, `SCHEDULE`.
    - En el **paso 4** del script, reemplaza `PEGUE_AQUI_EL_ACCESS_TOKEN` por el
      token real.

14. Ejecuta el despliegue:

    ```bash
    bash deploy_comandos.sh
    ```

    El script hace todo en orden: habilita APIs → crea la cuenta de servicio →
    asigna permisos → guarda el token en Secret Manager → despliega en Cloud Run
    → autoriza al Scheduler → crea la ejecución programada.

---

## FASE 4 — Validar producción

15. Lanza una corrida real de prueba (el script imprime este comando al final):

    ```bash
    curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" <URL_DEL_SERVICIO>
    ```

16. En **BigQuery**, revisa la tabla de control (`control.dataset` +
    `tiktok_events_enviados`): debe tener filas con `status = 'success'`.

17. En **Events Manager**, confirma que los eventos ya entran como **reales**
    (ya no en Test Events).

¡Listo! A partir de aquí el proceso corre solo según el `SCHEDULE` configurado
(por defecto, cada hora).

---

## Si algo falla

| Síntoma | Causa probable | Qué revisar |
|---|---|---|
| Error al ejecutar la consulta | Región del dataset mal puesta | `gcp.bq_location` debe coincidir con la región real |
| En la tabla de control `status = 'error'` y `api_code` ≠ 0 | Token inválido o event set equivocado | Regenera el `ACCESS_TOKEN`; verifica el `EVENT_SET_ID` |
| `403 / permission denied` | Faltan permisos IAM | Revisa los roles de la cuenta de servicio (sección 6 del README) |
| No llegan eventos a Test Events | El mapeo de nombres no coincide | Los `event_name` del `CASE` deben existir en Events Manager |
| El proceso no se ejecuta solo | El Scheduler no quedó creado | `gcloud scheduler jobs list --location <REGION>` |

**Fuente de verdad del proceso:** la tabla de control en BigQuery. Cada fila
guarda `status`, `http_code`, `api_code` y `message` de cada envío.
