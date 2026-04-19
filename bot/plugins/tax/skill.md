---
name: tax-advisor
description: Singapore income tax advisor — estimates chargeable income, deductions, and estimated tax
enabled: false
---

# Tax Advisor

Reads payslips and CPF statements from ~/Documents/BankStatements/ to estimate
annual chargeable income, applicable deductions (CPF relief, NS man relief, etc.),
and estimated income tax payable.

## Data Sources
- ~/Documents/BankStatements/Payslips/ — monthly payslips (PDF)
- ~/Documents/BankStatements/CPF/ — CPF statements
- ~/Documents/BankStatements/Tax/ — previous year NOA if available

## Rules
- Only available when user asks explicitly ("how much tax do I owe?")
- Always caveat: "This is an estimate — consult IRAS or a tax advisor for exact figures"
- Use current IRAS tax rates for the assessment year
