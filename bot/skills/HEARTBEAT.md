tasks:
  - id: claim_status
    every: 24h
    prompt: |
      Check if any submitted or for-approval claims changed status (approved/reimbursed/rejected).
      If status changed since last check, tell the user concisely.
      Reply HEARTBEAT_OK if nothing changed or no submitted claims.

  - id: aging_drafts
    every: 12h
    prompt: |
      Check if the user has any draft claims older than 3 days that haven't been submitted.
      If yes, remind them gently (one sentence, Janai style).
      Reply HEARTBEAT_OK if no aging drafts.

  - id: gmail_receipts
    every: 4h
    prompt: |
      Search Gmail for unread emails with subjects containing: receipt, invoice, order confirmation, payment.
      Use search: "is:unread (subject:receipt OR subject:invoice OR subject:confirmation OR subject:payment) newer_than:1d"
      If any found, list them briefly and ask if the user wants to file any.
      Reply HEARTBEAT_OK if nothing found.
