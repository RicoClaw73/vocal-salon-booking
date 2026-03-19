"""
Salon information module — loads salon.json and generates voice-friendly responses.

Used by the get_info intent handler to answer client questions about the salon:
address, opening hours, contact, pricing, team, payment methods, cancellation
policy, parking, products, and FAQ items (WiFi, animals, loyalty card, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import settings

_BASE_DIR = Path(__file__).parent.parent
_DEFAULT_DATA_PATH = _BASE_DIR / "data" / "normalized" / "salon.json"

# Per-tenant salon cache: slug → salon dict
_salon_cache: dict[str, dict] = {}


def _load_salon(tenant_slug: str = "default") -> dict:
    if tenant_slug not in _salon_cache:
        tenant_path = _BASE_DIR / "data" / "tenants" / tenant_slug / "salon.json"
        path = tenant_path if tenant_path.exists() else _DEFAULT_DATA_PATH
        with open(path, encoding="utf-8") as f:
            _salon_cache[tenant_slug] = json.load(f)
    return _salon_cache[tenant_slug]


def _salon(tenant_slug: str = "default") -> dict:
    return _load_salon(tenant_slug)


# ── Topic → response builders ────────────────────────────────

def _resp_address(slug: str, salon_name: str) -> str:
    s = _salon(slug)
    adr = s["adresse"]
    metro = s["acces"]["metro"][0]
    return (
        f"{salon_name} est situé au {adr['rue']}, "
        f"{adr['code_postal']} Paris, {adr['arrondissement']}, "
        f"quartier {adr['quartier']}. "
        f"Station de métro la plus proche : {metro}."
    )


def _resp_hours(slug: str, **_) -> str:
    h = _salon(slug)["horaires"]
    return (
        "Nous sommes ouverts du mardi au samedi. "
        f"Mardi et mercredi de {h['mardi']['debut']} à {h['mardi']['fin']}, "
        f"jeudi de {h['jeudi']['debut']} à {h['jeudi']['fin']}, "
        f"vendredi de {h['vendredi']['debut']} à {h['vendredi']['fin']}, "
        f"samedi de {h['samedi']['debut']} à {h['samedi']['fin']}. "
        "Nous sommes fermés le dimanche et le lundi."
    )


def _resp_contact(slug: str, **_) -> str:
    c = _salon(slug)["contact"]
    return (
        f"Vous pouvez nous joindre par téléphone au {c['telephone']}, "
        f"par email à {c['email']}, "
        f"ou sur Instagram {c['instagram']}. "
        "Les réservations se font de préférence par téléphone ou sur notre site."
    )


def _resp_price(slug: str, **_) -> str:
    faq = _salon(slug)["faq"]
    return (
        f"{faq['prix_moyen']} "
        "Un devis gratuit est proposé avant toute prestation technique importante."
    )


def _resp_team(slug: str, **_) -> str:
    eq = _salon(slug)["equipe"]
    langues = ", ".join(eq["langues"])
    return (
        f"Notre équipe comprend {eq['taille']} professionnels : {eq['description']}. "
        f"Nous parlons {langues}."
    )


def _resp_payment(slug: str, **_) -> str:
    p = _salon(slug)["paiements"]
    moyens = ", ".join(p["acceptes"][:4])
    return (
        f"Nous acceptons : {moyens}. "
        f"{p['acompte']}"
    )


def _resp_policy(slug: str, **_) -> str:
    pol = _salon(slug)["politique"]
    return (
        "Nous travaillons uniquement sur rendez-vous. "
        f"{pol['annulation']['description']} "
        f"{pol['retard']}"
    )


def _resp_parking(slug: str, **_) -> str:
    acc = _salon(slug)["acces"]
    faq = _salon(slug)["faq"]
    metros = "; ".join(acc["metro"][:2])
    return (
        f"{faq['parking']} "
        f"En métro : {metros}. "
        f"Vélib' : {acc['velo']}."
    )


def _resp_products(slug: str, **_) -> str:
    prods = _salon(slug)["produits"]
    listed = ", ".join(prods[:4])
    certs = "; ".join(_salon(slug)["certifications"][:2])
    return (
        f"Nous utilisons des produits professionnels haut de gamme : {listed}. "
        f"Certifications : {certs}."
    )


def _resp_services(slug: str, salon_name: str) -> str:
    specs = _salon(slug)["specialites"]
    listed = " | ".join(specs[:3])
    return (
        f"{salon_name} propose : {listed}. "
        "Souhaitez-vous plus de détails sur une prestation particulière ?"
    )


_TOPIC_HANDLERS: dict[str, callable] = {
    "address": _resp_address,
    "hours": _resp_hours,
    "contact": _resp_contact,
    "price": _resp_price,
    "team": _resp_team,
    "payment": _resp_payment,
    "policy": _resp_policy,
    "parking": _resp_parking,
    "products": _resp_products,
    "services": _resp_services,
    "faq_wifi": lambda slug, **_: _salon(slug)["faq"]["wifi"],
    "faq_animals": lambda slug, **_: _salon(slug)["faq"]["animaux"],
    "faq_loyalty": lambda slug, **_: _salon(slug)["faq"]["carte_fidelite"],
    "faq_gift": lambda slug, **_: _salon(slug)["faq"]["bon_cadeau"],
}


# ── Public API ───────────────────────────────────────────────

def get_info_response(
    info_topic: str | None,
    raw_text: str = "",
    tenant_slug: str = "default",
) -> str:
    """
    Return a voice-friendly response for a get_info intent.

    Args:
        info_topic:   Resolved topic key (from entity extraction or LLM).
        raw_text:     Original user utterance (fallback heuristic).
        tenant_slug:  Tenant slug for per-tenant salon.json lookup.

    Returns:
        A French, voice-friendly string ready for TTS.
    """
    salon_name = settings.SALON_NAME

    topic = info_topic or _guess_topic(raw_text.lower())
    handler = _TOPIC_HANDLERS.get(topic)
    if handler:
        return handler(slug=tenant_slug, salon_name=salon_name)
    return _resp_general(slug=tenant_slug, salon_name=salon_name)


def _resp_general(slug: str = "default", salon_name: str = "") -> str:
    s = _salon(slug)
    name = salon_name or settings.SALON_NAME
    return (
        f"{name} est un salon de coiffure haut de gamme situé au "
        f"{s['adresse']['rue']}, Paris {s['adresse']['arrondissement']}. "
        "Nous sommes ouverts du mardi au samedi. "
        f"Pour plus d'informations, appelez-nous au {s['contact']['telephone']} "
        f"ou consultez {s['contact']['site_web']}."
    )


def _guess_topic(text: str) -> str:
    """Keyword-based topic heuristic when entity extraction produced nothing."""
    checks = [
        ("address",     ["adresse", "où", "chemin", "trouver", "situé", "localiser", "plan"]),
        ("hours",       ["horaire", "heure", "ouvert", "fermé", "ouverture", "fermeture"]),
        ("price",       ["tarif", "prix", "coût", "combien", "cher", "budget"]),
        ("team",        ["équipe", "coiffeur", "coiffeuse", "personnel", "qui travaille", "staff"]),
        ("payment",     ["paiement", "payer", "carte", "espèces", "chèque", "apple pay"]),
        ("policy",      ["annulation", "politique", "retard", "acompte", "conditions"]),
        ("parking",     ["parking", "métro", "transport", "vélib", "bus", "comment venir"]),
        ("products",    ["produit", "wella", "kérastase", "olaplex", "marque"]),
        ("contact",     ["téléphone", "numéro", "email", "instagram", "contact", "joindre"]),
        ("faq_wifi",    ["wifi", "wi-fi", "internet"]),
        ("faq_animals", ["animal", "chien", "chat"]),
        ("faq_loyalty", ["fidélité", "carte fid", "points"]),
        ("faq_gift",    ["cadeau", "bon cadeau", "gift"]),
        ("services",    ["prestation", "service", "propose", "catalogue"]),
    ]
    for topic, keywords in checks:
        if any(kw in text for kw in keywords):
            return topic
    return "general"
