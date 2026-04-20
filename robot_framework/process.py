"""Daily summary of today's inspections — one mail to VejArealMail."""

from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement

import smtplib
from datetime import datetime
from email.message import EmailMessage
from html import escape

from azure.cosmos import CosmosClient

from robot_framework import config


FAKTURA_STATUS_LABELS = {
    "Ny": "Stadig opstillet",
    "Til fakturering": "Sendt til fakturering",
    "Fakturer ikke": "Skjult/Fjern",
    "Faktureret": "Faktureret",
}


def process(orchestrator_connection: OrchestratorConnection, queue_element: QueueElement | None = None) -> None:
    orchestrator_connection.log_trace("Sending daily inspection summary.")

    # --- Cosmos ---
    cosmos_credentials = orchestrator_connection.get_credential("AAKTilsynDB")
    client = CosmosClient(cosmos_credentials.username, credential=cosmos_credentials.password)
    container = client.get_database_client("aak-tilsyn").get_container_client("TilsynItems")

    # --- Today's inspections ---
    today = datetime.now().strftime("%Y-%m-%d")
    query = "SELECT * FROM c WHERE STARTSWITH(c.last_inspected_at, @today)"
    items = list(container.query_items(
        query=query,
        parameters=[{"name": "@today", "value": today}],
        enable_cross_partition_query=True,
    ))

    orchestrator_connection.log_info(f"Inspektioner i dag: {len(items)}")

    if not items:
        orchestrator_connection.log_info("Ingen inspektioner i dag — springer mail over.")
        return

    # Flatten: one row per inspection entry that happened today
    # (an item can be inspected multiple times in a day — show each)
    rows_data = []
    for item in items:
        for inspection in item.get("inspections") or []:
            if str(inspection.get("inspected_at", "")).startswith(today):
                rows_data.append((item, inspection))

    # Newest first
    rows_data.sort(key=lambda pair: pair[1].get("inspected_at", ""), reverse=True)

    recipient = orchestrator_connection.get_constant("VejArealMail").value

    # --- Build table rows ---
    row_html = []
    for item, inspection in rows_data:
        case_url = build_case_link(item)
        case_ref = build_case_ref(item)
        address = item.get("full_address") or ""
        info = build_info(item)
        status = build_status(item, inspection)
        extra = build_extra(item, inspection)
        comment = inspection.get("comment") or ""
        inspector = (inspection.get("inspector_email") or "").split("@")[0]
        inspected_time = format_time(inspection.get("inspected_at"))

        case_cell = (
            f'<a href="{escape(case_url)}" target="_blank">{escape(case_ref)}</a>'
            if case_url else escape(case_ref)
        )
        status_cell = escape(status)
        if extra:
            status_cell += f'<br><span style="color:#666;font-size:0.9em">{escape(extra)}</span>'

        row_html.append(f"""
            <tr>
                <td>{escape(inspected_time)}</td>
                <td>{escape(inspector)}</td>
                <td>{case_cell}</td>
                <td>{escape(address)}</td>
                <td>{escape(info)}</td>
                <td>{status_cell}</td>
                <td style="white-space:pre-wrap">{escape(comment)}</td>
            </tr>
        """)

    html = f"""
    <html>
      <body style="font-family:sans-serif">
        <h1>Dagens inspektioner</h1>
        <p>{len(rows_data)} inspektion(er) foretaget i dag fordelt på {len(items)} sag(er).</p>
        <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
          <thead>
            <tr>
              <th>Tid</th>
              <th>Tilsynsførende</th>
              <th>Sagsnummer</th>
              <th>Adresse</th>
              <th>Information</th>
              <th>Status</th>
              <th>Kommentar</th>
            </tr>
          </thead>
          <tbody>
            {''.join(row_html)}
          </tbody>
        </table>
      </body>
    </html>
    """
    jadt = orchestrator_connection.get_constant("jadt").value

    send_email(to_address=recipient, subject="Dagens inspektioner", body=html, bcc=jadt)
    orchestrator_connection.log_info("Mail sendt.")


def build_status(item: dict, inspection: dict) -> str:
    # Hide / restore is logged as an inspection entry with `hidden` set
    if "hidden" in inspection:
        return "Skjult fra tilsyn" if inspection["hidden"] else "Gendannet"

    t = item.get("type")
    if t == "permission":
        return inspection.get("selection") or ""
    if t == "henstilling":
        faktura = inspection.get("faktura_status")
        return FAKTURA_STATUS_LABELS.get(faktura, faktura or "Tilset")
    if t == "indmeldt":
        return "Tilset"
    return ""


def build_extra(item: dict, inspection: dict) -> str:
    """Second-line context under status (e.g. m² change for henstillinger)."""
    parts = []
    if item.get("type") == "henstilling":
        if inspection.get("kvadratmeter") is not None:
            parts.append(f"Areal: {inspection['kvadratmeter']} m²")
        if inspection.get("end_date"):
            parts.append(f"Slutdato: {inspection['end_date']}")
    return " · ".join(parts)


def format_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        return ""


def build_case_link(item: dict) -> str:
    t = item.get("type")
    if t == "permission":
        case_id = item.get("case_id")
        return f"https://vejman.vd.dk/permissions/update.jsp?caseid={case_id}" if case_id else ""
    if t == "henstilling":
        pezuuid = item.get("PEZUUID")
        return f"https://pez.giantleap.net/cases/view/{pezuuid}/case" if pezuuid else ""
    if t == "indmeldt":
        return "https://vejmankassen.adm.aarhuskommune.dk/tilsyn"
    return ""


def build_case_ref(item: dict) -> str:
    t = item.get("type")
    if t == "henstilling":
        return str(item.get("HenstillingId", ""))
    return str(item.get("case_number", ""))


def build_info(item: dict) -> str:
    t = item.get("type")
    if t == "permission":
        return item.get("rovm_equipment_type", "") or ""
    if t == "henstilling":
        return item.get("Forseelse", "") or ""
    if t == "indmeldt":
        return item.get("title", "") or ""
    return ""


def send_email(to_address: str | list[str], subject: str, body: str, bcc: str):
    msg = EmailMessage()
    msg["to"] = to_address
    msg["from"] = "AAKTilsyn <aaktilsyn@aarhus.dk>"
    msg["subject"] = subject
    msg['Bcc'] = bcc
    msg.set_content("Please enable HTML to view this message.")
    msg.add_alternative(body, subtype="html")

    with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.send_message(msg)