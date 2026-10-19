"""
features.csv -> Random Forest -> classification reports, confusion matrices,
feature importances, for three targets:

  op       which MongoDB operation (insert / find_one / find_many / update / delete / aggregate)
  service  which microservice issued it (user / txn / auth)
  label    both together (service:op)

usage: python train_rf.py data/features.csv results/
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (ConfusionMatrixDisplay, accuracy_score,
                             classification_report, f1_score)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

META = {"service", "op", "label", "pod_ip", "t_start"}
SEED = 42


def run(df, target, out):
    counts = df[target].value_counts()
    df = df[df[target].isin(counts[counts >= 10].index)]  # drop ultra-rare classes
    X = df[[c for c in df.columns if c not in META]]
    y = df[target]

    # Chronological-ish split is stricter, but stratified random is the standard baseline.
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, stratify=y, random_state=SEED)
    rf = RandomForestClassifier(n_estimators=300, n_jobs=-1, class_weight="balanced",
                                random_state=SEED)
    rf.fit(Xtr, ytr)
    pred = rf.predict(Xte)

    report = classification_report(yte, pred, digits=3)
    cv = cross_val_score(
        RandomForestClassifier(n_estimators=150, n_jobs=-1, class_weight="balanced",
                               random_state=SEED),
        X, y, cv=StratifiedKFold(5, shuffle=True, random_state=SEED), scoring="f1_macro")
    summary = {
        "target": target, "n_samples": int(len(df)), "n_classes": int(y.nunique()),
        "test_accuracy": round(accuracy_score(yte, pred), 4),
        "test_macro_f1": round(f1_score(yte, pred, average="macro"), 4),
        "cv5_macro_f1_mean": round(cv.mean(), 4), "cv5_macro_f1_std": round(cv.std(), 4),
        "majority_baseline_acc": round(y.value_counts(normalize=True).iloc[0], 4),
    }

    with open(os.path.join(out, f"report_{target}.txt"), "w") as fh:
        fh.write(f"Target: {target}\n{json.dumps(summary, indent=2)}\n\n{report}")
    print(f"\n===== {target} =====\n{json.dumps(summary, indent=2)}\n{report}")

    fig, ax = plt.subplots(figsize=(max(6, y.nunique() * 0.8),) * 2)
    ConfusionMatrixDisplay.from_predictions(yte, pred, ax=ax, xticks_rotation=60,
                                            normalize="true", values_format=".2f",
                                            cmap="Blues", colorbar=False)
    ax.set_title(f"Random Forest — {target} (row-normalised)")
    fig.tight_layout()
    fig.savefig(os.path.join(out, f"confusion_{target}.png"), dpi=150)
    plt.close(fig)

    imp = pd.Series(rf.feature_importances_, index=X.columns).sort_values()
    fig, ax = plt.subplots(figsize=(7, 0.28 * len(imp) + 1))
    imp.plot.barh(ax=ax, color="#3b6ea5")
    ax.set_title(f"Feature importance — {target}")
    fig.tight_layout()
    fig.savefig(os.path.join(out, f"importance_{target}.png"), dpi=150)
    plt.close(fig)
    return summary


def main(csv, out):
    os.makedirs(out, exist_ok=True)
    df = pd.read_csv(csv)
    print(f"{len(df)} samples, {df.shape[1] - len(META)} features")
    results = [run(df, t, out) for t in ("op", "service", "label")]
    pd.DataFrame(results).to_csv(os.path.join(out, "summary.csv"), index=False)
    print(f"\nresults written to {out}/")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
