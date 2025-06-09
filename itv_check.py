import os
import requests
import json
import re
from datetime import datetime
from collections import defaultdict

# ————————————
# Formateo de citas
# ————————————
def format_dates(appointments):
    if appointments.get("error"):
        return appointments["error"]
    
    result = appointments.get("result", {})
    available_dates = result.get("availableDates", {})
    if not available_dates:
        return "No hay citas disponibles"
    
    dates_by_day = defaultdict(list)
    for _, date_str in available_dates.items():
        date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        date = date.replace(hour=date.hour + 2)
        key = date.strftime("%Y-%m-%d")
        dates_by_day[key].append(date.strftime("%H:%M"))
    
    output = [f"CITAS ITV ({result.get('startTime','?')}-{result.get('endTime','?')})", "─"*40]
    dias = {
        'Monday': 'LUN','Tuesday': 'MAR','Wednesday': 'MIE',
        'Thursday': 'JUE','Friday': 'VIE','Saturday': 'SAB','Sunday': 'DOM'
    }
    for day in sorted(dates_by_day):
        dobj = datetime.strptime(day, "%Y-%m-%d")
        label = dias[dobj.strftime("%A")]
        output.append(f"{label} {dobj.strftime('%d/%m')}:")
        output.append("  " + " │ ".join(sorted(dates_by_day[day])))
        output.append("")
    output += [
        "─"*40,
        f"Total: {sum(len(v) for v in dates_by_day.values())} citas",
        "Centros: " + os.getenv("CENTERS", "35,36")
    ]
    return "\n".join(output)

# ————————————
# Lógica de consulta
# ————————————
def get_csrf_token(session):
    r = session.get("https://www.itv-tuvrheinland.es/cita-previa-itv")
    r.raise_for_status()
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
    return m.group(1) if m else None

def get_itv_appointments(plate, center_id):
    base = "https://www.itv-tuvrheinland.es"
    s = requests.Session()
    try:
        token = get_csrf_token(s)
        if not token:
            return {"error": "Could not obtain CSRF token"}
        hdr = {
            "Accept": "application/json, text/plain, */*",
            "Content-Language": "es",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "User-Agent": os.getenv("USER_AGENT", "Mozilla/5.0"),
            "X-CSRF-TOKEN": token,
            "Referer": f"{base}/cita-previa-itv"
        }
        s.post(f"{base}/citation/sendPlate", params={"plate": plate}, headers=hdr).raise_for_status()
        s.post(f"{base}/citation/getAllCitationServices", params={"vehicle_type_id": 10}, headers=hdr).raise_for_status()
        s.post(f"{base}/citation/validateFuelAndType", params={"typeId": 10, "fuelId": 1, "service_id": 1}, headers=hdr).raise_for_status()
        s.post(f"{base}/citation/getRegionsByService", params={"vehicle_type_id": 10, "service_id": 1}, headers=hdr).raise_for_status()
        resp = s.post(
            f"{base}/citation/getAvailableDates",
            params={"center_id": center_id, "vehicle_type_id": 10, "service_id": 1, "is_booking_update": 0},
            headers=hdr
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": f"Request failed: {e}"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {e}"}

# ————————————
# Envío por Telegram
# ————————————
def send_via_telegram(text):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text":    text,
        "parse_mode": "Markdown"
    }
    r = requests.post(url, json=payload)
    r.raise_for_status()

# ————————————
# Main
# ————————————
def main():
    plate      = os.getenv("PLATE")
    centers    = os.getenv("CENTERS", "35,36").split(",")
    parts      = []
    send_message = False

    for c in centers:
        data = get_itv_appointments(plate, int(c))
        # Verificar si hay citas disponibles (no es 512)
        if data.get("result", {}).get("availableDates", {}):
            txt = format_dates(data)
            parts.append(f"*Centro {c}*\n{txt}")
            send_message = True

    if send_message:
        message_body = "\n\n".join(parts)
        send_via_telegram(message_body)
        print("Mensaje enviado via Telegram.")
    else:
        print("No hay citas disponibles, no se envía mensaje.")

if __name__ == "__main__":
    main()
