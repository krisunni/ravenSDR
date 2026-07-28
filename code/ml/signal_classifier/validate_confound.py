#!/usr/bin/env python3
"""Does the model learn MODULATION, or just which band it is looking at?

The problem
-----------
Collected samples are labelled by preset, and each preset is one frequency. If a
class only ever appears at one frequency, a network can score ~100% by learning
that band's noise floor, filter shape and spurs — and would then fail completely
on the same modulation somewhere else. A random train/test split cannot detect
this, because both halves contain the same frequencies.

The test
--------
Hold entire FREQUENCIES out of training. Train on some of a class's frequencies,
test only on ones the model has never seen. If accuracy survives, the model
generalised to the modulation. If it collapses, it had memorised the bands.

This needs a class carried by several frequencies — FM qualifies (13 presets from
143 to 442 MHz), WFM marginally (2).

Usage:
    python3 validate_confound.py --data-dir data/collected [--epochs 8]
"""

import argparse
import glob
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataset import (RUNTIME_SAMPLE_LEN, iq_to_spectrogram,  # noqa: E402
                     sample_frequency_hz, spectrogram_to_image)


def load_grouped(data_dir, sample_len=RUNTIME_SAMPLE_LEN):
    """Return {class: {frequency_hz: [image, ...]}}."""
    out = defaultdict(lambda: defaultdict(list))
    for cls in sorted(os.listdir(data_dir)):
        cdir = os.path.join(data_dir, cls)
        if not os.path.isdir(cdir):
            continue
        for path in sorted(glob.glob(os.path.join(cdir, "*.npy"))):
            try:
                iq = np.load(path)
            except Exception:
                continue
            if not np.iscomplexobj(iq) or len(iq) < 256:
                continue
            img = spectrogram_to_image(iq_to_spectrogram(iq[:sample_len]))
            out[cls][sample_frequency_hz(path)].append(img)
    return out


def split_by_frequency(grouped, holdout_frac=0.34, seed=0):
    """Put whole frequencies in test. Single-frequency classes fall back to a
    random split, and are reported separately since they prove nothing."""
    rng = np.random.default_rng(seed)
    tr_x, tr_y, te_x, te_y = [], [], [], []
    classes = sorted(grouped)
    honest, weak = [], []

    for ci, cls in enumerate(classes):
        freqs = sorted(grouped[cls])
        if len(freqs) >= 2:
            n_hold = max(1, int(round(len(freqs) * holdout_frac)))
            held = set(freqs[-n_hold:])          # highest frequencies held out
            honest.append((cls, len(freqs), sorted(held)))
            for f in freqs:
                for img in grouped[cls][f]:
                    (te_x if f in held else tr_x).append(img)
                    (te_y if f in held else tr_y).append(ci)
        else:
            weak.append((cls, freqs[0] if freqs else 0))
            imgs = grouped[cls][freqs[0]] if freqs else []
            idx = rng.permutation(len(imgs))
            cut = int(len(imgs) * (1 - holdout_frac))
            for k, j in enumerate(idx):
                (tr_x if k < cut else te_x).append(imgs[j])
                (tr_y if k < cut else te_y).append(ci)

    return (np.array(tr_x), np.array(tr_y), np.array(te_x), np.array(te_y),
            classes, honest, weak)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/collected")
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=32)
    args = ap.parse_args()

    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader
    from torchvision import models

    print("Loading and grouping by frequency...")
    grouped = load_grouped(args.data_dir)
    tr_x, tr_y, te_x, te_y, classes, honest, weak = split_by_frequency(grouped)

    print("\nClasses tested on UNSEEN frequencies (this is the real test):")
    for cls, nf, held in honest:
        print("  %-10s %2d frequencies, holding out %s"
              % (cls, nf, ", ".join("%.3f MHz" % (h / 1e6) for h in held)))
    if weak:
        print("\nClasses with only ONE frequency (random split — proves nothing):")
        for cls, f in weak:
            print("  %-10s %.3f MHz" % (cls, f / 1e6))

    print("\nTrain %d / Test %d over %d classes  (%.2f GB held as uint8)"
          % (len(tr_x), len(te_x), len(classes),
             (tr_x.nbytes + te_x.nbytes) / 1e9))
    if not len(te_x):
        print("No test data — nothing to validate.")
        return

    class Uint8Spectrograms(torch.utils.data.Dataset):
        """Keeps images as uint8 and expands to float32 3-channel PER BATCH.

        Materialising the whole set as float32 x3 channels needs
        n * 224 * 224 * 3 * 4 bytes — 5.6 GB for 9353 samples, which OOM-killed
        the training VM. Held as uint8 it is 0.47 GB, and the expansion costs
        nothing when done one batch at a time.
        """

        def __init__(self, images, labels):
            self.images = images
            self.labels = torch.from_numpy(labels)

        def __len__(self):
            return len(self.images)

        def __getitem__(self, i):
            img = torch.from_numpy(self.images[i].astype(np.float32) / 255.0)
            return img.unsqueeze(0).repeat(3, 1, 1), self.labels[i]

    # Class weights: the corpus is heavily skewed (FM ~1200 vs APRS ~60) and an
    # unweighted loss just learns to predict the majority class.
    counts = np.bincount(tr_y, minlength=len(classes)).astype(np.float64)
    weights = np.where(counts > 0, counts.sum() / np.maximum(counts, 1), 0.0)
    weights = weights / weights[weights > 0].mean()
    print("Class weights:", {classes[i]: round(float(weights[i]), 2)
                             for i in range(len(classes)) if counts[i]})

    model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
    model.classifier[1] = nn.Linear(model.last_channel, len(classes))
    crit = nn.CrossEntropyLoss(weight=torch.tensor(weights, dtype=torch.float32))
    opt = torch.optim.Adam([
        {"params": model.features.parameters(), "lr": 1e-5},
        {"params": model.classifier.parameters(), "lr": 1e-3},
    ])

    loader = DataLoader(Uint8Spectrograms(tr_x, tr_y),
                        batch_size=args.batch_size, shuffle=True)
    for ep in range(args.epochs):
        model.train()
        tot = correct = 0
        run = 0.0
        for xb, yb in loader:
            opt.zero_grad()
            out = model(xb)
            loss = crit(out, yb)
            loss.backward()
            opt.step()
            run += float(loss) * len(yb)
            correct += int((out.argmax(1) == yb).sum())
            tot += len(yb)
        print("  epoch %d/%d  loss %.4f  train_acc %.4f"
              % (ep + 1, args.epochs, run / tot, correct / tot))

    model.eval()
    preds = []
    test_loader = DataLoader(Uint8Spectrograms(te_x, te_y), batch_size=32)
    with torch.no_grad():
        for xb, _ in test_loader:
            preds.append(model(xb).argmax(1).numpy())
    preds = np.concatenate(preds)

    print("\n" + "=" * 62)
    print("HELD-OUT-FREQUENCY ACCURACY: %.4f" % float((preds == te_y).mean()))
    print("=" * 62)
    honest_names = {c for c, _, _ in honest}
    print("%-10s %8s %8s   %s" % ("class", "n", "recall", "verdict"))
    for ci, cls in enumerate(classes):
        m = te_y == ci
        if not m.sum():
            continue
        rec = float((preds[m] == ci).mean())
        if cls in honest_names:
            verdict = ("generalises across frequency" if rec > 0.7
                       else "FAILS on unseen frequencies — learned the band")
        else:
            verdict = "single frequency — unproven"
        print("%-10s %8d %8.3f   %s" % (cls, int(m.sum()), rec, verdict))


if __name__ == "__main__":
    main()
