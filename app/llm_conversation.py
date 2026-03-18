"""
LLM-driven conversation engine for Maison Éclat voice agent.

Replaces the intent → handler pipeline with a single GPT-4o call that:
  1. Maintains full conversation history (OpenAI messages format)
  2. Uses function calling to interact with our slot engine and DB
  3. Generates natural French responses directly

Tools available to the LLM:
  - check_slots          → find_available_slots()
  - create_booking       → DB insert
  - cancel_booking       → DB update
  - reschedule_booking   → DB update
  - get_salon_info       → salon_info.py

Falls back gracefully to the legacy intent→handler pipeline if OpenAI
is not configured or any call fails.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Booking, BookingStatus, Employee, EmployeeCompetency, Service
from app.salon_info import get_info_response
from app.slot_engine import find_available_slots, validate_booking_request

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
LLM_TIMEOUT = 12.0          # seconds — slightly generous for tool-call rounds
MAX_TOOL_ROUNDS = 4         # max agentic iterations per turn
MAX_HISTORY_MESSAGES = 20   # trim history beyond this to control token cost

_DATA_DIR = Path(__file__).parent.parent / "data" / "normalized"

# ── System prompt builder ────────────────────────────────────

_SERVICES_BLOCK: str | None = None
_EMPLOYEES_BLOCK: str | None = None


def _services_block() -> str:
    global _SERVICES_BLOCK
    if _SERVICES_BLOCK:
        return _SERVICES_BLOCK
    try:
        with open(_DATA_DIR / "services.json", encoding="utf-8") as f:
            data = json.load(f)
        lines: list[str] = []
        for cat in data.get("categories", []):
            for svc in cat.get("services", []):
                genre_tag = f" [{svc['genre']}]" if svc.get("genre") not in ("mixte", None) else ""
                lines.append(
                    f"  {svc['id']}: {svc['label']}{genre_tag}"
                    f" — {svc['prix_eur']}€ — {svc['duree_min']}min"
                )
        _SERVICES_BLOCK = "\n".join(lines)
    except Exception as exc:
        logger.warning("services block load failed: %s", exc)
        _SERVICES_BLOCK = "  (catalogue indisponible)"
    return _SERVICES_BLOCK


def _employees_block() -> str:
    global _EMPLOYEES_BLOCK
    if _EMPLOYEES_BLOCK:
        return _EMPLOYEES_BLOCK
    try:
        with open(_DATA_DIR / "employees.json", encoding="utf-8") as f:
            data = json.load(f)
        lines: list[str] = []
        for emp in data.get("employees", []):
            spec = " | ".join(emp.get("specialites", [])[:2])
            lines.append(
                f"  {emp['id']} — {emp['prenom']} {emp['nom']}"
                f" ({emp['niveau']}) : {emp['role']}"
            )
            if spec:
                lines.append(f"    ↳ {spec}")
        _EMPLOYEES_BLOCK = "\n".join(lines)
    except Exception as exc:
        logger.warning("employees block load failed: %s", exc)
        _EMPLOYEES_BLOCK = "  (équipe indisponible)"
    return _EMPLOYEES_BLOCK


def build_system_prompt(today: str | None = None) -> str:
    """Build the full system prompt, injecting the current date."""
    today_str = today or date.today().isoformat()
    return f"""Tu es Marine, la réceptionniste IA de Maison Éclat, un salon de coiffure haut de gamme parisien.

SALON
- Adresse : 42 rue des Petits-Champs, 75002 Paris (quartier Palais-Royal / Vivienne)
- Tél : 01 42 60 74 28 | Email : contact@maison-eclat.fr | Instagram : @maison.eclat.paris
- Horaires : mardi-mercredi 9h-19h, jeudi 10h-20h, vendredi 9h-20h, samedi 9h-18h. Fermé dimanche et lundi.
- Aujourd'hui : {today_str}

RÈGLES CONVERSATIONNELLES (CRITIQUES — tu parles au téléphone)
- Réponses COURTES : 1 à 3 phrases max. Jamais de listes à puces.
- Une seule question à la fois. Ne demande pas plusieurs informations en même temps.
- Utilise toujours check_slots AVANT de demander les informations du client (nom, téléphone).
- Si check_slots confirme une disponibilité, ALORS demande prénom + nom + numéro de téléphone.
- Avant d'appeler create_booking, confirme explicitement le service, la date ET l'heure réelle du créneau disponible — même si le client a mentionné une heure différente.
- Si le client mentionne un coiffeur préféré, passe l'employee_id dans check_slots.
- Ne mentionne JAMAIS les identifiants techniques (service_id, employee_id) au client.
- Quand tu cites un créneau, mentionne toujours le jour ET l'heure (ex: "mardi 18 mars à 9 heures").
- Pour les questions sur les produits, l'équipe ou les services, appelle get_salon_info plutôt que de répondre de mémoire.
- Sois chaleureuse, naturelle et professionnelle. Tutoiement interdit, utilise "vous".
- Si le client demande un créneau le matin, passe time_to:"12:00" à check_slots.
- Si le client demande un créneau l'après-midi, passe time_from:"13:00" à check_slots.
- check_slots scanne automatiquement jusqu'à 14 jours en avant si aucun créneau ne correspond à la contrainte horaire — tu n'as pas besoin de rappeler l'outil plusieurs fois pour ça.
- Pour une coloration, un balayage ou des mèches, demande toujours la longueur des cheveux (courts, mi-longs ou longs) AVANT d'appeler check_slots — le prix et la durée varient fortement.
- Si le client demande un service que tu ne reconnais pas dans le catalogue ci-dessous, ne l'invente pas. Dis-lui que ce service n'est pas proposé et oriente-le vers les prestations proches du catalogue.
- Quand un outil retourne un message d'erreur ou d'échec, reformule-le toujours de façon naturelle et empathique — ne répète jamais le message brut.

ÉQUIPE (employee_id → profil)
{_employees_block()}

CATALOGUE (service_id → label — prix — durée)
{_services_block()}

OUTILS
- check_slots : vérifie les créneaux libres (service, date, employee optionnel)
- create_booking : crée un rendez-vous confirmé (après vérif et collecte des infos client)
- cancel_booking : annule un rendez-vous existant
- reschedule_booking : déplace un rendez-vous à une nouvelle date/heure
- get_salon_info : répond aux questions générales sur le salon"""


# ── Tool definitions ─────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "check_slots",
            "description": (
                "Vérifie les créneaux disponibles pour une prestation. "
                "Appelle toujours cet outil avant de confirmer une disponibilité. "
                "Si aucun créneau ne correspond à la contrainte horaire à la date demandée, "
                "l'outil scanne automatiquement les 14 jours suivants."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_id": {
                        "type": "string",
                        "description": "ID exact du service dans le catalogue (ex: coupe_femme_mi_long)",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date de départ souhaitée au format YYYY-MM-DD",
                    },
                    "employee_id": {
                        "type": "string",
                        "description": (
                            "ID de l'employé préféré si le client en a mentionné un. "
                            "emp_01=Sophie, emp_02=Karim, emp_03=Léa, emp_04=Hugo, emp_05=Amira"
                        ),
                    },
                    "time_from": {
                        "type": "string",
                        "description": (
                            "Heure minimale souhaitée au format HH:MM. "
                            "Utilise '13:00' quand le client veut un créneau l'après-midi."
                        ),
                    },
                    "time_to": {
                        "type": "string",
                        "description": (
                            "Heure maximale souhaitée (exclue) au format HH:MM. "
                            "Utilise '12:00' quand le client veut un créneau le matin."
                        ),
                    },
                },
                "required": ["service_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Crée un rendez-vous confirmé. "
                "N'appeler qu'après check_slots et après avoir collecté nom + téléphone du client."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_id": {"type": "string", "description": "ID du service"},
                    "employee_id": {
                        "type": "string",
                        "description": "ID de l'employé (issu du résultat de check_slots)",
                    },
                    "date": {"type": "string", "description": "Date au format YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Heure au format HH:MM"},
                    "client_name": {
                        "type": "string",
                        "description": "Prénom et nom complet du client",
                    },
                    "client_phone": {
                        "type": "string",
                        "description": "Numéro de téléphone du client (optionnel mais recommandé)",
                    },
                },
                "required": ["service_id", "employee_id", "date", "time", "client_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Annule un rendez-vous existant par son numéro.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "integer",
                        "description": "Numéro du rendez-vous à annuler",
                    },
                },
                "required": ["booking_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": "Déplace un rendez-vous existant à une nouvelle date et heure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "integer",
                        "description": "Numéro du rendez-vous à déplacer",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "Nouvelle date au format YYYY-MM-DD",
                    },
                    "new_time": {
                        "type": "string",
                        "description": "Nouvelle heure au format HH:MM",
                    },
                },
                "required": ["booking_id", "new_date", "new_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_salon_info",
            "description": (
                "Répond aux questions générales sur le salon : adresse, horaires, tarifs, "
                "équipe, paiement, parking, produits, politique d'annulation, WiFi, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "enum": [
                            "address", "hours", "price", "team", "payment",
                            "policy", "parking", "products", "contact",
                            "faq_wifi", "faq_animals", "faq_loyalty", "faq_gift", "services",
                        ],
                        "description": "Sujet de la question",
                    },
                },
                "required": ["topic"],
            },
        },
    },
]


# ── Tool execution ────────────────────────────────────────────

async def _exec_check_slots(args: dict, db: AsyncSession) -> str:
    service_id = args.get("service_id", "")
    date_str = args.get("date", "")
    employee_id = args.get("employee_id")
    time_from: str | None = args.get("time_from")  # e.g. "13:00" for afternoon
    time_to: str | None = args.get("time_to")      # e.g. "12:00" for morning only

    try:
        start_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return f"Format de date invalide : '{date_str}'. Utilise YYYY-MM-DD."

    svc = await db.get(Service, service_id)
    if svc is None:
        result = await db.execute(
            select(Service).where(Service.category_id == service_id).limit(3)
        )
        similar = result.scalars().all()
        if similar:
            suggestions = ", ".join(f"{s.id} ({s.label})" for s in similar)
            return f"Service '{service_id}' inconnu. Services proches : {suggestions}"
        return f"Service '{service_id}' introuvable dans le catalogue."

    def _filter(slots: list) -> list:
        """Keep only slots within the requested time window."""
        if not time_from and not time_to:
            return slots
        out = []
        for s in slots:
            t = s["start"].split("T")[1][:5]  # "HH:MM"
            if time_from and t < time_from:
                continue
            if time_to and t >= time_to:
                continue
            out.append(s)
        return out

    # With a time constraint, scan up to 14 days forward to find the first match.
    # Without a constraint, only check the requested date (original behaviour).
    scan_days = 14 if (time_from or time_to) else 1
    last_avail: dict = {}

    for offset in range(scan_days):
        check_date = start_date + timedelta(days=offset)
        avail = await find_available_slots(
            session=db,
            service_id=service_id,
            target_date=check_date,
            preferred_employee_id=employee_id,
        )
        last_avail = avail
        filtered = _filter(avail["slots"])
        if filtered:
            top = filtered[:5]
            date_label = check_date.isoformat()
            slots_info = " | ".join(
                f"{date_label} à {s['start'].split('T')[1][:5]} avec {s['employee']['prenom']}"
                f" (id={s['employee']['id']})"
                for s in top
            )
            skip_note = (
                f" (aucun créneau correspondant le {date_str}, premier match :)"
                if offset > 0 else ""
            )
            return (
                f"{len(filtered)} créneau(x) disponible(s) le {date_label}{skip_note} pour "
                f"{svc.label} ({svc.prix_eur}€, {svc.duree_min}min). "
                f"Options : {slots_info}"
            )

    # Nothing found across all scanned days
    if time_from or time_to:
        window = f"après {time_from}" if time_from else f"avant {time_to}"
        return (
            f"Aucun créneau disponible {window} pour {svc.label} "
            f"dans les {scan_days} prochains jours (à partir du {date_str})."
        )

    # No time filter, no slots on target date → return alternatives from slot engine
    alts = last_avail.get("alternatives", [])[:3]
    if alts:
        alt_text = " | ".join(
            f"{a['start'][:10]} à {a['start'].split('T')[1][:5]} avec {a['employee']['prenom']}"
            for a in alts
        )
        return (
            f"Aucun créneau le {date_str} pour {svc.label}. "
            f"Créneaux proches disponibles : {alt_text}"
        )
    return f"Aucun créneau disponible le {date_str} pour {svc.label}."


async def _exec_create_booking(args: dict, db: AsyncSession) -> str:
    service_id = args.get("service_id", "")
    employee_id = args.get("employee_id", "")
    date_str = args.get("date", "")
    time_str = args.get("time", "")
    client_name = args.get("client_name", "Client vocal")
    client_phone = args.get("client_phone")

    try:
        h, m = time_str.split(":")
        start_dt = datetime.combine(
            date.fromisoformat(date_str),
            datetime.min.time().replace(hour=int(h), minute=int(m)),
        )
    except (ValueError, TypeError, AttributeError):
        return f"Date ou heure invalide : '{date_str} {time_str}'."

    # Verify competency
    comp = await db.execute(
        select(EmployeeCompetency)
        .where(EmployeeCompetency.employee_id == employee_id)
        .where(EmployeeCompetency.service_id == service_id)
    )
    if comp.scalars().first() is None:
        emp = await db.get(Employee, employee_id)
        emp_name = emp.prenom if emp else employee_id
        return f"{emp_name} n'est pas habilité(e) pour ce service. Choisis un autre coiffeur."

    ok, message, end_time = await validate_booking_request(db, service_id, employee_id, start_dt)
    if not ok:
        return f"Créneau non disponible : {message}"

    svc = await db.get(Service, service_id)
    emp = await db.get(Employee, employee_id)

    booking = Booking(
        client_name=client_name,
        client_phone=client_phone,
        service_id=service_id,
        employee_id=employee_id,
        start_time=start_dt,
        end_time=end_time,
        status=BookingStatus.confirmed,
    )
    db.add(booking)
    await db.commit()
    await db.refresh(booking)

    svc_label = svc.label if svc else service_id
    emp_name = f"{emp.prenom} {emp.nom}" if emp else employee_id
    phone_info = f" — tél : {client_phone}" if client_phone else ""
    return (
        f"Rendez-vous #{booking.id} confirmé : {svc_label} "
        f"le {date_str} à {time_str} avec {emp_name}. "
        f"Client : {client_name}{phone_info}."
    )


async def _exec_cancel_booking(args: dict, db: AsyncSession) -> str:
    booking_id = args.get("booking_id")
    if not booking_id:
        return "Le numéro de rendez-vous est manquant. Demande-le au client."

    booking = await db.get(Booking, int(booking_id))
    if not booking:
        return (
            f"Aucun rendez-vous trouvé avec le numéro {booking_id}. "
            "Vérifie le numéro avec le client."
        )
    if booking.status == BookingStatus.cancelled:
        return f"Le rendez-vous numéro {booking_id} a déjà été annulé précédemment."

    booking.status = BookingStatus.cancelled
    await db.commit()
    return f"Rendez-vous numéro {booking_id} annulé avec succès."


async def _exec_reschedule_booking(args: dict, db: AsyncSession) -> str:
    booking_id = args.get("booking_id")
    new_date = args.get("new_date", "")
    new_time = args.get("new_time", "")

    if not booking_id:
        return "Le numéro de rendez-vous est manquant. Demande-le au client."

    result = await db.execute(
        select(Booking)
        .options(selectinload(Booking.service), selectinload(Booking.employee))
        .where(Booking.id == int(booking_id))
    )
    booking = result.scalars().first()

    if not booking:
        return (
            f"Aucun rendez-vous trouvé avec le numéro {booking_id}. "
            "Vérifie le numéro avec le client."
        )
    if booking.status != BookingStatus.confirmed:
        status_fr = {"cancelled": "annulé", "completed": "terminé", "no_show": "absent"}.get(
            booking.status.value, booking.status.value
        )
        return (
            f"Le rendez-vous numéro {booking_id} ne peut pas être déplacé "
            f"car il est {status_fr}."
        )

    try:
        h, m = new_time.split(":")
        new_start = datetime.combine(
            date.fromisoformat(new_date),
            datetime.min.time().replace(hour=int(h), minute=int(m)),
        )
    except (ValueError, TypeError):
        return f"Date ou heure invalide : '{new_date} {new_time}'."

    ok, message, end_time = await validate_booking_request(
        db, booking.service_id, booking.employee_id, new_start,
        exclude_booking_id=int(booking_id),
    )
    if not ok:
        return f"Créneau non disponible : {message}"

    booking.start_time = new_start
    booking.end_time = end_time
    await db.commit()

    emp_name = booking.employee.prenom if booking.employee else ""
    svc_label = booking.service.label if booking.service else ""
    return (
        f"Rendez-vous #{booking_id} déplacé : {svc_label} "
        f"le {new_date} à {new_time} avec {emp_name}. C'est confirmé."
    )


async def _execute_tool(name: str, args: dict, db: AsyncSession) -> str:
    """Dispatch a tool call and return a plain-text result for the LLM."""
    try:
        if name == "check_slots":
            return await _exec_check_slots(args, db)
        if name == "create_booking":
            return await _exec_create_booking(args, db)
        if name == "cancel_booking":
            return await _exec_cancel_booking(args, db)
        if name == "reschedule_booking":
            return await _exec_reschedule_booking(args, db)
        if name == "get_salon_info":
            return get_info_response(args.get("topic"))
        return f"Outil inconnu : {name}"
    except Exception as exc:
        logger.error("Tool '%s' raised: %s", name, exc)
        return f"Erreur interne lors de l'exécution de {name}."


# ── OpenAI client ─────────────────────────────────────────────

async def _call_openai(messages: list[dict], today: str | None = None) -> dict:
    """
    Make one OpenAI chat completion request with tools enabled.
    Returns the raw assistant message dict from choices[0].message.
    """
    payload: dict = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(today)},
            *messages,
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.4,
        "max_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(OPENAI_CHAT_URL, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]


# ── Public API ────────────────────────────────────────────────

def is_available() -> bool:
    """Return True if LLM-driven conversation is configured and enabled."""
    return bool(settings.OPENAI_API_KEY) and settings.LLM_PROVIDER == "openai"


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep only the most recent MAX_HISTORY_MESSAGES messages."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages
    return messages[-MAX_HISTORY_MESSAGES:]


async def llm_turn(
    messages: list[dict],
    user_text: str,
    db: AsyncSession,
    today: str | None = None,
) -> tuple[str, list[dict], str | None]:
    """
    Process one user turn through GPT-4o with function calling.

    Args:
        messages:   Conversation history so far (OpenAI format, no system message).
        user_text:  Transcribed user utterance.
        db:         Async DB session for tool execution.
        today:      ISO date to inject into system prompt (defaults to today).

    Returns:
        (response_text, updated_messages, action_taken)
        - response_text:    Natural French text, ready for TTS.
        - updated_messages: Full updated history (save to ConversationState.messages).
        - action_taken:     Name of last tool called, or None if no tool was used.
    """
    today = today or date.today().isoformat()
    working: list[dict] = trim_history(list(messages)) + [
        {"role": "user", "content": user_text}
    ]
    action_taken: str | None = None

    for _ in range(MAX_TOOL_ROUNDS):
        msg = await _call_openai(working, today=today)
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            # Final natural-language response
            response_text: str = msg.get("content") or (
                "Je suis désolée, je n'ai pas compris. Pouvez-vous répéter ?"
            )
            working.append({"role": "assistant", "content": response_text})
            return response_text, working, action_taken

        # Append assistant turn with tool calls
        working.append({
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": tool_calls,
        })

        # Execute every tool call in this round
        for tc in tool_calls:
            fn_name: str = tc["function"]["name"]
            action_taken = fn_name
            try:
                fn_args: dict = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                fn_args = {}

            logger.info("llm_tool: %s(%s)", fn_name, list(fn_args.keys()))
            result = await _execute_tool(fn_name, fn_args, db)
            logger.debug("llm_tool result: %s → %s", fn_name, result[:120])

            working.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    # Exceeded MAX_TOOL_ROUNDS — force a final response without tools
    logger.warning("llm_turn: MAX_TOOL_ROUNDS reached, forcing final response")
    payload_no_tools: dict = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": build_system_prompt(today)},
            *working,
        ],
        "temperature": 0.4,
        "max_tokens": 350,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(OPENAI_CHAT_URL, json=payload_no_tools, headers=headers)
    final_text = resp.json()["choices"][0]["message"].get(
        "content", "Désolée, une erreur est survenue."
    )
    working.append({"role": "assistant", "content": final_text})
    return final_text, working, action_taken
