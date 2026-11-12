# itv-notifier

Comprueba las citas disponibles en las estaciones ITV de TÜV Rheinland y
avisa por Telegram **solo cuando hay citas nuevas**.

## Cómo funciona

- Consulta la web de cita previa (`itv-tuvrheinland.es`) con la matrícula
  configurada para cada centro indicado.
- **Solo notifica citas nuevas**: las que no se han notificado antes, o que
  dejaron de estar disponibles y han vuelto a aparecer. Las citas ya
  notificadas que siguen disponibles no se repiten.
- El estado de lo ya notificado se guarda en `state.json`. El workflow de
  GitHub Actions lo re-ingresa al repositorio tras cada ejecución para que
  persista entre ejecuciones (los runners son efímeros).
- Si borras `state.json`, la siguiente ejecución vuelve a notificar todas
  las citas disponibles actuales como si fueran nuevas.

## Configuración (secrets del repositorio)

| Secret             | Descripción                                                          |
|--------------------|----------------------------------------------------------------------|
| `PLATE`            | Matrícula del vehículo (p. ej. `1234ABC`)                            |
| `CENTERS`          | IDs de centros separados por coma (35 = Menorca Maó, 36 = Ciutadella). Catálogo completo en `itv_check.py` |
| `TELEGRAM_TOKEN`   | Token del bot de Telegram                                            |
| `TELEGRAM_CHAT_ID` | ID del chat destino                                                  |

## Ejecución

### GitHub Actions

Ejecución manual: pestaña **Actions** → *Comprobar ITV cada 5min* →
*Run workflow*.

Para ejecutarlo periódicamente, añade un disparador `schedule` al workflow
(por ejemplo, cada 5 minutos):

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "*/5 * * * *"
```

### Local

```bash
pip install -r requirements.txt
PLATE=1234ABC CENTERS=35,36 \
TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... \
python itv_check.py
```

Variables opcionales:

| Variable     | Por defecto   | Descripción                         |
|--------------|---------------|-------------------------------------|
| `STATE_FILE` | `state.json`  | Ruta del fichero de estado          |

## Detalles y limitaciones

- Los horarios se muestran en **hora de Madrid** (`Europe/Madrid`),
  igual que en la web, con conversión correcta de zona horaria (incluido
  el cambio de hora de verano/invierno).
- La API de la ITV devuelve las citas **por semanas**; el script comprueba
  la semana en curso (mismo comportamiento que siempre).
- Ante un error de consulta (web caída, matrícula no válida, sesión
  caducada) el script aborta sin tocar el estado, de modo que la
  siguiente ejecución lo reintente sin perder el seguimiento.
