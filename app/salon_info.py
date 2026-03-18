"""
Salon information module — loads salon.json and generates voice-friendly responses.

Used by the get_info intent handler to answer client questions about the salon:
address, opening hours, contact, pricing, team, payment methods, cancellation
policy, parking, products, and FAQ items (WiFi, animals, loyalty card, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

_DATA_PATH = Path(__file__).parent.parent / "data" / "normalized" / "salon.json"
_SALON: dict | None = None


def _salon() -> dict:
    global _SALON
    if _SALON is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _SALON = json.load(f)
    return _SALON


# ── Topic → response builders ────────────────────────────────

def _resp_address() -> str:
    s = _salon()
    adr = s["adresse"]
    metro = s["acces"]["metro"][0]
    return (
        f"Maison Éclat est situé au {adr['rue']}, "
        f"{adr['code_postal']} Paris, {adr['arrondissement']}, "
        f"quartier {adr['quartier']}. "
        f"Station de métro la plus proche : {metro}."
    )


def _resp_hours() -> str:
    h = _salon()["horaires"]
    return (
        "Nous sommes ouverts du mardi au samedi. "
        f"Mardi et mercredi de {h['mardi']['debut']} à {h['mardi']['fin']}, "
        f"jeudi de {h['jeudi']['debut']} à {h['jeudi']['fin']}, "
        f"vendredi de {h['vendredi']['debut']} à {h['vendredi']['fin']}, "
        f"samedi de {h['samedi']['debut']} à {h['samedi']['fin']}. "
        "Nous sommes fermés le dimanche et le lundi."
    )


def _resp_contact() -> str:
    c = _salon()["contact"]
    return (
        f"Vous pouvez nous joindre par téléphone au {c['telephone']}, "
        f"par email à {c['email']}, "
        f"ou sur Instagram {c['instagram']}. "
        "Les réservations se font de préférence par téléphone ou sur notre site."
    )


def _resp_price() -> str:
    faq = _salon()["faq"]
    return (
        f"{faq['prix_moyen']} "
        "Un devis gratuit est proposé avant toute prestation technique importante."
    )


def _resp_team() -> str:
    eq = _salon()["equipe"]
    langues = ", ".join(eq["langues"])
    return (
        f"Notre équipe comprend {eq['taille']} professionnels : {eq['description']}. "
        f"Nous parlons {langues}."
    )


def _resp_payment() -> str:
    p = _salon()["paiements"]
    moyens = ", ".join(p["acceptes"][:4])
    return (
        f"Nous acceptons : {moyens}. "
        f"{p['acompte']}"
    )


def _resp_policy() -> str:
    pol = _salon()["politique"]
    return (
        "Nous travaillons uniquement sur rendez-vous. "
        f"{pol['annulation']['description']} "
        f"{pol['retard']}"
    )


def _resp_parking() -> str:
    acc = _salon()["acces"]
    faq = _salon()["faq"]
    metros = "; ".join(acc["metro"][:2])
    return (
        f"{faq['parking']} "
        f"En métro : {metros}. "
        f"Vélib' : {acc['velo']}."
    )


def _resp_products() -> str:
    prods = _salon()["produits"]
    listed = ", ".join(prods[:4])
    certs = "; ".join(_salon()["certifications"][:2])
    return (
        f"Nous utilisons des produits professionnels haut de gamme : {listed}. "
        f"Certifications : {certs}."
    )


def _resp_services() -> str:
    specs = _salon()["specialites"]
    listed = " | ".join(specs[:3])
    return (
        f"Maison Éclat propose : {listed}. "
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
    "faq_wifi": lambda: _salon()["faq"]["wifi"],
    "faq_animals": lambda: _salon()["faq"]["animaux"],
    "faq_loyalty": lambda: _salon()["faq"]["carte_fidelite"],
    "faq_gift": lambda: _salon()["faq"]["bon_cadeau"],
}


# ── Public API ───────────────────────────────────────────────

def get_info_response(info_topic: str | None, raw_text: str = "") -> str:
    """
    Return a voice-friendly response for a get_info intent.

    Args:
        info_topic: Resolved topic key (from entity extraction or LLM).
                    One of: address, hours, contact, price, team, payment,
                    policy, parking, products, services, faq_wifi, faq_animals,
                    faq_loyalty, faq_gift.
        raw_text:   Original user utterance (used for heuristic fallback when
                    info_topic is None).

    Returns:
        A French, voice-friendly string ready for TTS.
    """
    topic = info_topic or _guess_topic(raw_text.lower())
    handler = _TOPIC_HANDLERS.get(topic)
    if handler:
        return handler()
    return _resp_general()


def _resp_general() -> str:
    s = _salon()
    return (
        f"Maison Éclat est un salon de coiffure haut de gamme situé au "
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
