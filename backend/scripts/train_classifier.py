#!/usr/bin/env python
"""Train the support-triage ticket classifier.

This is the one place in Helix where a model is *trained* rather than prompted.
Two TF-IDF + logistic-regression pipelines (one for priority, one for category)
are fitted on a labelled ticket corpus, evaluated on a stratified held-out split,
and saved to `app/support/classifier.pkl`.

The point is cost and latency: a logistic regression classifies a ticket in
under a millisecond for zero marginal cost, so the LLM is only consulted for the
tickets the classifier is genuinely unsure about (see
`CLASSIFIER_CONFIDENCE_THRESHOLD`). On the corpus below that routes the large
majority of tickets away from the LLM entirely.

Usage:
    python -m scripts.train_classifier [--out PATH] [--seed 42] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, classification_report, precision_recall_fscore_support
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.pipeline import Pipeline

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "app" / "support" / "classifier.pkl"

# --------------------------------------------------------------------------- #
# Labelled corpus
# --------------------------------------------------------------------------- #
# Templates carry the label signal; slots add lexical variety so the vectoriser
# cannot simply memorise whole sentences.

PRODUCTS = ["the dashboard", "the API", "the mobile app", "the export tool", "the admin console"]
PLANS = ["Free", "Pro", "Team", "Enterprise"]
AMOUNTS = ["$49", "$120", "$19.99", "$450", "$1,200"]


@dataclass(frozen=True)
class Template:
    text: str
    priority: str
    category: str


TEMPLATES: tuple[Template, ...] = (
    # -- billing ----------------------------------------------------------
    Template(
        "I was charged {amount} twice this month, please refund the duplicate payment", "high", "billing"
    ),
    Template(
        "My invoice shows {amount} but my {plan} plan should cost less, can you check", "medium", "billing"
    ),
    Template("How do I download past invoices for accounting", "low", "billing"),
    Template("I cancelled my subscription last month but was billed {amount} again", "high", "billing"),
    Template(
        "Payment failed with card declined and our account is now suspended, the team is blocked",
        "urgent",
        "billing",
    ),
    Template("Can you switch my billing from monthly to annual on the {plan} plan", "low", "billing"),
    Template("We need a VAT receipt for the {amount} charge", "low", "billing"),
    Template("The refund I was promised of {amount} has not arrived after two weeks", "high", "billing"),
    Template("Please update the credit card on file for our {plan} subscription", "medium", "billing"),
    Template(
        "Our purchase order was approved, how do we pay by invoice instead of card", "medium", "billing"
    ),
    Template("I upgraded to {plan} but I am still being charged the old rate", "high", "billing"),
    Template(
        "Billing portal shows an outstanding balance of {amount} that we already paid", "high", "billing"
    ),
    Template("Where do I add a billing contact so finance receives the receipts", "low", "billing"),
    Template(
        "The proration on my upgrade invoice looks wrong, it charged a full {amount}", "medium", "billing"
    ),
    Template(
        "Our card expired and every payment retry is failing, service will lapse tomorrow",
        "urgent",
        "billing",
    ),
    Template("Can we get a quote for annual prepay on the {plan} tier", "low", "billing"),
    Template(
        "Why does my receipt show tax when we submitted a tax exemption certificate", "medium", "billing"
    ),
    Template("I need a copy of every invoice from last financial year for our audit", "low", "billing"),
    Template("We were double billed for the seats we removed, that is {amount} owed back", "high", "billing"),
    Template("The discount coupon we agreed was never applied to the subscription", "medium", "billing"),
    Template(
        "Stop the automatic renewal, we do not want to be charged {amount} next cycle", "high", "billing"
    ),
    Template("Can you explain what the usage overage line item on my bill covers", "low", "billing"),
    Template(
        "Finance says the wire transfer cleared but the account still shows unpaid and locked",
        "urgent",
        "billing",
    ),
    Template("Please change the currency on our subscription from dollars to euros", "low", "billing"),
    Template("We are being charged for {plan} seats that were deactivated months ago", "high", "billing"),
    Template("How long does a refund take to appear back on the original card", "low", "billing"),
    # -- bug --------------------------------------------------------------
    Template("{product} returns a 500 error every time I try to save", "high", "bug"),
    Template("Production is down, {product} is completely unreachable for all our users", "urgent", "bug"),
    Template("Getting an exception when uploading a CSV to {product}", "high", "bug"),
    Template("{product} is slow to load, it takes about eight seconds", "low", "bug"),
    Template("The chart on {product} renders with the wrong colours", "low", "bug"),
    Template("Data loss, the records we created yesterday in {product} have disappeared", "urgent", "bug"),
    Template("Search in {product} returns no results even for items that exist", "medium", "bug"),
    Template("{product} crashes on startup after the latest release", "urgent", "bug"),
    Template("Timestamps in {product} are displayed in the wrong timezone", "low", "bug"),
    Template("Webhook deliveries are failing intermittently with a 502 from your endpoint", "high", "bug"),
    Template("Pagination in {product} skips the last page of results", "medium", "bug"),
    Template("A null pointer error appears in the console when filtering {product}", "medium", "bug"),
    Template(
        "Our whole workspace is broken and nobody can log in, this is a critical outage", "urgent", "bug"
    ),
    Template("Sync between {product} and our warehouse silently stopped three days ago", "high", "bug"),
    Template("The save button in {product} does nothing, no request is sent at all", "high", "bug"),
    Template("Uploaded images come back corrupted and unreadable in {product}", "high", "bug"),
    Template("Every API call is timing out after thirty seconds, nothing completes", "urgent", "bug"),
    Template("The mobile layout of {product} overlaps text on small screens", "low", "bug"),
    Template("Duplicate records keep being created whenever we retry a failed import", "medium", "bug"),
    Template("Sorting by date in {product} orders the rows incorrectly", "low", "bug"),
    Template(
        "Deleting a project in {product} also deleted unrelated projects, we lost work", "urgent", "bug"
    ),
    Template("The CSV export is truncated at ten thousand rows without any warning", "medium", "bug"),
    Template("Notifications stopped being delivered after the maintenance window", "high", "bug"),
    Template("There is a typo in the confirmation dialog of {product}", "low", "bug"),
    Template("Rate limit headers report the wrong remaining quota on every response", "medium", "bug"),
    Template("Our background jobs are stuck in the queue and never finish processing", "high", "bug"),
    # -- account ----------------------------------------------------------
    Template("I forgot my password and the reset email never arrives", "high", "account"),
    Template("My account is locked after too many failed login attempts", "high", "account"),
    Template("How do I enable two factor authentication on my profile", "low", "account"),
    Template("Please add three new seats to our {plan} workspace", "medium", "account"),
    Template("I need to transfer ownership of the workspace to another administrator", "medium", "account"),
    Template("Cannot sign in with SSO, it just redirects back to the login page", "high", "account"),
    Template("Please delete my account and all associated personal data", "medium", "account"),
    Template("How do I change the email address on my profile", "low", "account"),
    Template(
        "A former employee still has admin access, this is a security concern, revoke it now",
        "urgent",
        "account",
    ),
    Template("Our SAML certificate expired and the entire team is locked out", "urgent", "account"),
    Template(
        "Can I merge two accounts that were created with different email addresses", "medium", "account"
    ),
    Template("The verification code text message never reaches my phone", "high", "account"),
    Template("I lost my authenticator device and cannot get past the second factor", "high", "account"),
    Template("How do I set my display name and avatar", "low", "account"),
    Template("Please downgrade my colleague from administrator to a standard member", "medium", "account"),
    Template(
        "We suspect unauthorised access to our workspace, please lock it immediately", "urgent", "account"
    ),
    Template("Can we enforce single sign on for everyone in the organisation", "medium", "account"),
    Template("My invitation link expired before I could accept it", "medium", "account"),
    Template("What is the session timeout and can we make it longer", "low", "account"),
    Template("Remove my personal email from the notification recipients list", "low", "account"),
    Template("Our domain changed, we need every user migrated to the new email domain", "medium", "account"),
    Template("Login is rejecting the correct password for every member of the team", "urgent", "account"),
    Template("How do I see which devices are currently signed in to my profile", "low", "account"),
    Template("Please restore the workspace member I accidentally removed this morning", "high", "account"),
    Template("Can guests be prevented from inviting other guests", "medium", "account"),
    Template("The permission change I made to a role has not taken effect", "medium", "account"),
    # -- how_to -----------------------------------------------------------
    Template("How do I export my data from {product} as JSON", "low", "how_to"),
    Template("What is the best way to bulk import users into {product}", "low", "how_to"),
    Template("Where can I find the documentation for the webhooks API", "low", "how_to"),
    Template("How do I set up a scheduled report in {product}", "low", "how_to"),
    Template("Is there a tutorial for connecting {product} to Slack", "low", "how_to"),
    Template("How do I filter results by date range in {product}", "low", "how_to"),
    Template("Can you explain how rate limits work on the {plan} plan", "medium", "how_to"),
    Template("What is the recommended way to rotate API keys", "medium", "how_to"),
    Template("How do I invite a guest user with read only permissions", "low", "how_to"),
    Template("We need a guide for migrating from the legacy endpoint to version two", "medium", "how_to"),
    Template("What is the correct way to paginate through a large result set", "low", "how_to"),
    Template("How should we structure our folders for a team of fifty", "low", "how_to"),
    Template("Is there a sandbox environment we can test against before going live", "medium", "how_to"),
    Template("How do I authenticate a server to server request without a browser", "medium", "how_to"),
    Template("Which fields are required when creating a record through the API", "low", "how_to"),
    Template("How do I roll back to a previous version of a saved view", "low", "how_to"),
    Template("What is the procedure for requesting a data export before we leave", "medium", "how_to"),
    Template("Can you point me to sample code for the batch endpoint", "low", "how_to"),
    Template("How do I configure a custom retention period for old records", "medium", "how_to"),
    Template("What is the difference between a workspace and an organisation", "low", "how_to"),
    Template("How do I test a webhook locally without exposing my machine", "low", "how_to"),
    Template("Which metrics are available for building a dashboard", "low", "how_to"),
    Template("How do we set up staging and production environments separately", "medium", "how_to"),
    Template("Is there a command line tool and where are the install instructions", "low", "how_to"),
    Template("How do I map our existing field names onto yours during import", "medium", "how_to"),
    Template("Where do I find my organisation identifier for the integration", "low", "how_to"),
    # -- feature_request --------------------------------------------------
    Template("It would be nice if {product} supported dark mode", "low", "feature_request"),
    Template("Feature request, allow exporting to Excel as well as CSV", "low", "feature_request"),
    Template("Please consider adding a bulk delete option to {product}", "low", "feature_request"),
    Template("Any plans on the roadmap for a Terraform provider", "low", "feature_request"),
    Template("We would like to see role based permissions added to {product}", "medium", "feature_request"),
    Template("Suggestion, send a weekly digest email summarising activity", "low", "feature_request"),
    Template("Can you add support for custom domains on the {plan} plan", "medium", "feature_request"),
    Template("We need an audit log we can stream into our SIEM", "medium", "feature_request"),
    Template("It would help a lot if {product} had keyboard shortcuts", "low", "feature_request"),
    Template("Please add a way to duplicate an existing configuration", "low", "feature_request"),
    Template(
        "Would you consider building a native integration with our data warehouse",
        "medium",
        "feature_request",
    ),
    Template(
        "An offline mode for {product} would be extremely valuable to our field staff",
        "medium",
        "feature_request",
    ),
    Template(
        "Can we get webhooks for the deletion event, only creation is supported today",
        "medium",
        "feature_request",
    ),
    Template("Please allow the dashboard widgets to be reordered by dragging", "low", "feature_request"),
    Template("We would love a public API for the reporting module", "medium", "feature_request"),
    Template("Is a mobile application on your roadmap for next year", "low", "feature_request"),
    Template(
        "Adding saved filters would remove a lot of repetitive clicking for us", "low", "feature_request"
    ),
    Template("Please support signing in with our internal identity provider", "medium", "feature_request"),
    Template(
        "It would be great to schedule exports to land in cloud storage automatically",
        "medium",
        "feature_request",
    ),
    Template("Could you increase the attachment size limit, ours are larger", "medium", "feature_request"),
    Template("A sandbox reset button would make our testing much easier", "low", "feature_request"),
    Template(
        "Please add comments and mentions so we can collaborate inside {product}", "low", "feature_request"
    ),
    Template(
        "We want granular notification preferences per project rather than global", "low", "feature_request"
    ),
    Template("Consider supporting multiple currencies in the reporting views", "medium", "feature_request"),
    Template(
        "An approval workflow before changes go live would unblock our compliance team",
        "medium",
        "feature_request",
    ),
    Template("Please expose the raw query so we can debug slow reports ourselves", "low", "feature_request"),
    # -- general ----------------------------------------------------------
    Template("Just wanted to say the new {product} update is great", "low", "general"),
    Template("Who should I talk to about a partnership opportunity", "low", "general"),
    Template("Do you have a status page I can subscribe to", "low", "general"),
    Template("What are your support hours in European timezones", "low", "general"),
    Template("Can you send over your SOC 2 report for our vendor review", "medium", "general"),
    Template(
        "We are evaluating {product} for a team of two hundred, who can we speak to", "medium", "general"
    ),
    Template("Please confirm receipt of the signed contract we emailed", "medium", "general"),
    Template("Is there a student discount available", "low", "general"),
    Template("Are you hiring support engineers at the moment", "low", "general"),
    Template("Could you review our case study draft before we publish it", "low", "general"),
    Template("We need your data processing agreement signed for our privacy review", "medium", "general"),
    Template("Which subprocessors do you use and where is our data stored", "medium", "general"),
    Template("Is there a user community or forum we can join", "low", "general"),
    Template("Please add me to your product release announcement mailing list", "low", "general"),
    Template("Our legal team has questions about the liability clause in the agreement", "medium", "general"),
    Template("Can we arrange an onboarding session for our new hires", "medium", "general"),
    Template("Where can I download your brand assets for a conference slide", "low", "general"),
    Template("Do you offer a nonprofit pricing programme", "low", "general"),
    Template("We would like to leave feedback about our experience with the sales process", "low", "general"),
    Template("Can someone from your team join our quarterly business review", "medium", "general"),
    Template("What is your policy on scheduled maintenance notifications", "low", "general"),
    Template("We are writing an article and would like a comment from your team", "low", "general"),
    Template(
        "Please share your penetration test summary for our security questionnaire", "medium", "general"
    ),
    Template("Is there a reseller programme in our region", "low", "general"),
    Template("Our renewal is approaching, who handles contract negotiation", "medium", "general"),
    Template("Thanks for the quick help last week, the issue is fully resolved now", "low", "general"),
)

PREFIXES = (
    "",
    "Hi team, ",
    "Hello, ",
    "Urgent: ",
    "Quick question - ",
    "Following up again: ",
    "Hi support, ",
)
SUFFIXES = (
    "",
    " Thanks in advance.",
    " Please advise.",
    " Let me know as soon as possible.",
    " This is blocking our team.",
    " No rush on this one.",
    " Happy to jump on a call.",
)


def build_dataset(
    seed: int = 42, target_size: int = 300
) -> tuple[list[str], list[str], list[str], list[int]]:
    """Expand templates into a labelled dataset of ~`target_size` tickets.

    Also returns a group id per row (the template it came from). That grouping
    is essential: variants of one template are near-duplicates, so a random
    train/test split would put paraphrases of the same sentence on both sides
    and report ~1.00 accuracy that means nothing. Splitting by *template*
    measures what we actually care about -- generalisation to ticket wording the
    model has never seen.
    """
    rng = random.Random(seed)
    texts: list[str] = []
    priorities: list[str] = []
    categories: list[str] = []
    groups: list[int] = []

    variants_per_template = max(1, round(target_size / len(TEMPLATES)))
    for group_id, template in enumerate(TEMPLATES):
        for _ in range(variants_per_template):
            body = template.text.format(
                product=rng.choice(PRODUCTS), plan=rng.choice(PLANS), amount=rng.choice(AMOUNTS)
            )
            prefix = rng.choice(PREFIXES)
            suffix = rng.choice(SUFFIXES)
            # "Urgent:" is a genuine signal, but only for genuinely urgent
            # tickets -- otherwise it teaches the model a false shortcut.
            if prefix.startswith("Urgent") and template.priority not in ("urgent", "high"):
                prefix = ""
            if suffix.startswith(" This is blocking") and template.priority in ("low",):
                suffix = ""
            texts.append(f"{prefix}{body}{suffix}".strip())
            priorities.append(template.priority)
            categories.append(template.category)
            groups.append(group_id)

    order = list(range(len(texts)))
    rng.shuffle(order)
    return (
        [texts[i] for i in order],
        [priorities[i] for i in order],
        [categories[i] for i in order],
        [groups[i] for i in order],
    )


# --------------------------------------------------------------------------- #
# Training
# --------------------------------------------------------------------------- #


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    # Unigrams, min_df=1, and crucially *no* stop-word removal.
                    # All three were measured under the grouped CV below, and
                    # dropping stop words was the single worst change: "how do
                    # I" is the strongest how_to signal in the corpus and "would
                    # be nice if" is the strongest feature_request signal, and
                    # an English stop-word list deletes both. Bigrams also lost,
                    # having too few occurrences each to survive a held-out
                    # template.
                    ngram_range=(1, 1),
                    min_df=1,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    lowercase=True,
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    max_iter=3000,
                    C=25.0,  # little regularisation: the feature space is sparse and small
                    class_weight="balanced",  # 'urgent' is the rarest and the costliest to miss
                    random_state=42,
                ),
            ),
        ]
    )


def evaluate(name: str, y_true: Iterable[str], y_pred: Iterable[str], *, quiet: bool = False) -> dict:
    y_true, y_pred = list(y_true), list(y_pred)
    accuracy = accuracy_score(y_true, y_pred)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, y_pred, average="weighted", zero_division=0
    )
    if not quiet:
        print(f"\n=== {name} ===")
        print(f"accuracy : {accuracy:.3f}")
        print(f"precision: {precision:.3f} (weighted)")
        print(f"recall   : {recall:.3f} (weighted)")
        print(f"f1       : {f1:.3f} (weighted)")
        print(classification_report(y_true, y_pred, zero_division=0))
    return {
        "accuracy": round(float(accuracy), 4),
        "precision": round(float(precision), 4),
        "recall": round(float(recall), 4),
        "f1": round(float(f1), 4),
    }


def train(out_path: Path, *, seed: int = 42, test_size: float = 0.25, quiet: bool = False) -> dict:
    texts, priorities, categories, groups = build_dataset(seed=seed)
    if not quiet:
        print(f"Dataset: {len(texts)} labelled tickets from {len(set(groups))} templates")
        print(f"  priorities: {ordered_counts(priorities)}")
        print(f"  categories: {ordered_counts(categories)}")

    # Grouped by template and stratified by category: no template's paraphrases
    # can straddle the split, so the reported score is real generalisation.
    n_splits = max(2, round(1 / test_size))
    splitter = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    train_idx, test_idx = next(splitter.split(texts, categories, groups))
    train_idx, test_idx = list(train_idx), list(test_idx)
    if not quiet:
        held_out = sorted({groups[i] for i in test_idx})
        print(
            f"  split: {len(train_idx)} train / {len(test_idx)} test, {len(held_out)} unseen templates held out"
        )

    def subset(values: list[str], indexes: list[int]) -> list[str]:
        return [values[i] for i in indexes]

    metrics: dict[str, dict] = {}
    models: dict[str, Pipeline] = {}
    for name, labels in (("priority", priorities), ("category", categories)):
        pipeline = build_pipeline()
        pipeline.fit(subset(texts, train_idx), subset(labels, train_idx))
        predictions = pipeline.predict(subset(texts, test_idx))
        metrics[name] = evaluate(name, subset(labels, test_idx), predictions, quiet=quiet)
        metrics[name]["baseline_accuracy"] = majority_baseline(
            subset(labels, train_idx), subset(labels, test_idx)
        )
        # Refit on everything for the artefact we actually ship: the held-out
        # split exists to measure, not to withhold data from production.
        final = build_pipeline()
        final.fit(texts, labels)
        models[name] = final

    artefact = {
        "version": 1,
        "priority_model": models["priority"],
        "category_model": models["category"],
        "priority_labels": sorted(set(priorities)),
        "category_labels": sorted(set(categories)),
        "metrics": metrics,
        "dataset_size": len(texts),
        "test_size": test_size,
        "seed": seed,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artefact, out_path)
    if not quiet:
        print(f"\nSaved model to {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
        print(json.dumps(metrics, indent=2))
    return metrics


def majority_baseline(train_labels: list[str], test_labels: list[str]) -> float:
    """Accuracy of always predicting the training majority class.

    Reported alongside the model so the headline number has a floor to beat.
    """
    counts = ordered_counts(train_labels)
    majority = next(iter(counts))
    return round(sum(1 for label in test_labels if label == majority) / max(1, len(test_labels)), 4)


def ordered_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=0.50,
        help="Exit non-zero if either task falls below this held-out accuracy.",
    )
    args = parser.parse_args(argv)

    metrics = train(args.out, seed=args.seed, test_size=args.test_size, quiet=args.quiet)
    worst = min(m["accuracy"] for m in metrics.values())
    if worst < args.min_accuracy:
        print(
            f"FAIL: held-out accuracy {worst:.3f} is below the {args.min_accuracy} threshold", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
