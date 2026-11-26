# Design note: who pays for a scam loss

Status: implemented in version 0.1 (`echtzeit/liability.py`). Sources checked in November 2026.

## Question

For each paid scam in the simulation, the loss is the amount paid minus the money recovered by
holds, as far as the regime's recovery reaches. Who carries that loss: the victim, the
victim's bank (sending PSP) or the bank that holds the first mule's account (receiving PSP)?

## Regimes

| Rule | `UK_PSR` | `EU_PSR_PROVISIONAL` | `NO_REFUND` | `BR_MED` |
|---|---|---|---|---|
| Status | in force since 7 October 2024 | Council position at first reading of 21 October 2026, not in force | baseline | in force; funds recovery since 29 August 2025 |
| Who is protected | consumers, microenterprises (and charities, not simulated) | every payer for art. 87 and 68; consumers for art. 60 | nobody | nobody: MED returns blocked money, no PSP pays for the loss |
| What moves the loss | any APP scam | a PSP that did not monitor (art. 87(3)); a payment executed despite justified suspicion (art. 68(2)); PSP impersonation through the PSP's channels (art. 60) | – | – |
| Cap per claim | £85,000 | none | – | – |
| Excess | optional, up to £100 | none | – | – |
| Who pays | sending and receiving PSP, 50:50 | the payer's PSP | – | – |
| Recovery reach | first receiving account | first receiving account | first receiving account | along the traced path |
| Recovered money | shared by the split, PSPs keep no more than they paid, the rest to the victim | to the payer's PSP | to the victim | to the victim |

## Recovery reach

The simulation freezes traced money along the whole path once a victim reports. That is what
Brazil's funds recovery does, but the UK and EU rules contain no comparable system-wide
tracing. Each case therefore records two figures: money stopped anywhere on the traced path,
and money stopped on the first receiving account (the first mule). `BR_MED` uses the first,
the other regimes use the second. This understates what cooperating banks may recover in the
UK and the EU, and it is a modelling choice, not a statement about any bank.

## Sources

UK (all from the Payment Systems Regulator): the consumer page on APP fraud reimbursement
protections (scope, start date, cap, excess, exceptions), policy statement PS24/7 (cap of
£85,000 from 7 October 2024), the APP scams overview (50:50 split) and the policy
clarifications page. The clarifications settle three details used here:

- With mule accounts, only the payment from the victim to the first mule is in scope, and the
  first mule's PSP is the receiving PSP.
- Repatriated funds are shared between the PSPs by their split, and PSPs should not keep more
  than their losses.
- When the sending PSP does not deduct the excess, the receiving PSP may lower its
  contribution by 50% of the maximum excess (£50).

EU: the legal text, read directly. The Council's position at first reading on the Payment
Services Regulation (document 8543/26, 21 October 2026) is the basis. The final compromise
text (8221/26, 17 April 2026) contains the same rules under earlier article numbers. The
Regulation is not in force, and the Parliament's second reading was not confirmed in the
sources read. The rules modelled:

- Art. 87(3). The payer's PSP must monitor before executing a payment. The payee's PSP must
  monitor before making the funds available. A PSP that does not is liable to the payer. If
  the payer's PSP cannot show that both PSPs monitored, it refunds the payer, who bears
  nothing unless the payer acted fraudulently.
- Art. 68(2) and (4). With objectively justified reasons to suspect fraud, the payer's PSP
  must refuse an instant payment, otherwise the payer bears no loss. The reasons may not rest
  solely on the Verification of Payee result, and an unusual order is not by itself such a
  reason.
- Art. 60. A consumer manipulated by someone pretending to be the consumer's PSP, using
  communication channels attributed to that PSP, is refunded in full within 15 business days.
  The consumer must have notified the PSP and reported to the police. The refund is refused
  only if the PSP proves fraud or gross negligence by the consumer.
- Art. 50(1a). A transaction is unauthorised only if a third party initiated or modified it.
- Art. 27(1). Business users may contract out of several articles, but not of 50(1a), 60, 68
  or 87.
- Art. 73(3) is not modelled. It lets the payee's PSP hold back and return suspicious incoming
  funds, and requires it when the case is clear and incontrovertible. No simulated policy
  screens incoming payments.

Brazil (both from the Banco Central do Brasil, read directly):

- Resolução BCB nº 493 of 28 August 2025, which amends the Pix regulation. The receiving PSP
  blocks the amount requested or, if smaller, the available balance, immediately after the
  notification (art. 41-D). The payer's PSP starts funds recovery when there is well-founded
  suspicion of fraud after the payer's complaint (art. 78-N). The resolution took effect on
  publication, with one paragraph revoked from 2 February 2026 (art. 4).
- Manual Operacional do DICT, version 8.5, section 20. The DICT builds a tracking graph from
  the contested payment and selects the most likely path. Every prioritised receiving PSP
  blocks at once up to the available balance, and blocks again when new money arrives.
  Returns go in sequence to the original payer, up to the contested amount. A payment can be
  contested for 80 days.

## Interpretation choices (this project's, not the regulators')

- The UK and Brazilian regimes are applied as what-ifs to simulated euro payments. The
  sterling cap and excess are converted at the ECB reference rate of 24 November 2026
  (£0.83700 per €1).
- EU art. 87(3): a sending bank that scores payments counts as monitoring. No simulated policy
  has the receiving bank screen incoming payments before crediting them, so by default
  art. 87(3) decides every EU case and the payer's PSP pays. A sensitivity check applies the
  rules as if receiving banks did screen.
- EU art. 68(2): a payment counts as executed despite justified suspicion when the bank's own
  score warned about it and a rule other than unusualness (new payee, amount, balance share)
  or the payee-name check fired. That is, `FRESH_RECEIVER` or `PASS_THROUGH`. The Regulation
  does not define these reasons further.
- EU art. 60: whether the simulated fraudsters used channels attributed to the bank, such as
  a spoofed number, is unknown. The default assumes they did, and a sensitivity check assumes
  they did not.
- EU art. 50(1a): an invoice whose IBAN was changed was paid by the payer from a forged
  document. The payment order itself was not initiated or modified by a third party, so the
  payment is authorised.
- EU: the articles read do not say which PSP finally carries a refund when the payee's PSP
  failed to monitor. The payer's PSP pays it here.
- When a UK refund has an odd cent, the sending PSP carries it. Recovered money is shared by
  integer division, so the receiving PSP can end up a few cents above the sending PSP in totals.

## Assumptions that can be switched

| Setting | Default | Why it matters |
|---|---|---|
| `sending_psp_applies_excess` | yes | UK victims carry up to £100 per claim |
| `victims_report_to_police_and_psp` | yes | condition of the UK claim and of EU art. 60; reporting also starts the freeze |
| `impersonation_via_bank_channels` | yes | condition of EU art. 60 |
| `receiving_banks_monitor_incoming` | no | whether EU art. 87(3) is met on the receiving side |
| `ignored_warning_is_gross_negligence` | no | a sensitivity test. The UK rules set a high bar for gross negligence. In the EU it only affects art. 60, because art. 87 and 68 protect the payer unless the payer acted fraudulently. |

## Not modelled

Claim deadlines and time limits for reimbursement (the simulated victims report within days),
vulnerability assessments, first-party fraud, the "stop the clock" mechanism, liability for
failing to offer Verification of Payee (the simulation always runs it), EU art. 73(3) and any
regime outside the four above. For `BR_MED`: re-blocking when new money reaches a blocked
account, the DICT's own prioritisation algorithm (the simulation's trace is used instead) and
the analysis step in which a receiving PSP may reject a notification.

## What the results can show

The results file lists, for every policy and regime, how much the victims and the banks carry.
They show how the same losses move between parties when the rules change, and how much of that
cost detection removes for each party. They rest on the same synthetic scenarios and
assumptions as the rest of the simulation and say nothing about real losses.
