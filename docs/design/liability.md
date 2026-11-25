# Design note: who pays for a scam loss

Status: implemented in version 0.1 (`echtzeit/liability.py`). Sources checked in November 2026.

## Question

For each paid scam in the simulation, the loss is the amount paid minus the money recovered by
holds. Who carries that loss — the victim, the victim's bank (sending PSP) or the bank that
holds the first mule's account (receiving PSP) — under different reimbursement regimes?

## Regimes

| Rule | `UK_PSR` | `EU_PSR_PROVISIONAL` | `NO_REFUND` |
|---|---|---|---|
| Status | in force since 7 October 2024 | provisional agreement of 27 November 2025, not in force | baseline |
| Victims covered | consumers, microenterprises (and charities, not simulated) | consumers | none |
| Scams covered | all APP scams | fraud where the fraudster impersonates the consumer's PSP (the safe-account scenario) | none |
| Cap per claim | £85,000 | none modelled | – |
| Excess | optional, up to £100 | none | – |
| Who pays the refund | sending and receiving PSP, 50:50 | the consumer's PSP | – |
| Recovered money | shared by the split, PSPs keep no more than they paid, the rest to the victim | to the refunding PSP, the rest to the victim | to the victim |

UK sources (all from the Payment Systems Regulator): the consumer page on APP fraud
reimbursement protections (scope, start date, cap, excess, exceptions), policy statement
PS24/7 (cap of £85,000 from 7 October 2024), the APP scams overview (50:50 split) and the
policy clarifications page. The clarifications settle three details used here:

- With mule accounts, only the payment from the victim to the first mule is in scope, and the
  first mule's PSP is the receiving PSP.
- Repatriated funds are shared between the PSPs by their split, and PSPs should not keep more
  than their losses.
- When the sending PSP does not deduct the excess, the receiving PSP may lower its
  contribution by 50% of the maximum excess (£50).

EU sources: the Council press release of 27 November 2025 (read directly), the European
Parliament press release 20251121IPR31540 (read through search-result excerpts, because the
page refused automated access) and the Parliament's answer to written question E-000069/26,
which describes the refund as a right for consumers. The legal text itself could not be read
here, because EUR-Lex returned no content to automated requests. The EU regime is therefore
modelled from official summaries, and the final text may differ. Secondary sources (law-firm
notes) report that the Council adopted its first-reading position in October 2026 and that the
Parliament's second reading was still pending; they were not used for any rule.

## Interpretation choices (this project's, not the regulators')

- The UK regime is applied as a what-if to simulated euro payments. The sterling cap and excess
  are converted at the ECB reference rate of 24 November 2026 (£0.83700 per €1).
- An invoice whose IBAN was changed is treated as an authorised payment under the EU text, not
  as a transaction the fraudster initiated or changed, so the EU impersonation refund does not
  apply to it. Business victims are outside the EU consumer refund anyway.
- Under the EU regime the whole refund falls on the consumer's PSP. Nothing in the summaries
  read describes a split with the receiving PSP. The EU rules on liability of online platforms
  towards PSPs are not modelled.
- When a refund has an odd cent, the sending PSP carries it. Recovered money is shared by
  integer division, so the receiving PSP can end up a few cents above the sending PSP in totals.

## Assumptions that can be switched

| Setting | Default | Why it matters |
|---|---|---|
| `sending_psp_applies_excess` | yes | UK victims carry up to £100 per claim |
| `victims_report_to_police_and_psp` | yes | condition of the EU refund; reporting also starts the freeze |
| `ignored_warning_is_gross_negligence` | no | a sensitivity test; the UK rules set a high bar for gross negligence and ignoring a warning is not gross negligence by itself |

## Not modelled

Claim deadlines and time limits for reimbursement (the simulated victims report within days),
vulnerability assessments, first-party fraud, the "stop the clock" mechanism, liability for
failing to offer Verification of Payee (the simulation always runs it) and any regime outside
the three above. Brazil's MED is planned for a later version, after its rules are read from
the Banco Central texts.

## What the results can show

The results file lists, for every policy and regime, how much the victims and the banks carry.
They show how the same losses move between parties when the rules change, and how much of that
cost detection removes for each party. They rest on the same synthetic scenarios and
assumptions as the rest of the simulation and say nothing about real losses.
