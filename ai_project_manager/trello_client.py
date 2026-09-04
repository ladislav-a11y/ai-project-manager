"""Trello API access.

Defines a small ``TrelloClient`` protocol plus two implementations:

- ``InMemoryTrelloClient`` - an in-process fake used by tests and local
  development so the rest of the system can be exercised without real
  Trello credentials.
- ``RealTrelloClient`` - a thin wrapper around the Trello REST API using
  ``requests``, configured via TRELLO_KEY / TRELLO_TOKEN / TRELLO_BOARD_ID
  environment variables.

Both speak the same plain-dict "card" shape:
    {
        "id": str,
        "name": str,
        "desc": str,
        "list_id": str,
        "labels": [{"id": str, "name": str}, ...],
    }
so that trello_sync.py can work against either without caring which one
is in use.
"""

from __future__ import annotations

from copy import deepcopy
import os
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional, Protocol

import requests


# Keep a safety margin below Trello/proxy description limits.  All card
# writers, including callers that bypass trello_sync, must reject a larger
# payload instead of allowing a truncated description to destroy PM-DATA.
MAX_TRELLO_DESC_CHARS = 14000


def _validate_card_description(desc: str) -> None:
    if not isinstance(desc, str):
        raise TrelloError("card description must be a string")
    if len(desc) > MAX_TRELLO_DESC_CHARS:
        raise TrelloError(
            "card description exceeds the safe limit of "
            f"{MAX_TRELLO_DESC_CHARS} characters; refusing the write"
        )


class TrelloClient(Protocol):
    """Minimal surface the rest of the system needs from Trello."""

    def list_lists(self) -> list[dict]:
        ...

    def get_list_id_by_name(self, name: str) -> Optional[str]:
        ...

    def list_cards(self, list_id: str) -> list[dict]:
        ...

    def get_card(self, card_id: str) -> dict:
        ...

    def create_card(self, list_id: str, name: str, desc: str = "", labels: Optional[list[str]] = None) -> dict:
        ...

    def update_card(
        self,
        card_id: str,
        name: Optional[str] = None,
        desc: Optional[str] = None,
        list_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
        position: Optional[str | float] = None,
    ) -> dict:
        ...

    def move_card(self, card_id: str, list_id: str) -> dict:
        ...

    def archive_card(self, card_id: str) -> dict:
        ...


class TrelloError(RuntimeError):
    pass


class InMemoryTrelloClient:
    """In-process fake Trello board. Deterministic, no network calls.

    Used for tests and for running the Project Manager before real
    Trello credentials are wired up.
    """

    def __init__(self, list_names: Iterable[str] = ("Inbox", "New", "Ready", "In Progress", "Testing", "Paused", "Blocked", "Done", "Error")):
        self._lists: dict[str, dict] = {}
        self._cards: dict[str, dict] = {}
        self._next_list_id = 1
        self._next_card_id = 1
        for name in list_names:
            self.add_list(name)

    def add_list(self, name: str) -> dict:
        list_id = f"list-{self._next_list_id}"
        self._next_list_id += 1
        lst = {"id": list_id, "name": name}
        self._lists[list_id] = lst
        return lst

    def list_lists(self) -> list[dict]:
        return list(self._lists.values())

    def get_list_id_by_name(self, name: str) -> Optional[str]:
        for lst in self._lists.values():
            if lst["name"] == name:
                return lst["id"]
        return None

    def list_cards(self, list_id: str) -> list[dict]:
        cards = [
            c for c in self._cards.values()
            if c["list_id"] == list_id and not c.get("closed", False)
        ]
        return [deepcopy(c) for c in sorted(cards, key=lambda c: c.get("position", 0))]

    def list_all_cards(self) -> list[dict]:
        return [deepcopy(c) for c in self._cards.values()]

    def get_card(self, card_id: str) -> dict:
        if card_id not in self._cards:
            raise TrelloError(f"unknown card {card_id}")
        return deepcopy(self._cards[card_id])

    def create_card(self, list_id: str, name: str, desc: str = "", labels: Optional[list[str]] = None) -> dict:
        if list_id not in self._lists:
            raise TrelloError(f"unknown list {list_id}")
        _validate_card_description(desc)
        card_id = f"card-{self._next_card_id}"
        self._next_card_id += 1
        card = {
            "id": card_id,
            "name": name,
            "desc": desc,
            "list_id": list_id,
            "labels": [{"id": f"label-{n}", "name": n} for n in (labels or [])],
            "url": f"https://trello.com/c/{card_id}",
            "position": len(self._cards) + 1,
            "closed": False,
            "last_activity_at": datetime.now(timezone.utc).isoformat(),
        }
        self._cards[card_id] = card
        return deepcopy(card)

    def update_card(
        self,
        card_id: str,
        name: Optional[str] = None,
        desc: Optional[str] = None,
        list_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
        position: Optional[str | float] = None,
    ) -> dict:
        if card_id not in self._cards:
            raise TrelloError(f"unknown card {card_id}")
        if desc is not None:
            _validate_card_description(desc)
        card = self._cards[card_id]
        if name is not None:
            card["name"] = name
        if desc is not None:
            card["desc"] = desc
        if list_id is not None:
            if list_id not in self._lists:
                raise TrelloError(f"unknown list {list_id}")
            card["list_id"] = list_id
        if labels is not None:
            card["labels"] = [{"id": f"label-{n}", "name": n} for n in labels]
        if position is not None:
            peers = [
                c.get("position", 0) for c in self._cards.values()
                if c["list_id"] == card["list_id"] and c["id"] != card_id
            ]
            if position == "top":
                card["position"] = (min(peers) - 1) if peers else 0
            elif position == "bottom":
                card["position"] = (max(peers) + 1) if peers else 0
            else:
                card["position"] = position
        card["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        return deepcopy(card)

    def move_card(self, card_id: str, list_id: str) -> dict:
        return self.update_card(card_id, list_id=list_id)

    def archive_card(self, card_id: str) -> dict:
        if card_id not in self._cards:
            raise TrelloError(f"unknown card {card_id}")
        self._cards[card_id]["closed"] = True
        self._cards[card_id]["last_activity_at"] = datetime.now(timezone.utc).isoformat()
        return deepcopy(self._cards[card_id])


class RealTrelloClient:
    """Thin wrapper around the real Trello REST API.

    Configured from environment variables so no secrets live in code:
      TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID
    """

    BASE_URL = "https://api.trello.com/1"

    # Trello requires a color when creating a board label; "null" makes
    # a colorless label, which is fine since these labels (P0..P5) carry
    # meaning through their name, not their color.
    DEFAULT_LABEL_COLOR = "null"
    DEFAULT_MAX_RETRY_DELAY = 30.0

    def __init__(
        self,
        key: Optional[str] = None,
        token: Optional[str] = None,
        board_id: Optional[str] = None,
        session=None,
        timeout: float = 30.0,
        max_read_attempts: int = 3,
        max_retry_delay: float = DEFAULT_MAX_RETRY_DELAY,
        sleep_fn=time.sleep,
    ):
        self.key = key or os.environ.get("TRELLO_KEY")
        self.token = token or os.environ.get("TRELLO_TOKEN")
        self.board_id = board_id or os.environ.get("TRELLO_BOARD_ID")
        if not (self.key and self.token and self.board_id):
            raise TrelloError(
                "TRELLO_KEY, TRELLO_TOKEN and TRELLO_BOARD_ID must be set "
                "to use RealTrelloClient"
            )
        if session is None:
            session = requests.Session()
        self._session = session
        # A hung Trello API call must never wedge the scheduler loop
        # forever - every request gets a hard timeout.
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.timeout = timeout
        if isinstance(max_read_attempts, bool) or not isinstance(max_read_attempts, int):
            raise ValueError("max_read_attempts must be a positive integer")
        if max_read_attempts < 1:
            raise ValueError("max_read_attempts must be at least 1")
        self.max_read_attempts = max_read_attempts
        if isinstance(max_retry_delay, bool) or not isinstance(max_retry_delay, (int, float)):
            raise ValueError("max_retry_delay must be a finite non-negative number")
        if not math.isfinite(max_retry_delay) or max_retry_delay < 0:
            raise ValueError("max_retry_delay must be a finite non-negative number")
        self.max_retry_delay = float(max_retry_delay)
        self._sleep = sleep_fn
        self._label_name_to_id: dict[str, str] = {}
        self._board_labels_loaded = False

    def _auth_params(self) -> dict:
        return {"key": self.key, "token": self.token}

    @staticmethod
    def _safe_error_detail(response) -> str:
        """Extract a short, credential-safe detail from a Trello error.

        Trello normally returns a small JSON object such as
        ``{"message": "invalid value for idLabels"}``, which is useful when
        diagnosing unattended writes.  Keep the allow-list deliberately
        narrow and discard suspicious values so scheduler logs cannot echo
        credentials or request URLs.
        """
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""

        details = []
        for key in ("message", "error", "code"):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            value = " ".join(str(value).split())
            if not value or len(value) > 240:
                continue
            lowered = value.lower()
            if any(
                marker in lowered
                for marker in (
                    "token=",
                    "api_key",
                    "apikey",
                    "secret",
                    "authorization:",
                    "webhook",
                )
            ):
                continue
            details.append(f"{key}={value}")
        return "; ".join(details)

    def _request(self, method: str, path: str, **kwargs):
        params = kwargs.pop("params", {}) or {}
        params.update(self._auth_params())
        kwargs.setdefault("timeout", self.timeout)
        attempts = self.max_read_attempts if method.upper() == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self._session.request(
                    method, f"{self.BASE_URL}{path}", params=params, **kwargs
                )
            except requests.RequestException:
                # A failed write may have reached Trello, so only idempotent
                # reads are safe to retry automatically.
                if attempt < attempts:
                    self._sleep(min(float(2 ** (attempt - 1)), self.max_retry_delay))
                    continue
                # Exception text can contain the authenticated prepared URL.
                raise TrelloError(f"Trello API request failed: {method} {path}") from None

            if resp.status_code not in {429, 500, 502, 503, 504} or attempt == attempts:
                break
            self._sleep(min(self._retry_delay(resp, attempt), self.max_retry_delay))
        if resp.status_code >= 400:
            # Error bodies are controlled by Trello or an intermediary and
            # can echo request details.  Include only the narrow,
            # credential-safe allow-list returned above; never include the
            # raw response body or the authenticated URL.
            detail = self._safe_error_detail(resp)
            suffix = f": {detail}" if detail else ""
            raise TrelloError(
                f"Trello API error {resp.status_code}: {method} {path}{suffix}"
            )
        try:
            return resp.json()
        except (TypeError, ValueError):
            # Successful proxies/load balancers can still return HTML or a
            # truncated body.  Treat that as an API failure instead of
            # leaking an implementation-specific JSON decoder exception.
            raise TrelloError(
                f"Trello API returned invalid JSON: {method} {path}"
            ) from None

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        """Honor numeric or HTTP-date Retry-After, else use bounded backoff."""
        raw = getattr(response, "headers", {}).get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                try:
                    deadline = parsedate_to_datetime(raw)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    delay = (deadline - datetime.now(timezone.utc)).total_seconds()
                    return max(0.0, delay)
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(2 ** (attempt - 1), 30))

    @staticmethod
    def _require_payload_type(payload, expected_type: type, operation: str):
        """Reject valid JSON with an unexpected top-level shape.

        Trello list endpoints must return arrays and single-resource
        endpoints must return objects.  Treat proxy/error-envelope responses
        as API failures here instead of leaking a later TypeError/KeyError
        from card synchronization into the unattended scheduler log.
        """
        if not isinstance(payload, expected_type):
            expected = "array" if expected_type is list else "object"
            raise TrelloError(
                f"Trello API returned an invalid payload for {operation}: "
                f"expected {expected}"
            )
        return payload

    @classmethod
    def _require_object_items(cls, payload, operation: str) -> list[dict]:
        """Validate a Trello collection before callers dereference items.

        A gateway can return syntactically valid JSON with the expected array
        root but malformed members (for example ``[null]``).  Convert that to
        the same stable, credential-safe API error as other bad payloads
        instead of leaking an incidental AttributeError/TypeError.
        """
        items = cls._require_payload_type(payload, list, operation)
        if any(not isinstance(item, dict) for item in items):
            raise TrelloError(
                f"Trello API returned an invalid payload for {operation}: "
                "expected an array of objects"
            )
        return items

    def _load_board_labels(self) -> None:
        raw = self._require_object_items(
            self._request("GET", f"/boards/{self.board_id}/labels"),
            "board labels",
        )
        for label in raw:
            name = label.get("name")
            if name:
                label_id = label.get("id")
                if not isinstance(label_id, str) or not label_id:
                    raise TrelloError(
                        "Trello API returned an invalid payload for board labels: "
                        "expected named labels to have a non-empty id"
                    )
                self._label_name_to_id[name] = label_id
        self._board_labels_loaded = True

    def _label_ids_for_names(self, names: Iterable[str]) -> list[str]:
        """Resolve label names (e.g. "P3") to the board's label IDs,
        creating any label that doesn't exist on the board yet - a
        card's ``labels`` field only round-trips by ID, never by name,
        so this is required for priority (or any other label) to
        actually persist onto the real Trello card."""
        names = tuple(names)
        if not names:
            return []

        if not self._board_labels_loaded:
            self._load_board_labels()

        ids = []
        for name in names:
            if name not in self._label_name_to_id:
                created = self._request(
                    "POST",
                    "/labels",
                    data={"name": name, "color": self.DEFAULT_LABEL_COLOR, "idBoard": self.board_id},
                )
                created = self._require_payload_type(created, dict, "created label")
                label_id = created.get("id")
                if not isinstance(label_id, str) or not label_id:
                    raise TrelloError(
                        "Trello API returned an invalid payload for created label: "
                        "expected a non-empty id"
                    )
                self._label_name_to_id[name] = label_id
            ids.append(self._label_name_to_id[name])
        return ids

    @staticmethod
    def _to_card(raw: dict) -> dict:
        card_id = raw.get("id")
        if not isinstance(card_id, str) or not card_id:
            raise TrelloError(
                "Trello API returned an invalid card payload: expected a non-empty id"
            )
        name = raw.get("name")
        desc = raw.get("desc")
        list_id = raw.get("idList")
        if not isinstance(name, str):
            raise TrelloError(
                "Trello API returned an invalid card payload: expected a string name"
            )
        if not isinstance(desc, str):
            raise TrelloError(
                "Trello API returned an invalid card payload: expected a string description"
            )
        if not isinstance(list_id, str) or not list_id:
            raise TrelloError(
                "Trello API returned an invalid card payload: expected a non-empty list id"
            )
        labels = raw.get("labels", [])
        if not isinstance(labels, list) or any(not isinstance(label, dict) for label in labels):
            raise TrelloError(
                "Trello API returned an invalid card payload: expected labels to be an array of objects"
            )
        if any(
            not isinstance(label.get("id"), str)
            or not label["id"]
            or not isinstance(label.get("name"), str)
            for label in labels
        ):
            raise TrelloError(
                "Trello API returned an invalid card payload: "
                "expected labels to have non-empty ids and string names"
            )
        short_url = raw.get("shortUrl")
        card = {
            "id": card_id,
            "name": name,
            "desc": desc,
            "list_id": list_id,
            "labels": [{"id": label.get("id"), "name": label.get("name")} for label in labels],
            "url": short_url if isinstance(short_url, str) and short_url else None,
        }
        if raw.get("pos") is not None:
            card["position"] = raw["pos"]
        if raw.get("dateLastActivity") is not None:
            card["last_activity_at"] = raw["dateLastActivity"]
        return card

    def list_lists(self) -> list[dict]:
        lists = self._require_object_items(
            self._request("GET", f"/boards/{self.board_id}/lists"),
            "board lists",
        )
        if any(
            not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("name"), str)
            for item in lists
        ):
            raise TrelloError(
                "Trello API returned an invalid payload for board lists: "
                "expected non-empty ids and string names"
            )
        return lists

    def get_list_id_by_name(self, name: str) -> Optional[str]:
        for lst in self.list_lists():
            if lst["name"] == name:
                return lst["id"]
        return None

    def list_cards(self, list_id: str) -> list[dict]:
        raw = self._require_object_items(
            self._request("GET", f"/lists/{list_id}/cards"),
            "list cards",
        )
        return [self._to_card(c) for c in raw]

    def get_card(self, card_id: str) -> dict:
        raw = self._require_payload_type(
            self._request("GET", f"/cards/{card_id}"),
            dict,
            "card",
        )
        return self._to_card(raw)

    def create_card(self, list_id: str, name: str, desc: str = "", labels: Optional[list[str]] = None) -> dict:
        _validate_card_description(desc)
        params = {"idList": list_id, "name": name, "desc": desc}
        if labels is not None:
            params["idLabels"] = ",".join(self._label_ids_for_names(labels))
        raw = self._require_payload_type(
            self._request("POST", "/cards", data=params),
            dict,
            "created card",
        )
        return self._to_card(raw)

    def update_card(
        self,
        card_id: str,
        name: Optional[str] = None,
        desc: Optional[str] = None,
        list_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
        position: Optional[str | float] = None,
    ) -> dict:
        if desc is not None:
            _validate_card_description(desc)
        params = {}
        if name is not None:
            params["name"] = name
        if desc is not None:
            params["desc"] = desc
        if list_id is not None:
            params["idList"] = list_id
        if labels is not None:
            params["idLabels"] = ",".join(self._label_ids_for_names(labels))
        if position is not None:
            params["pos"] = position
        raw = self._require_payload_type(
            # Card descriptions can contain the full human-visible DoD plus
            # PM-DATA and easily exceed proxy/request-line limits. Send card
            # fields in the form body; only auth remains in the query string.
            self._request("PUT", f"/cards/{card_id}", data=params),
            dict,
            "updated card",
        )
        return self._to_card(raw)

    def move_card(self, card_id: str, list_id: str) -> dict:
        return self.update_card(card_id, list_id=list_id)

    def archive_card(self, card_id: str) -> dict:
        raw = self._require_payload_type(
            self._request("PUT", f"/cards/{card_id}", data={"closed": "true"}),
            dict,
            "archived card",
        )
        return self._to_card(raw)
