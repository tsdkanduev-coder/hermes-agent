---
name: voice-call
description: Use when the user explicitly asks to call, book, reserve, check availability, or clarify something by phone. The assistant should find a phone number when needed, formulate a concise task, and start the call with voice_call.
platforms: ["telegram"]
---

# Voice Call Concierge

Use this skill only when the user clearly wants a phone call or reservation handled by phone.

## Flow

1. Understand whether this is a general call, a restaurant reservation, a change/cancel request, or an information request.
2. If no phone number is provided, use web search and prefer official sources. If the venue match is ambiguous, ask one short clarification.
3. Ask only for details that are genuinely missing. For restaurant bookings, naturally collect restaurant, date/time, party size, booking name, and any special wishes already mentioned.
4. Call `voice_call` with `action: "initiate_call"`, `to`, and a plain Russian `task`.
5. Tell the user the call is in progress.

## Task Format

Good:

```text
Забронировать столик в Sage на субботу в 21:00 на имя Цевдн, 2 гостя. Если 21:00 недоступно, уточнить ближайшие варианты.
```

Avoid adding role, tone, or tool instructions to `task`; the voice runtime supplies those centrally.

## Guardrails

- Do not call unless the user asked to call or complete a task that normally requires calling.
- Do not agree to payment, deposits, card transfer, or materially different terms without asking the user.
- Use respectful concierge tone and never use first-person plural in Russian.
