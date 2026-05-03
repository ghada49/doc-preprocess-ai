# Project Overview

## Problem

Libraries and archives hold substantial collections of scanned books, newspapers, and historical documents. These digitized materials are often not immediately suitable for reliable optical character recognition (OCR) or downstream digital use. Typical issues include skewed page images, imprecise cropping, intact page spreads that must be split or segmented, unclear page or region boundaries, weak or inconsistent layout structure, and text that is difficult for OCR engines to read accurately.

Beyond search and access, digitization addresses preservation: physical holdings can deteriorate, suffer damage, or become harder to use over time. Converting materials to digital form, when done carefully, helps reduce reliance on fragile originals and supports long-term stewardship of cultural and scholarly records.

## Target User

The primary intended users and organizational context for LibraryAI are:

- AUB Library
- Librarians
- Archive staff
- Digitization teams working with scanned books, newspapers, and historical documents

The system is intended to fit **practical library and archive digitization workflows**—quality control, batch processing, and collaboration between staff and automated tools—not as a standalone general-purpose AI demonstration disconnected from real operational needs.

## When processing starts: immediate vs scheduled (batch) windows

Digitization work is **inherently batch-oriented**: collections are ingested in **runs**, and turning scans into OCR-ready pages **does not need to complete immediately** after upload to still deliver library value. For **cost-aware production**, operators can use **`PROCESSING_START_MODE=scheduled_window`** — the API accepts jobs anytime, but **`normal_scaler`** does **not** scale GPU/workers on enqueue; **`scheduled-window.yml`** brings capacity up **only inside configured time windows** when there is processable work (`services/eep/app/scaling/normal_scaler.py`; `.github/workflows/scheduled-window.yml`; GitHub variable **`PROCESSING_START_MODE`**).

For **demonstrations**, instructor review, and **responsive** staging smoke tests, **`PROCESSING_START_MODE=immediate`** is the practical default (also the value baked into committed **`k8s/ecs/eep-task-def.json`**): after a durable enqueue, **`normal_scaler`** may trigger RunPod/ECS scale-up so processing can start **right away**. That path trades **higher reactivity** for **less batching of compute** than the scheduled-window model.

## Value

LibraryAI aims to narrow the gap between raw scans and trustworthy digital text by combining automated processing with explicit confidence and human oversight where needed.

- Supports **digital preservation** of valuable archives by improving the quality and usability of digitized collections.
- Improves **readiness of scanned documents before OCR** through preprocessing and layout-oriented handling that addresses common scan defects.
- **Reduces manual correction** by automatically accepting outcomes when the system is sufficiently confident, so staff effort concentrates on harder cases.
- **Routes uncertain or low-confidence pages to human review** rather than silently accepting unreliable results.
- **Captures accepted human corrections as training data** so the system can improve over time in domains relevant to the library’s materials.
- Contributes to a **more scalable, quality-controlled digitization pipeline** that balances throughput with accountability for errors.
