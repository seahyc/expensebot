---
name: miles-maximiser
description: Suggests best credit card to use for each receipt to maximise miles/cashback
enabled: false
---

# Miles Maximiser

When a receipt is parsed, compare the merchant category against known card earn rates
and suggest whether a better card could have been used.

## Card Earn Rates (SGD, miles per dollar)
<!-- Populate when enabling this plugin -->

## Rules
- Only suggest when a better card would earn >20% more
- Show the delta: "DBS Altitude would earn 2.4 mpd vs your 1.2 mpd on Citi Rewards"
- Never block filing — this is advisory only
