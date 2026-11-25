# Design note: holds and tracing

Status: implemented in version 0.1. All amounts are fictional euro cents.

## Why

Instant payments are final. When a scam is reported, the useful question is how much of the
money can still be stopped. Answering it needs two things the ledger did not have:

1. A way to stop money on an account without moving it (a **hold**).
2. A way to follow a payment forward through later transfers (a **trace**).

The liability report will use the result: money still on an account when the scam is
reported can be frozen, and money that has moved on cannot, unless it is followed further.

## Holds

- An account has `held_cents`. Spendable money is `balance_cents - held_cents`.
  A payment that needs more than that is refused as insufficient funds.
- A hold is an immutable row in `holds` (account, amount, reason, who placed it, optional
  UETR of the payment that triggered it). A release is an immutable row in `hold_releases`.
  A hold is active when it has no release. `accounts.held_cents` is a cache of the sum of
  active holds, and `verify_invariants` checks that the two agree.
- A hold records its position in the ledger: the highest ledger entry id at that moment.
  Tracing uses this to replay holds and entries in the right order.
- `place_hold` holds an exact amount and fails if the account cannot cover it.
  `freeze_available` holds as much as is available up to a requested amount and records both
  numbers, because "requested 10,000, secured 4,000" is the fact the report needs.
- A system account cannot be held. Holds can only be placed on customer accounts.
- Not enforced: who may place a hold. There is no authentication yet. The `placed_by` field
  is a free-text label.
- Not implemented: moving held money back to the victim. A hold only stops the money.

## Tracing

Given the UETR of a payment, the trace replays, in ledger order, every transfer made after
that payment's credit. Each account has two pools: traced money and other money. Just before
the payment, all money on every account is other money. The payment's amount then becomes
traced money on the receiving account.

For each later transfer, the debit is split between the sender's two pools by an
attribution rule. The traced part moves to the receiver's traced pool, and the rest moves to
the receiver's other pool. A transfer that carries traced money is recorded as a hop.

There is one pair of pools per account. That matters when traced money reaches an account by
two paths, or comes back to an account it left. A first version built a tree and replayed each
branch on its own. A test then showed that the same cents could be counted on two branches
(400 cents of traced money reported on an account holding 200). The result is therefore a
graph: a list of accounts and a list of hops.

Mixed money cannot be attributed exactly, because cents are interchangeable. The caller must
choose one of two rules (there is no default):

| Rule | A debit is taken from | Effect |
|---|---|---|
| `OWN_FUNDS_FIRST` | other money first, traced money only when other money is exhausted | Most traced money stays on accounts. Upper bound for what can be frozen. |
| `TRACED_FUNDS_FIRST` | traced money first | Least traced money stays. Lower bound for what can be frozen. |

These are two bounds for an estimate. They are not legal tracing doctrines, and no claim is
made about which one a court or a bank would apply. A report should show both.

Holds take part in the replay. A hold stops money whatever its tag, so the ledger never lets
held money move. The attribution question is only which pool a hold stops first: holds
tagged with the traced UETR stop traced money first, other holds stop other money first, and
either spills onto the other pool when its own pool is too small. Together the stopped parts
always equal the smaller of the balance and all active holds, which is exactly what the ledger
refuses to let move. Money stopped this way counts as secured.

This matters because a receiving bank may freeze a mule account without knowing which scam
the money came from. An earlier version counted only tagged holds as securing traced money and
under-reported recovery in that case. A test now covers it.

Because the replay's spendable money always equals the ledger's, a debit the replay cannot
cover can only mean a bug or tampering. The replay then raises `TraceInconsistency` instead of
returning a wrong number.

Depth and the depth limit: an account's depth is the fewest hops from the receiving account
among the hops replayed so far. Traced money that leaves an account at the depth limit is
counted as not followed, and the receiver gets it as other money. Because an account's depth
can only decrease over time, a hop replayed before a shorter path appeared is judged by the
longer depth. This is a simplification.

System accounts (the source of fictional money) can go negative. They never pass traced money
on, and traced money that reaches them stays there.

Result: for every account, the traced money that arrived, the money still there, the part of it
held by this payment's holds, the money that moved on and the money not followed; plus the
hops. Conservation is checked by tests: the payment amount equals the traced money remaining
on all accounts plus the money not followed.

## Freezing traced money

`freeze_traced_funds` traces a payment and places a hold on every node for the money
remaining there, in one database transaction, so nothing moves between the trace and the
holds. The attribution rule is a required argument, because it decides how much innocent
money might be frozen. The safe choice for avoiding wrongful freezes is
`TRACED_FUNDS_FIRST`, since it freezes the least.

## Limits

- The two-pool model gives bounds, not the truth.
- Each trace replays every transfer after the payment. That is fine for a simulator and too
  slow for a large ledger, which would need an index of hops or a stored running state.
- The depth limit (default 5) is a setting of this simulator.
- Trace and freeze rely on SQLite serializing writers. On a database with row locks they
  would need explicit locking.
- Speed is not modelled here. Real freezes take time, and every hop before the freeze is
  money that cannot be recovered. The scenario step models that delay.
- Regime rules (who pays, deadlines) are not part of this module.
