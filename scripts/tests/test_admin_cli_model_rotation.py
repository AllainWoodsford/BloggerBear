"""The Admin CLI's `model-rotation get|set`: the shared rotation pool (the `model-rotation` row of
the model-config table, docs/models.md)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import admin_cli


def cli(*argv):
    calls = []
    args = admin_cli.build_parser().parse_args(list(argv))

    def record(_args, method, path, body=None):
        calls.append((method, path, body))

    with patch.object(admin_cli, "_do_request", record):
        args.func(args)
    return calls


def test_get_reads_the_pool():
    assert cli("model-rotation", "get") == [("GET", "/model-rotation", None)]


def test_set_sends_the_ids_stripped_in_the_order_given():
    assert cli("model-rotation", "set", "--candidates", " model-b, model-a ,,model-c") == [
        ("PUT", "/model-rotation", {"candidates": ["model-b", "model-a", "model-c"]})
    ]


def test_an_empty_candidates_clears_the_pool():
    assert cli("model-rotation", "set", "--candidates", "") == [
        ("PUT", "/model-rotation", {"candidates": []})
    ]


def test_set_with_no_candidates_is_refused_before_any_request():
    args = admin_cli.build_parser().parse_args(["model-rotation", "set"])
    with patch.object(admin_cli, "_do_request") as m:
        with pytest.raises(admin_cli.CliError, match="--candidates"):
            args.func(args)
    m.assert_not_called()


def test_the_pool_and_a_topic_s_rotation_list_are_parsed_the_same_way():
    [(_, _, pool)] = cli("model-rotation", "set", "--candidates", "a, b,")
    [(_, _, topic)] = cli("topics", "update", "t", "--model-candidates", "a, b,")

    assert pool["candidates"] == topic["model_id_candidates"] == ["a", "b"]
