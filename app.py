import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import anthropic
import requests
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

VERIFY_TOKEN        = os.environ.get("VERIFY_TOKEN", "plascorp_verify_123")
WHATSAPP_TOKEN      = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID     = os.environ.get("PHONE_NUMBER_ID", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS  = os.environ.get("GOOGLE_CREDENTIALS", "")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

conversations = {}

def get_sheet():
    if not GOOGLE_CREDENTIALS:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        scopes = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        return sh.worksheet("📋 BASE DE DATOS")
    except Exception as e:
        logger.error(f"Error conectando a Google Sheets: {e}")
        return None

def analyze_conversation(messages, contact_name, product):
    conversation_text = "\n".join([
        f"{'Cliente' if m['role'] == 'user' else 'Plascorp'}: {m['content']}"
        for m in messages
    ])
    prompt = f"""Eres un asistente de ventas B2B para Plascorp, empresa chilena de envases plásticos (PET bottles, bidones, pallets).

Analiza esta conversación de WhatsApp con el contacto "{contact_name}" sobre "{product}":

{conversation_text}

Responde SOLO con un JSON válido con esta estructura exacta:
{{
  "estado": "EN CONVERSACIÓN | SIN RESPUESTA | REALIZAR SEGUIMIENTO | PENDIENTE INFORMACIÓN | DERIVADO A VENTAS",
  "urgencia": "ALTA | MEDIA | BAJA",
  "dias_seguimiento": 3,
  "motivo": "Descripción breve de por qué necesita seguimiento",
  "mensaje_sugerido": "Mensaje corto y natural para reactivar la conversación"
}}

Criterios:
- ALTA urgencia: lleva más de 7 días sin respuesta pero mostró interés concreto
- MEDIA: conversación activa pero sin avanzar
- BAJA: solo consultó sin intención clara
- dias_seguimiento: en cuántos días hay que contactar (1-30)"""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.content[0].text)
    except Exception as e:
        logger.error(f"Error analizando conversación: {e}")
        return {
            "estado": "REALIZAR SEGUIMIENTO",
            "urgencia": "MEDIA",
            "dias_seguimiento": 7,
            "motivo": "Error en análisis automático",
            "mensaje_sugerido": f"Hola {contact_name}, quedamos en contacto. ¿Pudiste avanzar con lo que necesitabas?"
        }

def update_lead_in_sheet(phone, name, analysis):
    sheet = get_sheet()
    if not sheet:
        return
    try:
        all_values = sheet.get_all_values()
        headers = all_values[1] if len(all_values) > 1 else []
        col_phone  = next((i for i, h in enumerate(headers) if "TELÉFONO" in h.upper() or "TELEFONO" in h.upper()), None)
        col_estado = next((i for i, h in enumerate(headers) if "ESTADO" in h.upper()), None)
        col_obs    = next((i for i, h in enumerate(headers) if "OBSERVACION" in h.upper()), None)
        if col_phone is None:
            return
        phone_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        row_index = None
        for i, row in enumerate(all_values[2:], start=3):
            if col_phone < len(row):
                row_phone = row[col_phone].replace("+", "").replace(" ", "").replace("-", "")
                if row_phone == phone_clean or row_phone.endswith(phone_clean[-9:]):
                    row_index = i
                    break
        today = datetime.now().strftime("%d/%m/%Y")
        obs_text = f"{today}: {analysis['motivo']} | Seguimiento sugerido: {analysis['mensaje_sugerido']}"
        if row_index:
            if col_estado is not None:
                sheet.update_cell(row_index, col_estado + 1, analysis["estado"])
            if col_obs is not None:
                sheet.update_cell(row_index, col_obs + 1, obs_text)
            logger.info(f"Lead actualizado: {name} ({phone}) → {analysis['estado']}")
    except Exception as e:
        logger.error(f"Error actualizando planilla: {e}")

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()
    try:
        entry = data.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return jsonify({"status": "no messages"}), 200
        message = messages[0]
        phone    = message.get("from", "")
        msg_type = message.get("type", "")
        timestamp = message.get("timestamp", "")
        if msg_type != "text":
            return jsonify({"status": "non-text message ignored"}), 200
        text = message.get("text", {}).get("body", "")
        contacts = value.get("contacts", [{}])
        name = contacts[0].get("profile", {}).get("name", phone) if contacts else phone
        if phone not in conversations:
            conversations[phone] = {"name": name, "messages": [], "product": "envases"}
        conversations[phone]["messages"].append({"role": "user", "content": text, "timestamp": timestamp})
        text_lower = text.lower()
        if "bidon" in text_lower or "bidón" in text_lower:
            conversations[phone]["product"] = "bidones"
        elif "pet" in text_lower or "botella" in text_lower:
            conversations[phone]["product"] = "botellas PET"
        elif "pallet" in text_lower or "palé" in text_lower:
            conversations[phone]["product"] = "pallets"
        conv_data = conversations[phone]
        analysis = analyze_conversation(conv_data["messages"], conv_data["name"], conv_data["product"])
        update_lead_in_sheet(phone, name, analysis)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Error procesando webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Plascorp CRM Webhook", "timestamp": datetime.now().isoformat()}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
