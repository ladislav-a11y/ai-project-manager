import pytest
import requests

from ai_project_manager.trello_client import (
    InMemoryTrelloClient,
    RealTrelloClient,
    TrelloError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Records every call and answers from a queue of scripted responses,
    keyed by (method, path) - so a test can assert exactly which Trello
    endpoints were hit, with what params, and in what order."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def script(self, method, path, payload):
        self._responses.setdefault((method, path), []).append(payload)

    def request(self, method, url, params=None, **kwargs):
        path = url[len(RealTrelloClient.BASE_URL):]
        self.calls.append({"method": method, "path": path, "params": params or {}, **kwargs})
        queue = self._responses.get((method, path))
        if not queue:
            raise AssertionError(f"no scripted response for {method} {path}")
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return FakeResponse(payload=payload)


def make_client(session=None, **kwargs):
    return RealTrelloClient(key="k", token="t", board_id="board-1", session=session or FakeSession(), **kwargs)


@pytest.mark.parametrize("read_method", ["get_card", "list_cards", "list_all_cards"])
def test_in_memory_reads_do_not_expose_mutable_nested_card_state(read_method):
    client = InMemoryTrelloClient(["Ready"])
    list_id = client.get_list_id_by_name("Ready")
    created = client.create_card(list_id, "Demo", labels=["P3"])

    if read_method == "get_card":
        card = client.get_card(created["id"])
    elif read_method == "list_cards":
        card = client.list_cards(list_id)[0]
    else:
        card = client.list_all_cards()[0]

    card["labels"][0]["name"] = "P0"
    card["labels"].append({"id": "injected", "name": "P5"})

    assert client.get_card(created["id"])["labels"] == [
        {"id": "label-P3", "name": "P3"}
    ]


def test_in_memory_write_results_do_not_expose_mutable_nested_card_state():
    client = InMemoryTrelloClient(["Ready"])
    list_id = client.get_list_id_by_name("Ready")

    created = client.create_card(list_id, "Demo", labels=["P3"])
    created["labels"][0]["name"] = "P0"
    assert client.get_card(created["id"])["labels"][0]["name"] == "P3"

    updated = client.update_card(created["id"], labels=["P4"])
    updated["labels"][0]["name"] = "P1"
    assert client.get_card(created["id"])["labels"][0]["name"] == "P4"


def test_in_memory_create_card_synthesizes_a_url():
    client = InMemoryTrelloClient(["Ready"])
    list_id = client.get_list_id_by_name("Ready")

    created = client.create_card(list_id, "Demo")

    assert created["url"] == f"https://trello.com/c/{created['id']}"
    assert client.get_card(created["id"])["url"] == created["url"]


def test_missing_credentials_raises_trello_error(monkeypatch):
    # Explicit None args must not silently fall back to whatever
    # TRELLO_* env vars happen to be set in the process (e.g. the real
    # ones this orchestrator runs with) - clear them so the test
    # actually exercises the "nothing configured" path.
    for name in ("TRELLO_KEY", "TRELLO_TOKEN", "TRELLO_BOARD_ID"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(TrelloError):
        RealTrelloClient(key=None, token=None, board_id=None, session=FakeSession())


def test_request_sends_auth_params_and_a_hard_timeout():
    session = FakeSession()
    session.script("GET", "/boards/board-1/lists", [])
    client = make_client(session, timeout=5)

    client.list_lists()

    call = session.calls[0]
    assert call["params"]["key"] == "k"
    assert call["params"]["token"] == "t"
    assert call["timeout"] == 5


def test_request_raises_trello_error_on_http_error_status():
    class ErrorSession(FakeSession):
        def request(self, method, url, params=None, **kwargs):
            return FakeResponse(
                status_code=404,
                text="request rejected; token=super-secret-token",
            )

    client = make_client(ErrorSession())

    with pytest.raises(
        TrelloError,
        match=r"^Trello API error 404: GET /boards/board-1/lists$",
    ) as caught:
        client.list_lists()

    assert "super-secret-token" not in str(caught.value)


def test_request_includes_safe_trello_error_detail_without_leaking_secrets():
    class ErrorSession(FakeSession):
        def request(self, method, url, params=None, **kwargs):
            return FakeResponse(
                status_code=400,
                payload={
                    "message": "invalid value for idLabels",
                    "code": "BAD_REQUEST",
                    "error": "token=super-secret-token",
                },
            )

    client = make_client(ErrorSession())

    with pytest.raises(TrelloError) as caught:
        client.list_lists()

    message = str(caught.value)
    assert message == (
        "Trello API error 400: GET /boards/board-1/lists: "
        "message=invalid value for idLabels; code=BAD_REQUEST"
    )
    assert "super-secret-token" not in message


def test_request_wraps_transport_error_without_leaking_credentials():
    class FailingSession(FakeSession):
        def request(self, method, url, params=None, **kwargs):
            prepared_url = f"{url}?key={params['key']}&token={params['token']}"
            raise requests.ConnectionError(f"failed to connect to {prepared_url}")

    client = RealTrelloClient(
        key="super-secret-key",
        token="super-secret-token",
        board_id="board-1",
        session=FailingSession(),
        sleep_fn=lambda _: None,
    )

    with pytest.raises(TrelloError) as caught:
        client.list_lists()

    message = str(caught.value)
    assert message == "Trello API request failed: GET /boards/board-1/lists"
    assert "super-secret-key" not in message
    assert "super-secret-token" not in message


def test_get_caps_transport_error_backoff_with_configured_retry_delay():
    class RecoveringSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def request(self, method, url, params=None, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise requests.ConnectionError("temporary outage")
            return FakeResponse(payload=[])

    delays = []
    session = RecoveringSession()
    client = make_client(
        session,
        max_retry_delay=0.25,
        sleep_fn=delays.append,
    )

    assert client.list_lists() == []
    assert session.attempts == 3
    assert delays == [0.25, 0.25]


def test_request_wraps_invalid_json_response():
    class InvalidJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("not JSON")

    class InvalidJsonSession(FakeSession):
        def request(self, method, url, params=None, **kwargs):
            return InvalidJsonResponse(status_code=200, text="<html>upstream error</html>")

    client = make_client(InvalidJsonSession())

    with pytest.raises(
        TrelloError,
        match=r"^Trello API returned invalid JSON: GET /boards/board-1/lists$",
    ):
        client.list_lists()


def test_get_retries_rate_limit_and_honors_retry_after():
    class RateLimitedSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.responses = [
                FakeResponse(status_code=429, headers={"Retry-After": "7"}),
                FakeResponse(payload=[]),
            ]

        def request(self, method, url, params=None, **kwargs):
            self.calls.append({"method": method, "url": url})
            return self.responses.pop(0)

    delays = []
    session = RateLimitedSession()
    client = make_client(session, sleep_fn=delays.append)

    assert client.list_lists() == []
    assert len(session.calls) == 2
    assert delays == [7.0]


@pytest.mark.parametrize(
    "retry_after",
    ["86400", "Fri, 31 Dec 9999 23:59:59 GMT"],
)
def test_get_caps_excessive_retry_after_so_scheduler_tick_cannot_wedge(retry_after):
    class RateLimitedSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.responses = [
                FakeResponse(status_code=429, headers={"Retry-After": retry_after}),
                FakeResponse(payload=[]),
            ]

        def request(self, method, url, params=None, **kwargs):
            return self.responses.pop(0)

    delays = []
    client = make_client(
        RateLimitedSession(),
        max_retry_delay=12,
        sleep_fn=delays.append,
    )

    assert client.list_lists() == []
    assert delays == [12.0]


def test_write_is_not_retried_after_transient_server_error():
    class UnavailableSession(FakeSession):
        def request(self, method, url, params=None, **kwargs):
            self.calls.append({"method": method, "url": url})
            return FakeResponse(status_code=503)

    session = UnavailableSession()
    client = make_client(session, sleep_fn=lambda _: None)

    with pytest.raises(TrelloError, match=r"API error 503: POST /cards"):
        client.create_card("list-1", "Do not duplicate")

    assert len(session.calls) == 1


def test_read_retry_attempt_count_must_be_positive():
    with pytest.raises(ValueError, match="max_read_attempts must be at least 1"):
        make_client(max_read_attempts=0)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_timeout_must_be_a_finite_positive_number(timeout):
    with pytest.raises(ValueError, match="timeout must be a finite positive number"):
        make_client(timeout=timeout)


@pytest.mark.parametrize("attempts", [1.5, True, "3"])
def test_read_retry_attempt_count_must_be_an_integer(attempts):
    with pytest.raises(ValueError, match="max_read_attempts must be a positive integer"):
        make_client(max_read_attempts=attempts)


@pytest.mark.parametrize("delay", [-1, float("inf"), float("nan"), True, "30"])
def test_max_retry_delay_must_be_a_finite_non_negative_number(delay):
    with pytest.raises(ValueError, match="max_retry_delay must be a finite non-negative number"):
        make_client(max_retry_delay=delay)


@pytest.mark.parametrize(
    ("method_name", "args", "path", "payload", "message"),
    [
        ("list_lists", (), "/boards/board-1/lists", {}, "expected array"),
        ("list_cards", ("list-1",), "/lists/list-1/cards", {}, "expected array"),
        ("get_card", ("card-1",), "/cards/card-1", [], "expected object"),
    ],
)
def test_resource_methods_reject_valid_json_with_the_wrong_top_level_shape(
    method_name, args, path, payload, message
):
    session = FakeSession()
    session.script("GET", path, payload)
    client = make_client(session)

    with pytest.raises(TrelloError, match=message):
        getattr(client, method_name)(*args)


@pytest.mark.parametrize(
    ("method_name", "args", "path", "payload", "operation"),
    [
        ("list_lists", (), "/boards/board-1/lists", [None], "board lists"),
        ("list_cards", ("list-1",), "/lists/list-1/cards", ["card"], "list cards"),
    ],
)
def test_collection_methods_reject_non_object_members(
    method_name, args, path, payload, operation
):
    session = FakeSession()
    session.script("GET", path, payload)
    client = make_client(session)

    with pytest.raises(
        TrelloError,
        match=rf"invalid payload for {operation}: expected an array of objects$",
    ):
        getattr(client, method_name)(*args)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "missing id", "labels": []},
        {"id": "card-1", "name": None, "desc": "", "idList": "list-1", "labels": []},
        {"id": "card-1", "name": "Demo", "desc": None, "idList": "list-1", "labels": []},
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "", "labels": []},
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": None},
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": [None]},
        {
            "id": "card-1",
            "name": "Demo",
            "desc": "",
            "idList": "list-1",
            "labels": [{"id": "", "name": "P3"}],
        },
        {
            "id": "card-1",
            "name": "Demo",
            "desc": "",
            "idList": "list-1",
            "labels": [{"id": "label-1", "name": None}],
        },
    ],
)
def test_get_card_rejects_malformed_card_fields(payload):
    session = FakeSession()
    session.script("GET", "/cards/card-1", payload)
    client = make_client(session)

    with pytest.raises(TrelloError, match="invalid card payload"):
        client.get_card("card-1")


def test_label_collection_rejects_non_object_members_before_creating_a_card():
    session = FakeSession()
    session.script("GET", "/boards/board-1/labels", [None])
    client = make_client(session)

    with pytest.raises(
        TrelloError,
        match=r"invalid payload for board labels: expected an array of objects$",
    ):
        client.create_card("list-1", "Demo", labels=["P3"])

    assert [call["path"] for call in session.calls] == ["/boards/board-1/labels"]


def test_list_cards_and_get_card_map_raw_trello_shape():
    session = FakeSession()
    raw_card = {
        "id": "card-1",
        "name": "Demo",
        "desc": "some description",
        "idList": "list-1",
        "labels": [{"id": "label-1", "name": "P3", "color": "yellow"}],
    }
    session.script("GET", "/lists/list-1/cards", [raw_card])
    session.script("GET", "/cards/card-1", raw_card)
    client = make_client(session)

    [card] = client.list_cards("list-1")
    assert card == {
        "id": "card-1",
        "name": "Demo",
        "desc": "some description",
        "list_id": "list-1",
        "labels": [{"id": "label-1", "name": "P3"}],
        "url": None,
    }

    assert client.get_card("card-1") == card


def test_list_cards_maps_short_url_to_url_field():
    session = FakeSession()
    raw_card = {
        "id": "card-1",
        "name": "Demo",
        "desc": "",
        "idList": "list-1",
        "labels": [],
        "shortUrl": "https://trello.com/c/abc123",
    }
    session.script("GET", "/lists/list-1/cards", [raw_card])
    client = make_client(session)

    [card] = client.list_cards("list-1")

    assert card["url"] == "https://trello.com/c/abc123"


def test_create_card_resolves_existing_label_names_to_ids():
    session = FakeSession()
    session.script(
        "GET", "/boards/board-1/labels",
        [{"id": "label-p3", "name": "P3"}, {"id": "label-p5", "name": "P5"}],
    )
    session.script(
        "POST", "/cards",
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.create_card("list-1", "Demo", labels=["P3"])

    create_call = next(c for c in session.calls if c["method"] == "POST" and c["path"] == "/cards")
    assert create_call["data"]["idLabels"] == "label-p3"
    # Labels were looked up once and cached - no repeated /labels call.
    assert sum(1 for c in session.calls if c["path"] == "/boards/board-1/labels") == 1


def test_create_card_auto_creates_a_missing_board_label():
    session = FakeSession()
    session.script("GET", "/boards/board-1/labels", [])
    session.script("POST", "/labels", {"id": "label-p0", "name": "P0"})
    session.script(
        "POST", "/cards",
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.create_card("list-1", "Demo", labels=["P0"])

    create_label_call = next(c for c in session.calls if c["method"] == "POST" and c["path"] == "/labels")
    assert create_label_call["data"]["name"] == "P0"
    assert create_label_call["data"]["idBoard"] == "board-1"

    create_card_call = next(c for c in session.calls if c["method"] == "POST" and c["path"] == "/cards")
    assert create_card_call["data"]["idLabels"] == "label-p0"
    assert sum(1 for c in session.calls if c["path"] == "/boards/board-1/labels") == 1


@pytest.mark.parametrize("payload", [[], {}, {"id": ""}])
def test_create_card_rejects_malformed_created_label(payload):
    session = FakeSession()
    session.script("GET", "/boards/board-1/labels", [])
    session.script("POST", "/labels", payload)
    client = make_client(session)

    with pytest.raises(TrelloError, match="invalid payload for created label"):
        client.create_card("list-1", "Demo", labels=["P0"])

    assert "/cards" not in [call["path"] for call in session.calls]


def test_update_card_can_clear_labels_without_loading_board_labels():
    session = FakeSession()
    session.script(
        "PUT", "/cards/card-1",
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.update_card("card-1", labels=[])

    update_call = next(c for c in session.calls if c["method"] == "PUT" and c["path"] == "/cards/card-1")
    assert update_call["data"]["idLabels"] == ""
    assert "/boards/board-1/labels" not in [c["path"] for c in session.calls]


def test_update_card_sends_idlabels_for_priority_change():
    session = FakeSession()
    session.script("GET", "/boards/board-1/labels", [{"id": "label-p4", "name": "P4"}])
    session.script(
        "PUT", "/cards/card-1",
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.update_card("card-1", labels=["P4"])

    update_call = next(c for c in session.calls if c["method"] == "PUT" and c["path"] == "/cards/card-1")
    assert update_call["data"]["idLabels"] == "label-p4"


def test_update_card_without_labels_never_touches_idlabels():
    session = FakeSession()
    session.script(
        "PUT", "/cards/card-1",
        {"id": "card-1", "name": "New name", "desc": "", "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.update_card("card-1", name="New name")

    update_call = next(c for c in session.calls if c["method"] == "PUT" and c["path"] == "/cards/card-1")
    assert "idLabels" not in update_call["data"]
    assert "/boards/board-1/labels" not in [c["path"] for c in session.calls]


def test_move_card_delegates_to_update_card_with_list_id():
    session = FakeSession()
    session.script(
        "PUT", "/cards/card-1",
        {"id": "card-1", "name": "Demo", "desc": "", "idList": "list-2", "labels": []},
    )
    client = make_client(session)

    client.move_card("card-1", "list-2")

    update_call = next(c for c in session.calls if c["method"] == "PUT" and c["path"] == "/cards/card-1")
    assert update_call["data"]["idList"] == "list-2"


def test_update_card_sends_long_description_in_body_not_url_query():
    session = FakeSession()
    long_desc = "x" * 20000
    session.script(
        "PUT", "/cards/card-1",
        {"id": "card-1", "name": "Demo", "desc": long_desc, "idList": "list-1", "labels": []},
    )
    client = make_client(session)

    client.update_card("card-1", desc=long_desc)

    update_call = next(c for c in session.calls if c["method"] == "PUT")
    assert update_call["data"]["desc"] == long_desc
    assert update_call["params"] == {"key": "k", "token": "t"}


def test_get_list_id_by_name_finds_matching_list():
    session = FakeSession()
    session.script(
        "GET", "/boards/board-1/lists",
        [{"id": "list-1", "name": "Inbox"}, {"id": "list-2", "name": "Ready"}],
    )
    client = make_client(session)

    assert client.get_list_id_by_name("Ready") == "list-2"
    assert client.get_list_id_by_name("Nonexistent") is None


@pytest.mark.parametrize(
    "payload",
    [[{}], [{"id": "", "name": "Inbox"}], [{"id": "list-1"}], [{"id": "list-1", "name": None}]],
)
def test_list_lists_rejects_objects_with_missing_required_fields(payload):
    session = FakeSession()
    session.script("GET", "/boards/board-1/lists", payload)
    client = make_client(session)

    with pytest.raises(TrelloError, match="invalid payload for board lists"):
        client.list_lists()


def test_existing_named_label_without_id_is_rejected_cleanly():
    session = FakeSession()
    session.script("GET", "/boards/board-1/labels", [{"name": "P0"}])
    client = make_client(session)

    with pytest.raises(TrelloError, match="invalid payload for board labels"):
        client.create_card("list-1", "Demo", labels=["P0"])

    assert "/cards" not in [call["path"] for call in session.calls]
