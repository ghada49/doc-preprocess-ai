import sys

sys.path.insert(0, "/app")

from services.iep1d.app.uvdoc import UVDocConfig, UVDocNet

# Create an empty model to see what keys it expects
config = UVDocConfig()
model = UVDocNet(num_filter=config.num_filter, kernel_size=config.kernel_size)

print("=" * 80)
print("MODEL EXPECTED KEYS:")
print("=" * 80)

state = model.state_dict()

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
    for key in sorted(groups[prefix])[:10]:
        print(f"  {key}")
    if len(groups[prefix]) > 10:
        print(f"  ... and {len(groups[prefix]) - 10} more")

print("\n" + "=" * 80)
print("Bridge structure details:")
print("=" * 80)

bridge_keys = [k for k in state.keys() if k.startswith("bridge_4.")]
print(f"\nbridge_4 indices: {sorted(set(int(k.split('.')[1]) for k in bridge_keys))}")
print("Sample bridge_4 keys:")
for key in sorted(k for k in state.keys() if k.startswith("bridge_4."))[:10]:
    print(f"  {key}")
