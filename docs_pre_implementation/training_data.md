
## Why the naive splits fail

**Train on one type, test on another:** The model learns the visual characteristics of the training material type and fails on the test type not because it is a bad model but because it never saw that distribution. The test results are pessimistically wrong — they measure domain shift, not model quality.

**Train on all types mixed, test on a held-out subset of the same mix:** This is data leakage in the sense that the model has seen examples from every material type during training and the test set is just more of the same distribution. The test results are optimistically wrong — they measure interpolation performance, not generalization. When a new collection arrives in production, performance will drop and you will have no warning it was coming.

Neither tells you what you actually need to know: will this model work reliably on real library material including material it has not seen before?

---

## The right evaluation framework for your situation

You need two separate things that most projects conflate:

**1. A development evaluation** — measures how well your models learn the preprocessing task across known material types. Used during training to tune hyperparameters, compare architectures, and set gate thresholds.

**2. A generalization evaluation** — measures how well your models transfer to unseen material. Used to understand production risk and set expectations before deployment.

These require different data splits and answer different questions.

---

## How to structure your splits given what you have

You have four collections:
- aub_aco003575 — book, book scanner (~125 files)
- mic_06 — documents, microfilm (~2000 files)
- na121_al-moqatam — newspaper, microfilm (~300 files)
- na246_sada-nahda — newspaper, microfilm (~275 files)

### For development evaluation — stratified within-collection splits

Within each collection, split at the file level into train/validation/test:
- 70% train, 15% validation, 15% test
- Split applied independently per collection
- Resulting sets each contain examples from all four material types

This measures: does the model learn the task across all material types it was trained on?

This is your primary development metric. Use it for architecture decisions, threshold calibration, and comparing IEP1A vs IEP1B model families.

### For generalization evaluation — leave-one-collection-out

Run four experiments, each time holding out one entire collection from training:

| Experiment | Train on | Test on |
|---|---|---|
| 1 | mic_06 + na121 + na246 | aub_aco003575 |
| 2 | aub_aco003575 + na121 + na246 | mic_06 |
| 3 | aub_aco003575 + mic_06 + na246 | na121 |
| 4 | aub_aco003575 + mic_06 + na121 | na246 |

This measures: how badly does performance drop when the model encounters a collection type it has never seen?

The gap between your within-collection test accuracy and your leave-one-out accuracy is your generalization gap. This tells you how much you should trust the system when a new collection arrives in production.

---

## What to do with the two newspaper collections

na121 and na246 are both microfilm newspapers but from different years and sources. They are not the same distribution — na121 had no splitting while na246 required it. Treat them as separate collections in your leave-one-out experiments, not as a single merged newspaper pool.

This matters because it gives you experiment 3 and 4 above as separate data points. If leaving out na121 causes a small accuracy drop but leaving out na246 causes a large one, you learn that split detection is the fragile capability — it only has one training source.

---

## How to handle the class imbalance across collections

mic_06 has ~2000 files versus ~125 for the book collection. If you train on the raw mix, the model will be biased toward microfilm document characteristics. Two approaches:

**Stratified batch sampling:** During training, sample batches so that each collection type contributes equally regardless of its size. The model sees each material type at the same frequency even though the dataset sizes are unequal. This is the simplest fix and usually sufficient.

**Per-collection sample weighting:** Assign higher loss weight to underrepresented collections. Similar effect to stratified sampling but implemented differently. Use this if stratified sampling alone does not produce balanced per-collection accuracy.

Do not oversample by duplicating minority examples — the book collection is small enough that heavy duplication will cause overfitting on those specific examples.

---

## The evaluation metrics you actually need

Do not report a single aggregate accuracy number. Report separately for each collection type in every evaluation:

- geometry accuracy: IoU between predicted page region and ground truth region derived from OTIFF→PTIFF pairs
- split accuracy: precision and recall on `split_required` prediction
- review rate: fraction of cases routed to human review
- bad auto-accept rate: fraction of auto-accepted cases where the output diverges from PTIFF by more than a threshold

The last metric is the most important for the library's trust. A model that sends 40% of cases to human review but never auto-accepts a bad output is more trustworthy for library operations than a model that auto-accepts 95% but gets 5% of them wrong.

---

## The practical sequence

**Phase 1 — baseline within-collection evaluation**

Do the stratified within-collection split. Train IEP1A and IEP1B. Measure per-collection accuracy. This tells you whether your models are learning the task at all and which material types are hardest.

**Phase 2 — generalization evaluation**

Run the four leave-one-out experiments. Measure the generalization gap per collection. This tells you which collection types the model can handle without having seen them before and which ones it cannot.

**Phase 3 — calibration**

Use the within-collection validation set to calibrate model confidence scores (temperature scaling). Use the within-collection test set to set gate thresholds. Do not use the leave-one-out test sets for threshold setting — those are reserved for measuring generalization only.

**Phase 4 — production monitoring**

Once deployed, measure per-collection-type accuracy and review rate on real production traffic. Treat every new collection that arrives as a new leave-one-out test. When performance drops on a new collection, route those cases to active learning and prioritize them for labeling.

---

## The honest assessment of your data volume

With ~125 book files, ~300 na121 files, and ~275 na246 files, your non-microfilm-document data is small. The leave-one-out results for the book collection will have high variance — 125 test examples is not enough to be confident in aggregate numbers, and stratifying by operation type (split vs no split, heavy warp vs clean) will produce even smaller subcategories.

This means your generalization evaluation for the book collection will give you directional signal but not statistically reliable estimates. You should treat the book collection leave-one-out result as a qualitative indicator — does the model completely fail on books, or does it degrade gracefully — rather than as a precise accuracy number.

The mic_06 collection is large enough that leave-one-out results on it will be reliable. This is your strongest generalization test.
