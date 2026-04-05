import re

import torch


def _remap_checkpoint_keys(state_dict: dict) -> dict:
    """Test version of the remapping function."""
    remapped = {}
    for key, value in state_dict.items():
        new_key = key

        # 1. Remap camelCase to snake_case for point positions
        new_key = new_key.replace("out_point_positions2D", "out_point_positions_2d")
        new_key = new_key.replace("out_point_positions3D", "out_point_positions_3d")

        # 2. Remap bridge layer indices: bridge_X.Y.Z → bridge_X.(Y*2 + Z)
        match = re.match(r"bridge_(\d+)\.(\d+)\.(\d+)(.*)", new_key)
        if match:
            bridge_num = match.group(1)
            nested_module_idx = int(match.group(2))
            nested_layer_idx = int(match.group(3))
            suffix = match.group(4)
            # Flatten: (nested_module_idx * 2) + nested_layer_idx
            new_flat_idx = nested_module_idx * 2 + nested_layer_idx
            new_key = f"bridge_{bridge_num}.{new_flat_idx}{suffix}"

        remapped[new_key] = value

    return remapped


# Load checkpoint
cp = torch.load("models/iep1d/best_model.pkl", map_location="cpu")
if isinstance(cp, dict) and "model_state" in cp:
    state = cp["model_state"]
else:
    state = cp

# Remap
remapped = _remap_checkpoint_keys(state)

print("=" * 80)
print("REMAPPED KEYS Sample:")
print("=" * 80)

# Group by prefix
groups = {}
for key in sorted(remapped.keys()):
    prefix = key.split(".")[0]
    if prefix not in groups:
        groups[prefix] = []
    groups[prefix].append(key)

# Show samples from each group
for prefix in sorted(groups.keys()):
    print(f"\n{prefix}: ({len(groups[prefix])} keys)")
    for key in sorted(groups[prefix])[:10]:
        print(f"  {key}")
    if len(groups[prefix]) > 10:
        print(f"  ... and {len(groups[prefix]) - 10} more")

print("\n" + "=" * 80)
print("CHECK: Do all bridge keys now use Y*2 + Z mapping?")
print("=" * 80)

# Manually check bridge_4 as an example
bridge_keys = [k for k in remapped.keys() if k.startswith("bridge_4.")]
print(f"\nbridge_4 remapped indices: {sorted(set(int(k.split('.')[1]) for k in bridge_keys))}")
print("Should be: 0, 1, 2, 3, 4, 5 (three modules × 2 layers each)")

# Expected for bridge_4:
# Y=0: Z=0 → index 0, Z=1 → index 1
# Y=1: Z=0 → index 2, Z=1 → index 3
# Y=2: Z=0 → index 4, Z=1 → index 5
print("\nSample bridge_4 keys:")
for key in sorted(k for k in remapped.keys() if k.startswith("bridge_4."))[:9]:
    orig_idx = int(key.split(".")[1])
    # Reverse map: orig_idx = Y*2 + Z, so Y = orig_idx // 2, Z = orig_idx % 2
    Y = orig_idx // 2
    Z = orig_idx % 2
    print(f"  {key} (was bridge_4.{Y}.{Z}.*)")
