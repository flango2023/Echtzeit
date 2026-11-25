# Design note: scam scenarios, payee check and risk score

Status: implemented in version 0.1. All data is synthetic and every amount is fictional.

## What this step answers

For the same synthetic population and the same scams, how much money is lost, prevented and
recovered under three policies, and what does that cost normal customers?

| Policy | What the banks do |
|---|---|
| `NONE` | Nothing before the payment. After the victim reports, the payment is traced and the money still on accounts is frozen. |
| `SENDER_ONLY` | The sending bank checks the payee name and scores every payment: allow, warn or block. |
| `SENDER_AND_RECEIVER` | As above. When an outgoing payment is blocked because money is passing straight through an account, that bank also freezes the money that just arrived. |

The receiving-side freeze models the provisional EU Payment Services Regulation, under which
the receiving provider must freeze transactions it finds suspicious (European Parliament press
release on the payment services deal, November 2025). The final legal text is not yet
adopted, and this simulator does not claim to implement it.

## Scenario patterns and their sources

The patterns are paraphrased from the EPC *2025 Payments Threats and Fraud Trends Report*
(EPC162-24, version 2.0). They stay at the level of detail that report publishes.

| Scenario | What happens | Source |
|---|---|---|
| Safe account | A fraudster posing as the bank tells the customer the account is at risk and that the money must go to a "safe" account the fraudster controls. The customer makes the payment. | EPC162-24, section 3.1.4 (bank employee impersonation, safe account fraud) |
| Invoice with a changed IBAN | An invoice keeps the real supplier's name but carries the IBAN of an account the fraudster controls. The business pays it. | EPC162-24: the invoicing stage is exposed to IBAN manipulation |
| Money mules | Mule accounts receive the money and pass it on so it cannot be tracked, often ending in a cash withdrawal. One or two levels of mules. | EPC162-24, section 3.1.7 (most cases involve no more than one or two levels of mules) |

Verification of Payee outcomes (match, close match, no match, verification not possible) come
from the EPC VoP scheme FAQ. How names are compared is not defined by the EPC and is this
project's choice (see `echtzeit/vop.py`).

## Risk rules

Each rule is a generic, widely published signal. Points and thresholds are this project's
choices and are not calibrated. They live in `RiskConfig`, and the results file prints them.

| Rule | Fires when |
|---|---|
| `NEW_PAYEE` | the payer has never paid this account |
| `AMOUNT_SPIKE` | the amount is more than 3 times the payer's largest earlier payment, or large with no history |
| `BALANCE_DRAIN` | the amount is at least 80% of the spendable balance |
| `VOP_NO_MATCH`, `VOP_CLOSE_MATCH`, `VOP_NOT_POSSIBLE` | the payee name check result |
| `FRESH_RECEIVER` | the receiving account has never been paid |
| `PASS_THROUGH` | most of the amount arrived from other customers in the last 60 minutes |

Decision: 70 points or more blocks, 40 or more warns. A warned payer decides. The probability
that a warned victim goes ahead is an assumption (`victim_ignores_warning`). Fraudsters are
reported to coach victims past warnings (EPC162-24 notes they convince customers that the
system has a flaw), but the value used is not measured.

## How the simulation keeps the comparison fair

- One seeded generator builds the population and the schedule before anything runs, so every
  policy sees the same customers, the same normal payments and the same scams.
- Choices that depend on the policy (does a warned victim go ahead, how long does a mule wait)
  use a hash of the seed and the case, not the shared generator, so they are the same choice
  under every policy.
- Each mule gets its own generator, because a second-level mule only exists when the first
  payment went through.
- Every run ends with `verify_invariants`, and the results say whether it held.
- The detailed results use one seed. A spread table reports the minimum, median and maximum
  over seeds 1 to 10.

## What the results mean and do not mean

- The same author wrote the scenarios and the rules. A rule that matches a scenario detail will
  look good. This is circular by construction, and the results measure the rules against
  these assumptions only.
- The amounts, probabilities and delays are assumptions, listed in the results file.
- The false-positive figure (normal payments warned or blocked) depends on equally assumed
  habits of normal customers. It shows that a trade-off exists, not its real size.
- Assessment time is measured on one machine with SQLite. It is a property of this code,
  not of a bank system, and it excludes network and message handling.
- Cash withdrawals are not scored, because they are not instant credit transfers. Mules that
  withdraw cash directly are only stopped by a freeze after the report.

## Not implemented

- Learning models of any kind. The rules are fixed and transparent on purpose.
- Customer-side detail such as device or session signals.
- Who pays for a loss. That is the next step, the regime-based liability report.
