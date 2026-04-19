---
name: fraud-detection
description: Cross-checks receipts against bank statements to detect anomalies or duplicates
enabled: false
---

# Fraud Detection

When a receipt is parsed, cross-reference against bank statement transactions to:
1. Confirm the transaction actually appears in the bank statement
2. Flag if the amount differs significantly (>5%) from what was charged
3. Detect if a receipt has already been claimed previously

## Data Sources
- ~/Documents/BankStatements/DBS_CreditCard/ — DBS credit card statements
- ~/Documents/BankStatements/OCBC_CreditCard/ — OCBC credit card statements
- Uses existing pdf_txn_checker.py parsing logic

## Rules
- Never block filing — flag as warning only
- Show: "⚠️ I see a $45.20 Grab charge on Apr 14 in your DBS statement — matches this receipt"
- If no match found: "Note: couldn't find this transaction in your bank statements"
