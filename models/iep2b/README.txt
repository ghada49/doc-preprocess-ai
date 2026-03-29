Place the approved DocLayout-YOLO checkpoint here for local development or
image assembly:

models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt
models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt.version

Production serving:
- Build a versioned IEP2B inference image with the approved checkpoint baked in.
- Bake the matching version sidecar into the image as
  `doclayout_yolo_docstructbench_imgsz1024.pt.version`.
- The image copies this directory to /opt/models/iep2b/.
- Real-mode startup loads from
  /opt/models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt by default.

Local development override:
- This directory is mounted to /dev-models/iep2b by docker-compose.
- Set
  IEP2B_LOCAL_WEIGHTS_PATH=/dev-models/iep2b/doclayout_yolo_docstructbench_imgsz1024.pt
  to use it.
- For consistent metadata in local real-mode tests, add the matching `.version`
  file or set IEP2B_MODEL_VERSION.

Do not commit model weights to Git.
