"""Run the scenarios under every policy and write the results as Markdown.

Usage:
    python -m echtzeit.scenario_report --seed 7 --spread-seeds 10 --out docs/results/scenarios-seed-7.md

`--spread-seeds N` also runs seeds 1..N and reports the minimum, median and maximum of the
main figures, so the detailed seed can be judged against the spread.

Every number in the output comes from the run. Latency depends on the machine.
"""

import argparse
import platform
import statistics
from dataclasses import fields
from pathlib import Path

from echtzeit import calibration
from echtzeit.liability import ECB_GBP_PER_EUR, ECB_RATE_DATE, REGIMES, Assumptions, totals
from echtzeit.risk import RiskConfig
from echtzeit.simulation import Policy, ScenarioConfig, SimulationResult, simulate


def euro(cents: int) -> str:
    return f"€{cents / 100:,.2f}"


def duration(delta) -> str:
    if delta is None:
        return "–"
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} ms"


def render_spread(
    runs: dict[int, list[SimulationResult]],
    title: str | None = None,
    note: list[str] | None = None,
) -> list[str]:
    """Minimum, median and maximum over seeds, per policy."""
    seeds = sorted(runs)
    policies = [result.policy for result in runs[seeds[0]]]
    metrics = [
        (
            "Lost after recovery along the traced path, share of attempted",
            lambda r: r.total("lost_cents") / max(1, r.total("attempted_cents")),
            "{:.1%}",
        ),
        (
            "Prevented, share of attempted",
            lambda r: r.total("prevented_cents") / max(1, r.total("attempted_cents")),
            "{:.1%}",
        ),
        (
            "Recovered along the traced path, share of attempted",
            lambda r: r.total("recovered_cents") / max(1, r.total("attempted_cents")),
            "{:.1%}",
        ),
        ("Normal payments warned or blocked", lambda r: r.legit.flagged_share, "{:.1%}"),
        (
            "Normal payments held back by the receiving bank",
            lambda r: r.legit.held_back_by_receiver / max(1, r.legit.payments),
            "{:.1%}",
        ),
    ]
    for regime in REGIMES:

        def victims(r, regime=regime):
            return totals(r.cases, regime, Assumptions()).victim_cents / max(
                1, r.total("attempted_cents")
            )

        def banks(r, regime=regime):
            return totals(r.cases, regime, Assumptions()).psp_cents / max(
                1, r.total("attempted_cents")
            )

        metrics.append((f"Victims bear under {regime.code}, share of attempted", victims, "{:.1%}"))
        if regime.receiving_share or regime.covered_victims:
            metrics.append((f"Banks bear under {regime.code}, share of attempted", banks, "{:.1%}"))
    lines = [
        title or f"## Spread over seeds {seeds[0]}–{seeds[-1]}",
        "",
        "Minimum / median / maximum over the seeds, for each policy.",
        "",
        *([*note, ""] if note else []),
        "| | " + " | ".join(p.value for p in policies) + " |",
        "|---|" + "---|" * len(policies),
    ]
    for label, measure, fmt in metrics:
        cells = []
        for index in range(len(policies)):
            values = [measure(runs[seed][index]) for seed in seeds]
            cells.append(
                " / ".join(
                    fmt.format(v) for v in (min(values), statistics.median(values), max(values))
                )
            )
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    ok = all(result.invariants_ok for results in runs.values() for result in results)
    lines += ["", f"Ledger invariants held in every run: {'yes' if ok else 'NO'}.", ""]
    return lines


def flagged_share(result: SimulationResult) -> float:
    """Normal payments warned, blocked or held back by the receiving bank."""
    legit = result.legit
    return (legit.warned + legit.blocked + legit.held_back_by_receiver) / max(1, legit.payments)


def render_real_rate(results: list[SimulationResult]) -> list[str]:
    rate = calibration.CT_FRAUD_RATE_BY_NUMBER
    per_scam = calibration.normal_payments_per_scam(rate.value)
    lines = [
        "## At the real fraud rate",
        "",
        (
            f"In this simulation about {len(results[0].cases)} of {results[0].legit.payments + len(results[0].cases)} "
            "payments are scams. In the EU/EEA in 2024, fraudulent credit transfers were "
            f"{rate.value * 100:.3f}% of all credit transfers by number ({rate.source.title}, {rate.where}), "
            f"about one in {per_scam:,.0f}. If the share of normal payments flagged here carried over to real "
            "payments, each scam payment would come with this many flagged normal payments:"
        ),
        "",
        "| Policy | Normal payments flagged | Flagged normal payments per scam payment at the real rate |",
        "|---|---|---|",
    ]
    for result in results:
        share = flagged_share(result)
        lines.append(
            f"| {result.policy.value} | {share:.2%} "
            f"| {calibration.flagged_per_scam_at_real_rate(share, rate.value):,.0f} |"
        )
    lines += [
        "",
        "Flagged means warned or blocked by the sending bank, or held back by the receiving bank.",
        "This is an order of magnitude. The real rate covers every kind of credit-transfer fraud and",
        "every credit transfer, and the simulated flag rate depends on assumed habits of normal",
        "customers.",
        "",
    ]
    return lines


def render_uk_check(results: list[SimulationResult]) -> list[str]:
    reimbursed = calibration.UK_REIMBURSED_SHARE
    rejected = calibration.UK_REJECTED_FOR_CARE_SHARE
    lines = [
        "## UK regime compared with the PSR dashboard",
        "",
        f"Real figures ({reimbursed.source.title}): {reimbursed.value:.0%} of the money lost to APP",
        f"scams was reimbursed, and {rejected.value:.0%} of claims were rejected because the customer",
        "did not take enough care. Simulated, over claims whose victim type the UK rules cover:",
        "",
        "| Policy | Claims in scope | Reimbursed share | Rejected for lack of care | Same, if ignoring a warning counted as gross negligence |",
        "|---|---|---|---|---|",
    ]
    strict = Assumptions(ignored_warning_is_gross_negligence=True)
    strict_shares = []
    for result in results:
        base = calibration.uk_outcomes(result.cases, Assumptions())
        tough = calibration.uk_outcomes(result.cases, strict)
        if tough.simulated_rejected_for_care_share is not None:
            strict_shares.append(tough.simulated_rejected_for_care_share)

        def pct(value):
            return "–" if value is None else f"{value:.0%}"

        lines.append(
            f"| {result.policy.value} | {base.cases_in_scope} | {pct(base.simulated_reimbursed_share)} "
            f"| {pct(base.simulated_rejected_for_care_share)} | {pct(tough.simulated_rejected_for_care_share)} |"
        )
    lines += [
        "",
        "The simulated share is higher because the model refunds every in-scope claim except the",
        "excess and never rejects one for lack of care unless the sensitivity switch is on. The",
        "dashboard does not define its denominator in exactly these terms, so the two shares are",
        "not strictly comparable. The comparison shows what the model leaves out; nothing is tuned",
        "to match it.",
        "",
    ]
    highest = max(strict_shares, default=0)
    if highest > rejected.value:
        lines[-1:-1] = [
            f"With the switch on, up to {highest:.0%} of in-scope claims are rejected here, against the",
            f"{rejected.value:.0%} the PSR reports. That argues against reading an ignored warning as",
            "gross negligence.",
            "",
        ]
    return lines


def render_figures() -> list[str]:
    lines = [
        "## Reference figures",
        "",
        "| Figure | Value | Source | Where |",
        "|---|---|---|---|",
    ]
    for figure in calibration.FIGURES:
        if figure.unit == "share":
            value = f"{(figure.value * 100).normalize():f}%"
        else:
            value = f"{figure.value:,} {figure.unit}"
        lines.append(
            f"| {figure.name} | {value} | [{figure.source.title}]({figure.source.url}) | {figure.where} |"
        )
    lines.append("")
    return lines


def render(
    results: list[SimulationResult],
    spread: dict[int, list[SimulationResult]] | None = None,
    calibrated: dict[int, list[SimulationResult]] | None = None,
) -> str:
    config = results[0].config
    risk = results[0].risk_config
    lines = [
        f"# Scenario results (seed {config.seed})",
        "",
        "Generated by `python -m echtzeit.scenario_report`. Do not edit by hand.",
        "",
        "All data is synthetic. The scenarios and the risk rules were written by the same author,",
        "so these numbers show how the rules behave against the assumptions below. They are not",
        "evidence about real fraud, real detection rates or real recovery rates.",
        "",
        "## Policies compared",
        "",
        "| | " + " | ".join(r.policy.value for r in results) + " |",
        "|---|" + "---|" * len(results),
    ]

    def row(label: str, values: list[str]) -> None:
        lines.append(f"| {label} | " + " | ".join(values) + " |")

    row(
        "Scam payments attempted",
        [f"{len(r.cases)} ({euro(r.total('attempted_cents'))})" for r in results],
    )
    row("Prevented before payment", [euro(r.total("prevented_cents")) for r in results])
    row("Recovered along the traced path", [euro(r.total("recovered_cents")) for r in results])
    row("Lost after that recovery", [euro(r.total("lost_cents")) for r in results])
    row(
        "Warned victims who went ahead",
        [str(sum(c.warned_and_ignored for c in r.cases)) for r in results],
    )
    row("Receiving-side freezes", [str(r.receiver_freezes) for r in results])
    row(
        "Scam payments held back by the receiving bank",
        [str(sum(c.held_back for c in r.cases)) for r in results],
    )
    row("Normal payments", [str(r.legit.payments) for r in results])
    row(
        "Normal payments warned",
        [f"{r.legit.warned} ({r.legit.warned / max(1, r.legit.payments):.1%})" for r in results],
    )
    row("Normal payments blocked", [str(r.legit.blocked) for r in results])
    row(
        "Normal payments held back by the receiving bank",
        [str(r.legit.held_back_by_receiver) for r in results],
    )
    row("Normal payments refused by the bank", [str(r.legit.refused_by_bank) for r in results])
    for percent in (50, 95, 99):
        row(f"Assessment time p{percent}", [_ms(r.latency_percentile(percent)) for r in results])
    row("Ledger invariants hold", ["yes" if r.invariants_ok else "NO" for r in results])
    lines += [
        "",
        "Recovered money is what holds stopped along the traced path after the victim's report,",
        "measured with the lower-bound attribution rule (`TRACED_FUNDS_FIRST`). Normal payments refused by the bank",
        "are mostly payments that found too little money on the account.",
        f"Assessment time was measured on {platform.system()} {platform.machine()} with SQLite; it",
        "covers the payee-name check and the risk rules, not network or message handling.",
        "",
        "Scam amounts in the safe-account scenario are a share of the victim's balance at that",
        "moment, so they can differ slightly between policies when earlier payments differed.",
        "Recovered money is lower under the scoring policies partly because fewer scam payments",
        "were made at all; compare the shares in the spread table.",
        "",
    ]
    if spread:
        lines += render_spread(spread)
    lines += render_real_rate(results)
    if calibrated:
        mean = next(iter(calibrated.values()))[0].config.scam_amount_mean_cents
        seeds = sorted(calibrated)
        lines += render_spread(
            calibrated,
            title=(
                f"## Sensitivity: scam amounts around EUR {mean / 100:,.0f}, seeds {seeds[0]}–{seeds[-1]}"
            ),
            note=[
                "Scam amounts drawn between half and one and a half times the EBA-ECB average value",
                "of a fraudulent credit transfer (the shape of that distribution is an assumption).",
                "Every other setting is unchanged.",
            ],
        )
    lines += render_uk_check(results)

    for result in results:
        lines += [
            f"## Cases under {result.policy.value}",
            "",
            "| Case | Kind | Decision (points) | Rules | VoP | Paid | Report after | Left on accounts (low–high) | Recovered | Lost |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for c in result.cases:
            decision = "not scored" if c.decision is None else f"{c.decision} ({c.points})"
            lines.append(
                f"| {c.case_id} | {c.kind} | {decision} | {', '.join(c.reasons) or '–'} | {c.vop or '–'} "
                f"| {'yes' if c.executed else 'no'} | {duration(c.report_delay)} "
                f"| {euro(c.remaining_lower_cents)}–{euro(c.remaining_upper_cents)} "
                f"| {euro(c.recovered_cents)} | {euro(c.lost_cents)} |"
            )
        lines.append("")

    lines += render_liability(results)
    lines += render_figures()
    lines += ["## Assumptions (ScenarioConfig)", "", "| Setting | Value |", "|---|---|"]
    for item in fields(config):
        lines.append(f"| `{item.name}` | {getattr(config, item.name)} |")
    lines += ["", "## Risk rules (RiskConfig)", "", "| Setting | Value |", "|---|---|"]
    for item in fields(risk):
        lines.append(f"| `{item.name}` | {getattr(risk, item.name)} |")
    lines.append("")
    return "\n".join(lines)


def render_liability(results: list[SimulationResult]) -> list[str]:
    assumptions = Assumptions()
    lines = [
        "## Who pays",
        "",
        "The loss of each paid scam (amount paid minus money recovered) is shared between the",
        "victim, the victim's bank (sending PSP) and the bank of the first mule (receiving PSP)",
        "under each regime. Regime rules and their sources are in `echtzeit/liability.py` and",
        "[docs/design/liability.md](../design/liability.md).",
        "",
        "Recovery reach differs by regime. `BR_MED` counts money stopped anywhere on the traced",
        "path, because Brazil's DICT funds recovery blocks along the path. The other regimes",
        "count only money stopped on the first receiving account, because they have no",
        "system-wide tracing; this understates what cooperating banks may recover.",
        "",
        "- `UK_PSR` is applied as a what-if: the real scheme covers sterling Faster Payments and",
        (
            f"  CHAPS. Cap and excess are converted at the ECB rate of {ECB_RATE_DATE} "
            f"(£{ECB_GBP_PER_EUR} per €1)."
        ),
        "- `EU_PSR_PROVISIONAL` follows the Council's position at first reading (21 October 2026),",
        "  which is not in force. Under its art. 87(3) the payer's bank refunds when it cannot show",
        "  that both banks monitored the payment. Only `FULL_CHAIN` has the receiving bank screen",
        "  incoming payments, so under the other policies that rule decides every case; the",
        "  sensitivity table applies the EU rules as if receiving banks had screened.",
        "- `BR_MED` is applied as a what-if to euro payments. It returns blocked money and makes",
        "  no bank pay for the loss.",
        "- Invoice scams hit businesses. Whether a business counts as a microenterprise is an",
        "  assumption (`microenterprise_share`).",
        "",
        "| Policy | Regime | Recovery | Paid scams | Covered | Loss | Victims | Sending banks | Receiving banks |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        for regime in REGIMES:
            t = totals(result.cases, regime, assumptions)
            lines.append(
                f"| {result.policy.value} | {regime.code} | {regime.recovery.value} | {t.cases} "
                f"| {t.covered} | {euro(t.loss_cents)} "
                f"| {euro(t.victim_cents)} | {euro(t.sending_psp_cents)} | {euro(t.receiving_psp_cents)} |"
            )
    variants = [
        (
            "Ignoring a warning treated as gross negligence (a test; the UK rules set a high bar)",
            Assumptions(ignored_warning_is_gross_negligence=True),
            [r for r in REGIMES if r.code == "UK_PSR"],
        ),
        (
            "Receiving banks also screen incoming payments",
            Assumptions(receiving_banks_monitor_incoming=True),
            [r for r in REGIMES if r.code == "EU_PSR_PROVISIONAL"],
        ),
        (
            "Receiving banks screen incoming payments, fraudsters did not use the bank's channels",
            Assumptions(
                receiving_banks_monitor_incoming=True, impersonation_via_bank_channels=False
            ),
            [r for r in REGIMES if r.code == "EU_PSR_PROVISIONAL"],
        ),
    ]
    lines += [
        "",
        "Sensitivity checks. Each row changes one or two assumptions; they are tests of the",
        "rules, not readings of them.",
        "",
        "| Variant | Policy | Regime | Covered | Victims | Banks |",
        "|---|---|---|---|---|---|",
    ]
    for label, variant, regimes in variants:
        for result in results:
            if result.policy.value == "NONE" and variant.ignored_warning_is_gross_negligence:
                continue  # nobody is warned when nothing is scored
            for regime in regimes:
                t = totals(result.cases, regime, variant)
                lines.append(
                    f"| {label} | {result.policy.value} | {regime.code} | {t.covered} "
                    f"| {euro(t.victim_cents)} | {euro(t.psp_cents)} |"
                )
    lines += [
        "",
        "Under `NONE` the sending bank does not monitor either, so EU art. 87(3) still applies in",
        "the receiving-bank variants.",
        "",
        "Settings used: " + ", ".join(f"`{k}={v}`" for k, v in vars(assumptions).items()) + ".",
        "",
    ]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=ScenarioConfig.seed)
    parser.add_argument("--spread-seeds", type=int, default=0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    def run_all(seed: int, **overrides) -> list[SimulationResult]:
        config = ScenarioConfig(seed=seed, **overrides)
        return [simulate(policy, config, RiskConfig()) for policy in Policy]

    results = run_all(args.seed)
    spread = {seed: run_all(seed) for seed in range(1, args.spread_seeds + 1)} or None
    eba_mean_cents = int(calibration.CT_FRAUD_AVERAGE_EUR.value * 100)
    calibrated = {
        seed: run_all(seed, scam_amount_mean_cents=eba_mean_cents)
        for seed in range(1, args.spread_seeds + 1)
    } or None
    text = render(results, spread, calibrated)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
