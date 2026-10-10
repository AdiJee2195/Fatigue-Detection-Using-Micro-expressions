"""Vision service tests.

The HTTP tests run in stub mode, so they need no model, no onnxruntime and no
network — they verify the contract the gateway and the narrative engine depend on.
The ONNX path is exercised with a fake classifier plus the real
``fatigue_xai.cv_adapter`` projection, so the softmax / ``p_fatigue`` definition
under test is still the shipped one.

The provenance assertions are the important ones: the service must never present a
synthetic or fallback-derived number as if it were a measurement.
"""
from __future__ import annotations

import base64
import io
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in (ROOT, os.path.join(ROOT, "xai_narrative")):
    if path not in sys.path:
        sys.path.insert(0, path)

from vision_service.app import create_app                        # noqa: E402
from vision_service.face import FaceLocator                       # noqa: E402
from vision_service.features import (                             # noqa: E402
    LEFT_EYE,
    RIGHT_EYE,
    UnavailableEarAuProvider,
    eye_aspect_ratio,
)
from vision_service.pipeline import (                             # noqa: E402
    VisionPipeline,
    decode_base64_image,
    no_face_response,
    stub_drive,
)
from vision_service.status import FaceSource                      # noqa: E402

TS_MS = 1_700_000_000_000.0
LABELS = {"microsleep", "neutral", "yawning"}


@pytest.fixture()
def client():
    # The context manager runs the lifespan, which is where the pipeline is built.
    with TestClient(create_app(VisionPipeline(mode="stub"))) as c:
        yield c


def analyze(client, frame="ZmFrZS1qcGVn", timestamp=TS_MS, **extra):
    return client.post("/api/v1/analyze", json={"frame": frame, "timestamp": timestamp, **extra})


def jpeg_frame(width=320, height=240, value=120):
    pytest.importorskip("PIL")
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (value, value, value)).save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode()


# ---------------------------------------------------------------------------
# HTTP contract
# ---------------------------------------------------------------------------
def test_health_reports_mode_and_model_state(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["mode"] == "stub"
    assert body["requested_mode"] == "stub"
    assert body["model_loaded"] is False
    assert body["degraded"] is False
    assert body["service"] == "vision-service"
    assert "face_backend" in body
    assert body["signals"]["rppg"] == "unavailable"
    assert body["stats"]["frames_received"] == 0


def test_analyze_returns_the_agreed_telemetry_contract(client):
    response = analyze(client, sessionId="op-1")
    assert response.status_code == 200

    body = response.json()
    for key in ("ts", "class_probs", "p_fatigue", "ear", "au", "face_detected",
                "illumination", "rppg_bpm", "risk_score", "au_intensities",
                "active_aus", "inference_latency_ms", "mode", "mode_class",
                "signal_status"):
        assert key in body, f"missing contract field {key}"

    # The narrative engine requires exactly these labels summing to ~1.
    assert set(body["class_probs"]) == LABELS
    assert sum(body["class_probs"].values()) == pytest.approx(1.0, abs=1e-6)

    # risk_score mirrors p_fatigue; the UI gauge depends on that.
    assert body["risk_score"] == pytest.approx(body["p_fatigue"], abs=1e-6)
    assert 0.0 <= body["p_fatigue"] <= 1.0

    # ts must be epoch SECONDS; millisecond values would break windowing.
    assert body["ts"] == pytest.approx(TS_MS / 1000.0, abs=1e-3)
    assert body["ts"] < 1e10
    assert isinstance(body["face_detected"], bool)


def test_au_intensities_are_the_normalised_copy_of_au(client):
    body = analyze(client).json()
    assert set(body["au"]) == set(body["au_intensities"]) == set(body["active_aus"])
    for code, intensity in body["au"].items():
        assert 0.0 <= intensity <= 5.0, "FACS intensity must be on the 0-5 scale"
        assert body["au_intensities"][code] == pytest.approx(min(1.0, intensity / 5.0), abs=1e-3)
        assert body["active_aus"][code] == (intensity >= 1.0)


def test_extra_fields_are_ignored(client):
    assert analyze(client, sessionId="op-1", unknownField=123).status_code == 200


def test_timestamp_is_optional(client):
    response = client.post("/api/v1/analyze", json={"frame": "ZmFrZQ=="})
    assert response.status_code == 200
    assert response.json()["ts"] > 0


def test_malformed_json_is_rejected(client):
    assert client.post("/api/v1/analyze", json={"nonsense": 1}).status_code == 422
    assert client.post("/api/v1/analyze", json={"frame": None}).status_code == 422


def test_frame_payloads_are_never_echoed_into_the_response(client):
    """A privacy check: telemetry must not carry the frame it was derived from."""
    body = analyze(client).json()
    serialized = str(body)
    assert "ZmFrZS1qcGVn" not in serialized


# ---------------------------------------------------------------------------
# provenance — the whole point of the mode/signal_status fields
# ---------------------------------------------------------------------------
def test_stub_rows_are_labelled_synthetic_everywhere(client):
    body = analyze(client).json()
    assert body["mode"] == "stub"
    status = body["signal_status"]
    assert status["class_probs"] == "synthetic"
    assert status["ear"] == "synthetic"
    assert status["au"] == "synthetic"
    assert status["face_detection"] == "synthetic"
    assert status["rppg_bpm"] == "not_configured"


def test_stub_mode_never_reports_a_signal_as_live(client):
    """No stub row may claim a real measurement, at any point in the drive cycle."""
    pipeline = VisionPipeline(mode="stub")
    for elapsed in (0.0, 20.0, 45.0, 75.0, 110.0):
        body = pipeline._analyze_stub(1.0, elapsed_s=elapsed)
        assert "live" not in body["signal_status"].values()
        assert body["mode"] == "stub"


def test_mode_class_is_the_canonical_answer_about_measurement(client):
    """`mode` says which code path ran; `mode_class` says whether it measured.

    The distinction is the whole reason the second field exists: downstream layers
    (narrative engine, language model, dashboard) are not allowed to guess the
    second from the first.
    """
    body = analyze(client).json()
    assert body["mode"] == "stub"
    assert body["mode_class"] == "synthetic"


def test_every_row_of_the_drive_cycle_declares_its_class():
    pipeline = VisionPipeline(mode="stub")
    for elapsed in (0.0, 20.0, 45.0, 75.0, 110.0, 130.0):
        body = pipeline._analyze_stub(1.0, elapsed_s=elapsed)
        assert body["mode_class"] == "synthetic"


def test_an_unusable_frame_is_still_classified():
    """A no-face row must not lose its provenance on the way out."""
    from vision_service.telemetry import no_face_response

    row = no_face_response(1.0, mode="stub", reason="test")
    assert row["mode_class"] == "synthetic"
    assert row["face_detected"] is False
    assert row["rppg_bpm"] is None


def test_mode_class_maps_only_onnx_to_live():
    """`serve_mode` is `onnx` only while a model is loaded, so this cannot lie."""
    from vision_service.telemetry import mode_class

    assert mode_class("stub") == "synthetic"
    assert mode_class("onnx") == "live"
    assert mode_class("anything-else") == "unknown"


# ---------------------------------------------------------------------------
# the response must be accepted by the narrative engine
# ---------------------------------------------------------------------------
def test_response_is_accepted_by_the_narrative_engine(client):
    from fatigue_xai.schemas import FrameTelemetry

    body = analyze(client).json()
    telemetry = FrameTelemetry(
        ts=body["ts"], class_probs=body["class_probs"], ear=body["ear"], au=body["au"],
        face_detected=body["face_detected"], illumination=body["illumination"],
        rppg_bpm=body["rppg_bpm"],
    )
    # p_fatigue is derived by the engine when absent; it must agree with ours.
    assert telemetry.p_fatigue == pytest.approx(body["p_fatigue"], abs=2e-3)
    assert 0.0 <= telemetry.p_fatigue <= 1.0


# ---------------------------------------------------------------------------
# stub drive
# ---------------------------------------------------------------------------
def test_stub_drive_ramps_and_resets():
    assert stub_drive(0) == pytest.approx(0.0)
    assert stub_drive(37.5) == pytest.approx(0.5)
    assert stub_drive(75) == pytest.approx(1.0)
    assert stub_drive(90) == pytest.approx(1.0, abs=1e-9), "holds at the peak"
    assert stub_drive(120) == pytest.approx(0.0), "sawtooth resets each cycle"
    assert stub_drive(180) == pytest.approx(0.8)


def test_stub_telemetry_escalates_and_is_well_formed_at_every_drive():
    pipeline = VisionPipeline(mode="stub")
    for elapsed in (0.0, 15.0, 30.0, 50.0, 75.0, 100.0, 119.0):
        body = pipeline._analyze_stub(1.0, elapsed_s=elapsed)
        assert set(body["class_probs"]) == LABELS
        assert sum(body["class_probs"].values()) == pytest.approx(1.0, abs=1e-6)
        assert 0.0 <= body["p_fatigue"] <= 1.0
        assert 0.0 <= body["au"]["AU4"] <= 5.0
        assert 0.02 <= body["ear"] <= 0.5

    early = pipeline._analyze_stub(1.0, elapsed_s=30.0)
    late = pipeline._analyze_stub(1.0, elapsed_s=75.0)
    assert late["p_fatigue"] > early["p_fatigue"]
    assert late["au"]["AU4"] > early["au"]["AU4"], "brow strain must rise before the model peaks"
    assert late["ear"] < early["ear"], "eyelid droop must lead the classifier"


def test_stub_emits_pre_attentive_droop_while_p_fatigue_is_still_low():
    """The whole point of the project: EAR/AU move before the CNN catches up."""
    pipeline = VisionPipeline(mode="stub")
    body = pipeline._analyze_stub(1.0, elapsed_s=32.0)
    baseline_ear = 0.30
    assert body["ear"] <= 0.88 * baseline_ear, "audible lid droop at ~40% drive"
    assert body["p_fatigue"] < 0.35, "while the model probability is still low"


# ---------------------------------------------------------------------------
# untrusted timestamps (contract: client clocks cannot be trusted)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, 0, -1, float("nan"), 1e18, "not-a-number", ""])
def test_untrusted_timestamps_are_replaced_with_the_current_time(raw):
    now = time.time()
    resolved = VisionPipeline._to_epoch_seconds(raw)
    assert now - 5 <= resolved <= now + 5, f"unsanitized timestamp {raw!r}"


def test_millisecond_timestamps_become_seconds():
    assert VisionPipeline._to_epoch_seconds(TS_MS) == pytest.approx(TS_MS / 1000.0, abs=1e-6)


# ---------------------------------------------------------------------------
# no-face handling
# ---------------------------------------------------------------------------
def test_undecodable_frames_raise_a_decode_error():
    for bad in ("", "%%%", "data:image/jpeg;base64"):
        with pytest.raises(ValueError):
            decode_base64_image(bad)


def test_no_face_response_is_well_formed():
    body = no_face_response(TS_MS / 1000.0)
    assert body["face_detected"] is False
    assert body["ear"] is None and body["au"] == {}
    assert sum(body["class_probs"].values()) == pytest.approx(1.0, abs=1e-6)
    assert body["signal_status"]["face_detection"] == "none"


def test_centre_crop_is_reported_as_no_face():
    numpy = pytest.importorskip("numpy")
    crop = FaceLocator().locate(numpy.full((240, 320, 3), 128, dtype=numpy.uint8))
    assert crop.detected is False, "a fallback crop must not claim a face was detected"
    if crop.source is FaceSource.CENTRE_CROP:
        assert crop.image.shape[0] == crop.image.shape[1] == 144


# ---------------------------------------------------------------------------
# the ONNX path, with the real cv_adapter projection
# ---------------------------------------------------------------------------
class _FullFrameDetector:
    """Reports the whole frame as a face so the ONNX path is testable without
    the optional mediapipe / opencv dependencies."""

    name = "test-full-frame"
    source = FaceSource.OPENCV

    @property
    def available(self) -> bool:
        return True

    def locate(self, rgb):
        height, width = rgb.shape[:2]
        return (0.0, 0.0, float(width), float(height))


class _FakeClassifier:
    """Stands in for OnnxFatigueClassifier so the path runs without onnxruntime."""

    name = "fake-classifier"

    def __init__(self, logits=(0.06, 1.04, -1.06)):
        self.logits = list(logits)
        self.calls = 0

    def predict(self, crop):
        self.calls += 1
        return self.logits


def _onnx_like_pipeline(classifier=None):
    pytest.importorskip("numpy")
    pipeline = VisionPipeline(
        mode="onnx",
        classifier=classifier or _FakeClassifier(),
        locator=FaceLocator(detectors=[_FullFrameDetector()]),
        ear_au_provider=UnavailableEarAuProvider(),
    )
    pipeline.load()
    assert pipeline.model_loaded, "the injected classifier should mark the pipeline live"
    return pipeline


def test_a_real_image_flows_through_the_onnx_path():
    pipeline = _onnx_like_pipeline()
    body = pipeline.analyze(jpeg_frame(), timestamp_ms=TS_MS)

    assert pipeline._classifier.calls == 1, "the classifier must actually have run"
    assert body["mode"] == "onnx"
    assert body["face_detected"] is True
    assert set(body["class_probs"]) == LABELS
    assert sum(body["class_probs"].values()) == pytest.approx(1.0, abs=1e-5)
    assert body["p_fatigue"] == pytest.approx(
        body["class_probs"]["microsleep"] + body["class_probs"]["yawning"], abs=1e-4), \
        "the dashboard shows p_fatigue and the class probs together, so they must agree " \
        "to the 4 decimals the contract publishes"
    assert body["ts"] == pytest.approx(TS_MS / 1000.0, abs=1e-3)
    # No EAR/AU provider is wired up, so those signals must say so rather than guess.
    assert body["ear"] is None and body["au"] == {}
    assert body["signal_status"]["ear"] == "unavailable"
    assert body["signal_status"]["au"] == "not_configured"
    assert body["signal_status"]["face_detection"] == "opencv"


def test_onnx_path_does_not_classify_a_fallback_crop():
    """A centre crop is not a face; deriving a fatigue probability from one is a lie."""
    pytest.importorskip("numpy")
    from vision_service.face import FaceLocator as Locator

    pipeline = VisionPipeline(
        mode="onnx",
        classifier=_FakeClassifier(),
        locator=Locator(detectors=[]),   # no detector -> centre-crop fallback
        ear_au_provider=UnavailableEarAuProvider(),
    )
    pipeline.load()
    body = pipeline.analyze(jpeg_frame(), timestamp_ms=TS_MS)

    assert pipeline._classifier.calls == 0, "the classifier must not run on a fallback crop"
    assert body["face_detected"] is False
    assert body["signal_status"]["ear"] == "not_detected"
    assert "centre-crop" in body["signal_status"].get("reason", "")


def test_undecodable_frame_gets_a_200_shaped_no_face_answer():
    """Empty/garbage frames are normal input, not a 5xx the gateway must classify."""
    body = _onnx_like_pipeline().analyze("", timestamp_ms=TS_MS)
    assert body["face_detected"] is False
    assert sum(body["class_probs"].values()) == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# degraded / fallback policy — never silently substitute synthetic for real ML
# ---------------------------------------------------------------------------
def test_onnx_mode_without_a_model_is_degraded_and_refuses_to_fabricate():
    pipeline = VisionPipeline(mode="onnx", model_path="/nonexistent/efficientnet_fatigue.onnx")
    with TestClient(create_app(pipeline)) as c:
        health = c.get("/health").json()
        assert health["status"] == "degraded"
        assert health["degraded"] is True
        assert health["requested_mode"] == "onnx"
        assert health["model_loaded"] is False
        assert health["load_error"], "the reason must be reported, not hidden"

        response = c.post("/api/v1/analyze", json={"frame": "ZmFrZQ==", "timestamp": TS_MS})
        assert response.status_code == 503, "synthetic data must not stand in for the model"
        assert "inference is unavailable" in response.json()["detail"]


def test_explicit_fallback_serves_labelled_synthetic_telemetry():
    pipeline = VisionPipeline(mode="onnx", model_path="/nonexistent/model.onnx",
                              fallback_to_stub=True)
    with TestClient(create_app(pipeline)) as c:
        health = c.get("/health").json()
        assert health["status"] == "degraded", "it is still a degradation"
        assert health["fallback_to_stub"] is True
        assert health["mode"] == "stub", "and the served mode says so"

        body = c.post("/api/v1/analyze", json={"frame": "ZmFrZQ=="}).json()
        assert body["mode"] == "stub"
        assert body["signal_status"]["class_probs"] == "synthetic"


def test_unknown_vision_mode_is_reported_rather_than_silently_ignored():
    pipeline = VisionPipeline(mode="onnxx")
    pipeline.load()
    assert pipeline.mode == "stub"
    assert pipeline.load_error and "onnxx" in pipeline.load_error


# ---------------------------------------------------------------------------
# bind address resolution
# ---------------------------------------------------------------------------
def test_vision_port_wins_over_a_generic_port(monkeypatch):
    from vision_service.app import resolve_port

    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("VISION_PORT", "8000")
    assert resolve_port() == 8000


def test_a_usable_generic_port_is_honoured(monkeypatch):
    from vision_service.app import resolve_port

    monkeypatch.delenv("VISION_PORT", raising=False)
    monkeypatch.setenv("PORT", "9001")
    assert resolve_port() == 9001


@pytest.mark.parametrize("raw", ["0", "", "  ", "not-a-port", "-1", "70000", "8000.5"])
def test_an_unusable_port_falls_back_to_the_documented_default(monkeypatch, raw):
    """`PORT=0` means 'any free port' to uvicorn. Honouring it would move a service
    the gateway must reach at a fixed address onto a random port, which looks exactly
    like the service being down, so it must fall back instead."""
    from vision_service.app import DEFAULT_PORT, resolve_port

    monkeypatch.delenv("VISION_PORT", raising=False)
    monkeypatch.setenv("PORT", raw)
    assert resolve_port() == DEFAULT_PORT


@pytest.mark.parametrize("raw", ["0", "not-a-port", "-1", "70000", "8000.5", "8_000"])
def test_an_unusable_vision_port_is_a_configuration_error(monkeypatch, raw):
    """``VISION_PORT`` is ours, so a bad value must fail fast, not fall back.

    ``PORT`` (the host's variable) keeps the lenient fallback above; the boundary
    is ownership. Serving from a port the operator did not ask for is worse than
    refusing to start, and ``8_000`` is rejected because ``int("8_000") == 8000``
    would otherwise turn a typo into a silent no-op.
    """
    from vision_service.app import ConfigError, resolve_port

    monkeypatch.setenv("VISION_PORT", raw)
    with pytest.raises(ConfigError) as excinfo:
        resolve_port()
    assert "VISION_PORT" in str(excinfo.value)


def test_a_valid_vision_port_is_used_verbatim(monkeypatch):
    from vision_service.app import resolve_port

    monkeypatch.setenv("VISION_PORT", "8123")
    assert resolve_port() == 8123


def test_our_own_variable_is_validated_even_when_port_is_also_set(monkeypatch):
    """A bad VISION_PORT must not be masked by a perfectly good generic PORT."""
    from vision_service.app import ConfigError, resolve_port

    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("VISION_PORT", "0")
    with pytest.raises(ConfigError):
        resolve_port()


def test_bind_host_prefers_the_service_specific_variable(monkeypatch):
    from vision_service.app import resolve_host

    monkeypatch.setenv("HOST", "127.0.0.1")
    assert resolve_host() == "127.0.0.1"
    monkeypatch.setenv("VISION_HOST", "0.0.0.0")
    assert resolve_host() == "0.0.0.0"


# ---------------------------------------------------------------------------
# feature-provider seams
# ---------------------------------------------------------------------------
def test_eye_aspect_ratio_matches_the_six_point_formula():
    import math

    # An open eye: 2 units wide (p1 -> p4) with 1 unit of vertical opening.
    open_eye = [(0.0, 0.0), (0.5, 0.5), (1.5, 0.5),
                (2.0, 0.0), (1.5, 0.5), (0.5, 0.5)]
    horizontal = math.dist(open_eye[0], open_eye[3])
    vertical = math.dist(open_eye[1], open_eye[5]) + math.dist(open_eye[2], open_eye[4])
    expected = vertical / (2.0 * horizontal)
    assert eye_aspect_ratio(open_eye, tuple(range(6))) == pytest.approx(expected, abs=1e-9)

    # Deprecated landmarks: the eye is reduced to a line, so EAR is 0.
    closed = [(0.0, 0.0), (0.5, 0.0), (1.5, 0.0), (2.0, 0.0), (1.5, 0.0), (0.5, 0.0)]
    assert eye_aspect_ratio(closed, tuple(range(6))) == pytest.approx(0.0, abs=1e-9)

    assert eye_aspect_ratio([(0.0, 0.0)], LEFT_EYE) is None, "must not fabricate on missing landmarks"
    assert LEFT_EYE != RIGHT_EYE, "the two eyes use different landmark indices"


def test_unavailable_ear_au_provider_produces_nothing_at_all():
    from vision_service.face import FaceCrop

    result = UnavailableEarAuProvider().extract(FaceCrop(image=None, source=FaceSource.NONE))
    assert result.ear is None
    assert result.au == {}
    assert result.ear_status.value == "unavailable"
