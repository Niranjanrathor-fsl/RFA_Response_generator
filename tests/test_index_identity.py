"""Document identity and deletion. No live Qdrant."""

from __future__ import annotations

from app.rag.index import _point_id, delete_document


def test_point_id_is_stable_for_the_same_item():
    assert _point_id("item-abc", 3) == _point_id("item-abc", 3)


def test_same_filename_in_different_folders_gets_different_ids():
    """The collision this whole identity change exists to prevent."""
    assert _point_id("item-in-isg", 0) != _point_id("item-in-hfs", 0)


def test_chunk_index_separates_points_within_a_document():
    assert _point_id("item-abc", 0) != _point_id("item-abc", 1)


class _FakeClient:
    def __init__(self):
        self.deleted = []

    def delete(self, collection_name, points_selector):
        self.deleted.append((collection_name, points_selector))


def test_delete_document_filters_on_item_id():
    client = _FakeClient()
    delete_document(client, "kb", "item-abc")
    collection, selector = client.deleted[0]
    assert collection == "kb"
    condition = selector.filter.must[0]
    assert condition.key == "item_id"
    assert condition.match.value == "item-abc"
