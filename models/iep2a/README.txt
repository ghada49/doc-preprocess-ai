Place the approved PubLayNet Detectron2 checkpoint here for local development
or image assembly:

models/iep2a/model_final.pth
models/iep2a/model_final.pth.version

Optional PaddleOCR PP-DocLayoutV2 backend:

models/iep2a/paddle/PP-DocLayoutV2/               (model directory)
models/iep2a/paddle/PP-DocLayoutV2.version        (authoritative version sidecar)

Production serving:
- Build a versioned IEP2A inference image with the approved checkpoint baked in.
- Bake the matching version sidecar into the image as `model_final.pth.version`.
- The image copies this directory to /opt/models/iep2a/.
- Real-mode startup loads from /opt/models/iep2a/model_final.pth by default.
- If PaddleOCR is enabled, bake the PP-DocLayoutV2 model directory into
  /opt/models/iep2a/paddle/PP-DocLayoutV2 and the matching sidecar into
  /opt/models/iep2a/paddle/PP-DocLayoutV2.version.

Local development override:
- This directory is mounted to /dev-models/iep2a by docker-compose.
- Set IEP2A_LOCAL_WEIGHTS_PATH=/dev-models/iep2a/model_final.pth to use it.
- Set IEP2A_PADDLE_LOCAL_MODEL_DIR=/dev-models/iep2a/paddle/PP-DocLayoutV2
  to use a mounted PaddleOCR PP-DocLayoutV2 directory.
- For consistent metadata in local real-mode tests, add
  /dev-models/iep2a/model_final.pth.version or set IEP2A_MODEL_VERSION.

Do not commit model weights to Git.
