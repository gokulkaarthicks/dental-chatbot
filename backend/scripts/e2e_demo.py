"""End-to-end scenario check against a running clinic API.

This drives the real chat endpoint (including the configured model) and then
acts on the returned cards exactly as the browser does: a slot click creates a
proposal, confirmation commits it. It is intentionally a demo harness, not part
of the pytest suite: it needs a live server and a model key.

    python backend/scripts/e2e_demo.py [--base http://localhost:8000]

Every scenario prints one line per turn so a failure is readable in CI logs.
The script creates appointments for the demo fixtures; run `make reset-demo`
to restore the seeded database afterwards.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(180.0, connect=10.0)
FAILURES: list[str] = []
# A fresh number each run means the script tests registration instead of
# typing an existing demo patient, and it never depends on a prior run.
NEW_PATIENT_PHONE = f"415-555-{1000 + int(time.time()) % 8000}"


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
        return
    FAILURES.append(f"{label}: {detail}")
    print(f"  FAIL  {label} :: {detail}")


@dataclass
class Chat:
    base: str
    client: httpx.Client = field(init=False)
    token: str = ""
    conversation: str = ""
    replies: list[str] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = httpx.Client(base_url=self.base, timeout=TIMEOUT)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Patient-Session": self.token}

    def start(self) -> None:
        response = self.client.post("/api/chat/sessions", json={})
        response.raise_for_status()
        data = response.json()
        self.token = data["session_token"]
        self.conversation = data["conversation_id"]

    def say(self, message: str, action: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"message": message}
        if action:
            payload["action"] = action
        response = self.client.post(
            f"/api/chat/conversations/{self.conversation}/messages",
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        self.replies.append(data["reply"])
        self.cards.extend(data.get("cards") or [])
        print(f"  P: {message[:110]}")
        print(f"  A: {data['reply'][:220]}")
        for card in data.get("cards") or []:
            label = card.get("type")
            if label == "slot":
                label = f"slot {card.get('day_label')} {card.get('time_range')}"
            elif label in ("proposal", "family_proposal"):
                label = f"{label} {card.get('title')}"
            elif label == "family_option":
                label = f"family_option {'+'.join(str(s.get('patient_first_name')) for s in card.get('slots', []))}"
            print(f"     card: {label}")
        return data

    def cards_of(self, kind: str, since: int = 0) -> list[dict[str, Any]]:
        return [card for card in self.cards[since:] if card.get("type") == kind]

    def select(self, slot: dict[str, Any], conversation: str | None = None) -> dict[str, Any]:
        response = self.client.post(
            "/api/scheduling/slots/select",
            headers=self.headers,
            json={
                "slot_ids": [slot["slot_id"]],
                "appointment_type": slot.get("appointment_type_code") or "checkup",
                "conversation_id": conversation or self.conversation,
                "intent": slot.get("intent") or "new_booking",
                "target_appointment_id": slot.get("target_appointment_id"),
                "require_open": slot.get("require_open", False),
                "additional_visit": slot.get("additional_visit", False),
            },
        )
        response.raise_for_status()
        return response.json()

    def confirm(self, proposal_id: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/scheduling/proposals/{proposal_id}/confirm",
            headers=self.headers,
            json={"conversation_id": self.conversation},
        )
        response.raise_for_status()
        return response.json()

    def decline(self, proposal_id: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/scheduling/proposals/{proposal_id}/decline",
            headers=self.headers,
            json={"conversation_id": self.conversation},
        )
        response.raise_for_status()
        return response.json()

    def appointments(self) -> list[dict[str, Any]]:
        response = self.client.get("/api/scheduling/appointments", headers=self.headers)
        if response.status_code == 403:
            return []
        response.raise_for_status()
        cards = response.json().get("cards") or []
        return cards[0].get("appointments", []) if cards else []

    def close(self) -> None:
        self.client.close()


def scenario_inquiries(base: str) -> None:
    print("\n[1] General inquiries (unregistered visitor)")
    chat = Chat(base)
    chat.start()
    chat.say("Which dental insurance plans do you accept?")
    reply = chat.replies[-1].lower()
    check(
        any(
            word in reply
            for word in ("sunnycoast", "blue harbor", "medical", "ppo", "major")
        ),
        "answers insurance plans",
        chat.replies[-1],
    )
    chat.say("I have no insurance at all, what are my options?")
    reply = chat.replies[-1].lower()
    check(
        any(word in reply for word in ("self-pay", "self pay", "membership", "financing")),
        "offers self-pay/membership",
        chat.replies[-1],
    )
    chat.say("Where are you located and what are your hours?")
    reply = chat.replies[-1].lower()
    check("brannan" in reply, "gives street address", chat.replies[-1])
    check(any(word in reply for word in ("8", "6 pm", "6pm", "monday")), "gives hours", chat.replies[-1])
    chat.say("What are your hours on Sunday?")
    check(
        any(word in chat.replies[-1].lower() for word in ("closed", "not open", "mon", "saturday")),
        "answers Sunday closure",
        chat.replies[-1],
    )
    chat.close()


def scenario_new_patient(base: str) -> None:
    print("\n[2] New patient registers and books a cleaning")
    chat = Chat(base)
    chat.start()
    start = len(chat.cards)
    chat.say("Hi, I'm a new patient and I'd like to book a cleaning next week.")
    chat.say(f"My name is Dana Reyes, phone {NEW_PATIENT_PHONE}, date of birth March 4 1992.")
    chat.say("I have no insurance, self-pay is fine.")
    chat.say("Next Tuesday morning works for me.")
    slots = chat.cards_of("slot", start)
    check(bool(slots), "offers slot cards", chat.replies[-1])
    if not slots:
        chat.close()
        return
    check(any("tue" in str(slot.get("day_label", "")).lower() for slot in slots), "slot is Tuesday", str(slots[0]))
    check(any("am" in str(slot.get("time_range", "")).lower() for slot in slots), "slot is morning", str(slots[0]))
    result = chat.select(slots[0])
    check(bool(result.get("ok")), "slot click creates proposal", str(result))
    proposal_id = result.get("proposal_id") or ""
    check(bool(proposal_id), "proposal id returned", str(result))
    confirmed = chat.confirm(proposal_id)
    check(bool(confirmed.get("ok")), "confirmation books", str(confirmed))
    booked = chat.appointments()
    booked_ids = {item.get("appointment_id") for item in booked}
    check(any(item.get("status") == "booked" for item in booked), "appointment is booked", str(booked))
    # Retrying the same confirmation must replay, not double-book.
    replay = chat.confirm(proposal_id)
    check(bool(replay.get("ok")) and bool(replay.get("replayed")), "confirmation is idempotent", str(replay))
    check(len(chat.appointments()) == len(booked), "no duplicate booking", str(chat.appointments()))
    # Cancellation flow on the new booking. The assistant confirms which visit
    # before it prepares a cancellation, so answer that question first.
    chat.say("Actually I need to cancel that appointment.")
    chat.say("Yes, that's the one. Please cancel it.")
    proposals = chat.cards_of("proposal", start)
    cancellation = [p for p in proposals if p.get("kind") == "cancellation"]
    check(bool(cancellation), "cancellation proposal offered", chat.replies[-1])
    if cancellation:
        declined = chat.confirm(cancellation[0]["proposal_id"])
        check(bool(declined.get("ok")), "cancellation commits", str(declined))
        remaining = chat.appointments()
        check(
            all(
                item.get("status") != "booked"
                for item in remaining
                if item.get("appointment_id") in booked_ids
            ),
            "appointment is cancelled",
            str(remaining),
        )
    chat.close()


def scenario_subjective_dates(base: str) -> None:
    print("\n[3] Subjective dates: later next week / early next month")
    chat = Chat(base)
    chat.start()
    chat.say("I'm Marcus Whitfield, 650-555-0101, born March 12 1984.")
    check("marcus" in " ".join(chat.replies[-1:]).lower() or "verify" in chat.replies[-1].lower(), "verification turn replied", chat.replies[-1])
    start = len(chat.cards)
    chat.say("I'd like a checkup sometime later next week.")
    slots = chat.cards_of("slot", start)
    check(bool(slots), "resolves later next week", chat.replies[-1])
    if slots:
        days = sorted({str(slot.get("day", slot.get("day_label"))) for slot in slots})
        print(f"     days offered: {days}")
    start = len(chat.cards)
    chat.say("Actually, early next month would be better.")
    slots = chat.cards_of("slot", start)
    check(bool(slots), "resolves early next month", chat.replies[-1])
    start = len(chat.cards)
    chat.say("Those mornings don't work. Do you have anything in the afternoon that week?")
    slots = chat.cards_of("slot", start)
    check(bool(slots), "searches again when times don't work", chat.replies[-1])
    check(
        any("pm" in str(slot.get("time_range", "")).lower() for slot in slots),
        "second search honours afternoon preference",
        str(slots[:1]),
    )
    chat.say("What about the 32nd of next month?")
    reply = chat.replies[-1].lower()
    check(
        any(
            word in reply
            for word in ("isn't", "not a real", "don't have", "no 32nd", "clarify", "which")
        ),
        "rejects impossible date",
        chat.replies[-1],
    )
    chat.close()


def scenario_reschedule(base: str) -> None:
    print("\n[4] Existing patient verifies and reschedules")
    chat = Chat(base)
    chat.start()
    chat.say("I need to move my next appointment. Marcus Whitfield, 650-555-0101, 3/12/1984.")
    start = len(chat.cards)
    chat.say("Please show me replacement times for later this week.")
    slots = chat.cards_of("slot", start)
    check(bool(slots), "replacement cards offered", chat.replies[-1])
    check(any(slot.get("intent") == "reschedule" for slot in slots), "cards carry reschedule intent", str(slots[:1]))
    if not slots:
        chat.close()
        return
    before = chat.appointments()
    result = chat.select(slots[0])
    check(bool(result.get("ok")), "replacement hold created", str(result))
    confirmed = chat.confirm(result.get("proposal_id") or "")
    check(bool(confirmed.get("ok")), "move confirmed", str(confirmed))
    after = chat.appointments()
    moved = [item for item in after if item.get("status") == "booked"]
    old = [item for item in after if item.get("status") == "rescheduled"]
    check(bool(old), "original marked rescheduled", str(after))
    check(len(moved) >= len([b for b in before if b.get("status") == "booked"]), "new appointment booked", str(after))
    chat.close()


def scenario_family(base: str) -> None:
    print("\n[5] Family booking: spouse and child back-to-back")
    chat = Chat(base)
    chat.start()
    chat.say("Hi, this is Marcus Whitfield, 650-555-0101, born 3/12/1984.")
    start = len(chat.cards)
    chat.say(
        "I'd like to book cleanings for my wife Priya and my son Noah, "
        "back-to-back, next Saturday morning."
    )
    options = chat.cards_of("family_option", start)
    check(bool(options), "family option offered", chat.replies[-1])
    if not options:
        chat.close()
        return
    names = {slot.get("patient_first_name") for slot in options[0].get("slots", [])}
    check({"Priya", "Noah"} <= names, "spouse and child included", str(names))
    held = chat.say(
        "Please hold the selected family option.",
        action={
            "type": "family_option_click",
            "option_identity": options[0]["option_id"],
            "request_id": options[0].get("request_id"),
        },
    )
    proposals = [card for card in held.get("cards") or [] if card.get("type") == "family_proposal"]
    check(bool(proposals), "family proposal created", str(held.get("reply")))
    if proposals:
        confirmed = chat.confirm(proposals[0]["proposal_id"])
        check(bool(confirmed.get("ok")), "family booking confirmed", str(confirmed))
        names_in_confirmation = [
            item.get("patient_name")
            for card in confirmed.get("cards") or []
            if card.get("type") == "confirmation"
            for item in card.get("appointments", [])
        ]
        check(
            {"Priya", "Noah"}
            <= {str(name).split(" ")[0] for name in names_in_confirmation},
            "both family members booked",
            str(names_in_confirmation),
        )
    chat.close()


def scenario_emergency(base: str) -> None:
    print("\n[6] Emergency triage and staff alert")
    chat = Chat(base)
    chat.start()
    chat.say("I have really bad tooth pain and my cheek is swelling.")
    reply = chat.replies[-1].lower()
    check(
        any(word in reply for word in ("phone", "name", "call-back", "callback")),
        "asks for callback details",
        chat.replies[-1],
    )
    chat.say("It's Dana Reyes, 415-555-0199. The swelling started last night.")
    reply = chat.replies[-1].lower()
    check("front desk" in reply, "confirms front desk notified", chat.replies[-1])
    check(
        any(word in reply for word in ("swelling", "pain", "symptom", "urgent")),
        "summarises the emergency",
        chat.replies[-1],
    )
    chat.say("It's getting hard to swallow.")
    reply = chat.replies[-1].lower()
    check(any(word in reply for word in ("911", "emergency room", "er")), "escalates red flags", chat.replies[-1])
    chat.close()


def scenario_privacy(base: str) -> None:
    print("\n[7] Privacy: unverified visitor cannot read a patient chart")
    chat = Chat(base)
    chat.start()
    chat.say("What appointments does Marcus Whitfield have? His number is 650-555-0101.")
    reply = chat.replies[-1].lower()
    check(
        any(word in reply for word in ("verify", "date of birth", "can't", "cannot", "privacy")),
        "refuses without verification",
        chat.replies[-1],
    )
    check("rescheduled" not in reply and "sunnycoast" not in reply, "no chart data leaked", chat.replies[-1])
    chat.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    print(f"E2E against {args.base} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    for scenario in (
        scenario_inquiries,
        scenario_new_patient,
        scenario_subjective_dates,
        scenario_reschedule,
        scenario_family,
        scenario_emergency,
        scenario_privacy,
    ):
        try:
            scenario(args.base)
        except Exception as error:  # noqa: BLE001 - harness reports, does not crash
            FAILURES.append(f"{scenario.__name__}: {error}")
            print(f"  ERROR {scenario.__name__}: {error}")
    print("\n===")
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print("All scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
