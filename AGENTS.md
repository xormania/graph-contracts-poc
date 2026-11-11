# Working on this PoC

Current explicit direction from xor governs. This repository is a modifiable
experiment, not a settled contract ontology or a second orchestrator.

Keep domain operations usable as a Rust library. Keep CLI and host integration
thin. Graph relationships, intended contracts, declarations and implementation
evidence have distinct meanings; preserve those distinctions and their identities.

Backend-native contract documents stay native. Unsupported behavior, missing
evidence and evaluation failures must stay visible. Do not silently replace a
selected evaluator or regenerate an intended contract from changed observations.

Use the editable examples to explain a semantic change. Prefer behavioral
matrices and reusable test helpers. Calibrate consequential checks with deliberate
faults or an identified defective predecessor; compilation failure is not red proof.

Before opening a draft PR, obtain an independent adversarial review and resolve
its material findings. Report what ran, what passed, and any remaining limitation.
Use the commands in README.md and keep CI on the committed source and lockfile.

Attribute work to xormania only. Do not add assistant co-author trailers.
