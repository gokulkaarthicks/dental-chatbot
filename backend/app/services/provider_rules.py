"""Provider-side visibility rules (specialties, emergency eligibility).

Kept separate from the search engine so the rules that decide *who may be
shown* can be unit-tested and audited without touching booking code.
"""

from __future__ import annotations

from app.models import AppointmentType, Provider

EMERGENCY_SPECIALTY = "emergency care"
PEDIATRIC_SPECIALTY = "pediatric dentistry"

# Emergency visits are offered only to providers configured for urgent care.
# General dentistry is deliberately excluded: a hygiene slot is not an emergency
# appointment, and offering one would misrepresent what the patient is booking.
EMERGENCY_ELIGIBLE: frozenset[str] = frozenset({EMERGENCY_SPECIALTY})


def provider_eligible(provider: Provider, appointment_type: AppointmentType) -> bool:
    """Whether this provider may be offered for this appointment type."""
    specialties = {name.strip().lower() for name in provider.specialty_names}

    if appointment_type.is_emergency:
        return bool(specialties & EMERGENCY_ELIGIBLE)

    required = (appointment_type.required_specialty or "").strip().lower()
    if required and required not in specialties:
        return False
    # An emergency-only provider should not be offered for routine visits.
    if not (specialties - {EMERGENCY_SPECIALTY}):
        return False
    return True


def is_pediatric_provider(provider: Provider) -> bool:
    return PEDIATRIC_SPECIALTY in {name.strip().lower() for name in provider.specialty_names}


def emergency_provider_ids(providers: list[Provider]) -> list[int]:
    return [provider.id for provider in providers if EMERGENCY_ELIGIBLE & {s.lower() for s in provider.specialty_names}]


__all__ = [
    "EMERGENCY_ELIGIBLE",
    "EMERGENCY_SPECIALTY",
    "PEDIATRIC_SPECIALTY",
    "emergency_provider_ids",
    "is_pediatric_provider",
    "provider_eligible",
]
