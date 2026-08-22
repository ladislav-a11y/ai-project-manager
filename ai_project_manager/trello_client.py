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

import os
from typing import Iterable, Optional, Protocol


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
    ) -> dict:
        ...

    def move_card(self, card_id: str, list_id: str) -> dict:
        ...


class TrelloError(RuntimeError):
    pass


class InMemoryTrelloClient:
    """In-process fake Trello board. Deterministic, no network calls.

    Used for tests and for running the Project Manager before real
    Trello credentials are wired up.
    """

    def __init__(self, list_names: Iterable[str] = ("Inbox", "New", "Ready", "In Progress", "Paused", "Blocked", "Done", "Error")):
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
        return [dict(c) for c in self._cards.values() if c["list_id"] == list_id]

    def list_all_cards(self) -> list[dict]:
        return [dict(c) for c in self._cards.values()]

    def get_card(self, card_id: str) -> dict:
        if card_id not in self._cards:
            raise TrelloError(f"unknown card {card_id}")
        return dict(self._cards[card_id])

    def create_card(self, list_id: str, name: str, desc: str = "", labels: Optional[list[str]] = None) -> dict:
        if list_id not in self._lists:
            raise TrelloError(f"unknown list {list_id}")
        card_id = f"card-{self._next_card_id}"
        self._next_card_id += 1
        card = {
            "id": card_id,
            "name": name,
            "desc": desc,
            "list_id": list_id,
            "labels": [{"id": f"label-{n}", "name": n} for n in (labels or [])],
        }
        self._cards[card_id] = card
        return dict(card)

    def update_card(
        self,
        card_id: str,
        name: Optional[str] = None,
        desc: Optional[str] = None,
        list_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> dict:
        if card_id not in self._cards:
            raise TrelloError(f"unknown card {card_id}")
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
        return dict(card)

    def move_card(self, card_id: str, list_id: str) -> dict:
        return self.update_card(card_id, list_id=list_id)


class RealTrelloClient:
    """Thin wrapper around the real Trello REST API.

    Configured from environment variables so no secrets live in code:
      TRELLO_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID
    """

    BASE_URL = "https://api.trello.com/1"

    def __init__(
        self,
        key: Optional[str] = None,
        token: Optional[str] = None,
        board_id: Optional[str] = None,
        session=None,
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
            import requests

            session = requests.Session()
        self._session = session

    def _auth_params(self) -> dict:
        return {"key": self.key, "token": self.token}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        params = kwargs.pop("params", {}) or {}
        params.update(self._auth_params())
        resp = self._session.request(method, f"{self.BASE_URL}{path}", params=params, **kwargs)
        if resp.status_code >= 400:
            raise TrelloError(f"Trello API error {resp.status_code}: {resp.text}")
        return resp.json()

    @staticmethod
    def _to_card(raw: dict) -> dict:
        return {
            "id": raw["id"],
            "name": raw.get("name", ""),
            "desc": raw.get("desc", ""),
            "list_id": raw.get("idList"),
            "labels": [{"id": l.get("id"), "name": l.get("name")} for l in raw.get("labels", [])],
        }

    def list_lists(self) -> list[dict]:
        return self._request("GET", f"/boards/{self.board_id}/lists")

    def get_list_id_by_name(self, name: str) -> Optional[str]:
        for lst in self.list_lists():
            if lst["name"] == name:
                return lst["id"]
        return None

    def list_cards(self, list_id: str) -> list[dict]:
        raw = self._request("GET", f"/lists/{list_id}/cards")
        return [self._to_card(c) for c in raw]

    def get_card(self, card_id: str) -> dict:
        raw = self._request("GET", f"/cards/{card_id}")
        return self._to_card(raw)

    def create_card(self, list_id: str, name: str, desc: str = "", labels: Optional[list[str]] = None) -> dict:
        params = {"idList": list_id, "name": name, "desc": desc}
        raw = self._request("POST", "/cards", params=params)
        return self._to_card(raw)

    def update_card(
        self,
        card_id: str,
        name: Optional[str] = None,
        desc: Optional[str] = None,
        list_id: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> dict:
        params = {}
        if name is not None:
            params["name"] = name
        if desc is not None:
            params["desc"] = desc
        if list_id is not None:
            params["idList"] = list_id
        raw = self._request("PUT", f"/cards/{card_id}", params=params)
        return self._to_card(raw)

    def move_card(self, card_id: str, list_id: str) -> dict:
        return self.update_card(card_id, list_id=list_id)
