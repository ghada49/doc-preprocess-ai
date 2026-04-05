IEP1D expects the official UVDoc checkpoint at:

  models/iep1d/best_model.pkl

Optional version sidecar:

  models/iep1d/best_model.pkl.version

Recommended contents for the sidecar:

  uvdoc-siggraphasia2023-official

Official sources:

  Repository: https://github.com/tanguymagne/UVDoc
  Checkpoint path in that repo: model/best_model.pkl
  Direct download URL:
    https://raw.githubusercontent.com/tanguymagne/UVDoc/main/model/best_model.pkl

Docker Compose mounts this directory into the IEP1D container at:

  /dev-models/iep1d

The service reads the local-development override from:

  IEP1D_LOCAL_WEIGHTS_PATH=/dev-models/iep1d/best_model.pkl
