import os
from PIL import Image

input_folder = "path/to/input_folder"
output_folder = "path/to/output_folder"
max_size = 2048

os.makedirs(output_folder, exist_ok=True)

for filename in os.listdir(input_folder):
    if filename.lower().endswith(('.tif', '.tiff')):
        img_path = os.path.join(input_folder, filename)
        img = Image.open(img_path)
        w, h = img.size
        scale = min(max_size / w, max_size / h, 1.0)
        new_w, new_h = int(w * scale), int(h * scale)
        if scale < 1.0:
            img = img.resize((new_w, new_h), Image.LANCZOS)
        img.save(os.path.join(output_folder, filename), format='TIFF')
        print(f"Processed {filename}: {w}x{h} -> {new_w}x{new_h}")