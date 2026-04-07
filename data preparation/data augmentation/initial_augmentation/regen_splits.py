"""Regenerate train.txt, val.txt, test.txt from actual files in each split folder."""
from pathlib import Path

BASE = Path(r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\Segmentation data augmentation")

for split in ["train", "val", "test"]:
    img_dir = BASE / "images" / split
    entries = sorted(
        f"data/images/{split}/{f.name}" for f in img_dir.iterdir() if f.suffix in {".tif", ".tiff", ".png", ".jpg"}
    )
    (BASE / f"{split}.txt").write_text("\n".join(entries) + "\n")
    print(f"{split}.txt: {len(entries)} entries")
