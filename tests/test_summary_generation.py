"""Synthetic loopback HTTP exercises the real summary provider protocol."""

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from hexatrek_exporter.summary_generation import generate_summaries
from hexatrek_exporter.summaries import summary_document


def completion(texts, **message_fields):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"summaries": texts}), **message_fields}}]}


@pytest.fixture
def source(tmp_path):
    def create(count=1, cached=None, language="de"):
        points = [{"id": f"poi-{index}", "name": f"Synthetic spring {index}",
                   "type": "Source", "description": "Water untested; carry a filter.",
                   "summary": cached,
                   "comments": [
                       {"created_at": "2025-01-01T00:00:00Z", "text": "Water flowed.",
                        "author": "PRIVATE_AUTHOR", "images": ["PRIVATE_PHOTO"]},
                       {"created_at": "2026-10-02T00:00:00Z", "text": "Dry; access closed.",
                        "author": "PRIVATE_AUTHOR"}],
                   "links": ["PRIVATE_LINK"], "raw": {"secret": "PRIVATE_RAW"}}
                  for index in range(count)]
        manifest = {"schema_version": 1, "pois": [*points, {"id": "unused"}],
                    "routes": [{"poi_ids": [p["id"] for p in points]},
                               {"poi_ids": [p["id"] for p in reversed(points)]}]}
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        document = summary_document(path, language=language)
        return path, document
    return create


@pytest.fixture
def provider():
    servers = []

    def start(responder=None):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append({"path": self.path, "headers": dict(self.headers), "payload": payload})
                data = json.loads(payload["messages"][1]["content"])
                if responder is None:
                    status, body = 200, completion({point["id"]: "Water uncertain."
                                                   for point in data["untrusted_pois"]})
                else:
                    status, body = responder(payload, data)
                if status is None:
                    self.close_connection = True
                    return
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if status == 302:
                    self.send_header("Location", "/stolen")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/v1", seen

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("language,text", [
    ("en", "Reported dry on 2026-10-02; access closed. Water untested; carry a filter."),
    ("fr", "Signalée sèche le 2026-10-02 ; accès fermé. Eau non contrôlée ; prévoir un filtre."),
])
def test_language_payload_output_and_source_privacy(source, provider, language, text):
    path, document = source(language=language)
    before = path.read_bytes()
    original = deepcopy(document)
    base, seen = provider(lambda _payload, data: (
        200, completion({point["id"]: text for point in data["untrusted_pois"]})))
    result = generate_summaries(path, document, api_base=base, model="synthetic-model", api_key="TEST_KEY")
    assert result["summaries"] == {"poi-0": text}
    assert result["summary_language"] == language
    assert document == original
    assert path.read_bytes() == before
    assert len(seen) == 1
    request = seen[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer TEST_KEY"
    assert request["payload"]["model"] == "synthetic-model"
    assert request["payload"]["stream"] is False
    assert request["payload"]["response_format"] == {"type": "json_object"}
    data = json.loads(request["payload"]["messages"][1]["content"])
    assert data["summary_language"] == language
    point = data["untrusted_pois"][0]
    assert point["name"] == "Synthetic spring 0"
    assert point["type"] == "Source"
    assert point["description"] == "Water untested; carry a filter."
    assert point["comments"] == [
        {"created_at": "2026-10-02T00:00:00Z", "text": "Dry; access closed."},
        {"created_at": "2025-01-01T00:00:00Z", "text": "Water flowed."}]
    sent = json.dumps(request["payload"])
    assert "PRIVATE_" not in sent
    assert "unused" not in sent
    assert "UNTRUSTED DATA" in sent
    assert "250 UTF-16" in sent


def test_fixed_batches_and_id_mapping(source, provider, monkeypatch):
    path, document = source(count=23)
    document["summaries"]["poi-1"] = ""
    document["summaries"]["poi-7"] = "Reviewed cached text."
    document["summaries"]["poi-12"] = "X" * 251
    base, seen = provider(lambda _payload, data: (200, completion({
        point["id"]: "Generated " + point["id"] for point in reversed(data["untrusted_pois"])})))
    # A broken environment proxy must never receive source content.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    result = generate_summaries(path, document, api_base=base, model="model")
    batches = [json.loads(r["payload"]["messages"][1]["content"])["untrusted_pois"] for r in seen]
    assert [len(batch) for batch in batches] == [10, 10, 1]
    assert all("Authorization" not in r["headers"] for r in seen)
    assert result["summaries"]["poi-1"] == ""
    assert result["summaries"]["poi-7"] == "Reviewed cached text."
    assert result["summaries"]["poi-12"] == "Generated poi-12"
    invalid = next(point for batch in batches for point in batch if point["id"] == "poi-12")
    assert invalid["previous_summary"] == "X" * 251
    assert set(result["summaries"]) == {f"poi-{index}" for index in range(23)}


@pytest.mark.parametrize("text", ["", "Reviewed text.", "😀" * 125])
def test_complete_cache_needs_no_configuration_or_credentials(source, text):
    path, document = source(cached=text)
    result = generate_summaries(path, document, api_base=None, model=None)
    assert result == document
    assert result is not document
    assert result["summaries"] is not document["summaries"]


@pytest.mark.parametrize("base", [None, "", "https://user:PRIVATE_KEY@example.test/v1",
    "http://example.test/v1", "http://127.0.0.2/v1", "ftp://localhost/v1",
    "https://example.test/v1?secret=PRIVATE_KEY", "https://example.test/v1#PRIVATE_KEY",
    "https://example.test/v1?", "https://example.test/v1#", "https://example.test/\nPRIVATE_KEY",
    "https://example.test:invalid", "https://example.test\\@localhost/v1"])
def test_invalid_endpoint_rejected_without_disclosure(source, base):
    path, document = source()
    with pytest.raises(ValueError) as error:
        generate_summaries(path, document, api_base=base, model="model")
    assert "PRIVATE_KEY" not in str(error.value)


@pytest.mark.parametrize("model", [None, "", "   "])
def test_missing_model_rejected_before_network(source, model):
    path, document = source()
    with pytest.raises(ValueError, match="explicit nonempty model"):
        generate_summaries(path, document, api_base="http://127.0.0.1:1/v1", model=model)


@pytest.mark.parametrize("key", ["", "\nPRIVATE_KEY", "clé"])
def test_invalid_supplied_key_rejected(source, key):
    path, document = source()
    with pytest.raises(ValueError, match="API key") as error:
        generate_summaries(path, document, api_base="http://127.0.0.1:1", model="model", api_key=key)
    assert "PRIVATE_KEY" not in str(error.value)


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_provider_errors_no_retry_redirect_or_body_disclosure(source, provider, status):
    path, document = source()
    base, seen = provider(lambda *_args: (status, b"PRIVATE_PROVIDER_BODY PRIVATE_KEY"))
    with pytest.raises(ValueError) as error:
        generate_summaries(path, document, api_base=base, model="model", api_key="PRIVATE_KEY")
    assert len(seen) == 1
    assert "PRIVATE_" not in str(error.value)
    assert document["summaries"] == {"poi-0": None}


@pytest.mark.parametrize("body", [
    b"not JSON PRIVATE_BODY", b'{"choices":[],"choices":[]}', {}, {"choices": []},
    {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]},
    completion({"poi-0": "Valid"}, refusal="PRIVATE_REFUSAL"),
    completion({"poi-0": "Valid"}, tool_calls=[{}]),
    completion({}), completion({"unknown": "Text"}),
    completion({"poi-0": "Text", "unknown": "Text"}),
    completion({"poi-0": None}), completion({"poi-0": 4}), completion({"poi-0": ""}),
    completion({"poi-0": "   "}), completion({"poi-0": "x" * 251}),
    completion({"poi-0": "😀" * 126}), completion({"poi-0": "unsafe\u0000text"}),
    completion({"poi-0": "unsafe\ud800text"}),
    completion({}, content='{"summaries":{"poi-0":"first","poi-0":"second"}}'),
    completion({}, content='{"summaries":{"poi-0":"ok"},"extra":true}'),
    completion({}, content='{"summaries":{"poi-0":NaN}}'),
    b"x" * (256 * 1024 + 1),
])
def test_bad_completion_rejected_without_disclosure_or_mutation(source, provider, body):
    path, document = source()
    before = deepcopy(document)
    base, seen = provider(lambda *_args: (200, body))
    with pytest.raises(ValueError) as error:
        generate_summaries(path, document, api_base=base, model="model", api_key="PRIVATE_KEY")
    assert len(seen) == 1
    assert "PRIVATE_" not in str(error.value)
    assert document == before


def test_hash_mismatch_rejected_without_network(source):
    path, document = source()
    document["source_manifest_sha256"] = hashlib.sha256(b"other source").hexdigest()
    with pytest.raises(ValueError, match="does not match"):
        generate_summaries(path, document, api_base=None, model=None)


def test_transport_error_redacts_endpoint(source):
    path, document = source()
    # Port zero is rejected locally, without any connection attempt.
    with pytest.raises(ValueError) as error:
        generate_summaries(path, document, api_base="http://127.0.0.1:0/PRIVATE_PATH", model="model")
    assert "PRIVATE_PATH" not in str(error.value)


def test_connection_closed_redacts_transport_exception(source, provider):
    path, document = source()
    base, seen = provider(lambda *_args: (None, None))
    with pytest.raises(ValueError, match="Summary provider request failed") as error:
        generate_summaries(path, document, api_base=base + "/PRIVATE_PATH",
                           model="model", api_key="PRIVATE_KEY")
    assert len(seen) == 1
    assert "PRIVATE_" not in str(error.value)
