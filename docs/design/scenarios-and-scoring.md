# Design note: scam scenarios, payee check and risk score

Status: implemented in version 0.1. All data is synthetic and every amount is fictional.

## What this step answers

For the same synthetic population and the same scams, how much money is lost, prevented and
recovered under four policies, and what does that cost normal customers?

| Policy | What the banks do |
|---|---|
| `NONE` | Nothing before the payment. After the victim reports, the payment is traced and the money still on accounts is frozen. |
| `SENDER_ONLY` | The sending bank checks the payee name and scores every payment: allow, warn or block. |
| `SENDER_AND_RECEIVER` | As above. When an outgoing payment is blocked because money is passing straight through an account, that bank also freezes the money that just arrived. |
| `FULL_CHAIN` | As above, and the payee's bank screens every incoming payment before crediting it. A payment it holds back is rejected (`RR04`) and nothing moves. |

The freeze in `SENDER_AND_RECEIVER` acts when the money tries to leave the account. The
incoming screen in `FULL_CHAIN` acts when it arrives, which is the moment art. 73(3) of the EU
Payment Services Regulation addresses (Council position at first reading, 21 October 2026,
not in force). Under that article the payee's PSP may hold back suspicious funds and return
them, and must when the reasons are clear and incontrovertible. `FULL_CHAIN` is also the only
policy in which both banks monitor, which EU art. 87(3) asks for (see
[liability.md](liability.md)).

## Scenario patterns and their sources

The patterns are paraphrased from the EPC *2025 Payments Threats and Fraud Trends Report*
(EPC162-24, version 2.0, issued 19 November 2025). They stay at the level of detail that
report publishes.

| Scenario | What happens | Source |
|---|---|---|
| Safe account | A fraudster posing as the bank tells the customer the account is at risk and that the money must go to a "safe" account the fraudster controls. The customer makes the payment. | EPC162-24, section 2, page 17 (bank employee impersonation, safe account fraud) |
| Invoice with a changed IBAN | An invoice keeps the real supplier's name but carries the IBAN of an account the fraudster controls. The business pays it. | EPC162-24, executive summary, page 7 (the invoicing stage is exposed to IBAN manipulation) |
| Money mules | Mule accounts receive the money and pass it on so it cannot be tracked, often ending in a cash withdrawal. One or two levels of mules. | EPC162-24, section 3.1.7.1, page 44 (most cases involve no more than one or two levels of mules) |

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
system has a flaw, section 2, page 17), but the value used is not measured.

## Incoming screen (payee's bank)

| Rule | Fires when | Points |
|---|---|---|
| `NAME_MISMATCH` | the name in the payment message does not match the account holder | 30 |
| `FIRST_CREDIT` | the account has never received a payment from another customer | 25 |
| `LARGE_FIRST_CREDIT` | the amount is at least €1,000 and more than 3 times any earlier payment to the account | 20 |

A payment is held back at 45 points, but only if a rule other than `NAME_MISMATCH` fired: EU
art. 73(3) does not allow the decision to rest solely on the payee-name check. The points
and the threshold are this project's choices and are not calibrated. The EPC lists no reason
code for "held back as suspected fraud", so `RR04` (regulatory reason) is used; that is also
this project's choice.

## Normal payments that look like scams

Without them the results would flatter every rule about new payees and new accounts. Each run
includes `new_account_gifts` (6 by default) large first payments from a relative to a family
member's account opened during the measured period. Under `FULL_CHAIN` the incoming screen
holds these back, and the results file counts them as normal payments held back by the
receiving bank.

## Calibration against real figures

No public transaction-level dataset of real APP scams or SEPA Instant fraud was found
(searched in November 2026). The public fraud datasets are either real but about other fraud
(the ULB credit-card set: card payments from September 2013, anonymised features) or
synthetic (PaySim, IBM AML, SAML-D, Feedzai's Bank Account Fraud suite). Adding them would not
make the results less synthetic. Real figures exist only as aggregates, and
`echtzeit/calibration.py` holds the ones used, each with its source and page:

- EBA and ECB, 2025 Report on payment fraud (data for 2024): fraudulent credit transfers were
  0.002% of all credit transfers by number (page 15) and 0.001% by value (page 6). The average
  fraudulent credit transfer was EUR 2,155 (page 15). Users bore around 85% of credit-transfer
  fraud losses (page 7).
- Payment Systems Regulator, APP scams reimbursement dashboard (7 October 2024 to 31 March
  2026): 88% of the money lost to APP scams was reimbursed, and 3% of claims were rejected for
  lack of care.

The results file uses them in three ways:

1. At the real fraud rate. About 3% of simulated payments are scams, against about one in
   50,000 credit transfers in reality. The share of normal payments flagged is converted into
   flagged normal payments per scam payment at the real rate. This is an order of magnitude:
   the real rate covers all credit-transfer fraud and all credit transfers.
2. Amount sensitivity. A second spread over the same seeds draws scam amounts between half and
   one and a half times EUR 2,155. The shape of that distribution is an assumption.
3. UK check. The simulated UK regime is set against the PSR dashboard. Nothing is tuned to
   match it; the gap shows what the model leaves out.

## How the simulation keeps the comparison fair

- One seeded generator builds the population and the schedule before anything runs, so every
  policy sees the same customers, the same normal payments and the same scams.
- Choices that depend on the policy (does a warned victim go ahead, how long does a mule wait)
  use a hash of the seed and the case, not the shared generator, so they are the same choice
  under every policy.
- Each mule gets its own generator, because a second-level mule only exists when the first
  payment went through.
- An established mule's earlier payment comes from an account created for that purpose, not
  from a real customer. An earlier version used a random customer, and the run failed when
  that customer had no money left.
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
- Who pays for a loss is a separate step: see [liability.md](liability.md).
