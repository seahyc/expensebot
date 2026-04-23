tasks:
  - id: claim_status
    every: 24h
    prompt: |
      Check the current status of the user's expense claims.
      If any claims are currently approved or reimbursed and haven't been acknowledged, inform the user.
      Reply HEARTBEAT_OK if all claims are in draft or for-approval state.

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
      Then ALSO call list_claims(status="all") and cross-reference: for each Gmail receipt, check whether a claim
      already exists with roughly matching amount + merchant + date (±1 day). Drop any that already match a filed claim —
      don't nudge about receipts the user has already filed. If the ONLY receipts you find are already filed, reply HEARTBEAT_OK.
      Only surface genuinely un-filed receipts; list them briefly and ask if the user wants to file any.
      Reply HEARTBEAT_OK if nothing unfilled found.
