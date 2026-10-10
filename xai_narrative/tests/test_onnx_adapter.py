import os, numpy as np, pytest
from fatigue_xai.cv_adapter import OnnxFatigueModel, preprocess_face_crop

ONNX = os.getenv("FATIGUE_ONNX", "models/efficientnet_fatigue.onnx")
pytestmark = pytest.mark.skipif(not os.path.exists(ONNX), reason="set FATIGUE_ONNX to run (needs .onnx + .onnx.data)")


def test_real_model_to_telemetry():
    m = OnnxFatigueModel(ONNX)
    img = (np.random.RandomState(0).rand(180, 160, 3) * 255).astype("uint8")
    assert preprocess_face_crop(img).shape == (1, 3, 224, 224)
    t = m.telemetry(img, ts=1.0, session_id="x")
    assert abs(sum(t.class_probs.values()) - 1) < 1e-5
    assert 0 <= t.p_fatigue <= 1
    assert t.p_fatigue == pytest.approx(t.class_probs["microsleep"] + t.class_probs["yawning"], abs=1e-6)
