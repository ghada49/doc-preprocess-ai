# LibraryAI Data Preparation Specification (Final)

This document provides a comprehensive, step-by-step guide for preparing data for training segmentation and keypoint models (IEP1A, IEP1B) in the LibraryAI pipeline. It incorporates best practices and detailed requirements for a robust, reproducible, and high-quality dataset.

---

## 1. Overview of IEP1 Modules

- **IEP1A (YOLOv8-seg):** Detects page geometry as segmentation masks or polygons—precise regions outlining each page in the image.
- **IEP1B (YOLOv8-pose):** Detects page geometry as keypoints—coordinates of specific points (typically corners) for each page in the image.
- **IEP1C:** Applies deterministic image processing (deskew, crop, split) using the geometry from IEP1A/IEP1B. No learning or randomness; always produces the same result for the same input.
- **IEP1D:** Rectification rescue stage for difficult or warped pages, triggered if geometry agreement or quality is insufficient.

---

## 2. Data Collection
- Gather OTIFF images from all collections (books, newspapers, documents).
- Ensure diversity: include single pages, two-page spreads, skewed, cropped, and microfilm artifacts.

---

## 3. Annotation Tool Setup
- **Recommended:** Local CVAT deployment (Docker) for large datasets; mount dataset as a network drive/volume for direct access.

---

## 4. Manual Labeling
- **Segmentation (IEP1A):**
  - Draw polygons or bounding boxes around each page region in every OTIFF image.
  - Label each region as "page" (or more specific if needed).
  - For two-page spreads, annotate each page separately (e.g., "page_left", "page_right").
- **Keypoints (IEP1B):**
  - Mark the four corners (or other required keypoints) for each page.
  - Assign keypoint labels (e.g., "top-left", "top-right", "bottom-left", "bottom-right").
  - For split pages, assign sub-page indices (left: 0, right: 1).

---

## 5. Output Format Examples

- **IEP1A (Polygons JSON):**
```json
{
  "pages": [
    { "label": "page_left", "polygon": [[100, 50], [200, 50], [200, 300], [100, 300]] },
    { "label": "page_right", "polygon": [[220, 50], [320, 50], [320, 300], [220, 300]] }
  ]
}
```
- **IEP1A (Segmentation Mask PNG):** Binary or multi-class PNG image, e.g., `image001_mask.png`.
- **IEP1B (Keypoints JSON):**
```json
{
  "pages": [
    {
      "label": "page_left",
      "keypoints": {
        "top_left": [100, 50], "top_right": [200, 50], "bottom_right": [200, 300], "bottom_left": [100, 300]
      },
      "sub_page_index": 0
    },
    {
      "label": "page_right",
      "keypoints": {
        "top_left": [220, 50], "top_right": [320, 50], "bottom_right": [320, 300], "bottom_left": [220, 300]
      },
      "sub_page_index": 1
    }
  ]
}
```

---

## 6. Handling Split Pages
- For two-page spreads, annotate each page separately with clear labels and sub-page indices.
- Ensure both single-page and split-page examples are present in your dataset.
- For keypoints, always include the `sub_page_index` field.

---

## 7. File Naming Convention
- Name output files according to the input image, e.g.:
  - `image001_mask.png` (IEP1A segmentation mask)
  - `image001_polygons.json` (IEP1A polygons)
  - `image001_keypoints.json` (IEP1B keypoints)
- Keep naming consistent for easy mapping between images and annotation files.

---

## 8. Annotation Export
- Export annotations in a format compatible with your model:
  - YOLO: .txt files with bounding box coordinates or keypoints
  - COCO: .json files with polygons and keypoints
  - Pascal VOC: .xml files with bounding boxes
- Double-check that exported files match the required schema for your model and include all necessary information (labels, coordinates, indices).
- For split pages, verify that each page in a spread is represented in the annotation file.
- Ensure each image has a corresponding annotation file, named consistently with the input image.

---

## 9. Dataset Organization
- Split dataset into:
  - Training set (majority of images)
  - Validation set (10–20% of images)
  - Test set (optional, for final evaluation)
- Organize images and annotation files into separate folders for each split.
- Record the number of images in each split for reproducibility.

---


## 10. Resizing Rules
- **Resizing to 2048 pixels (largest side):**
  - Resize each image so that its largest dimension (width or height) is at most 2048 pixels, maintaining the original aspect ratio (no distortion).
  - If the original image is already smaller than 2048 pixels on its largest side, no resizing is performed.
  - This ensures high resolution for detail, efficient training, and consistency across the dataset.
  - For most document and vision models, 2048 is more than sufficient

- **Why 2048 is a good choice:**
  - Preserves fine details and small text, which is important for page geometry and keypoint detection.
  - Future-proofs your dataset for possible OCR or higher-resolution tasks.
  - Ensures consistency if your original scans are high-resolution.
  - Modern GPUs and training frameworks can efficiently handle 2048px images for most document ML tasks.
  - You can always downscale later if needed, but upscaling loses information.

