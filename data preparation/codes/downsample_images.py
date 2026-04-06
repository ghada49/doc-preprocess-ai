import os
from PIL import Image

folders = [
    r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\data prep\book",
    r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\data prep\other",
    r"c:\Users\ayaae\OneDrive - American University of Beirut\Desktop\data prep\micro"
]

max_size = 2096 
output_folder_name = "downsampled"

for folder in folders:
    output_folder = os.path.join(folder, output_folder_name)
    os.makedirs(output_folder, exist_ok=True)
    for filename in os.listdir(folder):
        if filename.lower().endswith('.tif'):
            input_path = os.path.join(folder, filename)
            output_path = os.path.join(output_folder, filename)
            img = Image.open(input_path)
            w, h = img.size
            scale = min(max_size / w, max_size / h, 1.0)
            new_w, new_h = int(w * scale), int(h * scale)
            if scale < 1.0:
                img = img.resize((new_w, new_h), Image.LANCZOS)
            img.save(output_path, format='TIFF')
            print(f"Processed {input_path}: {w}x{h} -> {new_w}x{new_h}")
