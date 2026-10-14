"""utils/evaluation/check_vision_endpoints.py: request format, the ok / fail reading, the summary. No workspace call:
the HTTP side is an httpx.MockTransport."""
import importlib.util
import json
import os

import httpx

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "utils", "evaluation", "check_vision_endpoints.py")
spec = importlib.util.spec_from_file_location("check_vision_endpoints", SCRIPT)
cve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cve)

HOST = "https://workspace.example"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _answer(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_only_png_jpg_and_jpeg_files_are_checked(tmp_path):
    for name in ("b.JPG", "a.png", "c.jpeg", "d.txt", "e.gif"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in cve.image_files(tmp_path)] == ["a.png", "b.JPG", "c.jpeg"]


def test_the_image_goes_as_a_data_url_in_the_chat_format(tmp_path):
    image = tmp_path / "plan.png"
    image.write_bytes(b"\x89PNG-bytes")
    url = cve.data_url(image)
    assert url.startswith("data:image/png;base64,")
    payload = cve.build_payload("Describe it.", url)
    assert payload["messages"][0]["content"][0] == {"type": "text", "text": "Describe it."}
    assert payload["messages"][0]["content"][1] == {"type": "image_url", "image_url": {"url": url}}


def test_an_answer_with_text_is_ok_and_the_request_goes_to_the_endpoint(tmp_path):
    image = tmp_path / "pump.jpg"
    image.write_bytes(b"jpg")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return _answer("A pump with a label.")

    result = cve.check_one(_client(handler), HOST, "tok", "databricks-gpt-5-6-luna", image, "Q?")
    assert result.ok and result.status == 200 and result.text == "A pump with a label."
    assert seen["url"] == f"{HOST}/serving-endpoints/databricks-gpt-5-6-luna/invocations"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_a_refusal_is_a_fail_with_its_http_status(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    result = cve.check_one(_client(lambda r: httpx.Response(400, text="image not supported")),
                           HOST, "tok", "ep", image, "Q?")
    assert not result.ok and result.status == 400 and result.text == "image not supported"


def test_an_empty_answer_is_a_fail(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b"x")
    result = cve.check_one(_client(lambda r: _answer("  ")), HOST, "tok", "ep", image, "Q?")
    assert not result.ok and result.text == "empty answer"


def test_a_transport_error_is_a_fail_without_status(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b"x")

    def handler(request):
        raise httpx.ConnectTimeout("too slow", request=request)

    result = cve.check_one(_client(handler), HOST, "tok", "ep", image, "Q?")
    assert not result.ok and result.status is None and "ConnectTimeout" in result.text


def test_the_summary_counts_ok_and_fail_per_endpoint():
    results = [
        cve.CheckResult("a", "1.png", True, 200, 0.1, "x"),
        cve.CheckResult("a", "2.png", False, 400, 0.1, "y"),
        cve.CheckResult("b", "1.png", True, 200, 0.1, "z"),
    ]
    assert cve.summarize(results) == {"a": (1, 1), "b": (1, 0)}
