# Pending Requests Queue

<cite>
**Referenced Files in This Document**
- [staff_handoff.py](file://backend/app/services/staff_handoff.py)
- [staff_handoff_board.py](file://backend/app/services/staff_handoff_board.py)
- [staff.py](file://backend/app/api/staff.py)
- [base.py](file://backend/app/models/base.py)
- [conversations.py](file://backend/app/models/conversations.py)
- [staff.py](file://backend/app/models/staff.py)
- [audit.py](file://backend/app/services/audit.py)
- [RequestsSection.tsx](file://frontend/src/pages/staff/sections/RequestsSection.tsx)
- [staff.ts](file://frontend/src/api/staff.ts)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)

## Introduction
This document explains the pending requests queue that surfaces conversations requiring human intervention after the AI agent cannot resolve a patient request. It covers:

- How the system identifies handoffs from the AI agent.
- Request categorization and prioritization.
- The staff queue management interface.
- Staff actions: review, continuation, proposal approval/rejection, and manual intervention.
- The request data model, status transitions, and audit logging.
- Common handoff scenarios, resolution strategies, and quality assurance processes.
- Queue performance, real-time update behavior, and operational monitoring for backlog management.

The design separates two kinds of handoff items:

| Item | Purpose | Typical lifecycle |
|---|---|---|
| Alert | Awareness or escalation raised by the assistant (for example, emergency report, insurance mismatch, urgent callback). | Open → Acknowledged → Resolved |
| Pending request | A task the assistant could not complete (callback, reschedule, clinical question, billing/insurance). | Pending → In progress → Resolved; some require explicit approval before proceeding. |

## Project Structure
The pending requests queue spans backend services, API routes, database models, and the staff dashboard frontend.

```mermaid
graph TB
subgraph "Frontend"
RS["RequestsSection.tsx"]
STAFF_TS["staff.ts"]
end
subgraph "Backend API"
STAFF_API["api/staff.py"]
end
subgraph "Services"
BOARD["services/staff_handoff_board.py"]
HANDOFF["services/staff_handoff.py"]
AUDIT["services/audit.py"]
end
subgraph "Models"
BASE["models/base.py"]
CONVERSATIONS["models/conversations.py"]
STAFF_MODEL["models/staff.py"]
end
RS --> STAFF_TS
STAFF_TS --> STAFF_API
STAFF_API --> BOARD
BOARD --> AUDIT
BOARD --> STAFF_MODEL
HANDOFF --> STAFF_MODEL
HANDOFF --> AUDIT
STAFF_MODEL --> BASE
STAFF_MODEL --> CONVERSATIONS
```

**Diagram sources**
- [RequestsSection.tsx:1-367](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L1-L367)
- [staff.ts:1-405](file://frontend/src/api/staff.ts#L1-L405)
- [staff.py:515-654](file://backend/app/api/staff.py#L515-L654)
- [staff_handoff_board.py:1-599](file://backend/app/services/staff_handoff_board.py#L1-L599)
- [staff_handoff.py:1-326](file://backend/app/services/staff_handoff.py#L1-L326)
- [audit.py:1-76](file://backend/app/services/audit.py#L1-L76)
- [base.py:94-120](file://backend/app/models/base.py#L94-L120)
- [conversations.py:39-84](file://backend/app/models/conversations.py#L39-L84)
- [staff.py:142-185](file://backend/app/models/staff.py#L142-L185)

**Section sources**
- [staff_handoff.py:1-326](file://backend/app/services/staff_handoff.py#L1-L326)
- [staff_handoff_board.py:1-599](file://backend/app/services/staff_handoff_board.py#L1-L599)
- [staff.py:515-654](file://backend/app/api/staff.py#L515-L654)
- [RequestsSection.tsx:1-367](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L1-L367)
- [staff.ts:169-206](file://frontend/src/api/staff.ts#L169-L206)

## Core Components
The queue is built around three layers:

1. **Handoff creation layer**: Converts AI-agent uncertainty into structured alerts and pending requests.
2. **Board service layer**: Provides triage queries, state transitions, approval logic, summaries, and audit recording.
3. **Staff API and UI layer**: Exposes endpoints and renders the queue with filters, detail drawers, activity timelines, and action buttons.

Key responsibilities:

- `staff_handoff.raise_alert` creates or updates an alert when the assistant needs awareness or escalation.
- `staff_handoff.raise_request` creates or updates a pending request when the assistant cannot answer or act.
- `staff_handoff.request_status` returns all open items for a conversation so the assistant can tell the patient what is waiting.
- `staff_handoff_board` defines allowed transitions, approval rules, list filtering, serialization, mutations, and audit hooks.
- `api/staff.py` exposes `/staff/alerts`, `/staff/requests`, summary counts, and mutation endpoints.
- `RequestsSection.tsx` renders the pending requests queue, filters, drawer, and staff actions.

**Section sources**
- [staff_handoff.py:98-261](file://backend/app/services/staff_handoff.py#L98-L261)
- [staff_handoff_board.py:119-128](file://backend/app/services/staff_handoff_board.py#L119-L128)
- [staff_handoff_board.py:230-285](file://backend/app/services/staff_handoff_board.py#L230-L285)
- [staff.py:520-654](file://backend/app/api/staff.py#L520-L654)
- [RequestsSection.tsx:44-214](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L44-L214)

## Architecture Overview
The handoff flow starts in the AI agent path and ends in the staff dashboard.

```mermaid
sequenceDiagram
participant Patient as "Patient"
participant Agent as "AI Agent"
participant Handoff as "staff_handoff"
participant Board as "staff_handoff_board"
participant API as "staff.py API"
participant Frontend as "RequestsSection.tsx"
participant DB as "Database"
Patient->>Agent : "Ask about coverage, pricing, scheduling, or symptoms"
Agent->>Handoff : "raise_request(...) or raise_alert(...)"
Handoff->>DB : "Insert or dedupe alert/request"
Handoff-->>Agent : "HandoffResult with status"
Agent-->>Patient : "Reply referencing queued item"
Frontend->>API : "GET /staff/requests"
API->>Board : "list_requests(filters)"
Board->>DB : "Query pending requests"
DB-->>Board : "Rows"
Board-->>API : "Serialized requests"
API-->>Frontend : "Request list"
Frontend->>API : "POST /staff/requests/{id}/status or /approve"
API->>Board : "set_request_status(...) or approve_request(...)"
Board->>DB : "Update row + audit record"
Board-->>API : "Updated request"
API-->>Frontend : "New state"
```

**Diagram sources**
- [staff_handoff.py:98-261](file://backend/app/services/staff_handoff.py#L98-L261)
- [staff_handoff_board.py:260-285](file://backend/app/services/staff_handoff_board.py#L260-L285)
- [staff_handoff_board.py:460-542](file://backend/app/services/staff_handoff_board.py#L460-L542)
- [staff.py:590-654](file://backend/app/api/staff.py#L590-L654)
- [RequestsSection.tsx:55-75](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L55-L75)

## Detailed Component Analysis

### Handoff Creation: Alerts and Pending Requests
The handoff module distinguishes between awareness escalations and actionable requests.

- **Alerts** are created through `raise_alert`. They include kind, severity, message, optional caller contact details, and idempotency via `dedupe_key`. Repeated reports bump `report_count` and can refresh the message if details worsen.
- **Pending requests** are created through `raise_request`. They capture `conversation_id`, optional `patient_id`, `request_type`, free-text `details`, and a deduplication key scoped to request type and subject.
- Both functions return a `HandoffResult` describing whether the record was newly created, its current status, severity, and metadata suitable for the assistant’s reply wording.
- `request_status` aggregates alerts and requests for a conversation, newest first, so the assistant can inform the patient that something is queued.

```mermaid
flowchart TD
Start(["AI agent decides handoff"]) --> Decide{"Alert or request?"}
Decide --> |Escalation| RaiseAlert["raise_alert(...)"]
Decide --> |Task needed| RaiseRequest["raise_request(...)"]
RaiseAlert --> DedupeCheck["Find existing open/acknowledged alert"]
RaiseRequest --> ReqDedupe["Find existing pending/in_progress request"]
DedupeCheck --> UpdateOrCreate{"Existing?"}
ReqDedupe --> ReqUpdateOrCreate{"Existing?"}
UpdateOrCreate --> |Yes| UpdateAlert["Refresh message, bump report_count, keep status"]
UpdateOrCreate --> |No| CreateAlert["Create open alert"]
ReqUpdateOrCreate --> |Yes| UpdateRequest["Refresh details, keep status"]
ReqUpdateOrCreate --> |No| CreateRequest["Create pending request"]
UpdateAlert --> Result(["HandoffResult"])
CreateAlert --> Result
UpdateRequest --> Result
CreateRequest --> Result
```

**Diagram sources**
- [staff_handoff.py:98-200](file://backend/app/services/staff_handoff.py#L98-L200)
- [staff_handoff.py:203-261](file://backend/app/services/staff_handoff.py#L203-L261)

**Section sources**
- [staff_handoff.py:40-80](file://backend/app/services/staff_handoff.py#L40-L80)
- [staff_handoff.py:98-200](file://backend/app/services/staff_handoff.py#L98-L200)
- [staff_handoff.py:203-261](file://backend/app/services/staff_handoff.py#L203-L261)
- [staff_handoff.py:264-309](file://backend/app/services/staff_handoff.py#L264-L309)

### Board Service: Triage, Filtering, Transitions, and Approval
The board service owns the queue’s business rules.

#### Categorization and Prioritization
- **Request types**: callback, reschedule, clinical_question, billing.
- **Severity levels**: low, normal, high, urgent.
- **Approval gating**: derived from request type rather than stored boolean. Billing and reschedule requests require explicit approval unless tied to an already booked appointment.
- **Sorting**: unresolved items appear first; within unresolved rows, urgency and recency determine order.

#### Allowed Status Transitions
- **Alerts**: open ↔ acknowledged ↔ resolved.
- **Requests**: pending ↔ in_progress ↔ resolved.

```mermaid
stateDiagram-v2
[*] --> AlertOpen : "raise_alert"
AlertOpen --> AlertAcknowledged : "acknowledge"
AlertOpen --> AlertResolved : "resolve"
AlertAcknowledged --> AlertResolved : "resolve"
AlertAcknowledged --> AlertOpen : "reopen"
AlertResolved --> AlertOpen : "reopen"
[*] --> RequestPending : "raise_request"
RequestPending --> RequestInProgress : "start working"
RequestPending --> RequestResolved : "mark resolved"
RequestInProgress --> RequestResolved : "mark resolved"
RequestInProgress --> RequestPending : "reopen"
RequestResolved --> RequestInProgress : "reopen"
```

**Diagram sources**
- [staff_handoff_board.py:77-87](file://backend/app/services/staff_handoff_board.py#L77-L87)
- [staff_handoff_board.py:460-501](file://backend/app/services/staff_handoff_board.py#L460-L501)

#### Approval Workflow
Approval is only available for approval-gated requests. Approving moves a pending request into in progress and records who approved it and when. Routine requests reject approval attempts.

```mermaid
flowchart TD
Start(["Approve request"]) --> Lookup["Load request"]
Lookup --> CheckType{"approval_required(request_type)?"}
CheckType --> |No| Reject["Reject: routine request"]
CheckType --> |Yes| AlreadyApproved{"Already approved?"}
AlreadyApproved --> |Yes| ReturnSame["Return existing request"]
AlreadyApproved --> |No| Mutate["Set approved_at/approved_by<br/>If pending, move to in_progress"]
Mutate --> Audit["Record audit entry"]
Audit --> Success(["Updated request"])
Reject --> End(["End"])
ReturnSame --> End
Success --> End
```

**Diagram sources**
- [staff_handoff_board.py:119-128](file://backend/app/services/staff_handoff_board.py#L119-L128)
- [staff_handoff_board.py:504-542](file://backend/app/services/staff_handoff_board.py#L504-L542)

**Section sources**
- [staff_handoff_board.py:68-87](file://backend/app/services/staff_handoff_board.py#L68-L87)
- [staff_handoff_board.py:119-132](file://backend/app/services/staff_handoff_board.py#L119-L132)
- [staff_handoff_board.py:230-285](file://backend/app/services/staff_handoff_board.py#L230-L285)
- [staff_handoff_board.py:380-542](file://backend/app/services/staff_handoff_board.py#L380-L542)

### API Layer: Staff Dashboard Endpoints
The staff API provides authenticated endpoints for:

- Listing and detailing alerts.
- Listing and detailing pending requests.
- Changing alert status and severity.
- Changing request status.
- Approving approval-gated requests.
- Fetching summary counts for dashboard tiles.

All mutating endpoints validate input, delegate to the board service, and translate service errors into HTTP responses.

```mermaid
sequenceDiagram
participant UI as "RequestsSection.tsx"
participant Client as "staff.ts"
participant API as "staff.py"
participant Board as "staff_handoff_board.py"
UI->>Client : "requests(status, request_type, search)"
Client->>API : "GET /staff/requests"
API->>Board : "list_requests(filters)"
Board-->>API : "rows"
API-->>UI : "requests + count"
UI->>Client : "setRequestStatus(id, 'in_progress' | 'resolved', note)"
Client->>API : "POST /staff/requests/{id}/status"
API->>Board : "set_request_status(staff, id, status, note)"
Board-->>API : "updated request"
API-->>UI : "new request state"
```

**Diagram sources**
- [staff.py:590-654](file://backend/app/api/staff.py#L590-L654)
- [staff_handoff_board.py:260-285](file://backend/app/services/staff_handoff_board.py#L260-L285)
- [staff_handoff_board.py:460-501](file://backend/app/services/staff_handoff_board.py#L460-L501)
- [RequestsSection.tsx:315-342](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L315-L342)
- [staff.ts:377-397](file://frontend/src/api/staff.ts#L377-L397)

**Section sources**
- [staff.py:515-654](file://backend/app/api/staff.py#L515-L654)
- [staff.ts:377-397](file://frontend/src/api/staff.ts#L377-L397)

### Frontend Queue Interface
The pending requests section provides:

- Filters for status, request type, and free-text search.
- A table showing patient name, phone, request type, detail text, status badge, and received time.
- Counts for pending items and approval-required items.
- A detail drawer with:
  - Approval button when required and not yet approved.
  - Start working button for pending requests.
  - Mark resolved or reopen buttons depending on current status.
  - Internal note field.
  - Activity timeline showing staff actions.

```mermaid
flowchart TD
Load["Load requests"] --> Filter["Apply status/type/search filters"]
Filter --> Render["Render table rows"]
Render --> Select["Select row"]
Select --> Drawer["Open request drawer"]
Drawer --> Actions{"Action?"}
Actions --> |Approve| Approve["Call approve endpoint"]
Actions --> |Start working| Start["Call set status in_progress"]
Actions --> |Mark resolved| Resolve["Call set status resolved"]
Actions --> |Reopen| Reopen["Call set status in_progress"]
Approve --> Refresh["Reload detail and close drawer"]
Start --> Refresh
Resolve --> Refresh
Reopen --> Refresh
```

**Diagram sources**
- [RequestsSection.tsx:44-214](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L44-L214)
- [RequestsSection.tsx:216-367](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L216-L367)

**Section sources**
- [RequestsSection.tsx:1-367](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L1-L367)
- [staff.ts:169-206](file://frontend/src/api/staff.ts#L169-L206)

### Data Model: Alerts, Pending Requests, Conversations, and Audit
The relevant entities are:

| Entity | Purpose | Key fields |
|---|---|---|
| `Conversation` | Chat thread linking patient session, messages, proposals, and handoff context. | `public_id`, `patient_session_id`, `patient_id`, `caller_phone`, `started_at`, `last_message_at`, `scheduling_request` |
| `Message` | Individual turns in a conversation. | `conversation_id`, `role`, `content`, `sequence`, `created_at`, `cards` |
| `StaffAlert` | Assistant-created awareness or escalation. | `kind`, `severity`, `message`, `status`, `report_count`, `caller_name`, `caller_phone`, `dedupe_key` |
| `PendingRequest` | Task needing human follow-up. | `request_type`, `status`, `details`, `preferred_callback_at`, `assigned_to`, `approved_by`, `approved_at`, `dedupe_key` |
| `AuditRecord` | Append-only log of staff/system actions. | `action`, `actor_type`, `actor_id`, `entity_type`, `entity_id`, `details`, `occurred_at` |

```mermaid
erDiagram
CONVERSATIONS {
int id PK
string public_id UK
int patient_session_id FK
int patient_id FK
string caller_phone
datetime started_at
datetime ended_at
datetime last_message_at
json scheduling_request
}
MESSAGES {
int id PK
int conversation_id FK
string role
text content
int sequence
datetime created_at
datetime finished_at
json cards
}
STAFF_ALERTS {
int id PK
int conversation_id FK
int patient_id FK
string kind
string severity
text message
string status
int report_count
string dedupe_key
string caller_name
string caller_phone
datetime created_at
datetime updated_at
datetime last_reported_at
datetime acknowledged_at
datetime resolved_at
string resolution_note
}
PENDING_REQUESTS {
int id PK
int conversation_id FK
int patient_id FK
string request_type
string status
text details
datetime preferred_callback_at
int assigned_to FK
int approved_by FK
datetime approved_at
datetime resolved_at
text resolution_note
string dedupe_key
}
AUDIT_RECORDS {
int id PK
string action
string actor_type
string actor_id
string entity_type
string entity_id
json details
datetime occurred_at
}
CONVERSATIONS ||--o{ MESSAGES : "has many"
CONVERSATIONS ||--o{ STAFF_ALERTS : "context"
CONVERSATIONS ||--o{ PENDING_REQUESTS : "context"
STAFF_ALERTS ||--o{ AUDIT_RECORDS : "audited"
PENDING_REQUESTS ||--o{ AUDIT_RECORDS : "audited"
```

**Diagram sources**
- [conversations.py:39-123](file://backend/app/models/conversations.py#L39-L123)
- [staff.py:142-185](file://backend/app/models/staff.py#L142-L185)
- [audit.py:15-51](file://backend/app/services/audit.py#L15-L51)

**Section sources**
- [conversations.py:39-123](file://backend/app/models/conversations.py#L39-L123)
- [staff.py:142-185](file://backend/app/models/staff.py#L142-L185)
- [audit.py:15-51](file://backend/app/services/audit.py#L15-L51)

### Status Transitions and Audit Logging
Every meaningful state change is validated and recorded:

- Alert status changes must follow allowed transitions.
- Request status changes must follow allowed transitions.
- Severity changes are logged but do not imply resolution.
- Approval is separate from resolution: approval clears the approval gate; resolution marks the work done.
- Audit entries store actor, target, action, and safe details. Free-text notes are not copied verbatim into audit payloads; only their presence is recorded.

```mermaid
flowchart TD
Action["Staff action"] --> Validate["Validate transition or rule"]
Validate --> Valid{"Valid?"}
Valid --> |No| Error["Return error response"]
Valid --> |Yes| Write["Write entity change"]
Write --> Audit["Append audit record"]
Audit --> Commit["Commit transaction"]
Commit --> Response["Return updated entity"]
Error --> End(["End"])
Response --> End
```

**Diagram sources**
- [staff_handoff_board.py:380-457](file://backend/app/services/staff_handoff_board.py#L380-L457)
- [staff_handoff_board.py:460-542](file://backend/app/services/staff_handoff_board.py#L460-L542)
- [audit.py:15-51](file://backend/app/services/audit.py#L15-L51)

**Section sources**
- [staff_handoff_board.py:77-87](file://backend/app/services/staff_handoff_board.py#L77-L87)
- [staff_handoff_board.py:380-542](file://backend/app/services/staff_handoff_board.py#L380-L542)
- [audit.py:15-51](file://backend/app/services/audit.py#L15-L51)

### Common Handoff Scenarios and Resolution Strategies

| Scenario | System Behavior | Recommended Staff Action |
|---|---|---|
| Patient asks about insurance coverage and the agent cannot confirm eligibility. | Creates a billing-type pending request; approval is required. | Review details, verify coverage, approve if appropriate, then mark resolved after follow-up. |
| Patient wants to reschedule but the agent cannot find a valid replacement slot. | Creates a reschedule-type pending request; approval is required. | Check provider availability, propose new times, approve if policy allows, then mark resolved. |
| Caller reports severe pain without a verified patient account. | Creates an urgent alert with caller contact details; may include call-back marker. | Call back using provided contact, assess urgency, acknowledge, then resolve with a note. |
| Patient leaves feedback indicating confusion or dissatisfaction. | Feedback is surfaced separately; it signals where to inspect conversation history. | Review the flagged conversation transcript and consider improving agent handling or escalating to staff. |
| Assistant detects an emergency symptom pattern. | Raises an emergency alert with high or urgent severity. | Immediately acknowledge, escalate per clinic protocol, and resolve once handled. |

Resolution strategies should distinguish:

- **Routine tasks**: callback, clinical question, reschedule without approval gate.
- **Approval-gated tasks**: billing and reschedule where policy requires a decision.
- **Awareness-only items**: alerts that need acknowledgment and later resolution but are not approvals.

**Section sources**
- [staff_handoff_board.py:68-75](file://backend/app/services/staff_handoff_board.py#L68-L75)
- [staff_handoff_board.py:119-128](file://backend/app/services/staff_handoff_board.py#L119-L128)
- [staff_handoff.py:98-200](file://backend/app/services/staff_handoff.py#L98-L200)

### Quality Assurance Processes
Quality assurance is supported by:

- Conversation feedback collection, including thumbs-down ratings and optional reasons.
- Staff feedback endpoint exposing recent negative feedback with conversation excerpts.
- Audit trails for every triage action.
- Summary counts for open alerts, urgent alerts, pending requests, and approvals required.

Recommended QA practices:

- Monitor negative feedback trends and inspect related transcripts.
- Review unresolved alerts and urgent alerts regularly.
- Track approval turnaround time for billing and reschedule requests.
- Use audit logs to investigate missed steps or invalid transitions.
- Periodically review request types to ensure categorization remains accurate.

**Section sources**
- [staff.py:325-372](file://backend/app/api/staff.py#L325-L372)
- [staff_handoff_board.py:325-372](file://backend/app/services/staff_handoff_board.py#L325-L372)
- [audit.py:15-51](file://backend/app/services/audit.py#L15-L51)

## Dependency Analysis
The following diagram shows how components depend on each other during queue operations.

```mermaid
graph LR
Frontend["RequestsSection.tsx"] --> ApiClient["staff.ts"]
ApiClient --> StaffApi["api/staff.py"]
StaffApi --> BoardService["staff_handoff_board.py"]
BoardService --> Models["models/staff.py"]
BoardService --> BaseEnums["models/base.py"]
BoardService --> AuditService["services/audit.py"]
HandoffService["services/staff_handoff.py"] --> Models
HandoffService --> BaseEnums
HandoffService --> AuditService
Models --> Conversations["models/conversations.py"]
```

**Diagram sources**
- [RequestsSection.tsx:1-367](file://frontend/src/pages/staff/sections/RequestsSection.tsx#L1-L367)
- [staff.ts:1-405](file://frontend/src/api/staff.ts#L1-L405)
- [staff.py:515-654](file://backend/app/api/staff.py#L515-L654)
- [staff_handoff_board.py:1-599](file://backend/app/services/staff_handoff_board.py#L1-L599)
- [staff_handoff.py:1-326](file://backend/app/services/staff_handoff.py#L1-L326)
- [staff.py:142-185](file://backend/app/models/staff.py#L142-L185)
- [base.py:94-120](file://backend/app/models/base.py#L94-L120)
- [conversations.py:39-84](file://backend/app/models/conversations.py#L39-L84)
- [audit.py:1-76](file://backend/app/services/audit.py#L1-L76)

**Section sources**
- [staff_handoff_board.py:1-599](file://backend/app/services/staff_handoff_board.py#L1-L599)
- [staff_handoff.py:1-326](file://backend/app/services/staff_handoff.py#L1-L326)
- [staff.py:515-654](file://backend/app/api/staff.py#L515-L654)

## Performance Considerations
Queue performance is designed around bounded reads, server-side sorting, and minimal payload sizes.

- **List limits**: Default and maximum list limits prevent unbounded result sets.
- **Server-side filtering**: Status, kind, severity, request type, and search filters are applied in SQL.
- **Search scope**: Search matches against message/details and patient names using case-insensitive patterns.
- **Summary counts**: Dashboard counts use aggregate queries without loading full rows.
- **Concurrency**: Mutations run in thread pools and use write transactions; concurrent writes raise a busy error mapped to a service unavailable response.
- **Real-time updates**: The frontend uses query keys and reload calls; there is no persistent WebSocket connection shown in these files. Real-time-like behavior comes from polling or explicit refreshes.

Recommendations for backlog management:

- Keep default list limits reasonable and monitor slow queries under heavy search load.
- Use summary counts to detect growing backlogs.
- Alert on increasing counts of urgent alerts or approvals required.
- Avoid excessive client-side filtering; rely on server filters for large queues.
- Consider adding pagination if lists grow beyond current limits.

**Section sources**
- [staff_handoff_board.py:65-66](file://backend/app/services/staff_handoff_board.py#L65-L66)
- [staff_handoff_board.py:230-285](file://backend/app/services/staff_handoff_board.py#L230-L285)
- [staff_handoff_board.py:325-372](file://backend/app/services/staff_handoff_board.py#L325-L372)
- [staff.py:207-218](file://backend/app/api/staff.py#L207-L218)

## Troubleshooting Guide

### Common Errors and Responses
| Symptom | Likely Cause | Resolution |
|---|---|---|
| Invalid transition error when changing alert or request status | Attempted an unsupported state change. | Follow allowed transitions: open → acknowledged → resolved for alerts; pending → in_progress → resolved for requests. |
| Unknown alert or unknown request | ID does not exist or was removed. | Refresh the list and retry; check if the item was resolved or moved. |
| Busy error | Concurrent write conflict or backend saturation. | Retry after a short delay; reduce simultaneous mutations. |
| Not approvable error | Trying to approve a routine request. | Only billing and reschedule requests require approval; use status transitions instead. |
| CSRF or authentication failure | Missing or mismatched session/CSRF cookies. | Ensure the dashboard is authenticated and CSRF headers are included for mutating requests. |

### Operational Monitoring Checklist
- Verify summary counts reflect expected backlog size.
- Inspect urgent alerts and ensure they are acknowledged promptly.
- Review approval-required requests and track approval latency.
- Check audit logs for unexpected reopens or invalid transitions.
- Monitor negative feedback for recurring conversation issues.

**Section sources**
- [staff_handoff_board.py:90-95](file://backend/app/services/staff_handoff_board.py#L90-L95)
- [staff_handoff_board.py:380-542](file://backend/app/services/staff_handoff_board.py#L380-L542)
- [staff.py:207-218](file://backend/app/api/staff.py#L207-L218)
- [staff.py:325-372](file://backend/app/api/staff.py#L325-L372)

## Conclusion
The pending requests queue provides a clear separation between awareness escalations and actionable follow-ups. The AI agent hands off uncertain or incomplete patient interactions into structured alerts and pending requests. Staff triage those items through a filtered, searchable dashboard, apply allowed status transitions, approve policy-gated decisions, and record every meaningful action in an audit trail.

For reliable operations:

- Treat alerts as awareness and escalation signals.
- Treat pending requests as work items with clear status progression.
- Use approval only for approval-gated request types.
- Monitor summary counts, urgent alerts, and negative feedback.
- Rely on audit logs for accountability and troubleshooting.
- Keep list sizes bounded and prefer server-side filtering for performance.