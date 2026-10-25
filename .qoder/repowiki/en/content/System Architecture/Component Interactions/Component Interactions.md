# Component Interactions

<cite>
**Referenced Files in This Document**
- [README.md](file://README.md)
- [main.py](file://backend/app/main.py)
- [chat.py](file://backend/app/api/chat.py)
- [runner.py](file://backend/app/agent/runner.py)
- [deepseek.py](file://backend/app/ai/deepseek.py)
- [tools.py](file://backend/app/agent/tools.py)
- [scheduling.py](file://backend/app/services/scheduling.py)
- [emergency.py](file://backend/app/services/emergency.py)
- [client.ts](file://frontend/src/api/client.ts)
- [useChat.ts](file://frontend/src/hooks/useChat.ts)
- [sessionStore.ts](file://frontend/src/lib/sessionStore.ts)
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
This document explains how the dental receptionist system turns a patient message into an AI-driven conversation, tool execution, and database update. It covers:
- The React frontend session and chat flow.
- FastAPI endpoints for sessions, conversations, feedback, and scheduling actions.
- The agent runner orchestrating DeepSeek tool calls, conversation state, and fallback behavior.
- Service-layer domains for scheduling, patient management, and emergency triage.
- Sequence diagrams for appointment booking, family coordination, and emergency escalation.
- Error propagation, retries, and idempotency across components.

## Project Structure
The repository is split into a Python FastAPI backend and a React frontend:
- Backend: API routes, agent runner, AI client, tools, services, models, and SQLite persistence.
- Frontend: typed API client, chat hook, card actions, clinic data helpers, and tab-scoped session storage.

```mermaid
graph TB
subgraph "Frontend"
UI["React Chat UI"]
Hook["useChat hook"]
Client["Typed API client"]
Store["Tab session store"]
end
subgraph "Backend"
App["FastAPI app<br/>middleware + routers"]
ChatAPI["/api/chat routes"]
SchedAPI["/api/scheduling routes"]
Runner["Agent runner"]
Tools["Tool registry"]
SvcSched["Scheduling service"]
SvcEmerg["Emergency triage"]
DSClient["DeepSeek client"]
DB["SQLite / SQLAlchemy"]
end
UI --> Hook
Hook --> Client
Client --> ChatAPI
Client --> SchedAPI
ChatAPI --> Runner
Runner --> DSClient
Runner --> Tools
Tools --> SvcSched
Tools --> SvcEmerg
ChatAPI --> DB
SchedAPI --> DB
Runner --> DB
```

**Diagram sources**
- [main.py:202-252](file://backend/app/main.py#L202-L252)
- [chat.py:111-274](file://backend/app/api/chat.py#L111-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:178-329](file://backend/app/ai/deepseek.py#L178-L329)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)
- [client.ts:226-307](file://frontend/src/api/client.ts#L226-L307)
- [useChat.ts:271-749](file://frontend/src/hooks/useChat.ts#L271-L749)

**Section sources**
- [README.md:52-70](file://README.md#L52-L70)
- [README.md:305-337](file://README.md#L305-L337)

## Core Components
- FastAPI application: request ID middleware, CORS, exception handlers, router registration, and startup bootstrap.
- Patient chat API: session creation, conversation list/start/restore, message sending, feedback, and reset.
- Agent runner: bounded tool loop, prompt assembly, tool dispatch, result merging, card attachment, and fallback replies.
- DeepSeek client: OpenAI-compatible HTTP client with timeouts, retries, DSML parsing, and typed errors.
- Tool registry: Pydantic-validated tool schemas, context-bound handlers, and safe truncation of large results.
- Scheduling service: availability search, proposal holds, confirmation transactions, family plans, and slot cards.
- Emergency triage: keyword-based life-threatening detection, contact validation, alert creation, and follow-up handling.
- Frontend client: typed requests, headers, error mapping, and scheduling action payloads.
- Chat hook: boot/restore, optimistic send, live proposal reconciliation, retry, and feedback submission.
- Session store: tab-scoped token, conversation pointer, reset generation, and reset notice.

**Section sources**
- [main.py:45-179](file://backend/app/main.py#L45-L179)
- [chat.py:111-347](file://backend/app/api/chat.py#L111-L347)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:31-64](file://backend/app/ai/deepseek.py#L31-L64)
- [tools.py:41-127](file://backend/app/agent/tools.py#L41-L127)
- [scheduling.py:69-134](file://backend/app/services/scheduling.py#L69-L134)
- [emergency.py:117-148](file://backend/app/services/emergency.py#L117-L148)
- [client.ts:94-131](file://frontend/src/api/client.ts#L94-L131)
- [useChat.ts:271-749](file://frontend/src/hooks/useChat.ts#L271-L749)
- [sessionStore.ts:1-30](file://frontend/src/lib/sessionStore.ts#L1-L30)

## Architecture Overview
The patient message path is intentionally layered:
- The frontend owns UI state and sends messages through a typed client.
- FastAPI validates inputs, resolves the session, persists the patient message, and delegates to the runner.
- The runner builds prompts, calls DeepSeek, executes validated tools, and persists tool results and final text.
- Services enforce identity, scheduling rules, transactions, and emergency policy.
- The UI reconciles server-provided cards with live proposals and handles stale or settled states.

```mermaid
sequenceDiagram
participant UI as "React UI"
participant Hook as "useChat"
participant Client as "Typed API client"
participant ChatAPI as "FastAPI /api/chat"
participant Runner as "Agent runner"
participant DS as "DeepSeek client"
participant Tools as "Tool registry"
participant Svc as "Business services"
participant DB as "SQLite"
UI->>Hook : User types message
Hook->>Client : POST /chat/conversations/{id}/messages
Client->>ChatAPI : X-Patient-Session header
ChatAPI->>DB : Persist patient message
ChatAPI->>Runner : run_turn(...)
Runner->>DS : chat(messages, tools)
DS-->>Runner : AssistantTurn (text/tool_calls)
Runner->>Tools : execute_tool(name, args)
Tools->>Svc : Validate + call domain service
Svc->>DB : Read/write business state
Svc-->>Tools : Result + cards
Tools-->>Runner : Truncated-safe result
Runner->>DS : Follow-up turn if needed
Runner-->>ChatAPI : Reply + cards
ChatAPI-->>Client : Response payload
Client-->>Hook : Update transcript and cards
```

**Diagram sources**
- [chat.py:233-274](file://backend/app/api/chat.py#L233-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:205-329](file://backend/app/ai/deepseek.py#L205-L329)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)

## Detailed Component Analysis

### Patient Message Submission to Final Reply
This sequence shows one complete assistant turn from the React UI through FastAPI, the runner, DeepSeek, tools, and back to the UI.

```mermaid
sequenceDiagram
participant UI as "React UI"
participant Hook as "useChat"
participant Client as "client.ts"
participant ChatAPI as "chat.py"
participant Runner as "runner.py"
participant DS as "deepseek.py"
participant Tools as "tools.py"
participant Svc as "services/*"
participant DB as "SQLite"
UI->>Hook : send(message)
Hook->>Client : sendMessage(token, conversationId, message)
Client->>ChatAPI : POST /api/chat/conversations/{id}/messages
ChatAPI->>DB : Append patient message
ChatAPI->>Runner : run_turn(db, conversation, session, text)
Runner->>Runner : Build system prompt + history
Runner->>DS : chat(messages, tools)
alt Model returns tool_calls
Runner->>Tools : execute_tool(call)
Tools->>Svc : Call validated service
Svc->>DB : Commit business change
Svc-->>Tools : ok/message/cards
Tools-->>Runner : Truncated result
Runner->>DS : chat(messages_with_tool_results)
else Model returns prose
Runner->>Runner : Append assistant text
end
Runner-->>ChatAPI : TurnResult(reply, messages, cards, degraded)
ChatAPI-->>Client : ChatSendMessageResponse
Client-->>Hook : Apply messages + cards
Hook->>Hook : markProposalsResolved + attachLiveProposals
```

**Diagram sources**
- [useChat.ts:548-623](file://frontend/src/hooks/useChat.ts#L548-L623)
- [client.ts:265-301](file://frontend/src/api/client.ts#L265-L301)
- [chat.py:233-274](file://backend/app/api/chat.py#L233-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:205-329](file://backend/app/ai/deepseek.py#L205-L329)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)

**Section sources**
- [useChat.ts:548-623](file://frontend/src/hooks/useChat.ts#L548-L623)
- [client.ts:265-301](file://frontend/src/api/client.ts#L265-L301)
- [chat.py:233-274](file://backend/app/api/chat.py#L233-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)

### Appointment Booking Workflow
A typical booking involves availability search, hold creation, explicit confirmation, and transactional commit.

```mermaid
sequenceDiagram
participant UI as "React UI"
participant Hook as "useChat"
participant SchedAPI as "/api/scheduling"
participant Svc as "scheduling service"
participant DB as "SQLite"
UI->>Hook : Click slot card
Hook->>SchedAPI : POST /scheduling/slots/select
Note over SchedAPI : create_key = idempotency key
SchedAPI->>Svc : Hold slot(s)<br/>validate intent/policy
Svc->>DB : BEGIN IMMEDIATE<br/>create SlotClaim / proposal
DB-->>Svc : Success or conflict
Svc-->>SchedAPI : Awaiting Confirmation card
SchedAPI-->>UI : Proposal card with confirm/decline
UI->>SchedAPI : POST /scheduling/proposals/{id}/confirm
Note over SchedAPI : confirm_key = idempotency key
SchedAPI->>Svc : Recheck slots<br/>release hold<br/>book visit(s)
Svc->>DB : BEGIN IMMEDIATE<br/>commit appointment(s)
DB-->>Svc : Committed
Svc-->>SchedAPI : Confirmed response
SchedAPI-->>UI : Booked card + updated thread
```

**Diagram sources**
- [client.ts:370-416](file://frontend/src/api/client.ts#L370-L416)
- [scheduling.py:238-295](file://backend/app/services/scheduling.py#L238-L295)
- [scheduling.py:597-716](file://backend/app/services/scheduling.py#L597-L716)

**Section sources**
- [client.ts:370-416](file://frontend/src/api/client.ts#L370-L416)
- [scheduling.py:238-295](file://backend/app/services/scheduling.py#L238-L295)
- [scheduling.py:597-716](file://backend/app/services/scheduling.py#L597-L716)

### Family Coordination Workflow
Family bookings require multiple participants, coordinated contiguous or nearby options, and one atomic confirmation.

```mermaid
sequenceDiagram
participant UI as "React UI"
participant Hook as "useChat"
participant ChatAPI as "/api/chat"
participant Runner as "runner.py"
participant Tools as "tools.py"
participant Svc as "scheduling service"
participant DB as "SQLite"
UI->>ChatAPI : Send family request
ChatAPI->>Runner : run_turn(...)
Runner->>Tools : search_family_availability(...)
Tools->>Svc : Resolve members + preferences
Svc->>DB : Search family plans
Svc-->>Tools : Complete family options + cards
Tools-->>Runner : Cards with option_identity
Runner->>Runner : Attach family_option card
Runner-->>ChatAPI : Reply + family_option card
UI->>ChatAPI : Action family_option_click(option_identity, request_id)
ChatAPI->>Runner : run_turn(..., family_option_identity)
Runner->>Tools : hold_family_option(option_identity, request_id)
Tools->>Svc : Create family proposal + holds
Svc->>DB : Atomic proposal + claims
Svc-->>Tools : Awaiting Confirmation family card
Tools-->>Runner : Card + message
Runner-->>ChatAPI : Reply + family proposal card
```

**Diagram sources**
- [runner.py:153-169](file://backend/app/agent/runner.py#L153-L169)
- [runner.py:409-557](file://backend/app/agent/runner.py#L409-L557)
- [tools.py:549-559](file://backend/app/agent/tools.py#L549-L559)
- [scheduling.py:341-390](file://backend/app/services/scheduling.py#L341-L390)
- [scheduling.py:719-792](file://backend/app/services/scheduling.py#L719-L792)

**Section sources**
- [runner.py:153-169](file://backend/app/agent/runner.py#L153-L169)
- [runner.py:409-557](file://backend/app/agent/runner.py#L409-L557)
- [tools.py:549-559](file://backend/app/agent/tools.py#L549-L559)
- [scheduling.py:341-390](file://backend/app/services/scheduling.py#L341-L390)
- [scheduling.py:719-792](file://backend/app/services/scheduling.py#L719-L792)

### Emergency Escalation Workflow
Life-threatening symptoms bypass registration and scheduling; other urgent reports require callback details and create staff alerts.

```mermaid
flowchart TD
Start(["Patient describes symptoms"]) --> CheckLT{"Life-threatening keywords?"}
CheckLT --> |Yes| Immediate["Return 911 guidance immediately"]
CheckLT --> |No| Capture["Capture symptoms and description"]
Capture --> ContactCheck{"Verified patient or valid name+phone?"}
ContactCheck --> |Missing| AskContact["Ask for caller_name and caller_phone"]
ContactCheck --> |Valid| RaiseAlert["Create staff emergency alert"]
RaiseAlert --> Inform["Tell front desk has been notified"]
AskContact --> End(["Assistant asks for missing contact details"])
Immediate --> End
Inform --> OfferVisit["Offer real emergency slot check if requested"]
OfferVisit --> End
```

**Diagram sources**
- [emergency.py:219-232](file://backend/app/services/emergency.py#L219-L232)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)
- [emergency.py:390-455](file://backend/app/services/emergency.py#L390-L455)

**Section sources**
- [emergency.py:219-232](file://backend/app/services/emergency.py#L219-L232)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)
- [emergency.py:390-455](file://backend/app/services/emergency.py#L390-L455)

### Session Management and Real-Time Updates
The frontend maintains a tab-scoped session and restores threads on load. Real-time updates come from:
- Restore endpoint returning persisted messages plus pending proposals.
- Availability refresh endpoint revalidating slot grids.
- Live proposal reconciliation marking expired or superseded cards inactive.

```mermaid
sequenceDiagram
participant UI as "React UI"
participant Store as "sessionStore"
participant Hook as "useChat"
participant Client as "client.ts"
participant ChatAPI as "/api/chat"
participant SchedAPI as "/api/scheduling"
UI->>Store : Read token + conversationId
UI->>Hook : bootstrap()
Hook->>Client : GET /chat/conversations/{id}
Client->>ChatAPI : X-Patient-Session
ChatAPI-->>Client : Messages + pending_proposals
Hook->>Hook : markProposalsResolved + attachLiveProposals
UI->>SchedAPI : POST /scheduling/availability
SchedAPI-->>UI : Updated slot cards + rejected ids
Hook->>Hook : Track latest grid + lock stale proposals
```

**Diagram sources**
- [sessionStore.ts:70-138](file://frontend/src/lib/sessionStore.ts#L70-L138)
- [useChat.ts:356-443](file://frontend/src/hooks/useChat.ts#L356-L443)
- [useChat.ts:445-514](file://frontend/src/hooks/useChat.ts#L445-L514)
- [client.ts:259-301](file://frontend/src/api/client.ts#L259-L301)
- [client.ts:439-464](file://frontend/src/api/client.ts#L439-L464)

**Section sources**
- [sessionStore.ts:1-30](file://frontend/src/lib/sessionStore.ts#L1-L30)
- [sessionStore.ts:70-138](file://frontend/src/lib/sessionStore.ts#L70-L138)
- [useChat.ts:356-443](file://frontend/src/hooks/useChat.ts#L356-L443)
- [useChat.ts:445-514](file://frontend/src/hooks/useChat.ts#L445-L514)
- [client.ts:259-301](file://frontend/src/api/client.ts#L259-L301)
- [client.ts:439-464](file://frontend/src/api/client.ts#L439-L464)

### Agent Runner Orchestration and Fallbacks
The runner enforces safety and stability:
- Bounded tool rounds and per-round tool-call limits.
- Safe assistant text when model or tool failures occur.
- Card deduplication, settlement, and live proposal merging.
- Time-reask logic that replays or refreshes availability without double-booking.

```mermaid
flowchart TD
Entry(["run_turn entry"]) --> Validate["Validate text length and session binding"]
Validate --> PersistMsg["Persist patient message + title"]
PersistMsg --> BuildPrompt["Build system prompt + provider messages"]
BuildPrompt --> CallModel["Call DeepSeek with allowed tools"]
CallModel --> HasTools{"Tool calls present?"}
HasTools --> |No| SaveReply["Append assistant reply"]
HasTools --> |Yes| ExecuteTools["Execute each tool via registry"]
ExecuteTools --> MergeCards["Merge + settle + dedupe cards"]
MergeCards --> NextRound{"More rounds allowed?"}
NextRound --> |Yes| CallModel
NextRound --> |No| Finalize["Format reply + attach cards"]
SaveReply --> Finalize
Finalize --> Exit(["Return TurnResult"])
```

**Diagram sources**
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [runner.py:409-557](file://backend/app/agent/runner.py#L409-L557)
- [runner.py:560-719](file://backend/app/agent/runner.py#L560-L719)

**Section sources**
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [runner.py:409-557](file://backend/app/agent/runner.py#L409-L557)
- [runner.py:560-719](file://backend/app/agent/runner.py#L560-L719)

### Service Layer Interactions Across Domains
- Patient management tools validate and normalize names, phones, and insurance choices before creating or updating records.
- Scheduling service composes date parsing, availability search, proposal holds, and atomic confirmation.
- Emergency triage classifies severity, validates callback details, and raises staff alerts.

```mermaid
classDiagram
class ToolContext {
+session
+patient_session
+conversation_id
+verified_patient()
}
class ToolRegistry {
+openai_tool_schemas()
+execute_tool(ctx, name, arguments)
}
class SchedulingService {
+find_availability(...)
+slot_card(...)
+family_option_cards(...)
}
class EmergencyService {
+triage(...)
+follow_up(...)
+is_life_threatening(text)
}
ToolRegistry --> ToolContext : "uses"
ToolRegistry --> SchedulingService : "calls"
ToolRegistry --> EmergencyService : "calls"
```

**Diagram sources**
- [tools.py:41-127](file://backend/app/agent/tools.py#L41-L127)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)

**Section sources**
- [tools.py:41-127](file://backend/app/agent/tools.py#L41-L127)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)

## Dependency Analysis
Key dependency relationships:
- FastAPI app registers routers and installs observability middleware.
- Chat API depends on runner, conversation services, and patient session services.
- Runner depends on DeepSeek client, tool registry, and business services.
- Tools depend on validated argument models and services like scheduling and emergency.
- Frontend client depends on typed responses and uses AbortController for cancellation.
- useChat hook coordinates session restore, optimistic messaging, and live proposal reconciliation.

```mermaid
graph LR
Main["main.py"] --> ChatAPI["api/chat.py"]
Main --> SchedAPI["api/scheduling.py"]
ChatAPI --> Runner["agent/runner.py"]
Runner --> DS["ai/deepseek.py"]
Runner --> Tools["agent/tools.py"]
Tools --> SchedSvc["services/scheduling.py"]
Tools --> EmergSvc["services/emergency.py"]
FE_Client["frontend/api/client.ts"] --> ChatAPI
FE_Hook["frontend/hooks/useChat.ts"] --> FE_Client
FE_Store["frontend/lib/sessionStore.ts"] --> FE_Hook
```

**Diagram sources**
- [main.py:202-252](file://backend/app/main.py#L202-L252)
- [chat.py:111-274](file://backend/app/api/chat.py#L111-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:178-329](file://backend/app/ai/deepseek.py#L178-L329)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)
- [client.ts:226-307](file://frontend/src/api/client.ts#L226-L307)
- [useChat.ts:271-749](file://frontend/src/hooks/useChat.ts#L271-L749)
- [sessionStore.ts:70-138](file://frontend/src/lib/sessionStore.ts#L70-L138)

**Section sources**
- [main.py:202-252](file://backend/app/main.py#L202-L252)
- [chat.py:111-274](file://backend/app/api/chat.py#L111-L274)
- [runner.py:171-406](file://backend/app/agent/runner.py#L171-L406)
- [deepseek.py:178-329](file://backend/app/ai/deepseek.py#L178-L329)
- [tools.py:703-740](file://backend/app/agent/tools.py#L703-L740)
- [scheduling.py:393-716](file://backend/app/services/scheduling.py#L393-L716)
- [emergency.py:258-366](file://backend/app/services/emergency.py#L258-L366)
- [client.ts:226-307](file://frontend/src/api/client.ts#L226-L307)
- [useChat.ts:271-749](file://frontend/src/hooks/useChat.ts#L271-L749)
- [sessionStore.ts:70-138](file://frontend/src/lib/sessionStore.ts#L70-L138)

## Performance Considerations
- DeepSeek client uses bounded retries with exponential backoff for transient failures and strict timeouts.
- Runner limits tool rounds and tool calls per round to prevent runaway loops.
- Tool results are truncated safely while preserving interactive cards and decision fields.
- Scheduling uses immediate transactions and uniqueness constraints to avoid double-booking under contention.
- Frontend avoids unnecessary network calls by reconciling restored transcripts with live proposals.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and their handling:
- Database busy or integrity errors: FastAPI returns structured error codes and hints to refresh or retry.
- DeepSeek timeout or rate limit: Client retries briefly; runner returns a degraded message without claiming success.
- Stale or superseded cards: Frontend marks non-pending proposals inactive and re-checks availability.
- Session invalidation: Frontend detects 401/404, clears local pointers, and starts a fresh session.
- Emergency triage missing contact details: Assistant asks for name and phone before filing an alert.

**Section sources**
- [main.py:124-179](file://backend/app/main.py#L124-L179)
- [deepseek.py:31-64](file://backend/app/ai/deepseek.py#L31-L64)
- [deepseek.py:205-249](file://backend/app/ai/deepseek.py#L205-L249)
- [runner.py:264-292](file://backend/app/agent/runner.py#L264-L292)
- [useChat.ts:200-204](file://frontend/src/hooks/useChat.ts#L200-L204)
- [useChat.ts:592-616](file://frontend/src/hooks/useChat.ts#L592-L616)
- [emergency.py:151-180](file://backend/app/services/emergency.py#L151-L180)

## Conclusion
The system separates conversational interpretation from deterministic business logic. The React frontend manages user experience and session state, FastAPI exposes typed endpoints, the runner orchestrates AI tool calls safely, and services enforce identity, scheduling correctness, and emergency policy. Retries, idempotency keys, transactions, and card reconciliation ensure robustness even when external APIs fail or schedules change between offer and confirmation.

[No sources needed since this section summarizes without analyzing specific files]