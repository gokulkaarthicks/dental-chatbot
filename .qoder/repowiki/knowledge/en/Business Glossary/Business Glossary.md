---
kind: business_term
name: Business Glossary
category: business_term
scope:
    - '**'
---

### Oral-B Dental Care
- Definition：The fictional San Francisco dental practice that is the subject of this demo application. All clinic data, patients, providers, policies, and contact details are seeded fixtures -- not real patient records.
- Aliases：the practice、clinic

### receptionist
- Definition：The conversational AI agent that interacts with patients over chat. It handles registration, verification, general inquiries, appointment requests, family bookings, and emergency triage, but delegates all state-changing operations to deterministic backend tools.
- Aliases：chatbot、assistant

### proposal
- Definition：A pending, unconfirmed booking action created when a patient selects an availability card. It holds the requested slot(s) for two minutes (`HOLD_MINUTES`) until the patient confirms, rejects, changes their mind, or the hold expires. A family proposal groups multiple participants into one transaction.
- Aliases：pending proposal、slot proposal

### SlotClaim
- Definition：A database record joining an appointment or proposal to each 30-minute interval it occupies. Its uniqueness constraint is the load-bearing protection against double-booking: only one active claim per slot is allowed.
- Aliases：slot claim、claim

### reset generation
- Definition：A server-side monotonically increasing counter invalidated whenever `make reset-demo` recreates the database. A mismatch between the browser's stored generation and the server's current one invalidates old sessions, proposals, and conversation IDs to prevent stale UI from mutating a fresh dataset.
- Aliases：generation、reset-generation

### staff dashboard
- Definition：The separate React admin interface at `/staff` where dental staff view appointments, patients, pending handoff requests, emergency alerts, and conversation feedback. Authentication uses server-side sessions with an HttpOnly cookie and CSRF protection for writes.
- Aliases：staff UI、admin dashboard

### emergency alert board
- Definition：The staff-facing queue of life-threatening or urgent symptom reports captured by the receptionist. Staff can acknowledge and resolve alerts; no external paging/SMS/email integration exists -- alerts are persisted for manual review.
- Aliases：alerts board、emergency board

### family booking
- Definition：A single proposal that books multiple participants (e.g., spouse/partner and children) back-to-back. Each participant has independent patient, appointment type, duration, and provider eligibility; confirmation is atomic -- either all visits are booked or none, with rollback on conflict.
- Aliases：family scheduling、multi-patient booking

### handoff
- Definition：A pending request routed from the automated receptionist to a human staff member, surfaced in the staff dashboard's pending queue so staff can take over complex or exceptional cases.
- Aliases：staff handoff、pending request
