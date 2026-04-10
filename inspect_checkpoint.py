import torch

# Load the checkpoint to see its structure
cp = torch.load("models/iep1d/best_model.pkl", map_location="cpu")

# Get state_dict
if isinstance(cp, dict) and "model_state" in cp:
    state = cp["model_state"]
else:
    state = cp

print("=" * 80)
print("CHECKPOINT KEYS:")
print("=" * 80)

# Group by prefix
groups = {}
for key in sorted(state.keys()):
    prefix = key.split(".")[0]
    if prefix not in groups:
        groups[prefix] = []
    groups[prefix].append(key)

# Show samples from each group
for prefix in sorted(groups.keys()):
    print(f"\n{prefix}: ({len(groups[prefix])} keys)")
    for key in sorted(groups[prefix])[:15]:
        print(f"  {key}")
    if len(groups[prefix]) > 15:
        print(f"  ... and {len(groups[prefix]) - 15} more")
