#!/usr/bin/env python3
"""Comprueba las citas ITV disponibles y avisa por Telegram.

Recupera TODAS las citas del calendario abierto de cada centro (todas las
semanas con disponibilidad, no solo la actual: la API se consulta semana
a semana igual que hace la web con su calendario).

Solo notifica las citas NUEVAS: las que no se han notificado antes, o que
dejaron de estar disponibles y han vuelto a aparecer. Las citas ya
notificadas que siguen disponibles no se repiten.

El estado de lo ya notificado se guarda en STATE_FILE (por defecto
``state.json``). En GitHub Actions el workflow lo re-ingresa al repositorio
tras cada ejecución para que persista entre ejecuciones.
"""

import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://www.itv-tuvrheinland.es"
MADRID = ZoneInfo("Europe/Madrid")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BOOKING_URL = f"{BASE_URL}/cita-previa-itv"

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

# Servicio 1 = ITV normal (2 = segundas inspecciones).
SERVICE_ID = 1

# Seguridad: tope de semanas a consultar por centro (~2 años) y pausa
# entre peticiones para no saturar la web.
MAX_WEEKS = 104
WEEK_FETCH_DELAY = 0.3

# Límite de Telegram: 4096 caracteres por mensaje. Dejamos margen por si
# el cliente cuenta entidades de distinta forma.
MESSAGE_SAFE_LEN = 3600

DIAS = {
    "Monday": "Lunes",
    "Tuesday": "Martes",
    "Wednesday": "Miércoles",
    "Thursday": "Jueves",
    "Friday": "Viernes",
    "Saturday": "Sábado",
    "Sunday": "Domingo",
}

MESES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
    5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
    9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
}

# Catálogo de centros de la web de ITV (clave = tuv_id, que es lo que
# espera la API como center_id). Menorca: 35 = Maó, 36 = Ciutadella.
CENTROS = {
    10: "ITV Terrassa - Viladecavalls",
    17: "ITV Barcelona Àvila",
    18: "ITV Barcelona Còrsega",
    19: "ITV Barcelona La Maquinista - Caracas",
    20: "ITV Sant Cugat",
    21: "ITV CIM Vallès",
    22: "ITV Manresa",
    23: "ITV Berga",
    24: "ITV La Seu d'Urgell - Montferrer",
    25: "ITV Solsona",
    35: "ITV Menorca Maó",
    36: "ITV Menorca Ciutadella",
    38: "ITV Pamplona",
    39: "ITV Arbizu",
    40: "ITV Doneztebe",
    41: "ITV Areta Huarte",
    42: "ITV en Leganés",
    43: "ITV Parla",
    44: "ITV Navas del Rey",
    45: "ITV Alcorcón",
    46: "ITV Navalcarnero",
    47: "ITV en Fuenlabrada",
    48: "ITV Móstoles",
    49: "ITV en Rivas H2O",
    50: "ITV en San Sebastián de los Reyes",
    51: "ITV Julián Camarillo",
    52: "ITV Manzanares",
    53: "ITV Hellín",
    54: "ITV Almansa",
    55: "ITV Motilla",
    56: "ITV Alcolea del Pinar",
    57: "ITV en Toledo",
    58: "ITV Lorca",
    59: "ITV Cartagena",
    60: "ITV Caravaca",
    61: "ITV San Pedro del Pinatar",
    62: "ITV Media Sala (Nueva Cartagena)",
    63: "ITV Águilas",
    70: "ITV La Cayetana",
}


class ItvError(Exception):
    """Error devuelto por la web de la ITV."""


# ————————————————————————————————————————
# Consulta a la web de la ITV
# ————————————————————————————————————————
def _post_json(session, path, params, headers):
    """POST a la API de la ITV validando errores HTTP y de la propia API."""
    resp = session.post(
        f"{BASE_URL}/citation/{path}", params=params, headers=headers, timeout=30
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise ItvError(f"Respuesta no válida del endpoint {path}") from None
    # La API devuelve errores con HTTP 200 (p. ej. "Matrícula no válida"),
    # así que hay que inspeccionar el cuerpo de la respuesta.
    if isinstance(data, dict) and data.get("error") is True:
        raise ItvError(data.get("message") or f"Error desconocido en {path}")
    return data


def _parse_slot(raw):
    """Convierte la fecha cruda de la API (UTC) a hora de Madrid."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(MADRID)


def _slot_id(raw):
    """Identificador estable de la cita (instante UTC normalizado)."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()


def _parse_slots(result):
    """Extrae ``{id_cita: datetime}`` de la respuesta de un getAvailableDates."""
    slots = {}
    for raw in (result.get("availableDates") or {}).values():
        slots[_slot_id(raw)] = _parse_slot(raw)
    return slots


def _open_future_weeks(result):
    """Timestamps de las semanas FUTURAS con disponibilidad (servicio ITV).

    La respuesta inicial incluye ``resultAvailabilityPerWeek`` con todas
    las semanas abiertas del calendario del centro; es lo que usa la web
    para habilitar los botones de navegación por semanas. Las respuestas
    de semanas concretas ya no lo incluyen, así que se toma de aquí.
    """
    current_week_ts = result.get("WeekStartDateTimeStamp") or 0
    return sorted(
        {
            week["WeekStartDateTimeStamp"]
            for week in (result.get("resultAvailabilityPerWeek") or [])
            if week.get("IdService") == SERVICE_ID
            and week.get("WeekStartDateTimeStamp", 0) > current_week_ts
        }
    )


def fetch_available_dates(plate, center_id):
    """Devuelve ``{id_cita: datetime}`` con TODAS las citas del centro.

    Consulta la semana en curso y luego cada semana futura abierta del
    calendario (el listado de semanas abiertas llega en la primera
    respuesta, en ``resultAvailabilityPerWeek``).
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    page = session.get(f"{BASE_URL}/cita-previa-itv", timeout=30)
    page.raise_for_status()
    match = re.search(r'name="csrf-token"\s+content="([^"]+)"', page.text)
    if not match:
        raise ItvError("No se pudo obtener el token CSRF de la web")

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Language": "es",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
        "X-CSRF-TOKEN": match.group(1),
        "Referer": BOOKING_URL,
    }

    _post_json(session, "sendPlate", {"plate": plate}, headers)
    _post_json(session, "getAllCitationServices", {"vehicle_type_id": 10}, headers)
    _post_json(
        session, "validateFuelAndType",
        {"typeId": 10, "fuelId": 1, "service_id": SERVICE_ID}, headers,
    )
    _post_json(
        session, "getRegionsByService",
        {"vehicle_type_id": 10, "service_id": SERVICE_ID}, headers,
    )

    params = {
        "center_id": center_id,
        "vehicle_type_id": 10,
        "service_id": SERVICE_ID,
        "is_booking_update": 0,
    }

    # Semana en curso. Esta primera llamada también registra el centro en
    # la sesión: sin ella, las peticiones de semanas futuras fallan con
    # "No se ha seleccionado el Centro" (api_error_code 940).
    data = _post_json(session, "getAvailableDates", params, headers)
    result = data.get("result") or {}
    slots = _parse_slots(result)

    # Resto de semanas abiertas del calendario.
    for position, week_ts in enumerate(_open_future_weeks(result)[:MAX_WEEKS]):
        week_start = datetime.fromtimestamp(week_ts, MADRID).strftime("%Y-%m-%d")
        week_data = _post_json(
            session, "getAvailableDates",
            {**params, "week_start_date": week_start}, headers,
        )
        week_result = week_data.get("result") or {}
        if week_result.get("WeekStartDateTimeStamp") and week_result[
            "WeekStartDateTimeStamp"
        ] != week_ts:
            # La web devolvió otra semana distinta de la pedida: no mezclar.
            continue
        slots.update(_parse_slots(week_result))
        if position < MAX_WEEKS - 1:
            time.sleep(WEEK_FETCH_DELAY)

    return slots


def fetch_with_retry(plate, center_id, attempts=2, wait_seconds=2):
    """Reintenta la consulta: la web a veces caduca la sesión (HTTP 419)."""
    last_error = None
    for attempt in range(attempts):
        try:
            return fetch_available_dates(plate, center_id)
        except (ItvError, requests.RequestException) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(wait_seconds)
    raise last_error


# ————————————————————————————————————————
# Estado de citas ya notificadas
# ————————————————————————————————————————
def load_state():
    """Carga las citas ya notificadas: ``{centro: set(ids)}``."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {"notified": {k: set(v) for k, v in (data.get("notified") or {}).items()}}
        except (ValueError, OSError):
            print(
                f"Aviso: {STATE_FILE} estaba corrupto; se reinicia el seguimiento.",
                file=sys.stderr,
            )
    return {"notified": {}}


def save_state(state):
    """Guarda el estado de forma atómica (tmp + rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"notified": {k: sorted(v) for k, v in state["notified"].items()}}
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ————————————————————————————————————————
# Formato del mensaje de Telegram (HTML)
# ————————————————————————————————————————
def _center_groups(center_id, datetimes):
    """Grupos de líneas de un centro: cabecera, un grupo por día y recuento.

    Devolver grupos independientes permite trocear el mensaje por días si
    un centro tuviera demasiadas citas para un solo mensaje de Telegram.
    """
    name = CENTROS.get(int(center_id), f"Centro {center_id}")
    groups = [[f"📍 <b>{html.escape(name)}</b>", "─" * 20, ""]]

    by_day = defaultdict(list)
    for dt in datetimes:
        by_day[dt.date()].append(dt)

    for day in sorted(by_day):
        moments = sorted(by_day[day])
        first = moments[0]
        lines = [
            f"🗓 <b>{DIAS[first.strftime('%A')]}, {first.day} de {MESES[first.month]}</b>"
        ]
        times = [dt.strftime("%H:%M") for dt in moments]
        for i in range(0, len(times), 4):
            lines.append(" · ".join(f"<code>{t}</code>" for t in times[i : i + 4]))
        lines.append("")
        groups.append(lines)

    count = len(datetimes)
    plural = "s" if count != 1 else ""
    groups.append([f"➕ <i>{count} cita{plural} nueva{plural}</i>", ""])
    return groups


def _lines_len(lines):
    return sum(len(line) + 1 for line in lines)


def _pack(groups, first_title, cont_title, footer):
    """Empaqueta grupos de líneas en mensajes que no superen el límite."""
    chunks = []
    current = [first_title, ""]
    current_len = _lines_len(current)

    def flush():
        nonlocal current, current_len
        if len(current) > 2:
            chunks.append("\n".join(current))
        current = [cont_title, ""]
        current_len = _lines_len(current)

    def add_group(group):
        nonlocal current_len
        group_len = _lines_len(group)
        if group_len <= MESSAGE_SAFE_LEN:
            if current_len + group_len > MESSAGE_SAFE_LEN and len(current) > 2:
                flush()
            current.extend(group)
            current_len += group_len
        else:
            # Grupo anormalmente grande: trocear por líneas individuales.
            for line in group:
                if current_len + len(line) + 1 > MESSAGE_SAFE_LEN and len(current) > 2:
                    flush()
                current.append(line)
                current_len += len(line) + 1

    for group in groups:
        add_group(group)
    add_group(footer)
    if len(current) > 2:
        chunks.append("\n".join(current))
    return chunks


def build_message(new_by_center):
    """Construye el mensaje (o mensajes, si hay que trocearlos) en HTML."""
    total = sum(len(v) for v in new_by_center.values())
    plural = "s" if total != 1 else ""
    footer = [
        "─" * 20,
        f"ℹ️ Total: {total} cita{plural} nueva{plural}",
        f'🔗 <a href="{BOOKING_URL}">Reservar cita</a>',
    ]
    groups = []
    for center_id, datetimes in new_by_center.items():
        groups.extend(_center_groups(center_id, datetimes))
    return _pack(
        groups,
        "🚗 <b>Nuevas citas ITV</b>",
        "🚗 <b>Nuevas citas ITV</b> (continuación)",
        footer,
    )


# ————————————————————————————————————————
# Envío por Telegram
# ————————————————————————————————————————
def send_via_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")

    # Con muchas citas nuevas el mensaje se trocea y puede superarse el
    # límite de envío por chat: se reintenta respetando retry_after.
    for attempt in range(3):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=30,
        )
        if resp.status_code == 429:
            try:
                retry_after = int(
                    resp.json().get("parameters", {}).get("retry_after", 2)
                )
            except ValueError:
                retry_after = 2
            time.sleep(retry_after + 0.5)
            continue
        if resp.status_code != 200:
            raise RuntimeError(
                f"Telegram respondió {resp.status_code}: {resp.text[:200]}"
            )
        return
    raise RuntimeError("Telegram siguió respondiendo 429 tras varios reintentos")


# ————————————————————————————————————————
# Main
# ————————————————————————————————————————
def main():
    plate = os.getenv("PLATE", "").strip()
    centers = [c.strip() for c in os.getenv("CENTERS", "35,36").split(",") if c.strip()]
    if not plate:
        sys.exit("Error: la variable PLATE no está definida.")

    state = load_state()
    notified = state["notified"]

    # 1) Consultar todos los centros. Ante cualquier error, abortar sin
    #    tocar el estado para no perder el seguimiento ni avisar mal.
    current = {}
    for center in centers:
        try:
            current[center] = fetch_with_retry(plate, int(center))
        except (ItvError, requests.RequestException) as exc:
            sys.exit(f"Error consultando el centro {center}: {exc}")

        previous = notified.get(center, set())
        fresh = set(current[center]) - previous
        gone = previous - set(current[center])
        name = CENTROS.get(int(center), f"Centro {center}")
        print(
            f"[{name}] disponibles: {len(current[center])} · "
            f"nuevas: {len(fresh)} · retiradas: {len(gone)}"
        )

    # 2) Citas nuevas = nunca vistas, o vistas que desaparecieron y han
    #    vuelto a aparecer (se quitaron del estado al dejar de existir).
    new_by_center = {}
    for center in centers:
        fresh_ids = set(current[center]) - notified.get(center, set())
        if fresh_ids:
            new_by_center[center] = sorted(
                dt for slot_id, dt in current[center].items() if slot_id in fresh_ids
            )

    # 3) Enviar solo si hay algo nuevo. Si el envío falla, no se guarda el
    #    estado para reintentarlo en la siguiente ejecución.
    if new_by_center:
        total_new = sum(len(v) for v in new_by_center.values())
        plural = "s" if total_new != 1 else ""
        chunks = build_message(new_by_center)
        try:
            for index, chunk in enumerate(chunks):
                if index:
                    time.sleep(1.1)  # límite de ~1 mensaje/segundo por chat
                send_via_telegram(chunk)
        except RuntimeError as exc:
            sys.exit(f"Error enviando por Telegram: {exc}")
        print(
            f"Mensaje enviado por Telegram ({total_new} cita{plural} nueva{plural}"
            f" en {len(chunks)} mensaje{'s' if len(chunks) != 1 else ''})."
        )
    else:
        print("Sin citas nuevas; no se envía mensaje.")

    # 4) Persistir el estado: lo visto pasa a "notificado" y lo que ya no
    #    está disponible se olvida, para que reaparezca como novedad.
    for center in centers:
        notified[center] = set(current[center])
    save_state({"notified": notified})


if __name__ == "__main__":
    main()
